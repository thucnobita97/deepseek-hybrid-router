"""Routing logic — selects provider based on request classification and routing table."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import yaml

from router.analyzer import RequestAnalysis, RequestAnalyzer, RequestType
from router.adapters import AlibabaAdapter, BridgeAdapter, DeepInfraAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "routing.yaml"

# Adapter class registry keyed by provider name
_ADAPTER_MAP: dict[str, type] = {
    "deepseek-bridge": BridgeAdapter,
    "deepinfra": DeepInfraAdapter,
    "alibaba": AlibabaAdapter,
}

# Singleton instance
_router_instance: Optional["Router"] = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RoutingError(Exception):
    """Base routing error."""


class NoRouteFoundError(RoutingError):
    """No provider configured for the detected request type."""


class UnknownProviderError(RoutingError):
    """Provider name in config doesn't map to a known adapter."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_provider_model(spec: str) -> Tuple[str, str]:
    """Parse a ``provider:model`` string into (provider, model).

    Raises ValueError if the string doesn't contain exactly one colon separator.
    """
    parts = spec.split(":", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"Invalid provider:model spec {spec!r}. Expected format 'provider:model'."
        )
    return parts[0].strip(), parts[1].strip()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class Router:
    """Routes incoming chat-completion requests to the appropriate provider adapter.

    The router:
    1. Loads routing rules from a YAML config file.
    2. Uses :class:`RequestAnalyzer` to classify the request.
    3. Looks up the primary provider for that request type.
    4. Returns the adapter instance, model name, and detected request type.

    Supports dynamic config reload via :meth:`reload_config`.
    """

    def __init__(
        self,
        config_path: Optional[Union[str, Path]] = None,
        analyzer: Optional[RequestAnalyzer] = None,
    ) -> None:
        self._config_path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
        self._analyzer = analyzer or RequestAnalyzer()
        self._config: Dict[str, Any] = {}
        self._routing_rules: Dict[str, Dict[str, Any]] = {}
        self._providers_config: Dict[str, Dict[str, Any]] = {}
        self._adapter_cache: Dict[str, Any] = {}

        self._load_config()

    # ------------------------------------------------------------------
    # Singleton access
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(
        cls,
        config_path: Optional[Union[str, Path]] = None,
        force_new: bool = False,
    ) -> "Router":
        """Return the singleton Router instance, creating it if needed."""
        global _router_instance
        if _router_instance is None or force_new:
            _router_instance = cls(config_path=config_path)
        return _router_instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton (useful for testing)."""
        global _router_instance
        _router_instance = None

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        """Load and parse the routing YAML config."""
        if not self._config_path.exists():
            raise FileNotFoundError(
                f"Routing config not found: {self._config_path}"
            )

        with open(self._config_path, "r") as fh:
            self._config = yaml.safe_load(fh) or {}

        self._routing_rules = self._config.get("routing_rules", {})
        self._providers_config = self._config.get("providers", {})

        logger.info(
            "Loaded routing config from %s — %d rules, %d providers",
            self._config_path,
            len(self._routing_rules),
            len(self._providers_config),
        )

    def reload_config(self) -> None:
        """Reload config from disk (supports hot-reloading routing rules).

        Clears the adapter cache so new provider configs take effect.
        """
        self._adapter_cache.clear()
        self._load_config()
        logger.info("Routing config reloaded from %s", self._config_path)

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def routing_rules(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._routing_rules)

    # ------------------------------------------------------------------
    # Adapter management
    # ------------------------------------------------------------------

    def _get_adapter(self, provider: str) -> Any:
        """Return a cached adapter instance for *provider*.

        Creates the adapter on first use, pulling base_url and api_key_env
        from the ``providers`` section of the config.
        """
        if provider in self._adapter_cache:
            return self._adapter_cache[provider]

        adapter_cls = _ADAPTER_MAP.get(provider)
        if adapter_cls is None:
            raise UnknownProviderError(
                f"Unknown provider {provider!r}. "
                f"Known providers: {list(_ADAPTER_MAP.keys())}"
            )

        provider_cfg = self._providers_config.get(provider, {})
        base_url = provider_cfg.get("base_url")
        api_key_env = provider_cfg.get("api_key_env")

        # Build adapter kwargs based on provider type
        if adapter_cls is BridgeAdapter:
            kwargs: Dict[str, Any] = {}
            if base_url:
                kwargs["base_url"] = base_url
            adapter = BridgeAdapter(**kwargs)

        elif adapter_cls is DeepInfraAdapter:
            kwargs = {}
            if base_url:
                kwargs["base_url"] = base_url
            if api_key_env:
                api_key = os.environ.get(api_key_env)
                if api_key:
                    kwargs["api_key"] = api_key
            adapter = DeepInfraAdapter(**kwargs)

        elif adapter_cls is AlibabaAdapter:
            kwargs = {}
            if base_url:
                kwargs["base_url"] = base_url
            # AlibabaAdapter requires api_key; check env
            if api_key_env:
                api_key = os.environ.get(api_key_env)
                if api_key:
                    kwargs["api_key"] = api_key
            # If no key available, skip caching — will raise on use
            if "api_key" not in kwargs and not os.environ.get("ALIBABA_API_KEY"):
                logger.warning(
                    "ALIBABA_API_KEY not set; AlibabaAdapter will raise on instantiation"
                )
            try:
                adapter = AlibabaAdapter(**kwargs)
            except ValueError:
                raise UnknownProviderError(
                    f"Cannot create AlibabaAdapter for provider {provider!r}: "
                    "ALIBABA_API_KEY not set"
                )
        else:
            adapter = adapter_cls()

        self._adapter_cache[provider] = adapter
        logger.debug("Created adapter %s for provider %r", type(adapter).__name__, provider)
        return adapter

    # ------------------------------------------------------------------
    # Provider:model resolution
    # ------------------------------------------------------------------

    def _resolve_provider_model(self, spec: str) -> Tuple[Any, str]:
        """Parse spec, get adapter, return (adapter_instance, model_name).

        Raises UnknownProviderError if the provider is not in the adapter map.
        """
        provider, model = _parse_provider_model(spec)
        adapter = self._get_adapter(provider)
        return adapter, model

    # ------------------------------------------------------------------
    # Core routing
    # ------------------------------------------------------------------

    async def route(
        self, request: dict
    ) -> Tuple[Any, str, RequestType]:
        """Classify a request and route it to the appropriate adapter.

        Parameters
        ----------
        request:
            Raw dict representation of a ChatCompletionRequest (as received
            by FastAPI before Pydantic validation, or a dict-compatible object).

        Returns
        -------
        tuple[adapter, model_name, request_type]
            The adapter instance, the model name string from config,
            and the detected request type.

        Raises
        ------
        NoRouteFoundError
            If no primary or fallback is configured for the detected type.
        UnknownProviderError
            If the primary provider can't be instantiated.
        """
        from router.main import ChatCompletionRequest

        # Accept both dict and ChatCompletionRequest
        if isinstance(request, ChatCompletionRequest):
            parsed_request = request
        elif isinstance(request, dict):
            parsed_request = ChatCompletionRequest(**request)
        else:
            parsed_request = request

        # Classify
        analysis: RequestAnalysis = self._analyzer.analyze(parsed_request)
        request_type = analysis.request_type

        logger.info(
            "Routing decision: type=%s confidence=%.3f reasoning=%s",
            request_type.value,
            analysis.confidence,
            analysis.reasoning,
        )

        # Look up routing rule for this type
        rule = self._routing_rules.get(request_type.value)
        if not rule:
            raise NoRouteFoundError(
                f"No routing rule configured for request type {request_type.value!r}"
            )

        primary_spec = rule.get("primary")
        if not primary_spec:
            raise NoRouteFoundError(
                f"No primary provider configured for request type {request_type.value!r}"
            )

        # Try primary, fall back to first available fallback
        try:
            adapter, model = self._resolve_provider_model(primary_spec)
            logger.info(
                "Selected primary provider: %s (model=%s) for type=%s",
                primary_spec,
                model,
                request_type.value,
            )
            return adapter, model, request_type
        except (UnknownProviderError, ValueError) as exc:
            logger.warning(
                "Primary provider %s unavailable for type=%s: %s. Trying fallbacks.",
                primary_spec,
                request_type.value,
                exc,
            )

        # Try fallbacks
        fallbacks = self.get_fallback_chain(request_type)
        for fb_spec in fallbacks:
            try:
                adapter, model = self._resolve_provider_model(fb_spec)
                logger.info(
                    "Selected fallback provider: %s (model=%s) for type=%s",
                    fb_spec,
                    model,
                    request_type.value,
                )
                return adapter, model, request_type
            except (UnknownProviderError, ValueError) as exc:
                logger.warning(
                    "Fallback provider %s also unavailable: %s",
                    fb_spec,
                    exc,
                )
                continue

        raise NoRouteFoundError(
            f"All providers (primary + fallbacks) unavailable for type={request_type.value!r}. "
            f"Tried: {primary_spec}, fallbacks: {fallbacks}"
        )

    # ------------------------------------------------------------------
    # Fallback chain
    # ------------------------------------------------------------------

    def get_fallback_chain(self, request_type: RequestType) -> List[str]:
        """Return the ordered fallback provider:model list for a request type.

        Returns an empty list if no fallbacks are configured.
        Handles both ``fallbacks`` (list) and ``fallback`` (single string) keys.
        """
        rule = self._routing_rules.get(request_type.value, {})
        fallbacks: List[str] = []

        # Support "fallbacks" (list) key
        fb_list = rule.get("fallbacks")
        if isinstance(fb_list, list):
            fallbacks.extend(fb_list)

        # Support "fallback" (single string) key — e.g. vision rule
        fb_single = rule.get("fallback")
        if isinstance(fb_single, str) and fb_single not in fallbacks:
            fallbacks.append(fb_single)

        return fallbacks

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def get_route_info(self, request_type: RequestType) -> Dict[str, Any]:
        """Return full routing info for a request type (for debugging/inspection)."""
        rule = self._routing_rules.get(request_type.value, {})
        return {
            "request_type": request_type.value,
            "primary": rule.get("primary"),
            "fallbacks": self.get_fallback_chain(request_type),
            "description": rule.get("description", ""),
        }

    async def close(self) -> None:
        """Close all cached adapter HTTP clients."""
        for provider, adapter in self._adapter_cache.items():
            try:
                await adapter.close()
                logger.debug("Closed adapter for provider %s", provider)
            except Exception:
                logger.warning(
                    "Error closing adapter for provider %s", provider, exc_info=True
                )
        self._adapter_cache.clear()

    def __repr__(self) -> str:
        rules = list(self._routing_rules.keys())
        return f"<Router config={self._config_path.name} rules={rules}>"


__all__ = [
    "Router",
    "RoutingError",
    "NoRouteFoundError",
    "UnknownProviderError",
    "_parse_provider_model",
]
