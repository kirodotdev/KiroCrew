"""Dev Fleet auto-prune and prune runs.

Covers the auto-prune config, the reaper, the per-item prune run states, its
concurrency cap and name handling, pod-up, skill registration and the removal
refusals for the live and the running checkout.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dev_fleet_app_helpers import _reset_make_live_committed_latch  # noqa: F401 - fixture
from dev_fleet_app_helpers import _reset_shutdown_admission_state  # noqa: F401 - fixture
from dev_fleet_app_helpers import _sweep_staged_sync_tempdirs  # noqa: F401 - fixture
from dev_fleet_app_helpers import _venv_maps_to_the_synced_checkout  # noqa: F401 - fixture

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.dev_fleet import fleet_state as fleet_state_mod
from kiro_crew.apps.builtins.dev_fleet import live as live_mod
from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.apps.builtins.dev_fleet import worktree_ops as worktree_ops_mod
from kiro_crew.testing.wait import async_wait_until


# =============================================================================
# auto-prune reaper
# =============================================================================
def test_auto_prune_cfg_disabled_by_default():
    with patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={}):
        enabled, interval = mod._auto_prune_cfg()
    assert enabled is False
    assert interval == mod._AUTO_PRUNE_DEFAULT_INTERVAL_S


def test_auto_prune_cfg_enabled_with_interval_floor():
    with patch.object(
        repository_mod,
        "_load_dev_fleet_cfg",
        return_value={"auto_prune": {"enabled": True, "interval_secs": 5}},
    ):
        enabled, interval = mod._auto_prune_cfg()
    assert enabled is True
    # 5s is below the floor -> clamped up to the minimum.
    assert interval == mod._AUTO_PRUNE_MIN_INTERVAL_S


def test_auto_prune_cfg_bad_interval_falls_back():
    with patch.object(
        repository_mod,
        "_load_dev_fleet_cfg",
        return_value={"auto_prune": {"enabled": True, "interval_secs": "nope"}},
    ):
        enabled, interval = mod._auto_prune_cfg()
    assert enabled is True
    assert interval == mod._AUTO_PRUNE_DEFAULT_INTERVAL_S


@pytest.mark.asyncio
async def test_auto_prune_once_removes_merged_only_and_records_failures():
    """Acts ONLY on code=='merged' candidates (stale-empty is skipped); splits
    the merged results into removed/failed."""
    candidates = {
        "candidates": [
            {"name": "wt-merged", "code": "merged"},
            {"name": "wt-bad", "code": "merged"},
            {"name": "wt-stale-empty", "code": "empty"},  # must be skipped
        ]
    }
    seen = []

    async def _fake_remove(name, force=False, _caller="handler"):
        assert force is False  # reaper never force-removes
        assert _caller == "reaper"
        seen.append(name)
        return {"ok": True} if name == "wt-merged" else {"ok": False, "error": "nope"}

    with (
        patch.object(
            worktree_ops_mod, "_prune_candidates", new_callable=AsyncMock, return_value=candidates
        ),
        patch.object(worktree_ops_mod, "_worktree_remove", side_effect=_fake_remove),
    ):
        res = await mod._auto_prune_once()
    assert res["removed"] == ["wt-merged"]
    assert res["failed"] == [{"name": "wt-bad", "error": "nope"}]
    # stale-empty is never touched — unattended auto-prune is merged-only.
    assert "wt-stale-empty" not in seen


@pytest.mark.asyncio
async def test_auto_prune_once_never_reaps_closed_candidates():
    """REGRESSION PIN for hard constraint (a): the unattended reaper MUST stay
    MERGED-only. A CLOSED-PR worktree routinely holds the only copy of work
    that never landed, so silently reaping it on a timer would be irrecoverable
    data loss. This pins that a `closed` candidate — even a clean one that the
    MANUAL checklist WOULD offer — is never handed to _worktree_remove by the
    reaper. If someone later adds `closed` to the reaper's filter, this fails.
    """
    candidates = {
        "candidates": [
            {"name": "wt-merged", "code": "merged"},
            {"name": "wt-closed", "code": "closed", "unmerged_commits": True},
            {"name": "wt-closed-clean", "code": "closed", "unmerged_commits": False},
        ]
    }
    seen = []

    async def _fake_remove(name, force=False, _caller="handler"):
        assert force is False  # reaper never force-removes
        assert _caller == "reaper"
        seen.append(name)
        return {"ok": True}

    with (
        patch.object(
            worktree_ops_mod, "_prune_candidates", new_callable=AsyncMock, return_value=candidates
        ),
        patch.object(worktree_ops_mod, "_worktree_remove", side_effect=_fake_remove),
    ):
        res = await mod._auto_prune_once()
    # Only the merged worktree is reaped; NEITHER closed candidate is touched.
    assert seen == ["wt-merged"]
    assert res["removed"] == ["wt-merged"]
    assert "wt-closed" not in seen
    assert "wt-closed-clean" not in seen


@pytest.mark.asyncio
async def test_auto_prune_once_survives_scan_error():
    with patch.object(
        worktree_ops_mod,
        "_prune_candidates",
        new_callable=AsyncMock,
        side_effect=RuntimeError("gh down"),
    ):
        res = await mod._auto_prune_once()
    # scan failure is surfaced (not swallowed into an empty success) so the
    # reaper can emit a SEL failure event.
    assert res["removed"] == [] and res["failed"] == []
    assert "gh down" in res["error"]


def test_auto_prune_cfg_truthy_nonbool_stays_disabled():
    """A truthy-but-non-boolean 'enabled' (e.g. the string 'false', or 1) must
    NOT arm destructive auto-prune — only literal JSON true does (Codex HIGH)."""
    for bad in ("false", "true", 1, "yes", "0"):
        with patch.object(
            repository_mod, "_load_dev_fleet_cfg", return_value={"auto_prune": {"enabled": bad}}
        ):
            enabled, _ = mod._auto_prune_cfg()
        assert enabled is False, f"{bad!r} must not enable auto-prune"


@pytest.mark.asyncio
async def test_pod_up_fails_closed_when_not_active():
    """rc==0 but the unit is not active -> fail closed (no false 'started')."""
    with (
        patch.object(
            worktree_ops_mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None
        ),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "{}", "")),
        patch.object(runtime_mod, "_load_cfg", return_value=object()),
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/repo/kirocrew-wt-x"}, None),
        ),
        patch.object(runtime_mod, "_POD_AVAILABLE", True),
        patch.object(runtime_mod.rt, "active_names", return_value=set()),
    ):
        result = await mod._pod_up("kirocrew-wt-x")
    assert result["ok"] is False
    assert "not active after start" in result["error"]


@pytest.mark.asyncio
async def test_pod_up_ok_when_active():
    """rc==0 AND the unit active -> success, parsed JSON merged in.

    The token is minted IN THIS GATEWAY PROCESS (not by the sandboxed `pod up`
    child, whose foreign namespace the pod refuses to certify), so the child is
    launched with --no-token and the handle's token is the gateway's mint.
    """
    run_cmd = AsyncMock(return_value=(0, '{"port": 7999, "token": ""}', ""))
    with (
        patch.object(
            worktree_ops_mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None
        ),
        patch.object(runtime_mod, "_run_cmd", run_cmd),
        patch.object(runtime_mod, "_load_cfg", return_value=object()),
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/repo/kirocrew-wt-x"}, None),
        ),
        patch.object(
            worktree_ops_mod, "_read_pin_strict", return_value=(True, "/repo/kirocrew-wt-x")
        ),
        patch.object(runtime_mod.rt, "pod_name_mutex", return_value=MagicMock()),
        patch.object(runtime_mod.rt, "derive_port", return_value=7999),
        patch.object(runtime_mod, "_sel", return_value=MagicMock()),
        patch.object(runtime_mod, "_POD_AVAILABLE", True),
        patch.object(runtime_mod.rt, "active_names", return_value={"kirocrew-wt-x"}),
        patch.object(runtime_mod.rt, "mint_token", return_value="tok-gw"),
    ):
        result = await mod._pod_up("kirocrew-wt-x")
    assert result["ok"] is True
    assert result["port"] == 7999
    assert result["token"] == "tok-gw"
    # The boot child never mints: it is told --no-token.
    assert "--no-token" in run_cmd.await_args.args[0]


@pytest.mark.asyncio
async def test_auto_prune_reaper_audits_scan_failure():
    """A failed destructive-op cycle (scan error) must still emit a SEL failure
    event — the reaper cannot silently skip auditing (Codex HIGH)."""
    events = []
    fake_sel = MagicMock()
    fake_sel.log_tool_invocation = lambda **kw: events.append(kw)

    async def _break_after_first_cycle(_secs):
        raise asyncio.CancelledError  # exit the while True after one cycle

    with (
        patch.object(worktree_ops_mod, "_auto_prune_cfg", return_value=(True, 300)),
        patch.object(
            worktree_ops_mod,
            "_auto_prune_once",
            new_callable=AsyncMock,
            return_value={"removed": [], "failed": [], "error": "gh down"},
        ),
        patch.object(runtime_mod, "_sel", return_value=fake_sel),
        # flake-ok: pre-existing worker-wide asyncio.sleep patch that ends the reaper loop after one cycle, moved verbatim; tracked for a de-flake pass
        patch("asyncio.sleep", _break_after_first_cycle),
    ):
        with pytest.raises(asyncio.CancelledError):
            await mod._auto_prune_reaper()

    assert len(events) == 1
    assert events[0]["tool_name"] == "dev_fleet_auto_prune"
    assert events[0]["outcome"] == "failure"
    assert "gh down" in events[0]["error"]


# =============================================================================
# parallel prune
# =============================================================================
async def _await_prune_idle(timeout: float | None = None) -> None:
    """Wait until the background prune task drains (running -> False)."""
    await async_wait_until(
        lambda: not mod._PRUNE_STATE["running"],
        timeout=timeout,
        describe=lambda: f"prune not idle: {mod._PRUNE_STATE}",
    )


@pytest.fixture
def reset_prune_state():
    """Fresh prune locks + state bound to the CURRENT test's event loop.

    ``_PRUNE_LOCK`` / ``_GIT_MUTATION_LOCK`` are module-global asyncio.Locks and
    an asyncio.Lock raises if reused across event loops; pytest-asyncio gives
    each test its own loop, so re-create them (and clear the shared state) per
    test.
    """
    worktree_ops_mod._PRUNE_LOCK = asyncio.Lock()
    worktree_ops_mod._GIT_MUTATION_LOCK = asyncio.Lock()
    mod._PRUNE_STATE.update(
        {
            "running": False,
            "total": 0,
            "done": 0,
            "current": None,
            "results": [],
            "items": {},
        }
    )
    yield


@pytest.mark.asyncio
async def test_prune_run_per_item_states_and_failure_isolation(reset_prune_state):
    """Each item ends in a terminal per-item status; one failure never stops the
    others; the backward-compat top-level fields are all preserved."""
    names = ["wt-ok", "wt-bad-verdict", "wt-missing", "wt-remove-fail"]

    async def fake_find(nm):
        if nm == "wt-missing":
            return None, "unknown worktree: 'wt-missing'"
        return {"path": f"/wt/{nm}", "branch": f"feat/{nm}"}, None

    async def fake_prunable(path, branch):
        if path == "/wt/wt-bad-verdict":
            return {"ok": False, "code": "active"}
        return {"ok": True, "code": "merged"}

    async def fake_remove(
        nm, force=False, progress=None, _caller="handler", discard_untracked_paths=None
    ):
        # exercise the phase callback the parallel driver passes in
        if progress is not None:
            progress("stopping_pod")
            progress("removing")
        # No discard was requested for any of these names, so the driver must
        # not turn one on unbidden.
        assert discard_untracked_paths is None
        if nm == "wt-remove-fail":
            return {"ok": False, "error": "pod still active after shutdown"}
        return {"ok": True, "removed": True}

    with (
        patch.object(repository_mod, "_find_worktree", side_effect=fake_find),
        patch.object(worktree_ops_mod, "_prunable", side_effect=fake_prunable),
        patch.object(worktree_ops_mod, "_worktree_remove", side_effect=fake_remove),
    ):
        r = await mod._prune_run(names)
        assert r == {"ok": True, "total": 4}
        await _await_prune_idle()

    st = await mod._prune_status()
    # backward-compat top-level fields still present and correct
    assert st["running"] is False
    assert st["total"] == 4
    assert st["done"] == 4
    assert "current" in st
    assert len(st["results"]) == 4
    # per-item state machine
    items = st["items"]
    assert items["wt-ok"] == {"status": "done", "error": None}
    assert items["wt-bad-verdict"]["status"] == "failed"
    assert "not prunable" in items["wt-bad-verdict"]["error"]
    assert items["wt-missing"]["status"] == "failed"
    assert "unknown worktree" in items["wt-missing"]["error"]
    assert items["wt-remove-fail"]["status"] == "failed"
    assert "pod still active" in items["wt-remove-fail"]["error"]
    # failure isolation: the one healthy item completed despite 3 failures
    ok_results = [res for res in st["results"] if res.get("ok")]
    assert ok_results == [{"name": "wt-ok", "ok": True, "removed": True}]


@pytest.mark.asyncio
async def test_prune_run_exception_in_item_is_isolated(reset_prune_state):
    """An unexpected exception in one item is caught, marked failed, and does not
    wedge the batch (done still reaches total)."""
    names = ["wt-a", "wt-boom", "wt-b"]

    async def fake_find(nm):
        return {"path": f"/wt/{nm}", "branch": f"feat/{nm}"}, None

    async def fake_prunable(path, branch):
        return {"ok": True, "code": "merged"}

    async def fake_remove(
        nm, force=False, progress=None, _caller="handler", discard_untracked_paths=None
    ):
        assert discard_untracked_paths is None
        if nm == "wt-boom":
            raise RuntimeError("kaboom")
        return {"ok": True, "removed": True}

    with (
        patch.object(repository_mod, "_find_worktree", side_effect=fake_find),
        patch.object(worktree_ops_mod, "_prunable", side_effect=fake_prunable),
        patch.object(worktree_ops_mod, "_worktree_remove", side_effect=fake_remove),
    ):
        await mod._prune_run(names)
        await _await_prune_idle()

    st = await mod._prune_status()
    assert st["done"] == 3 and st["running"] is False
    assert st["items"]["wt-boom"]["status"] == "failed"
    assert "kaboom" in st["items"]["wt-boom"]["error"]
    assert st["items"]["wt-a"]["status"] == "done"
    assert st["items"]["wt-b"]["status"] == "done"


@pytest.mark.asyncio
async def test_prune_run_caps_concurrency_at_semaphore_limit(reset_prune_state, monkeypatch):
    """The expensive per-item phase runs concurrently but never exceeds
    _PRUNE_CONCURRENCY simultaneous items."""
    monkeypatch.setattr(worktree_ops_mod, "_PRUNE_CONCURRENCY", 2)
    names = [f"wt-{i}" for i in range(6)]
    inflight = 0
    peak = 0

    async def fake_find(nm):
        return {"path": f"/wt/{nm}", "branch": f"feat/{nm}"}, None

    async def fake_prunable(path, branch):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        # flake-ok: pre-existing 50 ms hold inside the stubbed prune step so items overlap, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing hold inside the stubbed prune step, moved verbatim; the test asserts the concurrency cap it exercises
        await asyncio.sleep(0.05)  # hold the semaphore slot
        inflight -= 1
        return {"ok": True, "code": "merged"}

    async def fake_remove(
        nm, force=False, progress=None, _caller="handler", discard_untracked_paths=None
    ):
        return {"ok": True, "removed": True}

    with (
        patch.object(repository_mod, "_find_worktree", side_effect=fake_find),
        patch.object(worktree_ops_mod, "_prunable", side_effect=fake_prunable),
        patch.object(worktree_ops_mod, "_worktree_remove", side_effect=fake_remove),
    ):
        await mod._prune_run(names)
        await _await_prune_idle()

    assert peak == 2, f"concurrency should reach (and not exceed) the cap of 2, saw {peak}"
    st = await mod._prune_status()
    assert st["done"] == 6
    assert all(it["status"] == "done" for it in st["items"].values())


@pytest.mark.asyncio
async def test_worktree_remove_serializes_git_mutations(reset_prune_state):
    """Concurrent removals must not overlap inside the git-mutation section — it
    is guarded by _GIT_MUTATION_LOCK so the shared MAIN_REPO .git state is
    mutated by only one worker at a time."""
    inside = 0
    overlapped = False

    async def fake_run_cmd(cmd, timeout=None, **kw):
        nonlocal inside, overlapped
        if "worktree" in cmd and "remove" in cmd:
            inside += 1
            if inside > 1:
                overlapped = True
            # flake-ok: pre-existing 50 ms hold inside the stubbed git removal so workers could overlap, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing hold inside the stubbed git removal, moved verbatim; the test asserts the lock keeps workers apart
            await asyncio.sleep(0.05)
            inside -= 1
        return (0, "", "")

    async def fake_find(name):
        return {"path": f"/wt/{name}", "branch": f"feat/{name}"}, None

    with (
        patch.object(repository_mod, "_find_worktree", side_effect=fake_find),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=0),
        patch.object(
            fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="a" * 40
        ),
        patch.object(
            fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True
        ),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="a" * 40),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", side_effect=fake_run_cmd),
    ):
        results = await asyncio.gather(
            mod._worktree_remove("wt-1", force=False),
            mod._worktree_remove("wt-2", force=False),
        )
    assert all(r.get("ok") for r in results), results
    assert overlapped is False, "git mutations overlapped — _GIT_MUTATION_LOCK not serializing"


@pytest.mark.asyncio
async def test_prune_run_deduplicates_names(reset_prune_state):
    """Duplicate names in one request must not spawn racing workers: the list
    is deduplicated (order-preserving) so the same worktree is processed
    exactly once and a duplicate can never report a spurious failure over the
    first worker's success."""
    removed: list[str] = []

    async def fake_find(nm):
        return {"path": f"/wt/{nm}", "branch": f"feat/{nm}"}, None

    async def fake_prunable(path, branch):
        return {"ok": True, "code": "merged"}

    async def fake_remove(
        nm, force=False, progress=None, _caller="handler", discard_untracked_paths=None
    ):
        assert discard_untracked_paths is None
        removed.append(nm)
        return {"ok": True, "removed": True}

    with (
        patch.object(repository_mod, "_find_worktree", side_effect=fake_find),
        patch.object(worktree_ops_mod, "_prunable", side_effect=fake_prunable),
        patch.object(worktree_ops_mod, "_worktree_remove", side_effect=fake_remove),
    ):
        r = await mod._prune_run(["wt-a", "wt-b", "wt-a", "wt-a"])
        assert r == {"ok": True, "total": 2}
        await _await_prune_idle()

    st = await mod._prune_status()
    assert removed.count("wt-a") == 1
    assert st["total"] == 2 and st["done"] == 2
    assert set(st["items"]) == {"wt-a", "wt-b"}
    assert all(it["status"] == "done" for it in st["items"].values())
    assert len(st["results"]) == 2
    # completed batch never leaves a finished name in ``current``
    assert st["current"] is None


@pytest.mark.asyncio
async def test_prune_run_processes_force_only_names(reset_prune_state):
    """A force-override on a kept worktree arrives in ``force_names`` disjoint
    from ``names``. It must still be processed and counted: absent from the
    work list, its ``done`` bump would have no denominator or item row,
    producing the impossible ``1/0`` counter (and a false failure toast). The
    forced item also skips the prunable-verdict recheck — ``_prunable`` is
    never consulted for it."""
    prunable_calls: list[str] = []
    removed: list[str] = []

    async def fake_find(nm):
        return {"path": f"/wt/{nm}", "branch": f"feat/{nm}"}, None

    async def fake_prunable(path, branch):
        prunable_calls.append(path)
        return {"ok": True, "code": "merged"}

    async def fake_remove(
        nm, force=False, progress=None, _caller="handler", discard_untracked_paths=None
    ):
        assert discard_untracked_paths is None
        removed.append(nm)
        return {"ok": True, "removed": True}

    with (
        patch.object(repository_mod, "_find_worktree", side_effect=fake_find),
        patch.object(worktree_ops_mod, "_prunable", side_effect=fake_prunable),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_staged_target", return_value=None),
        patch.object(worktree_ops_mod, "_worktree_remove", side_effect=fake_remove),
    ):
        # No regular candidates; a single kept worktree via force-override.
        r = await mod._prune_run([], force_names={"wt-kept"})
        assert r == {"ok": True, "total": 1}
        await _await_prune_idle()

    st = await mod._prune_status()
    # The forced worktree is in the denominator, has an item row, and is done.
    assert st["total"] == 1 and st["done"] == 1
    assert set(st["items"]) == {"wt-kept"}
    assert st["items"]["wt-kept"]["status"] == "done"
    assert removed == ["wt-kept"]
    # Forced items bypass the prunable verdict recheck entirely.
    assert prunable_calls == []


@pytest.mark.asyncio
async def test_prune_run_unions_regular_and_forced_names(reset_prune_state):
    """A mixed batch (regular candidates + a forced kept worktree) processes
    the order-preserving union: every name is counted and removed exactly
    once, and only the non-forced names go through the prunable recheck."""
    prunable_paths: list[str] = []
    removed: list[str] = []

    async def fake_find(nm):
        return {"path": f"/wt/{nm}", "branch": f"feat/{nm}"}, None

    async def fake_prunable(path, branch):
        prunable_paths.append(path)
        return {"ok": True, "code": "merged"}

    async def fake_remove(
        nm, force=False, progress=None, _caller="handler", discard_untracked_paths=None
    ):
        assert discard_untracked_paths is None
        removed.append(nm)
        return {"ok": True, "removed": True}

    with (
        patch.object(repository_mod, "_find_worktree", side_effect=fake_find),
        patch.object(worktree_ops_mod, "_prunable", side_effect=fake_prunable),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_staged_target", return_value=None),
        patch.object(worktree_ops_mod, "_worktree_remove", side_effect=fake_remove),
    ):
        r = await mod._prune_run(["wt-a", "wt-b"], force_names={"wt-forced"})
        assert r == {"ok": True, "total": 3}
        await _await_prune_idle()

    st = await mod._prune_status()
    assert st["total"] == 3 and st["done"] == 3
    assert set(st["items"]) == {"wt-a", "wt-b", "wt-forced"}
    assert all(it["status"] == "done" for it in st["items"].values())
    assert sorted(removed) == ["wt-a", "wt-b", "wt-forced"]
    # Only the two regular candidates go through the verdict recheck.
    assert sorted(prunable_paths) == ["/wt/wt-a", "/wt/wt-b"]


@pytest.mark.asyncio
async def test_worktree_remove_refuses_when_pod_reactivates_before_mutation(reset_prune_state):
    """TOCTOU guard: pod inactivity is verified before _GIT_MUTATION_LOCK is
    acquired; if the pod comes back while the worker queues on the lock, the
    recheck inside the lock must refuse the removal (never delete a live
    pod's checkout)."""
    # 1st call: pod-stop section sees inactive (no stop needed).
    # 2nd call: post-lock recheck sees the pod ACTIVE again -> refuse.
    active_calls = iter([[], ["wt-1"]])
    removed_cmds: list[list] = []

    async def fake_run_cmd(cmd, timeout=None, **kw):
        if "worktree" in cmd and "remove" in cmd:
            removed_cmds.append(cmd)
        return (0, "", "")

    async def fake_find(name):
        return {"path": f"/wt/{name}", "branch": f"feat/{name}"}, None

    with (
        patch.object(repository_mod, "_find_worktree", side_effect=fake_find),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=0),
        patch.object(
            fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="a" * 40
        ),
        patch.object(
            fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True
        ),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="a" * 40),
        patch.object(runtime_mod, "_load_cfg", return_value=object()),
        patch.object(runtime_mod, "_POD_AVAILABLE", True),
        patch.object(runtime_mod.rt, "require_backend", return_value=None),
        patch.object(runtime_mod.rt, "active_names", side_effect=lambda cfg: next(active_calls)),
        patch.object(runtime_mod, "_run_cmd", side_effect=fake_run_cmd),
    ):
        res = await mod._worktree_remove("wt-1", force=False)

    assert res.get("ok") is False
    assert "active again" in (res.get("error") or "")
    assert removed_cmds == [], "git worktree remove ran despite live pod"


# --- skill registration (bundled skills inside builtin app) ---


def test_register_skills_creates_symlinks_for_bundled_skills(tmp_path, monkeypatch):
    """Enabling the dev-fleet app registers its bundled pod-e2e skill.

    kirocrew-worktree-dev is deliberately NOT bundled here: the canonical copy
    ships in the top-level ``skills/`` catalog (synced into every install), and
    a second app-bridged copy would drift and be loaded nondeterministically
    against it.
    """
    from kiro_crew.apps.bridges import _register_skills
    from kiro_crew.apps.manifest import AppManifest

    fake_config = tmp_path / "config"
    fake_config.mkdir()
    monkeypatch.setattr("kiro_crew.apps.bridges.config_dir", lambda: fake_config)

    app_root = Path(__file__).resolve().parent.parent / ("src/kiro_crew/apps/builtins/dev_fleet")

    manifest = AppManifest(
        name="dev-fleet",
        version="1.0.0",
        skills=["skills/pod-e2e"],
    )

    registered = _register_skills("dev-fleet", manifest, app_root)

    skills_dir = fake_config / "skills"
    namespaced_dir = skills_dir / "dev-fleet"

    expected_skills = {"pod-e2e"}
    registered_names = {r.split("/")[-1] for r in registered}
    assert expected_skills <= registered_names
    # The stale bundled copy must stay deleted — the shipped catalog owns it.
    assert not (app_root / "skills" / "kirocrew-worktree-dev").exists()

    # is_link_or_junction, not is_symlink: registration links with a directory junction
    # on Windows (an unprivileged account holds no SeCreateSymbolicLinkPrivilege)
    # and a junction reports is_symlink() False.
    for skill_name in expected_skills:
        link = namespaced_dir / skill_name
        assert platform_compat.is_link_or_junction(link), f"Namespaced link missing: {link}"
        assert link.resolve().is_dir()
        flat = skills_dir / skill_name
        assert platform_compat.is_link_or_junction(flat), f"Flat link missing: {flat}"
        assert flat.resolve().is_dir()


def test_register_skills_tolerates_missing_feature_demo_recording(tmp_path, monkeypatch):
    """feature-demo-recording absence must not crash skill registration."""
    from kiro_crew.apps.bridges import _register_skills
    from kiro_crew.apps.manifest import AppManifest

    fake_config = tmp_path / "config"
    fake_config.mkdir()
    monkeypatch.setattr("kiro_crew.apps.bridges.config_dir", lambda: fake_config)

    app_root = Path(__file__).resolve().parent.parent / ("src/kiro_crew/apps/builtins/dev_fleet")

    manifest = AppManifest(
        name="dev-fleet",
        version="1.0.0",
        skills=[
            "skills/pod-e2e",
            "skills/kirocrew-worktree-dev",  # not bundled — must not crash
            "skills/feature-demo-recording",
        ],
    )

    registered = _register_skills("dev-fleet", manifest, app_root)

    registered_names = {r.split("/")[-1] for r in registered}
    assert "pod-e2e" in registered_names
    # Absent bundled dirs are tolerated, not registered.
    assert "kirocrew-worktree-dev" not in registered_names


@pytest.mark.asyncio
async def test_remove_refuses_live_worktree(monkeypatch):
    """Removing the checkout the live gateway runs from would kill the
    gateway mid-flight -- refused even with force."""

    async def fake_find(name):
        return {"path": "/wt/feature-x", "is_main": False, "branch": "feature-x"}, None

    seen_fresh: list = []

    async def fake_live(*, fresh: bool = False):
        seen_fresh.append(fresh)
        return "/wt/feature-x"

    monkeypatch.setattr(repository_mod, "_find_worktree", fake_find)
    monkeypatch.setattr(live_mod, "_live_worktree_path", fake_live)
    for force in (False, True):
        r = await mod._worktree_remove("feature-x", force=force)
        assert r["ok"] is False
        assert "live gateway" in r["error"]
    # Destructive callers must bypass the 30s cache -- a stale answer could
    # authorize deleting the checkout the gateway switched onto mid-TTL.
    assert seen_fresh == [True, True]


@pytest.mark.asyncio
async def test_remove_refuses_own_process_checkout(monkeypatch):
    """A gateway launched outside systemd is invisible to the unit probe --
    the target must also be checked against the checkout our own running
    code was imported from."""

    async def fake_find(name):
        return {"path": "/wt/self", "is_main": False, "branch": "self"}, None

    async def fake_live(*, fresh: bool = False):
        return None

    monkeypatch.setattr(repository_mod, "_find_worktree", fake_find)
    monkeypatch.setattr(live_mod, "_live_worktree_path", fake_live)
    monkeypatch.setattr(live_mod, "_own_checkout_path", lambda: "/wt/self")
    for force in (False, True):
        r = await mod._worktree_remove("self", force=force)
        assert r["ok"] is False
        assert "current gateway process" in r["error"]


def test_own_checkout_path_resolves_this_worktree():
    own = mod._own_checkout_path()
    assert own is not None
    from pathlib import Path

    assert (Path(own) / "src" / "kiro_crew").is_dir()
