"""Cost tracking and budget management for the DeepSeek Hybrid Router."""
from __future__ import annotations

import csv
import io
import json
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Union

import logging

logger = logging.getLogger(__name__)


class CostTracker:
    """Tracks token usage and costs per request, session, and overall."""

    def __init__(
        self,
        pricing_path: str = "config/pricing.json",
        db_path: str = "router/data/costs.db",
    ):
        self.pricing_path = Path(pricing_path)
        self.db_path = Path(db_path)
        self.pricing: Dict[str, Dict] = {}
        self.budget_config: Dict = {
            "daily_threshold_usd": 5.00,
            "warning_percent": 80,
            "alert_percent": 100,
        }
        self._pool_size = 3
        self._connections: List[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        self._pool: queue.Queue = queue.Queue(maxsize=self._pool_size)
        self._load_pricing()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_connections()
        self._init_db()

    def _load_pricing(self) -> None:
        """Load pricing configuration from JSON file."""
        if not self.pricing_path.exists():
            logger.warning("Pricing file not found: %s", self.pricing_path)
            return

        try:
            with open(self.pricing_path, "r") as f:
                data = json.load(f)
                self.pricing = data.get("pricing", {})
                self.budget_config.update(data.get("budget", {}))
                logger.info("Loaded pricing for %d models", len(self.pricing))
        except Exception as exc:
            logger.error("Failed to load pricing: %s", exc)

    def _init_db(self) -> None:
        """Initialize SQLite database for cost tracking."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with self.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS usage_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    session_id TEXT,
                    cost REAL NOT NULL
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_session ON usage_records(session_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp ON usage_records(timestamp)
            """)

            conn.commit()
        logger.info("Cost tracking database initialized: %s", self.db_path)

    def _init_connections(self) -> None:
        """Open persistent connections with PRAGMA optimizations."""
        for i in range(self._pool_size):
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-8000")
            conn.execute("PRAGMA temp_store=MEMORY")
            self._connections.append(conn)
            self._pool.put(conn)
        logger.info("Connection pool initialized with %d connections", self._pool_size)

    @contextmanager
    def get_connection(self):
        """Yield a connection from the pool; return it when done."""
        conn = self._pool.get()
        try:
            yield conn
        finally:
            self._pool.put(conn)

    def close(self) -> None:
        """Close all pooled connections."""
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except queue.Empty:
                break
        self._connections.clear()
        logger.info("Connection pool closed")

    def _calculate_cost(
        self, provider: str, model: str, prompt_tokens: int, completion_tokens: int
    ) -> float:
        """Calculate cost for a request based on pricing config."""
        key = f"{provider}:{model}"
        pricing_entry = self.pricing.get(key, {})

        input_rate = pricing_entry.get("input_cost_per_1k", 0.0)
        output_rate = pricing_entry.get("output_cost_per_1k", 0.0)

        cost = (prompt_tokens / 1000.0) * input_rate + (
            completion_tokens / 1000.0
        ) * output_rate
        return cost

    def record_usage(
        self,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        session_id: Optional[str] = None,
    ) -> Dict:
        """
        Record token usage for a request.

        Args:
            provider: Provider name (e.g., "deepinfra")
            model: Model name (e.g., "V4-Flash-0731")
            prompt_tokens: Number of prompt tokens
            completion_tokens: Number of completion tokens
            session_id: Optional session identifier

        Returns:
            Dict with request cost details
        """
        cost = self._calculate_cost(provider, model, prompt_tokens, completion_tokens)
        timestamp = int(time.time())

        with self._conn_lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO usage_records
                    (timestamp, provider, model, prompt_tokens, completion_tokens, session_id, cost)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        timestamp,
                        provider,
                        model,
                        prompt_tokens,
                        completion_tokens,
                        session_id,
                        cost,
                    ),
                )
                conn.commit()

        logger.info(
            "Recorded usage: %s:%s tokens=%d/%d cost=$%.4f session=%s",
            provider,
            model,
            prompt_tokens,
            completion_tokens,
            cost,
            session_id,
        )

        return {
            "provider": provider,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cost": cost,
            "session_id": session_id,
        }

    def get_session_cost(self, session_id: str) -> Dict:
        """
        Get cost breakdown for a specific session.

        Args:
            session_id: Session identifier

        Returns:
            Dict with session cost details including breakdown by provider
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Get totals for session
            cursor.execute(
                """
                SELECT
                    SUM(prompt_tokens) as total_prompt,
                    SUM(completion_tokens) as total_completion,
                    SUM(cost) as total_cost
                FROM usage_records
                WHERE session_id = ?
            """,
                (session_id,),
            )

            totals = cursor.fetchone()
            total_prompt = totals[0] or 0
            total_completion = totals[1] or 0
            total_cost = totals[2] or 0.0

            # Get breakdown by provider
            cursor.execute(
                """
                SELECT
                    provider,
                    SUM(prompt_tokens) as prompt_tokens,
                    SUM(completion_tokens) as completion_tokens,
                    SUM(cost) as cost
                FROM usage_records
                WHERE session_id = ?
                GROUP BY provider
            """,
                (session_id,),
            )

            breakdown = {}
            for row in cursor.fetchall():
                breakdown[row[0]] = {
                    "prompt_tokens": row[1],
                    "completion_tokens": row[2],
                    "total_tokens": row[1] + row[2],
                    "cost": row[3],
                }

        return {
            "session_id": session_id,
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "total_cost": total_cost,
            "breakdown_by_provider": breakdown,
        }

    def get_total_cost(self) -> Dict:
        """
        Get all-time cost totals.

        Returns:
            Dict with all-time totals and breakdown by provider
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Get overall totals
            cursor.execute(
                """
                SELECT
                    SUM(prompt_tokens) as total_prompt,
                    SUM(completion_tokens) as total_completion,
                    SUM(cost) as total_cost
                FROM usage_records
            """
            )

            totals = cursor.fetchone()
            total_prompt = totals[0] or 0
            total_completion = totals[1] or 0
            total_cost = totals[2] or 0.0

            # Get breakdown by provider
            cursor.execute(
                """
                SELECT
                    provider,
                    SUM(prompt_tokens) as prompt_tokens,
                    SUM(completion_tokens) as completion_tokens,
                    SUM(cost) as cost
                FROM usage_records
                GROUP BY provider
            """
            )

            breakdown = {}
            for row in cursor.fetchall():
                breakdown[row[0]] = {
                    "prompt_tokens": row[1],
                    "completion_tokens": row[2],
                    "total_tokens": row[1] + row[2],
                    "cost": row[3],
                }

        return {
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "total_cost": total_cost,
            "breakdown_by_provider": breakdown,
        }

    def export_usage_data(self, days: int = 7, format: str = 'csv') -> Union[str, List[Dict]]:
        """
        Export usage records for the last N days as CSV string or list of dicts.

        Args:
            days: Number of days to look back (default: 7).
            format: Output format — 'csv' returns a CSV string, 'json' returns a list of dicts.

        Returns:
            CSV string if format='csv', or list of dicts if format='json'.
            Each record contains: timestamp, provider, model, prompt_tokens, completion_tokens, cost.
        """
        cutoff = int(time.time()) - (days * 86400)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT timestamp, provider, model, prompt_tokens, completion_tokens, cost
                FROM usage_records
                WHERE timestamp >= ?
                ORDER BY timestamp ASC
                """,
                (cutoff,),
            )
            rows = cursor.fetchall()

        fields = ['timestamp', 'provider', 'model', 'prompt_tokens', 'completion_tokens', 'cost']
        records = [
            dict(zip(fields, row))
            for row in rows
        ]

        if format == 'json':
            return records

        # CSV format
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
        return output.getvalue()

    def check_budget(self, session_id: Optional[str] = None) -> Dict:
        """
        Check if budget threshold is exceeded.

        Args:
            session_id: Optional session ID to check session-specific budget

        Returns:
            Dict with budget status including within_budget, current_cost,
            threshold, and warning_level
        """
        threshold = self.budget_config.get("daily_threshold_usd", 5.00)
        warning_percent = self.budget_config.get("warning_percent", 80)
        alert_percent = self.budget_config.get("alert_percent", 100)

        # Get current cost (session or daily)
        if session_id:
            cost_data = self.get_session_cost(session_id)
            current_cost = cost_data["total_cost"]
        else:
            # For daily budget, get today's cost
            with self.get_connection() as conn:
                cursor = conn.cursor()

                # Get start of today (midnight UTC)
                now = int(time.time())
                today_start = now - (now % 86400)

                cursor.execute(
                    """
                    SELECT SUM(cost) as total_cost
                    FROM usage_records
                    WHERE timestamp >= ?
                """,
                    (today_start,),
                )

                result = cursor.fetchone()
                current_cost = result[0] or 0.0

        # Determine warning level
        if current_cost >= threshold * (alert_percent / 100.0):
            warning_level = "alert"
            within_budget = False
        elif current_cost >= threshold * (warning_percent / 100.0):
            warning_level = "warning"
            within_budget = True
        else:
            warning_level = "ok"
            within_budget = True

        return {
            "within_budget": within_budget,
            "current_cost": current_cost,
            "threshold": threshold,
            "warning_level": warning_level,
            "warning_percent": warning_percent,
            "alert_percent": alert_percent,
            "session_id": session_id,
        }

    def get_usage_trends(self, hours: int = 24) -> Dict:
        """
        Return hourly usage data points for the last N hours.

        Args:
            hours: Number of hours to look back (default 24).

        Returns:
            Dict with an hourly time-series of token counts, request counts,
            and costs.
        """
        now = int(time.time())
        start_ts = now - (hours * 3600)

        with self.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT
                    (timestamp / 3600) * 3600 AS hour_bucket,
                    COUNT(*) AS request_count,
                    SUM(prompt_tokens) AS prompt_tokens,
                    SUM(completion_tokens) AS completion_tokens,
                    SUM(cost) AS cost
                FROM usage_records
                WHERE timestamp >= ?
                GROUP BY hour_bucket
                ORDER BY hour_bucket
                """,
                (start_ts,),
            )

            data_points = []
            for row in cursor.fetchall():
                data_points.append(
                    {
                        "timestamp": row[0],
                        "request_count": row[1],
                        "prompt_tokens": row[2] or 0,
                        "completion_tokens": row[3] or 0,
                        "total_tokens": (row[2] or 0) + (row[3] or 0),
                        "cost": row[4] or 0.0,
                    }
                )

        return {
            "hours": hours,
            "data_points": data_points,
        }

    def get_hourly_stats(self) -> Dict:
        """
        Return usage statistics for the last 24 hours aggregated by hour.

        Returns:
            Dict with hourly buckets containing request count, token totals,
            and cost for each hour in the past 24 hours.
        """
        now = int(time.time())
        start_ts = now - (24 * 3600)

        with self.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT
                    (timestamp / 3600) * 3600 AS hour_bucket,
                    COUNT(*) AS request_count,
                    SUM(prompt_tokens) AS prompt_tokens,
                    SUM(completion_tokens) AS completion_tokens,
                    SUM(cost) AS cost
                FROM usage_records
                WHERE timestamp >= ?
                GROUP BY hour_bucket
                ORDER BY hour_bucket
                """,
                (start_ts,),
            )

            hours = []
            total_requests = 0
            total_cost = 0.0
            total_prompt = 0
            total_completion = 0

            for row in cursor.fetchall():
                prompt = row[2] or 0
                completion = row[3] or 0
                cost = row[4] or 0.0
                req_count = row[1]

                hours.append(
                    {
                        "hour": row[0],
                        "request_count": req_count,
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "total_tokens": prompt + completion,
                        "cost": cost,
                    }
                )
                total_requests += req_count
                total_cost += cost
                total_prompt += prompt
                total_completion += completion

        return {
            "period_hours": 24,
            "hours": hours,
            "summary": {
                "total_requests": total_requests,
                "total_prompt_tokens": total_prompt,
                "total_completion_tokens": total_completion,
                "total_tokens": total_prompt + total_completion,
                "total_cost": total_cost,
            },
        }

    def get_daily_stats(self, days: int = 7) -> Dict:
        """
        Return usage statistics for the last N days aggregated by day.

        Args:
            days: Number of days to look back (default 7).

        Returns:
            Dict with daily buckets containing request count, token totals,
            and cost for each day.
        """
        now = int(time.time())
        start_ts = now - (days * 86400)

        with self.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT
                    (timestamp / 86400) * 86400 AS day_bucket,
                    COUNT(*) AS request_count,
                    SUM(prompt_tokens) AS prompt_tokens,
                    SUM(completion_tokens) AS completion_tokens,
                    SUM(cost) AS cost
                FROM usage_records
                WHERE timestamp >= ?
                GROUP BY day_bucket
                ORDER BY day_bucket
                """,
                (start_ts,),
            )

            days_data = []
            total_requests = 0
            total_cost = 0.0
            total_prompt = 0
            total_completion = 0

            for row in cursor.fetchall():
                prompt = row[2] or 0
                completion = row[3] or 0
                cost = row[4] or 0.0
                req_count = row[1]

                days_data.append(
                    {
                        "day": row[0],
                        "request_count": req_count,
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "total_tokens": prompt + completion,
                        "cost": cost,
                    }
                )
                total_requests += req_count
                total_cost += cost
                total_prompt += prompt
                total_completion += completion

        return {
            "period_days": days,
            "days": days_data,
            "summary": {
                "total_requests": total_requests,
                "total_prompt_tokens": total_prompt,
                "total_completion_tokens": total_completion,
                "total_tokens": total_prompt + total_completion,
                "total_cost": total_cost,
            },
        }

    def get_top_models(self, limit: int = 10) -> Dict:
        """
        Return the top N most-used models ranked by total token count.

        Args:
            limit: Maximum number of models to return (default 10).

        Returns:
            Dict with a list of models ordered by total tokens descending,
            including token breakdown, cost, and request count.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT
                    provider,
                    model,
                    COUNT(*) AS request_count,
                    SUM(prompt_tokens) AS prompt_tokens,
                    SUM(completion_tokens) AS completion_tokens,
                    SUM(cost) AS cost
                FROM usage_records
                GROUP BY provider, model
                ORDER BY (SUM(prompt_tokens) + SUM(completion_tokens)) DESC
                LIMIT ?
                """,
                (limit,),
            )

            models = []
            for row in cursor.fetchall():
                prompt = row[3] or 0
                completion = row[4] or 0
                models.append(
                    {
                        "provider": row[0],
                        "model": row[1],
                        "request_count": row[2],
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "total_tokens": prompt + completion,
                        "cost": row[5] or 0.0,
                    }
                )

        return {
            "limit": limit,
            "models": models,
        }

    def get_provider_usage_trends(self, hours: int = 24) -> Dict:
        """
        Return usage breakdown by provider over the last N hours.

        Args:
            hours: Number of hours to look back (default 24).

        Returns:
            Dict keyed by provider name, each containing hourly time-series
            data points and an overall summary for that provider.
        """
        now = int(time.time())
        start_ts = now - (hours * 3600)

        with self.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT
                    provider,
                    (timestamp / 3600) * 3600 AS hour_bucket,
                    COUNT(*) AS request_count,
                    SUM(prompt_tokens) AS prompt_tokens,
                    SUM(completion_tokens) AS completion_tokens,
                    SUM(cost) AS cost
                FROM usage_records
                WHERE timestamp >= ?
                GROUP BY provider, hour_bucket
                ORDER BY provider, hour_bucket
                """,
                (start_ts,),
            )

            providers: Dict[str, Dict] = {}
            for row in cursor.fetchall():
                provider = row[0]
                prompt = row[3] or 0
                completion = row[4] or 0
                cost = row[5] or 0.0
                req_count = row[2]

                if provider not in providers:
                    providers[provider] = {
                        "data_points": [],
                        "summary": {
                            "total_requests": 0,
                            "total_prompt_tokens": 0,
                            "total_completion_tokens": 0,
                            "total_tokens": 0,
                            "total_cost": 0.0,
                        },
                    }

                providers[provider]["data_points"].append(
                    {
                        "timestamp": row[1],
                        "request_count": req_count,
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "total_tokens": prompt + completion,
                        "cost": cost,
                    }
                )

                s = providers[provider]["summary"]
                s["total_requests"] += req_count
                s["total_prompt_tokens"] += prompt
                s["total_completion_tokens"] += completion
                s["total_tokens"] += prompt + completion
                s["total_cost"] += cost

        return {
            "hours": hours,
            "providers": providers,
        }


# Singleton instance
_cost_tracker_instance: Optional[CostTracker] = None


def get_cost_tracker() -> CostTracker:
    """Get or create the singleton CostTracker instance."""
    global _cost_tracker_instance
    if _cost_tracker_instance is None:
        _cost_tracker_instance = CostTracker()
    return _cost_tracker_instance
