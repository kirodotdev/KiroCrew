"""A linked Slack thread's messages while a sign-in failure holds the slot's queue."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_slack_channel_agent_link import PINNED, _linked_ds, _patches

from kiro_crew.shared_crew.identity import local_operator_principal
from kiro_crew.testing.synthetic_credentials import github_classic


@pytest.fixture(autouse=True)
def _no_execution_record(_floor_monkeypatch, tmp_path):
    """No execution record, and the owner's redaction switch reads ON (it defaults to OFF)."""
    from kiro_crew.security import redaction_switch

    _floor_monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution", lambda *_a, **_k: None
    )
    _floor_monkeypatch.setattr(redaction_switch, "_path", lambda: tmp_path / "redaction.json")
    _floor_monkeypatch.setattr(redaction_switch, "DEFAULT_REDACTION_ENABLED", True)


def _hold(slot, text, meta=None):
    from kiro_crew.dashboard.chat_utils_injected_rows import SIGNOUT_RETAINED_KEY
    from kiro_crew.dashboard.slot_queue_repository import PUT_BACK_KEY

    slot.agent = "restricted-agent"
    slot._last_turn_auth_required = True
    slot.queue_append(text, meta=meta, directive_user_origin=True)
    slot._queue[-1].update({PUT_BACK_KEY: "g1", SIGNOUT_RETAINED_KEY: True})
    return slot._queue[-1]


def _append_row(_s, sl, role, content, cls, **_kw):
    return sl.append(role, content, cls)


async def _route(slot, text, *extra, append=_append_row, user_meta=None):
    from kiro_crew.slack import handler

    slack = MagicMock()
    slack.post_message = AsyncMock()
    ds, _mock = _linked_ds(agent="restricted-agent")
    if slot is not None:
        ds.get_linked_slot = MagicMock(return_value=slot)
    a, b = _patches()
    with ExitStack() as stack:
        for ctx in (a, b, patch.object(handler, "_dashboard_state", ds), *extra):
            stack.enter_context(ctx)
        stack.enter_context(patch.object(handler, "is_allowed_user", return_value=True))
        stack.enter_context(patch.object(handler, "append_and_surface", append))
        await handler.maybe_route_linked_thread(
            text, "slack:t1", "U1", PINNED, slack, "t1", user_meta=user_meta
        )
        await asyncio.sleep(0)
        if slot is not None and slot.task is not None:
            await slot.task


class TestSignedOutLinkedThread:
    @pytest.mark.asyncio
    async def test_the_turn_runs_under_the_redacted_row_the_route_wrote(self):
        written: list[dict] = []

        def _append(_state, _slot, role, content, cls, **_kw):
            written.append({"role": role, "content": content, "cls": cls})
            return written[-1]

        raw = "token " + github_classic()
        direct = patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock)
        run_chat = direct.start()
        try:
            await _route(None, raw, append=_append)
        finally:
            direct.stop()
        (row,) = written
        assert row["content"] != raw
        assert run_chat.call_args.args[2] == raw
        assert run_chat.call_args.kwargs["_current_message"] is row

    @pytest.mark.asyncio
    async def test_a_message_queued_behind_held_prompts_writes_one_row(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard import chat_runner as cr
        from kiro_crew.dashboard import responsive_chat
        from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot

        slot = _ChatSlot("slot1")
        held_row = slot.append("user", "held", "msg msg-u")
        responsive_chat.remember_row(slot, _hold(slot, "held"), held_row)
        raw = "token " + github_classic()
        ran: list[str] = []

        async def fake_run_chat(_st, _sl, msg, **_kwargs):
            ran.append(msg)

        await _route(
            slot,
            raw,
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock),
            patch("kiro_crew.dashboard.chat_runner._drain_parked_queues", AsyncMock()),
        )
        state = _make_state(tmp_path)
        state._slots[slot.key] = slot
        slot._last_turn_auth_required = False
        with patch.object(cr, "_run_chat", fake_run_chat):
            for _ in range(3):
                if not slot._queue:
                    break
                await cr._start_next_queued_turn(state, slot)
                if slot.task is not None:
                    await slot.task
        assert ran == ["held", raw]
        users = [m["content"] for m in slot.messages if m.get("role") == "user"]
        assert len(users) == 2 and users[0] == "held" and users[1] != raw

    @pytest.mark.asyncio
    async def test_a_thread_send_after_sign_in_passes_a_child(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard import chat_handlers, chat_runner, chat_turn

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slot1")
        _hold(slot, "held during the outage")
        for module in (chat_handlers, chat_runner, chat_turn.queue_drain):
            monkeypatch.setattr(module, "subagents_hold_user_messages", lambda *_a: True)
        ran: list[str] = []

        async def fake_run_chat(_st, _sl, msg, **_kwargs):
            ran.append(msg)

        real_drain = chat_runner._drain_parked_queues

        async def drain_on_state(_ds, keys, **kwargs):
            await real_drain(state, keys, **kwargs)

        monkeypatch.setattr(chat_runner, "_run_chat", fake_run_chat)
        monkeypatch.setattr(chat_runner, "_drain_parked_queues", drain_on_state)
        with patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as direct:
            await _route(slot, "sent after signing in")
        direct.assert_not_called()
        assert ran[:1] == ["held during the outage"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text, author",
        [("sent after signing in", None), ("deploy it", local_operator_principal("me"))],
        ids=["new-send-waits", "same-sender-resend-waits-too"],
    )
    async def test_a_send_waits_behind_the_held_prompts(self, text, author):
        from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot

        meta = {"author": author.to_public_dict()} if author else None
        slot = _ChatSlot("slot1")
        _hold(slot, "deploy it", meta)
        drained: list[list[str]] = []

        async def _drain(_state, _keys, **_kw):
            drained.append([entry["content"] for entry in slot._queue])

        run_chat = patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock)
        drain = patch("kiro_crew.dashboard.chat_runner._drain_parked_queues", _drain)

        def _shown(_s, _sl, role, content, cls, **kw):
            return {"role": role, "content": content, **kw}

        with run_chat as direct:
            await _route(slot, text, drain, append=_shown, user_meta=meta)
        direct.assert_not_called()
        assert drained == [["deploy it", text]]
