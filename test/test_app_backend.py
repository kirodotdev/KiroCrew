"""Tests for kiro_crew.apps.backend — backend process management."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from kiro_crew.apps.backend import (
    AppProcess,
    _find_free_port,
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
    launcher's separate unshare(NEWNS) (errno 1). sandbox._probe_unshare() gives
    a false positive there (it does NEWUSER|NEWNS in a SINGLE unshare call), so
    gate the real-spawn tests on the production path itself: wrap a trivial
    command exactly as the backend does and confirm it exits 0. Reusing
    wrap_argv() means this probe can never drift from start_app_backend().
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
            "kiro_crew.apps.module_loader._third_party_apps_allowed", lambda: False
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

    def test_builtin_module_backend_not_blocked_by_gate(self, tmp_path, app_env, monkeypatch):
        # The gate must NOT block a builtin backend even when the switch is off.
        # Builtin-ness is the installed record's origin == "builtin" (the trusted
        # provenance signal), NOT the manifest entry format — so we persist an
        # installed record with origin="builtin". Reaching the spawn sentinel proves
        # the gate let it through.
        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.manager import (
            InstalledApp,
            _write_installed,
            app_dir,
            get_app_manifest,
        )

        root = app_dir("builtin-module-app")
        root.mkdir(parents=True, exist_ok=True)
        (root / APP_MANIFEST_FILENAME).write_text(
            json.dumps(
                {
                    "name": "builtin-module-app",
                    "version": "1.0.0",
                    "displayName": "Builtin",
                    "description": "module-style builtin backend",
                    "backend": {"entryPoint": "kiro_crew.apps.builtins.x.server", "port": "auto"},
                }
            )
        )
        _write_installed(
            "builtin-module-app",
            InstalledApp(name="builtin-module-app", origin="builtin", enabled=True),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.module_loader._third_party_apps_allowed", lambda: False
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
        manifest = get_app_manifest("builtin-module-app")
        assert manifest is not None
        # Reaching the spawn sentinel proves the gate did NOT block the builtin.
        with pytest.raises(_ReachedSpawn):
            bmod._start_app_backend_body("builtin-module-app", manifest)

    def test_third_party_dotted_entry_refused_when_gate_off(
        self, tmp_path, app_env, monkeypatch, caplog
    ):
        # security-review bypass: a third-party app (origin != builtin) must NOT
        # escape the off-switch by declaring a dotted module-style entryPoint. The
        # gate keys on provenance, not entry format, so this is DENIED before any spawn.
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
                    "description": "third-party with a dotted entryPoint",
                    "backend": {"entryPoint": "kiro_crew.cli_server", "port": "auto"},
                }
            )
        )
        _write_installed(
            "evil-dotted",
            InstalledApp(name="evil-dotted", origin="registry", enabled=True),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.module_loader._third_party_apps_allowed", lambda: False
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
