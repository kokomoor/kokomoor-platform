"""Structured logging configuration using structlog.

Configures JSON or human-readable structured logging based on settings.
Call ``setup_logging()`` once at application startup. All modules should
use ``structlog.get_logger(__name__)`` for their loggers.

Usage:
    from core.observability import setup_logging
    setup_logging()

    import structlog
    logger = structlog.get_logger(__name__)
    logger.info("pipeline_started", pipeline="job_agent")
"""

from __future__ import annotations

import logging
import sys

import structlog

from core.config import get_settings


def setup_logging() -> None:
    """Configure structured logging for the platform.

    In dev mode (``log_json=False``), produces coloured, human-readable
    output.  In production (``log_json=True``), produces JSON lines
    suitable for log aggregation.
    """
    settings = get_settings()

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if settings.log_json:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(settings.log_level)

    # Quiet noisy third-party loggers.
    for noisy in ("httpx", "httpcore", "asyncio", "playwright"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
