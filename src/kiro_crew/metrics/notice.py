"""The one-line usage-data notice printed when Kiro Crew starts.

Local metric recording, the daily heartbeat and, where the build ships it, the
export of product usage metrics to Kiro telemetry are on by default. Nothing waits
for an acknowledgement. This line is a disclosure only: the first gateway (or
``kirocrew run`` / ``kirocrew chat``) start prints it to stderr and logs it at INFO,
and Settings > Privacy shows the same switches in the dashboard.

Printed once per install and notice version: a stamp file under the data home
records the ``beacon.PRIVACY_NOTICE_VERSION`` it showed, so a later version prints
once more. A stored ``dashboard.privacy_ack_version`` at or above that version
(written by builds that showed a dashboard notice) counts as told. If the stamp
cannot be written the line prints again next start, and nothing else changes. It
is never a recorded choice and never writes config.

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

#: The same line where the build sends product usage metrics to Kiro telemetry.
NOTICE_TEXT_KIRO = (
    "Kiro Crew records local performance metrics and sends product usage metrics to "
    "Kiro telemetry with a hashed install ID (no prompts, files or credentials). "
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
        target=maybe_print_notice,
        args=(cfg,),
        name="kirocrew-usage-notice",
        daemon=True,
    )
    _notice_thread = thread
    thread.start()
    return thread


def maybe_print_notice(
    cfg: "KiroCrewConfig | None" = None,
    *,
    stream: "TextIO | None" = None,
) -> bool:
    """Print the notice once per version while recording is on.

    Returns whether it printed. Silent when recording is off by any switch, when
    the stamp says this version was shown, or when the stored
    ``dashboard.privacy_ack_version`` already covers it.
    """
    try:
        from kiro_crew.beacon import PRIVACY_NOTICE_VERSION
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.paths import config_dir
        from kiro_crew.metrics.provider import _consent_enabled, _held_consent

        if cfg is None:
            cfg = KiroCrewConfig.load()
        if _held_consent(cfg, _consent_enabled(cfg.telemetry)) is not True:
            return False
        if int(cfg.dashboard.privacy_ack_version) >= PRIVACY_NOTICE_VERSION:
            return False
        if stamped_version() >= PRIVACY_NOTICE_VERSION:
            return False
        stamp = config_dir() / NOTICE_STAMP_FILE
        from kiro_crew.metrics import kiro_export

        text = NOTICE_TEXT_KIRO if kiro_export.destinations() else NOTICE_TEXT
        out = stream if stream is not None else sys.stderr
    except Exception:
        logger.debug("usage notice check failed; not printing", exc_info=True)
        return False
    try:
        print(text, file=out, flush=True)
    except Exception:  # a closed stderr must not stop a start
        logger.debug("usage notice print failed", exc_info=True)
    logger.info(text)
    try:
        from kiro_crew.atomic_write import atomic_write

        atomic_write(stamp, f"{PRIVACY_NOTICE_VERSION}\n")
    except Exception:
        logger.debug("usage notice stamp not written; it will print again", exc_info=True)
    return True


def stamped_version() -> int:
    """The notice version :data:`NOTICE_STAMP_FILE` holds; ``0`` when none.

    One written before the stamp carried a version holds ``1``. Fails closed to ``0``.
    """
    try:
        from kiro_crew.config.paths import config_dir

        raw = (config_dir() / NOTICE_STAMP_FILE).read_text(encoding="utf-8").strip()
        return int(raw) if raw.isdigit() else 0
    except Exception:
        return 0
