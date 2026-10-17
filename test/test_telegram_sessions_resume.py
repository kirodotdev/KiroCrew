"""Telegram inbound resume: a dashboard session continued from a Telegram chat.

Inbound routing to a resumed session and every hand-off outcome, the model picker and
approvals on a resumed session, cold resume, the dashboard-state injection and the
capability, a restricted (incognito or temporary) resumed session, and the refusals a
resumed session gets, byte for byte. The session picker and private-chat topic routing
are in ``test_telegram_sessions.py``; the shared doubles are in
``telegram_sessions_helpers.py``.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from chat_test_helpers import _make_state
from telegram_sessions_helpers import _reset_shared_trust  # noqa: F401
from telegram_sessions_helpers import (
    _callback,
    _Client,
    _dispatcher,
    _dm,
    _log,
    _picker_button,
    _topic,
)
from test_telegram import _origin

from kiro_crew.dashboard.channel_handoff import (
    HANDOFF_QUEUED,
    HANDOFF_REFUSED,
    HANDOFF_STEERED,
    QUEUED_BY_CLOSE,
    RAN_ON_SUCCESSOR,
    REFUSED_ATTACHMENTS,
    REFUSED_IDLE,
    REFUSED_MOVED,
    REFUSED_NO_SLOT,
    REFUSED_QUEUE_FULL,
    REFUSED_UNSAVED_CLOSE,
    ResumedBusyOutcome,
)
from kiro_crew.messaging import auto_title
from kiro_crew.messaging.renderer import session_provenance_tag
from kiro_crew.messaging.session_resume import RoutingDecision
from kiro_crew.telegram.renderer import TelegramApprovalDecider
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES
from kiro_crew.telegram.transport_dispatch import TelegramDispatcher
from kiro_crew.testing.wait import await_until


async def _bind(
    dispatcher: TelegramDispatcher,
    client: _Client,
) -> None:
    await dispatcher.handle_message(_dm("/sessions"))
    data, message_id = _picker_button(client)
    await dispatcher.on_callback(_callback(data, message_id=message_id))


class TestTelegramInboundResumeRouting:
    @pytest.mark.asyncio
    async def test_ordinary_message_takes_one_routing_decision(self, tmp_path: Any) -> None:
        dispatcher, _, sessions, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )

        await dispatcher.handle_message(_dm("continue here"))

        dispatcher._session_resume.route.assert_awaited_once_with(7, 7, "private", None)
        assert sessions.last_key == "dashboard:chat-1"

    @pytest.mark.asyncio
    async def test_multi_user_binding_is_refused_before_it_can_route(self, tmp_path: Any) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path, allowed={7, 8})
        link = dispatcher._session_resume.link_for(7, None)
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)

        await dispatcher.handle_message(_dm("must stay private"))

        assert sessions.last_key == ""
        assert any("exactly one" in text for text, _ in client.sent)
        assert any("NOT processed" in text for text, _ in client.sent)

    @pytest.mark.parametrize(
        ("command", "handler", "target_kw"),
        [
            ("/compact", "_handle_compact", "session_key"),
            ("/stop", "_handle_stop", "session_key"),
            ("/model", "_handle_model", "session_key"),
            ("/title renamed", "_handle_title", "session_key"),
            ("/spawn do work", "_handle_spawn", "session_key"),
            ("/task run spec.md", "_handle_task", "session_key"),
            ("/link", "_handle_link", "resumed_key"),
        ],
    )
    @pytest.mark.asyncio
    async def test_session_scoped_commands_receive_resumed_key(
        self, tmp_path: Any, command: str, handler: str, target_kw: str
    ) -> None:
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        mocked = AsyncMock()
        setattr(dispatcher, handler, mocked)

        await dispatcher.handle_message(_dm(command))

        mocked.assert_awaited_once()
        assert mocked.await_args.kwargs[target_kw] == "dashboard:chat-1"

    @pytest.mark.parametrize("command", ["/new", "/unlink"])
    @pytest.mark.asyncio
    async def test_recovery_command_releases_binding_and_next_message_runs_once(
        self, tmp_path: Any, command: str
    ) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        await _bind(dispatcher, client)
        link = dispatcher._session_resume.link_for(7, None)
        assert sessions.find_mirror_sessions(link, inbound_only=True) == ["dashboard:chat-1"]

        await dispatcher.handle_message(_dm(command))

        assert sessions.find_mirror_sessions(link, inbound_only=True) == []
        assert any("resumed session" in text for text, _ in client.sent)
        sent_before = len(client.sent)
        await dispatcher.handle_message(_dm("continue natively"))
        assert sessions.last_key != "dashboard:chat-1"
        assert len(client.sent) > sent_before
        assert not any("NOT processed" in text for text, _ in client.sent[sent_before:])

    @pytest.mark.parametrize("command", ["/help", "/status", "/new", "/unlink"])
    @pytest.mark.asyncio
    async def test_commands_remain_native_and_recovery_commands_bypass_refusals(
        self, tmp_path: Any, command: str
    ) -> None:
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(refusal="must not run")
        )

        await dispatcher.handle_message(_dm(command))

        dispatcher._session_resume.route.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_refusal_delivery_leaves_settlement_owed(self, tmp_path: Any) -> None:
        dispatcher, client, _, _ = _dispatcher(tmp_path)
        decision = RoutingDecision(refusal="Detached")
        dispatcher._session_resume.route = AsyncMock(return_value=decision)
        dispatcher._session_resume.settle = AsyncMock()
        client.send_fails = True

        await dispatcher.handle_message(_dm("continue"))

        dispatcher._session_resume.settle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_refusal_delivery_settles(self, tmp_path: Any) -> None:
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        decision = RoutingDecision(refusal="Detached")
        dispatcher._session_resume.route = AsyncMock(return_value=decision)
        dispatcher._session_resume.settle = AsyncMock()

        await dispatcher.handle_message(_dm("continue"))

        dispatcher._session_resume.settle.assert_awaited_once_with(7, None, decision)

    @pytest.mark.asyncio
    async def test_topic_routing_locks_are_isolated(self, tmp_path: Any) -> None:
        dispatcher, client, _, _ = _dispatcher(tmp_path)
        topic_12_routed = asyncio.Event()
        crossed: list[bool] = []

        async def _route(
            user_id: int,
            chat_id: int,
            chat_type: str,
            thread: int | None,
        ) -> RoutingDecision:
            assert user_id == 7 and chat_type == "supergroup"
            if thread == 12:
                topic_12_routed.set()
            return RoutingDecision(refusal=f"Detached {thread}")

        real_send = client.send_message

        async def _send(chat_id: int, text: str, **kwargs: Any) -> int | None:
            if text == "Detached 11":
                try:
                    await await_until(topic_12_routed, timeout=1)
                    crossed.append(True)
                except AssertionError:  # timed out: topic 12 not routed within the window
                    crossed.append(False)
            return await real_send(chat_id, text, **kwargs)

        dispatcher._session_resume.route = _route  # type: ignore[method-assign]
        client.send_message = _send  # type: ignore[method-assign]

        # flake-ok: pre-existing gather with no timeout of the two topics' handle_message calls, moved verbatim; tracked for a de-flake pass
        await asyncio.gather(
            dispatcher.handle_message(_topic("one", 11)),
            dispatcher.handle_message(_topic("two", 12)),
        )

        assert crossed == [True]

    @pytest.mark.asyncio
    async def test_same_route_waiters_settle_only_after_the_last_refusal(
        self, tmp_path: Any
    ) -> None:
        dispatcher, client, _, _ = _dispatcher(tmp_path)
        decision = RoutingDecision(refusal="Detached")
        dispatcher._session_resume.route = AsyncMock(return_value=decision)
        settlements: list[int] = []

        async def _settle(*args: Any) -> None:
            settlements.append(1)

        dispatcher._session_resume.settle = _settle  # type: ignore[method-assign]
        real_send = client.send_message
        first_started = asyncio.Event()
        second_started = asyncio.Event()

        async def _send(chat_id: int, text: str, **kwargs: Any) -> int | None:
            if text == "Detached" and not first_started.is_set():
                first_started.set()
                await await_until(second_started)
            return await real_send(chat_id, text, **kwargs)

        client.send_message = _send  # type: ignore[method-assign]
        first = asyncio.create_task(dispatcher.handle_message(_dm("first")))
        # flake-ok: pre-existing Event wait with no timeout for the first message's refusal send to start, moved verbatim; tracked for a de-flake pass
        await first_started.wait()
        second = asyncio.create_task(dispatcher.handle_message(_dm("second")))
        while len(dispatcher._routing_locks["chat:7"][1]) < 2:
            # flake-ok: pre-existing one-yield pacing of an uncapped poll until two messages wait on the chat:7 routing lock, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)
        second_started.set()
        # flake-ok: pre-existing gather with no timeout of the two handle_message tasks the gate releases, moved verbatim; tracked for a de-flake pass
        await asyncio.gather(first, second)

        assert settlements == [1]

    @pytest.mark.asyncio
    async def test_busy_resumed_session_hands_off_to_a_running_dashboard_turn(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The dashboard holds the resumed session's turn: the message goes to the slot's
        own machinery (steer or queue there), never to this chat's queue, and the chat is
        told. Base refused it ("busy with a turn started elsewhere")."""
        from kiro_crew.dashboard import channel_handoff

        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        sessions.busy = True
        handed: list[tuple[str, str, str, bool, dict[str, Any]]] = []

        async def _hand(state, session_key, text, *, mode, has_attachments, **recipient):
            handed.append((session_key, text, mode, has_attachments, recipient))
            return channel_handoff.ResumedBusyOutcome(channel_handoff.HANDOFF_QUEUED)

        monkeypatch.setattr(channel_handoff, "hand_to_resumed_slot", _hand)

        await dispatcher.handle_message(_dm("and the weather?"))

        # The chat rides along so a later drop notice can be addressed to it, with
        # the user this dispatcher admitted.
        assert handed == [
            (
                "dashboard:chat-1",
                "and the weather?",
                "steer",
                False,
                {"channel_type": "telegram", "conversation_id": "7", "principal": "7"},
            )
        ]
        assert sessions.provider.steered == []
        assert sessions.queued == []
        assert sessions.last_key == ""
        assert any("Queued for that session" in text for text, _ in client.sent)

    @pytest.mark.asyncio
    async def test_a_queued_command_reaches_the_hand_off_as_its_bare_words(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """``/queue /clear`` loses its directive before the hand-off and skips this
        dispatcher's command intercept, so the words "/clear" alone reach the slot's
        queue -- where the hand-off stamps them as turn content, never as the
        dashboard command."""
        from kiro_crew.dashboard import channel_handoff

        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        sessions.busy = True
        handed: list[tuple[str, str]] = []

        async def _hand(state, session_key, text, *, mode, has_attachments, **recipient):
            handed.append((text, mode))
            return channel_handoff.ResumedBusyOutcome(channel_handoff.HANDOFF_QUEUED)

        monkeypatch.setattr(channel_handoff, "hand_to_resumed_slot", _hand)

        await dispatcher.handle_message(_dm("/queue /clear"))

        assert handed == [("/clear", "queue")]
        assert sessions.queued == []

    @pytest.mark.parametrize(
        ("outcome", "told"),
        [
            (ResumedBusyOutcome(HANDOFF_STEERED), "Steering that session"),
            (ResumedBusyOutcome(HANDOFF_QUEUED), "Queued for that session"),
            (
                ResumedBusyOutcome(HANDOFF_QUEUED, QUEUED_BY_CLOSE),
                "closed while your message was in flight",
            ),
            (ResumedBusyOutcome(HANDOFF_QUEUED, RAN_ON_SUCCESSOR), "Delivered to that session"),
            (
                ResumedBusyOutcome(HANDOFF_REFUSED, REFUSED_IDLE),
                "busy with a turn started elsewhere",
            ),
            (ResumedBusyOutcome(HANDOFF_REFUSED, REFUSED_QUEUE_FULL), "queue is full"),
            (ResumedBusyOutcome(HANDOFF_REFUSED, REFUSED_ATTACHMENTS), "attachments cannot wait"),
            (
                ResumedBusyOutcome(HANDOFF_REFUSED, REFUSED_MOVED),
                "changed while your message was in flight",
            ),
            (
                ResumedBusyOutcome(HANDOFF_REFUSED, REFUSED_UNSAVED_CLOSE),
                "had not been saved with it yet",
            ),
        ],
        ids=lambda v: v if isinstance(v, str) else f"{v.kind}:{v.reason or '-'}",
    )
    @pytest.mark.asyncio
    async def test_every_hand_off_outcome_is_told_to_the_chat(
        self, tmp_path: Any, monkeypatch: Any, outcome: ResumedBusyOutcome, told: str
    ) -> None:
        from kiro_crew.dashboard import channel_handoff

        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        sessions.busy = True

        async def _hand(*a: Any, **k: Any) -> ResumedBusyOutcome:
            return outcome

        monkeypatch.setattr(channel_handoff, "hand_to_resumed_slot", _hand)

        await dispatcher.handle_message(_dm("and the weather?"))

        assert [text for text, _ in client.sent if told in text], client.sent
        assert sessions.queued == [] and sessions.last_key == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", [REFUSED_IDLE, REFUSED_NO_SLOT])
    @pytest.mark.parametrize("unlinked_meanwhile", [False, True], ids=["bound", "unlinked"])
    async def test_a_free_lease_with_no_dashboard_turn_runs_as_a_fresh_turn(
        self, tmp_path: Any, monkeypatch: Any, reason: str, unlinked_meanwhile: bool
    ) -> None:
        """The turn ended between this dispatcher's busy check and the hand-off's own
        read (an idle slot, or no open tab): nothing holds the lease any more, so
        the message runs instead of being refused. A lease still held (the chat's
        own turn on the resumed key) keeps the refusal -- the REFUSED_IDLE row of
        the table above.

        The fresh turn runs under the key resolved at admission, never through a
        second binding resolution: updates are concurrent tasks and ``/unlink`` is
        exempt from the busy gate, so a binding that changed in the window
        (``unlinked``) must not reroute the message into the native session."""
        from kiro_crew.dashboard import channel_handoff

        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        route = AsyncMock(return_value=RoutingDecision(resumed_key="dashboard:chat-1"))
        dispatcher._session_resume.route = route
        sessions.busy = True

        async def _hand(*a: Any, **k: Any) -> ResumedBusyOutcome:
            sessions.busy = False  # the turn ended while the hand-off was scheduled
            if unlinked_meanwhile:
                route.return_value = RoutingDecision()  # a concurrent /unlink landed
            return ResumedBusyOutcome(HANDOFF_REFUSED, reason)

        monkeypatch.setattr(channel_handoff, "hand_to_resumed_slot", _hand)

        await dispatcher.handle_message(_dm("and the weather?"))

        assert sessions.last_key == "dashboard:chat-1", "the message did not run there"
        route.assert_awaited_once()
        assert not any("busy with a turn started elsewhere" in text for text, _ in client.sent)

    @pytest.mark.asyncio
    async def test_queue_drain_keeps_native_affinity_after_a_later_bind(
        self, tmp_path: Any
    ) -> None:
        dispatcher, _, sessions, _ = _dispatcher(tmp_path)
        native_key = dispatcher._session_key(("direct", "7"))
        sessions.queued.append(("1", "queued text", _origin()))
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )

        await dispatcher._drain_queue(native_key)

        dispatcher._session_resume.route.assert_not_awaited()
        assert sessions.last_key == native_key

    @pytest.mark.asyncio
    async def test_option_redispatch_routes_once_to_the_retained_resumed_key(
        self, tmp_path: Any
    ) -> None:
        dispatcher, _, sessions, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        tag = session_provenance_tag("dashboard:chat-1")

        await dispatcher.on_callback(_callback(f"opt:0:{tag}", label="Ship it"))

        dispatcher._session_resume.route.assert_awaited_once_with(7, 7, "private", None)
        assert sessions.last_key == "dashboard:chat-1"


class TestTelegramModelPickerRouting:
    @pytest.mark.asyncio
    async def test_unlink_makes_resumed_model_picker_stale(self, tmp_path: Any) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        sessions.provider.advertised = [{"modelId": "gpt-test", "name": "Test model"}]
        link = dispatcher._session_resume.link_for(7, None)
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)

        await dispatcher.handle_message(_dm("/model"))
        model_message_id = client._mid
        await dispatcher.handle_message(_dm("/unlink"))
        await dispatcher.on_callback(_callback("m:1", message_id=model_message_id))

        assert sessions.provider.set_models == []
        assert "no longer controls" in client.edits[-1][1]


class TestTelegramApprovalResumeRouting:
    async def _arm(self, key: str, request_id: str = "rq1") -> asyncio.Future[bool]:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        token = TelegramApprovalDecider.key(key, request_id)
        TelegramApprovalDecider._REGISTRY[token] = future
        TelegramApprovalDecider.arm(token, "nonce")
        return future

    @pytest.mark.asyncio
    async def test_approval_uses_the_current_resumed_key(self, tmp_path: Any) -> None:
        dispatcher, _, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        future = await self._arm("dashboard:chat-1")
        try:
            await dispatcher.on_callback(_callback("a:rq1:nonce:1"))
            assert future.done() and future.result() is True
        finally:
            TelegramApprovalDecider._REGISTRY.clear()

    @pytest.mark.asyncio
    async def test_moved_binding_expires_old_trust_nonce_without_mutation(
        self, tmp_path: Any
    ) -> None:
        dispatcher, _, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        old = await self._arm("dashboard:chat-1")
        sessions.clear_mirror_link("dashboard:chat-1")
        sessions.set_mirror_link("dashboard:chat-2", link, accepts_inbound=True)
        try:
            await dispatcher.on_callback(_callback("a:rq1:nonce:t"))
            assert not old.done()
            assert sessions.approval_policies == []
        finally:
            TelegramApprovalDecider._REGISTRY.clear()

    @pytest.mark.asyncio
    async def test_ambiguous_binding_never_falls_back_to_native_approval(
        self, tmp_path: Any
    ) -> None:
        dispatcher, _, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        for key in ("dashboard:chat-1", "dashboard:chat-2"):
            sessions.mirror_links[key] = link
            sessions.inbound_keys.add(key)
        native = dispatcher._session_key(("direct", "7"))
        future = await self._arm(native)
        try:
            await dispatcher.on_callback(_callback("a:rq1:nonce:1"))
            assert not future.done()
        finally:
            TelegramApprovalDecider._REGISTRY.clear()


class TestTelegramColdResume:
    @pytest.mark.parametrize(
        "recorded, expected",
        [
            pytest.param("research-agent", "research-agent", id="recorded"),
            pytest.param("default", "kirocrew", id="default-sentinel"),
            pytest.param("Auto", "kirocrew", id="auto-sentinel"),
            pytest.param("", "kirocrew", id="absent"),
        ],
    )
    @pytest.mark.asyncio
    async def test_persisted_agent_policy_is_used_consistently(
        self, tmp_path: Any, recorded: str, expected: str
    ) -> None:
        log = _log(tmp_path, agent=recorded)
        dispatcher, _, sessions, _ = _dispatcher(tmp_path, log=log)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        captured: list[str | None] = []
        real_persist = dispatcher._persist_turn

        def _persist(*args: Any, **kwargs: Any) -> None:
            captured.append(kwargs.get("agent", args[5] if len(args) > 5 else None))
            real_persist(*args, **kwargs)

        dispatcher._persist_turn = _persist  # type: ignore[method-assign]
        await dispatcher.handle_message(_dm("continue"))

        assert sessions.last_agent == expected
        assert dispatcher.ctx_builder.build_calls[-1]["agent"] == expected
        assert captured == [expected]

    @pytest.mark.asyncio
    async def test_persisted_agent_metadata_read_runs_off_loop(self, tmp_path: Any) -> None:
        log = _log(tmp_path, agent="research-agent")
        dispatcher, _, _, _ = _dispatcher(tmp_path, log=log)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        loop_thread = threading.get_ident()
        reads: list[int] = []
        real_get = log.get_metadata

        def _get(key: str) -> dict[str, Any]:
            reads.append(threading.get_ident())
            return real_get(key)

        log.get_metadata = _get  # type: ignore[method-assign]
        await dispatcher.handle_message(_dm("continue"))

        assert reads and all(thread_id != loop_thread for thread_id in reads)

    @pytest.mark.asyncio
    async def test_unreadable_agent_metadata_falls_back_to_route_agent(self, tmp_path: Any) -> None:
        log = _log(tmp_path)
        dispatcher, _, sessions, _ = _dispatcher(tmp_path, log=log)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )

        def _broken(key: str) -> dict[str, Any]:
            raise OSError("unreadable")

        log.get_metadata = _broken  # type: ignore[method-assign]
        await dispatcher.handle_message(_dm("continue"))

        assert sessions.last_agent == "kirocrew"

    @pytest.mark.asyncio
    async def test_cold_resume_suppresses_every_native_only_side_effect(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        from kiro_crew.dashboard import channel_slots

        log = _log(tmp_path, agent="research-agent")
        dispatcher, _, sessions, _ = _dispatcher(tmp_path, log=log)
        sessions.is_new_result = True
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        surface = AsyncMock()
        monkeypatch.setattr(channel_slots, "surface_dispatcher_session", surface)
        title_claims: list[str] = []
        monkeypatch.setattr(auto_title, "try_claim", lambda key: title_claims.append(key) or True)
        before = log.get_metadata("dashboard:chat-1").get("title")

        await dispatcher.handle_message(_dm("continue"))

        assert sessions.set_channel_calls == []
        assert sessions.origin_links == {}
        assert log.get_metadata("dashboard:chat-1").get("title") == before
        assert title_claims == []
        surface.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resumed_turn_explicitly_broadcasts_channel_user_row(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        from kiro_crew.dashboard import channel_slots

        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        broadcasts: list[bool] = []

        def _project(*_args: Any, **kwargs: Any) -> None:
            broadcasts.append(bool(kwargs.get("broadcast_user")))

        monkeypatch.setattr(channel_slots, "project_channel_turn_live", _project)

        await dispatcher.handle_message(_dm("continue"))

        assert broadcasts == [True]


class TestTelegramResumeIntegration:
    def test_dashboard_state_injection_reaches_the_resume_controller(self, tmp_path: Any) -> None:
        dispatcher, client, _, _ = _dispatcher(tmp_path)
        state = _make_state(tmp_path)
        transport = SimpleNamespace(channel_type="telegram", dispatcher=dispatcher, client=client)

        state.register_channel_transport(transport)

        assert dispatcher.dashboard_state is state
        assert dispatcher._session_resume.dashboard_state is state

    def test_link_spelling_matches_the_existing_mirror_path(self, tmp_path: Any) -> None:
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        route = ("forum", "-100:11")

        assert dispatcher._session_resume.link_for(-100, 11) == dispatcher._origin_mirror_link(
            route, -100
        )

    def test_capability_exposes_inbound_resume(self) -> None:
        assert TELEGRAM_CAPABILITIES.supports_session_resume is True

    def test_dashboard_resume_target_requires_one_owner_dm(self) -> None:
        from kiro_crew.telegram.transport import TelegramTransport

        owner = TelegramTransport(_Client(), allowed_user_ids={7})
        assert owner.may_resume_from("7", None) is True
        assert owner.may_resume_from("8", None) is False
        # A threaded link never resumes a dashboard session: the one caller
        # (chat_mirror) only ever passes a threadless Telegram target, so inbound
        # resume stays scoped to the sole owner's threadless General DM.
        assert owner.may_resume_from("7", "11") is False

        shared = TelegramTransport(_Client(), allowed_user_ids={7, 8})
        assert shared.may_resume_from("7", None) is False
        assert shared.may_resume_from("8", None) is False
        assert shared.may_resume_from("7", "11") is False


class TestTelegramRestrictedResumedSession:
    """An incognito or temporary DASHBOARD session resumed here writes no transcript.

    The exposure this pins is specific to inbound resume: before Telegram could
    resume, the persist path only ever saw a Telegram-native key, for which
    ``privacy_mode.is_restricted`` is the right predicate. A resumed turn carries a
    ``dashboard:`` key instead, and that predicate reads a process-local tracker a
    dashboard slot never populates — so on its own it answers False for an
    incognito session and the turn would land in durable history.
    """

    def _state(self, slot: Any) -> Any:
        """Minimal dashboard state: only ``get_slot`` and ``sessions`` are read."""
        return SimpleNamespace(sessions=None, get_slot=lambda name: slot)

    @pytest.mark.asyncio
    async def test_restricted_live_slot_is_reported_restricted(self, tmp_path: Any) -> None:
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher.dashboard_state = self._state(SimpleNamespace(is_restricted=True))

        assert await dispatcher._session_restricted("dashboard:chat-1") is True

    @pytest.mark.asyncio
    async def test_unrestricted_live_slot_still_persists(self, tmp_path: Any) -> None:
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher.dashboard_state = self._state(SimpleNamespace(is_restricted=False))

        assert await dispatcher._session_restricted("dashboard:chat-1") is False

    @pytest.mark.asyncio
    async def test_channel_privacy_mode_alone_would_have_failed_open(self, tmp_path: Any) -> None:
        """The regression anchor: the native predicate cannot see this restriction."""
        from kiro_crew.messaging import privacy_mode

        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher.dashboard_state = self._state(SimpleNamespace(is_restricted=True))

        # What the writer-side gate would have concluded on its own.
        assert privacy_mode.is_restricted("dashboard:chat-1") is False
        # What the caller-side ceiling concludes instead.
        assert await dispatcher._session_restricted("dashboard:chat-1") is True

    @pytest.mark.asyncio
    async def test_a_closed_tab_keeps_recording_unless_the_mode_says_otherwise(
        self, tmp_path: Any
    ) -> None:
        """No live slot and NO transcript: recording must continue.

        The one unknown history allows, and it is the cold-resume case: nothing on
        disk claims the session is restricted. An unknown with a transcript present
        is a different case and denies — see the ambiguous-record test.
        """
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher.dashboard_state = self._state(None)

        assert await dispatcher._session_restricted("dashboard:never-existed") is False

    @pytest.mark.asyncio
    async def test_an_affirmative_incognito_marker_still_denies(self, tmp_path: Any) -> None:
        """The closed-tab rung that DOES restrict: the transcript says incognito."""
        from kiro_crew.messaging import upload_gate

        assert (
            upload_gate._persisted_mode_is_restricted(
                "dashboard:gone", lambda name: (True, "incognito"), False
            )
            is True
        )
        assert (
            upload_gate._persisted_mode_is_restricted(
                "dashboard:gone", lambda name: (True, "persistent"), False
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_an_existing_but_unreadable_record_denies_history(self, tmp_path: Any) -> None:
        """An ambiguous or corrupt transcript is where an incognito session hides.

        ``_probe_persisted_session`` reports ``(True, None)`` when one stem matches
        several transcripts, so taking the write would risk persisting a session
        that promised to leave nothing. A legacy header with no ``memory_mode``
        does NOT land here — it reads ``persistent`` — so denying costs no ordinary
        history.
        """
        from kiro_crew.messaging import upload_gate

        assert (
            upload_gate._persisted_mode_is_restricted(
                "dashboard:ambiguous", lambda name: (True, None), False
            )
            is True
        )
        # Truly absent is the one unknown history still allows.
        assert (
            upload_gate._persisted_mode_is_restricted(
                "dashboard:absent", lambda name: (False, None), False
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_the_upload_ceiling_keeps_denying_an_unknown_mode(self, tmp_path: Any) -> None:
        """The two postures stay independent: uploads deny unknown either way."""
        from kiro_crew.messaging import upload_gate

        for probe_result in ((False, None), (True, None)):
            assert (
                upload_gate._persisted_mode_is_restricted(
                    "dashboard:gone", lambda name, r=probe_result: r
                )
                is True
            )

    @pytest.mark.asyncio
    async def test_a_temporary_resumed_session_reads_no_memory(self, tmp_path: Any) -> None:
        """The write gate does not cover the INBOUND half.

        ``temporary`` blocks memory reads, and for a resumed ``dashboard:`` key that
        fact lives on the slot. Left to the channel tracker, stored memories and
        lessons would enter the model prompt for a session that asked for none.
        """
        from kiro_crew.messaging import privacy_mode

        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher.dashboard_state = self._state(
            SimpleNamespace(is_restricted=True, blocks_reads=True)
        )

        # What the channel-local predicate would have concluded on its own.
        assert privacy_mode.is_temporary("dashboard:chat-1") is False
        assert await dispatcher._blocks_memory_reads("dashboard:chat-1") is True

    @pytest.mark.asyncio
    async def test_an_incognito_resumed_session_still_reads(self, tmp_path: Any) -> None:
        """The documented difference between the two modes survives the fix."""
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher.dashboard_state = self._state(
            SimpleNamespace(is_restricted=True, blocks_reads=False)
        )

        assert await dispatcher._session_restricted("dashboard:chat-1") is True
        assert await dispatcher._blocks_memory_reads("dashboard:chat-1") is False

    @pytest.mark.asyncio
    async def test_the_read_gate_reaches_the_prompt_builder(self, tmp_path: Any) -> None:
        """End to end: the gate's answer is the value the prompt builder receives."""
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        dispatcher.dashboard_state = self._state(
            SimpleNamespace(is_restricted=False, blocks_reads=True)
        )

        await dispatcher.handle_message(_dm("what did we decide?"))

        assert dispatcher.ctx_builder.build_calls[-1]["blocks_reads"] is True

    @pytest.mark.asyncio
    async def test_a_native_telegram_key_is_unaffected(self, tmp_path: Any) -> None:
        """A key with no dashboard slot falls back to this channel's own mode."""
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher.dashboard_state = self._state(SimpleNamespace(is_restricted=True))

        assert await dispatcher._session_restricted("telegram:direct:7") is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        ["/temporary", "/incognito private question"],
    )
    async def test_privacy_modifier_is_refused_while_resumed(
        self, tmp_path: Any, command: str
    ) -> None:
        """A channel-local mark cannot promise privacy for a persistent live slot."""
        from kiro_crew.messaging import privacy_mode

        privacy_mode.reset()
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )

        await dispatcher.handle_message(_dm(command))

        assert not privacy_mode.is_restricted("dashboard:chat-1")
        assert sessions.last_key == "", "the command's message body must not reach the model"
        assert "NOT processed" in client.sent[-1][0]
        assert "/unlink" in client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_restricted_turn_is_neither_projected_nor_persisted(self, tmp_path: Any) -> None:
        """Skipping only the direct append is insufficient: projection dirties the slot."""
        dispatcher, _, sessions, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        persist_calls: list[str] = []
        dispatcher._persist_turn = (  # type: ignore[method-assign]
            lambda *args, **kwargs: persist_calls.append(str(args[0]))
        )

        class _RestrictedSlot:
            is_restricted = True

            def __init__(self) -> None:
                self.messages: list[dict[str, Any]] = []

            def append(self, role: str, content: str, cls: str = "", **kw: Any) -> dict[str, Any]:
                row = {"role": role, "content": content, "meta": {"mid": "m1"}}
                self.messages.append(row)
                return row

        slot = _RestrictedSlot()
        dispatcher.dashboard_state = SimpleNamespace(
            sessions=None,
            get_slot=lambda name: slot,
            push_slots_update=lambda: pytest.fail("restricted projection pushed the slot"),
        )

        await dispatcher.handle_message(_dm("private continuation"))

        assert sessions.last_key == "dashboard:chat-1"
        assert slot.messages == []
        assert persist_calls == []

    @pytest.mark.asyncio
    async def test_restricted_resumed_title_writes_nothing(self, tmp_path: Any) -> None:
        """The dashboard-aware gate applies to /title, not only ordinary turns."""
        dispatcher, client, _, _ = _dispatcher(tmp_path)
        titled: list[tuple[Any, ...]] = []
        dispatcher.conv_log = SimpleNamespace(  # type: ignore[assignment]
            set_title=lambda *args: titled.append(args)
        )
        dispatcher.dashboard_state = self._state(SimpleNamespace(is_restricted=True))

        await dispatcher._handle_title(
            ("direct", "7"),
            7,
            "Private project",
            session_key="dashboard:chat-1",
        )

        assert titled == []
        assert "private" in client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_unrestricted_resumed_title_updates_live_slot_and_disk(
        self, tmp_path: Any
    ) -> None:
        """A metadata-only rename is stale data: the live slot later writes it back."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.title = "Old title"
        slot._titled = True
        pushed: list[tuple[str, str]] = []
        state.push_slot_title = (  # type: ignore[method-assign]
            lambda key, title, **kw: pushed.append((key, title))
        )

        dispatcher, client, _, _ = _dispatcher(
            tmp_path,
            log=state.conversation_log,
        )
        dispatcher.dashboard_state = state

        await dispatcher._handle_title(
            ("direct", "7"),
            7,
            "New title",
            session_key="dashboard:chat-1",
        )

        assert slot.title == "New title"
        assert slot._titled is True
        assert slot._title_origin == "user"
        assert pushed == [("chat-1", "New title")]
        assert state.conversation_log.get_metadata("dashboard:chat-1")["title"] == "New title"
        assert "Renamed" in client.sent[-1][0]

    def test_the_persist_call_is_guarded_by_that_decision(self) -> None:
        """The ceiling is only a ceiling if the turn path consults it.

        Source-level because the alternative is driving a whole turn to observe an
        absent write; what matters is the ORDER — the decision is made on the loop,
        before the worker-thread write it guards.
        """
        import inspect

        src = inspect.getsource(TelegramDispatcher._run_turn)
        decided = src.index("dashboard_restricted = await self._session_restricted(")
        guarded = src.index("if not dashboard_restricted:")
        projected = src.index("mirror_mids = project_channel_turn_live(")
        persisted = src.index("self._persist_turn,")

        assert decided < guarded < projected < persisted


class TestResumedRefusalsCharacterization:
    """The refusals a RESUMED dashboard session gets, byte for byte, and their order.

    Telegram's busy branch hands a resumed session's message to the dashboard slot's
    own machinery (as Discord does); with no dashboard attached the slot cannot take
    it, so the refusal below is what the hand-off answers. A privacy modifier on a
    resumed session is refused before the busy check is even asked.
    """

    _BUSY = (
        "⏳ That session is busy with a turn started elsewhere. Send your message again "
        "once it finishes, or /unlink to return to your Telegram conversation."
    )
    _PRIVACY = (
        "🔒 Privacy mode can't be changed while a dashboard session is resumed. Your "
        "message was NOT processed. Use /unlink or /new first."
    )
    _BUSY_OPTION = (
        "🔘 That conversation is busy with another turn, so your choice was NOT "
        "applied. Type it as a message once the turn finishes."
    )

    @staticmethod
    def _resumed(tmp_path: Any) -> tuple[Any, Any, Any]:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(  # type: ignore[method-assign]
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        return dispatcher, client, sessions

    @pytest.mark.asyncio
    async def test_a_busy_resumed_session_refuses_byte_exact(self, tmp_path: Any) -> None:
        dispatcher, client, sessions = self._resumed(tmp_path)
        sessions.busy = True
        busy = AsyncMock()
        dispatcher._handle_busy = busy  # type: ignore[method-assign]

        await dispatcher.handle_message(_dm("continue"))

        assert [t for t, _ in client.sent] == [self._BUSY]
        assert client.send_threads == [None]
        busy.assert_not_awaited()
        assert sessions.last_key == "" and sessions.queued == []

    @pytest.mark.asyncio
    async def test_a_busy_resumed_topic_refuses_in_its_topic(self, tmp_path: Any) -> None:
        dispatcher, client, sessions = self._resumed(tmp_path)
        dispatcher.cfg.telegram.allow_forum = True
        dispatcher.cfg.telegram.allowed_forum_chat_ids = [-100]
        sessions.busy = True

        await dispatcher.handle_message(_topic("continue", 5))

        assert [t for t, _ in client.sent] == [self._BUSY]
        assert client.send_threads == [5]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["/temporary", "/incognito private question"])
    async def test_a_resumed_privacy_modifier_refuses_byte_exact(
        self, tmp_path: Any, monkeypatch: Any, text: str
    ) -> None:
        from kiro_crew.messaging import privacy_mode

        dispatcher, client, sessions = self._resumed(tmp_path)
        apply = AsyncMock()
        monkeypatch.setattr(privacy_mode, "apply_mode", apply)
        rotated: list[Any] = []
        original = dispatcher._rotated_session_key

        def _spy(route: Any) -> str:
            rotated.append(route)
            return original(route)

        dispatcher._rotated_session_key = _spy  # type: ignore[method-assign]
        sessions.busy = True

        await dispatcher.handle_message(_dm(text))

        assert [t for t, _ in client.sent] == [self._PRIVACY]
        assert client.send_threads == [None]
        apply.assert_not_awaited()
        assert rotated == [] and sessions.last_key == "" and sessions.queued == []

    @pytest.mark.asyncio
    async def test_a_busy_resumed_option_press_gets_the_options_refusal(
        self, tmp_path: Any
    ) -> None:
        dispatcher, client, sessions = self._resumed(tmp_path)
        tag = session_provenance_tag("dashboard:chat-1")
        sessions.busy = True

        await dispatcher.on_callback(_callback(f"opt:0:{tag}", label="Ship it"))

        assert [t for t, _ in client.sent] == [
            "<blockquote>Ship it</blockquote>",
            self._BUSY_OPTION,
        ]
        assert sessions.queued == [] and sessions.provider.steered == []
        assert sessions.last_key == ""

    @pytest.mark.asyncio
    async def test_a_resumed_press_never_rotates(self, tmp_path: Any) -> None:
        dispatcher, _client, sessions = self._resumed(tmp_path)
        rotated: list[Any] = []
        original = dispatcher._rotated_session_key
        dispatcher._rotated_session_key = lambda route: (  # type: ignore[method-assign]
            rotated.append(route) or original(route)
        )
        tag = session_provenance_tag("dashboard:chat-1")

        await dispatcher.on_callback(_callback(f"opt:0:{tag}", label="Ship it"))

        assert rotated == [] and sessions.last_key == "dashboard:chat-1"
