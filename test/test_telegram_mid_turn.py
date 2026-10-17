"""Telegram dispatcher: messages that arrive while a turn is running.

Covers the busy path of transport_dispatch.py: attachments and captions, steering into
the running turn versus queueing, the per-message /queue and /steer overrides, /stop,
the queued-message receipt, and the privacy modifier that rides a steer as one
transaction.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import _dispatcher, _dm, _origin, _prime_live, _tg_origin

from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.telegram.transport import TelegramInboundMessage
from kiro_crew.telegram.transport_dispatch import _STEER_ACK_EMOJI


class TestTelegramMidTurn:
    def test_drain_defers_past_the_attachment_cap(self) -> None:
        """Queue collapse must not exceed the shared ingestion file cap.

        Two queued 10-photo albums. Concatenating them hands 20 attachments to
        one turn and ingest_attachments silently processes only the first
        max_attachments, so the second album vanishes with no indication. The
        drain must defer the overflowing message (and everything behind it, to
        keep FIFO exact) AND then keep pumping so the deferred album runs in a
        second turn -- deferring without looping just strands it until the user
        happens to send something else.
        """
        from kiro_crew.messaging.attachments import IngestLimits

        cap = IngestLimits().max_attachments
        d, cli, sess = _dispatcher({7})
        album_a = [
            {"file_id": f"a{i}", "file_name": f"a{i}.jpg", "mime_type": "image/jpeg"}
            for i in range(cap)
        ]
        album_b = [
            {"file_id": f"b{i}", "file_name": f"b{i}.jpg", "mime_type": "image/jpeg"}
            for i in range(cap)
        ]
        # Two albums already sitting in the queue when the turn ends. Same sender,
        # so only the attachment cap can defer them.
        sess.queued = [
            (str(1), "album A", {"attachments": album_a, **_origin()}),
            (str(2), "album B", {"attachments": album_b, **_origin()}),
        ]
        sess._busy = False

        seen: list[tuple[str, int]] = []
        original = d.handle_message

        async def _spy(msg, **kw):  # type: ignore[no-untyped-def]
            seen.append((msg.text, len(msg.attachments)))
            return None  # don't run a real turn

        async def _go() -> None:
            d.handle_message = _spy  # type: ignore[assignment]
            try:
                await d._drain_queue("k")
            finally:
                d.handle_message = original  # type: ignore[assignment]

        asyncio.run(_go())

        # Cap first, and over EVERY turn: without this ordering a cap regression
        # merges both albums into one turn and would trip the pump-count
        # assertion instead, leaving the cap itself unpinned.
        assert seen, "the drain must run at least one turn"
        for i, (_t, n) in enumerate(seen):
            assert n <= cap, (
                f"turn {i} carried {n} attachments, over the cap of {cap} -- "
                "ingestion would silently drop the excess"
            )
        assert len(seen) == 2, (
            "the drain must keep pumping: the deferred album has to run in a "
            "SECOND turn of this same drain, not wait for unrelated user input"
        )
        first_text, _ = seen[0]
        second_text, _ = seen[1]
        assert (
            "album A" in first_text and "album B" not in first_text
        ), "album B must be deferred whole, not partially merged"
        assert "album B" in second_text, "album B must drain in the second turn"
        assert not sess.queued, "the queue must be empty once the pump finishes"

    def test_command_caption_on_attachment_is_content_not_a_command(self) -> None:
        """A photo captioned "/new" must be ingested, not intercepted.

        The command intercept returns BEFORE attachment ingestion, so treating a
        caption as a command silently discards the file the user attached to it.
        Attachments make the message content-bearing; Discord already gated on
        this via interpret_as_command.
        """
        d, cli, sess = _dispatcher({7})
        photos = [{"file_id": "p1", "file_name": "a.jpg", "mime_type": "image/jpeg"}]
        before_gen = d._conv.current_gen(
            d._route_key(
                chat_type="private",
                user_id=7,
                chat_id=7,
                thread=None,
            )
        )

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="/new",
                    attachments=photos,
                )
            )

        asyncio.run(_go())

        route = d._route_key(chat_type="private", user_id=7, chat_id=7, thread=None)
        assert d._conv.current_gen(route) == before_gen, (
            "/new as an attachment caption must NOT start a new conversation -- "
            "that path returns before ingestion and drops the photo"
        )
        assert not any(
            "New conversation started" in t for t, _ in cli.sent
        ), "the command confirmation must not be sent for an attachment caption"

    def test_bare_directive_caption_on_attachment_is_content_not_a_command(
        self,
    ) -> None:
        """A photo captioned "/queue" must be ingested, not answered with usage.

        Same loss as the "/new" caption above and the same cause: the bare
        "/queue" | "/steer" guard returns BEFORE attachment ingestion, so a
        caption read as a directive silently discards the file. ``attachments``
        make the message content-bearing, which is exactly what
        ``interpret_as_command`` encodes.
        """
        d, cli, _ = _dispatcher({7})
        photos = [{"file_id": "p1", "file_name": "a.jpg", "mime_type": "image/jpeg"}]

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="/queue",
                    attachments=photos,
                )
            )

        asyncio.run(_go())

        assert not any("Those take a message" in t for t, _ in cli.sent), (
            "the bare-directive usage reply must not fire for an attachment "
            "caption -- that path returns before ingestion and drops the photo"
        )

    def test_attachment_message_is_queued_not_steered(self) -> None:
        """A mid-turn message carrying files must NEVER take the steer path.

        ``steer`` forwards TEXT ONLY, so steering a photo/album message would
        deliver its caption and silently discard every attachment. This is the
        exact loss an album hits: a follow-up typed during the debounce window
        starts a turn, so the album's own flush lands mid-turn.

        The queue path is correct because it carries ``attachments`` through the
        drain -- assert they survive, not merely that steer was skipped.
        """
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        # Default (non-queue) mode: without the guard this would steer.
        d.cfg.messaging.queue_mode = "steer"
        _prime_live(d.cfg)
        photos = [
            {"file_id": "p1", "file_name": "a.jpg", "mime_type": "image/jpeg"},
            {"file_id": "p2", "file_name": "b.jpg", "mime_type": "image/jpeg"},
        ]

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="what is wrong here?",
                    attachments=photos,
                )
            )

        asyncio.run(_go())

        assert sess._gp.steered == [], "an attachment message must not be steered"
        assert len(sess.queued) == 1, "it must be queued instead"
        _ts, text, kwargs = sess.queued[0]
        assert text == "what is wrong here?"
        assert kwargs.get("attachments") == photos, (
            "the queue must carry the attachments -- otherwise the images are "
            "silently dropped exactly as steering would have done"
        )

    def test_busy_steer_folds_into_running_turn(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="and also this"
                )
            )

        asyncio.run(_go())
        # Folded into the running turn (steer called), not queued, and NO receipt
        # bubble is posted (M1: the steered continuation threads under the user's
        # message instead — see the renderer reply-linkage test).
        assert sess._gp.steered == ["and also this"]
        assert sess.queued == []
        assert not any("Steered" in t for t, _ in cli.sent)

    def test_busy_steer_skipped_when_turn_already_ended(self) -> None:
        # Race guard: is_busy() stays True through post-turn bookkeeping, but the
        # turn has actually ended (has_active_turn() False). The steer must NOT
        # be treated as terminal -- the message falls through to the queue/handle
        # path so it is never silently swallowed.
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        sess._gp.active_turn = False  # turn ended, semaphore still held

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="landed after turn",
                )
            )

        asyncio.run(_go())
        # Not steered (dead turn); preserved via the queue path instead of lost.
        assert sess._gp.steered == []
        assert [text for _ts, text, _ in sess.queued] == ["landed after turn"]

    def test_busy_steer_reacts_to_user_message(self) -> None:
        # Instant ack: a mid-turn steer reacts to the user's message (no extra
        # bubble) so it isn't silent while it waits for the next generation
        # boundary (the steered continuation only posts at turn end).
        d, cli, sess = _dispatcher({7})
        sess._busy = True

        async def _go() -> None:
            await d.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="stop, only banana",
                    message_id=4242,
                )
            )

        asyncio.run(_go())
        assert sess._gp.steered == ["stop, only banana"]  # steered
        assert cli.reactions == [(4242, _STEER_ACK_EMOJI)]  # reacted on the user's steer msg
        # No extra receipt/steer bubble is posted (the ack is the reaction only).
        assert not any("Steered" in t or "Queued" in t for t, _ in cli.sent)

    def test_queue_override_forces_queue_in_steer_mode(self) -> None:
        # "/queue …" holds the message even though the global mode is steer;
        # the directive is stripped from the queued text.
        d, cli, sess = _dispatcher({7})
        sess._busy = True  # global queue_mode defaults to "steer"

        async def _go() -> None:
            await d.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="/queue check disk after",
                    message_id=11,
                )
            )

        asyncio.run(_go())
        assert sess._gp.steered == []  # NOT steered
        assert [t for _ts, t, _ in sess.queued] == ["check disk after"]  # queued, stripped

    def test_steer_override_forces_steer_in_queue_mode(self) -> None:
        # "/steer …" folds into the running turn even though the global mode is
        # queue; the directive is stripped from the steered text.
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        d.cfg.messaging.queue_mode = "queue"
        _prime_live(d.cfg)

        async def _go() -> None:
            await d.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="/steer stop now",
                    message_id=12,
                )
            )

        asyncio.run(_go())
        assert sess._gp.steered == ["stop now"]  # steered, stripped
        assert sess.queued == []  # NOT queued
        assert cli.reactions == [(12, _STEER_ACK_EMOJI)]  # steer-ack on the steer message

    # -- the privacy confirmation follows the steer's result -------------------

    @staticmethod
    def _incognito_steer(steer_result):
        """A busy session in steer mode, a ``/incognito`` message, and a provider
        whose steer answers *steer_result* (a bool, or an exception to raise).
        Returns the dispatcher, the client and the provider's steer log."""
        from kiro_crew.messaging import privacy_mode

        privacy_mode.reset()
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        d.cfg.messaging.queue_mode = "steer"
        _prime_live(d.cfg)
        provider = sess._gp
        sent_at_steer: list[list[str]] = []

        async def _steer(text: str) -> bool:
            provider.steered.append(text)
            sent_at_steer.append([t for t, _ in cli.sent])
            if isinstance(steer_result, BaseException):
                raise steer_result
            return steer_result

        provider.steer = _steer  # type: ignore[method-assign]

        async def _go() -> None:
            await d.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="/incognito stop now",
                    message_id=12,
                )
            )

        return d, cli, sess, sent_at_steer, _go

    def test_a_steer_that_raises_keeps_the_mode_and_says_the_message_is_unconfirmed(
        self,
    ) -> None:
        """The reservation applies the mode ahead of the steer; the steer RAISES
        -- after its bytes may have reached the backend, so nobody knows whether
        the message is in the turn. Fail-closed: the mode stays on, and the user
        is told the mode is ON and that the message itself may not have run,
        through the same producer -- never that the mode was not applied (a
        message the backend records would then run unprotected). RED-BEFORE:
        the raise released the mode and announced "not made incognito"."""
        from kiro_crew.messaging import privacy_mode

        d, cli, sess, sent_at_steer, go = self._incognito_steer(RuntimeError("acp gone"))
        with pytest.raises(RuntimeError, match="acp gone"):
            asyncio.run(go())
        texts = [t for t, _ in cli.sent]
        assert texts == [
            f"{privacy_mode.NOTICE_INCOGNITO} {privacy_mode.NOTICE_UNCONFIRMED_SUFFIX}"
        ], f"a raised steer did not keep the mode and say the message is unconfirmed: sent={texts}"
        assert (
            list(privacy_mode._tracker("incognito")) != []
        ), "the mode was taken back over a message the backend may be recording"
        assert sess.queued == [], "a message whose steer raised was queued anyway"

    def test_a_raised_steer_whose_notice_also_fails_still_raises_its_own_error(
        self,
    ) -> None:
        """The steer RAISES and the confirmation the commit sends fails too (the
        Bot API is down). The commit records the mode BEFORE it sends, so the
        notice failure changes nothing about the mode -- and it must not replace
        the steer's own exception, which is what the caller diagnoses from.
        RED-BEFORE: the bare ``await commit(...)`` in the ``except BaseException``
        arm let the sender's error escape, so its ``raise`` never ran and the
        caller saw the notice failure instead of the steer's."""
        from kiro_crew.messaging import privacy_mode

        d, cli, sess, sent_at_steer, go = self._incognito_steer(RuntimeError("acp gone"))
        notices: list[str] = []

        async def _down(chat_id: int, text: str, **kw: Any) -> int:
            notices.append(text)
            raise OSError("bot api down")

        cli.send_message = _down  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="acp gone"):
            asyncio.run(go())
        assert notices == [
            f"{privacy_mode.NOTICE_INCOGNITO} {privacy_mode.NOTICE_UNCONFIRMED_SUFFIX}"
        ], f"the unconfirmed notice was not the one attempted: {notices}"
        assert (
            list(privacy_mode._tracker("incognito")) != []
        ), "a failed notice took the mode back over a message the backend may be recording"
        assert not [
            k for k in privacy_mode._pending if k[0] == "incognito"
        ], "the reservation was left pending after its commit"
        assert sess.queued == [], "a message whose steer raised was queued anyway"

    def test_a_steer_that_declines_confirms_nothing_here(self) -> None:
        """The provider declines the steer: the message falls through to the queue
        and runs at the drain, where the modifier is applied and announced. This
        path says nothing about the mode -- a confirmation here, for a message
        that has not run, would be false -- and takes the reservation back."""
        from kiro_crew.messaging import privacy_mode

        d, cli, sess, sent_at_steer, go = self._incognito_steer(False)
        asyncio.run(go())
        texts = [t for t, _ in cli.sent]
        assert (
            privacy_mode.NOTICE_INCOGNITO not in texts
        ), f"a declined steer left a confirmation: sent={texts}"
        assert [text for _, text, _ in sess.queued] == ["stop now"], sess.queued
        assert list(privacy_mode._tracker("incognito")) == [], "the mode was not taken back"

    def test_a_steer_that_lands_is_confirmed_once_after_it_landed(self) -> None:
        """The confirmation follows the steer's result: one notice, sent AFTER the
        provider reported the message in the turn -- never before it."""
        from kiro_crew.messaging import privacy_mode

        d, cli, sess, sent_at_steer, go = self._incognito_steer(True)
        asyncio.run(go())
        assert sess._gp.steered == ["stop now"]
        assert sent_at_steer == [
            []
        ], f"the confirmation was sent before the steer reported: at steer={sent_at_steer}"
        texts = [t for t, _ in cli.sent]
        assert texts == [
            privacy_mode.NOTICE_INCOGNITO
        ], f"one confirmation, after the steer: {texts}"
        assert list(privacy_mode._tracker("incognito")), "the steered message's mode was not kept"
        privacy_mode.reset()

    def test_busy_queue_mode_enqueues(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        d.cfg.messaging.queue_mode = "queue"
        _prime_live(d.cfg)

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="later"
                )
            )

        asyncio.run(_go())
        assert [text for _ts, text, _ in sess.queued] == ["later"]
        assert sess._gp.steered == []
        assert any("Queued" in t for t, _ in cli.sent)

    def test_not_busy_runs_a_full_turn(self) -> None:
        d, cli, sess = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="hello"
                )
            )

        asyncio.run(_go())
        assert sess.successes == ["telegram:kirocrew:direct:7"]
        assert sess._gp.steered == []

    def test_drain_collapses_queued_into_one_turn(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess.queued = [("t1", "first", _origin()), ("t2", "second", _origin())]

        async def _go() -> None:
            await d._drain_queue("telegram:kirocrew:direct:7")

        asyncio.run(_go())
        # All queued messages collapse into ONE combined turn (drain=False ->
        # no recursion), and the queue is emptied.
        assert sess.successes == ["telegram:kirocrew:direct:7"]
        assert sess.queued == []

    def test_queue_receipt_collapses_into_single_bubble(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        d.cfg.messaging.queue_mode = "queue"
        _prime_live(d.cfg)

        async def _go() -> None:
            for t in ("what time is it", "and the weather?"):
                await d.handle_message(
                    InboundMessage(
                        channel_type="telegram", user_id="7", conversation_id="7", text=t
                    )
                )

        asyncio.run(_go())
        # One receipt bubble is created, then edited in place to grow to (2) --
        # not one fresh "Queued" bubble per message.
        receipts = [t for t, _ in cli.sent if "Queued" in t]
        assert len(receipts) == 1 and "(1)" in receipts[0]
        grows = [txt for _mid, txt, _ in cli.edits if "Queued" in txt]
        assert any("(2)" in g for g in grows)
        assert [text for _ts, text, _ in sess.queued] == [
            "what time is it",
            "and the weather?",
        ]

    def test_stop_cancels_running_turn_and_clears_queue(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        sess.queued = [("t1", "pending", {})]

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="/stop"
                )
            )

        asyncio.run(_go())
        assert sess._gp.cancelled == 1  # in-flight turn aborted
        assert sess.queued == []  # pending queue cleared
        assert any("Stopped" in t for t, _ in cli.sent)

    def test_concurrent_queue_adds_share_one_receipt(self, monkeypatch: Any) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        d.cfg.messaging.queue_mode = "queue"
        _prime_live(d.cfg)
        # This test starts four first-use inbound checks concurrently. Keep the
        # receipt race isolated from governance's deliberately fail-closed lazy
        # profile load: otherwise whichever checks arrive while the first load is
        # in progress are denied before they ever reach the receipt queue.
        monkeypatch.setattr(
            "kiro_crew.telegram.transport_dispatch.channel_inbound_permitted",
            AsyncMock(return_value=True),
        )

        async def _go() -> None:
            await asyncio.gather(
                *[
                    d.handle_message(
                        InboundMessage(
                            channel_type="telegram",
                            user_id="7",
                            conversation_id="7",
                            text=f"m{i}",
                        )
                    )
                    for i in range(4)
                ]
            )

        asyncio.run(_go())
        # The receipt lock serializes the check-then-send: exactly ONE receipt
        # bubble is created; the other three grow it via edits (no orphans).
        sends = [t for t, _ in cli.sent if "Queued" in t]
        grows = [txt for _mid, txt, _ in cli.edits if "Queued" in txt]
        assert len(sends) == 1
        assert len(grows) == 3

    def test_drain_caps_collapse_and_drains_remainder_in_order(self) -> None:
        d, cli, sess = _dispatcher({7})
        # 52 queued -> the collapse cap (50) puts 50 into the first turn and
        # defers the 2-message remainder. The drain then keeps pumping, so the
        # remainder runs in a SECOND turn of the same drain rather than waiting
        # for unrelated future input. A 2+ item remainder is what exposes any
        # FIFO reordering of the surplus.
        sess.queued = [(f"t{i}", f"m{i}", _origin()) for i in range(52)]
        seen: list[str] = []
        original = d.handle_message

        async def _spy(msg, **kw):  # type: ignore[no-untyped-def]
            seen.append(msg.text)
            return None  # don't run a real turn

        async def _go() -> None:
            d.handle_message = _spy  # type: ignore[assignment]
            try:
                await d._drain_queue("telegram:kirocrew:direct:7")
            finally:
                d.handle_message = original  # type: ignore[assignment]

        asyncio.run(_go())

        assert len(seen) == 2, "cap-deferred surplus must drain in a second turn"
        # First turn takes exactly the cap, in order.
        assert seen[0].split("\n\n") == [f"m{i}" for i in range(50)]
        # Surplus drains next, IN ORIGINAL ORDER -- not dropped, not reordered.
        assert seen[1].split("\n\n") == ["m50", "m51"]
        assert not sess.queued, "the queue must be empty once the pump finishes"

    def test_flip_count_reflects_answered_not_full_queue(self) -> None:
        d, cli, sess = _dispatcher({7})
        key = "telegram:kirocrew:direct:7"
        sess._busy = True
        d.cfg.messaging.queue_mode = "queue"
        _prime_live(d.cfg)

        async def _go() -> None:
            # Build the receipt + queue via the real enqueue path (52 > cap 50).
            for i in range(52):
                await d._enqueue_with_receipt(key, 7, f"m{i}", origin=_tg_origin())
            sess._busy = False  # turn finished
            await d._drain_queue(key)

        asyncio.run(_go())
        flips = [txt for _mid, txt, _ in cli.edits if "Now answering" in txt]
        assert flips, "receipt should flip to answering"
        # Count reflects what THIS turn answers (50), not the full 52 queued,
        # and the 2-message remainder is called out rather than silently implied.
        assert "(50)" in flips[-1] and "+2 deferred" in flips[-1]

    def test_no_receipt_when_turn_finished_before_enqueue(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = False  # turn ended before the mid-turn message could queue

        async def _go() -> bool:
            return await d._enqueue_with_receipt(
                "telegram:kirocrew:direct:7", 7, "late message", origin=_tg_origin()
            )

        queued = asyncio.run(_go())
        # enqueue is a no-op once the semaphore is free -> not queued, and no
        # stale "Queued" receipt bubble is posted (the caller runs it fresh).
        assert queued is False
        assert sess.queued == []
        assert not any("Queued" in t for t, _ in cli.sent)


class TestMidTurnPrivacyTransaction:
    """A privacy modifier on a mid-turn message is one transaction with its steer.

    The mode is reserved before the steer, committed once it lands (or committed as
    unconfirmed when the steer raises or is cancelled, since its bytes may already be
    with the backend), and released only when the provider explicitly declines.
    """

    _KEY = "telegram:kirocrew:direct:7"

    @pytest.fixture(autouse=True)
    def _reset_modes(self) -> Any:
        from kiro_crew.messaging import privacy_mode

        privacy_mode.reset()
        yield
        privacy_mode.reset()

    @staticmethod
    def _unconfirmed() -> str:
        from kiro_crew.messaging import privacy_mode

        return f"{privacy_mode.NOTICE_INCOGNITO} {privacy_mode.NOTICE_UNCONFIRMED_SUFFIX}"

    def test_a_cancelled_steer_keeps_the_mode_and_reraises_the_cancellation(self) -> None:
        from kiro_crew.messaging import privacy_mode

        d, cli, sess, _at_steer, go = TestTelegramMidTurn._incognito_steer(asyncio.CancelledError())
        notes: list[str] = []
        d._active_renderers[self._KEY] = SimpleNamespace(note_steer=notes.append)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(go())

        assert [t for t, _ in cli.sent] == [self._unconfirmed()]
        assert list(privacy_mode._tracker("incognito")) != []
        assert not [k for k in privacy_mode._pending if k[0] == "incognito"]
        assert sess.queued == [] and cli.reactions == [] and notes == []

    def test_a_cancelled_steer_whose_notice_fails_still_raises_the_cancellation(self) -> None:
        from kiro_crew.messaging import privacy_mode

        d, cli, sess, _at_steer, go = TestTelegramMidTurn._incognito_steer(asyncio.CancelledError())

        async def _down(chat_id: int, text: str, **kw: Any) -> int:
            raise OSError("bot api down")

        cli.send_message = _down  # type: ignore[method-assign]

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(go())

        assert list(privacy_mode._tracker("incognito")) != []
        assert sess.queued == []

    def test_a_base_exception_from_the_steer_is_reraised_as_itself(self) -> None:
        from kiro_crew.messaging import privacy_mode

        class _Halt(BaseException):
            pass

        halt = _Halt()
        d, cli, sess, _at_steer, go = TestTelegramMidTurn._incognito_steer(halt)
        notes: list[str] = []
        d._active_renderers[self._KEY] = SimpleNamespace(note_steer=notes.append)

        with pytest.raises(_Halt) as raised:
            asyncio.run(go())

        assert raised.value is halt
        assert [t for t, _ in cli.sent] == [self._unconfirmed()]
        assert list(privacy_mode._tracker("incognito")) != []
        assert sess.queued == [] and cli.reactions == [] and notes == []

    def _record(self, monkeypatch: Any, d: Any, cli: Any, sess: Any, steer_result: Any) -> list:
        """Log reserve/steer/commit/release, sends, the chip and the reaction in order."""
        from kiro_crew.messaging import privacy_mode

        log: list[tuple[Any, ...]] = []
        real_reserve, real_commit, real_release = (
            privacy_mode.reserve,
            privacy_mode.commit,
            privacy_mode.release,
        )

        async def _reserve(mode: str, key: str, **kw: Any) -> Any:
            log.append(("reserve", mode, key, kw["caller"], kw["source"], kw["sessions"] is sess))
            return await real_reserve(mode, key, **kw)

        async def _commit(reservation: Any, **kw: Any) -> None:
            log.append(("commit", reservation.mode, reservation.session_key, kw))
            await real_commit(reservation, **kw)

        async def _release(reservation: Any, **kw: Any) -> None:
            log.append(("release", reservation.mode, reservation.session_key, kw))
            await real_release(reservation, **kw)

        monkeypatch.setattr(privacy_mode, "reserve", _reserve)
        monkeypatch.setattr(privacy_mode, "commit", _commit)
        monkeypatch.setattr(privacy_mode, "release", _release)
        real_send, real_react = cli.send_message, cli.set_message_reaction

        async def _send(chat_id: int, text: str, **kw: Any) -> int:
            log.append(("send", text))
            return await real_send(chat_id, text, **kw)

        async def _react(chat_id: int, mid: int, emoji: str) -> None:
            log.append(("react", mid, emoji))
            await real_react(chat_id, mid, emoji)

        cli.send_message = _send
        cli.set_message_reaction = _react

        async def _steer(text: str) -> Any:
            log.append(("steer", text))
            return steer_result

        sess._gp.steer = _steer
        d._active_renderers[self._KEY] = SimpleNamespace(
            note_steer=lambda text: log.append(("note", text))
        )
        return log

    def _send_busy(self, d: Any, text: str, message_id: int = 12) -> None:
        asyncio.run(
            d.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text=text,
                    message_id=message_id,
                )
            )
        )

    def test_a_landed_steer_commits_then_notes_then_reacts(self, monkeypatch: Any) -> None:
        from kiro_crew.messaging import privacy_mode

        d, cli, sess = _dispatcher({7})
        sess._busy = True
        log = self._record(monkeypatch, d, cli, sess, True)

        self._send_busy(d, "/incognito stop now")

        assert log == [
            ("reserve", "incognito", self._KEY, "7", "telegram", True),
            ("steer", "stop now"),
            ("commit", "incognito", self._KEY, {}),
            ("send", privacy_mode.NOTICE_INCOGNITO),
            ("note", "stop now"),
            ("react", 12, _STEER_ACK_EMOJI),
        ]
        assert sess.queued == []

    def test_a_plain_landed_steer_notes_then_reacts(self, monkeypatch: Any) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        log = self._record(monkeypatch, d, cli, sess, True)

        self._send_busy(d, "and also this", message_id=7)

        assert log == [("steer", "and also this"), ("note", "and also this"), ("react", 7, "🫡")]

    def test_a_declined_steer_releases_then_queues(self, monkeypatch: Any) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        log = self._record(monkeypatch, d, cli, sess, False)

        self._send_busy(d, "/incognito stop now")

        assert log == [
            ("reserve", "incognito", self._KEY, "7", "telegram", True),
            ("steer", "stop now"),
            (
                "release",
                "incognito",
                self._KEY,
                {"sessions": sess, "source": "telegram", "caller": "7"},
            ),
            ("send", "⏳ Queued (1): “stop now”"),
        ]
        assert sess.queued[-1][2]["privacy_request"] == "incognito"

    def test_a_truthy_steer_result_counts_as_landed(self, monkeypatch: Any) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        self._record(monkeypatch, d, cli, sess, "ok")
        self._send_busy(d, "x")
        assert sess.queued == [] and cli.reactions == [(12, _STEER_ACK_EMOJI)]

        d, cli, sess = _dispatcher({7})
        sess._busy = True
        self._record(monkeypatch, d, cli, sess, None)
        self._send_busy(d, "x")
        assert [t for _ts, t, _kw in sess.queued] == ["x"] and cli.reactions == []

    def test_a_reaction_failure_is_swallowed(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True

        async def _boom(chat_id: int, mid: int, emoji: str) -> None:
            raise RuntimeError("reactions unavailable")

        cli.set_message_reaction = _boom  # type: ignore[method-assign]

        self._send_busy(d, "x")

        assert sess._gp.steered == ["x"] and sess.queued == [] and cli.sent == []

    def test_a_landed_steer_whose_notice_fails_propagates_before_chip_and_reaction(
        self,
    ) -> None:
        from kiro_crew.messaging import privacy_mode

        d, cli, sess, _at_steer, go = TestTelegramMidTurn._incognito_steer(True)
        notes: list[str] = []
        d._active_renderers[self._KEY] = SimpleNamespace(note_steer=notes.append)

        async def _down(chat_id: int, text: str, **kw: Any) -> int:
            raise OSError("bot api down")

        cli.send_message = _down  # type: ignore[method-assign]

        with pytest.raises(OSError, match="bot api down"):
            asyncio.run(go())

        assert notes == [] and cli.reactions == []
        assert list(privacy_mode._tracker("incognito")) != []

    def test_an_already_marked_key_steers_without_a_notice(self) -> None:
        from kiro_crew.messaging import privacy_mode

        d, cli, sess = _dispatcher({7})
        sess._busy = True
        privacy_mode.mark_incognito(self._KEY)

        self._send_busy(d, "/incognito more")

        assert cli.sent == []
        assert sess._gp.steered == ["more"]
        assert cli.reactions == [(12, _STEER_ACK_EMOJI)]

    def test_a_reserve_refusal_returns_without_rerun_or_queue(self, monkeypatch: Any) -> None:
        from kiro_crew.messaging import privacy_mode

        d, cli, sess = _dispatcher({7})
        sess._busy = True
        refusal = AsyncMock(
            side_effect=privacy_mode.PrivacyModeRefused("incognito", self._KEY, "limit")
        )
        monkeypatch.setattr(privacy_mode, "reserve", refusal)
        calls: list[Any] = []
        original = d.handle_message

        async def _count(msg: Any, **kw: Any) -> None:
            calls.append(msg)
            await original(msg, **kw)

        d.handle_message = _count  # type: ignore[method-assign]

        self._send_busy(d, "/incognito stop now")

        assert len(calls) == 1
        assert refusal.await_count == 1
        assert sess._gp.steered == [] and sess.queued == []
        assert cli.sent == [] and cli.reactions == []

    @pytest.mark.parametrize("dead_turn", [True, False], ids=["dead-turn", "queue-mode"])
    def test_no_reservation_when_the_steer_is_unavailable(
        self, monkeypatch: Any, dead_turn: bool
    ) -> None:
        from kiro_crew.messaging import privacy_mode

        d, cli, sess = _dispatcher({7})
        sess._busy = True
        if dead_turn:
            sess._gp.active_turn = False
        else:
            d.cfg.messaging.queue_mode = "queue"
            _prime_live(d.cfg)
        reserve = AsyncMock(wraps=privacy_mode.reserve)
        monkeypatch.setattr(privacy_mode, "reserve", reserve)

        self._send_busy(d, "/incognito stop now")

        assert reserve.await_count == 0
        assert sess.queued[-1][1] == "stop now"
        assert sess.queued[-1][2]["privacy_request"] == "incognito"
        assert not privacy_mode.is_incognito(self._KEY)

    def test_the_steer_capability_is_read_once_before_the_reservation(self) -> None:
        from kiro_crew.messaging import privacy_mode

        class _Probe:
            def __init__(self) -> None:
                self.live_calls = 0
                self.flag_reads = 0
                self.steered: list[str] = []

            @property
            def supports_steer(self) -> bool:
                self.flag_reads += 1
                return True

            def has_active_turn(self) -> bool:
                self.live_calls += 1
                return self.live_calls == 1

            async def steer(self, text: str) -> bool:
                self.steered.append(text)
                return True

        d, cli, sess = _dispatcher({7})
        sess._busy = True
        probe = _Probe()
        sess._gp = probe

        self._send_busy(d, "/incognito stop now")

        assert (probe.live_calls, probe.flag_reads) == (1, 1)
        assert probe.steered == ["stop now"]
        assert [t for t, _ in cli.sent] == [privacy_mode.NOTICE_INCOGNITO]
        assert cli.reactions == [(12, _STEER_ACK_EMOJI)] and sess.queued == []

    def test_a_dead_turn_never_reads_the_steer_flag(self) -> None:
        class _Dead:
            flag_reads = 0

            @property
            def supports_steer(self) -> bool:
                type(self).flag_reads += 1
                return True

            def has_active_turn(self) -> bool:
                return False

            async def steer(self, text: str) -> bool:  # pragma: no cover - must not run
                raise AssertionError("a dead turn was steered")

        d, _cli, sess = _dispatcher({7})
        sess._busy = True
        sess._gp = _Dead()

        self._send_busy(d, "later")

        assert _Dead.flag_reads == 0
        assert [t for _ts, t, _kw in sess.queued] == ["later"]

    @pytest.mark.parametrize("mode", ["temporary", "incognito"])
    def test_a_bare_modifier_refusal_sends_nothing_more_and_runs_nothing(
        self, monkeypatch: Any, mode: str
    ) -> None:
        from kiro_crew.messaging import privacy_mode

        d, cli, sess = _dispatcher({7})
        calls: list[tuple[str, str, dict[str, Any]]] = []

        async def _refuse(applied_mode: str, key: str, **kw: Any) -> bool:
            calls.append((applied_mode, key, kw))
            raise privacy_mode.PrivacyModeRefused(applied_mode, key, "limit")

        monkeypatch.setattr(privacy_mode, "apply_mode", _refuse)

        asyncio.run(d.handle_message(_dm(f"/{mode}")))

        assert [(m, k) for m, k, _ in calls] == [(mode, self._KEY)]
        kwargs = calls[0][2]
        assert (kwargs["source"], kwargs["caller"], kwargs["sessions"] is sess) == (
            "telegram",
            "7",
            True,
        )
        assert callable(kwargs["notify"])
        assert cli.sent == []
        assert sess.begin_turns == 0 and sess.successes == [] and sess.requested_models == {}

    @pytest.mark.parametrize("mode", ["temporary", "incognito"])
    def test_a_bare_modifier_confirms_exactly_once_each_time(self, mode: str) -> None:
        from kiro_crew.messaging import privacy_mode

        literal = {
            "temporary": "🔒 Temporary mode ON — this thread won't read or save memory.",
            "incognito": "🕶️ Incognito mode ON — this thread can read memory but won't save anything.",
        }[mode]
        assert privacy_mode.notice(mode) == literal
        d, cli, _sess = _dispatcher({7})

        asyncio.run(d.handle_message(_dm(f"/{mode}")))
        asyncio.run(d.handle_message(_dm(f"/{mode}")))

        assert [t for t, _ in cli.sent] == [literal, literal]

    def test_a_bare_modifier_while_busy_applies_without_steer_or_queue(self) -> None:
        from kiro_crew.messaging import privacy_mode

        d, cli, sess = _dispatcher({7})
        sess._busy = True

        asyncio.run(d.handle_message(_dm("/incognito")))

        assert [t for t, _ in cli.sent] == [privacy_mode.NOTICE_INCOGNITO]
        assert privacy_mode.is_incognito(self._KEY)
        assert sess.queued == [] and sess._gp.steered == [] and cli.reactions == []

    def test_a_turn_path_refusal_runs_nothing(self, monkeypatch: Any) -> None:
        from kiro_crew.messaging import privacy_mode

        d, cli, sess = _dispatcher({7})
        monkeypatch.setattr(
            privacy_mode,
            "apply_mode",
            AsyncMock(
                side_effect=privacy_mode.PrivacyModeRefused("incognito", self._KEY, "persist")
            ),
        )

        asyncio.run(d.handle_message(_dm("/incognito summarise this")))

        assert cli.sent == []
        assert sess.requested_models == {} and sess.begin_turns == 0
        assert d.ctx_builder.build_calls == []
        assert d._active_renderers == {}
        assert sess.successes == [] and sess.released == []

    def test_the_turn_path_hydrates_then_applies_then_acquires(self, monkeypatch: Any) -> None:
        from kiro_crew.messaging import privacy_mode

        d, cli, sess = _dispatcher({7})
        log: list[tuple[Any, ...]] = []
        real_hydrate, real_apply = privacy_mode.hydrate, privacy_mode.apply_mode

        def _hydrate(sessions: Any, key: str) -> Any:
            log.append(("hydrate", key))
            return real_hydrate(sessions, key)

        async def _apply(mode: str, key: str, **kw: Any) -> bool:
            log.append(("apply", mode, key, kw["caller"], kw["source"]))
            return await real_apply(mode, key, **kw)

        real_acquire = sess.get_or_create

        async def _acquire(key: str, **kw: Any) -> Any:
            log.append(("acquire", key))
            return await real_acquire(key, **kw)

        monkeypatch.setattr(privacy_mode, "hydrate", _hydrate)
        monkeypatch.setattr(privacy_mode, "apply_mode", _apply)
        sess.get_or_create = _acquire  # type: ignore[method-assign]

        asyncio.run(d.handle_message(_dm("/incognito summarise this")))

        # Shared readers hydrate the key again later; the order that matters is the
        # dispatcher's own: its hydrate, then the one apply, then the acquire.
        apply = ("apply", "incognito", self._KEY, "7", "telegram")
        assert log[:2] == [("hydrate", self._KEY), apply]
        assert log.count(apply) == 1
        assert log.index(apply) < log.index(("acquire", self._KEY))
        texts = [t for t, _ in cli.sent]
        assert texts[0] == privacy_mode.NOTICE_INCOGNITO
        assert "Answer: summarise this" in (cli.final_text() or "")

    def test_an_already_marked_key_runs_the_turn_without_a_notice(self) -> None:
        from kiro_crew.messaging import privacy_mode

        d, cli, sess = _dispatcher({7})
        privacy_mode.mark_incognito(self._KEY)

        asyncio.run(d.handle_message(_dm("/incognito summarise this")))

        assert privacy_mode.NOTICE_INCOGNITO not in [t for t, _ in cli.sent]
        assert sess.successes == [self._KEY]
