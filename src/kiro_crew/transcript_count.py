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

import contextlib
import json
import os
from collections.abc import Callable
from pathlib import Path

from kiro_crew.atomic_write import atomic_write


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


#: Where a transcript's stored message count lives, under the transcripts' own
#: directory: a dot-directory, so no ``*.jsonl`` glob over that directory sees it.
COUNT_DIR = ".msg-count"


def _sidecar(path: Path) -> Path:
    return path.parent / COUNT_DIR / f"{path.name}.json"


def _stamp(path: Path | str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def _load(path: Path) -> tuple[int, int, int] | None:
    try:
        raw = json.loads(_sidecar(path).read_text(encoding="utf-8"))
        size, mtime, count = raw["size"], raw["mtime_ns"], raw["count"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if all(type(v) is int and v >= 0 for v in (size, mtime, count)):
        return size, mtime, count
    return None


def _store(path: Path, stamp: tuple[int, int], count: int) -> None:
    payload = {"size": stamp[0], "mtime_ns": stamp[1], "count": count}
    with contextlib.suppress(OSError):  # a lost write only costs one recount
        _sidecar(path).parent.mkdir(exist_ok=True)
        atomic_write(_sidecar(path), json.dumps(payload), fsync=False)


def stored_count(path: Path | str) -> int | None:
    """The message count stored for *path*, or ``None`` when absent or stale.

    Trusted only while the transcript's size and mtime still equal the ones the
    count was stored against; any other writer (rewrite, rotation, another tool)
    changes one of them, so the caller recounts.
    shortcut: a same-size, same-mtime rewrite of message rows is not detected;
    the metadata writers keep the mtime but change no message row.
    """
    path = Path(path)
    stored, now = _load(path), _stamp(path)
    return stored[2] if stored and now and stored[:2] == now else None


def count_and_store(
    path: Path | str, count_fn: Callable[[Path | str], int] = count_messages
) -> int:
    """*count_fn* (:func:`count_messages`) once, stored for the next start when the file held still."""
    path = Path(path)
    before = _stamp(path)
    count = count_fn(path)
    if before is not None and _stamp(path) == before:
        _store(path, before, count)
    return count


def stamp_before_append(path: Path | str) -> tuple[int, int, int] | None:
    """The stored ``(size, mtime_ns, count)`` when it matches *path* now; for :func:`bump_after_append`."""
    path = Path(path)
    stored, now = _load(path), _stamp(path)
    return stored if stored and now and stored[:2] == now else None


def bump_after_append(path: Path | str, before: tuple[int, int, int] | None, line: str) -> None:
    """Store the count after appending *line*; no-op without a matching *before*.

    *line* adds one only if :func:`message_row` calls it a message, as a full count would.
    """
    now = _stamp(path)
    if before is not None and now is not None:
        _store(Path(path), now, before[2] + (message_row(line) is not None))


def forget_count(path: Path | str) -> None:
    """Remove *path*'s stored count, for a transcript that was deleted."""
    with contextlib.suppress(OSError):
        _sidecar(Path(path)).unlink()
