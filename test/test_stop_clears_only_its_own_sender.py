"""``/stop`` clears the CALLER's queued messages, not the whole shared queue.

Under ``messaging.dm_scope = "unified"`` ``build_dm_session_key`` reduces a direct
chat's bucket to ``unified:{agent}``, dropping the channel and the user, so every
allow-listed person's DM on every transport resolves to one session key and therefore
one queue. Three layers have to agree for one person's Stop to leave everybody else's
messages alone, so each is pinned here:

* the QUEUE -- ``clear_queue`` with an ownership predicate keeps what it does not match,
  including entries another transport recorded and entries nobody claimed;
* the RECEIPT -- a bubble still carrying other people's lines is rewritten as queued
  rather than finalized to a cancellation they never asked for;
* the CALL SITES -- every ``/stop`` handler passes an owner, so a channel wired up
  later cannot inherit the whole-queue clear by leaving it out.

The enumeration is deliberate rather than a sample: the predicate has to answer for a
second sender, a second transport, an untagged producer, the same person in a different
place, and an owner it cannot name at all, and each of those is a different way to
discard somebody's message.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from typing import Any

import pytest

from kiro_crew.messaging.commands import stop_running_turn
from kiro_crew.messaging.queue_drain import (
    QUEUED_CHANNEL_KEY,
    QUEUED_OWNER_KEY,
    entries_queued_by,
    entry_owner,
    owner_token,
    tag_entry,
)
from kiro_crew.messaging.queue_receipt import (
    QueueReceipt,
    ReceiptLine,
    ReceiptQueue,
    receipt_address_key,
    receipt_text,
)

#: Two allow-listed people on one transport, and a third on another. Under a unified
#: scope all three land on ONE session key, which is the whole premise.
ALICE = owner_token("telegram", ("u-alice", "chat-alice", "", "private"))
BOB = owner_token("telegram", ("u-bob", "chat-bob", "", "private"))
CARLA = owner_token("discord", ("u-carla", "dm-carla", ""))


# ── the queue layer ─────────────────────────────────────────────────────────


class _Deps:
    """The one dependency the allocation boundary uses on a queue entry."""

    def __init__(self) -> None:
        self.unlinked: list[str] = []

    def unlink_queued_temp_paths(self, kwargs: dict) -> None:
        self.unlinked.append(str(kwargs.get("mark", "")))


def _manager() -> tuple[Any, Any, _Deps]:
    """A real ``SessionManager`` queue with one session on the shared key.

    The allocation boundary is exercised through the manager rather than reached into,
    because the manager's signature is what every channel calls.
    """
    from kiro_crew.session import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    boundary = mgr._allocation_boundary()
    deps = _Deps()
    boundary._deps = deps  # type: ignore[attr-defined]
    return mgr, boundary, deps


def _queued(owner: str, mark: str, channel: str = "telegram") -> dict:
    """One entry's keyword arguments, tagged the way a producer tags them."""
    return tag_entry({"mark": mark}, channel, owner)  # type: ignore[arg-type]


class _Session:
    """The queue-carrying parts of a session, which is all ``clear_queue`` reads."""

    def __init__(self, entries: list[tuple[str, str, dict]]) -> None:
        from collections import deque

        self.queue = deque(entries)
        self.cancelled: set[str] = set()


def _clear(
    entries: list[tuple[str, str, dict]], owned_by: Any
) -> tuple[list[str], _Deps, _Session]:
    """Run a clear over *entries* and report the marks that survived."""
    mgr, boundary, deps = _manager()
    session = _Session(entries)
    boundary._sessions = {"unified:agent": session}  # type: ignore[attr-defined]
    mgr._fold_key = lambda key: key  # type: ignore[assignment,method-assign]
    boundary.clear_queue("unified:agent", owned_by)
    return [kwargs["mark"] for _, _, kwargs in session.queue], deps, session


def _burst() -> list[tuple[str, str, dict]]:
    """Alice, Bob and Carla each holding one message on the one shared queue."""
    return [
        ("1", "alice asked", _queued(ALICE, "a")),
        ("2", "bob asked", _queued(BOB, "b")),
        ("3", "carla asked", _queued(CARLA, "c", channel="discord")),
    ]


class TestTheQueueKeepsWhatIsNotTheCallers:
    def test_a_second_senders_message_on_the_same_transport_survives(self) -> None:
        """The reported bug. Bob is still waiting for an answer.

        Alice's Stop means "stop MY turn". Bob never typed anything, so discarding his
        queued message is a loss he is not told about: the receipt he was shown flips to
        cancelled and his text is gone.
        """
        survived, _deps, _session = _clear(_burst(), entries_queued_by(ALICE))
        assert survived == ["b", "c"]

    def test_another_transports_message_survives(self) -> None:
        """Carla is on Discord; Alice's Telegram Stop cannot speak for her.

        Her entry carries no field Alice's channel can even read, which is why the owner
        token is neutral: the comparison has to work on an entry from a transport the
        caller knows nothing about.
        """
        survived, _deps, _session = _clear(_burst(), entries_queued_by(ALICE))
        assert "c" in survived

    def test_an_entry_that_named_no_owner_survives(self) -> None:
        """Untagged is nobody's, so it is nobody's to discard.

        Defaulting the other way would let any caller drop an entry it cannot prove is
        its own, which is the loss this change exists to stop.
        """
        entries = [("1", "who queued me", {"mark": "u", QUEUED_CHANNEL_KEY: "telegram"})]
        survived, _deps, _session = _clear(entries, entries_queued_by(ALICE))
        assert survived == ["u"]

    def test_the_same_person_in_a_different_place_survives(self) -> None:
        """The token is sender AND place, because a reply goes to a place.

        Alice in a forum Topic is a different conversation from Alice in her DM: the two
        get separate turns and separate answers, so a Stop in one may not empty the other.
        """
        elsewhere = owner_token("telegram", ("u-alice", "chat-alice", "99", "supergroup"))
        entries = [
            ("1", "in her dm", _queued(ALICE, "dm")),
            ("2", "in the topic", _queued(elsewhere, "topic")),
        ]
        survived, _deps, _session = _clear(entries, entries_queued_by(ALICE))
        assert survived == ["topic"]

    def test_an_unnamed_caller_clears_nothing(self) -> None:
        """An empty owner selects nothing, never everything.

        A caller that cannot say who it is has no claim on anyone's message, and the
        failure that matters here is the destructive one.
        """
        survived, _deps, _session = _clear(_burst(), entries_queued_by(""))
        assert survived == ["a", "b", "c"]

    def test_only_the_dropped_entries_temp_files_are_unlinked(self) -> None:
        """A surviving entry's attachment must still be there when its turn runs."""
        _survived, deps, _session = _clear(_burst(), entries_queued_by(ALICE))
        assert deps.unlinked == ["a"]

    def test_no_predicate_still_clears_the_whole_queue(self) -> None:
        """The whole-session callers are unchanged: ``/new``, teardown, a generation bump.

        Those genuinely retire the queue they are emptying, so narrowing them would leave
        entries on a key nothing will ever drain.
        """
        survived, deps, session = _clear(_burst(), None)
        assert survived == []
        assert deps.unlinked == ["a", "b", "c"]
        assert session.cancelled == set()


class TestTheCancelledMarkersSurviveAPartialClear:
    """``cancelled`` holds bare timestamps with nothing saying whose they are.

    ``cancel_queued`` puts a timestamp there when the message it names is already being
    drained, so ``dequeue`` skips it later. Clearing the set on a partial clear would
    un-cancel a cancel somebody else asked for, and that person's message would then be
    answered after they withdrew it.
    """

    def test_a_partial_clear_leaves_them_alone(self) -> None:
        mgr, boundary, _deps = _manager()
        session = _Session(_burst())
        session.cancelled.add("bobs-withdrawn-ts")
        boundary._sessions = {"unified:agent": session}  # type: ignore[attr-defined]
        mgr._fold_key = lambda key: key  # type: ignore[assignment,method-assign]
        boundary.clear_queue("unified:agent", entries_queued_by(ALICE))
        assert session.cancelled == {"bobs-withdrawn-ts"}

    def test_a_whole_session_clear_still_drops_them(self) -> None:
        mgr, boundary, _deps = _manager()
        session = _Session(_burst())
        session.cancelled.add("bobs-withdrawn-ts")
        boundary._sessions = {"unified:agent": session}  # type: ignore[attr-defined]
        mgr._fold_key = lambda key: key  # type: ignore[assignment,method-assign]
        boundary.clear_queue("unified:agent", None)
        assert session.cancelled == set()


class TestTheTokenAndItsReader:
    def test_a_producer_records_the_owner_beside_the_channel(self) -> None:
        kwargs = _queued(ALICE, "a")
        assert entry_owner(kwargs) == ALICE
        assert kwargs[QUEUED_OWNER_KEY] == ALICE

    def test_two_transports_that_spell_one_id_the_same_are_two_principals(self) -> None:
        """The channel leads the token, so a shared id cannot merge two people."""
        assert owner_token("telegram", ("u1",)) != owner_token("discord", ("u1",))

    def test_a_missing_owner_reads_as_unclaimed_rather_than_raising(self) -> None:
        assert entry_owner({}) == ""


# ── the receipt layer ───────────────────────────────────────────────────────


class _Surface:
    """One conversation's receipt bubble, standing in for one chat.

    ``msg_id`` is the id this conversation hands out for its next bubble. The
    cross-chat tests below give two conversations the SAME id deliberately: per-chat
    message ids are small and dense, so two chats holding the same number is the
    ordinary case, and it is what makes a mis-addressed edit land on an unrelated
    message rather than fail loudly.

    ``address`` is the conversation this surface writes to, and it defaults to the
    label so two differently-named surfaces are two chats. A GROUP SPACE is modelled
    by giving two of them the same ``address`` with different labels: separately built
    surfaces, one room, one valid ``msg_id``.
    """

    def __init__(
        self,
        label: str = "fake",
        msg_id: Any = 7,
        *,
        edit_refuses: bool = False,
        send_fails_after: int | None = None,
        address: str | None = None,
    ) -> None:
        self.label = label
        self._msg_id = msg_id
        #: How many sends succeed before the rest answer None. The bubble's own opening
        #: send is the first, so 1 opens the bubble and then refuses the record POST --
        #: which is what leaves a record still OWED, since a refused edit publishes
        #: immediately and needs no entry kept.
        self._send_fails_after = send_fails_after
        #: Report the refusal a rate-limited chat or a spent per-message edit cap
        #: really answers. A refused finalizing edit is what leaves an entry TERMINAL,
        #: still owing its record, which is the state the cross-chat tests need.
        self.edit_refuses = edit_refuses
        place = label if address is None else address
        self.address_key = receipt_address_key("fake", place) if place else ""
        self.sent: list[str] = []
        self.edits: list[tuple[Any, str]] = []

    async def send_receipt(self, body: str) -> Any | None:
        self.sent.append(body)
        if self._send_fails_after is not None and len(self.sent) > self._send_fails_after:
            return None
        return self._msg_id

    async def edit_receipt(self, msg_id: Any, body: str) -> bool | None:
        self.edits.append((msg_id, body))
        return False if self.edit_refuses else None


async def _bubble(
    queue: ReceiptQueue,
    surface: _Surface,
    lines: list[tuple[str, str]],
    session_key: str = "s",
) -> None:
    """Grow one receipt over *lines*, the way a mid-turn burst grows it."""
    async with queue.lock:
        for owner, text in lines:
            await queue.create_or_grow_locked(session_key, surface, text, owner)


class TestAPartialStopWritesToNobodysSurface:
    """The disclosure rule: a caller may not render another principal's line anywhere.

    Under ``messaging.dm_scope = "unified"`` one session key spans several chats, so
    one bubble's lines can belong to several people, and the registry is keyed on the
    session key alone -- so a transition takes its address from whichever caller
    invoked it. While somebody else's line is on the bubble there is therefore no
    surface a partial stop may write to: the caller's chat is not where the bubble is,
    and the bubble's chat is not the caller's to be told about. It writes nothing.
    """

    def test_a_stop_writes_nothing_at_all_while_another_line_remains(self) -> None:
        """Bob opened the bubble and is still queued; Alice stops. No chat is written.

        Both chats hand out id 7, so an edit through Alice's surface would rewrite
        whatever message 7 is in her chat, and an edit through it carrying Bob's text
        would put his message in her conversation. Neither happens.
        """

        async def go() -> tuple[_Surface, _Surface]:
            queue = ReceiptQueue()
            bobs_chat, alices_chat = _Surface("bob"), _Surface("alice")
            # Each sender arrives through their OWN chat, which is what production
            # hands the queue: one surface standing in for both cannot tell a
            # correctly addressed edit from a mis-addressed one.
            async with queue.lock:
                await queue.create_or_grow_locked("s", bobs_chat, "bob asked", BOB)
                await queue.create_or_grow_locked("s", alices_chat, "alice asked", ALICE)
                before_bob, before_alice = len(bobs_chat.edits), len(alices_chat.edits)
                await queue.finish_cancelled_locked("s", alices_chat, ALICE)
                assert len(bobs_chat.edits) == before_bob, "Bob is not told he was stopped"
                assert len(alices_chat.edits) == before_alice
            return bobs_chat, alices_chat

        bobs_chat, alices_chat = asyncio.run(go())
        assert alices_chat.sent == [], "the second sender grows the burst, never a bubble"

    def test_a_second_senders_grow_writes_through_nobodys_surface(self) -> None:
        """A second sender's line is recorded, and no bubble is edited at all.

        Neither address is correct. Through the OPENER's surface the edit would put this
        sender's text into the opener's chat, which this case has always refused. Through
        this sender's own it would target a ``msg_id`` minted in the opener's chat, and
        per-chat ids being small and dense, that number names an unrelated bot message
        here -- so the whole line list would overwrite it, permanently.

        Writing nothing costs only a bubble that does not yet show this line, because a
        grow owes no record: the message is still queued, and the next same-principal
        grow or the end-of-turn flip renders the whole list. Which conversation a shared
        bubble belongs to stays the receipt registry's own question, not this one's.
        """

        async def go() -> tuple[ReceiptQueue, _Surface, _Surface]:
            queue = ReceiptQueue()
            bobs_chat, alices_chat = _Surface("bob"), _Surface("alice")
            async with queue.lock:
                await queue.create_or_grow_locked("s", bobs_chat, "bob asked", BOB)
                await queue.create_or_grow_locked("s", alices_chat, "alice asked", ALICE)
            return queue, bobs_chat, alices_chat

        queue, bobs_chat, alices_chat = asyncio.run(go())
        assert bobs_chat.edits == [], "the opener's chat never receives another's text"
        assert alices_chat.edits == [], "and the second sender's chat is not rewritten"
        assert alices_chat.sent == [], "a second sender grows the burst, never a bubble"
        assert queue._receipts["s"].texts == [
            "bob asked",
            "alice asked",
        ], "the line is still RECORDED, so the next render shows the whole list"


class TestWhatTheBubbleSaysAfterAPartialStop:
    def test_nothing_is_written_while_someone_elses_lines_remain(self) -> None:
        """Bob's message is still queued, so nothing about this burst is recorded.

        Writing "Cancelled" would tell Bob his message went, when it is still on the
        queue and still owed an answer; rewriting the bubble to what is left would put
        Bob's text through Alice's surface. Alice learns her own stop worked from the
        stop reply, and the end-of-turn flip is what next updates the bubble.
        """

        async def go() -> _Surface:
            queue, chat = ReceiptQueue(), _Surface()
            await _bubble(queue, chat, [(BOB, "bob asked"), (ALICE, "alice asked")])
            before = len(chat.edits)
            async with queue.lock:
                await queue.finish_cancelled_locked("s", chat, ALICE)
            assert len(chat.edits) == before
            return chat

        chat = asyncio.run(go())
        assert not any("Cancelled" in body for _, body in chat.edits)

    def test_the_bubble_keeps_its_handle_while_those_lines_remain(self) -> None:
        """The other half of the rule above: silence AND a live entry.

        Dropping the handle here is what strands the bubble -- the later drain finds no
        entry to flip, so "Queued" is what it reads for good over messages that were
        answered, and the next burst opens a second bubble beside it.
        """

        async def go() -> ReceiptQueue:
            queue, chat = ReceiptQueue(), _Surface()
            await _bubble(queue, chat, [(BOB, "bob asked"), (ALICE, "alice asked")])
            async with queue.lock:
                await queue.finish_cancelled_locked("s", chat, ALICE)
            return queue

        queue = asyncio.run(go())
        assert queue.has_receipt("s"), "Bob is still queued, so the bubble keeps its handle"
        assert queue._receipts["s"].texts == ["bob asked"]

    def test_withdraw_takes_the_callers_lines_and_leaves_the_rest(self) -> None:
        """The record-level half, pinned on the object that holds it.

        What the boundary does with the remainder is its own decision; what must never
        happen is a withdraw that reaches past the caller's own lines.
        """
        receipt = QueueReceipt(
            msg_id=7,
            opened_by=BOB,
            lines=[
                ReceiptLine(owner=BOB, text="bob asked"),
                ReceiptLine(owner=ALICE, text="alice secret"),
                ReceiptLine(owner=CARLA, text="carla asked"),
            ],
        )
        assert receipt.withdraw(ALICE) == ["alice secret"]
        assert receipt.texts == ["bob asked", "carla asked"]

    def test_the_entry_is_dropped_so_a_later_drain_cannot_flip_a_foreign_id(self) -> None:
        """Alice opens the bubble, Bob queues, Alice stops. The entry must NOT survive.

        A drain flips using the chat of the entry it is answering, so an entry left
        behind here would hand Bob's drain a ``msg_id`` minted in ALICE's chat, and
        ``edit_message`` addresses a message by that per-chat id pair -- it would
        overwrite whatever unrelated message holds that number in Bob's chat.

        Nothing of ALICE's is still queued once her own line goes, so her bubble is
        finalized as cancelled before the entry is dropped. Bob's line does not hold it:
        that line was never rendered on this bubble, so the bubble owes it nothing.
        """

        async def go() -> tuple[ReceiptQueue, _Surface, _Surface]:
            queue = ReceiptQueue()
            alices_chat, bobs_chat = _Surface("alice"), _Surface("bob")
            async with queue.lock:
                await queue.create_or_grow_locked("s", alices_chat, "alice asked", ALICE)
                await queue.create_or_grow_locked("s", bobs_chat, "bob asked", BOB)
                await queue.finish_cancelled_locked("s", alices_chat, ALICE)
                # Bob's drain, arriving with Bob's own chat.
                await queue.flip_answering_locked("s", bobs_chat, ["bob asked"])
            return queue, alices_chat, bobs_chat

        queue, alices_chat, bobs_chat = asyncio.run(go())
        assert not queue.has_receipt("s")
        assert not any("Now answering" in body for _, body in bobs_chat.edits)
        assert alices_chat.edits[-1] == (7, receipt_text(["alice asked"], cancelled=True))
        assert not any("bob asked" in body for _, body in alices_chat.edits)

    def test_a_non_opener_stopping_writes_nothing_and_keeps_the_others_handle(self) -> None:
        """Bob stops a bubble Alice opened. Bob's surface does not address it.

        Alice's message is still queued and this entry is its only handle, so the entry
        is RETAINED. Dropping it would leave her bubble reading "Queued" for good: her
        own drain would find no entry to flip.
        """

        async def go() -> tuple[ReceiptQueue, _Surface, _Surface]:
            queue = ReceiptQueue()
            alices_chat, bobs_chat = _Surface("alice"), _Surface("bob")
            async with queue.lock:
                await queue.create_or_grow_locked("s", alices_chat, "alice asked", ALICE)
                await queue.create_or_grow_locked("s", bobs_chat, "bob asked", BOB)
                before = len(alices_chat.edits), len(bobs_chat.edits)
                await queue.finish_cancelled_locked("s", bobs_chat, BOB)
                assert (len(alices_chat.edits), len(bobs_chat.edits)) == before
            return queue, alices_chat, bobs_chat

        queue, _alices_chat, bobs_chat = asyncio.run(go())
        assert queue.has_receipt("s"), "Alice's message is still queued, so it keeps its handle"
        assert queue._receipts["s"].texts == ["alice asked"]
        assert not any("Cancelled" in body for _, body in bobs_chat.edits)

    def test_the_last_line_going_finalizes_and_drops_the_receipt(self) -> None:
        """Nobody is left waiting, and the caller is necessarily the opener here."""

        async def go() -> tuple[ReceiptQueue, _Surface]:
            queue, chat = ReceiptQueue(), _Surface()
            await _bubble(queue, chat, [(ALICE, "alice asked")])
            async with queue.lock:
                await queue.finish_cancelled_locked("s", chat, ALICE)
            return queue, chat

        queue, chat = asyncio.run(go())
        assert chat.edits[-1] == (7, receipt_text(["alice asked"], cancelled=True))
        assert not queue.has_receipt("s")

    def test_a_caller_with_nothing_on_the_bubble_does_not_touch_it(self) -> None:
        """Carla stopping changes nothing Alice and Bob can see."""

        async def go() -> tuple[ReceiptQueue, _Surface]:
            queue, chat = ReceiptQueue(), _Surface()
            await _bubble(queue, chat, [(ALICE, "alice asked"), (BOB, "bob asked")])
            before = len(chat.edits)
            async with queue.lock:
                await queue.finish_cancelled_locked("s", chat, CARLA)
            assert len(chat.edits) == before
            return queue, chat

        queue, _chat = asyncio.run(go())
        assert queue.has_receipt("s")

    def test_no_owner_still_finalizes_the_whole_receipt(self) -> None:
        """The whole-session callers are unchanged.

        Rendering every line as cancelled is right there because no principal is left
        waiting: the queue that was cleared was all of it.
        """

        async def go() -> tuple[ReceiptQueue, _Surface]:
            queue, chat = ReceiptQueue(), _Surface()
            await _bubble(queue, chat, [(ALICE, "alice asked"), (BOB, "bob asked")])
            async with queue.lock:
                await queue.finish_cancelled_locked("s", chat)
            return queue, chat

        queue, chat = asyncio.run(go())
        assert chat.edits[-1] == (
            7,
            receipt_text(["alice asked", "bob asked"], cancelled=True),
        )
        assert not queue.has_receipt("s")

    def test_an_untagged_line_is_never_withdrawn_by_a_named_caller(self) -> None:
        """Mirrors the queue side: unclaimed is nobody's to withdraw, so it survives.

        Alice's own line goes from the record and the untagged one stays. Nothing is
        written either, because the untagged producer opened this bubble, so the chat
        Alice holds is not the one the id was minted in.
        """

        async def go() -> tuple[ReceiptQueue, _Surface]:
            queue, chat = ReceiptQueue(), _Surface()
            await _bubble(queue, chat, [("", "whose is this"), (ALICE, "alice asked")])
            before = len(chat.edits)
            async with queue.lock:
                await queue.finish_cancelled_locked("s", chat, ALICE)
            assert len(chat.edits) == before
            return queue, chat

        queue, chat = asyncio.run(go())
        assert not any("Cancelled" in body for _, body in chat.edits)
        assert queue.has_receipt("s"), "the untagged line is still queued and still held"
        assert queue._receipts["s"].texts == ["whose is this"]

    def test_an_unnamed_caller_withdraws_nothing_at_all(self) -> None:
        """The guard the boundary above never reaches, pinned on the object itself.

        ``finish_cancelled_locked`` only calls ``withdraw`` for a named caller, so an
        empty token reaches it only from a future caller. Without the guard the empty
        string would match every untagged line -- the one population the queue side
        deliberately keeps -- so the contract belongs to the object, not to its caller.
        """
        receipt = QueueReceipt(
            msg_id=7,
            lines=[
                ReceiptLine(owner="", text="whose is this"),
                ReceiptLine(owner=ALICE, text="alice"),
            ],
        )
        assert receipt.withdraw("") == []
        assert receipt.texts == ["whose is this", "alice"]


# ── the two together, through the shared /stop ──────────────────────────────


class _Sessions:
    """The session surface ``stop_running_turn`` touches, holding a real queue."""

    def __init__(self, entries: list[tuple[str, str, dict]]) -> None:
        self.entries = list(entries)

    def is_busy(self, key: str) -> bool:
        return False

    def get_provider(self, key: str) -> Any:
        return None

    def clear_queue(self, key: str, owned_by: Any = None) -> None:
        if owned_by is None:
            self.entries = []
            return
        self.entries = [item for item in self.entries if not owned_by(item[2])]


class TestTheSharedStopPath:
    def test_one_persons_stop_leaves_the_others_message_queued(self) -> None:
        """End to end: Alice stops, Bob keeps his message and is told nothing.

        The bubble is written nothing at all, because Bob's line is still on it and his
        message is still queued -- not because of who opened it. One shared chat here,
        so addressing is not what decides this.
        """

        async def go() -> tuple[_Sessions, _Surface, ReceiptQueue, int]:
            queue, surface = ReceiptQueue(), _Surface()
            sessions = _Sessions(_burst())
            await _bubble(
                queue,
                surface,
                [(ALICE, "alice asked"), (BOB, "bob asked")],
                "unified:agent",
            )
            before = len(surface.edits)
            await stop_running_turn(
                sessions, "unified:agent", queue=queue, surface=surface, owner=ALICE
            )
            return sessions, surface, queue, before

        sessions, surface, queue, before = asyncio.run(go())
        assert [kwargs["mark"] for _, _, kwargs in sessions.entries] == ["b", "c"]
        assert surface.edits[before:] == [], "Bob is still queued, so nothing is recorded"
        assert not any("bob asked" in body for _, body in surface.edits[before:])
        assert queue.has_receipt("unified:agent"), "and his bubble keeps its only handle"

    def test_the_owner_argument_is_required(self) -> None:
        """No default, so a channel added later cannot inherit the whole-queue clear.

        A default would be silent: the new channel would pass its tests and discard other
        people's messages in production.
        """
        with pytest.raises(TypeError):
            asyncio.run(
                stop_running_turn(  # type: ignore[call-arg]
                    _Sessions([]), "s", queue=ReceiptQueue(), surface=_Surface()
                )
            )


# ── the call sites ──────────────────────────────────────────────────────────

#: Each dispatcher that clears a queue on Stop, and the handler that does it. A source
#: check rather than a behavioural one because the point is that NO such handler is
#: missing the argument -- including one added after these tests were written, which no
#: behavioural test can be written for in advance.
_STOP_HANDLERS = (
    ("discord", "_handle_stop"),
    ("telegram", "_handle_stop"),
    ("teams", "_handle_stop"),
    ("webex", "_handle_stop"),
)


def _handler_source(channel: str, name: str) -> str:
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "kiro_crew"
        / channel
        / "transport_dispatch.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"{channel}.{name} not found")


class TestEveryStopHandlerNamesItsCaller:
    @pytest.mark.parametrize(("channel", "name"), _STOP_HANDLERS)
    def test_the_handler_passes_an_owner(self, channel: str, name: str) -> None:
        """Whether through the shared helper or its own inline clear.

        Webex clears inline, so a check written only against ``stop_running_turn`` would
        pass while Webex still emptied the whole queue -- and Webex is the channel where
        one session key is shared even without a unified scope, because a group space
        routes every member onto it.
        """
        source = _handler_source(channel, name)
        assert "_entry_owner(" in source, f"{channel} Stop does not name its caller"

    @pytest.mark.parametrize(("channel", "name"), _STOP_HANDLERS)
    def test_the_handler_never_clears_the_whole_queue(self, channel: str, name: str) -> None:
        source = _handler_source(channel, name)
        assert "clear_queue(session_key)" not in source


class TestAnOwedRecordOnlyAddressesTheBubbleItBelongsTo:
    """A retained terminal entry is written through the surface it was OPENED on.

    A refused finalizing edit leaves the entry terminal, still owing its record, and
    the next transition retries it. That retry is the one write in the subsystem whose
    address does NOT come from the message in hand: the surface a transition is handed
    is built from the ARRIVING message, so under this file's unified key it belongs to
    whoever spoke last, while ``msg_id`` addresses a message in the opener's chat only
    and the owed body quotes the opener's own text. Both halves of that are pinned
    here, because reading the caller's surface instead sends one person's text into
    another's conversation and there is no later transition to correct it.

    Two conversations deliberately hand out the SAME ``msg_id``: per-chat ids are small
    and dense, so a mis-addressed edit overwrites an unrelated message rather than
    failing loudly.
    """

    def test_a_second_senders_message_writes_the_owed_record_into_the_openers_chat(
        self,
    ) -> None:
        async def go() -> tuple[ReceiptQueue, _Surface, _Surface]:
            queue = ReceiptQueue()
            alices_chat = _Surface("alice", edit_refuses=True, send_fails_after=1)
            bobs_chat = _Surface("bob")
            async with queue.lock:
                await queue.create_or_grow_locked("s", alices_chat, "alice asked", ALICE)
                # Alice's drain flips; the edit is refused, so the entry is terminal.
                await queue.flip_answering_locked("s", alices_chat, ["alice asked"])
                alices_chat.edit_refuses = False
                alices_chat._send_fails_after = None
                # Bob's mid-turn message, arriving with BOB's own surface.
                await queue.create_or_grow_locked("s", bobs_chat, "bob asked", BOB)
            return queue, alices_chat, bobs_chat

        queue, alices_chat, bobs_chat = asyncio.run(go())
        owed = receipt_text(["alice asked"], answering=True)
        assert alices_chat.edits[-1] == (7, owed), "the record lands in the opener's chat"
        assert bobs_chat.edits == [], "and never through the arriving sender's surface"
        assert bobs_chat.sent == [receipt_text(["bob asked"])], "Bob gets a fresh bubble"
        assert not queue.has_receipt("s") or queue._receipts["s"].opened_on is bobs_chat

    def test_a_second_senders_stop_does_not_post_the_openers_text_into_their_chat(
        self,
    ) -> None:
        """The fallback POST is the disclosing half: it carries the body, not just an id."""

        async def go() -> tuple[_Surface, _Surface]:
            queue = ReceiptQueue()
            # Alice's chat keeps refusing edits, so the owed record must be POSTED.
            alices_chat = _Surface("alice", edit_refuses=True, send_fails_after=1)
            bobs_chat = _Surface("bob")
            async with queue.lock:
                await queue.create_or_grow_locked("s", alices_chat, "alice secret", ALICE)
                await queue.flip_answering_locked("s", alices_chat, ["alice secret"])
                # The post is allowed now, so the retry Bob's stop triggers reaches it.
                alices_chat._send_fails_after = None
                await queue.finish_cancelled_locked("s", bobs_chat, BOB)
            return alices_chat, bobs_chat

        alices_chat, bobs_chat = asyncio.run(go())
        owed = receipt_text(["alice secret"], answering=True)
        assert owed in alices_chat.sent, "the record is posted into the bubble's own chat"
        assert bobs_chat.sent == [], "Bob's chat receives no post at all"
        assert bobs_chat.edits == [], "and no edit either"
        assert not any(
            "Cancelled" in body for body in alices_chat.sent
        ), "what is written is the record that was OWED, not one Bob's stop computed"

    def test_a_drain_arriving_on_another_chat_retries_the_record_on_the_openers(
        self,
    ) -> None:
        async def go() -> tuple[_Surface, _Surface]:
            queue = ReceiptQueue()
            alices_chat = _Surface("alice", edit_refuses=True, send_fails_after=1)
            bobs_chat = _Surface("bob")
            async with queue.lock:
                await queue.create_or_grow_locked("s", alices_chat, "alice asked", ALICE)
                await queue.flip_answering_locked("s", alices_chat, ["alice asked"])
                alices_chat.edit_refuses = False
                alices_chat._send_fails_after = None
                # A later drain answering BOB, so built with Bob's chat.
                await queue.flip_answering_locked("s", bobs_chat, ["bob asked"])
            return alices_chat, bobs_chat

        alices_chat, bobs_chat = asyncio.run(go())
        assert alices_chat.edits[-1] == (7, receipt_text(["alice asked"], answering=True))
        assert bobs_chat.edits == [] and bobs_chat.sent == []

    def test_the_fallback_post_uses_the_bubbles_own_send_address(self) -> None:
        """One principal, two addresses: the forum Topic case.

        A flip and a ``/stop`` build their surface with no thread, because both only
        ever EDIT and an edit addresses a message rather than a thread. The fallback
        POST does not, so taking the caller's surface would put the record in the
        parent chat -- the one place a served send never lands -- for a bubble that
        lives in a Topic.
        """

        async def go() -> tuple[_Surface, _Surface]:
            queue = ReceiptQueue()
            topic = _Surface("topic", edit_refuses=True)
            parent_chat = _Surface("parent-chat")
            async with queue.lock:
                await queue.create_or_grow_locked("s", topic, "asked in the topic", ALICE)
                await queue.flip_answering_locked("s", topic, ["asked in the topic"])
                # Same person, but the surface the /stop handler built has no thread.
                await queue.finish_cancelled_locked("s", parent_chat, ALICE)
            return topic, parent_chat

        topic, parent_chat = asyncio.run(go())
        owed = receipt_text(["asked in the topic"], answering=True)
        assert owed in topic.sent, "the record is posted into the Topic the bubble is in"
        assert parent_chat.sent == [], "never into the parent chat"
        assert parent_chat.edits == []

    def test_an_entry_with_no_bound_address_is_not_written_through_a_callers(self) -> None:
        """No address is not the same as any address, so nothing is written."""

        async def go() -> tuple[ReceiptQueue, _Surface]:
            queue = ReceiptQueue()
            receipt = QueueReceipt(
                msg_id=7,
                opened_by=ALICE,
                lines=[ReceiptLine(owner=ALICE, text="alice asked")],
            )
            receipt.terminalize(receipt_text(["alice asked"], answering=True))
            queue._receipts["s"] = receipt
            bobs_chat = _Surface("bob")
            async with queue.lock:
                await queue.create_or_grow_locked("s", bobs_chat, "bob asked", BOB)
            return queue, bobs_chat

        queue, bobs_chat = asyncio.run(go())
        assert bobs_chat.edits == [] and bobs_chat.sent == []
        assert queue._receipts["s"].owes_record, "the record stays owed rather than misfiled"

    def test_a_later_senders_grow_does_not_rebind_the_bubbles_address(self) -> None:
        """The address is the OPENER's for the entry's whole life.

        Rebinding it on a grow would hand the next owed record the newest speaker's
        chat, which is the same disclosure by a slower route.
        """

        async def go() -> tuple[ReceiptQueue, _Surface, _Surface]:
            queue = ReceiptQueue()
            alices_chat, bobs_chat = _Surface("alice"), _Surface("bob")
            async with queue.lock:
                await queue.create_or_grow_locked("s", alices_chat, "alice asked", ALICE)
                await queue.create_or_grow_locked("s", bobs_chat, "bob asked", BOB)
            return queue, alices_chat, bobs_chat

        queue, alices_chat, _bobs_chat = asyncio.run(go())
        receipt = queue._receipts["s"]
        assert receipt.opened_on is alices_chat
        assert receipt.opened_by == ALICE

    def test_a_whole_session_cancel_finalizes_through_the_bubbles_own_surface(self) -> None:
        """A clear that names no principal cannot assume its caller is the opener.

        A caller meaning "the queue was all of it" passes no owner, so under a shared key
        it can arrive from a different chat. Unlike a grow this record is TERMINAL: those
        messages have left the queue, nothing will revisit the bubble, and one left
        reading queued for cleared messages is wrong for good. So it IS written -- to the
        bubble's own chat, and over the bubble's own chat's lines ONLY. Bob's text is
        cleared too, but showing it in Alice's chat is how a private message reaches a
        reader it was never for, and the edit is only the cheaper half: a refusal POSTS
        the same body there as a fresh notified message.
        """

        async def go() -> tuple[_Surface, _Surface]:
            queue = ReceiptQueue()
            alices_chat, bobs_chat = _Surface("alice"), _Surface("bob")
            async with queue.lock:
                await queue.create_or_grow_locked("s", alices_chat, "alice asked", ALICE)
                await queue.create_or_grow_locked("s", bobs_chat, "bob asked", BOB)
                # No owner: the whole-session clear, as a /stop handler that names
                # nobody calls it, arriving on Bob's surface.
                await queue.finish_cancelled_locked("s", bobs_chat)
            return alices_chat, bobs_chat

        alices_chat, bobs_chat = asyncio.run(go())
        cancelled = receipt_text(["alice asked"], cancelled=True)
        assert alices_chat.edits[-1] == (7, cancelled), "finalized on the bubble's own id"
        assert not any(
            "bob asked" in body for _, body in alices_chat.edits
        ), "another chat's text never appears in this one"
        assert bobs_chat.edits == [], "never on the arriving caller's unrelated message"

    def test_a_whole_session_cancel_that_is_refused_keeps_the_record_owed(self) -> None:
        """The terminal half still holds when the bubble's own chat refuses the edit."""

        async def go() -> tuple[ReceiptQueue, _Surface, _Surface]:
            queue = ReceiptQueue()
            alices_chat = _Surface("alice", edit_refuses=True, send_fails_after=1)
            bobs_chat = _Surface("bob")
            async with queue.lock:
                await queue.create_or_grow_locked("s", alices_chat, "alice asked", ALICE)
                await queue.finish_cancelled_locked("s", bobs_chat)
            return queue, alices_chat, bobs_chat

        queue, alices_chat, bobs_chat = asyncio.run(go())
        assert queue._receipts["s"].owes_record, "a refused terminal edit stays owed"
        assert alices_chat.edits[-1][0] == 7
        assert bobs_chat.edits == [] and bobs_chat.sent == []


class TestOneSharedRoomIsOneAddress:
    """A group space shares ONE room, so every member writes to the same bubble.

    ``space:{room_id}`` puts every allow-listed member of one space on one session key
    and one queue under ANY ``dm_scope``, and each member's surface is built from their
    own arriving message while binding that same room. So the member is "somebody else"
    and the conversation is still the bubble's own -- which is why these rules are
    decided on the address and not on who is calling.
    """

    def test_a_second_members_message_updates_the_shared_bubble(self) -> None:
        """The regression a test on the caller's identity would reintroduce.

        Recorded but never rendered, the shared bubble would sit at "Queued (1)" and
        this member's message would never be acknowledged by any later transition
        either.
        """

        async def go() -> _Surface:
            queue = ReceiptQueue()
            # Separately built surfaces, one room: what production hands the queue.
            alice_in_room = _Surface("alice", address="room-1")
            bob_in_room = _Surface("bob", address="room-1")
            async with queue.lock:
                await queue.create_or_grow_locked("space:r1", alice_in_room, "alice asked", ALICE)
                await queue.create_or_grow_locked("space:r1", bob_in_room, "bob asked", BOB)
            return bob_in_room

        bob_in_room = asyncio.run(go())
        assert bob_in_room.edits == [
            (7, receipt_text(["alice asked", "bob asked"], cancelled=False))
        ], "the shared bubble grows, and shows both lines: one room, one readership"
        assert bob_in_room.sent == [], "it grows the existing bubble, never a second one"

    def test_a_whole_session_cancel_in_one_room_shows_the_whole_room(self) -> None:
        """Both members' lines render, because both arrived at the bubble's own address.

        The same transition under unified DM scope shows only the bubble's own chat's
        line. Narrowing to the OPENER's lines instead would be wrong here: a group
        space has one readership, and every member already saw every message go in.
        """

        async def go() -> tuple[_Surface, _Surface, int]:
            queue = ReceiptQueue()
            alice_in_room = _Surface("alice", address="room-1")
            bob_in_room = _Surface("bob", address="room-1")
            async with queue.lock:
                await queue.create_or_grow_locked("space:r1", alice_in_room, "alice asked", ALICE)
                await queue.create_or_grow_locked("space:r1", bob_in_room, "bob asked", BOB)
                # His grow legitimately reached the shared bubble; count from here.
                before = len(bob_in_room.edits)
                # A /stop handler that names nobody, arriving from the second member.
                await queue.finish_cancelled_locked("space:r1", bob_in_room)
            return alice_in_room, bob_in_room, before

        alice_in_room, bob_in_room, before = asyncio.run(go())
        assert alice_in_room.edits[-1] == (
            7,
            receipt_text(["alice asked", "bob asked"], cancelled=True),
        )
        assert len(bob_in_room.edits) == before, "written through the bubble's own surface"


class TestATransitionForAnotherChatIsSilentHere:
    def test_a_flip_from_another_chat_writes_nowhere_and_keeps_the_entry(self) -> None:
        """The drain's origin need not be the bubble's chat, so the flip is addressed.

        One drained turn carries ONE envelope -- same sender, same chat -- so a flip
        arriving on another chat's surface is answering that chat's messages, not this
        bubble's. Writing the edit would quote their text into this chat; a refusal
        would then POST the same body here as a fresh notified message. Neither is
        stranded by silence: those messages are still queued, which is why the entry
        stays LIVE rather than being dropped.
        """

        async def go() -> tuple[ReceiptQueue, _Surface, _Surface]:
            queue = ReceiptQueue()
            alices_chat, bobs_chat = _Surface("alice"), _Surface("bob")
            async with queue.lock:
                # Bob's first message got no entry (a transient refused send), so
                # ALICE's opened the bubble -- while the drain still takes its origin
                # from the first queued entry, which is Bob's.
                await queue.create_or_grow_locked("s", alices_chat, "alice asked", ALICE)
                await queue.flip_answering_locked("s", bobs_chat, ["bob asked"])
            return queue, alices_chat, bobs_chat

        queue, alices_chat, bobs_chat = asyncio.run(go())
        assert alices_chat.edits == [], "no foreign body reaches the bubble's chat"
        assert not any("bob asked" in body for body in alices_chat.sent)
        assert bobs_chat.edits == [] and bobs_chat.sent == []
        assert queue.has_receipt("s"), "Alice's message is still queued, so it keeps its handle"
        assert queue._receipts["s"].texts == ["alice asked"]

    def test_a_flip_from_the_bubbles_own_chat_still_writes(self) -> None:
        """The other direction, so the rule above is a test and not a mute button."""

        async def go() -> tuple[ReceiptQueue, _Surface]:
            queue, chat = ReceiptQueue(), _Surface("alice")
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "alice asked", ALICE)
                await queue.flip_answering_locked("s", chat, ["alice asked"])
            return queue, chat

        queue, chat = asyncio.run(go())
        assert chat.edits[-1] == (7, receipt_text(["alice asked"], answering=True))
        assert not queue.has_receipt("s")

    def test_a_grow_renders_only_its_own_chats_lines(self) -> None:
        """A shared bubble holds another chat's lines, and they are not shown here.

        The branch no finding named: the same disclosure as the whole-session cancel,
        reached by the opener simply queueing a second message.
        """

        async def go() -> _Surface:
            queue = ReceiptQueue()
            alices_chat, bobs_chat = _Surface("alice"), _Surface("bob")
            async with queue.lock:
                await queue.create_or_grow_locked("s", alices_chat, "alice one", ALICE)
                await queue.create_or_grow_locked("s", bobs_chat, "bob asked", BOB)
                await queue.create_or_grow_locked("s", alices_chat, "alice two", ALICE)
            return alices_chat

        alices_chat = asyncio.run(go())
        assert alices_chat.edits[-1] == (7, receipt_text(["alice one", "alice two"]))
        assert not any("bob asked" in body for _, body in alices_chat.edits)

    def test_a_whole_session_cancel_on_an_unbound_entry_writes_nowhere(self) -> None:
        """No bound address means no write at all, not a write through the caller.

        An entry built without a surface has no conversation of its own, so the only
        address available is the wrong one: the caller's chat holds no bubble, and the
        body would quote text from a chat that is not theirs.
        """

        async def go() -> _Surface:
            queue = ReceiptQueue()
            callers_chat = _Surface("bob")
            queue._receipts["s"] = QueueReceipt(
                msg_id=7,
                opened_by=ALICE,
                lines=[ReceiptLine(owner=ALICE, text="alice asked", address="fake\x00alice")],
            )
            async with queue.lock:
                await queue.finish_cancelled_locked("s", callers_chat)
            return callers_chat

        callers_chat = asyncio.run(go())
        assert callers_chat.edits == [] and callers_chat.sent == []


class TestATerminalEntryKeepsOnlyWhatItOwes:
    """The bound is applied where the entry is RETAINED, not at a render site below.

    A refused final edit keeps the entry for the life of the process, so holding every
    queued message verbatim there retains an unbounded burst. The owed body is already
    bounded -- a capped item list, each item truncated -- and is all a terminal entry
    needs: it is never grown, never flipped, never rendered again.
    """

    def test_a_refused_flip_keeps_the_body_and_drops_the_lines(self) -> None:
        async def go() -> ReceiptQueue:
            queue, chat = ReceiptQueue(), _Surface("alice", edit_refuses=True, send_fails_after=1)
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "alice asked", ALICE)
                await queue.flip_answering_locked("s", chat, ["alice asked"])
            return queue

        receipt = asyncio.run(go())._receipts["s"]
        assert receipt.owes_record and receipt.owed_bodies
        assert receipt.lines == [], "the burst is not retained beside the body it produced"

    def test_a_refused_partial_cancel_keeps_the_body_and_drops_the_lines(self) -> None:
        async def go() -> ReceiptQueue:
            queue, chat = ReceiptQueue(), _Surface("alice", edit_refuses=True, send_fails_after=1)
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "alice asked", ALICE)
                await queue.finish_cancelled_locked("s", chat, ALICE)
            return queue

        receipt = asyncio.run(go())._receipts["s"]
        assert receipt.owes_record and receipt.owed_bodies
        assert receipt.lines == []

    def test_a_refused_whole_session_cancel_keeps_the_body_and_drops_the_lines(self) -> None:
        async def go() -> ReceiptQueue:
            queue, chat = ReceiptQueue(), _Surface("alice", edit_refuses=True, send_fails_after=1)
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "alice asked", ALICE)
                await queue.finish_cancelled_locked("s", chat)
            return queue

        receipt = asyncio.run(go())._receipts["s"]
        assert receipt.owes_record and receipt.owed_bodies
        assert receipt.lines == []


class TestARetiredRecordDoesNotSwallowTheCurrentOne:
    def test_the_flip_publishes_its_own_record_after_retiring_an_older_one(self) -> None:
        """Two records are owed in sequence, and BOTH reach the reader.

        A refused edit leaves the bubble terminal owing record one. The next drain
        retries it -- correctly, since writing this turn's words over what happened would
        say the opposite -- and once it lands the bubble is spent, so this turn's own
        record has nothing left to edit. Retiring the key and returning there loses it
        for good: nothing revisits a retired key, and these messages have already left
        the queue.
        """

        async def go() -> tuple[ReceiptQueue, _Surface]:
            queue = ReceiptQueue()
            # The edit AND the post are refused, so the first flip leaves the record
            # owed; the post is then allowed so the retry can land.
            chat = _Surface("alice", edit_refuses=True, send_fails_after=1)
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "first", ALICE)
                await queue.flip_answering_locked("s", chat, ["first"])
                assert queue._receipts["s"].owes_record
                chat._send_fails_after = None
                # The next drain: the owed record is retried, and this turn's own record
                # must still reach the reader.
                await queue.flip_answering_locked("s", chat, ["second"])
            return queue, chat

        queue, chat = asyncio.run(go())
        posted = chat.sent[1:]
        assert any(
            receipt_text(["first"], answering=True) == body for body in posted
        ), "the older record is published first, unchanged"
        assert any(
            receipt_text(["second"], answering=True) == body for body in posted
        ), "and this turn's own record is published rather than discarded"
        assert not queue.has_receipt("s")

    def test_a_retired_record_is_not_followed_by_another_chats_text(self) -> None:
        """The same path, but the drain belongs to a different chat: post nothing.

        The older record still goes to the bubble. This turn's own body is that other
        chat's text, so it may not appear at this address at all.
        """

        async def go() -> tuple[ReceiptQueue, _Surface, _Surface]:
            queue = ReceiptQueue()
            alices_chat = _Surface("alice", edit_refuses=True, send_fails_after=1)
            bobs_chat = _Surface("bob")
            async with queue.lock:
                await queue.create_or_grow_locked("s", alices_chat, "alice asked", ALICE)
                await queue.flip_answering_locked("s", alices_chat, ["alice asked"])
                assert queue._receipts["s"].owes_record
                alices_chat._send_fails_after = None
                await queue.flip_answering_locked("s", bobs_chat, ["bob asked"])
            return queue, alices_chat, bobs_chat

        queue, alices_chat, bobs_chat = asyncio.run(go())
        assert any(
            receipt_text(["alice asked"], answering=True) == body for body in alices_chat.sent[1:]
        ), "the owed record still reaches the bubble"
        assert not any("bob asked" in body for body in alices_chat.sent)
        assert bobs_chat.sent == [] and bobs_chat.edits == []


class TestARefusedTransitionPublishesWithoutWaiting:
    """A refused edit posts its record NOW, so a burst that ends there is not stranded.

    A retained body is written only by a LATER transition, so a session with no further
    mid-turn message had no recovery path at all: the bubble kept asserting "Queued" over
    messages that were answered or cleared. Each of the three transitions whose messages
    have already LEFT the queue is checked, because the fix is only a fix if it is at all
    of them.

    The refused GROW is the branch that matches the shape and must NOT change: its
    message is still queued, so it owes no record and a post would announce something
    that has not happened.
    """

    def test_a_refused_flip_posts_its_record(self) -> None:
        async def go() -> tuple[ReceiptQueue, _Surface]:
            queue, chat = ReceiptQueue(), _Surface("alice", edit_refuses=True)
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "alice asked", ALICE)
                await queue.flip_answering_locked("s", chat, ["alice asked"])
            return queue, chat

        queue, chat = asyncio.run(go())
        assert chat.sent[-1] == receipt_text(["alice asked"], answering=True)
        assert "s" not in queue._receipts, "nothing is owed once it is published"

    def test_a_refused_partial_cancel_posts_its_record(self) -> None:
        async def go() -> tuple[ReceiptQueue, _Surface]:
            queue, chat = ReceiptQueue(), _Surface("alice", edit_refuses=True)
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "alice asked", ALICE)
                await queue.finish_cancelled_locked("s", chat, ALICE)
            return queue, chat

        queue, chat = asyncio.run(go())
        assert chat.sent[-1] == receipt_text(["alice asked"], cancelled=True)
        assert "s" not in queue._receipts

    def test_a_refused_whole_session_cancel_posts_its_record(self) -> None:
        async def go() -> tuple[ReceiptQueue, _Surface]:
            queue, chat = ReceiptQueue(), _Surface("alice", edit_refuses=True)
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "alice asked", ALICE)
                await queue.finish_cancelled_locked("s", chat)
            return queue, chat

        queue, chat = asyncio.run(go())
        assert chat.sent[-1] == receipt_text(["alice asked"], cancelled=True)
        assert "s" not in queue._receipts

    def test_a_refused_grow_posts_nothing_at_all(self) -> None:
        """The branch that matches but must not change: that message is still queued."""

        async def go() -> tuple[ReceiptQueue, _Surface]:
            queue, chat = ReceiptQueue(), _Surface("alice", edit_refuses=True)
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "first", ALICE)
                await queue.create_or_grow_locked("s", chat, "second", ALICE)
            return queue, chat

        queue, chat = asyncio.run(go())
        assert chat.sent == [receipt_text(["first"])], "no record is announced for a grow"
        assert queue.has_receipt("s"), "and the bubble is still live"
        assert queue._receipts["s"].texts == ["first", "second"]
