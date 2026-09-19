# DeepSeek Hybrid Router

A FastAPI-based smart proxy that sits between your OpenAI-compatible client (Hermes CLI, Open WebUI, any OpenAI SDK) and multiple AI providers. It analyzes incoming requests, classifies them by type, and routes them to the best available provider — with automatic fallback chains when a provider fails.

## Why?

Different AI models excel at different tasks. DeepSeek's free web-chat bridge is great for instant chat and search but fragile for tool calling. DeepInfra offers stable DeepSeek V4-Flash for function calls and GLM for vision. Alibaba's Token Plan provides strong reasoning via Qwen. Instead of picking one provider, the router picks the best provider *per request*.

## Architecture

```
Client (Hermes / Open WebUI / OpenAI SDK)
          │
          ▼
┌─────────────────────────────────────┐
│   DeepSeek Hybrid Router (:8001)    │
│                                     │
│  ┌───────────┐   ┌───────────────┐  │
│  │  Request   │──▶│   Routing     │  │
│  │  Analyzer  │   │   Engine      │  │
│  └───────────┘   └───────┬───────┘  │
│                          │          │
│               ┌──────────┼────────┐ │
│               ▼          ▼        ▼ │
│         ┌──────────┐ ┌───────┐ ┌────┐
│         │ Fallback │ │Health │ │Cost│ │
│         │ Manager  │ │ Track │ │Track│ │
│         └──────────┘ └───────┘ └────┘ │
└──────────┬──────────┬──────────┬──────┘
           │          │          │
     ┌─────▼───┐ ┌────▼────┐ ┌──▼──────┐
     │DeepSeek │ │DeepInfra│ │ Alibaba │
     │ Bridge  │ │  API    │ │DashScope│
     │ :8000   │ │         │ │         │
     └─────────┘ └─────────┘ └─────────┘
```

## Request Classification

The router analyzes each request and assigns one of five types:

| Type | Detection |
|------|-----------|
| `tool_calling` | Request has `tools`/`functions` definitions, `tool` role messages, or `tool_calls` |
| `vision` | Message content contains `image_url` or `image` blocks |
| `reasoning` | Heuristic keyword matching (explain why, prove, derive, architecture, etc.) + message length |
| `search` | Keywords like "latest", "current", "news", "search for" |
| `chat` | Default — no specialized signal detected |

## Supported Providers & Models

### DeepSeek Bridge (`deepseek-bridge`)
Free access via a reverse-engineered chat.deepseek.com bridge. Fragile but free.
- `v4-instant` → `deepseek-chat` — fast chat, search, vision
- `expert` → `deepseek-reasoner` — deep reasoning

### DeepInfra (`deepinfra`)
Stable paid API with good tool-calling support.
- `V4-Flash-0731` → `deepseek-ai/DeepSeek-V4-Flash-0731`
- `V4.1-Flash` → `deepseek-ai/DeepSeek-V4.1-Flash`
- `GLM-5.3-Flash` → `zai-org/GLM-5.3-Flash` — vision fallback

### Alibaba DashScope (`alibaba`)
Token-plan API with strong reasoning models.
- `qwen3.7-max` — complex reasoning, 128K context
- `qwen3.8-flash` — fast lightweight responses

## Default Routing Table

| Request Type | Primary | Fallbacks |
|-------------|---------|-----------|
| `tool_calling` | `deepinfra:V4-Flash-0731` | `alibaba:qwen-max`, `deepseek-bridge:v4-instant` |
| `vision` | `deepseek-bridge:v4-instant` | `deepinfra:GLM-5.3-Flash` |
| `reasoning` | `deepseek-bridge:expert` | `alibaba:qwen-max`, `deepinfra:V4-Flash-0731` |
| `search` | `deepseek-bridge:v4-instant` | — |
| `chat` | `deepseek-bridge:v4-instant` | `deepinfra:V4-Flash-0731`, `alibaba:qwen-max` |

## Quick Start

### 1. Clone and install

```bash
git clone <repo-url> ~/dev/deepseek-hybrid-router
cd ~/dev/deepseek-hybrid-router
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Configure API keys

```bash
cp .env.example .env
# Edit .env with your keys:
# DEEPINFRA_API_KEY=your-key-here
# ALIBABA_API_KEY=your-key-here
```

### 3. Start the bridge (optional, for free DeepSeek access)

The DeepSeek-API bridge must be running separately at `http://localhost:8000`. See the [DeepSeek-API fork documentation](https://github.com/sums001/Deepseek-API) for setup.

### 4. Start the router

```bash
# Direct
source .venv/bin/activate
uvicorn router.main:app --host 0.0.0.0 --port 8001

# Or with Docker
docker compose up -d
```

### 5. Point your client at the router

```bash
# OpenAI SDK / Hermes / Open WebUI
export OPENAI_BASE_URL=http://localhost:8001/v1
export OPENAI_API_KEY=not-needed  # router handles auth to providers
```

### 6. Verify

```bash
curl http://localhost:8001/healthz
# → {"status": "healthy", "timestamp": ...}

curl http://localhost:8001/v1/routing/info
# → {"routing_table": {"tool_calling": {...}, ...}}
```

## Documentation

- [Setup Guide](setup.md) — detailed installation and configuration
- [API Reference](api.md) — all endpoints with request/response schemas
- [Configuration Guide](configuration.md) — routing.yaml, confirmation.yaml, pricing.json, environment variables
- [Troubleshooting](troubleshooting.md) — common errors and how to fix them

## Project Structure

```
deepseek-hybrid-router/
├── config/
│   └── routing.yaml          # Routing rules and provider config
├── router/
│   ├── main.py               # FastAPI app and endpoints
│   ├── analyzer.py           # Request classification engine
│   ├── routing.py            # Routing logic and provider selection
│   ├── fallback.py           # Fallback chain and health tracking
│   ├── models.py             # Pydantic request/response models
│   ├── adapters/
│   │   ├── bridge.py         # DeepSeek-API bridge adapter
│   │   ├── deepinfra.py      # DeepInfra adapter
│   │   └── alibaba.py        # Alibaba DashScope adapter
│   └── confirmation/
│       ├── cli.py            # Terminal-based confirmation
│       ├── owui.py           # Open WebUI confirmation
│       └── config.py         # Config-based auto-approval
├── tests/
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
└── requirements.txt
```

## License

MIT
