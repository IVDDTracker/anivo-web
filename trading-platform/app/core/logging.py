"""Structured JSON logging with secret masking.

Every log record is emitted as one JSON line. Values of fields whose names look
sensitive are masked, and any configured secret literal appearing anywhere in the
message/args/exception text is replaced.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

SENSITIVE_KEY_RE = re.compile(r"(key|secret|token|password|signature|authorization|credential)", re.I)

_SECRET_VALUES: list[str] = []


def register_secret(value: str | None) -> None:
    """Register a secret literal so it is masked in all future log output."""
    if value and len(value) >= 6 and value not in _SECRET_VALUES:
        _SECRET_VALUES.append(value)


def mask_text(text: str) -> str:
    for secret in _SECRET_VALUES:
        if secret in text:
            text = text.replace(secret, "***MASKED***")
    return text


def mask_value(key: str, value: Any) -> Any:
    if isinstance(value, dict):
        return {k: mask_value(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_value(key, v) for v in value]
    if SENSITIVE_KEY_RE.search(key):
        return "***MASKED***"
    if isinstance(value, str):
        return mask_text(value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": mask_text(record.getMessage()),
        }
        extra = getattr(record, "ctx", None)
        if isinstance(extra, dict):
            out.update({k: mask_value(k, v) for k, v in extra.items()})
        if record.exc_info and record.exc_info[0] is not None:
            out["exc"] = mask_text(self.formatException(record.exc_info))[-4000:]
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("websockets.client", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_ctx(logger: logging.Logger, level: int, msg: str, **ctx: Any) -> None:
    """Log with structured context fields."""
    logger.log(level, msg, extra={"ctx": ctx})
