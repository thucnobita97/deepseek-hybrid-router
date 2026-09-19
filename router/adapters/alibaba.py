"""Alibaba DashScope adapter - OpenAI-compatible API at dashscope.aliyuncs.com."""

from __future__ import annotations

import os
import re
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# Allowed model patterns: qwen3.7-* and qwen3.8-flash only
_ALLOWED_MODEL_RE = re.compile(r"^(qwen3\.7-[a-z0-9\-]+|qwen3\.8-flash)$")

# Map short names -> full DashScope model IDs
_MODEL_MAP: Dict[str, str] = {
    "qwen3.7-max": "qwen3.7-max",
    "qwen3.8-flash": "qwen3.8-flash",
}

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class AlibabaAdapter:
    """Adapter for Alibaba DashScope (OpenAI-compatible) chat completions API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
    ) -> None:
        self.api_key: str = api_key or os.environ.get("ALIBABA_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "ALIBABA_API_KEY must be set via environment variable or passed explicitly"
            )
        self.base_url: str = base_url.rstrip("/")
        self._timeout: float = timeout
        self._client: Optional[httpx.AsyncClient] = None

        # Token usage tracking
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_tokens: int = 0

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        """Return the shared async HTTP client, creating it lazily.

        Connection pool settings (via ``httpx.Limits``):
            - ``max_connections=20``: ceiling on total open connections.
            - ``max_keepalive_connections=10``: idle connections retained
              for reuse before being closed.
            - ``keepalive_expiry=30``: seconds an idle keepalive connection
              may sit in the pool before being discarded.
        """
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(self._timeout, connect=10.0),
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                    keepalive_expiry=30,
                ),
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Model validation
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_model(model: str) -> str:
        """Validate and resolve a model name to its full DashScope ID.

        Raises ValueError for disallowed models.
        """
        # Direct short-name lookup
        resolved = _MODEL_MAP.get(model)
        if resolved is not None:
            return resolved

        # Check if it matches allowed patterns (qwen3.7-* or qwen3.8-flash)
        if not _ALLOWED_MODEL_RE.match(model):
            raise ValueError(
                f"Model '{model}' is not allowed. "
                f"Only qwen3.7-* and qwen3.8-flash models are permitted."
            )

        # Model matches pattern but isn't in our explicit map — pass through
        # (allows future qwen3.7-* variants without code changes)
        return model

    # ------------------------------------------------------------------
    # Main send method
    # ------------------------------------------------------------------

    async def send(self, request: dict, model: str) -> dict:
        """Send a chat completion request to DashScope.

        Args:
            request: OpenAI-format chat completion request body.
            model: Short model name (e.g. 'qwen3.7-max', 'qwen3.8-flash').

        Returns:
            The full response dict from DashScope, with usage tracked.

        Raises:
            ValueError: If the model is not allowed.
            httpx.HTTPStatusError: On non-retryable HTTP errors.
            AlibabaRateLimitError: On 429 rate limit.
            AlibabaServiceError: On 503 or other server errors.
            AlibabaAuthError: On 401/403 auth failures.
        """
        resolved_model = self._resolve_model(model)

        # Build the payload — inject resolved model name
        payload = {**request, "model": resolved_model}

        is_streaming = payload.get("stream", False)

        client = self._get_client()

        try:
            if is_streaming:
                return await self._send_streaming(client, payload)
            else:
                return await self._send_non_streaming(client, payload)
        except httpx.HTTPStatusError as exc:
            self._handle_http_error(exc)
            raise  # unreachable — _handle_http_error always raises
        except httpx.RequestError as exc:
            logger.error("DashScope request error: %s", exc)
            raise

    async def _send_non_streaming(
        self, client: httpx.AsyncClient, payload: dict
    ) -> dict:
        """Handle non-streaming chat completion."""
        response = await client.post("/chat/completions", json=payload)
        response.raise_for_status()
        data: dict = response.json()
        self._track_usage(data)
        return data

    async def _send_streaming(
        self, client: httpx.AsyncClient, payload: dict
    ) -> dict:
        """Handle streaming chat completion.

        Collects all SSE chunks, assembles the final message (including
        tool calls), and returns a unified response dict matching the
        non-streaming shape.
        """
        collected_content: list[str] = []
        tool_calls_map: dict[int, dict] = {}  # index -> {id, type, function}
        finish_reason: Optional[str] = None
        usage_data: Optional[dict] = None
        model_returned: str = payload.get("model", "")
        response_id: str = ""

        async with client.stream(
            "POST", "/chat/completions", json=payload
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                chunk_str = line[len("data: "):]
                if chunk_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(chunk_str)
                except json.JSONDecodeError:
                    continue

                # Track metadata from first chunk
                if not response_id and chunk.get("id"):
                    response_id = chunk["id"]
                if chunk.get("model"):
                    model_returned = chunk["model"]

                # Usage may appear in the final chunk (some providers)
                if chunk.get("usage"):
                    usage_data = chunk["usage"]

                choices = chunk.get("choices", [])
                if not choices:
                    continue

                delta = choices[0].get("delta", {})

                # Content text
                if delta.get("content"):
                    collected_content.append(delta["content"])

                # Tool calls (may arrive across multiple deltas)
                if delta.get("tool_calls"):
                    for tc_delta in delta["tool_calls"]:
                        idx = tc_delta.get("index", 0)
                        if idx not in tool_calls_map:
                            tool_calls_map[idx] = {
                                "id": tc_delta.get("id", ""),
                                "type": tc_delta.get("type", "function"),
                                "function": {
                                    "name": "",
                                    "arguments": "",
                                },
                            }
                        existing = tool_calls_map[idx]
                        if tc_delta.get("id"):
                            existing["id"] = tc_delta["id"]
                        fn = tc_delta.get("function", {})
                        if fn.get("name"):
                            existing["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            existing["function"]["arguments"] += fn["arguments"]

                fr = choices[0].get("finish_reason")
                if fr:
                    finish_reason = fr

        # Assemble final message
        message: Dict[str, Any] = {
            "role": "assistant",
            "content": "".join(collected_content) or None,
        }
        if tool_calls_map:
            message["tool_calls"] = [
                tool_calls_map[i] for i in sorted(tool_calls_map)
            ]

        # Track usage if present
        if usage_data:
            self._track_usage({"usage": usage_data})

        return {
            "id": response_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_returned,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason or "stop",
                }
            ],
            "usage": usage_data or {},
        }

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Check if DashScope is reachable by sending a minimal request.

        Returns True if the API responds successfully, False otherwise.
        """
        client = self._get_client()
        try:
            response = await client.post(
                "/chat/completions",
                json={
                    "model": "qwen3.8-flash",
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
            )
            return response.status_code < 500
        except (httpx.HTTPError, Exception) as exc:
            logger.warning("Alibaba health check failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    @staticmethod
    def _handle_http_error(exc: httpx.HTTPStatusError) -> None:
        """Translate HTTP errors into typed exceptions."""
        status = exc.response.status_code
        body_text = exc.response.text

        if status == 429:
            raise AlibabaRateLimitError(
                f"DashScope rate limit exceeded: {body_text}"
            ) from exc
        elif status in (401, 403):
            raise AlibabaAuthError(
                f"DashScope auth failed ({status}): {body_text}"
            ) from exc
        elif status == 503:
            raise AlibabaServiceError(
                f"DashScope service unavailable: {body_text}"
            ) from exc
        else:
            raise AlibabaAPIError(
                f"DashScope API error ({status}): {body_text}"
            ) from exc

    # ------------------------------------------------------------------
    # Usage tracking
    # ------------------------------------------------------------------

    def _track_usage(self, data: dict) -> None:
        """Extract and accumulate token usage from a response."""
        usage = data.get("usage", {})
        if not usage:
            return
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        total = usage.get("total_tokens", prompt + completion)
        self.total_prompt_tokens += prompt
        self.total_completion_tokens += completion
        self.total_tokens += total
        logger.debug(
            "DashScope usage: prompt=%d completion=%d total=%d",
            prompt,
            completion,
            total,
        )

    @property
    def usage_summary(self) -> Dict[str, int]:
        """Return accumulated token usage."""
        return {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
        }

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "AlibabaAdapter":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()


# ======================================================================
# Custom exceptions
# ======================================================================

class AlibabaAPIError(Exception):
    """General DashScope API error."""


class AlibabaRateLimitError(AlibabaAPIError):
    """429 Rate limit exceeded."""


class AlibabaAuthError(AlibabaAPIError):
    """401/403 Authentication failure."""


class AlibabaServiceError(AlibabaAPIError):
    """503 Service unavailable."""
