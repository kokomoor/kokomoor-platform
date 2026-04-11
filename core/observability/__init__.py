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
import logging.handlers
import sys
from datetime import UTC, datetime
from pathlib import Path

import structlog

from core.config import get_settings


def setup_logging() -> None:
    """Configure structured logging for the platform.

    In dev mode (``log_json=False``), produces coloured, human-readable
    output to stdout.  In production (``log_json=True``), produces JSON
    lines suitable for log aggregation.

    When ``log_file_enabled=True`` (the default), also writes JSON-format
    logs to a rotating file under ``log_file_dir``.  Files are named
    ``pipeline_<YYYYMMDD>.log`` and rotate at 10 MB (7 backups kept).
    This ensures full logs are preserved even when the terminal session
    is closed or output is truncated.
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
        console_renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        console_renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    console_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            console_renderer,
        ],
    )

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(console_formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(stdout_handler)
    root_logger.setLevel(settings.log_level)

    if settings.log_file_enabled:
        _add_file_handler(root_logger, settings.log_file_dir)

    # Quiet noisy third-party loggers.
    for noisy in ("httpx", "httpcore", "asyncio", "playwright"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _add_file_handler(root_logger: logging.Logger, log_dir: str) -> None:
    """Attach a rotating JSON file handler to *root_logger*.

    Logs are always written as JSON regardless of the ``log_json`` setting so
    that file contents are machine-parseable for post-hoc analysis.  The file
    is named ``pipeline_<YYYYMMDD>.log`` so runs from the same calendar day
    accumulate in the same file; the rotating handler rolls to a new file when
    that file exceeds 10 MB (7 backups).
    """
    log_path = Path(log_dir)
    try:
        log_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        logging.getLogger(__name__).warning(
            "log_file_dir_creation_failed",
            extra={"log_dir": log_dir},
        )
        return

    date_stamp = datetime.now(UTC).strftime("%Y%m%d")
    log_file = log_path / f"pipeline_{date_stamp}.log"

    file_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)
