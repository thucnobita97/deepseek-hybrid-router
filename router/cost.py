"""Cost tracking and budget management for the DeepSeek Hybrid Router."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional

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
        self._load_pricing()
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

        conn = sqlite3.connect(str(self.db_path))
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
        conn.close()
        logger.info("Cost tracking database initialized: %s", self.db_path)

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

        conn = sqlite3.connect(str(self.db_path))
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
        conn.close()

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
        conn = sqlite3.connect(str(self.db_path))
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

        conn.close()

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
        conn = sqlite3.connect(str(self.db_path))
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

        conn.close()

        return {
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "total_cost": total_cost,
            "breakdown_by_provider": breakdown,
        }

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
            conn = sqlite3.connect(str(self.db_path))
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
            conn.close()

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


# Singleton instance
_cost_tracker_instance: Optional[CostTracker] = None


def get_cost_tracker() -> CostTracker:
    """Get or create the singleton CostTracker instance."""
    global _cost_tracker_instance
    if _cost_tracker_instance is None:
        _cost_tracker_instance = CostTracker()
    return _cost_tracker_instance
