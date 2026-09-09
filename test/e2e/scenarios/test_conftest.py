from __future__ import annotations

import subprocess

# Relative, not `test.e2e.scenarios`: the interpreter's own stdlib `test`
# package shadows that absolute name wherever it is installed (the CI
# hostedtoolcache Python ships it), while pytest imports this file as
# `e2e.scenarios.test_conftest`, so the sibling conftest is `.conftest`.
from . import conftest as scenarios_conftest


def test_stale_plane_sweep_requires_suite_marker(tmp_path, monkeypatch):
    unmarked = tmp_path / "kce2e-99999999998"
    marked = tmp_path / "kce2e-99999999999"
    unmarked.mkdir()
    marked.mkdir()
    (unmarked / "keep.txt").write_text("unrelated", encoding="utf-8")
    (marked / scenarios_conftest._PLANE_MARKER).write_text("99999999999", encoding="ascii")
    monkeypatch.setattr(scenarios_conftest.platform_compat, "pid_exists", lambda _pid: False)

    scenarios_conftest._sweep_stale_plane_roots(tmp_path)

    assert (unmarked / "keep.txt").is_file()
    assert not marked.exists()


def test_force_clean_plane_unloads_launchd_jobs_and_removes_plists(tmp_path, monkeypatch):
    scratch = tmp_path / "plane"
    pods_dir = scratch / "h"
    pods_dir.mkdir(parents=True)
    label_prefix = "dev.kirocrew.pod.kirocrew-e2e-pod."
    smoke_label = f"{label_prefix}smoke"
    stale_label = f"{label_prefix}stale"
    smoke_plist = pods_dir / f"{smoke_label}.plist"
    stale_plist = pods_dir / f"{stale_label}.plist"
    unrelated_plist = pods_dir / "dev.kirocrew.pod.other-plane.keep.plist"
    for plist in (smoke_plist, stale_plist, unrelated_plist):
        plist.write_text("plist", encoding="utf-8")

    calls: list[list[str]] = []
    removed_roots = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def fake_rmtree(path, *, ignore_errors):
        assert ignore_errors is True
        removed_roots.append(path)

    monkeypatch.setattr(scenarios_conftest.sys, "platform", "darwin")
    monkeypatch.setattr(scenarios_conftest.subprocess, "run", fake_run)
    monkeypatch.setattr(scenarios_conftest.shutil, "rmtree", fake_rmtree)
    monkeypatch.setattr(
        scenarios_conftest.platform_compat, "live_thread_group_leaders", lambda: None
    )
    env = {
        "KIROCREW_POD_UNIT_PREFIX": scenarios_conftest.PLANE_PREFIX,
        "KIROCREW_POD_ROOT": str(pods_dir),
    }

    services, killed = scenarios_conftest._force_clean_plane(scratch, "smoke", env)

    # launchd.domain(), not os.getuid(): this test also runs on Windows, where
    # os has no getuid; the helper carries the same guard the product uses.
    domain = scenarios_conftest.launchd.domain()
    assert calls == [
        ["launchctl", "bootout", f"{domain}/{smoke_label}"],
        ["launchctl", "bootout", f"{domain}/{stale_label}"],
    ]
    assert services == [smoke_label, stale_label]
    assert killed == []
    assert not smoke_plist.exists()
    assert not stale_plist.exists()
    assert unrelated_plist.exists()
    assert removed_roots == [scratch]
