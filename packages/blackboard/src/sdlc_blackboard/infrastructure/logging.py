"""structlog wiring for the kernel (research-structlog.yaml canonical_config).

One ``configure_logging`` call at each process entrypoint (MCP server lifespan,
CLI callback) wires structlog's native fast path — ``make_filtering_bound_logger``
for level filtering plus a renderer fork: pretty ``ConsoleRenderer`` for dev TTYs,
orjson-backed ``JSONRenderer`` + ``BytesLoggerFactory`` for production. No stdlib
``logging`` handler wiring: the native path bypasses stdlib entirely (the docs'
recommended composition), so third-party stdlib logs are left untouched.

Consumes ``Settings.log_level`` (and the ``log_format`` toggle) — previously plumbed
end-to-end with zero consumers. Callers own *when* to configure; this module owns
*how*. It performs no import-time side effects so tests control configuration.
"""

import logging
import sys
from collections.abc import Callable

import orjson
import structlog
from structlog.typing import Processor, WrappedLogger


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Wire structlog once at process startup. Idempotent (safe to call twice).

    ``level`` is a stdlib level name (``"INFO"``, ``"DEBUG"``, ...); ``fmt`` is
    ``"console"`` (pretty, dev) or ``"json"`` (one orjson-encoded line per event).
    An unknown level name falls back to ``INFO``; an unknown format falls back to
    ``console``.
    """
    min_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    processors: list[Processor]
    logger_factory: Callable[..., WrappedLogger]
    if fmt == "json":
        processors = [
            *shared_processors,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(serializer=orjson.dumps),
        ]
        logger_factory = structlog.BytesLoggerFactory()
    else:
        processors = [
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ]
        logger_factory = structlog.PrintLoggerFactory(sys.stderr)

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(min_level),
        logger_factory=logger_factory,
        cache_logger_on_first_use=True,
    )
