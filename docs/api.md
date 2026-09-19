# API Reference

Complete API documentation for the DeepSeek Hybrid Router.

## Base URL

```
http://localhost:8001
```

## Endpoints

### Chat Completions

#### `POST /v1/chat/completions`

OpenAI-compatible chat completions endpoint. The router analyzes the request and routes it to the appropriate provider.

**Request:**

```json
{
  "model": "any",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello!"}
  ],
  "temperature": 1.0,
  "max_tokens": null,
  "stream": false,
  "tools": null,
  "functions": null
}
```

**Fields:**
- `model` (string, required): Model name. Use `"any"` to let the router pick the best model, or specify a provider:model format like `"deepinfra:V4-Flash-0731"`.
- `messages` (array, required): List of message objects with `role` and `content`.
- `temperature` (float, optional): Sampling temperature. Default: `1.0`.
- `max_tokens` (int, optional): Maximum tokens to generate. Default: `null` (provider default).
- `stream` (bool, optional): Enable streaming. Default: `false`.
- `tools` (array, optional): Tool/function definitions for function calling.
- `functions` (array, optional): Legacy function definitions (deprecated, use `tools`).

**Response (non-streaming):**

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1234567890,
  "model": "deepseek-ai/DeepSeek-V4-Flash-0731",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you today?"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 15,
    "total_tokens": 25
  }
}
```

**Response (streaming):**

Server-Sent Events (SSE) stream:

```
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"deepseek-ai/DeepSeek-V4-Flash-0731","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"deepseek-ai/DeepSeek-V4-Flash-0731","choices":[{"index":0,"delta":{"content":"!"},"finish_reason":null}]}

data: [DONE]
```

**Examples:**

```bash
# Basic chat
curl http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "any",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# Tool calling
curl http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "any",
    "messages": [{"role": "user", "content": "What is the weather?"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Get weather",
        "parameters": {
          "type": "object",
          "properties": {
            "location": {"type": "string"}
          }
        }
      }
    }]
  }'

# Streaming
curl http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "any",
    "messages": [{"role": "user", "content": "Tell me a story"}],
    "stream": true
  }'
```

---

### Models

#### `GET /v1/models`

List available models. Currently returns a static list of configured models.

**Response:**

```json
{
  "object": "list",
  "data": [
    {
      "id": "any",
      "object": "model",
      "created": 1234567890,
      "owned_by": "deepseek-hybrid-router"
    },
    {
      "id": "deepinfra:V4-Flash-0731",
      "object": "model",
      "created": 1234567890,
      "owned_by": "deepinfra"
    },
    {
      "id": "deepseek-bridge:v4-instant",
      "object": "model",
      "created": 1234567890,
      "owned_by": "deepseek-bridge"
    }
  ]
}
```

**Example:**

```bash
curl http://localhost:8001/v1/models
```

---

### Health Check

#### `GET /healthz`

Simple health check endpoint.

**Response:**

```json
{
  "status": "healthy",
  "timestamp": 1234567890
}
```

**Example:**

```bash
curl http://localhost:8001/healthz
```

---

### Routing Information

#### `GET /v1/routing/info`

Get the current routing table configuration. Useful for debugging and verification.

**Response:**

```json
{
  "routing_table": {
    "tool_calling": {
      "request_type": "tool_calling",
      "primary": "deepinfra:V4-Flash-0731",
      "fallbacks": [
        "alibaba:qwen-max",
        "deepseek-bridge:v4-instant"
      ],
      "description": "Requests with function/tool calls"
    },
    "vision": {
      "request_type": "vision",
      "primary": "deepseek-bridge:v4-instant",
      "fallbacks": ["deepinfra:GLM-5.3-Flash"],
      "description": "Requests with image content"
    },
    "reasoning": {
      "request_type": "reasoning",
      "primary": "deepseek-bridge:expert",
      "fallbacks": [
        "alibaba:qwen-max",
        "deepinfra:V4-Flash-0731"
      ],
      "description": "Complex reasoning, math, logic tasks"
    },
    "search": {
      "request_type": "search",
      "primary": "deepseek-bridge:v4-instant",
      "fallbacks": [],
      "description": "Web search and information retrieval"
    },
    "chat": {
      "request_type": "chat",
      "primary": "deepseek-bridge:v4-instant",
      "fallbacks": [
        "deepinfra:V4-Flash-0731",
        "alibaba:qwen-max"
      ],
      "description": "General conversation and simple queries"
    }
  }
}
```

**Example:**

```bash
curl http://localhost:8001/v1/routing/info
```

---

### Reload Configuration

#### `POST /v1/routing/reload`

Hot-reload the routing configuration from `config/routing.yaml` without restarting the server.

**Response:**

```json
{
  "status": "reloaded",
  "config_path": "/app/config/routing.yaml"
}
```

**Example:**

```bash
curl -X POST http://localhost:8001/v1/routing/reload
```

**Use Cases:**
- Update routing rules without downtime
- Add/remove providers dynamically
- Change fallback chains on the fly

---

### Cost Tracking (Planned)

#### `GET /v1/cost/session/{session_id}`

Get cost breakdown for a specific session.

**Response:**

```json
{
  "session_id": "abc123",
  "total_cost": 0.0042,
  "currency": "USD",
  "requests": 15,
  "tokens": {
    "prompt": 1200,
    "completion": 800,
    "total": 2000
  },
  "providers": {
    "deepinfra": {"cost": 0.003, "requests": 10},
    "alibaba": {"cost": 0.0012, "requests": 5}
  }
}
```

**Example:**

```bash
curl http://localhost:8001/v1/cost/session/abc123
```

---

#### `GET /v1/cost/total`

Get total cost across all sessions.

**Response:**

```json
{
  "total_cost": 0.1234,
  "currency": "USD",
  "sessions": 50,
  "requests": 1500,
  "tokens": {
    "prompt": 120000,
    "completion": 80000,
    "total": 200000
  }
}
```

**Example:**

```bash
curl http://localhost:8001/v1/cost/total
```

---

### Account Management (Planned)

#### `GET /v1/accounts`

List configured DeepSeek bridge accounts and their status.

**Response:**

```json
{
  "accounts": [
    {
      "id": "account1",
      "email": "user1@example.com",
      "status": "active",
      "requests_today": 45,
      "rate_limit_remaining": 55
    },
    {
      "id": "account2",
      "email": "user2@example.com",
      "status": "rate_limited",
      "requests_today": 100,
      "rate_limit_remaining": 0
    }
  ]
}
```

**Example:**

```bash
curl http://localhost:8001/v1/accounts
```

---

### Session Management (Planned)

#### `GET /v1/sessions`

List active sessions with their metadata.

**Response:**

```json
{
  "sessions": [
    {
      "id": "session123",
      "created": "2024-01-15T10:30:00Z",
      "last_activity": "2024-01-15T11:45:00Z",
      "requests": 25,
      "cost": 0.0042,
      "bridge_conversation_id": "conv_abc123"
    }
  ]
}
```

**Example:**

```bash
curl http://localhost:8001/v1/sessions
```

---

## Error Responses

### Provider Unavailable

When all providers in the fallback chain fail:

```json
{
  "error": {
    "message": "All providers in fallback chain failed: BridgeAdapter: Connection refused; DeepInfraAdapter: 429 Rate limit exceeded",
    "type": "provider_unavailable",
    "code": "all_providers_failed"
  }
}
```

**HTTP Status:** `503 Service Unavailable`

### Invalid Request

Malformed request body:

```json
{
  "detail": [
    {
      "loc": ["body", "messages"],
      "msg": "field required",
      "type": "value_error.missing"
    }
  ]
}
```

**HTTP Status:** `422 Unprocessable Entity`

### Authentication Error

Invalid API key for a provider:

```json
{
  "error": {
    "message": "DeepInfra authentication failed (401). Check DEEPINFRA_API_KEY.",
    "type": "auth_error",
    "code": "invalid_api_key"
  }
}
```

**HTTP Status:** `401 Unauthorized`

---

## Request Classification Examples

The router automatically classifies requests based on their content:

### Tool Calling

```json
{
  "model": "any",
  "messages": [{"role": "user", "content": "What's the weather?"}],
  "tools": [{
    "type": "function",
    "function": {"name": "get_weather", ...}
  }]
}
```
→ Routes to `deepinfra:V4-Flash-0731`

### Vision

```json
{
  "model": "any",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "What's in this image?"},
      {"type": "image_url", "image_url": {"url": "https://..."}}
    ]
  }]
}
```
→ Routes to `deepseek-bridge:v4-instant`

### Reasoning

```json
{
  "model": "any",
  "messages": [{
    "role": "user",
    "content": "Explain why quantum entanglement violates local realism and derive the Bell inequality step by step."
  }]
}
```
→ Routes to `deepseek-bridge:expert` (keyword matching: "explain why", "derive")

### Search

```json
{
  "model": "any",
  "messages": [{
    "role": "user",
    "content": "What's the latest news about AI regulation?"
  }]
}
```
→ Routes to `deepseek-bridge:v4-instant` (keyword: "latest")

### Chat

```json
{
  "model": "any",
  "messages": [{"role": "user", "content": "Hello, how are you?"}]
}
```
→ Routes to `deepseek-bridge:v4-instant` (default)

---

## Rate Limiting

The router itself doesn't enforce rate limits, but individual providers do:

- **DeepInfra**: Check your account limits at https://deepinfra.com
- **Alibaba**: Token plan limits apply
- **DeepSeek Bridge**: Subject to chat.deepseek.com rate limits

When a provider returns `429 Too Many Requests`, the router automatically tries the next fallback.

---

## WebSocket Support (Planned)

Future versions may support WebSocket for real-time streaming and Open WebUI confirmation dialogs.

---

## Authentication

The router doesn't require authentication for local use. If you expose it publicly, add your own auth layer (API key, OAuth, etc.) in front of the router.

Providers are authenticated using the API keys in your `.env` file.
