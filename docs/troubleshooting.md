# Troubleshooting

Common errors and solutions for the DeepSeek Hybrid Router.

## Quick Diagnostic Checklist

Before diving into specific errors, run through these checks:

```bash
# 1. Is the router running?
curl http://localhost:8001/healthz

# 2. What's the current routing table?
curl http://localhost:8001/v1/routing/info

# 3. Are API keys set?
echo $DEEPINFRA_API_KEY
echo $ALIBABA_API_KEY

# 4. Is the bridge running (if using deepseek-bridge)?
curl http://localhost:8000/healthz

# 5. Check router logs
docker compose logs router          # Docker
tail -f /var/log/router.log         # Systemd
```

## Router Startup Issues

### Port Already in Use

```
OSError: [Errno 98] Address already in use
```

**Cause:** Another process is using port 8001.

**Solution:**

```bash
# Find what's using the port
lsof -i :8001
# or
ss -tlnp | grep 8001

# Kill it
kill <PID>

# Or change the router port
export ROUTER_PORT=8002
uvicorn router.main:app --host 0.0.0.0 --port 8002
```

### Module Not Found

```
ModuleNotFoundError: No module named 'router'
```

**Cause:** Not running from project directory or venv not activated.

**Solution:**

```bash
cd ~/dev/deepseek-hybrid-router
source .venv/bin/activate
pip install -e .
uvicorn router.main:app --host 0.0.0.0 --port 8001
```

### Missing Dependencies

```
ImportError: cannot import name 'FastAPI' from 'fastapi'
```

**Solution:**

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### Routing Config Not Found

```
FileNotFoundError: Routing config not found: /app/config/routing.yaml
```

**Cause:** The `config/routing.yaml` file is missing or at wrong path.

**Solution:**

```bash
ls -la config/routing.yaml
# If missing, create it from the example in this docs

# If using Docker, ensure config/ is copied into the image
docker compose build --no-cache
docker compose up -d
```

---

## Bridge Connection Issues

### Connection Refused

```
BridgeConnectionError: Cannot reach bridge at http://localhost:8000
```

**Cause:** The DeepSeek-API bridge is not running.

**Solution:**

```bash
# Check if bridge is running
curl http://localhost:8000/healthz

# Start the bridge
cd ~/dev/deepseek-api-fork
python app.py

# If running in Docker, the bridge is on the host network
# Update BRIDGE_URL to use host.docker.internal or the host IP
export BRIDGE_URL=http://host.docker.internal:8000
```

### Bridge Timeout

```
BridgeTimeoutError: Bridge timed out: ...
```

**Cause:** DeepSeek's web API is slow or the bridge is overloaded.

**Solution:**

1. **Increase timeout** in code or config
2. **Wait and retry** — the fallback chain handles this automatically
3. **Check bridge logs** for specific errors
4. The router will fall back to DeepInfra or Alibaba after timeout

### Bridge Returns 401/403

```
BridgeAPIError: Bridge returned 401: Unauthorized
```

**Cause:** Bridge session expired or credentials invalid.

**Solution:**

```bash
# Re-login to the bridge
cd ~/dev/deepseek-api-fork
# Follow the bridge's re-authentication process

# The router will skip the bridge on auth errors and use fallbacks
```

### Bridge Session Expired

**Symptoms:** Requests that worked before suddenly fail with bridge errors.

**Cause:** DeepSeek web sessions expire (typically after 24-48 hours).

**Solution:**

- The router automatically falls back to other providers
- Re-authenticate the bridge when convenient
- Multi-account support (planned) rotates sessions automatically

---

## Provider API Errors

### DeepInfra: Authentication Failed

```
PermissionError: DeepInfra authentication failed (401). Check DEEPINFRA_API_KEY.
```

**Solution:**

```bash
# Verify key is set
echo $DEEPINFRA_API_KEY

# Get a new key from https://deepinfra.com/dashboard

# Update .env
DEEPINFRA_API_KEY=sk-...

# Restart router (env vars don't hot-reload)
docker compose restart router
```

### DeepInfra: Rate Limit (429)

```
RuntimeError: DeepInfra rate limit exceeded (429). Retry-After: ...
```

**Cause:** Exceeded DeepInfra's rate limit for your account.

**Solution:**

- **Wait** — the router automatically retries after the backoff period
- **Check your usage** at https://deepinfra.com/dashboard
- **Upgrade your plan** if you hit limits frequently
- The fallback chain will try Alibaba or Bridge while DeepInfra is rate-limited

### DeepInfra: Service Unavailable (503)

```
RuntimeError: DeepInfra service unavailable (503).
```

**Solution:** Wait and retry. The fallback chain handles this automatically.

### Alibaba: Authentication Failed

```
AlibabaAuthError: DashScope auth failed (401): ...
```

**Solution:**

```bash
# Verify key
echo $ALIBABA_API_KEY

# Get a new key from Alibaba Cloud DashScope console

# Update .env and restart
```

### Alibaba: Model Not Allowed

```
ValueError: Model 'qwen-turbo' is not allowed. Only qwen3.7-* and qwen3.8-flash models are permitted.
```

**Cause:** The adapter restricts models to `qwen3.7-*` and `qwen3.8-flash` patterns.

**Solution:**

- Use an allowed model name in `routing.yaml`:
  ```yaml
  reasoning:
    primary: alibaba:qwen3.7-max  # ✓ allowed
  ```
- To allow other models, modify `_ALLOWED_MODEL_RE` in `router/adapters/alibaba.py`

### All Providers Failed

```
RuntimeError: All providers in fallback chain failed: BridgeAdapter: ...; DeepInfraAdapter: ...; AlibabaAdapter: ...
```

**Cause:** Every provider in the chain failed for this request type.

**Diagnosis:**

```bash
# Check routing for the failing request type
curl http://localhost:8001/v1/routing/info | jq '.routing_table.<type>'

# Test each provider individually
curl http://localhost:8000/healthz                    # Bridge
curl -H "Authorization: Bearer $DEEPINFRA_API_KEY" \
  https://api.deepinfra.com/v1/openai/models          # DeepInfra
```

**Solutions:**

- Fix individual provider issues (see above)
- Add more fallbacks to `routing.yaml`
- Restart the router to clear unhealthy provider state

---

## Rate Limiting

### Frequent 429 Errors

**Symptoms:** Many requests fail with rate limit errors.

**Solutions:**

1. **Add more fallbacks** in `routing.yaml`:
   ```yaml
   chat:
     primary: deepseek-bridge:v4-instant
     fallbacks:
       - deepinfra:V4-Flash-0731
       - alibaba:qwen3.8-flash
       - deepinfra:V4.1-Flash
   ```

2. **Distribute load** across request types — the router's classifier helps by sending different request types to different providers

3. **Multi-account support** (planned) will rotate DeepSeek bridge accounts

4. **Upgrade provider plans** if you consistently hit limits

### Provider Marked Unhealthy

**Symptoms:** Logs show "Skipping unhealthy provider".

```
INFO: Skipping unhealthy provider BridgeAdapter (recovery in 25.5s)
```

**Cause:** A provider failed multiple times and was temporarily removed from the chain (30-second cooldown).

**This is normal behavior.** The provider auto-recovers after the cooldown. To check status programmatically (future API endpoint).

---

## Request Classification Issues

### Wrong Request Type Detected

**Symptoms:** A request routes to the wrong provider.

**Diagnosis:**

```bash
# Check what the router sees
curl http://localhost:8001/v1/routing/info
```

Check the router logs for the routing decision:

```
INFO: Routing decision: type=chat confidence=1.000 reasoning=No specialized signal detected...
```

**Common misclassifications:**

| Actual Intent | Detected As | Why | Fix |
|--------------|-------------|-----|-----|
| Complex reasoning | `chat` | Not enough keywords matched | Add more keywords to your prompt or lower `REASONING_THRESHOLD` |
| Tool calling | `chat` | `tools` field missing from request | Ensure client sends `tools` in the request body |
| Web search | `chat` | No search keywords detected | Include words like "latest", "current", "search for" |

**Customize detection:**

Edit `router/analyzer.py` to adjust keyword lists:

```python
REASONING_KEYWORDS = [
    "explain why",
    "prove",
    # Add your own:
    "debug this code",
    "refactor",
]
```

Then reload:

```bash
curl -X POST http://localhost:8001/v1/routing/reload
```

### Reasoning Never Detected

**Cause:** The reasoning threshold (default: 3) may be too high for your use case.

**Solution:**

```python
from router.analyzer import RequestAnalyzer

# Lower threshold for more aggressive reasoning detection
analyzer = RequestAnalyzer(reasoning_threshold=2)
```

Or adjust the threshold in the Router initialization.

---

## Session Expiry

### Bridge Sessions Expire Frequently

**Symptoms:** Bridge works for a while, then starts failing.

**Cause:** DeepSeek web sessions have a limited lifetime (typically 24-48 hours).

**Solutions:**

- **Rely on fallbacks** — the router falls back to DeepInfra/Alibaba when bridge fails
- **Session persistence** (planned) will proactively refresh sessions before expiry
- **Multi-account** (planned) rotates between accounts to spread session usage

---

## Docker Issues

### Container Can't Reach Bridge

**Symptoms:** Bridge works on host but fails inside Docker.

**Cause:** `localhost` inside Docker refers to the container, not the host.

**Solution:**

```yaml
# docker-compose.yml
services:
  router:
    environment:
      - BRIDGE_URL=http://host.docker.internal:8000
```

Or use the host's IP:

```bash
# Find host IP
ip addr show docker0

# Use that IP
BRIDGE_URL=http://172.17.0.1:8000
```

### Redis Connection Failed

**Symptoms:** Router starts but session persistence fails.

**Cause:** Redis not running or wrong URL.

**Solution:**

```bash
# Start Redis via docker compose
docker compose up -d redis

# Or disable Redis by removing REDIS_URL from .env
```

### Out of Memory

**Symptoms:** Container killed by OOM killer.

**Expected memory usage:**
- Router alone: ~200-300 MB
- With bridge in same container: ~650 MB - 1 GB

**Solution:**

```yaml
# docker-compose.yml
services:
  router:
    deploy:
      resources:
        limits:
          memory: 1G
```

### Image Build Fails

```
ERROR: failed to solve: failed to compute cache key
```

**Solution:**

```bash
# Clean build
docker compose build --no-cache

# Or prune Docker cache
docker system prune -a
```

---

## Configuration Issues

### YAML Syntax Error

```
yaml.scanner.ScannerError: mapping values are not allowed here
```

**Cause:** Indentation or syntax error in `routing.yaml`.

**Solution:**

```bash
# Validate YAML
python -c "import yaml; yaml.safe_load(open('config/routing.yaml'))"
```

Common mistakes:
- Tabs instead of spaces (YAML requires spaces)
- Missing colon after key
- Incorrect indentation (use 2 spaces)

### Unknown Provider

```
UnknownProviderError: Unknown provider 'openrouter'. Known providers: ['deepseek-bridge', 'deepinfra', 'alibaba']
```

**Cause:** `routing.yaml` references a provider that has no adapter.

**Solution:**

- Use only supported providers: `deepseek-bridge`, `deepinfra`, `alibaba`
- To add a new provider, create an adapter in `router/adapters/` and register it in `router/routing.py`

### Hot-Reload Not Taking Effect

**Symptoms:** `POST /v1/routing/reload` succeeds but old rules still apply.

**Diagnosis:**

```bash
# Check the reload response includes the correct path
curl -X POST http://localhost:8001/v1/routing/reload
# {"status": "reloaded", "config_path": "/app/config/routing.yaml"}

# Verify the file was actually edited
cat config/routing.yaml

# Check current routing
curl http://localhost:8001/v1/routing/info
```

**Note:** Environment variables (`DEEPINFRA_API_KEY`, etc.) require a restart — they don't hot-reload.

---

## Streaming Issues

### Stream Cuts Off Mid-Response

**Symptoms:** Streaming response stops before completion.

**Possible causes:**

1. **Bridge timeout** — increase `timeout` in BridgeConfig
2. **Provider disconnected** — the fallback chain should handle this
3. **Client timeout** — increase timeout on the client side

**Check logs:**

```
WARNING: Provider BridgeAdapter failed (attempt 1/2, category=timeout)
```

### Streaming Not Working

**Symptoms:** Request with `stream: true` returns non-streaming response.

**Diagnosis:**

```bash
# Test streaming directly
curl http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"any","messages":[{"role":"user","content":"hi"}],"stream":true}' \
  --no-buffer
```

Should see `data: {...}` SSE format.

---

## Performance Issues

### High Latency

**Symptoms:** Requests take >10 seconds.

**Diagnosis:**

```bash
# Time a request
time curl http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"any","messages":[{"role":"user","content":"hi"}]}'
```

**Solutions:**

1. **Check which provider is being used** — bridge is often slower than paid APIs
2. **Adjust timeouts** if providers are slow but reliable
3. **Reorder routing** to prefer faster providers:
   ```yaml
   chat:
     primary: alibaba:qwen3.8-flash  # Fast
     fallbacks:
       - deepinfra:V4-Flash-0731
       - deepseek-bridge:v4-instant  # Slowest
   ```

### High Memory Usage

**Solutions:**

- Reduce adapter cache (clear periodically)
- Limit concurrent requests
- Run without the bridge in the same container

---

## Log Analysis

### Enable Debug Logging

Add to your startup:

```bash
export LOG_LEVEL=DEBUG
uvicorn router.main:app --host 0.0.0.0 --port 8001 --log-level debug
```

### Key Log Messages

| Log Message | Meaning |
|------------|---------|
| `Routing decision: type=X` | Request classified as type X |
| `Selected primary provider: Y` | Primary provider chosen |
| `Provider Z failed (attempt N/M)` | Provider failure, retrying |
| `Fallback: switching from X to Y` | Fallback triggered |
| `Skipping unhealthy provider` | Provider in cooldown |
| `All providers in fallback chain failed` | Total failure |
| `Provider X auto-recovered after cooldown` | Provider back online |

---

## Still Stuck?

1. **Check the logs** — most issues have descriptive error messages
2. **Test providers individually** — bypass the router to isolate the problem
3. **Verify routing config** — `curl http://localhost:8001/v1/routing/info`
4. **Restart the router** — clears unhealthy provider state
5. **Check this guide** — most common issues are documented above
