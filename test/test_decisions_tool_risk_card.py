"""The ride-along, and the thing it must not touch: the permission decision.

Two halves.

The FIRST half drives the real ``_run_chat`` with a scripted ``tool_call`` frame
and checks that a ``tool.risk`` record reaches the tool card through BOTH doors a
client reads -- the live ``chat_message`` frame ``slot.append`` broadcasts from
inside the call, and the transcript line the history writer persists -- from ONE
write, and that it lands on the TOOL row rather than on the assistant reply. The
absence case is the one that has to hold on every ordinary turn: with the seam
off, a tool row must be exactly what it is today.

The SECOND half is the mutation check the feature's whole claim rests on:
permission behaviour is BYTE-IDENTICAL with the point answering and with it
silent. It runs one turn that raises a permission request in a trusting session
twice -- once with the point returning a ``risky`` record, once returning
``None`` -- and holds the approval calls and the SEL audit rows equal. That is a
property rather than a promise, and it is why the hook sits in the ``tool_call``
branch, which contains no approval code at all.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
)
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_persistence import _build_message_entry_uncached
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.decisions.points import tool_risk as tr
from kiro_crew.history import ConversationLog
from kiro_crew.providers.base import LLMEvent

RECORD = {
    "ts": "2026-09-20T07:00:00+00:00",
    "point": "tool.risk",
    "session": "0123456789ab",
    "latency_ms": 420,
    "scrubbed": False,
    "answers": None,
    "error": None,
    "turn_id": "tr-7",
    "tool": "bash",
    "tier": "risky",
    "p": 0.88,
    "policy": "trust",
    "flagged": True,
}


def _state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_slack_link = MagicMock(return_value=(None, None))
    sessions.get_mirror_link = MagicMock(return_value=None)
    sessions.get_provider = MagicMock(return_value=None)
    sessions.resumable_sid = MagicMock(return_value=None)
    sessions.check_context_usage = MagicMock()
    sessions.reset = AsyncMock()
    sessions.remove = AsyncMock()
    sessions.record_failure = AsyncMock()
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.push_refresh = MagicMock()
    state.refresh_slot_source_status = MagicMock()
    state.broadcast_context_usage = MagicMock()
    return state


def _slot(key: str = "chat-risk-1", *, trusted: bool = True) -> _ChatSlot:
    slot = _ChatSlot(key)
    slot._titled = True
    # The interactive "trust this session" grant -- the mode this point annotates.
    slot._trust = trusted
    return slot


def _runner(tmp_path):
    """``(state, client)`` wired for a scripted ``_run_chat`` turn."""
    state = _state(tmp_path)
    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=0.0)
    client.context_window_tokens = MagicMock(return_value=0)
    client.context_used_tokens = MagicMock(return_value=0)
    client.last_prompt_stats = None
    client._client = client
    client.exit_code = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions.consume_replay_suppression = MagicMock(return_value=False)
    state.sessions.consume_needs_reinjection = MagicMock(return_value=False)
    state._hook_store = MagicMock(fire=AsyncMock(return_value=[]))
    return state, client


@contextmanager
def _quiet_sel():
    with patch.object(chat_runner, "sel") as mock_sel:
        mock_sel.return_value = MagicMock()
        yield mock_sel


async def _settle(slot) -> None:
    task = slot.task
    if task is None or not hasattr(task, "cancel"):
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # pragma: no cover - draining, never the assertion
        pass


def _tool_call(
    tool_call_id: str = "tc-1",
    title: str = "bash",
    *,
    arguments: str = '{"command": "rm -rf /data"}',
    tool_kind: str = "execute",
    diff_path: str = "",
) -> LLMEvent:
    return LLMEvent(
        kind=EVENT_TOOL_CALL,
        title=title,
        tool_name=title,
        tool_call_id=tool_call_id,
        tool_kind=tool_kind,
        tool_input=arguments,
        is_shell=True,
        diff_path=diff_path,
    )


def _scripts(client, events) -> None:
    """Script ``client.stream`` so one turn yields *events* then completes."""

    async def _once():
        for event in events:
            yield event
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    client.stream = MagicMock(side_effect=lambda *_a, **_k: _once())


@contextmanager
def _answering(record: dict | None):
    """Patch the POINT, so the wiring under test is the call site, not the oracle."""
    calls: list[dict] = []

    async def _risk_record(**kwargs):
        calls.append(kwargs)
        return record

    with patch.object(tr, "risk_record", _risk_record):
        yield calls


def _tool_rows(slot: _ChatSlot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "tool"]


def _record_of(msg: dict) -> object:
    return (msg.get("meta") or {}).get("decisions_tool_risk")


# ── the ride-along ────────────────────────────────────────────────────────────


class TestTheRecordRidesTheToolCard:
    @pytest.mark.asyncio
    async def test_it_lands_on_the_tool_row(self, tmp_path):
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_tool_call()])

        with _quiet_sel(), _answering(RECORD) as calls:
            await chat_runner._run_chat(state, slot, "clean up /data")
        await _settle(slot)

        rows = _tool_rows(slot)
        assert rows, f"expected a tool row, got {slot.messages}"
        assert _record_of(rows[0]) == RECORD
        assert len(calls) == 1, "one oracle call per tool call"

    @pytest.mark.asyncio
    async def test_the_tool_rows_own_meta_survives_beside_it(self, tmp_path):
        """``_tool_meta``'s fields are what the inline detail panel reads."""
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_tool_call()])

        with _quiet_sel(), _answering(RECORD):
            await chat_runner._run_chat(state, slot, "clean up /data")
        await _settle(slot)

        meta = _tool_rows(slot)[0]["meta"]
        assert meta["decisions_tool_risk"] == RECORD
        assert meta["tool_call_id"]
        assert meta["kind"] == "execute"
        assert meta["input"]

    @pytest.mark.asyncio
    async def test_it_survives_into_the_persisted_transcript_line(self, tmp_path):
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_tool_call()])

        with _quiet_sel(), _answering(RECORD):
            await chat_runner._run_chat(state, slot, "clean up /data")
        await _settle(slot)

        entry = _build_message_entry_uncached(_tool_rows(slot)[0])
        assert entry is not None
        assert entry["meta"]["decisions_tool_risk"] == RECORD
        # Serializable as written: the transcript is JSONL, so a value the writer
        # cannot dump would cost the whole row, not just this field.
        assert json.loads(json.dumps(entry))["meta"]["decisions_tool_risk"] == RECORD

    @pytest.mark.asyncio
    async def test_the_live_frame_carries_it_because_it_is_set_before_the_broadcast(self, tmp_path):
        """``slot.append`` broadcasts from inside the call, so a later write misses it."""
        state, client = _runner(tmp_path)
        slot = _slot()
        broadcast: list[dict] = []
        slot._on_message = lambda _key, msg: broadcast.append(json.loads(json.dumps(msg)))
        _scripts(client, [_tool_call()])

        with _quiet_sel(), _answering(RECORD):
            await chat_runner._run_chat(state, slot, "clean up /data")
        await _settle(slot)

        tools = [m for m in broadcast if m.get("role") == "tool"]
        assert len(tools) == 1
        assert tools[0]["meta"]["decisions_tool_risk"] == RECORD

    @pytest.mark.asyncio
    async def test_the_assistant_reply_does_not_wear_it(self, tmp_path):
        """The annotation is about ONE call, so it belongs on that call's card."""
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(
            client,
            [_tool_call(), LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")],
        )

        with _quiet_sel(), _answering(RECORD):
            await chat_runner._run_chat(state, slot, "clean up /data")
        await _settle(slot)

        assistants = [m for m in slot.messages if m.get("role") == "assistant"]
        assert assistants, f"expected a reply, got {slot.messages}"
        for row in assistants:
            assert _record_of(row) is None

    @pytest.mark.asyncio
    async def test_each_tool_call_carries_its_own_record(self, tmp_path):
        """Several calls in one turn, so the per-card claim is not a one-card claim."""
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_tool_call("tc-1"), _tool_call("tc-2", "fsWrite")])

        with _quiet_sel(), _answering(RECORD) as calls:
            await chat_runner._run_chat(state, slot, "clean up /data")
        await _settle(slot)

        rows = _tool_rows(slot)
        assert len(rows) == 2
        assert all(_record_of(row) == RECORD for row in rows)
        # The caller's own counter drives the point's per-turn cap.
        assert [c["calls_this_turn"] for c in calls] == [1, 2]


class TestNothingAnswered:
    @pytest.mark.asyncio
    async def test_the_tool_row_carries_no_record_key_at_all(self, tmp_path):
        """Absent, not null: a consumer must not have to special-case an empty value."""
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_tool_call()])

        with _quiet_sel(), _answering(None):
            await chat_runner._run_chat(state, slot, "clean up /data")
        await _settle(slot)

        assert "decisions_tool_risk" not in (_tool_rows(slot)[0].get("meta") or {})

    @pytest.mark.asyncio
    async def test_the_persisted_line_is_unchanged_by_the_seam(self, tmp_path):
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_tool_call()])

        with _quiet_sel(), _answering(None):
            await chat_runner._run_chat(state, slot, "clean up /data")
        await _settle(slot)

        entry = _build_message_entry_uncached(_tool_rows(slot)[0])
        assert entry is not None
        assert "decisions_tool_risk" not in (entry.get("meta") or {})

    @pytest.mark.asyncio
    async def test_a_session_that_prompts_is_never_asked(self, tmp_path):
        """No trust, no annotation -- and no oracle call, so it costs that session nothing."""
        state, client = _runner(tmp_path)
        slot = _slot(trusted=False)
        state.is_yolo_active = MagicMock(return_value=False)
        _scripts(client, [_tool_call()])

        with _quiet_sel(), _answering(RECORD) as calls:
            await chat_runner._run_chat(state, slot, "clean up /data")
        await _settle(slot)

        assert calls == []
        assert "decisions_tool_risk" not in (_tool_rows(slot)[0].get("meta") or {})

    @pytest.mark.asyncio
    async def test_a_raising_point_cannot_cost_the_tool_row(self, tmp_path):
        state, client = _runner(tmp_path)
        slot = _slot()

        async def _boom(**_kwargs):
            raise RuntimeError("point exploded")

        _scripts(client, [_tool_call()])
        with _quiet_sel(), patch.object(tr, "risk_record", _boom):
            await chat_runner._run_chat(state, slot, "clean up /data")
        await _settle(slot)

        rows = _tool_rows(slot)
        assert rows, "the card is appended even when the annotation fails"
        assert "decisions_tool_risk" not in (rows[0].get("meta") or {})


class TestTheModeItAnnotates:
    def test_it_reads_the_same_two_helpers_the_permission_branch_decides_by(self):
        state = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        slot = _slot(trusted=False)

        assert chat_runner._session_auto_approves(state, slot) is False

        slot._trust = True
        assert chat_runner._session_auto_approves(state, slot) is True

        slot._trust = False
        state.is_yolo_active = MagicMock(return_value=True)
        assert chat_runner._session_auto_approves(state, slot) is True

    @pytest.mark.asyncio
    async def test_the_policy_it_reports_is_the_grant_that_answers_the_call(self, tmp_path):
        state, client = _runner(tmp_path)
        slot = _slot()
        state.is_yolo_active = MagicMock(return_value=False)
        _scripts(client, [_tool_call()])

        with _quiet_sel(), _answering(RECORD) as calls:
            await chat_runner._run_chat(state, slot, "clean up /data")
        await _settle(slot)

        assert calls[0]["policy"] == chat_runner._auto_approve_reason(slot, False)
        assert calls[0]["tool"] == "bash"
        assert calls[0]["arguments"] == '{"command": "rm -rf /data"}'
        assert calls[0]["message"] == "clean up /data"
        # The harness's ACP kind rides along; it decides the file-write rule.
        assert calls[0]["tool_kind"] == "execute"

    @pytest.mark.asyncio
    async def test_the_cached_diff_path_rides_along_too(self, tmp_path):
        """``diff_path`` is the OR half of the file-write predicate: an edit whose
        ACP ``kind`` arrives empty or ``read`` still routes on this alone, so a
        wiring test that only checks ``tool_kind`` would pass even if this field
        were silently dropped between the event and the call site."""
        state, client = _runner(tmp_path)
        slot = _slot()
        state.is_yolo_active = MagicMock(return_value=False)
        _scripts(client, [_tool_call(tool_kind="read", diff_path="README.md")])

        with _quiet_sel(), _answering(RECORD) as calls:
            await chat_runner._run_chat(state, slot, "clean up /data")
        await _settle(slot)

        assert calls[0]["tool_kind"] == "read"
        assert calls[0]["diff_path"] == "README.md"


# ── the mutation check: permission behaviour is byte-identical ────────────────


def _approval_trace(client, mock_sel) -> dict:
    """Everything the permission path did, as data two runs can be held equal on."""
    audits = [
        {k: v for k, v in call.kwargs.items() if k in {"outcome", "tool_name", "metadata"}}
        for call in mock_sel.return_value.log_tool_invocation.call_args_list
    ]
    return {
        "approved": [c.args[0] for c in client.approve_tool.call_args_list],
        "rejected": [c.args[0] for c in client.reject_tool.call_args_list],
        "audits": audits,
    }


async def _trusted_turn_with_a_permission_request(tmp_path, record: dict | None) -> dict:
    state, client = _runner(tmp_path)
    slot = _slot()
    state.is_yolo_active = MagicMock(return_value=False)
    _scripts(
        client,
        [
            _tool_call(),
            LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="rq1",
                title="bash",
                tool_name="bash",
                tool_call_id="tc-1",
                tool_kind="execute",
                tool_input='{"command": "rm -rf /data"}',
                is_shell=True,
                options=[{"id": "approve"}, {"id": "reject"}],
            ),
        ],
    )

    with _quiet_sel() as mock_sel, _answering(record):
        await chat_runner._run_chat(state, slot, "clean up /data")
    await _settle(slot)
    return _approval_trace(client, mock_sel)


class TestPermissionBehaviourIsUnchanged:
    """The feature's whole claim, as a mutation over the point's own answer.

    If a badge could ever change what the policy did, these two traces would
    differ. They are collected from the same scripted turn, so a difference is the
    annotation and nothing else.
    """

    @pytest.mark.asyncio
    async def test_a_risky_verdict_changes_nothing_about_the_approval(self, tmp_path):
        flagged = await _trusted_turn_with_a_permission_request(tmp_path / "on", RECORD)
        silent = await _trusted_turn_with_a_permission_request(tmp_path / "off", None)

        assert flagged["approved"] == silent["approved"]
        assert flagged["rejected"] == silent["rejected"]
        assert flagged["audits"] == silent["audits"]

    @pytest.mark.asyncio
    async def test_the_request_was_actually_answered_in_both_runs(self, tmp_path):
        """Otherwise the equality above would hold by measuring nothing."""
        flagged = await _trusted_turn_with_a_permission_request(tmp_path / "on", RECORD)

        assert flagged["approved"] == ["rq1"]
        assert flagged["rejected"] == []
        assert any(a["outcome"] == "auto_approved" for a in flagged["audits"])

    @pytest.mark.asyncio
    async def test_a_badged_call_can_still_be_refused(self, tmp_path):
        """The badge is not a verdict on whether the call RAN, and the docs say so.

        A `PreToolUse` hook blocking the call is the cheapest way to reach that
        state: the card is appended with its note in the `tool_call` branch, and the
        refusal happens afterwards in the permission branch. Pinned because the
        first version of this feature's own prose claimed "a flagged call still
        runs", which is exactly the sentence a reader would act on.
        """
        state, client = _runner(tmp_path)
        slot = _slot()
        state.is_yolo_active = MagicMock(return_value=False)
        # A blocking PreToolUse hook, in the shape `_pre_tool_hooks_should_block`
        # reads: the host refuses the call the seam has already annotated.
        state._hook_store = MagicMock(
            fire=AsyncMock(return_value=[{"decision": "block", "reason": "denied by policy"}])
        )
        _scripts(
            client,
            [
                _tool_call(),
                LLMEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    request_id="rq1",
                    title="bash",
                    tool_name="bash",
                    tool_call_id="tc-1",
                    tool_kind="execute",
                    tool_input='{"command": "rm -rf /data"}',
                    is_shell=True,
                    options=[{"id": "approve"}, {"id": "reject"}],
                ),
            ],
        )

        with _quiet_sel(), _answering(RECORD):
            await chat_runner._run_chat(state, slot, "clean up /data")
        await _settle(slot)

        rows = _tool_rows(slot)
        assert rows, f"expected a tool card, got {slot.messages}"
        assert _record_of(rows[0]) == RECORD, "the card still carries its note"
        assert client.reject_tool.call_args_list, "and the call was refused anyway"
        assert client.approve_tool.call_args_list == [], "never approved"
