# Performance Optimization Report

> DeepSeek Hybrid Router — Parallel Execution & Adaptive Session Management

---

## Table of Contents

1. [Current Performance Baseline](#1-current-performance-baseline)
2. [Threshold Tuning Recommendations](#2-threshold-tuning-recommendations)
3. [Adaptive Manager Behavior](#3-adaptive-manager-behavior)
4. [Optimization Strategies](#4-optimization-strategies)
5. [Monitoring and Alerting](#5-monitoring-and-alerting)

---

## 1. Current Performance Baseline

### 1.1 Independent Tasks (Parallel Speedup)

Benchmarked with 0.5s simulated bridge latency per request (`test_benchmark.py`):

| Sub-tasks | Sequential Time | Parallel Time | Speedup | Assertion Threshold |
|-----------|----------------|---------------|---------|---------------------|
| 2         | ~1.0s          | ~0.5s         | **~50%** | ≥ 30%              |
| 5         | ~2.5s          | ~0.5s         | **~80%** | ≥ 60%              |
| 10        | ~5.0s          | ~0.5s         | **~90%** | ≥ 75%              |

**Key insight:** Speedup scales with task count. With N independent tasks and sufficient concurrency, wall-clock time collapses to a single request latency (~0.5s). The formula:

```
speedup = ((N × latency) - latency) / (N × latency) × 100
        = (N - 1) / N × 100
```

### 1.2 Dependent Tasks (DAG Execution)

Tasks with dependency chains execute in topological layers:

| Scenario | Graph Structure | Layers | Sequential | Parallel | Speedup |
|----------|----------------|--------|------------|----------|---------|
| 5 tasks (deps) | `[1,2,4] → [3] → [5]` | 3 | 2.5s | 1.5s | **~40%** |
| Realistic (dashboard) | `[1,2,3] → [4] → [5]` | 3 | 2.5s | 1.5s | **≥ 40%** |

**Dependency speedup formula:**
```
speedup = 1 - (layers / N)
```
Where `layers` = depth of the dependency graph and `N` = total tasks.

### 1.3 Real-World E2E Scenarios

From `test_e2e_scenarios.py`:

| Scenario | Type | Sub-tasks | Dependencies | Parallelism | Key Verification |
|----------|------|-----------|--------------|-------------|------------------|
| Multi-step Fibonacci | Sequential steps | 5 | Chain (1→2→3→4→5) | Partial | Context passes between steps |
| Multi-file refactor | Independent files | 5 | None | Full (peak ≥ 2) | All files in combined response |
| Large document (8000+ tokens) | Token chunking | ≥2 | None | Full | All chunks aggregated |

**E2E pipeline flow:**
```
Request → TaskSizeDetector → TaskSplitter → ParallelExecutor → ContextManager → ResultAggregator
```

---

## 2. Threshold Tuning Recommendations

### 2.1 `token_threshold` — When to Split

| Parameter | Default | Source |
|-----------|---------|--------|
| `TOKEN_SPLIT_THRESHOLD` | **4000** | `task_splitter.py:78` |
| `_TOKEN_THRESHOLD` | **4000** | `main.py:45` |

```python
# task_splitter.py
TOKEN_SPLIT_THRESHOLD = 4000  # above this → recommend splitting

class TaskSizeDetector:
    def __init__(self, token_threshold: int = TOKEN_SPLIT_THRESHOLD):
        self.token_threshold = token_threshold
```

#### Decision Table

| Situation | Recommended Value | Rationale |
|-----------|------------------|-----------|
| Default / general use | **4000** | Balances split overhead vs context limits |
| High-latency bridge (>5s/req) | **6000–8000** | Fewer splits = fewer round-trips |
| Low-latency bridge (<1s/req) | **2000–3000** | Splits are cheap; maximize parallelism |
| Large context window model (128k+) | **8000–12000** | Model can handle more context |
| Small context window model (8k–16k) | **2000–3000** | Prevent overflow on sub-tasks |
| Token-sensitive billing | **3000** | Split earlier to avoid oversized prompts |

#### Configuration

```python
# In main.py or your initialization:
task_detector = TaskSizeDetector(token_threshold=4000)

# For high-latency environments:
task_detector = TaskSizeDetector(token_threshold=8000)
```

### 2.2 `max_sessions` — Parallelism Ceiling

| Parameter | Default | Min | Max | Source |
|-----------|---------|-----|-----|--------|
| `max_sessions` | **10** | 3 | 20 | `session_pool.py:98` |

```python
# session_pool.py
@dataclass
class PoolConfig:
    max_sessions: int = 10        # _DEFAULT_MAX_SESSIONS
    max_messages_per_session: int = 50  # _DEFAULT_MAX_MESSAGES
    session_ttl: int = 3600       # _DEFAULT_SESSION_TTL (1 hour)
```

**Note:** The benchmark tests use `_DEFAULT_CONCURRENCY = 5` in `ParallelExecutor`, while the pool default is `max_sessions = 10`. The effective parallelism is `min(max_concurrency, max_sessions)`.

#### Scaling Guidelines

| Workload Profile | Recommended `max_sessions` | Why |
|-----------------|---------------------------|-----|
| Light (1-3 concurrent users) | **3–5** | Low memory, adequate parallelism |
| Medium (5-10 users) | **5–10** | Matches typical sub-task count |
| Heavy (10+ users) | **10–15** | Prevents session queuing |
| Burst (batch processing) | **15–20** | Maximum throughput, higher memory |

**Memory tradeoff:** Each session holds conversation context. At 50 messages/session × ~500 tokens/message average, each session uses ~25k tokens of context. With `max_sessions=10`, that's 250k tokens in flight.

### 2.3 `max_messages_per_session` — Context Window Management

| Parameter | Default | Adaptive Min | Adaptive Max | Source |
|-----------|---------|-------------|-------------|--------|
| `max_messages_per_session` | **50** | 20 | 80 | `adaptive_manager.py:43-44` |

#### Memory vs Performance Tradeoff

```
Higher max_messages  →  Longer sessions  →  More context available  →  Higher truncation risk
Lower max_messages   →  More rotations   →  Fresher context         →  Rotation overhead
```

| Value | Use Case | Truncation Risk | Rotation Frequency |
|-------|----------|-----------------|-------------------|
| 20 | High-token responses (code gen) | Low | High |
| 50 | **Default — balanced** | Medium | Medium |
| 80 | Short responses (Q&A, classification) | High | Low |

---

## 3. Adaptive Manager Behavior

The `AdaptiveManager` (`adaptive_manager.py`) automatically tunes `PoolConfig` based on rolling metrics from the last 20 requests.

### 3.1 Tuning Rules

```
┌─────────────────────────────────────────────────────────────────┐
│                    Adaptive Decision Tree                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  truncation_rate > 30%?                                         │
│    YES → max_messages_per_session × 0.9 (floor: 20)            │
│    NO  → truncation_rate < 10%?                                 │
│           YES → max_messages_per_session × 1.1 (ceiling: 80)   │
│                                                                 │
│  error_rate > 20%?                                              │
│    YES → max_sessions -= 1 (floor: 3)                           │
│    NO  → success_rate > 95% AND avg_response_time < 5s?         │
│           YES → max_sessions += 1 (ceiling: 20)                 │
│                                                                 │
│  avg_response_time > 30s?                                       │
│    YES → Flag: recommend lower timeout                           │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 Threshold Constants

```python
# adaptive_manager.py
_HIGH_TRUNCATION_RATE = 0.3     # 30% — reduce max_messages
_LOW_TRUNCATION_RATE = 0.1      # 10% — increase max_messages
_HIGH_ERROR_RATE = 0.2          # 20% — reduce max_sessions
_HIGH_SUCCESS_RATE = 0.95       # 95% — eligible to increase max_sessions
_FAST_RESPONSE_TIME = 5.0       # seconds — paired with high success
_SLOW_RESPONSE_TIME = 30.0      # seconds — flag for timeout reduction

# Bounds
_MIN_MAX_MESSAGES = 20
_MAX_MAX_MESSAGES = 80
_MIN_MAX_SESSIONS = 3
_MAX_MAX_SESSIONS = 20
```

### 3.3 Adaptation Scenarios (from smoke test)

| Phase | Conditions | Metrics Observed | Config Change |
|-------|-----------|-----------------|---------------|
| 1: High truncation | 40% truncation, 25% errors | `truncation_rate=40%, error_rate=25%` | `max_messages: 50→45`, `max_sessions: 10→9` |
| 2: Healthy | 5% truncation, 5% errors, fast | `truncation_rate<10%, success>95%` | `max_messages: 45→49`, `max_sessions: 9→10` |
| 3: Degraded | 50% errors, slow (15-45s) | `error_rate=50%, avg_time=30s+` | `max_sessions: 10→9` |

### 3.4 AutoTuner Background Task

```python
# Automatic tuning every 5 minutes
tuner = AutoTuner(manager, interval=300)  # 300s = 5 minutes
await tuner.start()

# ... application runs ...

await tuner.stop()
```

The `AutoTuner` runs `apply_recommendations()` on a fixed interval. Changes are bounded by the min/max constants, preventing runaway scaling.

### 3.5 Change Audit Trail

Every config change is logged with full metrics snapshot:

```python
# Access the change log
for entry in manager.change_log:
    print(f"Time: {entry['timestamp']}")
    for param, delta in entry['changes'].items():
        print(f"  {param}: {delta['old']} → {delta['new']}")
    print(f"  Metrics: {entry['metrics_snapshot']}")
```

---

## 4. Optimization Strategies

### 4.1 LLM-Based vs Rule-Based Splitting

The `HybridTaskSplitter` selects the strategy automatically:

```python
# task_splitter.py — HybridTaskSplitter.split()
use_llm = (
    analysis.complexity == "complex"
    or analysis.suggested_split_count > 3
)
```

#### Decision Matrix

| Condition | Strategy | Latency | Quality |
|-----------|----------|---------|---------|
| Simple intent, ≤3 sub-tasks | **Rule-based** | ~0ms (local) | Good for structured tasks |
| Complex intent OR >3 sub-tasks | **LLM-based** | ~1-3s (network) | Better dependency detection |
| LLM call fails | **Rule-based (fallback)** | ~0ms | Automatic degradation |

```
┌──────────────────────────────────────────┐
│         HybridTaskSplitter               │
│                                          │
│  ┌──────────┐     ┌─────────────────┐   │
│  │ Detector │────→│ Strategy Select  │   │
│  └──────────┘     └────┬────────┬───┘   │
│                         │        │       │
│              simple     │        │complex│
│              ≤3 tasks   │        │>3     │
│                         ▼        ▼       │
│                   ┌────────┐ ┌───────┐  │
│                   │Rule    │ │LLM    │  │
│                   │Splitter│ │Splitter│  │
│                   └───┬────┘ └──┬────┘  │
│                       │    fail │        │
│                       │◄────────┘        │
│                       ▼                  │
│               List[SubTask]              │
└──────────────────────────────────────────┘
```

#### When to Prefer Rule-Based

- File-based refactors (regex detects file names)
- Step-by-step instructions (numbered list parsing)
- Token-based chunking (simple division)
- Latency-sensitive paths (no network call)

#### When to Prefer LLM-Based

- Ambiguous multi-part requests
- Tasks requiring semantic understanding of dependencies
- Requests with implicit parallelism opportunities
- Complex code generation with cross-file dependencies

### 4.2 Context Window Management

#### Token Estimation

```python
# task_splitter.py — uses tiktoken when available
try:
    import tiktoken
    _encoder = tiktoken.get_encoding("cl100k_base")
    def _estimate_tokens(text: str) -> int:
        return len(_encoder.encode(text))
except ImportError:
    def _estimate_tokens(text: str) -> int:
        return int(len(text.split()) * 1.3)  # ~1.3 tokens/word
```

#### Large Document Strategy

For documents exceeding `token_threshold`:

1. **Detection:** `TaskSizeDetector.analyze()` estimates tokens
2. **Splitting:** `TaskSplitter.split()` chunks by token count
3. **Execution:** Each chunk runs in parallel via `ParallelExecutor`
4. **Aggregation:** `ResultAggregator` combines with `Part N/M` headers

```python
# E2E test verified: 8000+ token document → multiple chunks → aggregated response
# All chunks processed in parallel, combined response > 100 chars
```

### 4.3 Parallelization Limits

#### Bottleneck Analysis

```
Effective parallelism = min(
    max_concurrency,     # ParallelExecutor default: 5
    max_sessions,        # SessionPool default: 10
    available_sessions   # Pool may have FULL/ERROR sessions
)
```

| Component | Default | Configurable | Location |
|-----------|---------|-------------|----------|
| `max_concurrency` | 5 | `ParallelExecutor(max_concurrency=N)` | `parallel_executor.py:46` |
| `max_sessions` | 10 | `PoolConfig(max_sessions=N)` | `session_pool.py:98` |
| Semaphore | = `max_concurrency` | Auto-set from `max_concurrency` | `parallel_executor.py` |

#### Scaling Recommendations

```
For N independent sub-tasks:
  - Set max_concurrency ≥ N (no wasted parallelism)
  - Set max_sessions ≥ max_concurrency (pool isn't the bottleneck)
  - Ensure session_ttl > expected_total_execution_time

For DAG workloads:
  - max_concurrency ≥ widest_layer (most parallel tasks in any layer)
  - max_sessions ≥ max_concurrency
```

### 4.4 Session Pool Lifecycle

```
                    ┌─────────┐
                    │ ACTIVE  │ ◄── newly created
                    └────┬────┘
                         │ message_count >= max_messages
                         ▼
                    ┌─────────┐
          ┌────────│  FULL   │────────┐
          │        └─────────┘        │
          │                           │ rotate_session()
          │ age > session_ttl         │
          ▼                           ▼
    ┌──────────┐               ┌─────────┐
    │ EXPIRED  │               │  NEW    │ (replacement)
    └──────────┘               │ ACTIVE  │
                               └─────────┘
          │
          │ mark_error()
          ▼
    ┌─────────┐
    │  ERROR  │
    └─────────┘
```

Sessions are selected via **round-robin** among ACTIVE sessions. When the pool is at capacity and all sessions are FULL, the oldest FULL session is rotated (replaced with a fresh one).

---

## 5. Monitoring and Alerting

### 5.1 Key Metrics

Access via `AdaptiveManager.get_status()`:

```python
status = adaptive_manager.get_status()

# Config
status["config"]["max_sessions"]              # Current session limit
status["config"]["max_messages_per_session"]  # Current message limit

# Metrics (rolling window of last 20 requests)
status["metrics"]["success_rate"]             # Target: > 95%
status["metrics"]["error_rate"]               # Alert: > 20%
status["metrics"]["truncation_rate"]          # Alert: > 30%
status["metrics"]["avg_response_time"]        # Alert: > 30s
status["metrics"]["avg_tokens_per_response"]  # Capacity planning
status["metrics"]["session_rotation_count"]   # High = aggressive limits
```

#### Metric Thresholds Summary

| Metric | Healthy | Warning | Critical |
|--------|---------|---------|----------|
| `success_rate` | > 95% | 80-95% | < 80% |
| `error_rate` | < 5% | 5-20% | > 20% |
| `truncation_rate` | < 10% | 10-30% | > 30% |
| `avg_response_time` | < 5s | 5-30s | > 30s |
| `session_rotation_count` | Low | Moderate | High (thrashing) |

### 5.2 When to Intervene vs Let Adaptive Manager Handle

```
┌────────────────────────────────────────────────────────────┐
│                  Intervention Decision Tree                 │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  Issue detected                                            │
│    │                                                       │
│    ├─ Transient? (single spike, resolves in < 5 min)      │
│    │   → Let AutoTuner handle (5-min interval)            │
│    │                                                      │
│    ├─ Persistent high error rate (> 20% for 10+ min)?     │
│    │   → INTERVENE: Check bridge health, network          │
│    │   → AutoTuner will reduce max_sessions but won't     │
│    │     fix upstream issues                               │
│    │                                                      │
│    ├─ Truncation > 30% sustained?                         │
│    │   → AutoTuner reduces max_messages (may suffice)     │
│    │   → If still high after 3 cycles: increase           │
│    │     token_threshold or switch to larger model         │
│    │                                                      │
│    ├─ Session pool exhausted (RuntimeError)?              │
│    │   → INTERVENE IMMEDIATELY                            │
│    │   → Increase max_sessions or investigate session leak │
│    │                                                      │
│    └─ Slow responses (> 30s avg)?                         │
│        → Check bridge latency first                       │
│        → AutoTuner flags but doesn't auto-fix timeout     │
│        → Consider: lower timeout, reduce max_concurrency  │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

### 5.3 Log Analysis Commands

The system uses Python's `logging` module. Key log messages to search for:

```bash
# Adaptive config changes applied
grep "Applied adaptive config" logs/app.log

# AutoTuner cycles
grep "AutoTuner cycle" logs/app.log

# Session pool events
grep "Session.*marked FULL\|Created new session\|Rotated session" logs/app.log

# Session pool exhaustion (critical)
grep "Session pool exhausted" logs/app.log

# High error rate warnings
grep "High error rate\|error_rate=" logs/app.log

# Truncation warnings
grep "High truncation rate\|truncation_rate=" logs/app.log

# Task splitting decisions
grep "HybridTaskSplitter: using" logs/app.log

# Performance tracker resets
grep "PerformanceTracker reset" logs/app.log
```

#### Programmatic Monitoring

```python
# Get current recommendations (human-readable)
recommendations = adaptive_manager.get_recommendations()
for rec in recommendations:
    print(rec)

# Example outputs:
# "⚠ High truncation rate (35.0%). Consider reducing max_messages_per_session..."
# "✓ Excellent performance (success=98.0%, avg_time=2.3s). System can handle more..."
# "⚠ Slow average response time (35.2s). Consider lowering the request timeout..."

# Get pool statistics
stats = await session_pool.get_pool_stats()
print(f"Sessions: {stats['total_sessions']}/{stats['max_sessions']}")
print(f"States: {stats['states']}")
print(f"Total messages: {stats['total_messages']}")

# Health check all sessions
health = await session_pool.health_check_all()
unhealthy = [sid for sid, ok in health.items() if not ok]
if unhealthy:
    print(f"Unhealthy sessions: {unhealthy}")
```

### 5.4 Dashboard Integration

Use `get_status()` to feed a monitoring dashboard:

```python
import json

def dashboard_snapshot(adaptive_manager, session_pool):
    """Return JSON-serializable status for dashboards."""
    status = adaptive_manager.get_status()
    pool_stats = asyncio.get_event_loop().run_until_complete(
        session_pool.get_pool_stats()
    )
    return {
        "timestamp": time.time(),
        "config": status["config"],
        "metrics": status["metrics"],
        "pool": {
            "active": pool_stats["states"].get("active", 0),
            "full": pool_stats["states"].get("full", 0),
            "error": pool_stats["states"].get("error", 0),
            "expired": pool_stats["states"].get("expired", 0),
        },
        "recommendations": status["recommendations"],
        "config_changes": status["change_count"],
    }
```

---

## Appendix: Configuration Quick Reference

```python
from router.session_pool import PoolConfig, SessionPool
from router.adaptive_manager import AdaptiveManager, PerformanceTracker, AutoTuner
from router.task_splitter import TaskSizeDetector, HybridTaskSplitter
from router.parallel_executor import ParallelExecutor

# Pool configuration
config = PoolConfig(
    max_sessions=10,              # 3-20, adaptive range
    max_messages_per_session=50,  # 20-80, adaptive range
    session_ttl=3600,             # seconds (1 hour)
)

# Initialize components
pool = SessionPool(config=config)
tracker = PerformanceTracker(buffer_size=100)
adaptive = AdaptiveManager(pool_config=config, tracker=tracker)

# Task detection
detector = TaskSizeDetector(token_threshold=4000)  # tokens
splitter = HybridTaskSplitter()

# Executor
executor = ParallelExecutor(
    session_pool=pool,
    max_retries=2,          # retry failed sub-tasks
    timeout=120.0,          # seconds per sub-task
    max_concurrency=5,      # max parallel sub-tasks
)

# Background auto-tuning (optional)
tuner = AutoTuner(adaptive, interval=300)  # evaluate every 5 min
await tuner.start()
```
