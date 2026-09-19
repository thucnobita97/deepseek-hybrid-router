"""Config-based confirmation mode — pre-defined rules, no interactive prompts."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Union

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "confirmation.yaml"
)

# Default rules applied when the config file is missing or malformed.
_DEFAULT_RULES: Dict[str, str] = {
    "tool_calling": "auto",
    "vision": "auto",
    "reasoning": "prefer:deepseek-bridge:expert",
    "search": "auto",
    "chat": "auto",
    "on_bridge_failure": "fallback",
    "on_all_free_fail": "prefer:openrouter",
}


class ConfigConfirmation:
    """Configuration-based confirmation resolver.

    Reads confirmation rules from a YAML config file and automatically
    resolves provider selection without any interactive prompts.

    Rule formats
    ------------
    - ``auto``: use the primary provider as-is
    - ``fallback``: skip primary, signal the caller to use the fallback chain
    - ``prefer:<provider:model>``: override with the specified provider

    Config file format (``config/confirmation.yaml``)::

        rules:
          tool_calling: auto
          vision: auto
          reasoning: prefer:deepseek-bridge:expert
          search: auto
          chat: auto
          on_bridge_failure: fallback
          on_all_free_fail: prefer:openrouter
    """

    def __init__(
        self,
        config_path: Optional[Union[str, Path]] = None,
    ) -> None:
        self._config_path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
        self._rules: Dict[str, str] = dict(_DEFAULT_RULES)
        self._load_config()

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        """Load confirmation rules from the YAML config file.

        Falls back to default rules if the file doesn't exist or is malformed.
        """
        if not self._config_path.exists():
            logger.info(
                "Confirmation config not found at %s — using defaults",
                self._config_path,
            )
            self._rules = dict(_DEFAULT_RULES)
            return

        try:
            with open(self._config_path, "r") as fh:
                data = yaml.safe_load(fh) or {}

            loaded_rules = data.get("rules", {})
            if isinstance(loaded_rules, dict):
                # Merge: config values override defaults; unknown keys kept as-is.
                self._rules = {
                    **_DEFAULT_RULES,
                    **{k: str(v) for k, v in loaded_rules.items()},
                }
                logger.info(
                    "Loaded confirmation config from %s — %d rules",
                    self._config_path,
                    len(self._rules),
                )
            else:
                logger.warning(
                    "Invalid 'rules' section in %s — using defaults",
                    self._config_path,
                )
                self._rules = dict(_DEFAULT_RULES)
        except Exception:
            logger.warning(
                "Failed to parse confirmation config at %s — using defaults",
                self._config_path,
                exc_info=True,
            )
            self._rules = dict(_DEFAULT_RULES)

    def reload_config(self) -> None:
        """Reload confirmation rules from disk (hot-reload support)."""
        self._load_config()
        logger.info("Confirmation config reloaded from %s", self._config_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(
        self,
        request_type: str,
        primary_provider: str,
        context: Dict[str, Any],
    ) -> str:
        """Resolve which provider to use for a given request type.

        Parameters
        ----------
        request_type:
            The classified request type (e.g. ``"tool_calling"``, ``"reasoning"``).
        primary_provider:
            The primary ``provider:model`` string from the routing config.
        context:
            Additional context dict (reserved for future rule extensions).

        Returns
        -------
        str
            The ``provider:model`` string to use, or the literal ``"fallback"``
            when the rule says to skip the primary and use the fallback chain.

        Rule resolution
        ---------------
        - ``auto`` → returns *primary_provider* unchanged.
        - ``fallback`` → returns ``"fallback"`` (caller uses fallback chain).
        - ``prefer:<provider:model>`` → returns the specified provider.
        - Unknown / missing rule → returns *primary_provider* with a warning.
        """
        rule = self._rules.get(request_type, "auto")

        if rule == "auto":
            logger.debug(
                "ConfigConfirmation: %s → auto (using primary %s)",
                request_type,
                primary_provider,
            )
            return primary_provider

        if rule == "fallback":
            logger.debug(
                "ConfigConfirmation: %s → fallback (skip primary %s)",
                request_type,
                primary_provider,
            )
            return "fallback"

        if rule.startswith("prefer:"):
            preferred = rule[len("prefer:"):]
            logger.debug(
                "ConfigConfirmation: %s → prefer %s (overriding primary %s)",
                request_type,
                preferred,
                primary_provider,
            )
            return preferred

        # Unknown rule format — fall back to primary with a warning.
        logger.warning(
            "ConfigConfirmation: unknown rule %r for %s — using primary %s",
            rule,
            request_type,
            primary_provider,
        )
        return primary_provider

    def should_ask(
        self,
        request_type: str,
        provider: str,
        context: Dict[str, Any],
    ) -> bool:
        """Whether to prompt the user for confirmation.

        Config mode never prompts — always returns ``False``.
        """
        return False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def rules(self) -> Dict[str, str]:
        """Return a copy of the current rules."""
        return dict(self._rules)

    @property
    def config_path(self) -> Path:
        return self._config_path

    def __repr__(self) -> str:
        return (
            f"<ConfigConfirmation config={self._config_path.name} "
            f"rules={len(self._rules)}>"
        )


__all__ = ["ConfigConfirmation"]
