"""What a transcript "message" row is, and a count of them that holds one line at a time.

:func:`message_row` is the single definition of a message row: a line of
``text.splitlines()`` that, once ``str.strip``-ped, is non-empty, parses with
``json.loads`` into a ``dict``, and whose ``"_type"`` is not ``"metadata"``.
``TranscriptReadProjection._read_messages_locked`` builds its list with it, and
:func:`count_messages` counts with it, so the two cannot drift apart.

:func:`count_messages` returns ``len`` of that list without materializing it.
It is equivalent line for line, not approximately:

* The file is split on ``b"\\n"`` and each piece is decoded and run through
  ``str.splitlines``. ``\\n`` is a ``splitlines`` boundary, so splitting there
  first and again on every other boundary inside the piece yields the same
  non-empty lines as one ``splitlines`` over the whole text. Text-mode
  newline translation (``\\r`` / ``\\r\\n`` -> ``\\n``) changes nothing either:
  both are ``splitlines`` boundaries already.
* Decoding is strict UTF-8 per piece. A multi-byte sequence never contains
  ``0x0A``, so a piece-wise decode fails exactly when the whole-file decode
  does, and raises the same ``UnicodeDecodeError`` class.
"""

from __future__ import annotations

import json
from pathlib import Path


def message_row(line: str) -> dict | None:
    """The parsed row when one ``splitlines`` line is a message, else ``None``."""
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("_type") == "metadata":
        return None
    return data


def count_messages(path: Path | str) -> int:
    """``len`` of the message rows ``_read_messages_locked`` would return for *path*.

    Raises ``OSError`` / ``UnicodeDecodeError`` / ``ValueError`` where the full
    read does.
    """
    count = 0
    with open(path, "rb") as handle:
        for piece in handle:
            for line in piece.decode("utf-8").splitlines():
                if message_row(line) is not None:
                    count += 1
    return count
