# DeepSeek Hybrid Router - Implementation Plan

## Goal

Build a smart hybrid router that routes requests to the best provider based on request type (tool calling, vision, reasoning, search, chat), with fallback chains and user confirmation. This enables using DeepSeek's free web chat API (via reverse-engineered bridge) alongside paid/stable providers like DeepInfra and Alibaba Token Plan.

## Architecture

```
Hermes CLI / Open WebUI
         ↓
Smart Router (FastAPI, localhost:8001, Docker)
         ↓
    ┌────┴────────────────┬──────────────┬─────────────┐
    ↓                     ↓              ↓             ↓
DeepSeek-API        DeepInfra       Alibaba      (future:
Bridge (fork)       V4-Flash-0731   Token Plan    OpenRouter)
                    V4.1-Flash                   (paid fallback)
                    GLM-5.3-Flash
```

**Components:**
- **DeepSeek-API Bridge** (forked from `sums001/Deepseek-API`): Free access to DeepSeek V4 Instant, Expert, and web search. Reverse-engineered from chat.deepseek.com. Fragile but free.
- **DeepInfra**: Stable API with DeepSeek V4-Flash-0731, V4.1-Flash, GLM-5.3-Flash. Good for tool calling and vision.
- **Alibaba Token Plan**: Stable API with Qwen3.7-max, Qwen3.8-flash. Good for reasoning and fast responses.
- **Smart Router**: FastAPI proxy that analyzes requests and routes to best provider with fallback.

## Model/Provider Mapping

### Tool Calling
```
Primary:   deepinfra:DeepSeek-V4-Flash-0731
Fallback:  deepinfra:DeepSeek-V4.1-Flash
Fallback:  alibaba:qwen3.7-max
```

### Vision
```
Primary:   deepseek-bridge:v4-instant (with vision upload, reverse-engineered)
Fallback:  deepinfra:GLM-5.3-Flash
```
**Note:** `V4-Flash-Vision-Exp` remains disabled per existing memory rule.

### Reasoning (Expert)
```
Primary:   deepseek-bridge:expert
Fallback:  alibaba:qwen3.7-max
Fallback:  deepinfra:DeepSeek-V4-Flash-0731
```

### Web Search
```
Primary:   deepseek-bridge:v4-instant (built-in search)
```

### Fast Chat
```
Primary:   deepseek-bridge:v4-instant
Fallback:  deepinfra:DeepSeek-V4-Flash-0731
Fallback:  alibaba:qwen3.8-flash
```

## Model Allocation Strategy

| Task Type | Model | Reason |
|-----------|-------|--------|
| **Complex reasoning** (architecture, reverse-engineering) | `alibaba:qwen3.7-max` | Strong reasoning, 128K context |
| **Medium code generation** (features, adapters) | `deepinfra:V4-Flash-0731` | Fast, good code quality |
| **Simple code** (boilerplate, docs, tests) | `alibaba:qwen3.8-flash` or `V4.1-Flash` | Fastest, cheapest |
| **Manual tasks** (setup, testing, browser work) | No model | Human work |

## Phases

### Phase 1: DeepSeek-API Bridge Fork & Fixes
**Goal:** Fork repo, fix thinking extraction + message structure, test basic chat

**Tasks:**

1.1 **Fork & setup local repo** (Manual, 30min)
- Fork `sums001/Deepseek-API` → `~/dev/deepseek-api-fork/`
- Setup Python venv, install dependencies
- Test basic chat flow
- **Verification:** `python app.py` runs, test 1 request succeeds

1.2 **Fix thinking extraction** (Model: `deepinfra:V4-Flash-0731`, Subagent: Yes, Parallel with 1.3, 1-2h)
- Modify `deepseek/client.py` `_parse_sse()` to extract THINKING fragments
- Add `reasoning_content` field to response
- **File:** `deepseek/client.py` lines 235-290
- **Verification:** Test with `thinking=True`, see reasoning chain in output

1.3 **Fix message structure** (Model: `deepinfra:V4-Flash-0731`, Subagent: Yes, Parallel with 1.2, 30min)
- Modify `server/openai_format.py` `messages_to_prompt()`
- Logic: if `conversation_id` present → send only last message; else → flatten all
- **File:** `server/openai_format.py` lines 33-48
- **Verification:** Test multi-turn conversation, verify context quality

1.4 **Test integration with Hermes/OWUI** (Manual, 30min)
- Config Hermes/OWUI → point to bridge (localhost:8000)
- Test chat, thinking, multi-turn
- **Verification:** 10 requests succeed, no errors

**Parallel Strategy:**
```
1.1 Fork & setup (manual, 30min)
    ↓
    ├─→ 1.2 Fix thinking (subagent, V4-Flash) ─┐
    └─→ 1.3 Fix message (subagent, V4-Flash) ──┤
                                                 ↓
                                          1.4 Test (manual, 30min)
```

**Deliverables:** Forked repo with 2 fixes, working bridge with thinking + better message handling, test results

**Total Effort:** 2-3 hours

---

### Phase 2: Vision Reverse-Engineering
**Goal:** Implement vision upload for DeepSeek-API bridge

**Tasks:**

2.1 **Research DeepSeek upload API** (Manual, 2-3h)
- Use browser DevTools or network sniffing to capture upload request
- Document endpoint, request format, response format
- **Tools:** Chrome DevTools, mitmproxy
- **Verification:** Clear documentation of upload API

2.2 **Implement file upload client** (Model: `alibaba:qwen3.7-max`, Subagent: Yes, Sequential after 2.1, 2-3h)
- Add `deepseek/upload.py` with `upload_image()` function
- Handle multipart form upload
- Return `file_id` from response
- **File:** `deepseek/upload.py` (new)
- **Verification:** Upload 1 image successfully, get file_id

2.3 **Integrate upload into completion request** (Model: `deepinfra:V4-Flash-0731`, Subagent: Yes, Parallel with 2.4, 1-2h)
- Modify `deepseek/client.py` to accept image input
- Attach `ref_file_ids` to completion body
- **File:** `deepseek/client.py` lines 138-183
- **Verification:** Send image + text → get response

2.4 **Add vision support to server** (Model: `deepinfra:V4-Flash-0731`, Subagent: Yes, Parallel with 2.3, 1-2h)
- Modify `server/openai_format.py` to handle image_url in messages
- Convert OpenAI image format → upload → ref_file_ids
- **File:** `server/openai_format.py`, `server/api.py`
- **Verification:** Test vision request via OpenAI SDK

2.5 **Test vision end-to-end** (Manual, 1h)
- Test with 5-10 different images
- Verify accuracy, latency
- **Verification:** 80%+ requests succeed, latency <10s

**Parallel Strategy:**
```
2.1 Research (manual, 2-3h)
    ↓
2.2 Upload client (subagent, qwen3.7-max, 2-3h)
    ↓
    ├─→ 2.3 Integrate upload (subagent, V4-Flash) ─┐
    └─→ 2.4 Add vision support (subagent, V4-Flash) ─┤
                                                      ↓
                                               2.5 Test (manual, 1h)
```

**Deliverables:** Vision support for bridge, documentation of DeepSeek upload API, test results with images

**Total Effort:** 6-10 hours (1-1.5 days)

**Risks:** DeepSeek upload API may change (fragile), rate limiting on upload endpoint, unclear image size limits

---

### Phase 3: Smart Router Core
**Goal:** Build FastAPI proxy server with routing logic, fallback chain, user confirmation

**Tasks:**

3.1 **Setup project structure** (Model: `alibaba:qwen3.8-flash`, Subagent: Yes, 2h)
- Create `~/dev/deepseek-hybrid-router/`
- Setup Python project (pyproject.toml, requirements.txt)
- Docker setup (Dockerfile, docker-compose.yml)
- **Files:** Project skeleton
- **Verification:** `docker build` succeeds

3.2 **Implement request analyzer** (Model: `alibaba:qwen3.7-max`, Subagent: Yes, Parallel with 3.3, 3-4h)
- Parse OpenAI request format
- Detect: has_tools?, has_image?, needs_reasoning?, has_search?
- Heuristics for reasoning detection (keywords, length, complexity)
- **File:** `router/analyzer.py`
- **Verification:** Unit tests for 10 request types

3.3 **Implement provider adapters** (3 subagents parallel, 4-6h total)
- **3.3a Bridge adapter** (Model: `deepinfra:V4-Flash-0731`, Subagent: Yes, Parallel, 2h)
  - Communicate with localhost:8000
  - **File:** `router/adapters/bridge.py`
- **3.3b DeepInfra adapter** (Model: `deepinfra:V4-Flash-0731`, Subagent: Yes, Parallel, 2h)
  - API key auth, model selection
  - **File:** `router/adapters/deepinfra.py`
- **3.3c Alibaba adapter** (Model: `deepinfra:V4-Flash-0731`, Subagent: Yes, Parallel, 2h)
  - Token plan auth, model selection
  - **File:** `router/adapters/alibaba.py`
- **Verification:** Each adapter tests 5 requests successfully

3.4 **Implement routing logic** (Model: `alibaba:qwen3.7-max`, Subagent: Yes, Sequential after 3.2+3.3, 3-4h)
- Rule-based routing based on request type
- Configurable routing table (JSON/YAML)
- **File:** `router/routing.py`
- **Verification:** 20 test cases with different request types

3.5 **Implement fallback chain** (Model: `alibaba:qwen3.7-max`, Subagent: Yes, Parallel with 3.4, 4-6h)
- Retry logic with exponential backoff
- Provider health checks
- Fallback triggers: 429, 503, timeout, auth_fail
- **File:** `router/fallback.py`
- **Verification:** Simulate provider failures, verify fallback works

3.6 **Implement user confirmation layer** (3 subagents parallel, 6-8h total)
- **3.6a CLI prompts** (Model: `deepinfra:V4-Flash-0731`, Subagent: Yes, Parallel, 2-3h)
  - stdin/stdout prompts
  - **File:** `router/confirmation/cli.py`
- **3.6b OWUI dialog** (Model: `alibaba:qwen3.7-max`, Subagent: Yes, Parallel, 3-4h)
  - WebSocket-based dialog for OWUI
  - **File:** `router/confirmation/owui.py`
- **3.6c Config rules** (Model: `alibaba:qwen3.8-flash`, Subagent: Yes, Parallel, 1-2h)
  - Pre-define rules, no prompts
  - **File:** `router/confirmation/config.py`
- **Verification:** Test each implementation

3.7 **Integrate with Hermes CLI** (Manual, 2h)
- Config Hermes → point to router (localhost:8001)
- Test tool calling, vision, expert
- **Verification:** 10 end-to-end tests

3.8 **Integrate with Open WebUI** (Manual, 2h)
- Config OWUI → point to router
- Test UI confirmations
- **Verification:** 5 end-to-end tests via OWUI

**Parallel Strategy:**
```
3.1 Setup project (subagent, qwen3.8-flash, 2h)
    ↓
    ├─→ 3.2 Request analyzer (subagent, qwen3.7-max, 3-4h)
    └─→ 3.3 Provider adapters (3 subagents parallel, V4-Flash, 2h each)
         ├─ 3.3a Bridge adapter
         ├─ 3.3b DeepInfra adapter
         └─ 3.3c Alibaba adapter
    ↓ (wait for 3.2 + 3.3)
    ├─→ 3.4 Routing logic (subagent, qwen3.7-max, 3-4h)
    └─→ 3.5 Fallback chain (subagent, qwen3.7-max, 4-6h)
    ↓ (wait for 3.4 + 3.5)
    3.6 User confirmation (3 subagents parallel, 2-4h each)
         ├─ 3.6a CLI (V4-Flash)
         ├─ 3.6b OWUI (qwen3.7-max)
         └─ 3.6c Config (qwen3.8-flash)
    ↓
3.7-3.8 Integration testing (manual, 4h)
```

**Deliverables:** Working router with routing + fallback + confirmation, Docker image, integration with Hermes + OWUI, test suite

**Total Effort:** 2-3 days (20-30 hours)

---

### Phase 4: Advanced Features
**Goal:** Add multi-account, cost tracking, session persistence, OWUI UI

**Tasks:**

4.1 **Multi-account bridge** (Model: `alibaba:qwen3.7-max`, Subagent: Yes, Parallel with 4.2+4.3+4.5, 4-6h)
- Support multiple DeepSeek accounts
- Load balancing (round-robin or weighted)
- Account health tracking
- **File:** `router/multi_account.py`
- **Verification:** Test with 2 accounts, verify load balancing

4.2 **Cost tracking** (Model: `deepinfra:V4-Flash-0731`, Subagent: Yes, Parallel with 4.1+4.3+4.5, 3-4h)
- Token counting for each provider
- Pricing database (DeepInfra, Alibaba rates)
- Session cost accumulation
- Budget alerts (configurable threshold)
- **File:** `router/cost.py`, `router/pricing.json`
- **Verification:** Verify cost calculation with 10 requests

4.3 **Session persistence** (Model: `alibaba:qwen3.7-max`, Subagent: Yes, Parallel with 4.1+4.2+4.5, 3-4h)
- Bridge session cache (Redis or SQLite)
- Proactive refresh (refresh before expire)
- Session failure recovery
- **File:** `router/session.py`
- **Verification:** Test session persistence across restarts

4.4 **OWUI UI components** (Model: `alibaba:qwen3.7-max`, Subagent: Yes, Sequential after 4.2+4.3, 4-6h)
- Confirmation dialog component
- Cost display widget
- Provider status indicator
- **Files:** OWUI extension (JavaScript/React)
- **Verification:** Test UI in OWUI

4.5 **Documentation** (Model: `alibaba:qwen3.8-flash`, Subagent: Yes, Parallel with 4.1+4.2+4.3, 3-4h)
- README.md (setup, usage)
- Architecture docs
- API docs
- Troubleshooting guide
- **Files:** `docs/`
- **Verification:** User can setup from docs

4.6 **Testing & validation** (Model: `deepinfra:V4-Flash-0731`, Subagent: Yes, Sequential after all, 4-6h)
- Unit tests (pytest)
- Integration tests
- E2E tests (Hermes + OWUI)
- Performance benchmarks
- **Files:** `tests/`
- **Verification:** 90%+ test coverage, all tests pass

**Parallel Strategy:**
```
Batch 1 (4 subagents parallel):
    ├─→ 4.1 Multi-account (qwen3.7-max, 4-6h)
    ├─→ 4.2 Cost tracking (V4-Flash, 3-4h)
    ├─→ 4.3 Session persistence (qwen3.7-max, 3-4h)
    └─→ 4.5 Documentation (qwen3.8-flash, 3-4h)
    ↓ (wait for all)
4.4 OWUI UI (subagent, qwen3.7-max, 4-6h)
    ↓
4.6 Testing (subagent, V4-Flash, 4-6h)
```

**Deliverables:** Multi-account support, cost tracking + budget alerts, session persistence, OWUI UI components, complete documentation, test suite

**Total Effort:** 2-3 days (20-30 hours)

---

## Total Effort Summary

| Phase | Tasks | Effort | Cumulative |
|-------|-------|--------|------------|
| Phase 1 | Bridge fork + fixes | 2-3 hours | 2-3 hours |
| Phase 2 | Vision reverse-engineering | 6-10 hours | 8-13 hours (1-1.5 days) |
| Phase 3 | Smart router core | 20-30 hours | 28-43 hours (3.5-5.5 days) |
| Phase 4 | Advanced features | 20-30 hours | 48-73 hours (6-9 days) |

**Total:** 6-9 days (if full-time)

**With parallelization:** ~5-6 days (30-40% time savings)

## Model Usage Summary

| Model | Tasks | Total Effort | Cost Estimate |
|-------|-------|--------------|---------------|
| **Alibaba:qwen3.7-max** | 2.2, 3.2, 3.4, 3.5, 3.6b, 4.1, 4.3, 4.4 | ~30-40h | Free (token plan) |
| **DeepInfra:V4-Flash-0731** | 1.2, 1.3, 2.3, 2.4, 3.3a-c, 3.6a, 4.2, 4.6 | ~20-25h | Free |
| **Alibaba:qwen3.8-flash** | 3.1, 3.6c, 4.5 | ~5-8h | Free (token plan) |
| **Manual** | 1.1, 1.4, 2.1, 2.5, 3.7, 3.8 | ~10-12h | $0 |

**Total model usage:** ~55-73 hours AI assistance (all free tier)

## Verification Strategy

**Unit tests:**
- Request analyzer: 20 test cases
- Provider adapters: 15 test cases (5 per adapter)
- Routing logic: 20 test cases
- Fallback chain: 10 test cases

**Integration tests:**
- Router + Bridge: 10 requests
- Router + DeepInfra: 10 requests
- Router + Alibaba: 10 requests

**E2E tests:**
- Hermes CLI → Router → Provider: 20 scenarios
- OWUI → Router → Provider: 20 scenarios

**Performance benchmarks:**
- Latency: <5s for normal requests, <15s for Expert
- Throughput: 10 requests/minute
- Uptime: 95%+ (bridge may fail)

## Risks & Mitigations

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| DeepSeek API changes | High | High | Monitor, quick fix, fallback to other providers |
| Vision reverse-engineering fails | Medium | Medium | Fallback to GLM-5.3-Flash only |
| Bridge session expires frequently | Medium | Low | Proactive refresh, multi-account |
| Docker performance issues | Low | Medium | Monitor, fallback to systemd |
| Rate limiting | Medium | Medium | Multi-account, fallback chain |

## Deployment

**Option:** Docker container
- Docker daemon already running on system
- Container: ~200-300MB RAM (FastAPI + Python + wasmtime)
- Bridge integration: Playwright + Chromium in container → ~400-600MB
- Total: ~650MB-1GB RAM
- Benefits: Isolation, easy rollback, reproducible, multi-instance support

## User Confirmation Strategy

**3 modes (configurable):**
1. **CLI prompts** (Hermes asks in terminal)
2. **OWUI dialog** (popup in web UI)
3. **Config rules** (pre-define, no prompts)

**When to ask:**
- Tool calling on bridge → "Route to DeepInfra?"
- Expert model slow → "Use Expert (12s) or Qwen3.7-max (4s)?"
- Bridge session expired → "Re-login or fallback to DeepInfra?"
- All free providers fail → "Use OpenRouter (paid)?"

**When to auto-route (no prompt):**
- Vision → GLM-5.3-Flash
- Tool calling → DeepInfra V4-Flash
- Normal chat → Bridge V4 Instant

## Next Steps

1. User confirms plan file is complete
2. User approves implementation start
3. Begin Phase 1 (fork + fixes)
4. Progress through phases with verification at each step
5. Final integration testing with Hermes + OWUI
