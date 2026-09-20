# Plan v2: Model Hybrid Nobita — Multi-Bridge Router with Copilot Integration

## Goal

Transform the current DeepSeek-only hybrid router into a **multi-bridge model router** that:
1. Renames from "deepseek-hybrid-nobita" to "model-hybrid-nobita"
2. Integrates Windows-Copilot-API as a second free bridge (port 8003)
3. Benchmarks DeepSeek vs Copilot and builds smart auto-routing logic
4. Dockerizes the entire system for portability across PCs

## Architecture (Target State)

```
Hermes CLI / Open WebUI
         |
Model Hybrid Nobita Router (localhost:8001, Docker)
         |
    +----+----------------+--------------+-------------+
    |                    |              |             |
    v                    v              v             v
DeepSeek Bridge     Copilot Bridge   DeepInfra    Alibaba Token
(localhost:8002)   (localhost:8003)  (paid)        (paid)
    |                    |
    v                    v
Free DeepSeek V4     Free GPT-4
                     (Copilot)
```

**Components:**
- **DeepSeek-API Bridge** (forked from `sums001/Deepseek-API`): Free access to DeepSeek V4 Instant, Expert, web search.
- **Copilot-API Bridge** (forked from `sums001/Windows-Copilot-API`): Free access to GPT-4 class via Microsoft Copilot. Uses curl_cffi + MSAL token.
- **DeepInfra**: Stable paid API with DeepSeek V4-Flash-0731, GLM-5.3-Flash.
- **Alibaba Token Plan**: Stable paid API with Qwen3.7-max, Qwen3.8-flash.
- **Smart Router**: FastAPI proxy that analyzes requests, benchmarks providers, and routes to the best available option with fallback.

## Model Allocation (per memory rules)

| Task Type | Model | Provider |
|-----------|-------|----------|
| Complex reasoning | qwen3.7-max | alibaba-token-plan |
| Code generation | deepseek-ai/DeepSeek-V4-Flash-0731 | deepinfra |
| Simple tasks/docs | qwen3.8-flash | alibaba-token-plan |
| Vision (if needed) | zai-org/GLM-5.3-Flash | deepinfra |

**Rules:**
- DeepInfra ONLY: `deepseek-ai/DeepSeek-V4-Flash-0731` + `zai-org/GLM-5.3-Flash`
- Alibaba Token Plan ONLY: `qwen3.7-*` + `qwen3.8-flash`
- No Pro models on any provider

---

## Phase 1: Rename & Restructure (30 minutes)

**Goal:** Rename everything from "deepseek-hybrid-nobita" to "model-hybrid-nobita"

### Task 1.1: Rename GitHub repo
- **Command:** `gh repo rename model-hybrid-nobita`
- **Verify:** Remote URL updates automatically

### Task 1.2: Stop services and rename local folder
- **Commands:**
  ```bash
  systemctl --user stop deepseek-router deepseek-bridge
  mv ~/dev/deepseek-hybrid-nobita ~/dev/model-hybrid-nobita
  ```
- **Verify:** `ls ~/dev/model-hybrid-nobita`

### Task 1.3: Update systemd service paths
- **File:** `~/.config/systemd/user/deepseek-router.service`
- **Changes:** Update `WorkingDirectory`, `EnvironmentFile`, `ExecStart` paths
- **Commands:**
  ```bash
  sed -i 's|deepseek-hybrid-nobita|model-hybrid-nobita|g' ~/.config/systemd/user/deepseek-router.service
  systemctl --user daemon-reload
  systemctl --user restart deepseek-router
  systemctl --user restart deepseek-bridge
  ```
- **Verify:** `systemctl --user status deepseek-router` shows active

### Task 1.4: Update Hermes config
- **File:** `~/.hermes/config.yaml`
- **Change:** `custom_providers.name` from `deepseek-hybrid-nobita` to `model-hybrid-nobita`
- **Verify:** `hermes config get custom_providers`

### Task 1.5: Update OWUI config
- **File:** OWUI database (docker exec)
- **Change:** `prefix_id` from `nobita` to `model-nobita`
- **Verify:** Restart OWUI, check model list shows new prefix

### Task 1.6: Git commit & push
- **Command:** `git add -A && git commit -m "chore: rename to model-hybrid-nobita" && git push`
- **Verify:** GitHub repo URL updated

**Deliverables:** All references changed to "model-hybrid-nobita", system working normally
**Downtime:** ~1-2 minutes during rename + restart

---

## Phase 2: Copilot Bridge Setup (1.5 hours)

**Goal:** Setup Windows-Copilot-API as second bridge on port 8003

### Task 2.1: Fork & clone Windows-Copilot-API
- **Commands:**
  ```bash
  gh repo fork sums001/Windows-Copilot-API --clone=false
  cd ~/dev && git clone git@github.com:thucnobita97/Windows-Copilot-API.git copilot-api-fork
  ```
- **Verify:** `ls ~/dev/copilot-api-fork`

### Task 2.2: Setup Python environment
- **Commands:**
  ```bash
  cd ~/dev/copilot-api-fork
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -e .
  ```
- **Verify:** `python -c "import copilot; print('OK')"`

### Task 2.3: Configure port 8003
- **File:** `~/dev/copilot-api-fork/.env`
- **Change:** `PORT=8003` (default is 8000)
- **Verify:** `grep PORT .env`

### Task 2.4: Sign-in Microsoft account (MANUAL)
- **Command:** `python -m copilot login`
- **Action:** User signs in via browser with Microsoft/Google account
- **Verify:** `ls session/` shows `token.json` and `session.json`
- **Note:** May require 2FA (manual step)

### Task 2.5: Test Copilot bridge
- **Command:** `python app.py` (runs on port 8003)
- **Test:**
  ```bash
  curl -X POST http://localhost:8003/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"copilot","messages":[{"role":"user","content":"Hello"}]}'
  ```
- **Verify:** Response has content, no errors

### Task 2.6: Create systemd service
- **File:** `~/.config/systemd/user/copilot-bridge.service`
- **Content:**
  ```ini
  [Unit]
  Description=Copilot API Bridge (port 8003)
  After=network.target

  [Service]
  Type=simple
  WorkingDirectory=/home/thucnobita/dev/copilot-api-fork
  ExecStart=/home/thucnobita/dev/copilot-api-fork/.venv/bin/python app.py
  Restart=always
  RestartSec=5

  [Install]
  WantedBy=default.target
  ```
- **Command:** `systemctl --user enable --now copilot-bridge`
- **Verify:** `systemctl --user status copilot-bridge` shows active

### Task 2.7: Git commit & push copilot-api-fork
- **Command:** `git add .env && git commit -m "feat: configure port 8003" && git push`
- **Verify:** GitHub repo updated

**Deliverables:** Copilot bridge running on port 8003, systemd service, session stored

**Risks:**
- Microsoft account sign-in may require 2FA (manual step)
- Cloudflare clearance expires after ~30 minutes (need re-login)
- Copilot rate limit 12 RPM (lower than DeepSeek)

---

## Phase 3: Integrate Copilot into Router (1 hour)

**Goal:** Add Copilot as provider in router, build integration layer

### Task 3.1: Create Copilot adapter
- **File:** `~/dev/model-hybrid-nobita/router/adapters/copilot.py` (new)
- **Model:** `deepinfra:deepseek-ai/DeepSeek-V4-Flash-0731` (code generation)
- **Logic:**
  ```python
  class CopilotAdapter:
      def __init__(self, base_url="http://localhost:8003"):
          self.base_url = base_url
      
      async def send(self, request: dict) -> dict:
          async with httpx.AsyncClient() as client:
              resp = await client.post(f"{self.base_url}/v1/chat/completions", json=request)
              return resp.json()
  ```
- **Verify:** Import works, can instantiate
- **Subagent:** Yes

### Task 3.2: Update routing config
- **File:** `~/dev/model-hybrid-nobita/config/routing.yaml`
- **Changes:** Add `copilot-bridge` provider, update fallback chains
- **New config:**
  ```yaml
  providers:
    copilot-bridge:
      base_url: http://localhost:8003
      description: "Windows Copilot API Bridge"
  
  routing_rules:
    chat:
      primary: deepseek-bridge:v4-instant
      fallbacks:
        - copilot-bridge:copilot
        - deepinfra:deepseek-ai/DeepSeek-V4-Flash-0731
        - alibaba:qwen3.8-flash
  ```
- **Model:** `deepinfra:deepseek-ai/DeepSeek-V4-Flash-0731`
- **Subagent:** Yes

### Task 3.3: Add health check endpoint
- **File:** `~/dev/model-hybrid-nobita/router/main.py`
- **Logic:** Add `/v1/copilot/health` endpoint, check `http://localhost:8003/health`
- **Model:** `deepinfra:deepseek-ai/DeepSeek-V4-Flash-0731`
- **Verify:** `curl http://localhost:8001/v1/copilot/health` returns status
- **Subagent:** Yes

### Task 3.4: Update /v1/models endpoint
- **File:** `~/dev/model-hybrid-nobita/router/main.py`
- **Logic:** Add Copilot models to the list
- **Model:** `deepinfra:deepseek-ai/DeepSeek-V4-Flash-0731`
- **Verify:** `curl http://localhost:8001/v1/models` includes Copilot entries
- **Subagent:** Yes

### Task 3.5: Test integration
- **Test:** `hermes chat -m model-hybrid-nobita:copilot -q "Hello"`
- **Verify:** Response from Copilot, no errors
- **Subagent:** No (manual)

### Task 3.6: Git commit & push
- **Command:** `git add router/adapters/copilot.py config/routing.yaml router/main.py && git commit -m "feat: integrate Copilot bridge" && git push`

**Deliverables:** Copilot bridge integrated into router, usable as provider, health check available

**Parallelization:** Tasks 3.1 + 3.2 + 3.3 + 3.4 can run in parallel (independent code paths)

---

## Phase 4: Benchmark & Auto-Routing (3 hours)

**Goal:** Test DeepSeek vs Copilot, build smart routing based on performance

### Task 4.1: Design benchmark test suite
- **File:** `~/dev/model-hybrid-nobita/tests/benchmark.py` (new)
- **Model:** `alibaba:qwen3.7-max` (reasoning)
- **Test cases:**
  - Chat (short): "Hello, how are you?"
  - Chat (long): 500-word prompt
  - Reasoning: Math problem, logic puzzle
  - Code generation: Simple function
  - Tool calling: Function call request
- **Metrics:** Latency (TTFB, total), success rate, quality (manual rating)
- **Subagent:** Yes

### Task 4.2: Run benchmark (MANUAL)
- **Command:** `python tests/benchmark.py --providers deepseek-bridge,copilot-bridge --iterations 10`
- **Duration:** ~30 minutes (10 iterations x 2 providers x 5 test cases)
- **Output:** `benchmark_results.json` with latency, success rate
- **Subagent:** No (manual)

### Task 4.3: Analyze results
- **Model:** `alibaba:qwen3.7-max` (analysis)
- **Output:** `benchmark_analysis.md` with recommendations
- **Subagent:** Yes

### Task 4.4: Implement smart routing logic
- **File:** `~/dev/model-hybrid-nobita/router/smart_router.py` (new)
- **Model:** `deepinfra:deepseek-ai/DeepSeek-V4-Flash-0731` (code generation)
- **Logic:** Score-based routing considering latency, success rate, and rate limits
- **Subagent:** Yes

### Task 4.5: Integrate smart router into main flow
- **File:** `~/dev/model-hybrid-nobita/router/main.py`
- **Model:** `alibaba:qwen3.7-max` (refactoring)
- **Logic:** Replace static routing with SmartRouter
- **Depends on:** Task 4.4
- **Subagent:** Yes

### Task 4.6: Add monitoring endpoint
- **File:** `~/dev/model-hybrid-nobita/router/main.py`
- **Logic:** Add `/v1/routing/stats` endpoint, return routing decisions
- **Model:** `deepinfra:deepseek-ai/DeepSeek-V4-Flash-0731`
- **Subagent:** Yes

### Task 4.7: Test smart routing
- **Test:** Send 20 requests, check routing decisions
- **Verify:** Router selects provider correctly based on benchmark
- **Subagent:** No (manual)

### Task 4.8: Git commit & push
- **Command:** `git add tests/benchmark.py router/smart_router.py router/main.py && git commit -m "feat: smart auto-routing based on benchmark" && git push`

**Deliverables:** Benchmark data, smart routing logic, monitoring endpoint

---

## Phase 5: Docker Verification (30 minutes)

**Goal:** Verify existing Docker setup works with all new components

**Current state:** Dockerfile + docker-compose.yml already exist from Phase 5 of original plan.

### Task 5.1: Verify docker-compose config
- **Command:** `cd ~/dev/model-hybrid-nobita && docker-compose config`
- **Verify:** No errors, all services defined correctly

### Task 5.2: Test docker-compose build
- **Command:** `docker-compose build`
- **Verify:** All images build successfully

### Task 5.3: Test docker-compose up
- **Command:** `docker-compose up -d`
- **Test:** Send requests through router, verify all bridges work
- **Verify:** All services running, requests succeed

### Task 5.4: Create setup script for other PCs
- **File:** `~/dev/model-hybrid-nobita/setup.sh` (new)
- **Content:** Clone all repos, build, start
- **Model:** `alibaba:qwen3.8-flash`

### Task 5.5: Update README with Docker docs
- **File:** `~/dev/model-hybrid-nobita/README.md`
- **Content:** Docker setup guide, troubleshooting, benchmark results
- **Model:** `alibaba:qwen3.8-flash`

### Task 5.6: Final git commit & push
- **Command:** `git add -A && git commit -m "feat: Docker migration verified" && git push`

**Deliverables:** Verified Docker setup, setup script, updated documentation

---

## Phase Dependencies

```
Phase 1 (Rename)
    |
    v
Phase 2 (Copilot Setup) -- needs folder renamed
    |
    v
Phase 3 (Integration) -- needs Copilot bridge running
    |
    v
Phase 4 (Benchmark) -- needs integration complete
    |
    v
Phase 5 (Docker verify) -- needs everything stable
```

## Subagent Strategy

| Task | Can Parallelize? | Model |
|------|-----------------|-------|
| 3.1 Copilot adapter | Yes (with 3.2, 3.3, 3.4) | deepinfra:V4-Flash-0731 |
| 3.2 Routing config | Yes (with 3.1, 3.3, 3.4) | deepinfra:V4-Flash-0731 |
| 3.3 Health check | Yes (with 3.1, 3.2, 3.4) | deepinfra:V4-Flash-0731 |
| 3.4 Models endpoint | Yes (with 3.1, 3.2, 3.3) | deepinfra:V4-Flash-0731 |
| 4.1 Benchmark suite | No (sequential) | alibaba:qwen3.7-max |
| 4.3 Analysis | No (sequential, after 4.2) | alibaba:qwen3.7-max |
| 4.4 Smart routing | No (sequential) | deepinfra:V4-Flash-0731 |
| 4.5 Integrate | No (after 4.4) | alibaba:qwen3.7-max |

## Total Effort

| Phase | Duration | Notes |
|-------|----------|-------|
| Phase 1: Rename | 30 min | Sequential, ~2min downtime |
| Phase 2: Copilot setup | 1.5 hours | Manual sign-in required |
| Phase 3: Integration | 1 hour | Tasks 3.1-3.4 parallel |
| Phase 4: Benchmark | 3 hours | Manual benchmark run |
| Phase 5: Docker verify | 30 min | Most Docker already done |
| **Total** | **~6-7 hours** | |

## Success Criteria

1. Rename complete, system working normally
2. Copilot bridge running on port 8003, systemd service active
3. Router integrates Copilot, has health check, models endpoint
4. Benchmark data complete, smart routing logic working
5. Docker setup verified, portable to other PCs

## Risks & Mitigations

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Microsoft sign-in 2FA | High | Low | Manual step, document clearly |
| Copilot rate limit 12 RPM | Medium | Medium | Throttle in benchmark, smart routing considers it |
| Docker networking | Low | Medium | Test thoroughly |
| Session persistence in Docker | Low | Medium | Verify volumes mount correctly |
| Rename breaks services | Low | High | Backup service files before, quick rollback |
