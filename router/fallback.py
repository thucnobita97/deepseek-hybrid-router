"""Fallback chain — handles provider failures with retries and health tracking.

The :class:`FallbackManager` takes an ordered list of ``(adapter, model)``
pairs and tries each in turn when the previous one fails.  Failures are
logged, providers are temporarily marked unhealthy, and exponential backoff
is applied between provider switches.

Fallback triggers
-----------------
* HTTP 429 (rate limit), 502, 503
* Connection timeout (10 s) / read timeout (60 s)
* Auth errors (401, 403) — skip to next provider **without** retry
* Any unhandled exception
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union

from router.adapters.alibaba import (
    AlibabaAPIError,
    AlibabaAuthError,
    AlibabaRateLimitError,
    AlibabaServiceError,
)
from router.adapters.bridge import (
    BridgeAPIError,
    BridgeConnectionError,
    BridgeTimeoutError,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503})
_AUTH_STATUS_CODES = frozenset({401, 403})

_DEFAULT_CONNECT_TIMEOUT = 10.0
_DEFAULT_READ_TIMEOUT = 60.0
_DEFAULT_MAX_RETRIES = 2
_DEFAULT_BACKOFF_BASE = 1.0  # seconds — doubles each step: 1, 2, 4, …


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ProviderHealth:
    """Tracks runtime health of a single provider."""

    name: str
    healthy: bool = True
    last_error: Optional[str] = None
    failure_count: int = 0
    unhealthy_until: Optional[float] = None  # epoch timestamp

    @property
    def recovery_time(self) -> Optional[float]:
        """Seconds until the provider is automatically recovered, or ``None``."""
        if self.unhealthy_until is None:
            return None
        remaining = self.unhealthy_until - time.time()
        return max(0.0, remaining)

    def is_available(self) -> bool:
        """Return ``True`` when the provider may receive traffic."""
        if self.healthy:
            return True
        if self.unhealthy_until is not None and time.time() >= self.unhealthy_until:
            # Auto-recover
            self.healthy = True
            self.unhealthy_until = None
            logger.info("Provider %s auto-recovered after cooldown", self.name)
            return True
        return False


@dataclass
class FallbackEvent:
    """A single fallback event for auditing / telemetry."""

    failed_provider: str
    error_type: str
    error_message: str
    fallback_provider: Optional[str] = None
    attempt: int = 0
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Error classification helpers
# ---------------------------------------------------------------------------


def _classify_error(exc: BaseException) -> Tuple[str, bool]:
    """Return ``(category, is_retryable)`` for *exc*.

    Categories: ``"auth"``, ``"rate_limit"``, ``"timeout"``,
    ``"connection"``, ``"service"``, ``"malformed"``, ``"unknown"``.
    """
    # --- Bridge errors ---
    if isinstance(exc, BridgeAPIError):
        if exc.status_code in _AUTH_STATUS_CODES:
            return "auth", False
        if exc.status_code in _RETRYABLE_STATUS_CODES:
            return "rate_limit" if exc.status_code == 429 else "service", True
        return "service", exc.status_code >= 500

    if isinstance(exc, BridgeTimeoutError):
        return "timeout", True
    if isinstance(exc, BridgeConnectionError):
        return "connection", True

    # --- Alibaba errors ---
    if isinstance(exc, AlibabaAuthError):
        return "auth", False
    if isinstance(exc, AlibabaRateLimitError):
        return "rate_limit", True
    if isinstance(exc, AlibabaServiceError):
        return "service", True
    if isinstance(exc, AlibabaAPIError):
        return "service", True

    # --- DeepInfra errors (stdlib exceptions) ---
    if isinstance(exc, PermissionError):
        msg = str(exc).lower()
        if "401" in msg or "403" in msg or "auth" in msg or "denied" in msg:
            return "auth", False
        return "unknown", True

    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        if "429" in msg or "rate limit" in msg:
            return "rate_limit", True
        if "503" in msg or "unavailable" in msg:
            return "service", True
        if "502" in msg:
            return "service", True
        return "unknown", True

    # --- Generic network / timeout ---
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout", True
    if isinstance(exc, (ConnectionError, OSError)):
        return "connection", True

    # --- Malformed response ---
    if isinstance(exc, (ValueError, KeyError)):
        return "malformed", True

    return "unknown", True


def _adapter_name(adapter: Any) -> str:
    """Derive a human-readable name for *adapter*."""
    return type(adapter).__name__


# ---------------------------------------------------------------------------
# FallbackManager
# ---------------------------------------------------------------------------


class FallbackManager:
    """Orchestrates a fallback chain over multiple provider adapters.

    Parameters
    ----------
    chain:
        Ordered list of ``(adapter, model)`` tuples.  The first entry is
        the primary provider; subsequent entries are fallbacks.
    max_retries:
        Number of times to retry a single provider before moving to the
        next one in the chain (default ``2``).
    backoff_base:
        Base delay in seconds for exponential backoff between *provider
        switches* (default ``1.0``).  Within-provider retries use a
        shorter fixed delay.
    connect_timeout:
        Connection timeout in seconds (informational; actual enforcement
        is delegated to each adapter).
    read_timeout:
        Read timeout in seconds (informational).
    """

    def __init__(
        self,
        chain: List[Tuple[Any, str]],
        *,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_base: float = _DEFAULT_BACKOFF_BASE,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = _DEFAULT_READ_TIMEOUT,
    ) -> None:
        if not chain:
            raise ValueError("Fallback chain must contain at least one provider")

        self._chain: List[Tuple[Any, str]] = list(chain)
        self.max_retries: int = max_retries
        self.backoff_base: float = backoff_base
        self.connect_timeout: float = connect_timeout
        self.read_timeout: float = read_timeout

        # Health tracking keyed by adapter name
        self._health: Dict[str, ProviderHealth] = {}
        for adapter, _model in self._chain:
            name = _adapter_name(adapter)
            if name not in self._health:
                self._health[name] = ProviderHealth(name=name)

        # Audit log
        self.events: List[FallbackEvent] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Execute *request* against the fallback chain.

        Tries each provider in order with up to ``max_retries`` attempts
        per provider.  On retryable failure, applies exponential backoff
        before switching to the next provider.  Auth failures cause an
        immediate skip.

        Returns the first successful response dict.

        Raises
        ------
        RuntimeError
            If every provider in the chain has been exhausted.
        """
        is_streaming = bool(request.get("stream", False))
        errors: List[Dict[str, Any]] = []
        last_exception: Optional[BaseException] = None

        for provider_idx, (adapter, model) in enumerate(self._chain):
            name = _adapter_name(adapter)
            health = self._health[name]

            # Skip unhealthy providers
            if not health.is_available():
                logger.info(
                    "Skipping unhealthy provider %s (recovery in %.1fs)",
                    name,
                    health.recovery_time or 0,
                )
                errors.append({
                    "provider": name,
                    "error": "provider marked unhealthy",
                    "skipped": True,
                })
                continue

            # Retry loop for this provider
            for attempt in range(1, self.max_retries + 1):
                try:
                    if is_streaming:
                        result = await self._execute_streaming(adapter, model, request)
                    else:
                        result = await adapter.send(request, model)

                    # Success — reset failure count
                    health.failure_count = 0
                    health.last_error = None
                    return result

                except Exception as exc:  # noqa: BLE001
                    last_exception = exc
                    category, retryable = _classify_error(exc)
                    error_msg = f"{type(exc).__name__}: {exc}"

                    logger.warning(
                        "Provider %s failed (attempt %d/%d, category=%s): %s",
                        name, attempt, self.max_retries, category, error_msg,
                    )

                    health.failure_count += 1
                    health.last_error = error_msg

                    errors.append({
                        "provider": name,
                        "attempt": attempt,
                        "error": error_msg,
                        "category": category,
                        "retryable": retryable,
                    })

                    # Record the event
                    next_name: Optional[str] = None
                    if provider_idx + 1 < len(self._chain):
                        next_name = _adapter_name(self._chain[provider_idx + 1][0])

                    self.events.append(FallbackEvent(
                        failed_provider=name,
                        error_type=category,
                        error_message=error_msg,
                        fallback_provider=next_name,
                        attempt=attempt,
                    ))

                    # Auth errors → skip immediately (no retry, no backoff)
                    if not retryable:
                        logger.info(
                            "Non-retryable %s error on %s — skipping to next provider",
                            category, name,
                        )
                        break

                    # Retryable: if more attempts remain, short pause then retry
                    if attempt < self.max_retries:
                        await asyncio.sleep(0.5)
                        continue

                    # Exhausted retries for this provider — mark unhealthy briefly
                    # and apply exponential backoff before next provider
                    health.healthy = False
                    health.unhealthy_until = time.time() + 30  # 30 s cooldown

                    if provider_idx + 1 < len(self._chain):
                        delay = self.backoff_base * (2 ** provider_idx)
                        logger.info(
                            "Fallback: switching from %s to %s (backoff %.1fs)",
                            name,
                            _adapter_name(self._chain[provider_idx + 1][0]),
                            delay,
                        )
                        await asyncio.sleep(delay)

        # All providers exhausted
        error_summary = "; ".join(
            f"{e['provider']}: {e.get('error', 'unknown')}" for e in errors
        )
        msg = f"All providers in fallback chain failed: {error_summary}"
        logger.error(msg)
        raise RuntimeError(msg) from last_exception

    def mark_unhealthy(
        self, adapter_name: str, duration_seconds: int = 60
    ) -> None:
        """Temporarily remove a provider from the active chain.

        Parameters
        ----------
        adapter_name:
            The class name of the adapter (e.g. ``"BridgeAdapter"``).
        duration_seconds:
            How long the provider stays unhealthy before auto-recovery.
        """
        health = self._health.get(adapter_name)
        if health is None:
            logger.warning("mark_unhealthy: unknown provider %r", adapter_name)
            return
        health.healthy = False
        health.unhealthy_until = time.time() + duration_seconds
        logger.info(
            "Provider %s marked unhealthy for %ds", adapter_name, duration_seconds
        )

    def get_provider_health(self) -> Dict[str, Dict[str, Any]]:
        """Return a snapshot of every provider's health status.

        Returns a dict keyed by adapter name with values:
        ``healthy``, ``last_error``, ``failure_count``, ``recovery_time``.
        """
        result: Dict[str, Dict[str, Any]] = {}
        for name, health in self._health.items():
            # Trigger auto-recovery check
            health.is_available()
            result[name] = {
                "healthy": health.healthy,
                "last_error": health.last_error,
                "failure_count": health.failure_count,
                "recovery_time": health.recovery_time,
            }
        return result

    # ------------------------------------------------------------------
    # Streaming fallback
    # ------------------------------------------------------------------

    async def _execute_streaming(
        self, adapter: Any, model: str, request: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Best-effort streaming execution.

        If the adapter returns an async generator we consume it into a
        collected dict so the caller gets a uniform interface.  If the
        stream fails mid-way, the exception propagates so
        :meth:`execute` can try the next provider.
        """
        result = await adapter.send(request, model)

        # Some adapters return an async generator for streaming
        if inspect.isasyncgen(result):
            return await self._consume_stream(result)  # type: ignore[arg-type]

        # Others (DeepInfra, Alibaba) return a dict with 'chunks'
        if isinstance(result, dict):
            return result

        # Unexpected type
        raise ValueError(
            f"Adapter {_adapter_name(adapter)} returned unexpected type: "
            f"{type(result).__name__}"
        )

    @staticmethod
    async def _consume_stream(
        stream: AsyncGenerator[Dict[str, Any], None],
    ) -> Dict[str, Any]:
        """Drain an async generator stream into a collected response dict."""
        collected_chunks: List[Dict[str, Any]] = []
        last_usage: Optional[Dict[str, Any]] = None

        async for chunk in stream:
            collected_chunks.append(chunk)
            if "usage" in chunk and chunk["usage"]:
                last_usage = chunk["usage"]

        return {"chunks": collected_chunks, "usage": last_usage}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def chain_names(self) -> List[str]:
        """Return the ordered list of provider names in the chain."""
        return [_adapter_name(a) for a, _ in self._chain]

    def __repr__(self) -> str:
        names = " → ".join(self.chain_names)
        return f"<FallbackManager chain=[{names}] max_retries={self.max_retries}>"
