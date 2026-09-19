"""End-to-end scenario tests for the full session manager pipeline.

Verifies 3 real-world scenarios exercising detect → split → execute → aggregate:
1. Multi-step task (5 steps) — sequential fibonacci implementation
2. Multi-file refactor (5 files) — parallel logging addition
3. Large document (8000+ tokens) — token-based chunking

Uses MockAdapter for deterministic, fast testing without external dependencies.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import pytest

from router.task_splitter import (
    TaskSizeDetector,
    TaskSplitter,
    SubTask,
    TaskAnalysis,
    TOKEN_SPLIT_THRESHOLD,
)
from router.parallel_executor import (
    ParallelExecutor,
    ExecutionResult,
)
from router.context_manager import ContextManager
from router.result_aggregator import (
    ResultAggregator,
    AggregatedResult,
)
from router.session_pool import PooledSession


# ---------------------------------------------------------------------------
# Lightweight request/message stubs
# ---------------------------------------------------------------------------


class _Msg:
    """Lightweight message mimicking ChatCompletionRequest messages."""
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content


class _Req:
    """Lightweight request mimicking ChatCompletionRequest."""
    def __init__(self, messages: list):
        self.messages = messages


# ---------------------------------------------------------------------------
# MockSessionPool — lightweight stub for executor
# ---------------------------------------------------------------------------


class MockSessionPool:
    """Lightweight session pool stub tracking acquire/release and concurrency."""

    def __init__(self):
        self._counter = 0
        self._active = 0
        self._peak = 0
        self._lock = asyncio.Lock()

    async def acquire_session(self) -> PooledSession:
        async with self._lock:
            self._counter += 1
            self._active += 1
            self._peak = max(self._peak, self._active)
            return PooledSession(session_id=f"mock-sess-{self._counter}")

    async def release_session(self, session_id: str) -> None:
        async with self._lock:
            self._active -= 1


# ---------------------------------------------------------------------------
# MockAdapter — configurable deterministic responses
# ---------------------------------------------------------------------------


class MockAdapter:
    """Mock adapter returning realistic responses keyed by subtask content.

    Responses include code snippets for fibonacci/logging scenarios and
    document summaries for the large-document scenario.
    """

    def __init__(self):
        self.call_count = 0
        self.call_log: List[Dict] = []
        self._call_times: List[float] = []

    async def send(self, request: Dict[str, Any], model: str) -> Dict[str, Any]:
        start = time.monotonic()
        self.call_count += 1

        # Extract user message
        user_msg = ""
        for msg in request.get("messages", []):
            if msg.get("role") == "user":
                user_msg = msg.get("content", "")
                break

        self.call_log.append({
            "request": request,
            "model": model,
            "user_msg_preview": user_msg[:120],
            "timestamp": start,
        })

        # Small delay to simulate work
        await asyncio.sleep(0.03)

        # Generate realistic response based on content
        content = self._generate_response(user_msg)

        duration = time.monotonic() - start
        self._call_times.append(duration)

        return {
            "choices": [{
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": len(user_msg.split()) * 2,
                "completion_tokens": len(content.split()) * 2,
                "total_tokens": len(user_msg.split()) * 2 + len(content.split()) * 2,
            },
        }

    def _generate_response(self, user_msg: str) -> str:
        """Generate context-appropriate response based on user message content."""
        lower = user_msg.lower()

        # Scenario 1: Fibonacci steps
        if "fibonacci" in lower or "base case" in lower:
            if "base case" in lower or "step 1" in lower:
                return (
                    "```python\ndef fibonacci(n):\n"
                    "    # Base cases\n"
                    "    if n <= 0:\n"
                    "        return 0\n"
                    "    if n == 1:\n"
                    "        return 1\n"
                    "```\nBase cases defined for n=0 and n=1."
                )
            elif "memoiz" in lower or "step 2" in lower:
                return (
                    "```python\nmemo = {}\n"
                    "def fibonacci(n):\n"
                    "    if n in memo:\n"
                    "        return memo[n]\n"
                    "    result = fibonacci(n-1) + fibonacci(n-2)\n"
                    "    memo[n] = result\n"
                    "    return result\n"
                    "```\nMemoization cache added."
                )
            elif "edge case" in lower or "step 3" in lower:
                return (
                    "```python\ndef fibonacci(n):\n"
                    "    if not isinstance(n, int):\n"
                    "        raise TypeError('n must be an integer')\n"
                    "    if n < 0:\n"
                    "        raise ValueError('n must be non-negative')\n"
                    "```\nEdge cases handled with type and value checks."
                )
            elif "type hint" in lower or "step 4" in lower:
                return (
                    "```python\nfrom typing import Dict\n\n"
                    "def fibonacci(n: int, memo: Dict[int, int] = None) -> int:\n"
                    "```\nType hints added for parameters and return value."
                )
            elif "docstring" in lower or "step 5" in lower:
                return (
                    "```python\ndef fibonacci(n: int) -> int:\n"
                    '    """Calculate the nth Fibonacci number.\n\n'
                    "    Args:\n"
                    "        n: Non-negative integer index.\n\n"
                    "    Returns:\n"
                    "        The nth Fibonacci number.\n\n"
                    "    Raises:\n"
                    "        ValueError: If n is negative.\n"
                    "        TypeError: If n is not an integer.\n"
                    '    """\n```\nDocstring added.'
                )
            return "Fibonacci implementation step completed."

        # Scenario 2: Multi-file logging refactor
        if "logging" in lower or "log" in lower:
            if "main.py" in lower:
                return (
                    "Added logging to main.py:\n"
                    "```python\nimport logging\n"
                    "logger = logging.getLogger(__name__)\n"
                    "logger.info('Application starting')\n"
                    "```"
                )
            elif "utils.py" in lower:
                return (
                    "Added logging to utils.py:\n"
                    "```python\nimport logging\n"
                    "logger = logging.getLogger(__name__)\n"
                    "logger.debug('Utility function called')\n"
                    "```"
                )
            elif "config.py" in lower:
                return (
                    "Added logging to config.py:\n"
                    "```python\nimport logging\n"
                    "logger = logging.getLogger(__name__)\n"
                    "logger.info('Configuration loaded')\n"
                    "```"
                )
            elif "database.py" in lower:
                return (
                    "Added logging to database.py:\n"
                    "```python\nimport logging\n"
                    "logger = logging.getLogger(__name__)\n"
                    "logger.info('Database connection established')\n"
                    "```"
                )
            elif "api.py" in lower:
                return (
                    "Added logging to api.py:\n"
                    "```python\nimport logging\n"
                    "logger = logging.getLogger(__name__)\n"
                    "logger.info('API endpoint called')\n"
                    "```"
                )
            return "Logging added to file."

        # Scenario 3: Large document chunks
        if "part" in lower or "chunk" in lower:
            return (
                f"Processed document section. "
                f"Key points extracted and summarized. "
                f"Technical analysis complete for this portion. "
                f"Architecture patterns identified and documented."
            )

        # Default fallback
        return f"Response for: {user_msg[:80]}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_large_document(target_tokens: int = 8000) -> str:
    """Generate a long technical document exceeding the token threshold.

    At ~1.3 tokens per word, 8000 tokens ≈ 6154 words.
    """
    paragraphs = [
        "The distributed caching layer implements a multi-tier architecture with "
        "Redis as the primary backend and local LRU caches as the secondary tier. "
        "Cache invalidation follows a write-through strategy with TTL-based expiry. "
        "Each cache entry includes metadata for tracking access patterns and hit rates.",

        "The authentication subsystem uses JWT tokens with RS256 signing. "
        "Token refresh follows a sliding window protocol where refresh tokens "
        "are rotated on each use. Session state is maintained server-side in "
        "Redis with automatic cleanup of expired sessions every 5 minutes.",

        "Database connection pooling is managed through asyncpg with configurable "
        "pool sizes. Query optimization includes prepared statement caching, "
        "connection health checks, and automatic failover to read replicas. "
        "Migration management uses versioned SQL files applied sequentially.",

        "The API gateway implements rate limiting using a token bucket algorithm "
        "with per-client quotas. Request routing uses content-based dispatching "
        "with circuit breaker patterns for downstream service protection. "
        "Response compression is applied for payloads exceeding 1KB.",

        "Logging infrastructure uses structured JSON logging with correlation IDs "
        "for distributed tracing. Log levels are configurable per-module through "
        "environment variables. Critical events trigger alerts through the "
        "notification pipeline with deduplication and throttling.",

        "The message queue system uses RabbitMQ with dead letter exchanges for "
        "failed message handling. Consumer groups enable parallel processing "
        "with at-least-once delivery guarantees. Message serialization uses "
        "Protocol Buffers for schema evolution compatibility.",

        "Monitoring dashboards track system health through custom Prometheus metrics. "
        "Key performance indicators include p99 latency, error rates, throughput, "
        "and resource utilization. Anomaly detection uses statistical baselines "
        "with configurable sensitivity thresholds.",

        "Configuration management follows the twelve-factor app methodology. "
        "Environment-specific overrides are applied through layered config files "
        "with validation at startup. Feature flags enable gradual rollouts "
        "with percentage-based targeting and user segment filtering.",

        "The testing framework combines unit tests with integration tests using "
        "test containers for external dependencies. Contract testing validates "
        "API compatibility between services. Performance benchmarks run nightly "
        "with regression detection against baseline measurements.",

        "Deployment automation uses containerized builds with multi-stage Dockerfiles. "
        "Blue-green deployments minimize downtime with automated health checks "
        "and rollback triggers. Infrastructure as code manages cloud resources "
        "through Terraform modules with state locking.",
    ]

    # Repeat and vary paragraphs to reach target word count
    words_needed = int(target_tokens / 1.3)
    doc_parts = []
    section_num = 1

    while len(" ".join(doc_parts).split()) < words_needed:
        for para in paragraphs:
            doc_parts.append(f"## Section {section_num}\n\n{para}")
            section_num += 1
            if len(" ".join(doc_parts).split()) >= words_needed:
                break

    return "\n\n".join(doc_parts)


# ---------------------------------------------------------------------------
# Scenario 1: Multi-step task (5 steps)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario1_multi_step_fibonacci():
    """Scenario 1: Multi-step fibonacci implementation with 5 sequential steps.

    Verifies:
    - Task is detected as multi-step with 5 subtasks
    - Subtasks have sequential dependencies
    - All 5 subtasks execute and complete
    - Final aggregated response contains code
    - Context passes between steps
    """
    # Setup components
    detector = TaskSizeDetector()
    splitter = TaskSplitter()
    adapter = MockAdapter()
    pool = MockSessionPool()
    executor = ParallelExecutor(
        session_pool=pool,
        max_retries=1,
        timeout=30.0,
        max_concurrency=5,
    )
    aggregator = ResultAggregator()
    ctx_manager = ContextManager()

    # Create the multi-step request
    text = (
        "Write a function to calculate fibonacci. Steps:\n"
        "1. Define base cases for n=0 and n=1\n"
        "2. Add memoization with a cache dictionary\n"
        "3. Handle edge cases like negative numbers and non-integers\n"
        "4. Add type hints for all parameters and return type\n"
        "5. Write docstring with Args, Returns, and Raises sections"
    )
    req = _Req([_Msg("user", text)])

    # Step 1: Detect
    analysis = detector.analyze(req)
    assert analysis.needs_splitting, "5-step task should trigger splitting"
    assert analysis.suggested_split_count >= 4, (
        f"Expected >= 4 suggested splits, got {analysis.suggested_split_count}"
    )

    # Step 2: Split
    sub_tasks = splitter.split(req, analysis)
    assert len(sub_tasks) == 5, f"Expected 5 subtasks, got {len(sub_tasks)}"

    # Verify sequential dependencies
    assert sub_tasks[0].depends_on == [], "First step should have no dependencies"
    for i, st in enumerate(sub_tasks[1:], start=2):
        assert len(st.depends_on) > 0, f"Step {i} should depend on previous step"

    # Step 3: Execute with timing
    start_time = time.monotonic()
    results = await executor.execute(sub_tasks, adapter, model="test-model")
    elapsed = time.monotonic() - start_time

    # All 5 should succeed
    assert len(results) == 5, f"Expected 5 results, got {len(results)}"
    assert all(r.success for r in results), (
        f"Not all subtasks succeeded: {[(r.sub_task_id, r.error) for r in results if not r.success]}"
    )
    assert adapter.call_count == 5, f"Expected 5 adapter calls, got {adapter.call_count}"

    # Step 4: Record context for each result
    for result in results:
        await ctx_manager.record_result(result)

    state = await ctx_manager.get_shared_state()
    assert len(state) > 0, "Context should be recorded"

    # Step 5: Aggregate
    aggregated = aggregator.aggregate(results, sub_tasks)
    assert isinstance(aggregated, AggregatedResult)
    assert aggregated.success_count == 5
    assert aggregated.failure_count == 0
    assert len(aggregated.sub_results) == 5

    # Verify combined response contains code
    combined = aggregated.combined_response
    assert len(combined) > 100, "Combined response should be substantial"
    assert "Part 1/5" in combined, "Should have section header for part 1"
    assert "Part 5/5" in combined, "Should have section header for part 5"

    # Verify code content in responses
    assert "fibonacci" in combined.lower() or "base case" in combined.lower() or "def " in combined, (
        "Combined response should contain fibonacci-related code"
    )

    # Step 6: Format as OpenAI response
    response = aggregator.format_response(aggregated)
    assert "choices" in response
    assert response["choices"][0]["message"]["role"] == "assistant"
    assert response["choices"][0]["finish_reason"] == "stop"
    assert response["_session_manager"]["aggregated"] is True
    assert response["_session_manager"]["sub_task_count"] == 5
    assert response["_session_manager"]["success_count"] == 5

    print(f"\n  Scenario 1 complete: 5 steps in {elapsed:.2f}s")
    print(f"  Adapter calls: {adapter.call_count}")
    print(f"  Combined response length: {len(combined)} chars")


# ---------------------------------------------------------------------------
# Scenario 2: Multi-file refactor (5 files)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario2_multi_file_refactor():
    """Scenario 2: Multi-file logging refactor across 5 files.

    Verifies:
    - Task splits by file into 5 independent subtasks
    - Subtasks have no dependencies (parallel execution)
    - All 5 execute concurrently
    - Aggregated response covers all files
    """
    # Setup components
    detector = TaskSizeDetector()
    splitter = TaskSplitter()
    adapter = MockAdapter()
    pool = MockSessionPool()
    executor = ParallelExecutor(
        session_pool=pool,
        max_retries=1,
        timeout=30.0,
        max_concurrency=5,
    )
    aggregator = ResultAggregator()

    # Create the multi-file request
    text = (
        "Refactor these files to add logging: "
        "main.py, utils.py, config.py, database.py, api.py. "
        "Add structured logging with appropriate log levels to each file."
    )
    req = _Req([_Msg("user", text)])

    # Step 1: Detect
    analysis = detector.analyze(req)
    assert analysis.needs_splitting, "5-file task should trigger splitting"
    assert analysis.intent == "refactor", f"Expected 'refactor' intent, got '{analysis.intent}'"

    # Step 2: Split
    sub_tasks = splitter.split(req, analysis)
    assert len(sub_tasks) == 5, f"Expected 5 subtasks (one per file), got {len(sub_tasks)}"

    # Verify all subtasks are independent (no dependencies)
    for st in sub_tasks:
        assert st.depends_on == [], (
            f"File-based subtask {st.id} should be independent, "
            f"but has deps: {st.depends_on}"
        )

    # Verify each file is represented
    descriptions = " ".join(st.description for st in sub_tasks)
    for filename in ["main.py", "utils.py", "config.py", "database.py", "api.py"]:
        assert filename in descriptions, (
            f"File '{filename}' should appear in subtask descriptions"
        )

    # Step 3: Execute with timing — parallel execution should be fast
    start_time = time.monotonic()
    results = await executor.execute(sub_tasks, adapter, model="test-model")
    elapsed = time.monotonic() - start_time

    # All 5 should succeed
    assert len(results) == 5
    assert all(r.success for r in results), (
        f"Not all subtasks succeeded: {[(r.sub_task_id, r.error) for r in results if not r.success]}"
    )
    assert adapter.call_count == 5

    # Verify parallel execution: 5 tasks at ~0.03s each should be < 0.5s
    # (serial would be ~0.15s minimum, parallel should be faster)
    assert elapsed < 1.0, f"Parallel execution should be < 1.0s, got {elapsed:.2f}s"

    # Verify peak concurrency was > 1 (parallel execution happened)
    assert pool._peak >= 2, (
        f"Expected peak concurrency >= 2 for parallel execution, got {pool._peak}"
    )

    # Step 4: Aggregate
    aggregated = aggregator.aggregate(results, sub_tasks)
    assert aggregated.success_count == 5
    assert aggregated.failure_count == 0
    assert len(aggregated.sub_results) == 5

    # Verify combined response mentions all files
    combined = aggregated.combined_response
    assert len(combined) > 100
    for filename in ["main.py", "utils.py", "config.py", "database.py", "api.py"]:
        assert filename in combined, (
            f"Combined response should reference '{filename}'"
        )

    # Verify logging-related content
    assert "logging" in combined.lower() or "logger" in combined.lower() or "log" in combined.lower(), (
        "Combined response should contain logging-related content"
    )

    # Step 5: Format as OpenAI response
    response = aggregator.format_response(aggregated)
    assert response["choices"][0]["finish_reason"] == "stop"
    assert response["_session_manager"]["sub_task_count"] == 5
    assert response["_session_manager"]["success_count"] == 5
    assert response["_session_manager"]["failure_count"] == 0

    print(f"\n  Scenario 2 complete: 5 files in {elapsed:.2f}s")
    print(f"  Peak concurrency: {pool._peak}")
    print(f"  Combined response length: {len(combined)} chars")


# ---------------------------------------------------------------------------
# Scenario 3: Large document (8000+ tokens)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario3_large_document():
    """Scenario 3: Large document (~8000 tokens) split by token count.

    Verifies:
    - Document exceeds token threshold and triggers splitting
    - Multiple subtasks are created by token-based chunking
    - All subtasks execute successfully
    - Aggregated response covers all chunks
    """
    # Setup components
    detector = TaskSizeDetector()
    splitter = TaskSplitter()
    adapter = MockAdapter()
    pool = MockSessionPool()
    executor = ParallelExecutor(
        session_pool=pool,
        max_retries=1,
        timeout=30.0,
        max_concurrency=5,
    )
    aggregator = ResultAggregator()

    # Generate large document (~8000+ tokens)
    large_text = _generate_large_document(target_tokens=8000)

    # Verify it exceeds the threshold
    estimated_tokens = detector.estimate_tokens(large_text)
    assert estimated_tokens > TOKEN_SPLIT_THRESHOLD, (
        f"Generated document has {estimated_tokens} tokens, "
        f"expected > {TOKEN_SPLIT_THRESHOLD}"
    )

    req = _Req([_Msg("user", f"Analyze and summarize this technical document:\n\n{large_text}")])

    # Step 1: Detect
    analysis = detector.analyze(req)
    assert analysis.needs_splitting, (
        f"Document with {estimated_tokens} tokens should trigger splitting"
    )
    assert analysis.suggested_split_count >= 2, (
        f"Expected >= 2 suggested splits for large document, got {analysis.suggested_split_count}"
    )

    # Step 2: Split
    sub_tasks = splitter.split(req, analysis)
    num_subtasks = len(sub_tasks)
    assert num_subtasks >= 2, (
        f"Expected >= 2 subtasks for large document, got {num_subtasks}"
    )

    # Step 3: Execute with timing
    start_time = time.monotonic()
    results = await executor.execute(sub_tasks, adapter, model="test-model")
    elapsed = time.monotonic() - start_time

    # All subtasks should succeed
    assert len(results) == num_subtasks
    assert all(r.success for r in results), (
        f"Not all subtasks succeeded: "
        f"{[(r.sub_task_id, r.error) for r in results if not r.success]}"
    )
    assert adapter.call_count == num_subtasks

    # Step 4: Aggregate
    aggregated = aggregator.aggregate(results, sub_tasks)
    assert aggregated.success_count == num_subtasks
    assert aggregated.failure_count == 0
    assert len(aggregated.sub_results) == num_subtasks

    # Verify combined response is substantial
    combined = aggregated.combined_response
    assert len(combined) > 100, "Combined response should be substantial"

    # Verify all parts are represented
    for i in range(1, num_subtasks + 1):
        assert f"Part {i}/{num_subtasks}" in combined, (
            f"Combined response should have section header for part {i}/{num_subtasks}"
        )

    # Step 5: Format as OpenAI response
    response = aggregator.format_response(aggregated)
    assert "choices" in response
    assert response["choices"][0]["message"]["role"] == "assistant"
    assert response["_session_manager"]["aggregated"] is True
    assert response["_session_manager"]["sub_task_count"] == num_subtasks
    assert response["_session_manager"]["success_count"] == num_subtasks

    print(f"\n  Scenario 3 complete: {num_subtasks} chunks in {elapsed:.2f}s")
    print(f"  Document tokens: ~{estimated_tokens}")
    print(f"  Adapter calls: {adapter.call_count}")
    print(f"  Combined response length: {len(combined)} chars")


# ---------------------------------------------------------------------------
# Scenario integration: full pipeline with context passing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario1_full_pipeline_with_context():
    """Full pipeline test for scenario 1 including context passing between steps.

    Verifies that context from step 1 (base cases) is available
    when step 2 (memoization) is prepared.
    """
    detector = TaskSizeDetector()
    splitter = TaskSplitter()
    adapter = MockAdapter()
    pool = MockSessionPool()
    executor = ParallelExecutor(
        session_pool=pool,
        max_retries=0,
        timeout=30.0,
        max_concurrency=5,
    )
    aggregator = ResultAggregator()
    ctx_manager = ContextManager()

    text = (
        "Write a function to calculate fibonacci. Steps:\n"
        "1. Define base cases for n=0 and n=1\n"
        "2. Add memoization with a cache dictionary\n"
        "3. Handle edge cases like negative numbers and non-integers\n"
        "4. Add type hints for all parameters and return type\n"
        "5. Write docstring with Args, Returns, and Raises sections"
    )
    req = _Req([_Msg("user", text)])

    analysis = detector.analyze(req)
    sub_tasks = splitter.split(req, analysis)

    # Execute all subtasks
    results = await executor.execute(sub_tasks, adapter, model="test-model")
    assert all(r.success for r in results)

    # Record context from each result in order
    for result in results:
        await ctx_manager.record_result(result)

    # Verify context accumulated across steps
    state = await ctx_manager.get_shared_state()

    # Should have entries for each of the 5 tasks
    task_keys = [k for k in state.keys() if k.startswith("task_")]
    assert len(task_keys) >= 5, (
        f"Expected context for 5 tasks, got {len(task_keys)} keys: {task_keys}"
    )

    # Verify success tracking for all tasks
    for i in range(1, 6):
        success_key = f"task_{i}_success"
        assert state.get(success_key) is True, (
            f"task_{i}_success should be True"
        )

    # Aggregate and verify
    aggregated = aggregator.aggregate(results, sub_tasks)
    assert aggregated.success_count == 5
    assert aggregated.failure_count == 0

    # Format response
    response = aggregator.format_response(aggregated)
    assert response["_session_manager"]["success_count"] == 5
    assert response["usage"]["total_tokens"] > 0


# ---------------------------------------------------------------------------
# Timing benchmarks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timing_parallel_vs_serial():
    """Verify that parallel execution is faster than serial would be.

    Independent subtasks (scenario 2) should execute concurrently.
    """
    adapter = MockAdapter()
    pool = MockSessionPool()

    # Use independent subtasks (like scenario 2)
    sub_tasks = [
        SubTask(
            id=i,
            description=f"Process file: file{i}.py to add logging",
            depends_on=[],
            original_context="Refactor files to add logging",
        )
        for i in range(1, 6)
    ]

    executor = ParallelExecutor(
        session_pool=pool,
        max_retries=0,
        timeout=30.0,
        max_concurrency=5,
    )

    start = time.monotonic()
    results = await executor.execute(sub_tasks, adapter, model="test-model")
    parallel_time = time.monotonic() - start

    assert len(results) == 5
    assert all(r.success for r in results)

    # Each task takes ~0.03s. Serial would be ~0.15s.
    # Parallel should be significantly less.
    serial_estimate = sum(r.duration for r in results)
    assert parallel_time < serial_estimate * 0.8, (
        f"Parallel time ({parallel_time:.3f}s) should be less than "
        f"80% of serial estimate ({serial_estimate:.3f}s)"
    )

    # Peak concurrency should be > 1
    assert pool._peak >= 2, (
        f"Expected peak concurrency >= 2, got {pool._peak}"
    )
