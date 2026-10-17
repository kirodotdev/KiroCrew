"""Telegram queue drain: who sent each queued message, and what the drain replays.

Covers the receipt text cap, the record a queue entry carries about its sender and
destination, the drain answering a queue two people share as two turns, a partial drain
inside one forum topic, and the exact arguments each collapsed turn is replayed with.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import _dispatcher, _origin, _prime_live, _tg_origin

from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.telegram.transport import TelegramInboundMessage
from kiro_crew.telegram.transport_dispatch import (
    _NOT_A_SENDER,
    _inbound_origin,
    _origin_kwargs,
    _queued_origin,
    _QueuedOrigin,
)


def test_receipt_text_caps_displayed_items() -> None:
    # A large mid-turn burst must not grow the rendered receipt unbounded: only
    # the first _RECEIPT_MAX_ITEMS are listed verbatim; the count stays true.
    from kiro_crew.messaging.queue_receipt import RECEIPT_MAX_ITEMS as _RECEIPT_MAX_ITEMS
    from kiro_crew.messaging.queue_receipt import receipt_text as _receipt_text

    texts = [f"msg number {i}" for i in range(_RECEIPT_MAX_ITEMS + 3)]
    out = _receipt_text(texts)
    surplus = len(texts) - _RECEIPT_MAX_ITEMS
    assert f"({len(texts)})" in out  # count prefix shows the true total
    assert f"…and {surplus} more" in out  # surplus collapsed, not listed verbatim
    assert texts[-1] not in out  # a beyond-cap item is not rendered verbatim


class TestDrainSenderIdentity:
    """A queue shared by two people must not be answered as one person.

    Under ``messaging.dm_scope = "unified"`` every allow-listed person's direct chat
    collapses into one session key -- ``build_dm_session_key`` reduces the bucket to
    ``unified:{agent}``, dropping both channel and user -- so ONE queue holds messages
    from several senders. A combined turn carries ONE envelope, so it may only combine
    messages that share one.
    """

    _KEY = "unified:kirocrew"

    @staticmethod
    def _msg(user: int, chat: int, text: str = "", *, message_id: int = 0) -> Any:
        return TelegramInboundMessage(
            channel_type="telegram",
            user_id=str(user),
            conversation_id=str(chat),
            text=text,
            message_id=message_id,
            chat_type="private",
        )

    def _queue(self, d: Any, sess: Any, *msgs: Any) -> None:
        """Queue each message through the REAL enqueue, mid-turn.

        End to end through the production writer, so the recorder and the reader are
        covered together: an origin nothing reads back is not a fix, and an origin a
        fixture spells by hand is not evidence production records one.
        """

        async def _go() -> None:
            sess._busy = True
            for msg in msgs:
                assert await d._enqueue_with_receipt(
                    self._KEY,
                    int(msg.conversation_id),
                    msg.text,
                    origin=_inbound_origin(msg),
                ), "the fake session must accept a mid-turn enqueue"
            sess._busy = False  # the turn they queued behind has finished

        asyncio.run(_go())

    @staticmethod
    def _drain(d: Any, key: str) -> list[Any]:
        """Drain, returning the envelope every replayed turn ran under."""
        seen: list[Any] = []
        original = d.handle_message

        async def _spy(msg: Any, **kw: Any) -> None:
            seen.append(msg)

        async def _go() -> None:
            d.handle_message = _spy
            try:
                await d._drain_queue(key)
            finally:
                d.handle_message = original

        asyncio.run(_go())
        return seen

    def test_the_receipt_counts_only_the_answered_senders_own_deferrals(self) -> None:
        """ "+N deferred" is a promise TO ONE PERSON, so it may only count their messages.

        ``len(remainder)`` also counts the other sender's entries and any entry another
        TRANSPORT recorded. Each of those drains in its own turn, in its own chat, so
        showing them here tells this person to expect a follow-up for text they never
        wrote -- and when their own burst fit in one turn, their true count is zero.
        """
        d, _cli, sess = _dispatcher({7, 8}, dm_scope="unified")
        deferred: list[int] = []

        async def _flip(
            session_key: str, chat_id: int, answered: list[str], n: int = 0, owner: str = ""
        ) -> None:
            deferred.append(n)

        d._receipt_flip_locked = _flip
        self._queue(
            d,
            sess,
            self._msg(7, 70, "mine", message_id=11),
            self._msg(8, 80, "theirs", message_id=12),
        )

        self._drain(d, self._KEY)

        assert deferred == [0, 0], "neither sender has a deferral of their OWN"

    def test_a_senders_own_surplus_is_still_counted(self) -> None:
        """The guard against fixing the count by always reporting zero."""
        from kiro_crew.telegram.transport_dispatch import _MAX_COLLAPSE

        d, _cli, sess = _dispatcher({7}, dm_scope="unified")
        deferred: list[int] = []

        async def _flip(
            session_key: str, chat_id: int, answered: list[str], n: int = 0, owner: str = ""
        ) -> None:
            deferred.append(n)

        d._receipt_flip_locked = _flip
        self._queue(
            d,
            sess,
            *(self._msg(7, 70, f"m{i}", message_id=i) for i in range(_MAX_COLLAPSE + 2)),
        )

        self._drain(d, self._KEY)

        assert deferred[0] == 2, "both of this sender's own surplus messages are theirs"

    def test_two_senders_on_one_queue_drain_as_two_turns(self) -> None:
        """Each drained turn names the sender who wrote its text, in its own chat."""
        d, cli, sess = _dispatcher({7, 8}, dm_scope="unified")
        self._queue(
            d,
            sess,
            self._msg(7, 70, "mine", message_id=11),
            self._msg(8, 80, "and mine", message_id=12),
        )

        seen = self._drain(d, self._KEY)

        assert [m.text for m in seen] == ["mine", "and mine"], "one turn each, FIFO order"
        assert [m.user_id for m in seen] == ["7", "8"], "the turn must name its own author"
        assert [m.conversation_id for m in seen] == ["70", "80"], "and answer in their own chat"
        assert sess.queued == [], "the pump must drain the deferred entry too, not strand it"

    def test_one_senders_burst_with_distinct_message_ids_still_collapses(self) -> None:
        """The ordinary case is unchanged: one person's burst is ONE turn.

        The two messages carry DISTINCT ``message_id`` values, because Telegram mints
        one per message and two real messages never share one. Grouping on a
        per-message identifier is the trap: it makes one person's own burst compare
        unequal, so the collapse stops firing and every burst drains as N turns.
        """
        d, cli, sess = _dispatcher({7}, dm_scope="unified")
        first = self._msg(7, 70, "first", message_id=11)
        second = self._msg(7, 70, "second", message_id=12)
        assert first.message_id != second.message_id, "the point of this test"
        self._queue(d, sess, first, second)

        seen = self._drain(d, self._KEY)

        assert [m.text for m in seen] == ["first\n\nsecond"], "the burst must still collapse"
        assert [m.user_id for m in seen] == ["7"]
        assert [m.conversation_id for m in seen] == ["70"]

    def test_a_changed_handle_mid_burst_does_not_split_the_turn(self) -> None:
        """``username`` is a mutable label for a sender ``user_id`` already pins.

        It rides on the origin so the replay is faithful, and stays OUT of the collapse
        key: a handle changed between two messages would otherwise split one person's
        burst into two turns.
        """
        d, cli, sess = _dispatcher({7}, dm_scope="unified")
        before = self._msg(7, 70, "first", message_id=11)
        before.username = "ray"
        after = self._msg(7, 70, "second", message_id=12)
        after.username = "raymond"
        self._queue(d, sess, before, after)

        seen = self._drain(d, self._KEY)

        assert [m.text for m in seen] == ["first\n\nsecond"], "a renamed sender is still one sender"
        # The replay carries the FIRST entry's handle, which is the envelope it runs
        # under -- not a merge of the two.
        assert seen[0].username == "ray"

    def test_a_third_sender_behind_two_does_not_jump_the_queue(self) -> None:
        """A differing sender defers itself AND everything behind it, so FIFO is exact."""
        d, cli, sess = _dispatcher({7, 8}, dm_scope="unified")
        self._queue(
            d,
            sess,
            self._msg(7, 70, "a", message_id=11),
            self._msg(8, 80, "b", message_id=12),
            self._msg(7, 70, "c", message_id=13),
        )

        seen = self._drain(d, self._KEY)

        # "c" is the same sender as "a", but it arrived AFTER "b": collapsing it into
        # the first turn would answer it ahead of a message that was queued earlier.
        assert [(m.user_id, m.text) for m in seen] == [("7", "a"), ("8", "b"), ("7", "c")]

    def test_a_deferred_entry_keeps_its_own_origin_when_requeued(self) -> None:
        """The re-enqueue must carry the origin, or the bug returns one iteration later."""
        d, cli, sess = _dispatcher({7, 8}, dm_scope="unified")
        self._queue(
            d,
            sess,
            self._msg(7, 70, "mine", message_id=11),
            self._msg(8, 80, "theirs", message_id=12),
        )
        requeued: list[dict] = []
        real_enqueue = sess.enqueue

        def _spy(k: str, ts: str, text: str, **kw: Any) -> bool:
            requeued.append(dict(kw))
            return real_enqueue(k, ts, text, **kw)

        sess.enqueue = _spy  # type: ignore[method-assign]

        self._drain(d, self._KEY)

        assert requeued, "the differing sender's entry must be re-enqueued, not dropped"
        # Read back through the production reader rather than by spelling the storage
        # keys, so renaming one cannot leave this test passing.
        assert _queued_origin(requeued[0]) == _tg_origin(8, 80)

    def test_the_receipt_is_flipped_in_the_chat_that_holds_its_bubble(self) -> None:
        """The bubble belongs to whoever queued first, not to whoever opened the turn."""
        d, cli, sess = _dispatcher({7, 8}, dm_scope="unified")
        d.cfg.messaging.queue_mode = "queue"
        _prime_live(d.cfg)
        self._queue(d, sess, self._msg(8, 80, "held", message_id=12))
        assert cli.send_chats and cli.send_chats[-1] == 80, "the receipt bubble lives in chat 80"

        self._drain(d, self._KEY)

        flips = [
            chat
            for chat, (_mid, text, _markup) in zip(cli.edit_chats, cli.edits)
            if "Now answering" in text
        ]
        assert flips, "the drain must flip the receipt"
        assert flips[0] == 80, "editing under another chat's address cannot land"

    def test_a_queued_forum_message_replays_under_its_own_topic(self) -> None:
        """The Topic rides on the entry, so a forum queue keeps its route.

        A forum route never collapses into the unified bucket (``build_dm_session_key``
        keeps its full ``{channel}:{agent}:{chat_type}:{user}`` bucket regardless of
        ``dm_scope``), so this pins that recording the route per entry did not lose it.
        """
        d, cli, sess = _dispatcher({7}, dm_scope="unified")
        forum = self._msg(7, -1001234567890, "in the topic", message_id=11)
        forum.chat_type = "supergroup"
        forum.thread_id = "5"
        self._queue(d, sess, forum)

        seen = self._drain(d, self._KEY)

        assert [m.text for m in seen] == ["in the topic"]
        assert seen[0].chat_type == "supergroup"
        assert seen[0].thread_id == "5"
        assert seen[0].conversation_id == "-1001234567890"

    def test_the_collapse_key_drops_only_the_mutable_handle(self) -> None:
        """The collapse key is every origin field except the ones that are not identity.

        Derived from ``_fields`` rather than restated, so adding a field to
        ``_QueuedOrigin`` joins the key by default: a WHO field left out would let two
        people's messages collapse under one identity, while a surplus field only costs
        a collapse. The exclusion set is pinned because widening it is how that
        identity bug would return.
        """
        assert _NOT_A_SENDER == {"username"}
        key_fields = tuple(n for n in _QueuedOrigin._fields if n not in _NOT_A_SENDER)
        assert key_fields == ("user_id", "chat_id", "thread_id", "chat_type")
        origin = _tg_origin(7, 70, username="ray")
        assert origin.sender_key == tuple(getattr(origin, n) for n in key_fields)
        # Same person and chat, renamed: equal keys, unequal origins.
        renamed = origin._replace(username="raymond")
        assert renamed.sender_key == origin.sender_key
        assert renamed != origin
        # Different person, and the same person in a different place: unequal keys.
        assert origin._replace(user_id="8").sender_key != origin.sender_key
        assert origin._replace(chat_id="80").sender_key != origin.sender_key
        assert origin._replace(thread_id="5").sender_key != origin.sender_key
        assert origin._replace(chat_type="supergroup").sender_key != origin.sender_key

    def test_an_entry_another_transport_recorded_is_deferred_not_lost(self) -> None:
        """One queue can hold two transports, and neither may answer the other's.

        Every DM dispatcher is built with the orchestrator's single ``SessionManager``,
        and ``build_dm_session_key(..., dm_scope="unified", chat_type="direct")``
        returns ``unified:{agent}`` for EVERY channel -- it drops the channel as well
        as the user -- so a Telegram DM and a Discord DM to the same agent share one
        queue. A Discord-recorded entry carries no field Telegram can address, so
        answering it here would post one transport's reply into another's conversation,
        and raising on it would discard every message already dequeued this iteration.
        """
        d, cli, sess = _dispatcher({7}, dm_scope="unified")
        foreign = {"discord_user_id": "u1", "discord_channel_id": "c1", "discord_thread_id": ""}
        assert _queued_origin(foreign) is None, "not this channel's entry to read"
        sess.queued = [("t0", "theirs", dict(foreign))]

        seen = self._drain(d, self._KEY)

        assert seen == [], "Telegram must not answer a Discord-recorded message"
        assert [text for _ts, text, _kw in sess.queued] == ["theirs"], "and must not lose it"
        assert sess.queued[0][2] == foreign, "re-enqueued verbatim, for its own drain"

    def test_a_foreign_entry_does_not_block_this_channels_own_messages(self) -> None:
        """It steps aside rather than holding the queue: order is per sender, not global.

        Blocking this channel's queue behind a foreign entry would strand it whenever
        the other transport sends nothing further, and FIFO between two transports is
        not something either sender can observe -- they are in different apps.
        """
        d, cli, sess = _dispatcher({7}, dm_scope="unified")
        self._queue(d, sess, self._msg(7, 70, "mine", message_id=11))
        foreign = {"discord_user_id": "u1", "discord_channel_id": "c1", "discord_thread_id": ""}
        sess.queued.insert(0, ("t-first", "theirs", dict(foreign)))

        seen = self._drain(d, self._KEY)

        assert [m.text for m in seen] == ["mine"], "the foreign entry ahead of it must not block"
        assert [text for _ts, text, _kw in sess.queued] == ["theirs"], "and stays for its own drain"

    def test_a_partly_recorded_own_entry_is_a_producer_bug_not_a_fallback(self) -> None:
        """An incomplete origin from THIS channel raises instead of guessing an address.

        Both producers are in this module -- ``_enqueue_with_receipt`` and the drain's
        own re-enqueue, which passes the entry's payload straight back -- so a partial
        record can only mean a change here dropped a field. Defaulting to empty strings
        would address the reply to an empty chat id, a silent misdelivery.

        Ownership is read off the NEUTRAL channel field, which is why an entry can be
        "mine, and broken" at all: without it, a missing field would be indistinguishable
        from another transport's entry and would be silently set aside forever.
        """
        with pytest.raises(KeyError) as caught:
            _queued_origin({"queued_channel": "telegram", "telegram_user_id": "7"})
        assert "telegram_chat_id" in str(caught.value), "the error must name what is missing"

        # An entry naming no channel, or another one, is the OTHER case: not this
        # dispatcher's, deferred rather than raised on.
        assert _queued_origin({}) is None
        assert _queued_origin({"queued_channel": "discord"}) is None

    def test_the_enqueued_entry_records_the_senders_own_origin(self) -> None:
        """Nothing downstream can recover an origin the entry never carried."""
        d, cli, sess = _dispatcher({7}, dm_scope="unified")
        self._queue(d, sess, self._msg(7, 70, "hello", message_id=11))

        assert _queued_origin(sess.queued[0][2]) == _tg_origin(7, 70)


class TestQueuedOriginRecord:
    """The exact record a queue entry carries about who sent it and where it goes.

    Every fixture in this file spells entries through the production writer, so a
    changed prefix, owner spelling or field order would stay green there. These pin
    the bytes themselves.
    """

    def test_origin_kwargs_has_the_exact_shape(self) -> None:
        from kiro_crew.messaging.queue_drain import (
            QUEUED_CHANNEL_KEY,
            QUEUED_OWNER_KEY,
            owner_token,
        )

        origin = _tg_origin(7, 70, username="ray")
        first = _origin_kwargs(origin)
        assert first == {
            "telegram_user_id": "7",
            "telegram_chat_id": "70",
            "telegram_thread_id": "",
            "telegram_chat_type": "private",
            "telegram_username": "ray",
            QUEUED_CHANNEL_KEY: "telegram",
            QUEUED_OWNER_KEY: owner_token("telegram", ("7", "70", "", "private")),
        }
        assert _origin_kwargs(origin) is not first, "each entry gets its own dict"

    def test_the_owner_token_is_the_sender_key_without_the_handle(self) -> None:
        from kiro_crew.messaging.queue_drain import owner_token
        from kiro_crew.telegram.transport_dispatch import _entry_owner

        assert _entry_owner(_tg_origin(7, 70, username="ray")) == owner_token(
            "telegram", ("7", "70", "", "private")
        )
        assert _entry_owner(_tg_origin(7, 70, username="ray")) == _entry_owner(
            _tg_origin(7, 70, username="renamed")
        )
        assert _entry_owner(_tg_origin(7, 70)) != _entry_owner(_tg_origin(8, 70))
        forum = _tg_origin(7, -100123, thread="5", chat_type="supergroup")
        assert _entry_owner(forum) == owner_token("telegram", ("7", "-100123", "5", "supergroup"))

    def test_inbound_origin_reads_plain_and_telegram_messages(self) -> None:
        plain = InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="x")
        assert _inbound_origin(plain) == _QueuedOrigin("7", "7", "", "private", "")
        topic = TelegramInboundMessage(
            channel_type="telegram",
            user_id="7",
            conversation_id="-100123",
            text="x",
            thread_id="5",
            chat_type="supergroup",
            username="ray",
        )
        assert _inbound_origin(topic) == _QueuedOrigin("7", "-100123", "5", "supergroup", "ray")
        no_thread = TelegramInboundMessage(
            channel_type="telegram", user_id="7", conversation_id="7", text="x", thread_id=None
        )
        assert _inbound_origin(no_thread).thread_id == ""

    def test_queued_origin_round_trips_and_coerces_none(self) -> None:
        origin = _tg_origin(7, -100123, thread="5", chat_type="supergroup", username="ray")
        assert _queued_origin(_origin_kwargs(origin)) == origin
        kwargs = _origin_kwargs(origin)
        kwargs["telegram_thread_id"] = None  # type: ignore[assignment]
        assert _queued_origin(kwargs) == origin._replace(thread_id="")


class TestPartialDrainInAForumTopic:
    """One Topic, several members, one bubble: a drain answers ONE member's lines.

    Every member of a forum Topic shares its chat address and its session key, so
    their queued messages share one receipt bubble. The drain that answers one
    member must leave the other members' lines -- and the entry that is their only
    handle on the bubble -- in place until their own turn runs.
    """

    _CHAT = -1001234567890
    _KEY = "telegram:kirocrew:forum:-1001234567890:5"

    def _member(self, user: int) -> _QueuedOrigin:
        return _tg_origin(user, self._CHAT, thread="5", chat_type="supergroup")

    def _queue_in_topic(self, d: Any, sess: Any, *entries: tuple[int, str]) -> None:
        async def _go() -> None:
            sess._busy = True
            for user, text in entries:
                assert await d._enqueue_with_receipt(
                    self._KEY, self._CHAT, text, thread=5, origin=self._member(user)
                )
            sess._busy = False

        asyncio.run(_go())

    def test_one_members_drain_leaves_the_other_members_line(self) -> None:
        from kiro_crew.messaging.queue_receipt import receipt_text

        d, cli, sess = _dispatcher({7, 8})
        self._queue_in_topic(d, sess, (7, "alice asked"), (8, "bob asked"))

        seen = TestDrainSenderIdentity._drain(d, self._KEY)

        assert [(m.user_id, m.text) for m in seen] == [("7", "alice asked"), ("8", "bob asked")]
        assert [t for t, _ in cli.sent] == [receipt_text(["alice asked"])]
        assert cli.send_chats == [self._CHAT] and cli.send_threads == [5]
        assert [(mid, txt) for mid, txt, _ in cli.edits] == [
            (101, receipt_text(["alice asked", "bob asked"])),
            (101, receipt_text(["bob asked"])),
            (101, receipt_text(["bob asked"], answering=True)),
        ]
        assert cli.edit_chats == [self._CHAT] * 3
        assert not d._queue.has_receipt(self._KEY)

    def test_own_deferred_decides_which_of_a_members_lines_drop(self) -> None:
        from kiro_crew.messaging.queue_receipt import receipt_text

        d, cli, sess = _dispatcher({7, 8})
        self._queue_in_topic(d, sess, (7, "a"), (8, "b"), (7, "c"))

        seen = TestDrainSenderIdentity._drain(d, self._KEY)

        assert [(m.user_id, m.text) for m in seen] == [("7", "a"), ("8", "b"), ("7", "c")]
        assert [txt for _mid, txt, _ in cli.edits] == [
            receipt_text(["a", "b"]),
            receipt_text(["a", "b", "c"]),
            receipt_text(["b", "c"]),
            receipt_text(["c"]),
            receipt_text(["c"], answering=True),
        ]

    def test_each_iteration_flips_with_its_own_owner_and_chat(self) -> None:
        from kiro_crew.telegram.transport_dispatch import _entry_owner

        d, _cli, sess = _dispatcher({7, 8}, dm_scope="unified")
        key = TestDrainSenderIdentity._KEY
        calls: list[tuple[Any, ...]] = []

        async def _flip(
            session_key: str, chat_id: int, answered: list[str], n: int = 0, owner: str = ""
        ) -> None:
            calls.append((session_key, chat_id, list(answered), n, owner, d._queue.lock.locked()))

        d._receipt_flip_locked = _flip  # type: ignore[method-assign]
        TestDrainSenderIdentity()._queue(
            d,
            sess,
            TestDrainSenderIdentity._msg(7, 70, "a"),
            TestDrainSenderIdentity._msg(8, 80, "b"),
            TestDrainSenderIdentity._msg(7, 70, "c"),
        )

        TestDrainSenderIdentity._drain(d, key)

        assert calls == [
            (key, 70, ["a"], 1, _entry_owner(_tg_origin(7, 70)), True),
            (key, 80, ["b"], 0, _entry_owner(_tg_origin(8, 80)), True),
            (key, 70, ["c"], 0, _entry_owner(_tg_origin(7, 70)), True),
        ]


class TestDrainReplayContract:
    """What the drain hands ``handle_message`` for each collapsed turn."""

    _KEY = "telegram:kirocrew:direct:7"

    @staticmethod
    def _replays(d: Any, key: str) -> list[tuple[Any, dict[str, Any]]]:
        seen: list[tuple[Any, dict[str, Any]]] = []
        original = d.handle_message

        async def _spy(msg: Any, **kw: Any) -> None:
            seen.append((msg, kw))

        async def _go() -> None:
            d.handle_message = _spy
            try:
                await d._drain_queue(key)
            finally:
                d.handle_message = original

        asyncio.run(_go())
        return seen

    def test_the_replay_keywords_are_exact(self) -> None:
        d, _cli, sess = _dispatcher({7})
        sess.queued = [("t", "hi", _origin())]

        seen = self._replays(d, self._KEY)

        assert [kw for _msg, kw in seen] == [
            {"drain": False, "interpret_commands": False, "privacy_request": ""}
        ]

    def test_a_dm_replay_envelope_has_the_documented_defaults(self) -> None:
        d, _cli, sess = _dispatcher({7})
        sess.queued = [("t", "hi", _origin())]

        msg = self._replays(d, self._KEY)[0][0]

        assert type(msg) is TelegramInboundMessage
        assert (
            msg.channel_type,
            msg.user_id,
            msg.conversation_id,
            msg.text,
            msg.thread_id,
            msg.chat_type,
            msg.username,
        ) == ("telegram", "7", "7", "hi", None, "private", "")
        assert msg.message_id == 0 and msg.attachments == []
        assert msg.from_widget is False

    def test_the_strictest_request_of_one_senders_burst_rides_the_replay(self) -> None:
        d, _cli, sess = _dispatcher({7})
        sess.queued = [
            ("1", "a", _origin()),
            ("2", "b", {"privacy_request": "incognito", **_origin()}),
            ("3", "c", {"privacy_request": "temporary", **_origin()}),
        ]

        seen = self._replays(d, self._KEY)

        assert [(m.text, kw["privacy_request"]) for m, kw in seen] == [("a\n\nb\n\nc", "temporary")]

    def test_one_senders_request_never_reaches_another_senders_turn(self) -> None:
        d, _cli, sess = _dispatcher({7, 8}, dm_scope="unified")
        sess.queued = [
            ("1", "a", {"privacy_request": "temporary", **_origin(7, 70)}),
            ("2", "b", _origin(8, 80)),
        ]

        seen = self._replays(d, TestDrainSenderIdentity._KEY)

        assert [(m.user_id, kw["privacy_request"]) for m, kw in seen] == [
            ("7", "temporary"),
            ("8", ""),
        ]

    def test_unknown_and_non_string_requests_are_dropped(self) -> None:
        for value in (123, "bogus"):
            d, _cli, sess = _dispatcher({7})
            sess.queued = [("1", "a", {"privacy_request": value, **_origin()})]
            seen = self._replays(d, self._KEY)
            assert [kw["privacy_request"] for _m, kw in seen] == [""], value

    def test_a_queued_command_is_literal_content_on_drain(self) -> None:
        d, cli, sess = _dispatcher({7})
        route = ("direct", "7")
        gen = d._conv.current_gen(route)
        sess.queued = [("t", "/new", _origin())]

        asyncio.run(d._drain_queue(self._KEY))

        assert d._conv.current_gen(route) == gen
        assert not any("New conversation started" in t for t, _ in cli.sent)
        assert "Answer: /new" in (cli.final_text() or "")
        assert sess.successes == [self._KEY]

    def test_a_drained_turn_replies_without_quoting_any_one_message(
        self,
    ) -> None:
        d, cli, sess = _dispatcher({7})
        sess.queued = [("t", "hi", _origin())]

        asyncio.run(d._drain_queue(self._KEY))

        assert sess.successes == [self._KEY]
        assert all(target is None for target in cli.reply_targets)
