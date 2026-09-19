# Plan: Session Manager for DeepSeek Hybrid Router

## Problem Statement

DeepSeek chat free has several limitations:
- **Session length limit**: ~50-100 messages before needing a new session
- **Token limit per response**: ~4000-8000 tokens
- **Context window**: Smaller than paid API
- **Rate limit**: After N consecutive requests

When processing large tasks (refactoring large codebases, complex analysis), the router encounters:
1. Truncated responses (cut short)
2. Lost context after many messages
3. Rate limiting mid-task

## Overall Architecture

```
User Request (large task)
    ↓
[Task Analyzer]
    ↓ (detect task size & complexity)
[Task Splitter]
    ├─→ Sub-task 1 → Bridge Session A → Result 1
    ├─→ Sub-task 2 → Bridge Session B → Result 2
    └─→ Sub-task 3 → Bridge Session C → Result 3
    ↓
[Result Aggregator] → Final Response
```

## Detailed Phases

### Phase 1: Core Infrastructure (2-3 hours)

**Goal**: Build foundation for session management

#### Task 1.1: Task Size Detector
- **Goal**: Analyze request to determine if splitting is needed
- **File**: `router/task_splitter.py`
- **Logic**:
  - Token count estimation (>4000 tokens → split)
  - Complexity detection (multi-file, multi-step keywords)
  - User intent analysis (refactor, analyze, generate)
- **Model**: `alibaba:qwen3.7-max` (strong reasoning)
- **Verification**: Unit tests with 10+ scenarios (small/medium/large tasks)

#### Task 1.2: Session Pool Manager
- **Goal**: Manage multiple bridge sessions
- **File**: `router/session_pool.py`
- **Features**:
  - Session creation & caching
  - Auto-rotation when session full (>50 messages)
  - Health tracking (active/expired/error)
  - Reuse strategy (prefer active sessions)
- **Model**: `deepinfra:V4-Flash-0731` (fast code generation)
- **Verification**: Test creating/rotating 5+ sessions in parallel

#### Task 1.3: Basic Task Splitter
- **Goal**: Split large tasks into simple sub-tasks
- **File**: `router/task_splitter.py` (extend from 1.1)
- **Strategy**: Rule-based splitting
  - Split by file (if multi-file task)
  - Split by step (if multi-step task)
  - Split by token count (even distribution)
- **Model**: `alibaba:qwen3.7-max`
- **Verification**: Test splitting 5 complex tasks into 15-20 sub-tasks

**Parallelization**: Tasks 1.1, 1.2, 1.3 can run in parallel (independent)

---

### Phase 2: Smart Execution (3-4 hours)

**Goal**: Execute sub-tasks and aggregate results

#### Task 2.1: Parallel Sub-task Executor
- **Goal**: Run multiple sub-tasks in parallel via multiple sessions
- **File**: `router/parallel_executor.py`
- **Features**:
  - Async execution with asyncio
  - Session allocation from pool
  - Error handling & retry logic
  - Progress tracking
- **Model**: `deepinfra:V4-Flash-0731`
- **Verification**: Execute 10 sub-tasks in parallel, verify all complete

#### Task 2.2: Context Preservation
- **Goal**: Maintain context between sub-tasks
- **File**: `router/context_manager.py`
- **Strategy**:
  - Extract key context from previous sub-task
  - Inject into prompt of next sub-task
  - Maintain shared state (file list, decisions, etc.)
- **Model**: `alibaba:qwen3.7-max`
- **Verification**: Test 5-turn conversation across 3 different sessions

#### Task 2.3: Basic Result Aggregator
- **Goal**: Aggregate results from multiple sub-tasks
- **File**: `router/result_aggregator.py`
- **Strategy**: Simple concatenation with conflict detection
  - Merge code files (if no conflict)
  - Combine analysis results
  - Detect & report conflicts
- **Model**: `deepinfra:V4-Flash-0731`
- **Verification**: Aggregate 5 sub-task results into 1 complete response

**Sequential**: 2.1 → 2.2 → 2.3 (dependencies)

---

### Phase 3: Advanced Features (3-4 hours)

**Goal**: Optimize with LLM-based splitting and intelligent aggregation

#### Task 3.1: LLM-based Task Splitter
- **Goal**: Use LLM to split tasks more intelligently
- **File**: `router/task_splitter.py` (upgrade from 1.3)
- **Approach**: Prompt LLM with task description → get sub-tasks
- **Prompt template**:
  ```
  Analyze this task and split it into independent sub-tasks:
  Task: {user_request}
  
  Return JSON:
  {
    "sub_tasks": [
      {"id": 1, "description": "...", "depends_on": []},
      {"id": 2, "description": "...", "depends_on": [1]}
    ]
  }
  ```
- **Model**: `alibaba:qwen3.7-max` (complex reasoning)
- **Verification**: Split 10 complex tasks, compare with rule-based

#### Task 3.2: Intelligent Result Aggregator
- **Goal**: Use LLM to merge results intelligently
- **File**: `router/result_aggregator.py` (upgrade from 2.3)
- **Approach**: Prompt LLM with all sub-results → get unified response
- **Features**:
  - Conflict resolution
  - Redundancy elimination
  - Coherence improvement
- **Model**: `alibaba:qwen3.7-max`
- **Verification**: Aggregate 10 sets of results, quality evaluation

#### Task 3.3: Adaptive Session Management
- **Goal**: Automatically adjust strategy based on performance
- **File**: `router/adaptive_manager.py`
- **Features**:
  - Monitor response quality (truncation rate)
  - Auto-adjust split threshold
  - Learn from historical data
- **Model**: `deepinfra:V4-Flash-0731`
- **Verification**: Run 20 requests, verify adaptation

**Parallelization**: 3.1 and 3.3 can run in parallel, 3.2 needs 3.1 to complete first

---

### Phase 4: Integration & Testing (2-3 hours)

**Goal**: Integrate into router and test end-to-end

#### Task 4.1: Router Integration
- **Goal**: Add session manager to router flow
- **File**: `router/main.py`
- **Changes**:
  - Add `/v1/chat/completions` logic to detect & use session manager
  - Add config option `enable_session_manager: true/false`
  - Add `/v1/session-manager/stats` endpoint
- **Model**: Manual implementation
- **Verification**: Router still works for small tasks, uses session manager for large tasks

#### Task 4.2: End-to-End Testing
- **Goal**: Test entire flow with real-world scenarios
- **Test scenarios**:
  1. Refactor 5000-line codebase (split into 5 sessions)
  2. Analyze 10 files simultaneously (split into 10 sessions)
  3. Generate complete microservice (split into 8 sessions)
- **Model**: Manual testing
- **Verification**: All scenarios complete successfully, results are usable

#### Task 4.3: Documentation & Examples
- **Goal**: Write docs and examples
- **Files**: `docs/session-manager.md`, `examples/session_manager_demo.py`
- **Content**:
  - How it works
  - Configuration options
  - Use cases & examples
  - Performance metrics
- **Model**: `alibaba:qwen3.8-flash` (fast documentation)
- **Verification**: Docs are clear, examples are runnable

**Sequential**: 4.1 → 4.2 → 4.3

---

## Estimated Total Time

- **Phase 1**: 2-3 hours
- **Phase 2**: 3-4 hours
- **Phase 3**: 3-4 hours
- **Phase 4**: 2-3 hours
- **Total**: 10-14 hours (can be parallelized to reduce to 6-8 hours)

## Resource Allocation

- **Models used**:
  - `alibaba:qwen3.7-max`: Reasoning tasks (splitting, aggregation)
  - `deepinfra:V4-Flash-0731`: Code generation (infrastructure, testing)
  - `alibaba:qwen3.8-flash`: Documentation, simple tasks

- **Delegation strategy**:
  - Phase 1: 3 subagents in parallel
  - Phase 2: sequential (dependencies)
  - Phase 3: 2 subagents in parallel (3.1 + 3.3)
  - Phase 4: sequential

## Risks & Mitigations

1. **Risk**: Bridge unstable when running multiple sessions in parallel
   - **Mitigation**: Test stability first, have fallback to DeepInfra/Alibaba

2. **Risk**: LLM-based splitting creates non-independent sub-tasks
   - **Mitigation**: Dependency tracking, validate independence

3. **Risk**: Low aggregation quality (incoherent results)
   - **Mitigation**: Quality checks, manual review during testing

## Success Criteria

- ✅ All phases completed with passing tests
- ✅ Can handle tasks that would normally be truncated
- ✅ Parallel execution reduces total time by 40%+
- ✅ Result quality comparable to single-session responses
- ✅ Documentation complete with working examples

## Next Steps

1. Review and approve this plan
2. Start Phase 1 implementation
3. Test each phase before proceeding
4. Iterate based on test results
