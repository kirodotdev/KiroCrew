"""Tests for kiro_crew.update_drain — the shared §5 drain-then-swap primitives.

Covers the three primitives this module contributes to the update
architecture RFC's §5 lifecycle:

* ``DrainGate`` — reference-counted background-intake quiesce flag.
* ``drain_in_flight`` — bounded wait for in-flight work, fail-idle on a
  broken counter, interruptible by an external shutdown.
* ``UpdateLease`` + ``verify_after_restart`` — the cross-process
  "one update in flight, ever" lease and the boot-time half of the
  §5 step-9 verification handshake.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import update_drain
from kiro_crew.update_drain import (
    DrainGate,
    UpdateLease,
    drain_in_flight,
    verify_after_restart,
)

# ---------------------------------------------------------------------------
# DrainGate
# ---------------------------------------------------------------------------


def test_drain_gate_basic():
    gate = DrainGate()
    assert not gate.is_draining()
    gate.enter()
    assert gate.is_draining()
    gate.exit()
    assert not gate.is_draining()


def test_drain_gate_nested_holders_compose():
    """The dashboard apply holds the gate while _restart_gateway takes it
    again — the inner exit must not drop the outer hold."""
    gate = DrainGate()
    gate.enter()  # outer: api_update_apply
    gate.enter()  # inner: _restart_gateway
    gate.exit()  # inner done
    assert gate.is_draining(), "outer hold must survive the inner exit"
    gate.exit()
    assert not gate.is_draining()


def test_drain_gate_exit_floors_at_zero():
    gate = DrainGate()
    gate.exit()  # unbalanced exit must not go negative
    gate.enter()
    assert gate.is_draining()


# ---------------------------------------------------------------------------
# drain_in_flight
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_returns_true_immediately_when_idle():
    ev = asyncio.Event()
    assert await drain_in_flight(ev, lambda: 0, drain_timeout=30.0) is True


@pytest.mark.asyncio
async def test_drain_none_counter_is_idle():
    ev = asyncio.Event()
    assert await drain_in_flight(ev, None, drain_timeout=30.0) is True


@pytest.mark.asyncio
async def test_drain_zero_timeout_skips_waiting():
    ev = asyncio.Event()
    assert await drain_in_flight(ev, lambda: 5, drain_timeout=0) is True


@pytest.mark.asyncio
async def test_drain_waits_until_work_finishes():
    ev = asyncio.Event()
    counts = [2, 1, 0]

    def counter() -> int:
        # First call is the pre-loop sample; subsequent calls are polls.
        return counts.pop(0) if counts else 0

    result = await drain_in_flight(ev, counter, drain_timeout=5.0, drain_poll=0.01)
    assert result is True
    assert not counts, "drain returned before the count reached zero"


@pytest.mark.asyncio
async def test_drain_timeout_returns_false():
    ev = asyncio.Event()
    result = await drain_in_flight(ev, lambda: 3, drain_timeout=0.05, drain_poll=0.01)
    assert result is False


@pytest.mark.asyncio
async def test_drain_external_shutdown_wins():
    """SIGTERM (shutdown_event set) mid-drain stops waiting immediately."""
    ev = asyncio.Event()

    async def set_soon() -> None:
        await asyncio.sleep(0.02)
        ev.set()

    setter = asyncio.create_task(set_soon())
    start = time.monotonic()
    result = await drain_in_flight(ev, lambda: 9, drain_timeout=30.0, drain_poll=0.5)
    elapsed = time.monotonic() - start
    await setter
    assert result is False
    assert elapsed < 5.0, "shutdown must interrupt the drain, not wait out the timeout"


@pytest.mark.asyncio
async def test_drain_broken_counter_fails_idle():
    """A raising counter must never wedge the drain (treated as idle)."""
    ev = asyncio.Event()

    def broken() -> int:
        raise RuntimeError("introspection broke")

    assert await drain_in_flight(ev, broken, drain_timeout=30.0) is True


@pytest.mark.asyncio
async def test_drain_counter_breaking_mid_drain_fails_idle():
    ev = asyncio.Event()
    calls = {"n": 0}

    def flaky() -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return 2
        raise RuntimeError("broke mid-drain")

    result = await drain_in_flight(ev, flaky, drain_timeout=5.0, drain_poll=0.01)
    assert result is True


# ---------------------------------------------------------------------------
# UpdateLease
# ---------------------------------------------------------------------------


@pytest.fixture()
def lease_path(tmp_path: Path) -> Path:
    return tmp_path / "run" / "update-lease.json"


def test_lease_acquire_writes_draining_state(lease_path: Path):
    lease = UpdateLease(lease_path)
    assert lease.acquire(from_version="1.0.0", source="test") is None
    data = json.loads(lease_path.read_text())
    assert data["state"] == "draining"
    assert data["from_version"] == "1.0.0"
    assert data["source"] == "test"
    assert data["pid"] == os.getpid()


def test_lease_second_acquire_refused_while_holder_alive(lease_path: Path):
    first = UpdateLease(lease_path)
    assert first.acquire(from_version="1.0.0", source="a") is None
    second = UpdateLease(lease_path)
    refusal = second.acquire(from_version="1.0.0", source="b")
    assert refusal is not None
    assert "already in flight" in refusal
    # The refused instance must not clobber the winner's lease on release().
    second.release()
    assert lease_path.exists()


def test_lease_release_unlinks(lease_path: Path):
    lease = UpdateLease(lease_path)
    lease.acquire(from_version="1.0.0", source="test")
    lease.release()
    assert not lease_path.exists()


def test_lease_dead_holder_still_refused_at_acquire(lease_path: Path, monkeypatch):
    """Acquire NEVER reclaims — even a dead holder's lease refuses; only boot
    verification consumes it (an unlink-then-create window would let two
    contenders both 'reclaim' and both succeed)."""
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text(
        json.dumps(
            {
                "pid": 1234567,
                "acquired_at": time.time() - 7200,
                "state": "draining",
            }
        )
    )
    monkeypatch.setattr(update_drain, "_pid_alive", lambda pid: False)
    lease = UpdateLease(lease_path)
    refusal = lease.acquire(from_version="2.0.0", source="test")
    assert refusal is not None
    assert "already in flight" in refusal
    assert lease_path.exists(), "acquire must not unlink an existing lease"


def test_lease_live_holder_not_reclaimed_even_when_old(lease_path: Path, monkeypatch):
    """Age alone never breaks a lease whose holder is demonstrably alive."""
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "acquired_at": time.time() - 30,
                "state": "draining",
            }
        )
    )
    monkeypatch.setattr(update_drain, "_pid_alive", lambda pid: True)
    lease = UpdateLease(lease_path)
    assert lease.acquire(from_version="2.0.0", source="test") is not None


def test_lease_corrupt_file_refused_at_acquire(lease_path: Path):
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text("{not json")
    lease = UpdateLease(lease_path)
    refusal = lease.acquire(from_version="2.0.0", source="test")
    assert refusal is not None
    assert "corrupt" in refusal


def test_lease_malformed_fields_refused_at_acquire_without_crash(lease_path: Path):
    """Valid JSON with wrong-typed fields (hand-edited or torn lease) must
    degrade to the corrupt-lease refusal, not raise out of acquire — a
    ValueError here surfaces as an HTTP 500 while the lease keeps blocking
    every later update."""
    lease_path.parent.mkdir(parents=True)
    for payload in (
        {"pid": "not-a-pid", "acquired_at": "yesterday", "state": 42},
        {"pid": 123, "acquired_at": "Infinity", "state": "draining"},
        {"pid": None, "acquired_at": float("nan"), "state": "draining"},
    ):
        lease_path.write_text(json.dumps(payload))
        lease = UpdateLease(lease_path)
        refusal = lease.acquire(from_version="2.0.0", source="test")
        assert refusal is not None, payload
        assert "corrupt" in refusal, payload
        assert lease_path.exists(), "acquire must not unlink an existing lease"


def test_lease_read_coerces_numeric_strings(lease_path: Path):
    """Numeric fields serialized as strings still coerce — only genuinely
    unconvertible values are treated as corrupt."""
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text(
        json.dumps({"pid": "1234", "acquired_at": "99.5", "state": "draining"})
    )
    data = UpdateLease(lease_path).read()
    assert data == {"pid": 1234, "acquired_at": 99.5, "state": "draining"}


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX dir mode bits don't restrict creation on Windows"
)
def test_lease_acquire_fails_closed_on_unwritable_dir(tmp_path: Path):
    """An uncreatable lease refuses the update rather than running unleased."""
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir(mode=0o500)
    lease = UpdateLease(ro_dir / "run" / "update-lease.json")
    refusal = lease.acquire(from_version="1.0.0", source="test")
    assert refusal is not None
    assert "cannot create" in refusal


def test_current_head_commit_requires_trusted_git(tmp_path: Path, monkeypatch):
    """The HEAD probe must never resolve git from PATH — a gateway's PATH can
    lead with agent-writable dirs, so a planted shim would run with the
    unsandboxed gateway's credentials. No trusted binary -> no subprocess,
    '' result, handshake falls back to version comparison."""
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
    monkeypatch.setattr(
        update_drain.platform_compat, "trusted_system_bin", lambda name: None
    )
    calls: list = []
    monkeypatch.setattr(
        update_drain.subprocess, "run", lambda *a, **k: calls.append(a)
    )
    assert update_drain.current_head_commit() == ""
    assert not calls, "no subprocess may be spawned without a trusted git"


def test_current_head_commit_spawns_the_trusted_path(tmp_path: Path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
    monkeypatch.setattr(
        update_drain.platform_compat, "trusted_system_bin", lambda name: "/usr/bin/git"
    )
    seen: dict = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="abc123\n")

    monkeypatch.setattr(update_drain.subprocess, "run", fake_run)
    assert update_drain.current_head_commit() == "abc123"
    assert seen["argv"][0] == "/usr/bin/git", "argv[0] must be the pinned path"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_mark_restarting_ignores_planted_predictable_tmp_symlink(
    lease_path: Path, tmp_path: Path
):
    """A symlink pre-planted at the predictable sibling name (update-lease.tmp)
    must never receive the handoff write — temp files use unpredictable
    mkstemp names, so a local attacker cannot aim the write at another file."""
    lease = UpdateLease(lease_path)
    assert lease.acquire(from_version="1.0.0", source="test") is None
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    planted = lease_path.with_suffix(".tmp")
    planted.symlink_to(victim)
    lease.mark_restarting(target="1.1.0")
    assert victim.read_text() == "untouched", "write must not follow a planted symlink"
    assert json.loads(lease_path.read_text())["state"] == "restarting"


def test_lease_mark_restarting(lease_path: Path):
    lease = UpdateLease(lease_path)
    lease.acquire(from_version="1.0.0", source="test")
    lease.mark_restarting(target="1.1.0")
    data = json.loads(lease_path.read_text())
    assert data["state"] == "restarting"
    assert data["target"] == "1.1.0"
    assert data["from_version"] == "1.0.0"


def test_lease_mark_restarting_noop_when_not_held(lease_path: Path):
    lease = UpdateLease(lease_path)
    lease.mark_restarting(target="1.1.0")
    assert not lease_path.exists()


# ---------------------------------------------------------------------------
# verify_after_restart (§5 step 9)
# ---------------------------------------------------------------------------


@pytest.fixture()
def homed_lease(lease_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(update_drain, "_lease_path", lambda: lease_path)
    return lease_path


def test_verify_no_lease_returns_none(homed_lease: Path):
    assert verify_after_restart("1.1.0") is None


def test_verify_success_when_version_changed(homed_lease: Path):
    lease = UpdateLease(homed_lease)
    lease.acquire(from_version="1.0.0", source="test")
    lease.mark_restarting(target="1.1.0")
    outcome = verify_after_restart("1.1.0")
    assert outcome is not None
    assert "verified" in outcome.lower()
    assert "1.1.0" in outcome
    assert not homed_lease.exists(), "verification must consume the lease"


def test_verify_success_without_target_uses_from_version(homed_lease: Path):
    lease = UpdateLease(homed_lease)
    lease.acquire(from_version="1.0.0", source="test")
    lease.mark_restarting()
    outcome = verify_after_restart("1.0.1")
    assert outcome is not None
    assert "verified" in outcome.lower()


def test_verify_failure_when_version_unchanged(homed_lease: Path, caplog):
    lease = UpdateLease(homed_lease)
    lease.acquire(from_version="1.0.0", source="test")
    lease.mark_restarting(target="1.1.0")
    outcome = verify_after_restart("1.0.0")
    assert outcome is not None
    assert "did not take effect" in outcome.lower()
    assert not homed_lease.exists(), "a failed swap must still consume the lease"


def test_verify_interrupted_apply_dead_holder(homed_lease: Path, monkeypatch):
    """A 'draining' lease whose holder is DEAD = interrupted mid-apply."""
    lease = UpdateLease(homed_lease)
    lease.acquire(from_version="1.0.0", source="test")
    monkeypatch.setattr(update_drain, "_pid_alive", lambda pid: False)
    outcome = verify_after_restart("1.0.0")
    assert outcome is not None
    assert "interrupted" in outcome.lower()
    assert not homed_lease.exists()


def test_verify_leaves_other_live_gateways_lease(homed_lease: Path, monkeypatch):
    """A live lease held by ANOTHER process (second gateway on the same data
    home) must never be consumed by this boot's verification."""
    homed_lease.parent.mkdir(parents=True)
    homed_lease.write_text(
        json.dumps({"pid": os.getpid() + 1, "acquired_at": time.time(), "state": "draining"})
    )
    monkeypatch.setattr(update_drain, "_pid_alive", lambda pid: True)
    assert verify_after_restart("1.1.0") is None
    assert homed_lease.exists(), "another process's live lease must survive"


def test_verify_leaves_own_in_flight_draining_lease(homed_lease: Path):
    """Our own pid, still 'draining' = an apply in flight in this process
    (POST /api/update racing the boot check) — not a leftover to consume."""
    lease = UpdateLease(homed_lease)
    lease.acquire(from_version="1.0.0", source="test")
    assert verify_after_restart("1.0.0") is None
    assert homed_lease.exists()


# ---------------------------------------------------------------------------
# Quiesce-gate wiring (cron claims + autonudge fires defer while draining)
# ---------------------------------------------------------------------------


@pytest.fixture()
def held_gate():
    """Hold the global drain gate for the duration of a test, always releasing."""
    update_drain.drain_gate.enter()
    try:
        yield update_drain.drain_gate
    finally:
        update_drain.drain_gate.exit()


@pytest.mark.asyncio
async def test_cron_tick_defers_claims_while_draining(tmp_path, held_gate):
    """_on_timer must not CLAIM due jobs mid-drain; they stay due for the
    relaunched gateway's first tick."""
    from kiro_crew.cron import CronJob, CronSchedule, CronService

    svc = CronService(base_dir=tmp_path)
    job = CronJob(
        id="j1",
        name="due-job",
        message="hi",
        schedule=CronSchedule(kind="every", every_secs=1),
        created_ts=time.time() - 60,
    )
    svc._jobs = [job]

    async def fail_if_run(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("job must not start while draining")

    svc._run_job_isolated = fail_if_run  # type: ignore[method-assign]
    await svc._on_timer()
    assert "j1" not in svc._executing, "drain tick must not claim the job"
    assert not svc._running_tasks


@pytest.mark.asyncio
async def test_cron_tick_defers_cron_expression_jobs_while_draining(tmp_path, held_gate):
    """A minute-matched cron-expression job due DURING the drain is deferred
    like every other schedule kind — no due job of any kind is claimed while
    the drain gate is active, so the swap window stays free of new work. The
    job is claimed after the swap if its matching minute has not passed; a
    swap crossing the minute boundary skips that one invocation by design."""
    from kiro_crew.cron import CronJob, CronSchedule, CronService

    svc = CronService(base_dir=tmp_path)
    interval = CronJob(
        id="iv",
        name="interval",
        message="hi",
        schedule=CronSchedule(kind="every", every_secs=1),
        created_ts=time.time() - 60,
    )
    expr = CronJob(
        id="cx",
        name="expr",
        message="hi",
        schedule=CronSchedule(kind="cron", cron_expr="* * * * *"),
        created_ts=time.time() - 3600,
    )
    svc._tick_scan_locked = lambda: [interval, expr]  # type: ignore[method-assign]

    ran: list[str] = []

    async def record_run(job):  # pragma: no cover - must not be reached
        ran.append(job.id)

    svc._run_job_isolated = record_run  # type: ignore[method-assign]
    await svc._on_timer()
    await asyncio.sleep(0.05)
    assert "cx" not in svc._executing, "cron-expression job must be deferred mid-drain"
    assert "iv" not in svc._executing, "interval job stays deferred"
    assert not svc._running_tasks
    assert ran == []


@pytest.mark.asyncio
async def test_autonudge_timer_defers_fire_while_draining(tmp_path, held_gate, monkeypatch):
    """_timer must re-arm (not fire, not consume a cycle) mid-drain."""
    from kiro_crew.autonudge import AutoNudgeService, NudgeLoop

    svc = AutoNudgeService(base_dir=tmp_path)
    fired: list[str] = []

    async def on_fire(loop: NudgeLoop) -> bool:  # pragma: no cover - must not fire
        fired.append(loop.id)
        return True

    svc._on_fire = on_fire
    loop = NudgeLoop(
        id="l1",
        slot_key="s1",
        message="nudge",
        idle_secs=1,
        active=True,
    )
    svc._loops[loop.id] = loop
    rearmed: list[float | None] = []

    def fake_arm(lp: NudgeLoop, delay: float | None = None) -> None:
        rearmed.append(delay)

    monkeypatch.setattr(svc, "_arm_timer", fake_arm)
    await svc._timer(loop, delay=0)
    assert not fired, "loop must not fire while an update is draining"
    assert rearmed, "loop must be re-armed for after the drain/restart"
    assert loop.cycle_count == 0, "a deferred fire must not consume a cycle"


# ---------------------------------------------------------------------------
# Review-round regressions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_async_twins_roundtrip(lease_path: Path):
    """acquire/mark_restarting/release must be awaitable (event-loop-safe)."""
    lease = UpdateLease(lease_path)
    assert await lease.acquire_async(from_version="1.0.0", source="t") is None
    await lease.mark_restarting_async(target="1.1.0")
    assert json.loads(lease_path.read_text())["state"] == "restarting"
    await lease.release_async()
    assert not lease_path.exists()


def test_cron_executing_count_includes_jitter_sleepers(tmp_path):
    """Jitter sleepers COUNT: every drain path holds the gate, and the jitter
    sleep wakes on the gate's drain event and runs immediately — so a
    claimed job is real in-flight work by the time a drain polls the count.
    (Excluding sleepers silently lost cron-expression invocations killed
    mid-sleep after their scheduled minute passed.)"""
    from kiro_crew.cron import CronService

    svc = CronService(base_dir=tmp_path)
    svc._executing = {"sleeping", "working", "just-claimed"}
    assert svc.executing_count() == 3


def test_drain_gate_event_set_and_cleared():
    """The drain event is set while ANY holder is in and cleared on last exit."""
    gate = update_drain.DrainGate()
    ev = gate.drain_event()
    assert not ev.is_set()
    gate.enter()
    gate.enter()
    assert ev.is_set()
    gate.exit()
    assert ev.is_set(), "nested exit must not clear the event"
    gate.exit()
    assert not ev.is_set()


@pytest.mark.asyncio
async def test_jitter_sleeper_wakes_on_drain(tmp_path, monkeypatch):
    """A cron job in its jitter window runs immediately when a drain begins,
    instead of sleeping out the jitter (and being killed by the exec)."""
    from kiro_crew.cron import CronJob, CronSchedule, CronService

    svc = CronService(base_dir=tmp_path)
    job = CronJob(
        id="j1",
        name="daily",
        message="hi",
        schedule=CronSchedule(kind="every", every_secs=3600),
        created_ts=time.time() - 7200,
    )
    monkeypatch.setattr(svc, "_compute_jitter", lambda j: 30.0)
    ran = asyncio.Event()

    async def on_job(j):
        ran.set()
        return "ok"

    svc._on_job = on_job
    svc._job_run_meta[job.id] = (time.time(), "scheduled")
    svc._executing.add(job.id)
    task = asyncio.create_task(svc._run_job_isolated(job))
    await asyncio.sleep(0.05)
    assert not ran.is_set(), "job must still be in its jitter sleep"
    update_drain.drain_gate.enter()
    try:
        await asyncio.wait_for(ran.wait(), timeout=5.0)
    finally:
        update_drain.drain_gate.exit()
        if not task.done():
            await asyncio.wait_for(task, timeout=10.0)


# ---------------------------------------------------------------------------
# Server review round 1 regressions
# ---------------------------------------------------------------------------


def test_verify_leaves_young_corrupt_lease(homed_lease: Path):
    """An unparsable lease younger than the construction grace may be another
    process between O_EXCL create and json.dump — verification must not
    unlink it (that would let a third contender double-acquire)."""
    homed_lease.parent.mkdir(parents=True)
    homed_lease.write_text("")  # created, not yet written
    assert verify_after_restart("1.1.0") is None
    assert homed_lease.exists()


def test_verify_consumes_stale_corrupt_lease(homed_lease: Path, monkeypatch):
    homed_lease.parent.mkdir(parents=True)
    homed_lease.write_text("{garbage")
    monkeypatch.setattr(
        update_drain, "self_path_mtime", lambda p: time.time() - 3600
    )
    assert verify_after_restart("1.1.0") is None
    assert not homed_lease.exists(), "stale corrupt lease is a crashed writer's leftover"


def test_verify_leaves_lease_reacquired_after_read(homed_lease: Path, monkeypatch):
    """Consume-vs-reacquire race: between verify's read and its consume, a
    peer gateway consumes the same dead lease and a new apply legitimately
    re-acquires the path. The stale verdict must not unlink the LIVE lease —
    that would let a second update start mid-apply."""
    homed_lease.parent.mkdir(parents=True)
    dead = {"pid": 4242, "acquired_at": time.time() - 600, "state": "draining"}
    homed_lease.write_text(json.dumps(dead))
    live = {"pid": 999999, "acquired_at": time.time(), "state": "draining"}

    def swap_then_dead(pid: int) -> bool:
        # Simulate the peer's consume + a fresh acquire landing inside the
        # window between verify's read and its unlink.
        homed_lease.write_text(json.dumps(live))
        return False

    monkeypatch.setattr(update_drain, "_pid_alive", swap_then_dead)
    assert verify_after_restart("1.1.0") is None, "verdict belongs to the peer"
    assert homed_lease.exists(), "must not unlink a re-acquired live lease"
    assert json.loads(homed_lease.read_text())["pid"] == 999999


def test_verify_leaves_fresh_corrupt_lease_swapped_in_after_grace_check(
    homed_lease: Path, monkeypatch
):
    """Corrupt-branch race: the pre-lock read saw a stale unparsable lease,
    but by consume time the path holds a DIFFERENT young unparsable file (a
    fresh contender between O_EXCL create and json.dump). The in-lock grace
    re-check must leave it."""
    homed_lease.parent.mkdir(parents=True)
    homed_lease.write_text("{garbage")
    ages = iter([3600.0, 1.0])
    monkeypatch.setattr(
        update_drain, "self_path_mtime", lambda p: time.time() - next(ages)
    )
    assert verify_after_restart("1.1.0") is None
    assert homed_lease.exists(), "a young mid-write file must survive consumption"


def test_verify_malformed_fields_treated_as_corrupt(homed_lease: Path, monkeypatch):
    """Wrong-typed lease fields at boot verification degrade to the
    corrupt-lease path (grace, then consume) instead of raising — a crash
    here aborts startup verification and strands the lease forever."""
    homed_lease.parent.mkdir(parents=True)
    homed_lease.write_text(
        json.dumps({"pid": ["not", "a", "pid"], "state": "restarting"})
    )
    monkeypatch.setattr(
        update_drain, "self_path_mtime", lambda p: time.time() - 3600
    )
    assert verify_after_restart("1.1.0") is None
    assert not homed_lease.exists(), "stale malformed lease must be consumed"


def test_verify_consumes_bare_restart_handoff_silently(homed_lease: Path):
    """A restart-only handoff (expect_change=False) with an UNCHANGED version
    is the expected outcome — no 'update did not take effect' report."""
    lease = UpdateLease(homed_lease)
    lease.acquire(from_version="1.0.0", source="gateway_restart")
    lease.mark_restarting(expect_change=False)
    assert verify_after_restart("1.0.0") is None
    assert not homed_lease.exists(), "the handoff must still be consumed"


def test_verify_commit_identity_wins_over_version(homed_lease: Path, monkeypatch):
    """On the git engine the handshake keys on the HEAD SHA: an unchanged
    version with a matching commit is SUCCESS (the overwhelming case for a
    successful pull), and a mismatched commit is failure even when the
    version string looks right."""
    lease = UpdateLease(homed_lease)
    lease.acquire(from_version="1.0.0", source="test")
    lease.mark_restarting(target="1.0.0", target_commit="a" * 40)
    monkeypatch.setattr(update_drain, "current_head_commit", lambda: "a" * 40)
    outcome = verify_after_restart("1.0.0")
    assert outcome is not None and "verified" in outcome.lower()

    lease2 = UpdateLease(homed_lease)
    lease2.acquire(from_version="1.0.0", source="test")
    lease2.mark_restarting(target="1.0.0", target_commit="a" * 40)
    monkeypatch.setattr(update_drain, "current_head_commit", lambda: "b" * 40)
    outcome = verify_after_restart("1.0.0")
    assert outcome is not None and "did not take effect" in outcome.lower()


def test_verify_falls_back_to_version_without_commits(homed_lease: Path, monkeypatch):
    """Non-git engines (no resolvable HEAD) keep the version handshake."""
    monkeypatch.setattr(update_drain, "current_head_commit", lambda: "")
    lease = UpdateLease(homed_lease)
    lease.acquire(from_version="1.0.0", source="test")
    lease.mark_restarting(target="1.1.0")
    outcome = verify_after_restart("1.1.0")
    assert outcome is not None and "verified" in outcome.lower()


# ---------------------------------------------------------------------------
# GPT review round 4 regressions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_handoff_write_precedes_close_all(monkeypatch):
    """§5 step 8→9 ordering: the lease handoff write runs BEFORE close_all().

    mark_restarting raises on a failed write (disk full, rename failure), and
    close_all() puts the session manager into its closing state — an abort
    after that point leaves a live gateway that rejects every subsequent
    turn. The write must happen while the gateway is still fully serving."""
    from kiro_crew.dashboard.handlers import updates

    order: list[str] = []

    class FakeSessions:
        async def close_all(self):
            order.append("close_all")

    class FakeState:
        sessions = FakeSessions()

        def push_update_progress(self, *a, **k):
            pass

    class FakeLease:
        async def mark_restarting_async(self, **kw):
            order.append("mark_restarting")

    monkeypatch.setattr(updates.os, "execv", lambda *a: order.append("execv"))
    monkeypatch.setattr("kiro_crew.dashboard.chat.save_all_slots_to_history", lambda s: None)
    await updates._restart_gateway(FakeState(), drain=False, lease=FakeLease())
    assert order == ["mark_restarting", "close_all", "execv"]


@pytest.mark.asyncio
async def test_restart_aborts_before_close_all_on_handoff_failure(monkeypatch):
    """A failed handoff write aborts the restart BEFORE sessions close: the
    gateway keeps serving instead of ending up permanently closing with the
    exec skipped."""
    from kiro_crew.dashboard.handlers import updates

    closed: list[str] = []

    class FakeSessions:
        async def close_all(self):  # pragma: no cover - must not be reached
            closed.append("close_all")

    class FakeState:
        sessions = FakeSessions()

        def push_update_progress(self, *a, **k):
            pass

    class FakeLease:
        async def mark_restarting_async(self, **kw):
            raise OSError("disk full")

    execd: list[str] = []
    monkeypatch.setattr(updates.os, "execv", lambda *a: execd.append("execv"))
    monkeypatch.setattr("kiro_crew.dashboard.chat.save_all_slots_to_history", lambda s: None)
    with pytest.raises(OSError):
        await updates._restart_gateway(FakeState(), drain=False, lease=FakeLease())
    assert not closed, "sessions must survive an aborted handoff"
    assert not execd
