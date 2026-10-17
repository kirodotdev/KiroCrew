"""Fixtures, markers and the sync harness shared by test_dev_fleet_app.py and its parts.

The parts are the ``test_dev_fleet_app_<topic>.py`` modules, split by behaviour. An
autouse fixture applies only in a module that imports it by name, so every one of
them imports the four autouse fixtures below.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import live as live_mod
from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.apps.builtins.dev_fleet import worktree_ops as worktree_ops_mod

# These Dev Fleet make-live / cancel / sync / build tests assert POSIX-only
# behaviour that has no Windows equivalent: os.geteuid, the ``.venv/bin`` layout
# (vs ``.venv\\Scripts\\kirocrew.exe``), systemctl/launchctl service probing, and
# ``/``-rooted trusted-binary paths. The production code is correct on Windows;
# only these fixtures/assertions are POSIX-shaped, so they are skipped under the
# reduced-scope backend CI that runs on Windows.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only Dev Fleet make-live/cancel/sync semantics (issue #2041)",
)


@pytest.fixture(autouse=True)
def _venv_maps_to_the_synced_checkout():
    """Answer the sync's foreign-venv guard with "it maps", for tests not about it.

    Both install paths now refuse a venv that serves a DIFFERENT checkout, and
    answering that question RUNS the target interpreter — which every sync test
    here points at a path that does not exist. Stubbing both halves keeps an
    unrelated assertion from turning into an unrunnable-interpreter refusal. The
    guard's own behaviour is asserted by
    ``test_sync_refuses_a_venv_that_serves_another_checkout``, which patches it
    back to a refusal, and the logic behind it lives in ``test/test_dep_sync.py``.
    """
    from kiro_crew import dep_sync

    with (
        patch.object(dep_sync, "installed_package_origin", return_value="<stubbed>"),
        patch.object(dep_sync, "venv_not_mapped_to", return_value=None),
    ):
        yield


def _steps_json_path_from_cmd(cmd):
    """The steps-JSON file path from a runner command.

    ``cmd`` is ``[python, -I, -c, <bootstrap>, <snapshot>, <snapshot-sha256>,
    <steps_json>, <repo>, --reserved, <codes>, ...]``. The steps file is the
    SECOND positional after the snapshot path (the snapshot ends in
    ``sync_runner.py``; the argv-pinned snapshot digest sits between them).
    """
    for i, tok in enumerate(cmd):
        if isinstance(tok, str) and tok.endswith("sync_runner.py"):
            return cmd[i + 2]
    raise AssertionError("sync_runner.py snapshot not found in command — shape changed")


def _sync_steps_from_cmd(cmd):
    """Pull the structured step list back out of the runner invocation.

    The runner is not a ``-c <script>`` string; it is a snapshot of
    sync_runner.py run BY PATH, and the steps travel as a JSON FILE whose path
    is the runner's first positional argument. Read that file so the assertions
    bite on the real step list rather than on source text.
    """
    import json as _json

    steps_json_path = _steps_json_path_from_cmd(cmd)
    with open(steps_json_path, encoding="utf-8") as fh:
        return _json.load(fh)


def _install_step(steps):
    """The 'pip install' step dict from a decoded step list.

    Both branches (editable reinstall and dependency-only substitute) label
    their install step 'pip install'; the argv is what differs between them.
    """
    for st in steps:
        if st["label"] == "pip install":
            return st
    raise AssertionError("no 'pip install' step found in the step list")


def _reap_cleanup_paths(cleanup_paths) -> None:
    """Remove what the sync registered for the run's cleanup, in order.

    Mirrors the real run's cleanup loop: unlink each entry and fall back to
    rmdir, which only succeeds once the directory is empty -- so the order the
    sync registered them in (files before their directory) is what makes the
    directory go too.
    """
    for path in cleanup_paths or []:
        try:
            os.unlink(path)
        except OSError:
            try:
                os.rmdir(path)
            except OSError:
                pass


def _start_run_stub(rid: str) -> AsyncMock:
    """A stand-in for ``_start_run`` that still honours ``cleanup_paths``.

    The sync stages its runner snapshot, steps file and preflight snapshot into
    private mkdtemp directories and hands them to ``_start_run`` for removal
    when the run ends. A bare ``AsyncMock`` swallows that kwarg, so every sync
    driven through one leaves those directories behind in the temp root. Use
    this in any test that does not need to read the staged files afterwards;
    a test that does must reap them itself once it has read them.
    """

    def _run(label, cmd, **kw):
        _reap_cleanup_paths(kw.get("cleanup_paths"))
        return rid

    return AsyncMock(side_effect=_run)


#: Temp-directory prefixes the dev-fleet sync stages and ``_start_run``'s finally owns.
_STAGED_SYNC_PREFIXES = (
    "kirocrew-sync-runner-",
    "kirocrew-npm-preflight-",
    "kirocrew-dep-sync-",
)


@pytest.fixture(autouse=True)
def _sweep_staged_sync_tempdirs():
    """Backstop for sync snapshots left by a test that stubbed ``_start_run`` itself.

    ``_sync_start_locked`` stages three kinds of snapshot into the temp root -- the
    runner source plus its steps JSON, the npm preflight, and the dependency-only
    ``dep_sync`` copy -- and registers each one in the ``cleanup_paths`` list that
    ``runtime._start_run``'s ``finally`` unlinks. Stubbing ``_start_run`` stubs out
    the ONLY cleanup.

    Two mechanisms cover that, and this is the second. :func:`_start_run_stub` is
    the exact one and is preferred: it honours ``cleanup_paths``, so it removes
    precisely what the sync registered. It is only usable where the test does not
    need the staged files AFTER the call, and several tests here read the steps JSON
    out of the staged directory, so they must keep it alive across the stubbed run.
    Those reach the leak through their own inline ``AsyncMock``, and a per-test call
    they must each remember is how this came back: MEASURED with
    ``KIROCREW_TMP_PER_TEST=1``, eleven directories outlived one run of this file,
    named after six tests.

    Sweeping is sound HERE in a way a general temp sweep is not (see the residue
    rules in testing-conventions): it runs after the test has finished reading, it is
    confined to THIS run's own redirected temp base, it matches only prefixes this app
    owns, and it removes only entries absent before the test -- so a concurrent run's
    directory, which lives under its own base, can never be a candidate.
    """
    base = Path(tempfile.gettempdir())

    def staged() -> set[Path]:
        try:
            return {
                p for p in base.iterdir() if p.is_dir() and p.name.startswith(_STAGED_SYNC_PREFIXES)
            }
        except OSError:
            return set()

    before = staged()
    yield
    for path in staged() - before:
        shutil.rmtree(path, ignore_errors=True)


def _cleanup_sync_tempdirs(mock_start):
    """Delete the snapshot dirs a stubbed _start_run would have cleaned up.

    Stubbing ``_start_run`` also stubs out its ``finally`` cleanup, so the
    snapshot dirs the sync stages (the runner snapshot + steps file, the
    preflight snapshot, and the dependency-only path's dep_sync snapshot) would
    outlive the test. Remove exactly what it registered, file before directory.
    """
    if not getattr(mock_start, "call_args", None):
        return
    _reap_cleanup_paths(mock_start.call_args.kwargs.get("cleanup_paths"))


#: The main checkout the sync tests run against. Pinned rather than ambient so the
#: sync's refusal path (no checkout discovered) cannot decide their outcome.
_SYNC_REPO = "/fake/main-checkout"


#: cleanup_paths captured from the last ``_run_sync``. The harness stubs
#: ``_start_run``, so a test cannot see what the sync registered for removal any
#: other way, and asserting on it is how a leaked snapshot gets caught. The list
#: is updated in place, so a module that imports it by name reads the latest run.
_LAST_CLEANUP_PATHS: list[str] = []


async def _run_sync(mod, locked):
    """Drive _sync_start_locked with the probe stubbed.

    Returns ``(result, cmd, steps)``: the result dict, the runner command handed
    to ``_start_run`` (``None`` when the sync refused), and the decoded step
    dicts read from the steps JSON file BEFORE the harness deletes it (``None``
    on a refusal). The runner is a snapshot of sync_runner.py run by path with
    the step list in a JSON file, so assertions inspect ``cmd`` and ``steps``
    rather than script source text.

    MAIN_REPO is pinned because the sync refuses outright when no checkout was
    discovered, and these tests are about the sync's own behaviour: leaving it
    ambient makes them pass or fail on whether the HOST running them happens to
    sit in a Kiro Crew checkout. Assertions that quote the repo path must use
    ``_SYNC_REPO`` rather than reading ``mod.MAIN_REPO``.

    The venv-origin guard is answered by the module's autouse fixture; a test
    about that guard patches it back to a refusal.
    """
    repository_mod._UPSTREAM_REMOTE = "origin"
    worktree_ops_mod._SYNC_RID = None
    with (
        patch.object(repository_mod, "MAIN_REPO", _SYNC_REPO),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="main"),
        patch.object(worktree_ops_mod, "_venv_python", return_value=Path("/fake/.venv/bin/python")),
        patch.object(runtime_mod, "_trusted_bin", side_effect=lambda n: f"/usr/bin/{n}"),
        patch.object(worktree_ops_mod.dep_sync, "locked_console_scripts", return_value=locked),
        patch(
            "kiro_crew.apps.builtins.dev_fleet.worktree_ops.sandboxed_spawn_argv",
            side_effect=lambda cmd, mode, env=None: (cmd, env or {}, None),
        ),
        patch.object(
            runtime_mod, "_start_run", new_callable=AsyncMock, return_value="run-123"
        ) as mock_start,
    ):
        async with mod._SYNC_LOCK:
            result = await mod._sync_start_locked()
    repository_mod._UPSTREAM_REMOTE = None
    cmd = mock_start.call_args[0][1] if mock_start.call_args else None
    # Decode the steps BEFORE cleanup deletes the staged JSON file — inside
    # try/finally so a malformed file or a changed command shape cannot leave
    # the staged directories behind (the finally owns cleanup either way).
    _LAST_CLEANUP_PATHS[:] = list(
        (mock_start.call_args.kwargs.get("cleanup_paths") or []) if mock_start.call_args else []
    )
    try:
        steps = _sync_steps_from_cmd(cmd) if cmd else None
    finally:
        # Stubbing _start_run also stubs out its `finally` cleanup, so anything
        # the sync staged for removal (the runner snapshot + steps file, and
        # the dependency-only path's dep_sync snapshot) would outlive the test.
        _cleanup_sync_tempdirs(mock_start)
    return result, cmd, steps


@pytest.fixture(autouse=True)
def _reset_make_live_committed_latch():
    """``_MAKE_LIVE_COMMITTED`` is a process-local latch: once a real cutover
    schedules a restart it refuses all further cutovers for the process's life.
    In-process pytest would leak that latched state into later tests, so reset
    it around every test to mirror a fresh gateway process."""
    live_mod._MAKE_LIVE_COMMITTED = False
    yield
    live_mod._MAKE_LIVE_COMMITTED = False


@pytest.fixture(autouse=True)
def _reset_shutdown_admission_state():
    """``_SHUTDOWN_IN_PROGRESS`` is set by ``dev_fleet_cleanup`` and never cleared
    in production (the process exits).  In-process pytest leaks the latched True
    state into later tests that call ``_start_run`` directly, causing them to
    raise RuntimeError instead of running normally.  Reset both the flag and the
    lock around every test to mirror a fresh gateway process."""
    runtime_mod._SHUTDOWN_IN_PROGRESS = False
    runtime_mod._SHUTDOWN_ADMISSION_LOCK = asyncio.Lock()
    yield
    runtime_mod._SHUTDOWN_IN_PROGRESS = False
    runtime_mod._SHUTDOWN_ADMISSION_LOCK = asyncio.Lock()


def _mk_make_live_wt(tmp_path, *, venv: bool = False, dist: bool = False, venv_exec: bool = True):
    """Build a fake worktree dir with optional .venv/bin/kirocrew and built dist.

    When ``venv`` is set the fake ``.venv/bin/kirocrew`` is created **executable**
    (``venv_exec=True``, the realistic provisioned state that passes the make-live
    exec-bit gate); pass ``venv_exec=False`` to simulate a present-but-non-executable
    binary (the ``venv_not_executable`` case)."""
    wt = tmp_path / "kirocrew-wt-feat"
    wt.mkdir(parents=True, exist_ok=True)
    if venv:
        vb = wt / ".venv" / "bin"
        vb.mkdir(parents=True, exist_ok=True)
        kcbin = vb / "kirocrew"
        kcbin.write_text("#!/bin/sh\n")
        kcbin.chmod(0o755 if venv_exec else 0o644)
    if dist:
        dd = wt / "src" / "kiro_crew" / "static" / "dist"
        dd.mkdir(parents=True, exist_ok=True)
        (dd / "index.html").write_text("<html></html>")
    return wt


def _assert_sandboxed(path, what: str, sandbox_root) -> None:
    """Fail loudly when a host-mutating make-live seam resolves outside the sandbox.

    The seams below decide WHERE the cutover writes and WHAT it executes. If one is
    left unpatched the production code is correct and does exactly what it is told —
    against the developer's own machine: it rewrites the live gateway's systemd
    drop-in to point at a pytest tmpdir and restarts the unit, which then fails
    203/EXEC on every boot once the tmpdir is reaped. Asserting containment here
    makes the next missed seam fail inside the test instead of taking down the host.

    *sandbox_root* is THIS test's own directory, not a generic temp root. The
    distinction is the whole strength of the check: "somewhere under the system temp
    dir" is satisfied by any tmp path at all, whereas "inside the tree this test
    built" is satisfied only by the redirect actually taking effect. It also stops the
    assertion depending on where ``tempfile`` happens to be rooted, which the suite's
    isolation floor now controls.
    """
    resolved = Path(path).resolve()
    root = Path(sandbox_root).resolve()
    assert root == resolved or root in resolved.parents, (
        f"{what} resolved OUTSIDE the temp sandbox: {resolved}. A test that reaches "
        f"the cutover path must never touch a real host path."
    )
    assert (
        Path.home() / ".config"
    ).resolve() not in resolved.parents, (
        f"{what} resolved inside the real user config dir: {resolved}"
    )


def _stub_make_live(
    monkeypatch,
    wt,
    *,
    live=None,
    in_pod=False,
    unit_status="ok",
    platform="linux",
    pointer_dir=None,
):
    """Wire the make-live seams: the path resolves to *wt*, pod/live/unit state
    fixed. ``unit_status`` stubs _live_user_unit_status so tests never depend on
    the host's real systemd --user state.

    ``pointer_dir`` isolates the live-target pointer: when set (a tmp_path
    sub-directory), ``live_target.pointer_path`` returns a file inside it so no
    test ever reads or writes the real data home. Every test that reaches the
    cutover path MUST pass this.

    The service-definition and command seams are isolated **unconditionally**,
    because forgetting them is not a test failure — it is a live-host outage.
    ``_dropin_path`` otherwise resolves ``$XDG_CONFIG_HOME``/``~/.config`` and
    ``_run_cmd`` otherwise runs the real ``systemctl --user``, so a test reaching
    the cutover path would repoint and restart the developer's own gateway at a
    tmpdir. Both are redirected under *wt*'s parent and containment-asserted; a
    test that needs to observe or shape them re-patches after this call, which
    wins because it is applied later.

    ``platform`` pins the service backend (default systemd). Without it these
    assertions silently follow the HOST's platform: the same test would check a
    systemd drop-in on Linux and a launchd agent on macOS. Pinning keeps every
    systemd expectation deterministic everywhere, and the launchd twins pin
    ``"darwin"`` explicitly.
    """
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform=platform))
    tool = "/usr/bin/systemctl" if platform == "linux" else "/bin/launchctl"
    monkeypatch.setattr(live_mod, "shutil", MagicMock(which=MagicMock(return_value=tool)))
    monkeypatch.setattr(
        repository_mod,
        "_discover_worktrees",
        AsyncMock(return_value=[{"path": str(wt), "branch": "feat", "is_main": False}]),
    )
    monkeypatch.setattr(live_mod, "_live_worktree_path", AsyncMock(return_value=live))
    monkeypatch.setattr(live_mod, "_in_pod", lambda: in_pod)
    monkeypatch.setattr(live_mod, "_live_user_unit_status", AsyncMock(return_value=unit_status))
    sandbox_root = Path(wt).parent
    sandbox_dropin = sandbox_root / "_systemd" / f"{mod._LIVE_GATEWAY_UNIT}.d" / "make-live.conf"
    _assert_sandboxed(sandbox_dropin, "_dropin_path", sandbox_root)
    monkeypatch.setattr(live_mod, "_dropin_path", lambda: sandbox_dropin)
    monkeypatch.setattr(runtime_mod, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    # Prove the redirect actually took: a rename of the production symbol would
    # otherwise leave the real path live while every test still looked green.
    _assert_sandboxed(mod._dropin_path(), "patched _dropin_path()", sandbox_root)
    if pointer_dir is not None:
        pointer_dir.mkdir(parents=True, exist_ok=True)
        ptr_file = pointer_dir / "live_target.json"
        monkeypatch.setattr(live_mod.live_target, "pointer_path", lambda: ptr_file)
    if platform == "darwin":
        monkeypatch.setattr(
            live_mod.gateway_service, "restart_contract_current", lambda _path: True
        )
        monkeypatch.setattr(
            live_mod.gateway_service, "loaded_restart_contract_current", lambda _out: True
        )


async def _sync_step_argvs(monkeypatch) -> list:
    """Run _sync with every spawn stubbed; return each step's argv, in order."""
    captured: list = []

    def fake_sandbox(argv, mode, *, env=None, **kw):
        captured.append(list(argv))
        return list(argv), dict(env or {}), None

    with (
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value=mod.BASE_BRANCH),
        patch.object(worktree_ops_mod, "_venv_python", return_value=Path("/fake/.venv/bin/python")),
        patch.object(runtime_mod, "_trusted_bin", side_effect=lambda n: f"/usr/bin/{n}"),
        patch.object(worktree_ops_mod, "sandboxed_spawn_argv", fake_sandbox),
        patch.object(runtime_mod, "_start_run", new_callable=AsyncMock, return_value="rid-steps"),
    ):
        worktree_ops_mod._SYNC_RID = None
        res = await mod._sync()
    assert res["ok"] is True
    return captured
