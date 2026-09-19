"""Prometheus-compatible metrics collector for the DeepSeek Hybrid Router.

Provides a lightweight, stdlib-only metrics collection system with support
for counters, histograms, and gauges. Exports metrics in Prometheus text
exposition format via ``GET /metrics``.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple


def _labels_key(labels: Dict[str, str]) -> Tuple[Tuple[str, str], ...]:
    """Convert a labels dict to a sorted tuple for use as a dict key.

    Args:
        labels: Mapping of label names to values.

    Returns:
        Sorted tuple of (name, value) pairs.
    """
    return tuple(sorted(labels.items()))


def _format_labels(labels: Tuple[Tuple[str, str], ...]) -> str:
    """Format a sorted label tuple into Prometheus label syntax.

    Args:
        labels: Sorted tuple of (name, value) pairs.

    Returns:
        Prometheus-formatted label string, e.g. '{provider="deepseek",status="200"}'.
        Returns empty string if no labels.
    """
    if not labels:
        return ""
    parts = [f'{k}="{v}"' for k, v in labels]
    return "{" + ",".join(parts) + "}"


class MetricsCollector:
    """Thread-safe Prometheus-compatible metrics collector.

    Stores counters, gauges, and histograms in plain dicts with no
    external dependencies.  All mutation methods are safe to call from
    any thread (or asyncio task) — failures are silently swallowed so
    metrics never break the main request flow.

    Usage::

        mc = MetricsCollector()
        mc.increment_counter("router_requests_total", labels={"status": "200"})
        mc.observe_histogram("router_request_duration_seconds", value=0.42)
        mc.set_gauge("router_active_connections", value=5)
        text = mc.generate_prometheus_text()
    """

    # Default histogram buckets (seconds)
    _DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

    def __init__(self) -> None:
        """Initialize the collector with empty metric stores."""
        self._lock = threading.Lock()

        # counter_name -> { labels_key -> value }
        self._counters: Dict[str, Dict[Tuple[Tuple[str, str], ...], float]] = defaultdict(
            lambda: defaultdict(float)
        )

        # gauge_name -> { labels_key -> value }
        self._gauges: Dict[str, Dict[Tuple[Tuple[str, str], ...], float]] = defaultdict(
            lambda: defaultdict(float)
        )

        # histogram_name -> { labels_key -> { "buckets": {le: count}, "sum": float, "count": int } }
        self._histograms: Dict[
            str,
            Dict[Tuple[Tuple[str, str], ...], Dict[str, Any]],
        ] = defaultdict(lambda: defaultdict(lambda: {
            "buckets": defaultdict(int),
            "sum": 0.0,
            "count": 0,
        }))

        # Metadata: name -> (type, help_text, label_names)
        self._metadata: Dict[str, Tuple[str, str, List[str]]] = {}

        # Register pre-defined metrics
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Register the standard metrics used by the router."""
        self.register_metric(
            "router_requests_total",
            metric_type="counter",
            help_text="Total number of routed chat completion requests.",
            label_names=["provider", "model", "status"],
        )
        self.register_metric(
            "router_request_duration_seconds",
            metric_type="histogram",
            help_text="Request latency in seconds.",
            label_names=["provider", "model"],
        )
        self.register_metric(
            "router_active_connections",
            metric_type="gauge",
            help_text="Number of currently active provider connections.",
            label_names=["provider"],
        )
        self.register_metric(
            "router_fallback_count",
            metric_type="counter",
            help_text="Number of fallback events between providers.",
            label_names=["from_provider", "to_provider"],
        )

    def register_metric(
        self,
        name: str,
        metric_type: str,
        help_text: str,
        label_names: Optional[List[str]] = None,
    ) -> None:
        """Register a named metric with its type, help text, and label schema.

        Args:
            name: Metric name in Prometheus format (e.g. ``router_requests_total``).
            metric_type: One of ``counter``, ``gauge``, ``histogram``.
            help_text: Human-readable description shown in exposition output.
            label_names: Ordered list of label names for this metric.
        """
        self._metadata[name] = (metric_type, help_text, label_names or [])

    def increment_counter(self, name: str, labels: Optional[Dict[str, str]] = None, amount: float = 1.0) -> None:
        """Increment a counter metric by ``amount`` (default 1).

        Silently does nothing if the counter is not registered or an
        internal error occurs.

        Args:
            name: Metric name (must be a registered counter).
            labels: Optional label dict; missing labels are filled with ``""``.
            amount: Increment amount (default 1.0).
        """
        try:
            labels = labels or {}
            key = _labels_key(labels)
            with self._lock:
                self._counters[name][key] += amount
        except Exception:
            pass

    def set_gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """Set a gauge metric to an absolute value.

        Silently does nothing on error.

        Args:
            name: Metric name (must be a registered gauge).
            value: The gauge value to set.
            labels: Optional label dict.
        """
        try:
            labels = labels or {}
            key = _labels_key(labels)
            with self._lock:
                self._gauges[name][key] = value
        except Exception:
            pass

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Record an observation for a histogram metric.

        Updates bucket counts, sum, and total count.  Silently does
        nothing on error.

        Args:
            name: Metric name (must be a registered histogram).
            value: Observed value (e.g. request duration in seconds).
            labels: Optional label dict.
        """
        try:
            labels = labels or {}
            key = _labels_key(labels)
            with self._lock:
                entry = self._histograms[name][key]
                entry["sum"] += value
                entry["count"] += 1
                for le in self._DEFAULT_BUCKETS:
                    if value <= le:
                        entry["buckets"][le] += 1
                # +Inf bucket always gets incremented
                entry["buckets"][float("inf")] = entry["count"]
        except Exception:
            pass

    def generate_prometheus_text(self) -> str:
        """Generate Prometheus text exposition format output.

        Iterates over all registered metrics and emits ``# HELP``,
        ``# TYPE``, and data lines in the standard Prometheus text format.

        Returns:
            Multi-line string suitable for the ``/metrics`` endpoint body.
        """
        lines: List[str] = []

        with self._lock:
            # Counters
            for name, series_map in self._counters.items():
                meta = self._metadata.get(name)
                help_text = meta[1] if meta else ""
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} counter")
                for label_key, value in sorted(series_map.items()):
                    lines.append(f"{name}{_format_labels(label_key)} {value:.0f}")
                lines.append("")

            # Gauges
            for name, series_map in self._gauges.items():
                meta = self._metadata.get(name)
                help_text = meta[1] if meta else ""
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} gauge")
                for label_key, value in sorted(series_map.items()):
                    lines.append(f"{name}{_format_labels(label_key)} {value}")
                lines.append("")

            # Histograms
            for name, series_map in self._histograms.items():
                meta = self._metadata.get(name)
                help_text = meta[1] if meta else ""
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} histogram")
                for label_key, entry in sorted(series_map.items()):
                    base_labels = _format_labels(label_key)
                    buckets = entry["buckets"]
                    for le in sorted(buckets.keys(), key=lambda x: (x == float("inf"), x)):
                        le_str = "+Inf" if le == float("inf") else str(le)
                        # Merge le label into existing labels
                        if base_labels:
                            inner = base_labels[1:-1]  # strip { }
                            bucket_label = "{" + inner + f',le="{le_str}"' + "}"
                        else:
                            bucket_label = f'{{le="{le_str}"}}'
                        lines.append(f"{name}_bucket{bucket_label} {buckets[le]:.0f}")
                    lines.append(f"{name}_sum{base_labels} {entry['sum']:.6f}")
                    lines.append(f"{name}_count{base_labels} {entry['count']:.0f}")
                lines.append("")

        return "\n".join(lines) + "\n" if lines else ""


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: Optional[MetricsCollector] = None
_instance_lock = threading.Lock()


def get_metrics() -> MetricsCollector:
    """Return the process-wide MetricsCollector singleton.

    Creates the instance on first call.  Thread-safe.

    Returns:
        The global ``MetricsCollector`` instance.
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = MetricsCollector()
    return _instance
