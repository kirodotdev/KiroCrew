"""Unit tests for native subagent card sync in chat_runner.

Native (use_subagent) crews surface in the Activity tab via kiro-cli's
``_kiro.dev/subagent/list_update`` notification. ``_native_subagent_sync``
reconciles one Activity card per sub-agent (spawn / done) from that list.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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


class TestEmptyTaskSkip:
    """Cards with empty initialQuery/sessionName are skipped entirely."""

    def test_empty_task_card_not_broadcast(self):
        state = _make_state()
        state._native_cards = {}
        tracker: dict = {}
        _native_subagent_sync(state, _make_slot(), [_sub("s1", "", "worker", "", "working")], tracker)
        assert "subagent_spawn" not in _ws_calls(state)
        # Marked done so subsequent updates never re-process it
        assert tracker["s1"]["done"] is True

    def test_empty_task_card_not_registered_for_cancel(self):
        state = _make_state()
        state._native_cards = {}
        _native_subagent_sync(state, _make_slot(), [_sub("s1", "", "worker", "", "working")], {})
        assert state._native_cards == {}

    def test_whitespace_task_also_skipped(self):
        state = _make_state()
        state._native_cards = {}
        tracker: dict = {}
        _native_subagent_sync(
            state, _make_slot(), [_sub("s1", "  ", "worker", " \n\t", "working")], tracker
        )
        assert "subagent_spawn" not in _ws_calls(state)
        assert tracker["s1"]["done"] is True


class TestStalenessTimeout:
    """Cards that disappeared from the list auto-close after the grace window."""

    def _stale_tracker(self, now_offset=200.0):
        import time as _time

        past = _time.time() - now_offset
        return {
            "gone": {
                "started": past, "done": False, "agent": "w", "task": "t",
                "last_activity": past,
            }
        }

    def test_disappeared_stale_card_auto_closed(self):
        state = _make_state()
        state._native_cards = {"native:gone": {"slot": "slot-1", "session_id": "gone", "started": 0}}
        tracker = self._stale_tracker()
        # Empty subagents list — 'gone' not reported anymore
        _native_subagent_sync(state, _make_slot(), [], tracker)
        dones = _ws_calls(state)["subagent_done"]
        assert dones[0]["id"] == "native:gone"
        assert dones[0]["error"] == "timed out (no activity)"
        assert tracker["gone"]["done"] is True
        # Unregistered from the cancel registry
        assert "native:gone" not in state._native_cards

    def test_still_reported_card_never_timed_out(self):
        state = _make_state()
        state._native_cards = {}
        tracker = self._stale_tracker()
        tracker["gone"]["task"] = "t"
        # Same sid IS in the current update — must not be closed
        subs = [_sub("gone", "n", "w", "task text", "working")]
        _native_subagent_sync(state, _make_slot(), subs, tracker)
        assert "subagent_done" not in _ws_calls(state)
        assert tracker["gone"]["done"] is False

    def test_seen_card_refreshes_last_activity_even_with_empty_message(self):
        import time as _time

        state = _make_state()
        state._native_cards = {}
        past = _time.time() - 500.0
        tracker = {
            "s1": {"started": past, "done": False, "agent": "w", "task": "t", "last_activity": past}
        }
        subs = [_sub("s1", "n", "w", "task text", "working", message="")]
        _native_subagent_sync(state, _make_slot(), subs, tracker)
        assert tracker["s1"]["last_activity"] > past

    def test_disappeared_but_recent_card_kept(self):
        import time as _time

        state = _make_state()
        state._native_cards = {}
        recent = _time.time() - 5.0
        tracker = {
            "s1": {"started": recent, "done": False, "agent": "w", "task": "t", "last_activity": recent}
        }
        _native_subagent_sync(state, _make_slot(), [], tracker)
        assert "subagent_done" not in _ws_calls(state)
        assert tracker["s1"]["done"] is False


class TestNativeCardRegistry:
    """_register_native_card / _unregister_native_card manage state._native_cards."""

    def test_register_creates_dict_and_entry(self):
        from kiro_crew.dashboard.chat_runner import _register_native_card

        class _Bare:
            pass

        state = _Bare()
        _register_native_card(state, "native:s1", "slot-1", "s1")
        assert state._native_cards["native:s1"]["slot"] == "slot-1"
        assert state._native_cards["native:s1"]["session_id"] == "s1"

    def test_unregister_removes_entry(self):
        from kiro_crew.dashboard.chat_runner import (
            _register_native_card,
            _unregister_native_card,
        )

        class _Bare:
            pass

        state = _Bare()
        _register_native_card(state, "native:s1", "slot-1", "s1")
        _unregister_native_card(state, "native:s1")
        assert "native:s1" not in state._native_cards

    def test_unregister_noop_without_registry(self):
        from kiro_crew.dashboard.chat_runner import _unregister_native_card

        class _Bare:
            pass

        _unregister_native_card(_Bare(), "native:s1")  # must not raise

    def test_spawn_registers_card_for_cancel(self):
        state = _make_state()
        state._native_cards = {}
        _native_subagent_sync(
            state, _make_slot(), [_sub("s1", "n", "w", "task text", "working")], {}
        )
        assert "native:s1" in state._native_cards

    def test_terminal_status_unregisters_card(self):
        state = _make_state()
        state._native_cards = {}
        tracker: dict = {}
        _native_subagent_sync(
            state, _make_slot(), [_sub("s1", "n", "w", "task text", "working")], tracker
        )
        _native_subagent_sync(
            state, _make_slot(), [_sub("s1", "n", "w", "task text", "terminated")], tracker
        )
        assert "native:s1" not in state._native_cards

    def test_close_all_unregisters_card(self):
        state = _make_state()
        state._native_cards = {"native:s1": {"slot": "slot-1", "session_id": "s1", "started": 0}}
        tracker = {"s1": {"started": 0.0, "done": False, "agent": "w", "task": "t"}}
        _native_subagent_close_all(state, _make_slot(), tracker)
        assert "native:s1" not in state._native_cards


class TestNativeCardFeedRedaction:
    """_native_card_feed joins, truncates, and redacts at the broadcast boundary."""

    def test_feed_joined_and_truncated(self):
        from kiro_crew.dashboard.chat_runner import _native_card_feed

        out = _native_card_feed({"c1": ["a" * 5000, "b" * 5000]}, "c1")
        assert len(out) <= 8000
        assert out.startswith("a")

    def test_feed_empty_when_no_output(self):
        from kiro_crew.dashboard.chat_runner import _native_card_feed

        assert _native_card_feed(None, "c1") == ""
        assert _native_card_feed({}, "c1") == ""

    def test_feed_applies_both_redactions(self):
        from unittest.mock import patch

        from kiro_crew.dashboard.chat_runner import _native_card_feed

        with patch(
            "kiro_crew.dashboard.chat_runner.redact_exfiltration_urls",
            return_value=("URLS_REDACTED", 0),
        ) as m_urls, patch(
            "kiro_crew.dashboard.chat_runner.redact_credentials",
            return_value=("FULLY_REDACTED", 0),
        ) as m_creds:
            out = _native_card_feed({"c1": ["secret output"]}, "c1")
        m_urls.assert_called_once_with("secret output")
        m_creds.assert_called_once_with("URLS_REDACTED")
        assert out == "FULLY_REDACTED"


class TestNativeCancelHandler:
    """DELETE /api/spawn/{id} handles native:* card IDs."""

    def _request(self, state, agent_id):
        from unittest.mock import MagicMock

        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"agent_id": agent_id}
        return request

    @pytest.mark.asyncio
    async def test_cancel_native_card_broadcasts_done_and_audits(self):
        import json
        from unittest.mock import MagicMock, patch

        from kiro_crew.dashboard.handlers.messaging import api_spawn_delete

        state = _make_state()
        state._native_cards = {"native:s1": {"slot": "slot-1", "session_id": "s1", "started": 0}}
        with patch("kiro_crew.dashboard.handlers.messaging._sel") as m_sel:
            m_sel.return_value.log_tool_invocation = MagicMock()
            resp = await api_spawn_delete(self._request(state, "native:s1"))
        body = json.loads(resp.text)
        assert body == {"ok": True, "cancelled": True}
        assert "native:s1" not in state._native_cards
        dones = _ws_calls(state)["subagent_done"]
        assert dones[0]["id"] == "native:s1"
        assert dones[0]["error"] == "Cancelled by user"
        audit_kwargs = m_sel.return_value.log_tool_invocation.call_args[1]
        assert audit_kwargs["tool_name"] == "cancel_native_subagent"
        assert audit_kwargs["outcome"] == "cancelled_by_user"

    @pytest.mark.asyncio
    async def test_cancel_unknown_native_card_404(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn_delete

        state = _make_state()
        state._native_cards = {}
        resp = await api_spawn_delete(self._request(state, "native:unknown"))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_cancel_native_card_survives_sel_failure(self):
        import json
        from unittest.mock import patch

        from kiro_crew.dashboard.handlers.messaging import api_spawn_delete

        state = _make_state()
        state._native_cards = {"native:s1": {"slot": "slot-1", "session_id": "s1", "started": 0}}
        with patch(
            "kiro_crew.dashboard.handlers.messaging._sel",
            side_effect=RuntimeError("sel down"),
        ):
            resp = await api_spawn_delete(self._request(state, "native:s1"))
        # SEL failure must never break the cancel response
        assert json.loads(resp.text)["ok"] is True
