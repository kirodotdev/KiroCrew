"""``mint_and_attach`` (the ssh ProxyCommand) against the real dashboard route.

The service is the loopback fake service; the dashboard route runs in a thread on a loopback port;
the ProxyCommand runs as a subprocess with a fake ``session-manager-plugin`` script. No
network beyond loopback, no AWS.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import stat
import subprocess
import threading
from pathlib import Path
from unittest import mock

import pytest
from aiohttp import web
from managed_env_fake_bff import FakeBff, FakeTime
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_engine import FakeRegistry

from kiro_crew.managed_env import mint_and_attach as ma
from kiro_crew.managed_env.client import BffClient
from kiro_crew.managed_env.engine import SandboxLaunchEngine
from kiro_crew.managed_env.guard import SandboxGuard
from kiro_crew.managed_env.routes import SandboxRoutes, register_sandbox_routes

DASH_TOKEN = "DASH-OWNER-TOKEN-xyz"


class Dashboard:
    """The sandbox routes on a real loopback port, in a background event loop."""

    def __init__(self, routes: SandboxRoutes):
        self.routes = routes
        self.loop = asyncio.new_event_loop()
        self.url = ""
        ready = threading.Event()

        async def start():
            app = web.Application()
            register_sandbox_routes(app, routes)
            self.runner = web.AppRunner(app)
            await self.runner.setup()
            site = web.TCPSite(self.runner, "127.0.0.1", 0)
            await site.start()
            self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
            ready.set()

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(start())
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert ready.wait(10)

    def close(self):
        fut = asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop)
        fut.result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


@pytest.fixture
def world(tmp_path, monkeypatch, request):
    """A lane with its fake service, guard, engine and routes, the owner check allowed."""
    from kiro_crew.dashboard.handlers import _shared

    bff = FakeBff()
    ft = FakeTime()
    guard = SandboxGuard(tmp_path / "state", now=ft.now)
    client = BffClient(
        lambda: "bff-tok", guard, endpoint=bff.endpoint, sleep=ft.sleep, clock=ft.clock
    )
    engine = SandboxLaunchEngine(client=client, registry_factory=lambda: FakeRegistry())
    routes = SandboxRoutes(lambda: engine, run_in_thread=False)

    async def allow(request, operation):
        return None

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", allow)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
    dash = Dashboard(routes)
    sid = bff.add_sandbox("RUNNING", "kc-aaaaaa")
    guard.record_created(dict(bff.sandboxes[sid]), "kc-aaaaaa")
    yield bff, guard, dash, sid
    dash.close()
    bff.close()


def fake_plugin(tmp_path: Path) -> Path:
    """Records argv and the env var it was given (test-only: real plugin never writes them)."""
    out = tmp_path / "plugin-calls"
    out.mkdir(exist_ok=True)
    script = tmp_path / "session-manager-plugin"
    script.write_text(
        "#!/bin/sh\n"
        f"n=$(ls {out} | wc -l)\n"
        f"printf '%s\\n' \"$@\" > {out}/argv$n\n"
        f"printf '%s' \"$AWS_SSM_START_SESSION_RESPONSE\" > {out}/env$n\n"
        f'printf \'%s|%s\' "$KIROCREW_SBX_DASHBOARD" "$KIROCREW_SBX_DASHBOARD_TOKEN" > {out}/dash$n\n'
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def run_proxy(alias, dash_url, plugin, extra_env=None):
    """The ProxyCommand's ``main`` in this process, under the ssh's environment only, with the
    owner-credential mint stubbed (a test has no gateway secret to mint from). The plugin runs
    as a real subprocess where ``execvpe`` would replace the process."""
    env = {
        "PATH": "/usr/bin:/bin",
        "KIROCREW_SSM_PLUGIN": str(plugin),
        "KIROCREW_SBX_DASHBOARD": dash_url,
        # the per-test data home pin stays: the ProxyCommand inherits it from the gateway
        "KIROCREW_HOME": os.environ.get("KIROCREW_HOME"),
    }
    env.update(extra_env or {})
    env = {k: v for k, v in env.items() if v is not None}
    err = io.StringIO()
    rc = 0

    def execvpe(file, args, child_env):
        p = subprocess.run(
            [file, *args[1:]],
            env=child_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        raise SystemExit(p.returncode)

    with (
        mock.patch.dict(os.environ, env, clear=True),
        mock.patch.object(ma, "mint_owner_token", lambda base, e, run=None: DASH_TOKEN),
        contextlib.redirect_stderr(err),
    ):
        try:
            ma.main(["mint_and_attach", alias], execvpe=execvpe)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 0
    return subprocess.CompletedProcess([alias], rc, "", err.getvalue())


def calls(tmp_path, kind):
    d = tmp_path / "plugin-calls"
    return [(d / n).read_text() for n in sorted(os.listdir(d)) if n.startswith(kind)]


def test_two_ssh_invocations_mint_two_different_tuples(world, tmp_path):
    bff, guard, dash, sid = world
    plugin = fake_plugin(tmp_path)
    for _ in range(2):
        r = run_proxy(f"crew-sbx-{sid}", dash.url, plugin)
        assert r.returncode == 0, r.stderr
    assert bff.count("StartSshSession") == 2
    envs = [json.loads(e) for e in calls(tmp_path, "env")]
    assert envs[0]["SessionId"] != envs[1]["SessionId"]
    assert (
        envs[0]["StreamUrl"] != envs[1]["StreamUrl"]
        and envs[0]["TokenValue"] != envs[1]["TokenValue"]
    )
    # the plugin's argv names the env var, region and StartSession: no secret in argv
    for argv in calls(tmp_path, "argv"):
        assert argv.split() == [ma.PLUGIN_ENV_VAR, "zz-example-1", "StartSession"]
        assert "STREAMSECRET" not in argv and "MGSSECRET" not in argv
    # the dashboard token and URL are not handed on to the plugin
    assert all(d == "|" for d in calls(tmp_path, "dash"))
    assert bff.count("ResumeSandbox") == 0


def test_not_running_is_one_line_exit_255_and_never_resumes(world, tmp_path):
    bff, guard, dash, sid = world
    bff.sandboxes[sid]["status"] = "STOPPED"
    r = run_proxy(f"crew-sbx-{sid}", dash.url, fake_plugin(tmp_path))
    assert r.returncode == 255
    lines = r.stderr.strip().splitlines()
    assert len(lines) == 1 and lines[0] == f"mint-and-attach: {ma.TEXT_STOPPED}"
    assert ma.TEXT_STOPPED == "Stopped. Resume it, then Connect."
    assert bff.count("ResumeSandbox") == 0 and bff.count("StartSshSession") == 1
    assert not (tmp_path / "plugin-calls").exists() or not os.listdir(tmp_path / "plugin-calls")
    assert "SECRET" not in r.stderr and DASH_TOKEN not in r.stderr


@pytest.mark.parametrize(
    "alias",
    ["crew-sbx-", "crew-sess-abc", "somehost", "crew-sbx-../x", "crew-sbx-env-404"],
)
def test_bad_or_foreign_alias_is_refused_before_any_bff_call(world, tmp_path, alias):
    bff, guard, dash, sid = world
    r = run_proxy(alias, dash.url, fake_plugin(tmp_path))
    assert r.returncode == 255 and len(r.stderr.strip().splitlines()) == 1
    assert r.stderr.startswith("mint-and-attach:")
    assert bff.count("StartSshSession") == 0


def test_environment_problems_are_one_line_each(world, tmp_path):
    bff, guard, dash, sid = world
    plugin = fake_plugin(tmp_path)
    cases = [
        {"KIROCREW_SBX_DASHBOARD": None},
        {"KIROCREW_SBX_DASHBOARD": "http://example.invalid:80"},  # not loopback
        {"KIROCREW_SBX_DASHBOARD": "https://127.0.0.1:9"},  # not http
        {"KIROCREW_SBX_DASHBOARD": "http://127.0.0.1:1"},  # nothing listens
        {"KIROCREW_SSM_PLUGIN": str(tmp_path / "no-such-plugin")},
    ]
    for extra in cases:
        r = run_proxy(f"crew-sbx-{sid}", dash.url, plugin, extra)
        assert r.returncode == 255, extra
        lines = r.stderr.strip().splitlines()
        assert len(lines) == 1 and lines[0].startswith("mint-and-attach:"), (extra, r.stderr)
        assert DASH_TOKEN not in r.stderr and "SECRET" not in r.stderr


def test_a_401_style_refusal_from_the_dashboard_is_a_clean_line(world, tmp_path, monkeypatch):
    bff, guard, dash, sid = world
    bff.queue_error("any_500", op="StartSshSession")  # the service fails: route answers 424
    r = run_proxy(f"crew-sbx-{sid}", dash.url, fake_plugin(tmp_path))
    assert r.returncode == 255 and len(r.stderr.strip().splitlines()) == 1
    assert "SECRET" not in r.stderr


def test_the_ssh_session_route_refuses_a_foreign_id_with_no_bff_call(world):
    import urllib.error
    import urllib.request

    bff, guard, dash, sid = world
    foreign = bff.add_sandbox("RUNNING", "someone-else")
    req = urllib.request.Request(
        f"{dash.url}/api/managed-environments/{foreign}/ssh-session", data=b"", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=20)
    assert e.value.code == 404
    assert bff.count("StartSshSession") == 0


def test_plugin_env_strips_the_dashboard_credentials(monkeypatch):
    monkeypatch.setenv(ma.ENV_DASHBOARD, "http://127.0.0.1:1")
    args, env = ma.plugin_env_and_argv(
        {"ssmSessionId": "s", "streamUrl": "wss://x", "tokenValue": "v"}, "p", "r1"
    )
    assert args == ["p", ma.PLUGIN_ENV_VAR, "r1", "StartSession"]
    assert ma.ENV_DASHBOARD not in env
    assert json.loads(env[ma.PLUGIN_ENV_VAR]) == {
        "SessionId": "s",
        "StreamUrl": "wss://x",
        "TokenValue": "v",
    }


def test_the_ssh_session_route_needs_the_owner_and_the_flag(world, monkeypatch):
    import urllib.error
    import urllib.request

    from kiro_crew.dashboard.handlers import _shared

    bff, guard, dash, sid = world
    asked = []

    async def deny(request, operation):
        asked.append(operation)
        return web.json_response({"error": "owner only"}, status=403)

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", deny)

    def post():
        req = urllib.request.Request(
            f"{dash.url}/api/managed-environments/{sid}/ssh-session", data=b"", method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=20)
        return e.value.code

    assert post() == 403 and asked == ["sandboxes_ssh_session"]
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: False)
    assert post() == 403  # the owner check comes first, flag on or off

    async def allow(request, operation):
        asked.append(operation)

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", allow)
    assert post() == 404  # flag off: as if not mounted, once the owner check passed
    assert bff.count("StartSshSession") == 0


# ---- the ProxyCommand mints its own owner token (nothing is passed in the ssh env) ----


class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


class _Opener:
    def __init__(self):
        self.urls = []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        return _Resp(
            {
                "ssmSessionId": "s",
                "streamUrl": "wss://x",
                "tokenValue": "v",
                "region": "zz-example-1",
            }
        )


def test_no_env_token_mints_one_and_uses_it_against_the_bound_port():
    minted = []

    def source(base, env):
        minted.append(base)
        return "MINTED-TOK"

    op = _Opener()
    env = {ma.ENV_BOUND_PORT: "51234"}  # what the gateway exports to its children; no URL, no token
    tup = ma.mint_tuple("sb-1", env, opener=op, token_source=source)
    assert tup["ssmSessionId"] == "s"
    assert minted == ["http://127.0.0.1:51234"]
    assert op.urls == [
        "http://127.0.0.1:51234/api/managed-environments/sb-1/ssh-session?token=MINTED-TOK"
    ]


@pytest.mark.parametrize(
    "env",
    [
        {},
        {ma.ENV_BOUND_PORT: "0"},
        {ma.ENV_BOUND_PORT: "70000"},
        {ma.ENV_BOUND_PORT: "x"},
        {ma.ENV_DASHBOARD: "http://example.invalid:80"},
        {ma.ENV_DASHBOARD: "https://127.0.0.1:9"},
    ],
)
def test_url_unset_or_not_loopback_refuses_and_never_mints(env):
    def source(base, env):
        raise AssertionError("must not mint")

    op = _Opener()
    with pytest.raises(ma.MintError):
        ma.mint_tuple("sb-1", env, opener=op, token_source=source)
    assert op.urls == []


def test_mint_owner_token_runs_the_core_cli_and_leaks_nothing():
    seen = {}

    class R:
        returncode = 0
        stdout = "http://127.0.0.1:51234/?token=abc.def-123\n"
        stderr = ""

    def run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]
        return R()

    env = {ma.ENV_BOUND_PORT: "51234", "KIROCREW_HOME": "/h"}
    assert ma.mint_owner_token("http://127.0.0.1:51234", env, run=run) == "abc.def-123"
    assert seen["argv"][1:] == ["-m", "kiro_crew", "token", "--port", "51234"]
    assert seen["env"]["KIROCREW_HOME"] == "/h"

    class Bad(R):
        returncode = 1
        stdout = "secret-looking-output token=zzz"

    with pytest.raises(ma.MintError) as e:
        ma.mint_owner_token("http://127.0.0.1:51234", env, run=lambda a, **k: Bad())
    assert "zzz" not in str(e.value) and "secret-looking" not in str(e.value)

    class Empty(R):
        stdout = "no url here\n"

    with pytest.raises(ma.MintError):
        ma.mint_owner_token("http://127.0.0.1:51234", env, run=lambda a, **k: Empty())


# ---- plain connect lines -------------------------------------------------------


def _signed_out(engine):
    from kiro_crew.managed_env.engine import SandboxEngineError

    def no_token():
        raise SandboxEngineError("not_signed_in", "not signed in")

    engine.client._token = no_token


def test_no_sign_in_is_a_409_not_a_500_and_the_line_says_sign_in(world, tmp_path):
    bff, guard, dash, sid = world
    _signed_out(dash.routes.engine)
    status, body = asyncio.run_coroutine_threadsafe(_post(dash.url, sid), dash.loop).result(10)
    assert (status, body) == (409, {"error": "not signed in", "kind": "not_signed_in"})
    r = run_proxy(f"crew-sbx-{sid}", dash.url, fake_plugin(tmp_path))
    assert r.returncode == 255
    assert r.stderr.strip().splitlines() == [f"mint-and-attach: {ma.TEXT_SIGN_IN}"]
    assert ma.TEXT_SIGN_IN == "Sign in under Settings > Kiro account, then Connect."
    assert bff.count("StartSshSession") == 0


async def _post(url, sid):
    import aiohttp

    async with aiohttp.ClientSession() as s:
        async with s.post(f"{url}/api/managed-environments/{sid}/ssh-session") as r:
            return r.status, await r.json()


def test_another_engine_refusal_is_a_424_with_its_kind(world):
    from kiro_crew.managed_env.engine import SandboxEngineError

    bff, guard, dash, sid = world

    def broken():
        raise SandboxEngineError("vault_locked", "the token vault is locked")

    dash.routes.engine.client._token = broken
    status, body = asyncio.run_coroutine_threadsafe(_post(dash.url, sid), dash.loop).result(10)
    assert status == 424 and body["kind"] == "vault_locked"


def test_no_dashboard_and_no_alias_get_plain_lines(world, tmp_path):
    bff, guard, dash, sid = world
    plugin = fake_plugin(tmp_path)
    for url in (None, "http://127.0.0.1:1"):
        extra = {"KIROCREW_SBX_DASHBOARD": url}
        r = run_proxy(f"crew-sbx-{sid}", dash.url, plugin, extra)
        assert r.returncode == 255 and ma.TEXT_NO_DASHBOARD in r.stderr, (extra, r.stderr)
    r = run_proxy("somehost", dash.url, plugin)
    assert r.returncode == 255 and ma.TEXT_NO_ALIAS in r.stderr
    assert ma.TEXT_NO_DASHBOARD == "Can't reach this Crew dashboard. Is it running?"
    assert ma.TEXT_NO_ALIAS == (
        "This crew's connection settings are missing. Connect it again from Your crews."
    )
    assert bff.count("StartSshSession") == 0


def _policy_403(bff):
    # the recorded answer (bff-calls.log line 2513: __type and message); its message is never kept
    body = {"__type": "com.example.kiro#ForbiddenException", "message": "x"}
    bff.queue("StartSshSession", 403, body)


def _plain_policy_line(r) -> str:
    from kiro_crew.managed_env.guard import POLICY_TEXT

    assert r.returncode == 255
    lines = r.stderr.strip().splitlines()
    assert lines == [f"mint-and-attach: {POLICY_TEXT}"], lines
    assert "StartSshSession" not in r.stderr and "HTTP" not in r.stderr and "424" not in r.stderr
    return lines[0]


def test_r11_the_policy_403_is_the_plain_line_alone(world, tmp_path):
    bff, guard, dash, sid = world
    _policy_403(bff)
    status, body = asyncio.run_coroutine_threadsafe(_post(dash.url, sid), dash.loop).result(10)
    assert status == 424 and body["kind"] == "policy_denied"
    _policy_403(bff)
    guard.clear_policy(sid)  # the route's ask above set the hold; this row is the 424's own
    _plain_policy_line(run_proxy(f"crew-sbx-{sid}", dash.url, fake_plugin(tmp_path)))
    assert bff.count("StartSshSession") == 2 and not guard.stopped
