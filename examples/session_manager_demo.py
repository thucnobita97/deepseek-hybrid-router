"""
Session Manager Demo
====================

Demonstrates the full Session Manager pipeline:
1. Analyze a large task
2. Split it into sub-tasks
3. Execute sub-tasks in parallel
4. Aggregate results
5. Show pool stats and performance metrics

Run with: python -m examples.session_manager_demo

This demo uses mock adapters and fake data — no real HTTP calls needed.
"""
import asyncio
import sys
import time
from typing import Any, Dict, List

# Add parent directory to path so we can import router modules
sys.path.insert(0, '/home/thucnobita/dev/deepseek-hybrid-router')

from router.task_splitter import TaskSizeDetector, HybridTaskSplitter, TaskAnalysis
from router.session_pool import SessionPool, PoolConfig
from router.parallel_executor import ParallelExecutor, ExecutionResult
from router.context_manager import ContextManager
from router.result_aggregator import ResultAggregator, HybridResultAggregator
from router.adaptive_manager import AdaptiveManager, PerformanceTracker


# ---------------------------------------------------------------------------
# Mock Request — simulates a ChatCompletionRequest object
# ---------------------------------------------------------------------------

class MockMessage:
    """Mock message with role and content attributes."""
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content


class MockRequest:
    """Mock request object that mimics ChatCompletionRequest structure."""
    def __init__(self, messages: List[Dict[str, str]]):
        self.messages = [MockMessage(m["role"], m["content"]) for m in messages]


# ---------------------------------------------------------------------------
# Mock Adapter — simulates a bridge adapter without making real HTTP calls
# ---------------------------------------------------------------------------

class MockBridgeAdapter:
    """Mock adapter that simulates LLM responses for demo purposes."""
    
    async def send(self, request: Dict[str, Any], model: str) -> Dict[str, Any]:
        """Simulate sending a request to the bridge and getting a response."""
        # Extract the user message from the request
        messages = request.get("messages", [])
        user_message = ""
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    # Multi-part content
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            user_message += part.get("text", "")
                elif isinstance(content, str):
                    user_message = content
                break
        
        # Simulate processing time (0.5-1.5 seconds)
        await asyncio.sleep(0.5 + (hash(user_message) % 1000) / 1000.0)
        
        # Generate a mock response based on the sub-task
        response_text = self._generate_mock_response(user_message)
        
        # Return OpenAI-compatible response format
        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": response_text
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": len(user_message.split()) * 2,
                "completion_tokens": len(response_text.split()) * 2,
                "total_tokens": (len(user_message) + len(response_text)) * 2
            }
        }
    
    def _generate_mock_response(self, user_message: str) -> str:
        """Generate a context-aware mock response."""
        lower = user_message.lower()
        
        if "file" in lower and ("refactor" in lower or "update" in lower):
            return f"I've analyzed the file and made the following changes:\n\n1. Updated imports to use modern syntax\n2. Refactored the main function for better readability\n3. Added type hints where missing\n4. Improved error handling\n\nThe file has been successfully processed."
        
        elif "test" in lower or "coverage" in lower:
            return f"I've created comprehensive unit tests:\n\n- test_basic_functionality(): Tests core logic\n- test_edge_cases(): Tests boundary conditions\n- test_error_handling(): Tests exception paths\n\nAll tests pass with 95% coverage."
        
        elif "document" in lower or "readme" in lower:
            return f"I've added documentation:\n\n- Module-level docstring explaining purpose\n- Function docstrings with parameter descriptions\n- Usage examples in README\n- Type hints for better IDE support"
        
        elif "analyze" in lower or "review" in lower:
            return f"Code analysis complete:\n\n- No critical bugs found\n- 3 performance optimization opportunities identified\n- Security review passed\n- Code style follows PEP 8 guidelines"
        
        else:
            return f"Task completed successfully. The request has been processed and all requirements have been met."


# ---------------------------------------------------------------------------
# Demo Functions
# ---------------------------------------------------------------------------

def print_section(title: str):
    """Print a formatted section header."""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70 + "\n")


async def demo_task_detection():
    """Demonstrate task size detection and analysis."""
    print_section("STEP 1: Task Detection")
    
    detector = TaskSizeDetector(token_threshold=4000)
    
    # Create a large, complex request that should trigger splitting
    large_request = MockRequest([
        {
            "role": "user",
            "content": """Please refactor the entire codebase across all files:
            
1. Update main.py to use modern async/await patterns
2. Refactor router.py to improve error handling
3. Update utils.py to add type hints
4. Modify adapters/bridge.py to use connection pooling
5. Update tests/test_main.py to add integration tests
6. Refactor session.py to simplify the API
7. Update config.py to support environment variables
8. Modify logging.py to add structured logging

This is a complex refactoring task that touches multiple files and requires careful coordination.
"""
        }
    ])
    
    print("Analyzing request...")
    analysis = detector.analyze(large_request)
    
    print(f"✓ Estimated tokens: {analysis.estimated_tokens}")
    print(f"✓ Complexity: {analysis.complexity}")
    print(f"✓ Intent: {analysis.intent}")
    print(f"✓ Needs splitting: {analysis.needs_splitting}")
    print(f"✓ Suggested split count: {analysis.suggested_split_count}")
    
    return large_request, analysis


async def demo_task_splitting(request: MockRequest, analysis: TaskAnalysis):
    """Demonstrate task splitting into sub-tasks."""
    print_section("STEP 2: Task Splitting")
    
    splitter = HybridTaskSplitter()
    
    print("Splitting task into sub-tasks...")
    sub_tasks = await splitter.split(request, analysis)
    
    print(f"✓ Split into {len(sub_tasks)} sub-tasks:\n")
    
    for i, task in enumerate(sub_tasks, 1):
        print(f"  {i}. Sub-task #{task.id}")
        print(f"     Description: {task.description[:80]}...")
        print(f"     Depends on: {task.depends_on if task.depends_on else 'none'}")
        print(f"     Priority: {task.priority}")
        print()
    
    return sub_tasks


async def demo_parallel_execution(sub_tasks):
    """Demonstrate parallel execution of sub-tasks."""
    print_section("STEP 3: Parallel Execution")
    
    # Create session pool with small config for demo
    pool_config = PoolConfig(
        max_sessions=5,
        max_messages_per_session=50,
        session_ttl=3600
    )
    session_pool = SessionPool(config=pool_config)
    
    # Create executor with progress callback
    def on_progress(completed: int, total: int, result: ExecutionResult):
        status = "✓" if result.success else "✗"
        print(f"  {status} Sub-task #{result.sub_task_id} completed ({completed}/{total}) - {result.duration:.2f}s")
    
    executor = ParallelExecutor(
        session_pool=session_pool,
        max_retries=2,
        timeout=120.0,
        max_concurrency=3,
        on_progress=on_progress
    )
    
    # Create mock adapter
    mock_adapter = MockBridgeAdapter()
    
    print(f"Executing {len(sub_tasks)} sub-tasks in parallel...\n")
    
    start_time = time.time()
    results = await executor.execute(sub_tasks, mock_adapter, "deepseek-chat")
    total_duration = time.time() - start_time
    
    print(f"\n✓ All sub-tasks completed in {total_duration:.2f}s")
    print(f"✓ Success rate: {sum(1 for r in results if r.success)}/{len(results)}")
    
    # Get pool stats
    pool_stats = await session_pool.get_pool_stats()
    states = pool_stats.get("states", {})
    print(f"\nSession Pool Stats:")
    print(f"  - Total sessions: {pool_stats['total_sessions']}")
    print(f"  - Active: {states.get('active', 0)}")
    print(f"  - Full: {states.get('full', 0)}")
    print(f"  - Total messages sent: {pool_stats['total_messages']}")
    
    return results, session_pool


async def demo_result_aggregation(results: List[ExecutionResult], sub_tasks):
    """Demonstrate result aggregation and conflict detection."""
    print_section("STEP 4: Result Aggregation")
    
    # Create aggregator
    aggregator = ResultAggregator()
    
    print("Aggregating results from all sub-tasks...")
    aggregated = aggregator.aggregate(results, sub_tasks)
    
    print(f"✓ Combined {aggregated.success_count} successful results")
    print(f"✓ {aggregated.failure_count} failures")
    print(f"✓ {len(aggregated.conflicts)} conflicts detected")
    print(f"✓ Total duration: {aggregated.total_duration:.2f}s")
    
    # Format final response
    print("\nFormatting final response...")
    response = aggregator.format_response(aggregated)
    
    print(f"✓ Response formatted (OpenAI-compatible format)")
    print(f"✓ Response length: {len(str(response))} chars")
    
    # Show a preview of the combined response
    combined_text = aggregated.combined_response
    preview = combined_text[:300] + "..." if len(combined_text) > 300 else combined_text
    print(f"\nCombined response preview:\n{preview}")
    
    return aggregated, response


async def demo_performance_tracking(results: List[ExecutionResult], session_pool: SessionPool):
    """Demonstrate performance tracking and adaptive management."""
    print_section("STEP 5: Performance Tracking")
    
    # Create performance tracker
    perf_tracker = PerformanceTracker(buffer_size=100)
    
    # Record metrics from the executed sub-tasks
    print("Recording performance metrics...")
    for result in results:
        perf_tracker.record_request(
            duration=result.duration,
            success=result.success,
            tokens=result.result.get("usage", {}).get("total_tokens", 0) if result.result else 0,
            truncated=False
        )
    
    # Get metrics
    metrics = perf_tracker.get_metrics()
    recent_metrics = perf_tracker.get_recent_metrics(window=10)
    
    print("\nPerformance Metrics (all requests):")
    print(f"  - Success rate: {metrics.success_rate:.1%}")
    print(f"  - Truncation rate: {metrics.truncation_rate:.1%}")
    print(f"  - Error rate: {metrics.error_rate:.1%}")
    print(f"  - Avg response time: {metrics.avg_response_time:.2f}s")
    print(f"  - Avg tokens per response: {metrics.avg_tokens_per_response:.0f}")
    
    print("\nRecent Performance (last 10 requests):")
    print(f"  - Success rate: {recent_metrics.success_rate:.1%}")
    print(f"  - Avg response time: {recent_metrics.avg_response_time:.2f}s")
    
    # Create adaptive manager
    pool_config = session_pool.config
    adaptive_mgr = AdaptiveManager(pool_config=pool_config, tracker=perf_tracker)
    
    print("\nAdaptive Recommendations:")
    recommendations = adaptive_mgr.get_recommendations()
    if recommendations:
        for rec in recommendations:
            print(f"  - {rec}")
    else:
        print("  - No adjustments needed (performance is healthy)")
    
    # Also show evaluated config
    evaluated = adaptive_mgr.evaluate()
    print(f"\nEvaluated PoolConfig:")
    print(f"  - max_messages_per_session: {evaluated.max_messages_per_session}")
    print(f"  - max_sessions: {evaluated.max_sessions}")


async def demo_small_request_passthrough():
    """Demonstrate that small requests pass through unchanged."""
    print_section("BONUS: Small Request Passthrough")
    
    detector = TaskSizeDetector(token_threshold=4000)
    
    # Create a small, simple request
    small_request = {
        "messages": [
            {
                "role": "user",
                "content": "What is 2 + 2?"
            }
        ]
    }
    
    print("Analyzing small request...")
    analysis = detector.analyze(small_request)
    
    print(f"✓ Estimated tokens: {analysis.estimated_tokens}")
    print(f"✓ Complexity: {analysis.complexity}")
    print(f"✓ Needs splitting: {analysis.needs_splitting}")
    print("\n✓ This request would pass through the normal fallback chain")
    print("  (no session manager overhead)")


# ---------------------------------------------------------------------------
# Main Demo Runner
# ---------------------------------------------------------------------------

async def main():
    """Run the complete Session Manager demo."""
    print("\n" + "=" * 70)
    print("  DEEPSEEK HYBRID ROUTER - SESSION MANAGER DEMO")
    print("=" * 70)
    print("\nThis demo shows how the Session Manager automatically handles")
    print("large requests by splitting them into parallel sub-tasks.\n")
    
    # Step 1: Detect large task
    request, analysis = await demo_task_detection()
    
    if not analysis.needs_splitting:
        print("\n⚠ Warning: Task was not flagged for splitting.")
        print("  This might happen if the request is too small.")
        return
    
    # Step 2: Split into sub-tasks
    sub_tasks = await demo_task_splitting(request, analysis)
    
    if len(sub_tasks) <= 1:
        print("\n⚠ Warning: Task was not split (only 1 sub-task).")
        print("  This might happen if the splitter couldn't identify split points.")
        return
    
    # Step 3: Execute in parallel
    results, session_pool = await demo_parallel_execution(sub_tasks)
    
    # Step 4: Aggregate results
    aggregated, response = await demo_result_aggregation(results, sub_tasks)
    
    # Step 5: Track performance
    await demo_performance_tracking(results, session_pool)
    
    # Bonus: Show small request behavior
    await demo_small_request_passthrough()
    
    # Summary
    print_section("DEMO COMPLETE")
    print("The Session Manager successfully:")
    print("  ✓ Detected a large, complex task")
    print("  ✓ Split it into multiple sub-tasks")
    print("  ✓ Executed them in parallel across sessions")
    print("  ✓ Aggregated results into a coherent response")
    print("  ✓ Tracked performance metrics")
    print("\nIn production, this happens automatically for requests that")
    print("exceed the token threshold, with transparent fallback to the")
    print("normal routing chain if anything fails.")
    print("\n" + "=" * 70 + "\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nDemo interrupted by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Demo failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
