"""Dev Fleet foreground last-resort restart and the cutover lock's artifact checks.

Covers the foreground backend, make-live's foreground fallback, and the artifact
and pointer probes the cutover runs inside its lock and off the event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dev_fleet_app_helpers import _reset_make_live_committed_latch  # noqa: F401 - fixture
from dev_fleet_app_helpers import _reset_shutdown_admission_state  # noqa: F401 - fixture
from dev_fleet_app_helpers import _sweep_staged_sync_tempdirs  # noqa: F401 - fixture
from dev_fleet_app_helpers import _venv_maps_to_the_synced_checkout  # noqa: F401 - fixture
from dev_fleet_app_helpers import _POSIX_ONLY, _mk_make_live_wt, _stub_make_live

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import live as live_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod

# =============================================================================
# Foreground last-resort restart
# =============================================================================


def _mk_kcbin(tmp_path: Path, name: str = "kirocrew") -> Path:
    """An executable file that passes ForegroundBackend's launcher validation."""
    d = tmp_path / "bin"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return p


def _fg(
    tmp_path: Path,
    *,
    port: int = 7777,
    pid: int = 4242,
    launcher: "str | None" = None,
    alive: bool = True,
    spawn=None,
    ports: "list[int] | None" = None,
    pids: "dict[int, int] | None" = None,
    confined: "str | None" = None,
):
    """A ForegroundBackend wired to fakes. Returns (backend, spawned_argvs)."""
    from kiro_crew.apps.builtins.dev_fleet import gateway_service as gs

    spawned: list[list[str]] = []

    def default_spawn(argv):
        spawned.append(list(argv))

    marker_ports = ports if ports is not None else [port]
    pid_by_port = pids if pids is not None else {port: pid}
    backend = gs.ForegroundBackend(
        marker_ports=lambda: list(marker_ports),
        read_pid=lambda p: pid_by_port.get(p),
        read_launcher=lambda p: launcher,
        pid_exists=lambda p: alive,
        spawn=spawn if spawn is not None else default_spawn,
        confinement=lambda: confined,
    )
    return backend, spawned


@pytest.mark.asyncio
async def test_foreground_backend_ok_and_start_id(tmp_path):
    """Single live marker + resolvable binary -> ok; start_id is the marker pid."""
    kc = _mk_kcbin(tmp_path)
    fg, _ = _fg(tmp_path, launcher=str(kc))
    assert await fg.status() == "ok"
    assert await fg.start_id() == "4242"


@pytest.mark.asyncio
async def test_foreground_backend_unavailable_cases(tmp_path):
    """No marker, dead pid, or ambiguous markers -> unavailable, start_id None."""
    kc = _mk_kcbin(tmp_path)
    # No marker at all.
    fg, _ = _fg(tmp_path, ports=[], pids={}, launcher=str(kc))
    assert await fg.status() == "no_foreground_gateway"
    assert await fg.start_id() is None
    # Marker whose pid is dead (crash leftover).
    fg, _ = _fg(tmp_path, alive=False, launcher=str(kc))
    assert await fg.status() == "no_foreground_gateway"
    # Two live markers: ambiguous, never guess which gateway to bounce.
    fg, _ = _fg(tmp_path, ports=[7777, 7778], pids={7777: 1, 7778: 2}, launcher=str(kc))
    assert await fg.status() == "no_foreground_gateway"
    assert await fg.start_id() is None


@pytest.mark.asyncio
async def test_foreground_backend_binary_resolution(tmp_path):
    """ONLY the keystone-fenced marker launcher is trusted — no PATH fallback
    (an agent can plant a `kirocrew` in ~/.local/bin); invalid recorded
    launchers are refused rather than guessed around."""
    kc = _mk_kcbin(tmp_path)
    # Marker launcher used, verbatim.
    fg, spawned = _fg(tmp_path, launcher=str(kc))
    ok, err = await fg.restart_detached()
    assert ok and spawned[0][0] == str(kc)
    # No recorded launcher (source-tree launch, empty marker): refuse — the
    # PATH fallback is deliberately absent.
    fg, spawned = _fg(tmp_path, launcher=None)
    assert await fg.status() == "no_kirocrew_binary"
    ok, err = await fg.restart_detached()
    assert not ok and "resolved" in err and spawned == []
    # A launcher that is not basenamed kirocrew is refused even when executable
    # (the _own_console_script rule: exec the entry point it claims to be).
    impostor = _mk_kcbin(tmp_path / "i", name="systemctl")
    fg, _ = _fg(tmp_path, launcher=str(impostor))
    assert await fg.status() == "no_kirocrew_binary"
    # A non-executable launcher is refused: it could stop the gateway but never
    # start the replacement.
    limp = _mk_kcbin(tmp_path / "n")
    limp.chmod(0o644)
    fg, _ = _fg(tmp_path, launcher=str(limp))
    assert await fg.status() == "no_kirocrew_binary"
    # A relative recorded path is refused (never resolved against a cwd).
    fg, _ = _fg(tmp_path, launcher="bin/kirocrew")
    assert await fg.status() == "no_kirocrew_binary"


@pytest.mark.asyncio
async def test_foreground_backend_refuses_when_confined(tmp_path):
    """A confined backend (OS sandbox / agents cgroup scope) never spawns: the
    replacement would inherit the confinement for the gateway's whole life."""
    kc = _mk_kcbin(tmp_path)
    fg, spawned = _fg(
        tmp_path, launcher=str(kc), confined="the Dev Fleet backend runs inside the sandbox"
    )
    assert await fg.status() == "backend_confined"
    ok, err = await fg.restart_detached()
    assert not ok and "sandbox" in err
    assert spawned == []


def test_default_confinement_detects_sandbox_marker(monkeypatch):
    """KIROCREW_SANDBOX_ACTIVE — the launcher-exported in-sandbox marker — is
    detected as confinement (checked before the cgroup read, so this is
    deterministic on any host)."""
    from kiro_crew.apps.builtins.dev_fleet import gateway_service as gs

    monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
    reason = gs.default_confinement()
    assert reason is not None and "sandbox" in reason


@pytest.mark.asyncio
async def test_foreground_restart_detached_pins_port(tmp_path):
    """The detached command is `<bin> restart --port <marker port>`."""
    kc = _mk_kcbin(tmp_path)
    fg, spawned = _fg(tmp_path, port=6776, pid=99, launcher=str(kc))
    ok, err = await fg.restart_detached()
    assert ok and err == ""
    assert spawned == [[str(kc), "restart", "--port", "6776"]]


@pytest.mark.asyncio
async def test_foreground_spawn_failure_signals_nothing(tmp_path, monkeypatch):
    """A spawn that cannot be established returns (False, why) and the backend
    never signals any process — the incumbent gateway must stay untouched."""
    kc = _mk_kcbin(tmp_path)

    def bad_spawn(argv):
        raise OSError("resource temporarily unavailable")

    kills: list = []
    monkeypatch.setattr(os, "kill", lambda *a: kills.append(a))
    fg, _ = _fg(tmp_path, launcher=str(kc), spawn=bad_spawn)
    ok, err = await fg.restart_detached()
    assert not ok and "resource temporarily unavailable" in err
    assert kills == []


@pytest.mark.asyncio
async def test_make_live_foreground_last_resort_cutover(monkeypatch, tmp_path):
    """When no manager is drivable but a single live foreground gateway exists,
    make-live finishes the cutover itself: pointer written, detached
    `kirocrew restart` established, start_id (the pre-restart pid) returned,
    and the committed latch set exactly as on a drivable host."""
    kc = _mk_kcbin(tmp_path)
    for status in ("no_systemd", "no_user_unit", "no_launchd", "no_agent"):
        wt = _mk_make_live_wt(tmp_path / status, venv=True, dist=True)
        ptr_dir = tmp_path / "ptr" / status
        _stub_make_live(monkeypatch, wt, unit_status=status, pointer_dir=ptr_dir)
        monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
        fg, spawned = _fg(tmp_path, port=7777, pid=31337, launcher=str(kc))
        monkeypatch.setattr(live_mod, "_foreground_backend", lambda fg=fg: fg)

        res = await mod._make_live(str(wt), dry_run=False)
        assert res["ok"] is True and res["cutover"] is True, f"{status}: {res}"
        assert "staged_only" not in res
        assert res["start_id"] == "31337"
        assert spawned == [[str(kc), "restart", "--port", "7777"]]
        # Pointer written with the target.
        data = json.loads((ptr_dir / "live_target.json").read_text())
        assert Path(data["checkout"]).resolve() == wt.resolve()
        # A restart IS pending: latched like the drivable path.
        assert mod._MAKE_LIVE_COMMITTED is True


@pytest.mark.asyncio
async def test_make_live_foreground_is_strictly_last_resort(monkeypatch, tmp_path):
    """Ordering is systemd > launchd > foreground: on a drivable host — or one
    whose manager exists but is mis-set-up (named-remedy codes) — the
    foreground backend is never even constructed."""
    factory = MagicMock()
    monkeypatch.setattr(live_mod, "_foreground_backend", factory)
    # Drivable systemd host: full managed cutover, no foreground.
    wt = _mk_make_live_wt(tmp_path / "ok", venv=True, dist=True)
    _stub_make_live(monkeypatch, wt, unit_status="ok", pointer_dir=tmp_path / "ptr-ok")
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True and "staged_only" not in res
    factory.assert_not_called()
    # Mis-set-up managers keep their named remedy; foreground must not bounce
    # a gateway behind a manager's back.
    for status in (
        "user_unit_inactive",
        "agent_not_indirected",
        "agent_restart_contract_outdated",
        "live_program_missing",
    ):
        wt = _mk_make_live_wt(tmp_path / status, venv=True, dist=True)
        _stub_make_live(monkeypatch, wt, unit_status=status, pointer_dir=tmp_path / f"ptr-{status}")
        monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
        res = await mod._make_live(str(wt), dry_run=False)
        assert res["ok"] is True and res["staged_only"] is True, f"{status}: {res}"
        factory.assert_not_called()


@pytest.mark.asyncio
async def test_make_live_foreground_spawn_failure_keeps_advisory(monkeypatch, tmp_path):
    """FAIL SAFE: when the detached spawn cannot be established the running
    gateway is untouched, the pointer STAYS staged, the committed latch is not
    set, and the response is the status-quo manual advisory."""
    kc = _mk_kcbin(tmp_path)
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, unit_status="no_systemd", pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)

    def bad_spawn(argv):
        raise OSError("spawn refused")

    fg, _ = _fg(tmp_path, launcher=str(kc), spawn=bad_spawn)
    monkeypatch.setattr(live_mod, "_foreground_backend", lambda: fg)

    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True and res["staged_only"] is True
    assert res["manual_restart"]
    assert res["manual_restart"] in res["notice"]
    # The plan must not keep promising an automatic restart that failed.
    assert res["plan"]["restart"] == "manual"
    # Pointer written and KEPT: "finish with one command", not "start over".
    data = json.loads((ptr_dir / "live_target.json").read_text())
    assert Path(data["checkout"]).resolve() == wt.resolve()
    # No restart pending -> not latched; a re-point must stay allowed.
    assert mod._MAKE_LIVE_COMMITTED is False


@pytest.mark.asyncio
async def test_make_live_foreground_unavailable_keeps_advisory(monkeypatch, tmp_path):
    """Eligible codes without a usable foreground gateway (no/ambiguous marker,
    unresolvable binary, confined backend) keep the pre-existing staged_only
    behaviour."""
    kc = _mk_kcbin(tmp_path)
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    for label, fg_kwargs in (
        ("no_marker", dict(ports=[], pids={}, launcher=None)),
        ("confined", dict(launcher=str(kc), confined="backend is sandboxed")),
    ):
        _stub_make_live(monkeypatch, wt, unit_status="no_systemd", pointer_dir=ptr_dir / label)
        monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
        fg, spawned = _fg(tmp_path, **fg_kwargs)
        monkeypatch.setattr(live_mod, "_foreground_backend", lambda fg=fg: fg)

        res = await mod._make_live(str(wt), dry_run=False)
        assert res["ok"] is True and res["staged_only"] is True, f"{label}: {res}"
        assert res["manual_restart"] in res["notice"]
        assert spawned == []
        assert mod._MAKE_LIVE_COMMITTED is False


@pytest.mark.asyncio
async def test_make_live_foreground_dry_run_plan(monkeypatch, tmp_path):
    """A dry run on a foreground-capable host reports the restart as automatic
    with the exact command — and mutates nothing, spawns nothing."""
    kc = _mk_kcbin(tmp_path)
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, unit_status="no_systemd", pointer_dir=ptr_dir)
    fg, spawned = _fg(tmp_path, port=7777, pid=1, launcher=str(kc))
    monkeypatch.setattr(live_mod, "_foreground_backend", lambda: fg)

    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is True and res["dry_run"] is True
    plan = res["plan"]
    assert plan["restart"] == "automatic"
    assert plan["restart_backend"] == "foreground"
    assert plan["restart_command"] == f"{kc} restart --port 7777"
    assert spawned == []
    assert not (ptr_dir / "live_target.json").exists()


@pytest.mark.asyncio
async def test_gateway_start_id_foreground_fallback(monkeypatch, tmp_path):
    """On an eligible host the health handshake identity is the marker pid, so
    the dashboard can observe a foreground cutover complete; on an ineligible
    host it stays None (degrade, never wait forever)."""
    kc = _mk_kcbin(tmp_path)
    fg, _ = _fg(tmp_path, pid=8080, launcher=str(kc))
    monkeypatch.setattr(live_mod, "_foreground_backend", lambda: fg)
    with (
        patch.object(live_mod, "sys", MagicMock(platform="linux")),
        patch.object(live_mod, "shutil", MagicMock(which=MagicMock(return_value=None))),
    ):
        # No systemctl at all -> primary yields None -> foreground pid.
        assert await mod._gateway_start_id() == "8080"
    with (
        patch.object(
            live_mod,
            "_live_user_unit_status",
            new_callable=AsyncMock,
            return_value="user_unit_inactive",
        ),
        patch.object(live_mod, "sys", MagicMock(platform="linux")),
        patch.object(live_mod, "shutil", MagicMock(which=MagicMock(return_value=None))),
    ):
        assert await mod._gateway_start_id() is None


# ---------------------------------------------------------------------------
# Regression tests: make-live artifact validation inside the cutover lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_artifact_changed_before_lock_is_revalidated(monkeypatch, tmp_path):
    """Artifacts that are valid at early-probe time but gone before the lock
    is acquired are caught by the in-lock re-validation.

    A side-effecting lock wrapper removes the venv binary at the instant the
    lock is acquired, reproducing a concurrent provision that replaces the
    binary between the early check and the commit.  The cutover must refuse
    with ``missing_venv`` and must NOT write the pointer.

    Without the production fix the early probe passes, the lock is acquired,
    and the cutover proceeds to write the pointer and stage a restart — the
    stale validation is never repeated and the race window is not closed.
    This test proves the ordering by observing the final response code and
    the pointer-file state.
    """
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False)

    kcbin = wt / ".venv" / "bin" / "kirocrew"

    # Wrap _MAKE_LIVE_LOCK so that entering the lock removes the binary,
    # simulating a concurrent rebuild that completes between the early probe
    # and the lock-acquire.
    real_lock = asyncio.Lock()

    class _SideEffectLock:
        """Proxy that removes *kcbin* when the lock body is entered."""

        def locked(self) -> bool:
            return real_lock.locked()

        async def __aenter__(self):
            await real_lock.__aenter__()
            # Binary vanishes at the moment the lock body begins.
            kcbin.unlink(missing_ok=True)
            return self

        async def __aexit__(self, *args):
            return await real_lock.__aexit__(*args)

    monkeypatch.setattr(live_mod, "_MAKE_LIVE_LOCK", _SideEffectLock())

    res = await mod._make_live(str(wt), dry_run=False)

    assert res["ok"] is False, (
        "cutover must be refused when the binary disappears inside the lock; "
        "got ok=True — the in-lock re-validation is absent or not running"
    )
    assert (
        res["code"] == "missing_venv"
    ), f"expected missing_venv from in-lock re-validation, got {res.get('code')!r}"
    ptr_file = ptr_dir / "live_target.json"
    assert (
        not ptr_file.exists()
    ), "the live-target pointer must NOT be written when in-lock re-validation fails"


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_artifact_checks_are_executor_offloaded(monkeypatch, tmp_path):
    """The artifact filesystem checks (``is_file`` / ``os.access``) are
    submitted to ``loop.run_in_executor`` rather than called inline on the
    event loop, preventing a slow or network-backed filesystem from stalling
    all Dev Fleet requests.

    The test wraps ``subprocess_executor()`` to record every callable submitted
    via ``loop.run_in_executor``.  A helper named ``_validate_artifacts_sync``
    must be submitted at least twice — once for the early probe and once for
    the in-lock re-validation — proving the checks are offloaded.

    Without the production fix, the checks are plain synchronous expressions
    (``kcbin.is_file()``, ``os.access()``, ``dist_index.is_file()``) executed
    inline; no callable named ``_validate_artifacts_sync`` is ever submitted.
    """
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_LOCK", asyncio.Lock())

    submitted_qualnames: list[str] = []

    # Intercept every run_in_executor call by wrapping the event loop's method.
    # asyncio.get_running_loop() inside _make_live returns the SAME object that
    # asyncio.get_event_loop() returns under pytest-asyncio's per-test loop.
    # We patch the loop object's method directly so the intercept is in place
    # when _make_live calls loop.run_in_executor(…).
    running_loop = asyncio.get_running_loop()
    real_run_in_executor = running_loop.run_in_executor

    async def _recording_run_in_executor(executor, fn, *args):
        submitted_qualnames.append(getattr(fn, "__qualname__", repr(fn)))
        return await real_run_in_executor(executor, fn, *args)

    monkeypatch.setattr(running_loop, "run_in_executor", _recording_run_in_executor)

    await mod._make_live(str(wt), dry_run=False)

    validate_submissions = [q for q in submitted_qualnames if "_validate_artifacts_sync" in q]
    assert len(validate_submissions) >= 2, (
        "artifact validation must be submitted to the executor at least twice "
        "(early probe + in-lock re-validation); "
        f"all submitted callables: {submitted_qualnames!r}.  "
        "Zero entries means the checks are still inline on the event loop."
    )


@pytest.mark.asyncio
async def test_make_live_pointer_and_path_probes_are_executor_offloaded(monkeypatch, tmp_path):
    """The cutover runs on the GATEWAY's loop, which serves the whole dashboard, so
    none of its filesystem probes may run inline: the worktree existence check,
    the pointer reads (`_staged_target`, `snapshot`), the path comparisons
    (`_same_path`), the pod detection (`_in_pod`), the plan (`validate` stats the
    target) and the pointer write all go through ``run_in_executor``."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_LOCK", asyncio.Lock())

    submitted: list[str] = []
    running_loop = asyncio.get_running_loop()
    real_run_in_executor = running_loop.run_in_executor

    async def _recording_run_in_executor(executor, fn, *args):
        submitted.append(getattr(fn, "__qualname__", repr(fn)))
        return await real_run_in_executor(executor, fn, *args)

    monkeypatch.setattr(running_loop, "run_in_executor", _recording_run_in_executor)

    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True, res

    joined = " ".join(submitted)
    # `_stub_make_live` replaces `_in_pod` with a lambda and reports no live
    # checkout (so no `_same_path` comparison runs); the lambda's submission is the
    # evidence that the pod probe went through the executor.
    for probe in (
        "Path.exists",
        "_plan_sync",
        "snapshot",
        "_write_pointer_sync",
        "stage.<locals>._write",
    ):
        assert probe in joined, f"{probe} must be offloaded; submitted: {submitted!r}"
    assert any(
        "<lambda>" in name for name in submitted
    ), f"the (stubbed) _in_pod probe must be offloaded; submitted: {submitted!r}"


@pytest.mark.asyncio
async def test_gateway_pointer_flows_call_no_filesystem_primitive_on_the_loop_thread(
    monkeypatch, tmp_path
):
    """The catch-all the allow-list above cannot be: instead of naming the probes
    that MUST be offloaded, trap the primitives every probe bottoms out in
    (``os.stat``/``lstat``, ``readlink``, ``os.path.exists``/``realpath``/
    ``samefile``, ``shutil.which``) and fail if ANY of them runs on the loop thread
    while the gateway-side flows run — the live-target read (``pointer_state``), the
    service-manager resolution behind it and the cutover itself. A primitive nobody
    enumerated is exactly the one the next review finds."""
    # Fixture work first: it resolves paths on this very thread, which the traps
    # below must not count.
    expected_live = str(tmp_path.resolve())
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)

    which_threads: list[int] = []

    def _which(_name):
        which_threads.append(threading.get_ident())
        return "/usr/bin/systemctl"

    async def fake_run_cmd(cmd, **kw):
        return (0, f"{tmp_path}\n", "")

    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="linux"))
    monkeypatch.setattr(live_mod, "shutil", MagicMock(which=_which))
    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    monkeypatch.setattr(live_mod, "_POINTER_PROVIDER", None, raising=False)
    monkeypatch.setattr(live_mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(live_mod, "_LIVE_WORKTREE", None, raising=False)
    monkeypatch.setattr(live_mod.live_target, "read_target", lambda: None)

    loop_thread = threading.get_ident()
    on_loop: list[str] = []

    # The trap stays a catch-all: it must still see EVERY product filesystem
    # read on the loop thread, including ones outside ``tmp_path`` — the
    # isolation fixture's data-home pointer (``read_previous_target`` /
    # ``_staged_target``) and the running checkout's ``src/`` that steps 1–2
    # reach before the cutover stub redirects them. The only calls that must not
    # count are pytest's own lazy imports: under ``-n auto`` the first test in a
    # worker to touch ``pygments`` / ``_pytest.assertion.rewrite`` / ``_pytest._io``
    # triggers ``lstat``/``realpath`` walks over the interpreter and
    # ``site-packages`` to resolve each module's ``__file__``; when that lands on
    # the loop thread inside the trapped window a path-blind trap miscounts them
    # as product work. So EXCLUDE (never allow-list): drop a call only when its
    # path is under the interpreter prefixes or a ``site-packages``/
    # ``dist-packages`` tree. Everything else — any product path, anywhere — is
    # still recorded, so a regression that moves a real read onto the loop still
    # fails the test.
    #
    # Classification is pure string work (``abspath``/``normpath``, no
    # ``stat``/``lstat``/``realpath``), so it never re-enters one of the traps.
    _real_fspath = os.fspath
    _real_fsdecode = os.fsdecode

    def _norm(p: str) -> str:
        return os.path.normpath(os.path.abspath(p))

    _interp_roots = {
        _norm(p)
        for p in (
            sys.prefix,
            sys.base_prefix,
            sys.exec_prefix,
            sys.base_exec_prefix,
            # The stdlib location (dir holding ``os.py``); its ``__file__`` import
            # walks are tool noise, not product reads.
            os.path.dirname(os.__file__ or ""),
        )
        if p
    }

    def _is_tooling_path(resolved: str) -> bool:
        parts = resolved.split(os.sep)
        if "site-packages" in parts or "dist-packages" in parts:
            return True
        for root in _interp_roots:
            if resolved == root or resolved.startswith(root + os.sep):
                return True
        return False

    def _should_record(arg) -> bool:
        if isinstance(arg, bytes):
            try:
                arg = _real_fsdecode(arg)
            except Exception:
                return True
        if not isinstance(arg, (str, os.PathLike)):
            # A file-descriptor (int) or anything unresolvable to a path: it is
            # not an interpreter/tooling import walk, so count it conservatively.
            return True
        try:
            resolved = _norm(_real_fspath(arg))
        except Exception:
            return True
        return not _is_tooling_path(resolved)

    def _trap(module, name):
        real = getattr(module, name)

        def _wrapped(*args, **kwargs):
            if threading.get_ident() == loop_thread and args and _should_record(args[0]):
                on_loop.append(f"{module.__name__}.{name}{args[:1]!r}")
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, _wrapped)

    for name in ("stat", "lstat", "readlink", "listdir", "scandir"):
        _trap(os, name)
    for name in ("exists", "realpath", "samefile", "isdir"):
        _trap(os.path, name)
    # ``Path.read_text``/``open()`` bottom out in ``io.open`` (``builtins.open`` is
    # the same callable bound separately, so both names are trapped).
    import builtins
    import io

    _trap(io, "open")
    _trap(builtins, "open")

    # 1. The service-manager fallthrough of ``_live_worktree_path`` (no pointer).
    assert await mod._live_worktree_path(fresh=True) == expected_live
    assert which_threads and all(
        t != loop_thread for t in which_threads
    ), "shutil.which walks PATH; it must run off the loop"

    # 2. The read broker the GET live-target route awaits.
    monkeypatch.setattr(live_mod, "_LIVE_WORKTREE", None, raising=False)
    await live_mod.pointer_state(fresh=True)
    assert on_loop == [], f"filesystem primitives ran on the loop thread: {on_loop!r}"

    # 3. The cutover. The stub replaces the live-path resolution the first two steps
    # exercised, so it goes in only now; its own fixture-time path work is not the
    # production flow and is discounted.
    _stub_make_live(monkeypatch, wt, pointer_dir=tmp_path / "ptr")
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_LOCK", asyncio.Lock())
    on_loop.clear()
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True, res

    assert on_loop == [], f"filesystem primitives ran on the loop thread: {on_loop!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["linux", "darwin"])
async def test_service_backend_availability_probe_runs_off_the_loop_thread(platform, tmp_path):
    """Both service backends gate every state read on ``which(<manager>)``. The
    injected ``which`` is ``shutil.which`` in production — a PATH walk of
    stat/access calls — so it must hop to the executor like the drop-in and
    plist work already does; the restart route, the status probe and the
    restart-gateway handshake all sit behind it on the gateway's loop."""
    loop_thread = threading.get_ident()
    which_threads: list[int] = []

    def _which(name):
        which_threads.append(threading.get_ident())
        return f"/usr/bin/{name}"

    async def _run(cmd, **kw):
        return (0, "12345\n", "")

    be = live_mod.gateway_service.backend(
        _run,
        unit=lambda: "kirocrew.service",
        label=lambda: "dev.kirocrew.gateway",
        platform=platform,
        which=_which,
        dropin_path=lambda: tmp_path / "override.conf",
        dropin_content=lambda _wt, _exe: "",
    )
    assert be is not None
    await be.status()
    await be.active()
    await be.start_id()
    if hasattr(be, "reload"):
        await be.reload()

    assert len(which_threads) >= 3, which_threads
    assert all(
        t != loop_thread for t in which_threads
    ), "the availability probe walked PATH on the loop thread"
