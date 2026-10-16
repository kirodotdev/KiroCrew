"""``POST /api/chat/slots/resume-all`` -- the bulk form of the Continue button.

Pins the candidate set (idle + interrupted; relay archives, running slots and
slots with live sub-agents out), the wave bounds and stagger, the per-slot
refusal and failure isolation, a candidate that finished before its wave, the
background run (202 at once, one run at a time, cancellation of the run only
between waves, one outcome frame however the run ends), the owner gate (app tokens and non-owner dashboard
sessions refused before any dispatch), and the empty install.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard import chat_handlers as ch
from kiro_crew.dashboard import chat_resume_all as mod
from kiro_crew.dashboard.chat_utils import (
    _MANUAL_CONTINUE_MSG,
    _MANUAL_RESUME_MSG,
    SESSION_START_FAILED_KIND,
    SYNTHETIC_RECOVERY_KIND,
)

_START_TIMEOUT = "Request initialize timed out after 90s"


@pytest.fixture
def dispatched(monkeypatch):
    """Stub the real turn dispatcher and the audit sink; make the stagger observable."""
    started = AsyncMock(return_value=True)
    mock_sel = MagicMock()
    pause = AsyncMock()
    monkeypatch.setattr(ch, "_start_next_queued_turn", started)
    monkeypatch.setattr(ch, "sel", lambda: mock_sel)
    monkeypatch.setattr(mod, "_pause", pause)
    return SimpleNamespace(started=started, sel=mock_sel, pause=pause)


def _interrupted(slot) -> None:
    """A user row with no reply: the shape a gateway restart mid-turn leaves."""
    slot.append("user", "refactor the parser", "msg msg-u")


def _start_failed(slot) -> None:
    """A turn that streamed nothing and died on session start."""
    slot.append("user", "do the thing", "msg msg-u")
    slot.append("error", _START_TIMEOUT, "msg msg-err", meta={"kind": SESSION_START_FAILED_KIND})


def _finished(slot) -> None:
    slot.append("user", "refactor the parser", "msg msg-u")
    slot.append("assistant", "Done; see lexer.py", "msg msg-a")


def _mark_running(slot) -> None:
    task = MagicMock()
    task.done.return_value = False
    slot.task = task


def _run(state, request=None) -> dict:
    return asyncio.run(mod.resume_all_interrupted(state, request))


# ── the candidate set ────────────────────────────────────────────────────────


def test_collects_only_idle_interrupted_slots(tmp_path, dispatched):
    state = _make_state(tmp_path)
    _interrupted(state.get_or_create_slot("cut-short"))
    _start_failed(state.get_or_create_slot("start-failed"))
    _finished(state.get_or_create_slot("finished"))
    state.get_or_create_slot("empty")
    busy = state.get_or_create_slot("busy")
    _interrupted(busy)
    _mark_running(busy)
    archive = state.get_or_create_slot("archive")
    _interrupted(archive)
    archive.executor = "remote"

    names = [name for name, _ in mod.collect_interrupted_idle(state)]

    assert names == ["cut-short", "start-failed"]


def test_a_reserved_slot_is_counted_as_the_sidebar_counts_it(tmp_path, dispatched):
    """The projection serializes ``running`` as ``turn_running``, so a slot whose
    admission is reserved but whose turn has not started shows as idle and is
    counted on the button. It is a candidate here too, and the per-slot check
    reports it as skipped, so ``total`` matches the count."""
    state = _make_state(tmp_path)
    reserved = state.get_or_create_slot("reserved")
    _interrupted(reserved)
    reserved._turn_admission_reserved = True

    assert [name for name, _ in mod.collect_interrupted_idle(state)] == ["reserved"]
    out = _run(state)
    assert out["total"] == 1
    assert out["skipped"] == [{"slot": "reserved", "code": "slot_running"}]
    dispatched.started.assert_not_awaited()


def test_resumes_every_candidate_with_the_pick_up_body(tmp_path, dispatched):
    state = _make_state(tmp_path)
    a = state.get_or_create_slot("a")
    b = state.get_or_create_slot("b")
    _interrupted(a)
    _start_failed(b)
    _finished(state.get_or_create_slot("c"))

    out = _run(state)

    assert out == {"ok": True, "total": 2, "resumed": ["a", "b"], "skipped": [], "failed": []}
    for slot in (a, b):
        (entry,) = list(slot._queue)
        assert entry["kind"] == SYNTHETIC_RECOVERY_KIND
        assert entry["content"] == _MANUAL_RESUME_MSG
        assert entry.get("_directive_user_origin"), "the owner's press is a human directive"
    assert dispatched.started.await_count == 2
    vias = {
        c.kwargs["metadata"].get("via") for c in dispatched.sel.log_tool_invocation.call_args_list
    }
    assert vias == {mod.RESUME_ALL_VIA}


def test_an_empty_install_answers_at_once(tmp_path, dispatched):
    state = _make_state(tmp_path)
    _finished(state.get_or_create_slot("done"))

    out = _run(state)

    assert out == {"ok": True, "total": 0, "resumed": [], "skipped": [], "failed": []}
    dispatched.started.assert_not_awaited()
    dispatched.pause.assert_not_awaited()


# ── waves ────────────────────────────────────────────────────────────────────


def test_launches_go_out_in_waves_with_a_pause_between(tmp_path, dispatched, monkeypatch):
    """Nine candidates, waves of four: three waves, two pauses, none before the first."""
    monkeypatch.setattr(mod, "RESUME_ALL_WAVE_SIZE", 4)
    state = _make_state(tmp_path)
    for i in range(9):
        _interrupted(state.get_or_create_slot(f"s{i}"))

    out = _run(state)

    assert out["resumed"] == [f"s{i}" for i in range(9)]
    assert dispatched.pause.await_count == 2
    assert all(c.args == (mod.RESUME_ALL_STAGGER_SECS,) for c in dispatched.pause.call_args_list)


def test_a_wave_dispatches_at_most_the_wave_size_at_once(tmp_path, dispatched, monkeypatch):
    """The dispatcher is held open: the fifth launch waits for the first wave."""
    monkeypatch.setattr(mod, "RESUME_ALL_WAVE_SIZE", 4)
    state = _make_state(tmp_path)
    for i in range(6):
        _interrupted(state.get_or_create_slot(f"s{i}"))
    in_flight = 0
    peak = 0
    release = asyncio.Event()

    async def slow_start(_state, _slot):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await release.wait()
        in_flight -= 1
        return True

    monkeypatch.setattr(ch, "_start_next_queued_turn", slow_start)

    async def drive():
        task = asyncio.ensure_future(mod.resume_all_interrupted(state))
        # Let the first wave reach the dispatcher; the lock and the sub-agent
        # probe inside continue_slot_turn take a few loop turns each.
        for _ in range(500):
            if in_flight == 4:
                break
            await asyncio.sleep(0)
        for _ in range(50):
            await asyncio.sleep(0)
        assert in_flight == 4, "the second wave must wait for the first"
        release.set()
        return await task

    out = asyncio.run(drive())

    assert peak == 4
    assert len(out["resumed"]) == 6


# ── isolation ────────────────────────────────────────────────────────────────


def test_a_refused_slot_is_reported_by_code_and_the_rest_resume(tmp_path, dispatched):
    state = _make_state(tmp_path)
    _interrupted(state.get_or_create_slot("fine"))
    queued = state.get_or_create_slot("queued")
    _interrupted(queued)
    queued.queue_insert(0, "next thing")  # the runner will pick it up on its own

    out = _run(state)

    assert out["resumed"] == ["fine"]
    assert out["skipped"] == [{"slot": "queued", "code": "slot_queue_pending"}]
    assert out["failed"] == []
    assert queued.queue_depth == 1, "no continuation was added behind the queued message"
    dispatched.started.assert_awaited_once()


def test_a_slot_that_fails_to_dispatch_does_not_stop_the_wave(tmp_path, dispatched, monkeypatch):
    state = _make_state(tmp_path)
    _interrupted(state.get_or_create_slot("ok1"))
    _interrupted(state.get_or_create_slot("boom"))
    _interrupted(state.get_or_create_slot("ok2"))

    async def start(_state, slot):
        if slot.key == "boom":
            raise RuntimeError("provider shutdown")
        return True

    monkeypatch.setattr(ch, "_start_next_queued_turn", start)

    out = _run(state)

    assert out["resumed"] == ["ok1", "ok2"]
    assert out["failed"] == [{"slot": "boom", "error": "RuntimeError"}]
    assert out["skipped"] == []


def test_a_slot_replaced_while_queued_is_skipped_not_resumed(tmp_path, dispatched, monkeypatch):
    """The same-name re-registration probe the single-slot route runs, per slot."""
    state = _make_state(tmp_path)
    stale = state.get_or_create_slot("swap")
    _interrupted(stale)
    _interrupted(state.get_or_create_slot("keep"))
    replacement = state.get_or_create_slot("swap-tmp")
    # The candidate list holds ``stale``; before its lock is taken the name is
    # re-registered to a fresh object (a reconnect landing mid-request).
    real_collect = mod.collect_interrupted_idle

    def collect_then_swap(st):
        picked = real_collect(st)
        st._slots["swap"] = replacement
        return picked

    monkeypatch.setattr(mod, "collect_interrupted_idle", collect_then_swap)

    out = _run(state)

    assert out["resumed"] == ["keep"]
    assert out["skipped"] == [{"slot": "swap", "code": "slot_not_found"}]
    assert list(stale._queue) == []
    assert list(replacement._queue) == []


# ── a candidate that finished before its wave ────────────────────────────────


def test_slot_finished_between_waves_is_not_continued(tmp_path, dispatched, monkeypatch):
    """s4 is in wave 2; its turn finishes on its own during wave 1's pause."""
    monkeypatch.setattr(mod, "RESUME_ALL_WAVE_SIZE", 4)
    state = _make_state(tmp_path)
    for i in range(5):
        _interrupted(state.get_or_create_slot(f"s{i}"))
    bodies: dict[str, str] = {}

    async def start(_state, slot):
        bodies[slot.key] = slot._queue[0]["content"]
        slot._queue.clear()
        return True

    monkeypatch.setattr(ch, "_start_next_queued_turn", start)

    async def pause(_secs):
        state._slots["s4"].append("assistant", "finished on its own", "msg msg-a")

    monkeypatch.setattr(mod, "_pause", pause)
    out = _run(state)

    assert "s4" not in out["resumed"], f"s4 got {bodies.get('s4')!r}"
    assert out["skipped"] == [{"slot": "s4", "code": "turn_not_failed"}]
    assert "s4" not in bodies


def test_second_press_after_turns_finished_fast_does_not_carry_on(
    tmp_path, dispatched, monkeypatch
):
    """A press whose snapshot went stale: every candidate finished cleanly first."""
    state = _make_state(tmp_path)
    for i in range(2):
        _interrupted(state.get_or_create_slot(f"s{i}"))

    async def start_and_finish(_state, slot):
        slot._queue.clear()
        slot.append("assistant", "done", "msg msg-a")
        return True

    monkeypatch.setattr(ch, "_start_next_queued_turn", start_and_finish)
    stale = mod.collect_interrupted_idle(state)  # the second press's snapshot
    _run(state)  # the first press runs to completion
    monkeypatch.setattr(mod, "collect_interrupted_idle", lambda _s: stale)
    seen: list[str] = []

    async def record(_state, slot):
        seen.extend(e["content"] for e in slot._queue)
        slot._queue.clear()
        return True

    monkeypatch.setattr(ch, "_start_next_queued_turn", record)
    out = _run(state)

    assert _MANUAL_CONTINUE_MSG not in seen, f"finished sessions got carry-on turns: {out}"
    assert out["resumed"] == []
    assert {s["code"] for s in out["skipped"]} == {"turn_not_failed"}


# ── candidate parity with the sidebar ────────────────────────────────────────


def test_slots_with_live_sub_agents_are_not_candidates(tmp_path, dispatched):
    """The sidebar leaves ``subagents_running`` rows out of its count; so does the server."""
    state = _make_state(tmp_path)
    _interrupted(state.get_or_create_slot("a"))
    _interrupted(state.get_or_create_slot("kids"))
    kids_key = ch.effective_session_key(state._slots["kids"])
    state.subagents = SimpleNamespace(
        running_agents_for=lambda key: ["child"] if key == kids_key else []
    )

    names = [name for name, _ in mod.collect_interrupted_idle(state)]

    assert names == ["a"]


def test_a_slot_under_construction_is_not_a_candidate(tmp_path, dispatched):
    state = _make_state(tmp_path)
    _interrupted(state.get_or_create_slot("a"))
    _interrupted(state.get_or_create_slot("building"))
    state._slots_under_construction = {"building"}

    names = [name for name, _ in mod.collect_interrupted_idle(state)]

    assert names == ["a"]


def test_a_failed_slots_push_after_dispatch_still_counts_as_resumed(
    tmp_path, dispatched, monkeypatch
):
    state = _make_state(tmp_path)
    _interrupted(state.get_or_create_slot("a"))
    _interrupted(state.get_or_create_slot("b"))
    monkeypatch.setattr(state, "push_slots_update", MagicMock(side_effect=RuntimeError("ws")))

    out = _run(state)

    assert out["resumed"] == ["a", "b"]
    assert out["failed"] == []
    assert dispatched.started.await_count == 2


# ── the background run ───────────────────────────────────────────────────────


def _frames(state) -> MagicMock:
    sink = MagicMock()
    state.broadcast_ws_owners = sink
    return sink


async def _drain(state) -> None:
    while state._background_tasks:
        await asyncio.gather(*list(state._background_tasks), return_exceptions=True)


def test_a_run_answers_at_once_and_pushes_its_outcome(tmp_path, dispatched):
    state = _make_state(tmp_path)
    frames = _frames(state)
    _interrupted(state.get_or_create_slot("a"))
    _finished(state.get_or_create_slot("b"))

    async def drive():
        status, body = mod.start_resume_all(state, {"app": ""})
        assert dispatched.started.await_count == 0, "nothing dispatched before the answer"
        await _drain(state)
        return status, body

    status, body = asyncio.run(drive())

    assert status == 202
    assert body == {"ok": True, "total": 1, "run_id": body["run_id"]}
    assert body["run_id"]
    frames.assert_called_once_with(
        mod.RESUME_ALL_DONE_FRAME,
        {
            "ok": True,
            "total": 1,
            "run_id": body["run_id"],
            "resumed": ["a"],
            "skipped": [],
            "failed": [],
        },
    )
    assert getattr(state, mod._RUN_ATTR) is None, "a finished run frees the button"


def test_nothing_to_resume_starts_no_run(tmp_path, dispatched):
    state = _make_state(tmp_path)
    frames = _frames(state)
    _finished(state.get_or_create_slot("done"))

    async def drive():
        return mod.start_resume_all(state, {"app": ""})

    assert asyncio.run(drive()) == (200, {"ok": True, "total": 0, "run_id": ""})
    dispatched.started.assert_not_awaited()
    frames.assert_not_called()


def test_a_second_press_during_a_run_is_refused_and_dispatches_nothing(
    tmp_path, dispatched, monkeypatch
):
    state = _make_state(tmp_path)
    frames = _frames(state)
    for i in range(3):
        _interrupted(state.get_or_create_slot(f"s{i}"))
    hold = asyncio.Event()
    starts: list[str] = []

    async def start(_state, slot):
        starts.append(slot.key)
        await hold.wait()
        slot._queue.clear()
        return True

    monkeypatch.setattr(ch, "_start_next_queued_turn", start)

    async def drive():
        first = mod.start_resume_all(state, {"app": ""})
        for _ in range(500):
            if len(starts) == 3:
                break
            await asyncio.sleep(0)
        second = mod.start_resume_all(state, {"app": ""})
        hold.set()
        await _drain(state)
        frames_after_first = frames.call_count
        starts_after_first = list(starts)
        third = mod.start_resume_all(state, {"app": ""})
        await _drain(state)
        return first, second, third, frames_after_first, starts_after_first

    first, second, third, frames_after_first, starts_after_first = asyncio.run(drive())

    assert first[0] == 202
    assert second == (
        409,
        {
            "error": "a resume-all run is already in progress",
            "code": mod.RESUME_ALL_RUNNING_CODE,
            "run_id": first[1]["run_id"],
            "total": 3,
        },
    )
    assert sorted(starts_after_first) == ["s0", "s1", "s2"], "one run, one start per slot"
    assert frames_after_first == 1
    # The run finished, so the button is free again: a third press is a fresh
    # run (the stub start leaves the transcripts interrupted), not a 409.
    assert third[0] == 202


def test_cancelling_the_run_mid_wave_leaves_no_slot_half_queued(tmp_path, dispatched, monkeypatch):
    """Cancelling the run task stops it BETWEEN waves only.

    This is a cancellation of the run task itself, not gateway shutdown: loop
    teardown cancels every task, the wave included, and the shield does not
    apply there. Wave 2's starts are held open when the run is cancelled. The shielded wave
    still runs to its end, so every slot whose recovery entry was queued gets
    its dispatch; waves 3+ are never touched; the outcome frame says cancelled.
    """
    monkeypatch.setattr(mod, "RESUME_ALL_WAVE_SIZE", 4)
    state = _make_state(tmp_path)
    frames = _frames(state)
    for i in range(12):
        _interrupted(state.get_or_create_slot(f"s{i}"))
    hold = asyncio.Event()
    in_wave2 = 0
    dispatched_keys: list[str] = []

    async def start(_state, slot):
        nonlocal in_wave2
        if int(slot.key[1:]) >= 4:
            in_wave2 += 1
            await hold.wait()
        slot._queue.clear()
        dispatched_keys.append(slot.key)
        return True

    monkeypatch.setattr(ch, "_start_next_queued_turn", start)

    async def drive():
        status, _body = mod.start_resume_all(state, {"app": ""})
        assert status == 202
        run, _run_id, _total = getattr(state, mod._RUN_ATTR)
        for _ in range(2000):
            if in_wave2 == 4:
                break
            await asyncio.sleep(0)
        assert in_wave2 == 4
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        hold.set()
        await _drain(state)

    asyncio.run(drive())

    depth = {k: state._slots[k].queue_depth for k in state._slots}
    assert all(d == 0 for d in depth.values()), f"a slot was left half-queued: {depth}"
    assert sorted(dispatched_keys, key=lambda k: int(k[1:])) == [f"s{i}" for i in range(8)]
    (call,) = frames.call_args_list
    outcome = call.args[1]
    assert outcome["cancelled"] is True
    # Sent once the in-flight wave finished, so it reports that wave too.
    assert sorted(outcome["resumed"], key=lambda k: int(k[1:])) == [f"s{i}" for i in range(8)]
    assert getattr(state, mod._RUN_ATTR) is None

    async def press_again():
        return await ch.continue_slot_turn(state, state._slots["s9"], directive_user_origin=True)

    assert asyncio.run(press_again()) == "resumed", "an untouched slot still takes Resume"


def test_an_exception_outside_the_slot_work_still_sends_the_outcome(
    tmp_path, dispatched, monkeypatch, caplog
):
    """An exception escaping the waves outside ``_one`` (here the inter-wave
    pause) frees the button, sends one ``ok: false`` frame naming every slot it
    never reached, and is logged through ``_log_task_exception``."""
    monkeypatch.setattr(mod, "RESUME_ALL_WAVE_SIZE", 1)
    monkeypatch.setattr(mod, "_pause", AsyncMock(side_effect=OSError("boom")))
    state = _make_state(tmp_path)
    frames = _frames(state)
    for i in range(2):
        _interrupted(state.get_or_create_slot(f"s{i}"))
    holder: dict = {}

    async def drive():
        status, _body = mod.start_resume_all(state, {"app": ""})
        assert status == 202
        holder["task"] = getattr(state, mod._RUN_ATTR)[0]
        await _drain(state)
        return mod.start_resume_all(state, {"app": ""})[0]

    with caplog.at_level("ERROR"):
        assert asyncio.run(drive()) == 202, "the guard is free for the next press"

    assert isinstance(holder["task"].exception(), OSError)
    first = frames.call_args_list[0].args[1]
    assert first["ok"] is False
    assert first["resumed"] == ["s0"]
    assert first["failed"] == [{"slot": "s1", "error": "OSError"}]
    assert "Background task failed" in caplog.text


# ── the route ────────────────────────────────────────────────────────────────


def _app(state) -> web.Application:
    from kiro_crew.dashboard.chat import api_chat_slots_resume_all

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/resume-all", api_chat_slots_resume_all)
    return as_owner(app)


@pytest.mark.asyncio
async def test_the_owner_resumes_over_http(tmp_path, dispatched):
    state = _make_state(tmp_path)
    frames = _frames(state)
    _interrupted(state.get_or_create_slot("a"))
    _finished(state.get_or_create_slot("b"))

    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/slots/resume-all")
        data = await resp.json()
        await _drain(state)

    assert resp.status == 202
    assert data == {"ok": True, "total": 1, "run_id": data["run_id"]}
    dispatched.started.assert_awaited_once()
    assert frames.call_args.args[1]["resumed"] == ["a"]


@pytest.mark.asyncio
async def test_the_answer_does_not_wait_for_any_dispatch(tmp_path, dispatched, monkeypatch):
    """The response arrives while every start is still held open.

    So a client or proxy timeout shorter than the run (a 0.4 s client timeout,
    say) cannot turn a working run into an error row.
    """
    state = _make_state(tmp_path)
    _frames(state)
    for i in range(6):
        _interrupted(state.get_or_create_slot(f"s{i}"))
    hold = asyncio.Event()

    async def start(_state, slot):
        await hold.wait()
        slot._queue.clear()
        return True

    monkeypatch.setattr(ch, "_start_next_queued_turn", start)

    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/slots/resume-all")
        data = await resp.json()
        assert not hold.is_set()
        hold.set()
        await _drain(state)

    assert resp.status == 202
    assert data["total"] == 6


@pytest.mark.asyncio
async def test_a_double_press_over_http_starts_one_run(tmp_path, dispatched, monkeypatch):
    state = _make_state(tmp_path)
    frames = _frames(state)
    for i in range(2):
        _interrupted(state.get_or_create_slot(f"s{i}"))
    hold = asyncio.Event()

    async def start(_state, slot):
        await hold.wait()
        slot._queue.clear()
        return True

    monkeypatch.setattr(ch, "_start_next_queued_turn", start)

    async with TestClient(TestServer(_app(state))) as client:
        first, second = await asyncio.gather(
            client.post("/api/chat/slots/resume-all"),
            client.post("/api/chat/slots/resume-all"),
        )
        statuses = sorted([first.status, second.status])
        refused = first if first.status == 409 else second
        body = await refused.json()
        hold.set()
        await _drain(state)

    assert statuses == [202, 409]
    assert body["code"] == mod.RESUME_ALL_RUNNING_CODE
    assert frames.call_count == 1


@pytest.mark.asyncio
async def test_an_app_token_is_refused_before_any_dispatch(tmp_path, dispatched):
    state = _make_state(tmp_path)
    _interrupted(state.get_or_create_slot("a"))

    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/slots/resume-all", headers={"X-Test-App": "some-app"})
        data = await resp.json()

    assert resp.status == 403
    assert data["code"] == "owner_only"
    dispatched.started.assert_not_awaited()
    assert list(state._slots["a"]._queue) == []


@pytest.mark.asyncio
async def test_a_non_owner_dashboard_session_is_refused(tmp_path, dispatched):
    """A Slack-allowlisted ``!dashboard`` user has an empty app claim and is not the owner."""
    state = _make_state(tmp_path)
    state.owner_id = "U0WNER"
    _interrupted(state.get_or_create_slot("a"))

    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/slots/resume-all", headers={"X-Test-User": "U0THER"})

    assert resp.status == 403
    dispatched.started.assert_not_awaited()


def test_the_route_is_registered_before_every_slot_pattern():
    """``resume-all`` must never be read as a slot name by any per-slot POST."""
    from kiro_crew.dashboard.routes import chat as chat_routes

    app = web.Application()
    chat_routes.register(app)
    posts = [
        r.resource.canonical
        for r in app.router.routes()
        if r.method == "POST" and r.resource is not None
    ]
    literal = posts.index("/api/chat/slots/resume-all")
    slot_patterns = [i for i, p in enumerate(posts) if p.startswith("/api/chat/slots/{slot}")]
    assert slot_patterns, "the per-slot POST routes moved; re-point this pin"
    assert literal < min(slot_patterns)
