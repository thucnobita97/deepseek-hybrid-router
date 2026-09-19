"""DeepSeek Bridge adapter.

Connects to a local DeepSeek-API bridge (OpenAI-compatible) running at
http://localhost:8000 and forwards chat-completion requests.

Model alias mapping
-------------------
* ``v4-instant`` → ``deepseek-chat``
* ``expert``     → ``deepseek-reasoner``

Extra request fields forwarded when present:
* ``conversation_id`` – multi-turn conversation resumption
* ``thinking``        – enable thinking/reasoning mode
* ``search``          – enable search augmentation

Streaming responses are returned as an async generator of SSE chunks.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, Optional, Union

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_BASE_URL = "http://localhost:8000"
_COMPLETIONS_PATH = "/v1/chat/completions"
_HEALTH_PATH = "/healthz"

_MODEL_MAP: Dict[str, str] = {
    "v4-instant": "deepseek-chat",
    "expert": "deepseek-reasoner",
}

_DEFAULT_TIMEOUT = 120.0
_DEFAULT_CONNECT_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BridgeError(Exception):
    """Base error for bridge adapter failures."""


class BridgeConnectionError(BridgeError):
    """Raised when the bridge is unreachable."""


class BridgeTimeoutError(BridgeError):
    """Raised when the bridge request exceeds the timeout."""


class BridgeAPIError(BridgeError):
    """Raised when the bridge returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Bridge returned {status_code}: {detail}")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class BridgeConfig:
    """Optional configuration knobs for :class:`BridgeAdapter`."""

    api_key: str = "not-needed"
    timeout: float = _DEFAULT_TIMEOUT
    connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT
    max_retries: int = 1
    extra_headers: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class BridgeAdapter:
    """Async adapter that forwards OpenAI-compatible requests to a local
    DeepSeek-API bridge.

    Parameters
    ----------
    base_url:
        Root URL of the bridge (no trailing slash).
    config:
        Optional :class:`BridgeConfig` for timeouts, auth, etc.
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        config: Optional[BridgeConfig] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.config = config or BridgeConfig()

        # Connection pool configuration:
        #   max_connections=20: ceiling on total open connections
        #   max_keepalive_connections=10: idle connections retained for reuse
        #   keepalive_expiry=30: seconds an idle keepalive connection may sit
        #                        in the pool before being discarded
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                **self.config.extra_headers,
            },
            timeout=httpx.Timeout(
                connect=self.config.connect_timeout,
                read=self.config.timeout,
                write=self.config.timeout,
                pool=self.config.connect_timeout,
            ),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30,
            ),
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_model(model: str) -> str:
        """Map a friendly alias to the bridge model name.

        If *model* is not in the alias table it is passed through unchanged
        (so callers can use ``deepseek-chat`` directly).
        """
        return _MODEL_MAP.get(model, model)

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    async def send(
        self,
        request: Dict[str, Any],
        model: str,
    ) -> Union[Dict[str, Any], AsyncGenerator[Dict[str, Any], None]]:
        """Forward a chat-completion request to the bridge.

        Parameters
        ----------
        request:
            OpenAI-format ``/v1/chat/completions`` payload.  Bridge-specific
            extra fields (``conversation_id``, ``thinking``, ``search``) are
            forwarded verbatim when present.
        model:
            Model alias (``v4-instant``, ``expert``) or the real bridge model
            name.

        Returns
        -------
        dict | AsyncGenerator
            * Non-streaming: the full JSON response dict augmented with a
              top-level ``conversation_id`` extracted from the response body.
            * Streaming (``request["stream"] is True``): an async generator
              yielding parsed SSE chunk dicts.
        """
        resolved_model = self.resolve_model(model)

        # Build the outbound payload ----------------------------------------
        payload: Dict[str, Any] = {**request, "model": resolved_model}

        # Preserve bridge-specific passthrough fields already in *request*:
        # conversation_id, thinking, search – they are kept as-is.

        is_stream = bool(payload.get("stream", False))

        # Execute -----------------------------------------------------------
        try:
            if is_stream:
                return self._stream(payload)
            return await self._non_stream(payload)

        except httpx.ConnectError as exc:
            logger.error("Bridge connection refused: %s", exc)
            raise BridgeConnectionError(
                f"Cannot reach bridge at {self.base_url}: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            logger.error("Bridge request timed out: %s", exc)
            raise BridgeTimeoutError(f"Bridge timed out: {exc}") from exc

    async def health_check(self) -> bool:
        """Return ``True`` when the bridge ``/healthz`` endpoint responds 200."""
        try:
            resp = await self._client.get(_HEALTH_PATH, timeout=self.config.connect_timeout)
            return resp.status_code == 200
        except (httpx.HTTPError, OSError):
            logger.debug("Health check failed for bridge at %s", self.base_url, exc_info=True)
            return False

    async def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "BridgeAdapter":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _non_stream(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Single-shot request → parsed JSON dict."""
        resp = await self._client.post(_COMPLETIONS_PATH, json=payload)

        if resp.status_code >= 400:
            raise BridgeAPIError(resp.status_code, resp.text[:500])

        data: Dict[str, Any] = resp.json()

        # Promote conversation_id (bridge extension) into top-level metadata.
        conversation_id = data.get("conversation_id")
        if conversation_id:
            data.setdefault("_bridge_meta", {})["conversation_id"] = conversation_id

        return data

    async def _stream(
        self, payload: Dict[str, Any]
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Yield parsed SSE chunks from a streaming completion."""
        import json as _json

        async with self._client.stream("POST", _COMPLETIONS_PATH, json=payload) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise BridgeAPIError(resp.status_code, body.decode(errors="replace")[:500])

            async for raw_line in resp.aiter_lines():
                line = raw_line.strip()
                if not line or not line.startswith("data:"):
                    continue

                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    return

                try:
                    chunk: Dict[str, Any] = _json.loads(data_str)
                except _json.JSONDecodeError:
                    logger.warning("Skipping malformed SSE chunk: %s", data_str)
                    continue

                # Surface conversation_id in every chunk that carries it.
                conversation_id = chunk.get("conversation_id")
                if conversation_id:
                    chunk.setdefault("_bridge_meta", {})["conversation_id"] = conversation_id

                yield chunk

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BridgeAdapter base_url={self.base_url!r}>"
