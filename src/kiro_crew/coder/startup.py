"""Content-free elapsed timings for remote session startup operations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from time import monotonic

TIMING_LOGGER_NAME = "kiro_crew.coder.startup_timing"


def startup_logger(fallback: logging.Logger) -> logging.Logger:
    """Use the gateway's content-free file producer when it is installed."""
    timing_logger = logging.getLogger(TIMING_LOGGER_NAME)
    return timing_logger if timing_logger.handlers else fallback


@contextmanager
def startup_timing(logger: logging.Logger, step: str, *, workspace: str = "") -> Iterator[None]:
    """Log fixed operation names, never command arguments, payloads or errors.

    Callers supply an internal step label and an already validated workspace name.
    Timeouts and cancellations retain their original exceptions and cleanup paths.
    """
    started = monotonic()
    outcome = "error"
    try:
        yield
        outcome = "ok"
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        # The gateway installs a dedicated INFO producer on its existing file
        # queue. Standalone callers retain their normal logging configuration.
        startup_logger(logger).info(
            "Coder startup step=%s workspace=%s outcome=%s elapsed_ms=%.0f",
            step,
            workspace,
            outcome,
            (monotonic() - started) * 1000.0,
        )
