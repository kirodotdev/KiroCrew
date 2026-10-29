"""Per-principal containment: slice naming, the hard-mode backstop, and one real-cgroup
integration exercise.

The integration test runs only where a systemd user manager with memory and pids
delegation is reachable. It builds its own throwaway slice tree (never the
gateway's ``kirocrew-agents`` slices), with tiny limits, and stops it at the end.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from kiro_crew.shared_crew import containment as c
from kiro_crew.testing.wait import wait_until


def test_principal_slice_nests_under_the_instance_slice_without_leaking_the_id():
    pid = "p_" + "a" * 32
    name = c.principal_slice_name(pid, instance_slice="kirocrew-agents-0123456789ab.slice")
    assert name.startswith("kirocrew-agents-0123456789ab-p") and name.endswith(".slice")
    token = name[len("kirocrew-agents-0123456789ab-p") : -len(".slice")]
    assert len(token) == 12 and all(ch in "0123456789abcdef" for ch in token)
    assert "a" * 32 not in name
    assert c.principal_slice_name("p_other", instance_slice="x.slice") != c.principal_slice_name(
        pid, instance_slice="x.slice"
    )


def test_slice_limit_properties_drop_cpu_without_delegation():
    lim = c.SliceLimits(cpu_quota_percent=800, memory_max_mb=24576, tasks_max=8192)
    assert lim.properties(cpu_delegated=True) == [
        "CPUQuota=800%",
        "MemoryMax=24576M",
        "MemorySwapMax=0",
        "TasksMax=8192",
    ]
    assert "CPUQuota=800%" not in lim.properties(cpu_delegated=False)
    assert c.SliceLimits().properties(cpu_delegated=True) == []


def test_probe_reason_classification_is_stable():
    assert c.classify_probe_reason("controllers not delegated: ['memory']") == c.REASON_CONTROLLERS
    assert c.classify_probe_reason("user bus unreachable (x)") == c.REASON_NO_USER_BUS
    assert c.classify_probe_reason("not Linux") == c.REASON_NOT_LINUX
    assert c.classify_probe_reason("something new") == c.REASON_UNKNOWN


def test_hard_mode_backstop_refuses_every_runtime_start(monkeypatch):
    from kiro_crew.acp import runtime_start

    refusing = c.ContainmentReport(
        platform="linux",
        available=False,
        reason=c.REASON_CONTROLLERS,
        cpu=False,
        memory=False,
        pids=False,
        hard_limits=True,
        hard_ready=False,
    )
    monkeypatch.setattr(c, "_HARD_MODE_REPORT", refusing)
    with pytest.raises(c.ContainmentUnavailable) as err:
        runtime_start._refuse_without_containment()
    assert err.value.reason == c.REASON_CONTROLLERS
    monkeypatch.setattr(c, "_HARD_MODE_REPORT", None)
    runtime_start._refuse_without_containment()  # no report: nothing refused


def test_scope_parent_slice_uses_principal_only_when_applied(monkeypatch):
    calls: list[list[str]] = []

    def runner(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    mgr = c.SliceManager(runner=runner, systemctl="/usr/bin/systemctl")
    monkeypatch.setattr(c, "_ACTIVE_SLICES", mgr)
    with c.principal_scope("p_someone"):
        assert c.scope_parent_slice() is None  # not applied yet: the instance slice
        name = c.principal_slice_name("p_someone")
        assert mgr.apply(name, c.SliceLimits(tasks_max=16), cpu_delegated=True)
        assert c.scope_parent_slice() == name
    assert c.scope_parent_slice() is None  # outside the scope
    assert mgr.apply(name, c.SliceLimits(tasks_max=16), cpu_delegated=True)
    assert len(calls) == 2  # set-property + start once; the repeat is remembered


# -- real cgroup integration ------------------------------------------------------


def _delegation_reason() -> str | None:
    if sys.platform != "linux":
        return "not Linux"
    if shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        return "systemd-run/systemctl not found"
    report = c.probe_containment(hard_limits=False, slices_enabled=True)
    if not report.available:
        return f"no systemd user delegation here ({report.reason}: {report.detail})"
    if not (report.memory and report.pids):
        return "memory and pids are not both delegated to the user slice"
    return None


#: The exercise drives the real user manager (``set-property``, ``start``, ``stop`` on
#: its own throwaway slice), which the root conftest's host-service guard refuses for
#: an ordinary test run. It runs when this variable is set and the module is on the
#: guard's allowlist, or directly: ``python test/test_shared_crew_containment.py``.
REAL_CGROUP_ENV = "KIROCREW_SHARED_CREW_REAL_CGROUP"


def _skip_reason() -> str | None:
    if os.environ.get(REAL_CGROUP_ENV) != "1":
        return f"opt-in real-cgroup exercise; set {REAL_CGROUP_ENV}=1"
    return _delegation_reason()


_SKIP = _skip_reason()


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def _cgroup_dir(unit: str) -> Path | None:
    out = _systemctl("show", "-p", "ControlGroup", "--value", unit).stdout.strip()
    return Path("/sys/fs/cgroup" + out) if out else None


def _read_int(path: Path) -> int:
    raw = path.read_text(encoding="utf-8").strip()
    return 0 if raw == "max" else int(raw)


@pytest.mark.skipif(_SKIP is not None, reason=f"real cgroup test skipped: {_SKIP}")
def test_real_principal_slice_aggregates_two_scopes_memory_and_pids():
    result = run_real_slice_exercise()
    assert result["slice_pids"] == result["scope_pids"] == 6


def run_real_slice_exercise() -> dict[str, object]:
    """Two scopes under one principal slice; the slice must count and cap them together."""
    tag = uuid.uuid4().hex[:10]
    parent = f"scctest{tag}.slice"
    principal = "p_" + tag
    slice_name = c.principal_slice_name(principal, instance_slice=parent)
    mgr = c.SliceManager()
    procs: list[subprocess.Popen[bytes]] = []
    try:
        assert mgr.apply(
            slice_name,
            c.SliceLimits(cpu_quota_percent=20, memory_max_mb=256, tasks_max=12),
            cpu_delegated=True,
        )
        for i in range(2):
            # Each scope: a shell, two sleeping children and an 8 MiB resident buffer.
            script = (
                "import os, signal, subprocess, sys, time\n"
                "buf = bytearray(8 << 20)\n"
                "for j in range(0, len(buf), 4096): buf[j] = 1\n"
                "kids = [subprocess.Popen(['sleep', '120']) for _ in range(2)]\n"
                "time.sleep(120)\n"
            )
            procs.append(
                subprocess.Popen(
                    [
                        "systemd-run",
                        "--user",
                        "--scope",
                        "-q",
                        f"--unit=scctest{tag}-{i}",
                        f"--slice={slice_name}",
                        "--",
                        sys.executable,
                        "-c",
                        script,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        scope_dirs = [
            wait_until(lambda i=i: _cgroup_dir(f"scctest{tag}-{i}.scope"), timeout=30)
            for i in range(2)
        ]
        slice_dir = wait_until(lambda: _cgroup_dir(slice_name), timeout=30)

        def _settled() -> bool:
            return all(_read_int(d / "pids.current") >= 3 for d in scope_dirs) and all(
                _read_int(d / "memory.current") >= 8 << 20 for d in scope_dirs
            )

        wait_until(
            _settled,
            timeout=30,
            describe=lambda: [(_read_int(d / "pids.current")) for d in scope_dirs],
        )
        scope_pids = sum(_read_int(d / "pids.current") for d in scope_dirs)
        scope_mem = sum(_read_int(d / "memory.current") for d in scope_dirs)
        # The slice counts both scopes together: one person's budget is one total.
        assert _read_int(slice_dir / "pids.current") == scope_pids == 6
        assert _read_int(slice_dir / "memory.current") >= scope_mem >= 16 << 20
        assert _read_int(slice_dir / "pids.max") == 12
        assert _read_int(slice_dir / "memory.max") == 256 << 20
        assert (slice_dir / "cpu.max").read_text(encoding="utf-8").split()[0] == "20000"
        # A third scope may only add tasks up to the slice's aggregate ceiling (12).
        third = subprocess.run(
            [
                "systemd-run",
                "--user",
                "--scope",
                "-q",
                f"--unit=scctest{tag}-2",
                f"--slice={slice_name}",
                "--",
                sys.executable,
                "-c",
                "import os\nok = 0\n"
                "for _ in range(12):\n"
                "    try:\n"
                "        pid = os.fork()\n"
                "    except OSError:\n"
                "        break\n"
                "    if pid == 0:\n"
                "        import time; time.sleep(5); os._exit(0)\n"
                "    ok += 1\n"
                "print(ok)\n",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        forked = int((third.stdout or "0").strip() or 0)
        assert forked < 12, third.stdout
        assert "max " in (slice_dir / "pids.events").read_text(encoding="utf-8")
        result = {
            "slice": slice_name,
            "scope_pids": scope_pids,
            "slice_pids": _read_int(slice_dir / "pids.current"),
            "scope_memory_bytes": scope_mem,
            "slice_memory_bytes": _read_int(slice_dir / "memory.current"),
            "third_scope_forks_before_limit": forked,
        }
        return result
    finally:
        _systemctl("stop", slice_name)
        _systemctl("stop", parent)
        for p in procs:
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=30)
        assert _systemctl("is-active", slice_name).stdout.strip() != "active"


if __name__ == "__main__":
    reason = _delegation_reason()
    if reason is not None:
        print(f"SKIPPED: {reason}")
        raise SystemExit(0)
    print(run_real_slice_exercise())
