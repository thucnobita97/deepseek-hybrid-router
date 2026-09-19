"""FastAPI application entry point for the DeepSeek Hybrid Router."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, List, Tuple, Optional

from dotenv import load_dotenv
load_dotenv()  # Load .env file before any other imports that need env vars

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

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
    # Startup
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

    # Shutdown
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
    router: Router = app.state.router

    # Route the request — get primary adapter
    adapter, model, request_type = await router.route(request.model_dump())
    
    logger.info(
        "Routing: type=%s adapter=%s model=%s stream=%s",
        request_type.value,
        type(adapter).__name__,
        model,
        request.stream,
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
        async def stream_generator():
            try:
                # Execute with fallback chain
                response = await fb_manager.execute(request_dict)
                
                # If response is an async generator (streaming), yield chunks
                if hasattr(response, "__aiter__"):
                    async for chunk in response:
                        if isinstance(chunk, str):
                            yield chunk
                        elif isinstance(chunk, dict):
                            yield f"data: {chunk}\n\n"
                else:
                    # Non-streaming response, wrap in SSE format
                    yield f"data: {response}\n\n"
                    yield "data: [DONE]\n\n"
            except Exception as exc:
                logger.error("Streaming failed: %s", exc)
                yield f"data: {{\"error\": \"{str(exc)}\"}}\n\n"
        
        return StreamingResponse(stream_generator(), media_type="text/event-stream")
    
    # Non-streaming: execute with fallback chain
    try:
        start_time = time.time()
        response = await fb_manager.execute(request_dict)
        duration = time.time() - start_time
        
        # Log fallback events if any occurred
        if fb_manager.events:
            logger.info(
                "Request completed with %d fallback events",
                len(fb_manager.events),
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
