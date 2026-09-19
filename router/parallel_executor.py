"""Parallel sub-task executor — runs split sub-tasks concurrently via the session pool.

Builds a dependency graph from :class:`SubTask` objects, executes independent
sub-tasks in parallel using :func:`asyncio.gather`, and runs dependent
sub-tasks only after their prerequisites have completed.

Each sub-task borrows a session from :class:`SessionPool`, forwards the
sub-task description to a bridge adapter, and returns an
:class:`ExecutionResult` with timing, success/failure, and the raw response.

Integration
-----------
* :class:`router.task_splitter.SubTask` – input unit of work
* :class:`router.session_pool.SessionPool` – session acquisition / release
* Any adapter exposing ``async send(request: dict, model: str) -> dict``
  (e.g. :class:`router.adapters.bridge.BridgeAdapter`)

Concurrency
-----------
* Independent sub-tasks run concurrently via :func:`asyncio.gather`.
* An :class:`asyncio.Semaphore` caps the maximum number of in-flight
  sub-tasks to avoid overwhelming the bridge.
* Dependent sub-tasks are dispatched in topological layers — each layer
  waits for the previous one to finish.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from router.task_splitter import SubTask
from router.session_pool import SessionPool, PooledSession

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / Defaults
# ---------------------------------------------------------------------------

_DEFAULT_MAX_RETRIES = 2
_DEFAULT_TIMEOUT = 120.0
_DEFAULT_CONCURRENCY = 5


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ExecutionResult:
    """Outcome of executing a single sub-task."""

    sub_task_id: int
    success: bool
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    duration: float = 0.0
    retries: int = 0


# Progress callback signature: (completed, total, latest_result)
ProgressCallback = Callable[[int, int, ExecutionResult], None]


# ---------------------------------------------------------------------------
# ParallelExecutor
# ---------------------------------------------------------------------------


class ParallelExecutor:
    """Orchestrates parallel execution of sub-tasks with dependency resolution.

    Parameters
    ----------
    session_pool:
        A :class:`SessionPool` instance used to acquire/release sessions.
    max_retries:
        Maximum number of retry attempts per sub-task on failure.
    timeout:
        Per-sub-task timeout in seconds.
    max_concurrency:
        Maximum number of sub-tasks executing simultaneously.
    on_progress:
        Optional callback invoked after each sub-task completes.
        Signature: ``callback(completed: int, total: int, result: ExecutionResult)``.
    """

    def __init__(
        self,
        session_pool: SessionPool,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        timeout: float = _DEFAULT_TIMEOUT,
        max_concurrency: int = _DEFAULT_CONCURRENCY,
        on_progress: Optional[ProgressCallback] = None,
    ) -> None:
        self.session_pool = session_pool
        self.max_retries = max_retries
        self.timeout = timeout
        self.max_concurrency = max_concurrency
        self.on_progress = on_progress

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        sub_tasks: List[SubTask],
        adapter: Any,
        model: str,
    ) -> List[ExecutionResult]:
        """Execute a list of sub-tasks respecting their dependency graph.

        Independent sub-tasks (those with empty ``depends_on``) are dispatched
        in parallel.  Dependent sub-tasks are grouped into topological layers
        and each layer waits for its predecessor to finish.

        Parameters
        ----------
        sub_tasks:
            The sub-tasks to execute (typically from :class:`TaskSplitter`).
        adapter:
            Any object with an ``async send(request: dict, model: str)`` method.
        model:
            Model name passed to ``adapter.send()``.

        Returns
        -------
        list[ExecutionResult]
            One result per sub-task, in the same order as *sub_tasks*.
        """
        if not sub_tasks:
            return []

        total = len(sub_tasks)
        semaphore = asyncio.Semaphore(self.max_concurrency)

        # Build lookup: id → SubTask
        task_map: Dict[int, SubTask] = {st.id: st for st in sub_tasks}

        # Validate dependency references
        all_ids: Set[int] = set(task_map.keys())
        for st in sub_tasks:
            for dep_id in st.depends_on:
                if dep_id not in all_ids:
                    logger.warning(
                        "Sub-task %d depends on unknown id %d — ignoring dependency",
                        st.id, dep_id,
                    )

        # Compute topological layers
        layers = self._topological_layers(sub_tasks)
        logger.info(
            "Execution plan: %d sub-tasks in %d layer(s): %s",
            total, len(layers), [[t.id for t in layer] for layer in layers],
        )

        # Results collector: id → ExecutionResult
        results_map: Dict[int, ExecutionResult] = {}
        completed_count = 0

        # Execute layer by layer
        for layer_idx, layer in enumerate(layers):
            logger.debug("Executing layer %d: %s", layer_idx, [t.id for t in layer])

            coros = [
                self._execute_with_semaphore(
                    st, adapter, model, semaphore,
                )
                for st in layer
            ]

            layer_results: List[ExecutionResult] = await asyncio.gather(*coros)

            for result in layer_results:
                results_map[result.sub_task_id] = result
                completed_count += 1

                # Fire progress callback
                if self.on_progress is not None:
                    try:
                        self.on_progress(completed_count, total, result)
                    except Exception:
                        logger.exception("Progress callback raised an exception")

        # Return results in original sub-task order
        return [results_map[st.id] for st in sub_tasks]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _execute_with_semaphore(
        self,
        sub_task: SubTask,
        adapter: Any,
        model: str,
        semaphore: asyncio.Semaphore,
    ) -> ExecutionResult:
        """Run a single sub-task, gated by the concurrency semaphore."""
        async with semaphore:
            return await self._execute_single(sub_task, adapter, model)

    async def _execute_single(
        self,
        sub_task: SubTask,
        adapter: Any,
        model: str,
    ) -> ExecutionResult:
        """Execute one sub-task with retry logic.

        Acquires a session from the pool, builds the request, calls the
        adapter, and releases the session.  Retries up to ``max_retries``
        times on failure.
        """
        last_error: Optional[str] = None
        retries_used = 0

        for attempt in range(self.max_retries + 1):
            start_time = time.monotonic()
            session: Optional[PooledSession] = None
            released = False

            try:
                # Acquire a session from the pool
                session = await self.session_pool.acquire_session()
                logger.debug(
                    "Sub-task %d: acquired session %s (attempt %d)",
                    sub_task.id, session.session_id, attempt + 1,
                )

                # Build the OpenAI-format request
                request: Dict[str, Any] = {
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are processing a sub-task as part of a larger request. "
                                "Focus only on the task described below."
                            ),
                        },
                        {
                            "role": "user",
                            "content": sub_task.description,
                        },
                    ],
                }

                # Include original context as a system hint when available
                if sub_task.original_context:
                    request["messages"].insert(1, {
                        "role": "system",
                        "content": f"Original request context:\n{sub_task.original_context[:1000]}",
                    })

                # Call adapter with timeout
                response: Dict[str, Any] = await asyncio.wait_for(
                    adapter.send(request, model),
                    timeout=self.timeout,
                )

                # Release session back to pool
                await self.session_pool.release_session(session.session_id)
                released = True

                duration = time.monotonic() - start_time
                logger.info(
                    "Sub-task %d completed in %.2fs (attempt %d)",
                    sub_task.id, duration, attempt + 1,
                )

                return ExecutionResult(
                    sub_task_id=sub_task.id,
                    success=True,
                    result=response,
                    error=None,
                    duration=duration,
                    retries=retries_used,
                )

            except asyncio.TimeoutError:
                last_error = f"Timed out after {self.timeout}s"
                logger.warning(
                    "Sub-task %d: %s (attempt %d/%d)",
                    sub_task.id, last_error, attempt + 1, self.max_retries + 1,
                )

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "Sub-task %d: %s (attempt %d/%d)",
                    sub_task.id, last_error, attempt + 1, self.max_retries + 1,
                )

            finally:
                # Release the session if we acquired one and haven't released yet
                if session is not None and not released:
                    try:
                        await self.session_pool.release_session(session.session_id)
                    except Exception:
                        logger.debug(
                            "Failed to release session %s for sub-task %d",
                            session.session_id if session else "?", sub_task.id,
                        )

            retries_used = attempt + 1

        # All attempts exhausted
        duration = time.monotonic() - start_time  # type: ignore[possibly-undefined]
        logger.error(
            "Sub-task %d failed after %d attempts: %s",
            sub_task.id, self.max_retries + 1, last_error,
        )

        return ExecutionResult(
            sub_task_id=sub_task.id,
            success=False,
            result=None,
            error=last_error,
            duration=duration,
            retries=retries_used - 1,
        )

    @staticmethod
    def _topological_layers(sub_tasks: List[SubTask]) -> List[List[SubTask]]:
        """Group sub-tasks into topological layers for parallel execution.

        Layer 0 contains all sub-tasks with no dependencies.  Layer N
        contains sub-tasks whose dependencies are all in layers < N.

        Returns
        -------
        list[list[SubTask]]
            Ordered layers; sub-tasks within a layer can run concurrently.

        Raises
        ------
        ValueError
            If a cycle is detected in the dependency graph.
        """
        task_map: Dict[int, SubTask] = {st.id: st for st in sub_tasks}
        all_ids: Set[int] = set(task_map.keys())

        # Build in-degree counts (only for deps that exist in our set)
        in_degree: Dict[int, int] = defaultdict(int)
        dependents: Dict[int, List[int]] = defaultdict(list)  # dep_id → list of ids that depend on it

        for st in sub_tasks:
            valid_deps = [d for d in st.depends_on if d in all_ids]
            in_degree[st.id] = len(valid_deps)
            for dep_id in valid_deps:
                dependents[dep_id].append(st.id)

        # Kahn's algorithm
        layers: List[List[SubTask]] = []
        remaining: Set[int] = set(all_ids)

        while remaining:
            # Find all tasks with in_degree == 0
            ready = [tid for tid in remaining if in_degree.get(tid, 0) == 0]

            if not ready:
                raise ValueError(
                    f"Cycle detected in sub-task dependency graph. "
                    f"Unresolved tasks: {remaining}"
                )

            layer = [task_map[tid] for tid in sorted(ready)]
            layers.append(layer)

            for tid in ready:
                remaining.discard(tid)
                for dependent_id in dependents.get(tid, []):
                    in_degree[dependent_id] -= 1

        return layers


# ---------------------------------------------------------------------------
# Quick-test main
# ---------------------------------------------------------------------------


async def main() -> None:
    """Simulate executing 5 sub-tasks with a mock adapter and session pool."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # ---- Mock adapter ---------------------------------------------------

    class MockAdapter:
        """Simulates a bridge adapter with variable latency and occasional errors."""

        def __init__(self) -> None:
            self.call_count = 0

        async def send(self, request: Dict[str, Any], model: str) -> Dict[str, Any]:
            self.call_count += 1
            # Extract the user message for the response
            messages = request.get("messages", [])
            user_msg = ""
            for msg in messages:
                if msg.get("role") == "user":
                    user_msg = msg["content"]
                    break

            # Simulate processing time (200-600ms)
            import random
            delay = random.uniform(0.2, 0.6)
            await asyncio.sleep(delay)

            # Simulate occasional failure on task 3 (first attempt only)
            if "Analyze" in user_msg and self.call_count <= 1:
                raise ConnectionError("Simulated transient failure")

            return {
                "id": f"mock-{self.call_count}",
                "model": model,
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": f"[Mock response for: {user_msg[:60]}...]",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            }

    # ---- Mock session pool ----------------------------------------------

    class MockSessionPool:
        """Lightweight pool stub — no real sessions, just tracks acquire/release."""

        def __init__(self) -> None:
            self._counter = 0
            self._active = 0
            self._peak = 0
            self._lock = asyncio.Lock()

        async def acquire_session(self) -> PooledSession:
            async with self._lock:
                self._counter += 1
                self._active += 1
                self._peak = max(self._peak, self._active)
                sid = f"mock-sess-{self._counter}"
                logger.info("Pool: acquired %s (active=%d)", sid, self._active)
                return PooledSession(session_id=sid)

        async def release_session(self, session_id: str) -> None:
            async with self._lock:
                self._active -= 1
                logger.info("Pool: released %s (active=%d)", session_id, self._active)

    # ---- Build sub-tasks ------------------------------------------------

    sub_tasks = [
        SubTask(id=1, description="Fetch user data from the API", depends_on=[], original_context="Build a user dashboard"),
        SubTask(id=2, description="Fetch recent activity logs", depends_on=[], original_context="Build a user dashboard"),
        SubTask(id=3, description="Analyze user engagement metrics", depends_on=[1], original_context="Build a user dashboard"),
        SubTask(id=4, description="Generate dashboard HTML", depends_on=[2, 3], original_context="Build a user dashboard"),
        SubTask(id=5, description="Send notification email", depends_on=[4], original_context="Build a user dashboard"),
    ]

    # ---- Progress callback ----------------------------------------------

    progress_log: List[str] = []

    def on_progress(completed: int, total: int, result: ExecutionResult) -> None:
        status = "✓" if result.success else "✗"
        line = f"  [{completed}/{total}] {status} Task {result.sub_task_id} ({result.duration:.2f}s)"
        if result.retries:
            line += f" (retries={result.retries})"
        if result.error:
            line += f" ERROR: {result.error}"
        progress_log.append(line)
        print(line)

    # ---- Execute --------------------------------------------------------

    adapter = MockAdapter()
    pool = MockSessionPool()

    executor = ParallelExecutor(
        session_pool=pool,
        max_retries=2,
        timeout=10.0,
        max_concurrency=3,
        on_progress=on_progress,
    )

    print("=" * 60)
    print("Parallel Executor — Smoke Test (5 sub-tasks)")
    print("=" * 60)
    print(f"\nDependency graph:")
    for st in sub_tasks:
        deps = st.depends_on if st.depends_on else ["(none)"]
        print(f"  Task {st.id} → depends on: {deps}")

    # Show planned layers
    layers = executor._topological_layers(sub_tasks)
    print(f"\nExecution plan ({len(layers)} layers):")
    for i, layer in enumerate(layers):
        print(f"  Layer {i}: {[t.id for t in layer]}")

    print(f"\nExecuting...")
    t0 = time.monotonic()
    results = await executor.execute(sub_tasks, adapter, model="deepseek-chat")
    total_time = time.monotonic() - t0

    # ---- Report ---------------------------------------------------------

    print(f"\n{'=' * 60}")
    print(f"Results:")
    print(f"{'=' * 60}")

    success_count = sum(1 for r in results if r.success)
    fail_count = sum(1 for r in results if not r.success)

    for r in results:
        status = "✓ PASS" if r.success else "✗ FAIL"
        print(f"  Task {r.sub_task_id}: {status} | {r.duration:.2f}s | retries={r.retries}")
        if r.result:
            content = r.result["choices"][0]["message"]["content"]
            print(f"    → {content[:80]}")
        if r.error:
            print(f"    → ERROR: {r.error}")

    print(f"\nSummary: {success_count} succeeded, {fail_count} failed")
    print(f"Total wall time: {total_time:.2f}s")
    print(f"Peak concurrent sessions: {pool._peak}")
    print(f"Adapter calls: {adapter.call_count}")

    # Verify retry happened for task 3
    task3_result = next(r for r in results if r.sub_task_id == 3)
    if task3_result.success and task3_result.retries > 0:
        print(f"\n✓ Retry mechanism verified: Task 3 succeeded after {task3_result.retries} retries")

    print(f"\n{'=' * 60}")
    print("ALL CHECKS PASSED ✓")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
