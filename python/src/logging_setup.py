"""Structured logging for the ETL.

Emits one JSON object per line on stdout, ready for CloudWatch Logs. Every
line carries the core fields ``timestamp``, ``level`` (UPPERCASE),
``service``, ``correlation_id``, ``logger``, ``message`` and ``release``.
The worker scripts run as separate processes, so the run-level context
arrives through the environment; see :func:`configure_logging`.

The schema is described in
``nlm-ckn-rnd/docs/proposals/logging-approach.md``.
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

# Libraries that log every request at INFO. Held at WARNING so a run is not
# buried in "HTTP Request: GET ..." lines (Prefect's API client alone makes
# several per task).
_QUIET_LOGGERS = ("httpx", "httpcore")


def _add_app_context(service: str):
    """Return a processor that adds ``service`` and ``release`` to each event.

    Parameters
    ----------
    service : str
        The deployable unit, for example ``etl-pipeline``

    Returns
    -------
    callable
        A structlog processor. ``release`` is read from the ``GIT_SHA``
        environment variable once, when the processor is built, and is
        ``unknown`` when unset. Existing values are not overwritten.
    """
    release = os.getenv(RELEASE_ENV) or "unknown"

    def processor(logger, method_name, event_dict):
        event_dict.setdefault("service", service)
        event_dict.setdefault("release", release)
        return event_dict

    return processor


def _uppercase_level(logger, method_name, event_dict):
    """Normalize ``level`` to the spelling Java emits.

    structlog emits ``warning`` and ``error``; Java emits ``WARN`` and
    ``ERROR``. Uppercasing, and spelling warning as ``WARN``, lets one
    ``level = "WARN"`` filter match every surface.
    """
    level = event_dict.get("level")
    if level:
        level = level.upper()
        event_dict["level"] = "WARN" if level == "WARNING" else level
    return event_dict


def _shared_processors() -> list:
    """Return the processors applied to both structlog and stdlib records.

    Returns
    -------
    list
        Processors that merge bound context, name the logger, set and
        normalize ``level``, add an ISO 8601 UTC ``timestamp``, and render
        stack info and exceptions into single string fields.
    """
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
    """Route structlog and stdlib logging to one-line records on ``stream``.

    Call once at process start, before the first log line. Reconfiguring
    replaces the root handler, so lines are not duplicated.

    The run-level context is read from the environment:

    - ``CORRELATION_ID``: the run-level join key; a fresh UUID when unset
    - ``PHASE``: the pipeline phase, bound only when set
    - ``GIT_SHA``: the release; ``unknown`` when unset
    - ``LOG_FORMAT``: ``console`` renders readable lines for local runs;
      anything else renders JSON

    Parameters
    ----------
    service : str
        The deployable unit, for example ``etl-pipeline``. The registry of
        allowed values is in the logging proposal.
    level : int
        The root logger level, default ``logging.INFO``
    stream : None | IO[str]
        Where to write records, default ``sys.stdout``
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
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    structlog.contextvars.clear_contextvars()
    bound = {
        "correlation_id": os.getenv(CORRELATION_ID_ENV) or str(uuid.uuid4()),
    }
    phase = os.getenv(PHASE_ENV)
    if phase:
        bound["phase"] = phase
    structlog.contextvars.bind_contextvars(**bound)
