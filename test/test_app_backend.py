"""Tests for kiro_crew.apps.backend — backend process management."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any

import pytest

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

        Boot spawns concurrently, and a fixed-port app used to record its port only
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

        The poll used to sleep its whole ~1.6s budget on the happy path and only
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

        N apps used to cost N x the survival window because each spawn ran to
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


class TestBootAdmissionRevet:
    """start_enabled_app_backends re-vets admission at boot (KiroCrew parity).

    An app enabled before a policy tightened (banned / now-unsigned) must NOT
    keep running across restarts, but builtins (origin == "builtin") are exempt
    so trusted first-party apps still boot under require_signature.
    """

    def _boot_env(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

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

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

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


def _reconcile_lock_held() -> bool:
    """Whether the health reconcile lock is held right now.

    Probed from a SEPARATE thread on purpose: the lock is an RLock, so a same-thread
    acquire would succeed by re-entrancy and report False no matter who holds it.
    """
    import threading

    import kiro_crew.apps.backend as bmod

    result: list[bool] = []

    def _probe() -> None:
        got = bmod._health_reconcile_lock.acquire(blocking=False)
        result.append(not got)
        if got:
            bmod._health_reconcile_lock.release()

    t = threading.Thread(target=_probe)
    t.start()
    t.join()
    return result[0]


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

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        self._fast_health(bmod, monkeypatch)
        calls = []
        monkeypatch.setattr(bmod, "_gate_mcp_registration",
                            lambda name, port, *, healthy: calls.append((name, port, healthy)))

        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *a, **k: _FakeHealthResp())

        with bmod._lock:
            bmod._processes["hg-app"] = AppProcess(app_name="hg-app", port=9150, healthy=False)
        try:
            bmod._health_check_loop(bmod._processes["hg-app"], "/health")
            assert calls == [("hg-app", 9150, True)]  # registered exactly once, healthy
            assert bmod._processes["hg-app"].healthy is True
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_does_not_register_if_stopped_mid_healthcheck(self, monkeypatch):
        # review-bot race finding: app removed from _processes between the poll and the lock →
        # must NOT register MCP for a now-dead backend.
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        self._fast_health(bmod, monkeypatch)
        calls = []
        monkeypatch.setattr(bmod, "_gate_mcp_registration",
                            lambda name, port, *, healthy: calls.append((name, port, healthy)))

        # urlopen "succeeds" but the app is NOT in _processes (stopped mid-check).
        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *a, **k: _FakeHealthResp())
        with bmod._lock:
            bmod._processes.clear()  # ensure absent

        bmod._health_check_loop(AppProcess(app_name="gone-app", port=9151), "/health")
        assert calls == []  # never registered — no dead-URL entry written

    def test_scrubs_when_never_healthy(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        self._fast_health(bmod, monkeypatch)
        calls = []
        monkeypatch.setattr(bmod, "_gate_mcp_registration",
                            lambda name, port, *, healthy: calls.append((name, port, healthy)))

        def _boom(*a, **k):
            raise OSError("connection refused")
        monkeypatch.setattr(bmod, "loopback_urlopen", _boom)

        with bmod._lock:
            bmod._processes["sick-app"] = AppProcess(app_name="sick-app", port=9152, healthy=False)
        try:
            bmod._health_check_loop(bmod._processes["sick-app"], "/health")
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
    (proj / "src" / "kiro_crew").mkdir(parents=True)
    (proj / "pyproject.toml").write_text("[project]\nname = 'kiro-crew'\n")
    monkeypatch.setenv("KIROCREW_DEVFLEET_REPO", "/opt/checkouts/kirocrew")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
    assert dfmod._default_main_repo() == "/opt/checkouts/kirocrew"

    # Without the override the chain falls through to PROJECT_DIR, which is
    # adopted only because it carries the Kiro Crew checkout markers.
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO")
    assert dfmod._default_main_repo() == str(proj)


class TestTheCacheOnlyChildCanSeeTheCacheItMustBootFrom:
    """``policy_cache`` is bind-mount-hidden in every sandbox tier.

    That is deliberate — it keeps the AGENT's own subprocesses from reading or rewriting
    the ceiling — but an app backend in cache-only mode resolves the fleet ceiling FROM
    that file and FAILS CLOSED without it. Without the visibility carve-out the two
    controls contradict each other and every app backend on a centrally-governed host
    exits at boot.
    """

    def test_the_spawn_passes_the_cache_as_a_visible_dir(self, app_env, tmp_path, monkeypatch):
        import kiro_crew.apps.backend as bmod
        from kiro_crew.platform import policy_distribution as pd

        # A gateway whose OWN ceiling came from the central tier is the only one that
        # flags a child cache-only, so that is the state to reproduce.
        pd._record_installed(b'{"version": 1, "boot": {}}')
        try:
            seen: dict = {}

            def _spy_wrap(argv, **kwargs):
                seen["visible"] = kwargs.get("extra_visible_dirs")
                return (list(argv), None)

            monkeypatch.setattr(bmod, "wrap_argv", _spy_wrap)
            monkeypatch.setattr(
                bmod.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("stop"))
            )

            src = tmp_path / "source" / "cache-only-app"
            src.mkdir(parents=True)
            (src / APP_MANIFEST_FILENAME).write_text(
                json.dumps(
                    {
                        "name": "cache-only-app",
                        "version": "1.0.0",
                        "displayName": "Cache only",
                        "description": "policy cache visibility",
                        "backend": {"entryPoint": "server.py", "healthCheck": "/health"},
                    }
                )
            )
            (src / "server.py").write_text("import time\ntime.sleep(30)\n")
            install_app(src)

            bmod.start_app_backend("cache-only-app")

            assert seen.get("visible"), "the spawn passed no visible dirs at all"
            assert str(pd.cache_dir()) in seen["visible"], (
                "the policy cache is hidden from a child that fails closed without it, "
                f"so every app backend would exit at boot (saw {seen['visible']!r})"
            )
        finally:
            pd.reset_process_state()

    @staticmethod
    def _spawn_with_wrap(bmod, tmp_path, monkeypatch, name, wrap):
        monkeypatch.setattr(bmod, "wrap_argv", wrap)
        monkeypatch.setattr(
            bmod.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("stop"))
        )
        src = tmp_path / "source" / name
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text(
            json.dumps(
                {
                    "name": name,
                    "version": "1.0.0",
                    "displayName": name,
                    "description": "confinement reporting",
                    "backend": {"entryPoint": "server.py", "healthCheck": "/health"},
                }
            )
        )
        (src / "server.py").write_text("import time\ntime.sleep(30)\n")
        install_app(src)
        bmod.start_app_backend(name)

    def test_an_unconfined_host_says_so_once_and_names_the_control(
        self, app_env, tmp_path, monkeypatch, caplog
    ):
        """A centrally governed host with no OS sandbox runs app code unconfined, so the
        read-only seal on the cache does not exist. NOT a refusal: an unconfined process can
        rewrite security_policy.json and the admission policy directly — the keystone gate
        covers TOOL CALLS, not an arbitrary process's ``open()`` — so forbidding an app to READ
        the ceiling while it can REPLACE the ceiling protects nothing. What does still hold is
        provenance, so the warning names it.
        """
        import logging

        import kiro_crew.apps.backend as bmod
        from kiro_crew.platform import policy_distribution as pd

        pd._record_installed(b'{"version": 1, "boot": {}}')
        monkeypatch.setattr(bmod, "_warned_unconfined_cache", False, raising=False)
        try:
            with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.backend"):
                # An unwrapped argv is exactly what wrap_argv returns with no sandbox backend.
                self._spawn_with_wrap(
                    bmod, tmp_path, monkeypatch, "unconfined-app", lambda argv, **kw: (list(argv), None)
                )
            messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
            said = [m for m in messages if "no OS sandbox" in m]
            assert said, f"the combination must be named (saw {messages!r})"
            assert "require_policy_signature" in said[0], "and the actionable control with it"
        finally:
            pd.reset_process_state()

    def test_a_confined_host_says_nothing(self, app_env, tmp_path, monkeypatch, caplog):
        """The control: where the seal applies there is nothing to warn about."""
        import logging

        import kiro_crew.apps.backend as bmod
        from kiro_crew.platform import policy_distribution as pd

        pd._record_installed(b'{"version": 1, "boot": {}}')
        monkeypatch.setattr(bmod, "_warned_unconfined_cache", False, raising=False)
        try:
            with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.backend"):
                # A CHANGED argv is what a real wrap returns.
                self._spawn_with_wrap(
                    bmod,
                    tmp_path,
                    monkeypatch,
                    "confined-app",
                    lambda argv, **kw: (["sandbox-exec", "-f", "/tmp/p.sb", *argv], None),
                )
            assert not [
                r for r in caplog.records if "no OS sandbox" in r.getMessage()
            ], "a confined spawn must not warn"
        finally:
            pd.reset_process_state()

    def test_an_ungoverned_host_passes_no_visible_dirs(self, app_env, tmp_path, monkeypatch):
        """No central ceiling means no cache-only flag and nothing to un-hide."""
        import kiro_crew.apps.backend as bmod
        from kiro_crew.platform import policy_distribution as pd

        pd.reset_process_state()
        seen: dict = {}

        def _spy_wrap(argv, **kwargs):
            seen["visible"] = kwargs.get("extra_visible_dirs")
            return (list(argv), None)

        monkeypatch.setattr(bmod, "wrap_argv", _spy_wrap)
        monkeypatch.setattr(
            bmod.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("stop"))
        )

        src = tmp_path / "source" / "plain-app"
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text(
            json.dumps(
                {
                    "name": "plain-app",
                    "version": "1.0.0",
                    "displayName": "Plain",
                    "description": "no central policy",
                    "backend": {"entryPoint": "server.py", "healthCheck": "/health"},
                }
            )
        )
        (src / "server.py").write_text("import time\ntime.sleep(30)\n")
        install_app(src)

        bmod.start_app_backend("plain-app")

        assert seen.get("visible") == ()


# =============================================================================
# Post-startup liveness watch (#5726)
# =============================================================================


class _FakeProc:
    """Popen stand-in for the watch's liveness check: alive until it is given an rc."""

    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


class TestBackendLivenessWatch:
    """``healthy`` must be able to go back to False (#5726).

    Before the watch, the startup poll wrote ``healthy = True`` once and nothing ever
    unwrote it, so a backend that died later kept the reverse proxy routing to its dead
    port and kept ``/api/apps`` reporting it healthy.
    """

    @pytest.fixture
    def watched(self, monkeypatch):
        """A tracked, healthy backend plus the seams the watch runs through.

        Yields ``(bmod, ap, probes, gate_calls)``. ``probes`` is the scripted probe
        result list — the watch stops by untracking the record once the script runs out,
        which is the same guard ``stop_app_backend`` exits it through in production.
        """
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        gate_calls: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration",
            lambda name, port, *, healthy: (gate_calls.append((name, port, healthy)), True)[1],
        )

        # mcp_healthy=True models the real precondition: a record only reaches the
        # watch after a promotion has already reconciled mcp.json for it.
        ap = AppProcess(app_name="watched", port=9160, pid=4242,
                        proc=_FakeProc(), healthy=True, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["watched"] = ap

        probes: list[bool] = []

        def _scripted(_port, _path):
            if not probes:
                # Script exhausted: untrack so the watch exits at its top-of-loop guard.
                with bmod._lock:
                    bmod._processes.pop("watched", None)
                return False
            return probes.pop(0)

        monkeypatch.setattr(bmod, "_health_probe", _scripted)
        try:
            yield bmod, ap, probes, gate_calls
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_survives_transient_failures_below_the_threshold(self, watched):
        # A backend that misses one probe and answers the next is briefly busy, not
        # dead: demoting there would take a working app offline.
        bmod, ap, probes, gate_calls = watched
        probes.extend([False] * (bmod._HEALTH_WATCH_FAILURES - 1) + [True])

        bmod._watch_backend_health(ap, "/health")

        assert ap.healthy is True
        assert gate_calls == []  # never demoted, so the MCP entry was never scrubbed

    def test_demotes_after_consecutive_failures_and_scrubs_mcp(self, watched):
        bmod, ap, probes, gate_calls = watched
        probes.extend([False] * bmod._HEALTH_WATCH_FAILURES)

        bmod._watch_backend_health(ap, "/health")

        assert ap.healthy is False
        assert ("watched", 9160, False) in gate_calls

    def test_a_demoted_backend_is_no_longer_routable(self, watched):
        # The load-bearing consequence: get_app_backend_port gates the reverse proxy
        # purely on this flag, so before the watch the proxy kept dialing a dead port.
        bmod, ap, probes, gate_calls = watched
        assert bmod.get_app_backend_port("watched") == 9160

        probes.extend([False] * bmod._HEALTH_WATCH_FAILURES)
        bmod._watch_backend_health(ap, "/health")

        assert bmod.get_app_backend_port("watched") is None

    def test_demotion_is_reversible(self, watched):
        # An app that wedged briefly must heal without operator action.
        bmod, ap, probes, gate_calls = watched
        probes.extend([False] * bmod._HEALTH_WATCH_FAILURES + [True])

        bmod._watch_backend_health(ap, "/health")

        assert ap.healthy is True
        assert gate_calls == [("watched", 9160, False), ("watched", 9160, True)]

    def test_an_exited_process_demotes_on_one_observation(self, watched):
        # No threshold: a dead Popen cannot recover, so waiting for three misses would
        # only keep routing to it for longer. The probe list stays untouched, pinning
        # that liveness is answered from the exit status without an HTTP round trip.
        bmod, ap, probes, gate_calls = watched
        ap.proc = _FakeProc(returncode=1)

        bmod._watch_backend_health(ap, "/health")

        assert ap.healthy is False
        assert gate_calls == [("watched", 9160, False)]
        assert probes == []  # never consulted the health endpoint

    def test_does_not_demote_a_newer_generation_under_the_same_name(self, watched):
        # A stop/start replaces the record; the old watch must not carry its verdict
        # over to a backend it never observed.
        bmod, ap, probes, gate_calls = watched
        replacement = AppProcess(app_name="watched", port=9161, pid=99,
                                 proc=_FakeProc(), healthy=True)
        with bmod._lock:
            bmod._processes["watched"] = replacement
        ap.proc = _FakeProc(returncode=1)  # the OLD generation is dead

        bmod._watch_backend_health(ap, "/health")

        assert replacement.healthy is True
        assert gate_calls == []
        assert bmod.get_app_backend_port("watched") == 9161

    def test_an_untracked_record_is_never_probed(self, watched):
        # stop_app_backend pops the record; the watch exits before touching the network,
        # so a stale watcher can never probe a port another backend has since taken.
        bmod, ap, probes, gate_calls = watched
        probes.append(True)
        with bmod._lock:
            bmod._processes.pop("watched")

        bmod._watch_backend_health(ap, "/health")

        assert probes == [True]  # untouched
        assert gate_calls == []


class TestBackendRunningReflectsTheProcess:
    """``/api/apps`` must not report an exited backend as running (#5726)."""

    def test_running_is_false_once_the_process_exits(self):
        ap = AppProcess(app_name="gone", port=9162, pid=7, proc=_FakeProc(returncode=0))
        assert ap.to_dict()["running"] is False

    def test_running_is_true_while_the_process_lives(self):
        ap = AppProcess(app_name="live", port=9163, pid=7, proc=_FakeProc())
        assert ap.to_dict()["running"] is True

    def test_an_adopted_backend_has_no_handle_to_poll(self):
        # proc=None is the adopted shape: the instance belongs to another supervisor, so
        # "we still track it" is the only honest answer and `healthy` carries the signal.
        ap = AppProcess(app_name="adopted", port=9164, pid=0, proc=None, healthy=True)
        assert ap.to_dict()["running"] is True


class TestHealthSupervisorHandoff:
    """The startup poll must hand the watch its record, or the whole watch is dead wiring."""

    def test_watch_runs_when_the_backend_came_up(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        ap = AppProcess(app_name="up", port=9170, healthy=True)
        watched: list[tuple[AppProcess, str]] = []
        monkeypatch.setattr(bmod, "_health_check_loop", lambda *_a, **_k: ap)
        monkeypatch.setattr(bmod, "_watch_backend_health",
                            lambda rec, path: watched.append((rec, path)))

        bmod._supervise_backend_health(ap, "/health")

        assert watched == [(ap, "/health")]

    def test_no_watch_when_the_backend_never_came_up(self, monkeypatch):
        # Nothing to watch: the startup path already scrubbed the MCP entry and gave up.
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        watched: list[Any] = []
        monkeypatch.setattr(bmod, "_health_check_loop", lambda *_a, **_k: None)
        monkeypatch.setattr(bmod, "_watch_backend_health",
                            lambda rec, path: watched.append(rec))

        bmod._supervise_backend_health(AppProcess(app_name="down", port=9171), "/health")

        assert watched == []


class TestHealthTransitionsRefuseAStaleRecord:
    """stop_app_backend can land between a probe and its verdict.

    Both transitions re-check identity under the lock, so a verdict formed about a
    record that has since been popped or replaced cannot be written — and cannot move
    the MCP entry of whatever holds the name now.
    """

    @pytest.fixture
    def gate_calls(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        calls: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration",
            lambda name, port, *, healthy: (calls.append((name, port, healthy)), True)[1],
        )
        with bmod._lock:
            bmod._processes.clear()
        try:
            yield bmod, calls
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_demote_refuses_an_untracked_record(self, gate_calls):
        bmod, calls = gate_calls
        ap = AppProcess(app_name="stale", port=9172, healthy=True)  # never tracked

        bmod._demote(ap, reason="test")

        assert ap.healthy is True  # untouched
        assert calls == []  # no scrub of a name this record no longer owns

    def test_promote_refuses_an_untracked_record(self, gate_calls):
        bmod, calls = gate_calls
        ap = AppProcess(app_name="stale", port=9173, healthy=False)

        bmod._promote(ap)

        assert ap.healthy is False
        assert calls == []


class TestMcpReconcileHonoursRecordIdentity:
    """A stale watcher must not move the SUCCESSOR's MCP entry (review finding).

    The MCP writers key on the app NAME, not on the record: `_deregister_mcp_servers`
    removes every `<app>:` entry. So the identity guard has to stay effective through
    the reconcile, not merely alongside the flag write — otherwise a demotion decided
    about a retired record scrubs the live successor's servers, and a stale promotion
    republishes the predecessor's dead port.
    """

    @pytest.fixture
    def wired(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        calls: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration",
            lambda name, port, *, healthy: (calls.append((name, port, healthy)), True)[1],
        )
        with bmod._lock:
            bmod._processes.clear()
        try:
            yield bmod, calls
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_retired_record_cannot_scrub_the_successors_servers(self, wired):
        bmod, calls = wired
        old = AppProcess(app_name="app", port=9180, healthy=True)
        new = AppProcess(app_name="app", port=9181, healthy=True)
        with bmod._lock:
            bmod._processes["app"] = new  # a stop/start already replaced `old`

        bmod._demote(old, reason="its own process exited")

        assert calls == []  # the successor's `app:` entries are untouched
        assert new.healthy is True
        assert bmod.get_app_backend_port("app") == 9181

    def test_a_retired_record_cannot_republish_its_dead_port(self, wired):
        bmod, calls = wired
        old = AppProcess(app_name="app", port=9182, healthy=False)
        new = AppProcess(app_name="app", port=9183, healthy=True)
        with bmod._lock:
            bmod._processes["app"] = new

        bmod._promote(old)

        assert calls == []  # never re-registers 9182, which nothing serves any more

    def test_the_reconcile_runs_under_the_serialization_lock(self, wired):
        # The ordering guarantee depends on the reconcile happening INSIDE
        # _health_reconcile_lock, not merely after the identity check. Observe it from
        # the reconcile itself, which is the only place the property is visible.
        bmod, calls = wired
        ap = AppProcess(app_name="app", port=9184, healthy=False)
        with bmod._lock:
            bmod._processes["app"] = ap
        held: list[bool] = []

        def _observe(name, port, *, healthy):
            held.append(_reconcile_lock_held())
            return True

        monkey = pytest.MonkeyPatch()
        monkey.setattr(bmod, "_gate_mcp_registration", _observe)
        monkey.setattr(bmod, "_app_enabled_state", lambda name: True)
        try:
            bmod._promote(ap)
        finally:
            monkey.undo()

        assert held == [True]

    def test_startup_registration_takes_the_same_lock_as_the_watch(self, monkeypatch):
        # Serializing only the watch would not be enough: the successor's own startup
        # registration has to queue behind a retiring watcher's reconcile, or the two
        # can still interleave and leave the dead port as the last write.
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_RETRIES", 2)
        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *a, **k: _FakeHealthResp())
        held: list[bool] = []
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration",
            lambda name, port, *, healthy: (held.append(_reconcile_lock_held()), True)[1],
        )
        ap = AppProcess(app_name="boot", port=9185, healthy=False)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["boot"] = ap
        try:
            assert bmod._health_check_loop(ap, "/health") is ap
            assert held == [True]
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestStartupProbeCannotPromoteAReplacement:
    """The startup poll must promote only the record it actually probed (review finding).

    Resolving the record by name AFTER the probe let a stop/start landing mid-probe hand
    back the successor, which was then marked healthy — and therefore routed to, and MCP
    registered — on evidence gathered about its predecessor, without ever having
    answered a probe itself.
    """

    def test_a_successor_installed_mid_probe_is_not_promoted(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_RETRIES", 2)
        calls: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration",
            lambda name, port, *, healthy: (calls.append((name, port, healthy)), True)[1],
        )

        original = AppProcess(app_name="app", port=9190, healthy=False)
        successor = AppProcess(app_name="app", port=9191, healthy=False)

        def _probe_then_swap(_port, _path):
            # The stop/start lands while the probe is in flight.
            with bmod._lock:
                bmod._processes["app"] = successor
            return True

        monkeypatch.setattr(bmod, "_health_probe", _probe_then_swap)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = original
        try:
            result = bmod._health_check_loop(original, "/health")

            assert result is None  # nothing promoted, so no watch is armed either
            assert successor.healthy is False  # never answered a probe of its own
            assert calls == []  # and its MCP entry was not written from a stale port
            assert bmod.get_app_backend_port("app") is None
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestFailedMcpReconcileIsRetried:
    """A reconcile that did not land must be retried (review finding).

    The health FLAG moves whether or not mcp.json could be written. Gating the
    reconcile on the health *transition* alone therefore stranded a failed write until
    the next transition — which, for a backend that then stays put, never comes: a dead
    URL kiro-cli dials on every session, or a live backend with no MCP entry at all.
    """

    @pytest.fixture
    def watched(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        ap = AppProcess(app_name="w", port=9200, pid=1, proc=_FakeProc(),
                        healthy=True, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["w"] = ap
        probes: list[bool] = []

        def _scripted(_port, _path):
            if not probes:
                with bmod._lock:
                    bmod._processes.pop("w", None)
                return False
            return probes.pop(0)
        monkeypatch.setattr(bmod, "_health_probe", _scripted)
        try:
            yield bmod, ap, probes
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_failed_scrub_is_reattempted_on_the_next_sweep(self, monkeypatch, watched):
        bmod, ap, probes = watched
        attempts: list[bool] = []

        def _gate(name, port, *, healthy):
            attempts.append(healthy)
            return len(attempts) > 1  # the first write fails, the retry lands
        monkeypatch.setattr(bmod, "_gate_mcp_registration", _gate)

        # Demote on sweep 3, then two more sweeps with the verdict unchanged.
        probes.extend([False] * (bmod._HEALTH_WATCH_FAILURES + 2))
        bmod._watch_backend_health(ap, "/health")

        assert attempts == [False, False]  # retried once, then stopped once it landed
        assert ap.mcp_healthy is False  # record only advances on a write that landed

    def test_a_landed_reconcile_is_not_rewritten_every_sweep(self, monkeypatch, watched):
        bmod, ap, probes = watched
        attempts: list[bool] = []
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration",
            lambda name, port, *, healthy: (attempts.append(healthy), True)[1],
        )

        probes.extend([False] * (bmod._HEALTH_WATCH_FAILURES + 3))
        bmod._watch_backend_health(ap, "/health")

        assert attempts == [False]  # one scrub; the steady state writes nothing further

    def test_an_exited_process_scrubs_an_entry_the_flag_had_already_left_behind(
        self, monkeypatch, watched
    ):
        # healthy=False but mcp_healthy=True is exactly the state a failed scrub leaves.
        # The terminal exit path has to consult the entry, not just the flag, or the
        # dead URL survives the watch's own shutdown.
        bmod, ap, probes = watched
        attempts: list[bool] = []
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration",
            lambda name, port, *, healthy: (attempts.append(healthy), True)[1],
        )
        ap.healthy = False
        ap.mcp_healthy = True
        ap.proc = _FakeProc(returncode=1)

        bmod._watch_backend_health(ap, "/health")

        assert attempts == [False]
        assert ap.mcp_healthy is False


class TestStartupPollBelongsToOneGeneration:
    """The whole startup poll is bound to the record it started on (design review).

    Pinning per ATTEMPT is not enough: a stop/start between attempts hands a later
    attempt the successor, and this poll then acts on it — promoting it, or on
    exhaustion scrubbing it — on evidence gathered entirely about its predecessor,
    having never probed the successor's port.
    """

    @pytest.fixture
    def wired(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_RETRIES", 4)
        calls: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration",
            lambda name, port, *, healthy: (calls.append((name, port, healthy)), True)[1],
        )
        with bmod._lock:
            bmod._processes.clear()
        try:
            yield bmod, calls
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_exhaustion_does_not_scrub_a_successor_that_replaced_it(
        self, monkeypatch, wired
    ):
        # The harmful shape the design review named: the retiring poll's terminal scrub
        # deregisters by app name, taking the healthy successor's entry with it — and
        # because that bypasses the record, the successor's mcp_healthy still reads True,
        # so the watch's retry never fires and the live backend stays MCP-less.
        bmod, calls = wired
        original = AppProcess(app_name="a", port=9210, healthy=False)
        successor = AppProcess(app_name="a", port=9211, healthy=True, mcp_healthy=True)
        with bmod._lock:
            bmod._processes["a"] = original

        swapped = {"done": False}

        def _never_answers(_port, _path):
            if not swapped["done"]:  # the restart lands between attempts
                swapped["done"] = True
                with bmod._lock:
                    bmod._processes["a"] = successor
            return False
        monkeypatch.setattr(bmod, "_health_probe", _never_answers)

        assert bmod._health_check_loop(original, "/health") is None
        assert calls == []  # the successor's `a:` entries survive
        assert successor.mcp_healthy is True
        assert bmod.get_app_backend_port("a") == 9211

    def test_a_successor_is_not_promoted_by_a_later_attempt(self, monkeypatch, wired):
        bmod, calls = wired
        original = AppProcess(app_name="a", port=9212, healthy=False)
        successor = AppProcess(app_name="a", port=9213, healthy=False)
        with bmod._lock:
            bmod._processes["a"] = original

        state = {"n": 0}

        def _probe(_port, _path):
            state["n"] += 1
            if state["n"] == 1:
                with bmod._lock:
                    bmod._processes["a"] = successor
                return False
            return True  # a later attempt would "succeed" against the OLD port
        monkeypatch.setattr(bmod, "_health_probe", _probe)

        assert bmod._health_check_loop(original, "/health") is None
        assert successor.healthy is False  # never probed on its own port
        assert calls == []


class TestTerminalScrubIsRetriedUntilItLands:
    """The exit path may not leave the dead URL behind (review finding).

    This is the one place where giving up is permanent: nothing revisits an exited
    backend, so a scrub that did not land stays unlanded and kiro-cli keeps dialing the
    dead url every session. The retry the watch does elsewhere was useless here, because
    the terminal path returned before it could run.
    """

    @pytest.fixture
    def wired(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        ap = AppProcess(app_name="x", port=9220, pid=3,
                        proc=_FakeProc(returncode=1), healthy=True, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["x"] = ap
        monkeypatch.setattr(bmod, "_health_probe", lambda *_a: False)
        try:
            yield bmod, ap
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_failed_terminal_scrub_is_retried(self, monkeypatch, wired):
        bmod, ap = wired
        attempts: list[bool] = []

        def _gate(name, port, *, healthy):
            attempts.append(healthy)
            return len(attempts) >= 3  # the first two writes fail
        monkeypatch.setattr(bmod, "_gate_mcp_registration", _gate)

        bmod._watch_backend_health(ap, "/health")

        assert attempts == [False, False, False]  # retried until it landed
        assert ap.mcp_healthy is False

    def test_it_stops_as_soon_as_the_scrub_lands(self, monkeypatch, wired):
        bmod, ap = wired
        attempts: list[bool] = []
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration",
            lambda name, port, *, healthy: (attempts.append(healthy), True)[1],
        )

        bmod._watch_backend_health(ap, "/health")

        assert attempts == [False]  # one scrub, then the watch is done

    def test_it_gives_up_when_the_record_is_dropped_mid_retry(self, monkeypatch, wired):
        # stop_app_backend popping the record ends the watch even with the scrub
        # unlanded — that path owns the teardown from there, and a watch that spun on a
        # record nobody tracks would never exit.
        bmod, ap = wired
        attempts: list[bool] = []

        def _gate(name, port, *, healthy):
            attempts.append(healthy)
            with bmod._lock:
                bmod._processes.pop("x", None)
            return False  # never lands
        monkeypatch.setattr(bmod, "_gate_mcp_registration", _gate)

        bmod._watch_backend_health(ap, "/health")

        assert attempts == [False]  # did not spin

    def test_nothing_registered_means_nothing_to_unwind(self, monkeypatch, wired):
        bmod, ap = wired
        ap.healthy = False
        ap.mcp_healthy = False
        attempts: list[bool] = []
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration",
            lambda name, port, *, healthy: (attempts.append(healthy), True)[1],
        )

        bmod._watch_backend_health(ap, "/health")

        assert attempts == []


class TestTeardownParticipatesInTheReconcileSerialization:
    """stop_app_backend takes the reconcile lock (review finding).

    A watcher that had already passed its identity check could still be inside
    `_gate_mcp_registration` when the caller's `deregister_app` scrubs — its write would
    land after, restoring the dead url the gate exists to keep out of mcp.json.
    """

    def test_stop_holds_the_reconcile_lock_across_the_pop(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        ap = AppProcess(app_name="s", port=9230, healthy=True, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["s"] = ap
        held: list[bool] = []
        real_lock = bmod._lock

        class _SpyLock:
            """Records whether the reconcile lock is held whenever `_lock` is entered.

            Observing the nesting directly is the point: the guarantee is that the pop
            happens INSIDE the serialization, and only the lock state at the moment
            `_lock` is taken can show that.
            """

            def __enter__(self):
                held.append(_reconcile_lock_held())
                return real_lock.__enter__()

            def __exit__(self, *exc):
                return real_lock.__exit__(*exc)

        monkeypatch.setattr(bmod, "_lock", _SpyLock())
        try:
            bmod.stop_app_backend("s")
        finally:
            monkeypatch.setattr(bmod, "_lock", real_lock)
            with bmod._lock:
                bmod._processes.clear()

        assert held and held[0] is True, held


class TestAdoptedRecoveryRebindsOwnership:
    """An adopted recovery must re-bind its owning PIDs (review finding).

    `adopted_pids` is what `stop_app_backend` signals and what uninstall acts behind. A
    recovery means the EXTERNAL supervisor put something back, possibly a different
    process — so promoting on the adoption-time identities would mark the record
    freshly-valid while naming a process that is gone.
    """

    @pytest.fixture
    def watched(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration", lambda name, port, *, healthy: True
        )
        ap = AppProcess(app_name="ad", port=9240, pid=0, proc=None, healthy=False,
                        mcp_healthy=False, adopted_pids=[111],
                        adopted_start_times={111: "old"})
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["ad"] = ap
        probes: list[bool] = []

        def _scripted(_port, _path):
            if not probes:
                with bmod._lock:
                    bmod._processes.pop("ad", None)
                return False
            return probes.pop(0)
        monkeypatch.setattr(bmod, "_health_probe", _scripted)
        try:
            yield bmod, ap, probes
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_recovery_recaptures_the_owner_set(self, monkeypatch, watched):
        bmod, ap, probes = watched
        monkeypatch.setattr(
            bmod, "_capture_adopted_owners",
            lambda name, port, path: ([222], {222: "new"}),
        )
        probes.append(True)

        bmod._watch_backend_health(ap, "/health")

        assert ap.healthy is True
        assert ap.adopted_pids == [222]  # rebound to the replacement
        assert ap.adopted_start_times == {222: "new"}

    def test_unconfirmable_ownership_refuses_the_promotion(self, monkeypatch, watched):
        # Unhealthy-but-serving is recoverable next sweep; a mis-bound owner set is not,
        # because stop would signal the wrong PIDs while the replacement keeps running.
        bmod, ap, probes = watched
        monkeypatch.setattr(
            bmod, "_capture_adopted_owners", lambda name, port, path: None
        )
        probes.append(True)

        bmod._watch_backend_health(ap, "/health")

        assert ap.healthy is False  # refused
        assert ap.adopted_pids == [111]  # untouched, not silently half-updated
        assert bmod.get_app_backend_port("ad") is None

    def test_a_spawned_backend_does_not_pay_the_recapture(self, monkeypatch, watched):
        # Only the adopted shape has external ownership to re-bind; a spawned backend
        # holds its own Popen and must not take this path.
        bmod, ap, probes = watched
        called: list[str] = []
        monkeypatch.setattr(
            bmod, "_capture_adopted_owners",
            lambda name, port, path: called.append(name) or ([9], {9: "x"}),
        )
        ap.proc = _FakeProc()
        probes.append(True)

        bmod._watch_backend_health(ap, "/health")

        assert called == []
        assert ap.healthy is True


class TestUnknownMcpStateStillGetsScrubbed:
    """`mcp_healthy` is tri-state; only False means confirmed-scrubbed (review finding).

    A startup reconcile that failed leaves `healthy=True, mcp_healthy=None` — the entry
    may well be on disk, and None records only that we never confirmed a write. Treating
    that as "nothing was ever registered" abandoned precisely the entry most in need of
    removal, one sweep after the process exited.
    """

    def test_a_none_mcp_state_keeps_retrying_the_terminal_scrub(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        attempts: list[bool] = []

        def _gate(name, port, *, healthy):
            attempts.append(healthy)
            return len(attempts) >= 3  # the first two scrubs fail
        monkeypatch.setattr(bmod, "_gate_mcp_registration", _gate)
        monkeypatch.setattr(bmod, "_health_probe", lambda *_a: False)

        # The state a failed startup reconcile leaves: promoted, never confirmed written.
        ap = AppProcess(app_name="u", port=9250, pid=5,
                        proc=_FakeProc(returncode=1), healthy=True, mcp_healthy=None)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["u"] = ap
        try:
            bmod._watch_backend_health(ap, "/health")
            assert attempts == [False, False, False]  # kept going past sweep 2
            assert ap.mcp_healthy is False
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_confirmed_scrub_still_ends_the_watch(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        attempts: list[bool] = []
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration",
            lambda name, port, *, healthy: (attempts.append(healthy), True)[1],
        )
        monkeypatch.setattr(bmod, "_health_probe", lambda *_a: False)

        ap = AppProcess(app_name="u", port=9251, pid=5,
                        proc=_FakeProc(returncode=0), healthy=False, mcp_healthy=False)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["u"] = ap
        try:
            bmod._watch_backend_health(ap, "/health")
            assert attempts == []  # nothing to unwind, and it does not spin
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestHealthProbeSecurity:
    """App-authored health paths cannot redirect a loopback liveness probe."""

    @pytest.mark.parametrize(
        "path",
        ["/health", "/healthz?verbose=1", "/a/b_c-d.e~f", "/", "//example.com/x"],
    )
    def test_ordinary_paths_preserve_the_loopback_authority(self, path):
        import urllib.parse

        import kiro_crew.apps.backend as bmod

        url = bmod._health_probe_url(9101, path)
        assert url == f"http://127.0.0.1:9101{path}"
        assert urllib.parse.urlsplit(url).hostname == "127.0.0.1"

    @pytest.mark.parametrize(
        "path",
        [
            "@example.com/", "health", "", "/a@b", "/x\ny", "/x\ty",
            "/x\ry", "/x y", "http://example.com/", "/#frag", "/a\\b", "/[::1]",
        ],
    )
    def test_authority_smuggling_and_ambiguous_paths_are_refused(self, path):
        import kiro_crew.apps.backend as bmod

        assert bmod._health_probe_url(9101, path) is None

    @pytest.mark.parametrize("port", [0, -1, 65536, 99999])
    def test_impossible_ports_are_refused(self, port):
        import kiro_crew.apps.backend as bmod

        assert bmod._health_probe_url(port, "/health") is None

    def test_the_userinfo_payload_would_otherwise_leave_loopback(self):
        import urllib.parse

        import kiro_crew.apps.backend as bmod

        naive = "http://127.0.0.1:9101@example.com/"
        assert urllib.parse.urlsplit(naive).hostname == "example.com"
        assert bmod._health_probe_url(9101, "@example.com/") is None

    def test_an_invalid_path_never_reaches_http_and_warns_only_once(
        self, monkeypatch, caplog
    ):
        import logging

        import kiro_crew.apps.backend as bmod

        monkeypatch.setattr(bmod, "_warned_health_paths", set())
        monkeypatch.setattr(
            bmod,
            "loopback_urlopen",
            lambda *_a, **_k: pytest.fail("unsafe health path reached HTTP client"),
        )
        with caplog.at_level(logging.WARNING, logger=bmod.logger.name):
            for _ in range(3):
                assert bmod._health_probe(9101, "@example.com/") is False

        warnings = [r.message for r in caplog.records if "health path" in r.message]
        assert len(warnings) == 1
        assert "@example.com/" in warnings[0]

    def test_a_valid_path_uses_the_hardened_opener_with_the_exact_url(
        self, monkeypatch
    ):
        import kiro_crew.apps.backend as bmod

        seen = {}

        def _open(req, *, timeout):
            seen.update(url=req.full_url, timeout=timeout)
            return _FakeHealthResp()

        monkeypatch.setattr(bmod, "loopback_urlopen", _open)
        monkeypatch.setattr(
            bmod.urllib.request,
            "urlopen",
            lambda *_a, **_k: pytest.fail("bare urlopen bypassed loopback policy"),
        )

        assert bmod._health_probe(9101, "/health?q=1", timeout=1.5) is True
        assert seen == {"url": "http://127.0.0.1:9101/health?q=1", "timeout": 1.5}

    def test_adoption_startup_and_watch_share_the_same_probe_contract(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        calls = []

        def _probe(port, path, **kwargs):
            calls.append((port, path, kwargs))
            return False

        monkeypatch.setattr(bmod, "_health_probe", _probe)
        assert bmod._probe_adoption_health(9101, "/adopt") is False

        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_RETRIES", 1)
        monkeypatch.setattr(bmod, "_set_backend_health", lambda *_a, **_k: True)
        startup = AppProcess(app_name="startup-contract", port=9102)
        with bmod._lock:
            bmod._processes[startup.app_name] = startup
        try:
            assert bmod._health_check_loop(startup, "/startup") is None
        finally:
            with bmod._lock:
                bmod._processes.clear()

        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        watch = AppProcess(
            app_name="watch-contract", port=9103, healthy=True, mcp_healthy=True
        )

        def _watch_probe(port, path, **kwargs):
            calls.append((port, path, kwargs))
            with bmod._lock:
                bmod._processes.pop(watch.app_name, None)
            return True

        monkeypatch.setattr(bmod, "_health_probe", _watch_probe)
        with bmod._lock:
            bmod._processes[watch.app_name] = watch
        bmod._watch_backend_health_sweeps(watch, "/watch")

        assert calls == [
            (9101, "/adopt", {"timeout": 3}),
            (9102, "/startup", {}),
            (9103, "/watch", {}),
        ]


class TestProbeSurvivesAMalformedHttpResponse:
    """`http.client.HTTPException` is not an `OSError` (review finding).

    An app backend is arbitrary third-party code — an `exec` backend, or an adopted
    process we do not own — so a non-HTTP first line on the port is a real condition.
    `BadStatusLine` escaping `_health_probe` would kill the standing daemon watch and
    freeze `healthy` at its last value: the write-once behaviour this PR removes,
    silently restored.
    """

    def _serve_garbage(self, payload: bytes) -> int:
        import socket
        import threading
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(1)

        def _serve():
            try:
                conn, _ = srv.accept()
                conn.recv(4096)
                conn.sendall(payload)
                conn.close()
            except OSError:
                pass
            finally:
                srv.close()

        threading.Thread(target=_serve, daemon=True).start()
        return port

    def test_a_malformed_status_line_is_a_failed_probe_not_a_raise(self):
        # Drives a REAL socket rather than a stubbed exception: the whole finding is
        # about which concrete exception urllib lets through, so faking it would beg
        # the question.
        import kiro_crew.apps.backend as bmod
        port = self._serve_garbage(b"NOT-HTTP garbage\r\n\r\n")

        assert bmod._health_probe(port, "/health") is False

    def test_the_adoption_probe_survives_it_too(self):
        # Same class of bug in the sibling probe — fixed together so one does not sit
        # next to the other still wrong.
        import kiro_crew.apps.backend as bmod
        port = self._serve_garbage(b"\x00\x01binary noise\r\n\r\n")

        assert bmod._probe_adoption_health(port, "/health") is False


class TestWatchSurvivesAnUnexpectedSweepFault:
    """A dying watch silently restores write-once, so a sweep fault must not kill it."""

    def test_the_watch_restarts_after_an_unexpected_exception(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration", lambda name, port, *, healthy: True
        )
        ap = AppProcess(app_name="w", port=9260, proc=_FakeProc(),
                        healthy=True, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["w"] = ap

        calls = {"n": 0}

        def _probe(_port, _path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("something nobody anticipated")
            with bmod._lock:  # second sweep: end the watch cleanly
                bmod._processes.pop("w", None)
            return True
        monkeypatch.setattr(bmod, "_health_probe", _probe)
        try:
            bmod._watch_backend_health(ap, "/health")
            assert calls["n"] == 2  # survived the fault and swept again
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_it_does_not_restart_once_the_record_is_gone(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        ap = AppProcess(app_name="w", port=9261, proc=_FakeProc(),
                        healthy=True, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["w"] = ap

        calls = {"n": 0}

        def _probe(_port, _path):
            calls["n"] += 1
            with bmod._lock:
                bmod._processes.pop("w", None)
            raise RuntimeError("faults while being torn down")
        monkeypatch.setattr(bmod, "_health_probe", _probe)
        try:
            bmod._watch_backend_health(ap, "/health")
            assert calls["n"] == 1  # did not spin on a record nobody tracks
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestScrubAlsoRefreshesMaterializedAgents:
    """Removing a server from the global map is only half the removal (review finding).

    An app's materialized agent JSONs COPY the server's launch spec, and the agent config
    is what kiro-cli actually loads — so scrubbing `mcp.json` alone leaves the agents
    still naming the dead url. The refresh goes through `bridges.refresh_app_agents`,
    which already carries the guards this path must honour: a `resources="app"` app
    publishes its own agents, and a denied app's are scrubbed rather than rewritten.
    """

    def test_an_unhealthy_reconcile_refreshes_the_apps_agents(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        seen: list[str] = []
        monkeypatch.setattr(
            brmod, "scrub_backend_mcp_url", lambda name, unreconciled=None: []
        )
        monkeypatch.setattr(
            brmod, "refresh_app_agents",
            lambda name, io_failures=None: (seen.append(name), [])[1],
        )

        # The demotion path's agent refresh is gated on the app still being
        # enabled — it re-materializes files a disable would have removed. The
        # gate itself is pinned by TestDemotionRefreshIsGatedOnEnablement.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        assert bmod._gate_mcp_registration("a", 9270, healthy=False) is True
        assert seen == ["a"]

    def test_a_healthy_reconcile_does_not_take_the_scrub_path(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        seen: list[str] = []
        monkeypatch.setattr(brmod, "reregister_app_mcp_servers",
                            lambda name, live_port=None, io_failures=None: [])
        monkeypatch.setattr(
            brmod, "refresh_app_agents",
            lambda name, io_failures=None: (seen.append(name), [])[1],
        )

        assert bmod._gate_mcp_registration("a", 9271, healthy=True) is True
        # reregister_app_mcp_servers does its own agent refresh; this must not double it.
        assert seen == []

    def test_an_agent_io_failure_makes_the_reconcile_unlanded(self, monkeypatch):
        # The agent JSON is the file kiro-cli reads, so a scrub whose agent half failed
        # has NOT achieved what the scrub exists for. Reporting it landed would let
        # `mcp_healthy` advance and strand the dead url there permanently.
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(
            brmod, "scrub_backend_mcp_url", lambda name, unreconciled=None: []
        )

        def _refresh(name, io_failures=None):
            if io_failures is not None:
                io_failures.append("a--agent.json")
            return []
        monkeypatch.setattr(brmod, "refresh_app_agents", _refresh)

        # The demotion path's agent refresh is gated on the app still being
        # enabled — it re-materializes files a disable would have removed. The
        # gate itself is pinned by TestDemotionRefreshIsGatedOnEnablement.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        assert bmod._gate_mcp_registration("a", 9272, healthy=False) is False

    def test_nothing_to_refresh_is_not_a_failure(self, monkeypatch):
        # A self-managed app, a denied one, and an app with no declared agents all return
        # an empty list. None is a failed write, and retrying them never converges — only
        # the io_failures collector means retry.
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(
            brmod, "scrub_backend_mcp_url", lambda name, unreconciled=None: []
        )
        monkeypatch.setattr(brmod, "refresh_app_agents", lambda name, io_failures=None: [])

        # The demotion path's agent refresh is gated on the app still being
        # enabled — it re-materializes files a disable would have removed. The
        # gate itself is pinned by TestDemotionRefreshIsGatedOnEnablement.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        assert bmod._gate_mcp_registration("selfmanaged", 9273, healthy=False) is True


class TestSupervisorIsBoundAtSpawn:
    """The supervisor carries its record, not a name to re-resolve (review finding).

    A name and a port are two independent inputs that can disagree. A stop/restart
    landing between the spawn inserting the record and the supervisor thread's first
    statement would hand a name lookup the SUCCESSOR while the port argument still named
    the predecessor — and the poll would then promote, or on exhaustion scrub, a backend
    whose port it never probed.
    """

    def test_the_starter_hands_over_the_record_itself(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        captured: list[object] = []
        monkeypatch.setattr(
            bmod.threading, "Thread",
            lambda **kw: SimpleNamespace(start=lambda: captured.append(kw["args"])),
        )
        ap = AppProcess(app_name="s", port=9290)

        bmod._start_health_supervisor(ap, "/health")

        assert captured == [(ap, "/health")]  # the record, not ("s", 9290)

    def test_a_restart_before_the_thread_runs_cannot_divert_the_poll(self, monkeypatch):
        # Simulates the window: the successor is installed before the supervisor's first
        # statement. Binding to the record means the poll retires instead of acting on a
        # generation it never probed.
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_RETRIES", 2)
        gate: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration",
            lambda name, port, *, healthy: (gate.append((name, port, healthy)), True)[1],
        )
        monkeypatch.setattr(bmod, "_health_probe", lambda *_a: True)

        original = AppProcess(app_name="s", port=9291, healthy=False)
        successor = AppProcess(app_name="s", port=9292, healthy=False)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["s"] = successor  # the restart already landed
        try:
            bmod._supervise_backend_health(original, "/health")
            assert successor.healthy is False  # never promoted on the old port's evidence
            assert gate == []
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestRegistrationReportsAgentIoFailuresToo:
    """The healthy branch owes the same guarantee as the scrub (review finding).

    Both directions write the agent JSONs, and both let `mcp_healthy` advance on success.
    Reporting a failed agent write as landed on RECOVERY strands the app's agent without
    its MCP tools exactly as reporting one on demotion stranded the dead url — the same
    defect, mirrored, and fixing only one half was an oversight rather than a decision.
    """

    def test_a_failed_agent_write_on_recovery_is_unlanded(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        def _reregister(app_name, live_port=None, io_failures=None):
            if io_failures is not None:
                io_failures.append("a--agent.json")
            return ["a:backend"]
        monkeypatch.setattr(brmod, "reregister_app_mcp_servers", _reregister)

        assert bmod._gate_mcp_registration("a", 9300, healthy=True) is False

    def test_a_clean_registration_still_lands(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(
            brmod, "reregister_app_mcp_servers",
            lambda app_name, live_port=None, io_failures=None: ["a:backend"],
        )

        assert bmod._gate_mcp_registration("a", 9301, healthy=True) is True

    def test_the_live_port_is_still_threaded_through(self, monkeypatch):
        # The collector must not displace the argument that makes the url reachable.
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        seen: dict[str, object] = {}

        def _reregister(app_name, live_port=None, io_failures=None):
            seen["port"] = live_port
            return []
        monkeypatch.setattr(brmod, "reregister_app_mcp_servers", _reregister)

        assert bmod._gate_mcp_registration("a", 9302, healthy=True) is True
        assert seen == {"port": 9302}


class TestPromotionRequiresAConfirmedEnabledApp:
    """A disabled app must not be resurrected by a health recovery (#5726 review).

    `kirocrew app disable` runs in its OWN process: it deregisters the app's resources
    and never touches this process's tracking table. The record survives, so a later
    recovery would re-register the MCP servers and agents the operator just removed.
    Demotion is never gated — scrubbing is always safe.
    """

    @pytest.fixture
    def tracked(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        calls: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration",
            lambda name, port, *, healthy: (calls.append((name, port, healthy)), True)[1],
        )
        ap = AppProcess(app_name="app", port=9310, healthy=False, mcp_healthy=False)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = ap
        try:
            yield bmod, ap, calls
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_disabled_app_is_not_promoted(self, monkeypatch, tracked):
        bmod, ap, calls = tracked
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)

        assert bmod._set_backend_health(ap, healthy=True) is False
        assert ap.healthy is False
        assert calls == []  # nothing re-registered for an app the operator disabled

    def test_an_enabled_app_is_promoted(self, monkeypatch, tracked):
        bmod, ap, calls = tracked
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        assert bmod._set_backend_health(ap, healthy=True) is True
        assert ap.healthy is True
        assert calls == [("app", 9310, True)]

    def test_demotion_is_never_gated(self, monkeypatch, tracked):
        # Refusing a scrub because enablement cannot be confirmed would strand the dead
        # url — the exact failure this whole gate exists to prevent.
        bmod, ap, calls = tracked
        ap.healthy = True
        ap.mcp_healthy = True
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)

        assert bmod._set_backend_health(ap, healthy=False) is True
        assert ap.healthy is False
        assert calls == [("app", 9310, False)]

    # The unreadable-state case is covered by
    # TestEnabledStateDistinguishesUnreadableFromDisabled, which drives REAL files. A
    # stub of `is_app_enabled` cannot test it: the defect was that the reader collapses
    # a read failure into False without raising, so stubbing it to raise would assert a
    # path production never takes.


class TestPromotionIsVerifiedAfterTheWrite:
    """The enabled check cannot be atomic with the write (#5726 review).

    `kirocrew app disable` runs in another process, so there is no lock to share. Ordering
    closes the interleave where the flag is read after the resources come down; this
    covers the other one — the check passes, the disable completes, and the write lands
    afterwards, leaving a disabled app dispatchable.
    """

    @pytest.fixture
    def tracked(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        calls: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration",
            lambda name, port, *, healthy: (calls.append((name, port, healthy)), True)[1],
        )
        ap = AppProcess(app_name="app", port=9320, healthy=False, mcp_healthy=False)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = ap
        try:
            yield bmod, ap, calls
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_disable_landing_mid_write_is_undone(self, monkeypatch, tracked):
        bmod, ap, calls = tracked
        # Enabled at the pre-check, disabled by the time the write has landed.
        states = iter([True, False])
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: next(states))
        undone: list[str] = []
        import kiro_crew.apps.bridges as brmod
        monkeypatch.setattr(brmod, "deregister_app", lambda n: undone.append(n))

        assert bmod._set_backend_health(ap, healthy=True) is False
        assert undone == ["app"]  # what the promotion registered is removed again
        assert ap.healthy is False
        assert ap.mcp_healthy is False

    def test_a_still_enabled_app_is_left_registered(self, monkeypatch, tracked):
        bmod, ap, calls = tracked
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        import kiro_crew.apps.bridges as brmod
        monkeypatch.setattr(
            brmod, "deregister_app",
            lambda n: pytest.fail("must not undo a promotion that is still valid"),
        )

        assert bmod._set_backend_health(ap, healthy=True) is True
        assert ap.healthy is True
        assert calls == [("app", 9320, True)]

    def test_a_demotion_is_never_verified_or_undone(self, monkeypatch, tracked):
        # Demotion is ungated going in and must stay ungated coming out: scrubbing a
        # disabled app's entry is exactly what should happen.
        bmod, ap, calls = tracked
        ap.healthy = True
        ap.mcp_healthy = True
        monkeypatch.setattr(
            bmod, "_app_enabled_state",
            lambda name: pytest.fail("a demotion must not consult enablement"),
        )

        assert bmod._set_backend_health(ap, healthy=False) is True
        assert calls == [("app", 9320, False)]


class TestUndoIsRetriedUntilItCompletes:
    """`deregister_app` reports softly, so a failed undo must not look clean (#5726 review).

    It returns problems in `RegistrationResult.errors` rather than raising, so recording
    the removal without reading that list leaves a disabled app's resources registered
    while the record claims otherwise — and the retry condition then sees agreement and
    never fires.
    """

    @pytest.fixture
    def disabled(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration", lambda name, port, *, healthy: True
        )
        ap = AppProcess(app_name="app", port=9330, healthy=True, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = ap
        try:
            yield bmod, ap
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_soft_failure_leaves_the_record_unreconciled(self, monkeypatch, disabled):
        bmod, ap = disabled
        import kiro_crew.apps.bridges as brmod
        monkeypatch.setattr(
            brmod, "deregister_app",
            lambda n: SimpleNamespace(errors=["could not remove agent: ENOSPC"]),
        )

        assert bmod._undo_promotion_of_disabled_app(ap) is False
        assert ap.healthy is False
        assert ap.mcp_healthy is True, "a removal that failed must not read as done"

    def test_a_clean_removal_is_recorded(self, monkeypatch, disabled):
        bmod, ap = disabled
        import kiro_crew.apps.bridges as brmod
        monkeypatch.setattr(brmod, "deregister_app", lambda n: SimpleNamespace(errors=[]))

        assert bmod._undo_promotion_of_disabled_app(ap) is True
        assert ap.mcp_healthy is False

    def test_a_raising_deregister_also_leaves_it_unreconciled(self, monkeypatch, disabled):
        bmod, ap = disabled
        import kiro_crew.apps.bridges as brmod

        def _boom(n):
            raise OSError("agents dir unwritable")
        monkeypatch.setattr(brmod, "deregister_app", _boom)

        assert bmod._undo_promotion_of_disabled_app(ap) is False
        assert ap.mcp_healthy is True

    def test_a_refused_promotion_retries_the_undo(self, monkeypatch, disabled):
        # Nothing else revisits a disabled app and the watch will not promote it, so the
        # refusal path is where an unlanded undo gets another attempt.
        bmod, ap = disabled
        attempts: list[str] = []
        import kiro_crew.apps.bridges as brmod
        monkeypatch.setattr(
            brmod, "deregister_app",
            lambda n: (attempts.append(n), SimpleNamespace(errors=["still failing"]))[1],
        )

        assert bmod._set_backend_health(ap, healthy=True) is False
        assert bmod._set_backend_health(ap, healthy=True) is False
        assert attempts == ["app", "app"]  # retried, not recorded and forgotten

    def test_a_completed_undo_is_not_retried(self, monkeypatch, disabled):
        bmod, ap = disabled
        ap.mcp_healthy = False  # already reconciled
        import kiro_crew.apps.bridges as brmod
        monkeypatch.setattr(
            brmod, "deregister_app",
            lambda n: pytest.fail("nothing of ours is registered; nothing to undo"),
        )

        assert bmod._set_backend_health(ap, healthy=True) is False


class TestDemotionRefreshIsGatedOnEnablement:
    """The demotion's agent refresh must not restore a disabled app (#5726 review).

    A demotion does two things: it scrubs the MCP entry — always safe, and deliberately
    ungated — and it re-materializes the agent configs. The second is a WRITE, so for an
    app the operator has disabled it puts back the very files a concurrent
    `deregister_app` just removed.
    """

    @pytest.fixture
    def wired(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        scrubbed: list[str] = []
        refreshed: list[str] = []
        dropped: list[str] = []
        monkeypatch.setattr(
            brmod, "scrub_backend_mcp_url",
            lambda name, unreconciled=None: (scrubbed.append(name), [])[1],
        )
        monkeypatch.setattr(
            brmod, "refresh_app_agents",
            lambda name, io_failures=None: (refreshed.append(name), [])[1],
        )
        monkeypatch.setattr(
            bmod, "_drop_disabled_app_resources",
            lambda name: (dropped.append(name), True)[1],
        )
        return bmod, scrubbed, refreshed, dropped

    def test_a_disabled_app_is_scrubbed_but_never_refreshed(self, monkeypatch, wired):
        bmod, scrubbed, refreshed, dropped = wired
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)

        assert bmod._gate_mcp_registration("app", 9340, healthy=False) is True
        assert scrubbed == ["app"]  # the scrub still happens — that is the desired outcome
        assert refreshed == []  # ...but nothing is written back
        assert dropped == ["app"]

    def test_a_disable_landing_mid_refresh_is_dropped(self, monkeypatch, wired):
        # Enabled at the pre-check, disabled by the time the refresh has landed. Neither
        # check can be atomic with the write, so only the pair converges.
        bmod, scrubbed, refreshed, dropped = wired
        states = iter([True, False])
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: next(states))

        assert bmod._gate_mcp_registration("app", 9341, healthy=False) is True
        assert refreshed == ["app"]  # it did run
        assert dropped == ["app"]  # ...and was undone

    def test_an_enabled_app_is_refreshed_and_not_dropped(self, monkeypatch, wired):
        bmod, scrubbed, refreshed, dropped = wired
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        assert bmod._gate_mcp_registration("app", 9342, healthy=False) is True
        assert refreshed == ["app"]
        assert dropped == []


class TestDisabledCleanupResultIsTheReconcileResult:
    """A failed disabled-app cleanup is not a completed reconcile (#5726 review)."""

    def test_a_failed_cleanup_reports_unlanded(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(
            brmod, "scrub_backend_mcp_url", lambda name, unreconciled=None: []
        )
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)
        monkeypatch.setattr(bmod, "_drop_disabled_app_resources", lambda name: False)

        assert bmod._gate_mcp_registration("app", 9350, healthy=False) is False

    def test_a_clean_cleanup_reports_landed(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(
            brmod, "scrub_backend_mcp_url", lambda name, unreconciled=None: []
        )
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)
        monkeypatch.setattr(bmod, "_drop_disabled_app_resources", lambda name: True)

        assert bmod._gate_mcp_registration("app", 9351, healthy=False) is True


class TestTheUndoNeverTouchesASuccessor:
    """Identity is checked BEFORE enablement (#5726 review).

    The undo deregisters by app NAME, so running it for a record that is no longer the
    tracked one deletes the SUCCESSOR's resources — and an unreadable enabled state is
    precisely what would send a retired watcher down that path.
    """

    def test_a_retired_record_with_unreadable_enablement_undoes_nothing(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        old = AppProcess(app_name="app", port=9360, healthy=False, mcp_healthy=True)
        successor = AppProcess(app_name="app", port=9361, healthy=True, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = successor
        monkeypatch.setattr(
            bmod, "_app_enabled_state",
            lambda name: pytest.fail("identity must be checked first"),
        )
        monkeypatch.setattr(
            bmod, "_undo_promotion_of_disabled_app",
            lambda ap: pytest.fail("must never deregister on behalf of a retired record"),
        )
        try:
            assert bmod._set_backend_health(old, healthy=True) is False
            assert successor.mcp_healthy is True
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestUnreadableManifestKeepsTheScrubUnlanded:
    """Keeping the agents is right, but it leaves them stale (#5726 review).

    `refresh_app_agents` gives up on the same unreadable manifest, so nothing else
    revisits those files. Recording the scrub as done would strand an agent config
    dialing the dead url forever; reporting it unlanded makes the watch retry until the
    manifest is readable and the refresh can correct them.
    """

    def test_an_unreadable_manifest_reports_unlanded(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        def _scrub(name, unreconciled=None):
            if unreconciled is not None:
                unreconciled.append(f"{name}: manifest unreadable")
            return []
        monkeypatch.setattr(brmod, "scrub_backend_mcp_url", _scrub)

        assert bmod._gate_mcp_registration("app", 9370, healthy=False) is False

    def test_a_readable_manifest_reports_landed(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(
            brmod, "scrub_backend_mcp_url", lambda name, unreconciled=None: []
        )
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(brmod, "refresh_app_agents", lambda name, io_failures=None: [])

        assert bmod._gate_mcp_registration("app", 9371, healthy=False) is True


class TestUnknownEnablementNeverDeletes:
    """Fail-closed is right for ADDING and wrong for DELETING (#5726 review).

    `installed.json` can fail to read transiently. Refusing to register when enablement
    is unknown is safe — the app stays as it is. Deregistering when it is unknown unlinks
    materialized agents and takes the user-owned fields `_preserve_user_agent_edits`
    carries, permanently, over a temporary fault.
    """

    @pytest.fixture
    def wired(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        dropped: list[str] = []
        monkeypatch.setattr(
            brmod, "scrub_backend_mcp_url", lambda name, unreconciled=None: []
        )
        monkeypatch.setattr(brmod, "refresh_app_agents", lambda name, io_failures=None: [])
        monkeypatch.setattr(
            bmod, "_drop_disabled_app_resources",
            lambda name: (dropped.append(name), True)[1],
        )
        return bmod, dropped

    def test_unknown_enablement_does_not_deregister(self, monkeypatch, wired):
        bmod, dropped = wired
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: None)

        assert bmod._gate_mcp_registration("app", 9380, healthy=False) is False
        assert dropped == [], "an unreadable state must never destroy user-edited agents"

    def test_a_confirmed_disable_does_deregister(self, monkeypatch, wired):
        bmod, dropped = wired
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)

        assert bmod._gate_mcp_registration("app", 9381, healthy=False) is True
        assert dropped == ["app"]

    def test_unknown_enablement_still_refuses_a_promotion(self, monkeypatch):
        # The other direction is unchanged: not-confirmed means do not add. Asserted
        # through the transition rather than a predicate, because what matters is that
        # the promotion does not happen AND nothing is deleted on the way.
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: None)
        monkeypatch.setattr(
            brmod, "deregister_app",
            lambda n: pytest.fail("an unknown state must never delete"),
        )
        ap = AppProcess(app_name="app", port=9382, healthy=False, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = ap
        try:
            assert bmod._set_backend_health(ap, healthy=True) is False
            assert ap.healthy is False
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestATransitionAlwaysReconciles:
    """`mcp_healthy` can be stale in the other direction (#5726 review).

    An MCP write that landed followed by an agent write that did not leaves `mcp_healthy`
    unmoved while the entry IS on disk. If the verdict then flips, matching that stale
    value would skip the scrub and leave the dead url registered.
    """

    def test_a_demotion_reconciles_even_when_the_flags_agree(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        calls: list[bool] = []
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration",
            lambda name, port, *, healthy: (calls.append(healthy), True)[1],
        )
        # healthy=True with mcp_healthy=False is exactly what a partial reconcile leaves.
        ap = AppProcess(app_name="app", port=9390, healthy=True, mcp_healthy=False)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = ap
        try:
            assert bmod._set_backend_health(ap, healthy=False) is True
            assert calls == [False], "the transition must scrub, not trust the stale flag"
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_an_unchanged_verdict_still_short_circuits(self, monkeypatch):
        # The fast path has to survive: without it a settled backend would rewrite
        # mcp.json on every sweep.
        import kiro_crew.apps.backend as bmod

        monkeypatch.setattr(
            bmod, "_gate_mcp_registration",
            lambda name, port, *, healthy: pytest.fail("nothing changed; nothing to write"),
        )
        ap = AppProcess(app_name="app", port=9391, healthy=False, mcp_healthy=False)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = ap
        try:
            assert bmod._set_backend_health(ap, healthy=False) is True
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestTheUndoPathsAlsoRefuseAnUnknownState:
    """The tri-state rule applies to ALL THREE deletion sites (#5726 review).

    The demotion path was fixed first; the two undo calls inside `_set_backend_health`
    were not, and they reach the same `deregister_app` → `_deregister_agents` → unlink.
    A transient `installed.json` fault must not destroy user-edited agent configs from
    any of them.
    """

    @pytest.fixture
    def tracked(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        deleted: list[str] = []
        monkeypatch.setattr(brmod, "deregister_app", lambda n: deleted.append(n))
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration", lambda name, port, *, healthy: True
        )
        ap = AppProcess(app_name="app", port=9400, healthy=False, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = ap
        try:
            yield bmod, ap, deleted
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_the_pre_check_undo_refuses_an_unknown_state(self, monkeypatch, tracked):
        bmod, ap, deleted = tracked
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: None)

        assert bmod._set_backend_health(ap, healthy=True) is False
        assert deleted == [], "unknown must not delete user-edited agents"

    def test_the_pre_check_undo_still_fires_on_a_confirmed_disable(self, monkeypatch, tracked):
        bmod, ap, deleted = tracked
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)

        assert bmod._set_backend_health(ap, healthy=True) is False
        assert deleted == ["app"]

    def test_the_post_write_verify_refuses_an_unknown_state(self, monkeypatch, tracked):
        # Enabled at the pre-check, unreadable by the verify: the registration stands,
        # because the pre-check confirmed it and deleting is the unrecoverable direction.
        bmod, ap, deleted = tracked
        states = iter([True, None])
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: next(states))

        assert bmod._set_backend_health(ap, healthy=True) is True
        assert deleted == []
        assert ap.healthy is True

    def test_the_post_write_verify_still_fires_on_a_confirmed_disable(
        self, monkeypatch, tracked
    ):
        bmod, ap, deleted = tracked
        states = iter([True, False])
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: next(states))

        assert bmod._set_backend_health(ap, healthy=True) is False
        assert deleted == ["app"]


class TestEnabledStateDistinguishesUnreadableFromDisabled:
    """The tri-state has to be real, not nominal (#5726 review).

    `is_app_enabled` returns False for BOTH a deliberate disable and an unreadable
    metadata file, because `_read_installed` answers None to both and never raises. Built
    on that, the "unknown" branch was unreachable for exactly the transient fault it
    exists to catch — so a momentary read blip during a demotion still deleted a
    still-enabled app's user-edited agents. Driven against REAL files, since the whole
    defect was a collapsed return value that a stubbed reader would hide.
    """

    def _meta(self, monkeypatch, tmp_path):
        import kiro_crew.apps.manager as mgr
        app = tmp_path / "probe"
        app.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mgr, "app_dir", lambda name: app)
        return mgr, app / mgr.INSTALLED_META_FILENAME

    def test_an_unreadable_file_is_unknown_not_disabled(self, monkeypatch, tmp_path):
        mgr, meta = self._meta(monkeypatch, tmp_path)
        meta.write_text("{ not json", encoding="utf-8")

        assert mgr.is_app_enabled("probe") is False  # the collapsed answer
        assert mgr.app_enabled_state("probe") is None  # ...kept apart here

    def test_a_deliberate_disable_is_false(self, monkeypatch, tmp_path):
        mgr, meta = self._meta(monkeypatch, tmp_path)
        meta.write_text(json.dumps({"name": "probe", "enabled": False}), encoding="utf-8")

        assert mgr.app_enabled_state("probe") is False

    def test_an_enabled_app_is_true(self, monkeypatch, tmp_path):
        mgr, meta = self._meta(monkeypatch, tmp_path)
        meta.write_text(json.dumps({"name": "probe", "enabled": True}), encoding="utf-8")

        assert mgr.app_enabled_state("probe") is True

    def test_a_missing_file_is_a_definite_false(self, monkeypatch, tmp_path):
        # Not installed is an ANSWER, not a failure to read one — deleting an
        # uninstalled app's leftovers is correct.
        mgr, _meta = self._meta(monkeypatch, tmp_path)

        assert mgr.app_enabled_state("probe") is False

    def test_the_backend_predicate_reports_unknown_for_an_unreadable_file(
        self, monkeypatch, tmp_path
    ):
        # The path that matters: backend must see None, or no deletion is ever refused.
        import kiro_crew.apps.backend as bmod
        mgr, meta = self._meta(monkeypatch, tmp_path)
        meta.write_text("{ not json", encoding="utf-8")

        assert bmod._app_enabled_state("probe") is None
