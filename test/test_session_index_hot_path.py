"""The index write path's two hot-loop contracts, pinned.

``cjk_inventory`` is the projection the index's ``unicode61`` column stores, so
its output is on-disk contract: the dedupe-first scan must produce exactly what
the per-character reference walk produces — same characters, same
first-seen order — or existing rows and new rows disagree about the same text.
The reference walk lives HERE, as the regression guard: both classify through
``is_cjk_char``, so any divergence is a bug in the projection, not a drift in
the character set.

``backfill_index`` defers sessions whose file changed within the quiet window:
a session being written changes every turn, and re-indexing per turn re-reads
the whole transcript each time. The tests pin the deferral (a fresh file is
reported as ``deferred``, never indexed, and never as ``remaining`` — the
caller's busy cadence keys on ``remaining`` and coming straight back cannot
service a deferral), its end (a quiet file is indexed on the next pass), and
that going stale puts an indexed session back behind the window rather than
re-indexing it hot.

CJK text is written as escapes because this repository forbids literal Chinese
characters in test sources.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew._sqlite_compat import _CJK_RANGES, fts5_available, is_cjk_char
from kiro_crew.history import ConversationLog
from kiro_crew.history_index import cjk_inventory

requires_fts5 = pytest.mark.skipif(not fts5_available(), reason="SQLite built without FTS5")


def _reference_inventory(folded: str) -> str:
    """The per-character walk ``cjk_inventory`` replaced, kept as its oracle."""
    seen: dict[str, None] = {}
    for ch in folded:
        if ch not in seen and is_cjk_char(ch):
            seen[ch] = None
    return " ".join(seen)


#: Every range edge, its immediate outside neighbours, and representatives of
#: the scripts involved — the exact codepoints where a hand-built character
#: class would get an off-by-one wrong.
_BOUNDARY_CHARS = "".join(
    chr(cp) for lo, hi in _CJK_RANGES for cp in (lo - 1, lo, lo + 1, hi - 1, hi, hi + 1)
)

_CASES = {
    "empty": "",
    "ascii_only": "the quick brown fox 0123456789 {}[]()",
    "cjk_only": "\u4e00\u4e8c\u4e09\u3042\u30a2\u31f0\u3400\uf900",
    "mixed": "error in \u4f1a\u8a71 module: \u30c6\u30b9\u30c8 failed at line 42",
    "duplicates_keep_first_seen_order": "\u4e09\u4e00\u4e09\u4e8c\u4e00\u4e8c\u4e09",
    "astral_extension_b": "before \U00020000\U0002a700\U0002ebef after",
    "astral_non_cjk_emoji": "smile \U0001f600 flag \U0001f1ef\U0001f1f5 ok",
    "range_boundaries": _BOUNDARY_CHARS,
    "boundaries_interleaved_with_ascii": " a ".join(_BOUNDARY_CHARS),
    "lone_surrogate_is_not_cjk": "x\ud800y\u4e00z",
}


@pytest.mark.parametrize("folded", list(_CASES.values()), ids=list(_CASES.keys()))
def test_cjk_inventory_matches_the_per_character_walk(folded):
    assert cjk_inventory(folded) == _reference_inventory(folded)


def test_cjk_inventory_first_seen_order_survives_interleaving():
    # Distinct characters arriving out of codepoint order: the inventory must
    # report arrival order, which is what makes the value diffable.
    folded = "\u9fff a \u4e00 b \u30a2 c \u4e00 d \u9fff"
    assert cjk_inventory(folded) == "\u9fff \u4e00 \u30a2"


def test_cjk_inventory_of_pure_ascii_is_empty():
    assert cjk_inventory("no ideographs here at all") == ""


def _quiet(log: ConversationLog, key: str, secs: float = 300.0) -> None:
    """Backdate *key*'s transcript so it reads as untouched for *secs*."""
    path = log._path(key)
    st = path.stat()
    backdated = st.st_mtime_ns - int(secs * 1e9)
    os.utime(path, ns=(st.st_atime_ns, backdated))


@pytest.fixture
def log(tmp_path):
    log = ConversationLog(base_dir=tmp_path / "sessions")
    try:
        yield log
    finally:
        index = getattr(log._catalog_projection, "_index", None)
        if index is not None:
            index.close()


@requires_fts5
def test_a_freshly_written_session_is_deferred_not_indexed(log):
    log.append("live", "user", "a session still being written")

    report = log._catalog_projection.backfill_index(budget_secs=30)

    assert report["indexed"] == 0
    assert report["deferred"] == 1
    # Deferred is NOT remaining: coming straight back cannot service a
    # deferral, so it must not hold the caller at its busy cadence.
    assert report["remaining"] == 0
    assert "live" not in log._catalog_projection.search_index.indexed_keys()


@requires_fts5
def test_a_quiet_session_is_indexed(log):
    log.append("settled", "user", "a session nobody is writing to")
    _quiet(log, "settled")

    report = log._catalog_projection.backfill_index(budget_secs=30)

    assert report["indexed"] == 1
    assert report["remaining"] == 0
    assert report["deferred"] == 0
    assert "settled" in log._catalog_projection.search_index.indexed_keys()


@requires_fts5
def test_a_deferred_session_is_indexed_once_it_goes_quiet(log):
    log.append("live", "user", "first turn")
    assert log._catalog_projection.backfill_index(budget_secs=30)["indexed"] == 0

    _quiet(log, "live")
    report = log._catalog_projection.backfill_index(budget_secs=30)

    assert report["indexed"] == 1
    assert report["remaining"] == 0
    assert report["deferred"] == 0


@requires_fts5
def test_an_indexed_session_that_changes_goes_back_behind_the_window(log):
    log.append("busy", "user", "first turn")
    _quiet(log, "busy")
    assert log._catalog_projection.backfill_index(budget_secs=30)["indexed"] == 1

    log.append("busy", "user", "second turn")  # fresh mtime again
    report = log._catalog_projection.backfill_index(budget_secs=30)

    assert report["indexed"] == 0
    assert report["deferred"] == 1
    assert report["remaining"] == 0
