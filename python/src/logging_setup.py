"""Structured logging for the ETL: one JSON object per line on stdout.

Core fields on every line: ``timestamp``, ``level`` (UPPERCASE), ``service``,
``correlation_id``, ``logger``, ``message`` and ``release``. The worker scripts
run as separate processes, so the run-level context arrives through the
environment:

- ``CORRELATION_ID``: the run-level join key (a fresh UUID when unset)
- ``PHASE``: the pipeline phase, bound when set
- ``GIT_SHA``: the release, ``unknown`` when unset
- ``LOG_FORMAT=console``: human-readable lines for local runs

See ``nlm-ckn-rnd/docs/proposals/logging-approach.md``.
"""

import logging
import os
import sys
import uuid
from typing import IO, Optional

import structlog

CORRELATION_ID_ENV = "CORRELATION_ID"
PHASE_ENV = "PHASE"
RELEASE_ENV = "GIT_SHA"
LOG_FORMAT_ENV = "LOG_FORMAT"


def _add_app_context(service: str):
    release = os.getenv(RELEASE_ENV) or "unknown"

    def processor(logger, method_name, event_dict):
        event_dict.setdefault("service", service)
        event_dict.setdefault("release", release)
        return event_dict

    return processor


def _uppercase_level(logger, method_name, event_dict):
    # structlog emits "warning"/"error"; Java emits "WARN"/"ERROR". Uppercase
    # and spell warning as Java does so one `level = "WARN"` filter matches
    # every surface.
    level = event_dict.get("level")
    if level:
        level = level.upper()
        event_dict["level"] = "WARN" if level == "WARNING" else level
    return event_dict


def _shared_processors() -> list:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        _uppercase_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]


def configure_logging(
    service: str,
    level: int = logging.INFO,
    stream: Optional[IO[str]] = None,
) -> None:
    """Route structlog and stdlib logging to JSON lines on ``stream``.

    Call once at process start, before the first log line. Binds
    ``correlation_id`` (and ``phase`` when set) from the environment.
    """
    shared = [_add_app_context(service), *_shared_processors()]

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    if os.getenv(LOG_FORMAT_ENV, "").lower() == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=False)
    else:
        renderer = structlog.processors.JSONRenderer()

    formatter = structlog.stdlib.ProcessorFormatter(
        # Applies the same fields to stdlib records (Prefect, third parties).
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.EventRenamer("message"),
            renderer,
        ],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    structlog.contextvars.clear_contextvars()
    bound = {
        "correlation_id": os.getenv(CORRELATION_ID_ENV) or str(uuid.uuid4()),
    }
    phase = os.getenv(PHASE_ENV)
    if phase:
        bound["phase"] = phase
    structlog.contextvars.bind_contextvars(**bound)
