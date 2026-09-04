"""Unit tests for native subagent card sync in chat_runner.

Native (use_subagent) crews surface in the Activity tab via kiro-cli's
``_kiro.dev/subagent/list_update`` notification. ``_native_subagent_sync``
reconciles one Activity card per sub-agent (spawn / done) from that list.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.chat_runner import (
    _append_native_output,
    _native_crew_should_auto_approve,
    _native_done_result,
    _native_subagent_close_all,
    _native_subagent_sync,
    _retain_terminal_native,
)
from kiro_crew.dashboard.state import (
    NATIVE_SUBAGENT_DONE_TRUNC_MARKER,
    NATIVE_SUBAGENT_OUTPUT_HARD,
    NATIVE_SUBAGENT_TERMINAL_KEEP,
    DashboardState,
    native_subagent_output_tail,
)


def _make_state():
    state = MagicMock()
    state.broadcast_ws = MagicMock()
    return state


def _make_slot():
    slot = MagicMock()
    slot.key = "slot-1"
    slot._native_subagent_tracker = {}
    slot._native_subagent_output = {}
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

    def test_active_card_is_available_to_reconnect_snapshot(self):
        state = _make_state()
        slot = _make_slot()
        state._slots = {slot.key: slot}
        tracker = slot._native_subagent_tracker
        _native_subagent_sync(
            state,
            slot,
            [_sub("s1", "readme", "worker", "summarize README", "working", "reading")],
            tracker,
            slot._native_subagent_output,
        )
        slot._native_subagent_output["native:s1"] = ["→ read README.md\n"]

        assert DashboardState.native_subagent_snapshots(state) == [
            {
                "id": "native:s1",
                "slot": "slot-1",
                "task": "summarize README",
                "agent": "worker",
                "done": False,
                "streaming": "→ read README.md\n",
                "last_tool": "reading",
                "started": tracker["s1"]["started"],
            }
        ]

        # After termination the card must still hydrate on reconnect — now as a
        # terminal (done) record, not dropped — because a socket that dropped
        # around completion would otherwise never see it again this turn.
        _native_subagent_sync(
            state,
            slot,
            [_sub("s1", "readme", "worker", "summarize README", "terminated")],
            tracker,
            slot._native_subagent_output,
        )
        snaps = DashboardState.native_subagent_snapshots(state)
        assert len(snaps) == 1
        done = snaps[0]
        assert done["id"] == "native:s1"
        assert done["done"] is True
        assert done["error"] is None
        assert "read README.md" in str(done["result"])
        assert done["elapsed"] == tracker["s1"]["elapsed"]

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

    def test_done_result_keeps_newest_tail_with_marker(self):
        # Over the cap, keep the NEWEST 8000 chars and prefix an explicit
        # truncation marker so the dropped head is signalled (finding 3).
        state = _make_state()
        slot = _make_slot()
        tracker: dict = {}
        card_output = {"native:s1": ["A" * 2000 + "B" * 8000]}
        _native_subagent_sync(
            state, slot, [_sub("s1", "n", "w", "q", "working")], tracker, card_output
        )
        _native_subagent_sync(
            state, slot, [_sub("s1", "n", "w", "q", "terminated")], tracker, card_output
        )
        result = _ws_calls(state)["subagent_done"][0]["result"]
        assert result.startswith(NATIVE_SUBAGENT_DONE_TRUNC_MARKER)
        body = result[len(NATIVE_SUBAGENT_DONE_TRUNC_MARKER) :]
        assert len(body) == 8000
        assert set(body) == {"B"}  # newest content only; older 'A' head dropped

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
        _native_subagent_sync(state, slot, [_sub("s1", "n", "w", "q", "failed", "boom")], tracker)
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
            state,
            slot,
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
        _native_subagent_sync(
            state, _make_slot(), [_sub("s1", "", "worker", "", "working")], tracker
        )
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
                "started": past,
                "done": False,
                "agent": "w",
                "task": "t",
                "last_activity": past,
            }
        }

    def test_disappeared_stale_card_auto_closed(self):
        state = _make_state()
        state._native_cards = {
            "native:gone": {"slot": "slot-1", "session_id": "gone", "started": 0}
        }
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
            "s1": {
                "started": recent,
                "done": False,
                "agent": "w",
                "task": "t",
                "last_activity": recent,
            }
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
        assert len(out) <= 8000 + len(NATIVE_SUBAGENT_DONE_TRUNC_MARKER)
        assert out.startswith(NATIVE_SUBAGENT_DONE_TRUNC_MARKER)
        assert out.endswith("b" * 5000)

    def test_feed_empty_when_no_output(self):
        from kiro_crew.dashboard.chat_runner import _native_card_feed

        assert _native_card_feed(None, "c1") == ""
        assert _native_card_feed({}, "c1") == ""

    def test_feed_applies_both_redactions(self):
        from unittest.mock import patch

        from kiro_crew.dashboard.chat_runner import _native_card_feed

        with (
            patch(
                "kiro_crew.dashboard.chat_runner.redact_exfiltration_urls",
                return_value=("URLS_REDACTED", 0),
            ) as m_urls,
            patch(
                "kiro_crew.dashboard.chat_runner.redact_credentials",
                return_value=("FULLY_REDACTED", 0),
            ) as m_creds,
        ):
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
        assert dones[0]["error"] is None
        assert dones[0]["stopped"] is True
        audit_kwargs = m_sel.return_value.log_tool_invocation.call_args[1]
        assert audit_kwargs["tool_name"] == "cancel_native_subagent"
        assert audit_kwargs["outcome"] == "cancelled_by_user"

    @pytest.mark.asyncio
    async def test_cancel_native_card_marks_tracker_stopped_for_replay(self):
        """Cancelling a native card persists the stop on the slot tracker so a
        reconnecting client's WS replay reconstructs it as STOPPED — not as
        still-running or successfully completed."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.dashboard.handlers.messaging import api_spawn_delete
        from kiro_crew.dashboard.state import DashboardState

        state = _make_state()
        slot = _make_slot()
        slot._native_subagent_tracker["s1"] = {
            "id": "native:s1",
            "task": "summarize",
            "agent": "worker",
            "done": False,
            "started": 100.0,
        }
        state.get_slot = MagicMock(return_value=slot)
        state._slots = {"slot-1": slot}
        state._native_cards = {
            "native:s1": {"slot": "slot-1", "session_id": "s1", "started": 100.0}
        }
        with patch("kiro_crew.dashboard.handlers.messaging._sel") as m_sel:
            m_sel.return_value.log_tool_invocation = MagicMock()
            await api_spawn_delete(self._request(state, "native:s1"))

        rec = slot._native_subagent_tracker["s1"]
        assert rec["done"] is True
        assert rec["stopped"] is True
        assert rec["error"] is None
        assert rec["result"] == "(cancelled)"

        snaps = DashboardState.native_subagent_snapshots(state)
        assert len(snaps) == 1
        assert snaps[0]["done"] is True
        assert snaps[0]["stopped"] is True
        assert snaps[0]["error"] is None

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


class TestTailJoin:
    def test_returns_full_output_under_limit(self):
        assert native_subagent_output_tail(["ab", "cd", "ef"], limit=100) == "abcdef"

    def test_returns_only_trailing_chars_over_limit(self):
        # Matches the old `"".join(chunks)[-limit:]` semantics exactly, without
        # materializing the full accumulated output.
        chunks = ["a" * 30_000, "b" * 30_000, "c" * 30_000]
        joined_tail = "".join(chunks)[-40_000:]
        assert native_subagent_output_tail(chunks, limit=40_000) == joined_tail
        assert len(native_subagent_output_tail(chunks, limit=40_000)) == 40_000

    def test_stops_walking_once_limit_covered(self):
        # Once the accumulated tail covers the limit, earlier chunks are never
        # walked. The final chunk alone fills the 100-char window, so neither the
        # "EARLIEST" marker nor the middle chunk appear in the result.
        chunks = ["EARLIEST", "b" * 100, "c" * 100]
        result = native_subagent_output_tail(chunks, limit=100)
        assert result == "c" * 100
        assert "EARLIEST" not in result
        assert "b" not in result

    def test_empty_and_nonpositive_limit(self):
        assert native_subagent_output_tail([]) == ""
        assert native_subagent_output_tail(["abc"], limit=0) == ""

    def test_snapshot_streaming_is_bounded_to_tail(self):
        state = _make_state()
        slot = _make_slot()
        state._slots = {slot.key: slot}
        tracker = slot._native_subagent_tracker
        _native_subagent_sync(
            state,
            slot,
            [_sub("s1", "readme", "worker", "q", "working", "reading")],
            tracker,
            slot._native_subagent_output,
        )
        slot._native_subagent_output["native:s1"] = ["x" * 50_000, "TAIL"]
        streaming = DashboardState.native_subagent_snapshots(state)[0]["streaming"]
        assert len(streaming) == 40_000
        assert streaming.endswith("TAIL")


class TestNativeTerminalHydration:
    """Reconnect must restore native cards that finished while the socket was
    down (parent turn still running), instead of silently dropping them."""

    def test_disconnected_completion_is_replayed_as_done(self):
        # A native subagent that spawned and then completed mid-turn must appear
        # in the reconnect snapshot as a terminal record carrying its frozen
        # elapsed/error/result, so the client can rebuild the done card.
        state = _make_state()
        slot = _make_slot()
        state._slots = {slot.key: slot}
        tracker = slot._native_subagent_tracker
        co = slot._native_subagent_output
        _native_subagent_sync(state, slot, [_sub("s1", "n", "w", "q", "working")], tracker, co)
        co["native:s1"] = ["\u2192 git log\n", "commit abc123\n"]
        _native_subagent_sync(state, slot, [_sub("s1", "n", "w", "q", "terminated")], tracker, co)
        snaps = DashboardState.native_subagent_snapshots(state)
        assert len(snaps) == 1
        rec = snaps[0]
        assert rec["done"] is True
        assert rec["id"] == "native:s1"
        assert rec["error"] is None
        assert "commit abc123" in str(rec["result"])
        assert float(rec["elapsed"]) >= 0.0

    def test_failed_completion_replays_error(self):
        state = _make_state()
        slot = _make_slot()
        state._slots = {slot.key: slot}
        tracker = slot._native_subagent_tracker
        _native_subagent_sync(state, slot, [_sub("s1", "n", "w", "q", "working")], tracker)
        _native_subagent_sync(state, slot, [_sub("s1", "n", "w", "q", "failed", "boom")], tracker)
        rec = DashboardState.native_subagent_snapshots(state)[0]
        assert rec["done"] is True
        assert rec["error"] == "boom"

    def test_running_and_done_coexist_for_reconnect(self):
        # Mixed state: one card still running, one already done. Both hydrate.
        state = _make_state()
        slot = _make_slot()
        state._slots = {slot.key: slot}
        tracker = slot._native_subagent_tracker
        _native_subagent_sync(
            state,
            slot,
            [_sub("s1", "n", "w", "q", "working"), _sub("s2", "n", "w", "q", "working")],
            tracker,
        )
        _native_subagent_sync(
            state,
            slot,
            [_sub("s1", "n", "w", "q", "working"), _sub("s2", "n", "w", "q", "terminated")],
            tracker,
        )
        snaps = {s["id"]: s for s in DashboardState.native_subagent_snapshots(state)}
        assert snaps["native:s1"]["done"] is False
        assert snaps["native:s2"]["done"] is True

    def test_slot_isolation(self):
        # A done card in one slot and a running card in another must each
        # replay under their own slot key with no cross-contamination.
        state = _make_state()
        slot_a = _make_slot()
        slot_a.key = "slot-a"
        slot_b = _make_slot()
        slot_b.key = "slot-b"
        state._slots = {"slot-a": slot_a, "slot-b": slot_b}
        _native_subagent_sync(
            state, slot_a, [_sub("a1", "n", "w", "q", "working")], slot_a._native_subagent_tracker
        )
        _native_subagent_sync(
            state,
            slot_a,
            [_sub("a1", "n", "w", "q", "terminated")],
            slot_a._native_subagent_tracker,
        )
        _native_subagent_sync(
            state, slot_b, [_sub("b1", "n", "w", "q", "working")], slot_b._native_subagent_tracker
        )
        by_id = {s["id"]: s for s in DashboardState.native_subagent_snapshots(state)}
        assert by_id["native:a1"]["slot"] == "slot-a"
        assert by_id["native:a1"]["done"] is True
        assert by_id["native:b1"]["slot"] == "slot-b"
        assert by_id["native:b1"]["done"] is False

    def test_terminal_cards_survive_turn_exit_then_clear_next_turn(self):
        # Turn teardown retains terminal (done) cards (chat_runner finally uses
        # _retain_terminal_native) so a reconnect after the parent turn exits
        # still sees them; the next turn's fresh tracker clears them.
        state = _make_state()
        slot = _make_slot()
        state._slots = {slot.key: slot}
        tracker = slot._native_subagent_tracker
        _native_subagent_sync(state, slot, [_sub("s1", "n", "w", "q", "working")], tracker)
        _native_subagent_sync(state, slot, [_sub("s1", "n", "w", "q", "terminated")], tracker)
        assert len(DashboardState.native_subagent_snapshots(state)) == 1
        # Socket down through parent completion: the finally block retains
        # terminal (done) cards (dropping running cards + output buffers) so a
        # reconnect in the idle gap after the parent turn exits still replays
        # them. A fresh tracker at the next turn start clears them — no
        # cross-turn leak (finding 2).
        slot._native_subagent_tracker = _retain_terminal_native(tracker)
        slot._native_subagent_output = {}
        after = DashboardState.native_subagent_snapshots(state)
        assert len(after) == 1
        assert after[0]["done"] is True
        # Next turn reassigns a fresh tracker → terminal cards gone (no leak).
        slot._native_subagent_tracker = {}
        assert DashboardState.native_subagent_snapshots(state) == []

    def test_stale_done_card_pruned_on_read(self):
        # A done card older than the read-side TTL is not replayed, so a very
        # late reconnect on an abandoned slot sees no stale completions.
        state = _make_state()
        slot = _make_slot()
        state._slots = {slot.key: slot}
        slot._native_subagent_tracker = {
            "s1": {
                "id": "native:s1",
                "started": 0.0,
                "done": True,
                "agent": "w",
                "task": "t",
                "elapsed": 1.0,
                "error": None,
                "result": "r",
                "done_at": time.time() - 10_000,
            }
        }
        assert DashboardState.native_subagent_snapshots(state) == []

    def test_terminal_retention_is_bounded(self):
        # A native crew that completes many sub-agents in one turn must not
        # replay every terminal card; retention caps to the most recent N.
        state = _make_state()
        slot = _make_slot()
        state._slots = {slot.key: slot}
        tracker = slot._native_subagent_tracker
        n = NATIVE_SUBAGENT_TERMINAL_KEEP + 10
        _base = time.time()
        for i in range(n):
            tracker[f"s{i}"] = {
                "id": f"native:s{i}",
                "started": 0.0,
                "done": True,
                "agent": "w",
                "task": "t",
                "elapsed": 1.0,
                "error": None,
                "result": "r",
                "done_at": _base + i,  # recent, ascending completion time
            }
        snaps = DashboardState.native_subagent_snapshots(state)
        assert len(snaps) == NATIVE_SUBAGENT_TERMINAL_KEEP
        # The most recently completed (largest done_at) are the ones retained.
        kept = {s["id"] for s in snaps}
        assert f"native:s{n - 1}" in kept
        assert "native:s0" not in kept

    def test_running_cards_not_capped_by_terminal_limit(self):
        # Running cards always replay; only terminal cards are capped.
        state = _make_state()
        slot = _make_slot()
        state._slots = {slot.key: slot}
        tracker = slot._native_subagent_tracker
        for i in range(NATIVE_SUBAGENT_TERMINAL_KEEP + 5):
            tracker[f"r{i}"] = {
                "id": f"native:r{i}",
                "started": 0.0,
                "done": False,
                "agent": "w",
                "task": "t",
                "last_tool": "",
            }
        snaps = DashboardState.native_subagent_snapshots(state)
        assert len(snaps) == NATIVE_SUBAGENT_TERMINAL_KEEP + 5
        assert all(s["done"] is False for s in snaps)


class TestAppendNativeOutput:
    """`_append_native_output` keeps a bounded rolling buffer so a noisy native
    sub-agent can't grow the feed or the completion join without limit."""

    def test_below_cap_accumulates_verbatim(self):
        buf: list[str] = []
        total = 0
        for chunk in ["ab", "cd", "ef"]:
            total = _append_native_output(buf, chunk, total)
        assert buf == ["ab", "cd", "ef"]
        assert total == 6
        assert "".join(buf) == "abcdef"

    def test_collapses_once_past_hard_ceiling(self):
        buf: list[str] = []
        total = 0
        # Push well past the hard ceiling in many small chunks.
        for _ in range(200):
            total = _append_native_output(buf, "x" * 1000, total)
        # Buffer stays bounded by the hard ceiling (collapses to the CAP tail
        # once it grows past HARD); length never runs away.
        assert total <= NATIVE_SUBAGENT_OUTPUT_HARD
        assert sum(len(c) for c in buf) <= NATIVE_SUBAGENT_OUTPUT_HARD
        assert total == len("".join(buf))

    def test_preserves_trailing_tail_exactly(self):
        # After bounding, the retained content is always the trailing tail, so
        # snapshot semantics (native_subagent_output_tail last 40 KB) are unaffected.
        buf: list[str] = []
        total = 0
        # Distinct trailing marker after a large volume of filler.
        for _ in range(90):
            total = _append_native_output(buf, "f" * 1000, total)
        total = _append_native_output(buf, "TAILMARKER", total)
        joined = "".join(buf)
        assert joined.endswith("TAILMARKER")
        # Bounded by the hard ceiling between collapses.
        assert len(joined) <= NATIVE_SUBAGENT_OUTPUT_HARD
        # The last 40 KB tail is intact regardless of collapse state.
        assert native_subagent_output_tail(buf, 40_000).endswith("TAILMARKER")
        assert len(native_subagent_output_tail(buf, 40_000)) == 40_000

    def test_bounded_buffer_keeps_done_result_within_cap(self):
        # The done result is the trailing 8 KB of the (now bounded) buffer,
        # prefixed with a truncation marker when the head is dropped.
        buf: list[str] = []
        total = 0
        for _ in range(200):
            total = _append_native_output(buf, "y" * 1000, total)
        done_result = _native_done_result(buf)
        assert len(done_result) <= 8000 + len(NATIVE_SUBAGENT_DONE_TRUNC_MARKER)
        assert done_result.endswith("y" * 100)

    def test_hard_ceiling_bounds_peak_memory(self):
        # Between collapses the buffer never exceeds the hard ceiling by more
        # than a single incoming chunk.
        buf: list[str] = []
        total = 0
        peak = 0
        for _ in range(500):
            total = _append_native_output(buf, "z" * 100, total)
            peak = max(peak, sum(len(c) for c in buf))
        assert peak <= NATIVE_SUBAGENT_OUTPUT_HARD + 100


class TestNativeDoneResult:
    """`_native_done_result` sends the NEWEST 8 KB of a card's feed, with an
    explicit marker when older output is dropped (finding 3)."""

    def test_under_cap_verbatim(self):
        assert _native_done_result(["hello ", "world"]) == "hello world"

    def test_none_is_empty(self):
        assert _native_done_result(None) == ""
        assert _native_done_result([]) == ""

    def test_over_cap_keeps_newest_tail_with_marker(self):
        chunks = ["OLD" + "x" * 100, "y" * 9000]
        result = _native_done_result(chunks)
        assert result.startswith(NATIVE_SUBAGENT_DONE_TRUNC_MARKER)
        body = result[len(NATIVE_SUBAGENT_DONE_TRUNC_MARKER) :]
        assert len(body) == 8000
        assert body == ("OLD" + "x" * 100 + "y" * 9000)[-8000:]
        assert "OLD" not in body  # oldest head dropped

    def test_exactly_at_cap_is_verbatim(self):
        chunks = ["c" * 8000]
        assert _native_done_result(chunks) == "c" * 8000  # no marker at the cap


class TestRetainTerminalNative:
    """Turn-exit retention keeps only terminal (done) cards, bounded + stale-
    pruned, so a reconnect after the parent turn exits still replays them
    without leaking across turns (finding 2)."""

    def _done(self, sid, done_at):
        return {
            "id": f"native:{sid}",
            "started": 0.0,
            "done": True,
            "agent": "w",
            "task": "t",
            "elapsed": 1.0,
            "error": None,
            "result": "r",
            "done_at": done_at,
        }

    def test_keeps_only_done_records(self):
        now = time.time()
        tracker = {
            "s1": self._done("s1", now),
            "s2": {
                "id": "native:s2",
                "started": now,
                "done": False,
                "agent": "w",
                "task": "t",
                "last_tool": "",
            },
        }
        retained = _retain_terminal_native(tracker, now=now)
        assert set(retained) == {"s1"}

    def test_prunes_stale_by_ttl(self):
        now = time.time()
        tracker = {
            "fresh": self._done("fresh", now - 10),
            "stale": self._done("stale", now - 10_000),  # older than the TTL
        }
        retained = _retain_terminal_native(tracker, now=now)
        assert set(retained) == {"fresh"}

    def test_bounded_to_keep_most_recent(self):
        now = time.time()
        tracker = {
            f"s{i}": self._done(f"s{i}", now - (100 - i)) for i in range(NATIVE_SUBAGENT_TERMINAL_KEEP + 5)
        }
        retained = _retain_terminal_native(tracker, now=now)
        assert len(retained) == NATIVE_SUBAGENT_TERMINAL_KEEP
        assert f"s{NATIVE_SUBAGENT_TERMINAL_KEEP + 4}" in retained  # newest kept
        assert "s0" not in retained  # oldest dropped

    def test_empty_when_no_done(self):
        tracker = {"r": {"id": "native:r", "done": False, "started": 0.0}}
        assert _retain_terminal_native(tracker) == {}


class TestNativeCrewAutoApproveGate:
    """Gate for the native-crew auto-approve path in chat_runner (CWE-1188).

    Secure default: a tool is auto-approved on this path ONLY when a native crew
    subagent is ACTIVE *and* at least one trust signal (auto_approve_subagent_tools
    hook / slot._trust / yolo) is set. With no active crew — or with all signals
    false — the predicate is False and the tool falls through to the normal
    interactive/trust gate instead of being silently approved.
    """

    def _state(self, *, hook=False, yolo=False):
        state = MagicMock()
        state.context_builder = MagicMock()
        state.context_builder.hooks.auto_approve_subagent_tools = hook
        state.is_yolo_active.return_value = yolo
        return state

    def _slot(self, *, trust=False):
        slot = MagicMock()
        slot._trust = trust
        return slot

    def test_no_active_crew_all_conditions_false_denies(self):
        # The core negative case: no crew active AND every trust signal false →
        # NOT auto-approved.
        state = self._state(hook=False, yolo=False)
        slot = self._slot(trust=False)
        assert _native_crew_should_auto_approve({}, state, slot) is False

    def test_no_active_crew_all_done_denies(self):
        # A tracker with only finished subagents is not "active".
        tracker = {"s1": {"done": True}, "s2": {"done": True}}
        state = self._state(hook=False, yolo=False)
        slot = self._slot(trust=False)
        assert _native_crew_should_auto_approve(tracker, state, slot) is False

    def test_no_active_crew_ignores_trust_signals(self):
        # Active-crew is a NECESSARY precondition: even with yolo/trust/hook all
        # ON, no active crew means this path must not auto-approve.
        state = self._state(hook=True, yolo=True)
        slot = self._slot(trust=True)
        assert _native_crew_should_auto_approve({}, state, slot) is False

    def test_active_crew_all_conditions_false_denies(self):
        # Deny-by-default: an active crew alone does not grant approval — a trust
        # signal must also be present.
        tracker = {"s1": {"done": False}}
        state = self._state(hook=False, yolo=False)
        slot = self._slot(trust=False)
        assert _native_crew_should_auto_approve(tracker, state, slot) is False

    def test_active_crew_with_hook_approves(self):
        tracker = {"s1": {"done": False}}
        state = self._state(hook=True, yolo=False)
        slot = self._slot(trust=False)
        assert _native_crew_should_auto_approve(tracker, state, slot) is True

    def test_active_crew_with_slot_trust_approves(self):
        tracker = {"s1": {"done": False}}
        state = self._state(hook=False, yolo=False)
        slot = self._slot(trust=True)
        assert _native_crew_should_auto_approve(tracker, state, slot) is True

    def test_active_crew_with_yolo_approves(self):
        tracker = {"s1": {"done": False}}
        state = self._state(hook=False, yolo=True)
        slot = self._slot(trust=False)
        assert _native_crew_should_auto_approve(tracker, state, slot) is True


class TestNativeAutoApproveLogSanitization:
    """Regression for CWE-117 log forging in the native-crew auto-approve
    debug line.

    Driving the full ``_native_crew_should_auto_approve`` -> ``logger.debug``
    path requires the whole chat_runner ACP event loop with a live client and
    request stream, which is impractical to stand up in a unit test. Instead
    this exercises the PRODUCTION helper ``_safe_native_crew_debug_title`` that
    the handler applies to the tool title before logging, plus the ``%r``
    formatting at the log call site. Reverting the redaction in chat_runner.py
    therefore breaks these assertions.
    """

    def test_newline_is_escaped_not_forged(self) -> None:
        from kiro_crew.dashboard.chat_runner import _safe_native_crew_debug_title

        forged = "innocent\nERROR: forged admin line"
        safe = _safe_native_crew_debug_title(forged)
        # The log call site renders the (already-redacted) title with %r; the
        # raw newline must survive to the helper output (redaction does not
        # strip it) but be escaped by repr into the literal two-char \n.
        rendered = "%r" % safe
        assert "\n" not in rendered
        assert "\\n" in rendered
        # The forged content cannot appear at the start of its own line.
        assert "\nERROR: forged admin line" not in rendered

    def test_credential_redacted_before_logging(self) -> None:
        from kiro_crew.dashboard.chat_runner import _safe_native_crew_debug_title

        cred = "ghp_" + "a" * 36
        safe = _safe_native_crew_debug_title("call " + cred + "\ntrailer")
        # Credential is replaced with the redaction marker.
        assert cred not in safe
        assert "REDACTED" in safe
        # And the embedded newline is still escaped, not raw, once formatted.
        rendered = "%r" % safe
        assert "\n" not in rendered
        assert "\\n" in rendered
