"""Multi-account manager for DeepSeek bridge instances.

Manages multiple DeepSeek bridge accounts with load balancing, health tracking,
automatic recovery, and SQLite persistence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = "router/data/accounts.db"
_DEFAULT_CONFIG_PATH = "config/accounts.yaml"
_DEFAULT_COOLDOWN_SECONDS = 60
_DEFAULT_RECOVERY_INTERVAL = 30
_VALID_STATUSES = {"active", "rate_limited", "expired", "error"}
_VALID_STRATEGIES = {"round_robin", "least_used", "random", "weighted"}


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class Account:
    """Represents a single DeepSeek bridge account."""

    account_id: str
    bridge_url: str
    session_data: Dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    reason: Optional[str] = None
    usage_count: int = 0
    weight: int = 1
    last_used: Optional[float] = None
    status_changed_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Account":
        return cls(
            account_id=row["account_id"],
            bridge_url=row["bridge_url"],
            session_data=json.loads(row["session_data"] or "{}"),
            status=row["status"],
            reason=row["reason"],
            usage_count=row["usage_count"],
            weight=row["weight"],
            last_used=row["last_used"],
            status_changed_at=row["status_changed_at"],
            created_at=row["created_at"],
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id        TEXT PRIMARY KEY,
    bridge_url        TEXT NOT NULL,
    session_data      TEXT NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL DEFAULT 'active',
    reason            TEXT,
    usage_count       INTEGER NOT NULL DEFAULT 0,
    weight            INTEGER NOT NULL DEFAULT 1,
    last_used         REAL,
    status_changed_at REAL,
    created_at        REAL NOT NULL
);
"""


class _AccountStore:
    """Thin SQLite persistence layer (thread-safe via a lock)."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- writes ---------------------------------------------------------

    def upsert(self, acct: Account) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO accounts
                       (account_id, bridge_url, session_data, status, reason,
                        usage_count, weight, last_used, status_changed_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(account_id) DO UPDATE SET
                       bridge_url        = excluded.bridge_url,
                       session_data      = excluded.session_data,
                       status            = excluded.status,
                       reason            = excluded.reason,
                       usage_count       = excluded.usage_count,
                       weight            = excluded.weight,
                       last_used         = excluded.last_used,
                       status_changed_at = excluded.status_changed_at,
                       created_at        = excluded.created_at
                """,
                (
                    acct.account_id,
                    acct.bridge_url,
                    json.dumps(acct.session_data),
                    acct.status,
                    acct.reason,
                    acct.usage_count,
                    acct.weight,
                    acct.last_used,
                    acct.status_changed_at,
                    acct.created_at,
                ),
            )
            self._conn.commit()

    def update_status(
        self, account_id: str, status: str, reason: Optional[str]
    ) -> bool:
        with self._lock:
            cur = self._conn.execute(
                """UPDATE accounts
                      SET status = ?, reason = ?, status_changed_at = ?
                    WHERE account_id = ?""",
                (status, reason, time.time(), account_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def increment_usage(self, account_id: str) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE accounts
                      SET usage_count = usage_count + 1, last_used = ?
                    WHERE account_id = ?""",
                (time.time(), account_id),
            )
            self._conn.commit()

    def delete(self, account_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM accounts WHERE account_id = ?", (account_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    # -- reads ----------------------------------------------------------

    def load_all(self) -> List[Account]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM accounts ORDER BY created_at"
            ).fetchall()
        return [Account.from_row(r) for r in rows]

    def load(self, account_id: str) -> Optional[Account]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
        return Account.from_row(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def _load_config(config_path: str) -> Dict[str, Any]:
    """Load accounts.yaml and return the parsed dict."""
    path = Path(config_path)
    if not path.exists():
        return {"accounts": [], "settings": {}}
    with open(path) as fh:
        return yaml.safe_load(fh) or {"accounts": [], "settings": {}}


# ---------------------------------------------------------------------------
# AccountManager
# ---------------------------------------------------------------------------


class AccountManager:
    """Manages multiple DeepSeek bridge accounts.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.
    config_path:
        Path to the YAML configuration file.
    cooldown_seconds:
        How long a rate-limited account stays blocked before auto-recovery.
    recovery_interval:
        How often (seconds) the background recovery task runs.
    """

    _instance: Optional["AccountManager"] = None

    def __init__(
        self,
        db_path: str = _DEFAULT_DB_PATH,
        config_path: str = _DEFAULT_CONFIG_PATH,
        cooldown_seconds: int = _DEFAULT_COOLDOWN_SECONDS,
        recovery_interval: int = _DEFAULT_RECOVERY_INTERVAL,
    ) -> None:
        self._db_path = db_path
        self._config_path = config_path
        self.cooldown_seconds = cooldown_seconds
        self.recovery_interval = recovery_interval

        self._store = _AccountStore(db_path)
        self._accounts: Dict[str, Account] = {}
        self._rr_index: int = 0
        self._lock = threading.Lock()
        self._recovery_task: Optional[asyncio.Task] = None

        # Load persisted accounts
        for acct in self._store.load_all():
            self._accounts[acct.account_id] = acct

        # Load config-defined accounts (merge, don't overwrite existing state)
        self._load_config_accounts()

    # -- singleton helpers ----------------------------------------------

    @classmethod
    def get_instance(cls, **kwargs: Any) -> "AccountManager":
        if cls._instance is None:
            cls._instance = cls(**kwargs)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        if cls._instance is not None:
            cls._instance.close()
            cls._instance = None

    # -- config loader --------------------------------------------------

    def _load_config_accounts(self) -> None:
        cfg = _load_config(self._config_path)
        self.cooldown_seconds = cfg.get("settings", {}).get(
            "cooldown_seconds", self.cooldown_seconds
        )
        self.recovery_interval = cfg.get("settings", {}).get(
            "recovery_interval", self.recovery_interval
        )

        for entry in cfg.get("accounts", []):
            aid = entry.get("id")
            if not aid:
                continue
            if aid in self._accounts:
                # Config may update bridge_url/weight but not reset status
                acct = self._accounts[aid]
                acct.bridge_url = entry.get("bridge_url", acct.bridge_url)
                acct.weight = entry.get("weight", acct.weight)
                self._store.upsert(acct)
            else:
                acct = Account(
                    account_id=aid,
                    bridge_url=entry.get("bridge_url", "http://localhost:8000"),
                    session_data=entry.get("session_data", {}),
                    weight=entry.get("weight", 1),
                )
                self._accounts[aid] = acct
                self._store.upsert(acct)

    def reload_config(self) -> int:
        """Reload accounts from YAML config. Returns number of new accounts added."""
        before = len(self._accounts)
        self._load_config_accounts()
        return len(self._accounts) - before

    # -- core API -------------------------------------------------------

    def add_account(
        self,
        account_id: str,
        bridge_url: str,
        session_data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Register a new bridge account.

        Returns ``True`` if the account was added, ``False`` if it already exists.
        """
        with self._lock:
            if account_id in self._accounts:
                return False
            acct = Account(
                account_id=account_id,
                bridge_url=bridge_url,
                session_data=session_data or {},
            )
            self._accounts[account_id] = acct
            self._store.upsert(acct)
            logger.info("Account added: %s → %s", account_id, bridge_url)
            return True

    def get_account(self, strategy: str = "round_robin") -> Optional[Dict[str, Any]]:
        """Return the next available account dict, or ``None`` if all are unhealthy.

        Strategies
        ----------
        round_robin : cycle through active accounts in order.
        least_used  : pick the active account with the lowest usage_count.
        random      : random choice among active accounts.
        weighted    : weighted random using each account's ``weight`` field.
        """
        if strategy not in _VALID_STRATEGIES:
            raise ValueError(
                f"Unknown strategy {strategy!r}. "
                f"Valid: {sorted(_VALID_STRATEGIES)}"
            )

        # Auto-recover inline before selection
        self._try_recover_accounts()

        with self._lock:
            healthy = [
                a for a in self._accounts.values() if a.status == "active"
            ]
            if not healthy:
                return None

            if strategy == "round_robin":
                idx = self._rr_index % len(healthy)
                chosen = healthy[idx]
                self._rr_index = idx + 1

            elif strategy == "least_used":
                chosen = min(healthy, key=lambda a: a.usage_count)

            elif strategy == "random":
                chosen = random.choice(healthy)

            elif strategy == "weighted":
                weights = [a.weight for a in healthy]
                chosen = random.choices(healthy, weights=weights, k=1)[0]

            else:  # pragma: no cover — guarded above
                raise ValueError(strategy)

        # Bump usage outside lock (store has its own lock)
        self._store.increment_usage(chosen.account_id)
        with self._lock:
            chosen.usage_count += 1
            chosen.last_used = time.time()

        return chosen.to_dict()

    def mark_account_status(
        self,
        account_id: str,
        status: str,
        reason: Optional[str] = None,
    ) -> bool:
        """Update an account's health status.

        Parameters
        ----------
        status:
            One of ``active``, ``rate_limited``, ``expired``, ``error``.
        reason:
            Optional human-readable reason for the status change.

        Returns ``True`` if the account was found and updated.
        """
        if status not in _VALID_STATUSES:
            raise ValueError(
                f"Invalid status {status!r}. Valid: {sorted(_VALID_STATUSES)}"
            )

        with self._lock:
            acct = self._accounts.get(account_id)
            if acct is None:
                return False
            acct.status = status
            acct.reason = reason
            acct.status_changed_at = time.time()

        self._store.update_status(account_id, status, reason)
        logger.info(
            "Account %s → %s%s",
            account_id,
            status,
            f" ({reason})" if reason else "",
        )
        return True

    def get_account_health(self) -> List[Dict[str, Any]]:
        """Return status information for every registered account."""
        with self._lock:
            return [a.to_dict() for a in self._accounts.values()]

    def remove_account(self, account_id: str) -> bool:
        """Remove an account. Returns ``True`` if it existed and was removed."""
        with self._lock:
            if account_id not in self._accounts:
                return False
            del self._accounts[account_id]
        self._store.delete(account_id)
        logger.info("Account removed: %s", account_id)
        return True

    # -- auto-recovery --------------------------------------------------

    def _try_recover_accounts(self) -> None:
        """Check rate_limited accounts and re-enable those past cooldown."""
        now = time.time()
        with self._lock:
            for acct in self._accounts.values():
                if acct.status != "rate_limited":
                    continue
                if acct.status_changed_at is None:
                    continue
                elapsed = now - acct.status_changed_at
                if elapsed >= self.cooldown_seconds:
                    logger.info(
                        "Auto-recovering account %s (rate_limited for %.0fs)",
                        acct.account_id,
                        elapsed,
                    )
                    acct.status = "active"
                    acct.reason = None
                    acct.status_changed_at = now
                    self._store.update_status(acct.account_id, "active", None)

    async def _recovery_loop(self) -> None:
        """Background task that periodically recovers rate-limited accounts."""
        while True:
            try:
                self._try_recover_accounts()
            except Exception:
                logger.exception("Recovery loop error")
            await asyncio.sleep(self.recovery_interval)

    def start_recovery(self) -> None:
        """Start the background auto-recovery task (call from lifespan)."""
        if self._recovery_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running event loop; recovery task not started")
            return
        self._recovery_task = loop.create_task(self._recovery_loop())
        logger.info(
            "Account recovery task started (interval=%ds, cooldown=%ds)",
            self.recovery_interval,
            self.cooldown_seconds,
        )

    def stop_recovery(self) -> None:
        """Cancel the background recovery task."""
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            self._recovery_task = None
            logger.info("Account recovery task stopped")

    # -- lifecycle ------------------------------------------------------

    def close(self) -> None:
        self.stop_recovery()
        self._store.close()

    # -- repr -----------------------------------------------------------

    def __repr__(self) -> str:
        total = len(self._accounts)
        active = sum(1 for a in self._accounts.values() if a.status == "active")
        return f"<AccountManager accounts={total} active={active}>"
