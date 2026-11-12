"""Ultra Mode "Pause all" / "Resume all": the pause record and the rules it drives.

Pause stops every running crew member at every depth and leaves one that waits on
the person alone; while it stands the root and the stopped members start no turn,
their patrol wakes are refused, and nobody in the crew opens a session; Resume all
continues exactly the stopped set; the record survives a reload.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import crew_mode, crew_pause, platform_compat
from kiro_crew.dashboard.turn_dispatch import mark_turn_unstarted
from kiro_crew.testing.wait import until_parked


@pytest.fixture(autouse=True)
def _pause_store(tmp_path, _floor_monkeypatch):
    path = tmp_path / "work-ledger" / "crew-pause.json"
    _floor_monkeypatch.setattr(crew_pause, "_path", lambda: path)
    _floor_monkeypatch.setattr(crew_pause, "_loaded_from", None)
    _floor_monkeypatch.setattr(crew_pause, "_records", {})
    _floor_monkeypatch.setattr(crew_pause, "_resuming", {})
    _floor_monkeypatch.setattr(crew_pause, "_unsaved", False)
    _floor_monkeypatch.setattr(crew_mode, "_known_roots", set())
    _floor_monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    return path


def _forget_memory() -> None:
    """What a gateway restart does to the in-memory copy, before the preload has run."""
    crew_pause._records.clear()
    crew_pause._resuming = {}
    crew_pause._loaded_from = None


def _restart() -> None:
    """A gateway restart whose startup preload has read the file back."""
    _forget_memory()
    crew_pause.load()


# ── the record ─────────────────────────────────────────────────────────────────


def test_a_pause_holds_the_root_and_what_it_stopped_and_survives_a_reload(_pause_store):
    crew_pause.pause("root", ["w1", "w2", "w3"], ["w1", "w3"])
    _restart()
    assert crew_pause.record("root")["stopped"] == ["w1", "w3"]
    # The whole crew is held, idle members included (their patrol wakes too).
    assert crew_pause.holds_turn("root") and crew_pause.holds_turn("w3")
    assert crew_pause.holds_turn("w2") and not crew_pause.holds_turn("other")
    assert crew_pause.blocks_create("root") and crew_pause.blocks_create("w2")
    assert not crew_pause.blocks_create("other")
    if platform_compat.IS_POSIX:  # Windows restricts it with an ACL, not mode bits
        assert oct(_pause_store.stat().st_mode & 0o777) == "0o600"


def test_a_members_own_continue_releases_only_that_member():
    crew_pause.pause("root", ["w1", "w2"], ["w1", "w2"])
    assert crew_pause.release("w1") == {"root": {"fields": ["held", "stopped"], "dropped": None}}
    assert not crew_pause.holds_turn("w1") and crew_pause.holds_turn("w2")
    # Still in the crew, so it still opens no session while paused.
    assert crew_pause.blocks_create("w1")
    _restart()
    assert crew_pause.record("root")["stopped"] == ["w2"]


def test_resume_drops_the_record_and_everything_it_held():
    crew_pause.pause("root", ["w1"], ["w1"])
    assert crew_pause.resume("root")["stopped"] == ["w1"]
    assert crew_pause.record("root") is None
    assert not crew_pause.holds_turn("root") and not crew_pause.blocks_create("w1")
    assert crew_pause.resume("root") is None


def test_a_write_that_fails_changes_no_live_state(monkeypatch):
    crew_pause.pause("root", ["w1"], ["w1"])

    def _fail(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(crew_pause, "atomic_write", _fail)
    with pytest.raises(OSError):
        crew_pause.resume("root")
    assert crew_pause.record("root") is not None and crew_pause.holds_turn("w1")
    with pytest.raises(OSError):
        crew_pause.release("w1")
    assert crew_pause.holds_turn("w1")


def test_a_pause_file_too_big_to_read_back_is_never_written(_pause_store, monkeypatch):
    crew_pause.pause("root", ["w1"], ["w1"])
    before = _pause_store.read_bytes()
    monkeypatch.setattr(crew_pause, "MAX_FILE_BYTES", len(before) + 10)
    many = [f"chat-member-{i}" for i in range(50)]
    with pytest.raises(OSError):
        crew_pause.pause("root2", many, many)
    # The stored pauses still read back after a restart, and memory agrees.
    assert _pause_store.read_bytes() == before
    assert crew_pause.record("root2") is None
    _restart()
    assert crew_pause.record("root")["stopped"] == ["w1"]


@pytest.mark.parametrize("cause", ["disk_full", "size_cap"])
def test_a_turn_owed_when_its_write_is_refused_is_still_continued(_pause_store, monkeypatch, cause):
    crew_pause.pause("root", ["w1", "w2"], ["w1"])

    def _fail(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as mp:
        if cause == "disk_full":
            mp.setattr(crew_pause, "atomic_write", _fail)
        else:  # the file is at the cap, so adding w2 would pass it
            mp.setattr(crew_pause, "MAX_FILE_BYTES", _pause_store.stat().st_size)
        # The dispatch gate ends w2's held turn, and the write of that obligation fails.
        assert crew_pause.owe_turn("w2") is True
    # Resume all still finds it owed, so it continues w2.
    assert crew_pause.record("root")["stopped"] == ["w1", "w2"]
    if cause == "disk_full":
        # Once the disk recovers, the gate's next refusal stores it for a restart.
        assert crew_pause.owe_turn("w2") is True
        _restart()
        assert crew_pause.record("root")["owed"] == ["w2"]
    assert crew_pause.resume("root")["stopped"] == ["w1", "w2"]


@pytest.mark.parametrize(
    "body", ["{not json", '{"paused": {"root": {"tree": 1, "held": "w1", "stopped": null}}}']
)
def test_a_damaged_file_pauses_nothing(_pause_store, body):
    _pause_store.parent.mkdir(parents=True)
    _pause_store.write_text(body, encoding="utf-8")
    crew_pause.load()  # never raises: every turn and create awaits this read
    assert crew_pause._loaded_from == _pause_store  # read once, not on every turn
    rec = crew_pause.record("root")
    assert rec is None or rec["held"] == rec["tree"] == rec["stopped"] == []
    assert not crew_pause.holds_turn("w1")


# ── the routes ─────────────────────────────────────────────────────────────────


def _row(key: str, **over: Any) -> dict[str, Any]:
    return {"key": key, "agent": "kirocrew", "agent_kind": "template", "parent": None, **over}


# root runs; w1 runs; its sub-team member w3 runs too; w2 waits on an approval;
# w4 is idle; "other" is outside the crew.
_TREE = [
    _row("root", agent="kirocrew-conductor", running=True),
    _row("w1", created_by="root", running=True),
    _row("w2", created_by="root", running=True, pending_approval=True),
    _row("w3", created_by="w1", running=True),
    _row("w4", created_by="root"),
    _row("other", running=True),
]


def _fake_slot(key: str, *, queued: int = 0) -> Any:
    return SimpleNamespace(
        key=key,
        running=False,
        linked_turn_running=False,
        queue_depth=queued,
        _queue=[{"id": "q"}] * queued,
        _lock=asyncio.Lock(),
        _turn_admission_reserved=False,
        is_closing=False,
        _stop_state="idle",
        window_pending=False,
        _turn_generation=1,
        _unstarted_generation=-1,
        _turns_started=0,
    )


def _state(rows: list[dict[str, Any]], slots: dict[str, Any] | None = None) -> Any:
    def serialize_slots(**_k: Any) -> list[dict[str, Any]]:
        out = [dict(r) for r in rows]
        crew_mode.stamp_rows(out)
        return out

    slots = slots if slots is not None else {r["key"]: _fake_slot(r["key"]) for r in rows}
    return SimpleNamespace(
        serialize_slots=serialize_slots, get_slot=slots.get, push_slots_update=lambda: None
    )


def _req(state: Any, slot: str = "root") -> Any:
    app = web.Application()
    app["state"] = state
    return make_mocked_request("POST", "/x", app=app, match_info={"slot": slot})


@pytest.fixture
def routes(monkeypatch):
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard import state as dash_state
    from kiro_crew.dashboard.handlers import crew_pause as handler

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(handler, "_gate", _allow)
    calls: dict[str, list[Any]] = {"stop": [], "continue": [], "drain": [], "notice": []}

    async def _stop(state: Any, slot: Any, **kw: Any) -> dict[str, Any]:
        calls["stop"].append((slot.key, kw.get("escalate")))
        return {"ok": True}

    async def _continue(state: Any, slot: Any, **kw: Any) -> str:
        calls["continue"].append(slot.key)
        return "resumed"

    async def _drain(state: Any, slot: Any, **kw: Any) -> bool:
        calls["drain"].append(slot.key)
        return True

    monkeypatch.setattr(chat_handlers, "stop_slot_turn", _stop)
    monkeypatch.setattr(chat_handlers, "continue_slot_turn", _continue)
    monkeypatch.setattr(chat_handlers, "_start_next_queued_turn", _drain)
    monkeypatch.setattr(
        dash_state, "append_and_surface", lambda *a, **kw: calls["notice"].append(kw["meta"])
    )
    return SimpleNamespace(handler=handler, calls=calls)


@pytest.mark.asyncio
async def test_pause_stops_every_running_member_at_every_depth_and_leaves_needs_you(routes):
    resp = await routes.handler.api_crew_pause(_req(_state(_TREE)))
    assert resp.status == 200, resp.text
    assert json.loads(resp.text)["stopped"] == ["w1", "w3"]
    stopped = sorted(key for key, _ in routes.calls["stop"])
    assert stopped == ["root", "w1", "w3"]
    # A first Stop press each: never escalated to a hard kill that drops the queue.
    assert {esc for _, esc in routes.calls["stop"]} == {False}
    rec = crew_pause.record("root")
    assert rec["stopped"] == ["root", "w1", "w3"]
    assert rec["tree"] == ["w1", "w2", "w3", "w4"]
    assert routes.calls["notice"] == [{"kind": "crew_paused_notice", "stopped": 2}]
    # Nothing new starts: the root, the needs-you member and the idle one open no session.
    assert all(crew_pause.blocks_create(k) for k in ("root", "w2", "w4"))
    assert crew_pause.holds_turn("w2") and not crew_pause.holds_turn("other")


@pytest.mark.asyncio
async def test_a_stop_the_compaction_declined_is_not_counted_as_stopped(routes, monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    async def _stop(state: Any, slot: Any, **kw: Any) -> dict[str, Any]:
        return {"ok": True, "info": "compacting"} if slot.key == "w3" else {"ok": True}

    monkeypatch.setattr(chat_handlers, "stop_slot_turn", _stop)
    resp = await routes.handler.api_crew_pause(_req(_state(_TREE)))
    body = json.loads(resp.text)
    assert body["stopped"] == ["w1"] and body["not_stopped"] == ["w3"]
    assert crew_pause.record("root")["stopped"] == ["root", "w1"]
    # Still held, so its turn's end starts nothing.
    assert crew_pause.holds_turn("w3")


@pytest.mark.asyncio
async def test_a_refused_stop_is_not_counted_as_stopped(routes, monkeypatch):
    """A stop refused with ``ok: False`` and no ``info`` (a cron run the pause may
    not cancel) stopped nothing, so Resume all owes it no Continue."""
    from kiro_crew.dashboard import chat_handlers

    async def _stop(state: Any, slot: Any, **kw: Any) -> dict[str, Any]:
        if slot.key == "w3":
            return {"ok": False, "code": "owner_only", "error": "refused"}
        return {"ok": True}

    monkeypatch.setattr(chat_handlers, "stop_slot_turn", _stop)
    resp = await routes.handler.api_crew_pause(_req(_state(_TREE)))
    body = json.loads(resp.text)
    assert body["stopped"] == ["w1"] and body["not_stopped"] == ["w3"]
    assert crew_pause.record("root")["stopped"] == ["root", "w1"]

    await routes.handler.api_crew_resume(_req(_state(_TREE)))
    assert "w3" not in routes.calls["continue"]


@pytest.mark.asyncio
async def test_a_turn_still_being_prepared_counts_as_stopped(routes, monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    async def _stop(state: Any, slot: Any, **kw: Any) -> dict[str, Any]:
        # The provider has no turn yet; the recorded Stop aborts it before dispatch.
        return {"ok": True, "info": "no active turn"} if slot.key == "w3" else {"ok": True}

    monkeypatch.setattr(chat_handlers, "stop_slot_turn", _stop)
    resp = await routes.handler.api_crew_pause(_req(_state(_TREE)))
    assert json.loads(resp.text)["stopped"] == ["w1", "w3"]
    assert crew_pause.record("root")["stopped"] == ["root", "w1", "w3"]


@pytest.mark.asyncio
async def test_a_repeated_pause_keeps_what_an_earlier_one_stopped(routes, monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    async def _stop(state: Any, slot: Any, **kw: Any) -> dict[str, Any]:
        return {"ok": True, "info": "compacting"} if slot.key == "w3" else {"ok": True}

    monkeypatch.setattr(chat_handlers, "stop_slot_turn", _stop)
    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    # Pressed again: w1 has settled, only w3 still runs, and declines again.
    again = [dict(r, running=r["key"] in ("w3", "other")) for r in _TREE]
    await routes.handler.api_crew_pause(_req(_state(again)))
    assert crew_pause.record("root")["stopped"] == ["root", "w1"]


@pytest.mark.asyncio
async def test_a_turn_an_earlier_pause_stopped_is_still_continued_after_a_second_pause(
    routes, monkeypatch
):
    from kiro_crew.dashboard import chat_handlers

    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    assert crew_pause.record("root")["stopped"] == ["root", "w1", "w3"]

    # Pressed again while w1's and the root's stopped turns still tear down: the
    # snapshot sees them running, and by the time the stop lands they are gone.
    async def _gone(state: Any, slot: Any, **kw: Any) -> dict[str, Any]:
        return {"ok": True, "info": "not running"}

    monkeypatch.setattr(chat_handlers, "stop_slot_turn", _gone)
    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    # The first pause's obligations stand: nothing it stopped is forgotten.
    assert crew_pause.record("root")["stopped"] == ["root", "w1", "w3"]

    resp = await routes.handler.api_crew_resume(_req(_state(_TREE)))
    assert resp.status == 200, resp.text
    body = json.loads(resp.text)
    assert body["resumed"]["w1"] == "continued" and body["resumed"]["w3"] == "continued"
    assert sorted(routes.calls["continue"]) == ["root", "w1", "w3"]
    assert crew_pause.record("root") is None


@pytest.mark.asyncio
async def test_resume_after_a_restart_sends_the_queued_message_of_a_stopped_worker(routes):
    from kiro_crew.dashboard.slot_queue_repository import (
        RESTORED_QUEUE_KEY,
        RESTORED_SEND_CHOSEN_KEY,
    )

    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    # A message queued on w1 while paused; the gateway restarts, so the queue
    # comes back from disk and the pause file is read again.
    _restart()
    assert crew_pause.holds_turn("w1")
    slots = {r["key"]: _fake_slot(r["key"]) for r in _TREE}
    slots["w1"] = _fake_slot("w1", queued=1)
    slots["w1"]._queue = [{"id": "q-restored", "content": "next step", RESTORED_QUEUE_KEY: True}]
    # w4 was idle, so the pause stopped nothing there: its restored card stays a card.
    slots["w4"] = _fake_slot("w4", queued=1)
    slots["w4"]._queue = [{"id": "q-w4", "content": "later", RESTORED_QUEUE_KEY: True}]
    resp = await routes.handler.api_crew_resume(_req(_state(_TREE, slots)))
    assert resp.status == 200, resp.text
    body = json.loads(resp.text)
    # Its queued message runs instead of a Continue the restored queue would refuse.
    assert body["resumed"]["w1"] == "drained"
    # Sent as the person's own "run this next": the card is marked chosen.
    assert slots["w1"]._queue[0][RESTORED_SEND_CHOSEN_KEY] is True
    assert "w1" in routes.calls["drain"] and "w1" not in routes.calls["continue"]
    assert body["resumed"]["w4"] == "idle" and "w4" not in routes.calls["drain"]
    assert RESTORED_SEND_CHOSEN_KEY not in slots["w4"]._queue[0]
    # Nothing is left on hold.
    assert crew_pause.record("root") is None
    assert not any(crew_pause.holds_turn(k) for k in ("root", "w1", "w2", "w3", "w4"))


@pytest.mark.parametrize("dropped", [False, True])
@pytest.mark.asyncio
async def test_a_restored_card_that_does_not_start_is_not_lost(routes, monkeypatch, dropped):
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.slot_queue_repository import (
        RESTORED_QUEUE_KEY,
        RESTORED_SEND_CHOSEN_KEY,
    )

    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    _restart()

    async def _not_started(state: Any, slot: Any, **kw: Any) -> bool:
        routes.calls["drain"].append(slot.key)
        if dropped:  # judged stale: the card is gone
            slot._queue.clear()
            slot.queue_depth = 0
        return False  # otherwise held back (sub-agents still running)

    monkeypatch.setattr(chat_handlers, "_start_next_queued_turn", _not_started)
    slots = {r["key"]: _fake_slot(r["key"]) for r in _TREE}
    slots["w1"] = _fake_slot("w1", queued=1)
    slots["w1"]._queue = [{"id": "q-restored", "content": "next step", RESTORED_QUEUE_KEY: True}]
    resp = await routes.handler.api_crew_resume(_req(_state(_TREE, slots)))
    body = json.loads(resp.text)
    if dropped:
        # Nothing will send it, and the stopped turn is still owed: Continue.
        assert body["resumed"]["w1"] == "continued" and "w1" in routes.calls["continue"]
    else:
        # Still queued and chosen, so the drain after the park sends it.
        assert body["resumed"]["w1"] == "held" and "w1" not in routes.calls["continue"]
        assert slots["w1"]._queue[0][RESTORED_SEND_CHOSEN_KEY] is True
    assert not crew_pause.holds_turn("w1")


@pytest.mark.parametrize("memory", ["not_ready", "ready", "task_unpublished"])
@pytest.mark.asyncio
async def test_resume_while_memory_prepares_never_parks_a_stopped_workers_card(
    routes, monkeypatch, memory
):
    from kiro_crew import memory_startup
    from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY

    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    _restart()
    slots = {r["key"]: _fake_slot(r["key"]) for r in _TREE}
    slots["w1"] = _fake_slot("w1", queued=1)
    slots["w1"]._queue = [{"id": "q-restored", "content": "next step", RESTORED_QUEUE_KEY: True}]
    state = _state(_TREE, slots)
    # Memory is still preparing after the restart.
    ready = memory == "ready"
    if memory == "task_unpublished":  # the listener is up, the task not yet on the state
        monkeypatch.setattr(memory_startup, "memory_startup_preparing", lambda: True)
    else:
        state.memory_startup_task = asyncio.get_running_loop().create_future()
    if ready:  # it finishes within a turn's grace
        asyncio.get_running_loop().call_soon(state.memory_startup_task.set_result, None)
    elif memory == "not_ready":  # it does not
        monkeypatch.setattr(memory_startup, "MEMORY_ADMISSION_WAIT_SECONDS", 0)
    before = crew_pause.record("root")
    resp = await routes.handler.api_crew_resume(_req(state))
    if ready:
        body = json.loads(resp.text)
        assert body["resumed"]["w1"] == "drained" and "w1" in routes.calls["drain"]
        assert not crew_pause.holds_turn("w1")
    else:
        # Its card would only park, and the drain after the park owes no
        # Continue: the pause stays exactly as it was, stored, for a later Resume.
        assert resp.status == 503 and json.loads(resp.text)["code"] == "memory_preparing"
        assert routes.calls["drain"] == [] and routes.calls["continue"] == []
        assert crew_pause.record("root") == before
        _restart()
        assert crew_pause.record("root") == before


@pytest.mark.parametrize("queued", [True, False])
@pytest.mark.asyncio
async def test_a_members_continue_while_memory_prepares_keeps_it_paused(
    tmp_path, monkeypatch, queued
):
    from chat_test_helpers import _make_state

    from kiro_crew import memory_startup
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat_api import continue_turn
    from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-w1")
    if queued:  # a restored card; otherwise Continue would queue its own recovery entry
        slot._queue = [{"id": "q-restored", "content": "next step", RESTORED_QUEUE_KEY: True}]
    crew_pause.pause("chat-root", ["chat-w1"], ["chat-w1"])
    state.memory_startup_task = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(memory_startup, "MEMORY_ADMISSION_WAIT_SECONDS", 0)
    started = AsyncMock(return_value=False)
    monkeypatch.setattr(chat_handlers, "_start_next_queued_turn", started)
    continued = AsyncMock()
    monkeypatch.setattr(chat_handlers, "continue_slot_turn", continued)
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("POST", "/x", app=app, match_info={"slot": "chat-w1"})
    resp = await continue_turn.api_chat_slot_continue(req)
    assert resp.status == 409 and json.loads(resp.text)["code"] == "crew_paused"
    # Nothing parked and nothing continued: the pause still owes it its hand-back.
    started.assert_not_awaited()
    continued.assert_not_awaited()
    assert crew_pause.record("chat-root")["stopped"] == ["chat-w1"]
    assert crew_pause.holds_turn("chat-w1")


@pytest.mark.asyncio
async def test_a_turn_the_pause_gate_ended_during_a_compaction_is_continued(routes, monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    # w3's send waits on a compaction: the pause's Stop answers "compacting".
    async def _stop(state: Any, slot: Any, **kw: Any) -> dict[str, Any]:
        return {"ok": True, "info": "compacting"} if slot.key == "w3" else {"ok": True}

    monkeypatch.setattr(chat_handlers, "stop_slot_turn", _stop)
    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    assert "w3" not in crew_pause.record("root")["stopped"]
    # The compaction ends and the dispatch gate ends the held turn: it is owed.
    assert crew_pause.owe_turn("w3") is True
    assert not crew_pause.owe_turn("other")
    resp = await routes.handler.api_crew_resume(_req(_state(_TREE)))
    assert json.loads(resp.text)["resumed"]["w3"] == "continued"
    assert "w3" in routes.calls["continue"]


@pytest.mark.asyncio
async def test_a_turn_owed_while_the_pause_settles_is_not_settled_away(routes, monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    # w3's Stop is declined by a compaction, and that compaction ends while the
    # pause still waits on the other stops: the gate ends w3's turn, owing it.
    async def _stop(state: Any, slot: Any, **kw: Any) -> dict[str, Any]:
        if slot.key == "w3":
            await asyncio.to_thread(crew_pause.owe_turn, "w3")
            return {"ok": True, "info": "compacting"}
        return {"ok": True}

    monkeypatch.setattr(chat_handlers, "stop_slot_turn", _stop)
    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    rec = crew_pause.record("root")
    assert "w3" in rec["stopped"] and rec["owed"] == ["w3"]


def test_a_turn_ended_while_resume_hands_back_is_left_to_that_resume():
    crew_pause.pause("root", ["w1", "w2"], ["w1"])
    # Resume all drops the record; a turn of w1 that raced the hold reaches its
    # gate before the hand-backs finish: that Resume continues w1, so end it.
    crew_pause.resume("root")
    assert crew_pause.owe_turn("w1") is True
    # w2 was never stopped: nothing continues it, so its turn runs.
    assert crew_pause.owe_turn("w2") is False
    crew_pause.forget_resuming(["w1"])
    assert crew_pause.owe_turn("w1") is False


def test_a_restart_before_resume_hands_back_keeps_the_stopped_members_paused():
    # Resume all dropped the record, then the gateway restarted before its
    # hand-backs finished: the members still owed stay paused.
    crew_pause.pause("root", ["w1", "w2", "w3"], ["root", "w1", "w2"])
    crew_pause.resume("root")
    crew_pause.forget_resuming(["w1"])  # w1 was handed back before the restart
    _restart()
    rec = crew_pause.record("root")
    assert rec is not None and rec["stopped"] == ["root", "w2"] and rec["held"] == ["w2"]
    assert crew_pause.holds_turn("w2") and not crew_pause.holds_turn("w1")
    assert crew_pause.root_paused("root") and crew_pause.owe_turn("w2") is True
    # The next Resume all continues them, and a later restart brings nothing back.
    assert crew_pause.resume("root")["stopped"] == ["root", "w2"]
    crew_pause.forget_resuming(["root", "w2"])
    _restart()
    assert crew_pause.record("root") is None


def test_members_resume_could_not_hand_back_are_held_in_the_same_write(_pause_store):
    crew_pause.pause("root", ["w1", "w2"], ["w1", "w2"])
    crew_pause.resume("root")
    crew_pause.forget_resuming(["w1", "w2"], root="root", pending=["w2"], root_pending=False)
    stored = json.loads(_pause_store.read_text(encoding="utf-8"))
    assert "resuming" not in stored and stored["paused"]["root"]["stopped"] == ["w2"]
    _restart()
    assert crew_pause.holds_turn("w2") and not crew_pause.holds_turn("w1")


def test_a_refused_hand_back_write_still_frees_memory_and_keeps_the_file_owing(
    _pause_store, monkeypatch
):
    crew_pause.pause("root", ["w1"], ["w1"])
    crew_pause.resume("root")

    def _fail(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as mp:
        mp.setattr(crew_pause, "atomic_write", _fail)
        assert crew_pause.forget_resuming(["w1"]) is False
        # The gate runs w1's turns again, though the write is still refused.
        assert crew_pause.owe_turn("w1") is False
    # The file still names it, so a restart before the next write holds it
    # again rather than losing it.
    assert json.loads(_pause_store.read_text(encoding="utf-8"))["resuming"] == {"root": ["w1"]}
    _restart()
    assert crew_pause.holds_turn("w1")


@pytest.mark.asyncio
async def test_resume_all_reports_a_pause_file_it_could_not_save(routes, monkeypatch):
    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    real = crew_pause.resume

    def _fail(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    def _resume_then_the_disk_fills(root: str) -> Any:
        rec = real(root)
        monkeypatch.setattr(crew_pause, "atomic_write", _fail)
        return rec

    monkeypatch.setattr(crew_pause, "resume", _resume_then_the_disk_fills)
    resp = await routes.handler.api_crew_resume(_req(_state(_TREE)))
    assert resp.status == 500 and json.loads(resp.text)["code"] == "pause_file_unsaved"


@pytest.mark.asyncio
async def test_a_live_queue_dropped_as_stale_still_continues_the_stopped_worker(
    routes, monkeypatch
):
    from kiro_crew.dashboard import chat_handlers

    await routes.handler.api_crew_pause(_req(_state(_TREE)))

    async def _stale(state: Any, slot: Any, **kw: Any) -> bool:
        slot._queue.clear()  # the follow-up failed its containment check
        slot.queue_depth = 0
        return False

    monkeypatch.setattr(chat_handlers, "_start_next_queued_turn", _stale)
    slots = {r["key"]: _fake_slot(r["key"]) for r in _TREE}
    slots["w1"] = _fake_slot("w1", queued=1)
    resp = await routes.handler.api_crew_resume(_req(_state(_TREE, slots)))
    body = json.loads(resp.text)
    assert body["resumed"]["w1"] == "continued" and "w1" in routes.calls["continue"]


def test_readers_never_wait_on_a_writer():
    crew_pause.pause("r", ["w1"], ["w1"])
    seen: list[Any] = []

    def _read() -> None:
        seen.extend(
            [
                crew_pause.holds_turn("w1"),
                crew_pause.blocks_create("w1"),
                crew_pause.root_paused("r"),
            ]
        )

    with crew_pause._lock:  # a writer mid-write
        reader = threading.Thread(target=_read)
        reader.start()
        reader.join(timeout=5)
        assert not reader.is_alive()
    assert seen == [True, True, True]


def test_both_entrypoints_preload_the_pause_file_at_startup():
    from kiro_crew.dashboard import server

    app = web.Application()
    server._register_mcp_routes(app)
    assert crew_pause.startup_preload in list(app.on_startup)


def test_the_startup_preload_is_not_awaited_and_the_first_read_sees_the_pause(
    _pause_store, _floor_monkeypatch
):
    crew_pause.pause("r", ["w1"], ["w1"])
    _forget_memory()
    release = threading.Event()
    real_load = crew_pause.load

    def _slow_load() -> None:
        release.wait(5)
        real_load()

    _floor_monkeypatch.setattr(crew_pause, "load", _slow_load)

    async def _boot() -> bool:
        # The hook returns while the read is still blocked: boot does not wait on it.
        # Were it awaited, the read would have finished and left no task in flight.
        await crew_pause.startup_preload()
        started = bool(crew_pause._PRELOAD_TASKS)
        release.set()
        await asyncio.gather(*list(crew_pause._PRELOAD_TASKS))
        return started

    assert asyncio.run(_boot()) is True
    assert not crew_pause._PRELOAD_TASKS
    assert crew_pause.holds_turn("w1") and crew_pause.record("r")["stopped"] == ["w1"]


def test_a_read_before_the_preload_finishes_never_reads_the_file(_pause_store, monkeypatch):
    crew_pause.pause("r", ["w1"], ["w1"])
    _forget_memory()

    def _no_file_read() -> None:
        raise AssertionError("a reader on the event loop read the pause file")

    monkeypatch.setattr(crew_pause, "load", _no_file_read)
    monkeypatch.setattr(crew_pause, "_ensure_loaded", _no_file_read)
    # In memory only: until the preload lands, no crew counts as paused yet.
    assert not crew_pause.holds_turn("w1")
    assert not crew_pause.blocks_create("w1") and not crew_pause.root_paused("r")
    assert crew_pause.record("r") is None


@pytest.mark.asyncio
async def test_a_route_that_must_know_waits_for_the_file_off_the_loop(_pause_store):
    crew_pause.pause("r", ["w1"], ["w1"])
    _forget_memory()
    await crew_pause.ensure_loaded()
    assert crew_pause.holds_turn("w1") and crew_pause.record("r")["stopped"] == ["w1"]


def test_load_reads_the_file_once_for_the_startup_preload(_pause_store):
    crew_pause.pause("r", ["w1"], [])
    _forget_memory()
    crew_pause.load()
    assert crew_pause._loaded_from == _pause_store
    assert crew_pause.holds_turn("w1")


@pytest.mark.asyncio
async def test_resume_continues_exactly_what_the_pause_stopped(routes):
    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    slots = {r["key"]: _fake_slot(r["key"]) for r in _TREE}
    slots["w3"] = _fake_slot("w3", queued=1)
    resp = await routes.handler.api_crew_resume(_req(_state(_TREE, slots)))
    assert resp.status == 200, resp.text
    # Stopped members are continued; the idle and needs-you ones only drain a queue.
    assert json.loads(resp.text)["resumed"] == {
        "w1": "continued",
        "w2": "idle",
        "w3": "drained",
        "w4": "idle",
    }
    assert sorted(routes.calls["continue"]) == ["root", "w1"]
    assert routes.calls["drain"] == ["w3"]
    assert crew_pause.record("root") is None
    again = await routes.handler.api_crew_resume(_req(_state(_TREE)))
    assert again.status == 409


@pytest.mark.asyncio
async def test_a_turn_started_during_the_pause_write_is_stopped_too(routes, monkeypatch):
    rows = [dict(r) for r in _TREE]
    real = crew_pause.pause
    calls: list[list[str]] = []

    def _pause_while_w4_wakes(root: str, tree: list[str], stopped: list[str]) -> Any:
        calls.append(list(stopped))
        if len(calls) == 1:
            rows[4]["running"] = True  # w4's patrol wake starts during the write
        return real(root, tree, stopped)

    monkeypatch.setattr(crew_pause, "pause", _pause_while_w4_wakes)
    resp = await routes.handler.api_crew_pause(_req(_state(rows)))
    assert json.loads(resp.text)["stopped"] == ["w1", "w3", "w4"]
    assert ("w4", False) in routes.calls["stop"]
    assert crew_pause.record("root")["stopped"] == ["root", "w1", "w3", "w4"]


@pytest.mark.asyncio
async def test_a_refused_hand_back_stays_paused_and_is_reported(routes, monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    await routes.handler.api_crew_pause(_req(_state(_TREE)))

    async def _refuse_w1(state: Any, slot: Any, **kw: Any) -> str:
        if slot.key == "w1":
            raise chat_handlers.SlotContinueRefusal(
                "sub-agents still run", "slot_subagents_running", 409
            )
        return "resumed"

    monkeypatch.setattr(chat_handlers, "continue_slot_turn", _refuse_w1)
    body = json.loads((await routes.handler.api_crew_resume(_req(_state(_TREE)))).text)
    assert body["failed"] == ["w1"] and body["paused"] is True
    assert body["resumed"]["w1"] == "slot_subagents_running"
    assert crew_pause.record("root")["stopped"] == ["w1"]
    assert crew_pause.holds_turn("w1") and not crew_pause.holds_turn("w3")


@pytest.mark.asyncio
async def test_resume_re_asks_a_synthesis_the_pause_withheld(routes, monkeypatch):
    from kiro_crew.dashboard import chat_runner

    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    slots = {r["key"]: _fake_slot(r["key"]) for r in _TREE}
    # w4 was idle with its sub-agents' synthesis armed and held by the pause.
    slots["w4"]._pending_synthesis = True
    slots["w4"]._synthesis_inflight = False
    slots["w4"]._synthesis_rechecks = 3
    armed: list[str] = []
    monkeypatch.setattr(chat_runner, "_arm_synthesis_recheck", lambda st, sl: armed.append(sl.key))
    await routes.handler.api_crew_resume(_req(_state(_TREE, slots)))
    assert armed == ["w4"]
    assert slots["w4"]._synthesis_rechecks == 0


@pytest.mark.asyncio
async def test_resume_gives_an_idle_root_no_continue_it_never_had_a_turn_for(routes):
    rows = [{**r, "running": False} if r["key"] == "root" else r for r in _TREE]
    await routes.handler.api_crew_pause(_req(_state(rows)))
    await routes.handler.api_crew_resume(_req(_state(rows)))
    assert "root" not in routes.calls["continue"]


@pytest.mark.asyncio
async def test_pause_refuses_a_session_that_is_not_a_crew_root(routes):
    resp = await routes.handler.api_crew_pause(_req(_state(_TREE), "other"))
    assert resp.status == 404
    assert crew_pause.record("other") is None


@pytest.mark.asyncio
async def test_the_pause_routes_take_the_owner_gate(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_pause as handler
    from kiro_crew.dashboard.handlers import crew_workers

    async def _deny(*_a: Any, **_k: Any) -> web.Response:
        return web.json_response({"code": "owner_only"}, status=403)

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(crew_workers, "_deny_app_caller", _allow)
    monkeypatch.setattr(crew_workers, "require_owner_dashboard_request", _deny)
    paused = await handler.api_crew_pause(_req(_state(_TREE)))
    resumed = await handler.api_crew_resume(_req(_state(_TREE)))
    assert paused.status == resumed.status == 403
    assert crew_pause.record("root") is None


@pytest.mark.asyncio
async def test_the_worker_list_carries_the_pause(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(crew_workers, "_gate", _allow)
    monkeypatch.setattr(crew_workers, "_closed_workers", lambda root, live: [])
    monkeypatch.setattr(crew_workers, "_ledger_items", lambda root: {})
    state = SimpleNamespace(**{**vars(_state(_TREE)), "get_slot": lambda _k: None})
    before = json.loads((await crew_workers.api_crew_workers(_req(state))).text)
    assert "paused" not in before
    # The lead's own stopped turn is not counted as a stopped worker.
    crew_pause.pause("root", ["w1"], ["root", "w1"])
    after = json.loads((await crew_workers.api_crew_workers(_req(state))).text)
    assert after["paused"]["stopped"] == ["w1"]


# ── what the pause holds ───────────────────────────────────────────────────────


def test_a_pause_landing_during_the_drain_probes_holds_the_drain(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_handlers, chat_runner

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-w9")
    slot.queue_append("next step")

    async def _pause_meanwhile(_state: Any, _slot: Any) -> bool:
        crew_pause.pause("chat-root", ["chat-w9"], [])
        return False

    monkeypatch.setattr(chat_runner, "_purge_superseded_continuations", _pause_meanwhile)
    started = asyncio.run(chat_handlers._start_next_queued_turn(state, slot))
    assert started is False
    assert [e.get("content") for e in slot._queue] == ["next step"]
    assert slot.task is None


def test_a_held_slot_keeps_its_queue_and_starts_no_turn(tmp_path):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_turn.queue_drain import _start_next_queued_turn

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-held")
    slot._queue.append({"id": "q1", "content": "queued while paused"})
    crew_pause.pause("chat-root", ["chat-held"], ["chat-held"])
    assert asyncio.run(_start_next_queued_turn(state, slot)) is False
    assert [e["id"] for e in slot._queue] == ["q1"]


def test_a_paused_crew_forks_no_new_session(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state
    from session_control_helpers import (  # noqa: F401 -- autouse fixtures register by import
        _agent_resolves,
        _enabled_fixture,
        _fresh_create_budget,
        _key,
        _slot,
    )

    from kiro_crew.dashboard import session_control as sc

    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    caller = _slot(state, "chat-w1")
    crew_pause.pause("chat-root", ["chat-w1"], [])
    with pytest.raises(sc.SessionControlError) as refused:
        asyncio.run(sc.fork_session(state, caller_session_key=_key(caller)))
    assert refused.value.code == "crew_paused"


def test_a_pause_landing_during_a_fork_refuses_the_child(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state
    from session_control_helpers import (  # noqa: F401 -- autouse fixtures register by import
        _agent_resolves,
        _enabled_fixture,
        _fresh_create_budget,
        _key,
        _slot,
    )

    from kiro_crew.dashboard import chat_fork
    from kiro_crew.dashboard import session_control as sc

    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    caller = _slot(state, "chat-w1")
    minted: list[str] = []

    async def _fork_slot(*_a: Any, recheck: Any = None, **_k: Any) -> Any:
        # Pause all lands while the fork reads the source transcript.
        crew_pause.pause("chat-root", ["chat-w1"], [])
        if recheck is not None:
            recheck()  # the gate at the mint
        minted.append("child")
        raise AssertionError("a paused crew minted a fork")

    monkeypatch.setattr(chat_fork, "fork_slot", _fork_slot)
    monkeypatch.setattr(sc, "fork_slot", _fork_slot, raising=False)
    with pytest.raises(sc.SessionControlError) as refused:
        asyncio.run(sc.fork_session(state, caller_session_key=_key(caller)))
    assert refused.value.code == "crew_paused" and minted == []


def test_a_paused_crew_opens_no_new_session(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state
    from session_control_helpers import (  # noqa: F401 -- autouse fixtures register by import
        _agent_resolves,
        _enabled_fixture,
        _fresh_create_budget,
        _key,
        _slot,
    )

    from kiro_crew.dashboard import session_control as sc

    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    caller = _slot(state, "chat-root")
    monkeypatch.setattr(crew_mode, "slot_is_crew_mode_root", lambda slot: True)
    crew_pause.pause("chat-root", [], [])
    with pytest.raises(sc.SessionControlError) as refused:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    assert refused.value.code == "crew_paused"


@pytest.mark.asyncio
async def test_a_patrol_wake_on_a_paused_root_is_refused_as_busy(monkeypatch):
    from test_autonudge_dashboard_fire import _fake_spawn, _loop, _orchestrator
    from test_autonudge_dashboard_fire import _slot as nudge_slot

    from kiro_crew.slack import gateway as gw

    orch = _orchestrator()
    orch.dashboard_state.get_slot = MagicMock(return_value=nudge_slot("chat-1-1785"))
    monkeypatch.setattr(crew_mode, "slot_is_crew_mode_root", lambda slot: True)
    crew_pause.pause("chat-1-1785", [], [])
    spawn = _fake_spawn()
    with (
        patch.object(gw, "spawn_guarded_turn", spawn),
        patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
    ):
        assert await orch._fire_dashboard_nudge(_loop()) is False
    assert spawn.calls == []
    orch.autonudge_svc.remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_pause_landing_while_a_patrol_prepares_holds_its_turn(monkeypatch):
    from test_autonudge_dashboard_fire import _fake_spawn, _loop, _orchestrator
    from test_autonudge_dashboard_fire import _slot as nudge_slot

    from kiro_crew.slack import gateway as gw

    orch = _orchestrator()
    orch.dashboard_state.get_slot = MagicMock(return_value=nudge_slot("chat-1-1785"))
    monkeypatch.setattr(crew_mode, "slot_is_crew_mode_root", lambda slot: True)

    async def _admits_then_paused(_loop: Any, _slot: Any) -> bool:
        crew_pause.pause("chat-1-1785", [], [])
        return True

    orch._dashboard_mode_admits = _admits_then_paused
    spawn = _fake_spawn()
    with (
        patch.object(gw, "spawn_guarded_turn", spawn),
        patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
    ):
        assert await orch._fire_dashboard_nudge(_loop()) is False
    assert spawn.calls == []


@pytest.mark.asyncio
async def test_continue_on_a_paused_root_points_at_the_board(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_handlers  # noqa: F401  (binds continue_turn's names)
    from kiro_crew.dashboard.chat_api.continue_turn import api_chat_slot_continue

    state = _make_state(tmp_path)
    state.get_or_create_slot("chat-root")
    monkeypatch.setattr(crew_mode, "slot_is_crew_mode_root", lambda slot: True)
    crew_pause.pause("chat-root", [], [])
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("POST", "/x", app=app, match_info={"slot": "chat-root"})
    resp = await api_chat_slot_continue(req)
    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "crew_paused"


@pytest.mark.asyncio
async def test_a_refused_member_continue_keeps_it_paused(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat_api import continue_turn

    state = _make_state(tmp_path)
    state.get_or_create_slot("chat-w1")
    crew_pause.pause("chat-root", ["chat-w1"], ["chat-w1"])

    async def _still_tearing_down(*_a: Any, **_k: Any) -> None:
        raise chat_handlers.SlotContinueRefusal("still stopping", "slot_running", 409)

    monkeypatch.setattr(continue_turn, "continue_slot_turn", _still_tearing_down)
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("POST", "/x", app=app, match_info={"slot": "chat-w1"})
    resp = await continue_turn.api_chat_slot_continue(req)
    assert resp.status == 409
    # Still owed its hand-back: Resume all continues it later.
    assert crew_pause.record("chat-root")["stopped"] == ["chat-w1"]
    assert crew_pause.holds_turn("chat-w1")


@pytest.mark.asyncio
async def test_a_resume_all_during_a_members_refused_continue_still_hands_it_back(
    tmp_path, routes, monkeypatch
):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_handlers

    monkeypatch.setattr(routes.handler, "_ROOT_LOCKS", {})
    state = _make_state(tmp_path)
    state.get_or_create_slot("chat-w1")
    crew_pause.pause("chat-root", ["chat-w1"], ["chat-w1"])
    resume: list[Any] = []

    async def _continue(_state: Any, slot: Any, **_k: Any) -> str:
        if resume:
            routes.calls["continue"].append(slot.key)
            return "continued"
        # Resume all is pressed while the member's own Continue is in flight, and
        # that Continue is then refused (its stopped turn is still tearing down).
        resume.append(
            asyncio.ensure_future(routes.handler.api_crew_resume(_req(state, "chat-root")))
        )
        await until_parked(routes.handler._root_lock("chat-root"))
        raise chat_handlers.SlotContinueRefusal("still stopping", "slot_running", 409)

    monkeypatch.setattr(chat_handlers, "continue_slot_turn", _continue)
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("POST", "/x", app=app, match_info={"slot": "chat-w1"})
    assert (await chat_handlers.api_chat_slot_continue(req)).status == 409
    # Resume all ran once the refusal had put the member back, so it continued it.
    assert json.loads((await resume[0]).text)["resumed"] == {"chat-w1": "continued"}
    assert routes.calls["continue"] == ["chat-w1"]


@pytest.mark.asyncio
async def test_a_members_continue_that_waited_behind_resume_all_starts_no_second_turn(
    tmp_path, routes, monkeypatch
):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_handlers

    monkeypatch.setattr(routes.handler, "_ROOT_LOCKS", {})
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-w1")
    crew_pause.pause("chat-root", ["chat-w1"], ["chat-w1"])
    lock = routes.handler._root_lock("chat-root")
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("POST", "/x", app=app, match_info={"slot": "chat-w1"})
    async with lock:
        # Resume all holds the lock when the member's own Continue arrives.
        pressed = asyncio.ensure_future(chat_handlers.api_chat_slot_continue(req))
        await until_parked(lock)
        # Resume all hands the member back: its continuation starts a turn.
        crew_pause.resume("chat-root")
        slot._turns_started += 1
    assert (await pressed).status == 200
    assert routes.calls["continue"] == []


@pytest.mark.asyncio
async def test_a_members_continue_after_resume_and_a_new_pause_still_continues_it(
    tmp_path, routes, monkeypatch
):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_handlers

    monkeypatch.setattr(routes.handler, "_ROOT_LOCKS", {})
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-w1")
    crew_pause.pause("chat-root", ["chat-w1"], ["chat-w1"])
    lock = routes.handler._root_lock("chat-root")
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("POST", "/x", app=app, match_info={"slot": "chat-w1"})
    async with lock:
        pressed = asyncio.ensure_future(chat_handlers.api_chat_slot_continue(req))
        await until_parked(lock)
        # Resume all hands the member back, then a newer Pause all stops it again.
        crew_pause.resume("chat-root")
        slot._turns_started += 1
        crew_pause.pause("chat-root", ["chat-w1"], ["chat-w1"])
    assert (await pressed).status == 200
    assert routes.calls["continue"] == ["chat-w1"]
    assert not crew_pause.holds_turn("chat-w1")


# ── Ultra Mode runs on the team lead ───────────────────────────────────────────


def test_ultra_mode_defaults_to_the_team_lead_and_a_set_key_keeps_its_value():
    from kiro_crew.config.loader_builders import _build_agent_config
    from kiro_crew.config.sections_agent import _CREW_MODE_AGENT_DEFAULT

    assert crew_mode.DEFAULT_CREW_MODE_AGENT == "kirocrew-team-lead"
    assert _CREW_MODE_AGENT_DEFAULT == crew_mode.DEFAULT_CREW_MODE_AGENT
    assert _build_agent_config({}).crew_mode_agent == "kirocrew-team-lead"
    assert _build_agent_config({"crew_mode_agent": "kirocrew-conductor"}).crew_mode_agent == (
        "kirocrew-conductor"
    )
    # The deprecated ledger alias still names the shipped conductor, not the default.
    assert crew_mode.CONDUCTOR_ALIASES["kirocrew-ledger-conductor"] == "kirocrew-conductor"


# ── the goal stays current ─────────────────────────────────────────────────────

_GOAL_FIRST = "call `work_ledger_record` `action=goal` FIRST, before you dispatch or reply"


def test_the_lead_prompt_and_both_skills_carry_the_goal_first_rule():
    from pathlib import Path

    from kiro_crew.agent_parts.spec_prompts import _TEAM_LEAD_SYSTEM_PROMPT

    skills = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "builtin_skills"
    texts = {
        "prompt": _TEAM_LEAD_SYSTEM_PROMPT,
        "team-lead": (skills / "team-lead" / "SKILL.md").read_text(encoding="utf-8"),
        "goal-conductor": (skills / "goal-conductor" / "SKILL.md").read_text(encoding="utf-8"),
    }
    for name, text in texts.items():
        flat = " ".join(text.split())
        assert _GOAL_FIRST in flat, name
        assert "`new_goal=true` for a new goal" in flat, name
        assert '"Done when ..." sentence' in flat, name


def test_an_ultra_mode_board_turn_tells_the_lead_to_write_the_goal(monkeypatch):
    from kiro_crew import dashboard_agentic

    monkeypatch.setattr(
        dashboard_agentic, "turn_block", lambda key, **_kw: "[DASHBOARD] goal-board"
    )
    monkeypatch.setattr(dashboard_agentic, "_shows_board", lambda key: True)
    from kiro_crew.dashboard_templates import instance as instance_store

    monkeypatch.setattr(instance_store, "read", lambda key: SimpleNamespace(state="adopted"))
    block = dashboard_agentic.session_turn_block("chat-root")
    assert dashboard_agentic.BOARD_IN_PANEL_LINE in block
    assert "If your ledger has no goal yet, record it now." in block
    assert _GOAL_FIRST in block


@pytest.mark.asyncio
async def test_a_send_to_a_held_idle_session_is_queued_not_started(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app, _make_state

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    started: list[Any] = []
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat._run_chat",
        AsyncMock(side_effect=lambda *a, **k: started.append(1)),
    )
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("held-slot")
    assert not slot.running
    crew_pause.pause("chat-root", ["held-slot"], ["held-slot"])
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat?ws=1", json={"message": "later", "slot": "held-slot"})
        assert resp.status == 200
        data = await resp.json()
    assert data["queued"] is True
    assert [e.get("content") for e in slot._queue] == ["later"]
    assert started == []


def test_a_peer_send_to_a_held_session_is_queued_not_started(tmp_path):
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("held-peer")
    crew_pause.pause("chat-root", ["held-peer"], [])
    run = AsyncMock()
    assert slot.enqueue_or_run_prompt("from a sibling", run, state) is False
    assert [e.get("content") for e in slot._queue] == ["from a sibling"]
    run.assert_not_called()


@pytest.mark.asyncio
async def test_resume_reaches_members_the_live_tree_no_longer_names(routes):
    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    # The root's tab closed and the stamps do not walk to it: the record still
    # names the crew, so every member with a tab is handed back.
    rows = [r for r in _TREE if r["key"] != "root"]
    slots = {r["key"]: _fake_slot(r["key"]) for r in _TREE}
    resp = await routes.handler.api_crew_resume(_req(_state(rows, slots)))
    assert resp.status == 200, resp.text
    assert set(json.loads(resp.text)["resumed"]) == {"w1", "w2", "w3", "w4"}
    assert crew_pause.record("root") is None


@pytest.mark.asyncio
async def test_a_stopped_turn_still_tearing_down_stays_held_for_the_next_resume(
    routes, monkeypatch
):
    monkeypatch.setattr(routes.handler, "_SETTLE_TRIES", 1)
    monkeypatch.setattr(routes.handler, "_SETTLE_STEP_SECS", 0)
    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    slots = {r["key"]: _fake_slot(r["key"]) for r in _TREE}
    slots["w1"].running = True
    resp = await routes.handler.api_crew_resume(_req(_state(_TREE, slots)))
    body = json.loads(resp.text)
    assert body["paused"] is True and body["resumed"]["w1"] == "running"
    rec = crew_pause.record("root")
    assert rec["stopped"] == ["w1"] and crew_pause.holds_turn("w1")
    # The rest of the crew is free, and so is the root's own turn.
    assert not crew_pause.holds_turn("w3") and not crew_pause.holds_turn("root")
    slots["w1"].running = False
    again = await routes.handler.api_crew_resume(_req(_state(_TREE, slots)))
    assert json.loads(again.text)["resumed"] == {"w1": "continued"}
    assert crew_pause.record("root") is None


@pytest.mark.parametrize("key", ["root", "w1"])
@pytest.mark.parametrize("successor_running", [False, True])
@pytest.mark.asyncio
async def test_a_turn_that_starts_once_the_hold_lifts_is_not_continued_or_held_again(
    routes, monkeypatch, key, successor_running
):
    monkeypatch.setattr(routes.handler, "_SETTLE_TRIES", 1)
    monkeypatch.setattr(routes.handler, "_SETTLE_STEP_SECS", 0)
    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    slots = {r["key"]: _fake_slot(r["key"]) for r in _TREE}
    slot = slots[key] = _fake_slot(key, queued=1)
    slot.running = True  # the stopped turn is still tearing down
    real = crew_pause.resume

    def _resume_then_tail_drains(root: str) -> Any:
        rec = real(root)
        # The hold is gone, so the stopped turn's own tail starts the queued
        # message: it either finished inside the wait, or still runs past it.
        slot._turn_generation += 1
        slot._turns_started += 1
        slot._queue, slot.queue_depth, slot.running = [], 0, successor_running
        return rec

    monkeypatch.setattr(crew_pause, "resume", _resume_then_tail_drains)
    body = json.loads((await routes.handler.api_crew_resume(_req(_state(_TREE, slots)))).text)
    assert key not in routes.calls["continue"]
    assert body["paused"] is False and not crew_pause.holds_turn(key)


@pytest.mark.parametrize("later_dispatch", [0, 1])
@pytest.mark.asyncio
async def test_a_dispatch_that_ends_unstarted_once_the_hold_lifts_leaves_the_continue_owed(
    routes, monkeypatch, later_dispatch
):
    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    slots = {r["key"]: _fake_slot(r["key"]) for r in _TREE}
    w1 = slots["w1"]
    real = crew_pause.resume

    def _resume_then_a_dispatch_ends_unstarted(root: str) -> Any:
        rec = real(root)
        # The stopped dispatch itself, or a wake that took the free slot, ends
        # before its turn starts; the queue hand-off that follows takes the slot.
        w1._turn_generation += later_dispatch
        mark_turn_unstarted(w1)
        w1._turn_generation += 1
        return rec

    monkeypatch.setattr(crew_pause, "resume", _resume_then_a_dispatch_ends_unstarted)
    body = json.loads((await routes.handler.api_crew_resume(_req(_state(_TREE, slots)))).text)
    assert body["resumed"]["w1"] == "continued"


@pytest.mark.parametrize("successor_running", [False, True])
@pytest.mark.asyncio
async def test_a_turn_owed_while_resume_lifts_the_hold_is_not_continued_once_it_ran(
    routes, monkeypatch, successor_running
):
    monkeypatch.setattr(routes.handler, "_SETTLE_TRIES", 1)
    monkeypatch.setattr(routes.handler, "_SETTLE_STEP_SECS", 0)
    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    slots = {r["key"]: _fake_slot(r["key"]) for r in _TREE}
    w4 = slots["w4"]
    real = crew_pause.resume
    assert "w4" not in (crew_pause.record("root") or {}).get("stopped", [])

    def _owed_then_resume_then_it_runs(root: str) -> Any:
        # A held turn reaches the pause gate after Resume all read the record:
        # it is now owed, then runs once the hold lifts.
        crew_pause.owe_turn("w4")
        rec = real(root)
        _turn_starts(w4)
        w4.running = successor_running
        return rec

    monkeypatch.setattr(crew_pause, "resume", _owed_then_resume_then_it_runs)
    body = json.loads((await routes.handler.api_crew_resume(_req(_state(_TREE, slots)))).text)
    assert "w4" not in routes.calls["continue"]
    assert body["paused"] is False and not crew_pause.holds_turn("w4")


def _ends_unstarted(slot: Any) -> None:
    # A dispatch ends before its turn starts; its queue hand-off takes the slot.
    slot._turn_generation += 1
    mark_turn_unstarted(slot)
    slot._turn_generation += 1


def _turn_starts(slot: Any) -> None:
    slot._turn_generation += 1
    slot._turns_started += 1


def _unstarted_then_queued_turn(slot: Any, later: int) -> None:
    # One or more dispatches end unstarted, then a hand-off starts the queued turn.
    for _ in range(1 + later):
        _ends_unstarted(slot)
    _turn_starts(slot)


def _turn_then_unstarted_wakes(slot: Any, later: int) -> None:
    # A queued turn runs and ends, then one or more wakes take the free slot
    # and end unstarted.
    _turn_starts(slot)
    for _ in range(1 + later):
        _ends_unstarted(slot)


@pytest.mark.parametrize("shape", [_unstarted_then_queued_turn, _turn_then_unstarted_wakes])
@pytest.mark.parametrize("later_dispatch", [0, 1])
@pytest.mark.parametrize("successor_running", [False, True])
@pytest.mark.asyncio
async def test_a_turn_beside_an_unstarted_dispatch_is_not_continued_or_held_again(
    routes, monkeypatch, shape, later_dispatch, successor_running
):
    monkeypatch.setattr(routes.handler, "_SETTLE_TRIES", 1)
    monkeypatch.setattr(routes.handler, "_SETTLE_STEP_SECS", 0)
    await routes.handler.api_crew_pause(_req(_state(_TREE)))
    slots = {r["key"]: _fake_slot(r["key"]) for r in _TREE}
    w1 = slots["w1"]
    real = crew_pause.resume

    def _resume_then_a_real_turn_runs(root: str) -> Any:
        rec = real(root)
        shape(w1, later_dispatch)
        w1.running = successor_running
        return rec

    monkeypatch.setattr(crew_pause, "resume", _resume_then_a_real_turn_runs)
    body = json.loads((await routes.handler.api_crew_resume(_req(_state(_TREE, slots)))).text)
    assert "w1" not in routes.calls["continue"]
    assert body["paused"] is False and not crew_pause.holds_turn("w1")


def test_a_refused_continue_on_the_last_owed_member_brings_the_record_back():
    # A Resume left only w1 owed (its Continue was refused); the root runs again.
    crew_pause.keep_pending("r", ["w1"], root_pending=False)
    removed = crew_pause.release("w1")
    assert crew_pause.record("r") is None  # its last obligation, so the record ended
    crew_pause.rehold("w1", removed)  # that Continue was refused too
    assert crew_pause.record("r")["stopped"] == ["w1"]
    assert crew_pause.holds_turn("w1") and not crew_pause.holds_turn("r")


def test_a_released_root_blocks_only_held_members_and_the_record_ends():
    # A Resume that left w1 and w2 tearing down: the root runs again.
    crew_pause.keep_pending("r", ["w1", "w2"], root_pending=False)
    assert not crew_pause.root_paused("r") and not crew_pause.holds_turn("r")
    crew_pause.release("w1")
    assert not crew_pause.blocks_create("w1") and crew_pause.blocks_create("w2")
    crew_pause.release("w2")
    assert crew_pause.record("r") is None


def test_a_chat_that_is_not_a_root_is_not_held_as_one():
    """A record left for a chat that is not an Ultra Mode root never holds it as a
    root; its held crew members stay held."""
    crew_pause.pause("r", ["w1"], ["w1"])
    assert crew_pause.holds_turn("r", lambda: True)
    assert not crew_pause.holds_turn("r", lambda: False)
    assert not crew_pause.blocks_create("r", lambda: False)
    assert crew_pause.holds_turn("w1", lambda: False)


@pytest.mark.asyncio
async def test_a_slow_resume_never_overwrites_a_newer_pause(routes, monkeypatch):
    crew_pause.pause("root", ["w1", "w2"], ["w1", "w2"])
    crew_pause.keep_pending("root", ["w9"], root_pending=False)
    rec = crew_pause.record("root")
    assert set(rec["held"]) >= {"w1", "w2", "w9"} and set(rec["stopped"]) >= {"w1", "w2", "w9"}
