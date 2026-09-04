"""A message's recorded origin survives being loaded and written back.

A channel tab and its channel share ONE transcript, so the window a dashboard
save re-serializes can hold turns that arrived from Slack or Discord. Those
turns must still name their real origin afterwards: ``source_thread`` is what
``ConversationLog.get_source_threads`` cites across sessions and what SEL
attribution and per-surface filtering read.

The load paths are the load-bearing half. Preserving provenance only in the
write path is silently vacuous for anything read back from disk, because the
in-memory message dict is built by ``_ChatSlot.append``, which has no
provenance argument — so a save would find nothing to preserve and fall back to
"dashboard" on every restored line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.chat_persistence import (
    _build_message_entry,
    _rehydrate_slot_from_history,
    _save_slot_to_history,
    restore_recent_sessions,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog, carry_provenance

SLACK_THREAD = "slack:1785861252.833429"
SLACK_USER = "W017SQBPZBN"


def _make_state(tmp_path: Path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    sessions.channel_key_for_stem = MagicMock(return_value=None)
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


def _write_session(
    tmp_path: Path, key: str, messages: list[dict], meta: dict | None = None
) -> Path:
    path = tmp_path / f"{key}.jsonl"
    meta_line: dict[str, Any] = {
        "_type": "metadata",
        "created_at": "2026-08-04T16:34:22.412779",
        "last_consolidated": 0,
    }
    if meta:
        meta_line.update(meta)
    lines = [json.dumps(meta_line)] + [json.dumps(m) for m in messages]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _message_lines(path: Path) -> list[dict]:
    """Parse a transcript's message lines (metadata line excluded)."""
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        entry = json.loads(ln)
        if entry.get("_type") == "metadata":
            continue
        out.append(entry)
    return out


def _inbound_slack_turn(ts: str = "2026-08-04T16:34:22.500000") -> dict:
    """A line as ``ConversationLog.append`` writes it for an inbound Slack DM."""
    return {
        "role": "user",
        "content": "This is a test session from Slack.",
        "ts": ts,
        "source_thread": SLACK_THREAD,
        "source_user": SLACK_USER,
    }


class TestCarryProvenance:
    def test_copies_both_fields(self) -> None:
        dest: dict = {}
        carry_provenance(dest, _inbound_slack_turn())
        assert dest == {"source_thread": SLACK_THREAD, "source_user": SLACK_USER}

    def test_absent_stays_absent(self) -> None:
        """No key at all, rather than an empty string.

        ``ConversationLog.append`` writes each field only when truthy and
        ``get_source_threads`` filters on the same truthiness, so an empty
        string would be a third state that reads as present-but-unusable.
        """
        dest: dict = {}
        carry_provenance(dest, {"role": "user", "content": "typed here"})
        assert dest == {}

    @pytest.mark.parametrize("bad", ["", None, 0, [], {"a": 1}, 17])
    def test_empty_and_non_string_values_are_absent(self, bad: Any) -> None:
        dest: dict = {}
        carry_provenance(dest, {"source_thread": bad, "source_user": bad})
        assert dest == {}

    def test_does_not_overwrite_with_an_absent_value(self) -> None:
        dest = {"source_thread": "dashboard", "source_user": "dashboard"}
        carry_provenance(dest, {"source_thread": SLACK_THREAD})
        assert dest["source_thread"] == SLACK_THREAD
        assert dest["source_user"] == "dashboard"


class TestBuildMessageEntry:
    def test_preserves_a_real_origin(self) -> None:
        entry = _build_message_entry(_inbound_slack_turn())
        assert entry is not None
        assert entry["source_thread"] == SLACK_THREAD
        assert entry["source_user"] == SLACK_USER

    def test_dashboard_authored_turn_keeps_the_dashboard_default(self) -> None:
        """"dashboard" is not invented provenance -- it is the right answer for a
        message with no recorded origin, which is a dashboard-authored turn."""
        entry = _build_message_entry({"role": "user", "content": "typed here", "ts": "t"})
        assert entry is not None
        assert entry["source_thread"] == "dashboard"
        assert entry["source_user"] == "dashboard"

    def test_never_writes_an_empty_origin(self) -> None:
        entry = _build_message_entry(
            {"role": "user", "content": "x", "ts": "t", "source_thread": "", "source_user": ""}
        )
        assert entry is not None
        assert entry["source_thread"] == "dashboard"
        assert entry["source_user"] == "dashboard"


class TestRehydrateThenFlushRoundTrip:
    """The regression test for the bug: load a transcript, then save it back.

    Asserting on a fresh in-memory message instead would pass even with the
    load paths left unfixed.
    """

    def _rehydrated(self, tmp_path: Path, monkeypatch: Any, messages: list[dict]) -> Any:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(tmp_path, "dashboard_chat1", messages, meta={"title": "T"})
        state = _make_state(tmp_path)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        return state, slot

    def test_slack_origin_survives_the_flush(self, tmp_path: Path, monkeypatch: Any) -> None:
        state, slot = self._rehydrated(tmp_path, monkeypatch, [_inbound_slack_turn()])

        # The load path must put provenance where the save path can find it.
        assert slot.messages[0]["source_thread"] == SLACK_THREAD
        assert slot.messages[0]["source_user"] == SLACK_USER

        slot.append("user", "and now from the dashboard")
        slot.drain()
        _save_slot_to_history(state, slot)

        lines = _message_lines(tmp_path / "dashboard_chat1.jsonl")
        assert len(lines) == 2
        assert lines[0]["source_thread"] == SLACK_THREAD
        assert lines[0]["source_user"] == SLACK_USER
        # The dashboard-authored turn appended alongside it is genuinely ours.
        assert lines[1]["source_thread"] == "dashboard"
        assert lines[1]["source_user"] == "dashboard"

    def test_every_role_in_the_window_keeps_its_origin(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """A reply the agent produced FOR a Slack thread belongs to that thread
        too -- the restamp flattened assistant and tool lines as well."""
        messages = [
            _inbound_slack_turn("2026-08-04T16:34:22.500000"),
            {
                "role": "assistant",
                "content": "Test session received.",
                "ts": "2026-08-04T16:34:25.000000",
                "source_thread": SLACK_THREAD,
                "source_user": SLACK_USER,
            },
            {
                "role": "tool",
                "content": "Running: ls",
                "ts": "2026-08-04T16:34:26.000000",
                "source_thread": SLACK_THREAD,
                "source_user": SLACK_USER,
            },
        ]
        state, slot = self._rehydrated(tmp_path, monkeypatch, messages)
        slot._dirty = True
        _save_slot_to_history(state, slot)

        lines = _message_lines(tmp_path / "dashboard_chat1.jsonl")
        assert [m["role"] for m in lines] == ["user", "assistant", "tool"]
        assert {m["source_thread"] for m in lines} == {SLACK_THREAD}
        assert {m["source_user"] for m in lines} == {SLACK_USER}

    def test_repeated_flushes_do_not_erode_provenance(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """The dashboard flushes every few seconds; one surviving round trip is
        not enough if the save's own output cannot be reloaded."""
        state, slot = self._rehydrated(tmp_path, monkeypatch, [_inbound_slack_turn()])
        path = tmp_path / "dashboard_chat1.jsonl"
        for _ in range(3):
            slot._dirty = True
            _save_slot_to_history(state, slot)
        assert _message_lines(path)[0]["source_thread"] == SLACK_THREAD

        # Reload from the file the saves produced and flush once more.
        state2 = _make_state(tmp_path)
        slot2 = _rehydrate_slot_from_history(state2, "chat1")
        assert slot2 is not None
        slot2._dirty = True
        _save_slot_to_history(state2, slot2)
        assert _message_lines(path)[0]["source_thread"] == SLACK_THREAD

    def test_frozen_prefix_and_window_agree(self, tmp_path: Path, monkeypatch: Any) -> None:
        """The prefix keeps provenance because it is copied verbatim; the window
        must now match it instead of contradicting it in the same file."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        messages = [
            {**_inbound_slack_turn(f"2026-08-04T16:{i:02d}:00.000000"), "content": f"m{i}"}
            for i in range(6)
        ]
        _write_session(tmp_path, "dashboard_chat1", messages)
        state = _make_state(tmp_path)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        # Pretend the first three lines are older than the loaded window.
        slot._disk_older_count = 3
        slot._disk_window_len = 3
        del slot.messages[:3]
        slot._frozen_prefix_cache = None
        slot._dirty = True
        _save_slot_to_history(state, slot)

        lines = _message_lines(tmp_path / "dashboard_chat1.jsonl")
        assert [m["content"] for m in lines] == [f"m{i}" for i in range(6)]
        assert {m["source_thread"] for m in lines} == {SLACK_THREAD}


class TestBulkRestoreRoundTrip:
    def test_startup_restore_carries_provenance(self, tmp_path: Path, monkeypatch: Any) -> None:
        """``restore_recent_sessions`` is a second, independent load path."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        path = _write_session(tmp_path, "dashboard_chat1", [_inbound_slack_turn()])
        path.touch()
        state = _make_state(tmp_path)
        assert restore_recent_sessions(state, window_minutes=60) == 1
        slot = state._slots["chat1"]
        assert slot.messages[0]["source_thread"] == SLACK_THREAD

        slot._dirty = True
        _save_slot_to_history(state, slot)
        assert _message_lines(path)[0]["source_thread"] == SLACK_THREAD


class TestChannelWindowRoundTrip:
    """A channel-bound tab loads the CHANNEL's transcript, so nearly every line
    it reads back arrived from the channel."""

    def test_rebuild_window_carries_provenance(self, tmp_path: Path, monkeypatch: Any) -> None:
        from kiro_crew.dashboard.channel_slots import _rebuild_window

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slack_1.1", linked_session_key="slack:1.1")
        _rebuild_window(slot, [_inbound_slack_turn()])

        assert slot.messages[0]["source_thread"] == SLACK_THREAD
        assert slot.messages[0]["source_user"] == SLACK_USER

        slot._dirty = True
        _save_slot_to_history(state, slot)
        lines = _message_lines(tmp_path / "slack_1.1.jsonl")
        assert lines[0]["source_thread"] == SLACK_THREAD
        assert lines[0]["source_user"] == SLACK_USER


class TestForeignAppendDedup:
    """Provenance must not perturb the foreign-append scan.

    Identity there is ``(ts, role, content)`` with a ``(role, content)``
    tiebreak -- never the whole line -- so a window entry that now carries
    provenance still matches its own on-disk copy rather than being treated as
    a cross-process append and duplicated.
    """

    def test_window_entry_is_not_duplicated_as_a_foreign_append(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        _write_session(tmp_path, "dashboard_chat1", [_inbound_slack_turn()])
        state = _make_state(tmp_path)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        # Force the slow path so the scan actually runs instead of the
        # mtime/size fast path serving cached bytes.
        slot._frozen_prefix_cache = None
        slot._dirty = True
        _save_slot_to_history(state, slot)

        lines = _message_lines(tmp_path / "dashboard_chat1.jsonl")
        assert len(lines) == 1, "the window's own line came back as a foreign append"
        assert lines[0]["source_thread"] == SLACK_THREAD

    def test_a_genuine_foreign_append_keeps_its_own_origin(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Foreign lines are merged as raw bytes, so they were always correct --
        lock that in so the merge is not 'fixed' into a rebuild later."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        path = _write_session(tmp_path, "dashboard_chat1", [_inbound_slack_turn()])
        state = _make_state(tmp_path)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None

        # Another process appends while this slot holds its pre-lock snapshot.
        with path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "from a subagent",
                        "ts": "2026-08-04T16:40:00.000000",
                        "source_thread": "discord:99",
                        "source_user": "U9",
                    }
                )
                + "\n"
            )
        slot._frozen_prefix_cache = None
        slot._dirty = True
        _save_slot_to_history(state, slot)

        lines = _message_lines(path)
        assert [m["source_thread"] for m in lines] == [SLACK_THREAD, "discord:99"]
