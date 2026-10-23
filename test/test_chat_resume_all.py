"""``POST /api/chat/slots/resume-all`` -- the bulk form of the Continue button.

Pins the candidate set (idle + interrupted; relay archives, running slots and
slots with live sub-agents out), one dispatch of every candidate whose cold
starts queue at ``SessionManager._start_sem``, the per-slot refusal and failure
isolation, a candidate that finished before its dispatch, the background run
(202 at once, one run at a time, one outcome frame however the run ends), the
owner gate (app tokens and non-owner dashboard sessions refused before any
dispatch), and the empty install.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner
from session_helpers import _mock_provider_factory

from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard import chat_handlers as ch
from kiro_crew.dashboard import chat_resume_all as mod
from kiro_crew.dashboard.chat_utils import (
    _MANUAL_CONTINUE_MSG,
    _MANUAL_RESUME_MSG,
    SESSION_START_FAILED_KIND,
    SYNTHETIC_RECOVERY_KIND,
)
from kiro_crew.session import SessionManager
from kiro_crew.session_allocation import MAX_CONCURRENT_COLD_STARTS, new_cold_start_semaphore
from kiro_crew.testing.wait import async_wait_until, await_until, finished

_START_TIMEOUT = "Request initialize timed out after 90s"


@pytest.fixture
def dispatched(monkeypatch):
    """Stub the real turn dispatcher and the audit sink."""
    started = AsyncMock(return_value=True)
    mock_sel = MagicMock()
    monkeypatch.setattr(ch, "_start_next_queued_turn", started)
    monkeypatch.setattr(ch, "sel", lambda: mock_sel)
    return SimpleNamespace(started=started, sel=mock_sel)


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


def _frames(state) -> MagicMock:
    sink = MagicMock()
    state.broadcast_ws_owners = sink
    return sink


async def _drain(state) -> None:
    while state._background_tasks:
        await await_until(
            asyncio.gather(*list(state._background_tasks), return_exceptions=True),
            what="the resume-all background tasks",
        )


async def _press(state) -> dict:
    """One press through ``start_resume_all``, run to its one outcome frame.

    Returns that frame without its ``run_id``, once the id is checked against the
    202's, so a test reads the per-slot record alone.
    """
    frames = _frames(state)
    status, body = mod.start_resume_all(state, {"app": ""})
    assert status == 202, (status, body)
    await _drain(state)
    ((event, outcome),) = [c.args for c in frames.call_args_list]
    assert event == mod.RESUME_ALL_DONE_FRAME
    assert outcome.pop("run_id") == body["run_id"]
    assert outcome["total"] == body["total"]
    return outcome


def _run(state) -> dict:
    return asyncio.run(_press(state))


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


# ── one dispatch, paced by the cold-start queue ──────────────────────────────


def test_a_burst_dispatches_every_candidate_and_its_starts_queue_at_start_sem(
    tmp_path, dispatched, monkeypatch, healthy_host_memory
):
    """Forty candidates, one gather, and the cold-start queue sets the pace.

    Every dispatch is in flight before any returns (the barrier below hangs a
    run that paces itself), and the cold starts they lead to go through a real
    ``SessionManager._start_sem``: four inside a start, the other thirty-six
    waiting for a permit, none refused.
    """
    burst = 40
    width = MAX_CONCURRENT_COLD_STARTS
    state = _make_state(tmp_path)
    for i in range(burst):
        _interrupted(state.get_or_create_slot(f"s{i}"))
    base = _mock_provider_factory()

    async def drive():
        all_in = asyncio.Event()
        release = asyncio.Event()
        entered = 0
        inside = 0
        peak = 0
        started_keys: list[str] = []
        starts: list[asyncio.Future] = []

        def factory(session_key=None, **kwargs):
            provider = base(session_key=session_key, **kwargs)

            async def start(*_args, **_kwargs):
                nonlocal inside, peak
                inside += 1
                peak = max(peak, inside)
                try:
                    await release.wait()
                finally:
                    inside -= 1
                started_keys.append(session_key)

            provider.start = start
            return provider

        mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
        mgr._start_sem = new_cold_start_semaphore(width)

        async def dispatch(_state, slot):
            nonlocal entered
            entered += 1
            if entered == burst:
                all_in.set()
            await all_in.wait()
            # As the queue drain does: the turn runs in its own task, so the
            # dispatch returns before its cold start.
            slot._queue.clear()
            starts.append(asyncio.ensure_future(mgr.get_or_create(f"dashboard:{slot.key}")))
            return True

        monkeypatch.setattr(ch, "_start_next_queued_turn", dispatch)
        try:
            out = await _press(state)
            await async_wait_until(
                lambda: inside == width and mgr._start_sem.queued == burst - width,
                describe=lambda: f"inside={inside} queued={mgr._start_sem.queued}",
            )
            release.set()
            await await_until(asyncio.gather(*starts), what="every cold start")
            return out, peak, started_keys
        finally:
            release.set()
            await finished(mgr.close_all(), "close_all")

    out, peak, started_keys = asyncio.run(drive())

    assert sorted(out["resumed"]) == sorted(f"s{i}" for i in range(burst))
    assert out["skipped"] == [] and out["failed"] == []
    assert peak == width, "never more starts inside the semaphore than its width"
    assert sorted(started_keys) == sorted(f"dashboard:s{i}" for i in range(burst))


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


def test_a_slot_that_fails_to_dispatch_does_not_stop_the_others(tmp_path, dispatched, monkeypatch):
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

    def collect_then_swap(st, **kw):
        picked = real_collect(st, **kw)
        st._slots["swap"] = replacement
        return picked

    monkeypatch.setattr(mod, "collect_interrupted_idle", collect_then_swap)

    out = _run(state)

    assert out["resumed"] == ["keep"]
    assert out["skipped"] == [{"slot": "swap", "code": "slot_not_found"}]
    assert list(stale._queue) == []
    assert list(replacement._queue) == []


# ── a candidate that finished before its dispatch ────────────────────────────


def test_slot_finished_before_its_dispatch_is_not_continued(tmp_path, dispatched, monkeypatch):
    """s4's turn finishes on its own between the collect and its Continue."""
    state = _make_state(tmp_path)
    for i in range(5):
        _interrupted(state.get_or_create_slot(f"s{i}"))
    bodies: dict[str, str] = {}

    async def start(_state, slot):
        bodies[slot.key] = slot._queue[0]["content"]
        slot._queue.clear()
        return True

    monkeypatch.setattr(ch, "_start_next_queued_turn", start)
    real_load = mod.load_window_off_loop

    async def load_then_finish(slot):
        await real_load(slot)
        if slot.key == "s4":
            slot.append("assistant", "finished on its own", "msg msg-a")

    monkeypatch.setattr(mod, "load_window_off_loop", load_then_finish)
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
    monkeypatch.setattr(mod, "collect_interrupted_idle", lambda _s, **_k: stale)
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


def test_only_chat_page_sessions_are_candidates(tmp_path, dispatched):
    """The sidebar lists only the chat page's sessions (``isChatPageSurface``), so
    a member's DM thread is never counted and never resumed by this button."""
    state = _make_state(tmp_path)
    _interrupted(state.get_or_create_slot("chat"))
    legacy = state.get_or_create_slot("autopilot")
    _interrupted(legacy)
    legacy.mode = "orchestrator"
    dm = state.get_or_create_slot("dm")
    _interrupted(dm)
    dm.mode = "member"

    assert [name for name, _ in mod.collect_interrupted_idle(state)] == ["chat", "autopilot"]
    out = _run(state)
    assert out["resumed"] == ["chat", "autopilot"]
    assert list(dm._queue) == []


def test_a_slot_whose_channel_turn_is_in_flight_is_not_a_candidate(
    tmp_path, dispatched, monkeypatch
):
    """A Slack-bound slot whose channel-path turn is still answering ends in a
    user row, but the projection reports it as running, so the sidebar does not
    count it. The collect must not count it either."""
    state = _make_state(tmp_path)
    _interrupted(state.get_or_create_slot("a"))
    bound = state.get_or_create_slot("bound")
    _interrupted(bound)
    monkeypatch.setattr(type(bound), "linked_turn_running", property(lambda s: s.key == "bound"))

    assert [name for name, _ in mod.collect_interrupted_idle(state)] == ["a"]
    out = _run(state)
    assert out["total"] == 1
    assert out["resumed"] == ["a"]
    assert out["skipped"] == []


#: The fixture the sidebar's count is checked against too (see its ``$comment``).
_COUNT_FIXTURE = Path(__file__).parent / "fixtures" / "resume_all_candidates.json"
#: The slots-payload fields the browser's count reads (``interruptedIdleSlots``
#: plus the chat page's ``isChatPageSurface`` filter).
_COUNT_ROW_FIELDS = frozenset(
    {"mode", "surface", "running", "interrupted", "subagents_running", "executor"}
)
_TRANSCRIPTS = {
    "interrupted": _interrupted,
    "start_failed": _start_failed,
    "finished": _finished,
    "empty": lambda _slot: None,
}


def _count_cases() -> list[dict]:
    return json.loads(_COUNT_FIXTURE.read_text(encoding="utf-8"))["cases"]


def _build_from_facts(state, entries: list[dict], monkeypatch) -> None:
    linked: set[str] = set()
    children: set[str] = set()
    building: set[str] = set()
    for entry in entries:
        facts = entry["facts"]
        slot = state.get_or_create_slot(entry["key"])
        _TRANSCRIPTS[facts.get("transcript", "empty")](slot)
        if facts.get("running"):
            _mark_running(slot)
        if facts.get("reserved"):
            slot._turn_admission_reserved = True
        if facts.get("linked_turn_running"):
            linked.add(slot.key)
        if facts.get("subagents_running"):
            children.add(ch.effective_session_key(slot))
        if facts.get("under_construction"):
            building.add(slot.key)
        if "executor" in facts:
            slot.executor = facts["executor"]
        if "mode" in facts:
            slot.mode = facts["mode"]
        if "agent" in facts:
            slot.agent = facts["agent"]
        if "created_by" in facts:
            slot._created_by = facts["created_by"]
    slot_type = type(next(iter(state._slots.values())))
    monkeypatch.setattr(slot_type, "linked_turn_running", property(lambda s: s.key in linked))
    state.subagents = SimpleNamespace(
        running_agents_for=lambda key: ["child"] if key in children else [],
        published_queued_depths=lambda: {},
    )
    state._slots_under_construction = building


@pytest.mark.parametrize("case", _count_cases(), ids=lambda case: case["name"])
def test_the_server_total_is_the_count_the_sidebar_reads_from_the_shared_fixture(
    case, tmp_path, dispatched, monkeypatch
):
    """The fixture's rows are what ``serialize_slots`` sends for its facts, and
    the server picks exactly its ``expected`` keys, as ``total`` too. The vitest
    suite checks the sidebar's count against the same rows and keys."""
    from kiro_crew import crew_mode

    # The Crew Mode lineage from these slots alone: no remembered root, no
    # persisted edge, the shipped conductor list.
    monkeypatch.setattr(crew_mode, "_known_roots", set())
    monkeypatch.setattr(crew_mode, "persisted_parents", dict)
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    state = _make_state(tmp_path)
    _build_from_facts(state, case["slots"], monkeypatch)

    payload = {row["key"]: row for row in state.serialize_slots()}
    for entry in case["slots"]:
        want = entry["row"]
        if want is None:
            assert entry["key"] not in payload, f"{entry['key']} is in the slots payload"
            continue
        assert (
            _COUNT_ROW_FIELDS <= set(want) <= _COUNT_ROW_FIELDS | {"crew_root"}
        ), f"{entry['key']}: name every field the count reads"
        # ``crew_root`` is on a worker's row only; a row the fixture gives none
        # must carry none, or the sidebar would drop a row this case counts.
        got = {field: payload[entry["key"]].get(field) for field in _COUNT_ROW_FIELDS}
        got["crew_root"] = payload[entry["key"]].get("crew_root") or None
        assert got == {"crew_root": None, **want}, entry["key"]

    crew = case.get("crew_mode", False)
    picked = mod.collect_interrupted_idle(state, crew_mode=crew)
    assert [name for name, _ in picked] == case["expected"]

    async def press():
        status, body = mod.start_resume_all(state, {"app": ""}, crew_mode=crew)
        await _drain(state)
        return status, body

    _frames(state)
    status, body = asyncio.run(press())
    assert status == (202 if case["expected"] else 200)
    assert body["total"] == len(case["expected"])


class _PendingWindow:
    """A restored row's lazy window loader: ``read`` is the disk read, the call
    is the replay. Records which thread each read ran on."""

    def __init__(self, rows: list) -> None:
        self.rows = rows
        self.read_threads: list[int] = []

    def read(self, _slot):
        self.read_threads.append(threading.get_ident())
        return (self.rows,)

    def __call__(self, slot, rows) -> None:
        slot._messages.extend(rows)


def _restore_pending(slot) -> _PendingWindow:
    """Turn *slot* into a restored row whose window is not loaded yet: the
    sidebar's bounded tail holds the same rows the window will load."""
    rows = list(slot.messages)
    loader = _PendingWindow(rows)
    slot._messages = []
    slot._lazy_window = loader
    slot._lazy_tail = list(rows)
    return loader


def test_the_collect_reads_a_restored_row_from_its_tail(tmp_path, dispatched):
    """Collecting reads no transcript: a restored row is judged from the tail the
    sidebar reads, and a finished restored row stays pending."""
    state = _make_state(tmp_path)
    cut = state.get_or_create_slot("cut-short")
    _interrupted(cut)
    done = state.get_or_create_slot("finished")
    _finished(done)
    cut_loader = _restore_pending(cut)
    done_loader = _restore_pending(done)

    assert [name for name, _ in mod.collect_interrupted_idle(state)] == ["cut-short"]
    assert cut.window_pending and done.window_pending
    assert cut_loader.read_threads == [] and done_loader.read_threads == []


def test_a_restored_candidate_loads_its_window_off_the_loop(tmp_path, dispatched):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("cut-short")
    _interrupted(slot)
    loader = _restore_pending(slot)

    async def drive():
        return threading.get_ident(), await _press(state)

    loop_thread, out = asyncio.run(drive())

    assert out["resumed"] == ["cut-short"]
    assert not slot.window_pending
    assert len(loader.read_threads) == 1
    assert loader.read_threads[0] != loop_thread, "the transcript was read on the loop"


def test_a_failed_window_read_fails_that_slot_only(tmp_path, dispatched):
    state = _make_state(tmp_path)
    _interrupted(state.get_or_create_slot("a"))
    broken = state.get_or_create_slot("broken")
    _interrupted(broken)
    loader = _restore_pending(broken)

    def unreadable(_slot):
        raise OSError("transcript unreadable")

    loader.read = unreadable  # type: ignore[method-assign]

    out = _run(state)

    assert out["resumed"] == ["a"]
    assert out["failed"] == [{"slot": "broken", "error": "OSError"}]


def test_a_failed_slots_push_fails_only_its_own_slot(tmp_path, dispatched, monkeypatch):
    """``continue_slot_turn`` raises when the slots push after its dispatch fails;
    ``_one`` records that slot as failed and the other slot still resumes."""
    state = _make_state(tmp_path)
    _interrupted(state.get_or_create_slot("a"))
    _interrupted(state.get_or_create_slot("b"))
    monkeypatch.setattr(
        state, "push_slots_update", MagicMock(side_effect=[RuntimeError("ws"), None])
    )

    out = _run(state)

    (failed,) = out["failed"]
    assert failed["error"] == "RuntimeError"
    assert sorted([*out["resumed"], failed["slot"]]) == ["a", "b"]
    assert len(out["resumed"]) == 1
    assert dispatched.started.await_count == 2


# ── the background run ───────────────────────────────────────────────────────


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


def test_a_cancelled_run_sends_one_outcome_frame_and_frees_the_button(
    tmp_path, dispatched, monkeypatch
):
    """Loop teardown is what cancels a run: one frame marked ``cancelled`` still
    goes out, with the slots recorded so far, and the button is free again."""
    state = _make_state(tmp_path)
    frames = _frames(state)
    for i in range(3):
        _interrupted(state.get_or_create_slot(f"s{i}"))
    all_in = asyncio.Event()
    hold = asyncio.Event()
    entered: list[str] = []

    async def start(_state, slot):
        entered.append(slot.key)
        if len(entered) == 3:
            all_in.set()
        await hold.wait()
        return True

    monkeypatch.setattr(ch, "_start_next_queued_turn", start)

    async def drive():
        status, _body = mod.start_resume_all(state, {"app": ""})
        assert status == 202
        run, _run_id, _total = getattr(state, mod._RUN_ATTR)
        await await_until(all_in, what="every dispatch in flight")
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await await_until(run, what="the cancelled run finishing")
        await _drain(state)

    asyncio.run(drive())

    (call,) = frames.call_args_list
    outcome = call.args[1]
    assert outcome["cancelled"] is True
    assert outcome["resumed"] == [] and outcome["failed"] == []
    assert getattr(state, mod._RUN_ATTR) is None
    # Only loop teardown cancels a run, so each slot whose dispatch was in flight
    # keeps its queued recovery entry for the restart to reload.
    assert {k: state._slots[k].queue_depth for k in ("s0", "s1", "s2")} == {
        "s0": 1,
        "s1": 1,
        "s2": 1,
    }


def test_an_exception_outside_the_slot_work_still_sends_the_outcome(
    tmp_path, dispatched, monkeypatch, caplog
):
    """An exception escaping the per-slot work (here raised by the candidate
    loop itself after one slot) frees the button, sends one ``ok: false`` frame
    naming every slot it never recorded, and is logged through
    ``_log_task_exception``."""

    async def resume_one_then_fail(_state, candidates, _claims, outcome):
        outcome["resumed"].append(candidates[0][0])
        raise OSError("boom")

    monkeypatch.setattr(mod, "_resume_candidates", resume_one_then_fail)
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
@pytest.mark.parametrize(
    ("body", "want"),
    [
        ({"crew_mode": True}, ["conductor"]),
        (None, ["conductor", "worker"]),
        ({"crew_mode": "yes"}, ["conductor", "worker"]),
        ("not json", ["conductor", "worker"]),
    ],
)
async def test_the_press_says_whether_crew_mode_left_workers_out(
    body, want, tmp_path, dispatched, monkeypatch
):
    """Only ``{"crew_mode": true}`` leaves a listed root's worker out of the run;
    no body, another value or a body that is no JSON counts it, as the sidebar
    does with the preview off."""
    from kiro_crew import crew_mode

    monkeypatch.setattr(crew_mode, "_known_roots", set())
    monkeypatch.setattr(crew_mode, "persisted_parents", dict)
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    state = _make_state(tmp_path)
    frames = _frames(state)
    root = state.get_or_create_slot("conductor")
    root.agent = "kirocrew-conductor"
    _interrupted(root)
    worker = state.get_or_create_slot("worker")
    worker._created_by = "conductor"
    _interrupted(worker)

    async with TestClient(TestServer(_app(state))) as client:
        if isinstance(body, str):
            resp = await client.post("/api/chat/slots/resume-all", data=body)
        else:
            resp = await client.post("/api/chat/slots/resume-all", json=body)
        data = await resp.json()
        await _drain(state)

    assert resp.status == 202
    assert data["total"] == len(want)
    assert sorted(frames.call_args.args[1]["resumed"]) == sorted(want)


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
