"""Dev Fleet restart identity, the fleet cache, the manifest and the serving install.

Covers the restart start-id handshake, sync step labels, fleet cache eviction and
coalescing, the manifest's platform declaration, process-tree kills and the
serving-install reason.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dev_fleet_app_helpers import _reset_make_live_committed_latch  # noqa: F401 - fixture
from dev_fleet_app_helpers import _reset_shutdown_admission_state  # noqa: F401 - fixture
from dev_fleet_app_helpers import _sweep_staged_sync_tempdirs  # noqa: F401 - fixture
from dev_fleet_app_helpers import _venv_maps_to_the_synced_checkout  # noqa: F401 - fixture
from dev_fleet_app_helpers import (
    _POSIX_ONLY,
    _mk_make_live_wt,
    _reap_cleanup_paths,
    _stub_make_live,
)

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.dev_fleet import fleet_state as fleet_state_mod
from kiro_crew.apps.builtins.dev_fleet import live as live_mod
from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.apps.builtins.dev_fleet import worktree_ops as worktree_ops_mod

# =============================================================================
# Task: restart identity handshake + sync step labels
# =============================================================================


def test_parse_step_marker_index_and_label():
    from kiro_crew.apps.builtins.dev_fleet.server import _parse_step_marker

    assert _parse_step_marker("::step::0::Pull") == (0, "Pull")
    assert _parse_step_marker("::step::3::pip install") == (3, "pip install")


def test_parse_step_marker_non_marker_and_partial():
    from kiro_crew.apps.builtins.dev_fleet.server import _parse_step_marker

    assert _parse_step_marker("regular build output") == (None, None)
    assert _parse_step_marker("::step::") == (None, None)  # no index or label
    assert _parse_step_marker("::step::2::") == (2, None)  # empty label
    assert _parse_step_marker("::step::x::npm ci") == (None, "npm ci")  # bad idx


@pytest.mark.asyncio
async def test_run_endpoint_exposes_step_label():
    """/run returns the server-tracked step + step_label so the UI can name the
    CURRENT sync step ("npm ci") instead of a bare spinner. The label survives
    the 60-line output tail window because it is stored on the run entry."""
    rid = "steplabel-rid"
    async with mod._RUNS_LOCK:
        mod._RUNS[rid] = {
            "status": "running",
            "exit_code": None,
            "label": "sync",
            "output": ["::step::3::npm ci"],
            "started": time.time(),
            "step": 3,
            "step_label": "npm ci",
        }
    try:
        req = MagicMock()
        req.query = {"id": rid}
        resp = await mod.api_dev_fleet_run(req)
        payload = json.loads(resp.text)
        assert payload["step"] == 3
        assert payload["step_label"] == "npm ci"
    finally:
        async with mod._RUNS_LOCK:
            del mod._RUNS[rid]


@pytest.mark.asyncio
async def test_gateway_start_id_reads_monotonic():
    """_gateway_start_id reads the unit's ExecMainStartTimestampMonotonic."""

    async def fake_run_cmd(cmd, **kw):
        assert "show" in cmd and "--value" in cmd
        assert any("ExecMainStartTimestampMonotonic" in c for c in cmd)
        return (0, "123456789\n", "")

    with (
        patch.object(runtime_mod, "_run_cmd", side_effect=fake_run_cmd),
        patch.object(live_mod, "sys", MagicMock(platform="linux")),
        patch.object(
            live_mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
        ),
    ):
        assert await mod._gateway_start_id() == "123456789"


@pytest.mark.asyncio
async def test_gateway_start_id_none_on_non_linux():
    """Non-Linux / no systemctl degrades to None (no hang in 'restarting')."""
    with (
        patch.object(live_mod, "sys", MagicMock(platform="darwin")),
        patch.object(live_mod, "shutil", MagicMock(which=MagicMock(return_value=None))),
    ):
        assert await mod._gateway_start_id() is None


@pytest.mark.asyncio
async def test_gateway_start_id_none_on_zero_empty_or_error():
    """A '0'/empty stamp or a failed probe all normalise to None."""
    with (
        patch.object(live_mod, "sys", MagicMock(platform="linux")),
        patch.object(
            live_mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
        ),
    ):
        with patch.object(
            runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "0\n", "")
        ):
            assert await mod._gateway_start_id() is None
        with patch.object(
            runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "\n", "")
        ):
            assert await mod._gateway_start_id() is None
        with patch.object(
            runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(1, "", "err")
        ):
            assert await mod._gateway_start_id() is None


@pytest.mark.asyncio
async def test_restart_gateway_returns_start_id_captured_before_restart():
    """restart-gateway captures the unit's start identity BEFORE scheduling the
    detached restart and returns it, so the frontend waits for a DIFFERENT one
    rather than 'a 200 came back'."""
    calls: list[list[str]] = []

    async def mock_run_cmd(cmd, **kw):
        calls.append(cmd)
        if "is-active" in cmd:
            return (0, "active\n", "")
        if "show" in cmd:  # _gateway_start_id identity probe
            return (0, "555000\n", "")
        return (0, "", "")  # systemd-run

    with (
        patch.object(runtime_mod, "_run_cmd", side_effect=mock_run_cmd),
        patch.object(live_mod, "sys", MagicMock(platform="linux")),
        patch.object(
            live_mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
        ),
    ):
        result = await mod._restart_gateway()

    assert result["ok"] is True
    assert result["start_id"] == "555000"
    # The identity probe MUST run before the detached restart is scheduled.
    show_idx = next(i for i, c in enumerate(calls) if "show" in c)
    run_idx = next(i for i, c in enumerate(calls) if "systemd-run" in c)
    assert show_idx < run_idx


@pytest.mark.asyncio
async def test_restart_gateway_start_id_none_safe_when_probe_fails():
    """A failed identity probe still restarts, with start_id=None — the frontend
    then degrades to reload-on-first-response instead of hanging."""

    async def mock_run_cmd(cmd, **kw):
        if "is-active" in cmd:
            return (0, "active\n", "")
        if "show" in cmd:
            return (1, "", "boom")  # probe fails
        return (0, "", "")  # systemd-run

    with (
        patch.object(runtime_mod, "_run_cmd", side_effect=mock_run_cmd),
        patch.object(live_mod, "sys", MagicMock(platform="linux")),
        patch.object(
            live_mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
        ),
    ):
        result = await mod._restart_gateway()

    assert result["ok"] is True
    assert result["start_id"] is None


@pytest.mark.asyncio
async def test_api_health_includes_start_id():
    """/health carries the current start identity for the restart handshake."""
    with patch.object(live_mod, "_gateway_start_id", new_callable=AsyncMock, return_value="98765"):
        resp = await mod.api_health(MagicMock())
    payload = json.loads(resp.text)
    assert resp.status == 200
    assert payload["status"] == "ok"
    assert payload["start_id"] == "98765"


@pytest.mark.asyncio
async def test_api_health_start_id_none_safe():
    """/health stays 200/ok with start_id=None when identity is unavailable."""
    with patch.object(live_mod, "_gateway_start_id", new_callable=AsyncMock, return_value=None):
        resp = await mod.api_health(MagicMock())
    payload = json.loads(resp.text)
    assert resp.status == 200
    assert payload["status"] == "ok"
    assert payload["start_id"] is None


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_returns_start_id(monkeypatch, tmp_path):
    """A real cutover captures + returns the pre-restart start identity so the
    dashboard reuses the same restart handshake."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        if "show" in cmd and any("ExecMainStartTimestampMonotonic" in c for c in cmd):
            return (0, "777111\n", "")
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True and res.get("cutover") is True
    assert res["start_id"] == "777111"
    # The identity probe must precede the detached restart.
    show_idx = next(
        i for i, c in enumerate(calls) if "show" in c and any("ExecMainStart" in x for x in c)
    )
    run_idx = next(
        i for i, c in enumerate(calls) if c[:2] == ["systemd-run", "--user"] and "restart" in c
    )
    assert show_idx < run_idx


def test_health_registered_on_proxied_api_path():
    """The restart handshake is only reachable if the identity handler is
    registered on the PROXIED /api namespace.

    The browser reaches this backend solely through the gateway proxy, which
    matches /apps/dev-fleet/api/{path} and forwards to /api/{path}; a bare
    /apps/dev-fleet/health is NOT proxied. So /api/health (not just the
    HMAC-exempt internal /health) is what makes the handshake work on the live
    gateway. Guard both registrations against a silent regression.
    """
    app = mod.create_app()
    paths = {
        r.resource.canonical
        for r in app.router.routes()
        if r.method == "GET" and r.resource is not None
    }
    assert "/health" in paths  # gateway-internal liveness poll (exempt)
    assert "/api/health" in paths  # proxied path the dashboard actually polls


# =============================================================================
# fleet cache: eviction on removal + single-flight rebuilds
# =============================================================================


@pytest.fixture
def _clean_fleet_cache():
    """Isolate the module-level fleet cache from other tests."""
    saved = dict(mod._FLEET_CACHE)
    saved_inflight = mod._FLEET_INFLIGHT
    saved_epoch = mod._FLEET_EPOCH
    saved_tombs = dict(mod._FLEET_TOMBSTONES)
    mod._FLEET_CACHE.update({"data": None, "ts": 0.0})
    fleet_state_mod._FLEET_INFLIGHT = None
    fleet_state_mod._FLEET_EPOCH = 0
    fleet_state_mod._FLEET_TOMBSTONES = {}
    try:
        yield
    finally:
        mod._FLEET_CACHE.clear()
        mod._FLEET_CACHE.update(saved)
        fleet_state_mod._FLEET_INFLIGHT = saved_inflight
        fleet_state_mod._FLEET_EPOCH = saved_epoch
        fleet_state_mod._FLEET_TOMBSTONES = saved_tombs


def test_fleet_forget_evicts_row(_clean_fleet_cache):
    """A removed worktree must vanish from the cached snapshot immediately.

    Without this the stale-while-revalidate cache serves the pre-removal
    snapshot, so the pruned row keeps rendering for a whole rebuild.
    """
    mod._FLEET_CACHE.update(
        {
            "data": {"worktrees": [{"name": "main"}, {"name": "wt-gone"}], "base_branch": "main"},
            "ts": time.monotonic(),
        }
    )
    mod._fleet_forget("wt-gone")

    names = [w["name"] for w in mod._FLEET_CACHE["data"]["worktrees"]]
    assert names == ["main"]
    # Unrelated snapshot fields survive the surgical eviction.
    assert mod._FLEET_CACHE["data"]["base_branch"] == "main"
    # Timestamp zeroed so the next read schedules a rebuild for the rest.
    assert mod._FLEET_CACHE["ts"] == 0.0


def test_fleet_forget_no_cache_is_noop(_clean_fleet_cache):
    """Removal before any snapshot exists must not crash or fabricate one."""
    mod._fleet_forget("wt-gone")
    assert mod._FLEET_CACHE["data"] is None


def test_fleet_forget_does_not_mutate_served_dict(_clean_fleet_cache):
    """The old dict may still be mid-serialization in a concurrent response."""
    served = {"worktrees": [{"name": "main"}, {"name": "wt-gone"}]}
    mod._FLEET_CACHE.update({"data": served, "ts": time.monotonic()})
    mod._fleet_forget("wt-gone")
    assert [w["name"] for w in served["worktrees"]] == ["main", "wt-gone"]
    assert mod._FLEET_CACHE["data"] is not served


@pytest.mark.asyncio
async def test_worktree_remove_evicts_from_cache(_clean_fleet_cache):
    """_worktree_remove is the single choke point for ALL removal paths
    (manual remove, each prune worker, the auto-prune reaper)."""
    mod._FLEET_CACHE.update(
        {
            "data": {"worktrees": [{"name": "main"}, {"name": "wt-x"}]},
            "ts": time.monotonic(),
        }
    )
    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new=AsyncMock(
                return_value=({"path": "/repo/wt-x", "branch": "feat/x", "is_main": False}, None)
            ),
        ),
        patch.object(live_mod, "_live_worktree_path", new=AsyncMock(return_value=None)),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new=AsyncMock(return_value=False)),
        patch.object(
            fleet_state_mod, "_pr_status_cached", new=AsyncMock(return_value={"state": "MERGED"})
        ),
        patch.object(repository_mod, "_own_commits_count", new=AsyncMock(return_value=0)),
        patch.object(fleet_state_mod, "_fetch_pr_head_oid", new=AsyncMock(return_value="deadbeef")),
        patch.object(fleet_state_mod, "_head_contained_in_pr", new=AsyncMock(return_value=True)),
        patch.object(repository_mod, "_git", new=AsyncMock(return_value="deadbeef")),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new=AsyncMock(return_value=(0, "", ""))),
    ):
        res = await mod._worktree_remove("wt-x")

    assert res["ok"] is True
    assert [w["name"] for w in mod._FLEET_CACHE["data"]["worktrees"]] == ["main"]


@pytest.mark.asyncio
async def test_inflight_rebuild_cannot_resurrect_an_evicted_worktree(_clean_fleet_cache):
    """A rebuild that started BEFORE a removal must not put the row back.

    Such a build read git before the worktree was removed, so its snapshot still
    contains it. Storing that verbatim would undo the eviction and the deleted
    row would reappear.
    """
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _slow_build():
        entered.set()
        await release.wait()
        # The pre-removal view of git: wt-gone still present.
        return {"worktrees": [{"name": "main"}, {"name": "wt-gone"}]}

    with patch.object(fleet_state_mod, "_build_fleet", new=_slow_build):
        task = mod._fleet_rebuild_task()
        # flake-ok: pre-existing unbounded wait for the stubbed build to start, moved verbatim; tracked for a de-flake pass
        await entered.wait()
        # Removal lands while that build is in flight.
        mod._fleet_forget("wt-gone")
        release.set()
        built = await task

    assert [w["name"] for w in built["worktrees"]] == ["main"]
    assert [w["name"] for w in mod._FLEET_CACHE["data"]["worktrees"]] == ["main"]


@pytest.mark.asyncio
async def test_fresh_request_coalescing_onto_a_racing_build_still_omits_the_row(
    _clean_fleet_cache,
):
    """The post-removal `fresh=1` refresh may coalesce onto a build that started
    BEFORE the removal. That is safe only because the build re-applies the
    eviction — otherwise the request would answer with the deleted row."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _slow_build():
        entered.set()
        await release.wait()
        return {"worktrees": [{"name": "main"}, {"name": "wt-gone"}]}

    with patch.object(fleet_state_mod, "_build_fleet", new=_slow_build):
        background = mod._fleet_rebuild_task()
        # flake-ok: pre-existing unbounded wait for the stubbed build to start, moved verbatim; tracked for a de-flake pass
        await entered.wait()
        mod._fleet_forget("wt-gone")
        waiter = asyncio.create_task(mod._fleet_refresh())
        # flake-ok: pre-existing single yield so the waiter reaches the shared build, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        release.set()
        served = await waiter
        await background

    assert [w["name"] for w in served["worktrees"]] == ["main"]


@pytest.mark.asyncio
async def test_tombstones_are_reaped_by_a_later_build(_clean_fleet_cache):
    """Tombstones must not accumulate: once a build that started after the
    eviction completes, git does not report the worktree and the entry is dead
    weight. A stale tombstone would also hide a worktree later re-created under
    the same name."""
    mod._fleet_forget("wt-gone")
    assert "wt-gone" in mod._FLEET_TOMBSTONES

    with patch.object(
        fleet_state_mod,
        "_build_fleet",
        new=AsyncMock(return_value={"worktrees": [{"name": "main"}]}),
    ):
        await mod._fleet_refresh()
    assert mod._FLEET_TOMBSTONES == {}

    # Re-created under the same name: no stale tombstone hides it.
    with patch.object(
        fleet_state_mod,
        "_build_fleet",
        new=AsyncMock(return_value={"worktrees": [{"name": "main"}, {"name": "wt-gone"}]}),
    ):
        again = await mod._fleet_refresh()
    assert [w["name"] for w in again["worktrees"]] == ["main", "wt-gone"]


@pytest.mark.asyncio
async def test_fleet_refresh_coalesces_concurrent_builds(_clean_fleet_cache):
    """Concurrent rebuilds share ONE build.

    A rebuild costs a `gh pr` round-trip per branch, so parallel `fresh=1`
    requests (Refresh clicked twice, several row actions finishing together)
    must not each start their own.
    """
    calls = 0
    gate = asyncio.Event()

    async def _slow_build():
        nonlocal calls
        calls += 1
        await gate.wait()
        return {"worktrees": [{"name": "main"}]}

    with patch.object(fleet_state_mod, "_build_fleet", new=_slow_build):
        waiters = [asyncio.create_task(mod._fleet_refresh()) for _ in range(4)]
        # flake-ok: pre-existing single yield so every waiter reaches the shared build, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)  # let every waiter reach the shared task
        gate.set()
        results = await asyncio.gather(*waiters)

    assert calls == 1
    assert all(r == {"worktrees": [{"name": "main"}]} for r in results)
    assert mod._FLEET_CACHE["data"] == {"worktrees": [{"name": "main"}]}


@pytest.mark.asyncio
async def test_fleet_cached_serves_stale_and_schedules_rebuild(_clean_fleet_cache):
    """Past the TTL the cached read stays non-blocking but does trigger a rebuild."""
    mod._FLEET_CACHE.update(
        {
            "data": {"worktrees": [{"name": "stale"}]},
            "ts": time.monotonic() - (mod._FLEET_TTL + 1),
        }
    )
    with patch.object(
        fleet_state_mod,
        "_build_fleet",
        new=AsyncMock(return_value={"worktrees": [{"name": "fresh"}]}),
    ):
        served = await mod._fleet_cached()
        assert served == {"worktrees": [{"name": "stale"}]}
        assert mod._FLEET_INFLIGHT is not None
        await mod._FLEET_INFLIGHT
    assert mod._FLEET_CACHE["data"] == {"worktrees": [{"name": "fresh"}]}


@pytest.mark.asyncio
async def test_fleet_cached_background_failure_is_swallowed(_clean_fleet_cache):
    """A failed background rebuild keeps serving the last good snapshot and must
    not surface as an unretrieved task exception."""
    mod._FLEET_CACHE.update(
        {
            "data": {"worktrees": [{"name": "stale"}]},
            "ts": time.monotonic() - (mod._FLEET_TTL + 1),
        }
    )
    with patch.object(
        fleet_state_mod, "_build_fleet", new=AsyncMock(side_effect=RuntimeError("git blew up"))
    ):
        served = await mod._fleet_cached()
        assert served == {"worktrees": [{"name": "stale"}]}
        task = mod._FLEET_INFLIGHT
        assert task is not None
        await asyncio.gather(task, return_exceptions=True)
    assert task.exception() is not None
    assert mod._FLEET_CACHE["data"] == {"worktrees": [{"name": "stale"}]}


# --- manifest platform declaration ---
def test_manifest_declares_every_platform_the_app_runs_on():
    """`platform.os` summarises the whole app, and the non-pod half (fleet view,
    Provision, Sync, Rebase, Prune) is git + filesystem work that runs anywhere.

    Pinned because both narrower answers misinform: `["linux"]` reads as "does
    not run on macOS", and omitting the block falls back to the implicit
    ``["macos", "linux"]`` default, which silently drops Windows.
    """
    manifest = json.loads((Path(mod.__file__).parent / "app.json").read_text(encoding="utf-8"))
    assert manifest["platform"]["os"] == ["macos", "linux", "windows"]

    # The pod requirement is carried in the UI copy, not the manifest gate. It
    # must track reality: pods run on Linux (systemd --user) AND macOS
    # (launchd) — with no enforced resource ceiling on macOS — and Make Live
    # stages its pointer on every platform, restarting automatically only under
    # a systemd --user unit or a macOS LaunchAgent (``live._service_manager``).
    # This test guards the manifest against lying in either direction.
    assert any(
        "launchd" in h and "Linux" in h for h in manifest["highlights"]
    ), "the highlight must state pods' per-platform reality (Linux systemd + macOS launchd)"
    assert any(
        "Make Live stages its pointer on every platform" in h
        and "systemd --user" in h
        and "LaunchAgent" in h
        for h in manifest["highlights"]
    ), "the manifest copy must state Make Live's per-platform restart reality"


def test_declared_platforms_all_resolve_to_a_real_sys_platform():
    """Every declared name must map to a sys.platform value.

    An unmapped name is silently accepted into the list and then never matches,
    so a declaration can claim a platform the gate rejects. `windows` was in
    exactly that state until the mapping row landed.
    """
    from kiro_crew.apps.manifest import PlatformConfig

    manifest = json.loads((Path(mod.__file__).parent / "app.json").read_text(encoding="utf-8"))
    cfg = PlatformConfig(os=manifest["platform"]["os"])
    for sys_platform in ("darwin", "linux", "win32"):
        assert cfg.supports_platform(sys_platform), sys_platform


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_sync_builds_and_stages_under_one_lock_holder(monkeypatch, tmp_path):
    """Pull+Build must build and stage inside ONE locked step.

    Without a staging step, an older target revision whose build still writes
    ``website/dist`` in place would empty and rewrite the assets a live gateway
    serves through the dev link ensure_dev_dist_symlink() makes. The step runs under the Dev Fleet
    backend's OWN interpreter with the target repo passed as an argument:
    resolving the helper from the target would make the step's existence
    contingent on the pulled revision carrying it, so an older target would turn
    the whole Pull+Build into an ImportError.
    """
    repo = tmp_path / "mainrepo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("")
    monkeypatch.setattr(repository_mod, "MAIN_REPO", str(repo))
    monkeypatch.setattr(worktree_ops_mod, "_SYNC_RID", None)

    async def fake_remote():
        return "origin"

    monkeypatch.setattr(repository_mod, "_upstream_remote", fake_remote, raising=False)
    monkeypatch.setattr(runtime_mod, "_trusted_bin", lambda n: f"/usr/bin/{n}")
    argvs: list[list[str]] = []

    def fake_sandboxed(argv, mode, env=None):
        argvs.append(list(argv))
        return list(argv), dict(env or {}), None

    monkeypatch.setattr(worktree_ops_mod, "sandboxed_spawn_argv", fake_sandboxed)

    async def fake_run_cmd(cmd, **kw):
        return 0, "main", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)

    async def fake_start_run(label, cmd, **kw):
        # The real run removes these when it ends; the stub must too.
        _reap_cleanup_paths(kw.get("cleanup_paths"))
        return "rid-stage"

    monkeypatch.setattr(runtime_mod, "_start_run", fake_start_run)

    res = await mod._sync_start_locked()
    assert res.get("ok"), res

    def _index(pred) -> int:
        for i, a in enumerate(argvs):
            if pred(a):
                return i
        raise AssertionError(f"step not found in {argvs}")

    # Build and stage are ONE step so a single lock holder spans both: the build
    # swaps a new tree into website/dist, and a peer flow copying concurrently
    # could copy half of each.
    stage_i = _index(lambda a: any("build_and_stage" in x for x in a))
    # THIS backend's interpreter, not the target checkout's: the logic is
    # revision-independent, while resolving it from the target would make the
    # step's very existence contingent on the pulled revision already carrying
    # build_and_stage, turning an older target into an ImportError that fails the
    # whole Pull+Build. The repo to build is passed as an argument instead.
    assert argvs[stage_i][0] == sys.executable
    assert str(repo) in argvs[stage_i], "the target repo must be passed explicitly"
    # The build+stage child reads its args positionally: sys.argv[1]=repo,
    # sys.argv[2]=npm, sys.argv[3]=git. Both binaries are the trusted _trusted_bin
    # paths (stubbed here to /usr/bin/<name>), passed through explicitly rather
    # than re-resolved in the child. The git path is a later addition (the
    # read-only build-source fingerprint), so npm is now the
    # second-to-last arg and git the last -- assert each trusted path reaches the
    # spawn at the position its child consumes, not merely that one is last.
    assert argvs[stage_i][4].endswith("npm"), "the trusted npm path is passed through (argv[2])"
    assert argvs[stage_i][5].endswith("git"), "the trusted git path is passed through (argv[3])"
    assert not any(
        a[1:] == ["run", "build", "--prefix", "website"] for a in argvs
    ), "a separate unlocked npm build step would reintroduce the race"
    # npm ci does not touch website/dist, so it stays its own step.
    ci_i = _index(lambda a: a[1:] == ["ci", "--prefix", "website"])
    assert ci_i < stage_i


def test_kill_tree_reaps_a_descendant_that_escaped_the_process_group():
    """A new-session descendant is outside the group, so killpg alone misses it."""
    from kiro_crew.apps.builtins.dev_fleet import server as dev

    killed: list[int] = []

    with (
        patch.object(platform_compat, "process_descendants", return_value=[222]),
        patch.object(
            platform_compat,
            "kill_process_tree",
            side_effect=lambda pid, *a, **k: killed.append(pid) or True,
        ),
    ):
        dev._kill_tree_sync(111)

    assert killed == [111, 222], "the group kill must run first, then each escaped descendant"


def test_kill_tree_enumerates_descendants_before_killing_anything():
    """Ordering is the whole mechanism.

    A kill reparents survivors to init and erases the PPID links, so a snapshot
    taken after the kill cannot see the processes that escaped.
    """
    from kiro_crew.apps.builtins.dev_fleet import server as dev

    events: list[str] = []

    with (
        patch.object(
            platform_compat,
            "process_descendants",
            side_effect=lambda pid: events.append("enumerate") or [222],
        ),
        patch.object(
            platform_compat,
            "kill_process_tree",
            side_effect=lambda pid, *a, **k: events.append(f"kill{pid}") or True,
        ),
    ):
        dev._kill_tree_sync(111)

    assert events[0] == "enumerate", f"enumeration must precede any kill: {events}"
    assert events == ["enumerate", "kill111", "kill222"]


def test_kill_tree_survives_an_already_dead_descendant():
    """The group kill usually reaps descendants; a dead pid must not raise."""
    from kiro_crew.apps.builtins.dev_fleet import server as dev

    def _kill(pid, *a, **k):
        if pid == 222:
            raise ProcessLookupError(pid)
        return True

    with (
        patch.object(platform_compat, "process_descendants", return_value=[222, 333]),
        patch.object(platform_compat, "kill_process_tree", side_effect=_kill) as km,
    ):
        dev._kill_tree_sync(111)

    # 333 is still attempted after 222's ProcessLookupError.
    assert [c.args[0] for c in km.call_args_list] == [111, 222, 333]


def test_live_program_missing_reason_names_the_non_destructive_repairs():
    """`service install` rewrites the whole plist, discarding operator env.

    Naming it as THE repair would contradict the reason this reconcile exists, so
    the guidance points at the two routes that leave the agent definition alone.
    """
    reason = mod._make_live_status_error("live_program_missing")

    assert "Make live" in reason
    assert "source checkout" in reason
    # The destructive route may be mentioned as a contrast, never as the remedy.
    assert "discard" in reason


# --- serving install vs managed checkout --------------------------------------


def test_serving_install_reason_is_silent_for_a_source_install():
    """The normal case: the package answering these routes lives in the checkout.

    Asserted against the REAL package location rather than a fixture, so the
    check cannot pass by accident on a layout that does not exist.
    """
    pkg = Path(mod.__file__).resolve().parents[3]

    assert mod._serving_install_reason_sync(str(pkg.parents[1]), ()) is None


def test_serving_install_reason_is_silent_when_the_checkout_is_the_package_dir():
    """A managed path that IS the serving package is not a mismatch either."""
    pkg = Path(mod.__file__).resolve().parents[3]

    assert mod._serving_install_reason_sync(str(pkg), ()) is None


def test_serving_install_reason_is_silent_after_make_live_onto_a_worktree(tmp_path):
    """Make live points the gateway at a LINKED worktree, outside the primary
    checkout. Warning about a state this app just created — and already labels
    via `is_live` — would train the user to dismiss the takeover signal.
    """
    pkg = Path(mod.__file__).resolve().parents[3]
    serving_checkout = str(pkg.parents[1])

    reason = mod._serving_install_reason_sync(
        str(tmp_path),  # primary checkout: somewhere else
        (str(tmp_path / "other-wt"), serving_checkout),
    )

    assert reason is None


def test_serving_install_reason_names_both_installs_and_a_remedy(tmp_path):
    """The silent-wrong-answer case: managing checkouts, running none of them.

    Every Dev Fleet control keeps reporting success here, so this string is the
    only thing that can tell the user the pulled code is not the running code —
    which makes naming a next step part of the contract, not decoration.
    """
    (tmp_path / ".git").mkdir()

    reason = mod._serving_install_reason_sync(
        str(tmp_path), (str(tmp_path / "wt-a"), str(tmp_path / "wt-b"))
    )

    assert reason is not None
    # Both sides must be named — one path alone does not identify the mismatch.
    assert tmp_path.name in reason
    assert "kiro_crew" in reason
    assert "Make live" in reason
    # Problem first, action before the paths: a warn banner that leads with two
    # absolute paths and buries the remedy at the end gets skimmed.
    assert reason.startswith("This dashboard is served by a different install")
    assert reason.index("Make live") < reason.index("Serving now:")


def test_serving_install_reason_is_silent_with_no_checkout_to_manage(tmp_path):
    """A path that is not a checkout is not something Dev Fleet manages.

    A desktop-bundle or pip install with no source checkout is the out-of-the-box
    case; warning it to "start the gateway from <path>" names a directory that is
    not there, and a dead-end instruction on every visit trains the signal away.
    """
    assert mod._serving_install_reason_sync(str(tmp_path / "absent"), ()) is None
    # Present but not a checkout is equally unmanageable.
    (tmp_path / "empty").mkdir()
    assert mod._serving_install_reason_sync(str(tmp_path / "empty"), ()) is None


def test_serving_install_reason_accepts_a_linked_worktree_dot_git_file(tmp_path):
    """A linked worktree's `.git` is a FILE, so existence is the right test."""
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")

    assert mod._serving_install_reason_sync(str(tmp_path), ()) is not None


def test_serving_install_reason_skips_unresolvable_entries(tmp_path):
    """A bad path must be skipped, not abort the scan or crash the payload."""
    pkg = Path(mod.__file__).resolve().parents[3]

    # The poison entry comes FIRST; the healthy one after it must still be seen.
    assert mod._serving_install_reason_sync("\x00not-a-path", (str(pkg.parents[1]),)) is None
    # And with nothing healthy anywhere, it still returns without raising.
    (tmp_path / ".git").mkdir()
    assert mod._serving_install_reason_sync(str(tmp_path), ()) is not None


@pytest.mark.asyncio
async def test_serving_install_reason_resolves_paths_off_the_event_loop(monkeypatch):
    """The resolution is filesystem IO, so it must not run on the loop.

    Memoized on the checkout set as well: /fleet is polled, and repeating the
    walk on every poll is what would make a network-backed checkout stall the
    gateway.
    """
    monkeypatch.setattr(fleet_state_mod, "_SERVING_REASON", None)
    monkeypatch.setattr(repository_mod, "MAIN_REPO", "/nowhere/at/all")
    calls: list[tuple] = []

    def _spy(main_repo: str, managed: tuple) -> str | None:
        calls.append((main_repo, managed))
        return "mismatch"

    monkeypatch.setattr(fleet_state_mod, "_serving_install_reason_sync", _spy)
    loop = asyncio.get_running_loop()
    offloaded: list[bool] = []
    real_executor = loop.run_in_executor

    def _tracking_executor(executor, func, *args):
        offloaded.append(True)
        return real_executor(executor, func, *args)

    monkeypatch.setattr(loop, "run_in_executor", _tracking_executor)
    wts = [{"path": "/wt/a"}, {"path": "/wt/b"}, {"no_path": 1}]

    assert await mod._serving_install_reason(wts) == "mismatch"
    assert await mod._serving_install_reason(wts) == "mismatch"

    assert calls == [("/nowhere/at/all", ("/wt/a", "/wt/b"))], "second call must be memoized"
    assert offloaded == [True], "the blocking work must go through an executor"


@pytest.mark.asyncio
async def test_serving_install_reason_recomputes_when_the_checkout_set_changes(monkeypatch):
    """A new worktree can make a foreign serving install managed, so
    the memo must be keyed on the set, not just on MAIN_REPO."""
    monkeypatch.setattr(fleet_state_mod, "_SERVING_REASON", None)
    monkeypatch.setattr(repository_mod, "MAIN_REPO", "/nowhere")
    seen: list[tuple] = []
    monkeypatch.setattr(
        fleet_state_mod,
        "_serving_install_reason_sync",
        lambda repo, managed: seen.append(managed) or "r",
    )

    await mod._serving_install_reason([{"path": "/wt/a"}])
    await mod._serving_install_reason([{"path": "/wt/a"}, {"path": "/wt/b"}])

    assert seen == [("/wt/a",), ("/wt/a", "/wt/b")]
