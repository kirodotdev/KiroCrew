"""SessionMap per-key flags: malformed persisted values and the explicit effort Default.

A persisted ``flags`` value that is not a mapping reads as empty and is replaced
on the next write; only a literal ``True`` under ``EXPLICIT_EFFORT_DEFAULT_FLAG``
is a pending retained Default, and the rows holding one are capped.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from session_map_mirror_helpers import session_map_fixture  # noqa: F401

from kiro_crew import session_map as session_map_module
from kiro_crew.session import SessionManager
from kiro_crew.session_map import (
    EXPLICIT_EFFORT_DEFAULT_FLAG,
    SUPPRESS_REPLAY_FLAG,
    EffortDefaultRowRefused,
    SessionMap,
)


class TestMalformedPersistedFlags:
    """Malformed persisted flag values act as an empty mapping."""

    @staticmethod
    def _load_with_flags(tmp_path, flags):
        key = "dashboard:malformed-flags"
        (tmp_path / "session_map.json").write_text(
            json.dumps({key: {"sid": "", "flags": flags}}), encoding="utf-8"
        )
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            return key, SessionMap()

    @pytest.mark.parametrize("flags", [["not", "a", "mapping"], "not a mapping", 1])
    def test_get_flag_reads_malformed_persisted_flags_as_empty(self, tmp_path, flags):
        key, session_map = self._load_with_flags(tmp_path, flags)

        assert session_map.get_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG) is False
        assert session_map.get_flag(key, SUPPRESS_REPLAY_FLAG) is False

    @pytest.mark.parametrize("flags", [["not", "a", "mapping"], "not a mapping", 1])
    def test_set_flag_replaces_malformed_persisted_flags(self, tmp_path, flags):
        key, session_map = self._load_with_flags(tmp_path, flags)

        session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, True)

        assert session_map._data[key]["flags"] == {EXPLICIT_EFFORT_DEFAULT_FLAG: True}

    @pytest.mark.parametrize("flags", [["not", "a", "mapping"], "not a mapping", 1])
    def test_clearing_flag_removes_malformed_persisted_flags(self, tmp_path, flags):
        key, session_map = self._load_with_flags(tmp_path, flags)

        session_map.set_flag(key, SUPPRESS_REPLAY_FLAG, False)

        assert "flags" not in session_map._data[key]


class TestExplicitEffortDefaultFlag:
    @pytest.mark.parametrize(
        ("value", "is_set"),
        [
            ("false", False),
            ("true", False),
            (1, False),
            (0, False),
            (None, False),
            ({}, False),
            (True, True),
        ],
    )
    def test_only_literal_true_is_a_pending_retained_default(
        self, session_map, monkeypatch, value, is_set
    ):
        key = "dashboard:explicit-default"
        session_map._data[key] = {"sid": "", "flags": {EXPLICIT_EFFORT_DEFAULT_FLAG: value}}
        manager = SessionManager.__new__(SessionManager)
        manager._session_map = session_map
        manager._fold_key = lambda candidate: candidate
        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 1)

        assert session_map.get_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG) is is_set
        assert manager.explicit_effort_default_pending(key) is is_set
        assert session_map._explicit_effort_default_rows_counted() == int(is_set)
        if is_set:
            with pytest.raises(EffortDefaultRowRefused):
                session_map.set_flag("dashboard:next", EXPLICIT_EFFORT_DEFAULT_FLAG, True)
            assert session_map.prune() == 0
            assert key in session_map._data
        else:
            session_map.set_flag("dashboard:next", EXPLICIT_EFFORT_DEFAULT_FLAG, True)
            session_map.set_flag("dashboard:next", EXPLICIT_EFFORT_DEFAULT_FLAG, False)
            assert session_map.prune() == 1
            assert key not in session_map._data

    def test_prune_preserves_a_sidless_explicit_default_intent(self, session_map):
        key = "dashboard:explicit-default"
        session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, True)

        assert session_map.prune() == 0
        assert session_map.get_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG) is True

        # The row existed only to carry the flag, so clearing it removes the row
        # at once rather than leaving it for a later prune.
        session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, False)
        assert session_map.get_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG) is False
        assert key not in session_map._data
        assert session_map.prune() == 0

    def test_clearing_the_flag_keeps_a_row_that_holds_other_state(self, session_map):
        key = "dashboard:ran-before"
        session_map.set(key, "sid-1")
        session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, True)

        session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, False)

        assert session_map._data[key] == {
            "sid": "sid-1",
            "slack_thread_ts": None,
            "slack_channel_id": None,
        }

    def test_picks_on_new_keys_leave_no_row_behind(self, session_map, monkeypatch):
        # Every row the flag creates is counted against its bound until the
        # flag is cleared; a row left behind after that would be counted by no
        # bound, one per new chat.
        from kiro_crew import session_map as session_map_module

        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 1)
        for index in range(5):
            key = f"dashboard:new-chat-{index}"
            session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, True)
            session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, False)

        assert not [key for key in session_map._data if key.startswith("dashboard:new-chat-")]

    def test_cap_refuses_a_new_default_row_without_writing(self, session_map, monkeypatch):
        from kiro_crew import session_map as session_map_module

        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 1)
        session_map.set_flag("dashboard:first", EXPLICIT_EFFORT_DEFAULT_FLAG, True)

        with pytest.raises(EffortDefaultRowRefused) as exc_info:
            session_map.set_flag("dashboard:second", EXPLICIT_EFFORT_DEFAULT_FLAG, True)

        assert exc_info.value.reason == "limit"
        assert "dashboard:second" not in session_map._data

    def test_a_row_with_malformed_flags_does_not_break_the_bound(self, session_map):
        session_map._data["dashboard:corrupt"] = {"sid": None, "flags": ["not", "an", "object"]}

        session_map.set_flag("dashboard:ok", EXPLICIT_EFFORT_DEFAULT_FLAG, True)

        assert session_map.get_flag("dashboard:ok", EXPLICIT_EFFORT_DEFAULT_FLAG) is True

    def test_a_held_row_stays_counted_and_its_key_arms_into_it(self, session_map, monkeypatch):
        from kiro_crew import session_map as session_map_module

        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 1)
        session_map.set_flag("dashboard:spent", EXPLICIT_EFFORT_DEFAULT_FLAG, True)

        with session_map.hold_explicit_effort_default("dashboard:spent"):
            session_map.set_flag("dashboard:spent", EXPLICIT_EFFORT_DEFAULT_FLAG, False)
            # The row is still the spent key's, so a newcomer cannot take it...
            with pytest.raises(EffortDefaultRowRefused) as exc_info:
                session_map.set_flag("dashboard:newcomer", EXPLICIT_EFFORT_DEFAULT_FLAG, True)
            # ...and the spent key arms into it although the bound is full.
            session_map.set_flag("dashboard:spent", EXPLICIT_EFFORT_DEFAULT_FLAG, True)

        assert exc_info.value.reason == "limit"
        assert "dashboard:newcomer" not in session_map._data
        assert session_map.get_flag("dashboard:spent", EXPLICIT_EFFORT_DEFAULT_FLAG) is True
        with pytest.raises(EffortDefaultRowRefused):
            session_map.set_flag("dashboard:newcomer", EXPLICIT_EFFORT_DEFAULT_FLAG, True)

    def test_a_released_hold_frees_the_row_its_key_left_clear(self, session_map, monkeypatch):
        from kiro_crew import session_map as session_map_module

        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 1)
        session_map.set_flag("dashboard:spent", EXPLICIT_EFFORT_DEFAULT_FLAG, True)

        with session_map.hold_explicit_effort_default("dashboard:spent"):
            session_map.set_flag("dashboard:spent", EXPLICIT_EFFORT_DEFAULT_FLAG, False)

        assert session_map.explicit_effort_default_held("dashboard:spent") is False
        session_map.set_flag("dashboard:newcomer", EXPLICIT_EFFORT_DEFAULT_FLAG, True)
        assert session_map.get_flag("dashboard:newcomer", EXPLICIT_EFFORT_DEFAULT_FLAG) is True

    def test_a_hold_on_a_key_without_a_row_holds_nothing(self, session_map, monkeypatch):
        from kiro_crew import session_map as session_map_module

        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 1)

        with session_map.hold_explicit_effort_default("dashboard:never-flagged"):
            assert session_map.explicit_effort_default_held("dashboard:never-flagged") is False
            session_map.set_flag("dashboard:newcomer", EXPLICIT_EFFORT_DEFAULT_FLAG, True)
            # A hold adds no row, so it cannot let its key arm past the bound.
            with pytest.raises(EffortDefaultRowRefused):
                session_map.set_flag("dashboard:never-flagged", EXPLICIT_EFFORT_DEFAULT_FLAG, True)

    def test_overlapping_holds_count_one_row_until_the_last_ends(self, session_map, monkeypatch):
        from kiro_crew import session_map as session_map_module

        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 2)
        key = "dashboard:spent"
        session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, True)

        with session_map.hold_explicit_effort_default(key):
            with session_map.hold_explicit_effort_default(key):
                session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, False)
                session_map.set_flag("dashboard:second", EXPLICIT_EFFORT_DEFAULT_FLAG, True)
            assert session_map.explicit_effort_default_held(key) is True
            with pytest.raises(EffortDefaultRowRefused):
                session_map.set_flag("dashboard:third", EXPLICIT_EFFORT_DEFAULT_FLAG, True)

        assert session_map.explicit_effort_default_held(key) is False
        session_map.set_flag("dashboard:third", EXPLICIT_EFFORT_DEFAULT_FLAG, True)

    def test_a_hold_is_released_when_its_block_raises(self, session_map):
        key = "dashboard:spent"
        session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, True)

        with pytest.raises(RuntimeError, match="boom"):
            with session_map.hold_explicit_effort_default(key):
                raise RuntimeError("boom")

        assert session_map.explicit_effort_default_held(key) is False

    def test_a_hold_does_not_lift_the_key_bound(self, session_map):
        from kiro_crew.session_map import PRIVACY_ROW_KEY_MAX

        # A row too long to admit can still arrive in a map file an older build wrote.
        key = "x" * (PRIVACY_ROW_KEY_MAX + 1)
        session_map._data[key] = {"sid": None, "flags": {EXPLICIT_EFFORT_DEFAULT_FLAG: True}}

        with session_map.hold_explicit_effort_default(key):
            session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, False)
            with pytest.raises(EffortDefaultRowRefused) as exc_info:
                session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, True)

        assert exc_info.value.reason == "key_too_long"

    def test_manager_arms_again_the_spelling_its_hold_kept(self, session_map, monkeypatch):
        from kiro_crew import session_map as session_map_module

        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 1)
        manager = SessionManager.__new__(SessionManager)
        manager._session_map = session_map
        manager._fold_key = lambda key: "dashboard:folded"
        # Written while the key folded to itself, before a live session changed that.
        session_map.set_flag("raw-key", EXPLICIT_EFFORT_DEFAULT_FLAG, True)

        with manager.hold_explicit_effort_default("raw-key"):
            manager.set_explicit_effort_default("raw-key", False)
            with pytest.raises(EffortDefaultRowRefused):
                session_map.set_flag("dashboard:newcomer", EXPLICIT_EFFORT_DEFAULT_FLAG, True)
            assert manager.set_explicit_effort_default("raw-key", True) is True

        # Armed in the row the hold kept, so the bound still counts one row.
        assert session_map.get_flag("raw-key", EXPLICIT_EFFORT_DEFAULT_FLAG) is True
        assert session_map.get_flag("dashboard:folded", EXPLICIT_EFFORT_DEFAULT_FLAG) is False
        assert manager.explicit_effort_default_pending("raw-key") is True

    def test_overlong_default_key_is_refused_without_writing(self, session_map):
        from kiro_crew.session_map import PRIVACY_ROW_KEY_MAX

        key = "x" * (PRIVACY_ROW_KEY_MAX + 1)
        with pytest.raises(EffortDefaultRowRefused) as exc_info:
            session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, True)

        assert exc_info.value.reason == "key_too_long"
        assert key not in session_map._data

    def test_clearing_and_resetting_an_existing_default_row_is_not_refused(
        self, session_map, monkeypatch
    ):
        from kiro_crew import session_map as session_map_module

        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 1)
        key = "dashboard:explicit-default"
        session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, True)
        session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, True)
        session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, False)
        session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, True)

        assert session_map.get_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG) is True

    def test_manager_refuses_an_unretainable_default_row(self, session_map, monkeypatch):
        from kiro_crew import session_map as session_map_module

        manager = SessionManager.__new__(SessionManager)
        manager._session_map = session_map
        manager._fold_key = lambda key: key
        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 0)

        assert manager.set_explicit_effort_default("dashboard:refused", True) is False
        assert "dashboard:refused" not in session_map._data

    def test_manager_reads_and_clears_folded_and_raw_spellings(self, session_map):
        manager = SessionManager.__new__(SessionManager)
        manager._session_map = session_map
        manager._fold_key = lambda key: "dashboard:folded"

        manager.set_explicit_effort_default("raw-key", True)
        assert session_map.get_flag("dashboard:folded", EXPLICIT_EFFORT_DEFAULT_FLAG) is True
        assert session_map.get_flag("raw-key", EXPLICIT_EFFORT_DEFAULT_FLAG) is False
        assert manager.explicit_effort_default_pending("raw-key") is True

        # Read like the replay-suppression flag: either spelling counts, and a
        # clear removes both.
        manager.set_explicit_effort_default("raw-key", False)
        session_map.set_flag("raw-key", EXPLICIT_EFFORT_DEFAULT_FLAG, True)
        assert manager.explicit_effort_default_pending("raw-key") is True
        manager.set_explicit_effort_default("raw-key", False)
        assert manager.explicit_effort_default_pending("raw-key") is False
        assert session_map.get_flag("raw-key", EXPLICIT_EFFORT_DEFAULT_FLAG) is False
