"""OWUI (Open WebUI) dialog-based confirmation for routing decisions.

Sends confirmation requests to OWUI via HTTP webhook/callback and waits for
user responses. Includes a simulation mode for development without a live
OWUI instance.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class ConfirmationStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


@dataclass
class ConfirmationRequest:
    """A pending confirmation request sent to OWUI."""

    id: str
    prompt_text: str
    options: List[str]
    default_option: str
    status: ConfirmationStatus = ConfirmationStatus.PENDING
    response: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    responded_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "prompt_text": self.prompt_text,
            "options": self.options,
            "default_option": self.default_option,
            "status": self.status.value,
            "response": self.response,
            "created_at": self.created_at,
            "responded_at": self.responded_at,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Auto-route request types (no confirmation needed)
# ---------------------------------------------------------------------------

_AUTO_ROUTE_TYPES = {"chat", "search"}
_CONFIRMATION_REQUIRED_TYPES = {"tool_calling", "vision", "reasoning"}


# ---------------------------------------------------------------------------
# OWUIConfirmation
# ---------------------------------------------------------------------------


class OWUIConfirmation:
    """Manages user confirmation dialogs via Open WebUI.

    Parameters
    ----------
    owui_webhook_url:
        URL to POST confirmation requests to (OWUI extension endpoint).
    owui_response_url:
        URL to poll for responses, or ``None`` to rely on the ``/respond``
        endpoint being called back directly.
    timeout:
        Seconds to wait for a response before falling back to default.
    simulation_mode:
        When ``True``, auto-confirms with the default option after logging
        the request. Useful for development without a live OWUI instance.
    poll_interval:
        Seconds between polling attempts when using ``owui_response_url``.
    """

    def __init__(
        self,
        owui_webhook_url: str = "http://localhost:8080/api/v1/confirmation/webhook",
        owui_response_url: Optional[str] = None,
        timeout: int = 30,
        simulation_mode: bool = True,
        poll_interval: float = 1.0,
    ) -> None:
        self.owui_webhook_url = owui_webhook_url
        self.owui_response_url = owui_response_url
        self.timeout = timeout
        self.simulation_mode = simulation_mode
        self.poll_interval = poll_interval

        # In-memory store of pending confirmations
        self._pending: Dict[str, ConfirmationRequest] = {}
        self._events: Dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ask(
        self,
        prompt_text: str,
        options: List[str],
        timeout: Optional[int] = None,
        default_option: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Send a confirmation dialog to OWUI and wait for the user's response.

        Parameters
        ----------
        prompt_text:
            The question/prompt to display in the OWUI dialog.
        options:
            List of selectable options (e.g. ``["Yes", "No"]``).
        timeout:
            Override the default timeout for this request.
        default_option:
            Option to return on timeout. Defaults to ``options[0]``.
        metadata:
            Extra context attached to the confirmation request.

        Returns
        -------
        str
            The selected option, or the default on timeout.
        """
        wait_timeout = timeout if timeout is not None else self.timeout
        default = default_option or (options[0] if options else "Yes")

        req = ConfirmationRequest(
            id=str(uuid.uuid4()),
            prompt_text=prompt_text,
            options=options,
            default_option=default,
            metadata=metadata or {},
        )
        event = asyncio.Event()
        self._pending[req.id] = req
        self._events[req.id] = event

        logger.info(
            "OWUI confirmation [%s]: %s (options=%s, default=%s, timeout=%ds, sim=%s)",
            req.id[:8],
            prompt_text,
            options,
            default,
            wait_timeout,
            self.simulation_mode,
        )

        try:
            if self.simulation_mode:
                return await self._simulate(req, event, wait_timeout)
            return await self._ask_live(req, event, wait_timeout)
        finally:
            # Cleanup
            self._pending.pop(req.id, None)
            self._events.pop(req.id, None)

    def should_ask(
        self,
        request_type: str,
        provider: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Determine whether a routing decision needs user confirmation.

        Returns ``True`` when the request type is sensitive enough to warrant
        a confirmation dialog (tool calling, vision, reasoning).

        Parameters
        ----------
        request_type:
            The classified request type (e.g. ``"tool_calling"``).
        provider:
            The primary provider selected for routing.
        context:
            Additional context (unused currently, reserved for future rules).
        """
        ctx = context or {}

        # Explicit override from context
        if ctx.get("force_confirm"):
            return True
        if ctx.get("auto_route"):
            return False

        # Auto-route safe request types
        if request_type in _AUTO_ROUTE_TYPES:
            return False

        # Require confirmation for sensitive types
        if request_type in _CONFIRMATION_REQUIRED_TYPES:
            return True

        # Unknown types: ask for confirmation
        return True

    async def confirm_routing(
        self,
        request_type: str,
        primary_provider: str,
        fallback_provider: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """High-level routing confirmation.

        If the request type is auto-routed, returns ``primary_provider``
        immediately. Otherwise sends a confirmation dialog to OWUI.

        Parameters
        ----------
        request_type:
            Classified request type string.
        primary_provider:
            The primary provider name (e.g. ``"deepseek-bridge"``).
        fallback_provider:
            The fallback provider name.
        context:
            Extra routing context.

        Returns
        -------
        str
            The confirmed provider name.
        """
        ctx = context or {}

        if not self.should_ask(request_type, primary_provider, ctx):
            logger.info(
                "Auto-routing %s -> %s (no confirmation needed)",
                request_type,
                primary_provider,
            )
            return primary_provider

        prompt = (
            f"Route {request_type} request?\n\n"
            f"Primary: {primary_provider}\n"
            f"Fallback: {fallback_provider}\n\n"
            f"Proceed with primary provider?"
        )
        options = [primary_provider, fallback_provider, "Cancel"]

        selected = await self.ask(
            prompt_text=prompt,
            options=options,
            default_option=primary_provider,
            metadata={
                "request_type": request_type,
                "primary_provider": primary_provider,
                "fallback_provider": fallback_provider,
                **(ctx.get("metadata", {})),
            },
        )

        if selected == "Cancel":
            logger.warning("Routing cancelled by user for %s", request_type)
            return primary_provider  # Fall back to primary on cancel

        logger.info("Routing confirmed: %s -> %s", request_type, selected)
        return selected

    # ------------------------------------------------------------------
    # Response handling (called by the /respond endpoint)
    # ------------------------------------------------------------------

    def respond(self, confirmation_id: str, selected_option: str) -> bool:
        """Record a user's response to a pending confirmation.

        Called by the ``/v1/confirmation/respond`` endpoint when OWUI
        posts back the user's choice.

        Returns ``True`` if the confirmation was found and updated.
        """
        req = self._pending.get(confirmation_id)
        if req is None:
            logger.warning("Unknown confirmation ID: %s", confirmation_id)
            return False

        if req.status != ConfirmationStatus.PENDING:
            logger.warning(
                "Confirmation %s already %s", confirmation_id, req.status.value
            )
            return False

        if selected_option not in req.options:
            logger.warning(
                "Invalid option %r for confirmation %s (valid: %s)",
                selected_option,
                confirmation_id[:8],
                req.options,
            )
            return False

        req.status = ConfirmationStatus.CONFIRMED
        req.response = selected_option
        req.responded_at = time.time()

        # Signal the waiting coroutine
        event = self._events.get(confirmation_id)
        if event:
            event.set()

        logger.info(
            "Confirmation %s responded: %s", confirmation_id[:8], selected_option
        )
        return True

    def get_pending(self) -> List[Dict[str, Any]]:
        """Return all pending confirmation requests as dicts."""
        return [
            req.to_dict()
            for req in self._pending.values()
            if req.status == ConfirmationStatus.PENDING
        ]

    # ------------------------------------------------------------------
    # Internal: live mode
    # ------------------------------------------------------------------

    async def _ask_live(
        self,
        req: ConfirmationRequest,
        event: asyncio.Event,
        timeout: int,
    ) -> str:
        """Send webhook and wait for response in live mode."""
        # Send the confirmation request to OWUI
        payload = {
            "confirmation_id": req.id,
            "prompt_text": req.prompt_text,
            "options": req.options,
            "default_option": req.default_option,
            "timeout": timeout,
            "metadata": req.metadata,
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.owui_webhook_url, json=payload)
                resp.raise_for_status()
                logger.info(
                    "Webhook sent to %s: status=%d",
                    self.owui_webhook_url,
                    resp.status_code,
                )
        except httpx.HTTPError as exc:
            logger.error("Failed to send webhook to OWUI: %s", exc)
            # Fall through to polling/waiting — maybe OWUI calls back anyway

        # Wait for response via event (set by respond()) or polling
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            req.status = ConfirmationStatus.TIMEOUT
            req.response = req.default_option
            req.responded_at = time.time()
            logger.warning(
                "Confirmation %s timed out after %ds, using default: %s",
                req.id[:8],
                timeout,
                req.default_option,
            )
            return req.default_option

        # If we also have a response URL, try polling as backup
        if not event.is_set() and self.owui_response_url:
            result = await self._poll_response(req.id, timeout)
            if result is not None:
                req.status = ConfirmationStatus.CONFIRMED
                req.response = result
                req.responded_at = time.time()
                return result

        return req.response or req.default_option

    async def _poll_response(
        self, confirmation_id: str, timeout: int
    ) -> Optional[str]:
        """Poll the OWUI response URL for a confirmation response."""
        if not self.owui_response_url:
            return None

        deadline = time.time() + timeout
        async with httpx.AsyncClient(timeout=5) as client:
            while time.time() < deadline:
                try:
                    resp = await client.get(
                        self.owui_response_url,
                        params={"confirmation_id": confirmation_id},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("status") == "confirmed":
                            return data.get("response")
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(self.poll_interval)

        return None

    # ------------------------------------------------------------------
    # Internal: simulation mode
    # ------------------------------------------------------------------

    async def _simulate(
        self,
        req: ConfirmationRequest,
        event: asyncio.Event,
        timeout: int,
    ) -> str:
        """Simulate OWUI confirmation: log and auto-confirm after a short delay."""
        logger.info(
            "[SIMULATION] Confirmation %s: auto-confirming with default %r",
            req.id[:8],
            req.default_option,
        )

        # Brief delay to simulate user interaction (0.5s)
        try:
            await asyncio.wait_for(event.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass

        # Check if a real response came in during the delay
        if event.is_set() and req.response:
            return req.response

        # Auto-confirm with default
        req.status = ConfirmationStatus.CONFIRMED
        req.response = req.default_option
        req.responded_at = time.time()
        return req.default_option

    def __repr__(self) -> str:
        return (
            f"<OWUIConfirmation webhook={self.owui_webhook_url!r} "
            f"timeout={self.timeout}s sim={self.simulation_mode} "
            f"pending={len(self._pending)}>"
        )


# Singleton instance
_instance: Optional[OWUIConfirmation] = None


def get_owui_confirmation() -> OWUIConfirmation:
    """Return the global OWUIConfirmation singleton."""
    global _instance
    if _instance is None:
        _instance = OWUIConfirmation()
    return _instance


def reset_owui_confirmation() -> None:
    """Reset the singleton (for testing)."""
    global _instance
    _instance = None


__all__ = [
    "OWUIConfirmation",
    "ConfirmationRequest",
    "ConfirmationStatus",
    "get_owui_confirmation",
    "reset_owui_confirmation",
]
