"""After a code channel's Stop, nothing it stopped starts again until a person writes
(RFC ``rfc-slack-code-channels`` §3 "Stop", "What happens next"; ``stop_hold``).

Every row of the hold's await table gets an interleaving test: the path runs under
:func:`_stepped`, which presses the Stop at one suspension point of the path and
then at the next, through all of them, and the outcome is the same wherever the
Stop lands. A row with no await between its decision and its effect is checked
with the Stop pressed before and after the call.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_runner_coverage_helpers import _slot, _state
from slack_code_channel_surface_helpers import env_fixture  # noqa: F401 - the ``env`` fixture

from kiro_crew.dashboard import chat_runner
from kiro_crew.slack.code_channels import stop_hold

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_pending() -> Any:
    stop_hold._keys.clear()
    stop_hold._channels.clear()
    yield
    stop_hold._keys.clear()
    stop_hold._channels.clear()


async def _stepped(coro: Any, at: int, stop: Any) -> tuple[Any, int]:
    """Run *coro* to its end, calling *stop* at its *at*-th suspension (none for ``-1``).

    Returns its result and how many times it suspended. Each yielded future is awaited
    here, so the coroutine resumes exactly as its task would have resumed it.
    """
    value: Any = None
    exc: BaseException | None = None
    count = 0
    while True:
        try:
            yielded = coro.throw(exc) if exc is not None else coro.send(value)
        except StopIteration as done:
            if count <= at:
                stop()  # it suspended fewer times this run: the Stop lands after it
            return done.value, count
        value, exc = None, None
        if count == at:
            stop()
        count += 1
        try:
            if yielded is None:
                await asyncio.sleep(0)
            else:
                yielded._asyncio_future_blocking = False
                value = await yielded
        except BaseException as raised:  # handed back to the coroutine, as its task would
            exc = raised


def _held(slot: Any) -> Any:
    """A slot a Stop held, and the press that held it."""
    stop_hold.Pressed(next(stop_hold._presses)).hold(slot)
    stop_hold._last = slot._stop_hold_seq
    return slot


def _press(slot: Any) -> None:
    """A later Stop on *slot*: a newer press than any stamp read before it."""
    stop_hold._last = next(stop_hold._presses)
    stop_hold._hold(slot, stop_hold._last)


# ── the decision ────────────────────────────────────────────────────────────────


async def test_only_a_persons_stamp_from_after_the_press_releases() -> None:
    slot = _held(SimpleNamespace(key="h1"))
    before = stop_hold.stamp(slot._stop_hold_seq - 1, True)
    after = stop_hold.stamp(stop_hold.seen(), True)
    assert stop_hold.decide(slot) == stop_hold.HELD
    assert stop_hold.decide(slot, before) == stop_hold.HELD
    assert stop_hold.decide(slot, stop_hold.stamp(stop_hold.seen(), False)) == stop_hold.HELD
    assert stop_hold.decide(slot, f"another-boot:{stop_hold.seen()}") == stop_hold.HELD
    assert stop_hold.decide(slot, after) == stop_hold.RELEASE
    _press(slot)
    assert stop_hold.decide(slot, after) == stop_hold.HELD, "a newer Stop outranks it"
    slot._stop_held = False  # the object's mirror is not the authority: its key's record is
    assert stop_hold.decide(slot, "") == stop_hold.HELD
    stop_hold._drop(slot.key)
    assert stop_hold.decide(slot, "") == stop_hold.FREE


async def test_the_floor_and_a_newborns_press_come_from_the_key_s_record() -> None:
    """r8 Opus notes: a stamp is judged against the press the key is held at, not an
    object's higher mirror; a helper born of a session held by its channel's fact is
    held at that fact's press."""
    other = SimpleNamespace(key="h1", _stop_held=True, _stop_hold_seq=9)
    stop_hold._keys["h1"] = 5  # an undo lowered the record; this object kept press 9
    assert stop_hold.decide(other, stop_hold.stamp(6, True)) == stop_hold.RELEASE
    stop_hold._channels["C1"] = ("chat-9-1", 7)
    creator = SimpleNamespace(key="chat-9-1", _stop_held=True, _stop_hold_seq=7)
    newborn = SimpleNamespace(key="chat-born-1")
    stop_hold.inherit(None, creator, newborn)
    assert newborn._stop_hold_seq == 7 and stop_hold._keys["chat-born-1"] == 7


async def test_a_slot_restored_held_is_released_by_any_stamp_of_this_boot_only() -> None:
    from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

    slot = SimpleNamespace(key="h-restored", _stop_held=False, _stop_hold_seq=0)
    codec._read_stop_held(SimpleNamespace(meta={"stop_held": True}, slot=slot))
    assert (slot._stop_held, slot._stop_hold_seq) == (True, 0)
    assert stop_hold.decide(slot, stop_hold.stamp(0, True)) == stop_hold.RELEASE
    queued_before_restart = "0123456789ab:99"
    assert stop_hold.decide(slot, queued_before_restart) == stop_hold.HELD
    cleared = SimpleNamespace()
    codec._read_stop_held(SimpleNamespace(meta={"stop_held": "yes"}, slot=cleared))
    assert not hasattr(cleared, "_stop_held"), "only a literal true holds"


async def test_restoring_the_line_keeps_a_press_already_applied_and_a_refused_reopen_spends_none(
    tmp_path: Any,
) -> None:
    """GPT r1 3: registration applied pending press 11 before the line was read; a
    stamp that read press 10 must not release the restored helper. A reopen that is
    then refused leaves the press pending for the next one."""
    from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

    stop_hold._keys["chat-h1"] = 11
    first = _slot("chat-h1")
    stop_hold.on_register(first)  # the reopen registers the slot ...
    codec._read_stop_held(SimpleNamespace(meta={"stop_held": True}, slot=first))  # ... then reads
    assert first._stop_hold_seq == 11
    assert stop_hold.decide(first, stop_hold.stamp(10, True)) == stop_hold.HELD
    retry = _slot("chat-h1")  # the first reopen was refused and discarded
    stop_hold.on_register(retry)
    assert stop_hold.holds(retry) and retry._stop_hold_seq == 11
    with patch.object(stop_hold, "_save_soon"):
        assert stop_hold.gate(None, retry, stop_hold.stamp(11, True))
    assert stop_hold._keys == {}, "a person's release spends it"


async def test_every_save_form_writes_the_hold_and_clears_it(tmp_path: Any) -> None:
    """The full line omits a released hold (the key is slot-owned, so the omission is
    the clear); the merge, which cannot delete a key, writes false."""
    from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

    slot = _slot("h1")
    folds = codec.SaveFolds(memory_mode="persistent", created_at="2026-01-01T00:00:00")
    assert "stop_held" not in codec.encode(slot, folds=folds)
    assert codec.encode(slot, folds=folds, merge=True)["stop_held"] is False
    _held(slot)
    assert codec.encode(slot, folds=folds)["stop_held"] is True
    assert codec.encode(slot, folds=folds, merge=True)["stop_held"] is True
    stop_hold._drop(slot.key)  # released through another object of the key
    assert slot._stop_held is True, "this object's mirror is stale"
    assert "stop_held" not in codec.encode(slot, folds=folds), "the line writes the key's"
    assert codec.encode(slot, folds=folds, merge=True)["stop_held"] is False


async def test_an_undo_never_lets_an_earlier_hold_lapse(monkeypatch: pytest.MonkeyPatch) -> None:
    """r11 Opus: a line is encoded off the loop from the key's record; an undo that
    restores an earlier press writes it back before dropping its own, so no encode
    in between reads the key as released."""
    slot = _held(_slot("chat-undo-1"))  # an earlier Stop
    later = stop_hold.Pressed(next(stop_hold._presses))
    later.hold(slot)
    seen: list[bool] = []
    real_drop = stop_hold._drop

    def drop(key: str, press_: int = 0) -> None:
        real_drop(key, press_)
        seen.append(stop_hold.held_flag(slot))

    monkeypatch.setattr(stop_hold, "_drop", drop)
    with patch.object(stop_hold, "_save_soon"):
        later.undo(None, slot)
    assert seen == [True] and stop_hold.holds(slot)


# ── the Stop's press (``stop_helpers.stop_channel``) ─────────────────────────


async def test_the_press_holds_the_session_before_reading_its_helpers_in_memory_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    channel = _slot("chat-9-1")
    helper = _slot("chat-h1")
    state = SimpleNamespace(get_slot={"chat-h1": helper}.get)

    class _Store:
        def workers_of(self, key: str) -> list[str]:
            order.append(f"read held={stop_hold.holds(channel)}")
            return ["chat-h1", "chat-closed", key]

    monkeypatch.setattr("kiro_crew.slack.code_channels.store.store", lambda: _Store())
    held = stop_hold.press(state, "chat-9-1", channel)
    assert order == ["read held=True"], "held before the read, so a birth sees one or the other"
    assert held == [channel, helper] and not held.unread
    assert all(stop_hold.holds(s) for s in held)
    assert not any(m["role"] == "notice" for m in helper.messages), "no row: no file I/O"
    press = channel._stop_hold_seq
    assert stop_hold._keys == {"chat-9-1": press, "chat-h1": press, "chat-closed": press}
    reopened = _slot("chat-closed")
    stop_hold.on_register(reopened)
    assert stop_hold.holds(reopened) and reopened._stop_hold_seq == channel._stop_hold_seq


async def test_a_stop_with_no_binding_or_not_applied_says_nothing_is_held(
    tmp_path: Any,
) -> None:
    """Opus r1 notes: the reply says nothing starts again only for a hold set."""
    from kiro_crew.slack.code_channels import stop_helpers

    state = _state(tmp_path)
    unbound = stop_hold.press(state, "", None)
    assert unbound == [] and not unbound.unread
    assert stop_helpers.reply_text(False, stop_helpers.Tally(), held=unbound) == "Nothing running."
    already = _held(_slot("chat-9-0"))  # an earlier Stop's hold
    compacting = _slot("chat-9-1")
    with patch("kiro_crew.slack.code_channels.store.store") as store:
        store.return_value.workers_of.return_value = []
        held = stop_hold.press(state, "chat-9-1", compacting)
        again = stop_hold.press(state, "chat-9-0", already)
    with patch.object(stop_hold, "_save_soon") as save:
        held.undo(state, compacting)  # the session was compacting: Stop not applied
        again.undo(state, already)
    assert not stop_hold.holds(compacting) and held == []
    assert stop_hold.holds(already), "a hold from before this press stays"
    assert save.call_count == 2


async def test_a_key_held_with_no_live_slot_counts_for_the_reply(tmp_path: Any) -> None:
    """GPT prompt r23: a press that holds only keys (no live slot) still says nothing
    starts again; a key released or pressed again since does not count, and a press that
    holds nothing omits the sentence."""
    from kiro_crew.slack.code_channels import stop_helpers

    state = _state(tmp_path)
    tail = " Nothing starts again until someone writes."
    with patch("kiro_crew.slack.code_channels.store.store") as store:
        store.return_value.workers_of.return_value = ["chat-h9"]  # no live slot either
        held = stop_hold.press(state, "chat-9-1", None)
    assert held == [] and held.keyed == {"chat-9-1", "chat-h9"} and held.holds_any()
    text = stop_helpers.reply_text(False, stop_helpers.Tally(), held=held)
    assert text == "Nothing running." + tail
    stop_hold._drop("chat-9-1")  # released since
    assert held.holds_any(), "the helper's key is still this press"
    with patch("kiro_crew.slack.code_channels.store.store") as store:
        store.return_value.workers_of.return_value = []
        stop_hold.press(state, "chat-h9", None)  # pressed again since
    assert not held.holds_any()
    assert stop_helpers.reply_text(False, stop_helpers.Tally(), held=held) == "Nothing running."
    nothing = stop_hold.press(state, "", None)
    assert not nothing.holds_any()


async def test_a_closed_channel_session_stays_held_when_its_compacting_helper_is_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The channel's session has no live slot (its tab is closed) and its one running
    helper is compacting: the helper ends not held, the channel's key stays held, and the
    reply still ends with the hold sentence (GPT prompt r23)."""
    from slack_code_channels_helpers import make_env
    from test_slack_code_channel_stop_helpers import CHANNEL_SESSION, _bang_stop, _world

    for env in make_env(monkeypatch, tmp_path):
        w = _world(env, monkeypatch, **{"chat-h1": True})
        env.state.linked.clear()  # type: ignore[union-attr]
        live = {"chat-h1": w["slots"]["chat-h1"]}
        env.state.get_slot = live.get  # type: ignore[union-attr]
        w["answers"]["chat-h1"] = {"ok": True, "info": "compacting"}
        with patch.object(stop_hold, "_save_soon"):
            await _bang_stop(env)
        assert not stop_hold.holds(w["slots"]["chat-h1"])
        assert stop_hold._keys.get(CHANNEL_SESSION) == stop_hold._last
        assert env.slack.posts[-1][1].endswith(" Nothing starts again until someone writes.")


async def test_unreadable_helper_records_are_said_in_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.slack.code_channels import stop_helpers
    from kiro_crew.slack.code_channels.store import StoreUnavailable

    class _Store:
        def workers_of(self, key: str) -> list[str]:
            raise StoreUnavailable("unreadable")

    monkeypatch.setattr("kiro_crew.slack.code_channels.store.store", lambda: _Store())
    held = stop_hold.press(SimpleNamespace(), "chat-9-1", _slot("chat-9-1"))
    assert held.unread
    text = stop_helpers.reply_text(False, stop_helpers.Tally(), held=held)
    assert text == (
        "Nothing running. Its helper agents' records could not be read, so they are not "
        "held. Nothing starts again until someone writes."
    )


async def test_the_press_and_the_running_read_both_come_before_the_first_await() -> None:
    """The brake's ``pressed`` (the channel's session running at the press) and the hold's
    press are both read in ``stop_channel``'s first synchronous span: neither waits
    behind the other's await (the write before the brake is the first)."""
    from kiro_crew.slack.code_channels import stop_helpers

    fn = ast.parse(textwrap.dedent(inspect.getsource(stop_helpers.stop_channel))).body[0]
    first = min(n.lineno for n in ast.walk(fn) if isinstance(n, ast.Await))
    reads = [
        n.lineno
        for n in ast.walk(fn)
        if (isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "pressed")
        or (isinstance(n, ast.Call) and ast.unparse(n.func) == "stop_hold.press")
    ]
    assert len(reads) == 2 and max(reads) < first, (reads, first)


async def test_the_stop_holds_everything_before_its_first_await(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Row ``hold (Stop)``: from the Stop's first suspension on, every decision sees the hold."""
    from slack_code_channels_helpers import OWNER, make_env

    from kiro_crew.slack.code_channels import routing

    for env in make_env(monkeypatch, tmp_path):
        from test_slack_code_channel_stop_helpers import _world

        w = _world(env, monkeypatch, **{"chat-h1": True})
        seen_held: list[bool] = []

        def _look() -> None:
            seen_held.append(all(stop_hold.holds(s) for s in w["slots"].values()))

        event = {
            "type": "message",
            "user": OWNER,
            "channel": "C0CODE",
            "ts": "1.0",
            "text": "!stop",
        }
        coro = routing.inbound_gate(
            env.orch, event, text="!stop", sender_id=OWNER, authorized=True, from_trusted_bot=False
        )
        _, total = await _stepped(coro, -1, lambda: None)
        assert total > 0
        for at in range(total):
            for s in w["slots"].values():
                s._stop_held = False
            coro = routing.inbound_gate(
                env.orch,
                {**event, "ts": f"2.{at}"},
                text="!stop",
                sender_id=OWNER,
                authorized=True,
                from_trusted_bot=False,
            )
            await _stepped(coro, at, _look)
        assert seen_held == [True] * total


# ── the dispatch gate ───────────────────────────────────────────────────────────


async def test_the_hold_is_written_before_the_brake_and_undone_where_not_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Ruling 5: held in memory, persisted BEFORE the brake (a crash mid-brake keeps
    it); a channel session that was compacting was not stopped, so it is not held."""
    from slack_code_channels_helpers import make_env
    from test_slack_code_channel_stop_helpers import CHANNEL_SESSION, _bang_stop, _world

    from kiro_crew.slack.code_channels import stop_helpers

    order: list[str] = []
    real_persist, real_brake = stop_hold.persist, stop_helpers.stop_helpers

    async def persist(state: Any, slots: list[Any]) -> None:
        order.append(f"persist {sorted(s.key for s in slots if stop_hold.holds(s))}")

    async def brake(*a: Any, **k: Any) -> Any:
        order.append("brake")
        return await real_brake(*a, **k)

    monkeypatch.setattr(stop_hold, "persist", persist)
    monkeypatch.setattr(stop_helpers, "stop_helpers", brake)
    assert real_persist is not persist
    for env in make_env(monkeypatch, tmp_path):
        w = _world(env, monkeypatch, **{"chat-h1": True})
        w["slots"][CHANNEL_SESSION].running = True
        w["answers"][CHANNEL_SESSION] = {"ok": True, "info": "compacting"}
        with patch.object(stop_hold, "_save_soon") as save:
            await _bang_stop(env)
        assert order[:2] == [f"persist {sorted([CHANNEL_SESSION, 'chat-h1'])}", "brake"]
        assert not stop_hold.holds(w["slots"][CHANNEL_SESSION]), "Stop was not applied to it"
        assert stop_hold.holds(w["slots"]["chat-h1"])
        assert save.call_args.args[1] is w["slots"][CHANNEL_SESSION], "the undo is written"


async def test_a_helper_whose_compaction_declined_the_stop_is_not_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The channel session's rule for a helper too (GPT r22): a helper that was compacting
    was not stopped, so it ends not held; a helper that stopped stays held; one an earlier
    Stop held keeps that hold."""
    from slack_code_channels_helpers import make_env
    from test_slack_code_channel_stop_helpers import _bang_stop, _world

    for env in make_env(monkeypatch, tmp_path):
        w = _world(env, monkeypatch, **{"chat-h1": True, "chat-h2": True, "chat-h3": True})
        earlier = _held(w["slots"]["chat-h3"])._stop_hold_seq
        w["answers"]["chat-h1"] = {"ok": True, "info": "compacting"}
        w["answers"]["chat-h3"] = {"ok": True, "info": "compacting"}
        with patch.object(stop_hold, "_save_soon"):
            await _bang_stop(env)
        h1, h2, h3 = (w["slots"][k] for k in ("chat-h1", "chat-h2", "chat-h3"))
        assert not stop_hold.holds(h1) and stop_hold.decide(h1) == stop_hold.FREE
        assert "chat-h1" not in stop_hold._keys
        assert stop_hold.holds(h2) and stop_hold._keys["chat-h2"] == stop_hold._last
        assert stop_hold.holds(h3) and stop_hold._keys["chat-h3"] == earlier < stop_hold._last


# ── the dispatch gate (continued) ───────────────────────────────────────────────


async def test_the_gate_releases_for_a_persons_stamp(tmp_path: Any) -> None:
    state = _state(tmp_path)
    slot = _held(_slot("h1"))
    with patch.object(stop_hold, "_save_soon") as save:
        assert stop_hold.gate(state, slot, "") is False
        assert stop_hold.holds(slot) and not save.called
        person = stop_hold.stamp(stop_hold.seen(), True)
        assert stop_hold.gate(state, slot, person) is True
    assert not stop_hold.holds(slot) and save.call_args.args == (state, slot)
    assert stop_hold.gate(state, slot, "") is True, "free: anyone's turn passes"


async def test_a_release_save_writes_the_live_value_whichever_lands_last(tmp_path: Any) -> None:
    """GPT r6 "superseded write": no write carries a captured value."""
    state = _state(tmp_path)
    slot = _held(_slot("h1"))
    state._slots[slot.key] = slot  # the live entry: only it is written
    written: list[bool] = []

    async def save(_state: Any, s: Any, **_kw: Any) -> bool:
        await asyncio.sleep(0)
        written.append(s._stop_held)
        return True

    with patch("kiro_crew.dashboard.chat_persistence.save_slot_off_loop", save):
        assert stop_hold.gate(state, slot, stop_hold.stamp(stop_hold.seen(), True))
        _press(slot)  # a newer Stop before the release's save ran
        await stop_hold.persist(state, [slot])
        for _ in range(3):
            await asyncio.sleep(0)
    assert written == [True, True]


# ── the queue (amendment 1) and the queue-or-run decision ──────────────────────


async def test_a_persons_entry_behind_a_held_one_releases_and_the_queue_drains_in_order(
    tmp_path: Any,
) -> None:
    """With the slot busy at send time: the person's message waited in the queue."""
    state = _state(tmp_path)
    slot = _held(_slot("h1"))
    slot.queue_append("a wake", meta={})
    slot.queue_append("go on", meta=stop_hold.meta(stop_hold.stamp(stop_hold.seen(), True)))
    assert not stop_hold.holds_queue(slot)
    runs: list[dict[str, Any]] = []

    def run_chat(*_a: Any, **kw: Any) -> Any:
        runs.append(kw)
        return MagicMock()

    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", MagicMock(side_effect=run_chat)),
    ):
        assert await chat_runner._start_next_queued_turn(state, slot) is True
    assert [c["content"] for c in slot._queue] == ["go on"]
    stamp = runs[0]["_stop_hold_stamp"]
    assert stop_hold.gate(state, slot, stamp) is True and not stop_hold.holds(slot)


async def test_a_merged_drain_takes_the_newest_persons_stamp() -> None:
    """GPT r2 3: two person's messages merged into one turn carry the newest stamp,
    never the first; one from before a later Stop of this slot does not release."""
    slot = _held(_slot("h1"))
    older = stop_hold.stamp(stop_hold.seen(), True)
    _press(_slot("elsewhere"))  # another channel's Stop: both still release this slot
    newer = stop_hold.stamp(stop_hold.seen(), True)
    entries = [{"meta": stop_hold.meta(older)}, {"meta": stop_hold.meta(newer)}]
    assert stop_hold.queue_stamp(slot, entries) == newer
    assert stop_hold.drain_kwargs(slot, entries) == {"_stop_hold_stamp": newer}
    _press(slot)
    assert stop_hold.queue_stamp(slot, entries) == "", "both predate this slot's newer Stop"


async def test_a_queue_restored_from_history_carries_no_stamp() -> None:
    """GPT r2 2: a stamp is valid only in the live queue it was minted into."""
    from kiro_crew.dashboard.slot_queue_repository import sanitize_restored_queue

    person = stop_hold.stamp(stop_hold.seen(), True)
    restored = sanitize_restored_queue(
        [{"id": "q1", "content": "go on", "meta": {**stop_hold.meta(person), "sendId": "s1"}}]
    )
    assert restored[0]["meta"] == {"sendId": "s1"}
    slot = _held(_slot("h1"))
    slot._queue = restored
    assert stop_hold.queue_stamp(slot) == "" and stop_hold.holds_queue(slot)


async def test_the_drain_holds_a_queue_with_no_persons_entry_wherever_the_stop_lands(
    tmp_path: Any,
) -> None:
    """Row ``queue drain``: a Stop at any of the drain's awaits leaves the queue as it is."""
    state = _state(tmp_path)

    def fresh() -> Any:
        slot = _slot("h1")
        slot.queue_append("a wake", meta={})
        return slot

    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()) as run_chat,
    ):
        slot = fresh()
        _, total = await _stepped(
            chat_runner._start_next_queued_turn(state, slot), -1, lambda: None
        )
        assert run_chat.called, "unheld, it drains"
        for at in range(total):
            run_chat.reset_mock()
            slot = fresh()
            result, _ = await _stepped(
                chat_runner._start_next_queued_turn(state, slot), at, lambda: _held(slot)
            )
            started = run_chat.called
            stamp = run_chat.call_args.kwargs.get("_stop_hold_stamp", "") if started else ""
            # Held before the decision: nothing starts. Held after it: the turn it
            # started ends at the dispatch gate.
            assert not started or stop_hold.gate(state, slot, stamp) is False, at


async def test_enqueue_or_run_queues_a_held_session(tmp_path: Any) -> None:
    """Row ``enqueue_or_run_prompt``: decision and queue-or-start in one synchronous
    span; no peer, wake or app releases a held session (ruling 09:50Z, no relay)."""
    state = _state(tmp_path)
    slot = _held(_slot("h1"))
    run = MagicMock(return_value=asyncio.sleep(0))
    assert slot.enqueue_or_run_prompt("a peer's text", run, state) is False
    assert [e["content"] for e in slot._queue] == ["a peer's text"]
    assert stop_hold.STAMP_META not in (slot._queue[0].get("meta") or {})


async def test_a_helper_born_of_a_held_session_is_held(tmp_path: Any) -> None:
    """Row ``session_create``: read on the loop after the worker record (Opus r6 7)."""
    state = _state(tmp_path)
    creator = _held(_slot("chat-9-1"))
    newborn = _slot("chat-h9")
    with patch.object(stop_hold, "_save_soon") as save:
        stop_hold.inherit(state, creator, newborn)
        stop_hold.inherit(state, _slot("free"), _slot("chat-h10"))
    assert stop_hold.holds(newborn) and newborn._stop_hold_seq == creator._stop_hold_seq
    assert save.call_count == 1


async def test_a_slot_published_after_the_press_is_held_at_registration(tmp_path: Any) -> None:
    """Row ``slot registration``: a closed or still-building helper (Opus r6 5)."""
    from kiro_crew.dashboard.slot_registry import SlotRegistry

    stop_hold._keys["chat-h1"] = 7
    owner = SimpleNamespace(_slots={})
    slot = _slot("chat-h1")
    with patch.object(SlotRegistry, "open_same_key_takeover_note"):
        SlotRegistry.put_slot(owner, "chat-h1", slot)
    assert stop_hold.holds(slot) and slot._stop_hold_seq == 7


# ── steers, wakes and resume ────────────────────────────────────────────────────


async def test_a_non_persons_steer_into_a_held_turn_goes_to_the_queue_path(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row ``session_deliver steer`` / AutoNudge steer: asked after the last await
    before the steer registers (GPT r6 2)."""
    from steer_requeue_helpers import _running_slot

    from kiro_crew.dashboard import chat_delivery

    state = _state(tmp_path)
    _yielding(monkeypatch, "kiro_crew.dashboard.steer_retract.wait_for_clear")

    async def steer(_text: str, **_k: Any) -> bool:
        await asyncio.sleep(0)
        return True

    client = SimpleNamespace(supports_steer=True, steer_needs_loss_recovery=False, steer=steer)

    async def run(slot: Any, at: int) -> tuple[str, int]:
        slot._acp_client = client
        with patch.object(chat_delivery, "_engine_has_no_prompt", return_value=False):
            return await _stepped(
                chat_delivery.steer_into_running_turn(state, slot, "peer", user_origin=False),
                at,
                lambda: _held(slot),
            )

    outcome, total = await run(_running_slot(state, key="h-free"), -1)
    assert outcome == chat_delivery.STEER_STEERED and total > 0
    refused = []
    for at in range(total):
        slot = _running_slot(state, key=f"h{at}")
        outcome, _ = await run(slot, at)
        if outcome == chat_delivery.STEER_UNAVAILABLE:
            refused.append(at)
            assert not slot._pending_steers
        else:
            # Only a Stop after the registration lets it in: the turn it joined is
            # the one being stopped.
            assert outcome == chat_delivery.STEER_STEERED and at > 0
    assert refused and refused[0] == 0, "a Stop at the last await before it refuses the steer"


async def test_restart_resume_refuses_a_held_slot(tmp_path: Any) -> None:
    """Row ``restart resume``: under the slot lock, before the queue insert."""
    from kiro_crew.slack.code_channels import resume

    slot = _held(_slot("chat-9-1"))
    state = SimpleNamespace(get_slot={"chat-9-1": slot}.get)
    with patch.object(resume, "_admitted_now", return_value=""):
        check = resume._recheck_for(state, "C1", slot, slot, "seen")
        assert check() == "stopped in the channel"


# ── a person's own requests: the stamp is read before the first await ─────────
#
# Opus/GPT r6 3: a request decided before the Stop must not release it, wherever
# the Stop lands among the request's awaits. Each request below runs through the
# real handler on a real dashboard state, its ``_run_chat`` replaced by a recorder.


def _yielding(monkeypatch: pytest.MonkeyPatch, *targets: str) -> None:
    """Make each awaited helper in *targets* really suspend, so a Stop can land there:
    with the test doubles behind them they would otherwise return without yielding."""
    import importlib

    for target in targets:
        module, _, name = target.rpartition(".")
        real = getattr(importlib.import_module(module), name)

        async def suspending(*a: Any, _real: Any = real, **k: Any) -> Any:
            await asyncio.sleep(0)
            return await _real(*a, **k)

        monkeypatch.setattr(target, suspending)


def _stepped_client(state: Any, routes: dict[str, Any], plan: dict[str, Any]) -> Any:
    """The REAL test chain (the arrival read, auth, the slot-ownership checkpoint with
    its transcript read) and handler, stepped from outside it: the Stop lands at each
    await from the request's arrival on (13:10Z ruling 4)."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app

    @web.middleware
    async def stepped(request: Any, handler: Any) -> Any:
        plan["entered"], plan["handler_started"] = plan.get("entered", 0) + 1, False
        if plan.get("internal"):
            # The loopback internal caller (an MCP tool, a script): token auth
            # marks the grant and never a dashboard user (GPT r1 1).
            request["internal_auth"], request["is_dashboard_user"] = True, None
        result, plan["count"] = await _stepped(handler(request), plan["at"], plan["stop"])
        return result

    def entered(handler: Any) -> Any:
        async def run(request: Any) -> Any:
            plan["handler_started"] = True
            return await handler(request)

        return run

    real = list(_make_app(state).middlewares)
    assert real[0] is stop_hold.stop_hold_arrival_middleware
    app = web.Application(middlewares=[stepped, *real])
    app["state"] = state
    for path, handler in routes.items():
        app.router.add_post(path, entered(handler))
    return TestClient(TestServer(app))


#: The awaits of the chain before a handler, made to really suspend (with the test
#: doubles behind them they would not): the GPT r8 trace's transcript read among them.
_CHAIN_AWAITS = (
    "kiro_crew.dashboard.slot_ownership.slot_route_denial",
    "kiro_crew.dashboard.slot_ownership.load_routed_slot_window",
)


def _stamps(slot: Any, runs: list[dict[str, Any]]) -> list[str]:
    """Every stamp the request left: on its dispatches and on its queued entries."""
    queued = [str((e.get("meta") or {}).get(stop_hold.STAMP_META) or "") for e in slot._queue]
    return [str(r.get("_stop_hold_stamp") or "") for r in runs] + queued


async def _never_releases_once_pressed(
    state: Any,
    make_slot: Any,
    path: str,
    handler: Any,
    run_target: str,
    body: Any = lambda _slot: {},
    query: str = "",
    queues_when_held: bool = False,
    phase: Any = lambda: "",
) -> None:
    """The request releases the hold when nothing presses; pressed at any one of its
    awaits, nothing it leaves (a dispatch, a queued entry) releases it. With
    *queues_when_held* no turn is dispatched that the gate would end: a send the
    Stop holds is queued or refused, never answered and dropped (Opus r1 3)."""
    from chat_test_helpers import drain_background_tasks

    runs: list[dict[str, Any]] = []
    held_dispatches: list[int] = []

    def run_chat(*a: Any, **kw: Any) -> Any:
        runs.append(kw)  # read when the dispatch is made, not when its task runs
        if stop_hold.queues(a[1], kw.get("_stop_hold_stamp", "")):
            held_dispatches.append(plan["at"])
        return asyncio.sleep(0)

    plan: dict[str, Any] = {"at": -1, "stop": lambda: None, "count": 0}
    with patch(run_target, run_chat):
        async with _stepped_client(state, {path: handler}, plan) as client:
            slot = make_slot()
            resp = await client.post(path.replace("{slot}", slot.key) + query, json=body(slot))
            await resp.read()
            assert resp.status == 200, await resp.text()
            await drain_background_tasks(state)
            stamps = [s for s in _stamps(slot, runs) if s]
            assert stamps and stop_hold.decide(slot, stamps[0]) == stop_hold.RELEASE
            total = plan["count"]
            assert total > 0, plan
            for at in range(total):
                runs.clear()
                slot = make_slot()

                def stop(slot: Any = slot, at: int = at) -> None:
                    if not plan.get("handler_started"):
                        plan.setdefault("before_handler", []).append(at)
                    _press(slot)
                    plan.update(at_phase=phase())

                plan.update(at=at, stop=stop)
                resp = await client.post(path.replace("{slot}", slot.key) + query, json=body(slot))
                await resp.read()
                await drain_background_tasks(state)
                released = [
                    s for s in _stamps(slot, runs) if stop_hold.decide(slot, s) == stop_hold.RELEASE
                ]
                assert released == [], f"pressed at await {at} of {total}: {resp.status}"
                if queues_when_held:
                    # Refused only when the Stop landed during the turn's admission,
                    # once nothing can be queued any more; queued before it.
                    assert resp.status == 200 or (
                        resp.status == 409 and plan.get("at_phase") == "admission"
                    ), (at, resp.status, plan.get("at_phase"))
            assert not queues_when_held or held_dispatches == [], held_dispatches
            # The chain's own awaits before the handler were stepped too (ruling 4).
            assert len(plan.get("before_handler", [])) >= 2, plan.get("before_handler")
            runs.clear()
            slot = make_slot()
            plan.update(at=-1, stop=lambda: None, internal=True)
            resp = await client.post(path.replace("{slot}", slot.key) + query, json=body(slot))
            await resp.read()
            await drain_background_tasks(state)
            plan["internal"] = False
            assert [s for s in _stamps(slot, runs) if s] == [], "an internal caller is no person"


async def test_a_persons_dashboard_send_decided_before_the_stop_never_releases(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row ``chat_send``."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_handlers import api_chat

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    count = iter(range(1000))
    _yielding(
        monkeypatch,
        *_CHAIN_AWAITS,
        "kiro_crew.dashboard.chat_handlers.read_bounded_json",
        "kiro_crew.dashboard.chat_handlers.cron_slot_creator",
        "kiro_crew.dashboard.chat_handlers.deny_app_session_control",
        "kiro_crew.dashboard.chat_handlers._settle_send_race",
        # The replay read: a Stop landing there is asked again after it (Opus r1 3).
        "kiro_crew.shared_crew.admission_entry.chat_replay",
    )
    from kiro_crew.shared_crew import admission_entry

    marks = {"phase": ""}
    real_admit = admission_entry.admit_chat_turn

    async def admitting(*a: Any, **k: Any) -> Any:
        marks["phase"] = "admission"  # and asked again after it
        await asyncio.sleep(0)
        return await real_admit(*a, **k)

    monkeypatch.setattr(admission_entry, "admit_chat_turn", admitting)

    def make_slot() -> Any:
        marks["phase"] = ""
        slot = state.get_or_create_slot(f"s{next(count)}")
        slot.append("user", "earlier")
        slot.append("assistant", "earlier reply")
        return _held(slot)

    await _never_releases_once_pressed(
        state,
        make_slot,
        "/api/chat",
        api_chat,
        "kiro_crew.dashboard.chat_handlers._run_chat",
        body=lambda slot: {"message": "go on", "slot": slot.key},
        query="?ws=1",  # the socket receipt: the response ends when the send is decided
        queues_when_held=True,
        phase=lambda: marks["phase"],
    )


async def test_a_persons_regenerate_or_edit_decided_before_the_stop_never_releases(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows ``Regenerate`` and ``Edit & Resend`` (Opus r6 3: dated at turn start)."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_regenerate import (
        api_chat_slot_edit_resend,
        api_chat_slot_regenerate,
    )

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.sessions.discard_conversation = MagicMock(side_effect=_true)
    state.sessions.aflush = MagicMock(side_effect=_none)
    state.sessions._session_map.get = MagicMock(return_value="")
    count = iter(range(1000))
    _yielding(
        monkeypatch,
        *_CHAIN_AWAITS,
        "kiro_crew.dashboard.chat_regenerate.rerun_signs_in_via_kiro_cli",
    )

    def make_slot() -> Any:
        slot = state.get_or_create_slot(f"r{next(count)}")
        slot.append("user", "do it")
        slot.append("assistant", "done")
        slot.drain()
        return _held(slot)

    await _never_releases_once_pressed(
        state,
        make_slot,
        "/api/chat/slots/{slot}/regenerate",
        api_chat_slot_regenerate,
        "kiro_crew.dashboard.chat_regenerate._run_chat",
    )
    await _never_releases_once_pressed(
        state,
        make_slot,
        "/api/chat/slots/{slot}/edit-resend",
        api_chat_slot_edit_resend,
        "kiro_crew.dashboard.chat_regenerate._run_chat",
        body=lambda _slot: {"index": 0, "content": "do it again"},
    )


async def _true(*_a: Any, **_k: Any) -> bool:
    await asyncio.sleep(0)
    return True


async def _none(*_a: Any, **_k: Any) -> None:
    await asyncio.sleep(0)


async def test_a_persons_rewind_decided_before_the_stop_never_releases(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row ``Rewind`` (Opus r6 3: the turn starts after ``dispatch_ready``)."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_rewind import api_chat_slot_rewind

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.sessions.discard_conversation = MagicMock(side_effect=_true)
    state.sessions.aflush = MagicMock(side_effect=_none)
    state.sessions._session_map.get = MagicMock(return_value="")
    _yielding(
        monkeypatch, *_CHAIN_AWAITS, "kiro_crew.dashboard.chat_rewind.rerun_signs_in_via_kiro_cli"
    )
    count = iter(range(1000))

    def make_slot() -> Any:
        slot = state.get_or_create_slot(f"w{next(count)}")
        slot.append("user", "first")
        slot.append("assistant", "first reply")
        slot.drain()
        return _held(slot)

    await _never_releases_once_pressed(
        state,
        make_slot,
        "/api/chat/slots/{slot}/rewind",
        api_chat_slot_rewind,
        "kiro_crew.dashboard.chat_rewind._run_chat",
        body=lambda _slot: {"index": 0, "content": "first, again"},
    )


async def test_a_persons_continue_decided_before_the_stop_never_releases(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row ``Continue``: the queued body carries the stamp read at the press's entry."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat_api.continue_turn import api_chat_slot_continue

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    # The queued body is the outcome: the drain that would dispatch it is not run.
    monkeypatch.setattr(chat_handlers, "_start_next_queued_turn", _true)
    _yielding(monkeypatch, *_CHAIN_AWAITS)
    count = iter(range(1000))

    def make_slot() -> Any:
        slot = state.get_or_create_slot(f"c{next(count)}")
        slot.append("user", "do it")
        slot.append("assistant", "half done")
        slot.drain()
        return _held(slot)

    await _never_releases_once_pressed(
        state,
        make_slot,
        "/api/chat/slots/{slot}/continue",
        api_chat_slot_continue,
        "kiro_crew.dashboard.chat_handlers._run_chat",
    )


async def test_an_autonudge_wake_is_busy_wherever_the_stop_lands_before_its_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row ``AutoNudge``: asked again after its last await, so no cycle is spent on a
    held session (GPT r6 2) and nothing is spawned."""
    from test_autonudge_dashboard_fire import _fake_spawn, _loop, _orchestrator
    from test_autonudge_dashboard_fire import _slot as nudge_slot

    from kiro_crew.slack import gateway as gw

    # The wake's body is composed after the fire's first checks; it really suspends,
    # whatever an earlier test left cached, so the Stop can land there.
    _yielding(monkeypatch, "kiro_crew.slack.gateway.compose_nudge_body")

    def fire(at: int) -> tuple[Any, Any, Any]:
        orch = _orchestrator()
        slot = nudge_slot("chat-1-1785")
        slot.append = lambda *_a, **_k: None
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        return orch, slot, _fake_spawn()

    orch, slot, spawn = fire(-1)
    with (
        patch.object(gw, "spawn_guarded_turn", spawn),
        patch("kiro_crew.dashboard.chat._run_chat", new=MagicMock()),
    ):
        _, total = await _stepped(orch._fire_dashboard_nudge(_loop()), -1, lambda: None)
    assert spawn.calls and total > 0, "unheld, the wake spawns its turn"
    for at in range(total):
        orch, slot, spawn = fire(at)
        with (
            patch.object(gw, "spawn_guarded_turn", spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=MagicMock()),
        ):
            await _stepped(orch._fire_dashboard_nudge(_loop()), at, lambda slot=slot: _held(slot))
        # Held before the spawn: nothing spawned. A turn spawned before the press
        # ends at the dispatch gate (no stamp).
        assert spawn.calls == [] or stop_hold.gate(None, slot, "") is False, at


async def test_the_linked_route_carries_the_entrys_stamp_unchanged(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row ``Slack linked path``: ``route_linked_thread`` reads no press and builds no
    stamp (GPT r1 2, Opus r1 1); it hands the entry's own on, through its reroutes,
    once ``is_allowed_user`` admitted the sender."""
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot
    from kiro_crew.history import ConversationLog
    from kiro_crew.slack import handler

    log = ConversationLog(base_dir=tmp_path / "sessions")
    _yielding(
        monkeypatch,
        "kiro_crew.slack.handler.closed_dashboard_owner_refused",
        "kiro_crew.slack.handler.linked_slot_channel_mismatch",
        "kiro_crew.slack.handler.store_linked_thread_images",
    )

    async def route(slot: Any, at: int, stamp: str, allowed: bool = True) -> tuple[list[str], int]:
        """Route one message into *slot*, pressing at its *at*-th await; its stamps."""
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=slot)
        ds._slots = {slot.key: slot}
        ds._background_tasks = set()
        ds.conversation_log = log
        slack = MagicMock()
        slack.post_message = MagicMock(side_effect=_none)
        runs: list[dict[str, Any]] = []

        async def run_chat(*_a: Any, **kw: Any) -> None:
            runs.append(kw)

        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=allowed),
            patch("kiro_crew.dashboard.chat._run_chat", new=run_chat),
        ):
            coro = handler.route_linked_thread(
                "go on", "slack:t1", "U1", "C1", slack, "t1", stop_hold_stamp=stamp
            )
            _, total = await _stepped(coro, at, lambda: _press(slot))
            for task in list(ds._background_tasks):
                await task
        return [s for s in _stamps(slot, runs) if s], total

    slot = _held(_ChatSlot("slot0"))
    entry = stop_hold.stamp(stop_hold.seen(), True)
    stamps, total = await route(slot, -1, entry)
    assert stamps == [entry] and stop_hold.decide(slot, entry) == stop_hold.RELEASE
    assert total > 0
    for at in range(total):
        slot = _held(_ChatSlot(f"slot{at + 1}"))
        entry = stop_hold.stamp(stop_hold.seen(), True)
        stamps, _ = await route(slot, at, entry)
        assert stamps == [entry] and stop_hold.decide(slot, entry) == stop_hold.HELD, at
    refused = _held(_ChatSlot("slot-refused"))
    stamps, _ = await route(refused, -1, stop_hold.stamp(stop_hold.seen(), True), allowed=False)
    assert stamps == [] and stop_hold.holds(refused), "a refused sender's message goes nowhere"
    plain = _held(_ChatSlot("slot-plain"))
    stamps, _ = await route(plain, -1, "")
    assert stamps == [] and stop_hold.holds(plain), "the other callers carry none"


def _cc_orch(env: Any) -> Any:
    from test_slack_code_channels_events import _orch

    return _orch(env)


async def test_a_slack_message_that_entered_before_the_stop_never_releases(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row ``Slack message``: the press is read at the envelope's entry and the stamp is
    built at ``_route_message`` once the sender is admitted; a Stop at any await from
    there to the session (the gate's lookups, the display name, the creation barrier,
    the bind lock) leaves a stamp that does not release (GPT r1 2, Opus r1 1)."""
    from slack_code_channels_helpers import OWNER, make_env

    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot
    from kiro_crew.slack.code_channels.store import Binding
    from kiro_crew.slack.events import SeenCache, _route_message

    for env in make_env(monkeypatch, tmp_path):
        env.store.put_binding(
            Binding(
                channel_id="C0CODE", team_id="T1", session_key="chat-9-1", anchor_ts="300.000001"
            )
        )
        env.info("C0CODE")
        _yielding(
            monkeypatch,
            "kiro_crew.slack.code_channels.routing._wait_for_creation",
            "kiro_crew.slack.code_channels.routing._route_to_slot",
        )

        async def run(at: int, n: int, trusted: bool = False) -> tuple[Any, int]:
            slot = _held(_ChatSlot(f"cc{n}"))
            orch = _cc_orch(env)
            orch.slack.get_user_info = MagicMock(side_effect=lambda _u: _info())
            event = {
                "type": "message",
                "user": OWNER,
                "channel": "C0CODE",
                "ts": f"301.{n:06d}",
                "text": "now add a regression test",
                "team": "T1",
            }
            seen0 = stop_hold.seen()  # what ``_on_event`` reads before its first await
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                coro = _route_message(
                    orch, event, SeenCache(), from_trusted_bot=trusted, stop_seen=seen0
                )
                _, total = await _stepped(coro, at, lambda: _press(slot))
            return slot, total

        slot, total = await run(-1, 0)
        assert env.stamps and stop_hold.decide(slot, env.stamps[-1]) == stop_hold.RELEASE
        assert total > 2, total
        for at in range(total):
            slot, _ = await run(at, at + 1)
            assert stop_hold.decide(slot, env.stamps[-1]) == stop_hold.HELD, at
        assert len(env.stamps) == total + 1, "every run reached the session"


async def _info() -> dict[str, str]:
    await asyncio.sleep(0)
    return {"real_name": "Owner"}


async def test_a_slack_command_that_entered_before_the_stop_never_releases(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row ``Slack command``: ``handle_slash`` builds the stamp from the envelope's press
    after ``is_allowed_user``; a Stop at any of its awaits leaves one that does not release."""
    from slack_code_channel_surface_helpers import _bind, _slash

    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot
    from kiro_crew.slack.code_channels import commands

    _bind(env)
    _yielding(monkeypatch, "kiro_crew.slack.code_channels.routing._route_to_slot")

    async def respond(_text: str) -> None:
        await asyncio.sleep(0)

    async def run(at: int, n: int) -> tuple[Any, int]:
        slot = _held(_ChatSlot(f"sl{n}"))
        coro = commands.handle_slash(
            env.orch, _slash("/summarize", trigger=f"t{n}"), respond, stop_seen=stop_hold.seen()
        )
        _, total = await _stepped(coro, at, lambda: _press(slot))
        return slot, total

    slot, total = await run(-1, 0)
    assert env.stamps and stop_hold.decide(slot, env.stamps[-1]) == stop_hold.RELEASE
    assert total > 2, total
    for at in range(total):
        slot, _ = await run(at, at + 1)
        assert stop_hold.decide(slot, env.stamps[-1]) == stop_hold.HELD, at
    assert len(env.stamps) == total + 1


async def test_a_peers_send_to_a_held_helper_waits(tmp_path: Any) -> None:
    """Row ``session_deliver peer``: queue-or-run is one synchronous span, so a Stop
    before the delivery queues it; the channel's own session is a peer too."""
    from kiro_crew.dashboard import session_deliver

    state = _state(tmp_path)
    helper = _held(_slot("chat-h1"))
    with patch("asyncio.create_task", return_value=MagicMock()) as spawn:
        outcome = await session_deliver.deliver(
            state, helper, "carry on", session_deliver.peer(steer=False, sender="chat-9-1")
        )
    assert outcome == session_deliver.DELIVERED_QUEUED and not spawn.called


async def test_restart_resume_neither_resumes_nor_offers_a_held_slot() -> None:
    """Row ``restart resume``: a Stop ended it, or it was still ending at the restart."""
    from kiro_crew.slack.code_channels import resume

    slot = _held(_slot("chat-9-1"))
    live = MagicMock(side_effect=_true)
    with (
        patch.object(resume, "_interrupted_turn", MagicMock(side_effect=_turn)),
        patch.object(resume, "_dispatch_token", MagicMock()) as token,
    ):
        params = list(inspect.signature(resume._resume_slot).parameters)
        args = {name: MagicMock() for name in params}
        args.update(slot=slot, live=live)
        await resume._resume_slot(**args)
    assert not live.called and not token.called


async def _turn(*_a: Any, **_k: Any) -> str:
    await asyncio.sleep(0)
    return "turn-1"


# ── provenance: the press is read only where a request arrives ────────────────

_MINTERS = {"seen", "stamp", "http_stamp", "_last"}
_HOME = "kiro_crew/slack/code_channels/stop_hold.py"
#: Where a person's stamp is built from the arrival read, once the gateway's own
#: authorization admitted a human: the only callers of ``stamp`` / ``http_stamp``.
_BUILDERS = frozenset(
    {
        ("kiro_crew/slack/events.py", "_route_message"),
        ("kiro_crew/slack/code_channels/commands.py", "run_command"),
        ("kiro_crew/dashboard/chat_api/chat_send.py", "_api_chat_send"),
        ("kiro_crew/dashboard/chat_api/continue_turn.py", "api_chat_slot_continue"),
        ("kiro_crew/dashboard/chat_regenerate.py", "api_chat_slot_regenerate"),
        ("kiro_crew/dashboard/chat_regenerate.py", "api_chat_slot_edit_resend"),
        ("kiro_crew/dashboard/chat_rewind.py", "api_chat_slot_rewind"),
    }
)


def _mints(tree: ast.Module, path: str) -> list[tuple[str, tuple[int, int], list[ast.AST]]]:
    """``(name, (line, col), enclosing functions outermost first)`` per call of a minter
    in *tree*, and per read of the press itself (``stop_hold._last``) outside its home."""
    found: list[tuple[str, tuple[int, int], list[ast.AST]]] = []
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("stop_hold")
        for alias in node.names
        if alias.name in _MINTERS
    } | ({"seen"} if path == _HOME else set())

    def visit(node: ast.AST, chain: list[ast.AST]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chain = [*chain, node]
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _MINTERS
            and ast.unparse(node.value).lstrip("_") == "stop_hold"
        ):
            found.append((node.attr, (node.lineno, node.col_offset), chain))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in imported:
                found.append((node.func.id, (node.lineno, node.col_offset), chain))
        for child in ast.iter_child_nodes(node):
            visit(child, chain)

    visit(tree, [])
    return found


def _scan_mints(root: Any) -> list[str]:
    bad = []
    for file in sorted(root.rglob("*.py")):
        rel = file.relative_to(root.parent).as_posix()
        text = file.read_text(encoding="utf-8")
        if "stop_hold" not in text and rel != _HOME:
            continue  # a call needs the module's name somewhere in the file
        for minter, at, chain in _mints(ast.parse(text), rel):
            names = {(rel, getattr(fn, "name", "")) for fn in chain}
            allowed = stop_hold.ENTRIES if minter == "seen" else _BUILDERS
            if minter == "_last" or not names & allowed:
                bad.append(f"{rel}:{at[0]} {minter}()")
                continue
            if minter == "seen":
                # Read before the arrival point's first await, so nothing it awaited
                # lets a Stop in first.
                entry = next(fn for fn in chain if (rel, getattr(fn, "name", "")) in allowed)
                awaits = [
                    (n.lineno, n.col_offset) for n in ast.walk(entry) if isinstance(n, ast.Await)
                ]
                if awaits and min(awaits) < at:
                    bad.append(f"{rel}:{at[0]} {minter}() after the await at {min(awaits)[0]}")
    return bad


async def test_the_press_is_read_only_where_a_request_arrives() -> None:
    """Ruling 3 (13:10Z): an AST scan of the product. ``seen()`` is called only at the
    arrival points (``stop_hold.ENTRIES``), before their first await; ``stamp()`` and
    ``http_stamp()`` only where the gateway's authorization admitted a human; nothing
    else reads the press."""
    from pathlib import Path

    import kiro_crew

    root = Path(kiro_crew.__file__).resolve().parent
    assert _scan_mints(root) == []
    used = {
        (file.relative_to(root.parent).as_posix(), getattr(fn, "name", ""))
        for file in root.rglob("*.py")
        if "stop_hold" in (text := file.read_text(encoding="utf-8"))
        for _m, _l, chain in _mints(ast.parse(text), file.relative_to(root.parent).as_posix())
        for fn in chain
    }
    assert stop_hold.ENTRIES | _BUILDERS <= used, "every listed site still reads (no stale list)"


async def test_the_arrival_middleware_is_first_in_both_apps() -> None:
    """Ruling 3: first in each server's own list, so no middleware of ours awaits before
    the read (the full chains are pinned in the composition contract)."""
    from kiro_crew.dashboard.server_runtime import middleware_chain

    for install in (
        middleware_chain._install_dashboard_middlewares,
        middleware_chain._install_api_middlewares,
    ):
        tree = ast.parse(textwrap.dedent(inspect.getsource(install)))
        lists = [
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "app.middlewares[:]"
        ]
        assert len(lists) == 1 and isinstance(lists[0], ast.List), install
        assert ast.unparse(lists[0].elts[0]) == "stop_hold_arrival_middleware", install
    inserts = [
        n
        for n in ast.walk(ast.parse(inspect.getsource(middleware_chain)))
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "app.middlewares.insert"
    ]
    assert [ast.unparse(n.args[0]) for n in inserts] == ["index + 1"], "nothing goes before it"


async def test_the_provenance_scan_catches_a_planted_late_read(tmp_path: Any) -> None:
    pkg = tmp_path / "kiro_crew" / "slack"
    pkg.mkdir(parents=True)
    (pkg / "planted.py").write_text(
        "from kiro_crew.slack.code_channels import stop_hold\n"
        "from kiro_crew.slack.code_channels.stop_hold import stamp\n"
        "async def deliver(x):\n"
        "    await x\n"
        "    return stop_hold.stamp(stop_hold.seen(), True), stamp(stop_hold._last, True)\n"
    )
    (pkg / "events.py").write_text(
        "from kiro_crew.slack.code_channels import stop_hold\n"
        "async def _on_event(c, r):\n"
        "    await c\n"
        "    s = stop_hold.seen()\n"
    )
    bad = _scan_mints(tmp_path / "kiro_crew")
    assert len([b for b in bad if "planted.py" in b]) == 4
    assert any("events.py:4 seen() after the await" in b for b in bad)


async def test_a_live_empty_helpers_hold_is_written(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT r2 4: a helper whose transcript is still empty (its birth record only) gets
    its hold written: only a forced save writes an empty window's line."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
    from kiro_crew.dashboard.chat_utils_slots import slot_history_key

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("h-empty")
    key = slot_history_key(slot)
    await asyncio.to_thread(state.conversation_log.update_metadata, key, {"title": "helper"})
    assert state.conversation_log.get_metadata(key) and not slot.messages
    unforced = []

    async def plain_save(st: Any, s: Any, **kw: Any) -> bool:
        unforced.append(kw.get("force", False))
        return await save_slot_off_loop(st, s, **kw)

    with patch("kiro_crew.dashboard.chat_persistence.save_slot_off_loop", plain_save):
        _held(slot)
        await stop_hold.persist(state, [slot])
    assert unforced == [True]
    assert state.conversation_log.get_metadata(key).get("stop_held") is True


class _AfterTheGate(Exception):
    """Raised by the first step after the slash gate's hold check."""


async def test_a_local_command_is_held_before_the_slash_gate_runs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GPT r2 5: a local command (``/workflow <name>``) acts before the dispatch gate,
    so the hold is asked at the start of ``_turn_slash_gate``, before its first await.
    Held, the turn ends there; a person's own command releases the hold."""

    def after(*_a: Any, **_k: Any) -> Any:
        raise _AfterTheGate

    monkeypatch.setattr(chat_runner, "_channels_judging_turn", after)

    def turn(slot: Any, stamp: str) -> Any:
        return SimpleNamespace(
            _commands_off=False,
            _directive_channel_origin=False,
            _directive_self_wake=False,
            _directive_user_origin=bool(stamp),
            _prompt_depth=0,
            _responsive_human=False,
            _responsive_protected=False,
            _synthetic_payload=False,
            _turn_actor="",
            _agent_started=False,
            _stop_hold_stamp=stamp,
            message="/workflow nightly",
            session_key="dashboard:h1",
            slot=slot,
            state=None,
            turn_exit=None,
        )

    slot = _held(_slot("h1"))
    assert await chat_runner._turn_slash_gate(turn(slot, "")) is chat_runner._Flow.END
    assert stop_hold.holds(slot)
    with patch.object(stop_hold, "_save_soon"), pytest.raises(_AfterTheGate):
        await chat_runner._turn_slash_gate(turn(slot, stop_hold.stamp(stop_hold.seen(), True)))
    assert not stop_hold.holds(slot), "a person's own command releases"


async def test_a_workflow_started_across_a_stop_never_runs(tmp_path: Any) -> None:
    """r3 A (both lanes): ``/workflow`` asks the hold at the run's commit point, after
    every await of its start. A Stop at any of them leaves no run; unpressed it runs."""
    from test_workflows_service import FakeSessions, _wait_terminal

    from kiro_crew.workflows.library import WorkflowDefinitionLibrary
    from kiro_crew.workflows.service import WorkflowService

    library = WorkflowDefinitionLibrary(tmp_path)
    source = (
        'META = {"name": "echo", "description": "d"}\n'
        "async def workflow(ctx):\n"
        "    return {'input': ctx.args.get('input', '')}\n"
    )
    saved = library.create(source=source, name="Echo")
    svc = WorkflowService(sessions=FakeSessions([]), persist=False, definition_library=library)

    async def start(at: int, n: int) -> tuple[dict[str, Any], int, Any]:
        slot = _held(_slot(f"w{n}"))
        stamp = stop_hold.stamp(stop_hold.seen(), True)  # the person's own command

        def admit() -> bool:
            return not stop_hold.queues(slot, stamp)

        coro = svc.start_definition(saved["slug"], input_text="hi", admit=admit)
        started, total = await _stepped(coro, at, lambda: _press(slot))
        return started, total, slot

    started, total, _ = await start(-1, 0)
    assert "run_id" in started
    await _wait_terminal(svc, started["run_id"])
    assert total > 1, total
    for at in range(total):
        before = {r["run_id"] for r in svc.list_runs()}
        started, _, slot = await start(at, at + 1)
        assert "run_id" not in started, at
        assert {r["run_id"] for r in svc.list_runs()} == before, at
        assert stop_hold.holds(slot)


async def test_a_persons_steer_into_a_held_turn_takes_the_stamped_queue_path(
    tmp_path: Any,
) -> None:
    """r3 B: no steer joins a held, ending turn, a person's included; its send then
    queues with its stamp, which releases the hold at the next drain."""
    from steer_requeue_helpers import _running_slot

    from kiro_crew.dashboard import chat_delivery

    state = _state(tmp_path)

    async def steer(_text: str, **_k: Any) -> bool:
        return True  # the engine would take it: only the hold refuses

    free = _running_slot(state, key="h-free-steer")
    client = SimpleNamespace(supports_steer=True, steer_needs_loss_recovery=False, steer=steer)
    free._acp_client = client
    with patch.object(chat_delivery, "_engine_has_no_prompt", return_value=False):
        assert (
            await chat_delivery.steer_into_running_turn(state, free, "go", user_origin=True)
            == chat_delivery.STEER_STEERED
        )
    slot = _held(_running_slot(state, key="h-steer"))
    slot._acp_client = client
    with patch.object(chat_delivery, "_engine_has_no_prompt", return_value=False):
        outcome = await chat_delivery.steer_into_running_turn(
            state, slot, "change course", user_origin=True
        )
    assert outcome == chat_delivery.STEER_UNAVAILABLE
    person = stop_hold.stamp(stop_hold.seen(), True)
    chat_delivery.queue_for_next_turn(state, slot, "change course", stop_hold_stamp=person)
    assert stop_hold.queue_stamp(slot) == person and not stop_hold.holds_queue(slot)


async def test_a_stop_with_the_channels_session_closed_holds_it_when_it_reopens(
    tmp_path: Any,
) -> None:
    """r3 C (fails open without it): the channel's session had no live slot at the
    press, so it is held when it registers, as a helper is."""
    state = _state(tmp_path)
    with patch("kiro_crew.slack.code_channels.store.store") as store:
        store.return_value.workers_of.return_value = []
        held = stop_hold.press(state, "chat-9-1", None)
    assert held == []
    reopened = _slot("chat-9-1")
    stop_hold.on_register(reopened)
    assert stop_hold.holds(reopened) and reopened._stop_hold_seq == held.press


async def test_a_drained_row_carries_no_stamp(tmp_path: Any) -> None:
    """r3 D: the stamp is queue plumbing; no transcript row carries it (nor the boot id)."""
    state = _state(tmp_path)
    slot = _held(_slot("h-row"))
    person = stop_hold.stamp(stop_hold.seen(), True)
    slot.queue_append("go on", meta=stop_hold.meta(person))
    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
    ):
        assert await chat_runner._start_next_queued_turn(state, slot) is True
    rows = [m for m in slot.messages if m.get("role") == "user"]
    assert rows and all(stop_hold.STAMP_META not in (m.get("meta") or {}) for m in rows)
    assert all(stop_hold.BOOT not in str(m) for m in slot.messages)


async def test_a_task_plan_held_at_its_commit_point_starts_nothing(tmp_path: Any) -> None:
    """r3 A, the task-plan arm of ``/workflow``: ``execute_plan`` asks *admit* after its
    last await, right before the run's task starts; held, it starts none."""
    from taskrunner_helpers import _make_mock_sessions

    from kiro_crew.taskrunner import Step, TaskRunner
    from kiro_crew.workflows.service import WorkflowService

    sessions = _make_mock_sessions()
    runner = TaskRunner(
        sessions=sessions,
        auto_test=False,
        work_dir=tmp_path,
        workflow_service=WorkflowService(sessions=sessions, persist=False),
    )
    runner._decompose = AsyncMock(return_value=[Step(index=1, title="Do", description="it")])
    run = await runner.plan("do it")
    with pytest.raises(ValueError, match="holds this session"):
        await runner.execute_plan(run.task_id, admit=lambda: False)
    assert run.task_id not in runner._tasks


async def test_a_harness_local_command_runs_in_the_span_of_its_ask() -> None:
    """A harness-local command asks the hold with no await between the ask and the
    command (``_answer_slash_for_harness``, run as a task the press ends). Kept as a source
    read: the behaviour (``test_a_stop_before_a_harness_local_command_runs_nothing``)
    lands a Stop at the awaits the code has, and no turn can land one at an await added
    between the ask and the command. Unlike the dispatch gate's, nothing asks again
    after it (the prompt write's ask covers the gate's span)."""
    from kiro_crew.dashboard.chat_turn import slash_commands

    fn = next(
        n
        for n in ast.walk(ast.parse(inspect.getsource(slash_commands)))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_turn_slash_gate"
    )
    for block in ast.walk(fn):
        body = getattr(block, "body", None)
        if not isinstance(body, list):
            continue
        for i, stmt in enumerate(body):
            if "_answer_slash_for_harness(" in ast.unparse(stmt) and isinstance(stmt, ast.Assign):
                assert (
                    ast.unparse(body[i + 1])
                    == "await stop_hold.run_local(slot, _command_backend, first_word, command)"
                )
                before = [ast.unparse(b) for b in body[max(0, i - 2) : i]]
                assert any(b.startswith("if not _admits():") for b in before), stmt.lineno
                assert not any("await" in b for b in before), stmt.lineno
                return
    raise AssertionError("the harness-local command call was not found")


async def test_a_stop_ends_a_running_context_add_before_its_second_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 GPT 1: a harness-local command runs as a task the press cancels, so a Stop
    during the first RPC of a two-path ``/context add`` sends no second RPC."""
    from kiro_crew.dashboard import kas_local_commands as kas
    from kiro_crew.testing.wait import await_until

    slot = _slot("h-ctx")
    calls: list[dict[str, Any]] = []
    first_sent = asyncio.Event()

    async def kas_call(_provider: Any, verb: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params)
        first_sent.set()
        await asyncio.Event().wait()  # the engine's answer, which never comes: no Stop waits for it
        return {"success": True}

    async def provider(*_a: Any) -> Any:
        return SimpleNamespace(cwd="/safe")

    monkeypatch.setattr(kas, "_inspect_live_provider", provider)
    monkeypatch.setattr(kas, "_checked_path", lambda raw, _cwd: (raw, ""))
    monkeypatch.setattr(kas, "_kas_call", kas_call)
    run = asyncio.ensure_future(
        stop_hold.run_local(
            slot, "kas", "/context", kas._kas_context_text(None, "dashboard:h-ctx", "add /a /b")
        )
    )
    try:
        await await_until(first_sent, what="the first RPC")
        stop_hold.Pressed(next(stop_hold._presses)).hold(slot)  # the Stop's press
        await await_until(run, what="the command ended by the press")  # not raised to it
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
    assert [c["path"] for c in calls] == ["/a"] and stop_hold._local == {}


async def test_an_outer_cancel_of_a_local_command_still_propagates() -> None:
    """r4 GPT 1 (c): only the press's cancel ends at that await; the record is cleared
    on success, error and cancel alike."""
    slot = _slot("h-local")

    async def done() -> None:
        await asyncio.sleep(0)

    async def fails() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("the command failed")

    await stop_hold.run_local(slot, "kas", "/code", done())
    assert stop_hold._local == {}
    with pytest.raises(RuntimeError):
        await stop_hold.run_local(slot, "kas", "/code", fails())
    assert stop_hold._local == {}
    outer = asyncio.ensure_future(stop_hold.run_local(slot, "kas", "/code", asyncio.sleep(3600)))
    await asyncio.sleep(0)
    assert slot.key in stop_hold._local
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert stop_hold._local == {}


async def test_a_replacement_channel_session_inherits_the_channels_press() -> None:
    """r4 GPT 2, r5 (fails open without it): the press writes the channel's fact; the
    session replacing the closed one, under a new key, reads it after its binding is
    written and is held before any message reaches it. A release of the channel's
    session by a person drops the fact."""
    stop_hold.press(SimpleNamespace(get_slot=lambda _k: None), "chat-old", None, "C1")
    replacement = _slot("chat-new")
    stop_hold.carry("C1", replacement)
    assert stop_hold.holds(replacement)
    assert stop_hold.decide(replacement, stop_hold.stamp(stop_hold.seen() - 1, True)) == "held"
    untouched = _slot("chat-other")
    stop_hold.carry("C-never-pressed", untouched)
    assert not stop_hold.holds(untouched)
    with patch.object(stop_hold, "_save_soon"):
        assert stop_hold.gate(None, replacement, stop_hold.stamp(stop_hold.seen(), True))
    assert "C1" not in stop_hold._channels, "released by a person: nothing more to carry"
    from kiro_crew.slack.code_channels import routing

    record = next(
        n
        for n in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(routing._bind_channel))))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "record_binding"
    )
    lines = [ast.unparse(s) for s in record.body]
    write = next(i for i, line in enumerate(lines) if "to_thread(write, binding)" in line)
    assert "stop_hold.carry(channel, slot)" in lines[write + 1], "read right after the write"


async def test_a_held_session_is_offered_no_resume_button() -> None:
    """r4 GPT 3: a Stop during restart recovery holds the session; the notice drops it
    (only a person's message releases it), and with nothing left posts nothing."""
    from kiro_crew.slack.code_channels import resume

    held = _held(_slot("chat-h1"))
    free = _slot("chat-h2")
    slots = {held.key: held, free.key: free}
    state = SimpleNamespace(get_slot=slots.get)
    report = resume.ResumeReport(channel_id="C1")
    report.hold(held.key, resume.HELD_REFUSED)
    report.hold(free.key, resume.HELD_REFUSED)
    assert resume._unheld(state, report) and report.held == [free.key]
    only = resume.ResumeReport(channel_id="C1")
    only.hold(held.key, resume.HELD_REFUSED)
    assert resume._unheld(state, only) is False
    binding = SimpleNamespace(channel_id="C1")
    with patch.object(resume, "store") as store:
        await resume._notify(state, binding, None, only)
    assert not store.called, "nothing to offer: no record read, no post"


async def test_a_stop_at_any_await_of_a_replace_bind_holds_the_replacement(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r5 (both lanes), the hold-move rows ``press`` and ``record_binding replace``: the
    channel's session was closed, so a message's bind replaces it under a new key. A
    Stop lands at each await of that bind, once as a ``!stop`` that read the OLD binding
    before the replace and once reading the binding then; either way the replacement is
    held before any message reaches it (the press writes the channel's fact, then reads
    the binding; the replace writes the binding, then reads the channel's fact)."""
    from slack_code_channels_helpers import OWNER, make_env

    from kiro_crew.slack.code_channels import routing
    from kiro_crew.slack.code_channels.store import Binding

    async def parked(*_a: Any, **_k: Any) -> None:
        await asyncio.Event().wait()  # the Stop's writes: its sync part is what matters

    monkeypatch.setattr(stop_hold, "persist", parked)
    for env in make_env(monkeypatch, tmp_path):
        _yielding(  # each await of the bind really suspends, so the Stop can land there
            monkeypatch,
            "kiro_crew.slack.code_channels.routing._hydrate_origin_thread",
            "kiro_crew.slack.code_channels.routing.resolve_session_spec",
            "kiro_crew.slack.code_channels.workspace.core.plan_session",
            "kiro_crew.slack.code_channels.routing._post_kickoff",
            "kiro_crew.slack.code_channels.routing.create_bound_session",
        )
        assert env.state is not None
        env.state.get_slot = lambda k: next(  # type: ignore[attr-defined]
            (s for s in env.state.linked.values() if s.key == k), None
        )
        old = Binding(
            channel_id="C0CODE",
            team_id="T1",
            session_key="chat-old-1",
            anchor_ts="299.000001",  # the closed session's kickoff, linked to nothing now
            origin_channel_id="C0ORIGIN",
            origin_ts="1700000000.000100",
        )
        env.info("C0CODE")
        stops: list[Any] = []

        def stop(read_old: bool) -> None:
            binding = old if read_old else env.store.get_binding("C0CODE")
            coro = routing._stop_bound(env.orch, binding, OWNER, "C0CODE", source="test")
            coro.send(None)  # up to its first await: the press, synchronously, here
            stops.append(coro)

        async def replace(at: int | None, read_old: bool) -> tuple[Any, int]:
            env.store.put_binding(old)  # closed in the dashboard: no live slot links it
            stop_hold._channels.clear()
            coro = routing._bind_channel(
                env.orch,
                channel="C0CODE",
                team_id="T1",
                title="t",
                origin_channel_id="C0ORIGIN",
                origin_ts="1700000000.000100",
                origin_thread_ts="",
                created_by_us=True,
                replace=True,
            )
            bound, total = await _stepped(coro, -2 if at is None else at, lambda: stop(read_old))
            if at is None:  # the Stop lands once the bind has returned
                stop(read_old)
            return env.state.linked[bound.anchor_ts], total

        fresh, total = await replace(-2, True)  # never pressed
        assert not stop_hold.holds(fresh) and total > 2, total
        for read_old in (True, False):
            for at in [*range(total), None]:  # each await, then after the bind returned
                slot, _ = await replace(at, read_old)
                assert stop_hold.holds(slot), (at, read_old)
        for coro in stops:
            coro.close()


async def test_a_stop_never_cuts_a_clear_short_and_only_rpc_verbs_are_endable() -> None:
    """r5 Opus 2: only KAS commands that just send engine RPCs may be ended by a press.
    A Stop at each await of ``/clear``'s teardown leaves it whole (no discard left
    armed, the provider shut down); a new local command is not endable by default."""
    assert stop_hold.ENDABLE_LOCAL == frozenset({"/context", "/code", "/knowledge"})
    slot = _slot("h-clear")

    def teardown(state: dict[str, Any]) -> Any:
        async def run() -> None:
            state["pending_discard"] = True
            await asyncio.sleep(0)  # the session's end record
            await asyncio.sleep(0)  # the provider's shutdown
            state["provider_shut_down"], state["pending_discard"] = True, False

        return run()

    for word, backend in (("/clear", "kas"), ("/new-command", "kas"), ("/context", "codex")):
        for at in range(2):
            state: dict[str, Any] = {}
            coro = stop_hold.run_local(slot, backend, word, teardown(state))
            await _stepped(coro, at, lambda: stop_hold.Pressed(next(stop_hold._presses)).hold(slot))
            slot._stop_held = False
            assert state == {"pending_discard": False, "provider_shut_down": True}, (word, at)


async def test_a_press_still_ends_a_local_command_in_a_task_that_absorbed_a_cancel() -> None:
    """r5 Opus hardening: the outer cancel is judged by the count growing during the
    wait, not its absolute value, so an earlier absorbed cancel does not leak the press's;
    and a command's record is its own, so one ending never drops another's."""
    slot = _slot("h-absorbed")

    async def body() -> str:
        me = asyncio.current_task()
        assert me is not None
        me.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass  # absorbed without uncancel(): cancelling() stays 1
        loop = asyncio.get_running_loop()
        loop.call_soon(lambda: stop_hold.Pressed(next(stop_hold._presses)).hold(slot))
        await stop_hold.run_local(slot, "kas", "/context", asyncio.Event().wait())
        return "ended by the press"

    assert await asyncio.ensure_future(body()) == "ended by the press"
    later = asyncio.Event()
    second = asyncio.ensure_future(stop_hold.run_local(slot, "kas", "/code", later.wait()))
    await asyncio.sleep(0)
    first_task = stop_hold._local[slot.key]
    stop_hold._local[slot.key] = asyncio.ensure_future(asyncio.sleep(0))  # a newer one's
    later.set()
    await second
    assert first_task.done() and slot.key in stop_hold._local, "the newer record stays"
    await stop_hold._local.pop(slot.key)


async def test_a_stop_at_any_await_of_a_helpers_reopen_holds_it(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hold-move rows ``press`` / ``on_register`` / ``restore``: a closed helper reopens
    from history while a Stop lands at each await of the reopen. The press writes the
    key's pending fact then reads the live slots; the registration writes the slot then
    reads the pending fact; either order holds it, with the press's number kept."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
    from kiro_crew.dashboard.chat_persistence_parts.slot_builders import (
        rehydrate_slot_from_history_async,
    )

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    count = iter(range(1000))
    presses: list[int] = []

    def press(key: str) -> None:
        with patch("kiro_crew.slack.code_channels.store.store") as store:
            store.return_value.workers_of.return_value = [key]
            presses.append(stop_hold.press(state, "chat-conductor-1", None).press)

    async def reopen(at: int) -> tuple[Any, int]:
        key = f"chat-h{next(count)}-1"
        slot = state.get_or_create_slot(key)
        slot.append("user", "do it")
        assert await save_slot_off_loop(state, slot, force=True)
        state._slots.pop(key)  # closed in the dashboard
        reopened, total = await _stepped(
            rehydrate_slot_from_history_async(state, key), at, lambda: press(key)
        )
        return reopened, total

    fresh, total = await reopen(-2)
    assert fresh is not None and not stop_hold.holds(fresh) and total > 0
    for at in range(total + 1):
        reopened, _ = await reopen(at)
        assert reopened is not None and stop_hold.holds(reopened), at
        assert reopened._stop_hold_seq == presses[-1], "the press's own number"


_HOLD_FIELDS = {"_stop_held", "_stop_hold_seq"}


def _hold_field_uses(root: Any) -> list[str]:
    """Every read or write of a slot's hold fields in *root*, outside ``stop_hold.py``."""
    found = []
    for file in sorted(root.rglob("*.py")):
        rel = file.relative_to(root.parent).as_posix()
        text = file.read_text(encoding="utf-8")
        if rel == "kiro_crew/slack/code_channels/stop_hold.py" or "_stop_h" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Attribute) and node.attr in _HOLD_FIELDS:
                found.append(f"{rel}:{node.lineno} .{node.attr}")
            elif (
                isinstance(node, ast.Call)
                and ast.unparse(node.func) in ("getattr", "setattr", "hasattr", "delattr")
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _HOLD_FIELDS
            ):
                found.append(f"{rel}:{node.lineno} {ast.unparse(node.func)}")
    return found


async def test_every_decision_goes_through_holds() -> None:
    """r6 item 2: no module but ``stop_hold`` reads or writes a slot's hold fields, so
    every decision reads the pending press through ``stop_hold.holds``."""
    from pathlib import Path

    import kiro_crew

    assert _hold_field_uses(Path(kiro_crew.__file__).resolve().parent) == []


async def test_the_hold_field_scan_catches_a_planted_reader(tmp_path: Any) -> None:
    pkg = tmp_path / "kiro_crew" / "dashboard"
    pkg.mkdir(parents=True)
    (pkg / "planted.py").write_text(
        "def held(slot):\n"
        "    return slot._stop_held or getattr(slot, '_stop_hold_seq', 0)\n"
        "def free(slot):\n"
        "    setattr(slot, '_stop_held', False)\n"
    )
    assert len(_hold_field_uses(tmp_path / "kiro_crew")) == 3
