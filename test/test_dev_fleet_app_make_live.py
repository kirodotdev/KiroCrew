"""Dev Fleet make-live: switching the live gateway to another worktree.

Covers the worktree detail view, the restart-gateway endpoint, the make-live plan,
cutover and undo, the committed latch, live-worktree resolution, the pod guard, the
staged-only path and the user-unit status probes.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dev_fleet_app_helpers import _reset_make_live_committed_latch  # noqa: F401 - fixture
from dev_fleet_app_helpers import _reset_shutdown_admission_state  # noqa: F401 - fixture
from dev_fleet_app_helpers import _sweep_staged_sync_tempdirs  # noqa: F401 - fixture
from dev_fleet_app_helpers import _venv_maps_to_the_synced_checkout  # noqa: F401 - fixture
from dev_fleet_app_helpers import _POSIX_ONLY, _mk_make_live_wt, _stub_make_live

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import fleet_state as fleet_state_mod
from kiro_crew.apps.builtins.dev_fleet import live as live_mod
from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod

# =============================================================================
# Task 1a: own-commits-only argv in _worktree_detail
# =============================================================================


@pytest.mark.asyncio
async def test_worktree_detail_uses_own_commits_log():
    """_worktree_detail must use `log <remote>/main..HEAD` (own commits only),
    never a bare `log -6` that bleeds shared history."""
    git_calls: list[list[str]] = []

    async def mock_git(path, *args, **kw):
        git_calls.append(list(args))
        if "rev-parse" in args and "--abbrev-ref" in args:
            return "feat-x"
        if "rev-parse" in args and "--short=7" in args:
            return "abc1234"
        if "status" in args:
            return ""
        if "rev-list" in args:
            return "2"
        if "log" in args and any("main..HEAD" in a for a in args):
            return "abc1234\x1ffix bug\x1f2 hours ago"
        if "diff" in args and "--name-only" in args:
            return ""
        if "log" in args and "--format=%ct" in args:
            return "1700000000"
        return None

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(repository_mod, "_git", side_effect=mock_git),
        patch.object(
            fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value=None
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=2),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(
            runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "50\t/fake/wt\n", "")
        ),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
    ):
        result = await mod._worktree_detail("feat-x")

    assert result["commits"] == [{"hash": "abc1234", "subject": "fix bug", "when": "2 hours ago"}]
    log_calls = [c for c in git_calls if "log" in c and any("main..HEAD" in a for a in c)]
    assert len(log_calls) == 1
    bare_log_calls = [
        c
        for c in git_calls
        if c[:1] == ["log"] and "-6" in c and not any("main..HEAD" in a for a in c)
    ]
    assert len(bare_log_calls) == 0


# =============================================================================
# Task 1b: design_docs filter
# =============================================================================


@pytest.mark.asyncio
async def test_worktree_detail_design_docs_filter():
    """design_docs filters for paths starting docs/ or containing /docs/ or design."""

    async def mock_git(path, *args, **kw):
        if "rev-parse" in args and "--abbrev-ref" in args:
            return "feat-x"
        if "rev-parse" in args and "--short=7" in args:
            return "abc1234"
        if "status" in args:
            return ""
        if "rev-list" in args:
            return "0"
        if "log" in args and "origin/main..HEAD" in args:
            return ""
        if "diff" in args and "--name-only" in args:
            return "docs/README.md\nsrc/main.py\npackage/docs/spec.md\ndesign-doc.md\n"
        if "log" in args and "--format=%ct" in args:
            return "1700000000"
        return None

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(repository_mod, "_git", side_effect=mock_git),
        patch.object(
            fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value=None
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=0),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(
            runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "50\t/fake/wt\n", "")
        ),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
    ):
        result = await mod._worktree_detail("feat-x")

    assert set(result["design_docs"]) == {"docs/README.md", "package/docs/spec.md", "design-doc.md"}


# =============================================================================
# Task 2: restart-gateway endpoint
# =============================================================================


@pytest.mark.asyncio
async def test_restart_gateway_not_active():
    """restart-gateway returns ok:false when service is not active."""
    with patch.object(
        runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(3, "", "inactive\n")
    ):
        result = await mod._restart_gateway()
    assert result["ok"] is False
    # With the foreground fallback, the error comes from whichever backend
    # ultimately fails: either "not running as a user service" (no foreground
    # eligible) or the foreground backend's own diagnostic.
    assert result["error"]


@pytest.mark.asyncio
async def test_restart_gateway_active_detached():
    """restart-gateway issues systemd-run --collect restart when active."""
    calls: list[list[str]] = []

    async def mock_run_cmd(cmd, **kw):
        calls.append(cmd)
        if "is-active" in cmd:
            return (0, "active\n", "")
        if "systemd-run" in cmd:
            return (0, "", "")
        return (0, "", "")

    with (
        patch.object(runtime_mod, "_run_cmd", side_effect=mock_run_cmd),
        patch.object(live_mod, "sys") as mock_sys,
        patch.object(live_mod, "shutil") as mock_shutil,
    ):
        mock_sys.platform = "linux"
        mock_shutil.which.return_value = "/usr/bin/systemctl"
        result = await mod._restart_gateway()
    assert result["ok"] is True
    restart_calls = [c for c in calls if "systemd-run" in c]
    assert len(restart_calls) == 1
    assert "--collect" in restart_calls[0]
    assert "restart" in restart_calls[0]


@pytest.mark.asyncio
async def test_restart_gateway_audited():
    """The restart-gateway endpoint lives in the gateway route module and is a coroutine
    (the backend does not expose it — see gateway_routes.py)."""
    import inspect

    from kiro_crew.apps.builtins.dev_fleet import gateway_routes

    assert not hasattr(mod, "api_dev_fleet_restart_gateway")
    fn = gateway_routes.handle_restart_gateway
    assert callable(fn) and inspect.iscoroutinefunction(fn)


# =============================================================================
# Task: make-live (switch the live gateway to another worktree)
# =============================================================================


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_dry_run_plan(monkeypatch, tmp_path):
    """dry_run returns the pointer-based plan without writing anything."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is True and res["dry_run"] is True
    plan = res["plan"]
    assert plan["mechanism"] == "live-target pointer"
    assert plan["pointer_path"] == str(ptr_dir / "live_target.json")
    assert plan["exec"] == str(wt / ".venv" / "bin" / "kirocrew")
    assert plan["restart"] == "automatic"
    assert plan["target"] == str(wt)
    # dry-run writes nothing: pointer file absent, no commands issued.
    assert not (ptr_dir / "live_target.json").exists()
    assert calls == []


@pytest.mark.asyncio
async def test_make_live_unknown_path(monkeypatch):
    """A path that is not a discovered worktree is refused."""
    monkeypatch.setattr(repository_mod, "_discover_worktrees", AsyncMock(return_value=[]))
    monkeypatch.setattr(live_mod, "_in_pod", lambda: False)
    res = await mod._make_live("/nope/not-a-worktree", dry_run=True)
    assert res["ok"] is False and res["code"] == "unknown_path"


@pytest.mark.asyncio
async def test_make_live_refuses_in_pod(monkeypatch, tmp_path):
    """Refuse to cut the real live gateway from inside a pod plane (even dry_run)."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    _stub_make_live(monkeypatch, wt, in_pod=True)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "pod"


@pytest.mark.asyncio
async def test_make_live_already_live(monkeypatch, tmp_path):
    """Refuse when the target is already the live gateway."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    _stub_make_live(monkeypatch, wt, live=str(wt.resolve()))
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "already_live"


@pytest.mark.asyncio
async def test_make_live_missing_venv(monkeypatch, tmp_path):
    """No .venv/bin/kirocrew -> actionable Provision error."""
    wt = _mk_make_live_wt(tmp_path, venv=False, dist=True)
    _stub_make_live(monkeypatch, wt)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "missing_venv"
    assert "Provision" in res["error"]


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_venv_not_executable(monkeypatch, tmp_path):
    """.venv/bin/kirocrew present but NOT executable -> a distinct, actionable
    error (missing_venv is for the not-a-file case). A non-executable binary
    would stop the live gateway but never start the replacement, so this MUST
    be refused before any cutover."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True, venv_exec=False)
    _stub_make_live(monkeypatch, wt)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "venv_not_executable"
    assert "chmod" in res["error"]
    # An executable binary (the realistic provisioned state) passes this gate
    # and proceeds to a valid plan — proving the check is exec-bit-specific,
    # not a blanket rejection.
    wt_ok = _mk_make_live_wt(tmp_path / "ok", venv=True, dist=True)
    _stub_make_live(monkeypatch, wt_ok)
    res_ok = await mod._make_live(str(wt_ok), dry_run=True)
    assert res_ok["ok"] is True and res_ok.get("dry_run") is True


@pytest.mark.asyncio
async def test_make_live_missing_dist(monkeypatch, tmp_path):
    """Built venv but no dist/index.html -> actionable Pull+Build error."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=False)
    _stub_make_live(monkeypatch, wt)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "missing_dist"
    assert "Pull+Build" in res["error"]


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_real_cutover_writes_pointer(monkeypatch, tmp_path):
    """A real cutover on a drivable service writes the pointer AND restages the
    service definition, issues a detached restart, and invalidates the
    live-worktree cache.

    Restaging the definition is what keeps its ExecStart binary present: a
    definition left pinned to a worktree made live earlier fails EXEC once
    that worktree is pruned, and the gateway then never starts far enough to read
    the pointer at all.
    """
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    previous = _mk_make_live_wt(tmp_path / "previous", venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    dropin = tmp_path / "dropins" / "make-live.conf"
    _stub_make_live(
        monkeypatch,
        wt,
        live=str(previous.resolve()),
        pointer_dir=ptr_dir,
    )
    monkeypatch.setattr(live_mod, "_dropin_path", lambda: dropin)
    monkeypatch.setattr(live_mod, "_LIVE_WORKTREE", "sentinel", raising=False)
    monkeypatch.setattr(live_mod, "_LIVE_CHECK_AT", 123.0, raising=False)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True and res.get("cutover") is True
    assert res.get("staged_only") is not True
    # Pointer file written with the resolved checkout path.
    ptr_file = ptr_dir / "live_target.json"
    assert ptr_file.is_file()
    import json as _json

    data = _json.loads(ptr_file.read_text())
    assert Path(data["checkout"]).resolve() == wt.resolve()
    assert Path(data["previous_checkout"]).resolve() == previous.resolve()
    # Service definition restaged at the SAME target, then re-read.
    assert dropin.is_file()
    assert str(wt) in dropin.read_text(encoding="utf-8")
    assert ["systemctl", "--user", "daemon-reload"] in calls
    # A DETACHED restart was issued (platform-specific; linux = systemd-run).
    assert any(c[:2] == ["systemd-run", "--user"] and "restart" in c for c in calls)
    # Live-worktree cache invalidated so the next poll re-resolves.
    assert mod._LIVE_WORKTREE is None
    assert mod._LIVE_CHECK_AT == 0.0


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_omits_unusable_previous_checkout(monkeypatch, tmp_path):
    """An unprovisioned running checkout is not a safe Undo destination, but
    it must not make the primary cutover unavailable."""
    target = _mk_make_live_wt(tmp_path / "target", venv=True, dist=True)
    previous = tmp_path / "unprovisioned-current"
    previous.mkdir()
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(
        monkeypatch,
        target,
        live=str(previous.resolve()),
        pointer_dir=ptr_dir,
    )

    res = await mod._make_live(str(target))

    assert res["ok"] is True
    data = json.loads((ptr_dir / "live_target.json").read_text())
    assert Path(data["checkout"]).resolve() == target.resolve()
    assert "previous_checkout" not in data


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_undo_make_live_clears_one_level_history(monkeypatch, tmp_path):
    """Undo reuses the cutover transaction but consumes, rather than flips,
    the previous checkout so the inverse does not become an implicit redo."""
    current = _mk_make_live_wt(tmp_path / "current", venv=True, dist=True)
    previous = _mk_make_live_wt(tmp_path / "previous", venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(
        monkeypatch,
        previous,
        live=str(current.resolve()),
        unit_status="no_user_unit",
        pointer_dir=ptr_dir,
    )
    live_mod.live_target.write_target(
        current,
        previous_checkout=previous,
    )

    res = await mod._make_live(str(previous), undo=True)

    assert res["ok"] is True
    assert res["staged_only"] is True
    data = json.loads((ptr_dir / "live_target.json").read_text())
    assert Path(data["checkout"]).resolve() == previous.resolve()
    assert "previous_checkout" not in data


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_undo_make_live_refuses_stale_banner_target(monkeypatch, tmp_path):
    """A banner rendered for an older cutover cannot reverse a newer one."""
    current = _mk_make_live_wt(tmp_path / "current", venv=True, dist=True)
    previous = _mk_make_live_wt(tmp_path / "previous", venv=True, dist=True)
    stale = _mk_make_live_wt(tmp_path / "stale", venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(
        monkeypatch,
        stale,
        live=str(current.resolve()),
        unit_status="no_user_unit",
        pointer_dir=ptr_dir,
    )
    live_mod.live_target.write_target(
        current,
        previous_checkout=previous,
    )
    before = (ptr_dir / "live_target.json").read_text()

    res = await mod._make_live(str(stale), undo=True)

    assert res["ok"] is False
    assert res["code"] == "undo_changed"
    assert (ptr_dir / "live_target.json").read_text() == before


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_staged_only_leaves_service_definition_untouched(
    monkeypatch,
    tmp_path,
):
    """On a host whose service Dev Fleet cannot drive, only the pointer is
    staged.

    The definition there is the baseline install, whose ExecStart binary is by
    construction the one currently running — so touching it could only break a
    working definition, and the pointer alone carries the cutover.
    """
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    dropin = tmp_path / "dropins" / "make-live.conf"
    _stub_make_live(monkeypatch, wt, unit_status="no_user_unit", pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_dropin_path", lambda: dropin)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True and res["staged_only"] is True
    assert (ptr_dir / "live_target.json").is_file()
    assert not dropin.exists()
    assert ["systemctl", "--user", "daemon-reload"] not in calls


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_latches_after_cutover(monkeypatch, tmp_path):
    """A successful cutover latches _MAKE_LIVE_COMMITTED. A second request —
    cutover for a DIFFERENT valid target, or even a dry_run — is then refused
    with restart_pending, so no concurrent cutover can mutate the pointer while
    the scheduled restart is tearing this process down."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)

    async def fake_run_cmd(cmd, **kw):
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    assert mod._MAKE_LIVE_COMMITTED is False
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True and res.get("cutover") is True
    assert mod._MAKE_LIVE_COMMITTED is True

    # Second cutover for a different, otherwise-valid target -> restart_pending.
    wt2 = _mk_make_live_wt(tmp_path / "b", venv=True, dist=True)
    _stub_make_live(monkeypatch, wt2, pointer_dir=ptr_dir)
    res2 = await mod._make_live(str(wt2), dry_run=False)
    assert res2["ok"] is False and res2["code"] == "restart_pending"
    # A dry_run is refused too (the latch is checked at entry, before dry_run).
    res3 = await mod._make_live(str(wt2), dry_run=True)
    assert res3["ok"] is False and res3["code"] == "restart_pending"


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_write_failure_does_not_latch(monkeypatch, tmp_path):
    """A cutover that fails during pointer write (before restart scheduling)
    must NOT latch — the restart never happened, so a subsequent cutover
    proceeds normally."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)

    # Make write_target raise an OSError to simulate a disk failure.
    original_write = live_mod.live_target.write_target
    call_count = {"n": 0}

    def fail_first_write(checkout, *, previous_checkout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("disk full")
        return original_write(checkout, previous_checkout=previous_checkout)

    monkeypatch.setattr(live_mod.live_target, "write_target", fail_first_write)

    async def fake_run_cmd(cmd, **kw):
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is False and res["code"] == "write_failed"
    assert mod._MAKE_LIVE_COMMITTED is False

    # With the seam repaired, a subsequent cutover proceeds (not latched).
    monkeypatch.setattr(live_mod.live_target, "write_target", original_write)
    res2 = await mod._make_live(str(wt), dry_run=False)
    assert res2["ok"] is True and res2.get("cutover") is True
    assert mod._MAKE_LIVE_COMMITTED is True


@pytest.mark.asyncio
async def test_live_worktree_path_reads_working_directory_with_spaces(monkeypatch):
    """_live_worktree_path resolves via `systemctl show --property=
    WorkingDirectory --value`, which (unlike the ExecStart path= regex) is NOT
    truncated at spaces — so a checkout path containing a space resolves whole."""
    spacey = "/home/u/my worktrees/kirocrew-wt-feat"

    async def fake_run_cmd(cmd, **kw):
        assert "--property=WorkingDirectory" in cmd and "--value" in cmd
        return (0, spacey + "\n", "")

    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="linux"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
    )
    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    monkeypatch.setattr(live_mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(live_mod, "_LIVE_WORKTREE", None, raising=False)
    got = await mod._live_worktree_path()
    assert got == str(Path(spacey).resolve())


@pytest.mark.asyncio
async def test_live_worktree_path_falls_back_to_execstart(monkeypatch, tmp_path):
    """When WorkingDirectory is empty, fall back to parsing ExecStart's path=."""
    checkout = tmp_path / "kirocrew-wt-feat"
    (checkout / ".venv" / "bin").mkdir(parents=True)
    exe = checkout / ".venv" / "bin" / "kirocrew"

    async def fake_run_cmd(cmd, **kw):
        if "--property=WorkingDirectory" in cmd:
            return (0, "\n", "")  # empty -> trigger ExecStart fallback
        if cmd[-1] == "ExecStart":
            return (0, f"{{ path={exe} ; argv[]={exe} gateway ; ignore_errors=no }}", "")
        raise AssertionError(f"unexpected cmd {cmd}")

    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="linux"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
    )
    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    monkeypatch.setattr(live_mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(live_mod, "_LIVE_WORKTREE", None, raising=False)
    got = await mod._live_worktree_path()
    assert got == str(checkout.resolve())


@pytest.mark.asyncio
async def test_make_live_already_live_space_path(monkeypatch, tmp_path):
    """already_live is detected when the live WorkingDirectory (and target)
    contain spaces — the regression the WorkingDirectory switch fixes: the old
    ExecStart path= regex truncated at the space, never matched, and would let
    the same worktree be pointlessly re-cut over and over."""
    wt = tmp_path / "my worktrees" / "kirocrew-wt-feat"
    wt.mkdir(parents=True)
    vb = wt / ".venv" / "bin"
    vb.mkdir(parents=True)
    kc = vb / "kirocrew"
    kc.write_text("#!/bin/sh\n")
    kc.chmod(0o755)
    dd = wt / "src" / "kiro_crew" / "static" / "dist"
    dd.mkdir(parents=True)
    (dd / "index.html").write_text("<html></html>")
    _stub_make_live(monkeypatch, wt, live=str(wt.resolve()))
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "already_live"


def test_make_live_route_registered_and_audited():
    """/make-live is wired in the GATEWAY route module, not the backend's create_app:
    the pointer it writes is masked from the backend and every child it spawns."""
    import inspect

    from aiohttp import web as _web

    from kiro_crew.apps.builtins.dev_fleet import gateway_routes

    backend_paths = [
        getattr(r.resource, "canonical", None) for r in mod.create_app().router.routes()
    ]
    assert "/api/make-live" not in backend_paths
    assert not hasattr(mod, "api_dev_fleet_make_live")
    gw = _web.Application()
    gateway_routes.register_routes(gw)
    gw_paths = [getattr(r.resource, "canonical", None) for r in gw.router.routes()]
    assert "/api/apps/dev-fleet/make-live" in gw_paths
    fn = gateway_routes.handle_make_live
    assert callable(fn) and inspect.iscoroutinefunction(fn)


# --- make-live: fail-closed pod guard ---
def test_in_pod_tristate(monkeypatch, tmp_path):
    """_in_pod is tri-state: True inside a pod home, False outside, and None
    when the config home cannot be resolved (fail-closed at the source — the
    previous fail-OPEN False would have let a pod cut the live gateway)."""
    import kiro_crew.config.loader as cfg_loader

    pod_home = tmp_path / ".kirocrew-pods" / "kirocrew-wt-x"
    pod_home.mkdir(parents=True)
    monkeypatch.setattr(cfg_loader, "config_dir", lambda: pod_home)
    assert mod._in_pod() is True

    live_home = tmp_path / ".kirocrew"
    live_home.mkdir()
    monkeypatch.setattr(cfg_loader, "config_dir", lambda: live_home)
    assert mod._in_pod() is False

    monkeypatch.setattr(cfg_loader, "config_dir", MagicMock(side_effect=RuntimeError("boom")))
    assert mod._in_pod() is None


@pytest.mark.asyncio
async def test_make_live_pod_indeterminate_fails_closed(monkeypatch, tmp_path):
    """Indeterminate pod status must refuse make-live (fail-closed), never
    proceed as if not-a-pod."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    _stub_make_live(monkeypatch, wt)
    monkeypatch.setattr(live_mod, "_in_pod", lambda: None)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "pod_indeterminate"


# --- make-live: staged-only when service not drivable ---
@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_stages_only_when_service_not_drivable(monkeypatch, tmp_path):
    """A live gateway installed as a SYSTEM unit (or no service at all) succeeds
    as staged_only: the pointer is written, no restart attempted, and a manual
    restart command is provided. The committed latch is NOT set (re-pointing to
    a different worktree must stay allowed)."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"

    for status in ("no_user_unit", "no_systemd"):
        ptr_dir_sub = ptr_dir / status
        _stub_make_live(monkeypatch, wt, unit_status=status, pointer_dir=ptr_dir_sub)
        monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
        calls: list = []

        async def fake_run_cmd(cmd, **kw):
            calls.append(cmd)
            return (0, "", "")

        monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
        res = await mod._make_live(str(wt), dry_run=False)
        assert res["ok"] is True, f"status={status}: {res}"
        assert res["staged_only"] is True
        assert res["manual_restart"]  # non-empty command string
        # The dashboard surfaces `notice` verbatim instead of entering the restart
        # handshake, so a staged_only response without it would silently drop the
        # only instruction the operator gets.
        assert res["manual_restart"] in res["notice"]
        # Pointer written with the target.
        ptr_file = ptr_dir_sub / "live_target.json"
        assert ptr_file.is_file()
        import json as _json

        data = _json.loads(ptr_file.read_text())
        assert Path(data["checkout"]).resolve() == wt.resolve()
        # NOT latched: a subsequent cutover to another worktree stays allowed.
        assert mod._MAKE_LIVE_COMMITTED is False
        # No restart command issued.
        assert not any(c[:2] == ["systemd-run", "--user"] for c in calls)


@pytest.mark.asyncio
async def test_live_user_unit_status_no_manager(monkeypatch):
    """A platform with neither systemd nor launchd -> no_systemd, no spawn.

    ``platform="darwin"`` is a SUPPORTED backend, so the "no manager at all"
    case has to be expressed with
    a platform that really has none.
    """
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="win32"))
    assert await mod._live_user_unit_status() == "no_systemd"


@pytest.mark.asyncio
async def test_live_user_unit_status_darwin_no_agent(monkeypatch):
    """macOS with launchctl but no such agent loaded -> no_agent."""
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(runtime_mod, "_run_cmd", AsyncMock(return_value=(1, "", "no such")))
    assert await mod._live_user_unit_status() == "no_agent"


@pytest.mark.asyncio
async def test_live_user_unit_status_darwin_agent_not_indirected(monkeypatch, tmp_path):
    """Agent loaded, but its plist does not go through the live-gateway symlink.

    Swapping the symlink would then be a silent no-op, so make-live must refuse
    with an actionable code instead of reporting a cutover that did nothing.
    """
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(runtime_mod, "_run_cmd", AsyncMock(return_value=(0, "  pid = 7\n", "")))
    plist = tmp_path / "agent.plist"
    plist.write_text("<string>/usr/local/bin/kirocrew</string>")
    monkeypatch.setattr(
        live_mod.gateway_service.LaunchdBackend, "plist_path", staticmethod(lambda: plist)
    )
    assert await mod._live_user_unit_status() == "agent_not_indirected"


@pytest.mark.asyncio
async def test_live_user_unit_status_darwin_ok(monkeypatch, tmp_path):
    """Agent loaded AND indirected through the live-gateway symlink -> ok."""
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(runtime_mod, "_run_cmd", AsyncMock(return_value=(0, "  pid = 7\n", "")))
    link = tmp_path / "live-gateway"
    link.write_text("#!/bin/sh\nexec '/usr/local/bin/kirocrew' \"$@\"\n")
    plist = tmp_path / "agent.plist"
    plist.write_text(f"<string>{link}</string>")
    monkeypatch.setattr(
        live_mod.gateway_service.LaunchdBackend, "plist_path", staticmethod(lambda: plist)
    )
    monkeypatch.setattr(
        live_mod.gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: link)
    )
    monkeypatch.setattr(live_mod.gateway_service, "restart_contract_current", lambda _path: True)
    monkeypatch.setattr(
        live_mod.gateway_service, "loaded_restart_contract_current", lambda _out: True
    )
    assert await mod._live_user_unit_status() == "ok"


@pytest.mark.asyncio
async def test_live_user_unit_status_darwin_loaded_contract_outdated(monkeypatch, tmp_path):
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    link = tmp_path / "live-gateway"
    link.write_text("#!/bin/sh\n")
    plist = tmp_path / "agent.plist"
    plist.write_text(f"<string>{link}</string>")
    monkeypatch.setattr(
        live_mod.gateway_service.LaunchdBackend, "plist_path", staticmethod(lambda: plist)
    )
    monkeypatch.setattr(
        live_mod.gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: link)
    )
    monkeypatch.setattr(live_mod.gateway_service, "restart_contract_current", lambda _path: True)
    monkeypatch.setattr(
        live_mod.gateway_service, "loaded_restart_contract_current", lambda _out: False
    )
    monkeypatch.setattr(runtime_mod, "_run_cmd", AsyncMock(return_value=(0, "pid = 7\n", "")))

    assert await mod._live_user_unit_status() == "agent_restart_contract_outdated"


@pytest.mark.asyncio
async def test_live_user_unit_status_ok_and_missing(monkeypatch):
    """`systemctl --user cat` rc==0 AND the unit running -> ok; rc!=0 (system unit
    / not installed) -> no_user_unit.

    Loadedness alone is not enough: `ok` means a restart replaces the gateway we
    are in, so the classifier also requires `is-active`.
    """
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="linux"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
    )

    async def loaded_and_running(cmd, **kw):
        assert cmd[:2] == ["systemctl", "--user"]
        if "is-active" in cmd:
            return (0, "active", "")
        assert "cat" in cmd
        return (0, "# unit contents", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", loaded_and_running)
    assert await mod._live_user_unit_status() == "ok"

    async def cat_missing(cmd, **kw):
        return (1, "", "No files found for kirocrew-gateway.service.")

    monkeypatch.setattr(runtime_mod, "_run_cmd", cat_missing)
    assert await mod._live_user_unit_status() == "no_user_unit"
