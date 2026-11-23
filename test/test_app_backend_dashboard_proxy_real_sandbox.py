"""A granted proxy backend mints through the real namespace sandbox; an ungranted one cannot.

A stub backend runs under the real namespace sandbox with the mask list and stand-ins
``apps/backend.py`` passes. The test process plays the gateway on a real loopback listener:
the real ``/api/token/local`` handler and the real token middleware. The stub reads
``run/gateway-<port>.secret``, presents what it read as ``X-Local-Secret``, and (when the
mint succeeds) fetches ``/api/auth/me``, a ``READ_ALLOW`` row that must answer 200 with its
allowed body, and ``/api/version``, which is not a row and must answer 403, both with the
token it got.

Linux needs unprivileged user and mount namespaces, which the namespace-sandbox CI job
provides and the shard runners do not, so there these tests skip with that reason.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from kiro_crew import platform_compat, sandbox
from kiro_crew.sandbox import userns_available

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only"),
    pytest.mark.skipif(
        not userns_available(),
        reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
        "namespaces)",
    ),
    pytest.mark.xdist_group("app_backend_secret_real_sandbox"),
]

_APP = "proxy-stub"
_REAL = "the-real-gateway-secret-in-words"
_STUB = r"""
import http.client, json, sys
port, secret_path = int(sys.argv[1]), sys.argv[2]
try:
    value = open(secret_path, encoding="utf-8").read().strip()
except OSError:
    value = ""
conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
conn.request("GET", "/api/token/local", headers={"X-Local-Secret": value})
resp = conn.getresponse()
body = resp.read().decode()
out = {"file_empty": value == "", "mint": resp.status}
if resp.status == 200:
    token = json.loads(body)["token"]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request("GET", "/api/auth/me?token=" + token)
    allowed = conn.getresponse()
    out["allowed_status"] = allowed.status
    out["allowed_body"] = json.loads(allowed.read().decode())
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request("GET", "/api/version?token=" + token)
    out["denied_status"] = conn.getresponse().status
print(json.dumps(out), flush=True)
"""


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    sandbox.reset_backend()
    yield home
    sandbox.reset_backend()


async def _gateway(home: Path, monkeypatch, state) -> TestServer:
    from kiro_crew.dashboard import token_auth
    from kiro_crew.dashboard.handlers import core as core_mod
    from kiro_crew.dashboard.routes import register_all

    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app["state"] = state
    app["local_secret"] = _REAL
    register_all(app)
    app.router.add_get("/api/token/local", core_mod.api_token_local)
    server = TestServer(app)
    await server.start_server()
    return server


async def _stub_report_line(proc: subprocess.Popen, seconds: float = 60) -> str:
    """The stub's one report line, once it has printed it and exited, within *seconds*.

    The read runs off the loop, because this loop serves the gateway the stub talks to. The
    exit is polled, not waited on from a pool thread. A stub that outlasts the deadline is
    killed and the test FAILS with its stderr; a timeout never passes or skips.
    """
    assert proc.stdout is not None and proc.stderr is not None
    deadline = time.monotonic() + seconds
    try:
        line = await asyncio.wait_for(asyncio.to_thread(proc.stdout.readline), seconds)
        while proc.poll() is None:
            if time.monotonic() > deadline:
                raise asyncio.TimeoutError
            await asyncio.sleep(0.05)
    except asyncio.TimeoutError:
        proc.kill()
        proc.wait()
        pytest.fail(
            f"the stub backend did not finish within {seconds} s; stderr: {proc.stderr.read()}"
        )
    return line


async def _run_stub(home: Path, monkeypatch, state, *, grant: bool) -> dict:
    import kiro_crew.apps.backend as bmod
    from kiro_crew.apps import dashboard_proxy

    dashboard_proxy.set_gateway_secret(_REAL)
    # Warm the backend probe off the loop: on the loop a cold cache answers "none", so
    # ``grant`` would refuse with ``platform-unsupported``.
    await asyncio.to_thread(dashboard_proxy.platform_supported)
    if grant:
        monkeypatch.setattr(dashboard_proxy, "registry_verified", lambda _n: True)
        assert dashboard_proxy.grant(_APP)[0] is True
    server = await _gateway(home, monkeypatch, state)
    (home / "run").mkdir(exist_ok=True)
    secret_file = home / "run" / f"gateway-{server.port}.secret"
    secret_file.write_text(_REAL, encoding="utf-8")
    hidden = sandbox.app_backend_secret_mask_targets()
    stand_ins = dashboard_proxy.stand_in_pairs(_APP, hidden)
    argv = sandbox.namespace_argv(
        [sys.executable, "-c", _STUB, str(server.port), str(secret_file)],
        "standard",
        extra_hidden_dirs=hidden,
        extra_stand_ins=stand_ins,
    )
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=home,
    )
    bmod._processes[_APP] = bmod.AppProcess(
        app_name=_APP,
        pid=proc.pid,
        proc=proc,
        pid_start_time=platform_compat.process_start_time(proc.pid),
    )
    try:
        line = await _stub_report_line(proc)
    finally:
        bmod._processes.pop(_APP, None)
        await server.close()
    return json.loads(line)


@pytest.mark.asyncio
async def test_a_granted_stub_reads_its_value_mints_and_fetches_status(home, monkeypatch):
    state = SimpleNamespace(owner_id="")
    _install_fixture(monkeypatch)
    report = await _run_stub(home, monkeypatch, state, grant=True)
    # One allowed row answers with exactly its allowed body; a row outside READ_ALLOW is refused.
    assert report == {
        "file_empty": False,
        "mint": 200,
        "allowed_status": 200,
        "allowed_body": {"token_accepted": True},
        "denied_status": 403,
    }


@pytest.mark.asyncio
async def test_an_ungranted_stub_reads_an_empty_file_and_is_refused(home, monkeypatch):
    state = SimpleNamespace(owner_id="")
    _install_fixture(monkeypatch)
    report = await _run_stub(home, monkeypatch, state, grant=False)
    assert report == {"file_empty": True, "mint": 403}


def _install_fixture(monkeypatch) -> None:
    """The install record and marker the grant reads, standing for an owner-enabled app."""
    from kiro_crew.apps import dashboard_proxy

    monkeypatch.setattr(dashboard_proxy, "install_identity", lambda _n: "pkg-one-v1")
    monkeypatch.setattr(dashboard_proxy, "_marker_state", lambda _n: (True, True))
