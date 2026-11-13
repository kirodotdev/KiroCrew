"""Every persisted-tag prune reads the vocabulary through one reader.

Three sites prune persisted tag ids against the vocabulary loaded from ``tags.json``:
the slot metadata restore (``metadata_codec._read_tags``), the folder / channel
inheritance filter (``validate_folder_tag_ids``) and the sidebar-column boot prune in
``load_tags``. A site with its own vocabulary set can disagree on shape (a non-string
column id would crash the columns load) or drift on the UNKNOWN / KNOWN-EMPTY rule.
These tests seed one store and assert every site answers exactly what
``prune_tag_ids(raw, known_tag_ids(state))`` answers, in each vocabulary state --
including a store that is readable but stale.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_tags import validate_folder_tag_ids
from kiro_crew.dashboard.slot_persistence import metadata_codec as codec
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.tag_vocabulary_read import known_tag_ids, prune_tag_ids

#: What a persisted value can carry: a listed id, an id the store does not list (a tag
#: applied after a stale snapshot, or a dangling id), a duplicate, and hand-edit junk.
RAW = ["old", "newer", "old", "", ["list-id"], 7]


def _write_store(home: Path, tags: object) -> None:
    (home / "tags.json").write_text(json.dumps(tags), encoding="utf-8")
    (home / "tag_boards.json").write_text(
        json.dumps([{"id": "c1", "name": "Col", "tag_ids": RAW, "mode": "any", "order": 0}]),
        encoding="utf-8",
    )


def _loaded(home: Path) -> DashboardState:
    st = DashboardState.__new__(DashboardState)
    st._tags = []
    st._unparsed_tag_entries = []
    st._tag_boards = []
    st._unparsed_tag_board_entries = []
    st._tags_authoritative = False
    st.load_tags()
    return st


def _slot_restore(tmp_path: Path, monkeypatch, loaded: DashboardState) -> list[str]:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state._tags = [dict(t) for t in loaded._tags]
    state._tags_authoritative = loaded._tags_authoritative
    meta = {"tags": list(RAW)}
    chosen = codec.Restore(name="r")
    slot = state.get_or_create_slot("r", **codec.slot_args(meta, chosen))
    codec.apply(state, slot, meta, chosen)
    return list(slot.tags)


# A stale-but-readable store lists only "old", though "newer" was applied since; it
# parses as KNOWN. An unreadable store is UNKNOWN; "[]" is KNOWN-EMPTY.
_STORES = {
    "stale-but-readable": ([{"id": "old", "name": "Old"}], ["old"]),
    "unknown": ("{not json", ["old", "newer"]),
    "known-empty": ([], []),
}


@pytest.mark.parametrize("store", sorted(_STORES))
def test_every_prune_site_answers_what_the_one_reader_answers(store, tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
    tags, expected = _STORES[store]
    if isinstance(tags, str):
        (tmp_path / "tags.json").write_text(tags, encoding="utf-8")
        (tmp_path / "tag_boards.json").write_text(
            json.dumps([{"id": "c1", "name": "Col", "tag_ids": RAW, "mode": "any"}]),
            encoding="utf-8",
        )
    else:
        _write_store(tmp_path, tags)
    st = _loaded(tmp_path)

    reader = prune_tag_ids(RAW, known_tag_ids(st))
    assert reader == expected
    # The column prune runs inside load_tags; a non-string id must not fail the load.
    assert st._tag_boards_load_failed is False
    assert st._tag_boards[0]["tag_ids"] == reader
    assert validate_folder_tag_ids(list(RAW), st) == reader
    assert _slot_restore(tmp_path, monkeypatch, st) == reader


def test_known_tag_ids_states():
    st = DashboardState.__new__(DashboardState)
    st._tags = [{"id": "a"}, {"id": ["bad"]}, {"id": ""}, "row"]
    st._tags_authoritative = False
    assert known_tag_ids(st) is None
    st._tags_authoritative = True
    assert known_tag_ids(st) == frozenset({"a"})
    st._tags = []
    assert known_tag_ids(st) == frozenset()


def test_prune_shape_guards_hold_in_every_state():
    assert prune_tag_ids("old", None) == []
    assert prune_tag_ids(None, frozenset({"old"})) == []
    assert prune_tag_ids(RAW, None) == ["old", "newer"]


def test_no_prune_site_reads_the_vocabulary_authority_flag_itself():
    """The UNKNOWN / KNOWN rule lives in the one reader; a prune site that reads
    ``_tags_authoritative`` again would be a second spelling of it. Writes (the loader
    and the create reconciliation set the flag) are fine; the agent-policy check, which
    refuses a removal and never prunes, is the only other reader."""
    src = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    read = re.compile(r"""(?:\.|["'])_tags_authoritative\b(?!["']?\s*(?::\s*bool\s*)?=(?!=))""")
    allowed = {"dashboard/tag_vocabulary_read.py", "dashboard/chat_tags_policy.py"}
    readers = sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if read.search(path.read_text(encoding="utf-8"))
    )
    assert [p for p in readers if p not in allowed] == []
