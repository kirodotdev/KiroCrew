"""Per-person slices: probe, unit queries, the slice manager and runtime ownership.

Every systemctl call goes through a recording fake runner and every cgroup read
through ``tmp_path``, so these run on any host and touch no real unit.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from kiro_crew import platform_compat, sandbox, sandbox_cgroup
from kiro_crew.config.sections_shared_crew import SharedCrewConfig, effective_limits
from kiro_crew.shared_crew import admission_runtime as ar
from kiro_crew.shared_crew import containment as c
from kiro_crew.shared_crew.admission import Liveness, RuntimeOwnershipRef
from kiro_crew.shared_crew.contracts import CapabilityState, EnforcementLevel

SYSTEMCTL = "/usr/bin/systemctl"
INSTANCE = "kirocrew-agents-test.slice"


class FakeRunner:
    """Answers systemctl argv from a table; records every call."""

    def __init__(self, answers: dict[tuple[str, ...], tuple[int, str]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []
        self.default: tuple[int, str] = (0, "")
        self.raises: BaseException | None = None

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if self.raises is not None:
            raise self.raises
        rc, out = self.answers.get(tuple(cmd[1:]), self.default)
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="boom" if rc else "")


@pytest.fixture
def systemctl(monkeypatch):
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: SYSTEMCTL)
    monkeypatch.setattr(platform_compat, "trusted_system_bin_quiet", lambda name: SYSTEMCTL)
    monkeypatch.setattr(sandbox, "_agents_slice_name", lambda: INSTANCE)


@pytest.fixture
def no_systemctl(monkeypatch):
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: None)
    monkeypatch.setattr(platform_compat, "trusted_system_bin_quiet", lambda name: None)
    monkeypatch.setattr(sandbox, "_agents_slice_name", lambda: INSTANCE)


def _events(root: Path, group: str, populated: str) -> None:
    d = root / group.lstrip("/")
    d.mkdir(parents=True, exist_ok=True)
    (d / "cgroup.events").write_text(f"populated {populated}\nfrozen 0\n", encoding="utf-8")


# -- reports -------------------------------------------------------------------


def test_report_refusal_and_capability_state():
    base = c.ContainmentReport(
        platform="linux", available=True, reason="", cpu=True, memory=True, pids=True
    )
    assert base.refusal_reason is None
    assert base.capability_state() is CapabilityState.DEGRADED  # slices off
    full = replace(base, slices_enabled=True)
    assert full.capability_state() is CapabilityState.AVAILABLE
    assert replace(full, cpu=False).capability_state() is CapabilityState.DEGRADED
    assert replace(base, available=False).capability_state() is CapabilityState.UNAVAILABLE
    hard = replace(base, hard_limits=True)
    assert hard.refusal_reason == c.REASON_UNKNOWN
    assert replace(hard, reason=c.REASON_NO_USER_BUS).refusal_reason == c.REASON_NO_USER_BUS
    assert replace(hard, hard_ready=True).refusal_reason is None
    d = full.to_dict()
    assert d["state"] == "available"
    assert d["controls"] == {
        "cpu": "enforced",
        "memory": "enforced",
        "pids": "enforced",
        "io": "unavailable",
        "disk": "app_accounting",
    }
    assert d["adversarial_escape_closed"] is False


def test_failed_and_admission_off_reports_refuse_in_hard_mode():
    failed = c.failed_report(hard_limits=True, detail="x" * 500)
    assert failed.refusal_reason == c.REASON_UNKNOWN
    assert len(failed.detail) == 200
    assert c.failed_report(hard_limits=False, detail="x").refusal_reason is None
    assert c.admission_off_report().refusal_reason == c.REASON_ADMISSION_OFF


def test_probe_off_linux_reports_admission_counts_only(monkeypatch):
    monkeypatch.setattr(c.sys, "platform", "darwin")
    r = c.probe_containment(hard_limits=True, slices_enabled=True)
    assert (r.available, r.reason, r.refusal_reason) == (False, "not_linux", "not_linux")


@pytest.mark.parametrize(
    "probe, controllers, systemctl_ok, slices, want",
    [
        ((True, ""), {"cpu", "memory", "pids"}, True, True, ("", True, True, True)),
        ((True, ""), {"memory"}, True, True, ("", True, False, False)),
        (
            (True, ""),
            {"cpu", "memory", "pids"},
            False,
            True,
            ("systemctl_untrusted", False, False, False),
        ),
        (
            (True, ""),
            {"cpu", "memory", "pids"},
            True,
            False,
            ("principal_slice_failed", False, False, False),
        ),
        (
            (False, "user bus unreachable: x"),
            set(),
            True,
            True,
            ("user_bus_unreachable", False, False, False),
        ),
    ],
)
def test_probe_on_linux(monkeypatch, probe, controllers, systemctl_ok, slices, want):
    monkeypatch.setattr(c.sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_probe_cgroup_scope", lambda: probe)
    monkeypatch.setattr(c, "_delegated_controllers", lambda: controllers)
    monkeypatch.setattr(
        platform_compat, "trusted_system_bin_quiet", lambda n: SYSTEMCTL if systemctl_ok else None
    )
    r = c.probe_containment(hard_limits=True, slices_enabled=slices)
    reason, available, cpu, hard_ready = want
    assert (r.reason, r.available, r.cpu, r.hard_ready) == (reason, available, cpu, hard_ready)
    assert r.level is (EnforcementLevel.BEST_EFFORT if available else EnforcementLevel.NONE)


def test_delegated_controllers_reads_the_user_slice_file(monkeypatch, tmp_path):
    f = tmp_path / "cgroup.controllers"
    f.write_text("cpu memory pids\n", encoding="utf-8")
    monkeypatch.setattr(sandbox_cgroup, "_user_slice_controllers_path", lambda: str(f))
    assert c._delegated_controllers() == {"cpu", "memory", "pids"}
    monkeypatch.setattr(sandbox_cgroup, "_user_slice_controllers_path", lambda: str(tmp_path / "x"))
    assert c._delegated_controllers() is None


# -- unit queries ----------------------------------------------------------------


def test_unit_active_state(systemctl):
    run = FakeRunner({("--user", "is-active", "a.scope"): (3, "inactive\n")})
    assert c.unit_active_state("a.scope", runner=run) == "inactive"
    assert c.unit_active_state("bad name.scope", runner=run) is None
    assert c.unit_active_state("a.service", runner=run) is None
    run.raises = OSError("x")
    assert c.unit_active_state("a.scope", runner=run) is None


def test_unit_queries_without_systemctl(no_systemctl):
    run = FakeRunner()
    assert c.unit_active_state("a.scope", runner=run) is None
    assert c.slice_populated("a.slice", runner=run) is None
    assert c.populated_scopes("a.slice", runner=run) is None
    assert c.stop_unit("a.scope", runner=run) is False
    assert run.calls == []


def test_slice_populated(systemctl, tmp_path):
    show = ("--user", "show", "-p", "ControlGroup", "--value", "p.slice")
    run = FakeRunner({show: (0, "/u/p.slice\n")})
    assert c.slice_populated("p.scope", runner=run, cgroup_root=str(tmp_path)) is None
    assert c.slice_populated("p.slice", runner=run, cgroup_root=str(tmp_path)) is False
    _events(tmp_path, "/u/p.slice", "1")
    assert c.slice_populated("p.slice", runner=run, cgroup_root=str(tmp_path)) is True
    _events(tmp_path, "/u/p.slice", "0")
    assert c.slice_populated("p.slice", runner=run, cgroup_root=str(tmp_path)) is False
    (tmp_path / "u/p.slice/cgroup.events").write_text("frozen 0\n", encoding="utf-8")
    assert c.slice_populated("p.slice", runner=run, cgroup_root=str(tmp_path)) is None
    run.answers[show] = (0, "")
    assert c.slice_populated("p.slice", runner=run, cgroup_root=str(tmp_path)) is False
    run.answers[show] = (1, "")
    assert c.slice_populated("p.slice", runner=run, cgroup_root=str(tmp_path)) is None
    run.raises = subprocess.TimeoutExpired("x", 1)
    assert c.slice_populated("p.slice", runner=run, cgroup_root=str(tmp_path)) is None


def test_populated_scopes(systemctl, tmp_path):
    show = ("--user", "show", "-p", "ControlGroup", "--value", "p.slice")
    run = FakeRunner({show: (0, "/u/p.slice")})
    assert c.populated_scopes("p.slice", runner=run, cgroup_root=str(tmp_path)) == []
    _events(tmp_path, "/u/p.slice/kirocrew-rt-a.scope", "1")
    _events(tmp_path, "/u/p.slice/kirocrew-rt-b.scope", "0")
    _events(tmp_path, "/u/p.slice/other.scope", "1")
    (tmp_path / "u/p.slice/kirocrew-rt-c.scope").mkdir()  # no events file yet
    got = c.populated_scopes("p.slice", runner=run, cgroup_root=str(tmp_path))
    assert got == ["kirocrew-rt-a.scope"]
    assert c.populated_scopes("p.scope", runner=run, cgroup_root=str(tmp_path)) is None
    run.answers[show] = (0, "")
    assert c.populated_scopes("p.slice", runner=run, cgroup_root=str(tmp_path)) == []
    run.answers[show] = (1, "")
    assert c.populated_scopes("p.slice", runner=run, cgroup_root=str(tmp_path)) is None
    run.raises = OSError("x")
    assert c.populated_scopes("p.slice", runner=run, cgroup_root=str(tmp_path)) is None


def test_stop_unit(systemctl):
    run = FakeRunner({("--user", "stop", "a.scope"): (0, "")})
    assert c.stop_unit("a.scope", runner=run) is True
    assert c.stop_unit("$(x).scope", runner=run) is False
    run.default = (5, "")
    assert c.stop_unit("b.scope", runner=run) is False
    run.raises = OSError("x")
    assert c.stop_unit("a.scope", runner=run) is False


# -- the slice manager -----------------------------------------------------------


def test_slice_manager_apply_is_remembered_once(systemctl):
    run = FakeRunner()
    m = c.SliceManager(runner=run)
    lim = c.SliceLimits(cpu_quota_percent=50, memory_max_mb=100, tasks_max=10)
    assert m.apply("p.slice", lim, cpu_delegated=True) is True
    assert run.calls == [
        [SYSTEMCTL, "--user", "set-property", "--runtime", "p.slice",
         "CPUQuota=50%", "MemoryMax=100M", "MemorySwapMax=0", "TasksMax=10"],
        [SYSTEMCTL, "--user", "start", "p.slice"],
    ]  # fmt: skip
    assert m.apply("p.slice", lim, cpu_delegated=True) is True
    assert len(run.calls) == 2
    assert m.is_ready("p.slice") and m.applied() == ("p.slice",)
    m.forget("p.slice")
    assert not m.is_ready("p.slice")
    assert m.apply("q.slice", c.SliceLimits(), cpu_delegated=False) is True
    assert run.calls[-1] == [SYSTEMCTL, "--user", "start", "q.slice"]
    assert m.apply("q.scope", lim, cpu_delegated=True) is False


def test_slice_manager_apply_failures(no_systemctl):
    m = c.SliceManager(runner=FakeRunner())
    assert m.apply("p.slice", c.SliceLimits(), cpu_delegated=False) is False
    run = FakeRunner()
    run.default = (1, "")
    m = c.SliceManager(runner=run, systemctl=SYSTEMCTL)
    assert m.apply("p.slice", c.SliceLimits(), cpu_delegated=False) is False
    run.raises = OSError("x")
    assert m.apply("p.slice", c.SliceLimits(), cpu_delegated=False) is False
    assert not m.is_ready("p.slice")


def test_slice_manager_populated_uses_its_runner(systemctl):
    run = FakeRunner()
    run.default = (1, "")
    m = c.SliceManager(runner=run)
    assert m.populated("p.slice") is None
    assert run.calls[0][:3] == [SYSTEMCTL, "--user", "show"]


def test_slice_manager_release(systemctl):
    run = FakeRunner()
    m = c.SliceManager(runner=run)
    assert m.release("p.slice", busy=lambda n: []) is False  # not owned
    assert m.release("p.scope", busy=lambda n: [], owned_only=False) is False
    m.apply("p.slice", c.SliceLimits(), cpu_delegated=False)
    assert m.release("p.slice", busy=lambda n: ["kirocrew-rt-a.scope"]) is False
    assert m.is_ready("p.slice")  # a busy slice is kept and stays sized
    assert m.release("p.slice", busy=lambda n: None) is False
    assert m.is_ready("p.slice")

    def boom(n):
        raise RuntimeError("x")

    assert m.release("p.slice", busy=boom) is False
    assert m.is_ready("p.slice")
    assert m.release("p.slice", busy=lambda n: []) is True
    assert not m.is_ready("p.slice")
    assert run.calls[-2:] == [
        [SYSTEMCTL, "--user", "stop", "p.slice"],
        [SYSTEMCTL, "--user", "revert", "p.slice"],
    ]
    run.raises = OSError("x")
    assert m.release("old.slice", busy=lambda n: [], owned_only=False) is False


def test_slice_manager_release_without_systemctl(no_systemctl):
    m = c.SliceManager(runner=FakeRunner(), systemctl="")
    assert m.release("old.slice", busy=lambda n: [], owned_only=False) is False
    m._applied["p.slice"] = ()
    assert m.release("p.slice", busy=lambda n: []) is False
    assert m.is_ready("p.slice")  # nothing was stopped, so it stays sized


def test_release_never_holds_the_map_lock_across_systemctl(systemctl):
    seen: list[bool] = []

    class Probe(FakeRunner):
        def __call__(self, cmd):
            seen.append(m._lock.acquire(blocking=False))
            m._lock.release() if seen[-1] else None
            return super().__call__(cmd)

    def busy(name):
        seen.append(m._lock.acquire(blocking=False))
        m._lock.release() if seen[-1] else None
        return []

    m = c.SliceManager(runner=Probe())
    m.apply("p.slice", c.SliceLimits(), cpu_delegated=False)
    assert m.release("p.slice", busy=busy) is True
    assert seen and all(seen)  # is_ready on the event loop never waits on a systemctl call


def test_slice_manager_principal_slices(systemctl):
    stem = INSTANCE[: -len(".slice")]
    listing = ("--user", "list-units", "--all", "--plain", "--no-legend", f"{stem}-p*.slice")
    run = FakeRunner(
        {listing: (0, f"{stem}-pabc.slice loaded active active x\n\nother.slice loaded\n")}
    )
    m = c.SliceManager(runner=run)
    assert m.principal_slices() == [f"{stem}-pabc.slice"]
    run.answers[listing] = (1, "")
    assert m.principal_slices() is None
    run.raises = OSError("x")
    assert m.principal_slices() is None


def test_principal_slices_without_systemctl(no_systemctl):
    assert c.SliceManager(runner=FakeRunner()).principal_slices() is None


# -- preparing slices for admitted work ------------------------------------------


def _limits(**kw):
    cfg = SharedCrewConfig(admission_enabled=True)
    return replace(effective_limits(cfg, auto_crew_concurrency=2), **kw)


def _ready_report(**kw):
    return replace(
        c.ContainmentReport(
            platform="linux", available=True, reason="", cpu=True, memory=True, pids=True
        ),
        **kw,
    )


def test_prepare_slices_applies_crew_then_principal(systemctl):
    run = FakeRunner()
    m = c.SliceManager(runner=run)
    lim = _limits(crew_cpu_quota_percent=200)
    name = asyncio.run(c.prepare_slices(m, "alice", lim, _ready_report()))
    assert name == c.principal_slice_name("alice")
    assert m.is_ready(INSTANCE) and m.is_ready(name)


def test_prepare_slices_skips_when_off_or_unavailable(systemctl):
    m = c.SliceManager(runner=FakeRunner())
    assert asyncio.run(c.prepare_slices(None, "a", _limits(), _ready_report())) == ""
    assert asyncio.run(c.prepare_slices(m, "a", _limits(enabled=False), _ready_report())) == ""
    off = _limits(containment_enabled=False)
    assert asyncio.run(c.prepare_slices(m, "a", off, _ready_report())) == ""
    gone = _ready_report(available=False)
    assert asyncio.run(c.prepare_slices(m, "a", _limits(), gone)) == ""
    assert m.applied() == ()


def test_prepare_slices_hard_mode_needs_the_crew_slice(systemctl):
    run = FakeRunner({("--user", "start", INSTANCE): (1, "")})
    m = c.SliceManager(runner=run)
    hard = _limits(crew_cpu_quota_percent=200, hard_limits=True)
    assert asyncio.run(c.prepare_slices(m, "a", hard, _ready_report())) == ""
    soft = _limits(crew_cpu_quota_percent=200, hard_limits=False)
    assert asyncio.run(c.prepare_slices(m, "a", soft, _ready_report())) != ""


# -- scope context ---------------------------------------------------------------


def test_publish_report_drives_the_backstop():
    try:
        c.publish_report(c.admission_off_report())
        assert c.hard_mode_refusal() == c.REASON_ADMISSION_OFF
    finally:
        c.publish_report(None)
    assert c.hard_mode_refusal() is None
    err = c.ContainmentUnavailable("not_linux")
    assert err.reason == "not_linux" and err.code == "containment_unavailable"


def test_create_task_in_scope_carries_the_principal(systemctl):
    m = c.SliceManager(runner=FakeRunner())
    m.apply(c.principal_slice_name("bob"), c.SliceLimits(), cpu_delegated=False)
    c.install_slice_manager(m)
    try:

        async def main():
            async def probe():
                return c.scope_parent_slice()

            inside = await c.create_task_in_scope(probe(), "bob", name="t")
            outside = await asyncio.create_task(probe())
            return inside, outside

        inside, outside = asyncio.run(main())
    finally:
        c.install_slice_manager(None)
    assert inside == c.principal_slice_name("bob")
    assert outside is None


def test_scope_parent_slice_survives_a_bad_name(monkeypatch):
    m = c.SliceManager(runner=FakeRunner())
    c.install_slice_manager(m)

    def bad(_p):
        raise RuntimeError("x")

    monkeypatch.setattr(c, "principal_slice_name", bad)
    try:
        with c.principal_scope("bob"):
            assert c.scope_parent_slice() is None
    finally:
        c.install_slice_manager(None)


# -- runtime ownership (admission_runtime) ---------------------------------------


def _ref(unit: str) -> RuntimeOwnershipRef:
    return RuntimeOwnershipRef(execution_id="e1", containment_unit=unit)


@pytest.mark.parametrize(
    "state, want",
    [
        ("active", Liveness.ALIVE),
        ("deactivating", Liveness.ALIVE),
        ("inactive", Liveness.STOPPED),
        ("failed", Liveness.STOPPED),
        ("", Liveness.STOPPED),
        ("maintenance", Liveness.UNKNOWN),
        (None, Liveness.UNKNOWN),
    ],
)
def test_scope_liveness(monkeypatch, state, want):
    monkeypatch.setattr(c, "unit_active_state", lambda unit: state)
    assert ar.systemd_unit_liveness(_ref("kirocrew-rt-a.scope")) is want


@pytest.mark.parametrize(
    "scopes, want",
    [(None, Liveness.UNKNOWN), ([], Liveness.STOPPED), (["s.scope"], Liveness.ALIVE)],
)
def test_slice_liveness(monkeypatch, scopes, want):
    monkeypatch.setattr(c, "populated_scopes", lambda unit: scopes)
    assert ar.systemd_unit_liveness(_ref("p.slice")) is want


def test_unitless_work_is_stopped():
    assert ar.systemd_unit_liveness(_ref("")) is Liveness.STOPPED
    assert ar.systemd_unit_liveness(_ref("x.service")) is Liveness.STOPPED


def test_systemd_reap(monkeypatch):
    stopped: list[str] = []
    monkeypatch.setattr(c, "stop_unit", lambda u: stopped.append(u) or u != "b.scope")
    monkeypatch.setattr(c, "populated_scopes", lambda u: ["a.scope", "b.scope"])
    assert ar.systemd_reap(_ref("x.scope")) == 1
    assert ar.systemd_reap(_ref("p.slice")) == 1
    assert ar.systemd_reap(_ref("")) == 0
    assert stopped == ["x.scope", "a.scope", "b.scope"]
    monkeypatch.setattr(c, "populated_scopes", lambda u: None)
    assert ar.systemd_reap(_ref("p.slice")) == 0


def _manager_with(names: list[str]) -> c.SliceManager:
    m = c.SliceManager(runner=FakeRunner(), systemctl=SYSTEMCTL)
    for n in names:
        m.apply(n, c.SliceLimits(), cpu_delegated=False)
    return m


def test_sweep_idle_slices_waits_out_the_idle_window(systemctl):
    alice, bob = c.principal_slice_name("alice"), c.principal_slice_name("bob")
    m = _manager_with([INSTANCE, alice, bob])
    idle: dict[str, float] = {alice: 5.0}

    def sweep(now, force=False):
        return asyncio.run(
            ar.sweep_idle_slices(
                m, budgets=["alice"], idle=idle, now=now, busy=lambda n: [], force=force
            )
        )

    assert sweep(100.0) == 0
    assert alice not in idle and idle[bob] == 100.0
    assert sweep(100.0 + ar.SLICE_IDLE_SECONDS) == 1
    assert not m.is_ready(bob) and bob not in idle
    assert sweep(0.0, force=True) == 1
    assert m.applied() == (INSTANCE,)


def test_sweep_idle_slices_without_names(monkeypatch):
    def bad():
        raise RuntimeError("x")

    monkeypatch.setattr(c, "instance_slice_name", bad)
    m = c.SliceManager(runner=FakeRunner())
    assert asyncio.run(ar.sweep_idle_slices(m, budgets=[], idle={}, now=0, busy=lambda n: [])) == 0
    assert ar.principal_slices_left(m) == []
    assert ar.release_instance_slice(m) == 0


def test_release_instance_slice_only_when_empty(systemctl):
    alice = c.principal_slice_name("alice")
    m = _manager_with([INSTANCE, alice])
    assert ar.principal_slices_left(m) == [alice]
    assert ar.release_instance_slice(m, populated=lambda n: False) == 0
    m.forget(alice)
    assert ar.release_instance_slice(m, populated=lambda n: None) == 0
    assert ar.release_instance_slice(m, populated=lambda n: True) == 0
    assert ar.release_instance_slice(m, populated=lambda n: False) == 1
    assert ar.release_instance_slice(m, populated=lambda n: False) == 0  # already gone


def test_release_stale_slices_skips_owned(systemctl):
    stem = INSTANCE[: -len(".slice")]
    owned, stale = f"{stem}-pown.slice", f"{stem}-pold.slice"
    listing = ("--user", "list-units", "--all", "--plain", "--no-legend", f"{stem}-p*.slice")
    run = FakeRunner({listing: (0, f"{owned} x\n{stale} x\n")})
    m = c.SliceManager(runner=run)
    m.apply(owned, c.SliceLimits(), cpu_delegated=False)
    assert asyncio.run(ar.release_stale_slices(m, busy=lambda n: [])) == 1
    assert m.is_ready(owned)
    run.answers[listing] = (1, "")
    assert asyncio.run(ar.release_stale_slices(m, busy=lambda n: [])) == 0


@pytest.mark.parametrize("failure", ["returncode", "oserror", "timeout", "missing_binary"])
def test_reset_instance_quota_retries_without_stopping_slices(systemctl, monkeypatch, failure):
    run = FakeRunner()
    manager = c.SliceManager(runner=run)
    assert manager.reset_instance_quota()
    assert run.calls == []
    limits = c.SliceLimits(cpu_quota_percent=2400, memory_max_mb=100, tasks_max=10)
    assert manager.apply(INSTANCE, limits, cpu_delegated=True)
    run.calls.clear()
    with monkeypatch.context() as patch:
        if failure == "returncode":
            patch.setattr(run, "default", (1, ""))
        elif failure == "missing_binary":
            patch.setattr(manager, "_bin", lambda: None)
        else:
            error = OSError("gone") if failure == "oserror" else subprocess.TimeoutExpired("x", 15)
            patch.setattr(run, "raises", error)
        assert not manager.reset_instance_quota()
    assert manager.reset_instance_quota()
    assert manager._applied[INSTANCE] == ("MemoryMax=100M", "MemorySwapMax=0", "TasksMax=10")
    assert all(
        cmd == [SYSTEMCTL, "--user", "set-property", "--runtime", INSTANCE, "CPUQuota="]
        for cmd in run.calls
    )
    run.calls.clear()
    assert manager.reset_instance_quota()
    assert run.calls == []
    assert manager.apply(INSTANCE, limits, cpu_delegated=True)
    assert run.calls[0][-4] == "CPUQuota=2400%"
