"""Host memory pressure on the work ledger: record, read, wake and create refusal.

``docs/request-for-change/rfc-work-ledger-resource-pressure.md`` §5, tests 1 to 7.
The notifier runs on injected probes and clocks; the store, the probe and the
create path run over a real ledger in an own data home.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from conductor_wake_helpers import CONDUCTOR, LOOP_ID, _Loop, _Svc

from kiro_crew import conductor_wake
from kiro_crew import host_pressure as hp
from kiro_crew import resource_status
from kiro_crew import work_ledger as wl
from kiro_crew.notifications.bus import NotificationBus
from kiro_crew.notifications.resource_pressure import (
    CRITICAL_REALERT_SECS,
    SUSTAINED_TIGHT_SECS,
    ResourcePressureNotifier,
)
from kiro_crew.probes.work_ledger import WorkLedgerProbe, person_wait_fingerprint
from kiro_crew.resource_status import (
    POSTURE_AMPLE,
    POSTURE_CRITICAL,
    POSTURE_TIGHT,
    POSTURE_UNKNOWN,
    AdmissionDecision,
    ResourceStatus,
)

# Own data home and an empty wake registry per test (autouse, from the helpers).
from conductor_wake_helpers import _isolated_home  # noqa: E402,F401  isort: skip

#: The real refusal, captured at import: the suite floor pins it off per test.
_REAL_REFUSAL = wl._refuse_if_host_critical


def _status(posture: str, gb: float = 1.5) -> ResourceStatus:
    return ResourceStatus(
        available_gb=gb,
        cpu_count=8,
        load_per_cpu=0.5,
        posture=posture,
        pressure_gb=4.0,
        critical_gb=2.0,
    )


class _Recorder:
    """A notifier whose record writes and wakes land in lists."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.wakes: list[str] = []
        self.notifier = ResourcePressureNotifier(
            NotificationBus(sink=lambda _note: None),
            wall_clock=lambda: 1_000_000.0,
            pressure_sink=lambda **record: self.writes.append(record),
            on_level_change=self.wakes.append,
        )

    def feed(self, posture: str, now: float) -> None:
        self.notifier.observe(_status(posture), now)
        self.notifier.flush_pressure()

    def levels(self) -> list[str]:
        return [write["level"] for write in self.writes]


# ── 1 and 2: the notifier's sink and its wake hook ──────────────────────────


def test_a_tight_blip_writes_nothing_and_wakes_nobody():
    rec = _Recorder()
    rec.feed(POSTURE_TIGHT, 0.0)
    rec.feed(POSTURE_TIGHT, SUSTAINED_TIGHT_SECS - 1)
    rec.feed(POSTURE_AMPLE, SUSTAINED_TIGHT_SECS)
    assert rec.writes == [] and rec.wakes == []


def test_sustained_tight_then_critical_then_recovery_writes_each_level_and_wakes_once_each():
    rec = _Recorder()
    rec.feed(POSTURE_TIGHT, 0.0)
    rec.feed(POSTURE_TIGHT, SUSTAINED_TIGHT_SECS)
    rec.feed(POSTURE_TIGHT, SUSTAINED_TIGHT_SECS + 30)
    rec.feed(POSTURE_CRITICAL, SUSTAINED_TIGHT_SECS + 60)
    rec.feed(POSTURE_AMPLE, SUSTAINED_TIGHT_SECS + 90)
    assert rec.levels() == ["tight", "tight", "critical", "clear"]
    assert rec.wakes == ["tight", "critical", "clear"]
    assert len({write["episode"] for write in rec.writes}) == 1


def test_critical_writes_at_once_and_its_refreshes_and_realert_do_not_wake_again():
    rec = _Recorder()
    rec.feed(POSTURE_CRITICAL, 0.0)
    rec.feed(POSTURE_CRITICAL, 30.0)
    rec.feed(POSTURE_TIGHT, 60.0)  # hysteresis: still the critical episode
    rec.feed(POSTURE_CRITICAL, 60.0 + CRITICAL_REALERT_SECS)
    assert rec.levels() == ["critical"] * 4
    assert rec.wakes == ["critical"]


def test_unknown_changes_nothing():
    rec = _Recorder()
    rec.feed(POSTURE_CRITICAL, 0.0)
    rec.feed(POSTURE_UNKNOWN, 30.0)
    assert rec.levels() == ["critical"]
    rec.feed(POSTURE_UNKNOWN, 60.0)
    rec.feed(POSTURE_AMPLE, 90.0)
    assert rec.levels() == ["critical", "clear"]


def test_a_new_episode_gets_a_new_id_and_wakes_again():
    rec = _Recorder()
    rec.feed(POSTURE_CRITICAL, 0.0)
    rec.feed(POSTURE_AMPLE, 30.0)
    rec.feed(POSTURE_CRITICAL, 60.0)
    assert rec.wakes == ["critical", "clear", "critical"]
    assert rec.writes[0]["episode"] != rec.writes[2]["episode"]


def test_a_failed_write_keeps_the_record_and_its_wake_for_the_next_sample():
    """Persist before publish: a ``clear`` that failed to land is retried, not lost."""
    writes: list[dict[str, Any]] = []
    wakes: list[str] = []
    fail = [True]

    def sink(**record: Any) -> None:
        if fail[0]:
            raise OSError("disk full")
        writes.append(record)

    notifier = ResourcePressureNotifier(
        NotificationBus(sink=lambda _note: None),
        pressure_sink=sink,
        on_level_change=wakes.append,
    )
    notifier.observe(_status(POSTURE_CRITICAL), 0.0)
    fail[0] = False
    notifier.flush_pressure()
    notifier.observe(_status(POSTURE_AMPLE), 30.0)
    fail[0] = True
    notifier.flush_pressure()
    assert [w["level"] for w in writes] == ["critical"] and wakes == ["critical"]
    notifier.observe(_status(POSTURE_AMPLE), 60.0)  # no new record of its own
    fail[0] = False
    notifier.flush_pressure()
    assert [w["level"] for w in writes] == ["critical", "clear"]
    assert wakes == ["critical", "clear"]


@pytest.mark.asyncio
async def test_a_flush_that_times_out_before_its_thread_starts_releases_the_slot(monkeypatch):
    rec = _Recorder()
    rec.notifier.observe(_status(POSTURE_CRITICAL), 0.0)

    async def _timeout_before_start(coro, timeout):  # type: ignore[no-untyped-def]
        coro.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", _timeout_before_start)
    await rec.notifier._flush_bounded()
    assert rec.notifier._flush_inflight is False
    assert rec.notifier._pending_pressure is not None  # left for the next sample
    assert rec.writes == []


@pytest.mark.asyncio
async def test_maybe_sample_writes_the_real_record_off_the_loop():
    notifier = ResourcePressureNotifier(
        NotificationBus(sink=lambda _note: None),
        probe_fn=lambda: _status(POSTURE_CRITICAL),
        clock=lambda: 0.0,
        on_level_change=lambda _level: None,
    )
    await notifier.maybe_sample()
    record = hp.read()
    assert record is not None and record["level"] == "critical"
    assert "1.5" in record["reading"]


# ── 3: the read surface and its expiry ──────────────────────────────────────


def _write(level: str = "critical", *, now: float, episode: str = "ep1") -> None:
    hp.write(episode=episode, level=level, since=now, reading="1.5 GB", load_per_cpu=0.4, now=now)


def test_a_fresh_record_reads_with_its_age():
    _write(now=1000.0)
    assert hp.read(now=1030.0) == {
        "level": "critical",
        "since": hp._iso(1000.0),
        "age_secs": 30,
        "reading": "1.5 GB",
        "load_per_cpu": 0.4,
    }


def test_a_record_nobody_refreshed_for_90_seconds_reads_absent():
    _write(now=1000.0)
    assert hp.read(now=1000.0 + hp.STALE_SECS) is not None
    assert hp.read(now=1000.0 + hp.STALE_SECS + 1) is None


def test_a_clear_record_outlasts_a_busy_conductors_turn():
    """``clear`` is written once, so it must still read on the next patrol tick."""
    _write("clear", now=1000.0)
    assert hp.read(now=1000.0 + hp.STALE_SECS + 300)["level"] == "clear"
    assert hp.read(now=1000.0 + hp.CLEAR_STALE_SECS + 1) is None


def test_a_create_refusal_names_what_lifts_it(monkeypatch):
    _board_with_open_item()
    _verdict(
        monkeypatch, AdmissionDecision(False, POSTURE_CRITICAL, 1.0, "host memory is critical")
    )
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "create", title="one more", acceptance={})
    assert "until memory is no longer critical" in str(caught.value)


def test_the_record_lives_in_the_masked_work_ledger_tree_outside_any_board():
    assert hp.path().parent.parent == wl._work_ledger_root()
    assert hp.path().parent.name in wl.RESERVED_ROOT_DIRS


def test_a_read_drops_a_foreign_episode_and_caps_the_reading():
    now = time.time()
    hp.write(episode="ep1", level="tight", since=now, reading="x" * 5000, load_per_cpu=0.1, now=now)
    assert len(hp.read(now=now)["reading"]) == hp.MAX_READING_CHARS
    hp.write(episode="bad {}", level="tight", since=now, reading="r", load_per_cpu=None, now=now)
    assert hp.read(now=now) is None


def test_a_code_channel_read_carries_the_level_but_not_the_memory_figure():
    """The pause rule turns on the level, so a code channel's conductor needs it
    too; the reading itself stays in the dashboard."""
    from kiro_crew.dashboard.handlers.work_ledger_read import _published_payload

    wl.ensure_conductor(CONDUCTOR, goal="drive the fleet")
    record = wl.read_conductor(CONDUCTOR)
    _write("tight", now=1000.0)
    pressure = hp.read(now=1010.0)
    header = _published_payload(record, [], pressure=pressure)["conductor"]
    assert header["host_pressure"] == {"level": "tight", "since": hp._iso(1000.0), "age_secs": 10}
    assert "host_pressure" not in _published_payload(record, [])["conductor"]


@pytest.mark.parametrize("body", ["", "{not json", json.dumps({"level": "panic"}), "[]"])
def test_a_torn_or_foreign_record_reads_absent_and_never_raises(body):
    hp.path().parent.mkdir(parents=True, exist_ok=True)
    hp.path().write_text(body, encoding="utf-8")
    assert hp.read() is None


def test_a_missing_record_reads_absent():
    assert hp.read() is None


# ── 4: the probe wakes once per episode and level ───────────────────────────


class _Ctx:
    def __init__(self) -> None:
        self.message = json.dumps({"conductor": CONDUCTOR})
        self.job = SimpleNamespace(id="loop-1")


def _pressure_obs(probe: WorkLedgerProbe) -> list[Any]:
    ctx = _Ctx()
    probe.identity(ctx)
    return [obs for obs in probe.observe(ctx).observations if obs.key.startswith("pressure:")]


def _board_with_open_item() -> str:
    wl.ensure_conductor(CONDUCTOR, goal="drive the fleet")
    made = wl.apply_conductor_action(CONDUCTOR, "create", title="t", acceptance={})
    return str(made["item"].item_id)


def test_the_probe_wakes_once_per_level_and_a_reread_is_quiet():
    _board_with_open_item()
    probe = WorkLedgerProbe()
    _write("tight", now=time.time())
    first = _pressure_obs(probe)
    assert [obs.key for obs in first] == ["pressure:ep1:tight"]
    assert "tight" in first[0].brief
    assert _pressure_obs(probe) == []
    _write("critical", now=time.time())
    assert [obs.key for obs in _pressure_obs(probe)] == ["pressure:ep1:critical"]


def test_the_pressure_wake_never_spends_a_real_items_budget():
    from kiro_crew import ledger_wake

    item_id = _board_with_open_item()
    probe = WorkLedgerProbe()
    for level in ("tight", "critical", "clear"):
        _write(level, now=time.time())
        assert _pressure_obs(probe)
    assert ledger_wake.within_rate_limit(CONDUCTOR, item_id)
    assert not ledger_wake.already_woke(CONDUCTOR, item_id, "pressure:ep1:clear", watcher="loop-1")


def test_a_clear_wakes_even_when_the_pressure_budget_is_spent(monkeypatch):
    """``clear`` resumes a paused board and is written once, so the hourly budget
    never swallows it; tight and critical stay budgeted."""
    from kiro_crew import ledger_wake

    _board_with_open_item()
    monkeypatch.setattr(ledger_wake, "within_rate_limit", lambda *_a, **_k: False)
    probe = WorkLedgerProbe()
    _write("critical", now=time.time())
    assert _pressure_obs(probe) == []
    _write("clear", now=time.time())
    assert [obs.key for obs in _pressure_obs(probe)] == ["pressure:ep1:clear"]
    assert _pressure_obs(probe) == [], "still deduped by its token"


def test_a_pressure_push_is_not_capped_per_item():
    svc = SimpleNamespace(
        _pull_forward_counts={}, _pushed_ticks=set(), _firing=set(), _pulled_forward=set()
    )
    for _ in range(conductor_wake.ITEM_PULLS_PER_HOUR + 3):
        assert conductor_wake._admit(svc, LOOP_ID, conductor_wake.PRESSURE_ITEM_ID, 0.0)


def test_a_stale_record_wakes_nobody():
    _board_with_open_item()
    _write("critical", now=1000.0)
    assert _pressure_obs(WorkLedgerProbe()) == []


# ── 5: the push reaches each watched board once ─────────────────────────────


class _ListingSvc(_Svc):
    def list_all(self) -> list[_Loop]:
        return [self._loop] if self._loop is not None else []


@pytest.mark.asyncio
async def test_wake_all_for_pressure_fires_each_watched_board_once():
    svc = _ListingSvc(_Loop())
    assert await conductor_wake.wake_all_for_pressure(svc) == [LOOP_ID]
    assert svc.fired == [LOOP_ID]


@pytest.mark.asyncio
async def test_a_refused_fire_now_is_dropped():
    svc = _ListingSvc(_Loop(), refuse="loop is busy")
    assert await conductor_wake.wake_all_for_pressure(svc) == []


@pytest.mark.asyncio
async def test_no_watched_board_fires_nothing():
    svc = _ListingSvc(_Loop(kind="gh-pr"))
    assert await conductor_wake.wake_all_for_pressure(svc) == []
    assert svc.fired == []


def test_the_thread_entry_without_a_service_is_a_no_op(monkeypatch):
    import kiro_crew.autonudge as autonudge

    monkeypatch.setattr(autonudge, "get_instance", lambda: None)
    assert conductor_wake.wake_conductors_for_pressure() is None


# ── 6: create is refused at critical, admitted otherwise ────────────────────


def _store_bytes() -> dict[str, bytes]:
    root = wl.conductor_dir(CONDUCTOR)
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def _verdict(monkeypatch, decision: AdmissionDecision) -> None:
    """Put the real refusal back (the suite floor pins it off) and feed it *decision*."""
    monkeypatch.setattr(wl, "_refuse_if_host_critical", _REAL_REFUSAL)
    monkeypatch.setattr(resource_status, "admission_check", lambda cfg=None: decision)


def test_a_create_at_critical_is_refused_and_writes_nothing(monkeypatch):
    _board_with_open_item()
    before = _store_bytes()
    _verdict(
        monkeypatch, AdmissionDecision(False, POSTURE_CRITICAL, 1.0, "host memory is critical")
    )
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "create", title="one more", acceptance={})
    assert caught.value.code == wl.CODE_HOST_PRESSURE_CRITICAL
    assert _store_bytes() == before


@pytest.mark.parametrize("posture", [POSTURE_TIGHT, POSTURE_UNKNOWN, POSTURE_AMPLE])
def test_a_create_is_admitted_below_critical(monkeypatch, posture):
    _board_with_open_item()
    _verdict(monkeypatch, AdmissionDecision(True, posture, 3.0))
    made = wl.apply_conductor_action(CONDUCTOR, "create", title="one more", acceptance={})
    assert made["item"].item_id


def test_a_disabled_admission_gate_admits_at_critical(monkeypatch):
    cfg = SimpleNamespace(agent=SimpleNamespace(admission_gate=False))
    monkeypatch.setattr(resource_status, "probe", lambda _cfg=None: _status(POSTURE_CRITICAL))
    assert resource_status.admission_check(cfg).admitted


# ── 7: a level change releases a board waiting only on people, once ─────────


def test_a_pressure_level_change_releases_a_person_wait_hold_once():
    """RFC §5 test 7 as amended by §12: tight -> clear releases once, then holds."""
    item_id = _board_with_open_item()
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key="chat-w")
    wl.apply_worker_report(
        CONDUCTOR, item_id, status="blocked", summary="approval", reason="approval"
    )

    def fingerprint() -> str | None:
        return person_wait_fingerprint(CONDUCTOR, worker_closed=lambda _key: False)

    _write("tight", now=time.time())
    held = fingerprint()
    assert held is not None
    # A refresh at the same episode and level is no news: still held.
    _write("tight", now=time.time())
    assert fingerprint() == held
    # tight -> clear is news: the fingerprint moves, so the hold releases.
    _write("clear", now=time.time())
    released = fingerprint()
    assert released is not None and released != held
    # The released turn records the new fingerprint; a repeat tick holds again.
    _write("clear", now=time.time())
    assert fingerprint() == released


def test_the_entry_point_never_raises_when_scheduling_fails(monkeypatch):
    import kiro_crew.autonudge as autonudge

    monkeypatch.setattr(autonudge, "get_instance", lambda: (_ for _ in ()).throw(RuntimeError))
    assert conductor_wake.wake_conductors_for_pressure() is None
