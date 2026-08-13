"""Structured logging. Human-readable locally, JSON in the cloud."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "asctime",
    "message",
    "taskName",
}


def safe_extra(extra: dict | None) -> dict:
    """Strip keys the stdlib ``LogRecord`` forbids in ``extra=``.

    ``makeRecord`` raises ``KeyError`` when ``extra`` tries to overwrite a
    reserved attribute (``message``, ``msg``, ``name``, ...). Persisted run
    reports are logged with their whole dict as context, so collisions must be
    dropped before the record is built - otherwise ingestion crashes after a
    successful fetch, right at the summary log line.
    """
    if not extra:
        return {}
    return {k: v for k, v in extra.items() if k not in _RESERVED}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        if extras:
            payload["context"] = {k: _safe(v) for k, v in extras.items()}
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _safe(value):
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def configure_logging(level: str | None = None, as_json: bool | None = None) -> None:
    level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    if as_json is None:
        as_json = os.getenv("LOG_JSON", "false").lower() in {"1", "true", "yes"}

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter()
        if as_json
        else logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)-38s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    if os.getenv("AIRFLOW_CTX_TASK_ID"):
        # Inside an Airflow task, sys.stdout is a StreamLogWriter that feeds
        # everything written to it back into Airflow's own logging. Installing
        # our stdout handler on the *root* logger makes every app log line
        # recurse: the re-propagated record reaches root, is written to stdout
        # again, is re-propagated... until the stack overflows and the task
        # dies. Attaching the handler to the ``brilink`` subtree instead, with
        # propagate disabled, emits each line exactly once; Airflow's own
        # stdout capture then forwards it to the task log file.
        logger = logging.getLogger("brilink")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
    else:
        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(level)

    for noisy in ("urllib3", "httpx", "transformers", "filelock", "praw"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
