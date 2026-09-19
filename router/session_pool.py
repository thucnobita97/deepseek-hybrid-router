"""Session Pool Manager — manages multiple bridge sessions with rotation.

Provides a pool of reusable bridge sessions with automatic rotation when
a session exceeds its message limit, health tracking, and round-robin
selection among healthy sessions.

Integration
-----------
Uses :class:`router.session.SessionManager` for persistent storage so
pool state survives router restarts.  The pool itself lives in memory
(fast lookups) while metadata is mirrored to SQLite via the manager.

Concurrency
-----------
All public methods are coroutine-safe and protected by an
:class:`asyncio.Lock`.  The round-robin index is advanced atomically
inside the lock.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from router.session import SessionManager, get_session_manager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / Defaults
# ---------------------------------------------------------------------------

_DEFAULT_MAX_SESSIONS = 10
_DEFAULT_MAX_MESSAGES = 50
_DEFAULT_SESSION_TTL = 3600  # seconds
_POOL_KEY_PREFIX = "pool:"


# ---------------------------------------------------------------------------
# Enums & Dataclasses
# ---------------------------------------------------------------------------


class SessionState(str, enum.Enum):
    """Lifecycle state of a pooled session."""

    ACTIVE = "active"
    FULL = "full"
    EXPIRED = "expired"
    ERROR = "error"


@dataclass
class PooledSession:
    """In-memory representation of one session in the pool."""

    session_id: str
    state: SessionState = SessionState.ACTIVE
    message_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @property
    def is_healthy(self) -> bool:
        """Return ``True`` when the session is usable (ACTIVE)."""
        return self.state == SessionState.ACTIVE

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict (for stats / persistence)."""
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "message_count": self.message_count,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Pool Configuration
# ---------------------------------------------------------------------------


@dataclass
class PoolConfig:
    """Tunable parameters for :class:`SessionPool`."""

    max_sessions: int = _DEFAULT_MAX_SESSIONS
    max_messages_per_session: int = _DEFAULT_MAX_MESSAGES
    session_ttl: int = _DEFAULT_SESSION_TTL


# ---------------------------------------------------------------------------
# SessionPool
# ---------------------------------------------------------------------------


class SessionPool:
    """Manages a pool of bridge sessions with auto-rotation.

    Parameters
    ----------
    config:
        Optional :class:`PoolConfig` for pool limits and TTL.
    session_manager:
        Optional :class:`SessionManager` instance for persistence.
        Falls back to the global singleton when ``None``.
    """

    def __init__(
        self,
        config: Optional[PoolConfig] = None,
        session_manager: Optional[SessionManager] = None,
    ) -> None:
        self.config = config or PoolConfig()
        self._manager = session_manager or get_session_manager()

        # In-memory session store: session_id → PooledSession
        self._sessions: Dict[str, PooledSession] = {}

        # Round-robin index for healthy session selection
        self._rr_index: int = 0

        # Protects all mutable state
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire_session(self) -> PooledSession:
        """Return a healthy session, creating one if necessary.

        Strategy:
        1. If there are ACTIVE sessions, pick the next one (round-robin).
        2. If the pool has room (< max_sessions), create a new session.
        3. If all sessions are FULL/ERROR and the pool is at capacity,
           rotate the oldest FULL session.

        Raises
        ------
        RuntimeError
            If no session can be acquired (all in ERROR state and at
            capacity).
        """
        async with self._lock:
            # 1. Try round-robin among ACTIVE sessions
            active = [s for s in self._sessions.values() if s.is_healthy]
            if active:
                # Round-robin pick
                idx = self._rr_index % len(active)
                self._rr_index += 1
                session = active[idx]
                session.last_used = time.time()
                logger.debug("Acquired existing session %s (rr=%d)", session.session_id, idx)
                return session

            # 2. Create new if under capacity
            if len(self._sessions) < self.config.max_sessions:
                session = await self._create_session_locked()
                logger.info("Created new session %s (pool size=%d)", session.session_id, len(self._sessions))
                return session

            # 3. Try to rotate a FULL session
            full_sessions = [
                s for s in self._sessions.values()
                if s.state == SessionState.FULL
            ]
            if full_sessions:
                # Rotate the oldest FULL session
                oldest = min(full_sessions, key=lambda s: s.created_at)
                new_session = await self._rotate_session_locked(oldest.session_id)
                return new_session

            # 4. Check for EXPIRED sessions we can reclaim
            expired_sessions = [
                s for s in self._sessions.values()
                if s.state == SessionState.EXPIRED
            ]
            if expired_sessions:
                oldest = min(expired_sessions, key=lambda s: s.created_at)
                new_session = await self._rotate_session_locked(oldest.session_id)
                return new_session

            raise RuntimeError(
                f"Session pool exhausted: {len(self._sessions)} sessions, "
                f"none available (states: {self._state_summary()})"
            )

    async def release_session(self, session_id: str) -> None:
        """Return a session to the pool after use.

        Increments the message count and transitions the state to FULL
        if the message limit is reached.
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                logger.warning("release_session: unknown session %s", session_id)
                return

            session.message_count += 1
            session.last_used = time.time()

            if session.message_count >= self.config.max_messages_per_session:
                session.state = SessionState.FULL
                logger.info(
                    "Session %s reached message limit (%d), marked FULL",
                    session_id,
                    self.config.max_messages_per_session,
                )

            # Persist updated state
            await self._persist_session_locked(session)

    async def rotate_session(self, session_id: str) -> PooledSession:
        """Mark a session as FULL and create a replacement.

        Returns the newly created session.

        Raises
        ------
        KeyError
            If *session_id* is not in the pool.
        """
        async with self._lock:
            return await self._rotate_session_locked(session_id)

    async def get_pool_stats(self) -> Dict[str, Any]:
        """Return a summary of the current pool state."""
        async with self._lock:
            states: Dict[str, int] = {}
            total_messages = 0
            for s in self._sessions.values():
                states[s.state.value] = states.get(s.state.value, 0) + 1
                total_messages += s.message_count

            return {
                "total_sessions": len(self._sessions),
                "max_sessions": self.config.max_sessions,
                "max_messages_per_session": self.config.max_messages_per_session,
                "session_ttl": self.config.session_ttl,
                "states": states,
                "total_messages": total_messages,
                "sessions": [s.to_dict() for s in self._sessions.values()],
            }

    async def cleanup_expired(self) -> int:
        """Remove expired sessions from the pool.

        A session is expired when ``now - created_at > session_ttl``.
        Returns the number of sessions removed.
        """
        async with self._lock:
            now = time.time()
            to_remove: List[str] = []

            for sid, session in self._sessions.items():
                age = now - session.created_at
                if age > self.config.session_ttl:
                    session.state = SessionState.EXPIRED
                    to_remove.append(sid)

            for sid in to_remove:
                session = self._sessions[sid]
                age = now - session.created_at
                del self._sessions[sid]
                # Also remove from persistent store
                await self._manager.delete_session(f"{_POOL_KEY_PREFIX}{sid}")
                logger.info("Cleaned up expired session %s (age=%.0fs)", sid, age)

            if to_remove:
                logger.info("Cleaned up %d expired sessions", len(to_remove))

            return len(to_remove)

    async def health_check_all(self) -> Dict[str, bool]:
        """Verify each ACTIVE session is still usable.

        Sessions that fail the check are moved to ERROR state.
        Returns a mapping of session_id → healthy (bool).
        """
        async with self._lock:
            results: Dict[str, bool] = {}

            for sid, session in self._sessions.items():
                if session.state not in (SessionState.ACTIVE, SessionState.FULL):
                    results[sid] = False
                    continue

                # Check TTL-based expiry
                age = time.time() - session.created_at
                if age > self.config.session_ttl:
                    session.state = SessionState.EXPIRED
                    results[sid] = False
                    logger.info("Health check: session %s expired (age=%.0fs)", sid, age)
                    continue

                # Check persistent store for the session data
                stored = await self._manager.get_session(f"{_POOL_KEY_PREFIX}{sid}")
                if stored is None:
                    session.state = SessionState.ERROR
                    results[sid] = False
                    logger.warning("Health check: session %s not found in store", sid)
                else:
                    results[sid] = True

            return results

    async def get_session(self, session_id: str) -> Optional[PooledSession]:
        """Look up a specific session by ID (read-only)."""
        async with self._lock:
            return self._sessions.get(session_id)

    async def mark_error(self, session_id: str, reason: str = "") -> None:
        """Mark a session as ERROR with an optional reason."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            session.state = SessionState.ERROR
            session.metadata["error_reason"] = reason
            session.metadata["error_at"] = time.time()
            await self._persist_session_locked(session)
            logger.warning("Session %s marked ERROR: %s", session_id, reason)

    async def close(self) -> None:
        """Persist all sessions and clean up resources."""
        async with self._lock:
            for session in self._sessions.values():
                await self._persist_session_locked(session)
        logger.info("SessionPool closed, %d sessions persisted", len(self._sessions))

    # ------------------------------------------------------------------
    # Internal helpers (caller MUST hold _lock)
    # ------------------------------------------------------------------

    async def _create_session_locked(self) -> PooledSession:
        """Create a new session and add it to the pool."""
        session_id = f"sess-{uuid.uuid4().hex[:12]}"
        now = time.time()

        session = PooledSession(
            session_id=session_id,
            state=SessionState.ACTIVE,
            message_count=0,
            created_at=now,
            last_used=now,
            metadata={"pool_managed": True},
        )

        self._sessions[session_id] = session
        await self._persist_session_locked(session)
        return session

    async def _rotate_session_locked(self, session_id: str) -> PooledSession:
        """Mark an existing session FULL and create a replacement."""
        old = self._sessions.get(session_id)
        if old is None:
            raise KeyError(f"Session {session_id} not in pool")

        # Mark old session as FULL
        old.state = SessionState.FULL
        old.metadata["rotated_at"] = time.time()
        await self._persist_session_locked(old)
        logger.info(
            "Rotated session %s (messages=%d) → FULL",
            session_id,
            old.message_count,
        )

        # Create replacement
        new_session = await self._create_session_locked()
        logger.info(
            "Replacement session %s created (replacing %s)",
            new_session.session_id,
            session_id,
        )
        return new_session

    async def _persist_session_locked(self, session: PooledSession) -> None:
        """Save session metadata to the SessionManager."""
        key = f"{_POOL_KEY_PREFIX}{session.session_id}"
        data = {
            "state": session.state.value,
            "message_count": session.message_count,
            "created_at": session.created_at,
            "last_used": session.last_used,
            "metadata": session.metadata,
        }
        try:
            await self._manager.save_session(key, data, ttl=self.config.session_ttl)
        except Exception:
            logger.exception("Failed to persist session %s", session.session_id)

    def _state_summary(self) -> Dict[str, int]:
        """Quick state count summary (no lock needed, read-only dict)."""
        counts: Dict[str, int] = {}
        for s in self._sessions.values():
            counts[s.state.value] = counts.get(s.state.value, 0) + 1
        return counts

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "SessionPool":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<SessionPool sessions={len(self._sessions)}/"
            f"{self.config.max_sessions} states={self._state_summary()}>"
        )


# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------


async def main() -> None:
    """Quick smoke test for SessionPool."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Use in-memory SessionManager for testing
    from router.session import SessionManager
    manager = SessionManager(db_path=None)
    config = PoolConfig(max_sessions=3, max_messages_per_session=3, session_ttl=3600)

    async with SessionPool(config=config, session_manager=manager) as pool:
        print("=== Session Pool Smoke Test ===\n")

        # 1. Create & fill sessions one at a time to populate the pool
        print("--- Building pool (acquire → fill → repeat) ---")
        seen_ids: set = set()
        for cycle in range(3):
            s = await pool.acquire_session()
            seen_ids.add(s.session_id)
            # Fill it to capacity so next acquire creates/rotates
            for _ in range(config.max_messages_per_session):
                await pool.release_session(s.session_id)
            print(f"  Cycle {cycle+1}: {s.session_id} → messages={s.message_count}, state={s.state.value}")

        print(f"  Unique sessions created: {len(seen_ids)}")

        # 2. Round-robin among remaining active sessions
        print("\n--- Round-robin selection ---")
        rr_ids = []
        for _ in range(4):
            s = await pool.acquire_session()
            rr_ids.append(s.session_id)
        print(f"  Picks: {rr_ids}")

        # 3. Pool stats
        print("\n--- Pool Stats ---")
        stats = await pool.get_pool_stats()
        print(f"  Total: {stats['total_sessions']} sessions")
        print(f"  States: {stats['states']}")
        print(f"  Total messages: {stats['total_messages']}")

        # 4. Health check
        print("\n--- Health Check ---")
        health = await pool.health_check_all()
        for sid, ok in health.items():
            print(f"  {sid}: {'✓' if ok else '✗'}")

        # 5. Mark error
        print("\n--- Mark Error ---")
        target = next(iter(health.keys()))
        await pool.mark_error(target, "simulated bridge failure")
        s_check = await pool.get_session(target)
        print(f"  {target}: state={s_check.state.value if s_check else 'N/A'}")

        # 6. Cleanup expired (none should be expired yet)
        print("\n--- Cleanup Expired ---")
        removed = await pool.cleanup_expired()
        print(f"  Removed: {removed}")

        # 7. Final stats
        print("\n--- Final Stats ---")
        stats = await pool.get_pool_stats()
        for s in stats["sessions"]:
            print(f"  {s['session_id']}: state={s['state']}, msgs={s['message_count']}")

    await manager.close()
    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
