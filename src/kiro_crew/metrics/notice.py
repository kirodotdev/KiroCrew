"""The one-line usage-data notice for CLI and headless starts.

Local metric recording is on by default. A dashboard user is told in the first-run
Privacy chapter; a CLI or headless install has no screen for it, so its first
gateway (or ``kirocrew run``) start prints one line to stderr and logs it at INFO.

Printed once per install and notice version: a stamp file under the data home
records the ``beacon.PRIVACY_NOTICE_VERSION`` it showed, so a later version prints
once more. If the stamp cannot be written the line prints again next start, and
nothing else changes. The notice is NOT a recorded choice: it never writes
``dashboard.privacy_ack_version``. An operator's own export destination still
waits for recording turned on explicitly (``provider.outbound_consented``); the
built-in Kiro telemetry destination, which this line names, exports once it has
printed (``kiro_export.notice_shown``).

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

#: Under the data home: the notice version whose Kiro telemetry line printed on a
#: start with no dashboard. Only this stamp counts as having been told about that
#: destination (``kiro_export.notice_shown``); a dashboard host is told by the
#: Privacy chapter instead.
KIRO_NOTICE_STAMP_FILE = "kiro_telemetry_notice_shown"

#: Off only by a written ``telemetry.enabled: false`` older builds put in every
#: new config (``kiro_export.off_offer_due``): one line, once, on a terminal.
OFFER_TEXT = (
    "Kiro Crew performance metrics are off on this machine. Turning them on records "
    "them here and sends product usage metrics to Kiro telemetry with a hashed install "
    "ID (no prompts, files or credentials). To turn them on, run kirocrew telemetry enable."
)

#: Under the data home: the notice version :data:`OFFER_TEXT` last printed for.
OFFER_STAMP_FILE = "telemetry_offer_shown"

#: The last background notice thread, held so it is not collected mid-print.
_notice_thread: "threading.Thread | None" = None


def print_notice_in_background(
    cfg: "KiroCrewConfig | None" = None, *, headless: bool = False
) -> threading.Thread:
    """Run :func:`maybe_print_notice` on a daemon thread and return at once.

    For start paths that must not wait on it (the gateway boot): the stat, the
    print and the stamp write happen beside the boot, never in front of it.
    *headless* is a start that serves no dashboard; see :func:`maybe_print_notice`.
    """
    global _notice_thread
    thread = threading.Thread(
        target=maybe_print_notice,
        args=(cfg,),
        kwargs={"headless": headless},
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
    headless: bool = False,
) -> bool:
    """Print the notice once per version when no choice is recorded and recording is on.

    Only a *headless* start (no dashboard: ``gateway --no-dashboard``, ``run``,
    ``chat``) that printed the Kiro telemetry line writes
    :data:`KIRO_NOTICE_STAMP_FILE`; on a dashboard host the Privacy chapter is
    what tells the person.

    Returns whether it printed. Silent when the user already acknowledged the
    current notice (dashboard chapter, or ``kirocrew telemetry enable|disable``),
    when recording is off by any switch, or when the stamp says it was shown.
    """
    try:
        from kiro_crew.beacon import PRIVACY_NOTICE_VERSION
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.paths import config_dir
        from kiro_crew.metrics.provider import _consent_enabled, _held_consent

        if cfg is None:
            cfg = KiroCrewConfig.load()
        if _held_consent(cfg, _consent_enabled(cfg.telemetry)) is not True:
            return _maybe_print_offer(cfg, stream, headless)
        stamp = config_dir() / NOTICE_STAMP_FILE
        from kiro_crew.metrics import kiro_export

        kiro = bool(kiro_export.destinations())
        text = NOTICE_TEXT_KIRO if kiro else NOTICE_TEXT
        out = stream if stream is not None else sys.stderr
        is_terminal = bool(getattr(out, "isatty", lambda: False)())
        generic_due = (
            int(cfg.dashboard.privacy_ack_version) < PRIVACY_NOTICE_VERSION
            and stamped_version() < PRIVACY_NOTICE_VERSION
        )
        # The Kiro line is owed on its own: a generic acknowledgement (a dashboard
        # start's log line, a redirected `telemetry enable`) never named it. Once
        # the generic line has printed, it is repeated only to a terminal, so a
        # cron job or a service does not print it on every start.
        owes_kiro = (
            headless and kiro and not kiro_export.notice_shown(cfg) and (generic_due or is_terminal)
        )
        if not generic_due and not owes_kiro:
            return False
    except Exception:
        logger.debug("usage notice check failed; not printing", exc_info=True)
        return False
    delivered = False
    try:
        print(text, file=out, flush=True)
        # A terminal, not a log or a pipe: only then did a person see it.
        delivered = is_terminal
    except Exception:  # a closed stderr must not stop a start
        logger.debug("usage notice print failed", exc_info=True)
    logger.info(text)
    try:
        from kiro_crew.atomic_write import atomic_write

        atomic_write(stamp, f"{PRIVACY_NOTICE_VERSION}\n")
        # Only a line that reached a terminal counts as having told the person.
        if owes_kiro and delivered:
            atomic_write(stamp.with_name(KIRO_NOTICE_STAMP_FILE), f"{PRIVACY_NOTICE_VERSION}\n")
    except Exception:
        logger.debug("usage notice stamp not written; it will print again", exc_info=True)
    return True


def stamped_version(name: str = NOTICE_STAMP_FILE) -> int:
    """The notice version stamp *name* holds; ``0`` when none.

    :data:`NOTICE_STAMP_FILE` (the default) is the version the CLI/headless line
    last printed; one written before it carried the version holds ``1``.
    :data:`KIRO_NOTICE_STAMP_FILE` is the version whose Kiro telemetry line a
    headless start printed. Fails closed to ``0``.
    """
    try:
        from kiro_crew.config.paths import config_dir

        raw = (config_dir() / name).read_text(encoding="utf-8").strip()
        return int(raw) if raw.isdigit() else 0
    except Exception:
        return 0


def _maybe_print_offer(cfg: "KiroCrewConfig", stream: "TextIO | None", headless: bool) -> bool:
    """Print :data:`OFFER_TEXT` once, on a terminal, when the off offer is due.

    Only a start with no dashboard: a dashboard host is asked by the Privacy
    chapter's off form instead. Changes no setting. Returns whether it printed.
    """
    try:
        from kiro_crew.beacon import PRIVACY_NOTICE_VERSION
        from kiro_crew.config.paths import config_dir
        from kiro_crew.metrics import kiro_export

        out = stream if stream is not None else sys.stderr
        if not headless or not bool(getattr(out, "isatty", lambda: False)()):
            return False
        if stamped_version(OFFER_STAMP_FILE) >= PRIVACY_NOTICE_VERSION:
            return False
        if not kiro_export.off_offer_due(cfg):
            return False
        print(OFFER_TEXT, file=out, flush=True)
    except Exception:
        logger.debug("telemetry offer line not printed", exc_info=True)
        return False
    try:
        from kiro_crew.atomic_write import atomic_write

        atomic_write(config_dir() / OFFER_STAMP_FILE, f"{PRIVACY_NOTICE_VERSION}\n")
    except Exception:
        logger.debug("telemetry offer stamp not written; it will print again", exc_info=True)
    return True
