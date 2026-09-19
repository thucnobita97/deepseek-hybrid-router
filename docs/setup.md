# Setup Guide

Complete setup instructions for the DeepSeek Hybrid Router.

## Prerequisites

- **Python 3.11+** (3.12 recommended)
- **Docker & Docker Compose** (optional, for containerized deployment)
- **Redis** (optional, for session persistence — included in docker-compose)
- **DeepSeek-API Bridge** (optional, running at `http://localhost:8000`)

## Installation

### Option 1: Local Python Installation

```bash
# Clone the repository
git clone <repo-url> ~/dev/deepseek-hybrid-router
cd ~/dev/deepseek-hybrid-router

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install in editable mode
pip install -e .

# Or install from requirements.txt
pip install -r requirements.txt
```

### Option 2: Docker

```bash
# Clone the repository
git clone <repo-url> ~/dev/deepseek-hybrid-router
cd ~/dev/deepseek-hybrid-router

# Build and start with Docker Compose
docker compose up -d

# Or build manually
docker build -t deepseek-hybrid-router .
docker run -d -p 8001:8001 \
  -e DEEPINFRA_API_KEY=your-key \
  -e ALIBABA_API_KEY=your-key \
  deepseek-hybrid-router
```

## Configuration

### 1. Environment Variables

Copy the example `.env` file and fill in your API keys:

```bash
cp .env.example .env
```

Edit `.env`:

```bash
# Required for DeepInfra provider
DEEPINFRA_API_KEY=your-deepinfra-api-key

# Required for Alibaba provider
ALIBABA_API_KEY=your-alibaba-api-key

# DeepSeek bridge URL (default: http://localhost:8000)
BRIDGE_URL=http://localhost:8000

# Router port (default: 8001)
ROUTER_PORT=8001

# Confirmation mode: config, cli, or owui
CONFIRMATION_MODE=config

# Redis URL for session persistence (optional)
REDIS_URL=redis://localhost:6379/0
```

**Getting API Keys:**
- **DeepInfra**: Sign up at https://deepinfra.com and create an API key
- **Alibaba**: Sign up for Alibaba Cloud DashScope and get an API key
- **DeepSeek Bridge**: Free, no API key needed (requires running the bridge separately)

### 2. Routing Configuration

Edit `config/routing.yaml` to customize routing rules:

```yaml
routing_rules:
  tool_calling:
    primary: deepinfra:V4-Flash-0731
    fallbacks:
      - alibaba:qwen-max
      - deepseek-bridge:v4-instant
    description: "Requests with function/tool calls"
    
  vision:
    primary: deepseek-bridge:v4-instant
    fallback: deepinfra:GLM-5.3-Flash
    description: "Requests with image content"
    
  reasoning:
    primary: deepseek-bridge:expert
    fallbacks:
      - alibaba:qwen-max
      - deepinfra:V4-Flash-0731
    description: "Complex reasoning, math, logic tasks"
    
  search:
    primary: deepseek-bridge:v4-instant
    description: "Web search and information retrieval"
    
  chat:
    primary: deepseek-bridge:v4-instant
    fallbacks:
      - deepinfra:V4-Flash-0731
      - alibaba:qwen-max
    description: "General conversation and simple queries"

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

**Key Points:**
- `primary`: The first provider to try for this request type
- `fallbacks`: Ordered list of backup providers if primary fails
- Format: `provider:model` (e.g., `deepinfra:V4-Flash-0731`)
- Provider names must match keys in the `providers` section

See [Configuration Guide](configuration.md) for advanced options.

### 3. DeepSeek Bridge Setup (Optional)

If you want to use the free DeepSeek web-chat bridge:

```bash
# Clone the DeepSeek-API fork
git clone <fork-url> ~/dev/deepseek-api-fork
cd ~/dev/deepseek-api-fork

# Follow the fork's README to:
# 1. Install dependencies
# 2. Configure DeepSeek web credentials
# 3. Start the bridge at localhost:8000
python app.py
```

The bridge must be running before starting the router if you plan to use `deepseek-bridge` routes.

## Starting the Router

### Local Mode

```bash
cd ~/dev/deepseek-hybrid-router
source .venv/bin/activate

# Load environment variables
export $(cat .env | xargs)

# Start with uvicorn
uvicorn router.main:app --host 0.0.0.0 --port 8001 --reload

# Or run directly
python -m router.main
```

### Docker Mode

```bash
cd ~/dev/deepseek-hybrid-router

# Start router + Redis
docker compose up -d

# View logs
docker compose logs -f router

# Stop
docker compose down
```

## Verifying It Works

### 1. Health Check

```bash
curl http://localhost:8001/healthz
```

Expected response:
```json
{"status": "healthy", "timestamp": 1234567890}
```

### 2. Routing Info

```bash
curl http://localhost:8001/v1/routing/info
```

Expected response:
```json
{
  "routing_table": {
    "tool_calling": {
      "request_type": "tool_calling",
      "primary": "deepinfra:V4-Flash-0731",
      "fallbacks": ["alibaba:qwen-max", "deepseek-bridge:v4-instant"],
      "description": "Requests with function/tool calls"
    },
    ...
  }
}
```

### 3. Test Chat Completion

```bash
curl http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "any",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": false
  }'
```

Expected: A valid OpenAI-format chat completion response.

### 4. Test with OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8001/v1",
    api_key="not-needed"
)

response = client.chat.completions.create(
    model="any",
    messages=[{"role": "user", "content": "What is 2+2?"}]
)

print(response.choices[0].message.content)
```

### 5. Test Tool Calling

```bash
curl http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "any",
    "messages": [{"role": "user", "content": "What is the weather in Tokyo?"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Get current weather",
        "parameters": {
          "type": "object",
          "properties": {
            "location": {"type": "string"}
          },
          "required": ["location"]
        }
      }
    }]
  }'
```

Expected: Response routed to `deepinfra:V4-Flash-0731` (tool_calling route).

## Integration with Clients

### Hermes CLI

In your Hermes config (`~/.hermes/config.yaml`):

```yaml
providers:
  openai:
    base_url: http://localhost:8001/v1
    api_key: not-needed
    model: any  # router picks the best model
```

### Open WebUI

In Open WebUI Admin Panel → Settings → Connections:

- **OpenAI API Base URL**: `http://localhost:8001/v1`
- **OpenAI API Key**: `not-needed`
- **Model**: `any` (or leave blank)

### Any OpenAI-Compatible Client

```bash
export OPENAI_BASE_URL=http://localhost:8001/v1
export OPENAI_API_KEY=not-needed
```

Then use your client as normal — the router handles provider selection.

## Next Steps

- Read the [API Reference](api.md) for all available endpoints
- Customize routing in [Configuration Guide](configuration.md)
- Check [Troubleshooting](troubleshooting.md) if something goes wrong
