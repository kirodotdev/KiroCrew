"""Teams mid-turn routing: /stop, the queue drain, and the command vocabulary.

These cover the affordances Teams gains from being able to EDIT its own
activities. WeCom and Weixin cannot have them because their reply is bound to the
inbound request, so a held message could not be acknowledged and answered later;
Teams can (``PUT .../activities/{id}``), which is what makes the shared
collapsing queue receipt reachable here.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from kiro_crew.teams.client import TeamsInbound
from kiro_crew.teams.commands import COMMAND_SPEC, build_help_text, parse_command
from kiro_crew.teams.transport_dispatch import (
    _NOT_A_SENDER,
    TeamsDispatcher,
    _origin_kwargs,
    _queued_origin,
    _QueuedOrigin,
)

_SVC = "https://smba.trafficmanager.net/teams"
_EMAIL = "me@example.com"


def _inbound(text: str) -> TeamsInbound:
    return TeamsInbound(
        conversation_id="CONV",
        conversation_type="personal",
        service_url=_SVC,
        text=text,
        user_email=_EMAIL,
        activity_id="act-1",
    )


class _Client:
    """Records sends and honours the edit contract (update by activity id)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.updates: list[tuple[str, str]] = []
        self._next_id = 0

    async def send_message(self, conversation_id, content, service_url):
        self.sent.append((conversation_id, content, service_url))
        self._next_id += 1
        return f"mid-{self._next_id}"

    async def update_message(self, conversation_id, activity_id, content, service_url):
        self.updates.append((activity_id, content))
        return True

    async def send_typing(self, conversation_id, service_url) -> None:
        return None


class _Provider:
    def __init__(self, *, cancels: bool = True) -> None:
        self.supports_steer = True
        self.cancelled: list[float] = []
        self._cancels = cancels

    def has_active_turn(self) -> bool:
        return True

    async def steer(self, text: str) -> bool:
        return False

    async def cancel(self, *, wait_ack_timeout: float = 0) -> None:
        if not self._cancels:
            raise RuntimeError("cancel exploded")
        self.cancelled.append(wait_ack_timeout)


class _Sessions:
    def __init__(self, provider, *, busy: bool = True) -> None:
        self._p = provider
        self._busy = busy
        self.queues: dict[str, list] = {}
        self.cleared: list[str] = []
        self.mirror_links: dict = {}
        self.opt_outs: dict = {}

    async def aflush(self) -> None:
        # The resume release flushes the session map before it reports success; a
        # double without this correctly surfaces as a release FAILURE.
        return None

    def clear_mirror_links_at(self, link, *, reason: str = "") -> list:
        return []

    def find_mirror_sessions(self, link, *, inbound_only: bool = False) -> list:
        # No resumed dashboard session in these tests, so routing is a no-op. Present
        # because Teams routes EVERY message through the resume resolver.
        return []

    def is_busy(self, key) -> bool:
        return self._busy

    def get_provider(self, key):
        return self._p

    def max_generation(self, bucket: str) -> int:
        return -1

    def enqueue(self, key, msg_ts, text, *, force=False, **kwargs) -> bool:
        if not force and not self._busy:
            return False
        self.queues.setdefault(key, []).append((msg_ts, text, kwargs))
        return True

    def dequeue(self, key):
        queue = self.queues.get(key) or []
        return queue.pop(0) if queue else None

    def clear_queue(self, key) -> None:
        self.cleared.append(key)
        self.queues.pop(key, None)

    def mirror_opt_out(self, key) -> bool:
        return bool(self.opt_outs.get(key))

    def set_mirror_opt_out(self, key, value) -> None:
        self.opt_outs[key] = value

    def get_mirror_link(self, key):
        return self.mirror_links.get(key)

    def set_mirror_link(self, key, link, *, reason="") -> None:
        self.mirror_links[key] = link

    def clear_mirror_link(self, key, *, reason="") -> bool:
        return self.mirror_links.pop(key, None) is not None

    def is_mirror_paused(self, key, *, origin=False) -> bool:
        return False

    def batched_save(self):
        return contextlib.nullcontext()


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        messaging=SimpleNamespace(
            queue_mode="steer", dm_scope="per_user", idle_reset_minutes=0, daily_reset_hour=-1
        ),
        agent=SimpleNamespace(default_agent="kirocrew", approval_mode="interactive"),
        teams=SimpleNamespace(soft_threshold_pct=80, hard_threshold_pct=95),
    )


def _dispatcher(sessions, client) -> TeamsDispatcher:
    d = TeamsDispatcher(
        sessions=sessions,
        ctx_builder=SimpleNamespace(hooks=SimpleNamespace(auto_approve_subagent_spawn=False)),
        cfg=_cfg(),
    )
    d.client = client
    return d


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_cancels_the_turn_and_clears_the_queue(self) -> None:
        provider = _Provider()
        sessions = _Sessions(provider)
        client = _Client()
        d = _dispatcher(sessions, client)
        key = d._session_key(_EMAIL)
        sessions.queues[key] = [("1", "held", {})]

        await d._handle_stop(_inbound("/stop"))

        # wait_ack_timeout=0: the cancel is cooperative and fire-and-forget, so
        # the acknowledgement is immediate and the turn stops at its next safe
        # point. Waiting here would stall the reply.
        assert provider.cancelled == [0]
        assert sessions.cleared == [key]
        assert "Stopped" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_stop_with_nothing_running_still_clears_the_queue(self) -> None:
        sessions = _Sessions(_Provider(), busy=False)
        client = _Client()
        d = _dispatcher(sessions, client)

        await d._handle_stop(_inbound("/stop"))

        assert sessions.cleared == [d._session_key(_EMAIL)]
        assert "Nothing was running" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_failing_cancel_still_clears_and_answers(self) -> None:
        """A provider that cannot cancel must not leave the queue full."""
        sessions = _Sessions(_Provider(cancels=False))
        client = _Client()
        d = _dispatcher(sessions, client)

        await d._handle_stop(_inbound("/stop"))

        assert sessions.cleared == [d._session_key(_EMAIL)]
        assert client.sent, "the user must still get an acknowledgement"

    @pytest.mark.asyncio
    async def test_stop_is_reachable_through_the_command_intercept(self) -> None:
        sessions = _Sessions(_Provider())
        client = _Client()
        d = _dispatcher(sessions, client)

        await d.handle_message(_inbound("/cancel"))

        assert sessions.cleared == [d._session_key(_EMAIL)], "/cancel aliases /stop"


class TestQueueReceipt:
    @pytest.mark.asyncio
    async def test_a_burst_grows_ONE_receipt_rather_than_many_bubbles(self) -> None:
        sessions = _Sessions(_Provider())
        client = _Client()
        d = _dispatcher(sessions, client)
        inbound = _inbound("first")

        await d._enqueue_with_receipt(d._session_key(_EMAIL), inbound, "first")
        await d._enqueue_with_receipt(d._session_key(_EMAIL), inbound, "second")

        assert len(client.sent) == 1, "the receipt is created once"
        assert client.updates, "and then EDITED in place as the burst grows"
        assert "Queued (2)" in client.updates[-1][1]

    @pytest.mark.asyncio
    async def test_the_receipt_is_edited_never_deleted_on_cancel(self) -> None:
        """The receipt is the durable record that a message was accepted."""
        sessions = _Sessions(_Provider())
        client = _Client()
        d = _dispatcher(sessions, client)
        inbound = _inbound("held")
        await d._enqueue_with_receipt(d._session_key(_EMAIL), inbound, "held")

        await d._handle_stop(inbound)

        assert any("Cancelled" in body for _, body in client.updates)


class TestCommandVocabulary:
    def test_every_spec_alias_parses_to_its_canonical_name(self) -> None:
        for canonical, aliases, _ in COMMAND_SPEC:
            for alias in aliases:
                assert parse_command(alias) == canonical

    def test_help_lists_every_command_so_it_cannot_drift(self) -> None:
        """A hand-written help card silently stops matching the parser."""
        help_text = build_help_text()
        for canonical, _, _ in COMMAND_SPEC:
            assert f"/{canonical}" in help_text

    def test_an_argument_does_not_defeat_the_parser(self) -> None:
        assert parse_command("/yolo on") == "yolo"

    def test_a_non_command_is_not_a_command(self) -> None:
        assert parse_command("what time is it") is None
        assert parse_command("/nonsense") is None


class TestDrain:
    @pytest.mark.asyncio
    async def test_a_burst_collapses_into_one_turn(self, monkeypatch) -> None:
        """N queued messages become ONE combined turn, not N replayed turns."""
        turns: list[str] = []

        async def _fake_drive(turn, **kw):
            turns.append(turn.user_text)

        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", _fake_drive)
        monkeypatch.setattr(
            "kiro_crew.teams.transport_dispatch.inbound_permitted",
            lambda _c: _true(),
        )
        sessions = _Sessions(_Provider(), busy=False)
        d = _dispatcher(sessions, _Client())
        key = d._session_key(_EMAIL)
        # Seeded through the PRODUCTION writer, not by spelling the storage keys: a
        # hand-built entry with no origin is a state production cannot create, and a
        # fixture that invents one would test a path that does not exist.
        entry = _origin_kwargs(_inbound("x"))
        sessions.queues[key] = [("1", "first", dict(entry)), ("2", "second", dict(entry))]

        await d._drain_queue(key, _inbound("x"))

        assert turns == ["first\n\nsecond"], "the burst must collapse, order preserved"

    @pytest.mark.asyncio
    async def test_the_drained_turn_does_not_nest_another_drain(self, monkeypatch) -> None:
        """A replay that re-entered the drain would nest one per burst round."""
        seen: list[bool] = []
        real = TeamsDispatcher._drain_queue

        async def _spy(self, session_key, inbound):
            seen.append(True)
            await real(self, session_key, inbound)

        async def _fake_drive(turn, **kw):
            return None

        monkeypatch.setattr(TeamsDispatcher, "_drain_queue", _spy)
        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", _fake_drive)
        monkeypatch.setattr(
            "kiro_crew.teams.transport_dispatch.inbound_permitted", lambda _c: _true()
        )
        sessions = _Sessions(_Provider(), busy=False)
        d = _dispatcher(sessions, _Client())
        key = d._session_key(_EMAIL)
        sessions.queues[key] = [("1", "held", _origin_kwargs(_inbound("x")))]

        await d.handle_message(_inbound("live"))

        assert (
            len(seen) == 1
        ), f"drain re-entered {len(seen)} times; the replay must pass drain=False"


class TestDrainIdentity:
    """A queue shared by two people must not be answered as one person.

    Under ``messaging.dm_scope = "unified"`` every allow-listed person's direct
    chat collapses into one session key, so ONE queue holds messages from several
    senders. A combined turn carries ONE envelope, so it may only combine messages
    that share one.
    """

    @staticmethod
    def _from(email: str, conversation: str, activity: str, text: str = "") -> TeamsInbound:
        return TeamsInbound(
            conversation_id=conversation,
            conversation_type="personal",
            service_url=_SVC,
            text=text,
            user_email=email,
            resolved_identity=email,
            activity_id=activity,
        )

    @staticmethod
    def _patch(monkeypatch) -> tuple[list, list]:
        """Capture the envelope every replay runs under, and every turn it drives."""
        envelopes: list[TeamsInbound] = []
        turns: list = []
        real_handle = TeamsDispatcher.handle_message

        async def _spy(self, inbound, **kw):
            envelopes.append(inbound)
            await real_handle(self, inbound, **kw)

        async def _fake_drive(turn, **kw):
            turns.append(turn)

        monkeypatch.setattr(TeamsDispatcher, "handle_message", _spy)
        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", _fake_drive)
        monkeypatch.setattr(
            "kiro_crew.teams.transport_dispatch.inbound_permitted", lambda _c: _true()
        )
        return envelopes, turns

    @pytest.mark.asyncio
    async def test_each_queued_message_is_answered_under_its_own_sender(self, monkeypatch) -> None:
        """Two senders on one queue drain as two turns, each in its own chat.

        End to end through the real enqueue, so the recorder and the reader are
        covered together: a recorded origin nothing reads back is not a fix.
        """
        envelopes, turns = self._patch(monkeypatch)
        sessions = _Sessions(_Provider())
        d = _dispatcher(sessions, _Client())
        key = d._session_key(_EMAIL)
        first = self._from("first@example.com", "CONV-FIRST", "act-first")
        second = self._from("second@example.com", "CONV-SECOND", "act-second")

        assert await d._enqueue_with_receipt(key, first, "mine")
        assert await d._enqueue_with_receipt(key, second, "and mine")
        # The turn they queued behind has now finished. The fake exposes no setter,
        # and the drain only runs once the semaphore is released.
        sessions._busy = False

        await d._drain_queue(key, first)

        assert [e.user_email for e in envelopes] == [
            "first@example.com",
            "second@example.com",
        ], "each drained turn must name the sender who wrote its text"
        assert [e.text for e in envelopes] == ["mine", "and mine"], "FIFO order, one turn each"
        assert [e.conversation_id for e in envelopes] == ["CONV-FIRST", "CONV-SECOND"]
        assert [e.activity_id for e in envelopes] == ["act-first", "act-second"]
        # The attribution the turn itself is recorded under -- its audit caller and
        # its session-attribution id both resolve from that envelope.
        assert [t.audit_caller for t in turns] == [
            "teams:first@example.com",
            "teams:second@example.com",
        ]
        assert [t.conversation_id for t in turns] == [
            "teams:first@example.com",
            "teams:second@example.com",
        ]

    @pytest.mark.asyncio
    async def test_one_senders_burst_still_collapses_into_a_single_turn(self, monkeypatch) -> None:
        """The ordinary case is unchanged: one person's burst is ONE turn.

        The two messages carry DISTINCT activity ids, because the Bot Framework mints
        one per activity and the replay guard depends on them being unique -- so two
        real messages never share one. Enqueuing a single inbound twice would give
        both entries the same id, which no live path produces, and the collapse
        condition would then pass here while never firing in production.
        """
        envelopes, turns = self._patch(monkeypatch)
        sessions = _Sessions(_Provider())
        d = _dispatcher(sessions, _Client())
        key = d._session_key(_EMAIL)
        first = self._from(_EMAIL, "CONV", "act-1")
        second = self._from(_EMAIL, "CONV", "act-2")
        assert first.activity_id != second.activity_id, "the point of this test"

        assert await d._enqueue_with_receipt(key, first, "first")
        assert await d._enqueue_with_receipt(key, second, "second")
        sessions._busy = False

        await d._drain_queue(key, first)

        assert [e.text for e in envelopes] == ["first\n\nsecond"], "the burst must still collapse"
        assert len(turns) == 1
        assert turns[0].audit_caller == f"teams:{_EMAIL}"
        assert envelopes[0].conversation_id == "CONV"
        # The replayed envelope still carries the FIRST entry's own activity id, which
        # is what the receipt bubble was posted against.
        assert envelopes[0].activity_id == "act-1"

    @pytest.mark.asyncio
    async def test_the_collapse_key_drops_only_the_message_id(self) -> None:
        """The collapse key is every origin field except the ones naming the message.

        Derived from ``_fields`` rather than restated, so adding a field to
        ``_QueuedOrigin`` joins the key by default: a WHO field left out would let two
        people's messages collapse under one identity, while a surplus WHICH-MESSAGE
        field only costs a collapse. The exclusion set is pinned because widening it is
        how that identity bug would return.
        """
        assert _NOT_A_SENDER == {"activity_id"}
        key_fields = tuple(n for n in _QueuedOrigin._fields if n not in _NOT_A_SENDER)
        assert key_fields == (
            "conversation_id",
            "service_url",
            "resolved_identity",
            "user_email",
            "aad_object_id",
        )
        origin = _QueuedOrigin("C", "S", "who", "who@example.com", "aad", "act-1")
        assert origin.sender_key == tuple(getattr(origin, n) for n in key_fields)
        # Same person and chat, different message: equal keys, unequal origins.
        later = origin._replace(activity_id="act-2")
        assert later.sender_key == origin.sender_key
        assert later != origin
        # Different person: unequal keys.
        assert origin._replace(user_email="other@example.com").sender_key != origin.sender_key

    def test_an_untagged_entry_is_foreign_not_a_producer_bug(self) -> None:
        """No channel tag means NOT MINE, so the drain sets it aside rather than raising.

        Raising here would lose messages: ``_queued_origin`` runs AFTER ``dequeue``, so an
        exception discards every message that iteration has already taken off the queue,
        and the remainder is re-enqueued only after the loop.
        An entry another transport recorded is reachable on the live path, because one
        session key under a unified scope is shared by every dispatcher on the host.
        """
        assert _queued_origin({}) is None
        assert _queued_origin({"queued_channel": "telegram", "telegram_user_id": "7"}) is None

    def test_an_entry_tagged_MINE_but_missing_a_field_is_a_producer_bug(self) -> None:
        """Ownership is decided on the tag, so a field missing from an OWN entry still raises.

        Both producers are in this module: ``_enqueue_with_receipt`` writes the origin, and
        the only other enqueue is the drain re-queueing an entry it set aside, which passes
        that entry's own kwargs back. So absence on an entry claiming this channel can only
        mean a NEW producer forgot, and the two silent alternatives are both worse than
        raising: empty strings would address an empty conversation id, and inheriting the
        opener's envelope is the exact defect this module exists to prevent.
        """
        with pytest.raises(KeyError) as caught:
            _queued_origin({"queued_channel": "teams"})
        assert "teams_conversation_id" in str(caught.value), "the error must name what is missing"

        # A partially recorded entry is a producer bug too, not a half-usable envelope.
        with pytest.raises(KeyError):
            _queued_origin({"queued_channel": "teams", "teams_conversation_id": "CONV"})

    @pytest.mark.asyncio
    async def test_the_receipt_is_flipped_in_the_chat_that_holds_its_bubble(
        self, monkeypatch
    ) -> None:
        """The bubble belongs to whoever queued first, not to whoever opened the turn."""
        self._patch(monkeypatch)
        sessions = _Sessions(_Provider())
        client = _AddressedClient()
        d = _dispatcher(sessions, client)
        key = d._session_key(_EMAIL)
        queuer = self._from("queuer@example.com", "CONV-QUEUER", "act-q")

        assert await d._enqueue_with_receipt(key, queuer, "held")
        sessions._busy = False

        await d._drain_queue(key, self._from(_EMAIL, "CONV-OPENER", "act-o"))

        flips = [u for u in client.updates if "Now answering" in u[2]]
        assert flips, "the drain must flip the receipt"
        assert flips[0][0] == "CONV-QUEUER", "editing under another chat's address cannot land"

    @pytest.mark.asyncio
    async def test_the_enqueued_entry_records_the_senders_own_origin(self) -> None:
        """Nothing downstream can recover an origin the entry never carried."""
        sessions = _Sessions(_Provider())
        d = _dispatcher(sessions, _Client())
        key = d._session_key(_EMAIL)
        sender = self._from("who@example.com", "CONV-WHO", "act-w")

        assert await d._enqueue_with_receipt(key, sender, "hello")

        # Read back through the production reader rather than by spelling the
        # storage keys, so renaming one cannot leave this test passing.
        origin = _queued_origin(sessions.queues[key][0][2])
        assert origin.conversation_id == "CONV-WHO"
        assert origin.user_email == "who@example.com"
        assert origin.resolved_identity == "who@example.com"
        assert origin.activity_id == "act-w"
        assert origin.service_url == _SVC


class TestTeamsSharesTheQueueWithOtherTransports:
    """This channel is not alone on its queue, and a foreign entry must not cost messages.

    Every DM dispatcher is handed the orchestrator's single ``SessionManager``, and under
    ``dm_scope = "unified"`` ``build_dm_session_key`` drops the CHANNEL from the bucket,
    so a Teams chat and a Telegram DM to one agent resolve to one session key and one
    queue. ``_queued_origin`` therefore decides ownership before it reads any prefixed
    key, and it runs after ``dequeue``: requiring its own keys on a Telegram entry would
    raise ``KeyError`` with this iteration's already-dequeued messages nowhere, since the
    remainder is re-enqueued only after the loop.
    """

    _from = staticmethod(TestDrainIdentity._from)
    _patch = staticmethod(TestDrainIdentity._patch)

    @staticmethod
    def _foreign(text: str = "from telegram") -> tuple[str, str, dict]:
        """A queue entry as the TELEGRAM producer records one, built by that producer."""
        from kiro_crew.telegram.transport_dispatch import _origin_kwargs as tg_kwargs
        from kiro_crew.telegram.transport_dispatch import _QueuedOrigin as TgOrigin

        origin = TgOrigin(user_id="7", chat_id="70", thread_id="", chat_type="private", username="")
        return ("t0", text, tg_kwargs(origin))

    def test_the_producer_tags_every_entry_with_this_channel(self) -> None:
        """Without the tag no peer can name this channel as the owner to wake."""
        from kiro_crew.messaging.queue_drain import entry_channel

        recorded = _origin_kwargs(self._from(_EMAIL, "CONV", "act-1"))

        assert entry_channel(recorded) == "teams"

    @pytest.mark.asyncio
    async def test_a_foreign_entry_is_set_aside_and_this_turn_still_answers(
        self, monkeypatch
    ) -> None:
        """The message-loss fix, measured on the loss rather than on the exception.

        The foreign entry is dequeued FIRST, so under the old required-key read the raise
        happened with this channel's own entry already off the queue. Both must survive:
        ours answered, theirs still queued for its owner.
        """
        envelopes, _turns = self._patch(monkeypatch)
        sessions = _Sessions(_Provider())
        d = _dispatcher(sessions, _Client())
        key = d._session_key(_EMAIL)
        mine = self._from(_EMAIL, "CONV", "act-1")

        assert await d._enqueue_with_receipt(key, mine, "mine")
        # Theirs arrives FIRST in the queue, which is the ordering the old read died on.
        sessions.queues[key].insert(0, self._foreign())
        sessions._busy = False

        await d._drain_queue(key, mine)

        assert [e.text for e in envelopes] == ["mine"], "our own entry is still answered"
        assert [t for _ts, t, _kw in sessions.queues[key]] == [
            "from telegram"
        ], "and theirs is kept, not consumed and not lost"

    @pytest.mark.asyncio
    async def test_the_drain_is_registered_and_answers_with_no_opening_envelope(
        self, monkeypatch
    ) -> None:
        """A peer wakes this drain with the session key alone.

        There is no inbound then -- the finished turn belonged to another transport -- so
        the replay is built on a bare template and every addressing field comes from the
        entry's own origin.
        """
        from kiro_crew.messaging import queue_drain

        envelopes, _turns = self._patch(monkeypatch)
        sessions = _Sessions(_Provider())
        d = _dispatcher(sessions, _Client())
        key = d._session_key(_EMAIL)
        mine = self._from("solo@example.com", "CONV-SOLO", "act-solo")

        assert await d._enqueue_with_receipt(key, mine, "answer me")
        sessions._busy = False

        assert queue_drain._DRAINS.get("teams") is not None, "a peer must be able to wake it"
        await queue_drain._DRAINS["teams"](key)

        assert [e.text for e in envelopes] == ["answer me"]
        assert envelopes[0].conversation_id == "CONV-SOLO"
        assert envelopes[0].user_email == "solo@example.com"
        assert envelopes[0].conversation_type == "personal", "the only scope this queue sees"

    @pytest.mark.asyncio
    async def test_the_drain_wakes_the_owner_of_an_entry_it_set_aside(self, monkeypatch) -> None:
        """Setting it aside is only half the fix: something must come back for it.

        The entry was already accepted and receipted, and a drain runs only from the tail
        of its OWN channel's turn -- so without the wake that message waits for its owner
        to finish some unrelated turn, and forever if that user goes quiet there.
        """
        from kiro_crew.messaging import queue_drain

        self._patch(monkeypatch)
        sessions = _Sessions(_Provider())
        d = _dispatcher(sessions, _Client())
        key = d._session_key(_EMAIL)
        woken: list[str] = []

        async def _peer(session_key: str) -> None:
            woken.append(session_key)

        queue_drain.register_drain("telegram", _peer)

        assert await d._enqueue_with_receipt(key, self._from(_EMAIL, "CONV", "act-1"), "mine")
        sessions.queues[key].append(self._foreign())
        sessions._busy = False

        await d._drain_queue(key, self._from(_EMAIL, "CONV", "act-1"))

        assert woken == [key], "the channel that owns the set-aside entry must be woken"

    @pytest.mark.asyncio
    async def test_the_receipt_counts_only_the_answered_senders_own_deferrals(
        self, monkeypatch
    ) -> None:
        """A foreign entry is not this sender's deferred message.

        It drains in its own channel, in its own chat. Counting it here would tell this
        person to expect a follow-up for text they never wrote.
        """
        self._patch(monkeypatch)
        sessions = _Sessions(_Provider())
        d = _dispatcher(sessions, _Client())
        key = d._session_key(_EMAIL)
        mine = self._from(_EMAIL, "CONV", "act-1")
        deferred: list[int] = []

        async def _flip(session_key, surface, answered, n=0):
            deferred.append(n)

        monkeypatch.setattr(d._queue, "flip_answering_locked", _flip)

        assert await d._enqueue_with_receipt(key, mine, "mine")
        sessions.queues[key].append(self._foreign())
        sessions._busy = False

        await d._drain_queue(key, mine)

        assert deferred == [0], "their entry is set aside, but it is not MY deferral"

    @pytest.mark.asyncio
    async def test_the_count_also_excludes_another_teams_senders_entry(self, monkeypatch) -> None:
        """A foreign entry is excluded because it has no origin here at all.

        Someone else on THIS channel is the harder case: their entry is owned, readable,
        and still not this sender's deferral. It drains in their own chat, so counting it
        promises this person a follow-up for text they never wrote.
        """
        self._patch(monkeypatch)
        sessions = _Sessions(_Provider())
        d = _dispatcher(sessions, _Client())
        key = d._session_key(_EMAIL)
        mine = self._from(_EMAIL, "CONV", "act-1")
        theirs = self._from("other@example.com", "CONV2", "act-2", "theirs")
        deferred: list[int] = []

        async def _flip(session_key, surface, answered, n=0):
            deferred.append(n)

        monkeypatch.setattr(d._queue, "flip_answering_locked", _flip)

        assert await d._enqueue_with_receipt(key, mine, "mine")
        sessions.queues[key].append(("t1", "theirs", _origin_kwargs(theirs)))
        sessions._busy = False

        await d._drain_queue(key, mine)

        # The pump loops, so the other sender's entry drains as its OWN turn in its own
        # chat. Two receipts are flipped and each reports zero, because neither sender
        # has a message of their own left waiting.
        assert deferred == [0, 0], "owned, readable, and still not the other's deferral"


class _AddressedClient(_Client):
    """Records the CHAT an edit was addressed to, which ``_Client`` drops."""

    def __init__(self) -> None:
        super().__init__()
        self.updates: list[tuple[str, str, str]] = []  # type: ignore[assignment]

    async def update_message(self, conversation_id, activity_id, content, service_url):
        self.updates.append((conversation_id, activity_id, content))
        return True


async def _true() -> bool:
    return True
