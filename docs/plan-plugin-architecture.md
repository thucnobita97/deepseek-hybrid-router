# Plugin Architecture Plan

## Overview

Transform the current hard-coded bridge system into a modular plugin architecture that allows easy addition of new free API bridges and local LLM providers.

## Goal

Create a plugin system where:
- Each free API (DeepSeek, Copilot, Claude, Gemini, etc.) is a self-contained plugin
- Plugins can be added/removed without modifying core router code
- Community can contribute new plugins
- Plugin health, metrics, and configuration are managed independently

## Architecture

### Directory Structure

```
model-hybrid-nobita/
├── plugins/
│   ├── base.py                      # Plugin interface (ABC)
│   ├── registry.py                  # Plugin loader and manager
│   ├── deepseek-bridge/
│   │   ├── __init__.py
│   │   ├── plugin.json              # Metadata, version, config schema
│   │   ├── adapter.py               # BridgePlugin implementation
│   │   ├── health_check.py          # Custom health check logic
│   │   └── README.md
│   ├── copilot-bridge/
│   │   ├── __init__.py
│   │   ├── plugin.json
│   │   ├── adapter.py
│   │   └── README.md
│   ├── claude-bridge/               # Future: Claude free API
│   ├── gemini-bridge/               # Future: Gemini free API
│   └── llama-local/                 # Future: Local LLM (Ollama, LM Studio)
├── config/
│   ├── plugins.yaml                 # Plugin configuration
│   └── routing.yaml                 # Routing rules (unchanged)
└── router/
    └── plugin_router.py             # Plugin-aware routing logic
```

### Plugin Interface

```python
# plugins/base.py
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from pydantic import BaseModel

class PluginConfig(BaseModel):
    """Base plugin configuration"""
    enabled: bool = True
    priority: int = 1
    port: int = 8000
    rate_limit_rpm: int = 60  # requests per minute
    timeout_seconds: int = 30

class BridgePlugin(ABC):
    """Abstract base class for all bridge plugins"""
    
    name: str
    version: str
    description: str
    config: PluginConfig
    
    @abstractmethod
    async def send(self, request: dict) -> dict:
        """
        Send request to bridge, return response.
        
        Args:
            request: OpenAI-compatible chat completion request
            
        Returns:
            OpenAI-compatible response
        """
        pass
    
    @abstractmethod
    async def health_check(self) -> dict:
        """
        Check bridge health status.
        
        Returns:
            {
                "status": "healthy" | "degraded" | "unhealthy",
                "latency_ms": int,
                "message": str
            }
        """
        pass
    
    @abstractmethod
    def get_models(self) -> List[str]:
        """
        Return list of available models.
        
        Returns:
            List of model names (e.g., ["gpt-4", "deepseek-chat"])
        """
        pass
    
    async def get_metrics(self) -> dict:
        """
        Return plugin performance metrics.
        
        Returns:
            {
                "total_requests": int,
                "success_rate": float,
                "avg_latency_ms": float,
                "rate_limited_requests": int
            }
        """
        # Default implementation - plugins can override
        return {}

class PluginMetadata(BaseModel):
    """Plugin metadata from plugin.json"""
    name: str
    version: str
    description: str
    author: str
    repository: str
    models: List[str]
    requirements: List[str]  # Python dependencies
```

### Plugin Configuration

```yaml
# config/plugins.yaml
plugins:
  # List of enabled plugins (order = priority)
  enabled:
    - deepseek-bridge
    - copilot-bridge
  
  # Plugin-specific configuration
  deepseek-bridge:
    enabled: true
    priority: 1
    port: 8002
    rate_limit_rpm: 120
    timeout_seconds: 30
    config:
      base_url: "http://localhost:8002"
      session_timeout: 3600
  
  copilot-bridge:
    enabled: true
    priority: 2
    port: 8003
    rate_limit_rpm: 60
    timeout_seconds: 45
    config:
      base_url: "http://localhost:8003"
      max_concurrent: 4
  
  # Future plugins
  claude-bridge:
    enabled: false
    priority: 3
    port: 8004
    config:
      base_url: "http://localhost:8004"
  
  gemini-bridge:
    enabled: false
    priority: 4
    port: 8005
    config:
      base_url: "http://localhost:8005"
```

### Plugin Registry

```python
# plugins/registry.py
import importlib
import json
from pathlib import Path
from typing import Dict, List, Optional
from .base import BridgePlugin, PluginMetadata

class PluginRegistry:
    """Manages plugin discovery, loading, and lifecycle"""
    
    def __init__(self, plugins_dir: Path, config_path: Path):
        self.plugins_dir = plugins_dir
        self.config_path = config_path
        self._plugins: Dict[str, BridgePlugin] = {}
        self._metadata: Dict[str, PluginMetadata] = {}
    
    def discover_plugins(self) -> List[str]:
        """Scan plugins directory and return list of plugin names"""
        plugins = []
        for plugin_dir in self.plugins_dir.iterdir():
            if plugin_dir.is_dir() and (plugin_dir / "plugin.json").exists():
                plugins.append(plugin_dir.name)
        return plugins
    
    def load_plugin(self, name: str) -> BridgePlugin:
        """Load a plugin by name"""
        if name in self._plugins:
            return self._plugins[name]
        
        plugin_dir = self.plugins_dir / name
        metadata_path = plugin_dir / "plugin.json"
        
        # Load metadata
        with open(metadata_path) as f:
            metadata = PluginMetadata(**json.load(f))
        self._metadata[name] = metadata
        
        # Import plugin module
        module = importlib.import_module(f"plugins.{name}.adapter")
        plugin_class = getattr(module, f"{name.replace('-', '_').title().replace('_', '')}Plugin")
        
        # Instantiate plugin
        plugin = plugin_class()
        self._plugins[name] = plugin
        
        return plugin
    
    def get_enabled_plugins(self) -> List[BridgePlugin]:
        """Return list of enabled plugins sorted by priority"""
        # Load config
        import yaml
        with open(self.config_path) as f:
            config = yaml.safe_load(f)
        
        enabled_names = config.get("plugins", {}).get("enabled", [])
        plugins = []
        
        for name in enabled_names:
            try:
                plugin = self.load_plugin(name)
                if plugin.config.enabled:
                    plugins.append(plugin)
            except Exception as e:
                print(f"Failed to load plugin {name}: {e}")
        
        # Sort by priority
        plugins.sort(key=lambda p: p.config.priority)
        return plugins
    
    async def health_check_all(self) -> Dict[str, dict]:
        """Check health of all enabled plugins"""
        results = {}
        for plugin in self.get_enabled_plugins():
            try:
                results[plugin.name] = await plugin.health_check()
            except Exception as e:
                results[plugin.name] = {
                    "status": "unhealthy",
                    "error": str(e)
                }
        return results
```

## Phases

### Phase 6: Plugin Architecture Foundation (3 hours)

**Goal:** Design and implement core plugin system, migrate existing bridges to plugins

#### Task 6.1: Design Plugin Interface (1 hour)
- **File:** `plugins/base.py`
- **Model:** `alibaba:qwen3.7-max` (architecture design)
- **Actions:**
  - Define `BridgePlugin` ABC with abstract methods
  - Define `PluginConfig` Pydantic model
  - Define `PluginMetadata` Pydantic model
- **Verification:** Interface is clean, extensible, well-documented

#### Task 6.2: Implement Plugin Registry (1 hour)
- **File:** `plugins/registry.py`
- **Model:** `deepinfra:V4-Flash-0731` (code generation)
- **Actions:**
  - Implement plugin discovery (scan directory)
  - Implement plugin loading (dynamic import)
  - Implement plugin lifecycle (init, health check, metrics)
  - Implement priority-based sorting
- **Verification:** Registry can load plugins, sort by priority

#### Task 6.3: Migrate DeepSeek Bridge to Plugin (1 hour)
- **Files:** 
  - `plugins/deepseek-bridge/plugin.json`
  - `plugins/deepseek-bridge/adapter.py`
  - `plugins/deepseek-bridge/health_check.py`
- **Model:** `deepinfra:V4-Flash-0731`
- **Actions:**
  - Create plugin directory structure
  - Implement `DeepSeekBridgePlugin` class
  - Migrate existing adapter logic to plugin
  - Implement health check
  - Create `plugin.json` metadata
- **Verification:** Plugin loads, health check works, can send requests

#### Task 6.4: Migrate Copilot Bridge to Plugin (1 hour)
- **Files:**
  - `plugins/copilot-bridge/plugin.json`
  - `plugins/copilot-bridge/adapter.py`
- **Model:** `deepinfra:V4-Flash-0731`
- **Actions:**
  - Similar to Task 6.3 for Copilot
- **Verification:** Both plugins work independently

#### Task 6.5: Update Router to Use Plugin Registry (1 hour)
- **File:** `router/plugin_router.py` (new)
- **Model:** `alibaba:qwen3.7-max` (refactoring)
- **Actions:**
  - Replace hard-coded bridge logic with plugin registry
  - Update routing to use `registry.get_enabled_plugins()`
  - Maintain backward compatibility with existing config
- **Verification:** Router works with plugins, no breaking changes

#### Task 6.6: Plugin Configuration System (30 minutes)
- **File:** `config/plugins.yaml`
- **Model:** `alibaba:qwen3.8-flash` (simple task)
- **Actions:**
  - Create plugin config schema
  - Implement config loading and validation
  - Support hot-reload (watch file changes)
- **Verification:** Config loads correctly, validation works

#### Task 6.7: Plugin Metrics and Monitoring (1 hour)
- **File:** `router/plugin_metrics.py` (new)
- **Model:** `deepinfra:V4-Flash-0731`
- **Actions:**
  - Track requests per plugin
  - Track success rate, latency, rate limits
  - Expose metrics via `/v1/plugins/metrics` endpoint
- **Verification:** Metrics collected, endpoint returns data

#### Task 6.8: Documentation (1 hour)
- **Files:**
  - `docs/plugin-development-guide.md`
  - `plugins/README.md`
  - Update main `README.md`
- **Model:** `alibaba:qwen3.8-flash` (documentation)
- **Actions:**
  - Write plugin development guide
  - Document plugin interface
  - Provide example plugin template
  - Update architecture diagram
- **Verification:** Documentation is clear, examples work

**Deliverables:**
- Plugin system foundation
- DeepSeek and Copilot migrated to plugins
- Plugin registry, config, metrics
- Developer documentation

**Risks:**
- Breaking changes during migration → maintain backward compatibility
- Plugin loading performance → cache loaded plugins
- Config complexity → start simple, iterate

---

### Phase 7: Example Plugins (2 hours)

**Goal:** Create 2-3 example plugins to demonstrate extensibility

#### Task 7.1: Create Plugin Template (30 minutes)
- **File:** `plugins/template/` (example plugin)
- **Model:** `alibaba:qwen3.8-flash`
- **Actions:**
  - Create minimal plugin example
  - Include all required files (plugin.json, adapter.py, README.md)
  - Add inline documentation
- **Verification:** Template loads, can be used as starting point

#### Task 7.2: Implement Claude Bridge Plugin (1 hour)
- **Files:** `plugins/claude-bridge/`
- **Model:** `deepinfra:V4-Flash-0731`
- **Actions:**
  - Research Claude free API (if exists) or mock implementation
  - Implement plugin following template
  - Test integration with router
- **Verification:** Plugin works, router can route to Claude

#### Task 7.3: Implement Gemini Bridge Plugin (1 hour)
- **Files:** `plugins/gemini-bridge/`
- **Model:** `deepinfra:V4-Flash-0731`
- **Actions:**
  - Similar to Task 7.2 for Gemini
- **Verification:** Plugin works, router can route to Gemini

#### Task 7.4: Implement Local LLM Plugin (1 hour)
- **Files:** `plugins/llama-local/`
- **Model:** `alibaba:qwen3.7-max` (complex integration)
- **Actions:**
  - Support Ollama API (http://localhost:11434)
  - Support LM Studio API (http://localhost:1234)
  - Auto-detect available local models
- **Verification:** Plugin connects to local LLM, router can use it

**Deliverables:**
- Plugin template
- 3 example plugins (Claude, Gemini, Local LLM)
- Demonstrated extensibility

**Risks:**
- Free APIs may not exist → create mock implementations
- Local LLM setup complexity → provide clear documentation

---

### Phase 8: Plugin Ecosystem (2 hours)

**Goal:** Build tools and documentation for plugin ecosystem

#### Task 8.1: Plugin CLI Tool (1 hour)
- **File:** `scripts/plugin_manager.py`
- **Model:** `deepinfra:V4-Flash-0731`
- **Actions:**
  - `plugin list` - list all plugins
  - `plugin enable <name>` - enable plugin
  - `plugin disable <name>` - disable plugin
  - `plugin install <url>` - install plugin from git repo
  - `plugin health` - check all plugin health
- **Verification:** CLI commands work correctly

#### Task 8.2: Plugin Testing Framework (1 hour)
- **File:** `tests/test_plugins.py`
- **Model:** `alibaba:qwen3.7-max`
- **Actions:**
  - Create plugin test suite
  - Test plugin loading, health check, send
  - Test plugin isolation (one plugin fail doesn't affect others)
  - Test plugin metrics
- **Verification:** All tests pass, coverage > 80%

#### Task 8.3: Community Plugin Guidelines (1 hour)
- **File:** `docs/community-plugins.md`
- **Model:** `alibaba:qwen3.8-flash`
- **Actions:**
  - Document how to contribute plugins
  - Plugin review checklist
  - Security considerations
  - Performance benchmarks
- **Verification:** Guidelines are clear, actionable

#### Task 8.4: Plugin Marketplace (Future) (optional)
- **Note:** This is a future enhancement, not in current scope
- **Ideas:**
  - Plugin registry (like npm/pip)
  - Plugin ratings and reviews
  - Automated testing on submission
  - Security scanning

**Deliverables:**
- Plugin CLI tool
- Plugin testing framework
- Community guidelines

**Risks:**
- CLI complexity → start simple, add features incrementally
- Testing coverage → focus on critical paths first

---

## Summary

### Total Effort

| Phase | Duration | Description |
|-------|----------|-------------|
| Phase 6 | 9 hours | Plugin architecture foundation |
| Phase 7 | 4 hours | Example plugins |
| Phase 8 | 3 hours | Plugin ecosystem tools |
| **Total** | **16 hours** | **2 days** |

### Model Allocation

| Task Type | Model | Reason |
|-----------|-------|--------|
| Architecture design | `alibaba:qwen3.7-max` | Complex reasoning |
| Code generation | `deepinfra:V4-Flash-0731` | Fast, good quality |
| Simple tasks | `alibaba:qwen3.8-flash` | Fast, cheap |
| Refactoring | `alibaba:qwen3.7-max` | Complex logic |

### Success Criteria

- ✅ Plugin system is modular, extensible, well-documented
- ✅ Existing bridges (DeepSeek, Copilot) work as plugins
- ✅ 3 example plugins demonstrate extensibility
- ✅ CLI tool allows easy plugin management
- ✅ Testing framework ensures plugin quality
- ✅ Community can contribute plugins

### Dependencies

- **Must complete first:** Phase 1-5 (current plan)
- **Can start after:** System is stable with DeepSeek + Copilot bridges

### Future Enhancements (Not in Scope)

- Plugin marketplace / registry
- Automated plugin discovery
- Plugin versioning and updates
- Plugin security sandboxing
- Plugin performance profiling
- Multi-tenant plugin isolation

---

## Conclusion

This plugin architecture transforms the router from a fixed system into an extensible platform that can grow with new free APIs and local LLM providers. The modular design allows:

1. **Easy addition** of new bridges (Claude, Gemini, local LLMs)
2. **Community contributions** via plugin development
3. **Independent evolution** of each bridge
4. **Better maintainability** through isolation
5. **Future-proofing** for emerging AI providers

The phased approach ensures we build a solid foundation before adding complexity, with clear deliverables and success criteria at each stage.
