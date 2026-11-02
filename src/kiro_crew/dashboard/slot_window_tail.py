"""The newest window of a transcript, read through its page index.

A sidebar row's window holds the newest rows of its transcript; the rows before
them stay on disk and page in when someone scrolls up. Building that window from
a full read parses every row ever written. The chained page read decodes only
the newest page (its row index is persisted beside the transcript), and returns
the same rows and the same total the full read gives.

The window also needs how many of the older rows are durable
(``durable_row_count``). That count equals the older row count when no row on
disk carries a transient role, which a byte search over the files proves without
parsing them. When the search finds one, or anything here cannot be read the
fast way, :func:`read_window_tail` returns ``None`` and the caller does the full
read.
"""

from __future__ import annotations

import mmap
import re
from typing import TYPE_CHECKING, NamedTuple

from kiro_crew.dashboard.state import _TRANSIENT_ROLES

if TYPE_CHECKING:
    from pathlib import Path

    from kiro_crew.history import ConversationLog

#: Any row whose role is transient. A match inside another row's payload only
#: sends the read down the full path, so a false hit costs time, never a row.
_TRANSIENT_ROLE = re.compile(
    rb'"role"\s*:\s*"(?:'
    + b"|".join(re.escape(r.encode()) for r in sorted(_TRANSIENT_ROLES))
    + rb')"'
)


class WindowTail(NamedTuple):
    """The newest *rows* of a transcript of *total* rows, *older_durable* of the older ones durable."""

    rows: list[dict]
    total: int
    older_durable: int


def _may_hold_transient_rows(path: Path) -> bool:
    """Whether *path* may hold a row with a transient role. BLOCKING."""
    try:
        with open(path, "rb") as handle:
            if handle.seek(0, 2) == 0:
                return False
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as view:
                return _TRANSIENT_ROLE.search(view) is not None  # type: ignore[call-overload]
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True


def read_row_page(
    conv_log: ConversationLog, key: str, *, limit: int, before: int | None
) -> tuple[list[dict], int, bool, int] | None:
    """One display page of a sidebar row whose window has not loaded, or ``None``. BLOCKING.

    Such a row holds no rows in memory, so its transcript on disk is the whole
    conversation and the page is a plain ranged read: it decodes only the rows
    the page returns and leaves the window unloaded. The same transient-role
    search as :func:`read_window_tail` guards it, because a raw disk row then
    equals its display row (no ``chunk`` run to fold, no ``done`` to drop).
    ``None`` sends the caller down its usual path, which loads the window.
    """
    try:
        keys = conv_log.chained_keys(key) or [key]
        if any(_may_hold_transient_rows(conv_log._path(k)) for k in keys):
            return None
        probe = conv_log.read_messages_chained_page(key, limit=0)
        end = probe.total if before is None else max(0, min(before, probe.total))
        start = max(0, end - limit)
        rows = (
            conv_log.read_messages_chained_page(
                key, limit=end - start, before=end, expected_revision=probe.revision
            ).messages
            if end > start
            else []
        )
    except Exception:  # noqa: BLE001 -- the usual path is the authority; let it answer
        return None
    return rows, probe.total, start > 0, start


def read_window_tail(conv_log: ConversationLog, key: str, window: int) -> WindowTail | None:
    """The newest *window* rows of *key*'s chained transcript, or ``None``. BLOCKING.

    ``None`` means the caller must read the transcript in full: a file may hold
    a transient row, or the page read refused this transcript.
    """
    try:
        keys = conv_log.chained_keys(key) or [key]
        if any(_may_hold_transient_rows(conv_log._path(k)) for k in keys):
            return None
        page = conv_log.read_messages_chained_page(key, limit=window)
    except Exception:  # noqa: BLE001 -- the full read is the authority; let it answer
        return None
    older = page.total - len(page.messages)
    if older < 0:
        return None
    return WindowTail(page.messages, page.total, older)
