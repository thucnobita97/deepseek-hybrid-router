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

## Phase 2: Copilot Bridge Setup (SKIPPED 2026-09-20)

**Reasons for skipping:**
- Copilot geo-blocked in Vietnam ("Not available in your region")
- Exporting browser profile from Windows Edge to WSL is complex (cf_clearance cookie required)
- Cloudflare clearance expires ~30 minutes → frequent re-login
- Low rate limit (12 RPM vs DeepSeek's 120 RPM)

**Decision:** Focus on existing bridges:
- DeepSeek free bridge (120 RPM, already running on port 8002)
- Paid APIs: DeepInfra + Alibaba Token Plan (stable, no geo-restrictions)

**Impact:** Still achieves smart routing goals with 2-tier fallback (free → paid)

---

## Phase 3: Integrate Copilot into Router (SKIPPED 2026-09-20)

Skipped due to Phase 2 failure. Router will benchmark and route between:
- Primary: DeepSeek free bridge
- Fallback: DeepInfra + Alibaba paid APIs

---

## Phase 4: Benchmark & Auto-Routing (2 hours, modified)

**Goal:** Benchmark DeepSeek free bridge vs Paid APIs, build smart routing based on cost/latency/quality

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

## Phase Dependencies (Updated)

```
Phase 1 (Rename) -- DONE
    |
    v
Phase 2 (Copilot Setup) -- SKIPPED (geo-blocked)
Phase 3 (Integration) -- SKIPPED
    |
    v
Phase 4 (Benchmark) -- DeepSeek free vs Paid APIs
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

## Total Effort (Updated)

| Phase | Duration | Notes |
|-------|----------|-------|
| Phase 1: Rename | 30 min | DONE |
| Phase 2: Copilot setup | SKIPPED | Geo-blocked in VN |
| Phase 3: Integration | SKIPPED | No Copilot |
| Phase 4: Benchmark | 2 hours | DeepSeek free vs Paid APIs |
| Phase 5: Docker verify | 30 min | Most Docker already done |
| **Total** | **~2.5 hours** | |

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
