"""
Centralized logging setup for the whole pipeline. Call configure_logging()
once, at application startup (see app.py) -- every other module just does
`logger = logging.getLogger(__name__)` and logs normally; this file only
controls HOW those records are formatted/routed, not what gets logged
where.

Structured, not just readable: any call site can attach extra fields via
`logger.info("...", extra={"role": role_name, "donor_id": donor_id})`.
Two output formats, chosen via LOG_FORMAT:
  - "text" (default): human-readable, `key=value` extras appended --
    what you want staring at a terminal during local development.
  - "json": one JSON object per line, every extra field as a real key --
    what a real log aggregator (CloudWatch, Loki, ELK, ...) expects, so
    a long-running deployment can actually query/alert on this instead
    of grepping raw text.
Switching between them is a deployment-time env var, not a code change.
"""

import json
import logging
import os
import sys

# Every attribute a stdlib LogRecord already carries, plus the two
# pseudo-fields Formatter.format() computes on demand (message, asctime).
# Anything NOT in this set on a given record is a caller-supplied extra
# field, which is exactly what both formatters below render specially.
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {"message", "asctime"}


def _extras_of(record):
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_extras_of(record))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class KeyValueFormatter(logging.Formatter):
    def format(self, record):
        base = f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} {record.levelname:<7} {record.name}: {record.getMessage()}"
        extras = _extras_of(record)
        if extras:
            base += " | " + " ".join(f"{k}={v}" for k, v in sorted(extras.items()))
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging():
    """
    Idempotent -- safe to call more than once (e.g. once from app.py's
    factory, again if a test re-imports it) without stacking up
    duplicate handlers that would print every line twice.
    """
    root = logging.getLogger()
    if getattr(root, "_ledger_ask_configured", False):
        return

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    fmt = os.getenv("LOG_FORMAT", "text").lower()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else KeyValueFormatter())

    root.handlers = [handler]
    root.setLevel(getattr(logging, level_name, logging.INFO))
    root._ledger_ask_configured = True
