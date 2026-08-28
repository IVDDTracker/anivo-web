"""JSON-lines structured logging with secret masking."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_SENSITIVE_RE = re.compile(r"(key|secret|token|password|signature|bearer)", re.I)
_SECRETS: list[str] = []


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6 and value not in _SECRETS:
        _SECRETS.append(value)


def _mask(text: str) -> str:
    for s in _SECRETS:
        if s in text:
            text = text.replace(s, "***")
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": _mask(record.getMessage()),
        }
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            for k, v in ctx.items():
                out[k] = "***" if _SENSITIVE_RE.search(k) else (
                    _mask(v) if isinstance(v, str) else v)
        if record.exc_info and record.exc_info[0] is not None:
            out["exc"] = _mask(self.formatException(record.exc_info))[-3000:]
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("websockets.client", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_ctx(logger: logging.Logger, level: int, msg: str, **ctx: Any) -> None:
    logger.log(level, msg, extra={"ctx": ctx})
