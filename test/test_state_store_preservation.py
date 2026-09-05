"""Malformed-entry preservation across the sibling JSON stores in state.py.

Regression coverage for #5792: ``load_*`` drops rows the active list cannot
use but must keep them verbatim in an ``_unparsed_*`` list so the next
``save_*`` round-trips them back to disk, rather than the previous behaviour
where a dropped row's bytes were erased on the next write. Mirrors the
cron-folder contract landed in #5768 across ``chat_pins``, ``tags`` and
``tag_boards`` — the ``tags`` case is the worst because its save runs DURING
load (seed / back-fill), so a hand-edited typo was wiped at boot with no user
action.

These use ``DashboardState.__new__`` + a ``config_dir`` monkeypatch, the same
lightweight load/save harness the cron-folder tests use — no event loop, no
full DashboardState construction.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.dashboard.state import DashboardState


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path, raising=False)
    return tmp_path


# --------------------------------------------------------------------------- #
# chat_pins
# --------------------------------------------------------------------------- #
class TestChatPinsPreservation:
    def _fresh(self):
        st = DashboardState.__new__(DashboardState)
        st._chat_pins = []
        st._unparsed_chat_pin_entries = []
        return st

    def _valid_pin(self, pin_id):
        return {
            "id": pin_id,
            "slot_key": "slot1",
            "mid": "m-" + pin_id,
            "preview": "hello",
            "pinned_at": "2026-01-01T00:00:00Z",
        }

    def test_load_keeps_malformed_entries_inactive(self, cfg):
        (cfg / "chat_pins.json").write_text(
            json.dumps(
                [
                    self._valid_pin("good1"),
                    "not-a-dict",
                    {"id": "no-slot", "mid": "x", "preview": "p", "pinned_at": "t"},
                    {"slot_key": "s", "mid": "x", "preview": "p", "pinned_at": "t"},  # no id
                    {  # no identity field (no mid, no message_ts)
                        "id": "no-ident",
                        "slot_key": "s",
                        "preview": "p",
                        "pinned_at": "t",
                    },
                    self._valid_pin("good2"),
                ]
            ),
            encoding="utf-8",
        )
        st = self._fresh()
        st.load_chat_pins()
        assert [p["id"] for p in st._chat_pins] == ["good1", "good2"]
        assert len(st._unparsed_chat_pin_entries) == 4
        assert "not-a-dict" in st._unparsed_chat_pin_entries

    def test_malformed_entry_survives_a_subsequent_save(self, cfg):
        """A hand-edited pin with a typo must not be erased when an unrelated
        save fires."""
        path = cfg / "chat_pins.json"
        malformed = {"id": "bbb", "slot_key": "s", "preview": "typo", "pinnd_at": "t", "mid": "m"}
        path.write_text(
            json.dumps([self._valid_pin("aaa"), malformed, self._valid_pin("ccc")]),
            encoding="utf-8",
        )
        st = self._fresh()
        st.load_chat_pins()
        assert [p["id"] for p in st._chat_pins] == ["aaa", "ccc"]

        # An unrelated pin write persists — the malformed entry must ride along.
        st.save_chat_pins()

        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert malformed in on_disk
        assert {
            p["id"] for p in on_disk if isinstance(p, dict) and "slot_key" in p and p.get("id")
        } >= {
            "aaa",
            "ccc",
        }

    def test_no_unparsed_entries_leaves_payload_clean(self, cfg):
        path = cfg / "chat_pins.json"
        path.write_text(json.dumps([self._valid_pin("aaa")]), encoding="utf-8")
        st = self._fresh()
        st.load_chat_pins()
        assert st._unparsed_chat_pin_entries == []
        st.save_chat_pins()
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert [p["id"] for p in on_disk] == ["aaa"]

    def test_whole_file_parse_error_does_not_clear_prior_preservation(self, cfg):
        """A later unreadable read must not wipe entries preserved by an earlier
        good read (whole-file corruption is out of the per-entry contract)."""
        st = self._fresh()
        st._unparsed_chat_pin_entries = [{"stale": "kept"}]
        (cfg / "chat_pins.json").write_text("{ not json", encoding="utf-8")
        st.load_chat_pins()
        assert st._chat_pins == []
        assert st._unparsed_chat_pin_entries == [{"stale": "kept"}]

    def test_oversized_records_are_dropped_and_preserved(self, cfg):
        """A hand-edited pin exceeding the create-time length caps must not be
        served: it is dropped from the active list and preserved verbatim in
        ``_unparsed`` (mirrors api_chat_pins_create's ingress caps)."""
        from kiro_crew.dashboard.chat_pins import (
            _MAX_MESSAGE_TS_CHARS,
            _MAX_MID_CHARS,
            _MAX_PREVIEW_INPUT_CHARS,
        )

        big_preview = dict(self._valid_pin("bigprev"), preview="x" * (_MAX_PREVIEW_INPUT_CHARS + 1))
        big_mid = dict(self._valid_pin("bigmid"), mid="m" * (_MAX_MID_CHARS + 1))
        big_ts = {
            "id": "bigts",
            "slot_key": "slot1",
            "message_ts": "t" * (_MAX_MESSAGE_TS_CHARS + 1),
            "preview": "ok",
            "pinned_at": "2026-01-01T00:00:00Z",
        }
        (cfg / "chat_pins.json").write_text(
            json.dumps([self._valid_pin("good1"), big_preview, big_mid, big_ts]),
            encoding="utf-8",
        )
        st = self._fresh()
        st.load_chat_pins()
        assert [p["id"] for p in st._chat_pins] == ["good1"]
        assert big_preview in st._unparsed_chat_pin_entries
        assert big_mid in st._unparsed_chat_pin_entries
        assert big_ts in st._unparsed_chat_pin_entries

    def test_non_string_identity_does_not_crash_the_loader(self, cfg):
        """A hand-edited record with a non-string ``mid`` (e.g. a JSON number)
        but a valid string ``message_ts`` must not crash the loader when the
        length caps are applied: ``len()`` is only taken on string identities.
        Before the isinstance guard, ``len(123)`` raised TypeError and took the
        whole loader (and startup) down. The record remains valid via its
        string ``message_ts`` identity; the point is that loading does not
        raise."""
        numeric_mid = {
            "id": "nummid",
            "slot_key": "slot1",
            "mid": 123,  # non-string identity — must not reach len()
            "message_ts": "2026-01-01T00:00:00Z",
            "preview": "ok",
            "pinned_at": "2026-01-01T00:00:00Z",
        }
        (cfg / "chat_pins.json").write_text(
            json.dumps([self._valid_pin("good1"), numeric_mid]),
            encoding="utf-8",
        )
        st = self._fresh()
        st.load_chat_pins()  # must not raise TypeError
        # good1 always loads; the numeric-mid record survives via its valid
        # string message_ts identity. The regression is the crash, not the
        # record's fate — assert the loader completed and good1 is present.
        assert "good1" in [p["id"] for p in st._chat_pins]


# --------------------------------------------------------------------------- #
# tags — the boot-erasure case
# --------------------------------------------------------------------------- #
class TestTagsPreservation:
    def _fresh(self):
        st = DashboardState.__new__(DashboardState)
        st._tags = []
        st._unparsed_tag_entries = []
        st._tag_boards = []
        st._unparsed_tag_board_entries = []
        st._committed_tag_ids = None
        return st

    def test_load_keeps_malformed_tag_entries_inactive(self, cfg):
        (cfg / "tags.json").write_text(
            json.dumps(
                [
                    {"id": "t1", "name": "Keep", "status": True},
                    "not-a-dict",
                    {"name": "no id"},
                    {"id": "", "name": "empty id"},
                    {"id": "t2", "name": "Also keep", "status": False},
                ]
            ),
            encoding="utf-8",
        )
        st = self._fresh()
        st.load_tags()
        assert [t["id"] for t in st._tags] == ["t1", "t2"]
        assert len(st._unparsed_tag_entries) == 3
        assert "not-a-dict" in st._unparsed_tag_entries

    def test_malformed_tag_survives_the_boot_save(self, cfg):
        """The regression: load_tags back-fills the ``status`` flag and calls
        ``save_tags()`` DURING load. A hand-edited malformed row must survive
        that boot-time save rather than being erased with no user action."""
        path = cfg / "tags.json"
        malformed = {"nome": "typo-key", "colour": "red"}  # no id -> dropped
        # A valid row WITHOUT the status field forces the back-fill save to run.
        path.write_text(
            json.dumps([{"id": "t1", "name": "Keep"}, malformed]),
            encoding="utf-8",
        )
        st = self._fresh()
        st.load_tags()  # triggers the status back-fill save internally
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert malformed in on_disk
        assert any(isinstance(t, dict) and t.get("id") == "t1" for t in on_disk)

    def test_malformed_tag_survives_snapshot_save(self, cfg):
        path = cfg / "tags.json"
        malformed = {"broken": True}
        path.write_text(
            json.dumps([{"id": "t1", "name": "Keep", "status": False}, malformed]),
            encoding="utf-8",
        )
        st = self._fresh()
        st.load_tags()
        # The chat_tags.py write path captures an active snapshot then persists.
        snapshot = [dict(t) for t in st._tags]
        st.save_tags_snapshot(snapshot)
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert malformed in on_disk
        assert any(isinstance(t, dict) and t.get("id") == "t1" for t in on_disk)

    def test_no_unparsed_tags_leaves_payload_clean(self, cfg):
        path = cfg / "tags.json"
        path.write_text(
            json.dumps([{"id": "t1", "name": "Keep", "status": False}]), encoding="utf-8"
        )
        st = self._fresh()
        st.load_tags()
        assert st._unparsed_tag_entries == []


# --------------------------------------------------------------------------- #
# tag_boards (sidebar columns)
# --------------------------------------------------------------------------- #
class TestTagBoardsPreservation:
    def _fresh(self):
        st = DashboardState.__new__(DashboardState)
        st._tags = []
        st._unparsed_tag_entries = []
        st._tag_boards = []
        st._unparsed_tag_board_entries = []
        st._committed_tag_ids = None
        return st

    def test_load_keeps_malformed_columns_inactive(self, cfg):
        # tags.json present-but-empty so load_tags does not seed and the board
        # block runs cleanly.
        (cfg / "tags.json").write_text(json.dumps([]), encoding="utf-8")
        (cfg / "tag_boards.json").write_text(
            json.dumps(
                [
                    {"id": "c1", "name": "Col", "tag_ids": [], "order": 0},
                    "not-a-dict",
                    {"name": "no id"},
                    {"id": "c2", "name": "Col2", "tag_ids": [], "order": 1},
                ]
            ),
            encoding="utf-8",
        )
        st = self._fresh()
        st.load_tags()
        assert [c["id"] for c in st._tag_boards] == ["c1", "c2"]
        assert len(st._unparsed_tag_board_entries) == 2
        assert "not-a-dict" in st._unparsed_tag_board_entries

    def test_malformed_column_survives_a_subsequent_save(self, cfg):
        (cfg / "tags.json").write_text(json.dumps([]), encoding="utf-8")
        path = cfg / "tag_boards.json"
        malformed = {"nme": "typo"}  # no id -> dropped
        path.write_text(
            json.dumps([{"id": "c1", "name": "Col", "tag_ids": [], "order": 0}, malformed]),
            encoding="utf-8",
        )
        st = self._fresh()
        st.load_tags()
        assert [c["id"] for c in st._tag_boards] == ["c1"]

        # The chat_tags.py board write path persists a captured snapshot.
        snapshot = [dict(c) for c in st._tag_boards]
        st.save_tag_boards_snapshot(snapshot)
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert malformed in on_disk
        assert any(isinstance(c, dict) and c.get("id") == "c1" for c in on_disk)

    def test_no_unparsed_columns_leaves_payload_clean(self, cfg):
        (cfg / "tags.json").write_text(json.dumps([]), encoding="utf-8")
        path = cfg / "tag_boards.json"
        path.write_text(
            json.dumps([{"id": "c1", "name": "Col", "tag_ids": [], "order": 0}]),
            encoding="utf-8",
        )
        st = self._fresh()
        st.load_tags()
        assert st._unparsed_tag_board_entries == []
        st.save_tag_boards()
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert [c["id"] for c in on_disk] == ["c1"]


# --------------------------------------------------------------------------- #
# Shared helpers (#6326) — the partition/append mechanics all four stores share
# --------------------------------------------------------------------------- #
class TestPartitionPreserving:
    """Direct coverage of the extracted ``_partition_preserving`` helper.

    The four stores keep their own predicates; this asserts the shared
    mechanics: order-preserving split into (active, unparsed), and a single
    preserving-N warning emitted only when a row is dropped.
    """

    def test_splits_preserving_order(self):
        raw = [{"id": "a"}, "bad", {"id": "b"}, {"no": "id"}]
        active, unparsed = DashboardState._partition_preserving(
            raw,
            lambda r: isinstance(r, dict) and bool(r.get("id")),
            "entr(ies)",
            "tags.json",
        )
        assert active == [{"id": "a"}, {"id": "b"}]
        assert unparsed == ["bad", {"no": "id"}]

    def test_all_valid_produces_no_unparsed_and_no_warning(self, caplog):
        raw = [{"id": "a"}, {"id": "b"}]
        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.state"):
            active, unparsed = DashboardState._partition_preserving(
                raw, lambda r: bool(r.get("id")), "entr(ies)", "tags.json"
            )
        assert active == raw
        assert unparsed == []
        assert not [r for r in caplog.records if "Preserving" in r.message]

    def test_warns_once_with_count_noun_and_file(self, caplog):
        raw = ["x", {"id": "ok"}, "y"]
        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.state"):
            _, unparsed = DashboardState._partition_preserving(
                raw,
                lambda r: isinstance(r, dict) and bool(r.get("id")),
                "chat pin record(s)",
                "chat_pins.json",
            )
        assert unparsed == ["x", "y"]
        warnings = [r.message for r in caplog.records if "Preserving" in r.message]
        assert len(warnings) == 1
        assert (
            "Preserving 2 malformed chat pin record(s) while loading chat_pins.json" in warnings[0]
        )

    def test_empty_input_is_noop(self, caplog):
        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.state"):
            active, unparsed = DashboardState._partition_preserving(
                [], lambda r: True, "entr(ies)", "tags.json"
            )
        assert active == []
        assert unparsed == []
        assert not [r for r in caplog.records if "Preserving" in r.message]
