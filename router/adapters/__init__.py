"""Adapters package — exposes all provider adapters."""

from router.adapters.alibaba import (
    AlibabaAdapter,
    AlibabaAPIError,
    AlibabaAuthError,
    AlibabaRateLimitError,
    AlibabaServiceError,
)
from router.adapters.bridge import (
    BridgeAdapter,
    BridgeAPIError,
    BridgeConfig,
    BridgeConnectionError,
    BridgeError,
    BridgeTimeoutError,
)
from router.adapters.deepinfra import DeepInfraAdapter

__all__ = [
    "AlibabaAdapter",
    "AlibabaAPIError",
    "AlibabaAuthError",
    "AlibabaRateLimitError",
    "AlibabaServiceError",
    "BridgeAdapter",
    "BridgeAPIError",
    "BridgeConfig",
    "BridgeConnectionError",
    "BridgeError",
    "BridgeTimeoutError",
    "DeepInfraAdapter",
]
