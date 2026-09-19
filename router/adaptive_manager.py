"""Adaptive Session Management — automatically adjusts pool strategy based on performance.

Monitors request metrics (truncation rate, error rate, response times) and
dynamically tunes :class:`PoolConfig` parameters to maintain optimal throughput.

Components
----------
* :class:`PerformanceMetrics` — snapshot of observed performance.
* :class:`PerformanceTracker` — records per-request outcomes in a circular
  buffer and computes rolling metrics.
* :class:`AdaptiveManager` — evaluates metrics and returns an adjusted
  :class:`PoolConfig`.
* :class:`AutoTuner` — optional background task that periodically evaluates
  and applies safe configuration changes.

Integration
-----------
* :class:`router.session_pool.PoolConfig` — the configuration object that
  gets adjusted.
* :class:`router.parallel_executor.ExecutionResult` — metrics source
  (callers feed results into :class:`PerformanceTracker`).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Deque, Dict, List, Optional

from router.session_pool import PoolConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / Bounds
# ---------------------------------------------------------------------------

_BUFFER_SIZE = 100  # circular buffer capacity

# Adaptive tuning bounds
_MIN_MAX_MESSAGES = 20
_MAX_MAX_MESSAGES = 80
_MIN_MAX_SESSIONS = 3
_MAX_MAX_SESSIONS = 20

# Thresholds
_HIGH_TRUNCATION_RATE = 0.3
_LOW_TRUNCATION_RATE = 0.1
_HIGH_ERROR_RATE = 0.2
_HIGH_SUCCESS_RATE = 0.95
_FAST_RESPONSE_TIME = 5.0  # seconds
_SLOW_RESPONSE_TIME = 30.0  # seconds

# AutoTuner
_DEFAULT_TUNER_INTERVAL = 300.0  # 5 minutes


# ---------------------------------------------------------------------------
# PerformanceMetrics
# ---------------------------------------------------------------------------


@dataclass
class PerformanceMetrics:
    """Point-in-time snapshot of observed performance metrics.

    All rate fields are in the range [0.0, 1.0] unless stated otherwise.
    """

    truncation_rate: float = 0.0
    avg_response_time: float = 0.0
    success_rate: float = 1.0
    avg_tokens_per_response: float = 0.0
    session_rotation_count: int = 0
    error_rate: float = 0.0
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Internal request record
# ---------------------------------------------------------------------------


@dataclass
class _RequestRecord:
    """Single request observation stored in the circular buffer."""

    duration: float
    success: bool
    tokens: int
    truncated: bool
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# PerformanceTracker
# ---------------------------------------------------------------------------


class PerformanceTracker:
    """Records per-request outcomes and computes rolling metrics.

    Internally maintains a circular buffer of the last 100 request records.
    Thread-safe for single-writer / multi-reader patterns typical of async
    applications (all mutations happen on the event loop).

    Parameters
    ----------
    buffer_size:
        Maximum number of request records to retain.  Defaults to 100.
    """

    def __init__(self, buffer_size: int = _BUFFER_SIZE) -> None:
        self._buffer: Deque[_RequestRecord] = deque(maxlen=buffer_size)
        self._total_rotation_count: int = 0
        self._buffer_size: int = buffer_size

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_request(
        self,
        duration: float,
        success: bool,
        tokens: int = 0,
        truncated: bool = False,
    ) -> None:
        """Record a single request outcome.

        Parameters
        ----------
        duration:
            Wall-clock time for the request in seconds.
        success:
            ``True`` if the request completed without error.
        tokens:
            Token count for the response (0 if unknown / failed).
        truncated:
            ``True`` if the response was truncated (e.g. hit max_tokens).
        """
        record = _RequestRecord(
            duration=duration,
            success=success,
            tokens=tokens,
            truncated=truncated,
        )
        self._buffer.append(record)
        logger.debug(
            "Recorded request: dur=%.2fs success=%s tokens=%d trunc=%s",
            duration, success, tokens, truncated,
        )

    def record_rotation(self) -> None:
        """Increment the session rotation counter."""
        self._total_rotation_count += 1

    def get_metrics(self) -> PerformanceMetrics:
        """Compute metrics over the entire buffer.

        Returns a :class:`PerformanceMetrics` snapshot.  If the buffer is
        empty, returns default (zeroed) metrics.
        """
        return self._compute_metrics(list(self._buffer))

    def get_recent_metrics(self, window: int = 20) -> PerformanceMetrics:
        """Compute metrics over the last *window* requests.

        Parameters
        ----------
        window:
            Number of most-recent records to consider.  Clamped to the
            actual buffer size if fewer records exist.

        Returns
        -------
        PerformanceMetrics
        """
        records = list(self._buffer)
        if window < len(records):
            records = records[-window:]
        return self._compute_metrics(records)

    def reset(self) -> None:
        """Clear all recorded data and reset counters."""
        self._buffer.clear()
        self._total_rotation_count = 0
        logger.info("PerformanceTracker reset")

    @property
    def record_count(self) -> int:
        """Number of records currently in the buffer."""
        return len(self._buffer)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _compute_metrics(self, records: List[_RequestRecord]) -> PerformanceMetrics:
        """Derive aggregate metrics from a list of request records."""
        if not records:
            return PerformanceMetrics()

        n = len(records)
        total_duration = sum(r.duration for r in records)
        success_count = sum(1 for r in records if r.success)
        failure_count = n - success_count
        truncation_count = sum(1 for r in records if r.truncated)
        total_tokens = sum(r.tokens for r in records)

        # avg_tokens should only consider successful responses
        successful_records = [r for r in records if r.success]
        avg_tokens = (
            sum(r.tokens for r in successful_records) / len(successful_records)
            if successful_records
            else 0.0
        )

        return PerformanceMetrics(
            truncation_rate=truncation_count / n,
            avg_response_time=total_duration / n,
            success_rate=success_count / n,
            avg_tokens_per_response=avg_tokens,
            session_rotation_count=self._total_rotation_count,
            error_rate=failure_count / n,
            timestamp=time.time(),
        )


# ---------------------------------------------------------------------------
# AdaptiveManager
# ---------------------------------------------------------------------------


class AdaptiveManager:
    """Evaluates performance metrics and returns adjusted pool configurations.

    Uses a set of heuristic rules to tune :class:`PoolConfig` parameters:

    * **High truncation rate (>30%)**: reduce ``max_messages_per_session``
      by 10% (floor: 20) — shorter sessions mean fresher context.
    * **Low truncation rate (<10%)**: increase ``max_messages_per_session``
      by 10% (ceiling: 80) — sessions can carry more context safely.
    * **High error rate (>20%)**: reduce ``max_sessions`` by 1 (floor: 3)
      — fewer concurrent sessions to reduce contention.
    * **High success + fast responses**: increase ``max_sessions`` by 1
      (ceiling: 20) — the system can handle more parallelism.
    * **Slow responses (>30s avg)**: flag for lower timeout recommendation.

    Parameters
    ----------
    pool_config:
        The current pool configuration to use as a baseline.
    tracker:
        A :class:`PerformanceTracker` instance.  Created automatically if
        ``None``.
    """

    def __init__(
        self,
        pool_config: PoolConfig,
        tracker: Optional[PerformanceTracker] = None,
    ) -> None:
        self._config = pool_config
        self._tracker = tracker or PerformanceTracker()
        self._change_log: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def config(self) -> PoolConfig:
        """Current (possibly adjusted) pool configuration."""
        return self._config

    @property
    def tracker(self) -> PerformanceTracker:
        """The performance tracker in use."""
        return self._tracker

    @property
    def change_log(self) -> List[Dict[str, Any]]:
        """Audit log of all configuration changes applied."""
        return list(self._change_log)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self) -> PoolConfig:
        """Evaluate current metrics and return an adjusted :class:`PoolConfig`.

        The returned config is a *proposal* — call :meth:`apply_recommendations`
        to make it the active config.  The original config is not mutated.

        Returns
        -------
        PoolConfig
            A new config with adjustments applied based on recent metrics.
        """
        metrics = self._tracker.get_recent_metrics(window=20)
        new_config = replace(self._config)
        adjustments: List[str] = []

        # --- Truncation-based message limit tuning ---
        if metrics.truncation_rate > _HIGH_TRUNCATION_RATE:
            new_val = max(
                _MIN_MAX_MESSAGES,
                int(new_config.max_messages_per_session * 0.9),
            )
            if new_val != new_config.max_messages_per_session:
                adjustments.append(
                    f"truncation_rate={metrics.truncation_rate:.1%} > "
                    f"{_HIGH_TRUNCATION_RATE:.0%}: max_messages "
                    f"{new_config.max_messages_per_session} → {new_val}"
                )
                new_config.max_messages_per_session = new_val

        elif metrics.truncation_rate < _LOW_TRUNCATION_RATE:
            new_val = min(
                _MAX_MAX_MESSAGES,
                int(new_config.max_messages_per_session * 1.1),
            )
            if new_val != new_config.max_messages_per_session:
                adjustments.append(
                    f"truncation_rate={metrics.truncation_rate:.1%} < "
                    f"{_LOW_TRUNCATION_RATE:.0%}: max_messages "
                    f"{new_config.max_messages_per_session} → {new_val}"
                )
                new_config.max_messages_per_session = new_val

        # --- Error-rate-based session count tuning ---
        if metrics.error_rate > _HIGH_ERROR_RATE:
            new_val = max(_MIN_MAX_SESSIONS, new_config.max_sessions - 1)
            if new_val != new_config.max_sessions:
                adjustments.append(
                    f"error_rate={metrics.error_rate:.1%} > "
                    f"{_HIGH_ERROR_RATE:.0%}: max_sessions "
                    f"{new_config.max_sessions} → {new_val}"
                )
                new_config.max_sessions = new_val

        elif (
            metrics.success_rate > _HIGH_SUCCESS_RATE
            and metrics.avg_response_time < _FAST_RESPONSE_TIME
        ):
            new_val = min(_MAX_MAX_SESSIONS, new_config.max_sessions + 1)
            if new_val != new_config.max_sessions:
                adjustments.append(
                    f"success_rate={metrics.success_rate:.1%} > "
                    f"{_HIGH_SUCCESS_RATE:.0%} & avg_time={metrics.avg_response_time:.1f}s < "
                    f"{_FAST_RESPONSE_TIME}s: max_sessions "
                    f"{new_config.max_sessions} → {new_val}"
                )
                new_config.max_sessions = new_val

        if adjustments:
            logger.info(
                "Adaptive evaluation: %d adjustment(s) proposed: %s",
                len(adjustments),
                "; ".join(adjustments),
            )
        else:
            logger.debug("Adaptive evaluation: no adjustments needed")

        return new_config

    def get_recommendations(self) -> List[str]:
        """Return human-readable recommendations based on current metrics.

        Unlike :meth:`evaluate`, this does not compute config changes — it
        returns descriptive strings suitable for display in dashboards or
        logs.

        Returns
        -------
        list[str]
            Zero or more recommendation strings.
        """
        metrics = self._tracker.get_recent_metrics(window=20)
        recommendations: List[str] = []

        if metrics.truncation_rate > _HIGH_TRUNCATION_RATE:
            recommendations.append(
                f"⚠ High truncation rate ({metrics.truncation_rate:.1%}). "
                f"Consider reducing max_messages_per_session to shorten "
                f"session context windows."
            )
        elif metrics.truncation_rate < _LOW_TRUNCATION_RATE and self._tracker.record_count >= 10:
            recommendations.append(
                f"✓ Low truncation rate ({metrics.truncation_rate:.1%}). "
                f"Sessions can safely carry more context — consider "
                f"increasing max_messages_per_session."
            )

        if metrics.error_rate > _HIGH_ERROR_RATE:
            recommendations.append(
                f"⚠ High error rate ({metrics.error_rate:.1%}). "
                f"Consider reducing max_sessions to lower concurrency "
                f"and reduce contention."
            )

        if (
            metrics.success_rate > _HIGH_SUCCESS_RATE
            and metrics.avg_response_time < _FAST_RESPONSE_TIME
        ):
            recommendations.append(
                f"✓ Excellent performance (success={metrics.success_rate:.1%}, "
                f"avg_time={metrics.avg_response_time:.1f}s). "
                f"System can handle more parallel sessions."
            )

        if metrics.avg_response_time > _SLOW_RESPONSE_TIME:
            recommendations.append(
                f"⚠ Slow average response time ({metrics.avg_response_time:.1f}s). "
                f"Consider lowering the request timeout or investigating "
                f"upstream latency."
            )

        if metrics.session_rotation_count > 0:
            recommendations.append(
                f"ℹ {metrics.session_rotation_count} session rotation(s) "
                f"recorded. High rotation may indicate aggressive message "
                f"limits."
            )

        if not recommendations:
            recommendations.append("✓ No adjustments recommended. Performance is within normal range.")

        return recommendations

    def apply_recommendations(self) -> PoolConfig:
        """Evaluate and apply configuration changes.

        Calls :meth:`evaluate`, replaces the active config with the result,
        and logs the change for audit purposes.

        Returns
        -------
        PoolConfig
            The newly applied configuration.
        """
        new_config = self.evaluate()
        old_config = self._config

        changes: Dict[str, Any] = {}
        if old_config.max_sessions != new_config.max_sessions:
            changes["max_sessions"] = {
                "old": old_config.max_sessions,
                "new": new_config.max_sessions,
            }
        if old_config.max_messages_per_session != new_config.max_messages_per_session:
            changes["max_messages_per_session"] = {
                "old": old_config.max_messages_per_session,
                "new": new_config.max_messages_per_session,
            }
        if old_config.session_ttl != new_config.session_ttl:
            changes["session_ttl"] = {
                "old": old_config.session_ttl,
                "new": new_config.session_ttl,
            }

        if changes:
            self._config = new_config
            entry = {
                "timestamp": time.time(),
                "changes": changes,
                "metrics_snapshot": {
                    "truncation_rate": self._tracker.get_recent_metrics().truncation_rate,
                    "error_rate": self._tracker.get_recent_metrics().error_rate,
                    "success_rate": self._tracker.get_recent_metrics().success_rate,
                    "avg_response_time": self._tracker.get_recent_metrics().avg_response_time,
                },
            }
            self._change_log.append(entry)
            logger.info(
                "Applied adaptive config changes: %s",
                {k: f"{v['old']}→{v['new']}" for k, v in changes.items()},
            )
        else:
            logger.debug("No config changes to apply")

        return self._config

    def get_status(self) -> Dict[str, Any]:
        """Return a comprehensive status snapshot.

        Includes the current config, recent metrics, and recommendations.

        Returns
        -------
        dict
            Keys: ``config``, ``metrics``, ``recommendations``,
            ``change_count``, ``record_count``.
        """
        metrics = self._tracker.get_recent_metrics(window=20)
        return {
            "config": {
                "max_sessions": self._config.max_sessions,
                "max_messages_per_session": self._config.max_messages_per_session,
                "session_ttl": self._config.session_ttl,
            },
            "metrics": {
                "truncation_rate": round(metrics.truncation_rate, 4),
                "avg_response_time": round(metrics.avg_response_time, 4),
                "success_rate": round(metrics.success_rate, 4),
                "avg_tokens_per_response": round(metrics.avg_tokens_per_response, 2),
                "session_rotation_count": metrics.session_rotation_count,
                "error_rate": round(metrics.error_rate, 4),
            },
            "recommendations": self.get_recommendations(),
            "change_count": len(self._change_log),
            "record_count": self._tracker.record_count,
        }


# ---------------------------------------------------------------------------
# AutoTuner
# ---------------------------------------------------------------------------


class AutoTuner:
    """Periodically evaluates and auto-applies safe configuration changes.

    Runs a background :func:`asyncio.Task` that calls
    :meth:`AdaptiveManager.apply_recommendations` at a fixed interval.

    Only *safe* changes are applied automatically — the manager's built-in
    floors and ceilings prevent reducing parameters below minimums or
    above maximums.

    Parameters
    ----------
    manager:
        The :class:`AdaptiveManager` to drive.
    interval:
        Evaluation interval in seconds.  Defaults to 300 (5 minutes).

    Usage
    -----
    ::

        tuner = AutoTuner(manager, interval=300)
        await tuner.start()
        # ... application runs ...
        await tuner.stop()
    """

    def __init__(
        self,
        manager: AdaptiveManager,
        interval: float = _DEFAULT_TUNER_INTERVAL,
    ) -> None:
        self._manager = manager
        self._interval = interval
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._cycle_count = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """``True`` when the background evaluation loop is active."""
        return self._running

    @property
    def cycle_count(self) -> int:
        """Number of evaluation cycles completed."""
        return self._cycle_count

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background evaluation loop.

        No-op if already running.
        """
        if self._running:
            logger.warning("AutoTuner already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("AutoTuner started (interval=%.0fs)", self._interval)

    async def stop(self) -> None:
        """Stop the background evaluation loop.

        Waits for the current cycle (if any) to complete before returning.
        """
        if not self._running:
            return

        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("AutoTuner stopped after %d cycle(s)", self._cycle_count)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Background loop: evaluate → apply → sleep → repeat."""
        logger.info("AutoTuner loop started")
        try:
            while self._running:
                await asyncio.sleep(self._interval)
                if not self._running:
                    break

                try:
                    old_config = self._manager.config
                    new_config = self._manager.apply_recommendations()
                    self._cycle_count += 1

                    if new_config != old_config:
                        logger.info(
                            "AutoTuner cycle %d: config updated — "
                            "max_sessions=%d, max_messages=%d",
                            self._cycle_count,
                            new_config.max_sessions,
                            new_config.max_messages_per_session,
                        )
                    else:
                        logger.debug(
                            "AutoTuner cycle %d: no changes",
                            self._cycle_count,
                        )
                except Exception:
                    logger.exception("AutoTuner cycle %d failed", self._cycle_count + 1)
        except asyncio.CancelledError:
            logger.debug("AutoTuner loop cancelled")
            raise

        logger.info("AutoTuner loop exited")


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


async def main() -> None:
    """Simulate 50 requests with varying success/truncation and show adaptation."""
    import random

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    random.seed(42)

    print("=" * 65)
    print("Adaptive Session Management — Smoke Test (50 requests)")
    print("=" * 65)

    # --- Setup ---
    initial_config = PoolConfig(max_sessions=10, max_messages_per_session=50, session_ttl=3600)
    tracker = PerformanceTracker()
    manager = AdaptiveManager(pool_config=initial_config, tracker=tracker)

    print(f"\nInitial config:")
    print(f"  max_sessions: {initial_config.max_sessions}")
    print(f"  max_messages_per_session: {initial_config.max_messages_per_session}")
    print(f"  session_ttl: {initial_config.session_ttl}s")

    # --- Phase 1: 20 requests — high truncation, moderate errors ---
    print(f"\n{'─' * 65}")
    print("Phase 1: High truncation, moderate errors (20 requests)")
    print(f"{'─' * 65}")

    for i in range(20):
        success = random.random() > 0.25  # 75% success
        truncated = random.random() < 0.40  # 40% truncation
        tokens = random.randint(100, 2000) if success else 0
        duration = random.uniform(1.0, 8.0)
        tracker.record_request(duration=duration, success=success, tokens=tokens, truncated=truncated)

    metrics = tracker.get_recent_metrics()
    print(f"  Metrics after Phase 1:")
    print(f"    truncation_rate: {metrics.truncation_rate:.1%}")
    print(f"    error_rate:      {metrics.error_rate:.1%}")
    print(f"    success_rate:    {metrics.success_rate:.1%}")
    print(f"    avg_response_time: {metrics.avg_response_time:.2f}s")

    config_after_p1 = manager.apply_recommendations()
    print(f"  Config after adaptation:")
    print(f"    max_messages_per_session: {initial_config.max_messages_per_session} → {config_after_p1.max_messages_per_session}")
    print(f"    max_sessions:             {initial_config.max_sessions} → {config_after_p1.max_sessions}")

    for rec in manager.get_recommendations():
        print(f"    {rec}")

    # --- Phase 2: 15 requests — low truncation, high success, fast ---
    print(f"\n{'─' * 65}")
    print("Phase 2: Low truncation, high success, fast (15 requests)")
    print(f"{'─' * 65}")

    for i in range(15):
        success = random.random() > 0.05  # 95% success
        truncated = random.random() < 0.05  # 5% truncation
        tokens = random.randint(200, 1500)
        duration = random.uniform(0.5, 3.0)
        tracker.record_request(duration=duration, success=success, tokens=tokens, truncated=truncated)

    metrics = tracker.get_recent_metrics()
    print(f"  Metrics (last 20 window):")
    print(f"    truncation_rate: {metrics.truncation_rate:.1%}")
    print(f"    error_rate:      {metrics.error_rate:.1%}")
    print(f"    success_rate:    {metrics.success_rate:.1%}")
    print(f"    avg_response_time: {metrics.avg_response_time:.2f}s")

    config_after_p2 = manager.apply_recommendations()
    print(f"  Config after adaptation:")
    print(f"    max_messages_per_session: {config_after_p1.max_messages_per_session} → {config_after_p2.max_messages_per_session}")
    print(f"    max_sessions:             {config_after_p1.max_sessions} → {config_after_p2.max_sessions}")

    for rec in manager.get_recommendations():
        print(f"    {rec}")

    # --- Phase 3: 15 requests — high errors, slow responses ---
    print(f"\n{'─' * 65}")
    print("Phase 3: High errors, slow responses (15 requests)")
    print(f"{'─' * 65}")

    for i in range(15):
        success = random.random() > 0.50  # 50% success
        truncated = random.random() < 0.15
        tokens = random.randint(50, 800) if success else 0
        duration = random.uniform(15.0, 45.0)
        tracker.record_request(duration=duration, success=success, tokens=tokens, truncated=truncated)

    tracker.record_rotation()
    tracker.record_rotation()

    metrics = tracker.get_recent_metrics()
    print(f"  Metrics (last 20 window):")
    print(f"    truncation_rate: {metrics.truncation_rate:.1%}")
    print(f"    error_rate:      {metrics.error_rate:.1%}")
    print(f"    success_rate:    {metrics.success_rate:.1%}")
    print(f"    avg_response_time: {metrics.avg_response_time:.2f}s")
    print(f"    session_rotations: {metrics.session_rotation_count}")

    config_after_p3 = manager.apply_recommendations()
    print(f"  Config after adaptation:")
    print(f"    max_messages_per_session: {config_after_p2.max_messages_per_session} → {config_after_p3.max_messages_per_session}")
    print(f"    max_sessions:             {config_after_p2.max_sessions} → {config_after_p3.max_sessions}")

    for rec in manager.get_recommendations():
        print(f"    {rec}")

    # --- Full status dump ---
    print(f"\n{'─' * 65}")
    print("Full Status (get_status)")
    print(f"{'─' * 65}")

    status = manager.get_status()
    print(f"  Config:")
    for k, v in status["config"].items():
        print(f"    {k}: {v}")
    print(f"  Metrics:")
    for k, v in status["metrics"].items():
        print(f"    {k}: {v}")
    print(f"  Recommendations:")
    for r in status["recommendations"]:
        print(f"    {r}")
    print(f"  Total config changes: {status['change_count']}")
    print(f"  Total records tracked: {status['record_count']}")

    # --- Change log ---
    print(f"\n{'─' * 65}")
    print(f"Change Log ({len(manager.change_log)} entries)")
    print(f"{'─' * 65}")
    for i, entry in enumerate(manager.change_log):
        print(f"  Change #{i + 1}:")
        for param, delta in entry["changes"].items():
            print(f"    {param}: {delta['old']} → {delta['new']}")

    # --- AutoTuner quick demo (2 cycles at 0.1s interval) ---
    print(f"\n{'─' * 65}")
    print("AutoTuner demo (2 cycles, 0.1s interval)")
    print(f"{'─' * 65}")

    tuner = AutoTuner(manager, interval=0.1)
    await tuner.start()
    print(f"  AutoTuner running: {tuner.is_running}")
    await asyncio.sleep(0.35)
    await tuner.stop()
    print(f"  Cycles completed: {tuner.cycle_count}")
    print(f"  AutoTuner running: {tuner.is_running}")

    print(f"\n{'=' * 65}")
    print("ALL CHECKS PASSED ✓")
    print(f"{'=' * 65}")


if __name__ == "__main__":
    asyncio.run(main())
