"""A linked Slack thread's messages while a sign-in failure holds the slot's queue."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_slack_channel_agent_link import PINNED, _linked_ds, _patches

from kiro_crew.shared_crew.identity import local_operator_principal


@pytest.fixture(autouse=True)
def _no_execution_record(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution", lambda *_a, **_k: None
    )


class TestSignedOutLinkedThread:
    @pytest.mark.asyncio
    async def test_the_turn_runs_under_the_redacted_row_the_route_wrote(self):
        from kiro_crew.slack import handler

        slack = MagicMock()
        slack.post_message = AsyncMock()
        ds, _slot = _linked_ds(agent="restricted-agent")
        written: list[dict] = []

        def _append(_state, _slot, role, content, cls, **_kw):
            written.append({"role": role, "content": content, "cls": cls})
            return written[-1]

        raw = "token " + "ghp_" + "a" * 36
        a, b = _patches()
        with (
            a,
            b,
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch.object(handler, "append_and_surface", _append),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
        ):
            await handler.maybe_route_linked_thread(raw, "slack:t1", "U1", PINNED, slack, "t1")
            await asyncio.sleep(0)
        (row,) = written
        assert row["content"] != raw
        assert run_chat.call_args.args[2] == raw
        assert run_chat.call_args.kwargs["_current_message"] is row

    @staticmethod
    async def _route_while_held(text, held_text, author=None):
        from kiro_crew.dashboard.chat_utils_injected_rows import SIGNOUT_RETAINED_KEY
        from kiro_crew.dashboard.slot_queue_repository import PUT_BACK_KEY
        from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot
        from kiro_crew.slack import handler

        slack = MagicMock()
        slack.post_message = AsyncMock()
        ds, _mock = _linked_ds(agent="restricted-agent")
        slot = _ChatSlot("slot1")
        slot.agent = "restricted-agent"
        ds.get_linked_slot = MagicMock(return_value=slot)
        slot._last_turn_auth_required = True
        meta = {"author": author} if author else None
        slot.queue_append(held_text, meta=meta, directive_user_origin=True)
        slot._queue[0].update({PUT_BACK_KEY: "g1", SIGNOUT_RETAINED_KEY: True})
        drained: list[list[str]] = []

        async def _drain(_state, keys):
            drained.append([entry["content"] for entry in slot._queue])

        a, b = _patches()
        with (
            a,
            b,
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch.object(
                handler,
                "append_and_surface",
                lambda _s, _sl, role, content, cls, **kw: {"role": role, "content": content, **kw},
            ),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
            patch("kiro_crew.dashboard.chat_runner._drain_parked_queues", _drain),
        ):
            await handler.maybe_route_linked_thread(
                text, "slack:t1", "U1", PINNED, slack, "t1", user_meta=meta
            )
            await asyncio.sleep(0)
        return run_chat, drained

    @pytest.mark.asyncio
    async def test_a_message_queued_behind_held_prompts_writes_one_row(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_runner as cr
        from kiro_crew.dashboard import responsive_chat
        from kiro_crew.dashboard.chat_utils_injected_rows import SIGNOUT_RETAINED_KEY
        from kiro_crew.dashboard.slot_queue_repository import PUT_BACK_KEY
        from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot
        from kiro_crew.slack import handler

        slack = MagicMock()
        slack.post_message = AsyncMock()
        ds, _mock = _linked_ds(agent="restricted-agent")
        slot = _ChatSlot("slot1")
        slot.agent = "restricted-agent"
        ds.get_linked_slot = MagicMock(return_value=slot)
        slot._last_turn_auth_required = True
        held_row = slot.append("user", "held", "msg msg-u")
        slot.queue_append("held", directive_user_origin=True)
        slot._queue[0].update({PUT_BACK_KEY: "g1", SIGNOUT_RETAINED_KEY: True})
        responsive_chat.remember_row(slot, slot._queue[0], held_row)
        raw = "token " + "ghp_" + "a" * 36
        ran: list[str] = []

        async def fake_run_chat(_st, _sl, msg, **_kwargs):
            ran.append(msg)

        async def no_drain(_state, _keys):
            return None

        a, b = _patches()
        with (
            a,
            b,
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch.object(
                handler,
                "append_and_surface",
                lambda _s, sl, role, content, cls, **_kw: sl.append(role, content, cls),
            ),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock),
            patch("kiro_crew.dashboard.chat_runner._drain_parked_queues", no_drain),
        ):
            await handler.maybe_route_linked_thread(raw, "slack:t1", "U1", PINNED, slack, "t1")
        from chat_test_helpers import _make_state

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

        assert len(ran) == 2 and ran[0] == "held"
        users = [m["content"] for m in slot.messages if m.get("role") == "user"]
        assert len(users) == 2 and users[0] == "held" and users[1] != raw

    @pytest.mark.asyncio
    async def test_a_message_waits_behind_prompts_held_for_sign_in(self):
        run_chat, drained = await self._route_while_held("sent after signing in", "held")

        run_chat.assert_not_called()
        assert drained == [["held", "sent after signing in"]]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "author, left",
        [(local_operator_principal("me").to_public_dict(), 1), (None, 2)],
        ids=["same-sender", "unknown-senders"],
    )
    async def test_only_the_same_known_sender_drops_the_held_copy(self, author, left):
        run_chat, drained = await self._route_while_held("deploy it", "deploy it", author)

        run_chat.assert_not_called()
        assert drained == [["deploy it"] * left]
