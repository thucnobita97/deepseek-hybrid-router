# Model Hybrid Nobita - Master Plan

## Overview

Comprehensive plan to build an extensible, cost-efficient hybrid AI router with multi-bridge support, plugin architecture, and DeepSeek Harness integration.

**Total Scope:** 11 phases, 35.5 hours (~4.5 days)

## Plan Documents

1. **[plan-model-hybrid-nobita.md](plan-model-hybrid-nobita.md)** - Core Router + Multi-Bridge System
   - Phase 1-5: 10.5 hours
   - Focus: Rename, Copilot integration, smart routing, Docker migration

2. **[plan-plugin-architecture.md](plan-plugin-architecture.md)** - Extensible Plugin System
   - Phase 6-8: 16 hours
   - Focus: Plugin interface, registry, example plugins, ecosystem tools

3. **[plan-harness-integration.md](plan-harness-integration.md)** - DeepSeek Harness Agent Runtime
   - Phase 9-11: 9 hours
   - Focus: Harness integration, hybrid routing, custom tools

## Execution Roadmap

### Stage 1: Foundation (Phase 1-5)
**Duration:** 10.5 hours (1.5 days)  
**Goal:** Build core router with DeepSeek + Copilot bridges

| Phase | Task | Duration | Status |
|-------|------|----------|--------|
| 1 | Rename to model-hybrid-nobita | 30 min | ⏳ Pending |
| 2 | Copilot Bridge Setup | 1.5 hours | ⏳ Pending |
| 3 | Integrate Copilot into Router | 1 hour | ⏳ Pending |
| 4 | Benchmark & Auto-Routing | 3 hours | ⏳ Pending |
| 5 | Docker Migration | 4 hours | ⏳ Pending |

**Deliverables:**
- ✅ Renamed system (model-hybrid-nobita)
- ✅ DeepSeek bridge (port 8002)
- ✅ Copilot bridge (port 8003)
- ✅ Smart auto-routing based on benchmarks
- ✅ Docker containerization
- ✅ Portable setup script

**Success Criteria:**
- Router routes to DeepSeek/Copilot based on request type
- Fallback to paid APIs when bridges fail
- Docker setup works on fresh WSL/VM

---

### Stage 2: Extensibility (Phase 6-8)
**Duration:** 16 hours (2 days)  
**Goal:** Build plugin architecture for easy addition of new bridges

| Phase | Task | Duration | Status |
|-------|------|----------|--------|
| 6 | Plugin Architecture Foundation | 9 hours | ⏳ Pending |
| 7 | Example Plugins | 4 hours | ⏳ Pending |
| 8 | Plugin Ecosystem | 3 hours | ⏳ Pending |

**Deliverables:**
- ✅ Plugin interface (BridgePlugin ABC)
- ✅ Plugin registry (discovery, loading, lifecycle)
- ✅ Migrated DeepSeek/Copilot to plugins
- ✅ 3 example plugins (Claude, Gemini, Local LLM)
- ✅ Plugin CLI tool (list, enable, disable, install)
- ✅ Plugin testing framework
- ✅ Community contribution guidelines

**Success Criteria:**
- New plugins can be added without modifying core code
- Plugin CLI manages plugins easily
- Community can contribute plugins following guidelines

---

### Stage 3: Agent Runtime (Phase 9-11)
**Duration:** 9 hours (1 day)  
**Goal:** Integrate DeepSeek Harness for 40+ local tools

| Phase | Task | Duration | Status |
|-------|------|----------|--------|
| 9 | Harness Integration | 4 hours | ⏳ Pending |
| 10 | Hybrid Routing | 3 hours | ⏳ Pending |
| 11 | Custom Tools for Bridge | 2 hours | ⏳ Pending |

**Deliverables:**
- ✅ Harness integrated as agent runtime
- ✅ 40+ local tools available (shell, fs, web, code, LSP, subagents)
- ✅ Hybrid routing (complex tasks → Harness, simple tasks → Bridge)
- ✅ 3 custom tools (bash, read, write)
- ✅ Performance benchmarks
- ✅ Integration documentation

**Success Criteria:**
- Harness uses free bridges for LLM reasoning
- Tools execute locally (no API cost)
- 90% cost reduction achieved
- Complex tasks work via Harness, simple tasks via bridge

---

## Cost Analysis

### Before Implementation
- **LLM API:** 100% paid (DeepInfra, Alibaba, OpenAI)
- **Tools:** None (manual execution)
- **Estimated cost:** $50-100/month

### After Stage 1 (Phase 1-5)
- **LLM API:** 70% free (DeepSeek + Copilot), 30% paid fallback
- **Tools:** None
- **Estimated cost:** $15-30/month (70% savings)

### After Stage 3 (Phase 9-11)
- **LLM API:** 90% free (bridges), 10% paid fallback
- **Tools:** 40+ local tools (100% free)
- **Estimated cost:** $5-10/month (90% savings)

---

## Architecture Evolution

### Stage 1: Multi-Bridge Router
```
User → Router (8001) → DeepSeek Bridge (8002)
                     → Copilot Bridge (8003)
                     → Paid APIs (fallback)
```

### Stage 2: Plugin System
```
User → Router → Plugin Registry → DeepSeek Plugin
                                → Copilot Plugin
                                → Claude Plugin (future)
                                → Gemini Plugin (future)
                                → Local LLM Plugin (future)
```

### Stage 3: Agent Runtime
```
User → Router → Complexity Detector → Simple: Bridge + Custom Tools
                                    → Complex: Harness + 40+ Tools
                                                    ↓
                                              LLM via Router (free bridges)
```

---

## Dependencies & Prerequisites

### Stage 1 Dependencies
- ✅ DeepSeek API fork cloned and working
- ✅ Windows Copilot API fork ready
- ✅ Microsoft account for Copilot login
- ✅ Docker installed
- ✅ WSL2 environment

### Stage 2 Dependencies
- ✅ Stage 1 complete and stable
- ✅ Python 3.11+ for plugin development
- ✅ Git for plugin installation

### Stage 3 Dependencies
- ✅ Stage 2 complete
- ✅ Node.js 20+ for Harness build
- ✅ pnpm package manager
- ✅ 8GB+ RAM for Harness runtime

---

## Risk Assessment

### Stage 1 Risks
| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Copilot login fails | Medium | High | Document troubleshooting steps |
| Bridge rate limits | High | Medium | Implement queuing and caching |
| Docker networking issues | Low | Medium | Test thoroughly before deployment |

### Stage 2 Risks
| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Plugin interface too rigid | Medium | High | Design flexible ABC, iterate |
| Plugin loading performance | Low | Medium | Cache loaded plugins |
| Community adoption | Medium | Low | Provide excellent documentation |

### Stage 3 Risks
| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Harness build fails | Medium | High | Provide pre-built Docker image |
| Rate limit exhaustion | High | High | Implement smart queuing |
| Complexity detection errors | Medium | Medium | Start simple, iterate |

---

## Success Metrics

### Stage 1 Metrics
- [ ] Router uptime > 99%
- [ ] Bridge success rate > 90%
- [ ] Average latency < 2 seconds
- [ ] Docker setup works on 3 different machines

### Stage 2 Metrics
- [ ] 5+ plugins available
- [ ] Plugin load time < 100ms
- [ ] 2+ community-contributed plugins
- [ ] Plugin test coverage > 80%

### Stage 3 Metrics
- [ ] 40+ tools available via Harness
- [ ] 90% cost reduction achieved
- [ ] Complex task success rate > 85%
- [ ] Hybrid routing accuracy > 90%

---

## Timeline

### Week 1: Foundation
- Day 1-2: Phase 1-3 (Rename, Copilot setup, integration)
- Day 3-4: Phase 4 (Benchmark & routing)
- Day 5: Phase 5 (Docker migration)

### Week 2: Extensibility
- Day 1-3: Phase 6 (Plugin architecture)
- Day 4: Phase 7 (Example plugins)
- Day 5: Phase 8 (Ecosystem tools)

### Week 3: Agent Runtime
- Day 1-2: Phase 9 (Harness integration)
- Day 3: Phase 10 (Hybrid routing)
- Day 4: Phase 11 (Custom tools)
- Day 5: Testing & documentation

**Total: 3 weeks (15 working days)**

---

## Resource Allocation

### Models by Task Type
| Task Type | Model | Reason |
|-----------|-------|--------|
| Architecture design | `alibaba:qwen3.7-max` | Complex reasoning |
| Code generation | `deepinfra:V4-Flash-0731` | Fast, good quality |
| Simple tasks | `alibaba:qwen3.8-flash` | Fast, cheap |
| Refactoring | `alibaba:qwen3.7-max` | Complex logic |
| Documentation | `alibaba:qwen3.8-flash` | Fast, clear |

### Subagent Strategy
| Phase | Tasks | Can Parallelize? |
|-------|-------|------------------|
| 1-5 | Mostly sequential | Limited (some testing) |
| 6-8 | Some parallel | Yes (plugin examples) |
| 9-11 | Sequential | No (dependencies) |

---

## Maintenance & Evolution

### Post-Implementation Tasks
1. **Monitoring:** Set up Prometheus + Grafana for metrics
2. **Logging:** Centralized logging with ELK stack
3. **Alerting:** Slack/Discord alerts for failures
4. **Backup:** Automated backup of configs and sessions
5. **Updates:** Monthly review of new free APIs to add as plugins

### Future Enhancements (Out of Scope)
- Plugin marketplace / registry
- Automated plugin discovery from GitHub
- Multi-tenant isolation
- Advanced caching (semantic similarity)
- Federated learning across instances
- Mobile app for monitoring

---

## Conclusion

This master plan provides a comprehensive roadmap to build a cost-efficient, extensible, and powerful hybrid AI system. The phased approach ensures:

1. **Incremental value:** Each stage delivers tangible benefits
2. **Risk mitigation:** Test thoroughly before moving to next stage
3. **Flexibility:** Can pause at any stage if requirements change
4. **Community growth:** Plugin system enables ecosystem expansion
5. **Cost optimization:** 90% cost reduction with 40+ free tools

**Total Investment:** 35.5 hours (4.5 days)  
**Expected ROI:** 90% cost reduction + 40+ tools + extensible platform

---

## Next Steps

1. **Review this master plan** with stakeholders
2. **Prioritize stages** based on business needs
3. **Allocate resources** (time, compute, budget)
4. **Begin Stage 1** (Phase 1: Rename system)
5. **Track progress** using success metrics

---

## Appendix: Quick Reference

### Key Files
- `docs/plan-model-hybrid-nobita.md` - Core router plan
- `docs/plan-plugin-architecture.md` - Plugin system plan
- `docs/plan-harness-integration.md` - Harness integration plan
- `docs/MASTER_PLAN.md` - This document

### Key Commands
```bash
# Start router
docker-compose up -d

# Check bridge health
curl http://localhost:8001/v1/bridge/health

# List plugins
python scripts/plugin_manager.py list

# Run Harness agent
dsh --profile headless "your task here"
```

### Support & Resources
- Documentation: `docs/` directory
- Issues: GitHub repository
- Community: Discord server (future)
