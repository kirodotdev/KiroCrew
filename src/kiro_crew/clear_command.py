"""The ``/clear`` boundary in a transcript: the typed user row that starts a session over.

Kept free of dashboard imports so the context builder can stop its replay at the
same boundary the dashboard's derivations stop at.
"""

from __future__ import annotations

from typing import Any

CLEAR_COMMAND = "/clear"


def is_clear_command_row(msg: Any) -> bool:
    """Whether *msg* is a user row that typed ``/clear``.

    Matches the runner's own test (``chat_turn/slash_commands.py``): the first
    word of the message, case-insensitive. The user row is the boundary because
    it is durable and every harness writes it, while the confirmation row's text
    differs per harness and a native clear empties only the in-memory window, so
    a restore brings the earlier rows back.
    """
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, str):
        return False
    words = content.split(maxsplit=1)
    return bool(words) and words[0].lower() == CLEAR_COMMAND
