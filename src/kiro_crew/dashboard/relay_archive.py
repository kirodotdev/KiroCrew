"""Old relay chats: read-only archives of turns a peer crew once ran.

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
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

RELAY_ARCHIVE_CODE = "relay_archive_read_only"
RELAY_ARCHIVE_ERROR = (
    "this chat ran on a remote crew and is read-only now; "
    "open the crew's own session to keep going"
)


def is_relay_archive(slot: Any) -> bool:
    """True for a slot an older build bound to a peer crew."""
    return getattr(slot, "executor", "") == "remote"


def relay_archive_refusal(slot: Any) -> web.Response | None:
    """The 409 for an action on a relay archive, else ``None``."""
    if not is_relay_archive(slot):
        return None
    return web.json_response(
        {"error": RELAY_ARCHIVE_ERROR, "code": RELAY_ARCHIVE_CODE},
        status=409,
    )
