"""Read-only chats: old relay archives and reopened cron run transcripts.

A slot whose metadata line carries ``executor == "remote"`` was run on a
connected peer by the retired turn relay. The binding fields still load from
disk (``slot_persistence.metadata_codec``) so the transcript reads as it did,
but nothing can run it any more: there is no relay to send the turn to, and
running it here would put the crew's work on the wrong machine. Every
turn-starting or reconfiguring entry point refuses such a slot with the one
code below, and ``chat_runner._run_chat`` refuses it again as the last line of
defense.

Keyed on ``executor`` alone, not on the whole binding: a half-written binding
is an archive too, and must never fall through to a local run.

The second kind is a cron run's transcript reopened from History: a sequence
agent's ``cron_<job id>_<agent>`` or a stateless run's ``cron_<job id>_<run id>``
whose metadata line names no session to bind. Its tab would save to a second
``dashboard:`` file the delete witness refuses, so a reply would run and then
vanish. The History resume marks such a slot ``cron_run_read_only``
(:func:`mark_cron_run_read_only`, in memory only), and the same refusal answers
it with its own code. :func:`is_relay_archive` keeps meaning "ran on a peer
crew": fork, merge-back and the crew paths that read it are not about this kind.
"""

from __future__ import annotations

import re
from typing import Any

from aiohttp import web

from kiro_crew.history_files import transcript_stem

RELAY_ARCHIVE_CODE = "relay_archive_read_only"
RELAY_ARCHIVE_ERROR = (
    "this chat ran on a remote crew and is read-only now; "
    "open the crew's own session to keep going"
)


def is_relay_archive(slot: Any) -> bool:
    """True for a slot an older build bound to a peer crew."""
    return getattr(slot, "executor", "") == "remote"


CRON_RUN_READ_ONLY_CODE = "cron_run_read_only"
CRON_RUN_READ_ONLY_ERROR = "this run's transcript can be read here but not replied to"

# A sequence agent's or a stateless run's transcript: the job's own
# ``cron_<job id>`` has no suffix and reopens bound to its job.
_CRON_RUN_STEM = re.compile(r"cron_[0-9a-f]{8}_.+")


def is_cron_run_read_only(slot: Any) -> bool:
    """True for a slot the History resume marked a cron run's transcript.

    Read as ``is True`` so a ``MagicMock`` slot never reads as marked.
    """
    return getattr(slot, "cron_run_read_only", False) is True


def mark_cron_run_read_only(
    slot: Any, *, history_key: str, app: str, transcript_read: bool
) -> None:
    """Mark *slot* read-only when it shows a cron run's transcript it cannot save to.

    Only for the dashboard user's resume (no *app*) of a transcript read off
    disk, keyed on the STEM of the transcript actually read: a user's own tab
    named ``cron_0a1b2c3d_writer`` reads ``dashboard:cron_0a1b2c3d_writer`` and
    stays writable. A slot that bound to a session saves there and is left alone.
    """
    if (
        not app
        and transcript_read
        and not getattr(slot, "linked_session_key", "")
        and _CRON_RUN_STEM.fullmatch(transcript_stem(history_key))
    ):
        slot.cron_run_read_only = True


def relay_archive_refusal(slot: Any) -> web.Response | None:
    """The 409 for an action on a read-only chat, else ``None``."""
    if is_cron_run_read_only(slot):
        return web.json_response(
            {"error": CRON_RUN_READ_ONLY_ERROR, "code": CRON_RUN_READ_ONLY_CODE},
            status=409,
        )
    if not is_relay_archive(slot):
        return None
    return web.json_response(
        {"error": RELAY_ARCHIVE_ERROR, "code": RELAY_ARCHIVE_CODE},
        status=409,
    )
