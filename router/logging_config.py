"""Structured JSON logging configuration for the DeepSeek Hybrid Router."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


# Standard LogRecord attributes that are NOT user-supplied extras
_LOG_RECORD_BUILTINS = {
    "name", "msg", "args", "created", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message",
    "pathname", "process", "processName", "relativeCreated", "thread",
    "threadName", "exc_info", "exc_text", "stack_info", "taskName",
}

# Known extra fields we explicitly surface when present
_KNOWN_EXTRA_FIELDS = {"request_id", "provider", "latency_ms", "model", "status_code"}


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON strings.

    Each emitted line contains an ISO-8601 timestamp, severity level,
    message, source location (module/function/line), and any extra
    fields attached to the record (request_id, provider, latency_ms,
    model, status_code, etc.).
    """

    def format(self, record: logging.LogRecord) -> str:
        """Convert a LogRecord into a JSON-encoded string.

        Args:
            record: The standard library LogRecord to format.

        Returns:
            A JSON string with structured log fields.
        """
        # Let the base class populate record.message, record.exc_text, etc.
        record.message = record.getMessage()

        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "message": record.message,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Explicitly pick up known extra fields
        for key in _KNOWN_EXTRA_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                log_entry[key] = value

        # Also surface any unexpected extras that aren't standard attrs
        for key, value in record.__dict__.items():
            if (
                key not in _LOG_RECORD_BUILTINS
                and key not in log_entry
                and key != "message"
            ):
                log_entry[key] = value

        # Include exception traceback when present
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            log_entry["exception"] = record.exc_text

        return json.dumps(log_entry, default=str)


def setup_structured_logging() -> logging.Handler:
    """Configure structured JSON logging for the ``router`` logger.

    Creates a :class:`logging.StreamHandler` (writing to *stderr*) with a
    :class:`JSONFormatter`, attaches it to the ``router`` logger, and sets
    the logger level to ``INFO``.

    Returns:
        The created handler so the caller can remove it during shutdown.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JSONFormatter())

    router_logger = logging.getLogger("router")
    router_logger.setLevel(logging.INFO)
    router_logger.addHandler(handler)

    return handler
