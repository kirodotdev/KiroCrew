"""The shared mid-turn queue receipt: lifecycle, lock contract, and a ratchet.

Telegram and Discord grew this subsystem independently and kept ~560 duplicated
lines of it. The channel-neutral half now lives in
``messaging/queue_receipt.py``; these tests pin that channel-neutral behaviour
once, and add the
mechanism that stops a third channel from starting a third copy.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import kiro_crew.messaging.queue_receipt as Q
from kiro_crew.messaging.queue_receipt import (
    RECEIPT_MAX_ITEMS,
    RECEIPT_MAX_OWED,
    ReceiptQueue,
    receipt_address_key,
    receipt_text,
)


class _Surface:
    """Records what a channel would have put on the wire."""

    label = "fake"

    def __init__(
        self,
        *,
        send_id: Any = 7,
        edit_raises: bool = False,
        edit_refuses: bool = False,
        edit_answers_none: bool = False,
        send_fails_after: int | None = None,
        address: str = "chat",
    ) -> None:
        self._send_id = send_id
        self.edit_raises = edit_raises
        #: Report the refusal a rate-limited chat or a spent edit cap really answers.
        self.edit_refuses = edit_refuses
        #: Answer None: silence, which is not a reported failure.
        self.edit_answers_none = edit_answers_none
        #: Sends past this many fail. Public so a test can lift the outage part-way and
        #: watch what the registry does once the channel answers again.
        self.send_fails_after = send_fails_after
        #: Which conversation this surface writes to. Two surfaces sharing it address
        #: one chat; an empty one cannot name its address at all.
        self.address_key = receipt_address_key("fake", address) if address else ""
        self.sent: list[str] = []
        self.edits: list[tuple[Any, str]] = []

    async def send_receipt(self, body: str) -> Any | None:
        self.sent.append(body)
        if self.send_fails_after is not None and len(self.sent) > self.send_fails_after:
            return None
        return self._send_id

    async def edit_receipt(self, msg_id: Any, body: str) -> bool | None:
        self.edits.append((msg_id, body))
        if self.edit_raises:
            raise RuntimeError("edit failed mid-flush")
        if self.edit_answers_none:
            return None
        return not self.edit_refuses


class TestReceiptText:
    def test_queued_grows_with_the_count(self) -> None:
        assert receipt_text(["a"]).startswith("⏳ Queued (1):")
        assert receipt_text(["a", "b"]).startswith("⏳ Queued (2):")

    def test_past_the_cap_the_tail_is_summarised_not_dropped(self) -> None:
        texts = [f"m{i}" for i in range(RECEIPT_MAX_ITEMS + 3)]
        out = receipt_text(texts)
        # The count is the TRUE total even though only the cap is listed.
        assert f"({len(texts)})" in out
        assert "…and 3 more" in out

    def test_the_three_states_are_distinguishable(self) -> None:
        assert "Now answering" in receipt_text(["a"], answering=True)
        assert "Cancelled" in receipt_text(["a"], cancelled=True)


class TestLifecycle:
    def test_create_then_grow_edits_one_bubble(self) -> None:
        q, s = ReceiptQueue(), _Surface(send_id=42)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "first")
                await q.create_or_grow_locked("s", s, "second")

        asyncio.run(go())
        assert len(s.sent) == 1, "a second message would orphan the first bubble"
        assert s.edits == [(42, receipt_text(["first", "second"]))]

    def test_flip_drops_the_entry_so_the_next_burst_opens_a_fresh_bubble(self) -> None:
        q, s = ReceiptQueue(), _Surface()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])
                assert not q.has_receipt("s")
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        assert len(s.sent) == 2, "post-flip burst must start a NEW receipt"

    def test_deferred_remainder_is_stated_not_implied(self) -> None:
        q, s = ReceiptQueue(), _Surface()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"], deferred=4)

        asyncio.run(go())
        assert "+4 deferred" in s.edits[-1][1]

    def test_cancel_finalises_with_the_full_queued_list(self) -> None:
        q, s = ReceiptQueue(), _Surface()

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.create_or_grow_locked("s", s, "b")
                await q.finish_cancelled_locked("s", s)

        asyncio.run(go())
        assert "Cancelled (2)" in s.edits[-1][1]
        assert not q.has_receipt("s")

    def test_a_failing_edit_never_escapes(self) -> None:
        """Receipt upkeep is cosmetic; it must not fail the turn around it."""
        q, s = ReceiptQueue(), _Surface(edit_raises=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.create_or_grow_locked("s", s, "b")  # edit raises
                await q.flip_answering_locked("s", s, ["a", "b"])  # raises too

        asyncio.run(go())  # must not raise

    def test_a_send_that_returns_no_id_records_no_receipt(self) -> None:
        """No id means no bubble to edit later -- storing one would 404 forever."""
        q, s = ReceiptQueue(), _Surface(send_id=None)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")

        asyncio.run(go())
        assert not q.has_receipt("s")


class TestATransitionIsPublishedOnlyWhenItLands:
    """A refused edit is an ordinary answer, not an exception, and not a success."""

    def test_a_refused_flip_publishes_its_record_instead_of_waiting(self) -> None:
        """The bubble refuses the edit, so the record is POSTED beside it right away.

        Waiting for a later transition to write it is what strands a burst that ends
        here: nothing revisits the key on its own, and the bubble reads "Queued" over
        messages that were answered.
        """
        q, s = ReceiptQueue(), _Surface(send_id=42, edit_refuses=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])

        asyncio.run(go())
        assert s.sent[-1] == receipt_text(["a"], answering=True), "the record reached the reader"
        assert "s" not in q._receipts, "and nothing is owed, so no entry is kept"

    def test_a_refused_flip_keeps_the_handle_when_the_post_fails_too(self) -> None:
        """Only then is there something left to owe, and the entry carries it."""
        q, s = ReceiptQueue(), _Surface(send_id=42, edit_refuses=True, send_fails_after=1)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])

        asyncio.run(go())
        assert q._receipts["s"].owes_record
        assert q._receipts["s"].owed_bodies == [receipt_text(["a"], answering=True)]
        # Not LIVE though -- it cannot be grown.
        assert not q.has_receipt("s")

    def test_a_refused_grow_leaves_no_record_owed(self) -> None:
        """That message is still QUEUED, which is what the bubble's ledger says."""
        q, s = ReceiptQueue(), _Surface(edit_refuses=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        assert q.has_receipt("s"), "nothing has left the queue, so the bubble is still live"
        assert q._receipts["s"].texts == ["a", "b"]

    def test_silence_is_not_a_reported_failure(self) -> None:
        """A surface answering None has not reported one; reading it as failure would
        keep every receipt in the registry for good."""
        q, s = ReceiptQueue(), _Surface(edit_answers_none=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])

        asyncio.run(go())
        assert "s" not in q._receipts
        assert s.sent == [
            receipt_text(["a"])
        ], "silence means the edit LANDED, so no record is owed and none is announced"

    def test_a_raise_and_a_refusal_are_the_same_answer(self) -> None:
        q, s = ReceiptQueue(), _Surface(edit_raises=True, send_fails_after=1)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])

        asyncio.run(go())
        assert q._receipts["s"].owes_record

    def test_a_refused_cancel_keeps_the_handle_too(self) -> None:
        q, s = ReceiptQueue(), _Surface(edit_refuses=True, send_fails_after=1)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.finish_cancelled_locked("s", s)

        asyncio.run(go())
        assert q._receipts["s"].owed_bodies == [receipt_text(["a"], cancelled=True)]


class TestAnOwedRecordIsTheOneThatWasOwed:
    """The record travels with the entry, so a retry writes what actually happened."""

    def test_the_next_message_writes_the_owed_record_before_opening_a_bubble(self) -> None:
        q = ReceiptQueue()
        # The post fails too, so the record stays OWED and a later transition writes it.
        s = _Surface(send_id=42, edit_refuses=True, send_fails_after=1)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])  # refused, now terminal
                s.edit_refuses = False
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        owed = receipt_text(["a"], answering=True)
        assert s.edits[-1] == (42, owed), "the retry writes the record the flip owed"
        # Three sends: the bubble, the record post that failed, then a FRESH bubble --
        # not a grow of the answered one.
        assert len(s.sent) == 3
        assert s.sent[-1] == receipt_text(["b"])

    def test_a_terminal_entry_is_never_grown(self) -> None:
        """Growing it would put already-answered text back under "Queued".

        The answered text does appear once more, in the RECORD the refused edit posts --
        that is the record saying it was answered. What must never reappear is that text
        under "Queued", which is what a grow of a terminal entry would write.
        """
        q, s = ReceiptQueue(), _Surface(edit_refuses=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])
                s.edit_refuses = False
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        queued = [body for body in s.sent[1:] + [e[1] for e in s.edits] if "Queued" in body]
        assert all("a" not in body for body in queued), "answered text must not return"
        assert s.sent[-1] == receipt_text(["b"]), "the next message opens its own bubble"

    def test_the_key_is_kept_until_the_record_is_on_the_bubble(self) -> None:
        q = ReceiptQueue()
        # The edit stays refused AND the record cannot be posted either.
        s = _Surface(send_id=42, edit_refuses=True, send_fails_after=1)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        assert q._receipts["s"].owes_record, "the record is still owed, so the key is held"
        assert q._receipts["s"].owed_bodies == [receipt_text(["a"], answering=True)]

    def test_a_bubble_that_refuses_edits_gets_the_record_posted(self) -> None:
        """Past Webex's per-message edit cap no edit of that id ever lands, so retrying
        alone would owe the record for the life of the process."""
        q = ReceiptQueue()
        s = _Surface(send_id=42, edit_refuses=True)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])
                await q.create_or_grow_locked("s", s, "b")

        asyncio.run(go())
        owed = receipt_text(["a"], answering=True)
        assert owed in s.sent, "the record is POSTED when the bubble will not take it"
        assert "s" in q._receipts and not q._receipts["s"].owes_record

    def test_a_stop_does_not_recompute_a_record_another_transition_owes(self) -> None:
        """Writing "Cancelled" over an owed "Now answering" says the opposite of what
        happened, permanently."""
        q = ReceiptQueue()
        # The post fails too, so the entry stays TERMINAL and still owes its record.
        s = _Surface(send_id=42, edit_refuses=True, send_fails_after=1)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])  # owes "Now answering"
                s.edit_refuses = False
                await q.finish_cancelled_locked("s", s)

        asyncio.run(go())
        assert s.edits[-1][1] == receipt_text(["a"], answering=True)
        assert "Cancelled" not in s.edits[-1][1]
        assert "s" not in q._receipts

    def test_a_second_flip_retries_the_owed_record_rather_than_its_own(self) -> None:
        q = ReceiptQueue()
        # The post fails too, so the entry stays TERMINAL and still owes its record.
        s = _Surface(send_id=42, edit_refuses=True, send_fails_after=1)

        async def go() -> None:
            async with q.lock:
                await q.create_or_grow_locked("s", s, "a")
                await q.flip_answering_locked("s", s, ["a"])
                s.edit_refuses = False
                await q.flip_answering_locked("s", s, ["zzz"])

        asyncio.run(go())
        assert s.edits[-1][1] == receipt_text(["a"], answering=True)
        assert "zzz" not in s.edits[-1][1]


class TestLockIsCallerHeld:
    def test_the_transitions_do_not_take_the_lock_themselves(self) -> None:
        """The atomicity contract, asserted rather than documented.

        Callers hold the lock ACROSS enqueue+receipt (and dequeue+flip), which is
        what makes the subsystem race-free against the drain. If a future change
        moved the acquire inside these methods, this deadlocks -- so the bounded
        wait is the assertion, not a timeout guard.
        """
        q, s = ReceiptQueue(), _Surface()

        async def go() -> None:
            async with q.lock:
                await asyncio.wait_for(q.create_or_grow_locked("s", s, "a"), timeout=2)
                await asyncio.wait_for(q.flip_answering_locked("s", s, ["a"]), timeout=2)
                await asyncio.wait_for(q.finish_cancelled_locked("s", s), timeout=2)

        asyncio.run(go())


def _dispatchers() -> list[Path]:
    pkg = Path(Q.__file__).resolve().parent.parent
    found = sorted(pkg.glob("*/transport_dispatch.py"))
    assert len(found) >= 5, f"expected the dispatcher set, found {found}"
    return found


class TestRatchet:
    def test_no_channel_keeps_its_own_receipt_registry_or_lock(self) -> None:
        """A third copy of this subsystem must fail here, not in production."""
        offenders: dict[str, list[str]] = {}
        for path in _dispatchers():
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
            names = {
                node.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and node.attr
                in {
                    "_queue_receipts",
                    "_receipt_lock",
                }
            }
            if names:
                offenders[path.parent.name] = sorted(names)
        assert not offenders, (
            "these channels carry a private receipt registry/lock instead of the "
            f"shared ReceiptQueue, so the lock discipline can drift again: {offenders}"
        )

    def test_every_channel_with_a_queue_uses_the_shared_one(self) -> None:
        missing = []
        for path in _dispatchers():
            src = path.read_text(encoding="utf-8")
            if "_enqueue_with_receipt" in src and "ReceiptQueue" not in src:
                missing.append(path.parent.name)
        assert not missing, f"{missing} implement a mid-turn queue without the shared ReceiptQueue"


class TestAnAddressKeyNamesOneConversation:
    """The signal every write rule is decided on, so its edges are its own tests."""

    def test_the_same_conversation_builds_the_same_key(self) -> None:
        """Two surfaces built separately for one chat MUST compare equal.

        This is what lets a second member of a group space update the one shared
        bubble: each member's surface is constructed from their own arriving message.
        """
        assert receipt_address_key("webex", "room-1") == receipt_address_key("webex", "room-1")

    def test_different_conversations_build_different_keys(self) -> None:
        assert receipt_address_key("telegram", 11) != receipt_address_key("telegram", 12)
        assert receipt_address_key("telegram", 11) != receipt_address_key("discord", 11)

    def test_a_missing_part_is_unknown_and_matches_nothing(self) -> None:
        """An absent provider id reads as UNKNOWN, never as an address.

        Two surfaces that both failed to name their conversation are not thereby in the
        same one, so the empty key must not equal itself through
        :meth:`QueueReceipt.addressed_by` -- which is where the comparison happens.
        """
        assert receipt_address_key("teams", "https://svc", "") == ""
        assert receipt_address_key("", "room-1") == ""
        unknown = Q.QueueReceipt(msg_id=7, opened_on=_Surface(address=""))
        assert unknown.address == ""
        assert not unknown.addressed_by(_Surface(address=""))
        assert unknown.texts_at_address() == []

    def test_multi_part_keys_cannot_collide_across_a_separator(self) -> None:
        """Teams joins a service URL with a conversation id, and URLs carry ':'.

        Split differently, the same characters are a DIFFERENT pair of addresses, so
        joining on a character a URL can contain would make two conversations equal.
        """
        assert receipt_address_key("teams", "https://a", "b:c") != receipt_address_key(
            "teams", "https://a:b", "c"
        )

    def test_no_address_means_no_bubble(self) -> None:
        """A surface that cannot name its conversation opens nothing.

        Fail closed: with no key every later write would have to guess which chat this
        entry's ``msg_id`` belongs to, and a wrong guess rewrites a stranger's message.
        A channel whose inbound omitted its conversation id degrades to no receipt,
        which is what a refused send already does.
        """

        async def go() -> tuple[ReceiptQueue, _Surface]:
            queue, nameless = ReceiptQueue(), _Surface(address="")
            async with queue.lock:
                await queue.create_or_grow_locked("s", nameless, "hello")
            return queue, nameless

        queue, nameless = asyncio.run(go())
        assert nameless.sent == [], "nothing is posted without an address to post to"
        assert not queue.has_receipt("s")

    def test_owed_bodies_is_only_written_through_terminalize(self) -> None:
        """The retention bound lives at ONE seam, so a fourth transition inherits it.

        ``terminalize`` appends the owed body, caps how many are held, AND drops
        ``lines``; a write anywhere else would retain a whole burst verbatim, or an
        uncapped list of records, for the life of the process -- the defect this pin
        exists to stop coming back.
        """
        src = Path(Q.__file__).with_suffix(".py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "owed_bodies":
                        offenders.append(node.lineno)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "owed_bodies"
            ):
                offenders.append(node.lineno)
        # The one inside terminalize is the seam itself.
        assert len(offenders) == 1, (
            "owed_bodies is written outside terminalize, so that site retains records "
            f"with no cap and a burst verbatim beside them: lines {offenders}"
        )
        seam = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "terminalize"
        ]
        assert seam, "terminalize is gone; the bound has no home"
        assert seam[0].lineno < offenders[0] < seam[0].end_lineno

    def test_every_receipt_surface_stand_in_names_its_address(self) -> None:
        """A fake without an address withholds every write, and says nothing about why.

        `address_key` is part of what a channel BINDS, so a stand-in that omits it is
        not a simplified surface -- it is one the registry refuses to write through, and
        the failure surfaces as an attribute error in whichever suite happens to touch
        it rather than at the fake's own definition. Checked as an ASSIGNMENT, because a
        mention in a docstring satisfies a substring search and writes nothing.
        """
        root = Path(Q.__file__).resolve().parent.parent.parent.parent
        missing = []
        for path in sorted((root / "test").glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                posts = any(
                    isinstance(f, ast.AsyncFunctionDef) and f.name == "send_receipt"
                    for f in node.body
                )
                if not posts:
                    continue
                names = {
                    t.attr if isinstance(t, ast.Attribute) else getattr(t, "id", "")
                    for inner in ast.walk(node)
                    if isinstance(inner, (ast.Assign, ast.AnnAssign))
                    for t in (inner.targets if isinstance(inner, ast.Assign) else [inner.target])
                }
                if "address_key" not in names:
                    missing.append(f"{path.name}::{node.name}")
        assert not missing, (
            "these stand in for a receipt surface without assigning an address, so the "
            f"registry writes through none of them: {missing}"
        )

    def test_every_channel_surface_names_its_address(self) -> None:
        """A channel that forgets the key would silently stop updating its bubbles.

        Every write rule reads ``address_key``, and an absent one is UNKNOWN, so a new
        channel omitting it would pass its own tests while its receipts froze at
        "Queued" in production.
        """
        missing = []
        for path in _dispatchers():
            src = path.read_text(encoding="utf-8")
            if "ReceiptSurface" not in src:
                continue
            if "receipt_address_key(" not in src:
                missing.append(path.parent.name)
        assert not missing, f"{missing} build a receipt surface without an address key"


class TestSeveralOwedRecordsSurviveInOrder:
    """A transition meeting an unpublishable debt keeps its OWN record too.

    The channel that refused the earlier record is usually still refusing, so a single
    owed slot meant the later transition's record was dropped with nothing left to
    publish it: the messages had already left the queue and a retired key is revisited
    by nothing, so that record reached nobody, ever.
    """

    def test_a_drain_meeting_an_unpublishable_debt_keeps_both_records(self) -> None:
        """The fixed loss. Neither record has a channel, so both wait for one."""

        async def go() -> ReceiptQueue:
            queue = ReceiptQueue()
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "first", "alice")
                # Refused edit AND refused post: the entry goes terminal owing this one.
                await queue.flip_answering_locked("s", chat, ["first"])
                await queue.create_or_grow_locked("s", chat, "second", "alice")
                await queue.flip_answering_locked("s", chat, ["second"])
            return queue

        receipt = asyncio.run(go())._receipts["s"]
        assert receipt.owed_bodies == [
            receipt_text(["first"], answering=True),
            receipt_text(["second"], answering=True),
        ], "the second drain's record is kept behind the first, not dropped"

    def test_a_cross_address_drain_keeps_only_the_older_record(self) -> None:
        """The branch that matches but must NOT change.

        ``answered`` belongs to a different conversation under a shared key. Retaining it
        here would post that chat's text into this bubble later, which is the disclosure
        the address rule exists to stop, and nothing is stranded: the drain answers one
        envelope at a time, so this bubble's own messages are still queued.
        """

        async def go() -> ReceiptQueue:
            queue = ReceiptQueue()
            alice = _Surface(edit_refuses=True, send_fails_after=1, address="alice")
            bob = _Surface(address="bob")
            async with queue.lock:
                await queue.create_or_grow_locked("s", alice, "alice asked", "alice")
                await queue.flip_answering_locked("s", alice, ["alice asked"])
                await queue.flip_answering_locked("s", bob, ["bob asked"])
            return queue

        receipt = asyncio.run(go())._receipts["s"]
        assert receipt.owed_bodies == [receipt_text(["alice asked"], answering=True)]
        assert all("bob asked" not in b for b in receipt.owed_bodies)

    def test_the_oldest_record_takes_the_bubble_and_the_rest_are_posted_beneath(self) -> None:
        """Order is the order they happened: one bubble, and the rest arrive after it."""

        async def go() -> _Surface:
            queue = ReceiptQueue()
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "first", "alice")
                await queue.flip_answering_locked("s", chat, ["first"])
                await queue.create_or_grow_locked("s", chat, "second", "alice")
                await queue.flip_answering_locked("s", chat, ["second"])
                # The channel answers again, and a clear retries what is owed.
                chat.edit_refuses = False
                chat.send_fails_after = None
                await queue.finish_cancelled_locked("s", chat)
            return chat

        chat = asyncio.run(go())
        first = receipt_text(["first"], answering=True)
        second = receipt_text(["second"], answering=True)
        assert chat.edits[-1] == (7, first), "the oldest record goes into the bubble"
        assert chat.sent[-1] == second, "the later record is posted beneath it"

    def test_recovery_releases_the_key_once_everything_owed_has_published(self) -> None:
        """A debt is held while it is unpublished, and released once it publishes."""

        async def go() -> ReceiptQueue:
            queue = ReceiptQueue()
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "first", "alice")
                await queue.flip_answering_locked("s", chat, ["first"])
                await queue.create_or_grow_locked("s", chat, "second", "alice")
                await queue.flip_answering_locked("s", chat, ["second"])
                chat.edit_refuses = False
                chat.send_fails_after = None
                await queue.create_or_grow_locked("s", chat, "third", "alice")
            return queue

        queue = asyncio.run(go())
        assert "s" in queue._receipts, "the third message opens a fresh bubble"
        assert not queue._receipts["s"].owes_record, "nothing is still owed"

    def test_a_body_leaves_the_debt_only_once_it_has_landed(self) -> None:
        """Partial progress is kept, so a published record is never published twice."""

        async def go() -> tuple[ReceiptQueue, _Surface]:
            queue = ReceiptQueue()
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "first", "alice")
                await queue.flip_answering_locked("s", chat, ["first"])
                await queue.create_or_grow_locked("s", chat, "second", "alice")
                await queue.flip_answering_locked("s", chat, ["second"])
                # Edits work again, so the oldest lands in the bubble; sends still fail,
                # so the later one does not.
                chat.edit_refuses = False
                await queue.finish_cancelled_locked("s", chat)
            return queue, chat

        queue, chat = asyncio.run(go())
        assert queue._receipts["s"].owed_bodies == [
            receipt_text(["second"], answering=True)
        ], "the record that landed is released; the one that did not is kept"

    def test_the_number_of_owed_records_is_capped_at_the_retention_point(self) -> None:
        """An outage cannot grow the debt without bound.

        The OLDEST is released on overflow: the newest record is the one that corrects
        what the reader can currently see.
        """

        async def go() -> ReceiptQueue:
            queue = ReceiptQueue()
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "m0", "alice")
                for n in range(RECEIPT_MAX_OWED + 2):
                    await queue.flip_answering_locked("s", chat, [f"m{n}"])
                    await queue.create_or_grow_locked("s", chat, f"m{n + 1}", "alice")
            return queue

        receipt = asyncio.run(go())._receipts["s"]
        assert len(receipt.owed_bodies) == RECEIPT_MAX_OWED
        newest = RECEIPT_MAX_OWED + 1
        assert receipt.owed_bodies[-1] == receipt_text([f"m{newest}"], answering=True)
        assert receipt.owed_bodies[0] == receipt_text(
            [f"m{newest - RECEIPT_MAX_OWED + 1}"], answering=True
        ), "the oldest was released, not the newest"

    def test_a_terminal_entry_retains_no_lines_however_many_messages_arrive(self) -> None:
        """The other half of the bound, and why the grow records nothing while terminal.

        A mid-turn message meeting a terminal entry gets no bubble, and its line is not
        kept: no path reads a terminal entry's lines, so keeping them would change
        nothing a reader sees while holding a verbatim burst for the whole outage.
        """

        async def go() -> ReceiptQueue:
            queue = ReceiptQueue()
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "first", "alice")
                await queue.flip_answering_locked("s", chat, ["first"])
                for n in range(6):
                    await queue.create_or_grow_locked("s", chat, f"during outage {n}", "alice")
            return queue

        receipt = asyncio.run(go())._receipts["s"]
        assert receipt.lines == []
        assert all("during outage" not in b for b in receipt.owed_bodies)


class TestOnePublishedRecordSpendsTheBubble:
    """There is one bubble and it holds the OLDEST record, so nothing may edit it twice.

    A debt is published one body at a time and a body leaves the debt only when it lands,
    so a channel that recovers part-way leaves the bubble already carrying a record while
    later bodies are still owed. Editing the next one in would erase the record the reader
    can see and put the two out of order.
    """

    @staticmethod
    async def _two_owed(chat: _Surface) -> ReceiptQueue:
        """A terminal entry owing two records, neither published."""
        queue = ReceiptQueue()
        async with queue.lock:
            await queue.create_or_grow_locked("s", chat, "first", "alice")
            await queue.flip_answering_locked("s", chat, ["first"])
            await queue.flip_answering_locked("s", chat, ["second"])
        return queue

    def test_a_landed_edit_stops_the_next_record_editing_over_it(self) -> None:
        """The fixed loss: the second record posts beneath the first, never over it."""

        async def go() -> _Surface:
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            queue = await self._two_owed(chat)
            chat.edit_refuses = False  # the channel takes edits again, posts still refused
            async with queue.lock:
                await queue.finish_cancelled_locked("s", chat)
                await queue.finish_cancelled_locked("s", chat)
            return chat

        chat = asyncio.run(go())
        first = receipt_text(["first"], answering=True)
        second = receipt_text(["second"], answering=True)
        assert any(body == first for _, body in chat.edits), "the oldest took the bubble"
        assert all(
            body != second for _, body in chat.edits
        ), "the second record must never be edited onto the bubble the first one holds"
        assert second in chat.sent, "it is offered beneath the bubble instead"

    def test_a_posted_record_also_spends_the_bubble(self) -> None:
        """Publication, not the path it took, is what uses the bubble up.

        A record that had to be POSTED sits BELOW the bubble, so editing the next one into
        the bubble would show the reader the second record above the first.
        """

        async def go() -> _Surface:
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            queue = await self._two_owed(chat)
            # Counted from the sends the outage already spent: a fixed number here is
            # already past, and every post would go on failing.
            chat.send_fails_after = len(chat.sent) + 1
            async with queue.lock:
                await queue.finish_cancelled_locked("s", chat)
                chat.edit_refuses = False
                await queue.finish_cancelled_locked("s", chat)
            return chat

        chat = asyncio.run(go())
        second = receipt_text(["second"], answering=True)
        assert receipt_text(["first"], answering=True) in chat.sent, "the oldest was posted"
        assert all(
            body != second for _, body in chat.edits
        ), "a posted record still spends the bubble, so the next one may not edit it"

    def test_an_unpublished_debt_may_still_take_the_bubble(self) -> None:
        """The branch that matches but must NOT change.

        Nothing reached the reader, so the bubble still says "Queued" and is the right
        place for the oldest record the moment the channel answers.
        """

        async def go() -> _Surface:
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            queue = ReceiptQueue()
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "first", "alice")
                await queue.flip_answering_locked("s", chat, ["first"])
                chat.edit_refuses = False
                await queue.finish_cancelled_locked("s", chat)
            return chat

        chat = asyncio.run(go())
        assert (7, receipt_text(["first"], answering=True)) in chat.edits


class TestReleasedRecordsAreCounted:
    """A shortened debt must not pass for a burst that produced no such records.

    The cap releases the oldest, and a released record leaves no other trace: the list
    simply gets shorter. So the number released travels with the entry and is named on the
    next record that reaches the reader.
    """

    OMITTED = "earlier record(s) omitted"

    @staticmethod
    async def _overflowed(chat: _Surface, drains: int) -> ReceiptQueue:
        queue = ReceiptQueue()
        async with queue.lock:
            await queue.create_or_grow_locked("s", chat, "first", "alice")
            for n in range(drains):
                await queue.flip_answering_locked("s", chat, [f"drain {n}"])
        return queue

    def test_the_cap_counts_what_it_releases(self) -> None:
        async def go() -> ReceiptQueue:
            return await self._overflowed(_Surface(edit_refuses=True, send_fails_after=1), 6)

        receipt = asyncio.run(go())._receipts["s"]
        assert len(receipt.owed_bodies) == RECEIPT_MAX_OWED
        assert receipt.omitted_records == 2, "six records owed, four retained, two counted"

    def test_the_count_is_named_once_and_then_cleared(self) -> None:
        async def go() -> tuple[_Surface, ReceiptQueue]:
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            queue = await self._overflowed(chat, 6)
            chat.edit_refuses = False
            chat.send_fails_after = None
            # Only what lands counts as said: every write above was refused, and a refused
            # attempt reached nobody.
            chat.edits, chat.sent = [], []
            async with queue.lock:
                await queue.finish_cancelled_locked("s", chat)
            return chat, queue

        chat, queue = asyncio.run(go())
        published = [body for _, body in chat.edits if self.OMITTED in body]
        published += [body for body in chat.sent if self.OMITTED in body]
        assert len(published) == 1, "said out loud exactly once, not on every record"
        assert "2 earlier record(s) omitted" in published[0]
        assert "s" not in queue._receipts, "the whole debt published, so the key is released"

    def test_the_count_never_accumulates_into_the_retained_body(self) -> None:
        """The report is rendered at publish time, so a refused attempt leaves no trace.

        Writing it into the retained body instead would append one more phrase on every
        attempt, growing a field whose whole point is to be bounded.
        """

        async def go() -> ReceiptQueue:
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            queue = await self._overflowed(chat, 6)
            # One more refused attempt, with nothing appended after it: a body polluted
            # here is one the cap will not go on to release, so it stays to be seen.
            async with queue.lock:
                await queue.finish_cancelled_locked("s", chat)
            return queue

        receipt = asyncio.run(go())._receipts["s"]
        assert all(self.OMITTED not in body for body in receipt.owed_bodies)
        assert receipt.omitted_records == 2, "the count lives in its own field, not in a body"

    def test_nothing_released_says_nothing(self) -> None:
        """The count is a report of a real loss, so an intact debt must not carry one."""

        async def go() -> _Surface:
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            queue = await self._overflowed(chat, 2)
            chat.edit_refuses = False
            chat.send_fails_after = None
            async with queue.lock:
                await queue.finish_cancelled_locked("s", chat)
            return chat

        chat = asyncio.run(go())
        assert all(self.OMITTED not in body for _, body in chat.edits)
        assert all(self.OMITTED not in body for body in chat.sent)
