"""Fixtures and fakes shared by the ``test_app_backend*.py`` modules.

``app_env`` is defined as ``app_env_fixture`` under ``@pytest.fixture(name="app_env")``
so a test module can import it without each test's ``app_env`` parameter
shadowing the import.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME
from kiro_crew.testing.wait import wait_until


def _probe_outcome(ok: bool):
    """A `_health_probe` return value for a stub that only scripts healthy-or-not.

    The probe answers WHAT it observed, so a stub has to name something; these tests are
    about the transitions rather than the status, hence one shape for each verdict.
    """
    from kiro_crew.apps.backend import HealthProbeOutcome

    if ok:
        return HealthProbeOutcome.answered(200)
    return HealthProbeOutcome(None, "connection refused")


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
        "import http.server, os, sys\n"
        'port = int(os.environ.get("PORT", 9100))\n'
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        '        self.wfile.write(b"ok")\n'
        "    def log_message(self, *a): pass\n"
        'http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()\n'
    )
    return src


@pytest.fixture(name="app_env")
def app_env_fixture(tmp_path, monkeypatch, worker_id):
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
    if worker_id and worker_id != "master":  # wokeignore:rule=master
        try:
            idx = int(worker_id.replace("gw", "")) if worker_id.startswith("gw") else 0
        except ValueError:
            idx = 0
        # flake-ok: pre-existing per-worker window of the ports the product allocates for real backends; moved verbatim, tracked for a de-flake pass
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
        def _bindable(port: int) -> bool:
            s = _sock.socket()
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                return False
            finally:
                s.close()
            return True

        for port in ports:
            if not port:
                continue
            try:
                wait_until(lambda p=port: _bindable(p), timeout=5)
            except AssertionError:
                # Teardown is best effort: a port still held here surfaces as the
                # next test's own EADDRINUSE, which names the colliding spawn.
                pass

    _reap()  # clean slate before the test
    yield home
    _reap()  # and reap anything the test left running


class _FakeHealthResp:
    """Minimal urlopen() stand-in: a 200 response usable as a context manager."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _FakeProc:
    """Popen stand-in for the watch's liveness check: alive until it is given an rc."""

    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode
