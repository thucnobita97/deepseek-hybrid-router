# Configuration Guide

Detailed configuration options for the DeepSeek Hybrid Router.

## Configuration Files

The router uses three main configuration files:

1. **`config/routing.yaml`** — Routing rules and provider definitions
2. **`config/confirmation.yaml`** — Confirmation mode settings (planned)
3. **`config/pricing.json`** — Provider pricing for cost tracking (planned)
4. **`.env`** — Environment variables (API keys, URLs)

## routing.yaml

The main configuration file that defines how requests are routed to providers.

### Structure

```yaml
routing_rules:
  <request_type>:
    primary: <provider:model>
    fallbacks:
      - <provider:model>
      - <provider:model>
    description: <string>

providers:
  <provider_name>:
    base_url: <url>
    api_key_env: <env_var_name>
    description: <string>
```

### Request Types

The router classifies requests into five types. Each type needs a routing rule:

#### `tool_calling`

Requests with function/tool definitions or tool-related messages.

**Detection criteria:**
- `tools` or `functions` field in request
- Messages with `role: "tool"`
- Messages with `tool_calls` field

**Recommended providers:**
```yaml
tool_calling:
  primary: deepinfra:V4-Flash-0731
  fallbacks:
    - alibaba:qwen-max
    - deepseek-bridge:v4-instant
  description: "Requests with function/tool calls"
```

**Why DeepInfra first:** Best tool-calling support, stable API.

#### `vision`

Requests containing image content.

**Detection criteria:**
- Message content blocks with `type: "image_url"` or `type: "image"`

**Recommended providers:**
```yaml
vision:
  primary: deepseek-bridge:v4-instant
  fallback: deepinfra:GLM-5.3-Flash
  description: "Requests with image content"
```

**Why Bridge first:** Free vision via reverse-engineered upload. GLM as stable fallback.

#### `reasoning`

Complex reasoning, math, logic, architecture tasks.

**Detection criteria:**
- Keywords: "explain why", "prove", "derive", "math", "calculate", "analyze", "architecture", "compare and contrast", "step by step reasoning", "think through", "design pattern", "theorem", "deduce", "formal proof"
- Long user messages (>500 chars) get +1 score
- Multiple question marks (≥2) get +1 score
- Threshold: score ≥ 3

**Recommended providers:**
```yaml
reasoning:
  primary: deepseek-bridge:expert
  fallbacks:
    - alibaba:qwen-max
    - deepinfra:V4-Flash-0731
  description: "Complex reasoning, math, logic tasks"
```

**Why Bridge Expert first:** DeepSeek's reasoner model is excellent. Qwen3.7-max as strong fallback.

#### `search`

Web search and information retrieval.

**Detection criteria:**
- Keywords: "latest", "current", "today", "recent", "news", "search for", "find out", "up to date", "what happened", "who won", "breaking", "yesterday", "this week"

**Recommended providers:**
```yaml
search:
  primary: deepseek-bridge:v4-instant
  description: "Web search and information retrieval"
```

**Why Bridge only:** DeepSeek's web chat has built-in search augmentation. No fallback needed.

#### `chat`

General conversation — the default when no specialized signal is detected.

**Detection criteria:**
- None of the above types match

**Recommended providers:**
```yaml
chat:
  primary: deepseek-bridge:v4-instant
  fallbacks:
    - deepinfra:V4-Flash-0731
    - alibaba:qwen-max
  description: "General conversation and simple queries"
```

**Why Bridge first:** Free and fast. Paid providers as fallback when bridge is down.

### Provider Format

Each routing rule uses the format `provider:model`:

```yaml
primary: deepinfra:V4-Flash-0731
```

- **`provider`**: Must match a key in the `providers` section
- **`model`**: Model name or alias (see provider-specific model mappings below)

### Fallbacks

Fallbacks can be a list or a single string:

```yaml
# List of fallbacks (recommended)
fallbacks:
  - alibaba:qwen-max
  - deepseek-bridge:v4-instant

# Single fallback (also supported)
fallback: deepinfra:GLM-5.3-Flash
```

The router tries providers in order:
1. `primary`
2. First item in `fallbacks`
3. Second item in `fallbacks`
4. ...and so on

If a provider fails with a retryable error (429, 502, 503, timeout), the router moves to the next. Auth errors (401, 403) skip immediately to the next provider.

### Provider Definitions

The `providers` section defines connection details for each provider:

```yaml
providers:
  deepseek-bridge:
    base_url: http://localhost:8000
    description: "Local DeepSeek-API fork"
    
  deepinfra:
    base_url: https://api.deepinfra.com/v1/openai
    api_key_env: DEEPINFRA_API_KEY
    description: "DeepInfra API"
    
  alibaba:
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: ALIBABA_API_KEY
    description: "Alibaba Token Plan (DashScope)"
```

**Fields:**
- `base_url` (required): The provider's API endpoint
- `api_key_env` (optional): Environment variable name for the API key
- `description` (optional): Human-readable description

### Model Mappings

Each provider has its own model naming. The adapters map short aliases to full model IDs:

#### DeepSeek Bridge (`deepseek-bridge`)

| Alias | Full Model ID | Use Case |
|-------|--------------|----------|
| `v4-instant` | `deepseek-chat` | Fast chat, search, vision |
| `expert` | `deepseek-reasoner` | Deep reasoning |

#### DeepInfra (`deepinfra`)

| Alias | Full Model ID | Use Case |
|-------|--------------|----------|
| `V4-Flash-0731` | `deepseek-ai/DeepSeek-V4-Flash-0731` | Tool calling, general |
| `V4.1-Flash` | `deepseek-ai/DeepSeek-V4.1-Flash` | Alternative DeepSeek |
| `GLM-5.3-Flash` | `zai-org/GLM-5.3-Flash` | Vision fallback |

**Blocked models:** `Pro` and `Vision-Exp` variants are disabled.

#### Alibaba DashScope (`alibaba`)

| Alias | Full Model ID | Use Case |
|-------|--------------|----------|
| `qwen3.7-max` | `qwen3.7-max` | Complex reasoning |
| `qwen3.8-flash` | `qwen3.8-flash` | Fast lightweight |

**Allowed patterns:** Only `qwen3.7-*` and `qwen3.8-flash` models are permitted.

## Environment Variables

Defined in `.env` file or exported in shell:

```bash
# Required for DeepInfra provider
DEEPINFRA_API_KEY=your-deepinfra-api-key

# Required for Alibaba provider
ALIBABA_API_KEY=your-alibaba-api-key

# DeepSeek bridge URL (default: http://localhost:8000)
BRIDGE_URL=http://localhost:8000

# Router port (default: 8001)
ROUTER_PORT=8001

# Confirmation mode: config, cli, or owui (default: config)
CONFIRMATION_MODE=config

# Redis URL for session persistence (optional)
REDIS_URL=redis://localhost:6379/0
```

### Variable Details

#### `DEEPINFRA_API_KEY`

**Required if using DeepInfra provider.**

Get your key from https://deepinfra.com/dashboard

```bash
DEEPINFRA_API_KEY=sk-...
```

#### `ALIBABA_API_KEY`

**Required if using Alibaba provider.**

Sign up for Alibaba Cloud DashScope and create an API key.

```bash
ALIBABA_API_KEY=sk-...
```

#### `BRIDGE_URL`

URL of the DeepSeek-API bridge. Default: `http://localhost:8000`

```bash
BRIDGE_URL=http://localhost:8000
```

If running the bridge on a different port or host, update this.

#### `ROUTER_PORT`

Port the router listens on. Default: `8001`

```bash
ROUTER_PORT=8001
```

#### `CONFIRMATION_MODE`

How the router handles sensitive actions (planned feature):

- `config` — Auto-approve based on rules in `confirmation.yaml`
- `cli` — Prompt user in terminal
- `owui` — Prompt via Open WebUI dialog

Default: `config`

```bash
CONFIRMATION_MODE=config
```

#### `REDIS_URL`

Redis connection URL for session persistence (optional).

```bash
REDIS_URL=redis://localhost:6379/0
```

If using Docker Compose, Redis is included and the URL is `redis://redis:6379/0`.

## Hot-Reloading Configuration

The router supports hot-reloading `routing.yaml` without restart:

```bash
curl -X POST http://localhost:8001/v1/routing/reload
```

**What gets reloaded:**
- Routing rules (primary, fallbacks)
- Provider definitions (base_url, api_key_env)
- Adapter cache is cleared (new adapters created on next request)

**What doesn't get reloaded:**
- Environment variables (restart required)
- Port binding (restart required)

**Use cases:**
- Update routing rules on the fly
- Add/remove providers without downtime
- Change fallback chains during operation

**Example workflow:**

1. Edit `config/routing.yaml`:
```yaml
chat:
  primary: alibaba:qwen3.8-flash  # Changed from bridge
  fallbacks:
    - deepinfra:V4-Flash-0731
```

2. Reload:
```bash
curl -X POST http://localhost:8001/v1/routing/reload
# {"status": "reloaded", "config_path": "/app/config/routing.yaml"}
```

3. Verify:
```bash
curl http://localhost:8001/v1/routing/info | jq '.routing_table.chat'
```

## Advanced Configuration

### Custom Reasoning Threshold

The reasoning detector uses a score threshold (default: 3). To change it, modify the analyzer in code:

```python
from router.analyzer import RequestAnalyzer

analyzer = RequestAnalyzer(reasoning_threshold=5)  # More strict
```

### Custom Timeouts

Timeouts are configured in adapter code. To override, modify adapter initialization:

```python
from router.adapters.bridge import BridgeAdapter, BridgeConfig

config = BridgeConfig(
    timeout=180.0,        # 3 minutes
    connect_timeout=15.0  # 15 seconds
)
adapter = BridgeAdapter(base_url="http://localhost:8000", config=config)
```

### Fallback Backoff

The fallback manager uses exponential backoff between provider switches. Default base: 1.0 second (doubles each step: 1, 2, 4, 8...).

To change, modify fallback manager initialization:

```python
from router.fallback import FallbackManager

manager = FallbackManager(
    chain=[(adapter1, "model1"), (adapter2, "model2")],
    max_retries=3,        # Retry each provider 3 times
    backoff_base=2.0      # Start with 2s, then 4s, 8s...
)
```

### Provider Health Tracking

The fallback manager tracks provider health and temporarily marks failed providers as unhealthy (30-second cooldown by default).

To manually mark a provider unhealthy:

```python
fallback_manager.mark_unhealthy("BridgeAdapter", duration_seconds=60)
```

To check health:

```python
health = fallback_manager.get_provider_health()
print(health)
# {
#   "BridgeAdapter": {
#     "healthy": False,
#     "last_error": "Connection refused",
#     "failure_count": 3,
#     "recovery_time": 25.5
#   },
#   ...
# }
```

## Docker Configuration

### docker-compose.yml

The included `docker-compose.yml` runs the router with Redis:

```yaml
version: "3.9"

services:
  router:
    build: .
    ports:
      - "8001:8001"
    environment:
      - DEEPINFRA_API_KEY=${DEEPINFRA_API_KEY}
      - ALIBABA_API_KEY=${ALIBABA_API_KEY}
      - BRIDGE_URL=${BRIDGE_URL:-http://localhost:8000}
      - ROUTER_PORT=8001
      - CONFIRMATION_MODE=${CONFIRMATION_MODE:-config}
      - REDIS_URL=redis://redis:6379/0
    depends_on:
      - redis
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis-data:/data
    restart: unless-stopped

volumes:
  redis-data:
```

**Customization:**

- Change port mapping: `"9000:8001"` (host:container)
- Add volume for config: `./config:/app/config`
- Remove Redis if not needed

### Dockerfile

The `Dockerfile` uses multi-stage build for smaller image:

```dockerfile
# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Runtime
FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /install /usr/local

COPY router/ ./router/
COPY config/ ./config/

EXPOSE 8001

CMD ["uvicorn", "router.main:app", "--host", "0.0.0.0", "--port", "8001"]
```

**Build and run:**

```bash
docker build -t deepseek-hybrid-router .
docker run -d -p 8001:8001 \
  -e DEEPINFRA_API_KEY=... \
  -e ALIBABA_API_KEY=... \
  deepseek-hybrid-router
```

## Example Configurations

### Minimal Setup (Bridge Only)

Use only the free DeepSeek bridge, no paid providers:

```yaml
routing_rules:
  tool_calling:
    primary: deepseek-bridge:v4-instant
  vision:
    primary: deepseek-bridge:v4-instant
  reasoning:
    primary: deepseek-bridge:expert
  search:
    primary: deepseek-bridge:v4-instant
  chat:
    primary: deepseek-bridge:v4-instant

providers:
  deepseek-bridge:
    base_url: http://localhost:8000
```

### Paid-Only Setup (No Bridge)

Use only stable paid providers:

```yaml
routing_rules:
  tool_calling:
    primary: deepinfra:V4-Flash-0731
    fallbacks:
      - alibaba:qwen-max
  vision:
    primary: deepinfra:GLM-5.3-Flash
  reasoning:
    primary: alibaba:qwen-max
    fallbacks:
      - deepinfra:V4-Flash-0731
  search:
    primary: deepinfra:V4-Flash-0731
  chat:
    primary: alibaba:qwen3.8-flash
    fallbacks:
      - deepinfra:V4-Flash-0731

providers:
  deepinfra:
    base_url: https://api.deepinfra.com/v1/openai
    api_key_env: DEEPINFRA_API_KEY
    
  alibaba:
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: ALIBABA_API_KEY
```

### High-Availability Setup

Maximum fallbacks for reliability:

```yaml
routing_rules:
  tool_calling:
    primary: deepinfra:V4-Flash-0731
    fallbacks:
      - deepinfra:V4.1-Flash
      - alibaba:qwen-max
      - deepseek-bridge:v4-instant
  vision:
    primary: deepseek-bridge:v4-instant
    fallbacks:
      - deepinfra:GLM-5.3-Flash
  reasoning:
    primary: deepseek-bridge:expert
    fallbacks:
      - alibaba:qwen-max
      - deepinfra:V4-Flash-0731
      - deepinfra:V4.1-Flash
  search:
    primary: deepseek-bridge:v4-instant
    fallbacks:
      - deepinfra:V4-Flash-0731
  chat:
    primary: deepseek-bridge:v4-instant
    fallbacks:
      - deepinfra:V4-Flash-0731
      - alibaba:qwen3.8-flash
      - deepinfra:V4.1-Flash
```

## Validation

After editing configuration, validate it:

```bash
# Check YAML syntax
python -c "import yaml; yaml.safe_load(open('config/routing.yaml'))"

# Reload and check routing info
curl -X POST http://localhost:8001/v1/routing/reload
curl http://localhost:8001/v1/routing/info | jq .
```

## Next Steps

- See [API Reference](api.md) for endpoint details
- Check [Troubleshooting](troubleshooting.md) for common issues
