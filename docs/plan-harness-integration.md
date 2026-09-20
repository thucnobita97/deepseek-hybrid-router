# DeepSeek Harness Integration Plan

## Overview

Integrate DeepSeek Harness as an agent runtime layer that provides 40+ local tools (shell, filesystem, web, code execution, LSP, subagents, workflow) while using free API bridges for LLM reasoning.

## Goal

Create a hybrid system where:
- **LLM reasoning**: Uses free bridges (DeepSeek, Copilot) via router
- **Tool execution**: Uses Harness's 40+ local tools (no API cost)
- **Fallback**: Paid APIs only when free bridges fail
- **Result**: 90% free, 10% paid → 90% cost reduction

## Architecture

### Current System
```
User Request → Router (8001) → DeepSeek/Copilot Bridges → Response
                                    ↓
                              (No tools)
```

### Target System with Harness
```
User Request → Router (8001) → Harness Agent Runtime
                                    ↓
                    ┌───────────────┴───────────────┐
                    ↓                               ↓
              LLM Reasoning                   Tool Execution
              (via Router)                    (Local - FREE)
                    ↓                               ↓
              DeepSeek Bridge              40+ Harness Tools
              Copilot Bridge               - bash, file ops
              (Fallback: paid)             - web fetch
                                           - code execution
                                           - LSP, subagents
```

### Integration Options

#### Option 1: Harness as Agent Runtime (Recommended)
```
User → Hermes/OWUI → Router → Harness → Bridge (free)
                              ↓
                        Local tools
```
- Harness manages entire agent loop
- Router provides LLM via free bridges
- Tools execute locally

**Pros:**
- Full agent capabilities immediately
- 40+ production-ready tools
- Cordis plugin architecture

**Cons:**
- Heavy (full Cordis runtime)
- Complex build process (pnpm install + build)
- Harder to debug

#### Option 2: Extract Tools to Bridge
```
User → Hermes/OWUI → Router → Bridge + Custom Tools
```
- Copy tool definitions from Harness
- Implement tool execution in bridge
- Lightweight integration

**Pros:**
- Lightweight, full control
- Easier to debug
- No Cordis dependency

**Cons:**
- Need to build tools from scratch
- Missing advanced features (sandbox, approval, workflows)
- More code to maintain

#### Option 3: Hybrid Approach (Best)
```
User → Hermes/OWUI → Router → Smart Routing
                                ↓
                    ┌───────────┴───────────┐
                    ↓                       ↓
            Harness (complex)      Bridge + custom (simple)
            - multi-step tasks     - quick bash
            - file editing         - read file
            - code navigation      - simple queries
            - subagents
```
- Router detects task complexity
- Complex tasks → Harness
- Simple tasks → Bridge with custom tools

**Pros:**
- Best of both worlds
- Optimal performance
- Flexible

**Cons:**
- Complex routing logic
- Two systems to maintain

## Harness Tools Inventory

### Core Tools (Always Available)
1. **Shell Execution**
   - `bash` - One-shot bash commands
   - `bash (persistent)` - Persistent PTY shell
   - `pwsh` - PowerShell (Windows)
   - `terminal_open/close/list/read/send/signal` - 6 terminal management tools

2. **Filesystem Operations**
   - `read` - Read UTF-8 files with line numbers
   - `write` - Create/replace files
   - `edit` - Literal text replacement
   - `read_image` - Read PNG/JPEG/WebP/GIF
   - `glob` - Find files by pattern
   - `grep` - Search file contents (ripgrep)
   - `str_replace_editor` - Advanced editor

3. **Web Access**
   - `web_search` - Web search (Exa/Perplexity/DeepSeek)
   - `web_fetch` - Fetch web page content

4. **Code Execution**
   - `run_code` - Execute TypeScript with tool bindings

5. **Job Management**
   - `bash (background)` - Background job execution
   - `job_list/job_output/job_kill` - Job management

### Advanced Tools
6. **Subagent System**
   - `subagent` - Delegate to child agents
   - `subagent_fork` - Fork-based delegation
   - `send_message` - Message background agents
   - `interrupt_agent` - Cancel agent turn
   - `list_agents` - List background agents
   - `report` - Report to parent agent

7. **Language Server Protocol**
   - `lsp` - Code navigation (definition, references, hover)

8. **Session Management**
   - `session_event_read/search/trace` - Query session events
   - `session_search/trace` - Cross-session search

9. **Task Management**
   - `todo_write` - Task checklist
   - `create_goal/get_goal/update_goal` - Goal tracking

10. **Scheduling**
    - `schedule_create/delete/list` - Reminder scheduling

11. **Plugin System**
    - `skill` - Load skill instructions
    - `cordis_define/run/stop/undefine/inspect_*` - 7 Cordis tools

12. **Team Collaboration**
    - `spawn_teammate` - Spawn team member
    - `followup_task` - Follow-up task
    - `team_task_*` - 10 team task tools

13. **User Interaction**
    - `ask_user_question` - Ask user for input
    - `exit_plan_mode` - Present plan for approval

14. **Workflow Engine**
    - `workflow` - Execute workflow scripts
    - `ralph` - Fresh-agent iteration loop

## Integration Challenges

### 1. Rate Limits
**Problem:** Agent loop creates 5-20 LLM calls per task. Free APIs have low rate limits.

**Solution:**
- Request queuing in router
- Exponential backoff on 429 errors
- Token counting to stay within TPM limits
- Smart caching of LLM responses

### 2. Streaming Format
**Problem:** Harness expects specific OpenAI SSE format. Bridge must match exactly.

**Solution:**
- Verify bridge SSE format matches OpenAI spec
- Patch llm-deepseek adapter if needed
- Test with simple requests first

### 3. Build Complexity
**Problem:** Harness requires full build from source (pnpm install + build).

**Solution:**
- Create Docker image with pre-built Harness
- Document build process clearly
- Provide binary releases if possible

### 4. Reasoning Mode
**Problem:** Free APIs may not support thinking/reasoning features.

**Solution:**
- Disable reasoning mode in config
- Use standard chat completion
- Accept lower reasoning quality

### 5. Context Window
**Problem:** Free APIs have smaller context windows (32K-64K vs 128K+).

**Solution:**
- Configure maxTokens appropriately
- Implement context compression
- Use subagents to split large tasks

### 6. Image Handling
**Problem:** Vision features require Files API (upload base64 images).

**Solution:**
- Implement Files API in bridge
- Or disable vision features
- Use alternative vision services

## Phases

### Phase 9: Harness Integration (4 hours)

**Goal:** Integrate Harness as agent runtime with free bridge backend

#### Task 9.1: Build Harness from Source (1 hour)
- **Commands:**
  ```bash
  cd ~/dev/deepseek-harness
  pnpm install
  pnpm run build
  ```
- **Verification:** Build succeeds, `dsh` CLI available
- **Risk:** Build may fail, need to debug

#### Task 9.2: Configure Harness to Use Bridge (30 minutes)
- **File:** `cordis.patch.yml` or environment variables
- **Config:**
  ```yaml
  llm:
    provider: deepseek
    baseURL: http://localhost:8001  # Router endpoint
    apiKey: dummy  # Router handles auth
  ```
- **Verification:** Harness connects to router, can send requests
- **Risk:** Config format may differ, need to check docs

#### Task 9.3: Test Basic Agent Loop (1 hour)
- **Action:** Run simple agent task
  ```bash
  dsh --profile headless "Read /etc/hostname and tell me what it says"
  ```
- **Verification:** 
  - Harness calls LLM via router
  - Router routes to free bridge
  - Harness executes `read` tool
  - Returns correct answer
- **Risk:** Agent loop may fail, need to debug

#### Task 9.4: Implement Tool Whitelist (30 minutes)
- **File:** `cordis.patch.yml`
- **Config:**
  ```yaml
  tools:
    enabled:
      - bash
      - read
      - write
      - edit
      - glob
      - grep
      - web_fetch
      - todo_write
    disabled:
      - web_search  # Requires API key
      - schedule_create  # Not needed
  ```
- **Verification:** Only whitelisted tools available
- **Risk:** Some tools may have dependencies

#### Task 9.5: Test Complex Agent Task (1 hour)
- **Action:** Run multi-step task
  ```bash
  dsh --profile headless "Create a Python script that reads a file, counts lines, and saves result to output.txt"
  ```
- **Verification:**
  - Harness plans task
  - Calls LLM multiple times
  - Executes tools (write, bash, read)
  - Completes successfully
- **Risk:** Complex tasks may hit rate limits

#### Task 9.6: Performance Benchmark (30 minutes)
- **Action:** Measure performance
  - Simple task: 1 LLM call + 1 tool
  - Complex task: 5 LLM calls + 3 tools
  - Measure latency, success rate
- **Output:** Performance report
- **Risk:** Rate limits may affect results

#### Task 9.7: Documentation (30 minutes)
- **File:** `docs/harness-integration.md`
- **Content:**
  - Setup guide
  - Configuration options
  - Available tools
  - Troubleshooting
  - Performance tips
- **Verification:** Documentation is clear, examples work

**Deliverables:**
- Harness integrated with router
- Tool whitelist configured
- Performance benchmarks
- Integration documentation

**Risks:**
- Build failures → provide pre-built Docker image
- Rate limits → implement queuing and caching
- Config complexity → provide templates

---

### Phase 10: Hybrid Routing (3 hours)

**Goal:** Implement smart routing between Harness and Bridge based on task complexity

#### Task 10.1: Design Complexity Detector (1 hour)
- **File:** `router/complexity_detector.py` (new)
- **Model:** `alibaba:qwen3.7-max` (complex logic)
- **Logic:**
  ```python
  class ComplexityDetector:
      def detect(self, request: dict) -> str:
          """
          Returns: 'simple' | 'complex'
          
          Simple: 
          - Single tool call
          - No file editing
          - No multi-step reasoning
          
          Complex:
          - Multi-tool tasks
          - File editing
          - Code navigation
          - Subagent delegation
          """
          # Analyze request content
          # Check for keywords (edit, refactor, analyze)
          # Check message length
          # Check for tool definitions
          pass
  ```
- **Verification:** Detector correctly classifies 10 test cases

#### Task 10.2: Implement Hybrid Router (1 hour)
- **File:** `router/hybrid_router.py` (new)
- **Model:** `deepinfra:V4-Flash-0731` (code generation)
- **Logic:**
  ```python
  class HybridRouter:
      def route(self, request: dict) -> str:
          complexity = self.detector.detect(request)
          
          if complexity == 'simple':
              # Use bridge directly (fast, cheap)
              return 'bridge'
          else:
              # Use Harness (powerful, complex)
              return 'harness'
  ```
- **Verification:** Router correctly routes 10 test cases

#### Task 10.3: Integrate with Main Router (1 hour)
- **File:** `router/main.py`
- **Model:** `alibaba:qwen3.7-max` (refactoring)
- **Actions:**
  - Add complexity detection before routing
  - Route to Harness or Bridge based on complexity
  - Maintain backward compatibility
- **Verification:** Main router works with hybrid routing

#### Task 10.4: Test Hybrid System (30 minutes)
- **Actions:**
  - Test simple task → should use bridge
  - Test complex task → should use Harness
  - Measure performance difference
- **Verification:** Hybrid routing works correctly

#### Task 10.5: Documentation (30 minutes)
- **File:** `docs/hybrid-routing.md`
- **Content:**
  - How complexity detection works
  - When to use Harness vs Bridge
  - Configuration options
  - Performance benchmarks
- **Verification:** Documentation is clear

**Deliverables:**
- Complexity detector
- Hybrid router
- Performance benchmarks
- Documentation

**Risks:**
- Complexity detection accuracy → start simple, iterate
- Routing overhead → optimize hot path
- Backward compatibility → test thoroughly

---

### Phase 11: Custom Tools for Bridge (2 hours)

**Goal:** Implement lightweight custom tools for simple bridge requests

#### Task 11.1: Design Custom Tool Interface (30 minutes)
- **File:** `bridge/tools/base.py` (new)
- **Model:** `alibaba:qwen3.7-max` (architecture)
- **Interface:**
  ```python
  class CustomTool:
      name: str
      description: str
      
      def execute(self, params: dict) -> dict:
          """Execute tool, return result"""
          pass
  ```
- **Verification:** Interface is clean, extensible

#### Task 11.2: Implement bash Tool (30 minutes)
- **File:** `bridge/tools/bash.py`
- **Model:** `deepinfra:V4-Flash-0731`
- **Logic:**
  ```python
  class BashTool(CustomTool):
      name = "bash"
      description = "Execute bash command"
      
      def execute(self, params: dict) -> dict:
          command = params.get("command")
          result = subprocess.run(
              command,
              shell=True,
              capture_output=True,
              text=True
          )
          return {
              "stdout": result.stdout,
              "stderr": result.stderr,
              "exit_code": result.returncode
          }
  ```
- **Verification:** Tool executes commands correctly

#### Task 11.3: Implement read Tool (30 minutes)
- **File:** `bridge/tools/read.py`
- **Model:** `deepinfra:V4-Flash-0731`
- **Logic:**
  ```python
  class ReadTool(CustomTool):
      name = "read"
      description = "Read file content"
      
      def execute(self, params: dict) -> dict:
          path = params.get("path")
          with open(path, 'r') as f:
              content = f.read()
          return {"content": content}
  ```
- **Verification:** Tool reads files correctly

#### Task 11.4: Implement write Tool (30 minutes)
- **File:** `bridge/tools/write.py`
- **Model:** `deepinfra:V4-Flash-0731`
- **Logic:**
  ```python
  class WriteTool(CustomTool):
      name = "write"
      description = "Write file content"
      
      def execute(self, params: dict) -> dict:
          path = params.get("path")
          content = params.get("content")
          with open(path, 'w') as f:
              f.write(content)
          return {"success": True}
  ```
- **Verification:** Tool writes files correctly

#### Task 11.5: Test Custom Tools (30 minutes)
- **Actions:**
  - Test bash tool
  - Test read tool
  - Test write tool
  - Test tool integration with bridge
- **Verification:** All tools work correctly

**Deliverables:**
- Custom tool interface
- 3 basic tools (bash, read, write)
- Tool integration tests

**Risks:**
- Security (bash tool) → implement sandboxing
- File permissions (read/write) → validate paths
- Error handling → add try-catch blocks

---

## Summary

### Total Effort

| Phase | Duration | Description |
|-------|----------|-------------|
| Phase 9 | 4 hours | Harness integration |
| Phase 10 | 3 hours | Hybrid routing |
| Phase 11 | 2 hours | Custom tools for bridge |
| **Total** | **9 hours** | **1 day** |

### Cost Analysis

**Before Harness Integration:**
- LLM reasoning: Paid APIs (100%)
- Tools: None (manual execution)
- **Cost: 100% paid**

**After Harness Integration:**
- LLM reasoning: Free bridges (90%) + Paid fallback (10%)
- Tools: 40+ local tools (100% free)
- **Cost: 10% paid (90% savings)**

### Success Criteria

- ✅ Harness integrated with router
- ✅ 40+ tools available via Harness
- ✅ Hybrid routing works correctly
- ✅ Custom tools for simple tasks
- ✅ 90% cost reduction achieved
- ✅ Performance benchmarks documented

### Dependencies

- **Must complete first:** Phase 1-5 (router + bridges), Phase 6-8 (plugins)
- **Can start after:** Plugin system is stable

### Future Enhancements (Not in Scope)

- More custom tools (web_search, grep, etc.)
- Advanced Harness features (workflows, subagents)
- Tool marketplace (community contributions)
- Tool performance profiling
- Multi-tenant tool isolation

---

## Conclusion

DeepSeek Harness integration provides a powerful agent runtime with 40+ local tools while using free API bridges for LLM reasoning. The hybrid approach ensures:

1. **Cost efficiency**: 90% free, 10% paid
2. **Flexibility**: Complex tasks use Harness, simple tasks use bridge
3. **Extensibility**: 40+ tools available immediately
4. **Performance**: Optimal routing based on task complexity
5. **Maintainability**: Clear separation of concerns

The phased approach allows gradual integration, testing each component before moving to the next, ensuring a stable and reliable system.
