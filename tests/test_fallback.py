"""Tests for fallback chain manager."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

from router.fallback import FallbackManager, ProviderHealth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockAdapter:
    """Minimal mock adapter for testing."""

    def __init__(self, name: str = "MockAdapter"):
        self.name = name
        self.send = AsyncMock()
        self.health_check = AsyncMock(return_value=True)

    def __repr__(self) -> str:
        return f"<{self.name}>"


class BridgeAdapter:
    """Named mock adapter."""

    def __init__(self):
        self.send = AsyncMock()
        self.health_check = AsyncMock(return_value=True)


class DeepInfraAdapter:
    """Named mock adapter."""

    def __init__(self):
        self.send = AsyncMock()
        self.health_check = AsyncMock(return_value=True)


class AlibabaAdapter:
    """Named mock adapter."""

    def __init__(self):
        self.send = AsyncMock()
        self.health_check = AsyncMock(return_value=True)


# ---------------------------------------------------------------------------
# Test 1: Primary provider succeeds on first try
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_primary_success():
    """Primary provider succeeds without fallback."""
    adapter = MockAdapter("PrimaryAdapter")
    adapter.send.return_value = {"id": "123", "choices": [{"message": {"content": "ok"}}]}

    manager = FallbackManager([(adapter, "model-a")])
    result = await manager.execute({"messages": [{"role": "user", "content": "hi"}]})

    assert result["id"] == "123"
    adapter.send.assert_called_once()


# ---------------------------------------------------------------------------
# Test 2: Primary fails, fallback succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_after_primary_failure():
    """Primary fails with 503, fallback succeeds."""
    from router.adapters.bridge import BridgeAPIError

    primary = BridgeAdapter()
    primary.send.side_effect = BridgeAPIError(503, "service unavailable")

    fallback = DeepInfraAdapter()
    fallback.send.return_value = {"id": "fb-1", "choices": []}

    manager = FallbackManager(
        [(primary, "model-a"), (fallback, "model-b")],
        max_retries=1,
        backoff_base=0.01,  # fast test
    )
    result = await manager.execute({"messages": []})

    assert result["id"] == "fb-1"
    assert primary.send.call_count == 1
    assert fallback.send.call_count == 1
    assert len(manager.events) == 1
    assert manager.events[0].failed_provider == "BridgeAdapter"
    assert manager.events[0].fallback_provider == "DeepInfraAdapter"


# ---------------------------------------------------------------------------
# Test 3: Auth error skips immediately (no retry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_error_skips_immediately():
    """Auth error (401) should skip to next provider without retry."""
    from router.adapters.alibaba import AlibabaAuthError

    primary = AlibabaAdapter()
    primary.send.side_effect = AlibabaAuthError("auth failed")

    fallback = MockAdapter("FallbackAdapter")
    fallback.send.return_value = {"id": "auth-fb"}

    manager = FallbackManager(
        [(primary, "qwen"), (fallback, "model-b")],
        max_retries=3,  # should NOT retry 3 times
        backoff_base=0.01,
    )
    result = await manager.execute({"messages": []})

    assert result["id"] == "auth-fb"
    # Auth error → exactly 1 attempt (no retries)
    assert primary.send.call_count == 1
    assert fallback.send.call_count == 1


# ---------------------------------------------------------------------------
# Test 4: All providers fail → RuntimeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_providers_fail():
    """All providers failing should raise RuntimeError."""
    from router.adapters.bridge import BridgeTimeoutError

    a1 = BridgeAdapter()
    a1.send.side_effect = BridgeTimeoutError("timeout")

    a2 = DeepInfraAdapter()
    a2.send.side_effect = RuntimeError("503 service unavailable")

    manager = FallbackManager(
        [(a1, "m1"), (a2, "m2")],
        max_retries=1,
        backoff_base=0.01,
    )

    with pytest.raises(RuntimeError, match="All providers in fallback chain failed"):
        await manager.execute({"messages": []})

    # Both providers should have been tried
    assert a1.send.call_count == 1
    assert a2.send.call_count == 1
    assert len(manager.events) == 2


# ---------------------------------------------------------------------------
# Test 5: Retry logic — provider fails twice then succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_within_provider():
    """Provider should be retried up to max_retries before falling back."""
    from router.adapters.bridge import BridgeTimeoutError

    adapter = MockAdapter("RetryAdapter")
    # Fail twice, succeed on third call
    adapter.send.side_effect = [
        BridgeTimeoutError("timeout 1"),
        BridgeTimeoutError("timeout 2"),
        {"id": "retry-success"},
    ]

    manager = FallbackManager(
        [(adapter, "model-a")],
        max_retries=3,
    )
    result = await manager.execute({"messages": []})

    assert result["id"] == "retry-success"
    assert adapter.send.call_count == 3


# ---------------------------------------------------------------------------
# Test 6: mark_unhealthy skips provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_unhealthy_skips_provider():
    """Marked-unhealthy provider should be skipped."""
    primary = MockAdapter("PrimaryAdapter")
    primary.send.return_value = {"id": "primary"}

    fallback = MockAdapter("FallbackAdapter")
    fallback.send.return_value = {"id": "fallback"}

    manager = FallbackManager(
        [(primary, "m1"), (fallback, "m2")],
        max_retries=1,
    )

    # Mark primary unhealthy
    manager.mark_unhealthy("MockAdapter", duration_seconds=60)

    # Primary is MockAdapter (first in chain) — but both are MockAdapter!
    # So both will be skipped. Let's use distinct names.
    # Rebuild with distinct types:
    primary2 = BridgeAdapter()
    primary2.send.return_value = {"id": "bridge"}
    fallback2 = DeepInfraAdapter()
    fallback2.send.return_value = {"id": "deepinfra"}

    manager2 = FallbackManager(
        [(primary2, "m1"), (fallback2, "m2")],
        max_retries=1,
    )
    manager2.mark_unhealthy("BridgeAdapter", duration_seconds=60)

    result = await manager2.execute({"messages": []})
    assert result["id"] == "deepinfra"
    # BridgeAdapter should not have been called
    primary2.send.assert_not_called()
    fallback2.send.assert_called_once()


# ---------------------------------------------------------------------------
# Test 7: Provider auto-recovers after duration expires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_recovery_after_cooldown():
    """Provider should auto-recover after unhealthy duration expires."""
    adapter = BridgeAdapter()
    adapter.send.return_value = {"id": "recovered"}

    manager = FallbackManager([(adapter, "m1")], max_retries=1)
    manager.mark_unhealthy("BridgeAdapter", duration_seconds=0)  # immediate recovery

    # Wait a tiny bit to ensure time has passed
    await asyncio.sleep(0.01)

    result = await manager.execute({"messages": []})
    assert result["id"] == "recovered"
    adapter.send.assert_called_once()


# ---------------------------------------------------------------------------
# Test 8: get_provider_health returns correct status
# ---------------------------------------------------------------------------


def test_get_provider_health():
    """Health snapshot should reflect current provider states."""
    a1 = BridgeAdapter()
    a2 = DeepInfraAdapter()

    manager = FallbackManager([(a1, "m1"), (a2, "m2")])

    health = manager.get_provider_health()
    assert "BridgeAdapter" in health
    assert "DeepInfraAdapter" in health
    assert health["BridgeAdapter"]["healthy"] is True
    assert health["BridgeAdapter"]["failure_count"] == 0
    assert health["BridgeAdapter"]["recovery_time"] is None

    # Mark one unhealthy
    manager.mark_unhealthy("BridgeAdapter", duration_seconds=100)
    health = manager.get_provider_health()
    assert health["BridgeAdapter"]["healthy"] is False
    assert health["BridgeAdapter"]["recovery_time"] is not None
    assert health["BridgeAdapter"]["recovery_time"] > 0


# ---------------------------------------------------------------------------
# Test 9: Fallback events are logged correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_events_logged():
    """Each fallback attempt should generate a FallbackEvent."""
    from router.adapters.bridge import BridgeAPIError

    primary = BridgeAdapter()
    primary.send.side_effect = BridgeAPIError(429, "rate limit")

    fallback = DeepInfraAdapter()
    fallback.send.return_value = {"id": "ok"}

    manager = FallbackManager(
        [(primary, "m1"), (fallback, "m2")],
        max_retries=2,
        backoff_base=0.01,
    )
    await manager.execute({"messages": []})

    # 2 retry attempts on primary → 2 events
    assert len(manager.events) == 2
    for event in manager.events:
        assert event.failed_provider == "BridgeAdapter"
        assert event.error_type == "rate_limit"
        assert event.fallback_provider == "DeepInfraAdapter"


# ---------------------------------------------------------------------------
# Test 10: Streaming fallback collects chunks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_fallback():
    """Streaming response should be collected and returned as dict."""
    adapter = MockAdapter("StreamAdapter")

    async def mock_stream(*args, **kwargs):
        return {"chunks": [{"content": "part1"}, {"content": "part2"}], "usage": None}

    adapter.send = mock_stream

    manager = FallbackManager([(adapter, "model-a")])
    result = await manager.execute({"messages": [], "stream": True})

    assert "chunks" in result
    assert len(result["chunks"]) == 2


# ---------------------------------------------------------------------------
# Test 11: Empty chain raises ValueError
# ---------------------------------------------------------------------------


def test_empty_chain_raises():
    """Empty fallback chain should raise ValueError."""
    with pytest.raises(ValueError, match="at least one provider"):
        FallbackManager([])


# ---------------------------------------------------------------------------
# Test 12: chain_names property
# ---------------------------------------------------------------------------


def test_chain_names():
    """chain_names should return ordered provider names."""
    a1 = BridgeAdapter()
    a2 = DeepInfraAdapter()
    manager = FallbackManager([(a1, "m1"), (a2, "m2")])
    assert manager.chain_names == ["BridgeAdapter", "DeepInfraAdapter"]
