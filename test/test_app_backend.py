"""Tests for kiro_crew.apps.backend — backend process management.

The spawn environment, the post-startup health watch, the MCP reconcile and the
enablement gate on promotion and undo live in ``test_app_backend_spawn_env.py``,
``test_app_backend_health_watch.py``, ``test_app_backend_mcp_reconcile.py`` and
``test_app_backend_promotion_gate.py``; shared fixtures and fakes in
``app_backend_helpers.py``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from app_backend_helpers import app_env_fixture  # noqa: F401
from app_backend_helpers import _make_app_with_backend
from test_update_provider import _UNALLOCATABLE_PID

from conftest import requires_symlinks
from kiro_crew.apps.backend import (
    AppProcess,
    PortUnavailableError,
    _find_free_port,
    _is_shell_entry,
    get_app_process,
    list_app_processes,
    start_app_backend,
    stop_app_backend,
)
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app


def _own_probe_sel(probe_home: str):
    """Give ``_sandbox_can_spawn`` a synchronous SEL bound under *probe_home*, or None.

    Returns the instance the probe now owns, or ``None`` when the process already
    held a singleton -- one the operator's code constructed (a ``base_dir``
    instance from a sibling module, say) is not the probe's to replace or retire,
    and ``wrap_argv()`` will simply append to it. If construction fails, the
    probe clears the partially published singleton before propagating the error.

    ``sync=True`` is the whole point: the probe's denial audit is then written
    INLINE on this thread and NO writer thread is ever started, so nothing can
    outlive the ``with TemporaryDirectory()`` holding a reference to the
    directory it is about to remove. The earlier shape retired an async instance
    after the fact with ``flush()`` + shutdown sentinel + ``join(timeout=5)``,
    and a join that times out leaves a daemon thread whose next ``_flush_batch``
    re-creates the deleted home -- the leak this exists to close, one race away.
    """
    from kiro_crew.sel import SecurityEventLog

    if SecurityEventLog._instance is not None:
        return None
    try:
        return SecurityEventLog(Path(probe_home), sync=True)
    except BaseException:
        # ``__new__`` publishes the singleton before ``_init_locked`` finishes,
        # so failed initialization can leave this probe's half-built instance.
        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        raise


def _retire_probe_sel(owned) -> None:
    """Clear the class slots for the instance ``_own_probe_sel`` returned.

    Nothing to flush or join: a ``sync=True`` instance has no queue and no
    thread. Clearing the slots lets the first test's ``sel()`` rebuild under
    the session floor's redirected default dir (``_isolate_sel_default_dir``).
    """
    from kiro_crew.sel import SecurityEventLog

    if owned is None or SecurityEventLog._instance is not owned:
        return
    SecurityEventLog._instance = None
    SecurityEventLog._initialized = False


def _sandbox_can_spawn() -> bool:
    """True if the OS sandbox can launch a surviving child on this host.

    start_app_backend() fail-closes to None when the sandbox launcher can't
    start — e.g. GitHub hosted runners allow unshare(NEWUSER) but deny the
    launcher's separate unshare(NEWNS) (errno 1). sandbox._probe_unshare() used
    to give a false positive there because it issued NEWUSER|NEWNS in a SINGLE
    unshare call, which the kernel satisfies atomically; the probe now mirrors
    the launcher's split sequence, so detect_backend() reports such hosts
    honestly. This gate still runs the production path rather than trusting any
    probe: a spawn can fail for reasons a capability probe cannot see, and
    reusing wrap_argv() means this check can never drift from
    start_app_backend().

    The probe runs under an EMPTY ``KIROCREW_HOME``. It is evaluated at
    collection, before the per-test isolation fixture pins the data home, so a
    bare ``wrap_argv()`` here reads the OPERATOR's real ``~/.kiro/crew/config.json``
    -- and on a Windows or macOS developer machine that file routinely carries
    ``agent.sandbox_allow_unsandboxed_exec=true`` (the only way Kiro Crew runs
    there). That made the probe answer "can spawn" for a host with no sandbox
    backend at all, and every test it gates then failed closed under the
    fixture's default config, while CI (no operator config) skipped them. The
    gate must observe what the tests will observe: the default config.

    The probe also OWNS the Security Event Log ``wrap_argv()`` writes to. On a
    host with no sandbox backend the call fail-closes and records a ``denied``
    audit through ``sel()`` -- a process SINGLETON whose ``_dir`` is bound once,
    here from ``empty_home``. Left to construct itself that instance would be
    ASYNC, and (a) its writer thread keeps the chain lock / log open long enough
    on Windows that ``TemporaryDirectory`` cannot remove ``empty_home`` -- the
    failure lands in the bare ``except`` below and the directory leaks at the
    TEMP root, one per xdist worker, holding a ``security_events.jsonl`` and a
    ``trust/sel_hmac.key`` -- and (b) it outlives the probe: the rootdir
    ``_isolate_sel_default_dir`` floor only resets the singleton at the first
    test's setup, and any write on the lingering thread ``mkdir``s the deleted
    home back into existence. MEASURED: five full runs each left ten such
    directories. So the probe constructs the singleton itself, ``sync=True``
    (inline writes, no thread), and clears it before the directory goes.
    """
    try:
        from kiro_crew import sandbox as _sb

        with tempfile.TemporaryDirectory() as empty_home:
            saved = os.environ.get("KIROCREW_HOME")
            os.environ["KIROCREW_HOME"] = empty_home
            owned = None
            cleanup = None
            try:
                owned = _own_probe_sel(empty_home)
                argv, cleanup = _sb.wrap_argv([sys.executable, "-c", "pass"], mode="standard")
            finally:
                if saved is None:
                    os.environ.pop("KIROCREW_HOME", None)
                else:
                    os.environ["KIROCREW_HOME"] = saved
                _retire_probe_sel(owned)
            try:
                # cwd=empty_home: a child inherits pytest's CWD (the checkout)
                # unless told otherwise, so the probe child runs from the same
                # throwaway directory the rest of the probe already owns.
                return (
                    subprocess.run(argv, capture_output=True, timeout=15, cwd=empty_home).returncode
                    == 0
                )
            finally:
                if cleanup:
                    try:
                        os.unlink(cleanup)
                    except OSError:
                        pass
    except Exception:  # noqa: BLE001 — any probe failure => treat as "can't spawn"
        return False


# Evaluated once per worker at collection; the two lifecycle tests below need a
# real sandboxed backend to come up and stay up.
_needs_sandbox_spawn = pytest.mark.skipif(
    not _sandbox_can_spawn(),
    reason="OS sandbox cannot spawn a surviving child here (e.g. GitHub hosted "
    "runners deny unshare(NEWNS)); start_app_backend() correctly fail-closes to None",
)


class TestPortAllocation:
    def test_find_free_port(self, app_env):
        import kiro_crew.apps.backend as bmod

        port = _find_free_port()
        # Bounds read off the module, not the 9100/9200 literals: ``app_env`` gives
        # each xdist worker a DISJOINT window, so a hardcoded range would fail on
        # every worker but gw0.
        assert bmod._MIN_PORT <= port <= bmod._MAX_PORT

    def test_concurrent_allocation_never_hands_out_the_same_port(self, app_env, monkeypatch):
        """Parallel boot spawns must not collide on one auto-allocated port.

        Boot starts app backends concurrently, so two apps can select a port at
        the same time. The allocation is reserve-then-return under one lock; if it
        were not, both children would bind the same port and the loser would
        crash-loop with EADDRINUSE.
        """
        import threading

        import kiro_crew.apps.backend as bmod

        with bmod._lock:
            bmod._allocated_ports.clear()
        ports: list[int] = []
        errors: list[BaseException] = []
        start = threading.Barrier(8)

        def _grab(idx: int) -> None:
            try:
                start.wait(timeout=5)
                ports.append(bmod._reserve_free_port(f"racer-{idx}"))
            except BaseException as exc:  # noqa: BLE001 — surface to the assert
                errors.append(exc)

        threads = [threading.Thread(target=_grab, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        try:
            assert not errors, errors
            assert len(ports) == 8
            assert len(set(ports)) == 8, f"duplicate port handed out: {sorted(ports)}"
        finally:
            with bmod._lock:
                for i in range(8):
                    bmod._allocated_ports.pop(f"racer-{i}", None)


def _drive_spawn_to_publication(monkeypatch, name, *, wrap_marks_sandbox):
    """Run ``start_app_backend`` to a published record, stubbing the spawn seams.

    ``wrap_marks_sandbox`` chooses whether the stub ``wrap_argv`` returns a
    launcher cleanup path alongside its wrapped argv or None with an unchanged argv.
    On Linux that cleanup path identifies the generated forking namespace launcher.
    Everything after the Popen boundary is faked so the body reaches publication.
    """
    import kiro_crew.apps.backend as bmod

    def _wrap(argv, **k):
        if wrap_marks_sandbox:
            return (["/run/kirocrew_sandbox_1.py", *argv], "/run/kirocrew_sandbox_1.py")
        return (list(argv), None)

    class _AliveProc:
        # The fixture's teardown hands this to the REAL ``stop_app_backend`` kill
        # path, so the pid must be one no live process can own.
        pid = _UNALLOCATABLE_PID

        def poll(self):
            return None

    monkeypatch.setattr(bmod, "_app_enabled_state", lambda n: True)
    monkeypatch.setattr(bmod, "wrap_argv", _wrap)
    monkeypatch.setattr(bmod, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(bmod, "popen_limited", lambda *a, **k: _AliveProc())
    monkeypatch.setattr(bmod, "_survived_spawn", lambda proc, port: True)
    monkeypatch.setattr(bmod, "_record_app_pid", lambda *a, **k: "ST-4242")
    monkeypatch.setattr(bmod, "_start_health_supervisor", lambda ap, hc: None)
    bmod.start_app_backend(name)
    return bmod._processes.get(name)


def test_spawn_sets_forking_launcher_true_for_the_namespace_launcher_wrap(
    tmp_path, app_env, monkeypatch
):
    """The spawn path identifies the forking launcher from its cleanup path.

    On Linux a non-null cleanup path names the generated namespace launcher, so the
    published record carries ``forking_sandbox_launcher=True`` and
    ``running_spawned_backend_pids`` will widen to the launcher's forked server child.
    The predicate is ``IS_LINUX and cleanup_path is not None``; on a non-Linux host
    the seatbelt profile's cleanup path does not mark a forking launcher, so the flag
    stays False -- which is what this asserts against the live platform.
    """
    from kiro_crew import platform_compat

    src = tmp_path / "source" / "ns-app"
    src.mkdir(parents=True)
    (src / APP_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": "ns-app",
                "version": "1.0.0",
                "displayName": "NS",
                "description": "namespace launcher",
                "backend": {"entryPoint": "server.py", "healthCheck": "/health"},
            }
        )
    )
    (src / "server.py").write_text("import time\ntime.sleep(30)\n")
    install_app(src)

    ap = _drive_spawn_to_publication(monkeypatch, "ns-app", wrap_marks_sandbox=True)
    assert ap is not None, "the spawn never published a record"
    assert ap.forking_sandbox_launcher is platform_compat.IS_LINUX


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the namespace launcher is POSIX-only: rendering it reads os.getuid",
)
@pytest.mark.parametrize("tier", ["strict", "standard", "cc"])
def test_the_namespace_launcher_forks_once_and_its_parent_only_waits(tier):
    """Pins the launcher shape that ``forking_sandbox_launcher`` relies on.

    For a record marked ``forking_sandbox_launcher``,
    ``running_spawned_backend_pids`` claims the Popen root's direct children,
    because the generated namespace launcher forks exactly once, its child execs
    the server, and its parent only waits for that child. A launcher that gained
    or lost a fork layer would make that claim over- or under-reach with no other
    test turning red, so revisit the children claim in
    ``apps/backend_runtime/tracking.py`` before changing this shape.
    """
    import ast

    from kiro_crew import sandbox

    tree = ast.parse(sandbox._build_launcher_script(tier))
    called = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    process_makers = {
        c
        for c in called
        if c.startswith(("os.fork", "os.spawn", "os.posix_spawn", "os.exec", "subprocess."))
        or c in ("os.system", "os.popen")
    }
    assert process_makers == {"os.fork", "os.execvp"}
    assert called.count("os.fork") == 1

    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    parent = next(
        n for n in ast.walk(main) if isinstance(n, ast.If) and ast.unparse(n.test) == "pid > 0"
    )
    parent_calls = {
        ast.unparse(c.func) for s in parent.body for c in ast.walk(s) if isinstance(c, ast.Call)
    }
    assert "os.waitpid" in parent_calls
    assert not parent_calls & process_makers
    assert ast.unparse(parent.body[-1]).startswith("sys.exit(")
    # The child carries on past the parent's branch: its exec is reached through
    # ``run_child``, whose last statement is ``exec_agent``, the one ``os.execvp``.
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    child = main.body[main.body.index(parent) + 1 :]
    child_calls = {
        ast.unparse(c.func) for s in child for c in ast.walk(s) if isinstance(c, ast.Call)
    }
    assert "run_child" in child_calls and not child_calls & process_makers
    assert ast.unparse(functions["run_child"].body[-1]) == "exec_agent(launch, argv)"
    exec_calls = {
        ast.unparse(c.func) for c in ast.walk(functions["exec_agent"]) if isinstance(c, ast.Call)
    }
    assert "os.execvp" in exec_calls


def test_spawn_sets_forking_launcher_false_for_a_noop_wrap(
    tmp_path, app_env, monkeypatch
):
    """MUTATION TARGET: an unconfined (no-op wrap) spawn leaves the flag False.

    When ``wrap_argv`` returns the argv unchanged -- an unconfined host -- the root IS
    the server, so the record must NOT be marked and no children are ever claimed.
    """
    src = tmp_path / "source" / "plain-app"
    src.mkdir(parents=True)
    (src / APP_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": "plain-app",
                "version": "1.0.0",
                "displayName": "Plain",
                "description": "no sandbox",
                "backend": {"entryPoint": "server.py", "healthCheck": "/health"},
            }
        )
    )
    (src / "server.py").write_text("import time\ntime.sleep(30)\n")
    install_app(src)

    ap = _drive_spawn_to_publication(monkeypatch, "plain-app", wrap_marks_sandbox=False)
    assert ap is not None, "the spawn never published a record"
    assert ap.forking_sandbox_launcher is False


def test_an_app_entry_point_named_like_the_launcher_does_not_set_the_forking_flag(
    tmp_path, app_env, monkeypatch
):
    """An app-controlled entry-point name cannot confer ownership of its children."""
    from kiro_crew import platform_compat, sandbox
    from kiro_crew.apps.backend import running_spawned_backend_pids

    src = tmp_path / "source" / "launcher-named-app"
    src.mkdir(parents=True)
    entry_point = "kirocrew_sandbox_main.py"
    (src / APP_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": "launcher-named-app",
                "version": "1.0.0",
                "displayName": "Launcher-named app",
                "description": "no sandbox",
                "backend": {"entryPoint": entry_point, "healthCheck": "/health"},
            }
        )
    )
    (src / entry_point).write_text("import time\ntime.sleep(30)\n")
    install_app(src)

    ap = _drive_spawn_to_publication(monkeypatch, "launcher-named-app", wrap_marks_sandbox=False)
    assert ap is not None, "the spawn never published a record"
    assert ap.forking_sandbox_launcher is False
    assert sandbox.wrapped_by_crew_sandbox([sys.executable, str(src / entry_point)]) is True
    monkeypatch.setattr(
        platform_compat, "_proc_children", lambda pid: [ap.pid + 1] if pid == ap.pid else []
    )
    assert running_spawned_backend_pids() == {ap.pid}


class TestFixedAndAutoPortIsolation:
    def test_boot_preclaims_fixed_ports_before_any_spawn(self, monkeypatch):
        """A declared fixed port must not be lost to a concurrent auto-port app.

        A fixed port is a requirement, not a preference. Without pre-claiming, an
        auto worker can select that exact number first and the fixed app is then
        refused even though other ports were free — an enabled backend left down.
        """
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        fixed = bmod._MIN_PORT + 3
        manifests = {
            "fixed-app": SimpleNamespace(
                backend=SimpleNamespace(port=str(fixed), entryPoint="s.py")
            ),
            "auto-app": SimpleNamespace(
                backend=SimpleNamespace(port="auto", entryPoint="s.py")
            ),
        }
        monkeypatch.setattr(bmod, "get_app_manifest", lambda n: manifests.get(n))

        seen: dict[str, int | None] = {}

        def _fake_start(app_name: str):
            if app_name == "auto-app":
                # What a concurrent auto spawn sees must already exclude `fixed`.
                seen["reserved"] = bmod._allocated_ports.get("fixed-app")
            return AppProcess(app_name=app_name, port=1, pid=1, healthy=True)

        monkeypatch.setattr(bmod, "start_app_backend", _fake_start)
        with bmod._lock:
            bmod._allocated_ports.clear()
        try:
            started = bmod._start_backends_concurrently(["auto-app", "fixed-app"])
            assert sorted(started) == ["auto-app", "fixed-app"]
            assert seen.get("reserved") == fixed, (
                "the fixed port was not reserved before spawns were submitted, so a "
                "concurrent auto-port app could still take it"
            )
        finally:
            with bmod._lock:
                bmod._allocated_ports.clear()

    def test_preclaim_tolerates_unreadable_or_invalid_manifests(self, monkeypatch):
        """Pre-claiming is best-effort: it must never itself fail boot."""
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        manifests = {
            "no-manifest": None,
            "bad-port": SimpleNamespace(backend=SimpleNamespace(port="not-a-number")),
            "out-of-range": SimpleNamespace(backend=SimpleNamespace(port="1")),
        }
        monkeypatch.setattr(bmod, "get_app_manifest", lambda n: manifests.get(n))
        with bmod._lock:
            bmod._allocated_ports.clear()
        try:
            bmod._preclaim_fixed_ports(list(manifests))  # must not raise
            assert bmod._allocated_ports == {}
        finally:
            with bmod._lock:
                bmod._allocated_ports.clear()

    def test_a_fixed_port_app_claims_it_before_binding(self, tmp_path, app_env, monkeypatch):
        """The SPAWN PATH must claim a fixed manifest port, not just record it later.

        Boot spawns concurrently, and a fixed-port app that records its port only
        AFTER binding. An auto-port app selecting inside that window could be handed
        the same number, so one of the two children would die of EADDRINUSE and its
        backend would stay unavailable. Asserted at the real seam: the port must
        already be reserved by the time the spawn body runs.
        """
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        # Stub the OS sandbox: these tests are about PORT bookkeeping, and
        # wrap_argv() fail-closes before that code on hosts without a backend
        # (e.g. native Windows), which would otherwise skip the coverage.
        monkeypatch.setattr(bmod, "wrap_argv", lambda argv, **k: (list(argv), None))
        fixed = bmod._MIN_PORT + 7
        src = tmp_path / "source" / "fixed-app"
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text(json.dumps({
            "name": "fixed-app", "version": "1.0.0",
            "displayName": "Fixed", "description": "fixed port",
            "backend": {
                "entryPoint": "server.py",
                "port": str(fixed),
                "healthCheck": "/health",
            },
        }))
        (src / "server.py").write_text("import time\ntime.sleep(30)\n")
        install_app(src)

        # Freeze the spawn right after port resolution and inspect the reservation
        # a concurrent auto-port app would see at that instant.
        seen: dict[str, int | None] = {}

        def _spy_popen(*a, **k):
            seen["reserved"] = bmod._allocated_ports.get("fixed-app")
            raise OSError("stop here — we only needed the pre-bind state")

        monkeypatch.setattr(bmod.subprocess, "Popen", _spy_popen)
        bmod.start_app_backend("fixed-app")

        assert seen.get("reserved") == fixed, (
            "fixed port was not reserved before the bind, so a concurrent auto-port "
            f"app could be handed {fixed} too (saw {seen.get('reserved')!r})"
        )

    def test_a_failed_spawn_releases_its_port_reservation(self, tmp_path, app_env, monkeypatch):
        """A failed spawn must not retire its port from the pool.

        Ports are now reserved BEFORE the bind (so concurrent boot cannot double-
        allocate), so a failure that kept the reservation would permanently burn
        that port — and a gateway retrying a broken app would leak one per attempt.
        """
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        # Stub the OS sandbox: these tests are about PORT bookkeeping, and
        # wrap_argv() fail-closes before that code on hosts without a backend
        # (e.g. native Windows), which would otherwise skip the coverage.
        monkeypatch.setattr(bmod, "wrap_argv", lambda argv, **k: (list(argv), None))
        src = _make_app_with_backend(tmp_path, name="doomed-app")
        install_app(src)
        # Let the real body run far enough to RESERVE a port, then fail the spawn.
        # (Stubbing the whole body would reserve nothing and prove nothing.)
        reserved: dict[str, int] = {}
        real_reserve = bmod._reserve_free_port

        def _spy_reserve(app_name: str) -> int:
            port = real_reserve(app_name)
            reserved[app_name] = port
            return port

        monkeypatch.setattr(bmod, "_reserve_free_port", _spy_reserve)
        monkeypatch.setattr(
            bmod.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
        )

        assert bmod.start_app_backend("doomed-app") is None
        assert reserved.get("doomed-app"), "the spawn must have reserved a port to release"
        assert "doomed-app" not in bmod._allocated_ports, (
            "a failed spawn leaked its port reservation — that port is now retired "
            "from the pool for the life of the process"
        )
        assert "doomed-app" not in bmod._processes

    def test_a_fixed_port_already_taken_by_another_app_is_refused(self):
        """Claiming a fixed port must FAIL when another app already holds it.

        Fixed manifest ports are required to sit inside the auto range
        (_MIN_PORT.._MAX_PORT), so under concurrent boot an auto app can reserve
        the very number a fixed-port app declares. Recording the claim anyway
        leaves two apps mapped to one port; both children then bind it and the
        loser dies of EADDRINUSE, staying unavailable.
        """
        import kiro_crew.apps.backend as bmod

        with bmod._lock:
            bmod._allocated_ports.clear()
        try:
            taken = bmod._reserve_free_port("auto-app")
            with pytest.raises(PortUnavailableError):
                bmod._claim_port("fixed-app", taken)
            # The loser must not be left holding a duplicate mapping.
            assert list(bmod._allocated_ports.values()).count(taken) == 1
            assert "fixed-app" not in bmod._allocated_ports
        finally:
            with bmod._lock:
                bmod._allocated_ports.clear()

    def test_reclaiming_your_own_fixed_port_is_idempotent(self):
        """A restart/retry of the SAME app must not be refused its own port."""
        import kiro_crew.apps.backend as bmod

        with bmod._lock:
            bmod._allocated_ports.clear()
        try:
            bmod._claim_port("fixed-app", bmod._MIN_PORT)
            bmod._claim_port("fixed-app", bmod._MIN_PORT)  # must not raise
            assert bmod._allocated_ports["fixed-app"] == bmod._MIN_PORT
        finally:
            with bmod._lock:
                bmod._allocated_ports.clear()

    def test_auto_allocation_skips_ports_claimed_by_other_apps(self):
        """The free-port scan must honor claims, not just live sockets."""
        import kiro_crew.apps.backend as bmod

        with bmod._lock:
            bmod._allocated_ports.clear()
        try:
            claimed = {bmod._MIN_PORT, bmod._MIN_PORT + 1}
            for i, port in enumerate(sorted(claimed)):
                bmod._claim_port(f"claimer-{i}", port)
            got = bmod._reserve_free_port("late-app")
            assert got not in claimed
        finally:
            with bmod._lock:
                bmod._allocated_ports.clear()


def _slow_never_owns(port, pid):
    """Stand-in for the real lsof-backed ownership probe's cost (~150ms)."""
    time.sleep(0.15)
    return False


def _frozen_spawn_time() -> SimpleNamespace:
    """Keep poll-count scenarios independent of host wall-clock scheduling."""
    return SimpleNamespace(
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
    )


class TestBootSpawnLatency:
    def test_survival_check_exits_early_for_a_healthy_child(self, monkeypatch):
        """A living child must not cost the full survival window.

        The poll would sleep its whole ~1.6s budget on the happy path and only
        break when the child DIED, so every app added ~1.6s of dead time to boot.
        It must return as soon as the child is confirmed alive.
        """
        import time as real_time

        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        # OUR child owns the listener — the very bind whose failure this guards.
        monkeypatch.setattr(bmod, "_port_is_listening", lambda port: True)
        monkeypatch.setattr(bmod, "_spawn_owns_listener", lambda port, pid: True)

        class _Alive:
            pid = 4242

            def poll(self) -> None:
                return None

        started = real_time.monotonic()
        assert bmod._survived_spawn(_Alive(), 9100) is True
        elapsed = real_time.monotonic() - started
        budget = bmod._SPAWN_SURVIVAL_CHECKS * bmod._SPAWN_SURVIVAL_INTERVAL
        assert elapsed < budget, (
            f"healthy child burned {elapsed:.2f}s of a {budget:.2f}s budget"
        )

    def test_survival_check_still_detects_a_late_exit(self, monkeypatch):
        """Liveness alone must NOT end the wait early.

        A child that crashes a few polls in (slow sandboxed interpreter on a loaded
        host) must be reported as failed. Only OUR child owning the listener —
        positive evidence that its own bind succeeded — may short-circuit.
        """
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        monkeypatch.setattr(bmod, "time", _frozen_spawn_time())
        monkeypatch.setattr(bmod.platform_compat, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(bmod, "_port_is_listening", lambda port: True)
        monkeypatch.setattr(bmod, "_spawn_owns_listener", lambda port, pid: False)

        class _DiesLate:
            pid = 4242

            def __init__(self) -> None:
                self.calls = 0

            def poll(self):
                self.calls += 1
                return None if self.calls < 3 else 1

        assert bmod._survived_spawn(_DiesLate(), 9100) is False

    def test_early_exit_requires_our_own_child_to_own_the_listener(self, monkeypatch):
        """A listener owned by SOMEONE ELSE must not count as our bind.

        Two apps on the same fixed port (or any unrelated process already holding
        it) would otherwise let the LOSER pass this probe while it is still alive
        and about to die of EADDRINUSE — reporting a doomed pid as started and
        routing two apps at one backend.
        """
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        monkeypatch.setattr(bmod, "time", _frozen_spawn_time())
        # Something is listening, but it is not our child (nor its descendant).
        monkeypatch.setattr(bmod.platform_compat, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(bmod, "_port_is_listening", lambda port: True)
        monkeypatch.setattr(bmod, "_listening_pids", lambda port: [99999])
        monkeypatch.setattr(bmod, "_pid_is_self_or_descendant_of", lambda pid, ancestor: False)

        class _DiesOfCollision:
            pid = 4242

            def __init__(self) -> None:
                self.calls = 0

            def poll(self):
                self.calls += 1
                return None if self.calls < 4 else 1

        assert bmod._survived_spawn(_DiesOfCollision(), 9100) is False

    def test_early_exit_accepts_a_listener_owned_by_a_descendant(self, monkeypatch):
        """The sandbox launcher execs the real server as a CHILD of our pid.

        Ownership must therefore be satisfied by our pid OR any descendant of it,
        or the early exit would never fire in production (where the listening pid
        is the launcher's child, not the pid Popen returned).
        """
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        monkeypatch.setattr(bmod, "time", _frozen_spawn_time())
        monkeypatch.setattr(bmod.platform_compat, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(bmod, "_port_is_listening", lambda port: True)
        monkeypatch.setattr(bmod, "_listening_pids", lambda port: [4243])
        monkeypatch.setattr(
            bmod,
            "_pid_is_self_or_descendant_of",
            lambda pid, ancestor: pid == 4243 and ancestor == 4242,
        )

        class _DiesAfterBind:
            pid = 4242

            def __init__(self) -> None:
                self.calls = 0

            def poll(self):
                self.calls += 1
                return None if self.calls == 1 else 1

        proc = _DiesAfterBind()
        assert bmod._survived_spawn(proc, 9100) is True
        assert proc.calls == 1

    def test_failure_path_never_exceeds_the_original_budget(self):
        """The ownership probe must not stretch the wait it is embedded in.

        The probe shells out to lsof (~150ms). Charging it to every poll interval
        made the FAILURE path take ~2x the original 1.6s budget — i.e. the boot fix
        would have regressed boot for exactly the apps that are slowest to start.
        The loop is wall-clock bounded, so a slow probe cannot extend it.
        """
        import time as real_time

        import kiro_crew.apps.backend as bmod

        class _Alive:
            pid = 4242

            def poll(self) -> None:
                return None

        # A listener exists but is never ours, so every poll runs the slow probe.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bmod, "_port_is_listening", lambda port: True)
            mp.setattr(bmod, "_spawn_owns_listener", _slow_never_owns)
            started = real_time.monotonic()
            assert bmod._survived_spawn(_Alive(), 9100) is True
            elapsed = real_time.monotonic() - started

        budget = bmod._SPAWN_SURVIVAL_CHECKS * bmod._SPAWN_SURVIVAL_INTERVAL
        assert elapsed < budget * 1.6, (
            f"failure path took {elapsed:.2f}s against a {budget:.2f}s budget"
        )

    def test_survival_check_without_a_port_polls_the_full_budget(self):
        """No port to observe → unchanged behavior (wait out the whole window)."""
        import time as real_time

        import kiro_crew.apps.backend as bmod

        class _Alive:
            pid = 4242

            def poll(self) -> None:
                return None

        started = real_time.monotonic()
        assert bmod._survived_spawn(_Alive(), None) is True
        elapsed = real_time.monotonic() - started
        budget = bmod._SPAWN_SURVIVAL_CHECKS * bmod._SPAWN_SURVIVAL_INTERVAL
        assert elapsed >= budget * 0.9, "must not short-circuit without a port"

    def test_ownership_check_degrades_to_the_full_poll_without_lsof(self, monkeypatch):
        """No port->PID tool → cannot prove ownership → keep the old behavior."""
        import time as real_time

        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        monkeypatch.setattr(
            bmod.platform_compat, "listening_pid_tool_available", lambda: False
        )
        # Would short-circuit if consulted; it must not be.
        monkeypatch.setattr(bmod, "_port_is_listening", lambda port: True)
        monkeypatch.setattr(bmod, "_spawn_owns_listener", lambda port, pid: True)

        class _Alive:
            pid = 4242

            def poll(self) -> None:
                return None

        started = real_time.monotonic()
        assert bmod._survived_spawn(_Alive(), 9100) is True
        elapsed = real_time.monotonic() - started
        budget = bmod._SPAWN_SURVIVAL_CHECKS * bmod._SPAWN_SURVIVAL_INTERVAL
        assert elapsed >= budget * 0.9, "no ownership tool → must not short-circuit"

    def test_boot_starts_app_backends_concurrently(self, monkeypatch):
        """Boot must not serialize per-app spawn latency.

        N apps would cost N x the survival window because each spawn ran to
        completion before the next began. With 4 apps that is ~6.4s of pure boot
        latency on the happy path.
        """
        import threading

        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        names = [f"par-app-{i}" for i in range(4)]
        concurrent = threading.Barrier(len(names), timeout=10)

        def _fake_start(app_name: str):
            # Every spawn must be in flight at the same moment, or this blocks
            # until the barrier times out and raises.
            concurrent.wait()
            return AppProcess(app_name=app_name, port=9100, pid=1, healthy=True)

        monkeypatch.setattr(bmod, "start_app_backend", _fake_start)
        started = bmod._start_backends_concurrently(names)
        assert sorted(started) == sorted(names)

    def test_concurrent_boot_isolates_a_single_app_failure(self, monkeypatch):
        """One app's spawn failure must never take down the others (or boot)."""
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        def _fake_start(app_name: str):
            if app_name == "bad-app":
                raise RuntimeError("sandbox unavailable")
            if app_name == "none-app":
                return None
            return AppProcess(app_name=app_name, port=9100, pid=1, healthy=True)

        monkeypatch.setattr(bmod, "start_app_backend", _fake_start)
        started = bmod._start_backends_concurrently(["ok-app", "bad-app", "none-app"])
        assert started == ["ok-app"]


class TestAppProcess:
    def test_to_dict(self):
        ap = AppProcess(app_name="test", port=9100, pid=123, healthy=True)
        d = ap.to_dict()
        assert d["app_name"] == "test"
        assert d["port"] == 9100
        assert d["healthy"] is True


class TestBackendLifecycle:
    def test_no_backend_returns_none(self, tmp_path, app_env):
        src = tmp_path / "source" / "no-backend"
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text(json.dumps({
            "name": "no-backend", "version": "1.0.0",
            "displayName": "No Backend", "description": "No backend",
        }))
        install_app(src)
        result = start_app_backend("no-backend")
        assert result is None

    @_needs_sandbox_spawn
    def test_start_and_stop(self, tmp_path, app_env):
        src = _make_app_with_backend(tmp_path)
        install_app(src)
        ap = start_app_backend("backend-app")
        assert ap is not None
        assert ap.port > 0
        assert ap.pid > 0
        procs = list_app_processes()
        assert len(procs) == 1
        assert procs[0]["app_name"] == "backend-app"
        stopped = stop_app_backend("backend-app")
        assert stopped is True
        assert list_app_processes() == []

    def test_stop_not_running(self, app_env):
        assert stop_app_backend("nonexistent") is False

    @_needs_sandbox_spawn
    @pytest.mark.skipif(sys.platform == "win32", reason="shell launchers are POSIX-only")
    def test_start_and_stop_shell_launcher(self, tmp_path, app_env):
        """An extensionless bash launcher entrypoint is exec'd directly, not fed
        to the Python interpreter (the common `bin/<name>` launcher pattern)."""
        name = "shell-app"
        src = tmp_path / "source" / name
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text(json.dumps({
            "name": name, "version": "1.0.0",
            "displayName": "Shell App", "description": "bash launcher backend",
            "author": "tester",
            "backend": {
                "entryPoint": "bin/shell-app",
                "port": "auto",
                "healthCheck": "/health",
            },
        }))
        (src / "bin").mkdir()
        launcher = src / "bin" / "shell-app"
        # Bash launcher that would die instantly under a Python interpreter
        # (`set -euo pipefail` is a SyntaxError), then execs a tiny server.
        launcher.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'exec "{sys.executable}" -c \''
            "import http.server, os\n"
            "port = int(os.environ.get(\"PORT\", 9100))\n"
            "class H(http.server.BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        self.send_response(200)\n"
            "        self.end_headers()\n"
            "        self.wfile.write(b\"ok\")\n"
            "    def log_message(self, *a):\n"
            "        pass\n"
            "http.server.HTTPServer((\"127.0.0.1\", port), H).serve_forever()\n"
            "'\n"
        )
        launcher.chmod(0o755)
        install_app(src)
        ap = start_app_backend(name)
        assert ap is not None
        assert ap.port > 0
        assert ap.pid > 0
        stopped = stop_app_backend(name)
        assert stopped is True

    @_needs_sandbox_spawn
    def test_get_process(self, tmp_path, app_env):
        src = _make_app_with_backend(tmp_path)
        install_app(src)
        start_app_backend("backend-app")
        ap = get_app_process("backend-app")
        assert ap is not None
        assert ap.app_name == "backend-app"
        stop_app_backend("backend-app")

    def test_get_process_not_running(self, app_env):
        assert get_app_process("nonexistent") is None

    def test_missing_entry_point(self, tmp_path, app_env):
        src = tmp_path / "source" / "bad-entry"
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text(json.dumps({
            "name": "bad-entry", "version": "1.0.0",
            "displayName": "Bad Entry", "description": "Missing entry",
            "backend": {"entryPoint": "nonexistent.py"},
        }))
        install_app(src)
        result = start_app_backend("bad-entry")
        assert result is None

    @requires_symlinks
    def test_backend_entrypoint_escapes_app_root(self, tmp_path, app_env, caplog):
        # The boot path (start_installed_backends) spawns persisted manifests
        # WITHOUT re-running validate(), so a manifest whose backend.entryPoint
        # resolves outside the app root (via a symlink target) must be rejected
        # by the runtime backstop in _start_app_backend_body. We materialize the
        # app dir directly (bypassing install-time validation) to exercise the
        # boot-time guard — never spawning a real process.
        from kiro_crew.apps.backend import _start_app_backend_body
        from kiro_crew.apps.manager import app_dir, get_app_manifest

        root = app_dir("escape-app")
        root.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "evil.py").write_text("import time; time.sleep(60)\n")
        # A symlink inside the app root pointing outside it — is_file() is True,
        # so only the resolve()+is_relative_to backstop catches the escape.
        (root / "server.py").symlink_to(outside / "evil.py")
        (root / APP_MANIFEST_FILENAME).write_text(json.dumps({
            "name": "escape-app", "version": "1.0.0",
            "displayName": "Escape", "description": "escapes app root",
            "backend": {"entryPoint": "server.py", "port": "auto"},
        }))
        manifest = get_app_manifest("escape-app")
        assert manifest is not None
        result = _start_app_backend_body("escape-app", manifest)
        assert result is None
        assert any("escapes app root" in r.message for r in caplog.records)

    def test_third_party_backend_refused_when_gate_off(self, tmp_path, app_env, monkeypatch, caplog):
        # The apps_allow_third_party off-switch must also block
        # the OUT-OF-PROCESS backend spawn, not just in-process module loads. A
        # file-path (third-party) backend must be refused (None, before any Popen)
        # when the switch is off.
        import logging

        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.manager import app_dir, get_app_manifest

        root = app_dir("third-party-backend")
        root.mkdir(parents=True, exist_ok=True)
        (root / "server.py").write_text("x = 1\n")
        (root / APP_MANIFEST_FILENAME).write_text(
            json.dumps(
                {
                    "name": "third-party-backend",
                    "version": "1.0.0",
                    "displayName": "TP",
                    "description": "third-party backend",
                    "backend": {"entryPoint": "server.py", "port": "auto"},
                }
            )
        )
        monkeypatch.setattr(
            "kiro_crew.apps.execution.third_party_execution_allowed", lambda: False
        )
        monkeypatch.setattr(
            bmod.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned despite gate off")
        )
        manifest = get_app_manifest("third-party-backend")
        assert manifest is not None
        with caplog.at_level(logging.WARNING):
            result = bmod._start_app_backend_body("third-party-backend", manifest)
        assert result is None
        assert any("Refusing to spawn third-party app" in r.message for r in caplog.records)

    def test_shipped_builtin_module_backend_not_blocked_by_gate(
        self, tmp_path, app_env, monkeypatch
    ):
        # The gate stays open for a real shipped builtin only when the manifest's
        # python -m target resolves inside that builtin's immutable package.
        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.manager import (
            InstalledApp,
            _write_installed,
            app_dir,
            get_app_manifest,
        )

        name = "file-explorer"
        root = app_dir(name)
        root.mkdir(parents=True, exist_ok=True)
        (root / APP_MANIFEST_FILENAME).write_text(
            json.dumps(
                {
                    "name": name,
                    "version": "1.0.0",
                    "displayName": "Files",
                    "description": "shipped builtin backend",
                    "backend": {
                        "entryPoint": "kiro_crew.apps.builtins.file_explorer.server",
                        "port": "auto",
                    },
                }
            )
        )
        _write_installed(
            name,
            InstalledApp(name=name, origin="builtin", enabled=True),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.execution.third_party_execution_allowed", lambda: False
        )

        class _ReachedSpawn(Exception):
            pass

        def _sentinel(*a, **k):
            raise _ReachedSpawn()

        # Neutralize the OS-sandbox wrap so the test isolates the third-party
        # GATE (its purpose) from sandbox availability: on a host without a
        # sandbox backend, wrap_argv now fails closed before Popen, which would
        # mask whether the gate let the builtin through.
        monkeypatch.setattr(bmod, "wrap_argv", lambda cmd, **k: (cmd, None))
        monkeypatch.setattr(bmod.subprocess, "Popen", _sentinel)
        manifest = get_app_manifest(name)
        assert manifest is not None
        # Reaching the spawn sentinel proves the immutable package proof passed.
        with pytest.raises(_ReachedSpawn):
            bmod._start_app_backend_body(name, manifest)

    def test_forged_builtin_origin_fake_name_cannot_claim_shipped_module(
        self, tmp_path, app_env, monkeypatch, caplog
    ):
        # A forged installed.json origin cannot exempt a fake app name, even
        # when its dotted entry resolves to genuine shipped builtin code.
        import logging

        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.manager import (
            InstalledApp,
            _write_installed,
            app_dir,
            get_app_manifest,
        )

        root = app_dir("evil-dotted")
        root.mkdir(parents=True, exist_ok=True)
        (root / APP_MANIFEST_FILENAME).write_text(
            json.dumps(
                {
                    "name": "evil-dotted",
                    "version": "1.0.0",
                    "displayName": "Evil",
                    "description": "forged builtin provenance",
                    "backend": {
                        "entryPoint": "kiro_crew.apps.builtins.file_explorer.server",
                        "port": "auto",
                    },
                }
            )
        )
        _write_installed(
            "evil-dotted",
            InstalledApp(name="evil-dotted", origin="builtin", enabled=True),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.execution.third_party_execution_allowed", lambda: False
        )
        monkeypatch.setattr(
            bmod.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned despite gate off")
        )
        manifest = get_app_manifest("evil-dotted")
        assert manifest is not None
        with caplog.at_level(logging.WARNING):
            result = bmod._start_app_backend_body("evil-dotted", manifest)
        assert result is None
        assert any("Refusing to spawn third-party app" in r.message for r in caplog.records)

    def test_forged_builtin_origin_real_name_cannot_claim_installed_file(
        self, tmp_path, app_env, monkeypatch, caplog
    ):
        import logging

        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.manager import (
            InstalledApp,
            _write_installed,
            app_dir,
            get_app_manifest,
        )

        name = "file-explorer"
        root = app_dir(name)
        root.mkdir(parents=True, exist_ok=True)
        (root / "server.py").write_text("raise AssertionError('must not execute')\n")
        (root / APP_MANIFEST_FILENAME).write_text(
            json.dumps(
                {
                    "name": name,
                    "version": "1.0.0",
                    "displayName": "Forged Files",
                    "description": "real builtin name with mutable code",
                    "backend": {"entryPoint": "server.py", "port": "auto"},
                }
            )
        )
        _write_installed(
            name,
            InstalledApp(name=name, origin="builtin", enabled=True),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.execution.third_party_execution_allowed", lambda: False
        )
        monkeypatch.setattr(
            bmod.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned mutable code")
        )
        manifest = get_app_manifest(name)
        assert manifest is not None
        with caplog.at_level(logging.WARNING):
            result = bmod._start_app_backend_body(name, manifest)
        assert result is None
        assert any("Refusing to spawn third-party app" in r.message for r in caplog.records)

    @_needs_sandbox_spawn
    def test_immediate_exit_is_not_reported_as_started(self, tmp_path, app_env, monkeypatch):
        # A backend that dies right away (e.g. EADDRINUSE port collision) must NOT be
        # reported as started — otherwise the gateway proxies to a dead port (502) and
        # respawns onto the same doomed port forever (the crash-loop we hit). The spawn
        # verifies the child survived its bind; an immediate exit → None + cleared state.
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        # Widen the survival-check grace window for this test only. The boom.py child
        # exits immediately ONCE it runs, but under heavy pytest-xdist parallelism
        # (-n auto, ~32 workers) the sandboxed interpreter can take well over the default
        # 1.6s window just to start, so proc.poll() still reports it alive across the
        # whole default window and the dying process gets mis-reported as 'started'
        # (flaky failure on loaded build hosts). The poll loop breaks as soon as the
        # child exits, so a longer ceiling only costs wall-time when the host is starved.
        monkeypatch.setattr(bmod, "_SPAWN_SURVIVAL_CHECKS", 100)  # up to ~20s ceiling
        src = tmp_path / "source" / "die-app"
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text(json.dumps({
            "name": "die-app", "version": "1.0.0",
            "displayName": "Die", "description": "exits immediately",
            "backend": {"entryPoint": "boom.py", "port": "auto", "healthCheck": "/health"},
        }))
        # boom.py: a backend that dies the instant it runs. The stderr line mimics
        # the real EADDRINUSE crash this test guards against, but is cosmetic here —
        # the test asserts on the None return, not the log contents. It is a
        # deliberate fake, not a real bind; fixture stderr like this is the kind of
        # thing that can mislead a static analyzer into flagging a phantom port.
        (src / "boom.py").write_text(
            'import sys\n'
            'sys.stderr.write("OSError: [Errno 98] address already in use\\n")\n'
            'sys.exit(1)\n'
        )
        install_app(src)
        result = start_app_backend("die-app")
        assert result is None
        # the STARTING placeholder was cleared — a later retry isn't wedged
        assert "die-app" not in bmod._processes

    def test_concurrent_starts_single_flight_one_spawn(self, tmp_path, app_env, monkeypatch):
        # Two concurrent start_app_backend calls for the same app must not both spawn
        # onto the same auto-allocated port (the TOCTOU that crash-looped the loser).
        # The STARTING placeholder single-flights them: exactly one spawn body runs,
        # both callers converge on the SAME resolved process. We mock the spawn body so
        # the test exercises the COORDINATION (placeholder + await) without two real
        # sandboxed os.fork()s racing (a fork-in-threads deadlock unrelated to this fix).
        import threading

        import kiro_crew.apps.backend as bmod

        src = _make_app_with_backend(tmp_path)
        install_app(src)

        spawn_calls = {"n": 0}
        gate = threading.Event()

        def _fake_body(app_name, manifest):
            spawn_calls["n"] += 1
            gate.wait(timeout=5)  # hold the placeholder in-flight while the 2nd call arrives
            ap = AppProcess(app_name=app_name, port=9137, pid=4242, healthy=True,
                            started_at=0.0)
            with bmod._lock:
                bmod._processes[app_name] = ap
                bmod._allocated_ports[app_name] = 9137
            return ap

        monkeypatch.setattr(bmod, "_start_app_backend_body", _fake_body)

        results: list = []
        barrier = threading.Barrier(2)

        def _go():
            barrier.wait()
            results.append(start_app_backend("backend-app"))

        threads = [threading.Thread(target=_go) for _ in range(2)]
        for t in threads:
            t.start()
        time.sleep(0.3)   # let one claim the placeholder + the other hit the await
        gate.set()        # release the single spawn body
        for t in threads:
            t.join(timeout=10)

        # exactly ONE spawn body ran (single-flighted), both callers got the same proc
        assert spawn_calls["n"] == 1, f"spawn body ran {spawn_calls['n']} times (race not single-flighted)"
        non_none = [r for r in results if r is not None]
        assert len(non_none) == 2, f"a caller got None: {results}"
        assert {r.port for r in non_none} == {9137}
        assert len(list_app_processes()) == 1
        # cleanup the fake-process state so it can't leak into the next test
        with bmod._lock:
            bmod._processes.clear()
            bmod._allocated_ports.clear()

    def test_await_inflight_spawn_timeout_clears_stale_placeholder(self, app_env):
        # If a spawn body hangs without raising (so the owner's None/exception cleanup
        # never fires), an awaiting caller hits the deadline with the placeholder still
        # STARTING. It must clear that placeholder and return None — otherwise the app is
        # wedged in 'starting' forever and every later call re-enters the 20s wait.
        import kiro_crew.apps.backend as bmod

        with bmod._lock:
            bmod._processes["wedged-app"] = AppProcess(
                app_name="wedged-app", starting=True, started_at=0.0
            )
        # Short timeout so the test is fast; the placeholder never resolves.
        result = bmod._await_inflight_spawn("wedged-app", timeout=0.3)
        assert result is None
        # The stale placeholder is gone, so a fresh start_app_backend can spawn again.
        assert "wedged-app" not in bmod._processes


class TestShellEntryDetection:
    """Unit coverage for _is_shell_entry (shell launcher heuristic)."""

    def _write(self, tmp_path, name, content, executable=True):
        f = tmp_path / name
        f.write_text(content)
        if executable:
            f.chmod(0o755)
        return f

    def test_sh_suffix_is_shell(self, tmp_path):
        f = self._write(tmp_path, "run.sh", "#!/bin/sh\necho hi\n", executable=False)
        assert _is_shell_entry(f) is True

    def test_extensionless_bash_shebang_is_shell(self, tmp_path):
        f = self._write(tmp_path, "my-launcher", "#!/usr/bin/env bash\nset -euo pipefail\n")
        assert _is_shell_entry(f) is True

    def test_python_shebang_launcher_is_not_shell(self, tmp_path):
        f = self._write(tmp_path, "launcher", "#!/usr/bin/env python3\nprint('hi')\n")
        assert _is_shell_entry(f) is False

    def test_py_extension_is_not_shell(self, tmp_path):
        f = self._write(tmp_path, "server.py", "#!/usr/bin/env bash\n")
        assert _is_shell_entry(f) is False

    def test_extensionless_non_executable_is_not_shell(self, tmp_path):
        f = self._write(tmp_path, "launcher", "#!/bin/bash\n", executable=False)
        assert _is_shell_entry(f) is False

    def test_extensionless_no_shebang_is_not_shell(self, tmp_path):
        f = self._write(tmp_path, "launcher", "echo hi\n")
        assert _is_shell_entry(f) is False


class TestShellDispatch:
    """Dispatch-level coverage: the shell branch selects the right argv without
    a real spawn. Complements the e2e launcher test (which only covers the
    auto-detect path) with the explicit ``backend.type: "exec"`` route and the
    /bin/sh fallback for a non-executable ``.sh`` entry."""

    def _dispatch_cmd(self, tmp_path, monkeypatch, name, entry_rel, content, *,
                      executable, backend_type=""):
        """Install an app, then capture the argv the dispatch builds."""
        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.manager import get_app_manifest

        src = tmp_path / "source" / name
        src.mkdir(parents=True)
        backend: dict = {"entryPoint": entry_rel, "port": "auto"}
        if backend_type:
            backend["type"] = backend_type
        (src / APP_MANIFEST_FILENAME).write_text(json.dumps({
            "name": name, "version": "1.0.0",
            "displayName": name, "description": "shell dispatch test",
            "author": "tester",
            "backend": backend,
        }))
        entry = src / entry_rel
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(content)
        if executable:
            entry.chmod(0o755)
        install_app(src)

        captured: dict = {}

        def _capture_wrap(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return cmd, None

        class _ReachedSpawn(Exception):
            pass

        def _sentinel(*a, **k):
            raise _ReachedSpawn()

        # Neutralize the sandbox wrap so the test isolates DISPATCH (its
        # purpose) from sandbox availability, and stop before any real spawn.
        monkeypatch.setattr(bmod, "wrap_argv", _capture_wrap)
        monkeypatch.setattr(bmod.subprocess, "Popen", _sentinel)
        manifest = get_app_manifest(name)
        assert manifest is not None
        with pytest.raises(_ReachedSpawn):
            bmod._start_app_backend_body(name, manifest)
        return captured["cmd"]

    def test_explicit_backend_type_exec_routes_to_shell_branch(
            self, tmp_path, app_env, monkeypatch):
        # A launcher the auto-detect can NOT identify (extensionless, no
        # shebang — the stand-in for a compiled/ELF binary) must still hit the
        # shell branch when the manifest declares `"type": "exec"` explicitly.
        cmd = self._dispatch_cmd(
            tmp_path, monkeypatch, "explicit-shell", "bin/launcher",
            "echo hi\n", executable=True, backend_type="exec",
        )
        assert len(cmd) == 1
        assert cmd[0].endswith("bin/launcher")

    def test_non_executable_sh_entry_falls_back_to_bin_sh(self, tmp_path, app_env,
                                                          monkeypatch):
        # A shebang-less `.sh` entry that lost its exec bit is run via /bin/sh
        # as the last resort.
        cmd = self._dispatch_cmd(
            tmp_path, monkeypatch, "sh-fallback", "run.sh",
            "echo hi\n", executable=False,
        )
        assert cmd[0] == "/bin/sh"
        assert len(cmd) == 2
        assert cmd[1].endswith("run.sh")

    def test_non_executable_bash_entry_honors_shebang(self, tmp_path, app_env,
                                                      monkeypatch):
        # A non-executable launcher with a bash shebang must run under ITS
        # declared interpreter, not /bin/sh — bash-isms like
        # `set -euo pipefail` die under dash-as-sh (Debian/Ubuntu).
        cmd = self._dispatch_cmd(
            tmp_path, monkeypatch, "bash-shebang", "run.sh",
            "#!/usr/bin/env bash\nset -euo pipefail\necho hi\n",
            executable=False,
        )
        assert cmd[:2] == ["/usr/bin/env", "bash"]
        assert cmd[2].endswith("run.sh")

    def test_shell_backend_refused_on_non_posix(self, tmp_path, app_env,
                                                monkeypatch):
        # On native Windows (IS_POSIX False) the shell branch must fail fast
        # with a logged error and return None — never reach Popen with a
        # shebang-dependent argv or the nonexistent /bin/sh.
        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.manager import get_app_manifest

        name = "win-shell-refused"
        src = tmp_path / "source" / name
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text(json.dumps({
            "name": name, "version": "1.0.0",
            "displayName": name, "description": "non-posix guard test",
            "author": "tester",
            "backend": {"entryPoint": "run.sh", "port": "auto",
                        "type": "exec"},
        }))
        (src / "run.sh").write_text("#!/bin/sh\necho hi\n")
        install_app(src)

        def _boom(*a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("Popen must not be called on non-POSIX")

        monkeypatch.setattr(bmod.subprocess, "Popen", _boom)
        monkeypatch.setattr(bmod.platform_compat, "IS_POSIX", False)
        manifest = get_app_manifest(name)
        assert manifest is not None
        assert bmod._start_app_backend_body(name, manifest) is None
