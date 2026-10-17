"""Dashboard chat: refusal fallback swap and restore of the slot's primary model."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from test_dashboard_chat_refusal import TestRunChatRefusalFallback as _RefusalFallbackSuite

from kiro_crew.dashboard.recovery_replays import ReplayFamily
from kiro_crew.testing.wait import await_until


class TestRunChatRefusalFallbackSwap:
    """TestRunChatRefusalFallback: the swap, its unwinding, and the primary's restore."""

    _make_state_for_run_chat = staticmethod(_RefusalFallbackSuite._make_state_for_run_chat)
    _make_refusing_client = staticmethod(_RefusalFallbackSuite._make_refusing_client)
    _refusal_events = staticmethod(_RefusalFallbackSuite._refusal_events)

    @pytest.mark.asyncio
    async def test_replay_turn_does_not_pin_unpinned_slot(self, tmp_path, monkeypatch):
        """The replay turn runs with the candidate active while
        ``_active_fallback_model`` is empty — the slot.model backfill must not
        persist the temporary candidate into the (durable) pin."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        # Simulate the real provider contract the backfill documents: the
        # resolved model readable off the client IS the currently served one.
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._backfill_canonical_model",
            lambda client, provider_name: getattr(client, "served_model", "") or "",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        assert not slot.model, "precondition: slot starts unpinned"
        client = self._make_refusing_client(self._refusal_events(category="CYBER"))
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")
        # Turn 1 backfilled the PRIMARY (legitimate). Clear it to model the
        # documented CC case — the canonical id is unresolvable until a later
        # turn — so the replay turn runs against a genuinely unpinned slot.
        slot.model = ""
        assert slot.task is not None
        await slot.task  # the replay turn — candidate active, throttle state empty

        assert (
            not slot.model
        ), f"replay turn persisted the temporary candidate into slot.model: {slot.model!r}"

    @pytest.mark.asyncio
    async def test_synthetic_recovery_does_not_restore(self, tmp_path, monkeypatch):
        """A synthetic recovery turn after the replay keeps the fallback
        active; only a genuine turn restores the primary."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(self._refusal_events(category="CYBER"))
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")
        assert slot.task is not None
        await slot.task
        assert slot._refusal_fallback_primary == "fable-5"

        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat_utils import _SYNTHETIC_RECOVERY_MSGS
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        async def _ok_stream(msg):
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _ok_stream
        client.stream_command = _ok_stream

        # A runner-authored continuation must finish on the model that
        # produced the work it continues — no restore.
        await _run_chat(state, slot, _SYNTHETIC_RECOVERY_MSGS[0])
        assert slot._refusal_fallback_primary == "fable-5"
        assert [c.args[0] for c in client.set_model.await_args_list] == ["opus-test"]

        # The next genuine turn restores.
        await _run_chat(state, slot, "a fresh question")
        assert slot._refusal_fallback_primary == ""
        assert [c.args[0] for c in client.set_model.await_args_list] == [
            "opus-test",
            "fable-5",
        ]

    @pytest.mark.asyncio
    async def test_restore_keeps_record_on_silent_noop(self, tmp_path, monkeypatch):
        """A non-raising ``set_model`` that does not move the model must NOT
        clear the restore record — the next turn retries instead of stranding
        the session on the fallback with nothing left to restore from."""
        from kiro_crew.dashboard.chat_runner import _restore_refusal_fallback

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client([])
        client.served_model = "opus-test"  # fallback currently active
        slot._refusal_fallback_primary = "fable-5"
        slot._refusal_fallback_candidate = "opus-test"
        client.set_model = AsyncMock()  # silent no-op: served_model unchanged

        await _restore_refusal_fallback(slot, client)
        client.set_model.assert_awaited_once_with("fable-5")
        assert (
            slot._refusal_fallback_primary == "fable-5"
        ), "restore record cleared on a silent set_model no-op"

        # A working set_model on the next attempt restores and clears.
        def _apply(mid: str) -> None:
            client.served_model = mid

        client.set_model = AsyncMock(side_effect=_apply)
        await _restore_refusal_fallback(slot, client)
        assert slot._refusal_fallback_primary == ""
        assert client.served_model == "fable-5"

    @pytest.mark.asyncio
    async def test_explicit_pick_after_retry_wins_over_restore(self, tmp_path, monkeypatch):
        """An explicit model pick landing between the replay and the next
        genuine turn drops the restore record without touching the model —
        even a pick of the fallback candidate itself, which current-model
        equality alone cannot distinguish from the automatic swap."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(self._refusal_events(category="CYBER"))
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")
        assert slot.task is not None
        await slot.task
        assert slot._refusal_fallback_primary == "fable-5"

        # An explicit pick moves the generation (api_chat_slot_model's seam).
        slot._model_pick_gen += 1

        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        async def _ok_stream(msg):
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _ok_stream
        client.stream_command = _ok_stream
        await _run_chat(state, slot, "a fresh question")

        # No restore call — only the original swap — and the record is gone.
        assert [c.args[0] for c in client.set_model.await_args_list] == ["opus-test"]
        assert slot._refusal_fallback_primary == ""
        assert slot._refusal_fallback_candidate == ""

    @pytest.mark.asyncio
    async def test_no_retry_when_follow_up_queued(self, tmp_path, monkeypatch):
        """A queued follow-up is the user's next intent — often a correction
        of the refused message. The replay inserts at queue index 0, so it
        would run BEFORE that correction; the retry must refuse instead and
        leave the queue untouched."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(self._refusal_events(category="CYBER"))
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        slot.queue_append("actually, drop that request")

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        client.set_model.assert_not_awaited()
        assert slot._refusal_fallback_attempted is False
        assert slot._refusal_fallback_primary == ""
        cards = [
            i
            for i, m in enumerate(slot.messages)
            if m.get("role") == "error"
            and "Response declined by the model." in m.get("content", "")
        ]
        assert len(cards) == 1, "the terminal card must render when the retry is refused"
        # No replay was recorded or announced — the follow-up owns the next
        # turn instead of being jumped by a re-send of the refused message.
        assert not slot.replays.armed(ReplayFamily.CONTENT_FILTER)
        assert not any("retrying once on" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_swap_unwound_when_follow_up_arrives_during_swap(self, tmp_path, monkeypatch):
        """A follow-up landing while set_model is in flight changes intent
        under the swap: the swap is unwound (witnessed restore), the refusal
        surfaces, and nothing is queued ahead of the follow-up."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(self._refusal_events(category="CYBER"))

        def _apply_and_interrupt(mid: str) -> None:
            client.served_model = mid
            if mid == "opus-test":
                slot.queue_append("wait — different plan")

        client.set_model = AsyncMock(side_effect=_apply_and_interrupt)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # Swap then unwind: candidate first, primary back, record cleared.
        assert [c.args[0] for c in client.set_model.await_args_list] == ["opus-test", "fable-5"]
        assert client.served_model == "fable-5"
        assert slot._refusal_fallback_primary == ""
        assert slot._refusal_fallback_attempted is False
        assert any(
            m.get("role") == "error" and "Response declined by the model." in m.get("content", "")
            for m in slot.messages
        )
        # No replay was recorded or announced ahead of the follow-up.
        assert not slot.replays.armed(ReplayFamily.CONTENT_FILTER)
        assert not any("retrying once on" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_swap_unwound_when_stop_completes_during_swap(self, tmp_path, monkeypatch):
        """A Stop that presses AND resolves back to idle while set_model is in
        flight leaves ``_stop_state == "idle"``, so the state check alone
        misses it. The monotonic stop generation still moved: the swap must
        unwind and nothing may be queued — otherwise the stopped request
        replays, and the enqueue snapshot would bake the moved counter in so
        the drain purge could never catch it either."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(self._refusal_events(category="CYBER"))

        def _apply_and_stop(mid: str) -> None:
            client.served_model = mid
            if mid == "opus-test":
                # Stop pressed and fully resolved during the swap await:
                # the counter moved, the state is back to idle.
                slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1
                slot._stop_state = "idle"

        client.set_model = AsyncMock(side_effect=_apply_and_stop)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # Swap then unwind: candidate first, primary back, record cleared.
        assert [c.args[0] for c in client.set_model.await_args_list] == ["opus-test", "fable-5"]
        assert client.served_model == "fable-5"
        assert slot._refusal_fallback_primary == ""
        assert slot._refusal_fallback_attempted is False
        # No replay recorded or announced: the stopped request must not rerun.
        assert not slot.replays.armed(ReplayFamily.CONTENT_FILTER)
        assert not any("retrying once on" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_chained_swap_preserves_original_primary(self, tmp_path, monkeypatch):
        """A second swap while the restore record is still live must NOT
        overwrite the recorded primary. After a failed/no-op restore the
        session is stranded on fallback A with the record still naming the
        TRUE primary P; a second refusal's swap A->B reads A as "active" —
        recording A would lose P permanently (restore would return to A,
        never to P)."""
        from kiro_crew.dashboard.chat_runner import (
            _refusal_fallback_swap,
            _restore_refusal_fallback,
        )

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client([])
        # Stranded state: restore to fable-5 failed last turn, record kept.
        client.served_model = "opus-test"
        slot._refusal_fallback_primary = "fable-5"
        slot._refusal_fallback_candidate = "opus-test"

        def _apply(mid: str) -> None:
            client.served_model = mid

        client.set_model = AsyncMock(side_effect=_apply)

        # Second refusal on the fallback: auto recommends a different model.
        returned = await _refusal_fallback_swap(slot, client, "sonnet-test")

        assert returned == "opus-test", "the notice names the model that refused"
        assert client.served_model == "sonnet-test"
        assert (
            slot._refusal_fallback_primary == "fable-5"
        ), "chained swap overwrote the true primary with the stale fallback"
        assert slot._refusal_fallback_candidate == "sonnet-test"

        # The restore returns the session to the TRUE primary directly.
        await _restore_refusal_fallback(slot, client)
        assert client.served_model == "fable-5"
        assert slot._refusal_fallback_primary == ""

    @pytest.mark.asyncio
    async def test_swap_skipped_when_auto_primary_has_no_provable_restore(
        self, tmp_path, monkeypatch
    ):
        """Unpinned session (the "auto" sentinel): the restore leg must
        set_model("auto"), which partitions that do not advertise the
        sentinel refuse — the restore then fails every turn and the session
        stays stranded on the candidate. The swap must not run when the
        return leg is unprovable."""
        from kiro_crew.dashboard.chat_runner import _refusal_fallback_swap

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client([])
        client.served_model = "auto"  # provider_active_model() -> ""
        client.available_models = lambda: [
            {"modelId": "fable-5"},
            {"modelId": "opus-test"},
        ]  # no "auto" advertised: the restore target is unprovable

        set_model = AsyncMock()
        client.set_model = set_model

        returned = await _refusal_fallback_swap(slot, client, "opus-test")

        assert returned is None, "swap must be skipped when the restore leg is unprovable"
        set_model.assert_not_awaited()
        assert getattr(slot, "_refusal_fallback_primary", "") in ("", None)

    @pytest.mark.asyncio
    async def test_swap_proceeds_when_auto_is_advertised(self, tmp_path, monkeypatch):
        """When the backend advertises the "auto" sentinel as a target the
        round trip is provable: the swap runs and records "auto" as the
        primary for the restore."""
        from kiro_crew.dashboard.chat_runner import _refusal_fallback_swap

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client([])
        client.served_model = "auto"
        client.available_models = lambda: [
            {"modelId": "auto"},
            {"modelId": "opus-test"},
        ]

        def _apply(mid: str) -> None:
            client.served_model = mid

        client.set_model = AsyncMock(side_effect=_apply)

        returned = await _refusal_fallback_swap(slot, client, "opus-test")

        assert returned == "auto"
        assert client.served_model == "opus-test"
        assert slot._refusal_fallback_primary == "auto"
        assert slot._refusal_fallback_candidate == "opus-test"

    @pytest.mark.asyncio
    async def test_orchestration_queue_entry_does_not_suppress_retry(self, tmp_path, monkeypatch):
        """A queued cron notification is orchestration, not a user correction:
        it must not suppress the configured retry. Only USER-authored queue
        entries express superseding intent (`_has_user_queued_followup`)."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(self._refusal_events(category="CYBER"))
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat_utils import CRON_NOTIFICATION_KIND

        slot.queue_append(
            '[Cron notification from "scanner"]\nno new items', kind=CRON_NOTIFICATION_KIND
        )

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # The retry proceeded despite the queued orchestration entry.
        client.set_model.assert_awaited_once_with("opus-test")
        assert slot._refusal_fallback_attempted is True
        assert any(
            "retrying once on 'opus-test'" in m.get("content", "")
            for m in slot.messages
            if m.get("role") == "error"
        ), "retry notice missing — the cron entry suppressed the retry"

    @pytest.mark.asyncio
    async def test_unattended_actor_keeps_terminal_refusal(self, tmp_path, monkeypatch):
        """A cron/autonudge/sub-agent wake keeps the terminal refusal: nobody
        attends the announced swap, and an unattended replay doubles whatever
        the wake was about. The gate reads the turn's ledger actor."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(self._refusal_events(category="CYBER"))
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "nightly scan wake", _turn_actor="subagent")

        client.set_model.assert_not_awaited()
        assert slot._refusal_fallback_attempted is False
        assert slot._refusal_fallback_primary == ""
        assert not slot.replays.armed(ReplayFamily.CONTENT_FILTER)
        assert any(
            m.get("role") == "error" and "Response declined by the model." in m.get("content", "")
            for m in slot.messages
        ), "the unattended turn must surface the ordinary terminal card"
        assert not any("retrying once on" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_a_loops_refused_cycle_records_the_loop_on_the_slot(self, tmp_path, monkeypatch):
        """A loop's own cycle that ends refused names that loop on the slot.

        The fire guard reads the pair before the next cycle and stops the loop
        rather than sending the same prompt to the same model again. Recorded
        with a fallback configured too, because the fallback never runs for an
        unattended cycle, so the card is terminal either way.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._configured_refusal_fallback",
            lambda: "opus-test",
        )
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(self._refusal_events(category="CYBER"))
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(
            state,
            slot,
            "patrol wake",
            _directive_self_wake=True,
            _directive_loop_id="loop-42",
            _directive_loop_gen=7,
        )

        client.set_model.assert_not_awaited()
        assert slot._last_turn_model_refused_loop_id == "loop-42"
        assert slot._last_turn_model_refused_loop_gen == 7

    @pytest.mark.asyncio
    async def test_a_refused_person_turn_names_no_loop(self, tmp_path, monkeypatch):
        """A person's refused turn must not stop a loop that shares the slot."""
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client(self._refusal_events(category="CYBER"))
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert slot._last_turn_model_refused_loop_id == ""
        assert slot._last_turn_model_refused_loop_gen == 0

    @pytest.mark.asyncio
    async def test_a_genuine_new_turn_clears_the_refused_cycle(self, tmp_path, monkeypatch):
        """A person's next message is a new context, so the loop may fire again."""
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot._last_turn_model_refused_loop_id = "loop-42"
        slot._last_turn_model_refused_loop_gen = 7
        client = self._make_refusing_client(
            [
                LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
                LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "try a different angle")

        assert slot._last_turn_model_refused_loop_id == ""
        assert slot._last_turn_model_refused_loop_gen == 0

    @pytest.mark.asyncio
    async def test_throttle_walk_divergence_redirects_walk_restore_target(
        self, tmp_path, monkeypatch
    ):
        """A throttle walk advancing OFF the refusal candidate mid-retry must
        not cost the true primary: the walk's restore target is rewritten to
        the refusal primary and the refusal record hands over, with the wire
        model left on the walk's live choice."""
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client([])
        client.served_model = "haiku-test"  # the walk's live choice
        slot._refusal_fallback_primary = "fable-5"
        slot._refusal_fallback_candidate = "opus-test"
        slot._refusal_pick_gen = slot._model_pick_gen
        slot._active_fallback_model = "haiku-test"
        slot._fallback_primary_model = "opus-test"  # walk departed FROM our candidate

        from kiro_crew.dashboard.chat_runner import _restore_refusal_fallback

        await _restore_refusal_fallback(slot, client)

        assert (
            slot._fallback_primary_model == "fable-5"
        ), "walk restore target must become the refusal primary"
        assert slot._refusal_fallback_primary == ""
        assert slot._refusal_fallback_candidate == ""
        client.set_model.assert_not_awaited()
        assert client.served_model == "haiku-test", "the walk's live model must not be touched"

    @pytest.mark.asyncio
    async def test_alias_pick_epoch_blocks_restore(self, tmp_path, monkeypatch):
        """An explicit pick through a session ALIAS bumps the shared client's
        pick epoch without touching this slot's generation; the restore must
        drop its record without moving the model — otherwise it silently
        overrides the pick the user made through the other slot. Cross-layer
        on purpose: the pick handler stamps the INNER wrapped client while
        this restore holds the provider WRAPPER, so both ends must resolve
        the same epoch host."""
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client([])
        client.served_model = "opus-test"  # still on the candidate

        class _InnerClient:
            """Non-callable inner wrapped client, as pick_epoch_host resolves."""

        inner = _InnerClient()
        client.client = inner  # the wrapper's .client, like AcpProvider.client
        slot._refusal_fallback_primary = "fable-5"
        slot._refusal_fallback_candidate = "opus-test"
        slot._refusal_pick_gen = slot._model_pick_gen  # THIS slot saw no pick
        slot._refusal_client_pick_epoch = 0
        # The alias's live-switch pick stamps the INNER client (the handler's
        # side of pick_epoch_host) — even a pick of exactly the candidate,
        # which model equality alone cannot see.
        inner._explicit_pick_epoch = 1

        from kiro_crew.dashboard.chat_runner import _restore_refusal_fallback

        await _restore_refusal_fallback(slot, client)

        client.set_model.assert_not_awaited()
        assert client.served_model == "opus-test", "the alias's pick must stand"
        assert slot._refusal_fallback_primary == ""
        assert slot._refusal_fallback_candidate == ""

    @pytest.mark.asyncio
    async def test_pick_during_restore_set_model_await_is_not_overwritten(
        self, tmp_path, monkeypatch
    ):
        """A pick landing INSIDE the restore's ``set_model`` await must win.

        The epoch snapshot is read once before the await, so without
        serialization an alias pick that lands while the restore's RPC is in
        flight is applied first and then silently overwritten when the
        restore's ``set_model(primary)`` completes LAST. The restore now
        holds the session-scoped switch lock across the whole
        check-and-restore, so the pick (which takes the same lock) is
        strictly ordered after it and its choice stands.
        """
        import asyncio as _asyncio

        from kiro_crew.dashboard.chat_runner import _restore_refusal_fallback
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.llm_helpers import pick_epoch_host, slot_switch_session_lock

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client([])
        client.served_model = "opus-test"  # wire is on the candidate

        # Gate set_model so the RPC "lands" only on release — modelling the
        # in-flight window the finding names: the model applies when the
        # await COMPLETES, so an unserialized concurrent pick applies first
        # and the restore's write lands last.
        restore_entered = _asyncio.Event()
        release_restore = _asyncio.Event()

        async def _gated_set_model(model: str) -> None:
            restore_entered.set()
            await release_restore.wait()
            client.served_model = model

        client.set_model = AsyncMock(side_effect=_gated_set_model)

        slot._refusal_fallback_primary = "fable-5"
        slot._refusal_fallback_candidate = "opus-test"
        slot._refusal_pick_gen = slot._model_pick_gen
        slot._refusal_client_pick_epoch = 0  # epoch unmoved when restore starts

        restore_task = _asyncio.create_task(_restore_refusal_fallback(slot, client))
        await await_until(restore_entered, what="the restore reaching set_model")

        async def _alias_pick() -> None:
            # The pick handler's essential moves, under the SAME session
            # lock it takes at its acquisition site: bump the shared epoch,
            # apply the model on the wire.
            async with slot_switch_session_lock(effective_session_key(slot)):
                host = pick_epoch_host(client)
                host._explicit_pick_epoch = getattr(host, "_explicit_pick_epoch", 0) + 1
                client.served_model = "user-picked"

        pick_task = _asyncio.create_task(_alias_pick())
        for _ in range(10):  # let the pick reach the lock (or, unfixed, run)
            await _asyncio.sleep(0)
        release_restore.set()
        await await_until(restore_task, what="the restore finishing")
        await await_until(pick_task, what="the aliased pick finishing")

        client.set_model.assert_awaited_once_with("fable-5")
        assert client.served_model == "user-picked", (
            "the pick that landed during the restore's set_model await must "
            "apply last — the restore overwrote the user's selection"
        )

    @pytest.mark.asyncio
    async def test_pick_during_swap_set_model_await_is_not_reverted(self, tmp_path, monkeypatch):
        """A pick landing INSIDE the swap's ``set_model`` await must survive.

        The swap snapshots the client pick epoch AFTER its await, so without
        the session lock an alias pick landing while the swap's RPC is in
        flight is folded into the snapshot — the restore's alias-pick guard
        then reads equal and restores the primary over the user's choice.
        The swap now holds the session-scoped switch lock across the await
        and the snapshot (same order as the restore), so the pick is
        strictly ordered after the snapshot and the restore drops its
        record instead of touching the model.
        """
        import asyncio as _asyncio

        from kiro_crew.dashboard.chat_runner import (
            _refusal_fallback_swap,
            _restore_refusal_fallback,
        )
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.llm_helpers import pick_epoch_host, slot_switch_session_lock

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_refusing_client([])
        client.served_model = "fable-5"  # wire on the primary pre-swap

        swap_entered = _asyncio.Event()
        release_swap = _asyncio.Event()

        async def _gated_set_model(model: str) -> None:
            swap_entered.set()
            await release_swap.wait()
            client.served_model = model

        client.set_model = AsyncMock(side_effect=_gated_set_model)

        swap_task = _asyncio.create_task(_refusal_fallback_swap(slot, client, "opus-test"))
        await await_until(swap_entered, what="the swap reaching set_model")

        async def _alias_pick() -> None:
            # The pick handler's essential moves, under the SAME session
            # lock it takes at its acquisition site: bump the shared epoch,
            # apply the model on the wire.
            async with slot_switch_session_lock(effective_session_key(slot)):
                host = pick_epoch_host(client)
                host._explicit_pick_epoch = getattr(host, "_explicit_pick_epoch", 0) + 1
                client.served_model = "user-picked"

        pick_task = _asyncio.create_task(_alias_pick())
        for _ in range(10):  # let the pick reach the lock (or, unfixed, run)
            await _asyncio.sleep(0)
        release_swap.set()
        assert await await_until(swap_task, what="the swap finishing") == "fable-5"
        await await_until(pick_task, what="the aliased pick finishing")

        # The retry ran; the next genuine turn restores. The record must be
        # dropped on the epoch mismatch without touching the model.
        client.set_model = AsyncMock()
        await _restore_refusal_fallback(slot, client)

        client.set_model.assert_not_awaited()
        assert client.served_model == "user-picked", (
            "the pick that landed during the swap's set_model await must "
            "stand — the restore reverted the user's selection"
        )
        assert slot._refusal_fallback_primary == ""
