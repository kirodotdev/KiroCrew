"""Where the KAS telemetry notices reach a person: one bell note per data home, or a chat row.

Every session a KAS process sent ``telemetryEnabled: false`` carries
``TELEMETRY_UNCONFIRMED_NOTICE``, and the sentence is the same every time, because the
engine never confirms the setting. So it is about the install, not about a chat: the
first session that carries it posts one dashboard bell note (:func:`deliver`), claimed
with an ``O_EXCL`` marker under the data home before the note goes out, and a chat's
first telemetry notice gets no row when it is this one (:func:`notice_gets_row`).
``TELEMETRY_NOT_APPLIED_NOTICE`` is about one chat, so it stays a row once in that
chat, and a chat that showed one telemetry row still gets the other as a row when its
answer changes. See the the-kas-telemetry-off-notice-is-one-bell-note-per-install
entry in docs/decisions. It lives in ``agent_sdk`` because it reads the ACP layer's
notice text, as :mod:`kiro_crew.agent_sdk.kas_default_model` reads kiro-cli's settings.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Any

from kiro_crew.acp.kas_cli_settings import TELEMETRY_UNCONFIRMED_NOTICE
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: Claimed once per data home, right before the note is delivered.
MARKER_FILENAME = "announced-kas-telemetry-unconfirmed"

NOTE_KIND = "agent"
NOTE_CHANNEL = "system.agent"
NOTE_TITLE = "Kiro Crew asked KAS to turn telemetry off"
NOTE_BODY = TELEMETRY_UNCONFIRMED_NOTICE.removeprefix("ℹ ")


def _telemetry_notice_is_new(slot: Any, notice: str) -> bool:
    """Whether *slot*'s chat has yet to show telemetry *notice*; marks it shown.

    Every KAS session a chat creates or reloads carries the notice, and nothing the
    user does in the chat ends it, so the chat takes it once. It is new again only
    when the text differs from the last one taken: the opt-out stopped applying to
    this chat, or applies again. The mark is saved on the chat's metadata line, so
    a restart does not repeat the notice.
    """
    mark = hashlib.sha256(notice.encode()).hexdigest()[:16]
    if slot._telemetry_notice_shown == mark:
        return False
    slot._telemetry_notice_shown = mark
    return True


def notice_gets_row(slot: Any, notice: str) -> bool:
    """Whether telemetry *notice* is a row in *slot*'s chat now; marks it taken.

    A chat's first telemetry notice gets no row when it is the unconfirmed one, which
    the bell carries; the mark is still recorded, so a later not-applied notice shows,
    and a chat that showed a row gets the unconfirmed one back as a row when its
    answer changes again, so a stale row is never the chat's last word.
    """
    first = not slot._telemetry_notice_shown
    if not _telemetry_notice_is_new(slot, notice):
        return False
    return not (first and notice == TELEMETRY_UNCONFIRMED_NOTICE)


def _claim() -> bool:
    """Create the marker exclusively. ``True`` only for the caller that made it. Blocking."""
    try:
        path = config_dir() / MARKER_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError:
        logger.warning("could not claim the KAS telemetry notice", exc_info=True)
        return False
    os.close(fd)
    return True


async def deliver(state: Any, client: Any) -> bool:
    """Post the one bell note when *client*'s new session carries the unconfirmed notice.

    Returns whether a note went out: only for the first session on this data home,
    and never for a client without that notice, which costs no I/O. Never raises.
    """
    if getattr(client, "telemetry_unconfirmed_notice", "") != TELEMETRY_UNCONFIRMED_NOTICE:
        return False
    try:
        if not await asyncio.to_thread(_claim):
            return False
        state.notify(NOTE_KIND, NOTE_TITLE, NOTE_BODY, channel=NOTE_CHANNEL)
    except Exception:  # noqa: BLE001 - an informational note must never fail a turn
        logger.warning("could not deliver the KAS telemetry notice", exc_info=True)
        return False
    return True
