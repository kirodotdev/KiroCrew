"""``kirocrew chat -m -``: read the single message from a piped stdin.

A script that builds a long prompt (a diff, a log excerpt, a file) cannot fit
it safely into one argument: shell quoting breaks on quotes, backticks and
``$``, the text sits in the process list for the whole turn, and a large one
exceeds the argument size limit. ``-m -`` takes the message from stdin instead,
the usual shell form. Everything else about ``-m`` is unchanged: the turn is
still non-interactive (``cli_chat._can_prompt``).
"""

from __future__ import annotations

import sys
from typing import BinaryIO

from kiro_crew.bounded_read import bounded_read

#: The ``-m`` value that means "read the message from stdin".
STDIN_MESSAGE = "-"

#: Upper bound on a piped message, in bytes. Large enough for a real diff or
#: log excerpt, small enough that a runaway pipe cannot fill memory.
MAX_STDIN_MESSAGE_BYTES = 1024 * 1024


class StdinMessageError(ValueError):
    """The piped message cannot be sent; the text says why, for one stderr line."""


def read_stdin_message(stream: BinaryIO, *, is_tty: bool) -> str:
    """Return the message piped on *stream*, read to end of file.

    Refused, with :class:`StdinMessageError`: a terminal stdin (``-m`` never
    waits on a keyboard), an empty or whitespace-only one (not sent as an empty
    turn), one over :data:`MAX_STDIN_MESSAGE_BYTES`, and bytes that are not
    UTF-8. The text is returned unchanged otherwise.
    """
    if is_tty:
        raise StdinMessageError(
            "-m - reads the message from a pipe or a file, and stdin is a terminal; "
            "pipe the message in (e.g. `git diff | kirocrew chat -m -`) or pass it as -m 'text'"
        )
    data = bounded_read(stream, MAX_STDIN_MESSAGE_BYTES + 1)
    if len(data) > MAX_STDIN_MESSAGE_BYTES:
        # Not drained to count the rest: an endless pipe (`yes |`) would never end.
        raise StdinMessageError(
            f"the message on stdin is over {MAX_STDIN_MESSAGE_BYTES} bytes, "
            "the limit for -m -; nothing was sent"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StdinMessageError(
            f"the message on stdin is not UTF-8 text (invalid byte at offset {exc.start})"
        ) from None
    if not text.strip():
        raise StdinMessageError("the message on stdin is empty; nothing was sent")
    return text


def resolve_chat_message(message: str | None) -> str | None:
    """The message ``kirocrew chat`` sends: *message* itself, or stdin for ``-``."""
    if message != STDIN_MESSAGE:
        return message
    return read_stdin_message(sys.stdin.buffer, is_tty=sys.stdin.isatty())
