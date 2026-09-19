"""Tests for routing logic."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from router.models import ChatCompletionRequest, Message, ToolCall, ToolDefinition
from router.analyzer import RequestType
from router.adapters import AlibabaAdapter, BridgeAdapter, DeepInfraAdapter
from router.routing import (
    Router,
    NoRouteFoundError,
    UnknownProviderError,
    _parse_provider_model,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def router():
    """Fresh Router instance for each test."""
    Router.reset_instance()
    r = Router()
    yield r
    Router.reset_instance()


@pytest.fixture
def config_dir(tmp_path):
    """Create a temporary config directory with a test routing.yaml."""
    config_path = tmp_path / "config" / "routing.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """
routing_rules:
  tool_calling:
    primary: deepinfra:V4-Flash-0731
    fallbacks:
      - alibaba:qwen-max
      - deepseek-bridge:v4-instant
    description: "Test tool calling"

  vision:
    primary: deepseek-bridge:v4-instant
    fallback: deepinfra:GLM-5.3-Flash
    description: "Test vision"

  reasoning:
    primary: deepseek-bridge:expert
    fallbacks:
      - alibaba:qwen-max
      - deepinfra:V4-Flash-0731
    description: "Test reasoning"

  search:
    primary: deepseek-bridge:v4-instant
    description: "Test search"

  chat:
    primary: deepseek-bridge:v4-instant
    fallbacks:
      - deepinfra:V4-Flash-0731
      - alibaba:qwen-max
    description: "Test chat"

providers:
  deepseek-bridge:
    base_url: http://localhost:8000
    description: "Test bridge"

  deepinfra:
    base_url: https://api.deepinfra.com/v1/openai
    api_key_env: DEEPINFRA_API_KEY
    description: "Test DeepInfra"

  alibaba:
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: ALIBABA_API_KEY
    description: "Test Alibaba"
"""
    )
    return config_path.parent.parent


# ---------------------------------------------------------------------------
# Helper: parse_provider_model
# ---------------------------------------------------------------------------


def test_parse_provider_model_valid():
    """Should parse valid provider:model strings."""
    provider, model = _parse_provider_model("deepinfra:V4-Flash-0731")
    assert provider == "deepinfra"
    assert model == "V4-Flash-0731"


def test_parse_provider_model_with_spaces():
    """Should strip whitespace from provider and model."""
    provider, model = _parse_provider_model(" alibaba : qwen-max ")
    assert provider == "alibaba"
    assert model == "qwen-max"


def test_parse_provider_model_invalid_no_colon():
    """Should raise ValueError for strings without colon."""
    with pytest.raises(ValueError, match="Invalid provider:model spec"):
        _parse_provider_model("deepinfra")


def test_parse_provider_model_invalid_empty():
    """Should raise ValueError for empty provider or model."""
    with pytest.raises(ValueError, match="Invalid provider:model spec"):
        _parse_provider_model(":model")
    with pytest.raises(ValueError, match="Invalid provider:model spec"):
        _parse_provider_model("provider:")


# ---------------------------------------------------------------------------
# Router initialization
# ---------------------------------------------------------------------------


def test_router_loads_config(router):
    """Router should load routing rules from config."""
    assert router.routing_rules
    assert "chat" in router.routing_rules
    assert "tool_calling" in router.routing_rules


def test_router_custom_config_path(config_dir):
    """Router should load from a custom config path."""
    config_path = config_dir / "config" / "routing.yaml"
    r = Router(config_path=config_path)
    assert r.config_path == config_path
    assert "chat" in r.routing_rules


def test_router_missing_config():
    """Router should raise FileNotFoundError for missing config."""
    with pytest.raises(FileNotFoundError, match="Routing config not found"):
        Router(config_path="/nonexistent/config.yaml")


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------


def test_get_fallback_chain_with_list(router):
    """Should return fallbacks list when 'fallbacks' key exists."""
    chain = router.get_fallback_chain(RequestType.TOOL_CALLING)
    assert isinstance(chain, list)
    assert len(chain) == 2
    assert "alibaba:qwen3.7-max" in chain
    assert "deepseek-bridge:v4-instant" in chain


def test_get_fallback_chain_vision(router):
    """Should return vision fallbacks list."""
    chain = router.get_fallback_chain(RequestType.VISION)
    assert isinstance(chain, list)
    assert len(chain) == 1
    assert "deepseek-bridge:v4-instant" in chain


def test_get_fallback_chain_search(router):
    """Should return search fallbacks list."""
    chain = router.get_fallback_chain(RequestType.SEARCH)
    assert isinstance(chain, list)
    assert len(chain) == 2
    assert "alibaba:qwen3.8-flash" in chain
    assert "deepinfra:V4-Flash-0731" in chain


# ---------------------------------------------------------------------------
# Adapter instantiation
# ---------------------------------------------------------------------------


def test_router_creates_bridge_adapter(router):
    """Should create BridgeAdapter for deepseek-bridge provider."""
    adapter = router._get_adapter("deepseek-bridge")
    assert isinstance(adapter, BridgeAdapter)
    assert adapter.base_url == "http://localhost:8002"


def test_router_creates_deepinfra_adapter(router):
    """Should create DeepInfraAdapter for deepinfra provider."""
    adapter = router._get_adapter("deepinfra")
    assert isinstance(adapter, DeepInfraAdapter)
    assert adapter.base_url == "https://api.deepinfra.com/v1/openai"


def test_router_creates_alibaba_adapter_with_key(router):
    """Should create AlibabaAdapter when API key is set."""
    with patch.dict(os.environ, {"ALIBABA_API_KEY": "test-key"}):
        router._adapter_cache.clear()  # Force recreation
        adapter = router._get_adapter("alibaba")
        assert isinstance(adapter, AlibabaAdapter)


def test_router_unknown_provider():
    """Should raise UnknownProviderError for unknown provider."""
    r = Router()
    with pytest.raises(UnknownProviderError, match="Unknown provider"):
        r._get_adapter("unknown-provider")


def test_router_caches_adapters(router):
    """Should cache adapter instances."""
    adapter1 = router._get_adapter("deepseek-bridge")
    adapter2 = router._get_adapter("deepseek-bridge")
    assert adapter1 is adapter2


# ---------------------------------------------------------------------------
# Route method (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_chat_request(router):
    """Should route simple chat request to bridge adapter."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="user", content="Hello!")],
    )
    adapter, model, request_type = await router.route(request.model_dump())
    assert isinstance(adapter, BridgeAdapter)
    assert model == "v4-instant"
    assert request_type == RequestType.CHAT


@pytest.mark.asyncio
async def test_route_tool_calling_request(router):
    """Should route tool calling request to deepinfra adapter."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="user", content="What's the weather?")],
        tools=[
            ToolDefinition(
                type="function",
                function={"name": "get_weather", "parameters": {}},
            )
        ],
    )
    adapter, model, request_type = await router.route(request.model_dump())
    assert isinstance(adapter, DeepInfraAdapter)
    assert model == "V4-Flash-0731"
    assert request_type == RequestType.TOOL_CALLING


@pytest.mark.asyncio
async def test_route_vision_request(router):
    """Should route vision request to deepinfra adapter (GLM-5.3-Flash)."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[
            Message(
                role="user",
                content=[
                    {"type": "text", "text": "What's in this image?"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/img.jpg"}},
                ],
            )
        ],
    )
    adapter, model, request_type = await router.route(request.model_dump())
    assert isinstance(adapter, DeepInfraAdapter)
    assert model == "GLM-5.3-Flash"
    assert request_type == RequestType.VISION


@pytest.mark.asyncio
async def test_route_reasoning_request(router):
    """Should route reasoning request to bridge adapter."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[
            Message(
                role="user",
                content="Explain why the sky is blue. Prove your answer with math and derive the equations.",
            )
        ],
    )
    adapter, model, request_type = await router.route(request.model_dump())
    assert isinstance(adapter, BridgeAdapter)
    assert model == "expert"
    assert request_type == RequestType.REASONING


@pytest.mark.asyncio
async def test_route_search_request(router):
    """Should route search request to bridge adapter."""
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="user", content="What's the latest news about AI?")],
    )
    adapter, model, request_type = await router.route(request.model_dump())
    assert isinstance(adapter, BridgeAdapter)
    assert model == "v4-instant"
    assert request_type == RequestType.SEARCH


# ---------------------------------------------------------------------------
# Fallback behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_fallback_on_primary_failure(config_dir):
    """Should fall back when primary provider is unavailable."""
    config_path = config_dir / "config" / "routing.yaml"

    # Create a config where primary is unknown
    config_path.write_text(
        """
routing_rules:
  chat:
    primary: unknown-provider:model
    fallbacks:
      - deepseek-bridge:v4-instant
      - deepinfra:V4-Flash-0731

providers:
  deepseek-bridge:
    base_url: http://localhost:8000
  deepinfra:
    base_url: https://api.deepinfra.com/v1/openai
    api_key_env: DEEPINFRA_API_KEY
"""
    )

    r = Router(config_path=config_path)
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="user", content="Hello")],
    )

    # Should fall back to deepseek-bridge
    adapter, model, request_type = await r.route(request.model_dump())
    assert isinstance(adapter, BridgeAdapter)
    assert model == "v4-instant"
    assert request_type == RequestType.CHAT


@pytest.mark.asyncio
async def test_route_no_fallback_raises(config_dir):
    """Should raise NoRouteFoundError when all providers fail."""
    config_path = config_dir / "config" / "routing.yaml"

    # Create a config with no valid providers
    config_path.write_text(
        """
routing_rules:
  chat:
    primary: unknown1:model
    fallbacks:
      - unknown2:model

providers: {}
"""
    )

    r = Router(config_path=config_path)
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="user", content="Hello")],
    )

    with pytest.raises(NoRouteFoundError, match="All providers.*unavailable"):
        await r.route(request.model_dump())


# ---------------------------------------------------------------------------
# Config reload
# ---------------------------------------------------------------------------


def test_router_reload_config(config_dir):
    """Should reload config from disk."""
    config_path = config_dir / "config" / "routing.yaml"
    r = Router(config_path=config_path)

    # Modify config
    new_config = """
routing_rules:
  chat:
    primary: alibaba:qwen-max
    description: "Updated chat rule"

providers:
  alibaba:
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: ALIBABA_API_KEY
"""
    config_path.write_text(new_config)

    # Reload
    r.reload_config()

    # Verify new config loaded
    assert r.routing_rules["chat"]["primary"] == "alibaba:qwen-max"
    assert r.routing_rules["chat"]["description"] == "Updated chat rule"

    # Adapter cache should be cleared
    assert len(r._adapter_cache) == 0


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_router_singleton():
    """Should return the same instance on multiple calls."""
    Router.reset_instance()
    r1 = Router.get_instance()
    r2 = Router.get_instance()
    assert r1 is r2
    Router.reset_instance()


def test_router_singleton_force_new():
    """Should create new instance when force_new=True."""
    Router.reset_instance()
    r1 = Router.get_instance()
    r2 = Router.get_instance(force_new=True)
    assert r1 is not r2
    Router.reset_instance()


# ---------------------------------------------------------------------------
# Route info
# ---------------------------------------------------------------------------


def test_get_route_info(router):
    """Should return full routing info for a request type."""
    info = router.get_route_info(RequestType.TOOL_CALLING)
    assert info["request_type"] == "tool_calling"
    assert info["primary"] == "deepinfra:V4-Flash-0731"
    assert "alibaba:qwen3.7-max" in info["fallbacks"]
    assert "tool" in info["description"].lower()  # Check it contains "tool"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_with_dict_input(router):
    """Should accept dict input (not just ChatCompletionRequest)."""
    request_dict = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    adapter, model, request_type = await router.route(request_dict)
    assert isinstance(adapter, BridgeAdapter)
    assert request_type == RequestType.CHAT


@pytest.mark.asyncio
async def test_route_no_rule_raises(config_dir):
    """Should raise NoRouteFoundError when no rule exists for type."""
    config_path = config_dir / "config" / "routing.yaml"

    # Config with no chat rule
    config_path.write_text(
        """
routing_rules:
  tool_calling:
    primary: deepinfra:V4-Flash-0731

providers:
  deepinfra:
    base_url: https://api.deepinfra.com/v1/openai
"""
    )

    r = Router(config_path=config_path)
    request = ChatCompletionRequest(
        model="deepseek-chat",
        messages=[Message(role="user", content="Hello")],
    )

    # Should detect CHAT but no rule exists
    with pytest.raises(NoRouteFoundError, match="No routing rule configured"):
        await r.route(request.model_dump())
