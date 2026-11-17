"""Any publication of a slot meets a pending Stop at its first decision (``stop_hold``).

r6 GPT: a helper republished without ``SlotRegistry.put_slot`` (reserved resume's
``state._slots[key] = slot``, a close rollback, a transfer install) never ran
``on_register``. ``stop_hold.holds`` now reads the pending press itself, so the first
decision on the slot holds it, whatever path published it; the hold applied is saved.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state, close_before_resume

from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.slack.code_channels import stop_hold

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    from kiro_crew.coder import retention

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(retention, "schedule_archived", lambda keys, notify=None: None)

    async def keep(_keys: Any) -> None:
        return None

    monkeypatch.setattr(retention, "keep_reopened", keep)
    stop_hold._keys.clear()
    stop_hold._channels.clear()
    yield
    stop_hold._keys.clear()
    stop_hold._channels.clear()


def _press(state: Any, key: str) -> int:
    """The channel's Stop, its helper *key* recorded in the store."""
    with patch("kiro_crew.slack.code_channels.store.store") as store:
        store.return_value.workers_of.return_value = [key]
        return stop_hold.press(state, "chat-conductor-1", None).press


def _queues_a_peers_send(state: Any, slot: Any) -> bool:
    run = MagicMock()
    return slot.enqueue_or_run_prompt("a peer's text", run, state) is False and not run.called


async def test_a_pending_press_holds_a_slot_whatever_published_it(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r6 item 3: with ``on_register`` turned into a no-op, a pending press still holds a
    slot the first time anything asks, and that hold is saved (item 5)."""
    monkeypatch.setattr(stop_hold, "on_register", lambda _slot: None)
    state = _make_state(tmp_path)
    press = _press(state, "chat-h1-1")
    slot = state.get_or_create_slot("chat-h1-1")  # registered: on_register did nothing
    assert slot._stop_held is False, "nothing wrote the hold at registration"
    with patch.object(stop_hold, "_save_soon") as save:
        assert stop_hold.holds(slot) and slot._stop_hold_seq == press
        assert save.call_args.args[1] is slot, "the hold applied on first ask is saved"
        assert stop_hold.holds(slot) and save.call_count == 1, "applied once"
    direct = stop_hold.queues  # a direct ``state._slots[key] = slot`` publication
    other = state.get_or_create_slot("chat-h2-1")
    state._slots.pop(other.key)
    _press(state, other.key)
    state._slots[other.key] = other
    assert direct(other) and _queues_a_peers_send(state, other)


async def test_a_stop_at_any_await_of_a_reserved_resume_holds_the_reopened_helper(
    tmp_path: Any,
) -> None:
    """Reserved resume: the slot is retracted while the tail awaits, then published by a
    direct write. A Stop at each await of the resume holds what it publishes."""
    from test_slack_code_channel_stop_hold import _stepped

    from kiro_crew.dashboard.chat_handlers import close_slot, resume_slot_from_history

    state = _make_state(tmp_path)
    count = iter(range(1000))

    async def closed_helper() -> tuple[str, str]:
        slot = state.get_or_create_slot(f"chat-r{next(count)}-1")
        slot.messages.extend(
            [{"role": "user", "content": "m0"}, {"role": "assistant", "content": "m1"}]
        )
        slot._dirty = True
        key = slot_history_key(slot)
        await close_slot(state, slot, slot.key)
        await asyncio.to_thread(close_before_resume, state.conversation_log, key)
        return slot.key, key

    async def resume(at: int) -> tuple[Any, int]:
        name, key = await closed_helper()
        coro = resume_slot_from_history(state, name=name, history_key=key)
        outcome, total = await _stepped(coro, at, lambda: _press(state, name))
        assert outcome.refusal is None, outcome.refusal
        return outcome.slot, total

    fresh, total = await resume(-2)
    assert not stop_hold.holds(fresh) and total > 1, total
    for at in range(total + 1):
        stop_hold._keys.clear()
        slot, _ = await resume(at)
        assert stop_hold.holds(slot) and _queues_a_peers_send(state, slot), at


async def test_a_close_rolled_back_after_a_stop_publishes_a_held_helper(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close whose archive save fails puts the slot back with a direct write; a Stop
    that landed during that save holds it."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-close-1")
    slot.append("user", "do it")
    pressed_absent: list[bool] = []

    async def failing_save(_state: Any, s: Any, **kw: Any) -> bool:
        if kw.get("closed"):
            pressed_absent.append(state.get_slot(s.key) is None)
            _press(state, s.key)
            raise OSError("disk full")
        return True

    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", failing_save)
    with pytest.raises(chat_handlers.SlotCloseError):
        await chat_handlers.close_slot(state, slot, slot.key)
    assert state.get_slot(slot.key) is slot, "the close rolled back"
    assert pressed_absent == [True], "the Stop found no live slot: only a pending press"
    assert stop_hold.holds(slot) and _queues_a_peers_send(state, slot)


async def test_a_live_helper_held_then_reopened_keeps_its_press(tmp_path: Any) -> None:
    """r7 GPT (fails open without it): the Stop holds a LIVE helper; it is closed and
    reopened through History Resume in the same boot. The reopened object is a new one,
    but the hold is its key's record: a stamp read before the press is refused on it,
    one read after releases, and the release leaves no record behind."""
    from kiro_crew.dashboard.chat_handlers import close_slot, resume_slot_from_history

    state = _make_state(tmp_path)
    helper = state.get_or_create_slot("chat-live-1")
    helper.messages.extend(
        [{"role": "user", "content": "m0"}, {"role": "assistant", "content": "m1"}]
    )
    helper._dirty = True
    before = stop_hold.stamp(stop_hold.seen(), True)  # a request that entered first
    press = _press(state, helper.key)
    assert stop_hold.holds(helper) and helper._stop_hold_seq == press
    key = slot_history_key(helper)
    await close_slot(state, helper, helper.key)
    await asyncio.to_thread(close_before_resume, state.conversation_log, key)
    outcome = await resume_slot_from_history(state, name=helper.key, history_key=key)
    reopened = outcome.slot
    assert reopened is not None and reopened is not helper
    assert stop_hold.decide(reopened, before) == stop_hold.HELD, "refused on any object"
    assert _queues_a_peers_send(state, reopened)
    after = stop_hold.stamp(stop_hold.seen(), True)
    with patch.object(stop_hold, "_save_soon"):
        assert stop_hold.gate(state, reopened, after)
    assert reopened.key not in stop_hold._keys and not stop_hold._channels, "bounded"
    assert not stop_hold.holds(reopened)


async def test_a_hold_applied_on_first_ask_is_saved_only_for_the_live_entry(
    tmp_path: Any,
) -> None:
    """r6 Opus note 3: the first-ask save is skipped for an object that is not the live
    registry entry (a closed or retracted one), so no stale object is written."""
    state = _make_state(tmp_path)
    live = state.get_or_create_slot("chat-entry-1")
    _press(state, "chat-other-1")  # the press ran under this state
    stop_hold._keys[live.key] = stop_hold.seen()
    stale = type(live)(live.key)  # another object of the same key, not registered
    with patch.object(stop_hold, "_save_soon") as save:
        assert stop_hold.holds(stale) and not save.called
        assert stop_hold.holds(live) and save.call_args.args[1] is live


async def test_a_refused_reserved_resume_of_a_held_helper_stays_closed_after_a_restart(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r8 Opus: the registration of a held key under construction must not save it (the
    save would write an open line before the tail refuses). The hold stays owed, and the
    first ask after a publication saves it."""
    from chat_test_helpers import drain_background_tasks

    from kiro_crew.coder import retention
    from kiro_crew.dashboard.chat_handlers import close_slot, resume_slot_from_history

    state = _make_state(tmp_path)
    helper = state.get_or_create_slot("chat-refused-1")
    helper.messages.extend(
        [{"role": "user", "content": "m0"}, {"role": "assistant", "content": "m1"}]
    )
    helper._dirty = True
    _press(state, helper.key)
    assert stop_hold.holds(helper)
    key = slot_history_key(helper)
    await close_slot(state, helper, helper.key)
    await asyncio.to_thread(close_before_resume, state.conversation_log, key)

    async def refuse(_keys: Any) -> str:
        return "disk full"

    monkeypatch.setattr(retention, "keep_reopened", refuse)
    outcome = await resume_slot_from_history(state, name=helper.key, history_key=key)
    assert outcome.refusal is not None and outcome.refusal.code == "reopen_retention_failed"
    await drain_background_tasks(state)
    meta, readable = state.conversation_log.get_metadata_status(key)
    assert readable and meta.get("closed"), "what the next start reads: still closed"
    monkeypatch.setattr(retention, "keep_reopened", lambda _keys: asyncio.sleep(0))
    with patch.object(stop_hold, "_save_soon") as save:
        reopened = (await resume_slot_from_history(state, name=helper.key, history_key=key)).slot
        assert reopened is not None and not save.called, "owed, not saved, while it was built"
        assert stop_hold.holds(reopened) and save.call_args.args[1] is reopened


async def test_a_directly_published_held_slot_no_ask_reached_still_writes_its_hold(
    tmp_path: Any,
) -> None:
    """r9 GPT B 1: a reserved resume publishes by a direct write and nothing asks before
    a shutdown save; the line still writes the key's hold, so a restart keeps it."""
    from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-direct-1")
    state._slots.pop(slot.key)  # retracted while its reserved tail awaits
    _press(state, slot.key)  # the Stop finds no live slot: only the key's record
    state._slots[slot.key] = slot  # the tail's direct publication
    assert slot._stop_held is False, "no ask has mirrored it"
    folds = codec.SaveFolds(memory_mode="persistent", created_at="2026-01-01T00:00:00")
    assert codec.encode(slot, folds=folds)["stop_held"] is True
    assert codec.encode(slot, folds=folds, merge=True)["stop_held"] is True
