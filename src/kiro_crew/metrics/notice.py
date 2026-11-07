"""The one-line usage-data notice for CLI and headless starts.

Local metric recording is on by default. A dashboard user is told in the first-run
Privacy chapter; a CLI or headless install has no screen for it, so its first
gateway (or ``kirocrew run``) start prints one line to stderr and logs it at INFO.

Printed once per install: a stamp file under the data home records that it was
shown. If the stamp cannot be written the line prints again next start, and
nothing else changes. The notice is NOT a recorded choice: it never writes
``dashboard.privacy_ack_version``, and metric export stays off until recording is
turned on explicitly (see ``provider.outbound_consented``).

Never raises and never waits for input. A failed read prints nothing.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)

#: Under the data home. Not carried by export/snapshot (both copy named files
#: only), so a restored install on a new machine prints the line once more.
NOTICE_STAMP_FILE = "usage_notice_shown"

NOTICE_TEXT = (
    "Kiro Crew records local performance metrics (no prompts, files or credentials). "
    "To turn them off, run kirocrew telemetry disable or set DO_NOT_TRACK=1."
)

#: The last background notice thread, held so it is not collected mid-print.
_notice_thread: "threading.Thread | None" = None


def print_notice_in_background(cfg: "KiroCrewConfig | None" = None) -> threading.Thread:
    """Run :func:`maybe_print_notice` on a daemon thread and return at once.

    For start paths that must not wait on it (the gateway boot): the stat, the
    print and the stamp write happen beside the boot, never in front of it.
    """
    global _notice_thread
    thread = threading.Thread(
        target=maybe_print_notice, args=(cfg,), name="kirocrew-usage-notice", daemon=True
    )
    _notice_thread = thread
    thread.start()
    return thread


def maybe_print_notice(
    cfg: "KiroCrewConfig | None" = None, *, stream: "TextIO | None" = None
) -> bool:
    """Print the notice once when no choice is recorded and recording is on.

    Returns whether it printed. Silent when the user already acknowledged the
    current notice (dashboard chapter, or ``kirocrew telemetry enable|disable``),
    when recording is off by any switch, or when the stamp says it was shown.
    """
    try:
        from kiro_crew.beacon import PRIVACY_NOTICE_VERSION
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.paths import config_dir
        from kiro_crew.metrics.provider import _consent_enabled

        if cfg is None:
            cfg = KiroCrewConfig.load()
        if int(cfg.dashboard.privacy_ack_version) >= PRIVACY_NOTICE_VERSION:
            return False
        if not _consent_enabled(cfg.telemetry):
            return False
        stamp = config_dir() / NOTICE_STAMP_FILE
        if stamp.exists():
            return False
    except Exception:
        logger.debug("usage notice check failed; not printing", exc_info=True)
        return False
    try:
        print(NOTICE_TEXT, file=stream if stream is not None else sys.stderr)
    except Exception:  # a closed stderr must not stop a start
        logger.debug("usage notice print failed", exc_info=True)
    logger.info(NOTICE_TEXT)
    try:
        from kiro_crew.atomic_write import atomic_write

        atomic_write(stamp, "1\n")
    except Exception:
        logger.debug("usage notice stamp not written; it will print again", exc_info=True)
    return True
