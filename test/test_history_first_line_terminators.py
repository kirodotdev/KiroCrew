"""Every rewrite of a session file's first line leaves the bytes after it alone.

The metadata line is the only line these writers own. A rewrite that read the
file in text mode and wrote it back translated every row's line terminator on
the way: universal newlines turn ``\\r\\n`` into ``\\n`` on read, and a text-mode
write turns ``\\n`` into the platform's separator. On Windows, where the append
path writes ``\\r\\n`` rows, that rewrote every row of the transcript, and a
compare-and-set restore of the first line (``restore_first_line_if``) could no
longer put the file back byte for byte.

The fixtures below are re-terminated explicitly, so each terminator shape runs
on every OS rather than only on the runner that produces it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from kiro_crew.history import ConversationLog

KEY = "dashboard:demo"

#: Line terminators per shape: (first line, every row after it). ``mixed`` is
#: the shape a Windows host produces today: a metadata line written as bytes
#: with ``\n`` above rows appended in text mode with ``\r\n``.
_SHAPES = {
    "lf": (b"\n", b"\n"),
    "crlf": (b"\r\n", b"\r\n"),
    "mixed": (b"\n", b"\r\n"),
}


def _terminate(log: ConversationLog, shape: str) -> tuple[bytes, bytes]:
    """Re-terminate *KEY*'s file as *shape*; return (first line's terminator, rest)."""
    head_eol, row_eol = _SHAPES[shape]
    path = log._path(KEY)
    first, _, rest = path.read_bytes().replace(b"\r\n", b"\n").partition(b"\n")
    rest = rest.replace(b"\n", row_eol)
    path.write_bytes(first + head_eol + rest)
    log._invalidate_cache(KEY)
    return head_eol, rest


def _seed(tmp_path: Path) -> ConversationLog:
    log = ConversationLog(tmp_path)
    log.update_metadata(KEY, {"title": "Kept", "closed": True, "closed_at": 1.0})
    log.append(KEY, "user", "an earlier turn")
    log.append(KEY, "assistant", "a reply")
    return log


_REWRITERS: dict[str, Callable[[ConversationLog], None]] = {
    "update_metadata": lambda log: log.update_metadata(KEY, {"title": "Renamed"}),
    "clear_closed": lambda log: log.clear_closed(KEY),
    "mark_consolidated": lambda log: log.mark_consolidated(KEY, 1),
    "advance_consolidation_sub_offset": lambda log: log.advance_consolidation_sub_offset(
        KEY, 0, 5, 0
    ),
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("rewriter", sorted(_REWRITERS))
def test_a_first_line_rewrite_keeps_every_later_byte_and_its_own_terminator(
    tmp_path: Path, rewriter: str, shape: str
) -> None:
    log = _seed(tmp_path)
    head_eol, rest = _terminate(log, shape)
    before = log._path(KEY).read_bytes()
    _REWRITERS[rewriter](log)
    after = log._path(KEY).read_bytes()
    # The rewrite happened: the line changed.
    assert after != before
    first, terminator, tail = after.partition(head_eol)
    assert terminator == head_eol
    # The line keeps the terminator it had; no stray ``\r`` is left inside it.
    assert b"\r" not in first and b"\n" not in first
    # Nothing after the line moved, byte for byte.
    assert tail == rest


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_a_restore_after_a_metadata_write_puts_the_file_back_byte_for_byte(
    tmp_path: Path, shape: str
) -> None:
    log = _seed(tmp_path)
    _terminate(log, shape)
    path = log._path(KEY)
    before = path.read_bytes()
    with log._locked(KEY):
        prior = log.first_line_bytes_locked(KEY)
        log._update_metadata_locked(KEY, {"title": "newborn"})
        written = log.first_line_bytes_locked(KEY)
    assert written is not None and path.read_bytes() != before
    assert log.restore_first_line_if(KEY, written, prior) is True
    assert path.read_bytes() == before
