"""Tests for kiro_crew.apps.backend — backend process management."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

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
    """
    try:
        from kiro_crew import sandbox as _sb

        argv, cleanup = _sb.wrap_argv([sys.executable, "-c", "pass"], mode="standard")
    except Exception:  # noqa: BLE001 — any probe failure => treat as "can't spawn"
        return False
    try:
        return subprocess.run(argv, capture_output=True, timeout=15).returncode == 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass


# Evaluated once per worker at collection; the two lifecycle tests below need a
# real sandboxed backend to come up and stay up.
_needs_sandbox_spawn = pytest.mark.skipif(
    not _sandbox_can_spawn(),
    reason="OS sandbox cannot spawn a surviving child here (e.g. GitHub hosted "
    "runners deny unshare(NEWNS)); start_app_backend() correctly fail-closes to None",
)


def _make_app_with_backend(tmp_path, name="backend-app"):
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Backend App",
        "description": "App with a backend",
        "author": "tester",
        "backend": {
            "entryPoint": "backend/server.py",
            "port": "auto",
            "healthCheck": "/health",
        },
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    # Create a minimal backend that starts an HTTP server
    (src / "backend").mkdir()
    (src / "backend" / "server.py").write_text(
        'import http.server, os, sys\n'
        'port = int(os.environ.get("PORT", 9100))\n'
        'class H(http.server.BaseHTTPRequestHandler):\n'
        '    def do_GET(self):\n'
        '        self.send_response(200)\n'
        '        self.end_headers()\n'
        '        self.wfile.write(b"ok")\n'
        '    def log_message(self, *a): pass\n'
        'http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()\n'
    )
    return src


@pytest.fixture()
def app_env(tmp_path, monkeypatch, worker_id):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    # These tests exercise admitted backend process mechanics.
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    import kiro_crew.apps.backend as bmod

    # Under xdist (-n auto) each worker runs in its OWN process with its own
    # _allocated_ports dict, so two workers both auto-allocate 9100 and the real
    # servers collide (EADDRINUSE). Give each worker a DISJOINT port window so
    # parallel real-spawn tests never contend. (Production is single-process; this
    # only matters for the test harness.)
    if worker_id and worker_id != "master":
        try:
            idx = int(worker_id.replace("gw", "")) if worker_id.startswith("gw") else 0
        except ValueError:
            idx = 0
        base = 9100 + idx * 20
        monkeypatch.setattr(bmod, "_MIN_PORT", base)
        monkeypatch.setattr(bmod, "_MAX_PORT", base + 20)

    def _reap() -> None:
        # KILL any spawned backend processes, not just clear the tracking dicts — a
        # test that spawns a real server and doesn't stop it would otherwise leave the
        # process holding its port, so the next test's auto-allocated port collides
        # (EADDRINUSE). Before the spawn survival-check this leak was silently tolerated
        # (the colliding spawn was reported as 'started' anyway); now it's caught, so the
        # fixture must clean up properly. Use stop_app_backend → it killpg's the whole
        # process group (the sandbox wraps the child, so a plain terminate misses it).
        import socket as _sock
        ports = [getattr(ap, "port", 0) for ap in bmod._processes.values()]
        for name in list(bmod._processes.keys()):
            try:
                bmod.stop_app_backend(name)
            except Exception:  # noqa: BLE001
                pass
        bmod._processes.clear()
        bmod._allocated_ports.clear()
        # Wait for each killed server's port to actually be released so the next test's
        # auto-allocation can't re-pick a still-occupied port (EADDRINUSE).
        for port in ports:
            if not port:
                continue
            for _ in range(50):  # up to ~5s
                s = _sock.socket()
                try:
                    s.bind(("127.0.0.1", port))
                    s.close()
                    break
                except OSError:
                    s.close()
                    time.sleep(0.1)

    _reap()       # clean slate before the test
    yield home
    _reap()       # and reap anything the test left running


class TestPortAllocation:
    def test_find_free_port(self):
        port = _find_free_port()
        assert 9100 <= port <= 9200

    def test_concurrent_allocation_never_hands_out_the_same_port(self, monkeypatch):
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


class TestFixedAndAutoPortIsolation:
    def test_boot_preclaims_fixed_ports_before_any_spawn(self, monkeypatch):
        """A declared fixed port must not be lost to a concurrent auto-port app.

        A fixed port is a requirement, not a preference. Without pre-claiming, an
        auto worker can select that exact number first and the fixed app is then
        refused even though other ports were free — an enabled backend left down.
        """
        import kiro_crew.apps.backend as bmod

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

        Boot spawns concurrently, and a fixed-port app used to record its port only
        AFTER binding. An auto-port app selecting inside that window could be handed
        the same number, so one of the two children would die of EADDRINUSE and its
        backend would stay unavailable. Asserted at the real seam: the port must
        already be reserved by the time the spawn body runs.
        """
        import kiro_crew.apps.backend as bmod

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


class TestBootSpawnLatency:
    def test_survival_check_exits_early_for_a_healthy_child(self, monkeypatch):
        """A living child must not cost the full survival window.

        The poll used to sleep its whole ~1.6s budget on the happy path and only
        break when the child DIED, so every app added ~1.6s of dead time to boot.
        It must return as soon as the child is confirmed alive.
        """
        import time as real_time

        import kiro_crew.apps.backend as bmod

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

        monkeypatch.setattr(bmod.time, "sleep", lambda s: None)
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

        monkeypatch.setattr(bmod.time, "sleep", lambda s: None)
        # Something is listening, but it is not our child (nor its descendant).
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

        monkeypatch.setattr(bmod.time, "sleep", lambda s: None)
        monkeypatch.setattr(bmod, "_listening_pids", lambda port: [4243])
        monkeypatch.setattr(
            bmod, "_pid_is_self_or_descendant_of",
            lambda pid, ancestor: pid == 4243 and ancestor == 4242,
        )

        class _Alive:
            pid = 4242

            def poll(self) -> None:
                return None

        assert bmod._survived_spawn(_Alive(), 9100) is True

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

        N apps used to cost N x the survival window because each spawn ran to
        completion before the next began. With 4 apps that is ~6.4s of pure boot
        latency on the happy path.
        """
        import threading

        import kiro_crew.apps.backend as bmod

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
        # App without backend section
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
        # Process should be in the list
        procs = list_app_processes()
        assert len(procs) == 1
        assert procs[0]["app_name"] == "backend-app"
        # Stop it
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
        # security-review finding: the apps_allow_third_party off-switch must also block
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


class TestBootAdmissionRevet:
    """start_enabled_app_backends re-vets admission at boot (KiroCrew parity).

    An app enabled before a policy tightened (banned / now-unsigned) must NOT
    keep running across restarts, but builtins (origin == "builtin") are exempt
    so trusted first-party apps still boot under require_signature.
    """

    def _boot_env(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        monkeypatch.setattr(bmod, "_reap_stale_app_backends", lambda: 0)
        started: list[str] = []

        def _fake_start(name):
            started.append(name)
            return None  # no real spawn; skip the health-gate branch

        monkeypatch.setattr(bmod, "start_app_backend", _fake_start)
        monkeypatch.setattr(bmod, "get_app_manifest", lambda name: None)
        return bmod, started

    def test_banned_third_party_skipped_at_boot(self, tmp_path, app_env, monkeypatch):
        bmod, started = self._boot_env(monkeypatch)
        (app_env / "app_admission.json").write_text(
            json.dumps({"mode": "enforce", "banned": ["evil-app"]})
        )
        apps = [{
            "name": "evil-app", "enabled": True, "origin": "registry",
            "manifest": {"backend": {"entryPoint": "server.py"}},
        }]
        monkeypatch.setattr(bmod, "list_apps", lambda: apps)
        result = bmod.start_enabled_app_backends()
        assert "evil-app" not in result
        assert "evil-app" not in started

    def test_builtin_still_boots_under_require_signature(self, tmp_path, app_env, monkeypatch):
        bmod, started = self._boot_env(monkeypatch)
        (app_env / "app_admission.json").write_text(
            json.dumps({
                "mode": "enforce", "require_signature": True,
                "approved": [], "trust_keys": {},
            })
        )
        apps = [{
            "name": "core-builtin", "enabled": True, "origin": "builtin",
            "manifest": {"backend": {"entryPoint": "server.py"}},
        }]
        monkeypatch.setattr(bmod, "list_apps", lambda: apps)
        bmod.start_enabled_app_backends()
        # Builtin is exempt from the gate — start_app_backend was invoked for it.
        assert "core-builtin" in started

    def test_spawn_exception_isolated_and_boot_continues(self, tmp_path, app_env, monkeypatch):
        """A per-app spawn failure (e.g. sandbox.wrap_argv fail-closing on macOS 26
        where sandbox-exec is gone) must NOT crash the whole gateway — the loop logs,
        skips the failing app, and still boots the healthy one."""
        import kiro_crew.apps.backend as bmod

        monkeypatch.setattr(bmod, "_reap_stale_app_backends", lambda: 0)
        monkeypatch.setattr(bmod, "get_app_manifest", lambda name: None)
        started: list[str] = []

        def _fake_start(name):
            if name == "boom-app":
                raise RuntimeError(
                    "Sandbox backend unavailable and allow_unsandboxed_exec is not set."
                )
            started.append(name)
            return None

        monkeypatch.setattr(bmod, "start_app_backend", _fake_start)
        apps = [
            {"name": "boom-app", "enabled": True, "origin": "builtin",
             "manifest": {"backend": {"entryPoint": "server.py"}}},
            {"name": "ok-app", "enabled": True, "origin": "builtin",
             "manifest": {"backend": {"entryPoint": "server.py"}}},
        ]
        monkeypatch.setattr(bmod, "list_apps", lambda: apps)
        # Must not raise despite boom-app's spawn raising.
        result = bmod.start_enabled_app_backends()
        # boom-app was skipped; ok-app still got its spawn attempt.
        assert "boom-app" not in started
        assert "ok-app" in started
        assert "boom-app" not in result


class _FakeHealthResp:
    """Minimal urlopen() stand-in: a 200 response usable as a context manager."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class TestHealthGatedMcpRegistration:
    """Health-gated MCP registration (review + review-bot race finding).

    The health-check loop must register an app's MCP servers ONLY when the backend is
    still tracked and healthy, and scrub them when it never becomes healthy — never write
    a dead-URL entry (the kiro-cli outage shape)."""

    def _fast_health(self, bmod, monkeypatch):
        # Make the loop iterate instantly.
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_RETRIES", 3)

    def test_registers_when_healthy_and_still_tracked(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        self._fast_health(bmod, monkeypatch)
        calls = []
        monkeypatch.setattr(bmod, "_gate_mcp_registration",
                            lambda name, port, *, healthy: calls.append((name, port, healthy)))

        monkeypatch.setattr(bmod.urllib.request, "urlopen", lambda *a, **k: _FakeHealthResp())

        with bmod._lock:
            bmod._processes["hg-app"] = AppProcess(app_name="hg-app", port=9150, healthy=False)
        try:
            bmod._health_check_loop("hg-app", 9150, "/health")
            assert calls == [("hg-app", 9150, True)]  # registered exactly once, healthy
            assert bmod._processes["hg-app"].healthy is True
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_does_not_register_if_stopped_mid_healthcheck(self, monkeypatch):
        # review-bot race finding: app removed from _processes between the poll and the lock →
        # must NOT register MCP for a now-dead backend.
        import kiro_crew.apps.backend as bmod
        self._fast_health(bmod, monkeypatch)
        calls = []
        monkeypatch.setattr(bmod, "_gate_mcp_registration",
                            lambda name, port, *, healthy: calls.append((name, port, healthy)))

        # urlopen "succeeds" but the app is NOT in _processes (stopped mid-check).
        monkeypatch.setattr(bmod.urllib.request, "urlopen", lambda *a, **k: _FakeHealthResp())
        with bmod._lock:
            bmod._processes.clear()  # ensure absent

        bmod._health_check_loop("gone-app", 9151, "/health")
        assert calls == []  # never registered — no dead-URL entry written

    def test_scrubs_when_never_healthy(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        self._fast_health(bmod, monkeypatch)
        calls = []
        monkeypatch.setattr(bmod, "_gate_mcp_registration",
                            lambda name, port, *, healthy: calls.append((name, port, healthy)))

        def _boom(*a, **k):
            raise OSError("connection refused")
        monkeypatch.setattr(bmod.urllib.request, "urlopen", _boom)

        with bmod._lock:
            bmod._processes["sick-app"] = AppProcess(app_name="sick-app", port=9152, healthy=False)
        try:
            bmod._health_check_loop("sick-app", 9152, "/health")
            # Never healthy → scrub (healthy=False), never register.
            assert calls == [("sick-app", 9152, False)]
        finally:
            with bmod._lock:
                bmod._processes.clear()


# =============================================================================
# KIROCREW_DEVFLEET_REPO forwarding (silent-empty-fleet fix)
# =============================================================================


def test_devfleet_repo_survives_the_app_backend_env_allowlist(monkeypatch):
    """The documented dev-fleet repo override must be ABLE to reach the backend.

    The dev-fleet backend runs as a separate process started with
    ``apps.registry.minimal_env()``, which passes only a fixed safe-key set.
    ``KIROCREW_DEVFLEET_REPO`` is dev-fleet's highest-priority repo discovery
    hint, but until it is added to the explicit platform extras the allowlist
    strips it — the operator sets the documented override, the backend never
    sees it, and the fleet silently renders empty (the remaining hints are
    ``KIROCREW_PROJECT_DIR``, which packaged installs point at the app bundle
    with no ``.git``, and a hardcoded ``~/kirocrew`` fallback).
    """
    from pathlib import Path

    import kiro_crew.apps.backend as bmod
    from kiro_crew.apps.registry import minimal_env

    monkeypatch.setenv("KIROCREW_DEVFLEET_REPO", "/opt/checkouts/kirocrew")
    # The generic allowlist does NOT carry it — that is the trap this guards.
    assert "KIROCREW_DEVFLEET_REPO" not in minimal_env()

    # apps/backend.py must therefore add it to the explicit platform extras
    # (same mechanism that carries KIROCREW_PROJECT_DIR and the
    # KIROCREW_DEVFLEET_BIN_* trusted-binary overrides).
    body = Path(bmod.__file__).read_text()
    assert '_platform_extra["KIROCREW_DEVFLEET_REPO"]' in body, \
        "the KIROCREW_DEVFLEET_REPO override no longer reaches app backends"


def test_devfleet_repo_env_wins_repo_discovery(monkeypatch, tmp_path):
    """dev-fleet honors the forwarded override ahead of every other hint."""
    from kiro_crew.apps.builtins.dev_fleet import server as dfmod

    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_DEVFLEET_REPO", "/opt/checkouts/kirocrew")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
    assert dfmod._default_main_repo() == "/opt/checkouts/kirocrew"

    # Without the override the chain falls through to PROJECT_DIR (with .git),
    # and then to the ~/kirocrew fallback — the packaged-install trap.
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO")
    assert dfmod._default_main_repo() == str(proj)
