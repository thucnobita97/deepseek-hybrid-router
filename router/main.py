"""FastAPI application entry point for the DeepSeek Hybrid Router."""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, List, Tuple, Optional

from dotenv import load_dotenv
load_dotenv()  # Load .env file before any other imports that need env vars

from fastapi import FastAPI
from fastapi.responses import Response, JSONResponse, StreamingResponse, HTMLResponse

# Import models from shared module to avoid circular imports
from router.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Message,
    ToolCall,
    ToolDefinition,
)
from router.routing import Router, _parse_provider_model
from router.fallback import FallbackManager
from router.session import sessions_router, get_session_manager
from router.cost import get_cost_tracker
from router.analyzer import RequestType
from router.multi_account import AccountManager
from router.metrics import get_metrics
from router.logging_config import setup_structured_logging

# Session Manager imports (Phase 4 integration)
from router.task_splitter import TaskSizeDetector, HybridTaskSplitter, TaskAnalysis
from router.session_pool import SessionPool, PoolConfig
from router.parallel_executor import ParallelExecutor
from router.context_manager import ContextManager
from router.result_aggregator import ResultAggregator, HybridResultAggregator
from router.adaptive_manager import AdaptiveManager, PerformanceTracker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session Manager configuration constants
# ---------------------------------------------------------------------------
_SESSION_MGR_ENABLED = True   # Can be made configurable later via env/config
_TOKEN_THRESHOLD = 4000        # Token threshold above which tasks are split

# Re-export for backward compatibility
__all__ = [
    "app",
    "ChatCompletionRequest",
    "ChatCompletionResponse",
    "Message",
    "ToolCall",
    "ToolDefinition",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle — initializes and tears down the Router."""
    # Startup: attach structured JSON logging
    _log_handler = setup_structured_logging()

    router = Router.get_instance()
    logger.info("Router initialized: %s", router)
    app.state.router = router

    # Start session background refresh
    session_mgr = get_session_manager()
    await session_mgr.start_background_refresh()

    # Initialize multi-account manager and start recovery
    acct_mgr = AccountManager.get_instance()
    acct_mgr.start_recovery()
    app.state.account_manager = acct_mgr

    # Initialize Session Manager components (Phase 4)
    pool_config = PoolConfig()
    session_pool = SessionPool(config=pool_config)
    perf_tracker = PerformanceTracker()
    adaptive_mgr = AdaptiveManager(pool_config=pool_config, tracker=perf_tracker)
    
    # Task detection and splitting
    task_detector = TaskSizeDetector(token_threshold=_TOKEN_THRESHOLD)
    splitter = HybridTaskSplitter()
    executor = ParallelExecutor(session_pool=session_pool)
    
    # Store in app state
    app.state.session_pool = session_pool
    app.state.perf_tracker = perf_tracker
    app.state.adaptive_mgr = adaptive_mgr
    app.state.task_detector = task_detector
    app.state.splitter = splitter
    app.state.executor = executor
    
    logger.info("Session Manager initialized: threshold=%d tokens", _TOKEN_THRESHOLD)

    yield

    # Shutdown: remove structured logging handler first, then tear down services
    logging.getLogger("router").removeHandler(_log_handler)
    acct_mgr.stop_recovery()
    AccountManager.reset_instance()
    await session_mgr.close()
    await router.close()
    Router.reset_instance()
    logger.info("Router closed")


app = FastAPI(title="DeepSeek Hybrid Router", version="0.1.0", lifespan=lifespan)

# Register session management endpoints
app.include_router(sessions_router)


@app.get("/healthz")
async def health_check():
    return {"status": "healthy", "timestamp": int(time.time())}


@app.get("/v1/models")
async def list_models():
    """OpenAI-compatible /v1/models endpoint for client discovery."""
    router: Router = app.state.router
    from router.analyzer import RequestType

    seen = {}
    for rt in RequestType:
        try:
            info = router.get_route_info(rt)
            for spec_str in [info.get("primary", "")] + (info.get("fallbacks") or []):
                if not spec_str:
                    continue
                provider, model = _parse_provider_model(spec_str)
                key = f"{provider}/{model}"
                if key not in seen:
                    seen[key] = {
                        "id": f"model-hybrid-nobita:{key}",
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": provider,
                    }
        except Exception:
            continue

    # Always expose a generic "auto" entry that routes by request type
    seen["auto"] = {
        "id": "model-hybrid-nobita:auto",
        "object": "model",
        "created": int(time.time()),
        "owned_by": "hybrid-router",
    }

    return {"object": "list", "data": list(seen.values())}


@app.get("/v1/bridge/health")
async def bridge_health():
    """Check if the free DeepSeek bridge is up and usable."""
    import httpx
    router: Router = app.state.router
    bridge_url = os.getenv("BRIDGE_URL", "http://localhost:8002").rstrip("/")
    
    status = {"bridge_url": bridge_url, "available": False, "last_check": int(time.time())}
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Try common health endpoints
            for path in ["/healthz", "/health", "/"]:
                try:
                    resp = await client.get(f"{bridge_url}{path}")
                    if resp.status_code < 500:
                        status["available"] = True
                        status["status_code"] = resp.status_code
                        status["endpoint"] = path
                        break
                except Exception:
                    continue
    except Exception as exc:
        status["error"] = str(exc)
    
    # Also include routing info to show current state
    try:
        info = {}
        from router.analyzer import RequestType
        for rt in RequestType:
            route_info = router.get_route_info(rt)
            info[rt.value] = {
                "primary": route_info.get("primary"),
                "bridge_is_primary": "deepseek-bridge" in (route_info.get("primary", "") or ""),
            }
        status["routing"] = info
    except Exception:
        pass
    
    return status


def _extract_text(content) -> str:
    """Extract text from message content (str or list of content blocks)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # List of content blocks
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return " ".join(parts)


def _build_fallback_chain(
    router: Router,
    primary_adapter: Any,
    primary_model: str,
    request_type: RequestType,
) -> List[Tuple[Any, str]]:
    """Build an ordered fallback chain: [(primary_adapter, model), (fallback_adapter, model), ...]."""
    chain = [(primary_adapter, primary_model)]
    
    # Get fallback specs from routing config
    fallback_specs = router.get_fallback_chain(request_type)
    for spec in fallback_specs:
        try:
            provider, model = _parse_provider_model(spec)
            adapter = router._get_adapter(provider)
            chain.append((adapter, model))
        except Exception as exc:
            logger.warning("Skipping fallback %s: %s", spec, exc)
            continue
    
    return chain


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """
    OpenAI-compatible chat completions endpoint.
    Routes the request to the appropriate provider adapter with fallback chain.
    """
    # Generate short request ID for tracing
    request_id = str(uuid.uuid4())[:8]
    
    router: Router = app.state.router

    # Route the request — get primary adapter
    adapter, model, request_type = await router.route(request.model_dump())
    
    provider_name = type(adapter).__name__.replace("Adapter", "").lower()
    
    logger.info(
        "Routing: type=%s adapter=%s model=%s stream=%s",
        request_type.value,
        type(adapter).__name__,
        model,
        request.stream,
        extra={
            "request_id": request_id,
            "provider": provider_name,
            "model": model,
        },
    )

    # Build fallback chain: primary + configured fallbacks
    chain = _build_fallback_chain(router, adapter, model, request_type)
    
    # Create FallbackManager for this request
    fb_manager = FallbackManager(chain=chain)
    
    # Prepare request dict for adapters
    request_dict = request.model_dump()
    request_dict["model"] = model  # Use the mapped model name
    
    # Session Manager: check if task needs splitting (non-streaming only)
    if _SESSION_MGR_ENABLED and not request.stream:
        try:
            detector = app.state.task_detector
            analysis = detector.analyze(request_dict)
            
            if analysis.needs_splitting:
                logger.info(
                    "Session Manager: task needs splitting (tokens=%d, complexity=%s, suggested=%d)",
                    analysis.estimated_tokens, analysis.complexity, analysis.suggested_split_count
                )
                
                # Use session manager flow
                splitter = app.state.splitter
                sub_tasks = await splitter.split(request_dict, analysis)
                
                if len(sub_tasks) > 1:
                    logger.info("Session Manager: split into %d sub-tasks", len(sub_tasks))
                    
                    # Execute via session manager
                    executor = app.state.executor
                    ctx_mgr = ContextManager()
                    
                    # Prepare sub-tasks with context
                    for task in sub_tasks:
                        await ctx_mgr.prepare_sub_task(task)
                    
                    # Get adapter for bridge
                    bridge_adapter = router._get_adapter('deepseek-bridge')
                    bridge_model = 'deepseek-chat'
                    
                    # Execute sub-tasks
                    start_time = time.time()
                    execution_results = await executor.execute(sub_tasks, bridge_adapter, bridge_model)
                    execution_duration = time.time() - start_time
                    
                    # Record context from each result
                    for result in execution_results:
                        await ctx_mgr.record_result(result)
                    
                    # Aggregate results
                    aggregator = HybridResultAggregator(bridge_adapter, bridge_model)
                    aggregated = await aggregator.aggregate(execution_results, sub_tasks)
                    
                    # Format response
                    result_aggregator = ResultAggregator()
                    response = result_aggregator.format_response(aggregated)
                    
                    # Track performance
                    app.state.perf_tracker.record_request(
                        duration=execution_duration,
                        success=aggregated.success_count > 0,
                        tokens=aggregated.success_count,
                        truncated=False
                    )
                    
                    logger.info(
                        "Session Manager: completed %d/%d sub-tasks in %.2fs",
                        aggregated.success_count, len(sub_tasks), execution_duration
                    )
                    
                    return response
                
        except Exception as exc:
            logger.warning(
                "Session Manager failed, falling back to normal flow: %s",
                exc, exc_info=True
            )
            # Fall through to existing flow
    
    # Handle streaming
    if request.stream:
        import json as _json

        def _sse(obj: dict) -> str:
            """Emit one SSE line as proper JSON (double-quoted, null-safe)."""
            return f"data: {_json.dumps(obj, ensure_ascii=False)}\n\n"

        async def stream_generator():
            try:
                # Execute with fallback chain
                response = await fb_manager.execute(request_dict)

                # Case 1: async generator — yield each chunk as SSE JSON
                if hasattr(response, "__aiter__"):
                    async for chunk in response:
                        if isinstance(chunk, str):
                            # Already SSE-formatted
                            yield chunk if chunk.startswith("data:") else f"data: {chunk}\n\n"
                        elif isinstance(chunk, dict):
                            yield _sse(chunk)
                    yield "data: [DONE]\n\n"
                    return

                # Case 2: dict with collected 'chunks' list (adapter buffer mode)
                if isinstance(response, dict) and "chunks" in response:
                    for chunk in response["chunks"]:
                        yield _sse(chunk)
                    yield "data: [DONE]\n\n"
                    return

                # Case 3: single non-streaming response dict — wrap as one SSE event
                if isinstance(response, dict):
                    yield _sse(response)
                    yield "data: [DONE]\n\n"
                    return

                # Fallback: unknown type, stringify
                yield f"data: {_json.dumps({'error': 'unexpected response type'})}\n\n"
                yield "data: [DONE]\n\n"

            except Exception as exc:
                logger.error("Streaming failed: %s", exc, exc_info=True)
                yield _json.dumps({"error": str(exc)}) + "\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(stream_generator(), media_type="text/event-stream")
    
    # Non-streaming: execute with fallback chain
    try:
        start_time = time.time()
        response = await fb_manager.execute(request_dict)
        duration = time.time() - start_time
        latency_ms = int(duration * 1000)
        
        # Log completion with structured context
        logger.info(
            "Request completed: provider=%s model=%s latency=%dms",
            provider_name,
            model,
            latency_ms,
            extra={
                "request_id": request_id,
                "provider": provider_name,
                "model": model,
                "latency_ms": latency_ms,
            },
        )
        
        # Record Prometheus metrics
        try:
            metrics = get_metrics()
            metrics.increment_counter(
                "router_requests_total",
                labels={"provider": provider_name, "model": model, "status": "success"}
            )
            metrics.observe_histogram(
                "router_request_duration_seconds",
                value=duration,
                labels={"provider": provider_name, "model": model}
            )
        except Exception as metrics_err:
            logger.debug("Failed to record metrics: %s", metrics_err)
        
        # Log fallback events if any occurred
        if fb_manager.events:
            logger.info(
                "Request completed with %d fallback events",
                len(fb_manager.events),
                extra={
                    "request_id": request_id,
                    "provider": provider_name,
                    "model": model,
                },
            )
        
        # Track performance for non-session-manager requests
        tokens = 0
        truncated = False
        if isinstance(response, dict) and "usage" in response:
            usage = response["usage"]
            tokens = usage.get("total_tokens", 0)
            # Check if truncated (finish_reason == 'length')
            choices = response.get("choices", [])
            if choices and choices[0].get("finish_reason") == "length":
                truncated = True
        
        app.state.perf_tracker.record_request(
            duration=duration,
            success=True,
            tokens=tokens,
            truncated=truncated
        )
        
        # Track cost from response usage
        if isinstance(response, dict) and "usage" in response:
            usage = response["usage"]
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            
            # Determine provider from adapter name
            adapter_name = type(adapter).__name__.replace("Adapter", "").lower()
            
            cost_tracker = get_cost_tracker()
            cost_tracker.record_usage(
                provider=adapter_name,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                session_id=None  # Can be extended later
            )
            logger.debug(
                "Recorded usage: provider=%s model=%s tokens=%d+%d",
                adapter_name, model, prompt_tokens, completion_tokens
            )
        
        return response
    
    except RuntimeError as exc:
        logger.error("All providers failed: %s", exc)
        return {
            "error": {
                "message": str(exc),
                "type": "provider_unavailable",
                "code": "all_providers_failed",
            }
        }


@app.get("/v1/routing/info")
async def routing_info():
    """Expose current routing table for debugging/inspection."""
    router: Router = app.state.router
    from router.analyzer import RequestType

    info = {}
    for rt in RequestType:
        info[rt.value] = router.get_route_info(rt)
    return {"routing_table": info}


@app.post("/v1/routing/reload")
async def reload_routing_config():
    """Hot-reload routing config from disk."""
    router: Router = app.state.router
    router.reload_config()
    return {"status": "reloaded", "config_path": str(router.config_path)}


# ---------------------------------------------------------------------------
# OWUI Confirmation endpoints
# ---------------------------------------------------------------------------


@app.get("/v1/confirmation/pending")
async def list_pending_confirmations():
    """List all pending OWUI confirmation requests."""
    from router.confirmation import get_owui_confirmation
    owui = get_owui_confirmation()
    return {"pending": owui.get_pending(), "count": len(owui.get_pending())}


@app.post("/v1/confirmation/respond")
async def respond_to_confirmation(payload: Dict[str, Any]):
    """Endpoint for OWUI to post user responses to pending confirmations.

    Expects JSON body: ``{"confirmation_id": "...", "selected_option": "..."}``
    """
    from router.confirmation import get_owui_confirmation

    confirmation_id = payload.get("confirmation_id")
    selected_option = payload.get("selected_option")

    if not confirmation_id or not selected_option:
        return {
            "error": {
                "message": "Missing confirmation_id or selected_option",
                "code": "invalid_request",
            }
        }

    owui = get_owui_confirmation()
    success = owui.respond(confirmation_id, selected_option)
    if success:
        return {"status": "ok", "confirmation_id": confirmation_id}
    return {
        "error": {
            "message": f"Confirmation {confirmation_id} not found or already resolved",
            "code": "not_found",
        }
    }


# ============================================================================
# Cost Tracking Endpoints
# ============================================================================


@app.get("/v1/cost/session/{session_id}")
async def get_session_cost(session_id: str):
    """Get cost breakdown for a specific session."""
    tracker = get_cost_tracker()
    cost_data = tracker.get_session_cost(session_id)
    return cost_data


@app.get("/v1/cost/total")
async def get_total_cost():
    """Get all-time cost totals and breakdown by provider."""
    tracker = get_cost_tracker()
    cost_data = tracker.get_total_cost()
    return cost_data


@app.get("/v1/cost/budget")
async def get_budget_status(session_id: Optional[str] = None):
    """
    Check budget status.

    Returns budget status including within_budget, current_cost, threshold,
    and warning_level. Optionally filter by session_id.
    """
    tracker = get_cost_tracker()
    budget_status = tracker.check_budget(session_id)
    return budget_status


@app.get("/metrics")
async def metrics_endpoint():
    """
    Prometheus metrics endpoint.
    
    Returns metrics in Prometheus text exposition format for scraping.
    """
    metrics = get_metrics()
    return Response(
        content=metrics.generate_prometheus_text(),
        media_type="text/plain; version=0.0.4; charset=utf-8"
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def cost_dashboard():
    """
    Interactive HTML cost dashboard with auto-refresh.
    Serves a self-contained dark-themed page showing cost summary,
    provider breakdown, usage trends, top models, and budget status.
    """
    tracker = get_cost_tracker()

    # ── Gather data from CostTracker ──────────────────────────────────
    total_data = tracker.get_total_cost()
    budget_data = tracker.check_budget()

    total_cost = total_data.get("total_cost", 0.0)
    total_tokens = total_data.get("total_tokens", 0)
    total_prompt = total_data.get("total_prompt_tokens", 0)
    total_completion = total_data.get("total_completion_tokens", 0)
    breakdown = total_data.get("breakdown_by_provider", {})

    # Count requests from DB
    import sqlite3 as _sqlite
    _conn = _sqlite.connect(str(tracker.db_path))
    _cur = _conn.cursor()
    _cur.execute("SELECT COUNT(*) FROM usage_records")
    request_count = _cur.fetchone()[0] or 0
    _conn.close()

    # Budget info
    current_cost = budget_data.get("current_cost", 0.0)
    threshold = budget_data.get("threshold", 5.0)
    warning_level = budget_data.get("warning_level", "ok")
    budget_pct = (current_cost / threshold * 100) if threshold > 0 else 0

    # ── Optional new methods (from Task 1.1) ─────────────────────────
    trends_html = '<tr><td colspan="4" style="text-align:center;color:#888;">Coming soon</td></tr>'
    top_models_html = '<tr><td colspan="4" style="text-align:center;color:#888;">Coming soon</td></tr>'

    try:
        hourly = tracker.get_hourly_stats()
        if hourly:
            rows = []
            for entry in hourly:
                ts = entry.get("hour", entry.get("timestamp", ""))
                pt = entry.get("prompt_tokens", 0)
                ct = entry.get("completion_tokens", 0)
                c = entry.get("cost", 0.0)
                rows.append(
                    f"<tr><td>{ts}</td><td>{pt:,}</td><td>{ct:,}</td><td>${c:.4f}</td></tr>"
                )
            trends_html = "\n".join(rows)
    except (AttributeError, TypeError):
        pass

    try:
        top = tracker.get_top_models()
        if top:
            rows = []
            for entry in top:
                model = entry.get("model", "unknown")
                reqs = entry.get("request_count", entry.get("requests", 0))
                toks = entry.get("total_tokens", 0)
                c = entry.get("cost", 0.0)
                rows.append(
                    f"<tr><td>{model}</td><td>{reqs:,}</td><td>{toks:,}</td><td>${c:.4f}</td></tr>"
                )
            top_models_html = "\n".join(rows)
    except (AttributeError, TypeError):
        pass

    # ── Provider breakdown rows ──────────────────────────────────────
    provider_rows = ""
    for prov, info in breakdown.items():
        provider_rows += (
            f"<tr>"
            f"<td>{prov}</td>"
            f"<td>{info['prompt_tokens']:,}</td>"
            f"<td>{info['completion_tokens']:,}</td>"
            f"<td>${info['cost']:.4f}</td>"
            f"</tr>\n"
        )
    if not provider_rows:
        provider_rows = '<tr><td colspan="4" style="text-align:center;color:#888;">No data yet</td></tr>'

    # ── Budget bar colour ─────────────────────────────────────────────
    bar_color = "#22c55e" if warning_level == "ok" else ("#eab308" if warning_level == "warning" else "#ef4444")
    bar_width = min(budget_pct, 100)

    # ── Build HTML ────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="30">
<title>DeepSeek Hybrid Router - Cost Dashboard</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f1117; color: #e2e8f0; padding: 24px; min-height: 100vh;
  }}
  h1 {{ font-size: 1.6rem; font-weight: 700; margin-bottom: 24px; color: #f1f5f9; }}
  h2 {{ font-size: 1.1rem; font-weight: 600; margin-bottom: 12px; color: #cbd5e1; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 28px; }}
  .card {{
    background: #1a1d2e; border: 1px solid #2d3148; border-radius: 12px;
    padding: 20px; text-align: center;
  }}
  .card .label {{ font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; color: #94a3b8; margin-bottom: 6px; }}
  .card .value {{ font-size: 1.7rem; font-weight: 700; color: #f8fafc; }}
  .card .value.cost {{ color: #38bdf8; }}
  .section {{ background: #1a1d2e; border: 1px solid #2d3148; border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th {{ text-align: left; padding: 10px 12px; background: #252840; color: #94a3b8; font-weight: 600; text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.04em; }}
  td {{ padding: 10px 12px; border-top: 1px solid #2d3148; }}
  tr:hover td {{ background: #1f2237; }}
  .budget-wrap {{ display: flex; align-items: center; gap: 16px; margin-top: 8px; }}
  .budget-bar-bg {{ flex: 1; height: 22px; background: #252840; border-radius: 6px; overflow: hidden; }}
  .budget-bar {{ height: 100%; border-radius: 6px; transition: width 0.5s ease; }}
  .budget-label {{ font-size: 0.85rem; color: #94a3b8; white-space: nowrap; }}
  .badge {{ display: inline-block; padding: 3px 10px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; }}
  .badge-ok {{ background: #064e3b; color: #34d399; }}
  .badge-warning {{ background: #713f12; color: #fbbf24; }}
  .badge-alert {{ background: #7f1d1d; color: #f87171; }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  @media (max-width: 800px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}
  .footer {{ text-align: center; color: #475569; font-size: 0.75rem; margin-top: 28px; }}
</style>
</head>
<body>

<h1>DeepSeek Hybrid Router - Cost Dashboard</h1>

<div class="cards">
  <div class="card">
    <div class="label">Total Cost</div>
    <div class="value cost">${total_cost:.4f}</div>
  </div>
  <div class="card">
    <div class="label">Total Tokens</div>
    <div class="value">{total_tokens:,}</div>
  </div>
  <div class="card">
    <div class="label">Request Count</div>
    <div class="value">{request_count:,}</div>
  </div>
  <div class="card">
    <div class="label">Prompt / Completion</div>
    <div class="value" style="font-size:1.1rem;">{total_prompt:,} / {total_completion:,}</div>
  </div>
</div>

<div class="section">
  <h2>Budget Status
    <span class="badge badge-{warning_level}">{warning_level.upper()}</span>
  </h2>
  <div class="budget-wrap">
    <div class="budget-bar-bg">
      <div class="budget-bar" style="width:{bar_width:.1f}%; background:{bar_color};"></div>
    </div>
    <div class="budget-label">${current_cost:.4f} / ${threshold:.2f} ({budget_pct:.1f}%)</div>
  </div>
</div>

<div class="section">
  <h2>Provider Breakdown</h2>
  <table>
    <thead>
      <tr><th>Provider</th><th>Prompt Tokens</th><th>Completion Tokens</th><th>Cost</th></tr>
    </thead>
    <tbody>
      {provider_rows}
    </tbody>
  </table>
</div>

<div class="grid-2">
  <div class="section">
    <h2>Usage Trends (Hourly)</h2>
    <table>
      <thead>
        <tr><th>Hour</th><th>Prompt Tokens</th><th>Completion Tokens</th><th>Cost</th></tr>
      </thead>
      <tbody>
        {trends_html}
      </tbody>
    </table>
  </div>
  <div class="section">
    <h2>Top Models</h2>
    <table>
      <thead>
        <tr><th>Model</th><th>Requests</th><th>Total Tokens</th><th>Cost</th></tr>
      </thead>
      <tbody>
        {top_models_html}
      </tbody>
    </table>
  </div>
</div>

<div class="footer">Auto-refreshes every 30 seconds</div>

</body>
</html>"""
    return html


@app.get("/v1/cost/export/csv")
async def export_usage_csv(days: int = 7):
    """
    Export usage records as a downloadable CSV file.

    Args:
        days: Number of days to look back (default: 7).

    Returns:
        CSV file download with Content-Disposition header.
    """
    tracker = get_cost_tracker()
    csv_data = tracker.export_usage_data(days=days, format='csv')
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=usage_{days}d.csv"},
    )


@app.get("/v1/cost/export/json")
async def export_usage_json(days: int = 7):
    """
    Export usage records as a downloadable JSON array.

    Args:
        days: Number of days to look back (default: 7).

    Returns:
        JSONResponse with the array of usage records.
    """
    tracker = get_cost_tracker()
    records = tracker.export_usage_data(days=days, format='json')
    return JSONResponse(
        content=records,
        headers={"Content-Disposition": f"attachment; filename=usage_{days}d.json"},
    )


@app.get("/v1/cost/trends")
async def get_usage_trends(hours: int = 24):
    """
    Get hourly usage trends for the last N hours.

    Returns a time-series of usage data points bucketed by hour,
    including request count, token totals, and cost.
    """
    tracker = get_cost_tracker()
    return tracker.get_usage_trends(hours)


@app.get("/v1/cost/stats/hourly")
async def get_hourly_stats():
    """
    Get usage statistics for the last 24 hours aggregated by hour.

    Returns per-hour breakdown with a summary of total requests,
    tokens, and cost across the entire period.
    """
    tracker = get_cost_tracker()
    return tracker.get_hourly_stats()


@app.get("/v1/cost/stats/daily")
async def get_daily_stats(days: int = 7):
    """
    Get usage statistics for the last N days aggregated by day.

    Returns per-day breakdown with a summary of total requests,
    tokens, and cost across the entire period.
    """
    tracker = get_cost_tracker()
    return tracker.get_daily_stats(days)


@app.get("/v1/cost/top-models")
async def get_top_models(limit: int = 10):
    """
    Get the top N most-used models ranked by total token count.

    Returns a ranked list of models with request count, token
    breakdown, and cost for each.
    """
    tracker = get_cost_tracker()
    return tracker.get_top_models(limit)


@app.get("/v1/cost/providers/usage")
async def get_provider_usage_trends(hours: int = 24):
    """
    Get usage breakdown by provider over the last N hours.

    Returns per-provider hourly time-series data and summary
    totals for each provider.
    """
    tracker = get_cost_tracker()
    return tracker.get_provider_usage_trends(hours)


# ============================================================================
# Multi-Account Endpoints
# ============================================================================


@app.get("/v1/accounts")
async def list_accounts():
    """List all registered bridge accounts."""
    mgr: AccountManager = app.state.account_manager
    return {"accounts": mgr.get_account_health()}


@app.post("/v1/accounts")
async def create_account(body: Dict[str, Any]):
    """Register a new bridge account.

    Body: {"account_id": "...", "bridge_url": "...", "session_data": {...}}
    """
    mgr: AccountManager = app.state.account_manager
    account_id = body.get("account_id")
    bridge_url = body.get("bridge_url")
    if not account_id or not bridge_url:
        return {"error": "account_id and bridge_url are required"}
    session_data = body.get("session_data", {})
    added = mgr.add_account(account_id, bridge_url, session_data)
    if not added:
        return {"error": f"account {account_id} already exists"}
    return {"status": "created", "account_id": account_id}


@app.delete("/v1/accounts/{account_id}")
async def delete_account(account_id: str):
    """Remove a bridge account."""
    mgr: AccountManager = app.state.account_manager
    removed = mgr.remove_account(account_id)
    if not removed:
        return {"error": f"account {account_id} not found"}
    return {"status": "deleted", "account_id": account_id}


@app.get("/v1/accounts/health")
async def accounts_health():
    """Return health status for all registered accounts."""
    mgr: AccountManager = app.state.account_manager
    return {"accounts": mgr.get_account_health()}


# ============================================================================
# Session Manager Endpoints (Phase 4)
# ============================================================================


@app.get("/v1/session-manager/stats")
async def session_manager_stats():
    """Return session pool stats, performance metrics, and adaptive recommendations."""
    try:
        pool_stats = await app.state.session_pool.get_pool_stats()
        perf_metrics = app.state.perf_tracker.get_metrics()
        recent_metrics = app.state.perf_tracker.get_recent_metrics(window=20)
        recommendations = app.state.adaptive_mgr.get_recommendations()

        return {
            "pool": pool_stats,
            "performance": {
                "truncation_rate": perf_metrics.truncation_rate,
                "avg_response_time": perf_metrics.avg_response_time,
                "success_rate": perf_metrics.success_rate,
                "avg_tokens_per_response": perf_metrics.avg_tokens_per_response,
                "session_rotation_count": perf_metrics.session_rotation_count,
                "error_rate": perf_metrics.error_rate,
            },
            "recent_performance": {
                "truncation_rate": recent_metrics.truncation_rate,
                "avg_response_time": recent_metrics.avg_response_time,
                "success_rate": recent_metrics.success_rate,
                "error_rate": recent_metrics.error_rate,
            },
            "recommendations": recommendations,
            "enabled": _SESSION_MGR_ENABLED,
        }
    except Exception as exc:
        logger.error("Failed to get session manager stats: %s", exc)
        return {"error": str(exc)}


@app.get("/v1/session-manager/config")
async def session_manager_config():
    """Return current session manager settings."""
    pool_config = app.state.adaptive_mgr.config
    return {
        "enabled": _SESSION_MGR_ENABLED,
        "token_threshold": _TOKEN_THRESHOLD,
        "pool_config": {
            "max_sessions": pool_config.max_sessions,
            "max_messages_per_session": pool_config.max_messages_per_session,
            "session_ttl": pool_config.session_ttl,
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
