# Plan: Model Hybrid Nobita — Multi-Bridge Router with Copilot Integration

## Goal

Transform the current DeepSeek-only hybrid router into a **multi-bridge model router** that:
1. Renames from "deepseek-hybrid-nobita" → "model-hybrid-nobita"
2. Integrates Windows-Copilot-API as a second free bridge (port 8003)
3. Benchmarks DeepSeek vs Copilot and builds smart auto-routing logic
4. Dockerizes the entire system for portability across PCs

## Architecture (Target State)

```
Hermes CLI / Open WebUI
         ↓
Model Hybrid Nobita Router (localhost:8001, Docker)
         ↓
    ┌────┴────────────────┬──────────────┬─────────────┐
    ↓                     ↓              ↓             ↓
DeepSeek Bridge     Copilot Bridge   DeepInfra    Alibaba Token
(localhost:8002)   (localhost:8003)  (paid)        (paid)
    ↓                     ↓
Free DeepSeek V4     Free GPT-4
                     (Copilot)
```

**Components:**
- **DeepSeek-API Bridge** (forked from `sums001/Deepseek-API`): Free access to DeepSeek V4 Instant, Expert, web search. Reverse-engineered from chat.deepseek.com.
- **Copilot-API Bridge** (forked from `sums001/Windows-Copilot-API`): Free access to GPT-4 class via Microsoft Copilot. Uses curl_cffi + MSAL token.
- **DeepInfra**: Stable paid API with DeepSeek V4-Flash-0731, GLM-5.3-Flash.
- **Alibaba Token Plan**: Stable paid API with Qwen3.7-max, Qwen3.8-flash.
- **Smart Router**: FastAPI proxy that analyzes requests, benchmarks providers, and routes to the best available option with fallback.

---

## Phase 1: Rename & Restructure (30 minutes)

**Goal:** Rename everything from "deepseek-hybrid-nobita" → "model-hybrid-nobita"

### Task 1.1: Rename GitHub repo
- **Action:** `gh repo rename model-hybrid-nobita`
- **Verify:** Remote URL updates automatically

### Task 1.2: Rename local folder
- **Action:** `mv ~/dev/deepseek-hybrid-nobita ~/dev/model-hybrid-nobita`
- **Verify:** `ls ~/dev/model-hybrid-nobita`

### Task 1.3: Update systemd service paths
- **File:** `~/.config/systemd/user/deepseek-router.service`
- **Changes:** Update `WorkingDirectory`, `EnvironmentFile`, `ExecStart` paths
- **Commands:**
  ```bash
  sed -i 's|deepseek-hybrid-nobita|model-hybrid-nobita|g' ~/.config/systemd/user/deepseek-router.service
  systemctl --user daemon-reload
  systemctl --user restart deepseek-router
  ```
- **Verify:** `systemctl --user status deepseek-router` shows active

### Task 1.4: Update Hermes config
- **File:** `~/.hermes/config.yaml`
- **Change:** `custom_providers.name` from `deepseek-hybrid-nobita` → `model-hybrid-nobita`
- **Command:** `hermes config set custom_providers '[{"name":"model-hybrid-nobita","base_url":"http://localhost:8001/v1","api_key": ***,"context_length":131072}]'`
- **Verify:** `hermes config get custom_providers`

### Task 1.5: Update OWUI config
- **File:** OWUI database (docker exec)
- **Change:** `prefix_id` from `nobita` → `model-nobita`
- **Action:** SQL update via docker exec python script
- **Verify:** Restart OWUI, check model list shows new prefix

### Task 1.6: Git commit & push
- **Command:** `git add -A && git commit -m "chore: rename to model-hybrid-nobita" && git push`
- **Verify:** GitHub repo URL updated

**Deliverables:** All references changed to "model-hybrid-nobita", system working normally

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
- **Model:** `deepinfra:V4-Flash-0731` (code generation)
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
        - deepinfra:V4-Flash-0731
        - alibaba:qwen3.8-flash
  ```
- **Verify:** Router reloads config without errors

### Task 3.3: Add health check endpoint
- **File:** `~/dev/model-hybrid-nobita/router/main.py`
- **Model:** `deepinfra:V4-Flash-0731`
- **Logic:** Add `/v1/copilot/health` endpoint, check `http://localhost:8003/health`
- **Verify:** `curl http://localhost:8001/v1/copilot/health` returns status

### Task 3.4: Update /v1/models endpoint
- **File:** `~/dev/model-hybrid-nobita/router/main.py`
- **Logic:** Add Copilot models to the list
- **Verify:** `curl http://localhost:8001/v1/models` includes Copilot entries

### Task 3.5: Test integration
- **Test:** `hermes chat -m model-hybrid-nobita:copilot -q "Hello"`
- **Verify:** Response from Copilot, no errors

### Task 3.6: Git commit & push
- **Command:** `git add router/adapters/copilot.py config/routing.yaml router/main.py && git commit -m "feat: integrate Copilot bridge" && git push`

**Deliverables:** Copilot bridge integrated into router, usable as provider, health check available

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
- **Verify:** Test script runs, has clear structure

### Task 4.2: Run benchmark (MANUAL)
- **Command:** `python tests/benchmark.py --providers deepseek-bridge,copilot-bridge --iterations 10`
- **Duration:** ~30 minutes (10 iterations × 2 providers × 5 test cases)
- **Output:** `benchmark_results.json` with latency, success rate
- **Verify:** Data collected, no crashes

### Task 4.3: Analyze results
- **Model:** `alibaba:qwen3.7-max` (analysis)
- **Logic:**
  - DeepSeek average latency
  - Copilot average latency
  - Success rate comparison
  - Quality comparison (manual review)
- **Output:** `benchmark_analysis.md` with recommendations
- **Verify:** Analysis has insights, clear recommendations

### Task 4.4: Implement smart routing logic
- **File:** `~/dev/model-hybrid-nobita/router/smart_router.py` (new)
- **Model:** `deepinfra:V4-Flash-0731` (code generation)
- **Logic:**
  ```python
  class SmartRouter:
      def __init__(self, benchmark_results: dict):
          self.deepseek_score = benchmark_results['deepseek']['score']
          self.copilot_score = benchmark_results['copilot']['score']
      
      def select_provider(self, request: dict) -> str:
          # If Copilot is faster and quality is comparable → prefer Copilot
          if self.copilot_score > self.deepseek_score:
              return 'copilot-bridge'
          else:
              return 'deepseek-bridge'
  ```
- **Verify:** Unit tests pass, logic is correct

### Task 4.5: Integrate smart router into main flow
- **File:** `~/dev/model-hybrid-nobita/router/main.py`
- **Logic:** Replace static routing with SmartRouter
- **Verify:** Router uses smart routing, test 10 requests

### Task 4.6: Add monitoring endpoint
- **File:** `~/dev/model-hybrid-nobita/router/main.py`
- **Logic:** Add `/v1/routing/stats` endpoint, return routing decisions
- **Verify:** `curl http://localhost:8001/v1/routing/stats` has data

### Task 4.7: Test smart routing
- **Test:** Send 20 requests, check routing decisions
- **Verify:** Router selects provider correctly based on benchmark

### Task 4.8: Git commit & push
- **Command:** `git add tests/benchmark.py router/smart_router.py router/main.py && git commit -m "feat: smart auto-routing based on benchmark" && git push`

**Deliverables:** Benchmark data, smart routing logic, monitoring endpoint

**Risks:**
- Benchmark may not be representative (need many iterations)
- Copilot rate limit 12 RPM (need throttling)
- Smart routing may overfit (need validation)

---

## Phase 5: Docker Migration (4 hours)

**Goal:** Containerize entire system for portability across PCs

### Task 5.1: Create Dockerfile for router
- **File:** `~/dev/model-hybrid-nobita/Dockerfile`
- **Model:** `deepinfra:V4-Flash-0731` (code generation)
- **Content:**
  ```dockerfile
  FROM python:3.12-slim
  WORKDIR /app
  COPY requirements.txt .
  RUN pip install -r requirements.txt
  COPY . .
  EXPOSE 8001
  CMD ["uvicorn", "router.main:app", "--host", "0.0.0.0", "--port", "8001"]
  ```
- **Verify:** `docker build -t model-hybrid-nobita .` succeeds

### Task 5.2: Create Dockerfile for DeepSeek bridge
- **File:** `~/dev/deepseek-api-fork/Dockerfile`
- **Content:**
  ```dockerfile
  FROM python:3.12-slim
  WORKDIR /app
  COPY requirements.txt .
  RUN pip install -r requirements.txt
  COPY . .
  EXPOSE 8002
  CMD ["python", "app.py"]
  ```
- **Verify:** `docker build -t deepseek-bridge .` succeeds

### Task 5.3: Create Dockerfile for Copilot bridge
- **File:** `~/dev/copilot-api-fork/Dockerfile`
- **Content:**
  ```dockerfile
  FROM python:3.12-slim
  WORKDIR /app
  COPY requirements.txt .
  RUN pip install -r requirements.txt
  COPY . .
  EXPOSE 8003
  CMD ["python", "app.py"]
  ```
- **Verify:** `docker build -t copilot-bridge .` succeeds

### Task 5.4: Create docker-compose.yml
- **File:** `~/dev/model-hybrid-nobita/docker-compose.yml`
- **Content:**
  ```yaml
  version: '3.8'
  services:
    router:
      build: .
      ports:
        - "8001:8001"
      depends_on:
        - deepseek-bridge
        - copilot-bridge
      volumes:
        - ./config:/app/config
        - ./router/data:/app/router/data
    
    deepseek-bridge:
      build: ../deepseek-api-fork
      ports:
        - "8002:8002"
      volumes:
        - ../deepseek-api-fork/session:/app/session
    
    copilot-bridge:
      build: ../copilot-api-fork
      ports:
        - "8003:8003"
      volumes:
        - ../copilot-api-fork/session:/app/session
  ```
- **Verify:** `docker-compose config` is valid

### Task 5.5: Test Docker setup
- **Command:** `docker-compose up --build`
- **Test:** Send requests through router, verify both bridges work
- **Verify:** All services running, requests succeed

### Task 5.6: Create setup script for other PCs
- **File:** `~/dev/model-hybrid-nobita/setup.sh`
- **Content:**
  ```bash
  #!/bin/bash
  git clone https://github.com/thucnobita97/model-hybrid-nobita.git
  git clone https://github.com/thucnobita97/Deepseek-API.git deepseek-api-fork
  git clone https://github.com/thucnobita97/Windows-Copilot-API.git copilot-api-fork
  cd model-hybrid-nobita
  docker-compose up --build -d
  echo "Setup complete! Sign-in required for bridges."
  ```
- **Verify:** Script runs on new PC (test on VM or fresh WSL)

### Task 5.7: Update documentation
- **File:** `~/dev/model-hybrid-nobita/README.md`
- **Content:** Docker setup guide, troubleshooting, benchmark results
- **Verify:** README is clear, has examples

### Task 5.8: Git commit & push
- **Command:** `git add Dockerfile docker-compose.yml setup.sh README.md && git commit -m "feat: Docker migration for portability" && git push`

**Deliverables:** Complete Docker setup, portable to other PCs, full documentation

**Risks:**
- Docker networking between containers (need thorough testing)
- Session persistence via Docker volumes (need verification)
- Playwright in Docker (need to install Chromium)

---

## Summary

**Total estimated time:** 10.5 hours (1.5 days)
- Phase 1: 30 minutes (rename)
- Phase 2: 1.5 hours (Copilot setup)
- Phase 3: 1 hour (integration)
- Phase 4: 3 hours (benchmark + smart routing)
- Phase 5: 4 hours (Docker migration)

**Model allocation:**
- Complex reasoning: `alibaba:qwen3.7-max`
- Code generation: `deepinfra:V4-Flash-0731`
- Simple tasks: `alibaba:qwen3.8-flash`

**Subagent strategy:**
- Tasks 2.2, 3.1, 4.1, 4.4, 5.1-5.3: Can parallelize (independent)
- Tasks 1, 2.4, 3.5, 4.2, 5.5: Manual tasks (no delegation)

**Success criteria:**
1. ✅ Rename complete, system working normally
2. ✅ Copilot bridge running on port 8003, systemd service active
3. ✅ Router integrates Copilot, has health check, models endpoint
4. ✅ Benchmark data complete, smart routing logic working
5. ✅ Docker setup complete, portable to other PCs

**Risks & mitigations:**
- Microsoft sign-in 2FA → Manual step, document clearly
- Copilot rate limit → Throttle in benchmark, smart routing considers it
- Docker networking → Test thoroughly before finalizing
- Session persistence → Verify volumes mount correctly
