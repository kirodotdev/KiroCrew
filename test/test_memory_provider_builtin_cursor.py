"""The built-in memory provider's list cursor: an offset the store cannot take lists the first page.

``<section>:<offset>`` comes from the dashboard route's ``cursor`` query string. A
cursor that does not name a section and a count (``lesson:abc``) already lists the
first page; a digit string that ``int`` cannot read, or a count above what the index
binds as an ``OFFSET``, must read the same way instead of raising out of the list.
"""

from __future__ import annotations

import pytest

from kiro_crew.memory_provider.builtin import BUILTIN
from kiro_crew.memory_provider.contract import ProviderScope

SCOPE = ProviderScope("target", None, "persistent", "dashboard")
_SQLITE_INTEGER_MAX = 2**63 - 1


class _RecordingIndex:
    """The three list readers of the memory index; each notes the offset it was asked for."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, int]] = []

    def _note(self, section: str, offset: int) -> None:
        if offset > _SQLITE_INTEGER_MAX:
            raise OverflowError("Python int too large to convert to SQLite INTEGER")
        self.asked.append((section, offset))

    def get_lessons(self, limit: int, offset: int) -> list:
        self._note("lesson", offset)
        return []

    def get_all_semantic(self, limit: int, offset: int, exclude_lessons: bool) -> list:
        self._note("fact", offset)
        return []

    def get_episodic_list(self, limit: int, offset: int, tag_filter: object) -> list:
        self._note("episode", offset)
        return []


@pytest.fixture
def index(monkeypatch: pytest.MonkeyPatch) -> _RecordingIndex:
    recording = _RecordingIndex()
    monkeypatch.setattr(type(BUILTIN), "_vectors", lambda _self, _scope: recording)
    monkeypatch.setattr(type(BUILTIN), "_jsonl_lessons", lambda _self, _scope, _vectors: [])
    monkeypatch.setattr(type(BUILTIN), "_documents", lambda _self, _scope, _vectors: [])
    return recording


@pytest.mark.parametrize(
    "cursor",
    [
        "lesson:\u00b2",
        "lesson:\u2460",
        "lesson:" + str(_SQLITE_INTEGER_MAX + 1),
        "lesson:" + "9" * 19,
        "lesson:" + "9" * 5000,
    ],
    ids=["superscript-two", "circled-one", "one-past-64-bits", "19-digits", "5000-digits"],
)
def test_an_offset_the_store_cannot_take_lists_the_first_page(index, cursor) -> None:
    page = BUILTIN.blocking_list(SCOPE, cursor, None, None)
    assert page.items == ()
    assert index.asked[0] == ("lesson", 0)


@pytest.mark.parametrize(
    ("cursor", "first_read"),
    [
        ("lesson:5", ("lesson", 5)),
        ("fact:7", ("fact", 7)),
        ("lesson:" + "9" * 18, ("lesson", 10**18 - 1)),
        ("lesson:" + str(_SQLITE_INTEGER_MAX), ("lesson", _SQLITE_INTEGER_MAX)),
        ("lesson:\u0663\u0660", ("lesson", 30)),
        ("lesson:abc", ("lesson", 0)),
        ("shelf:5", ("lesson", 0)),
        (None, ("lesson", 0)),
    ],
    ids=[
        "offset",
        "other-section",
        "18-digits",
        "largest-offset",
        "arabic-indic",
        "junk",
        "unknown",
        "none",
    ],
)
def test_a_readable_cursor_and_every_other_junk_cursor_read_as_before(
    index, cursor, first_read
) -> None:
    BUILTIN.blocking_list(SCOPE, cursor, None, None)
    assert index.asked[0] == first_read
