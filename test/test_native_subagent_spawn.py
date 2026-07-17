"""Unit tests for native subagent card sync in chat_runner.

Native (use_subagent) crews surface in the Activity tab via kiro-cli's
``_kiro.dev/subagent/list_update`` notification. ``_native_subagent_sync``
reconciles one Activity card per sub-agent (spawn / done) from that list.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kiro_crew.dashboard.chat_runner import (
    _native_subagent_close_all,
    _native_subagent_sync,
)


def _make_state():
    state = MagicMock()
    state.broadcast_ws = MagicMock()
    return state


def _make_slot():
    slot = MagicMock()
    slot.key = "slot-1"
    return slot


def _ws_calls(state):
    """Return {event_type: [payloads...]} from broadcast_ws calls."""
    out: dict[str, list[dict]] = {}
    for c in state.broadcast_ws.call_args_list:
        out.setdefault(c.args[0], []).append(c.args[1])
    return out


def _sub(session_id, name, role, query, status_type, message=""):
    return {
        "sessionId": session_id,
        "sessionName": name,
        "agentName": role,
        "role": role,
        "initialQuery": query,
        "status": {"type": status_type, "message": message},
    }


class TestNativeSubagentSync:
    def test_ignores_non_list(self):
        state = _make_state()
        _native_subagent_sync(state, _make_slot(), None, {})
        state.broadcast_ws.assert_not_called()

    def test_spawns_one_card_per_subagent(self):
        state = _make_state()
        slot = _make_slot()
        tracker: dict = {}
        subs = [
            _sub("s1", "readme", "gpu-multiagent-worker", "summarize README", "working"),
            _sub("s2", "git-log", "gpu-multiagent-worker", "git log -3", "working"),
            _sub("s3", "modules", "gpu-multiagent-worker", "list py files", "working"),
        ]
        _native_subagent_sync(state, slot, subs, tracker)
        spawns = _ws_calls(state).get("subagent_spawn", [])
        assert len(spawns) == 3
        ids = {s["id"] for s in spawns}
        assert ids == {"native:s1", "native:s2", "native:s3"}
        # Task + agent come from the per-subagent fields.
        by_id = {s["id"]: s for s in spawns}
        assert by_id["native:s1"]["task"] == "summarize README"
        assert by_id["native:s1"]["agent"] == "gpu-multiagent-worker"
        assert len(tracker) == 3

    def test_spawn_is_idempotent_across_updates(self):
        state = _make_state()
        slot = _make_slot()
        tracker: dict = {}
        subs = [_sub("s1", "readme", "worker", "q", "working")]
        _native_subagent_sync(state, slot, subs, tracker)
        _native_subagent_sync(state, slot, subs, tracker)  # same list again
        assert len(_ws_calls(state).get("subagent_spawn", [])) == 1  # not re-spawned

    def test_terminated_status_completes_card(self):
        state = _make_state()
        slot = _make_slot()
        tracker: dict = {}
        _native_subagent_sync(state, slot, [_sub("s1", "n", "w", "q", "working")], tracker)
        _native_subagent_sync(state, slot, [_sub("s1", "n", "w", "q", "terminated")], tracker)
        dones = _ws_calls(state).get("subagent_done", [])
        assert len(dones) == 1
        assert dones[0]["id"] == "native:s1"
        assert dones[0]["error"] is None
        assert tracker["s1"]["done"] is True

    def test_done_result_defaults_when_no_output(self):
        # With no accumulated card output, the done result falls back to the
        # "(output in chat)" sentinel so the card isn't blank.
        state = _make_state()
        slot = _make_slot()
        tracker: dict = {}
        _native_subagent_sync(state, slot, [_sub("s1", "n", "w", "q", "working")], tracker)
        _native_subagent_sync(state, slot, [_sub("s1", "n", "w", "q", "terminated")], tracker)
        assert _ws_calls(state)["subagent_done"][0]["result"] == "(output in chat)"

    def test_done_result_uses_accumulated_feed(self):
        # The published frontend replaces a card's live `streaming` with
        # `result` on done, so the accumulated tool feed must be sent as the
        # done result to persist it.
        state = _make_state()
        slot = _make_slot()
        tracker: dict = {}
        card_output = {"native:s1": ["\u2192 git log -1\n", "commit abc123\n"]}
        _native_subagent_sync(
            state, slot, [_sub("s1", "n", "w", "q", "working")], tracker, card_output
        )
        _native_subagent_sync(
            state, slot, [_sub("s1", "n", "w", "q", "terminated")], tracker, card_output
        )
        result = _ws_calls(state)["subagent_done"][0]["result"]
        assert "git log -1" in result
        assert "commit abc123" in result

    def test_done_result_truncated_to_8000(self):
        state = _make_state()
        slot = _make_slot()
        tracker: dict = {}
        card_output = {"native:s1": ["x" * 10000]}
        _native_subagent_sync(
            state, slot, [_sub("s1", "n", "w", "q", "working")], tracker, card_output
        )
        _native_subagent_sync(
            state, slot, [_sub("s1", "n", "w", "q", "terminated")], tracker, card_output
        )
        assert len(_ws_calls(state)["subagent_done"][0]["result"]) <= 8000

    def test_done_fires_once(self):
        state = _make_state()
        slot = _make_slot()
        tracker: dict = {}
        _native_subagent_sync(state, slot, [_sub("s1", "n", "w", "q", "working")], tracker)
        _native_subagent_sync(state, slot, [_sub("s1", "n", "w", "q", "terminated")], tracker)
        _native_subagent_sync(state, slot, [_sub("s1", "n", "w", "q", "terminated")], tracker)
        assert len(_ws_calls(state).get("subagent_done", [])) == 1

    def test_failed_status_sets_error(self):
        state = _make_state()
        slot = _make_slot()
        tracker: dict = {}
        _native_subagent_sync(state, slot, [_sub("s1", "n", "w", "q", "working")], tracker)
        _native_subagent_sync(
            state, slot, [_sub("s1", "n", "w", "q", "failed", "boom")], tracker
        )
        done = _ws_calls(state)["subagent_done"][0]
        assert done["error"] == "boom"

    def test_skips_entry_without_session_id(self):
        state = _make_state()
        _native_subagent_sync(state, _make_slot(), [{"sessionName": "x"}], {})
        state.broadcast_ws.assert_not_called()

    def test_skips_non_dict_entries_in_list(self):
        # A list containing non-dict entries must skip them without error and
        # still process the valid ones.
        state = _make_state()
        slot = _make_slot()
        tracker: dict = {}
        subs = ["not a dict", None, 42, _sub("s1", "n", "w", "q", "working")]
        _native_subagent_sync(state, slot, subs, tracker)
        spawns = _ws_calls(state).get("subagent_spawn", [])
        assert len(spawns) == 1
        assert spawns[0]["id"] == "native:s1"

    def test_redacts_task_and_agent(self):
        state = _make_state()
        slot = _make_slot()
        _native_subagent_sync(
            state, slot,
            [_sub("s1", "n", "role", "key AKIAIOSFODNN7EXAMPLE here", "working")],
            {},
        )
        spawn = _ws_calls(state)["subagent_spawn"][0]
        assert "AKIAIOSFODNN7EXAMPLE" not in spawn["task"]

    def test_nongeneric_status_message_emits_tool(self):
        state = _make_state()
        slot = _make_slot()
        tracker: dict = {}
        _native_subagent_sync(state, slot, [_sub("s1", "n", "w", "q", "working")], tracker)
        _native_subagent_sync(
            state, slot, [_sub("s1", "n", "w", "q", "working", "reading files")], tracker
        )
        tools = _ws_calls(state).get("subagent_tool", [])
        assert any(t["tool"] == "reading files" for t in tools)


class TestNativeSubagentCloseAll:
    def test_closes_open_cards(self):
        state = _make_state()
        slot = _make_slot()
        tracker = {
            "s1": {"started": 0.0, "done": False, "agent": "w", "task": "t1"},
            "s2": {"started": 0.0, "done": True, "agent": "w", "task": "t2"},
        }
        _native_subagent_close_all(state, slot, tracker)
        dones = _ws_calls(state).get("subagent_done", [])
        # Only the still-open card (s1) gets a done.
        assert len(dones) == 1
        assert dones[0]["id"] == "native:s1"
        assert tracker["s1"]["done"] is True

    def test_close_all_uses_accumulated_feed(self):
        state = _make_state()
        slot = _make_slot()
        tracker = {"s1": {"started": 0.0, "done": False, "agent": "w", "task": "t1"}}
        card_output = {"native:s1": ["\u2192 read foo.py\n", "print('hi')\n"]}
        _native_subagent_close_all(state, slot, tracker, card_output)
        result = _ws_calls(state)["subagent_done"][0]["result"]
        assert "read foo.py" in result
        assert "print('hi')" in result

    def test_close_all_defaults_when_no_output(self):
        state = _make_state()
        slot = _make_slot()
        tracker = {"s1": {"started": 0.0, "done": False, "agent": "w", "task": "t1"}}
        _native_subagent_close_all(state, slot, tracker)
        assert _ws_calls(state)["subagent_done"][0]["result"] == "(output in chat)"
