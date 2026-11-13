"""Unit tests for the Dev Fleet per-pod system-resource probe.

Everything is injected: no real ``systemctl``, no real ``du``, no network, and
no writes. ``rt`` (the pod runtime) is replaced with a ``SimpleNamespace`` and
``subprocess.run`` is stubbed, so these run identically on any platform and
exercise the parse / CPU-delta / TTL-cache / absent-off-Linux contracts.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.apps.builtins.dev_fleet import fleet_state, runtime

# Pod HOME values are opaque (``rt.pod_home`` is stubbed), but built from the
# temp dir so no path in this file is impossible on Windows.
_TMP = tempfile.gettempdir()
_HOME_MISSING = os.path.join(_TMP, "kc-nonexistent-pod-home")
_HOME_ALPHA = os.path.join(_TMP, "kc-pods", "alpha")

# Canned ``systemctl --user show`` dump; the probe matches blocks to units by
# ``Id``. Beta has MemoryAccounting=no and no MemoryMax (systemd omits a property
# it cannot answer, so the parser must tolerate it).
_UNIT_A = "kirocrew-pod@alpha.service"
_UNIT_B = "kirocrew-pod@beta.service"
_CANNED_TWO_PODS = f"""\
Id={_UNIT_A}
MemoryCurrent=652242944
MemoryMax=4294967296
CPUUsageNSec=430854771000
TasksCurrent=108
MemoryAccounting=yes
CPUAccounting=yes

Id={_UNIT_B}
MemoryCurrent=18446744073709551615
CPUUsageNSec=1000
TasksCurrent=3
MemoryAccounting=no
CPUAccounting=no
"""


@pytest.fixture
def _cfg():
    """A stand-in PodConfig: only pod_unit/pod_home reach it via ``rt``."""
    return SimpleNamespace(unit_prefix="kirocrew-pod")


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Isolate the module-level CPU/home caches between tests."""
    fleet_state._POD_CPU_SAMPLES.clear()
    fleet_state._POD_HOME_SIZE_CACHE.clear()
    yield
    fleet_state._POD_CPU_SAMPLES.clear()
    fleet_state._POD_HOME_SIZE_CACHE.clear()


def _stub_rt(monkeypatch, *, show_stdout="", show_exc=None, home=_HOME_MISSING):
    """Install an ``rt`` whose systemctl returns *show_stdout* (or raises)."""

    def _systemctl(*args, **kw):
        if show_exc is not None:
            raise show_exc
        return SimpleNamespace(returncode=0, stdout=show_stdout, stderr="")

    ns = SimpleNamespace(
        pod_unit=lambda cfg, name: f"{cfg.unit_prefix}@{name}.service",
        pod_home=lambda cfg, name: home,
        systemctl=_systemctl,
        orphan_homes=lambda cfg: [],
    )
    monkeypatch.setattr(runtime, "rt", ns, raising=False)
    return ns


# _parse_systemctl_records + _coerce_uint
def test_parse_splits_records_in_order():
    records = fleet_state._parse_systemctl_records(_CANNED_TWO_PODS)
    assert len(records) == 2
    assert records[0]["MemoryCurrent"] == "652242944"
    assert records[0]["MemoryAccounting"] == "yes"
    # Second record is missing MemoryMax entirely — tolerated, absent key.
    assert "MemoryMax" not in records[1]
    assert records[1]["MemoryAccounting"] == "no"


def test_parse_empty_input_is_no_records():
    assert fleet_state._parse_systemctl_records("") == []
    assert fleet_state._parse_systemctl_records("\n\n") == []


def test_coerce_uint_rejects_sentinels():
    # max-uint (systemd 'infinity'/'not measured') and [not set] collapse to None.
    assert fleet_state._coerce_uint("18446744073709551615") is None
    assert fleet_state._coerce_uint("[not set]") is None
    assert fleet_state._coerce_uint("infinity") is None
    assert fleet_state._coerce_uint("") is None
    assert fleet_state._coerce_uint(None) is None
    # A real measurement survives.
    assert fleet_state._coerce_uint("652242944") == 652242944


def test_a_short_record_list_never_misattributes(monkeypatch, _cfg):
    """Records are matched by unit Id, never by position.

    A unit systemd does not know emits no block, so positional pairing would
    publish one pod's figures under another pod's name.
    """
    unit_c = "kirocrew-pod@gamma.service"
    dump = (
        f"Id={_UNIT_A}\nMemoryCurrent=100\nTasksCurrent=1\n"
        "MemoryAccounting=yes\nCPUAccounting=no\n\n"
        f"Id={unit_c}\nMemoryCurrent=300\nTasksCurrent=3\n"
        "MemoryAccounting=yes\nCPUAccounting=no\n"
    )
    _stub_rt(monkeypatch, show_stdout=dump)
    out = fleet_state._pod_resources_sync(_cfg, ["alpha", "beta", "gamma"])
    # beta was unknown to systemd -> absent, not filled with gamma's block.
    assert "beta" not in out
    assert out["alpha"]["mem_current"] == 100
    assert out["gamma"]["mem_current"] == 300
    assert out["gamma"]["tasks"] == 3


def test_home_size_cache_drops_pods_that_stopped(monkeypatch, _cfg):
    """A worktree evicted mid-TTL must stop contributing its cached size.

    The cache is pruned against the same liveness signal as the CPU samples, so
    a removed pod is not summed into the fleet total.
    """
    _stub_rt(monkeypatch, show_stdout=_CANNED_TWO_PODS)
    fleet_state._POD_HOME_SIZE_CACHE[_UNIT_A] = (1.0, 111)
    fleet_state._POD_HOME_SIZE_CACHE["kirocrew-pod@evicted.service"] = (1.0, 999)
    fleet_state._pod_resources_sync(_cfg, ["alpha", "beta"])
    assert _UNIT_A in fleet_state._POD_HOME_SIZE_CACHE
    assert "kirocrew-pod@evicted.service" not in fleet_state._POD_HOME_SIZE_CACHE
    assert fleet_state._coerce_uint("0") == 0


# _pod_resources_sync: parse canned output, accounting-off, missing prop
def test_resources_parse_two_pods(monkeypatch, _cfg):
    _stub_rt(monkeypatch, show_stdout=_CANNED_TWO_PODS)
    # Avoid the du path: home size is None for a nonexistent tree.
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(return_value=SimpleNamespace(returncode=1, stdout="", stderr="")),
    )
    out = fleet_state._pod_resources_sync(_cfg, ["alpha", "beta"])

    a = out["alpha"]
    assert a["mem_current"] == 652242944
    assert a["mem_max"] == 4294967296
    assert a["tasks"] == 108
    assert a["cpu_pct"] is None  # first sample -> null, never a fake 0

    b = out["beta"]
    # MemoryAccounting=no -> mem_current absent, not a fabricated number.
    assert b["mem_current"] is None
    # MemoryMax was absent in the dump -> None (no ceiling reported).
    assert b["mem_max"] is None
    # CPUAccounting=no -> cpu_pct absent.
    assert b["cpu_pct"] is None
    assert b["tasks"] == 3


def test_resources_empty_running_set_short_circuits(monkeypatch, _cfg):
    ns = _stub_rt(monkeypatch, show_stdout="SHOULD-NOT-BE-CALLED")
    called = {"n": 0}

    def _tracking(*a, **k):
        called["n"] += 1
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    ns.systemctl = _tracking
    assert fleet_state._pod_resources_sync(_cfg, []) == {}
    assert called["n"] == 0  # no subprocess spawned when nothing is running


# CPU delta: null on first sample, correct % on the second
def test_cpu_percent_null_then_pct():
    unit = "kirocrew-pod@alpha.service"
    rec_on = {"CPUAccounting": "yes", "CPUUsageNSec": "1000000000"}  # 1.0s cpu
    # First observation at t=100.0 -> null (no prior sample).
    assert fleet_state._cpu_percent(unit, rec_on, 100.0) is None
    # Second observation 2 wall-seconds later, +1.0 cpu-second -> 50.0%.
    rec2 = {"CPUAccounting": "yes", "CPUUsageNSec": "2000000000"}
    assert fleet_state._cpu_percent(unit, rec2, 102.0) == 50.0


def test_cpu_percent_absent_when_accounting_off():
    unit = "kirocrew-pod@x.service"
    assert fleet_state._cpu_percent(unit, {"CPUAccounting": "no", "CPUUsageNSec": "5"}, 1.0) is None
    # Accounting-off must not leave a stale sample that fakes a later delta.
    assert unit not in fleet_state._POD_CPU_SAMPLES


def test_cpu_percent_null_on_counter_reset():
    unit = "kirocrew-pod@y.service"
    assert (
        fleet_state._cpu_percent(unit, {"CPUAccounting": "yes", "CPUUsageNSec": "500"}, 10.0)
        is None
    )
    # Counter went backwards (unit restarted) -> null, not a negative %.
    assert (
        fleet_state._cpu_percent(unit, {"CPUAccounting": "yes", "CPUUsageNSec": "100"}, 12.0)
        is None
    )


def test_cpu_percent_null_across_a_fast_restart():
    """A restart must not be read as one continuous counter.

    The backwards-counter guard misses a fast restart that already burned past
    the old total; only the invocation id makes the restart unambiguous.
    """
    unit = "kirocrew-pod@z.service"
    first = {"CPUAccounting": "yes", "CPUUsageNSec": "1000000000", "InvocationID": "aaa"}
    assert fleet_state._cpu_percent(unit, first, 100.0) is None
    # Restarted (new invocation) AND already past the old total, so the delta is
    # positive -- the guard that would catch a backwards counter does not fire.
    restarted = {"CPUAccounting": "yes", "CPUUsageNSec": "3000000000", "InvocationID": "bbb"}
    assert fleet_state._cpu_percent(unit, restarted, 102.0) is None
    # The new invocation's own next sample is comparable again.
    same = {"CPUAccounting": "yes", "CPUUsageNSec": "4000000000", "InvocationID": "bbb"}
    assert fleet_state._cpu_percent(unit, same, 104.0) == 50.0


def test_empty_running_set_clears_stale_caches(monkeypatch, _cfg):
    """No pods running is liveness information, not a reason to skip pruning.

    Otherwise a stale home size keeps feeding the fleet total and a stale CPU
    sample is compared against a restarted pod's counter.
    """
    _stub_rt(monkeypatch, show_stdout="")
    fleet_state._POD_CPU_SAMPLES["kirocrew-pod@gone.service"] = (1.0, 5, "aaa")
    fleet_state._POD_HOME_SIZE_CACHE["kirocrew-pod@gone.service"] = (1.0, 999)
    assert fleet_state._pod_resources_sync(_cfg, []) == {}
    assert fleet_state._POD_CPU_SAMPLES == {}
    assert fleet_state._POD_HOME_SIZE_CACHE == {}


# Non-Linux / failed probe -> absent fields, never zeros
def test_probe_failure_yields_empty(monkeypatch, _cfg):
    # rt.systemctl raising (require_systemd off Linux, or any error) -> {}.
    _stub_rt(monkeypatch, show_exc=RuntimeError("systemctl unavailable off Linux"))
    assert fleet_state._pod_resources_sync(_cfg, ["alpha"]) == {}


# Home-size cache is not recomputed within its TTL
@pytest.mark.asyncio
async def test_home_size_cached_within_ttl(monkeypatch, _cfg):
    _stub_rt(monkeypatch, home=_HOME_ALPHA)
    calls = {"n": 0, "argv": None}

    async def _fake_run_cmd(argv, **kw):
        calls["n"] += 1
        calls["argv"] = argv
        return 0, f"123456\t{_HOME_ALPHA}\n", ""

    monkeypatch.setattr(runtime, "_run_cmd", _fake_run_cmd)

    unit = "kirocrew-pod@alpha.service"
    # t=1000: first call runs du.
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: "/usr/bin/du")
    assert await fleet_state._pod_home_size(_cfg, "alpha", unit, 1000.0) == 123456
    assert calls["n"] == 1
    # t=1000+30 (< TTL 60): served from cache, du NOT re-run.
    assert await fleet_state._pod_home_size(_cfg, "alpha", unit, 1030.0) == 123456
    assert calls["n"] == 1
    # t=1000+61 (> TTL): recomputed.
    assert await fleet_state._pod_home_size(_cfg, "alpha", unit, 1061.0) == 123456
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_home_size_goes_through_the_routed_chokepoint(monkeypatch, _cfg):
    """The `du` must be issued via `_run_cmd`, not a bare subprocess.

    `_run_cmd` vets the binary instead of trusting the service PATH (which leads
    with agent-writable directories), pins the child's PATH and encoding, and
    keeps the spawn inside the sandbox chokepoint the spawn audit requires.
    """
    _stub_rt(monkeypatch, home=_HOME_ALPHA)
    seen = {}

    async def _fake_run_cmd(argv, **kw):
        seen["argv"] = argv
        return 0, "1\t.\n", ""

    monkeypatch.setattr(runtime, "_run_cmd", _fake_run_cmd)
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: "/usr/bin/du")
    raw = MagicMock()
    monkeypatch.setattr(subprocess, "run", raw)
    await fleet_state._pod_home_size(_cfg, "alpha", "kirocrew-pod@alpha.service", 1.0)
    # Routed by NAME -- `_run_cmd` resolves and vets it; the caller never picks
    # the binary path itself.
    assert seen["argv"][0] == "du"
    assert str(_HOME_ALPHA) in seen["argv"][-1]
    raw.assert_not_called()


@pytest.mark.asyncio
async def test_home_size_failure_is_none(monkeypatch, _cfg):
    _stub_rt(monkeypatch, home=_HOME_ALPHA)

    async def _fail(argv, **kw):
        return 1, "", "err"

    monkeypatch.setattr(runtime, "_run_cmd", _fail)
    assert (
        await fleet_state._pod_home_size(_cfg, "alpha", "kirocrew-pod@alpha.service", 1.0) is None
    )


def test_orphan_count(monkeypatch, _cfg):
    monkeypatch.setattr(
        runtime, "rt", SimpleNamespace(orphan_homes=lambda cfg: ["dead1", "dead2"]), raising=False
    )
    assert fleet_state._orphan_count_sync(_cfg) == 2

    monkeypatch.setattr(
        runtime,
        "rt",
        SimpleNamespace(orphan_homes=MagicMock(side_effect=OSError("boom"))),
        raising=False,
    )
    assert fleet_state._orphan_count_sync(_cfg) is None
