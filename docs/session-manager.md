# Session Manager

## Overview

The Session Manager is a pipeline that automatically detects oversized chat-completion requests, splits them into smaller sub-tasks, executes those sub-tasks in parallel across multiple bridge sessions, and merges the results into a single coherent response.

It exists because DeepSeek's free chat bridge has hard limits:

| Limitation | Effect |
|---|---|
| ~50-100 messages per session | Context degrades after many turns |
| ~4000-8000 tokens per response | Large answers get truncated mid-stream |
| Smaller context window | Earlier messages fall out of view |
| Rate limiting after N requests | Long tasks fail partway through |

Without the Session Manager, a request like *"refactor the entire codebase"* returns a truncated, half-finished response. With it, the same request is split into file-scoped sub-tasks, run across fresh sessions, and reassembled.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    POST /v1/chat/completions             │
│                      (non-streaming)                     │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
              ┌─────────────────────────┐
              │    TaskSizeDetector      │  Estimate tokens, detect intent,
              │    (task_splitter.py)    │  decide if splitting is needed
              └────────────┬────────────┘
                           │ needs_splitting?
                    ┌──────┴──────┐
                    │ no          │ yes
                    ▼             ▼
            ┌──────────┐  ┌────────────────────┐
            │ Pass     │  │ HybridTaskSplitter  │  Rule-based split first,
            │ through  │  │ (task_splitter.py)  │  then optional LLM refinement
            │ to       │  └─────────┬──────────┘
            │ normal   │            │ List[SubTask]
            │ fallback │            ▼
            │ chain    │  ┌────────────────────┐
            └──────────┘  │  ContextManager     │  Inject shared context
                          │  (context_manager) │  into each sub-task prompt
                          └─────────┬──────────┘
                                    │
                                    ▼
                          ┌────────────────────┐
                          │  ParallelExecutor   │  Async execution with
                          │  (parallel_executor)│  dependency resolution
                          │                     │  via SessionPool
                          └─────────┬──────────┘
                                    │ List[ExecutionResult]
                                    ▼
                          ┌────────────────────┐
                          │ ResultAggregator    │  Merge results, detect
                          │ (result_aggregator) │  conflicts, format response
                          └─────────┬──────────┘
                                    │
                                    ▼
                          ┌────────────────────┐
                          │ PerformanceTracker  │  Record metrics for
                          │ (adaptive_manager)  │  adaptive tuning
                          └────────────────────┘
```

### Supporting components

```
┌──────────────────┐     ┌──────────────────┐
│   SessionPool     │────▶│  PoolConfig       │  max_sessions, max_messages,
│  (session_pool)   │     │                   │  session_ttl
│                   │     └──────────────────┘
│  • Round-robin    │
│    session pick   │     ┌──────────────────┐
│  • Auto-rotation  │────▶│ AdaptiveManager   │  Reads metrics, adjusts
│    on full/error  │     │ (adaptive_manager)│  PoolConfig automatically
│  • Health states  │     └──────────────────┘
└──────────────────┘
```

---

## How It Works

The pipeline has four stages: **detect → split → execute → aggregate**.

### 1. Detect (`TaskSizeDetector`)

Every non-streaming request to `/v1/chat/completions` passes through `TaskSizeDetector.analyze()`, which:

1. **Concatenates** all message content into a single text string
2. **Estimates tokens** using `tiktoken` (cl100k_base) — falls back to `word_count × 1.3` if tiktoken isn't installed
3. **Detects intent** from keywords: `refactor`, `analyze`, `generate`, `translate`, `test`, `document`, or `general`
4. **Classifies complexity** as `simple` / `medium` / `complex` based on token count, multi-file patterns, multi-step patterns, and file-path density
5. **Decides** whether splitting is needed:
   - Tokens > threshold (default 4000)
   - Complexity == `complex`
   - ≥ 2 distinct file paths mentioned
   - ≥ 3 numbered/sequential steps detected

Returns a `TaskAnalysis` dataclass with `needs_splitting`, `estimated_tokens`, `complexity`, `intent`, and `suggested_split_count`.

### 2. Split (`HybridTaskSplitter`)

If `needs_splitting` is true, the `HybridTaskSplitter` produces a list of `SubTask` objects:

- **Rule-based split** (`TaskSplitter`) — tried first, no LLM call:
  - **By file**: when ≥ 2 file paths are detected, one sub-task per file
  - **By step**: when ≥ 3 numbered steps are detected, one sub-task per step
  - **By token count**: fallback — evenly divides text into chunks of ~2000 tokens
- **LLM-based split** (`LLMTaskSplitter`) — optional refinement via the bridge adapter, produces sub-tasks with `depends_on` and `priority` metadata
- **Hybrid orchestration**: tries LLM first, falls back to rule-based on any error

Each `SubTask` carries: `id`, `description`, `depends_on` (list of prerequisite IDs), `original_context`, and `priority` (1-10).

### 3. Execute (`ParallelExecutor` + `SessionPool`)

The executor respects the dependency graph:

1. **Topological sort** — groups sub-tasks into layers (independent tasks run together, dependent tasks wait)
2. **Semaphore-gated concurrency** — caps in-flight sub-tasks to `max_concurrency` (default 5)
3. **Session borrowing** — each sub-task acquires a session from `SessionPool` via round-robin
4. **Retry with backoff** — up to `max_retries` (default 2) attempts per sub-task
5. **Timeout** — per-sub-task timeout of 120s (configurable)
6. **Progress callback** — optional `on_progress(completed, total, result)` hook

The `SessionPool` manages session lifecycle:

| State | Meaning |
|---|---|
| `ACTIVE` | Usable, under message limit |
| `FULL` | Hit `max_messages_per_session`, needs rotation |
| `EXPIRED` | Past `session_ttl`, needs rotation |
| `ERROR` | Unrecoverable failure, needs rotation |

When no `ACTIVE` session is available and the pool is at capacity, the oldest `FULL`/`EXPIRED` session is rotated (replaced with a fresh one).

### 4. Aggregate (`ResultAggregator` / `HybridResultAggregator`)

1. **Extract** text content from each `ExecutionResult` (handles OpenAI response format)
2. **Order** results by dependency graph (topological sort)
3. **Concatenate** with section headers
4. **Detect conflicts** (`ConflictDetector`):
   - File conflicts: multiple sub-tasks modify the same file
   - Contradictory instructions: e.g. one says "delete" while another says "keep"
5. **Format** as an OpenAI-compatible response dict
6. **LLM refinement** (`LLMResultAggregator`): optionally sends all sub-results to an LLM for coherent merging, conflict resolution, and redundancy elimination

### Context Preservation (`ContextManager`)

Between execute and aggregate, the `ContextManager`:

- **Extracts** structured context from each completed sub-task (files mentioned, decisions made, variables defined, changes made)
- **Stores** context in a thread-safe `ContextStore` (async-safe dict)
- **Injects** a `[CONTEXT FROM PREVIOUS TASKS]` block into subsequent sub-task prompts
- **Summarizes** context with a character budget (default 2000 chars) to stay within token limits

---

## Configuration

### Global toggles (in `router/main.py`)

| Constant | Default | Description |
|---|---|---|
| `_SESSION_MGR_ENABLED` | `True` | Master on/off switch for the session manager pipeline |
| `_TOKEN_THRESHOLD` | `4000` | Token estimate above which a request is flagged for splitting |

### PoolConfig (in `router/session_pool.py`)

| Parameter | Default | Description |
|---|---|---|
| `max_sessions` | `10` | Maximum number of sessions in the pool |
| `max_messages_per_session` | `50` | Message count before a session is marked `FULL` |
| `session_ttl` | `3600` | Session time-to-live in seconds (1 hour) |

### ParallelExecutor parameters

| Parameter | Default | Description |
|---|---|---|
| `max_retries` | `2` | Retry attempts per sub-task on failure |
| `timeout` | `120.0` | Per-sub-task timeout in seconds |
| `max_concurrency` | `5` | Max sub-tasks executing simultaneously |

### Adaptive tuning bounds (in `router/adaptive_manager.py`)

| Bound | Value | Description |
|---|---|---|
| `_MIN_MAX_MESSAGES` | `20` | Floor for `max_messages_per_session` |
| `_MAX_MAX_MESSAGES` | `80` | Ceiling for `max_messages_per_session` |
| `_MIN_MAX_SESSIONS` | `3` | Floor for `max_sessions` |
| `_MAX_MAX_SESSIONS` | `20` | Ceiling for `max_sessions` |
| `_HIGH_TRUNCATION_RATE` | `0.3` | Triggers reducing messages-per-session |
| `_LOW_TRUNCATION_RATE` | `0.1` | Triggers increasing messages-per-session |
| `_HIGH_ERROR_RATE` | `0.2` | Triggers reducing max_sessions |
| `_DEFAULT_TUNER_INTERVAL` | `300.0` | AutoTuner check interval (5 minutes) |

---

## API Endpoints

### `GET /v1/session-manager/stats`

Returns pool statistics, performance metrics, and adaptive recommendations.

**Response:**

```json
{
  "pool": {
    "total_sessions": 3,
    "active": 2,
    "full": 1,
    "expired": 0,
    "error": 0,
    "sessions": [...]
  },
  "performance": {
    "truncation_rate": 0.05,
    "avg_response_time": 4.2,
    "success_rate": 0.95,
    "avg_tokens_per_response": 3200,
    "session_rotation_count": 12,
    "error_rate": 0.05
  },
  "recent_performance": {
    "truncation_rate": 0.0,
    "avg_response_time": 3.8,
    "success_rate": 1.0,
    "error_rate": 0.0
  },
  "recommendations": {
    "max_messages_per_session": 55,
    "max_sessions": 10
  },
  "enabled": true
}
```

### `GET /v1/session-manager/config`

Returns current session manager settings.

**Response:**

```json
{
  "enabled": true,
  "token_threshold": 4000,
  "pool_config": {
    "max_sessions": 10,
    "max_messages_per_session": 50,
    "session_ttl": 3600
  }
}
```

### `POST /v1/chat/completions`

The standard OpenAI-compatible endpoint. When `_SESSION_MGR_ENABLED` is `true` and `stream` is `false`, requests exceeding the token threshold are automatically routed through the session manager pipeline. Small requests pass through the normal fallback chain unchanged.

---

## Module Reference

| Module | Key Classes | Purpose |
|---|---|---|
| `task_splitter.py` | `TaskSizeDetector`, `TaskAnalysis`, `SubTask`, `TaskSplitter`, `LLMTaskSplitter`, `HybridTaskSplitter` | Analyze requests, decide if splitting is beneficial, produce sub-tasks |
| `session_pool.py` | `SessionPool`, `PooledSession`, `PoolConfig`, `SessionState` | Manage a pool of reusable bridge sessions with rotation and health tracking |
| `parallel_executor.py` | `ParallelExecutor`, `ExecutionResult` | Execute sub-tasks concurrently with dependency resolution and retry logic |
| `context_manager.py` | `ContextManager`, `ContextStore`, `ContextExtractor`, `ContextInjector` | Preserve and inject shared context between sub-tasks across sessions |
| `result_aggregator.py` | `ResultAggregator`, `AggregatedResult`, `ConflictDetector`, `LLMResultAggregator`, `HybridResultAggregator` | Merge sub-task results, detect conflicts, format final response |
| `adaptive_manager.py` | `AdaptiveManager`, `PerformanceTracker`, `PerformanceMetrics`, `AutoTuner` | Monitor performance and auto-tune pool configuration |

---

## Performance Considerations

### Latency

- **Detection overhead**: ~1-2ms (token estimation + keyword matching). Negligible compared to LLM call time.
- **Splitting overhead**: rule-based split is ~1ms; LLM-based split adds one extra LLM call (~2-5s depending on model).
- **Parallel execution**: independent sub-tasks run concurrently. A task split into 5 sub-tasks that each take 4s completes in ~4s wall-clock time instead of ~20s sequential.
- **Aggregation overhead**: concatenation is ~1ms; LLM-based merging adds one extra LLM call.

### Token efficiency

- Each sub-task receives only its relevant context plus a shared summary (capped at 2000 chars), reducing wasted tokens.
- Shorter sessions mean fresher context windows — less risk of early messages being dropped.

### Memory

- `PerformanceTracker` uses a circular buffer of 100 records — bounded and predictable.
- `SessionPool` holds at most `max_sessions` in-memory session objects.
- `ContextStore` is per-request and discarded after aggregation.

### When NOT to use the Session Manager

- **Streaming requests**: the pipeline only runs for non-streaming (`stream=false`) requests.
- **Small requests**: requests under the token threshold pass through unchanged.
- **Time-sensitive requests**: splitting adds overhead (detection + aggregation + optional LLM calls). If latency matters more than completeness, disable it.

---

## Fallback Behavior

The session manager is designed to fail gracefully:

```
Session Manager pipeline
        │
        ├─ Success → return aggregated response
        │
        └─ Any exception during detect/split/execute/aggregate
                │
                └─ Log warning with traceback
                   Fall through to normal fallback chain:
                     primary adapter → fallback adapters → error response
```

Specific failure modes:

| Failure | Behavior |
|---|---|
| `TaskSizeDetector` raises | Falls through to normal flow (treat as small request) |
| `HybridTaskSplitter` LLM call fails | Falls back to rule-based `TaskSplitter` |
| Rule-based splitter produces 1 sub-task | Falls through to normal flow (no benefit from splitting) |
| `ParallelExecutor` sub-task fails | Retries up to `max_retries`; partial results are still aggregated |
| All sub-tasks fail | Aggregated result has `success_count=0`; falls through to normal flow |
| `ResultAggregator` raises | Caught by outer `try/except`; falls through to normal flow |
| `SessionPool` exhausted | Raises `RuntimeError`; caught by outer handler |
| Any unhandled exception | Outer `try/except` in `main.py` catches it, logs it, and falls through |

The normal fallback chain (`FallbackManager`) always serves as the last resort: primary adapter → configured fallback adapters → error response.
