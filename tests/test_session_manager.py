"""End-to-end tests for the Session Manager system (Task 4.2).

Tests the full session manager flow with mocked adapters covering:
- Task detection and splitting
- Parallel execution with dependencies
- Context passing between sub-tasks
- Result aggregation (success and partial failure)
- Session pool rotation
- Adaptive config management
- Full pipeline integration
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from router.task_splitter import (
    TaskSizeDetector,
    TaskSplitter,
    HybridTaskSplitter,
    SubTask,
    TaskAnalysis,
    TOKEN_SPLIT_THRESHOLD,
)
from router.session_pool import (
    SessionPool,
    PoolConfig,
    PooledSession,
    SessionState,
)
from router.parallel_executor import (
    ParallelExecutor,
    ExecutionResult,
)
from router.context_manager import (
    ContextManager,
    ContextStore,
    ContextExtractor,
    ContextInjector,
)
from router.result_aggregator import (
    ResultAggregator,
    AggregatedResult,
    ConflictDetector,
)
from router.adaptive_manager import (
    AdaptiveManager,
    PerformanceTracker,
    PerformanceMetrics,
)
from router.session import SessionManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Msg:
    """Lightweight message object mimicking ChatCompletionRequest messages."""
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content


class _Req:
    """Lightweight request object mimicking ChatCompletionRequest."""
    def __init__(self, messages: list):
        self.messages = messages


class MockAdapter:
    """Mock adapter with configurable responses and failure injection."""

    def __init__(self, default_response: Optional[Dict] = None, fail_for: Optional[set] = None):
        self.default_response = default_response or {
            "choices": [{
                "message": {"role": "assistant", "content": "Mock response"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
        }
        self.fail_for = fail_for or set()  # sub-task IDs that should fail
        self.call_count = 0
        self.call_log: List[Dict] = []
        self._call_times: List[float] = []

    async def send(self, request: Dict[str, Any], model: str) -> Dict[str, Any]:
        start = time.monotonic()
        self.call_count += 1

        # Extract sub-task description to find the ID being processed
        user_msg = ""
        for msg in request.get("messages", []):
            if msg.get("role") == "user":
                user_msg = msg.get("content", "")
                break

        self.call_log.append({
            "request": request,
            "model": model,
            "user_msg_preview": user_msg[:100],
        })

        await asyncio.sleep(0.05)  # minimal delay

        # Check if this call should fail
        for fail_id in self.fail_for:
            if f"Sub-task {fail_id}" in user_msg or f"Part {fail_id}" in user_msg:
                raise ConnectionError(f"Simulated failure for sub-task {fail_id}")

        # Build response with the user message echoed
        response = dict(self.default_response)
        response["choices"] = [{
            "message": {
                "role": "assistant",
                "content": f"Response for: {user_msg[:80]}",
            },
            "finish_reason": "stop",
        }]
        return response


class MockSessionPool:
    """Lightweight session pool stub for executor tests."""

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


@pytest.fixture
def detector():
    return TaskSizeDetector()


@pytest.fixture
def splitter():
    return TaskSplitter()


@pytest.fixture
def mock_adapter():
    return MockAdapter()


@pytest.fixture
def mock_pool():
    return MockSessionPool()


@pytest.fixture
def context_manager():
    return ContextManager()


@pytest.fixture
def result_aggregator():
    return ResultAggregator()


@pytest.fixture
def session_manager():
    """In-memory SessionManager for pool tests."""
    return SessionManager(db_path=None)


@pytest.fixture
def pool_config():
    return PoolConfig(max_sessions=3, max_messages_per_session=3, session_ttl=3600)


@pytest.fixture
def tracker():
    return PerformanceTracker()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(sub_task_id: int, success: bool = True, content: str = "done",
                 duration: float = 0.5) -> ExecutionResult:
    """Create an ExecutionResult for testing."""
    if success:
        return ExecutionResult(
            sub_task_id=sub_task_id,
            success=True,
            result={
                "choices": [{
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
            },
            duration=duration,
        )
    else:
        return ExecutionResult(
            sub_task_id=sub_task_id,
            success=False,
            result=None,
            error="Simulated error",
            duration=duration,
        )


def _make_sub_task(id: int, description: str = "test task",
                   depends_on: Optional[List[int]] = None) -> SubTask:
    return SubTask(
        id=id,
        description=description,
        depends_on=depends_on or [],
        original_context="test context",
    )


# ---------------------------------------------------------------------------
# Test 1: Small task passes through without splitting
# ---------------------------------------------------------------------------


def test_task_detector_small_task_no_split(detector):
    """Small tasks should not trigger splitting."""
    req = _Req([_Msg("user", "What is the capital of France?")])
    analysis = detector.analyze(req)

    assert not analysis.needs_splitting
    assert analysis.estimated_tokens < TOKEN_SPLIT_THRESHOLD
    assert analysis.complexity == "simple"
    assert analysis.suggested_split_count == 1


# ---------------------------------------------------------------------------
# Test 2: Large task (>4000 tokens) triggers splitting
# ---------------------------------------------------------------------------


def test_task_detector_large_task_triggers_split(detector):
    """Large tasks exceeding the token threshold should trigger splitting."""
    # Generate text that exceeds 4000 tokens (~3000+ words at 1.3 tokens/word)
    large_text = "Implement a comprehensive caching layer with Redis backend, " \
                 "including TTL management, cache invalidation, and distributed locking. " * 200
    req = _Req([_Msg("user", large_text)])
    analysis = detector.analyze(req)

    assert analysis.needs_splitting
    assert analysis.estimated_tokens > TOKEN_SPLIT_THRESHOLD
    assert analysis.suggested_split_count >= 2


# ---------------------------------------------------------------------------
# Test 3: Multi-file task splits by file
# ---------------------------------------------------------------------------


def test_multi_file_task_splits_by_file(detector, splitter):
    """Tasks with multiple file paths should split into per-file sub-tasks."""
    text = (
        "Refactor all files in the project. Update router/main.py, "
        "router/session.py, router/routing.py, and router/fallback.py "
        "to use async/await patterns consistently."
    )
    req = _Req([_Msg("user", text)])
    analysis = detector.analyze(req)

    assert analysis.needs_splitting
    assert analysis.intent == "refactor"

    subtasks = splitter.split(req, analysis)
    assert len(subtasks) >= 3  # at least 3 files detected

    # Each sub-task should reference a file
    descriptions = [st.description for st in subtasks]
    assert any("main.py" in d for d in descriptions)
    assert any("session.py" in d for d in descriptions)
    assert any("routing.py" in d for d in descriptions)

    # File-based splits should produce independent tasks (no deps)
    for st in subtasks:
        assert st.depends_on == []


# ---------------------------------------------------------------------------
# Test 4: Numbered steps create sequential sub-tasks with deps
# ---------------------------------------------------------------------------


def test_numbered_steps_split_sequentially(detector, splitter):
    """Tasks with numbered steps should create sequential sub-tasks with dependencies."""
    text = (
        "Please implement the following changes:\n"
        "1. Add authentication middleware to the API\n"
        "2. Create a user registration endpoint\n"
        "3. Add rate limiting to all routes\n"
        "4. Write unit tests for the new endpoints\n"
        "5. Update the README with new API documentation"
    )
    req = _Req([_Msg("user", text)])
    analysis = detector.analyze(req)

    assert analysis.needs_splitting

    subtasks = splitter.split(req, analysis)
    assert len(subtasks) >= 4  # at least 4 steps detected

    # First task should have no dependencies
    assert subtasks[0].depends_on == []

    # Subsequent tasks should depend on the previous one (sequential)
    for st in subtasks[1:]:
        assert len(st.depends_on) > 0, f"Sub-task {st.id} should have dependencies"


# ---------------------------------------------------------------------------
# Test 5: Independent sub-tasks run concurrently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_executor_runs_independent_tasks(mock_pool):
    """Independent sub-tasks should run concurrently."""
    adapter = MockAdapter()
    executor = ParallelExecutor(
        session_pool=mock_pool,
        max_retries=0,
        timeout=10.0,
        max_concurrency=5,
    )

    # 3 independent sub-tasks
    sub_tasks = [
        _make_sub_task(1, "Task A"),
        _make_sub_task(2, "Task B"),
        _make_sub_task(3, "Task C"),
    ]

    start = time.monotonic()
    results = await executor.execute(sub_tasks, adapter, model="test-model")
    elapsed = time.monotonic() - start

    # All should succeed
    assert len(results) == 3
    assert all(r.success for r in results)
    assert adapter.call_count == 3

    # With 3 independent tasks at 0.05s each, parallel should be < 0.15s serial
    # Allow generous margin for CI
    assert elapsed < 0.5, f"Expected parallel execution < 0.5s, got {elapsed:.2f}s"

    # Peak concurrency should be 3 (all independent)
    assert mock_pool._peak >= 2  # at least 2 concurrent


# ---------------------------------------------------------------------------
# Test 6: Dependent sub-tasks wait for prerequisites
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_executor_respects_dependencies(mock_pool):
    """Dependent sub-tasks should wait for their prerequisites to complete."""
    adapter = MockAdapter()
    executor = ParallelExecutor(
        session_pool=mock_pool,
        max_retries=0,
        timeout=10.0,
        max_concurrency=5,
    )

    # Task 2 depends on task 1; task 3 depends on task 2
    sub_tasks = [
        _make_sub_task(1, "First: set up database"),
        _make_sub_task(2, "Second: create models", depends_on=[1]),
        _make_sub_task(3, "Third: build API", depends_on=[2]),
    ]

    results = await executor.execute(sub_tasks, adapter, model="test-model")

    assert len(results) == 3
    assert all(r.success for r in results)
    assert adapter.call_count == 3

    # Verify ordering: results should be in sub-task order
    assert [r.sub_task_id for r in results] == [1, 2, 3]

    # The executor should have created multiple layers
    layers = executor._topological_layers(sub_tasks)
    assert len(layers) == 3  # Each task in its own layer (sequential chain)


# ---------------------------------------------------------------------------
# Test 7: Context from task 1 is injected into task 2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_passing_between_tasks(context_manager):
    """Context from task 1 should be injected into task 2's request."""
    cm = context_manager

    # Simulate task 1 result
    result_1 = _make_result(
        sub_task_id=1,
        content=(
            "Created the database module in db/connection.py. "
            "Decided to use asyncpg for PostgreSQL access. "
            "The DatabasePool class defines connect() and disconnect() methods."
        ),
    )

    await cm.record_result(result_1)

    # Prepare task 2 - should have context injected
    sub_task_2 = SubTask(
        id=2,
        description="Create the User model in models/user.py",
        depends_on=[1],
        original_context="Build a user management system",
    )

    request = await cm.prepare_sub_task(sub_task_2)

    # Verify context was injected into the user message
    user_msg = request["messages"][-1]["content"]
    assert "[CONTEXT FROM PREVIOUS TASKS]" in user_msg
    assert "[END CONTEXT]" in user_msg

    # Context should contain info from task 1
    assert "db/connection.py" in user_msg or "DatabasePool" in user_msg or "task_1" in user_msg.lower() or "asyncpg" in user_msg

    # Verify the actual task description is also present
    assert "User model" in user_msg


# ---------------------------------------------------------------------------
# Test 8: Multiple successful results merged correctly
# ---------------------------------------------------------------------------


def test_result_aggregation_success(result_aggregator):
    """Multiple successful results should be merged into a coherent response."""
    results = [
        _make_result(1, content="Created database module in db/connection.py"),
        _make_result(2, content="Created User model in models/user.py"),
        _make_result(3, content="Created API endpoints in api/users.py"),
    ]

    sub_tasks = [
        _make_sub_task(1, "Create database module"),
        _make_sub_task(2, "Create User model", depends_on=[1]),
        _make_sub_task(3, "Create API endpoints", depends_on=[1, 2]),
    ]

    aggregated = result_aggregator.aggregate(results, sub_tasks)

    assert isinstance(aggregated, AggregatedResult)
    assert aggregated.success_count == 3
    assert aggregated.failure_count == 0
    assert len(aggregated.sub_results) == 3

    # Combined response should contain content from all tasks
    assert "db/connection.py" in aggregated.combined_response
    assert "User model" in aggregated.combined_response
    assert "api/users.py" in aggregated.combined_response

    # Should have section headers
    assert "Part 1/3" in aggregated.combined_response
    assert "Part 2/3" in aggregated.combined_response
    assert "Part 3/3" in aggregated.combined_response


# ---------------------------------------------------------------------------
# Test 9: Partial failures handled gracefully
# ---------------------------------------------------------------------------


def test_result_aggregation_with_failures(result_aggregator):
    """Partial failures should be handled gracefully in the aggregated result."""
    results = [
        _make_result(1, success=True, content="Database module created successfully"),
        _make_result(2, success=False, duration=2.0),  # This one failed
        _make_result(3, success=True, content="API endpoints created"),
    ]

    sub_tasks = [
        _make_sub_task(1, "Create database module"),
        _make_sub_task(2, "Create User model", depends_on=[1]),
        _make_sub_task(3, "Create API endpoints", depends_on=[2]),
    ]

    aggregated = result_aggregator.aggregate(results, sub_tasks)

    assert aggregated.success_count == 2
    assert aggregated.failure_count == 1
    assert len(aggregated.sub_results) == 3

    # Combined response should still contain successful content
    assert "Database module" in aggregated.combined_response
    assert "API endpoints" in aggregated.combined_response

    # Failed task should have error info
    assert "failed" in aggregated.combined_response.lower() or "error" in aggregated.combined_response.lower()

    # format_response should produce valid OpenAI-compatible dict
    response = result_aggregator.format_response(aggregated)
    assert "choices" in response
    assert "usage" in response
    assert "_session_manager" in response
    assert response["_session_manager"]["success_count"] == 2
    assert response["_session_manager"]["failure_count"] == 1


# ---------------------------------------------------------------------------
# Test 10: Sessions rotate when message limit reached
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_pool_rotation(session_manager, pool_config):
    """Sessions should rotate (become FULL) when message limit is reached."""
    pool = SessionPool(config=pool_config, session_manager=session_manager)

    # Acquire first session
    session1 = await pool.acquire_session()
    assert session1.state == SessionState.ACTIVE

    # Release messages until the limit is hit (max_messages_per_session=3)
    for _ in range(pool_config.max_messages_per_session):
        await pool.release_session(session1.session_id)

    # Session should now be FULL
    session1_check = await pool.get_session(session1.session_id)
    assert session1_check.state == SessionState.FULL
    assert session1_check.message_count == pool_config.max_messages_per_session

    # Acquiring a new session should give a different session (or rotate)
    session2 = await pool.acquire_session()
    # Could be a new session or a rotated one
    assert session2.session_id != session1.session_id or session2.state == SessionState.ACTIVE

    # Verify pool stats
    stats = await pool.get_pool_stats()
    assert stats["total_sessions"] >= 1
    assert stats["total_messages"] >= pool_config.max_messages_per_session

    await pool.close()
    await session_manager.close()


# ---------------------------------------------------------------------------
# Test 11: Config adapts based on performance metrics
# ---------------------------------------------------------------------------


def test_adaptive_manager_adjusts_config(tracker):
    """AdaptiveManager should adjust config based on performance metrics."""
    initial_config = PoolConfig(
        max_sessions=10,
        max_messages_per_session=50,
        session_ttl=3600,
    )
    manager = AdaptiveManager(pool_config=initial_config, tracker=tracker)

    # Record requests with high truncation rate (40%)
    for _ in range(20):
        tracker.record_request(
            duration=2.0,
            success=True,
            tokens=500,
            truncated=True,  # all truncated
        )

    # Evaluate - should reduce max_messages_per_session
    new_config = manager.evaluate()
    assert new_config.max_messages_per_session < initial_config.max_messages_per_session, \
        "High truncation should reduce max_messages_per_session"

    # Apply and verify change log
    applied_config = manager.apply_recommendations()
    assert applied_config.max_messages_per_session < 50
    assert len(manager.change_log) == 1

    # Now record low-truncation, high-success, fast requests
    tracker.reset()
    for _ in range(20):
        tracker.record_request(
            duration=1.0,
            success=True,
            tokens=500,
            truncated=False,
        )

    # Evaluate again - should increase max_sessions (good perf)
    config_after_good = manager.evaluate()
    assert config_after_good.max_sessions >= applied_config.max_sessions, \
        "Good performance should maintain or increase max_sessions"


# ---------------------------------------------------------------------------
# Test 12: Complete flow: detect -> split -> execute -> aggregate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_pipeline_mock(mock_pool):
    """Complete flow: detect → split → execute → aggregate with mocked adapter."""
    # 1. Setup
    detector = TaskSizeDetector()
    splitter = TaskSplitter()
    adapter = MockAdapter()
    executor = ParallelExecutor(
        session_pool=mock_pool,
        max_retries=1,
        timeout=10.0,
        max_concurrency=3,
    )
    aggregator = ResultAggregator()
    ctx_manager = ContextManager()

    # 2. Create a request that triggers splitting (multiple files)
    text = (
        "Refactor the following files to use async patterns: "
        "router/main.py, router/session.py, and router/routing.py"
    )
    req = _Req([_Msg("user", text)])

    # 3. Detect
    analysis = detector.analyze(req)
    assert analysis.needs_splitting

    # 4. Split
    sub_tasks = splitter.split(req, analysis)
    assert len(sub_tasks) >= 3

    # 5. Execute
    results = await executor.execute(sub_tasks, adapter, model="test-model")
    assert len(results) == len(sub_tasks)
    assert all(r.success for r in results)

    # 6. Record context for each result
    for result in results:
        await ctx_manager.record_result(result)

    # Verify context accumulated
    state = await ctx_manager.get_shared_state()
    assert len(state) > 0

    # 7. Aggregate
    aggregated = aggregator.aggregate(results, sub_tasks)
    assert aggregated.success_count == len(sub_tasks)
    assert aggregated.failure_count == 0
    assert len(aggregated.combined_response) > 0

    # 8. Format as OpenAI response
    response = aggregator.format_response(aggregated)
    assert "choices" in response
    assert response["choices"][0]["message"]["role"] == "assistant"
    assert len(response["choices"][0]["message"]["content"]) > 0
    assert response["_session_manager"]["aggregated"] is True
    assert response["_session_manager"]["sub_task_count"] == len(sub_tasks)


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_manager_handles_failed_result():
    """Context manager should handle failed results without crashing."""
    cm = ContextManager()

    failed_result = ExecutionResult(
        sub_task_id=1,
        success=False,
        result=None,
        error="Connection timeout",
        duration=30.0,
    )

    # Should not raise
    await cm.record_result(failed_result)
    state = await cm.get_shared_state()
    assert state.get("task_1_success") is False


def test_result_aggregator_all_failures():
    """When all sub-tasks fail, should produce error summary."""
    aggregator = ResultAggregator()
    results = [
        _make_result(1, success=False),
        _make_result(2, success=False),
    ]
    sub_tasks = [_make_sub_task(1), _make_sub_task(2)]

    aggregated = aggregator.aggregate(results, sub_tasks)
    assert aggregated.success_count == 0
    assert aggregated.failure_count == 2
    assert "failed" in aggregated.combined_response.lower()


@pytest.mark.asyncio
async def test_session_pool_creates_initial_session(session_manager, pool_config):
    """Pool should create a session on first acquire when empty."""
    pool = SessionPool(config=pool_config, session_manager=session_manager)

    session = await pool.acquire_session()
    assert session is not None
    assert session.state == SessionState.ACTIVE
    assert session.message_count == 0

    await pool.close()
    await session_manager.close()


def test_performance_tracker_metrics(tracker):
    """PerformanceTracker should compute correct metrics."""
    # Record 10 requests: 8 success, 2 failure, 3 truncated
    for i in range(10):
        tracker.record_request(
            duration=1.0 + i * 0.1,
            success=i < 8,
            tokens=100,
            truncated=i < 3,
        )

    metrics = tracker.get_metrics()
    assert metrics.success_rate == pytest.approx(0.8, abs=0.01)
    assert metrics.error_rate == pytest.approx(0.2, abs=0.01)
    assert metrics.truncation_rate == pytest.approx(0.3, abs=0.01)
    assert metrics.avg_response_time > 0
    assert tracker.record_count == 10


@pytest.mark.asyncio
async def test_hybrid_task_splitter_simple_task():
    """HybridTaskSplitter should use rule-based for simple tasks."""
    splitter = HybridTaskSplitter()
    req = {"messages": [{"role": "user", "content": "What is Python?"}]}

    sub_tasks = await splitter.split(req)
    assert len(sub_tasks) == 1  # No splitting needed
    assert sub_tasks[0].id == 1

    await splitter.close()


@pytest.mark.asyncio
async def test_parallel_executor_retry_on_failure(mock_pool):
    """Executor should retry failed sub-tasks up to max_retries."""
    # Adapter that fails on first call but succeeds on retry
    call_count = 0

    class FlakeyAdapter:
        async def send(self, request, model):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("Transient failure")
            return {
                "choices": [{"message": {"role": "assistant", "content": "Success after retry"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }

    executor = ParallelExecutor(
        session_pool=mock_pool,
        max_retries=2,
        timeout=10.0,
        max_concurrency=1,
    )

    sub_tasks = [_make_sub_task(1, "Flakey task")]
    results = await executor.execute(sub_tasks, FlakeyAdapter(), model="test")

    assert len(results) == 1
    assert results[0].success
    assert results[0].retries >= 1  # At least one retry
    assert call_count == 2  # First call failed, second succeeded


def test_topological_layers_detection():
    """Topological layering should correctly group independent and dependent tasks."""
    sub_tasks = [
        _make_sub_task(1, "A"),
        _make_sub_task(2, "B"),
        _make_sub_task(3, "C", depends_on=[1]),
        _make_sub_task(4, "D", depends_on=[1, 2]),
        _make_sub_task(5, "E", depends_on=[3, 4]),
    ]

    layers = ParallelExecutor._topological_layers(sub_tasks)

    # Layer 0: tasks 1, 2 (no deps)
    # Layer 1: tasks 3, 4 (depend on 1 and/or 2)
    # Layer 2: task 5 (depends on 3, 4)
    assert len(layers) == 3

    layer_0_ids = {t.id for t in layers[0]}
    layer_1_ids = {t.id for t in layers[1]}
    layer_2_ids = {t.id for t in layers[2]}

    assert layer_0_ids == {1, 2}
    assert layer_1_ids == {3, 4}
    assert layer_2_ids == {5}
