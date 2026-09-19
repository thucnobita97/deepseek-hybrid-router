"""Session persistence for DeepSeek bridge sessions.

Caches bridge sessions (conversation_ids, auth tokens, cookies) with TTL
so they survive router restarts. Uses SQLite for durability with an
in-memory dict fallback when SQLite is unavailable.

A background task proactively refreshes sessions that are about to expire.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_DB_DIR = Path(__file__).resolve().parent / "data"
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "sessions.db"
_DEFAULT_TTL = 3600  # 1 hour
_REFRESH_WINDOW = 600  # 10 minutes before expiry → trigger refresh
_CHECK_INTERVAL = 300  # background check every 5 minutes

_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS sessions (
    key         TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    last_used   REAL NOT NULL
);
"""

# ---------------------------------------------------------------------------
# Session data helpers
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class SessionManager:
    """Manages bridge session lifecycle with TTL-based expiry.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file. Set to ``None`` to force
        in-memory mode.
    """

    def __init__(self, db_path: Optional[Path] = _DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._mem: Dict[str, Dict[str, Any]] = {}  # fallback store
        self._use_sqlite = False
        self._lock = asyncio.Lock()
        self._refresh_task: Optional[asyncio.Task] = None

        # Attempt SQLite initialisation
        if db_path is not None:
            try:
                db_path.parent.mkdir(parents=True, exist_ok=True)
                self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
                self._conn.execute("PRAGMA journal_mode=WAL;")
                self._conn.execute(_TABLE_SQL)
                self._conn.commit()
                self._use_sqlite = True
                logger.info("Session store: SQLite at %s", db_path)
            except Exception as exc:
                logger.warning("SQLite unavailable (%s), falling back to in-memory store", exc)
                self._conn = None
                self._use_sqlite = False
        else:
            logger.info("Session store: in-memory (no db_path)")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_session(self, key: str) -> Optional[Dict[str, Any]]:
        """Return cached session data if it exists and has not expired."""
        async with self._lock:
            row = self._raw_get(key)
            if row is None:
                return None

            expires_at: float = row["expires_at"]
            if _now() >= expires_at:
                # Expired — clean up
                self._raw_delete(key)
                return None

            # Update last_used timestamp
            self._touch(key)
            return {**row["data"], "_meta": {
                "key": key,
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "last_used": _now(),
            }}

    async def save_session(
        self, key: str, data: Dict[str, Any], ttl: int = _DEFAULT_TTL
    ) -> None:
        """Store session data with a TTL (seconds). Default 1 hour."""
        now = _now()
        async with self._lock:
            self._raw_put(
                key=key,
                data=data,
                created_at=now,
                expires_at=now + ttl,
                last_used=now,
            )

    async def delete_session(self, key: str) -> None:
        """Remove a session from the store."""
        async with self._lock:
            self._raw_delete(key)

    async def refresh_session(self, key: str) -> bool:
        """Check whether a session is close to expiry and extend it.

        Returns ``True`` if the session was refreshed (or did not need
        refreshing), ``False`` if the session was not found or already
        expired.

        The actual refresh logic (re-authenticating with the bridge) is
        delegated to ``_do_refresh`` which is a placeholder that
        subclasses or callers can override.
        """
        async with self._lock:
            row = self._raw_get(key)
            if row is None:
                return False

            if _now() >= row["expires_at"]:
                # Already expired — remove
                self._raw_delete(key)
                return False

            remaining = row["expires_at"] - _now()
            if remaining < _REFRESH_WINDOW:
                # Trigger refresh
                new_data = await self._do_refresh(key, row["data"])
                if new_data is not None:
                    now = _now()
                    self._raw_put(
                        key=key,
                        data=new_data,
                        created_at=row["created_at"],
                        expires_at=now + _DEFAULT_TTL,
                        last_used=now,
                    )
                    logger.info("Session %s refreshed, new TTL=%ds", key, _DEFAULT_TTL)
                    return True
                else:
                    logger.warning("Refresh failed for session %s", key)
                    return False

            # Still has plenty of time — no refresh needed
            return True

    async def list_sessions(self) -> List[Dict[str, Any]]:
        """Return metadata for all active (non-expired) sessions."""
        async with self._lock:
            now = _now()
            rows = self._raw_all()
            result = []
            for row in rows:
                if row["expires_at"] <= now:
                    # Lazy cleanup
                    self._raw_delete(row["key"])
                    continue
                result.append({
                    "key": row["key"],
                    "created_at": row["created_at"],
                    "expires_at": row["expires_at"],
                    "last_used": row["last_used"],
                    "ttl_remaining": int(row["expires_at"] - now),
                    "data_keys": list(row["data"].keys()),
                })
            return result

    # ------------------------------------------------------------------
    # Background proactive refresh
    # ------------------------------------------------------------------

    async def start_background_refresh(self) -> None:
        """Start the periodic background task that refreshes expiring sessions."""
        if self._refresh_task is not None:
            return
        self._refresh_task = asyncio.create_task(self._background_loop())
        logger.info("Session background refresh started (interval=%ds)", _CHECK_INTERVAL)

    async def stop_background_refresh(self) -> None:
        """Cancel the background refresh task."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
            logger.info("Session background refresh stopped")

    async def _background_loop(self) -> None:
        """Periodically scan for sessions nearing expiry and refresh them."""
        while True:
            try:
                await asyncio.sleep(_CHECK_INTERVAL)
                await self._refresh_expiring()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Error in session background refresh loop")

    async def _refresh_expiring(self) -> None:
        """Find and refresh all sessions within the refresh window."""
        now = _now()
        rows = self._raw_all()
        for row in rows:
            if row["expires_at"] <= now:
                # Expired — clean up
                self._raw_delete(row["key"])
                continue
            remaining = row["expires_at"] - now
            if remaining < _REFRESH_WINDOW:
                logger.debug("Proactive refresh: session %s (%.0fs remaining)", row["key"], remaining)
                await self.refresh_session(row["key"])

    # ------------------------------------------------------------------
    # Refresh hook — override or monkey-patch for real bridge refresh
    # ------------------------------------------------------------------

    async def _do_refresh(
        self, key: str, current_data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Placeholder refresh: extends the session with the same data.

        In production, this should call the bridge to re-authenticate
        or obtain a new conversation_id / token. Override this method
        or replace it on the instance.
        """
        logger.debug("Placeholder refresh for session %s", key)
        # Return the same data — the caller (refresh_session) will
        # reset the TTL.
        return current_data

    # ------------------------------------------------------------------
    # Close / cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Stop background tasks and close the database connection."""
        await self.stop_background_refresh()
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ------------------------------------------------------------------
    # Internal storage primitives (caller holds _lock)
    # ------------------------------------------------------------------

    def _raw_get(self, key: str) -> Optional[Dict[str, Any]]:
        """Fetch a row without locking. Returns dict or None."""
        if self._use_sqlite and self._conn is not None:
            cur = self._conn.execute(
                "SELECT key, data, created_at, expires_at, last_used FROM sessions WHERE key = ?",
                (key,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "key": row[0],
                "data": json.loads(row[1]),
                "created_at": row[2],
                "expires_at": row[3],
                "last_used": row[4],
            }
        else:
            entry = self._mem.get(key)
            if entry is None:
                return None
            return {**entry}  # shallow copy

    def _raw_put(
        self,
        key: str,
        data: Dict[str, Any],
        created_at: float,
        expires_at: float,
        last_used: float,
    ) -> None:
        """Insert or replace a row (caller holds lock)."""
        if self._use_sqlite and self._conn is not None:
            self._conn.execute(
                """INSERT OR REPLACE INTO sessions (key, data, created_at, expires_at, last_used)
                   VALUES (?, ?, ?, ?, ?)""",
                (key, json.dumps(data), created_at, expires_at, last_used),
            )
            self._conn.commit()
        else:
            self._mem[key] = {
                "key": key,
                "data": data,
                "created_at": created_at,
                "expires_at": expires_at,
                "last_used": last_used,
            }

    def _raw_delete(self, key: str) -> None:
        """Delete a row (caller holds lock)."""
        if self._use_sqlite and self._conn is not None:
            self._conn.execute("DELETE FROM sessions WHERE key = ?", (key,))
            self._conn.commit()
        else:
            self._mem.pop(key, None)

    def _raw_all(self) -> List[Dict[str, Any]]:
        """Return all rows (caller holds lock)."""
        if self._use_sqlite and self._conn is not None:
            cur = self._conn.execute(
                "SELECT key, data, created_at, expires_at, last_used FROM sessions"
            )
            results = []
            for row in cur.fetchall():
                results.append({
                    "key": row[0],
                    "data": json.loads(row[1]),
                    "created_at": row[2],
                    "expires_at": row[3],
                    "last_used": row[4],
                })
            return results
        else:
            return [{**v} for v in self._mem.values()]

    def _touch(self, key: str) -> None:
        """Update last_used to now (caller holds lock)."""
        now = _now()
        if self._use_sqlite and self._conn is not None:
            self._conn.execute(
                "UPDATE sessions SET last_used = ? WHERE key = ?", (now, key)
            )
            self._conn.commit()
        else:
            if key in self._mem:
                self._mem[key]["last_used"] = now


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """Return the process-wide SessionManager, creating it on first call."""
    global _manager
    if _manager is None:
        _manager = SessionManager()
    return _manager


def reset_session_manager() -> None:
    """Discard the global manager (used in tests / shutdown)."""
    global _manager
    _manager = None


# ---------------------------------------------------------------------------
# FastAPI router — session management endpoints
# ---------------------------------------------------------------------------

sessions_router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@sessions_router.get("")
async def list_all_sessions():
    """List all active sessions with metadata."""
    mgr = get_session_manager()
    sessions = await mgr.list_sessions()
    return {"sessions": sessions, "count": len(sessions)}


@sessions_router.get("/{key:path}")
async def get_session_by_key(key: str):
    """Retrieve a single session by key."""
    mgr = get_session_manager()
    session = await mgr.get_session(key)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{key}' not found or expired")
    return session


@sessions_router.delete("/{key:path}")
async def delete_session_by_key(key: str):
    """Delete a session."""
    mgr = get_session_manager()
    await mgr.delete_session(key)
    return {"deleted": key}


@sessions_router.post("/refresh")
async def refresh_session_endpoint(payload: Dict[str, Any]):
    """Refresh a session that is close to expiry.

    Expects JSON body: ``{"key": "..."}``
    """
    key = payload.get("key")
    if not key:
        raise HTTPException(status_code=400, detail="Missing 'key' in request body")

    mgr = get_session_manager()
    success = await mgr.refresh_session(key)
    if not success:
        raise HTTPException(status_code=404, detail=f"Session '{key}' not found or already expired")
    return {"refreshed": key, "success": True}
