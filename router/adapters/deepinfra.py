"""
DeepInfra adapter — connects to https://api.deepinfra.com/v1/openai
OpenAI-compatible API supporting streaming, tool calling, and vision (GLM).
"""

from __future__ import annotations

import os
from typing import Any, AsyncGenerator

import httpx


# ── Model mapping ────────────────────────────────────────────────────────────

_SHORT_NAME_MAP: dict[str, str] = {
    "V4-Flash-0731": "deepseek-ai/DeepSeek-V4-Flash-0731",
    "V4.1-Flash": "deepseek-ai/DeepSeek-V4.1-Flash",
    "GLM-5.3-Flash": "zai-org/GLM-5.3-Flash",
}

_BLOCKED_SUBSTRINGS: tuple[str, ...] = ("Pro", "Vision-Exp")


def _resolve_model(model: str) -> str:
    """Map short model names to full DeepInfra IDs and reject banned variants."""
    for blocked in _BLOCKED_SUBSTRINGS:
        if blocked in model:
            raise ValueError(
                f"Model '{model}' is not allowed (contains '{blocked}'). "
                "Only V4-Flash-0731, V4.1-Flash, and GLM-5.3-Flash are permitted."
            )
    return _SHORT_NAME_MAP.get(model, model)


# ── Adapter class ────────────────────────────────────────────────────────────


class DeepInfraAdapter:
    """Async adapter for the DeepInfra OpenAI-compatible API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.deepinfra.com/v1/openai",
        *,
        timeout: float = 120.0,
    ) -> None:
        self.api_key = api_key or os.getenv("DEEPINFRA_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

        # Cumulative token usage tracking
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0

    # ── Client lifecycle ─────────────────────────────────────────────────

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(self._timeout, connect=10.0),
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client gracefully."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ── Token tracking ───────────────────────────────────────────────────

    def _record_usage(self, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        self.total_prompt_tokens += usage.get("prompt_tokens", 0)
        self.total_completion_tokens += usage.get("completion_tokens", 0)

    @property
    def total_tokens(self) -> int:
        return self.total_prompt_tokens + self.total_completion_tokens

    # ── Error handling ───────────────────────────────────────────────────

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """Raise a descriptive exception for known error codes."""
        if resp.status_code == 401:
            raise PermissionError("DeepInfra authentication failed (401). Check DEEPINFRA_API_KEY.")
        if resp.status_code == 403:
            raise PermissionError("DeepInfra access denied (403).")
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "")
            raise RuntimeError(
                f"DeepInfra rate limit exceeded (429). Retry-After: {retry_after or 'N/A'}"
            )
        if resp.status_code == 503:
            raise RuntimeError("DeepInfra service unavailable (503). Try again later.")
        if resp.status_code >= 500:
            raise RuntimeError(
                f"DeepInfra server error ({resp.status_code}): {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise ValueError(
                f"DeepInfra request error ({resp.status_code}): {resp.text[:200]}"
            )

    # ── Main API call ────────────────────────────────────────────────────

    async def send(self, request: dict[str, Any], model: str) -> dict[str, Any]:
        """
        Send a chat-completion request to DeepInfra.

        Parameters
        ----------
        request : dict
            OpenAI-style chat completion request body. The ``model`` key,
            if present, will be overridden.
        model : str
            Short model name (e.g. ``'V4-Flash-0731'``) or full DeepInfra ID.

        Returns
        -------
        dict
            For non-streaming: the parsed JSON response.
            For streaming (``request['stream']=True``): a dict with key
            ``'chunks'`` containing a list of the accumulated streamed
            delta dicts, and ``'usage'`` if present in the final chunk.
        """
        resolved = _resolve_model(model)
        payload: dict[str, Any] = {**request, "model": resolved}

        streaming = payload.get("stream", False)
        client = self._get_client()

        if streaming:
            return await self._send_streaming(client, payload)
        return await self._send_sync(client, payload)

    async def _send_sync(
        self, client: httpx.AsyncClient, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Non-streaming chat completion."""
        resp = await client.post("/chat/completions", json=payload)
        self._raise_for_status(resp)
        data: dict[str, Any] = resp.json()
        self._record_usage(data.get("usage"))
        return data

    async def _send_streaming(
        self, client: httpx.AsyncClient, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Streaming chat completion.

        Returns a dict with ``chunks`` (list of parsed SSE events) and
        ``usage`` from the final ``[DONE]`` message if present.
        """
        collected_chunks: list[dict[str, Any]] = []
        last_usage: dict[str, Any] | None = None

        async with client.stream("POST", "/chat/completions", json=payload) as resp:
            self._raise_for_status(resp)
            async for raw_line in resp.aiter_lines():
                line = raw_line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                try:
                    import json
                    chunk: dict[str, Any] = json.loads(data_str)
                except Exception:  # noqa: BLE001
                    continue
                collected_chunks.append(chunk)
                if "usage" in chunk and chunk["usage"]:
                    last_usage = chunk["usage"]

        self._record_usage(last_usage)
        return {"chunks": collected_chunks, "usage": last_usage}

    # ── Streaming generator (for callers that want to yield) ─────────────

    async def stream(
        self, request: dict[str, Any], model: str
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        Yield each SSE chunk as a parsed dict.

        This is an alternative to ``send()`` for callers that want to
        consume the stream incrementally rather than collecting all chunks.
        """
        import json as _json

        resolved = _resolve_model(model)
        payload: dict[str, Any] = {**request, "model": resolved, "stream": True}
        client = self._get_client()

        async with client.stream("POST", "/chat/completions", json=payload) as resp:
            self._raise_for_status(resp)
            async for raw_line in resp.aiter_lines():
                line = raw_line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = _json.loads(data_str)
                except Exception:  # noqa: BLE001
                    continue
                if "usage" in chunk and chunk["usage"]:
                    self._record_usage(chunk["usage"])
                yield chunk

    # ── Health check ─────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """
        Lightweight health probe — sends a minimal completion request.

        Returns ``True`` on success, ``False`` on any error.
        """
        client = self._get_client()
        try:
            resp = await client.post(
                "/chat/completions",
                json={
                    "model": "deepseek-ai/DeepSeek-V4-Flash-0731",
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
            )
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    # ── Convenience / context manager ────────────────────────────────────

    async def __aenter__(self) -> "DeepInfraAdapter":
        self._get_client()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f"<DeepInfraAdapter base_url={self.base_url!r}>"
