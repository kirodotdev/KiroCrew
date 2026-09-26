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

        Emptying the list is the other direction and cannot retain anything, so it has
        its own seam: ``abandon``, which gives a debt up and returns the count that keeps
        the loss from being silent. Pinned here as well, so a third site cannot discard
        records by claiming to be that one.
        """
        src = Path(Q.__file__).with_suffix(".py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        offenders = []
        emptied = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "owed_bodies":
                        offenders.append(node.lineno)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "owed_bodies"
            ):
                if node.func.attr == "append":
                    offenders.append(node.lineno)
                elif node.func.attr == "clear":
                    emptied.append(node.lineno)
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
        give_up = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "abandon"
        ]
        assert give_up, "abandon is gone; giving a debt up has no counted seam"
        assert all(
            give_up[0].lineno < line < give_up[0].end_lineno for line in emptied
        ), f"owed_bodies is emptied outside abandon, so those records drop uncounted: {emptied}"

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

        Driven by drains alone. A grow against a terminal entry is a refused publication
        too, so interleaving them would spend the lifetime allowance before the list could
        overflow -- which is why that allowance is sized off this cap.
        """

        async def go() -> ReceiptQueue:
            queue = ReceiptQueue()
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "m0", "alice")
                for n in range(RECEIPT_MAX_OWED + 2):
                    await queue.flip_answering_locked("s", chat, [f"m{n}"])
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

        Held inside the lifetime allowance, so what is pinned here is the line retention
        alone; giving the debt up past that allowance has its own tests.
        """

        async def go() -> ReceiptQueue:
            queue = ReceiptQueue()
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "first", "alice")
                await queue.flip_answering_locked("s", chat, ["first"])
                for n in range(Q.RECEIPT_MAX_PUBLISH_ATTEMPTS - 2):
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


class TestRetentionIsBoundedInLifetimeAndPopulation:
    """A debt is kept while it is plausibly publishable, and while the registry has room.

    ``RECEIPT_MAX_OWED`` bounds how much one debt holds. These bound the other two ways it
    can grow without end: how LONG one is held, and how MANY are held. The entry is the
    registry's only entry for its session key, and a key can span several conversations,
    so a debt that is never given up is a key that is never released -- and every healthy
    conversation sharing it goes without receipts too.
    """

    GIVEN_UP = "queue receipt debt given up"

    @staticmethod
    def given_up(caplog: Any) -> list[int]:
        """The record count from each give-up warning, in order.

        A released record leaves no other trace, so the warning IS the accounting. Read as
        the log's own arguments rather than its rendered text, which is what the operator's
        handler receives.
        """
        return [
            r.args[2]
            for r in caplog.records
            if isinstance(r.args, tuple) and len(r.args) == 3 and "given up" in str(r.msg)
        ]

    @staticmethod
    async def _owing(chat: _Surface, key: str = "s") -> ReceiptQueue:
        """A terminal entry on *key*, owing one record that reached nobody."""
        queue = ReceiptQueue()
        async with queue.lock:
            await queue.create_or_grow_locked(key, chat, "first", "alice")
            await queue.flip_answering_locked(key, chat, ["first"])
        assert queue._receipts[key].owes_record, "the record was refused, so it is owed"
        return queue

    def test_the_allowance_outlives_the_records_the_size_cap_needs(self) -> None:
        """The two bounds race, and the sizing is what keeps the size cap reachable.

        A debt takes on one record per refused publication, so an allowance shorter than
        the retentions ``RECEIPT_MAX_OWED`` needs would give the debt up before that cap
        could ever bite -- leaving ``omitted_records`` describing a state nothing reaches.
        """
        assert Q.RECEIPT_MAX_PUBLISH_ATTEMPTS > RECEIPT_MAX_OWED + 2

    def test_a_conversation_that_refuses_every_write_gives_the_debt_up(self, caplog) -> None:
        """The permanent-failure case: neither the edit nor the post ever lands.

        Every arriving message is turned away with no bubble while the debt is held, so the
        allowance bounds the acknowledgements one debt may cost. Past it the debt is given
        up and the key released.
        """

        async def go() -> tuple[ReceiptQueue, bool]:
            # One surface throughout, and its sends are spent, so nothing this
            # conversation is offered can land and no fresh bubble hides the release.
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            queue = await self._owing(chat)
            async with queue.lock:
                # The refusal that created the debt counts as one, so this is one short.
                for _ in range(Q.RECEIPT_MAX_PUBLISH_ATTEMPTS - 2):
                    await queue.create_or_grow_locked("s", chat, "again", "alice")
                held = "s" in queue._receipts
                await queue.create_or_grow_locked("s", chat, "again", "alice")
            return queue, held

        with caplog.at_level("WARNING", logger="kiro_crew.messaging.queue_receipt"):
            queue, held = asyncio.run(go())
        assert held, "inside the allowance the record is still owed, not discarded"
        assert "s" not in queue._receipts, "past it the debt is given up and the key freed"
        assert self.given_up(caplog) == [1], "the record given up is counted, not dropped"

    def test_the_ordinary_grow_and_drain_pattern_reaches_the_bound(self, caplog) -> None:
        """The interleaving the bound exists for, and the one it must not be blind to.

        Real traffic alternates a mid-turn message with the drain that answers it, and both
        are refused publications against a dead conversation. A retention that restarted
        the allowance would leave the count oscillating below the cap forever, so the key
        would never be released however long the outage lasted.
        """

        async def go() -> ReceiptQueue:
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            queue = await self._owing(chat)
            async with queue.lock:
                for n in range(Q.RECEIPT_MAX_PUBLISH_ATTEMPTS):
                    await queue.create_or_grow_locked("s", chat, f"mid {n}", "alice")
                    await queue.flip_answering_locked("s", chat, [f"mid {n}"])
            return queue

        with caplog.at_level("WARNING", logger="kiro_crew.messaging.queue_receipt"):
            queue = asyncio.run(go())
        assert not queue.has_receipt("s"), "no live bubble on a conversation taking nothing"
        assert "s" not in queue._receipts, "the key is released despite the interleaving"
        assert self.given_up(caplog), "and what it cost is counted"

    def test_a_healthy_sibling_on_a_shared_key_gets_its_receipt_back(self) -> None:
        """The harm the lifetime bound exists to end.

        One session key can span several conversations -- a group space routes as one key
        for every member. While an unreachable conversation holds the key, a healthy
        sibling's mid-turn message takes the terminal branch and gets no bubble. Once the
        debt is given up, the very next message opens a fresh bubble on the sibling's own
        surface.
        """

        async def go() -> tuple[ReceiptQueue, _Surface]:
            gone = _Surface(edit_refuses=True, send_fails_after=1, address="gone")
            queue = await self._owing(gone)
            alive = _Surface(address="alive")
            async with queue.lock:
                for _ in range(Q.RECEIPT_MAX_PUBLISH_ATTEMPTS - 1):
                    await queue.create_or_grow_locked("s", alive, "hello", "bob")
            return queue, alive

        queue, alive = asyncio.run(go())
        assert alive.sent == [receipt_text(["hello"])], "the sibling gets one fresh bubble"
        assert queue.has_receipt("s"), "and a live entry, so its next message grows that one"
        assert queue._receipts["s"].address == alive.address_key, "opened on the sibling"

    def test_a_spent_debt_still_gives_a_sibling_drain_its_record(self, caplog) -> None:
        """A drain answering a HEALTHY chat is not given up with the dead one's debt.

        The release exists to hand the key back to the siblings, so discarding the very
        record that arrives on a working surface would be the harm it was meant to end. The
        debt's refusals are evidence about the dead conversation and none about this one,
        and the answered messages have already left the queue, so the record is posted
        there rather than counted lost.
        """

        async def go() -> tuple[ReceiptQueue, _Surface]:
            gone = _Surface(edit_refuses=True, send_fails_after=1, address="gone")
            queue = await self._owing(gone)
            alive = _Surface(address="alive")
            async with queue.lock:
                # One short of the allowance, so the drain below is the attempt that
                # spends it.
                for _ in range(Q.RECEIPT_MAX_PUBLISH_ATTEMPTS - 2):
                    await queue.create_or_grow_locked("s", gone, "again", "alice")
                await queue.flip_answering_locked("s", alive, ["hello"])
            return queue, alive

        with caplog.at_level("WARNING", logger="kiro_crew.messaging.queue_receipt"):
            queue, alive = asyncio.run(go())
        assert alive.sent == [receipt_text(["hello"], answering=True)], "posted on its own"
        assert "s" not in queue._receipts, "and the dead conversation's key is released"
        assert self.given_up(caplog) == [1], "only the debt's own record is given up"

    def test_a_sibling_record_its_own_chat_refuses_is_counted(self, caplog) -> None:
        """The loss is accounted for when the sibling's surface will not take it either.

        Then this record does reach nobody, and a bound that released it silently would be
        indistinguishable from one that never held anything.
        """

        async def go() -> ReceiptQueue:
            gone = _Surface(edit_refuses=True, send_fails_after=1, address="gone")
            queue = await self._owing(gone)
            # Sends past zero fail, so the sibling has a working address and no channel.
            mute = _Surface(address="alive", send_fails_after=0)
            async with queue.lock:
                for _ in range(Q.RECEIPT_MAX_PUBLISH_ATTEMPTS - 2):
                    await queue.create_or_grow_locked("s", gone, "again", "alice")
                await queue.flip_answering_locked("s", mute, ["hello"])
            return queue

        with caplog.at_level("WARNING", logger="kiro_crew.messaging.queue_receipt"):
            queue = asyncio.run(go())
        assert "s" not in queue._receipts, "the key is still released"
        assert self.given_up(caplog) == [1, 1], "the debt, then the record that followed it"

    def test_a_debt_that_publishes_part_of_itself_is_not_given_up(self, caplog) -> None:
        """Landing a body restarts the allowance, so a slow channel keeps its records.

        The allowance is spent by attempts that move NOTHING. A channel that lands the
        oldest record and refuses the next is working, and giving the rest up there would
        discard records on their way to a reader.
        """

        async def go() -> tuple[ReceiptQueue, int]:
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            queue = await self._owing(chat)
            async with queue.lock:
                # A second record joins the debt, then arriving messages are turned away
                # until one attempt short of the allowance.
                await queue.flip_answering_locked("s", chat, ["second"])
                while queue._receipts["s"].publish_failures < Q.RECEIPT_MAX_PUBLISH_ATTEMPTS - 1:
                    await queue.create_or_grow_locked("s", chat, "again", "alice")
                spent = queue._receipts["s"].publish_failures
                # Edits land again: the oldest record publishes, the newer one still
                # cannot be posted beneath it.
                chat.edit_refuses = False
                await queue.create_or_grow_locked("s", chat, "again", "alice")
            return queue, spent

        with caplog.at_level("WARNING", logger="kiro_crew.messaging.queue_receipt"):
            queue, spent = asyncio.run(go())
        assert spent == Q.RECEIPT_MAX_PUBLISH_ATTEMPTS - 1, "it really was one short"
        receipt = queue._receipts["s"]
        assert receipt.owed_bodies == [receipt_text(["second"], answering=True)]
        assert receipt.publish_failures == 0, "one record landed, so the allowance restarts"
        assert self.given_up(caplog) == [], "nothing is given up while the debt is draining"

    def test_a_given_up_debt_does_not_re_arm_its_own_key(self, caplog) -> None:
        """A drain meeting a spent debt must not put the key straight back into retention.

        Its own record has nowhere to go -- the attempt just refused an edit AND a post on
        that surface -- so retaining it would re-arm the very key the bound released, with
        a fresh allowance, and starve the siblings again. It goes with the debt instead,
        counted rather than dropped.
        """

        async def go() -> ReceiptQueue:
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            queue = await self._owing(chat)
            async with queue.lock:
                for n in range(Q.RECEIPT_MAX_PUBLISH_ATTEMPTS):
                    await queue.flip_answering_locked("s", chat, [f"drain {n}"])
            return queue

        with caplog.at_level("WARNING", logger="kiro_crew.messaging.queue_receipt"):
            queue = asyncio.run(go())
        assert "s" not in queue._receipts, "the key stays released, not re-armed"
        counts = self.given_up(caplog)
        assert len(counts) == 1, "given up once, not a second write-off on the same entry"
        assert counts[0] > RECEIPT_MAX_OWED, (
            "the count covers more than the bodies still held: the omitted records the "
            "entry carried, and this drain's own record that died with the debt"
        )

    def test_a_key_that_stops_arriving_is_released_by_the_population_bound(self, caplog) -> None:
        """The orphan case: a debt no transition will ever visit again.

        A session key carries a generation that rotates on reset, so traffic moves to a new
        key and the debt on the old one is never retried -- per-debt attempts cannot expire
        what is never attempted. The population bound is what reaches it.
        """

        async def go() -> ReceiptQueue:
            queue = await self._owing(
                _Surface(edit_refuses=True, send_fails_after=1, address="before"), "s"
            )
            async with queue.lock:
                for n in range(Q.RECEIPT_MAX_DEBTS):
                    key = f"s:gen{n + 1}"
                    chat = _Surface(edit_refuses=True, send_fails_after=1, address=key)
                    await queue.create_or_grow_locked(key, chat, "m", "alice")
                    await queue.flip_answering_locked(key, chat, ["m"])
            return queue

        with caplog.at_level("WARNING", logger="kiro_crew.messaging.queue_receipt"):
            queue = asyncio.run(go())
        assert "s" not in queue._receipts, "the orphaned pre-rotation debt is the one released"
        assert len(queue._receipts) == Q.RECEIPT_MAX_DEBTS, "the registry is held at the bound"
        assert self.given_up(caplog) == [1], "the released record is counted, not silent"

    def test_the_population_bound_spares_a_debt_that_is_still_being_retried(self) -> None:
        """Eviction position follows what was ATTEMPTED, not only what was retained.

        A debt refused on every burst is the opposite of silent, and it still has a channel
        to hope for. Reading position from retentions alone would leave it at the front and
        evict it while an untouched orphan sat behind it -- losing the record that had the
        better chance.
        """

        async def go() -> ReceiptQueue:
            retried = _Surface(edit_refuses=True, send_fails_after=1, address="retried")
            queue = await self._owing(retried, "retried")
            async with queue.lock:
                # An orphan retained AFTER it, so retention order alone would spare the
                # orphan and evict the one still being addressed.
                orphan = _Surface(edit_refuses=True, send_fails_after=1, address="orphan")
                await queue.create_or_grow_locked("orphan", orphan, "m", "alice")
                await queue.flip_answering_locked("orphan", orphan, ["m"])
                # The retried debt is attempted again, which is what moves it behind.
                await queue.create_or_grow_locked("retried", retried, "again", "alice")
                for n in range(Q.RECEIPT_MAX_DEBTS - 1):
                    key = f"filler{n}"
                    chat = _Surface(edit_refuses=True, send_fails_after=1, address=key)
                    await queue.create_or_grow_locked(key, chat, "m", "alice")
                    await queue.flip_answering_locked(key, chat, ["m"])
            return queue

        queue = asyncio.run(go())
        assert "orphan" not in queue._receipts, "the untouched debt is the victim"
        assert "retried" in queue._receipts, "the one still being retried keeps its record"

    def test_the_population_bound_counts_the_records_already_counted_as_omitted(
        self, caplog
    ) -> None:
        """A released debt carries its own count of losses, and that is given up too.

        Counting only the bodies still held would lose a count that was itself the record
        of a loss -- the same silence one level up.
        """

        async def go() -> ReceiptQueue:
            chat = _Surface(edit_refuses=True, send_fails_after=1, address="before")
            queue = ReceiptQueue()
            async with queue.lock:
                await queue.create_or_grow_locked("s", chat, "first", "alice")
                # Six records owed: four retained, two counted as omitted.
                for n in range(RECEIPT_MAX_OWED + 2):
                    await queue.flip_answering_locked("s", chat, [f"drain {n}"])
                overflowed = queue._receipts["s"]
                assert len(overflowed.owed_bodies) == RECEIPT_MAX_OWED
                assert overflowed.omitted_records == 2
                for n in range(Q.RECEIPT_MAX_DEBTS):
                    key = f"s:gen{n + 1}"
                    other = _Surface(edit_refuses=True, send_fails_after=1, address=key)
                    await queue.create_or_grow_locked(key, other, "m", "alice")
                    await queue.flip_answering_locked(key, other, ["m"])
            return queue

        with caplog.at_level("WARNING", logger="kiro_crew.messaging.queue_receipt"):
            queue = asyncio.run(go())
        assert "s" not in queue._receipts
        assert RECEIPT_MAX_OWED + 2 in self.given_up(caplog), "held bodies AND the omitted"

    def test_the_population_bound_never_releases_a_live_entry(self, caplog) -> None:
        """A live entry's messages are still QUEUED, so its bubble is not the registry's.

        Dropping one strands that bubble on "⏳ Queued" and opens a second beside it. Only
        terminal entries are candidates, however full the registry is.
        """

        async def go() -> ReceiptQueue:
            queue = ReceiptQueue()
            live = _Surface(address="live")
            async with queue.lock:
                await queue.create_or_grow_locked("live", live, "still queued", "alice")
                for n in range(Q.RECEIPT_MAX_DEBTS + 3):
                    key = f"debt{n}"
                    chat = _Surface(edit_refuses=True, send_fails_after=1, address=key)
                    await queue.create_or_grow_locked(key, chat, "m", "alice")
                    await queue.flip_answering_locked(key, chat, ["m"])
            return queue

        with caplog.at_level("WARNING", logger="kiro_crew.messaging.queue_receipt"):
            queue = asyncio.run(go())
        assert queue.has_receipt("live"), "the live entry survives a registry full of debts"
        assert queue._receipts["live"].texts == ["still queued"]
        assert len(self.given_up(caplog)) == 3, "only the terminal entries past the bound go"

    def test_giving_a_debt_up_says_nothing_to_the_reader(self) -> None:
        """The count is for the operator's log, not for a chat nobody can write to.

        Every write to that conversation has just been refused, so there is no reader to
        tell. What the bound leaves behind is the bubble's own stale text, which no write
        could have corrected either.
        """

        async def go() -> list[str]:
            chat = _Surface(edit_refuses=True, send_fails_after=1)
            queue = await self._owing(chat)
            # Read from here on rather than clearing the log: the fake refuses sends past
            # a count of what it has already sent, so emptying it would let them land.
            mark_sent, mark_edits = len(chat.sent), len(chat.edits)
            async with queue.lock:
                for _ in range(Q.RECEIPT_MAX_PUBLISH_ATTEMPTS):
                    await queue.create_or_grow_locked("s", chat, "again", "alice")
            return chat.sent[mark_sent:] + [b for _, b in chat.edits[mark_edits:]]

        offered = asyncio.run(go())
        assert all("omitted" not in body for body in offered)
        assert all("given up" not in body for body in offered)
