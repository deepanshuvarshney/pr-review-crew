"""Structured logging. JSON when stderr is not a terminal or when requested."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "taskName",
    "asctime",
}


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in RESERVED or key.startswith("_"):
            continue
        payload[key] = value
    return payload


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = [f"{key}={value}" for key, value in _extras(record).items()]
        if extras:
            return base + " " + " ".join(extras)
        return base


def configure_logging(level: str | None = None, *, json_logs: bool | None = None) -> None:
    """Attach a single handler to the ``prcrew`` logger.

    Library code does not configure logging itself, so tests can capture records
    through the root logger. The CLI calls this once at startup.
    """

    chosen = (level or os.environ.get("PRCREW_LOG_LEVEL") or "INFO").upper()
    if json_logs is None:
        requested = os.environ.get("PRCREW_LOG_FORMAT", "").lower()
        if requested == "json":
            json_logs = True
        elif requested == "text":
            json_logs = False
        else:
            json_logs = not sys.stderr.isatty()

    logger = logging.getLogger("prcrew")
    logger.setLevel(chosen)
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(chosen)
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(PlainFormatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
