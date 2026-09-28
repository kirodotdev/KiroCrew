"""The seam that lets one channel's queue drain wake another channel's.

Under ``messaging.dm_scope = "unified"`` one queue holds entries from several
transports: every DM dispatcher is built with the orchestrator's single
``SessionManager``, and ``build_dm_session_key`` reduces a direct chat's bucket to
``unified:{agent}``, dropping the CHANNEL as well as the user. A drain can only answer
the entries its own channel recorded, so it sets the others aside -- and because those
were already accepted and receipted, something has to come back for them.

Covered here: the registry's own rules (who is woken, who is skipped, what a raising
drain costs), the cascade terminating, and the two dispatchers driving each other end
to end through a genuinely shared queue.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_discord import _dispatcher as _dc_dispatcher
from test_telegram import _dispatcher as _tg_dispatcher

from kiro_crew.messaging import queue_drain
from kiro_crew.messaging.link import build_dm_session_key
from kiro_crew.messaging.queue_drain import (
    draining,
    register_drain,
    reset_drains,
    wake_other_drains,
)
from kiro_crew.telegram.transport import TelegramInboundMessage

#: The key every allow-listed person's DM collapses onto under a unified scope. Derived
#: rather than spelled, so a change to the key shape moves these tests with it.
_UNIFIED = build_dm_session_key("telegram", "kirocrew", "7", dm_scope="unified", chat_type="direct")


@pytest.fixture(autouse=True)
def _clean_registry():
    """A registration is process-wide; a test must not inherit another's dispatcher."""
    reset_drains()
    yield
    reset_drains()


def _tg_msg(user: int, chat: int, text: str, *, message_id: int = 0) -> Any:
    return TelegramInboundMessage(
        channel_type="telegram",
        user_id=str(user),
        conversation_id=str(chat),
        text=text,
        message_id=message_id,
        chat_type="private",
    )


class TestTheKeyIsGenuinelyShared:
    """The premise, measured rather than asserted from the issue text."""

    def test_two_transports_resolve_one_session_key_under_a_unified_scope(self) -> None:
        telegram = build_dm_session_key(
            "telegram", "kirocrew", "7", dm_scope="unified", chat_type="direct"
        )
        discord = build_dm_session_key(
            "discord", "kirocrew", "u1", dm_scope="unified", chat_type="direct"
        )
        assert telegram == discord == "unified:kirocrew", "one key, therefore one queue"

    def test_the_default_scope_keeps_them_apart(self) -> None:
        """Without ``unified`` there is no shared queue and nothing to wake."""
        telegram = build_dm_session_key("telegram", "kirocrew", "7")
        discord = build_dm_session_key("discord", "kirocrew", "u1")
        assert telegram != discord


class TestTheRegistry:
    """Who gets woken, who is skipped, and what a failing peer costs."""

    @pytest.mark.asyncio
    async def test_a_registered_peer_is_woken_with_the_session_key(self) -> None:
        seen: list[str] = []

        async def _drain(key: str) -> None:
            seen.append(key)

        register_drain("discord", _drain)

        await wake_other_drains(waker="telegram", session_key=_UNIFIED, channels={"discord"})

        assert seen == [_UNIFIED], "the peer is told WHICH queue, and nothing else"

    @pytest.mark.asyncio
    async def test_the_waker_never_wakes_itself(self) -> None:
        """Its own entries are already drained by its own pump."""
        calls: list[str] = []

        async def _drain(key: str) -> None:
            calls.append(key)

        register_drain("telegram", _drain)

        await wake_other_drains(waker="telegram", session_key=_UNIFIED, channels={"telegram"})

        assert calls == []

    @pytest.mark.asyncio
    async def test_an_unregistered_channel_is_skipped_not_raised_on(self) -> None:
        """Its dispatcher is not running in this process; there is nothing to call.

        The empty channel in the set is the untagged case: an entry that names no owner
        cannot name a wake target either, and must not raise on the way past.
        """
        assert "discord" not in queue_drain._DRAINS

        await wake_other_drains(waker="telegram", session_key=_UNIFIED, channels={"discord", ""})

    @pytest.mark.asyncio
    async def test_a_drain_that_raises_is_retired_so_it_is_not_woken_again(self) -> None:
        """A dead dispatcher woken twice loses messages twice.

        Its pump dequeues what it owns under its own lock and replays AFTER releasing it,
        so a closed client raising in the replay drops messages it has already taken off
        the queue. Once a drain has raised it has proven it cannot serve this queue, so it
        is retired: the channel is then simply absent, which is the same path as one whose
        dispatcher never ran here -- entries wait instead of being consumed.
        """
        from kiro_crew.messaging import queue_drain as qd

        calls: list[str] = []

        async def _dead(session_key: str) -> None:
            calls.append(session_key)
            raise RuntimeError("client is closed")

        register_drain("discord", _dead)

        await wake_other_drains(waker="telegram", session_key=_UNIFIED, channels={"discord"})
        await wake_other_drains(waker="telegram", session_key=_UNIFIED, channels={"discord"})

        assert calls == [_UNIFIED], "woken once, then retired"
        assert "discord" not in qd._DRAINS

    @pytest.mark.asyncio
    async def test_retiring_does_not_remove_a_dispatcher_that_restarted_meanwhile(
        self,
    ) -> None:
        """A restart registers the replacement before the old one's failure is handled.

        Dropping unconditionally there would remove the LIVE registration and leave the
        channel unwakeable until its next restart -- the stranding this module exists to
        prevent. The drop compares first, so a stale failure is a no-op.
        """
        from kiro_crew.messaging import queue_drain as qd

        async def _fresh(session_key: str) -> None: ...

        async def _dying(session_key: str) -> None:
            # The replacement dispatcher comes up while this one is failing.
            register_drain("discord", _fresh)
            raise RuntimeError("old client is closed")

        register_drain("discord", _dying)

        await wake_other_drains(waker="telegram", session_key=_UNIFIED, channels={"discord"})

        assert qd._DRAINS.get("discord") is _fresh, "the live replacement survives"

    @pytest.mark.asyncio
    async def test_hitting_the_round_cap_leaves_the_wake_owed(self, caplog) -> None:
        """The last round cannot serve a wake, so it must not consume the request either.

        Taking it there would leave the entry queued AND erase the only record that
        anything is owed for it, so not even a later peer drain would know to look. Left
        in place, the next drain on this session finds it and re-pumps.
        """
        import logging

        from kiro_crew.messaging import queue_drain as qd

        rounds = {"n": 0}

        async def _pump(foreign: set[str]) -> None:
            rounds["n"] += 1
            # A peer asks to wake us on EVERY round, so the cap is what ends the loop.
            qd._PENDING.setdefault(_UNIFIED, set()).add("telegram")

        with caplog.at_level(logging.WARNING, logger="kiro_crew.messaging.queue_drain"):
            await qd.drain_until_quiet(channel="telegram", session_key=_UNIFIED, pump=_pump)

        assert rounds["n"] == qd._MAX_WAKE_ROUNDS, "the cap bounds the rounds"
        assert qd._peek_pending("telegram", _UNIFIED), "and the owed wake survives the exit"
        assert len(caplog.records) == 1, "and the operator is told once, not per round"

    @pytest.mark.asyncio
    async def test_a_channel_named_twice_is_woken_once(self) -> None:
        """The parameter is an ``Iterable``, so a repeat is a caller shape, not a bug.

        Production passes a set, but the signature does not require one, and waking a
        peer twice runs that channel's whole turn twice -- it would answer its queue,
        find it empty, and post a second time for entries it already handled. The
        de-duplication is what makes the looser parameter type safe to offer.
        """
        seen: list[str] = []

        async def _drain(session_key: str) -> None:
            seen.append(session_key)

        register_drain("discord", _drain)

        await wake_other_drains(
            waker="telegram", session_key=_UNIFIED, channels=["discord", "discord"]
        )

        assert seen == [_UNIFIED], "named twice, woken once"

    @pytest.mark.asyncio
    async def test_a_channel_that_is_not_running_is_not_reported_as_a_FAILED_wake(
        self, caplog
    ) -> None:
        """Skipping it is normal; logging a failure for it sends an operator chasing nothing.

        Without the registry check the lookup raises ``KeyError`` INSIDE the per-peer
        try/except, which swallows it -- so the observable outcome is still "skipped", and
        only the warning distinguishes a channel that is not deployed from a live channel
        whose drain actually broke.
        """
        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.messaging.queue_drain"):
            await wake_other_drains(waker="telegram", session_key=_UNIFIED, channels={"discord"})

        assert caplog.records == [], "a channel that is not deployed is not a wake failure"

    @pytest.mark.asyncio
    async def test_a_peer_whose_client_is_not_connected_does_not_strand_the_others(
        self,
    ) -> None:
        """One channel raising must not cost the rest their wake.

        A dispatcher constructed without a live client raises on its first send, and the
        waker has already finished its own work by then -- so swallowing per peer is the
        only behaviour that keeps the other channels' accepted messages moving.
        """
        order: list[str] = []

        async def _broken(key: str) -> None:
            order.append("discord")
            raise AssertionError("client is not connected")

        async def _healthy(key: str) -> None:
            order.append("teams")

        register_drain("discord", _broken)
        register_drain("teams", _healthy)

        await wake_other_drains(
            waker="telegram", session_key=_UNIFIED, channels=["discord", "teams"]
        )

        assert order == ["discord", "teams"], "the raise must not stop the loop"

    @pytest.mark.asyncio
    async def test_a_channel_already_draining_this_session_is_not_re_entered(self) -> None:
        """This is what makes the cascade terminate.

        A woken drain sets aside the waker's own newly queued entries and would wake it
        straight back. Refusing a target already on the stack stops that; those entries
        drain at their own channel's next turn tail, the ordinary contract for any
        mid-turn message.
        """
        calls: list[str] = []

        async def _drain(key: str) -> None:
            calls.append(key)

        register_drain("telegram", _drain)

        with draining("telegram", _UNIFIED):
            await wake_other_drains(waker="discord", session_key=_UNIFIED, channels={"telegram"})

        assert calls == []

    @pytest.mark.asyncio
    async def test_the_guard_is_per_session_not_global(self) -> None:
        """Another conversation's drain is unrelated and must still be wakeable."""
        calls: list[str] = []

        async def _drain(key: str) -> None:
            calls.append(key)

        register_drain("telegram", _drain)

        with draining("telegram", _UNIFIED):
            await wake_other_drains(
                waker="discord", session_key="telegram:kirocrew:direct:9", channels={"telegram"}
            )

        assert calls == ["telegram:kirocrew:direct:9"]

    @pytest.mark.asyncio
    async def test_a_mutually_waking_pair_terminates(self) -> None:
        """Two peers that each wake the other must not recurse without end."""
        depth = {"n": 0, "max": 0}

        async def _make(channel: str, peer: str):
            async def _drain(key: str) -> None:
                depth["n"] += 1
                depth["max"] = max(depth["max"], depth["n"])
                assert depth["n"] < 10, "the cascade did not terminate"
                with draining(channel, key):
                    await wake_other_drains(waker=channel, session_key=key, channels={peer})
                depth["n"] -= 1

            return _drain

        register_drain("telegram", await _make("telegram", "discord"))
        register_drain("discord", await _make("discord", "telegram"))

        with draining("telegram", _UNIFIED):
            await wake_other_drains(waker="telegram", session_key=_UNIFIED, channels={"discord"})

        assert depth["max"] == 1, "discord ran once and could not wake telegram back"
        assert depth["n"] == 0, "and every frame unwound"

    def test_the_marker_is_released_even_when_the_drain_raises(self) -> None:
        """A channel left marked active would be unwakeable for the rest of the process."""
        with pytest.raises(RuntimeError):
            with draining("telegram", _UNIFIED):
                raise RuntimeError("boom")

        assert queue_drain._ACTIVE.get(_UNIFIED) in (None, set())

    def test_a_later_dispatcher_replaces_an_earlier_registration(self) -> None:
        """One dispatcher per channel per process: a stale one holds a dead client."""

        async def _first(key: str) -> None: ...

        async def _second(key: str) -> None: ...

        register_drain("discord", _first)
        register_drain("discord", _second)

        assert set(queue_drain._DRAINS) == {"discord"}
        assert queue_drain._DRAINS["discord"] is _second


class TestTheTwoDispatchersDriveEachOther:
    """End to end on ONE genuinely shared queue, through both real drains."""

    @staticmethod
    def _pair() -> tuple[Any, Any, Any, Any]:
        """A Telegram and a Discord dispatcher over one shared fake session store.

        The shared store is the whole point: in production both dispatchers are handed
        the orchestrator's single ``SessionManager``, which is why one queue holds both
        transports' entries.
        """
        tg, tg_cli, sess = _tg_dispatcher({7}, dm_scope="unified")
        dc, dc_cli, _ = _dc_dispatcher({"u1"}, dm_scope="unified")
        dc.sessions = sess  # one store, one queue -- as the gateways wire it
        return tg, dc, sess, (tg_cli, dc_cli)

    @staticmethod
    def _spy(dispatcher: Any) -> list[Any]:
        seen: list[Any] = []

        async def _handle(msg: Any, **kw: Any) -> None:
            seen.append(msg)

        dispatcher.handle_message = _handle
        return seen

    @pytest.mark.asyncio
    async def test_a_telegram_drain_wakes_discord_for_the_entry_it_cannot_answer(
        self,
    ) -> None:
        """The finding this seam closes: an accepted, receipted entry left unanswered."""
        tg, dc, sess, _ = self._pair()
        tg_seen = self._spy(tg)
        dc_seen = self._spy(dc)

        sess._busy = True
        assert await tg._enqueue_with_receipt(
            _UNIFIED, 70, "from telegram", origin=_tg_origin_for(7, 70)
        )
        assert await dc._enqueue_with_receipt(
            _UNIFIED, "c1", "from discord", origin=_dc_origin_for("u1", "c1")
        )
        sess._busy = False

        await tg._drain_queue(_UNIFIED)

        assert [m.text for m in tg_seen] == ["from telegram"], "telegram answers its own"
        assert [m.text for m in dc_seen] == ["from discord"], "and hands the rest back"
        assert [m.user_id for m in dc_seen] == ["u1"]
        assert [m.conversation_id for m in dc_seen] == ["c1"]
        assert sess.queued == [], "nothing is left queued once both drains have run"

    @pytest.mark.asyncio
    async def test_the_wake_works_in_the_other_direction_too(self) -> None:
        """Neither channel is privileged; whichever turn ends first wakes the other."""
        tg, dc, sess, _ = self._pair()
        tg_seen = self._spy(tg)
        dc_seen = self._spy(dc)

        sess._busy = True
        assert await dc._enqueue_with_receipt(
            _UNIFIED, "c1", "from discord", origin=_dc_origin_for("u1", "c1")
        )
        assert await tg._enqueue_with_receipt(
            _UNIFIED, 70, "from telegram", origin=_tg_origin_for(7, 70)
        )
        sess._busy = False

        await dc._drain_queue(_UNIFIED)

        assert [m.text for m in dc_seen] == ["from discord"]
        assert [m.text for m in tg_seen] == ["from telegram"]
        assert sess.queued == []

    @pytest.mark.asyncio
    async def test_each_channel_keeps_its_own_senders_in_order(self) -> None:
        """FIFO holds inside a channel; interleaving across channels does not reorder it.

        Order between two transports is not something either sender can observe -- they
        are in different apps -- but order within one channel's own messages is, and the
        foreign entries sitting between them must not disturb it.
        """
        tg, dc, sess, _ = self._pair()
        tg_seen = self._spy(tg)
        dc_seen = self._spy(dc)

        sess._busy = True
        for i in range(3):
            assert await tg._enqueue_with_receipt(
                _UNIFIED, 70, f"tg{i}", origin=_tg_origin_for(7, 70)
            )
            assert await dc._enqueue_with_receipt(
                _UNIFIED, "c1", f"dc{i}", origin=_dc_origin_for("u1", "c1")
            )
        sess._busy = False

        await tg._drain_queue(_UNIFIED)

        # One sender per channel, so each channel's three collapse into one turn, in
        # arrival order.
        assert [m.text for m in tg_seen] == ["tg0\n\ntg1\n\ntg2"]
        assert [m.text for m in dc_seen] == ["dc0\n\ndc1\n\ndc2"]
        assert sess.queued == []

    @pytest.mark.asyncio
    async def test_the_peer_cannot_re_enter_this_drain_so_the_depth_is_bounded(self) -> None:
        """The wake runs INSIDE the waker's active marker, which is what bounds it.

        A woken peer sets aside whatever this channel queued during its turn and would
        wake this drain straight back. The marker makes that target refuse, so each
        drain frame costs at most one hop per peer. Waking after the marker released
        would let the peer re-enter, and with messages still arriving the hops would
        not be bounded by anything.

        Refusing the wake is not the same as dropping it. The refused request is
        retained, and this drain re-pumps for it before releasing its marker -- so the
        entry is answered HERE, in this frame, rather than waiting for a next turn that
        a quiet channel never has. That is what keeps the bound safe to hold.
        """
        tg, dc, sess, _ = self._pair()
        tg_seen = self._spy(tg)
        depth = {"n": 0, "max": 0}
        real_dc_drain = dc._drain_queue

        async def _counted(key: str) -> None:
            depth["n"] += 1
            depth["max"] = max(depth["max"], depth["n"])
            assert depth["n"] < 5, "the cascade did not terminate"
            try:
                await real_dc_drain(key)
            finally:
                depth["n"] -= 1

        register_drain("discord", _counted)

        # A Telegram message arrives WHILE Discord's woken turn runs. That is the
        # condition that would recurse: Discord's pump then sets aside a Telegram
        # entry and has a channel to wake back. Enqueued once, so the queue cannot
        # grow without end whichever way the guard falls.
        late = {"sent": False}

        async def _dc_handle(msg: Any, **kw: Any) -> None:
            if not late["sent"]:
                late["sent"] = True
                sess.enqueue(
                    _UNIFIED,
                    "late",
                    "arrived during their turn",
                    force=True,
                    **_tg_entry_kwargs(7, 70),
                )

        dc.handle_message = _dc_handle

        sess._busy = True
        assert await tg._enqueue_with_receipt(_UNIFIED, 70, "mine", origin=_tg_origin_for(7, 70))
        assert await dc._enqueue_with_receipt(
            _UNIFIED, "c1", "theirs", origin=_dc_origin_for("u1", "c1")
        )
        sess._busy = False

        await tg._drain_queue(_UNIFIED)

        assert late["sent"], "the test must actually create the re-entry condition"
        assert depth["max"] == 1, "discord ran once and could not wake telegram back"
        assert depth["n"] == 0, "and every frame unwound"
        assert [m.text for m in tg_seen] == [
            "mine",
            "arrived during their turn",
        ], "the retained wake is answered in this drain, not left for a next turn"
        assert sess.queued == [], "nothing accepted is left waiting"

    @pytest.mark.asyncio
    async def test_one_retained_wake_costs_exactly_one_extra_round(self) -> None:
        """Consuming the retention is what ends the loop, and the peer is woken once.

        Reading the retention without clearing it would keep re-pumping to the safety cap
        and log a warning for work that was already done. Carrying an earlier round's
        set-aside channels forward would wake that peer again on every round, running a
        whole turn for a queue it has already emptied. Two rounds, one wake.
        """
        tg, dc, sess, _ = self._pair()
        rounds = {"n": 0}
        wakes: list[str] = []
        real_pump = tg._pump_queue

        async def _counted_pump(key: str, foreign: set[str]) -> None:
            rounds["n"] += 1
            await real_pump(key, foreign)

        tg._pump_queue = _counted_pump

        # The REAL peer drain, because the retention is written by the peer's own
        # ``wake_other_drains`` call -- a stub that never wakes records nothing, and the
        # test would then pass for the wrong reason.
        real_dc_drain = dc._drain_queue
        late = {"sent": False}

        async def _dc_drain(key: str) -> None:
            wakes.append(key)
            await real_dc_drain(key)

        async def _dc_handle(msg: Any, **kw: Any) -> None:
            if not late["sent"]:
                late["sent"] = True
                sess.enqueue(
                    _UNIFIED, "late", "after their turn", force=True, **_tg_entry_kwargs(7, 70)
                )

        dc.handle_message = _dc_handle
        register_drain("discord", _dc_drain)

        sess._busy = True
        assert await tg._enqueue_with_receipt(_UNIFIED, 70, "mine", origin=_tg_origin_for(7, 70))
        assert await dc._enqueue_with_receipt(
            _UNIFIED, "c1", "theirs", origin=_dc_origin_for("u1", "c1")
        )
        sess._busy = False

        await tg._drain_queue(_UNIFIED)

        assert late["sent"], "the test must actually create the retained-wake condition"
        assert rounds["n"] == 2, "the first pump, then exactly one re-pump for the retention"
        assert wakes == [_UNIFIED], "and the peer was woken once, not once per round"
        assert sess.queued == [], "nothing accepted is left waiting"

    @pytest.mark.asyncio
    async def test_a_woken_peer_that_fails_leaves_the_wakers_own_work_done(self) -> None:
        """The waker has already answered its own messages by the time it wakes a peer."""
        tg, dc, sess, _ = self._pair()
        tg_seen = self._spy(tg)

        async def _explode(key: str) -> None:
            raise AssertionError("discord client is not connected")

        register_drain("discord", _explode)

        sess._busy = True
        assert await tg._enqueue_with_receipt(_UNIFIED, 70, "mine", origin=_tg_origin_for(7, 70))
        assert await dc._enqueue_with_receipt(
            _UNIFIED, "c1", "theirs", origin=_dc_origin_for("u1", "c1")
        )
        sess._busy = False

        await tg._drain_queue(_UNIFIED)

        assert [m.text for m in tg_seen] == ["mine"], "telegram's own turn still ran"
        assert [text for _ts, text, _kw in sess.queued] == ["theirs"], "and theirs is kept"

    @pytest.mark.asyncio
    async def test_an_entry_naming_no_channel_is_kept_without_a_wake(self) -> None:
        """An untagged entry is not lost, but nothing can be woken for it either.

        All four channels that drain this queue tag their entries now, so this is the
        fifth-channel case: a new producer that forgot. There is nothing naming its
        owner, so it stays queued for that channel's own next turn rather than being
        answered here under an address this drain guessed.
        """
        tg, dc, sess, _ = self._pair()
        tg_seen = self._spy(tg)
        dc_seen = self._spy(dc)
        sess.queued = [("t0", "from a new channel", {"someother_conversation_id": "CONV"})]

        await tg._drain_queue(_UNIFIED)

        assert tg_seen == [] and dc_seen == []
        assert [text for _ts, text, _kw in sess.queued] == ["from a new channel"]


class TestTheContractCoversEveryDrainingChannel:
    """Four channels drain this queue. The contract is only worth anything on all four.

    The finding that forced these: two of the four tagged their entries, so a Teams or
    Webex entry read as belonging to NOBODY. Telegram and Discord set it aside correctly
    and then had no owner to wake, so an accepted, receipted message waited for its own
    channel to finish some unrelated turn -- forever if that user went quiet.
    """

    #: Every dispatcher module with a ``_drain_queue`` on the shared mid-turn queue.
    #: Spelled out rather than discovered, so ADDING a fifth draining channel without
    #: adding it here is the thing that fails.
    _MODULES = {
        "telegram": "kiro_crew.telegram.transport_dispatch",
        "discord": "kiro_crew.discord.transport_dispatch",
        "teams": "kiro_crew.teams.transport_dispatch",
        "webex": "kiro_crew.webex.transport_dispatch",
    }

    def test_the_enumeration_matches_the_modules_that_actually_drain(self) -> None:
        """Guards the list above: a fifth draining channel must not slip past it."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        draining_modules = {
            f"kiro_crew.{p.relative_to(root).with_suffix('').as_posix().replace('/', '.')}"
            for p in root.rglob("transport_dispatch.py")
            if "async def _drain_queue" in p.read_text(encoding="utf-8")
        }
        assert draining_modules == set(self._MODULES.values())

    def test_each_channel_names_itself_once_and_the_names_are_distinct(self) -> None:
        """One ``_CHANNEL`` per module, used for BOTH the tag and the registration.

        A tag that disagrees with the registration name is the silent failure this
        pins: entries nothing can be woken for, with nothing raising.
        """
        import importlib

        seen = {}
        for expected, dotted in self._MODULES.items():
            mod = importlib.import_module(dotted)
            assert mod._CHANNEL == expected, f"{dotted} names itself {mod._CHANNEL!r}"
            seen[mod._CHANNEL] = dotted
        assert len(seen) == len(self._MODULES), "two channels sharing a name share a drain"

    def test_the_neutral_key_is_defined_in_exactly_one_place(self) -> None:
        """The Design finding: a restated literal makes one typo strand a channel.

        Asserted on the SOURCE rather than on behaviour, because a per-module copy that
        happens to be spelled right behaves identically -- the defect is that the next
        copy can be spelled wrong, and no test would go red.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        definers = sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("*.py")
            if '"queued_channel"' in p.read_text(encoding="utf-8")
        )
        assert definers == ["messaging/queue_drain.py"]

    def test_every_producer_tags_with_its_own_channel_name(self) -> None:
        """Read off the real producers, so a tag written by hand cannot pass this."""
        from kiro_crew.discord.transport_dispatch import _origin_kwargs as dc_kwargs
        from kiro_crew.messaging.queue_drain import QUEUED_CHANNEL_KEY, entry_channel
        from kiro_crew.telegram.transport_dispatch import _origin_kwargs as tg_kwargs

        assert entry_channel(tg_kwargs(_tg_origin_for(7, 70))) == "telegram"
        assert entry_channel(dc_kwargs(_dc_origin_for("u1", "c1"))) == "discord"
        # And the key itself is the neutral one every drain reads.
        assert QUEUED_CHANNEL_KEY in tg_kwargs(_tg_origin_for(7, 70))

    def test_an_entry_is_owned_by_exactly_one_channel(self) -> None:
        """Each drain's ownership read accepts its own tag and refuses the other three."""
        import importlib

        from kiro_crew.messaging.queue_drain import tag_entry

        readers = {
            "telegram": ("_queued_origin", {"telegram_user_id": "7"}),
            "discord": ("_queued_origin", {"discord_user_id": "u1"}),
            "teams": ("_queued_origin", {"teams_conversation_id": "C"}),
            "webex": ("_queued_place", {"webex_room_id": "R"}),
        }
        for owner, (fn_name, _payload) in readers.items():
            read = getattr(importlib.import_module(self._MODULES[owner]), fn_name)
            for other in readers:
                if other == owner:
                    continue
                assert read(tag_entry({}, other, "")) is None, f"{owner} claimed {other}'s entry"


def _tg_origin_for(user: int, chat: int) -> Any:
    from kiro_crew.telegram.transport_dispatch import _QueuedOrigin as TgOrigin

    return TgOrigin(
        user_id=str(user), chat_id=str(chat), thread_id="", chat_type="private", username=""
    )


def _tg_entry_kwargs(user: int, chat: int) -> dict:
    """A Telegram queue entry's payload, spelled by the PRODUCTION writer."""
    from kiro_crew.telegram.transport_dispatch import _origin_kwargs

    return _origin_kwargs(_tg_origin_for(user, chat))


def _dc_origin_for(user: str, channel: str) -> Any:
    from kiro_crew.discord.transport_dispatch import _QueuedOrigin as DcOrigin

    return DcOrigin(user_id=user, channel_id=channel, thread_id="")
