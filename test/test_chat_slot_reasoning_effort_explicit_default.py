"""Reasoning-effort endpoint tests: the explicit Default intent a request leaves behind.

An explicit Default the request could not apply itself is recorded on the
session key for its next cold start; no answer reports success after a failed
intent write, and the same pick retries an owed write.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_slot_reasoning_effort_helpers import _make_app, _mock_state

from kiro_crew.dashboard import chat_handlers, chat_persistence
from kiro_crew.dashboard.chat_api import slot_effort
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.testing.wait import default_timeout


async def _await_test(awaitable, what: str):
    try:
        async with asyncio.timeout(default_timeout()) as deadline:
            return await awaitable
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        raise AssertionError(f"timed out waiting for {what}") from exc


class TestExplicitDefaultIntent:
    """An explicit Default the request could not apply itself reaches the next cold start.

    Only an explicit Default may remove a workspace effort entry Kiro Crew did
    not write. When the pick cannot do that itself -- no live session, a turn in
    flight, a live clear that raised, a model without effort -- the handler
    records the intent on the session key, and the key's next cold start
    applies it before spawning.
    """

    @staticmethod
    def _live(**overrides: object) -> MagicMock:
        provider = MagicMock(spec=AcpProvider)
        provider.supports_effort = MagicMock(return_value=True)
        provider.has_active_turn = MagicMock(return_value=False)
        for name, value in overrides.items():
            setattr(provider, name, value)
        return provider

    @staticmethod
    async def _pick(state: DashboardState, effort: str) -> int:
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": effort}
            )
            return resp.status

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("target_key", "alias_key"),
        [
            ("slack:1700000000.000100", "1700000000.000100"),
            ("1700000000.000100", "slack:1700000000.000100"),
        ],
    )
    async def test_a_saved_change_bumps_every_open_slot_on_the_folded_session(
        self, target_key, alias_key
    ):
        target = _ChatSlot("test")
        target.linked_session_key = target_key
        target.reasoning_effort = "high"
        alias = _ChatSlot("alias")
        alias.linked_session_key = alias_key
        other = _ChatSlot("other")
        other.linked_session_key = "slack:1700000000.000200"
        state = _mock_state(target)
        state._slots.update({alias.key: alias, other.key: other})

        assert await self._pick(state, "") == 200

        assert target._session_effort_intent_gen == 1
        assert alias._session_effort_intent_gen == 1
        assert other._session_effort_intent_gen == 0

    def test_effort_intent_state_is_bounded_by_open_slots(self):
        from kiro_crew.session import SessionManager

        target = _ChatSlot("test")
        target.linked_session_key = "slack:1700000000.000100"
        alias = _ChatSlot("alias")
        alias.linked_session_key = "1700000000.000100"
        other = _ChatSlot("other")
        other.linked_session_key = "slack:1700000000.000200"
        state = _mock_state(target)
        state._slots.update({alias.key: alias, other.key: other})

        for index in range(2_000):
            slot_effort._bump_session_effort_intent_slots(state, f"slack:1800000000.{index:06d}")
        slot_effort._bump_session_effort_intent_slots(state, target.linked_session_key)

        assert target._session_effort_intent_gen == 1
        assert alias._session_effort_intent_gen == 1
        assert other._session_effort_intent_gen == 0
        assert {name for name in vars(SessionManager) if name.startswith("effort_intent_")} == {
            "effort_intent_write"
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reset_verdict", [True, False])
    async def test_a_stopped_chat_leaves_the_default_for_its_next_cold_start(self, reset_verdict):
        # No live session, so nothing can remove the entry now. Recorded before
        # the reset (a message landing mid-reset cold-starts from it) and still
        # set when the request answers, whether or not the reset tore anything
        # down.
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot)
        key = chat_handlers.effective_session_key(slot)
        seen_at_reset: list[bool | None] = []

        async def reset(*_args, **_kwargs):
            seen_at_reset.append(state.sessions.default_intents.get(key))
            return reset_verdict

        state.sessions.reset = AsyncMock(side_effect=reset)

        assert await self._pick(state, "") == 200
        assert seen_at_reset == [True]
        assert state.sessions.default_intents == {key: True}
        state.sessions.aflush.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_cold_start_during_the_reset_spends_the_intent_for_good(self):
        # A message landing mid-reset cold-starts from the committed Default,
        # applies it and clears it. Recording it again once the reset returns
        # would make a later cold start remove an entry written since.
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot)
        key = chat_handlers.effective_session_key(slot)

        async def reset(*_args, **_kwargs):
            state.sessions.default_intents[key] = False
            return True

        state.sessions.reset = AsyncMock(side_effect=reset)

        assert await self._pick(state, "") == 200
        assert state.sessions.default_intents == {key: False}

    @pytest.mark.asyncio
    async def test_a_level_pick_spends_a_pending_default(self):
        slot = _ChatSlot("test")
        slot.reasoning_effort = ""
        state = _mock_state(slot)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[key] = True

        assert await self._pick(state, "high") == 200
        assert state.sessions.default_intents == {key: False}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("applied_live", [True, False])
    async def test_a_live_clear_leaves_nothing_for_a_cold_start(self, applied_live):
        # True applied the default live; False rewrote the file and asks for a
        # reset. Either way this request removed the entry itself.
        provider = self._live(clear_effort=AsyncMock(return_value=applied_live))
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot, provider)
        key = chat_handlers.effective_session_key(slot)

        assert await self._pick(state, "") == 200
        provider.clear_effort.assert_awaited_once_with(
            owned_only=False, fence_rewrite=state.sessions.fence_effort_overlay_rewrite
        )
        assert state.sessions.reset.await_count == (0 if applied_live else 1)
        assert state.sessions.default_intents == {key: False}

    @pytest.mark.asyncio
    async def test_a_live_clear_that_asks_for_a_reset_needs_no_new_row(self):
        # Its rewrite landed and only the live push did not (False), so the pick
        # records no Default: a full explicit-Default bound cannot refuse a pick
        # that already changed the file and the provider.
        provider = self._live(clear_effort=AsyncMock(return_value=False))
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot, provider)
        key = chat_handlers.effective_session_key(slot)
        record = state.sessions.set_explicit_effort_default.side_effect

        def refuse_every_new_row(session_key: str, pending: bool) -> bool:
            return False if pending else record(session_key, pending)

        state.sessions.set_explicit_effort_default.side_effect = refuse_every_new_row

        assert await self._pick(state, "") == 200
        assert slot.reasoning_effort == ""
        state.sessions.reset.assert_awaited_once()
        assert state.sessions.default_intents == {key: False}

    @pytest.mark.asyncio
    async def test_a_live_clear_that_raised_leaves_the_default_for_the_next_cold_start(self):
        provider = self._live(clear_effort=AsyncMock(side_effect=RuntimeError("boom")))
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot, provider)
        key = chat_handlers.effective_session_key(slot)

        assert await self._pick(state, "") == 200
        state.sessions.reset.assert_awaited_once()
        assert state.sessions.default_intents == {key: True}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("effort", "pending"), [("", True), ("high", False)])
    async def test_a_deferred_pick_records_the_intent(self, effort, pending):
        # The push waits for the turn, and a live session on Default is pushed
        # nothing, so only a cold start can remove the entry.
        provider = self._live(has_active_turn=MagicMock(return_value=True))
        slot = _ChatSlot("test")
        slot.reasoning_effort = "low"
        state = _mock_state(slot, provider)
        key = chat_handlers.effective_session_key(slot)

        assert await self._pick(state, effort) == 200
        assert state.sessions.default_intents == {key: pending}
        assert slot._session_effort_intent_gen == int(pending)

    @pytest.mark.asyncio
    async def test_a_model_without_effort_leaves_the_default_for_the_next_cold_start(self):
        provider = self._live(supports_effort=MagicMock(return_value=False))
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot, provider)
        key = chat_handlers.effective_session_key(slot)

        assert await self._pick(state, "") == 200
        state.sessions.reset.assert_not_called()
        assert state.sessions.default_intents == {key: True}

    @pytest.mark.asyncio
    async def test_an_overlay_busy_refusal_records_nothing(self):
        provider = self._live(clear_effort=AsyncMock(return_value=None))
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot, provider)

        assert await self._pick(state, "") == 409
        state.sessions.set_explicit_effort_default.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_refused_intent_returns_503_without_changing_the_slot(self):
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot)
        state.sessions.set_explicit_effort_default = MagicMock(return_value=False)

        assert await self._pick(state, "") == 503
        assert slot.reasoning_effort == "high"
        assert state.sessions.default_intents == {}
        state.sessions.aflush.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_pre_pop_reset_raise_restores_the_prior_intent(self):
        from kiro_crew.providers.base import LLMProvider

        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot)
        key = chat_handlers.effective_session_key(slot)
        alive = MagicMock(spec=LLMProvider)
        alive.has_active_turn.return_value = False
        state.sessions.get_provider = MagicMock(return_value=alive)
        seen_at_reset: list[bool | None] = []

        async def reset(*_args, **_kwargs):
            seen_at_reset.append(state.sessions.default_intents.get(key))
            raise RuntimeError("pre-pop boom")

        state.sessions.reset = AsyncMock(side_effect=reset)

        assert await self._pick(state, "") == 500
        assert seen_at_reset == [True]
        assert slot.reasoning_effort == "high"
        assert state.sessions.default_intents == {key: False}

    @pytest.mark.asyncio
    async def test_a_turn_in_flight_decline_restores_the_prior_intent(self, monkeypatch):
        # The reset declined because a turn slipped in, so nothing switched and
        # the value recorded before the reset is taken back.
        from kiro_crew.providers.base import LLMProvider

        monkeypatch.setattr(chat_handlers, "_switch_target_busy", lambda *_args: False)
        slot = _ChatSlot("test")
        slot.reasoning_effort = ""
        state = _mock_state(slot)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[key] = True
        busy = MagicMock(spec=LLMProvider)
        busy.has_active_turn.return_value = True
        state.sessions.get_provider = MagicMock(return_value=busy)
        state.sessions.reset = AsyncMock(return_value=False)

        assert await self._pick(state, "high") == 409
        assert slot.reasoning_effort == ""
        assert state.sessions.default_intents == {key: True}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("effort", "prior_pending", "restore_pending"),
        [("", False, False), ("high", True, True)],
    )
    async def test_a_reset_refusal_warns_when_rollback_intent_cannot_be_saved(
        self, monkeypatch, effort, prior_pending, restore_pending
    ):
        from kiro_crew.providers.base import LLMProvider

        monkeypatch.setattr(chat_handlers, "_switch_target_busy", lambda *_args: False)
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high" if not effort else ""
        state = _mock_state(slot)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[key] = prior_pending
        busy = MagicMock(spec=LLMProvider)
        busy.has_active_turn.return_value = True
        state.sessions.get_provider = MagicMock(return_value=busy)
        state.sessions.reset = AsyncMock(return_value=False)
        record_intent = state.sessions.set_explicit_effort_default.side_effect

        def refuse_rollback(key: str, pending: bool) -> bool:
            if pending is restore_pending:
                return False
            return record_intent(key, pending)

        state.sessions.set_explicit_effort_default.side_effect = refuse_rollback

        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post(
                "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": effort}
            )
            body = await response.json()

        assert response.status == 409
        assert body["warning"] == slot_effort._EFFORT_INTENT_ROLLBACK_NOT_SAVED

    @pytest.mark.asyncio
    async def test_a_rollback_keeps_an_intent_a_cold_start_spent_during_the_reset(
        self, monkeypatch
    ):
        # A legacy pair model makes a Default pick on a Default slot reach the
        # reset. A cold start during the reset applies the pending Default and
        # spends it; the declined reset must not arm it again.
        from kiro_crew.providers.base import LLMProvider

        monkeypatch.setattr(chat_handlers, "_switch_target_busy", lambda *_args: False)
        monkeypatch.setattr(
            chat_handlers, "_configured_backend_for_slot", AsyncMock(return_value="codex")
        )
        slot = _ChatSlot("test")
        slot.model = "gpt-6-sol[max]"
        slot.reasoning_effort = ""
        state = _mock_state(slot)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[key] = True
        busy = MagicMock(spec=LLMProvider)
        busy.has_active_turn.return_value = True
        state.sessions.get_provider = MagicMock(return_value=busy)

        async def reset(*_args, **_kwargs):
            state.sessions.default_intents[key] = False
            return False

        state.sessions.reset = AsyncMock(side_effect=reset)

        assert await self._pick(state, "") == 409
        assert state.sessions.default_intents == {key: False}

    @pytest.mark.asyncio
    async def test_a_rebind_during_the_live_clear_leaves_the_default_on_the_new_binding(self):
        # The clear landed on the session the slot WAS bound to. The new
        # binding's overlay was never touched, so its next cold start applies
        # the Default.
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"

        async def clear_effort(**_kwargs):
            slot.linked_session_key = "slack:1700000000.000100"
            return True

        provider = self._live(clear_effort=AsyncMock(side_effect=clear_effort))
        state = _mock_state(slot, provider)
        old_key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[old_key] = True

        assert await self._pick(state, "") == 200
        new_key = chat_handlers.effective_session_key(slot)
        assert new_key != old_key
        # The clear removed the entry for the old binding, so its pending
        # Default is spent there.
        assert state.sessions.default_intents == {old_key: False, new_key: True}

    @pytest.mark.asyncio
    async def test_a_failed_save_for_the_old_binding_after_a_rebound_push_is_reported(self):
        # The new binding's intent is saved, so the slot shows the pick; only the
        # binding the push landed on could not be saved, and the answer says so.
        slot = _ChatSlot("test")
        slot.reasoning_effort = ""

        async def change_effort(_effort):
            slot.linked_session_key = "slack:1700000000.000100"
            return True

        provider = self._live(change_effort=AsyncMock(side_effect=change_effort))
        state = _mock_state(slot, provider)
        state.sessions.aflush = AsyncMock(side_effect=[None, OSError("disk full")])

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": "high"}
            )
            body = await resp.json()

        assert resp.status == 503
        assert body["code"] == "effort_intent_unsaved"
        assert "rebound" in body["warning"]
        assert slot.reasoning_effort == "high"

    @pytest.mark.asyncio
    async def test_a_rebound_level_the_new_binding_cannot_save_is_refused(self):
        # A level clears any Default pending on the new binding, so the slot shows
        # it only once that is saved; the refusal first puts the session the push
        # reached back on Default with an automatic clear.
        slot = _ChatSlot("test")
        slot.reasoning_effort = ""

        async def change_effort(_effort):
            slot.linked_session_key = "slack:1700000000.000100"
            return True

        provider = self._live(
            change_effort=AsyncMock(side_effect=change_effort),
            clear_effort=AsyncMock(return_value=True),
        )
        state = _mock_state(slot, provider)
        state.sessions.aflush = AsyncMock(side_effect=OSError("disk full"))

        assert await self._pick(state, "high") == 503
        assert slot.reasoning_effort == ""
        provider.clear_effort.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_a_rebound_level_does_not_save_the_old_bindings_clear_twice(self):
        # The old binding's Default was cleared and saved before the push, so the
        # rebind writes only the new binding.
        slot = _ChatSlot("test")
        slot.reasoning_effort = ""

        async def change_effort(_effort):
            slot.linked_session_key = "slack:1700000000.000100"
            return True

        provider = self._live(change_effort=AsyncMock(side_effect=change_effort))
        state = _mock_state(slot, provider)
        old_key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[old_key] = True
        state.sessions.aflush = AsyncMock(side_effect=[None, None, OSError("disk full")])

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": "high"}
            )
            body = await resp.json()

        new_key = chat_handlers.effective_session_key(slot)
        assert resp.status == 200
        assert "warning" in body and not body["warning"].endswith(
            slot_effort._EFFORT_INTENT_NOT_SAVED
        )
        assert state.sessions.aflush.await_count == 2
        assert state.sessions.default_intents == {old_key: False, new_key: False}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["refused", "unsaved"])
    async def test_a_rebound_default_the_new_binding_cannot_keep_is_refused(self, failure):
        # Nothing reached the new binding, so the slot may show Default only once
        # that key's next cold start is sure to apply it; the refusal first puts
        # the session the clear reached back on the slot's level.
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"

        async def clear_effort(**_kwargs):
            slot.linked_session_key = "slack:1700000000.000100"
            return True

        provider = self._live(
            clear_effort=AsyncMock(side_effect=clear_effort),
            change_effort=AsyncMock(return_value=True),
        )
        state = _mock_state(slot, provider)
        old_key = chat_handlers.effective_session_key(slot)
        record = state.sessions.set_explicit_effort_default.side_effect
        if failure == "refused":

            def refuse_the_new_binding(key: str, pending: bool) -> bool:
                if key != old_key and pending:
                    return False
                return record(key, pending)

            state.sessions.set_explicit_effort_default.side_effect = refuse_the_new_binding
        else:
            state.sessions.aflush = AsyncMock(side_effect=OSError("disk full"))

        assert await self._pick(state, "") == 503
        new_key = chat_handlers.effective_session_key(slot)
        assert new_key != old_key
        assert slot.reasoning_effort == "high"
        assert state.sessions.default_intents.get(new_key, False) is False
        provider.change_effort.assert_awaited_once_with("high")

        # The slot still shows the level, so a retry runs the pick again instead
        # of stopping at the same-value check.
        state.sessions.set_explicit_effort_default.side_effect = record
        state.sessions.aflush = AsyncMock()
        assert await self._pick(state, "") == 200
        assert slot.reasoning_effort == ""
        assert provider.clear_effort.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("undo", ["refused", "raised"])
    async def test_a_rebound_pick_whose_push_cannot_be_undone_is_committed_and_refused(self, undo):
        # The session the clear reached cannot be put back, so the pick stands:
        # the slot shows what that session runs, and the answer says the new
        # binding's intent was not saved.
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"

        async def clear_effort(**_kwargs):
            slot.linked_session_key = "slack:1700000000.000100"
            return True

        change_effort = (
            AsyncMock(return_value=False)
            if undo == "refused"
            else AsyncMock(side_effect=RuntimeError("rpc failed"))
        )
        provider = self._live(
            clear_effort=AsyncMock(side_effect=clear_effort), change_effort=change_effort
        )
        state = _mock_state(slot, provider)
        state.sessions.aflush = AsyncMock(side_effect=OSError("disk full"))

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": ""}
            )
            body = await resp.json()

        assert resp.status == 503
        assert body["code"] == "effort_intent_unsaved"
        assert "rebound" in body["warning"]
        assert slot.reasoning_effort == ""
        change_effort.assert_awaited_once_with("high")

    @pytest.mark.asyncio
    async def test_a_rebound_pick_that_pushed_nothing_is_refused_without_an_undo(self, monkeypatch):
        # The model takes no effort, so no session received the pick and there is
        # nothing to put back before refusing.
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"

        def rebind(_effort):
            slot.linked_session_key = "slack:1700000000.000100"

        monkeypatch.setattr(chat_handlers, "_remember_reasoning_effort_for_restore", rebind)
        provider = self._live(supports_effort=MagicMock(return_value=False))
        state = _mock_state(slot, provider)
        state.sessions.aflush = AsyncMock(side_effect=OSError("disk full"))

        assert await self._pick(state, "") == 503
        assert slot.reasoning_effort == "high"
        provider.change_effort.assert_not_awaited()
        provider.clear_effort.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_cancelled_save_puts_the_prior_value_back(self):
        # A pick cancelled while its value was being saved committed nothing, so
        # its value must not reach the file with a later save.
        import asyncio

        import aiohttp

        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot)
        key = chat_handlers.effective_session_key(slot)
        saved: list[dict[str, bool]] = []

        async def aflush():
            saved.append(dict(state.sessions.default_intents))
            if len(saved) == 1:
                raise asyncio.CancelledError

        state.sessions.aflush = AsyncMock(side_effect=aflush)

        async with TestClient(TestServer(_make_app(state))) as client:
            try:
                resp = await _await_test(
                    client.post(
                        "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": ""}
                    ),
                    "the Default pick request",
                )
            except aiohttp.ClientError:
                resp = None
            assert resp is None or resp.status >= 500

        assert saved == [{key: True}, {key: False}]
        assert state.sessions.default_intents == {key: False}
        assert slot.reasoning_effort == "high"
        state.sessions.reset.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failed_save_puts_a_pending_intent_back_inside_its_hold(self):
        # The pick cleared the key's own pending Default before its save failed, so
        # it held that row first and puts the flag back into it before releasing.
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[key] = True
        state.sessions.aflush = AsyncMock(side_effect=OSError("disk full"))

        await self._pick(state, "high")

        assert state.sessions.default_intents[key] is True
        assert state.sessions.effort_events == [
            ("hold", key),
            ("set", key, False),
            ("set", key, True),
            ("release", key),
        ]

    @pytest.mark.asyncio
    async def test_a_rolled_back_reset_arms_the_default_again_inside_its_hold(self, monkeypatch):
        # The reset declined after the pick saved its cleared intent, so the row
        # the pick cleared stays held until the rollback has put the flag back.
        from kiro_crew.providers.base import LLMProvider

        monkeypatch.setattr(chat_handlers, "_switch_target_busy", lambda *_args: False)
        slot = _ChatSlot("test")
        slot.reasoning_effort = ""
        state = _mock_state(slot)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[key] = True
        busy = MagicMock(spec=LLMProvider)
        busy.has_active_turn.return_value = True
        state.sessions.get_provider = MagicMock(return_value=busy)
        state.sessions.reset = AsyncMock(return_value=False)

        assert await self._pick(state, "high") == 409
        assert state.sessions.default_intents == {key: True}
        assert state.sessions.effort_events == [
            ("hold", key),
            ("set", key, False),
            ("set", key, True),
            ("release", key),
        ]

    @pytest.mark.asyncio
    async def test_a_failed_save_before_anything_changed_refuses_the_pick(self, caplog):
        # The saved intent is what the next cold start acts on, so a pick whose
        # intent could not be saved is refused before the slot or the session
        # changes.
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.aflush = AsyncMock(side_effect=OSError("disk full"))

        assert await self._pick(state, "") == 503
        assert slot.reasoning_effort == "high"
        state.sessions.reset.assert_not_called()
        assert state.sessions.default_intents == {key: False}
        assert slot._session_effort_intent_gen == 0
        assert "Could not save explicit effort Default intent" in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "overrides",
        [
            {"has_active_turn": MagicMock(return_value=True)},
            {"supports_effort": MagicMock(return_value=False)},
        ],
        ids=["deferred", "model-without-effort"],
    )
    async def test_a_failed_save_on_a_live_session_that_was_not_pushed_refuses(self, overrides):
        provider = self._live(**overrides)
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot, provider)
        state.sessions.aflush = AsyncMock(side_effect=OSError("disk full"))

        assert await self._pick(state, "") == 503
        assert slot.reasoning_effort == "high"
        state.sessions.reset.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failed_save_after_a_live_push_commits_and_refuses(self):
        # The live session already runs the level, so the slot must show it; the
        # file keeps what the last saved pick left, and the answer says so.
        provider = self._live(change_effort=AsyncMock(return_value=True))
        slot = _ChatSlot("test")
        slot.reasoning_effort = ""
        state = _mock_state(slot, provider)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.aflush = AsyncMock(side_effect=OSError("disk full"))

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": "high"}
            )
            body = await resp.json()

        assert resp.status == 503
        assert body["code"] == "effort_intent_unsaved"
        assert body["reasoning_effort"] == "high"
        assert slot.reasoning_effort == "high"
        assert state.sessions.default_intents == {key: False}

    @pytest.mark.asyncio
    async def test_a_level_saves_the_cleared_default_before_its_live_push(self):
        # A later cold start that takes no level must not find the Default the
        # level replaced, so the clear is saved before the push and never again.
        slot = _ChatSlot("test")
        slot.reasoning_effort = ""
        seen_at_push = []

        async def change_effort(_effort):
            seen_at_push.append(
                (state.sessions.default_intents.get(key), state.sessions.aflush.await_count)
            )
            return True

        provider = self._live(change_effort=AsyncMock(side_effect=change_effort))
        state = _mock_state(slot, provider)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[key] = True

        assert await self._pick(state, "high") == 200
        assert seen_at_push == [(False, 1)]
        assert slot.reasoning_effort == "high"
        assert state.sessions.effort_events == [
            ("hold", key),
            ("set", key, False),
            ("release", key),
        ]

    @pytest.mark.asyncio
    async def test_a_level_whose_cleared_default_cannot_be_saved_is_refused_unpushed(self):
        provider = self._live(change_effort=AsyncMock(return_value=True))
        slot = _ChatSlot("test")
        slot.reasoning_effort = ""
        state = _mock_state(slot, provider)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[key] = True
        state.sessions.aflush = AsyncMock(side_effect=OSError("disk full"))

        assert await self._pick(state, "high") == 503
        provider.change_effort.assert_not_awaited()
        assert slot.reasoning_effort == ""
        assert state.sessions.default_intents == {key: True}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("push", ["refused", "raised"])
    async def test_a_level_push_that_does_not_end_on_the_level_puts_the_default_back(
        self, monkeypatch, push
    ):
        # The push did not land and the fallback then declined, so the pick
        # replaced nothing and the Default it cleared comes back.
        monkeypatch.setattr(chat_handlers, "_switch_target_busy", lambda *_args: True)
        change_effort = (
            AsyncMock(return_value=False)
            if push == "refused"
            else AsyncMock(side_effect=RuntimeError("rpc failed"))
        )
        provider = self._live(change_effort=change_effort)
        slot = _ChatSlot("test")
        slot.reasoning_effort = ""
        state = _mock_state(slot, provider)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[key] = True

        assert await self._pick(state, "high") == 409
        assert slot.reasoning_effort == ""
        assert state.sessions.default_intents == {key: True}
        assert state.sessions.effort_events == [
            ("hold", key),
            ("set", key, False),
            ("set", key, True),
            ("release", key),
        ]

    @pytest.mark.asyncio
    async def test_a_put_back_that_cannot_be_saved_keeps_the_default_in_memory(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(chat_handlers, "_switch_target_busy", lambda *_args: True)
        provider = self._live(change_effort=AsyncMock(return_value=False))
        slot = _ChatSlot("test")
        slot.reasoning_effort = ""
        state = _mock_state(slot, provider)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[key] = True
        state.sessions.aflush = AsyncMock(side_effect=[None, OSError("disk full")])

        assert await self._pick(state, "high") == 409
        # The map's next save writes it back to the file.
        assert state.sessions.default_intents == {key: True}
        assert "Could not save the explicit effort Default again" in caplog.text

    # -- a cold start in flight under the key owns its effort basis -------------
    #
    # The start reads the one-shot Default once and publishes a session built on
    # that read. A pick landing in between that would change the flag would
    # leave the slot showing a value the session does not run, so the handler
    # refuses it while the key's allocation reservation is held
    # (``SessionManager.effort_basis_locked``); a level pick on a key whose flag
    # is already clear changes no basis and lands.

    @staticmethod
    async def _pick_response(state: DashboardState, effort: str) -> tuple[int, dict]:
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": effort}
            )
            return resp.status, await resp.json()

    @staticmethod
    def _real_manager(monkeypatch, tmp_path):
        """A real ``SessionManager``, so ``effort_basis_locked`` folds keys as in production."""
        from kiro_crew import session as session_module

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "kirocrew_home"))
        cfg = MagicMock()
        cfg.session.pool_size = 0
        cfg.session.pool_agent = "kirocrew"
        cfg.session.pool_ttl_secs = 1800
        cfg.session.timeout_secs = 3600
        cfg.agent.default_agent = ""
        cfg.agent.model = "auto"
        with monkeypatch.context() as scoped:
            scoped.setattr(session_module, "default_project_dir", lambda: str(tmp_path))
            return session_module.SessionManager(cfg, provider_factory=MagicMock())

    @staticmethod
    def _hold_reservation(manager, key: str) -> None:
        # The token the allocation path adds before ``_get_or_create_impl`` and
        # removes after registration; held here for the whole request.
        manager._allocation_boundary()._allocation_reservations.setdefault(key, set()).add(object())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("effort", ["high", ""])
    @pytest.mark.parametrize("shape", ["no_session", "turn_in_flight"])
    async def test_a_pick_is_refused_while_the_keys_start_is_in_flight(self, effort, shape):
        # A level and a Default alike: the request changes nothing and answers
        # the retryable 409 the picker already has copy for.
        provider = (
            self._live(has_active_turn=MagicMock(return_value=True))
            if shape == "turn_in_flight"
            else None
        )
        slot = _ChatSlot("test")
        slot.reasoning_effort = "low"
        state = _mock_state(slot, provider)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[key] = effort != ""
        state.sessions.effort_basis_locked = MagicMock(side_effect=lambda k: k == key)

        status, body = await self._pick_response(state, effort)

        assert status == 409, body
        assert body["code"] == "turn_in_flight"
        assert slot.reasoning_effort == "low"
        assert state.sessions.default_intents == {key: effort != ""}
        state.sessions.set_explicit_effort_default.assert_not_called()
        state.sessions.aflush.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        if provider is not None:
            # Nothing reached the live session or the overlay file.
            provider.change_effort.assert_not_awaited()
            provider.clear_effort.assert_not_awaited()
        state.sessions.effort_basis_locked.assert_called_once_with(key)

    @pytest.mark.asyncio
    async def test_a_pending_default_pick_is_refused_during_a_live_turn(
        self, monkeypatch, tmp_path
    ):
        # The cold start can spend an existing Default from a caller-selected
        # level. Keep it pending until a retry can safely apply the pick.
        manager = self._real_manager(monkeypatch, tmp_path)
        provider = self._live(has_active_turn=MagicMock(return_value=True))
        slot = _ChatSlot("test")
        slot.reasoning_effort = "low"
        state = _mock_state(slot, provider)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[key] = True
        state.sessions.effort_basis_locked = manager.effort_basis_locked
        self._hold_reservation(manager, key)
        assert manager.effort_basis_locked(key) is True

        status, body = await self._pick_response(state, "")

        assert status == 409, body
        assert body["code"] == "turn_in_flight"
        assert slot.reasoning_effort == "low"
        assert state.sessions.default_intents == {key: True}
        state.sessions.set_explicit_effort_default.assert_not_called()
        state.sessions.effort_intent_write.assert_not_called()
        state.sessions.aflush.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        state.push_slots_update.assert_not_called()
        provider.change_effort.assert_not_awaited()
        provider.clear_effort.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_level_pick_with_nothing_to_record_is_deferred_during_a_live_turn(
        self, monkeypatch, tmp_path
    ):
        # A second claimant waits on the live session's lease for the whole turn
        # with the key's reservation held. The level changes no flag, so the
        # start that reservation guards reads the same basis either way: the
        # pick takes the deferred path instead of the start's 409.
        manager = self._real_manager(monkeypatch, tmp_path)
        provider = self._live(has_active_turn=MagicMock(return_value=True))
        slot = _ChatSlot("test")
        slot.reasoning_effort = "low"
        state = _mock_state(slot, provider)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.effort_basis_locked = manager.effort_basis_locked
        self._hold_reservation(manager, key)
        assert manager.effort_basis_locked(key) is True

        status, body = await self._pick_response(state, "high")

        assert status == 200, body
        assert body == {"ok": True, "reasoning_effort": "high", "deferred": True}
        assert slot.reasoning_effort == "high"
        assert state.sessions.default_intents == {}
        state.sessions.set_explicit_effort_default.assert_not_called()
        state.sessions.effort_intent_write.assert_not_called()
        state.sessions.aflush.assert_not_awaited()
        provider.change_effort.assert_not_awaited()
        provider.clear_effort.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("effort", "pending"), [("high", True), ("", False)], ids=["level", "default"]
    )
    async def test_a_pick_that_would_change_the_flag_is_refused_during_a_live_turn(
        self, monkeypatch, tmp_path, effort, pending
    ):
        # The same held reservation: a level that would clear a pending Default
        # and a Default that would arm one both change the basis the guarded
        # start reads, so each keeps the 409 and changes nothing.
        manager = self._real_manager(monkeypatch, tmp_path)
        provider = self._live(has_active_turn=MagicMock(return_value=True))
        slot = _ChatSlot("test")
        slot.reasoning_effort = "low"
        state = _mock_state(slot, provider)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[key] = pending
        state.sessions.effort_basis_locked = manager.effort_basis_locked
        self._hold_reservation(manager, key)

        status, body = await self._pick_response(state, effort)

        assert status == 409, body
        assert body["code"] == "turn_in_flight"
        assert slot.reasoning_effort == "low"
        assert state.sessions.default_intents == {key: pending}
        state.sessions.set_explicit_effort_default.assert_not_called()
        state.sessions.effort_intent_write.assert_not_called()
        state.sessions.aflush.assert_not_awaited()
        provider.change_effort.assert_not_awaited()
        provider.clear_effort.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_levels_clear_of_a_pending_default_is_refused_before_its_push(self):
        # The level would clear the Default before pushing live; with the key's
        # start in flight that clear is refused and the push never happens, so
        # the overlay file and the live session stay as they were.
        provider = self._live(change_effort=AsyncMock(return_value=True))
        slot = _ChatSlot("test")
        slot.reasoning_effort = ""
        state = _mock_state(slot, provider)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[key] = True
        state.sessions.effort_basis_locked = MagicMock(return_value=True)

        status, body = await self._pick_response(state, "high")

        assert status == 409, body
        assert body["code"] == "turn_in_flight"
        assert slot.reasoning_effort == ""
        assert state.sessions.default_intents == {key: True}
        provider.change_effort.assert_not_awaited()
        assert state.sessions.effort_events == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("effort", ["high", ""])
    async def test_the_retry_lands_once_the_start_has_registered(self, effort):
        # Each pick would change the flag (the level clears a pending Default,
        # the Default arms one), so the start in flight refuses it.
        slot = _ChatSlot("test")
        slot.reasoning_effort = "low"
        state = _mock_state(slot)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[key] = effort != ""
        in_flight = True
        state.sessions.effort_basis_locked = MagicMock(side_effect=lambda _k: in_flight)

        assert await self._pick(state, effort) == 409
        assert slot.reasoning_effort == "low"
        assert state.sessions.default_intents == {key: effort != ""}

        in_flight = False

        assert await self._pick(state, effort) == 200
        assert slot.reasoning_effort == effort
        # What the key's next cold start reads is the pick that landed.
        assert state.sessions.default_intents == {key: effort == ""}

    @pytest.mark.asyncio
    async def test_a_start_under_another_key_does_not_refuse_the_pick(self, monkeypatch, tmp_path):
        manager = self._real_manager(monkeypatch, tmp_path)
        slot = _ChatSlot("test")
        slot.reasoning_effort = "low"
        state = _mock_state(slot)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.effort_basis_locked = manager.effort_basis_locked
        self._hold_reservation(manager, "dashboard:another")

        assert await self._pick(state, "") == 200
        assert slot.reasoning_effort == ""
        assert state.sessions.default_intents == {key: True}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("slot_key", "reserved_key"),
        [
            ("slack:1700000000.000100", "1700000000.000100"),
            ("1700000000.000100", "slack:1700000000.000100"),
        ],
    )
    async def test_a_start_under_a_folded_alias_of_the_key_refuses_the_pick(
        self, monkeypatch, tmp_path, slot_key, reserved_key
    ):
        # The legacy bare thread_ts and its ``slack:`` spelling name one session,
        # so a start reserved under either spelling owns the basis of both.
        manager = self._real_manager(monkeypatch, tmp_path)
        slot = _ChatSlot("test")
        slot.reasoning_effort = "low"
        slot.linked_session_key = slot_key
        state = _mock_state(slot)
        assert chat_handlers.effective_session_key(slot) == slot_key
        state.sessions.effort_basis_locked = manager.effort_basis_locked
        self._hold_reservation(manager, reserved_key)

        status, body = await self._pick_response(state, "")

        assert status == 409, body
        assert body["code"] == "turn_in_flight"
        assert slot.reasoning_effort == "low"
        assert state.sessions.default_intents == {}

    @pytest.mark.asyncio
    async def test_the_put_back_still_arms_the_default_while_a_start_is_in_flight(
        self, monkeypatch
    ):
        # The level cleared the Default before a start began under the key, and
        # its push then did not land. The put-back restores a Default this same
        # request cleared: a start that read the cleared value arms nothing, and
        # the NEXT cold start applies the flag, so the write is not refused.
        monkeypatch.setattr(chat_handlers, "_switch_target_busy", lambda *_args: True)
        locked: list[bool] = []

        async def change_effort(_level: str) -> bool:
            # The start begins once the clear is saved and the push is in flight.
            locked.append(True)
            return False

        provider = self._live(change_effort=AsyncMock(side_effect=change_effort))
        slot = _ChatSlot("test")
        slot.reasoning_effort = ""
        state = _mock_state(slot, provider)
        key = chat_handlers.effective_session_key(slot)
        state.sessions.default_intents[key] = True
        state.sessions.effort_basis_locked = MagicMock(side_effect=lambda _k: bool(locked))

        assert await self._pick(state, "high") == 409
        assert slot.reasoning_effort == ""
        assert state.sessions.default_intents == {key: True}
        assert state.sessions.effort_events == [
            ("hold", key),
            ("set", key, False),
            ("set", key, True),
            ("release", key),
        ]
        # The clear was checked; the put-back is exempt and was not.
        state.sessions.effort_basis_locked.assert_called_once_with(key)

    # -- a pick saving its flag when a start begins is settled before the read --
    #
    # The check passes before the start takes the key's reservation, so the
    # pick's in-memory flag is already written when the start begins and its
    # save is still in flight. The handler counts the write from that flag
    # through the save and its put-back (``effort_intent_write``), and the start
    # waits for the count to settle before its basis read.

    async def _a_default_pick_saving_as_a_start_begins(
        self, manager, state: DashboardState, key: str, outcome: str
    ) -> tuple[int | None, list[tuple[bool, bool]]]:
        """Race a Default pick's save against a cold start that begins during it.

        The start is modelled as the allocation makes it: it takes the key's
        reservation while the save is in flight, waits for the key's intent
        writes, then reads the flag once. ``outcome`` is what the pick's save
        does once the start is waiting: "saved", "raised" or "cancelled".
        Returns the pick's status (None when the connection dropped) and the
        start's read as ``(value, save_settled)``.
        """
        import asyncio

        import aiohttp

        save_started = asyncio.Event()
        save_release = asyncio.Event()
        start_waiting = asyncio.Event()
        save_settled = False
        flushes = 0
        reads: list[tuple[bool, bool]] = []

        async def aflush() -> None:
            nonlocal flushes, save_settled
            flushes += 1
            if flushes > 1:
                # The put-back's save after a cancelled one.
                return
            save_started.set()
            await _await_test(save_release.wait(), "save_release")
            save_settled = True
            if outcome == "raised":
                raise OSError("disk full")
            if outcome == "cancelled":
                raise asyncio.CancelledError

        async def cold_start() -> None:
            await _await_test(save_started.wait(), "save_started")
            self._hold_reservation(manager, key)
            start_waiting.set()
            await manager.wait_for_effort_intent_writes(key)
            reads.append((state.sessions.default_intents.get(key, False), save_settled))

        state.sessions.aflush = AsyncMock(side_effect=aflush)
        state.sessions.effort_basis_locked = manager.effort_basis_locked
        state.sessions.effort_intent_write = manager.effort_intent_write
        start = asyncio.create_task(cold_start())
        request: asyncio.Task | None = None
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                request = asyncio.create_task(
                    client.post(
                        "/api/chat/slots/test/reasoning-effort",
                        json={"reasoning_effort": ""},
                    )
                )
                await _await_test(start_waiting.wait(), "cold start reaching its basis read")
                save_release.set()
                try:
                    resp = await _await_test(request, "the Default pick request")
                except aiohttp.ClientError:
                    resp = None
                await _await_test(start, "cold start completion")
        finally:
            save_release.set()
            participants = [start]
            if request is not None:
                participants.append(request)
            for participant in participants:
                if not participant.done():
                    participant.cancel()
            await _await_test(
                asyncio.gather(*participants, return_exceptions=True),
                "readiness-race cleanup",
            )
        return (None if resp is None else resp.status), reads

    @pytest.mark.asyncio
    async def test_a_default_whose_save_raises_under_a_start_is_read_as_put_back(
        self, monkeypatch, tmp_path
    ):
        manager = self._real_manager(monkeypatch, tmp_path)
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot)
        key = chat_handlers.effective_session_key(slot)

        status, reads = await self._a_default_pick_saving_as_a_start_begins(
            manager, state, key, "raised"
        )

        # The pick is refused and put the prior value back; the start read that
        # value, after the save had settled, so it arms nothing.
        assert status == 503
        assert reads == [(False, True)]
        assert state.sessions.default_intents == {key: False}
        assert slot.reasoning_effort == "high"
        assert key not in manager._effort_intent_writes

    @pytest.mark.asyncio
    async def test_a_default_whose_save_lands_under_a_start_is_read_once_saved(
        self, monkeypatch, tmp_path
    ):
        manager = self._real_manager(monkeypatch, tmp_path)
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot)
        key = chat_handlers.effective_session_key(slot)

        status, reads = await self._a_default_pick_saving_as_a_start_begins(
            manager, state, key, "saved"
        )

        # The start read the pick's value only once its save had settled.
        assert status == 200
        assert reads == [(True, True)]
        assert state.sessions.default_intents == {key: True}
        assert slot.reasoning_effort == ""
        assert key not in manager._effort_intent_writes

    @pytest.mark.asyncio
    async def test_a_default_whose_save_is_cancelled_under_a_start_is_read_as_put_back(
        self, monkeypatch, tmp_path
    ):
        manager = self._real_manager(monkeypatch, tmp_path)
        slot = _ChatSlot("test")
        slot.reasoning_effort = "high"
        state = _mock_state(slot)
        key = chat_handlers.effective_session_key(slot)

        status, reads = await self._a_default_pick_saving_as_a_start_begins(
            manager, state, key, "cancelled"
        )

        # The cancelled pick committed nothing and put the prior value back
        # before its write exited; that is what the start read.
        assert status is None or status >= 500
        assert reads == [(False, True)]
        assert state.sessions.default_intents == {key: False}
        assert slot.reasoning_effort == "high"
        state.sessions.reset.assert_not_called()
        assert key not in manager._effort_intent_writes


class TestEffortIntentFailureMatrix:
    """No answer reports success after a failed intent write.

    Asserted over every exit family rather than per path: a 2xx needs every
    intent write the request made to land, and whatever the answer, the slot
    shows the level the live session was last left on or, with nothing pushed,
    the old value or the pick a later start applies.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("before", "requested", "live", "rebind", "undo_lands", "failing_flush"),
        [
            pytest.param("", "high", "idle", False, None, None, id="live-level-saved"),
            pytest.param("", "high", "idle", False, None, "every", id="live-level-unsaved"),
            pytest.param("high", "", "idle", False, None, "every", id="live-default-unsaved"),
            pytest.param("", "high", "idle", True, None, "second", id="rebound-old-unsaved"),
            pytest.param("high", "", "idle", True, True, "every", id="rebound-new-unsaved-undone"),
            pytest.param("high", "", "idle", True, False, "every", id="rebound-new-unsaved-kept"),
            pytest.param("", "high", "busy", False, None, "every", id="deferred-unsaved"),
            pytest.param("high", "", "stopped", False, None, "every", id="stopped-unsaved"),
            pytest.param(
                "high", "", "no-effort", False, None, "every", id="model-without-effort-unsaved"
            ),
        ],
    )
    async def test_the_answer_never_publishes_an_unsaved_intent(
        self, before, requested, live, rebind, undo_lands, failing_flush
    ):
        slot = _ChatSlot("test")
        slot.reasoning_effort = before
        running: list[str] = []

        async def push(level: str) -> bool:
            if running and undo_lands is not None:
                # A second push is the undo of the first.
                if undo_lands:
                    running.append(level)
                return undo_lands
            running.append(level)
            if rebind:
                slot.linked_session_key = "slack:1700000000.000100"
            return True

        async def change_effort(level: str) -> bool:
            return await push(level)

        async def clear_effort(**_kwargs: object) -> bool:
            return await push("")

        provider = None
        if live != "stopped":
            provider = TestExplicitDefaultIntent._live(
                change_effort=AsyncMock(side_effect=change_effort),
                clear_effort=AsyncMock(side_effect=clear_effort),
                supports_effort=MagicMock(return_value=live != "no-effort"),
                has_active_turn=MagicMock(return_value=live == "busy"),
            )
        state = _mock_state(slot, provider)
        flushes = 0
        failed_flushes = 0

        async def flush() -> None:
            nonlocal flushes, failed_flushes
            flushes += 1
            if failing_flush == "every" or (failing_flush == "second" and flushes == 2):
                failed_flushes += 1
                raise OSError("disk full")

        state.sessions.aflush = AsyncMock(side_effect=flush)

        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post(
                "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": requested}
            )
            body = await response.json()

        if failed_flushes:
            assert response.status == 503, body
        if response.status < 300:
            assert slot.reasoning_effort == requested
        if running:
            assert slot.reasoning_effort == running[-1]
        else:
            assert slot.reasoning_effort in (before, requested)
        if body.get("code") == "effort_intent_unavailable":
            assert slot.reasoning_effort == before
        if body.get("code") == "effort_intent_unsaved":
            assert body["reasoning_effort"] == slot.reasoning_effort
            assert slot._effort_intent_owed is True


class TestEffortIntentRetry:
    @pytest.mark.asyncio
    async def test_the_same_pick_retries_an_owed_intent_write(self):
        slot = _ChatSlot("test")
        provider = TestExplicitDefaultIntent._live(change_effort=AsyncMock(return_value=True))
        state = _mock_state(slot, provider)
        state.sessions.aflush = AsyncMock(side_effect=OSError("disk full"))

        assert await TestExplicitDefaultIntent._pick(state, "high") == 503
        assert slot._effort_intent_owed is True
        state.sessions.aflush = AsyncMock()
        assert await TestExplicitDefaultIntent._pick(state, "high") == 200
        assert slot._effort_intent_owed is False
        assert await TestExplicitDefaultIntent._pick(state, "high") == 200
        assert provider.change_effort.await_count == 2

    @pytest.mark.asyncio
    async def test_the_same_pick_after_a_saved_intent_is_a_no_op(self):
        slot = _ChatSlot("test")
        provider = TestExplicitDefaultIntent._live(change_effort=AsyncMock(return_value=True))
        state = _mock_state(slot, provider)

        assert await TestExplicitDefaultIntent._pick(state, "high") == 200
        assert slot._effort_intent_owed is False
        assert await TestExplicitDefaultIntent._pick(state, "high") == 200
        provider.change_effort.assert_awaited_once_with("high")

    @pytest.mark.asyncio
    async def test_a_refused_intent_write_marks_the_slot_owed(self):
        slot = _ChatSlot("test")
        provider = TestExplicitDefaultIntent._live(change_effort=AsyncMock(return_value=True))
        state = _mock_state(slot, provider)
        state.sessions.set_explicit_effort_default = MagicMock(return_value=False)

        assert await TestExplicitDefaultIntent._pick(state, "high") == 503
        assert slot._effort_intent_owed is True

    @pytest.mark.asyncio
    async def test_owed_flag_does_not_survive_slot_persistence_round_trip(self, tmp_path):
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("test")
        slot._effort_intent_owed = True
        slot.append("user", "persist me")
        assert await chat_persistence.save_slot_off_loop(state, slot, force=True)

        restored_state = _make_state(tmp_path)
        restored = chat_persistence._rehydrate_slot_from_history(restored_state, "test")
        assert restored is not None
        assert restored._effort_intent_owed is False
