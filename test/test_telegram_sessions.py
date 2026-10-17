"""Telegram's session picker and the private-chat topic routing.

The picker lists this DM's sessions, and a pick binds one -- its expectation, the mirror
it takes over, the rollback when a write fails, the owner gate -- with the route model a
picked session does or does not inherit. Inbound messages to a resumed dashboard session
-- their routing and hand-off outcomes, the model picker and approvals on it, cold resume,
a restricted resumed session and the resumed refusals -- are in
``test_telegram_sessions_resume.py``; the doubles both modules share are in
``telegram_sessions_helpers.py``.
"""

from __future__ import annotations

import asyncio
import importlib
import threading
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telegram_sessions_helpers import _reset_shared_trust  # noqa: F401
from telegram_sessions_helpers import (
    _callback,
    _dispatcher,
    _dm,
    _log,
    _picker_button,
    _Sessions,
    _topic,
)

from kiro_crew.messaging.link import UNBIND_REASON_UNSPECIFIED, ChannelLink
from kiro_crew.messaging.session_resume import SETTLE_NOTHING, RoutingDecision
from kiro_crew.telegram.session_resume import _ROUTE_OWNER_REFUSAL
from kiro_crew.telegram.transport import TelegramInboundMessage
from kiro_crew.testing.wait import await_until


def _resume_module() -> Any:
    return importlib.import_module("kiro_crew.telegram.session_resume")


def _private_topic(text: str, thread: int, *, chat_id: int = 7, user_id: int = 7):
    """A message in a forum Topic INSIDE a 1:1 private chat.

    Telegram carries ``message_thread_id`` in a private chat once direct-message
    topics are on; the chat is still ``private`` and its chat_id still equals the
    user id. The threadless General topic carries no ``thread_id`` and routes as
    an ordinary DM.
    """
    return TelegramInboundMessage(
        channel_type="telegram",
        user_id=str(user_id),
        conversation_id=str(chat_id),
        text=text,
        chat_type="private",
        thread_id=str(thread),
    )


class TestTelegramPrivateChatTopicRouting:
    """Each forum Topic in a private DM is its own session, keyed by
    ``(chat_id, thread)`` the way a supergroup Topic already is -- while staying
    'private' for the DM-only audience guards."""

    def test_threadless_private_dm_keeps_the_pre_topic_identity(self, tmp_path: Any) -> None:
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        route = dispatcher._route_key(chat_type="private", user_id=7, chat_id=7, thread=None)
        assert route == ("direct", "7")
        assert dispatcher._route_thread(route) is None
        # Byte-identical to the pre-forum DM key: users who never enable topics
        # see no change.
        assert dispatcher._session_key(route) == "telegram:kirocrew:direct:7"

    def test_a_private_topic_folds_chat_id_and_thread(self, tmp_path: Any) -> None:
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        route = dispatcher._route_key(chat_type="private", user_id=7, chat_id=7, thread=11)
        assert route == ("direct_topic", "7:11")
        assert dispatcher._route_thread(route) == 11
        assert dispatcher._session_key(route) == "telegram:kirocrew:direct_topic:7:11"

    def test_two_private_topics_resolve_to_distinct_sessions(self, tmp_path: Any) -> None:
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        a = dispatcher._session_key(
            dispatcher._route_key(chat_type="private", user_id=7, chat_id=7, thread=11)
        )
        b = dispatcher._session_key(
            dispatcher._route_key(chat_type="private", user_id=7, chat_id=7, thread=12)
        )
        general = dispatcher._session_key(
            dispatcher._route_key(chat_type="private", user_id=7, chat_id=7, thread=None)
        )
        assert len({a, b, general}) == 3

    def test_private_topic_is_private_for_audience_guards(self, tmp_path: Any) -> None:
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        topic = dispatcher._route_key(chat_type="private", user_id=7, chat_id=7, thread=11)
        general = dispatcher._route_key(chat_type="private", user_id=7, chat_id=7, thread=None)
        group = dispatcher._route_key(chat_type="supergroup", user_id=7, chat_id=-100, thread=11)
        # A private topic is a 1:1 DM: it keeps /kirocrew dashboard and the
        # host-wide listings; a supergroup Topic (group-readable) does not.
        assert dispatcher._is_private_route(topic) is True
        assert dispatcher._is_private_route(general) is True
        assert dispatcher._is_private_route(group) is False

    def test_private_topic_does_not_collapse_under_unified_dm_scope(self, tmp_path: Any) -> None:
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher.cfg.messaging.dm_scope = "unified"
        general = dispatcher._session_key(
            dispatcher._route_key(chat_type="private", user_id=7, chat_id=7, thread=None)
        )
        topic = dispatcher._session_key(
            dispatcher._route_key(chat_type="private", user_id=7, chat_id=7, thread=11)
        )
        # The threadless DM collapses into the shared unified bucket; the topic
        # keeps its own full bucket, or the fix would be a no-op.
        assert general.startswith("unified:")
        assert topic == "telegram:kirocrew:direct_topic:7:11"

    @pytest.mark.asyncio
    async def test_a_private_topic_turn_runs_under_its_own_session(self, tmp_path: Any) -> None:
        dispatcher, _, sessions, _ = _dispatcher(tmp_path)
        await dispatcher.handle_message(_private_topic("in topic 11", thread=11))
        assert sessions.last_key == "telegram:kirocrew:direct_topic:7:11"


class TestTelegramSessionPicker:
    @pytest.mark.asyncio
    async def test_keyboard_payload_owner_message_index_and_double_press(
        self, tmp_path: Any
    ) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)

        await dispatcher.handle_message(_dm("/sessions"))

        data, message_id = _picker_button(client)
        markup = client.sent[-1][1]
        assert data.startswith("s:")
        assert data.isascii() and len(data.encode("ascii")) <= 64
        assert all(len(row) == 1 for row in markup["inline_keyboard"])

        await dispatcher.on_callback(_callback(data, message_id=message_id + 1))
        assert sessions.mirror_links == {}
        await dispatcher.on_callback(_callback(data.replace(":0", ":99"), message_id=message_id))
        assert sessions.mirror_links == {}
        await dispatcher.on_callback(_callback(data, message_id=message_id))
        assert sessions.find_mirror_sessions(
            ChannelLink("telegram", channel_id="7"), inbound_only=True
        ) == ["dashboard:chat-1"]
        before = dict(sessions.mirror_links)
        await dispatcher.on_callback(_callback(data, message_id=message_id))
        assert sessions.mirror_links == before
        assert "expired" in client.edits[-1][1].lower()

    @pytest.mark.asyncio
    async def test_lists_only_this_dm_native_generations(self, tmp_path: Any) -> None:
        dispatcher, client, sessions, log = _dispatcher(tmp_path)
        prior_key = "telegram:kirocrew:direct:7:gen4"
        other_key = "telegram:kirocrew:direct:8:gen4"
        await asyncio.to_thread(log.append, prior_key, "user", "earlier telegram work")
        await asyncio.to_thread(log.set_title, prior_key, "Earlier Telegram generation")
        await asyncio.to_thread(log.append, other_key, "user", "somebody else's work")
        await asyncio.to_thread(log.set_title, other_key, "Another Telegram user")
        sessions.channel_keys.update({prior_key, other_key})

        await dispatcher.handle_message(_dm("/sessions"))

        labels = [row[0]["text"] for row in client.sent[-1][1]["inline_keyboard"]]
        # This regression owns eligibility, not the mtime-ranked catalog order.
        assert sorted(label.partition(". ")[2] for label in labels) == [
            "Earlier Telegram generation",
            "Launch plan",
        ]

    @pytest.mark.asyncio
    async def test_repeated_new_does_not_materialize_empty_history_rows(
        self, tmp_path: Any
    ) -> None:
        dispatcher, _, sessions, log = _dispatcher(tmp_path)
        route = ("direct", "7")

        await dispatcher.handle_message(_dm("/new"))
        first_key = dispatcher._session_key(route)
        await dispatcher.handle_message(_dm("/new"))
        second_key = dispatcher._session_key(route)

        assert first_key != second_key
        assert not await asyncio.to_thread(log.has_log, first_key)
        assert not await asyncio.to_thread(log.has_log, second_key)
        assert {first_key, second_key} <= sessions.reserved_generations

    @pytest.mark.asyncio
    async def test_failed_post_registers_no_picker(self, tmp_path: Any) -> None:
        dispatcher, client, _, _ = _dispatcher(tmp_path)
        client.send_fails = True

        await dispatcher.handle_message(_dm("/sessions"))

        assert len(dispatcher._session_resume.pickers) == 0

    @pytest.mark.parametrize(
        "message",
        [
            pytest.param(_dm("/sessions", chat_id=8), id="private-chat-not-user"),
            pytest.param(_topic("/sessions", 11), id="forum-topic"),
        ],
    )
    @pytest.mark.asyncio
    async def test_owner_gate_runs_before_history_read(
        self, tmp_path: Any, message: TelegramInboundMessage, monkeypatch: Any
    ) -> None:
        log = _log(tmp_path)
        reads = 0

        def _read() -> list[dict[str, Any]]:
            nonlocal reads
            reads += 1
            return []

        monkeypatch.setattr(log, "list_sessions", _read)
        dispatcher, _, _, _ = _dispatcher(tmp_path, log=log)

        await dispatcher.handle_message(message)

        assert reads == 0

    @pytest.mark.asyncio
    async def test_multiple_allowed_ids_are_not_an_owner(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        log = _log(tmp_path)
        read = AsyncMock()
        monkeypatch.setattr(log, "list_sessions", read)
        dispatcher, client, _, _ = _dispatcher(tmp_path, allowed={7, 8}, log=log)

        await dispatcher.handle_message(_dm("/sessions"))

        assert read.call_count == 0
        assert "single" in client.sent[-1][0] or "exactly one" in client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_expectation_is_durable_before_success_and_no_history_is_replayed(
        self, tmp_path: Any
    ) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        await dispatcher.handle_message(_dm("/sessions"))
        data, message_id = _picker_button(client)
        sent_before = len(client.sent)
        seen: list[Any] = []
        real_edit = client.edit_message

        async def _observe(*args: Any, **kwargs: Any) -> bool:
            if "Resumed" in str(args[2]):
                seen.append(await dispatcher._session_resume.expectations.get("chat:7"))
            return await real_edit(*args, **kwargs)

        client.edit_message = _observe  # type: ignore[method-assign]
        await dispatcher.on_callback(_callback(data, message_id=message_id))

        assert seen and seen[0].key == "dashboard:chat-1"
        assert len(client.sent) == sent_before, "Telegram resume must not replay transcript rows"
        assert client.edits[-1][0] == message_id
        assert client.edits[-1][2] == {"inline_keyboard": []}
        assert sessions.last_key == ""

    def test_expectation_and_link_identity_include_the_full_topic(self, tmp_path: Any) -> None:
        mod = _resume_module()
        resume = mod.TelegramSessionResume(_Sessions(), _log(tmp_path), {7})

        assert resume.expectation_id(7, None) == "chat:7"
        assert resume.expectation_id(-100, 11) == "topic:-100:11"
        assert resume.expectation_id(-100, 12) == "topic:-100:12"
        assert resume.link_for(-100, 11) == ChannelLink(
            "telegram", channel_id="-100", thread_id="11"
        )

    @pytest.mark.asyncio
    async def test_pick_replaces_outbound_native_mirror_in_one_click(self, tmp_path: Any) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        native_key = dispatcher._session_key(("direct", "7"))
        sessions.set_mirror_link(native_key, link, accepts_inbound=False)

        await dispatcher.handle_message(_dm("/session launch"))
        data, message_id = _picker_button(client)
        await dispatcher.on_callback(_callback(data, message_id=message_id))

        assert native_key not in sessions.mirror_links
        assert sessions.find_mirror_sessions(link, inbound_only=True) == ["dashboard:chat-1"]
        assert "Resumed" in client.edits[-1][1]

    @pytest.mark.asyncio
    async def test_pick_never_displaces_deliberate_dashboard_mirror(self, tmp_path: Any) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        native_key = dispatcher._session_key(("direct", "7"))
        deliberate_key = "dashboard:deliberate"
        sessions.set_mirror_link(native_key, link, accepts_inbound=False)
        sessions.set_mirror_link(deliberate_key, link, accepts_inbound=False)

        await dispatcher.handle_message(_dm("/session launch"))
        data, message_id = _picker_button(client)
        await dispatcher.on_callback(_callback(data, message_id=message_id))

        assert sessions.mirror_links == {
            native_key: link,
            deliberate_key: link,
        }
        assert sessions.inbound_keys == set()
        assert "already attached" in client.edits[-1][1]

    @pytest.mark.asyncio
    async def test_failed_takeover_restores_outbound_native_mirror(self, tmp_path: Any) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        native_key = dispatcher._session_key(("direct", "7"))
        sessions.set_mirror_link(native_key, link, accepts_inbound=False)
        real_set = sessions.set_mirror_link

        def _fail_selected(
            key: str,
            candidate: ChannelLink,
            *,
            accepts_inbound: bool = False,
            reason: str = UNBIND_REASON_UNSPECIFIED,
        ) -> None:
            if key == "dashboard:chat-1" and accepts_inbound:
                raise OSError("simulated bind failure")
            real_set(
                key,
                candidate,
                accepts_inbound=accepts_inbound,
                reason=reason,
            )

        sessions.set_mirror_link = _fail_selected  # type: ignore[method-assign]
        await dispatcher.handle_message(_dm("/session launch"))
        data, message_id = _picker_button(client)
        await dispatcher.on_callback(_callback(data, message_id=message_id))

        assert sessions.mirror_links == {native_key: link}
        assert sessions.inbound_keys == set()
        assert "Couldn't resume" in client.edits[-1][1]
        expectation = await dispatcher._session_resume.expectations.get("chat:7")
        assert expectation is not None and expectation.retired

        sent_before = len(client.sent)
        await dispatcher.handle_message(_dm("continue natively"))
        assert sessions.last_key == native_key
        assert not any("NOT processed" in text for text, _ in client.sent[sent_before:])

    @pytest.mark.asyncio
    async def test_batch_write_failure_rolls_back_claim_and_expectation(
        self, tmp_path: Any
    ) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        native_key = dispatcher._session_key(("direct", "7"))
        sessions.set_mirror_link(native_key, link, accepts_inbound=False)
        sessions.batch_failures = 1

        await dispatcher.handle_message(_dm("/session launch"))
        data, message_id = _picker_button(client)
        await dispatcher.on_callback(_callback(data, message_id=message_id))

        assert sessions.mirror_links == {native_key: link}
        assert sessions.inbound_keys == set()
        expectation = await dispatcher._session_resume.expectations.get("chat:7")
        assert expectation is not None and expectation.retired
        assert "Couldn't resume" in client.edits[-1][1]

    @pytest.mark.asyncio
    async def test_a_detached_expectation_never_reaches_a_non_owner(self, tmp_path: Any) -> None:
        """The live-binding gate alone is not the whole owner rule.

        The binder also answers from the durable expectation store, so a binding
        that was detached while its expectation survives resolves NO key and no
        ambiguity — yet still yields a notice built from the dashboard session's
        title. With a multi-user allow-list that notice would disclose host-wide
        history to a non-owner, so any non-empty decision becomes the generic
        refusal, and its settlement stays owed for the real owner.
        """
        dispatcher, client, sessions, _ = _dispatcher(tmp_path, allowed={7, 8})
        resume = dispatcher._session_resume
        # No live binding at all: the first gate cannot fire.
        assert sessions.find_mirror_sessions(resume.link_for(7, None)) == []
        await resume.expectations.record("chat:7", "dashboard:secret", "Acquisition planning")

        decision = await resume.route(7, 7, "private", None)

        assert decision.refusal == _ROUTE_OWNER_REFUSAL
        assert "Acquisition planning" not in (decision.refusal or "")
        assert decision.resumed_key is None
        assert decision.settle == SETTLE_NOTHING, "the notice stays owed, not acknowledged"
        # And the record survives, so the owner still gets told.
        still_there = await resume.expectations.get("chat:7")
        assert still_there is not None and not still_there.retired

    @pytest.mark.asyncio
    async def test_the_owner_still_receives_the_detach_notice(self, tmp_path: Any) -> None:
        """Non-vacuity: the gate refuses the non-owner, not the mechanism."""
        dispatcher, client, _, _ = _dispatcher(tmp_path)
        resume = dispatcher._session_resume
        await resume.expectations.record("chat:7", "dashboard:secret", "Acquisition planning")

        decision = await resume.route(7, 7, "private", None)

        assert decision.refusal != _ROUTE_OWNER_REFUSAL
        assert decision.refusal is not None and "Acquisition planning" in decision.refusal

    @pytest.mark.asyncio
    async def test_a_rebind_landing_before_the_lock_is_not_erased(self, tmp_path: Any) -> None:
        """The transaction must decide on state it read UNDER the map lock.

        Moving the batch off the loop opened a window: the dashboard and other
        channels bind without taking ``binder.lock``, so a rebind can land between
        the loop-side read and the worker acquiring ``_MAP_LOCK``. Acting on the
        stale view would clear a key whose newer, deliberate mirror this pick never
        saw. Simulated by rebinding at batch entry — inside the lock, before the
        transaction body runs.
        """
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        native_key = dispatcher._session_key(("direct", "7"))
        sessions.set_mirror_link(native_key, link, accepts_inbound=False)
        real_batched_save = sessions.batched_save
        injected: list[bool] = []

        @contextmanager
        def _rebind_then_batch() -> Any:
            with real_batched_save():
                if not injected:
                    injected.append(True)
                    # A deliberate dashboard mirror claims this DM first.
                    sessions.mirror_links["dashboard:other"] = link
                yield

        sessions.batched_save = _rebind_then_batch  # type: ignore[method-assign]

        await dispatcher.handle_message(_dm("/session launch"))
        data, message_id = _picker_button(client)
        await dispatcher.on_callback(_callback(data, message_id=message_id))

        assert injected, "the rebind was never simulated"
        assert sessions.mirror_links.get("dashboard:other") == link, "newer mirror was erased"
        assert sessions.mirror_links.get(native_key) == link, "displaced key was cleared anyway"
        assert "dashboard:chat-1" not in sessions.inbound_keys, "the pick must not bind"
        expectation = await dispatcher._session_resume.expectations.get("chat:7")
        assert expectation is not None and expectation.retired

    @pytest.mark.asyncio
    async def test_the_binding_transaction_runs_off_the_event_loop(self, tmp_path: Any) -> None:
        """A whole-map rewrite must not stall the loop every other task shares.

        ``batched_save`` holds the map lock across the block and writes the file on
        the way out, so the commit and its rollback both belong in a worker thread.
        The map documents itself as callable from any thread; what this pins is that
        the batch is not entered from the loop thread.
        """
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        loop_thread = threading.get_ident()
        batch_threads: list[int] = []
        real_batched_save = sessions.batched_save

        @contextmanager
        def _recording_batch() -> Any:
            batch_threads.append(threading.get_ident())
            with real_batched_save():
                yield

        sessions.batched_save = _recording_batch  # type: ignore[method-assign]

        await dispatcher.handle_message(_dm("/session launch"))
        data, message_id = _picker_button(client)
        await dispatcher.on_callback(_callback(data, message_id=message_id))

        assert sessions.inbound_keys == {"dashboard:chat-1"}, "the bind must still succeed"
        assert batch_threads, "expected the binding transaction to open a batch"
        assert all(
            tid != loop_thread for tid in batch_threads
        ), "a session-map batch ran on the event loop"

    @pytest.mark.asyncio
    async def test_unreadable_expectation_store_answers_the_press(self, tmp_path: Any) -> None:
        """A consumed choice must never end in silence.

        The snapshot read runs AFTER the picker registry hands over the choice, so
        an escaping store error would discard the press with no reply and leave a
        dead button. It settles fail-closed instead.
        """
        from kiro_crew.messaging.resume_expectation import ExpectationStoreError

        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        expectations = dispatcher._session_resume.expectations

        await dispatcher.handle_message(_dm("/session launch"))
        data, message_id = _picker_button(client)

        async def _boom(channel_id: str) -> None:
            raise ExpectationStoreError("holds a malformed row")

        expectations.get = _boom  # type: ignore[method-assign]

        await dispatcher.on_callback(_callback(data, message_id=message_id))

        assert sessions.inbound_keys == set(), "no binding may be claimed"
        assert "Couldn't save" in client.edits[-1][1]

    @pytest.mark.asyncio
    async def test_a_press_serializes_against_the_next_message(self, tmp_path: Any) -> None:
        """A press and the message after it are independent Telegram tasks.

        Without a shared per-route lock the message can resolve its session before
        the press commits the binding, so its turn — and its transcript — lands in
        the native session the user just left.
        """
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        await dispatcher.handle_message(_dm("/session launch"))
        data, message_id = _picker_button(client)

        route_id = dispatcher._session_resume.expectation_id(7, None)
        held: list[str] = []
        choose_started = asyncio.Event()
        release_choose = asyncio.Event()
        message_attempted = asyncio.Event()
        real_choose = dispatcher._session_resume.choose

        async def _slow_choose(*args: Any, **kwargs: Any) -> None:
            held.append("choose-start")
            choose_started.set()
            await await_until(release_choose)
            await real_choose(*args, **kwargs)
            held.append("choose-end")

        dispatcher._session_resume.choose = _slow_choose  # type: ignore[method-assign]

        async def _message_after_press() -> None:
            await await_until(choose_started)
            held.append("message")
            message_attempted.set()
            async with dispatcher._routing_turn(route_id):
                held.append("message-routed")

        press = asyncio.create_task(dispatcher.on_callback(_callback(data, message_id=message_id)))
        message = asyncio.create_task(_message_after_press())
        try:
            await await_until(message_attempted)
            # The message runs from set() through lock acquisition before this
            # waiter resumes: both deciders must overlap while choose is held.
            assert "message-routed" not in held, held
            assert len(dispatcher._routing_locks[route_id][1]) == 2
        finally:
            release_choose.set()
            try:
                await await_until(asyncio.ensure_future(asyncio.gather(press, message)))
            finally:
                for task in (press, message):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(press, message, return_exceptions=True)

        assert held.index("choose-end") < held.index(
            "message-routed"
        ), f"the message routed before the press committed: {held}"
        assert sessions.inbound_keys == {"dashboard:chat-1"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/spawn list", "/task status"])
    async def test_host_listings_are_not_routed_through_a_binding(
        self, tmp_path: Any, command: str
    ) -> None:
        """A host-scoped listing reports on the box, not on the resumed session.

        Routing it through a resumed binding lets a stale or refused one withhold
        output that has nothing to do with that session.
        """
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(refusal="🔒 refused")
        )

        await dispatcher.handle_message(_dm(command))

        dispatcher._session_resume.route.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_conversation_scoped_spawn_still_routes(self, tmp_path: Any) -> None:
        """Non-vacuity: the bypass is keyed on the ARGUMENT, not the verb."""
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )

        await dispatcher.handle_message(_dm("/spawn go research this"))

        dispatcher._session_resume.route.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_resumed_session_does_not_inherit_the_route_model(self, tmp_path: Any) -> None:
        """A Telegram /model pick must not follow the user into someone else's session.

        ``_model_pref`` is this ROUTE's choice. A resumed dashboard conversation
        already has a model of its own, so handing the route's preference to a cold
        start would run it under a model its owner never picked — silently, and for
        every later turn.
        """
        dispatcher, _, sessions, _ = _dispatcher(tmp_path)
        route = dispatcher._route_key(chat_type="private", user_id=7, chat_id=7, thread=None)
        dispatcher._model_pref[route] = "telegram-picked-model"
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )

        await dispatcher.handle_message(_dm("continue here"))

        assert sessions.last_key == "dashboard:chat-1"
        assert sessions.last_model is None, "the resumed session inherited the route's model"

    @pytest.mark.asyncio
    async def test_a_native_turn_still_uses_the_route_model(self, tmp_path: Any) -> None:
        """Non-vacuity: the preference still applies to Telegram's own session."""
        dispatcher, _, sessions, _ = _dispatcher(tmp_path)
        route = dispatcher._route_key(chat_type="private", user_id=7, chat_id=7, thread=None)
        dispatcher._model_pref[route] = "telegram-picked-model"

        await dispatcher.handle_message(_dm("hello"))

        assert sessions.last_model == "telegram-picked-model"

    @pytest.mark.asyncio
    async def test_unified_dm_scope_still_gets_one_click_takeover(self, tmp_path: Any) -> None:
        """A non-default ``dm_scope`` must not cost the headline behaviour.

        With ``dm_scope="unified"`` the native bucket is a ``unified:`` key, so the
        namespace test that recognises a ``telegram:`` origin mirror sees a stranger
        and refuses — the user is told to ``/unlink`` first, which is exactly the
        friction one-click takeover removes. The dispatcher supplies its own key so
        the classifier does not have to infer the scope.
        """
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        unified_native = "unified:kirocrew:direct:7"
        sessions.set_mirror_link(unified_native, link, accepts_inbound=False)

        await dispatcher.handle_message(_dm("/session launch"))
        data, message_id = _picker_button(client)
        await dispatcher._session_resume.choose(
            client,
            _callback(data, message_id=message_id),
            native_key=unified_native,
        )

        assert sessions.inbound_keys == {"dashboard:chat-1"}, "the takeover must bind"
        assert unified_native not in sessions.mirror_links, "the native mirror must be replaced"
        assert "Resumed" in client.edits[-1][1]

    @pytest.mark.asyncio
    async def test_a_foreign_native_key_grants_nothing(self, tmp_path: Any) -> None:
        """The supplied key is only honoured while it occupies THIS conversation."""
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        sessions.set_mirror_link("dashboard:other", link, accepts_inbound=False)

        await dispatcher.handle_message(_dm("/session launch"))
        data, message_id = _picker_button(client)
        await dispatcher._session_resume.choose(
            client,
            _callback(data, message_id=message_id),
            native_key="unified:kirocrew:direct:999",
        )

        assert sessions.inbound_keys == set(), "an unrelated mirror must not be displaced"
        assert sessions.mirror_links.get("dashboard:other") == link
        assert "already attached" in client.edits[-1][1]

    @pytest.mark.asyncio
    async def test_failed_bind_restores_the_expectation_it_displaced(self, tmp_path: Any) -> None:
        """A failed pick must not turn a live record into a detach marker.

        ``record`` overwrites the channel's expectation, so retiring the
        replacement on failure leaves DETACHED where ACTIVE belongs — and that
        active record is the evidence a lost link still owes the user a notice.
        Retiring it makes the next message route natively, so the notice is never
        delivered.
        """
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        expectations = dispatcher._session_resume.expectations

        prior = await expectations.record("chat:7", "dashboard:earlier", "Earlier work")
        assert not prior.retired

        sessions.batch_failures = 1
        await dispatcher.handle_message(_dm("/session launch"))
        data, message_id = _picker_button(client)
        await dispatcher.on_callback(_callback(data, message_id=message_id))

        restored = await expectations.get("chat:7")
        assert restored is not None
        assert restored.retired is False, "the displaced ACTIVE record must not detach"
        assert restored.key == "dashboard:earlier"
        assert restored.title == "Earlier work"
        assert restored.version > prior.version, "the undo writes a successor, not a rewrite"
        assert "Couldn't resume" in client.edits[-1][1]
