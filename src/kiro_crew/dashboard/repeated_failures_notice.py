"""The opt-in weekly bell note about agent tool failures that keep coming back.

``memory.repeated_failures_notice`` (default off) turns it on. When it is on, the
gateway's heartbeat asks this module about once a day whether a note is due: at
most one note per :data:`NOTICE_PERIOD`, and only when
:func:`kiro_crew.failure_recurrence.scan` finds at least one recurring failure in
the last week. The note links to the Repeated failures card on the Memory page,
where the owner reads each finding and decides whether it becomes a lesson. The
note itself names no tool and quotes no error: the card is owner-only, and a bell
note reaches every surface the bell does.

Restart-safe through one state file under the data home holding the time the last
note went out. Persist before you publish, as ``apps.update_notify`` does: the
file is written before the note is built, and a write that fails sends nothing,
so a write that keeps failing cannot turn into a note on every check. A missing,
unreadable or malformed file reads as "not sent yet" with a warning, and the next
note that goes out rewrites it. A time in the future reads the same way, so a
clock that once ran ahead cannot hold the note back for years.

Never blocks the loop and never raises: :meth:`RepeatedFailuresNotifier.maybe_check`
costs one clock read on the heartbeats between checks, and a due check runs its
config read, state file and log scan in a worker thread, in a task of its own, so
the heartbeat that called it is not held while the log is read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig, config_dir

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: The state file, under the data home. Not under ``cache/``: dropping a cache is
#: a supported repair, and dropping this file would send the note again early.
STATE_FILENAME = "repeated-failures-notice.json"

#: The least time between two notes, and the window the scan reads.
NOTICE_PERIOD = timedelta(days=7)

#: Seconds between two checks while the gateway runs.
CHECK_INTERVAL_SECS = 24 * 60 * 60

#: Seconds after start before the first check, so it does not add to a busy boot.
FIRST_CHECK_DELAY_SECS = 10 * 60

#: The notification channel and legacy kind of the note.
NOTE_CHANNEL = "system.memory"
NOTE_KIND = "agent"

#: Where the note links: the Memory page, scrolled to the Repeated failures card.
NOTE_URL = "/settings/overview?view=memory&focus=failures"

#: The dashboard copy id the bell renders in the reader's language.
NOTE_COPY_ID = "repeated_failures"

#: The English the note stores, for the OS banner and the feed's stored text.
NOTE_TITLE = "Tool failures keep coming back"
NOTE_BODY = (
    "Some agent tool calls failed again and again this week. "
    "Open this note to see them and turn one into a lesson."
)


def _state_path() -> Path:
    return config_dir() / STATE_FILENAME


def _enabled() -> bool:
    """Whether the note is on. A wrong-typed value reads as off: the config
    loader already warns about it and uses the default."""
    return KiroCrewConfig.load().memory.repeated_failures_notice is True


def _last_sent(now: datetime) -> datetime | None:
    """When the last note went out, or ``None`` for "not sent yet".

    Every way the file can be wrong reads as ``None`` with a warning: missing is
    the first run, and the rest are repaired by the next write.
    """
    path = _state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        logger.warning("could not read %s; treating the note as not sent yet", path, exc_info=True)
        return None
    try:
        data = json.loads(raw)
        sent = datetime.fromisoformat(data["last_sent"])
    except (ValueError, TypeError, KeyError):
        logger.warning("%s is malformed; treating the note as not sent yet", path)
        return None
    if sent.tzinfo is None:
        logger.warning("%s has a time with no zone; treating the note as not sent yet", path)
        return None
    if sent > now:
        logger.warning("%s holds a time in the future; treating the note as not sent yet", path)
        return None
    return sent


def _record_sent(now: datetime) -> bool:
    """Write *now* as the last send. Returns whether it is on disk."""
    try:
        atomic_write(_state_path(), json.dumps({"last_sent": now.isoformat()}) + "\n")
    except OSError:
        logger.warning("could not write %s; not sending the note", _state_path(), exc_info=True)
        return False
    return True


def _recurring_count(now: datetime) -> int:
    from kiro_crew.dashboard.handlers.core import _sel
    from kiro_crew.failure_recurrence import scan

    return len(scan(_sel(), now=now, window=NOTICE_PERIOD))


def prepare_notice(now: datetime) -> bool:
    """Whether a note is due at *now*; when it is, the send is already recorded.

    Blocking (config, state file and log reads): run off the loop. Total: any
    failure is logged and answers ``False``.
    """
    try:
        if not _enabled():
            return False
        sent = _last_sent(now)
        if sent is not None and now - sent < NOTICE_PERIOD:
            return False
        if _recurring_count(now) == 0:
            return False
        return _record_sent(now)
    except Exception:
        logger.warning(
            "could not work out whether the repeated-failures note is due", exc_info=True
        )
        return False


def deliver_notice(state: Any) -> None:
    """Send the note through ``state.notify``. Call ON the serving loop."""
    try:
        state.notify(
            NOTE_KIND,
            NOTE_TITLE,
            NOTE_BODY,
            url=NOTE_URL,
            channel=NOTE_CHANNEL,
            i18n={"id": NOTE_COPY_ID},
        )
    except Exception:
        logger.warning("could not deliver the repeated-failures note", exc_info=True)


class RepeatedFailuresNotifier:
    """Runs :func:`prepare_notice` about once a day, from the heartbeat."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._next_check = monotonic() + FIRST_CHECK_DELAY_SECS
        self._task: asyncio.Task[None] | None = None

    def maybe_check(self, state: "DashboardState") -> None:
        """Start a check when one is due and none is running. Never raises."""
        try:
            if self._task is not None and not self._task.done():
                return
            mono = self._monotonic()
            if mono < self._next_check:
                return
            self._next_check = mono + CHECK_INTERVAL_SECS
            self._task = asyncio.get_running_loop().create_task(self._check(state))
        except Exception:
            logger.debug("repeated-failures check not started", exc_info=True)

    async def _check(self, state: "DashboardState") -> None:
        try:
            due = await asyncio.to_thread(prepare_notice, self._utcnow())
        except Exception:
            logger.debug("repeated-failures check did not run", exc_info=True)
            return
        if due:
            deliver_notice(state)
