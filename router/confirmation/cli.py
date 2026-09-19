"""CLI confirmation prompts — terminal-based user confirmation for routing decisions."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_AUTO_CONFIRM_TIMEOUT = 30
_DEFAULT_ALWAYS_CONFIRM: List[str] = [
    "bridge_session_expired",
    "all_free_providers_failed",
]
_DEFAULT_NEVER_CONFIRM: List[str] = [
    "vision",
    "chat",
]

# Known expert/slow models (trigger confirmation for speed warning)
_EXPERT_MODELS = {"deepseek-reasoner", "deepseek-r1"}


# ---------------------------------------------------------------------------
# CLIConfirmation
# ---------------------------------------------------------------------------


class CLIConfirmation:
    """Terminal-based confirmation for routing decisions.

    Decides whether to prompt the user before routing to a potentially
    expensive or risky provider.

    Parameters
    ----------
    auto_confirm_timeout:
        Seconds to wait for user input before accepting the default provider.
    always_confirm:
        Scenarios (context keys) that always require explicit confirmation.
    never_confirm:
        Request types that auto-route without prompting.
    """

    def __init__(
        self,
        auto_confirm_timeout: int = _DEFAULT_AUTO_CONFIRM_TIMEOUT,
        always_confirm: Optional[List[str]] = None,
        never_confirm: Optional[List[str]] = None,
    ) -> None:
        self.auto_confirm_timeout = auto_confirm_timeout
        self.always_confirm = list(always_confirm or _DEFAULT_ALWAYS_CONFIRM)
        self.never_confirm = list(never_confirm or _DEFAULT_NEVER_CONFIRM)

    # ------------------------------------------------------------------
    # ask
    # ------------------------------------------------------------------

    async def ask(
        self,
        prompt_text: str,
        options: List[str],
        timeout: int = _DEFAULT_AUTO_CONFIRM_TIMEOUT,
    ) -> str:
        """Present options to the user via stdin/stdout and return the selection.

        If no response is received within *timeout* seconds, returns the
        first option (the default).

        Parameters
        ----------
        prompt_text:
            Question or explanation shown to the user.
        options:
            List of selectable option strings. The first is the default.
        timeout:
            Seconds to wait before auto-selecting the default.

        Returns
        -------
        str
            The selected option string.
        """
        if not options:
            raise ValueError("options must be a non-empty list")

        default = options[0]
        formatted = "\n".join(
            f"  [{i + 1}] {opt}" for i, opt in enumerate(options)
        )

        prompt = (
            f"\n{prompt_text}\n"
            f"{formatted}\n"
            f"Select [1-{len(options)}] (default: 1, timeout: {timeout}s): "
        )

        loop = asyncio.get_running_loop()

        try:
            # Run blocking stdin read in a thread pool so we can apply timeout.
            raw = await asyncio.wait_for(
                loop.run_in_executor(None, self._read_line, prompt),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.info(
                "Confirmation timed out after %ds — auto-selecting default %r",
                timeout,
                default,
            )
            print(f"\n[timeout] Auto-selected: {default}", file=sys.stderr)
            return default

        raw = raw.strip()
        if not raw:
            return default

        try:
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1]
        except ValueError:
            pass

        # Non-numeric input: try to match option text
        for opt in options:
            if raw.lower() == opt.lower():
                return opt

        logger.warning("Invalid input %r — falling back to default %r", raw, default)
        return default

    @staticmethod
    def _read_line(prompt: str) -> str:
        """Blocking stdin read (runs in executor thread)."""
        try:
            print(prompt, end="", flush=True, file=sys.stderr)
            return input()
        except EOFError:
            return ""

    # ------------------------------------------------------------------
    # should_ask
    # ------------------------------------------------------------------

    def should_ask(
        self,
        request_type: str,
        provider: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Return True if this scenario needs user confirmation.

        Returns False for auto-route scenarios (vision, chat, normal tool_calling
        via DeepInfra).

        Parameters
        ----------
        request_type:
            The request classification (e.g. 'tool_calling', 'vision', 'chat').
        provider:
            The primary provider selected for routing.
        context:
            Additional context (e.g. model name, session state).
        """
        context = context or {}

        # Check always_confirm scenarios (context keys)
        for scenario in self.always_confirm:
            if context.get(scenario):
                return True

        # Never confirm auto-route scenarios
        if request_type in self.never_confirm:
            return False

        # Tool calling routed to bridge is fragile — always confirm
        if request_type == "tool_calling" and provider == "deepseek-bridge":
            return True

        # Expert/slow models — warn about latency
        model = context.get("model", "")
        if any(expert in str(model).lower() for expert in _EXPERT_MODELS):
            return True

        # Bridge session expired
        if context.get("bridge_session_expired"):
            return True

        # All free providers failed — offer paid option
        if context.get("all_free_providers_failed"):
            return True

        return False

    # ------------------------------------------------------------------
    # confirm_routing
    # ------------------------------------------------------------------

    async def confirm_routing(
        self,
        request_type: str,
        primary_provider: str,
        fallback_provider: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Decide the final provider for a routing request.

        If the scenario auto-routes, returns *primary_provider* without
        prompting. If it needs confirmation, prompts the user and returns
        the chosen provider.

        Parameters
        ----------
        request_type:
            The request classification string.
        primary_provider:
            The provider selected as primary.
        fallback_provider:
            The alternative/fallback provider.
        context:
            Additional context dict.

        Returns
        -------
        str
            The provider name to use for routing.
        """
        context = context or {}

        if not self.should_ask(request_type, primary_provider, context):
            return primary_provider

        # Build prompt based on scenario
        prompt_text, options = self._build_prompt(
            request_type, primary_provider, fallback_provider, context
        )

        chosen = await self.ask(
            prompt_text=prompt_text,
            options=options,
            timeout=self.auto_confirm_timeout,
        )
        return chosen

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        request_type: str,
        primary_provider: str,
        fallback_provider: str,
        context: Dict[str, Any],
    ) -> tuple[str, List[str]]:
        """Build a human-readable prompt and option list for a scenario."""

        # Tool calling on bridge — suggest DeepInfra
        if request_type == "tool_calling" and primary_provider == "deepseek-bridge":
            return (
                "Tool calling via DeepSeek Bridge is fragile and may fail.\n"
                "DeepInfra has more reliable tool calling support.",
                [fallback_provider, primary_provider],
            )

        # Expert/slow model warning
        model = context.get("model", "")
        if any(expert in str(model).lower() for expert in _EXPERT_MODELS):
            return (
                f"Expert model {model!r} selected — response may take ~12s.\n"
                f"Use faster alternative {fallback_provider!r}?",
                [primary_provider, fallback_provider],
            )

        # Bridge session expired
        if context.get("bridge_session_expired"):
            return (
                "DeepSeek Bridge session has expired.\n"
                "Re-login or switch to a fallback provider?",
                [
                    "relogin",
                    fallback_provider,
                    primary_provider,
                ],
            )

        # All free providers failed
        if context.get("all_free_providers_failed"):
            return (
                "All free providers have failed.\n"
                "Switch to paid OpenRouter, or retry fallback?",
                [
                    fallback_provider,
                    primary_provider,
                ],
            )

        # Generic fallback prompt
        return (
            f"Routing {request_type!r} request.\n"
            f"Primary: {primary_provider}, Fallback: {fallback_provider}",
            [primary_provider, fallback_provider],
        )


__all__ = ["CLIConfirmation"]
