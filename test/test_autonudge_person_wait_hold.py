"""A work-ledger watch holds while every open item waits on a person.

A worker says why it is blocked with ``work_report``'s ``reason``. When every open
item is ``blocked`` / ``question`` with reason ``approval`` or ``needs_human`` and the
ledger is unchanged since the loop's last delivered turn, the loop fires nothing,
takes no floor turn and runs no bound extension. Any change to the ledger releases
it on the next tick.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew import autonudge as _an
from kiro_crew import validation
from kiro_crew import work_ledger as wl
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.autonudge_service import firing
from kiro_crew.crew_log import projection
from kiro_crew.dashboard.handlers import autonudge as autonudge_routes
from kiro_crew.probes import work_ledger as probe

CONDUCTOR = "chat-7-777"


@pytest.fixture(autouse=True)
def _enable(_floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def _unpublish():
    yield
    svc = _an.get_instance()
    if svc is None:
        return
    try:
        for task in list(getattr(svc, "_inflight_adds", ())):
            task.cancel()
        svc.stop()
    finally:
        _an._INSTANCE = None


@pytest.fixture
def svc(tmp_path_factory):
    return AutoNudgeService(base_dir=tmp_path_factory.mktemp("autonudge-person-wait"))


@pytest.fixture
def _nosleep(monkeypatch):
    """The timer's own wait, skipped: each test drives ticks by hand, and the armed
    interval is what a held loop re-arms at, so it is never meant to elapse here."""

    async def _noop(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _noop)  # flake-ok: no wall clock is read


@pytest.fixture
def ledger(monkeypatch):
    """Every loop is a work-ledger watch; ``state["fp"]`` is the person-wait read."""
    state: dict = {"fp": None, "open_asked": 0}

    def _open(_key: str) -> bool:
        state["open_asked"] += 1
        return True

    monkeypatch.setattr(firing, "_ledger_person_wait_fp", lambda _key, *_a: state["fp"])
    monkeypatch.setattr(firing, "_ledger_has_open_items", _open)
    monkeypatch.setattr(AutoNudgeService, "_observes_work_ledger", lambda self, loop: True)
    return state


async def _armed(svc, fired: list, **kwargs) -> NudgeLoop:
    async def on_fire(loop):
        fired.append(loop.cycle_count)
        return True

    await svc.start()
    svc._on_fire = on_fire
    loop = await svc.add(slot_key=CONDUCTOR, message="patrol", idle_secs=300, **kwargs)
    await svc._timers[loop.id]
    return loop


async def _tick(svc, loop) -> None:
    svc._cancel_timer(loop.id)
    await svc._timer(loop)


async def _drain(svc: AutoNudgeService) -> None:
    timers = list(svc._timers.values())
    svc.stop()
    if timers:
        await asyncio.gather(*timers, return_exceptions=True)


# --------------------------------------------------------------------------- #
# The hold on the timer
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_all_person_wait_ledger_holds_the_loop(svc, _nosleep, ledger):
    ledger["fp"] = "fp-a"
    fired: list = []
    loop = await _armed(svc, fired)
    assert len(fired) == 1, "the first tick has no recorded fingerprint, so it fires"
    assert svc._loops[loop.id].ledger_seen_fp == "fp-a"

    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    cycles = svc._loops[loop.id].cycle_count
    await _tick(svc, loop)

    refreshed = svc._loops[loop.id]
    assert len(fired) == 1, "nothing changed since the last turn, so no turn fires"
    assert refreshed.cycle_count == cycles
    assert refreshed.active is True and refreshed.stopped_reason == ""
    assert refreshed.waiting_on_person is True
    assert "updated" in events, "the hold must reach the popover"
    timer = svc._timers.get(loop.id)
    assert timer is not None and not timer.done(), "a held loop keeps checking"
    reading = autonudge_routes._autonudge_loop_reading(refreshed)
    assert reading["waiting_on_person"] is True
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_new_report_releases_the_hold(svc, _nosleep, ledger):
    ledger["fp"] = "fp-a"
    fired: list = []
    loop = await _armed(svc, fired)
    await _tick(svc, loop)
    assert svc._loops[loop.id].waiting_on_person is True

    # A worker re-reports (still blocked on a person): the ledger moved.
    ledger["fp"] = "fp-b"
    await _tick(svc, loop)

    refreshed = svc._loops[loop.id]
    assert len(fired) == 2, "a changed ledger is news, so the next tick fires"
    assert refreshed.waiting_on_person is False
    assert refreshed.ledger_seen_fp == "fp-b"

    await _tick(svc, loop)
    assert len(fired) == 2, "unchanged again after that turn, so it holds again"
    assert svc._loops[loop.id].waiting_on_person is True
    await _drain(svc)


@pytest.mark.asyncio
async def test_release_after_a_long_hold_fires_instead_of_stopping(svc, _nosleep, ledger):
    """Held time goes back to the runtime clock, so the news is a turn, not a stop."""
    ledger["fp"] = "fp-a"
    fired: list = []
    loop = await _armed(svc, fired, max_runtime_secs=3600)
    await _tick(svc, loop)
    held = svc._loops[loop.id]
    assert held.waiting_on_person is True
    # The hold began just after a creation thirty days ago: far past the budget
    # and the backstop, every second of it held.
    created = held.created_ts
    held.created_ts -= 30 * 24 * 3600
    held.waiting_on_person_at = held.created_ts + 1

    ledger["fp"] = "fp-b"
    await _tick(svc, loop)

    refreshed = svc._loops[loop.id]
    assert refreshed.active is True and refreshed.stopped_reason == ""
    assert len(fired) == 2, "the release tick delivers the turn"
    assert refreshed.waiting_on_person_at == 0.0
    assert refreshed.created_ts >= created - 2, "the held time was handed back"
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_hold_the_store_refuses_is_not_announced_and_fires(
    svc, _nosleep, ledger, monkeypatch
):
    ledger["fp"] = "fp-a"
    fired: list = []
    loop = await _armed(svc, fired)
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))

    async def _refuse(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", _refuse)
    await _tick(svc, loop)

    refreshed = svc._loops[loop.id]
    assert refreshed.waiting_on_person is False and refreshed.waiting_on_person_at == 0.0
    assert "updated" not in events, "a hold the store never took is never shown"
    assert len(fired) == 2, "doubt fires"
    await _drain(svc)


@pytest.mark.asyncio
async def test_the_live_loop_shows_the_hold_only_after_the_write(
    svc, _nosleep, ledger, monkeypatch
):
    ledger["fp"] = "fp-a"
    fired: list = []
    loop = await _armed(svc, fired)
    seen_during_write: list[bool] = []
    real = svc._write_monitor_snapshot_locked

    async def _watch(*args):
        seen_during_write.append(svc._loops[loop.id].waiting_on_person)
        return await real(*args)

    monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", _watch)
    await _tick(svc, loop)

    assert seen_during_write and seen_during_write[0] is False, "not shown before it lands"
    assert svc._loops[loop.id].waiting_on_person is True
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_malformed_hold_start_releases_without_crashing(svc, _nosleep, ledger):
    ledger["fp"] = "fp-a"
    fired: list = []
    loop = await _armed(svc, fired)
    await _tick(svc, loop)
    held = svc._loops[loop.id]
    created = held.created_ts
    held.waiting_on_person_at = None  # type: ignore[assignment]  # a hand-edited row

    ledger["fp"] = "fp-b"
    await _tick(svc, loop)

    refreshed = svc._loops[loop.id]
    assert refreshed.waiting_on_person is False
    assert refreshed.created_ts == created, "no credit from a start it cannot read"
    assert len(fired) == 2
    await _drain(svc)


def _pin_clock(monkeypatch, now: float) -> None:
    """Pin both holds' own clock -- the module-local ``time`` bindings in ``firing``
    and ``timers`` -- to *now*, leaving the shared ``time`` module alone."""
    from kiro_crew.autonudge_service import timers

    real = firing.time
    pinned = SimpleNamespace(**{n: getattr(real, n) for n in dir(real) if not n.startswith("__")})
    pinned.time = lambda: now
    monkeypatch.setattr(firing, "time", pinned)
    monkeypatch.setattr(timers, "time", pinned)


@pytest.mark.asyncio
async def test_overlapping_holds_credit_the_overlap_once(svc, _nosleep, ledger, monkeypatch):
    ledger["fp"] = "fp-a"
    fired: list = []
    loop = await _armed(svc, fired)
    await _tick(svc, loop)
    held = svc._loops[loop.id]
    assert held.waiting_on_person is True
    # On a pinned clock: the person-wait hold began at T-300, and an approval stall
    # overlapped its last 100 seconds.
    t = 2_000_000_000.0
    _pin_clock(monkeypatch, t)
    held.waiting_on_person_at = t - 300
    held.approval_stalled = True
    held.approval_stalled_at = t - 100
    created = held.created_ts

    await svc.release_approval_hold(held.slot_key, why="test", arm=False)
    assert svc._loops[loop.id].created_ts - created == 100.0

    ledger["fp"] = "fp-b"
    await _tick(svc, loop)
    assert svc._loops[loop.id].created_ts - created == 300.0, "300s held, credited once"
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_held_loop_runs_no_extension_and_is_not_stopped(svc, _nosleep, ledger):
    ledger["fp"] = "fp-a"
    fired: list = []
    loop = await _armed(svc, fired, max_cycles=200)
    loop.cycle_count = 200
    await _tick(svc, loop)

    refreshed = svc._loops[loop.id]
    assert refreshed.max_cycles == 200, "a held loop is not extended"
    assert ledger["open_asked"] == 0, "the extension's ledger read never ran"
    assert refreshed.active is True and refreshed.stopped_reason == ""
    assert len(fired) == 1
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_mixed_ledger_does_not_hold(svc, _nosleep, ledger):
    ledger["fp"] = None  # one item still progressing: no person-wait fingerprint
    fired: list = []
    loop = await _armed(svc, fired)
    await _tick(svc, loop)
    await _tick(svc, loop)

    assert len(fired) == 3
    assert svc._loops[loop.id].waiting_on_person is False
    assert svc._loops[loop.id].ledger_seen_fp == ""
    await _drain(svc)


@pytest.mark.asyncio
async def test_an_approval_stalled_loop_is_left_to_the_approval_hold(svc, _nosleep, ledger):
    ledger["fp"] = "fp-a"
    fired: list = []
    loop = await _armed(svc, fired)
    loop.approval_stalled = True
    await _tick(svc, loop)

    assert svc._loops[loop.id].waiting_on_person is False
    timer = svc._timers.get(loop.id)
    assert timer is None or timer.done(), "the approval hold arms nothing"
    assert len(fired) == 1
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_turn_that_never_started_is_not_counted_as_seen(svc, _nosleep, ledger):
    ledger["fp"] = "fp-a"
    fired: list = []
    loop = await _armed(svc, fired)
    assert svc._loops[loop.id].ledger_seen_fp == "fp-a"

    svc.notify_cycle_start_failed(CONDUCTOR)
    await _tick(svc, loop)

    assert len(fired) == 2, "the conductor never read the ledger, so the tick retries"
    assert svc._loops[loop.id].waiting_on_person is False
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_manual_fire_is_not_held(svc, _nosleep, ledger):
    ledger["fp"] = "fp-a"
    fired: list = []
    loop = await _armed(svc, fired)
    await _tick(svc, loop)
    assert len(fired) == 1

    _loop, _err, status = await svc.fire_now(loop.id)
    assert status == 200
    await svc._timers[loop.id]
    assert len(fired) == 2, "a person's press must reach the conductor"
    await _drain(svc)


# --------------------------------------------------------------------------- #
# The person-wait read on a real ledger
# --------------------------------------------------------------------------- #


def _item(title: str) -> str:
    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    created = wl.apply_conductor_action(
        CONDUCTOR, "create", title=title, acceptance={"kind": "human_approval"}
    )
    return created["item"].item_id


def test_the_fingerprint_needs_every_open_item_waiting_on_a_person():
    first, second = _item("a"), _item("b")
    wl.apply_worker_report(CONDUCTOR, first, status="blocked", summary="x", reason="approval")
    wl.apply_worker_report(CONDUCTOR, second, status="progress", summary="building")
    assert probe.person_wait_fingerprint(CONDUCTOR) is None, "one item still moves"

    wl.apply_worker_report(CONDUCTOR, second, status="blocked", summary="ci")
    assert probe.person_wait_fingerprint(CONDUCTOR) is None, "no reason: it moves on its own"

    wl.apply_worker_report(
        CONDUCTOR, second, status="question", summary="which?", reason="needs_human"
    )
    held = probe.person_wait_fingerprint(CONDUCTOR)
    assert held
    assert probe.person_wait_fingerprint(CONDUCTOR) == held, "stable while unchanged"

    wl.apply_worker_report(CONDUCTOR, first, status="blocked", summary="y", reason="approval")
    moved = probe.person_wait_fingerprint(CONDUCTOR)
    assert moved and moved != held, "any report changes it"


def test_conductor_writes_leave_the_fingerprint_alone_and_reports_move_it(monkeypatch):
    stamps = iter(f"2026-10-08T07:00:{n:02d}+00:00" for n in range(60))
    monkeypatch.setattr(wl, "_now_iso", lambda: next(stamps))
    item_id = _item("a")
    wl.apply_worker_report(CONDUCTOR, item_id, status="blocked", summary="x", reason="approval")
    first = probe.person_wait_fingerprint(CONDUCTOR)
    assert first
    # A conductor that writes every turn must still be able to stay held.
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="ask the user")
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="ship it again", round_number=2)
    assert probe.person_wait_fingerprint(CONDUCTOR) == first
    # The same blocked report again is news: its report stamp moves.
    wl.apply_worker_report(CONDUCTOR, item_id, status="blocked", summary="x", reason="approval")
    assert probe.person_wait_fingerprint(CONDUCTOR) != first


def test_an_item_whose_worker_is_gone_is_not_a_person_wait():
    item_id = _item("a")
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key="chat-9-999")
    wl.apply_worker_report(CONDUCTOR, item_id, status="blocked", summary="x", reason="approval")
    assert probe.person_wait_fingerprint(CONDUCTOR, worker_closed=lambda _k: False)
    assert (
        probe.person_wait_fingerprint(CONDUCTOR, worker_closed=lambda k: k == "chat-9-999") is None
    )


def test_the_fingerprint_is_none_without_a_ledger_or_open_items():
    assert probe.person_wait_fingerprint(CONDUCTOR) is None
    only = _item("a")
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=only, state="abandoned")
    assert probe.person_wait_fingerprint(CONDUCTOR) is None


# --------------------------------------------------------------------------- #
# The reason field
# --------------------------------------------------------------------------- #


def test_reason_is_stored_and_cleared_by_the_next_report():
    item_id = _item("a")
    wl.apply_worker_report(CONDUCTOR, item_id, status="blocked", summary="x", reason="approval")
    stored = wl.read_work_item(CONDUCTOR, item_id)
    assert stored is not None and stored.reason == "approval"
    assert stored.to_dict()["reason"] == "approval"

    wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="moving again")
    stored = wl.read_work_item(CONDUCTOR, item_id)
    assert stored is not None and stored.reason is None


@pytest.mark.parametrize("status", ["progress", "done"])
def test_reason_is_refused_off_blocked_and_question(status):
    item_id = _item("a")
    with pytest.raises(wl.WorkLedgerError) as exc:
        wl.apply_worker_report(CONDUCTOR, item_id, status=status, summary="x", reason="approval")
    assert exc.value.code == wl.CODE_INVALID_VALUE
    with pytest.raises(validation.ValidationError):
        validation.validate_tool_args(
            {"status": status, "summary": "x", "reason": "approval"},
            validation.WORK_REPORT_SCHEMA,
        )


def test_an_unknown_reason_is_refused():
    item_id = _item("a")
    with pytest.raises(wl.WorkLedgerError):
        wl.apply_worker_report(CONDUCTOR, item_id, status="blocked", summary="x", reason="tired")
    with pytest.raises(validation.ValidationError):
        validation.validate_tool_args(
            {"status": "blocked", "summary": "x", "reason": "tired"},
            validation.WORK_REPORT_SCHEMA,
        )


def test_the_fold_keeps_the_reason_and_a_bare_report_clears_it():
    item = projection._work_new_item("it_abcd1234", 0)
    report = {"actor": "worker", "action": "report", "status": "blocked", "summary": "x"}
    projection._work_apply(item, {**report, "reason": "needs_human"}, 1_000)
    assert item["reason"] == "needs_human"
    projection._work_apply(item, {**report, "status": "progress"}, 2_000)
    assert item["reason"] is None, "every report replaces the reason"
    projection._work_apply(item, {**report, "reason": "bogus"}, 3_000)
    assert item["reason"] is None, "the fold keeps only the closed vocabulary"
