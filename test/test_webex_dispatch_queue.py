"""WebexDispatcher's mid-turn queue: a flat drain, attachments that arrive mid-turn, and a queue
shared with other transports.
"""

from __future__ import annotations

from unittest import mock

import pytest
from webex_dispatch_helpers import (
    _EMAIL,
    FakeClient,
    FakeCtx,
    FakeProvider,
    FakeSessions,
    _dispatcher,
    _entry,
    _inbound,
)

from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent
from kiro_crew.webex import transport_dispatch as webex_dispatch
from kiro_crew.webex.client import WebexInbound
from kiro_crew.webex.transport_dispatch import _MAX_COLLAPSE


class TestDrainIsFlat:
    @pytest.mark.asyncio
    async def test_the_replayed_turn_does_not_start_its_own_drain(self) -> None:
        """The drain loop is the pump, and it must stay FLAT.

        Letting each replayed turn drain too would nest one Python frame per
        burst, so a sustained burst grows the stack without bound — and the
        nested call would re-enter with the queue in a state its caller has
        already read.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [_entry(str(i), f"m{i}") for i in range(_MAX_COLLAPSE + 2)]
        depth = {"now": 0, "max": 0}
        real_drain = d._drain_queue

        async def _counting(inbound, session_key):
            depth["now"] += 1
            depth["max"] = max(depth["max"], depth["now"])
            try:
                await real_drain(inbound, session_key)
            finally:
                depth["now"] -= 1

        with mock.patch.object(d, "_drain_queue", _counting):
            with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _noop_turn):
                await d._drain_queue(d._session_key(_EMAIL), _inbound("x"))

        assert depth["max"] == 1, "the drain re-entered itself"
        assert sessions.queued == []  # everything still drained


async def _noop_turn(turn, *, sessions, ctx_builder):
    """A drive_turn stand-in: the drain's behaviour is what is under test."""
    return None


# ------------------------------------------------------------------
# Tests: mid-turn attachments
# ------------------------------------------------------------------


def _with_files(text: str = "", urls: tuple[str, ...] = ("https://webexapis.com/v1/c/A",)):
    return WebexInbound(
        person_email=_EMAIL,
        room_id="ROOM",
        text=text,
        room_type="direct",
        file_urls=urls,
    )


class TestMidTurnAttachments:
    @pytest.mark.asyncio
    async def test_a_message_with_files_is_queued_even_in_steer_mode(self) -> None:
        """Steer forwards TEXT ONLY.

        Steering a message that carries files acknowledges a fold while silently
        dropping every attachment, so the files reach the agent one turn later
        rather than never.
        """
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        d = _dispatcher(sessions, FakeCtx(), FakeClient())

        await d.handle_message(_with_files("look at this"))

        assert provider.steered == []
        assert sessions.queued[0][1] == "look at this"
        assert sessions.queued[0][2]["webex_file_urls"] == ["https://webexapis.com/v1/c/A"]

    @pytest.mark.asyncio
    async def test_an_uncaptioned_attachment_still_gets_a_receipt(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_with_files(""))

        assert any(webex_dispatch._QUEUED_ATTACHMENT_LABEL in m for (_, m) in client.sent)

    @pytest.mark.asyncio
    async def test_the_drain_replays_the_queued_files_and_not_the_finished_turns(self) -> None:
        """*inbound* is the message that OPENED the finished turn.

        Inheriting its attachments would re-download and re-summarize files the
        agent has already been shown, once per drain iteration.
        """
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [_entry("1", "and this", webex_file_urls=["QUEUED-URL"])]
        seen: list = []

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            seen.append((inbound.text, inbound.file_urls))

        with mock.patch.object(type(d), "handle_message", _replay):
            await d._drain_queue("KEY", _with_files("first", ("OPENING-URL",)))

        assert seen == [("and this", ("QUEUED-URL",))]

    @pytest.mark.asyncio
    async def test_a_deferred_entry_keeps_its_attachments(self) -> None:
        """A burst past the collapse cap re-enqueues the surplus IN ORDER.

        Re-enqueueing without the kwargs would silently lose the files of
        everything after the cap — and the deferred entry drains in THIS pump, so
        the loss would land one iteration later in the same call.
        """
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [
            _entry(str(i), f"m{i}", webex_file_urls=[f"U{i}"]) for i in range(_MAX_COLLAPSE + 1)
        ]
        seen: list = []

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            seen.append(inbound.file_urls)

        with mock.patch.object(type(d), "handle_message", _replay):
            await d._drain_queue("KEY", _inbound("first"))

        # Two iterations: the capped burst, then the one deferred entry — which
        # still carries the file it was queued with.
        assert len(seen[0]) == _MAX_COLLAPSE
        assert seen[1] == (f"U{_MAX_COLLAPSE}",)

    @pytest.mark.asyncio
    async def test_two_space_members_do_not_answer_under_one_identity(self) -> None:
        """A group space puts several senders on ONE queue, so the key must name them.

        ``_route_of`` maps a space to ``space:{room_id}`` and ``_session_key`` takes that
        route, so every allow-listed member of one space shares a session key and a queue
        under ANY ``dm_scope`` -- this needs no unified setting. Keying the collapse on
        room and thread alone merges two members' mid-turn text into one turn, and the
        replay stamps the FIRST entry's email, so the second member's words run and are
        audited as the first member. Delivery into the shared space still looks right,
        which is what makes the misattribution the half that lasts.
        """
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [
            _entry("1", "mine", room="SPACE", email="a@example.com", room_type="group"),
            _entry("2", "theirs", room="SPACE", email="b@example.com", room_type="group"),
        ]
        seen: list[tuple[str, str]] = []

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            seen.append((inbound.person_email, inbound.text))

        with mock.patch.object(type(d), "handle_message", _replay):
            await d._drain_queue("KEY", _inbound("opener"))

        assert seen == [
            ("a@example.com", "mine"),
            ("b@example.com", "theirs"),
        ], "one turn per sender, each under its own identity"

    @pytest.mark.asyncio
    async def test_a_room_that_speaks_again_does_not_jump_the_room_before_it(self) -> None:
        """Matching the room is not enough: the entry must also be reached before a defer.

        A/B/A on one queue. Testing only ``item_convo == place.convo`` per entry lets the
        second A rejoin A's turn, so A's later message is answered BEFORE a B that
        arrived first. Whoever is in room B watches their turn lose its place to someone
        who spoke after them, which is the ordering this queue exists to keep exact.
        """
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [
            _entry("1", "a1"),
            _entry("2", "b1", room="ROOM2", email="other@example.com"),
            _entry("3", "a2"),
        ]
        seen: list[tuple[str, str]] = []

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            seen.append((inbound.room_id, inbound.text))

        with mock.patch.object(type(d), "handle_message", _replay):
            await d._drain_queue("KEY", _inbound("opener"))

        assert seen == [
            ("ROOM", "a1"),
            ("ROOM2", "b1"),
            ("ROOM", "a2"),
        ], "each room answered in arrival order, and A's second message did not jump B"


class TestWebexSharesTheQueueWithOtherTransports:
    """This channel is not alone on its queue, and a foreign entry must not be answered.

    Every DM dispatcher is handed the orchestrator's single ``SessionManager``, and under
    ``dm_scope = "unified"`` ``build_dm_session_key`` drops the CHANNEL from the bucket, so
    a Webex room and a Telegram DM to one agent resolve to one session key and one queue.
    Ownership is therefore decided before any ``webex_`` field is read. Reading
    ``webex_room_id`` with a DEFAULT instead would yield ``""`` for a foreign entry, and
    that empty string -- being the first entry read -- would become THE room for the whole
    turn: another transport's text answered into an empty room under an empty identity, and
    consumed off the queue while doing it.
    """

    @staticmethod
    def _foreign(text: str = "from telegram") -> tuple[str, str, dict]:
        """A queue entry as the TELEGRAM producer records one, built by that producer."""
        from kiro_crew.telegram.transport_dispatch import _origin_kwargs as tg_kwargs
        from kiro_crew.telegram.transport_dispatch import _QueuedOrigin as TgOrigin

        origin = TgOrigin(user_id="7", chat_id="70", thread_id="", chat_type="private", username="")
        return ("t0", text, tg_kwargs(origin))

    @pytest.mark.asyncio
    async def test_the_producer_tags_its_entries_and_records_the_room_type(self) -> None:
        """The tag names the owner to wake; the room type decides the replay's session key.

        ``_route_of`` reads ``room_type`` to choose between a person and a space, so an
        entry that did not carry it could only be replayed onto the OPENER's routing --
        a different session key whenever the two differ.
        """
        from kiro_crew.messaging.queue_drain import entry_channel

        sessions = FakeSessions(FakeProvider([]))
        sessions._busy = True
        d = _dispatcher(sessions, FakeCtx(), FakeClient())

        assert await d._enqueue_with_receipt("KEY", "held", _inbound("held"))

        _ts, _text, kwargs = sessions.queued[0]
        assert entry_channel(kwargs) == "webex"
        assert kwargs["webex_room_type"] == "direct"

    @pytest.mark.asyncio
    async def test_a_foreign_entry_is_never_answered_and_never_consumed(self) -> None:
        """The misdelivery fix. The entry stays queued for the channel that owns it."""
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [self._foreign()]
        seen: list = []

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            seen.append((inbound.text, inbound.room_id, inbound.person_email))

        with mock.patch.object(type(d), "handle_message", _replay):
            await d._drain_queue("KEY", _inbound("opener"))

        assert seen == [], "answering it here is the defect; it holds no Webex address"
        assert [t for _ts, t, _kw in sessions.queued] == ["from telegram"], "and it is kept"

    @pytest.mark.asyncio
    async def test_a_foreign_entry_does_not_strand_this_channels_own_message(self) -> None:
        """Set aside WITHOUT deferring the rest, so our queue is not blocked behind theirs.

        Their entry is first, and a drain that stopped at it would leave our own accepted
        message unanswered until that other transport spoke again.
        """
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [self._foreign(), _entry("1", "mine", room="ROOM_MINE")]
        seen: list = []

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            seen.append((inbound.text, inbound.room_id))

        with mock.patch.object(type(d), "handle_message", _replay):
            await d._drain_queue("KEY", _inbound("opener"))

        assert seen == [("mine", "ROOM_MINE")], "ours answers, in ITS OWN room"
        assert [t for _ts, t, _kw in sessions.queued] == ["from telegram"]

    @pytest.mark.asyncio
    async def test_the_drain_is_registered_and_answers_with_no_opening_envelope(self) -> None:
        """A peer wakes this drain with the session key alone.

        There is no inbound then -- the finished turn belonged to another transport -- so
        every addressing field comes from the queued entry's own recorded place.
        """
        from kiro_crew.messaging import queue_drain

        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [_entry("1", "answer me", room="ROOM_SOLO", email="solo@example.com")]
        seen: list = []

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            seen.append((inbound.text, inbound.room_id, inbound.person_email, inbound.room_type))

        assert queue_drain._DRAINS.get("webex") is not None, "a peer must be able to wake it"
        with mock.patch.object(type(d), "handle_message", _replay):
            await queue_drain._DRAINS["webex"]("KEY")

        assert seen == [("answer me", "ROOM_SOLO", "solo@example.com", "direct")]

    @pytest.mark.asyncio
    async def test_the_receipt_is_flipped_in_the_room_that_holds_its_bubble(self) -> None:
        """The bubble belongs to whoever queued first, not to whoever opened the turn.

        ``edit_receipt`` carries the room id, so editing under the opener's address reaches
        a different room, where that message id does not exist.
        """
        sessions = FakeSessions(FakeProvider([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        sessions._busy = True
        queuer = WebexInbound(
            person_email="q@example.com", room_id="ROOM_QUEUER", text="held", room_type="direct"
        )

        assert await d._enqueue_with_receipt("KEY", "held", queuer)
        sessions._busy = False

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            return None

        with mock.patch.object(type(d), "handle_message", _replay):
            await d._drain_queue("KEY", _inbound("opener"))

        flips = [room for (_mid, room, body) in client.edits if "answering" in body.lower()]
        assert flips == ["ROOM_QUEUER"], "the opener's room would be the wrong room"

    @pytest.mark.asyncio
    async def test_the_drain_wakes_the_owner_of_an_entry_it_set_aside(self) -> None:
        """Setting it aside is only half the fix: something must come back for it.

        The entry was already accepted and receipted, and a drain runs only from the tail of
        its OWN channel's turn -- so without the wake that message waits for its owner to
        finish some unrelated turn, and forever if that user goes quiet there.
        """
        from kiro_crew.messaging import queue_drain

        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [_entry("1", "mine"), self._foreign()]
        woken: list[str] = []

        async def _peer(session_key: str) -> None:
            woken.append(session_key)

        queue_drain.register_drain("telegram", _peer)

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            return None

        with mock.patch.object(type(d), "handle_message", _replay):
            await d._drain_queue("KEY", _inbound("opener"))

        assert woken == ["KEY"], "the channel that owns the set-aside entry must be woken"

    def test_an_owned_entry_with_no_room_is_a_producer_bug_not_an_empty_reply(self) -> None:
        """An empty room on an entry claiming THIS channel raises instead of addressing "".

        Answering into ``""`` is the silent misdelivery this whole contract exists to stop,
        so the loud failure is the correct outcome. Both producers are in the module, and
        the queue is an in-process list on a live session, so there is no lenient case.
        """
        from kiro_crew.messaging.queue_drain import tag_entry

        entry = tag_entry(
            {
                "webex_room_id": "",
                "webex_parent_id": "",
                "webex_person_email": "a@example.com",
                "webex_room_type": "direct",
            },
            "webex",
            "",
        )

        with pytest.raises(KeyError) as caught:
            webex_dispatch._queued_place(entry)
        assert "webex_room_id" in str(caught.value), "the error must name what is missing"

    @pytest.mark.asyncio
    async def test_the_receipt_counts_only_the_answered_rooms_own_deferrals(self) -> None:
        """A foreign entry is not this room's deferred message."""
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [_entry("1", "mine"), self._foreign()]
        deferred: list[int] = []

        async def _flip(session_key, surface, answered, n=0, *, owner):
            deferred.append(n)

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            return None

        with mock.patch.object(d._queue, "flip_answering_locked", _flip):
            with mock.patch.object(type(d), "handle_message", _replay):
                await d._drain_queue("KEY", _inbound("opener"))

        assert deferred == [0], "their entry is set aside, but it is not THIS room's deferral"

    @pytest.mark.asyncio
    async def test_the_count_also_excludes_another_webex_rooms_entry(self) -> None:
        """A foreign entry is excluded because it carries no place this channel can read.

        Another Webex room is the harder case: the entry is owned, its room is readable,
        and it is still not this room's deferral. It drains as its own turn in its own
        room, so counting it here promises this room a follow-up it is not owed.
        """
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [
            _entry("1", "mine"),
            _entry("2", "theirs", room="ROOM2", email="other@example.com"),
        ]
        deferred: list[int] = []

        async def _flip(session_key, surface, answered, n=0, *, owner):
            deferred.append(n)

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            return None

        with mock.patch.object(d._queue, "flip_answering_locked", _flip):
            with mock.patch.object(type(d), "handle_message", _replay):
                await d._drain_queue("KEY", _inbound("opener"))

        # The pump loops, so the other room's entry drains as its own turn. Each receipt
        # reports zero, because neither room has a message of its own left waiting.
        assert deferred == [0, 0], "owned, readable, and still not the other room's deferral"
