"""A transcript row with no ``role`` or ``content`` does not break the replay.

The transcript is a file on disk, and its reader hands back every non-metadata
JSON object as a row. ``_quota_tail`` indexed ``m["role"]`` and ``m["content"]``,
so one hand-edited or torn row raised ``KeyError`` out of ``build_session_replay``
and the fallback recall, which ``build_session_context`` calls unguarded.
"""

from __future__ import annotations

import pytest

from kiro_crew.context import _recall_rows, build_session_replay


class _Log:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def read_messages(self, key: str) -> list[dict]:
        return list(self._rows)

    def read_messages_chained(self, key: str) -> list[dict]:
        return list(self._rows)


_ROWS = [
    {"role": "user", "content": "hello"},
    {"content": "a row with no role"},
    {"role": "assistant"},
    {"role": "assistant", "content": "hi there"},
]


@pytest.mark.parametrize("read", ["replay", "recall"])
def test_a_row_with_no_role_or_content_is_read_past(read: str) -> None:
    log = _Log(_ROWS)
    if read == "replay":
        text = build_session_replay(log, "dashboard:x")  # type: ignore[arg-type]
        assert "hello" in text and "hi there" in text
        assert "a row with no role" not in text
    else:
        rows = _recall_rows(log, "dashboard:x", conv_max=20)  # type: ignore[arg-type]
        assert [r["role"] for r in rows] == ["user", "assistant", "assistant"]
        assert [r["content"] for r in rows] == ["hello", "", "hi there"]
