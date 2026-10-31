"""The enterprise sign-in-only gateway serves sign-in and nothing else.

Design: docs/request-for-change/rfc-crew-enterprise-edition.md, "Enterprise gate".
A signed-out enterprise start serves a 127.0.0.1 app holding only the sign-in
screen and the sign-in API, then boots again once a sign-in lands.
"""

from __future__ import annotations

import asyncio
import re
import socket
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest
from aiohttp import web

from kiro_crew import enterprise_boot
from kiro_crew import enterprise_sign_in as sign_in
from kiro_crew import enterprise_signin_gateway as signin
from kiro_crew.dashboard import routes as routes_pkg
from kiro_crew.dashboard.token_auth_tokens import generate_token
from kiro_crew.platform import enterprise_policy_fetch as epf
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.testing.wait import await_until

_NO_SIGN_IN = "no Kiro sign-in was found"
_SIGNED_OUT = PlatformCompositionError(f"policy could not be loaded: {_NO_SIGN_IN}")


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    monkeypatch.delenv(signin.DESKTOP_GATEWAY_ENV, raising=False)


@pytest.fixture
def signed_out(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """The rules fetch recorded "no sign-in"; ``signed_in()`` answers the last value here."""
    monkeypatch.setattr(epf, "_last_failure", ("", ""))
    epf._note_cause(sign_in.CAUSE_NO_SIGN_IN, _NO_SIGN_IN)
    answers = [False]
    monkeypatch.setattr(signin, "signed_in", lambda: answers[-1])
    return answers


class _Client:
    """The sign-in app served on a socket held from bind to close, so no port race."""

    def __init__(self, runner: web.AppRunner, session: aiohttp.ClientSession) -> None:
        self._runner = runner
        self._session = session

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._session.request(method, path, **kwargs)

    def get(self, path: str, **kwargs: Any) -> Any:
        return self._session.get(path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self._session.post(path, **kwargs)

    async def close(self) -> None:
        await self._session.close()
        await self._runner.cleanup()


async def _client() -> tuple[_Client, int]:
    """The sign-in app on a port-0 socket that stays bound until the client closes."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    runner = web.AppRunner(await signin.build_app(port, ""))
    await runner.setup()
    await web.SockSite(runner, sock).start()
    session = aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}")
    return _Client(runner, session), port


def _owner_cookie(port: int) -> dict[str, str]:
    return {f"mc_token_{port}": generate_token("local-startup", ttl_seconds=3600)}


def _dashboard_route_table() -> list[tuple[str, str]]:
    """Every (method, path) the full dashboard registers, MCP routes included."""
    from kiro_crew.dashboard import server

    app = web.Application()
    routes_pkg.register_all(app)
    server._register_mcp_routes(app)
    return [
        (route.method, route.resource.canonical)
        for route in app.router.routes()
        if route.method not in ("HEAD", "*")
    ]


def _sample_path(canonical: str) -> str:
    return re.sub(r"\{[^}]*\}", "x", canonical)


# -- when the sign-in screen applies ------------------------------------------


def test_oss_never_serves_the_sign_in_screen(signed_out: list[bool]) -> None:
    assert not signin.applies(_SIGNED_OUT)


def test_signed_out_enterprise_start_gets_the_sign_in_screen(
    enterprise: None, signed_out: list[bool]
) -> None:
    assert signin.applies(_SIGNED_OUT)


def test_a_signed_in_failure_still_exits(enterprise: None, signed_out: list[bool]) -> None:
    signed_out.append(True)
    assert not signin.applies(PlatformCompositionError("rules unreachable"))


def test_a_failure_that_is_not_the_missing_sign_in_still_exits(
    enterprise: None, signed_out: list[bool]
) -> None:
    # Signed out, but boot failed earlier for another reason (unreadable plugin
    # metadata, say): signing in cannot fix it, so it exits as before.
    assert not signin.applies(PlatformCompositionError("plugin metadata unreadable"))


def test_other_errors_sandboxed_children_and_desktop_still_exit(
    enterprise: None, signed_out: list[bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert not signin.applies(ValueError("x"))
    monkeypatch.setenv(signin.DESKTOP_GATEWAY_ENV, "1")
    assert not signin.applies(_SIGNED_OUT)
    monkeypatch.delenv(signin.DESKTOP_GATEWAY_ENV)
    monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
    assert not signin.applies(_SIGNED_OUT)


def test_an_unreadable_vault_counts_as_signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Any:
        raise OSError("unreadable")

    monkeypatch.setattr("kiro_crew.auth.bridge.default_token_store", broken)
    assert signin.signed_in()


def test_only_an_empty_vault_reads_as_signed_out(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.auth.store import TokenStore

    store = TokenStore(tmp_path)
    monkeypatch.setattr("kiro_crew.auth.bridge.default_token_store", lambda: store)
    assert not signin.signed_in()
    # A vault file that cannot be read is not proof that nobody is signed in.
    store._vault._store_path.parent.mkdir(parents=True, exist_ok=True)
    store._vault._store_path.write_bytes(b"not an encrypted vault")
    assert signin.signed_in()


# -- what the sign-in app exposes -----------------------------------------------


def test_the_app_registers_only_allow_listed_routes() -> None:
    # Only the route table is read; nothing binds, so any port number will do.
    app = asyncio.run(signin.build_app(5476, ""))
    for route in app.router.routes():
        path = _sample_path(route.resource.canonical)
        assert signin.allowed(route.method, path), (route.method, route.resource.canonical)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_owner_token", [False, True])
async def test_every_dashboard_route_but_the_allow_list_is_refused(with_owner_token: bool) -> None:
    table = _dashboard_route_table()
    assert len(table) > 300  # the walk saw the real tables, not an empty app
    client, port = await _client()
    cookies = _owner_cookie(port) if with_owner_token else {}
    origin = {"Origin": f"http://127.0.0.1:{port}"}
    try:
        for method, canonical in table:
            path = _sample_path(canonical)
            if signin.allowed(method, path):
                continue
            resp = await client.request(method, path, cookies=cookies, headers=origin)
            assert resp.status in (403, 404), (method, canonical, resp.status)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_websocket_upgrade_on_an_allowed_path_is_refused() -> None:
    client, _port = await _client()
    try:
        resp = await client.get(
            "/api/health", headers={"Upgrade": "websocket", "Connection": "Upgrade"}
        )
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_sign_in_api_needs_the_owner_token() -> None:
    client, port = await _client()
    try:
        assert (await client.get("/api/health")).status == 200
        assert (await client.get("/api/kas-login")).status in (401, 403)
        assert (await client.get("/api/kiro-prerequisite")).status in (401, 403)
        resp = await client.get("/api/kiro-prerequisite", cookies=_owner_cookie(port))
        assert resp.status == 200
        body = await resp.json()
        assert body["enterprise_sign_in_only"] is True
        assert body["enterprise_login_required"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_only_an_organisation_sign_in_can_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.dashboard.handlers import kas_login

    started: list[str] = []

    async def begin(request: web.Request) -> web.Response:
        started.append((await request.json())["provider"])
        return web.json_response({"ok": True})

    monkeypatch.setattr(kas_login, "api_kas_login_begin_device", begin)
    client, port = await _client()
    owner = {"cookies": _owner_cookie(port), "headers": {"Origin": f"http://127.0.0.1:{port}"}}
    try:
        for provider in ("builder_id", "google", "github", ""):
            resp = await client.post("/api/kas-login/device", json={"provider": provider}, **owner)
            assert resp.status == 400, provider
            assert (await resp.json())["code"] == "organisation_sign_in_required"
        resp = await client.post(
            "/api/kas-login/device",
            json={"provider": "idc", "start_url": "https://x.awsapps.com/start"},
            **owner,
        )
        assert resp.status == 200
        assert started == ["idc"]
        resp = await client.post("/api/kas-login/loopback", json={"provider": "google"}, **owner)
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_cross_site_sign_in_post_is_refused() -> None:
    client, port = await _client()
    try:
        resp = await client.post(
            "/api/kas-login/device",
            json={"provider": "idc"},
            cookies=_owner_cookie(port),
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status == 403
    finally:
        await client.close()


# -- serving, then exiting -------------------------------------------------------


def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(signin, "_WATCH_INTERVAL_SECS", 0.0)
    monkeypatch.setattr(signin, "_DRAIN_SECS", 0.0)
    monkeypatch.setattr("kiro_crew.frontend.ensure_dev_dist_symlink", lambda: None)
    # The sign-in that lands is one the next start can use.
    monkeypatch.setattr(sign_in, "resolve", lambda **_kw: None)


@pytest.mark.asyncio
async def test_an_unusable_sign_in_is_never_published_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast(monkeypatch)
    monkeypatch.setattr(signin, "signed_in", lambda: True)

    def not_eligible(**_kw: Any) -> None:
        raise sign_in.SignInError("no organisation profile", sign_in.CAUSE_NOT_ELIGIBLE)

    monkeypatch.setattr(sign_in, "resolve", not_eligible)
    apps: list[web.Application] = []
    real_build = signin.build_app

    async def build(port: int, owner_id: str) -> web.Application:
        apps.append(await real_build(port, owner_id))
        return apps[-1]

    monkeypatch.setattr(signin, "build_app", build)
    task = asyncio.ensure_future(signin.serve_until_signed_in(0, "", no_open=True))
    with pytest.raises(sign_in.SignInError):
        await await_until(task, what="the app to refuse the unusable sign-in")
    # The page was never told "signed in", and the port is free again.
    assert not apps[0][signin._SIGNED_IN_KEY].is_set()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", apps[0]["port"]))


@pytest.mark.asyncio
async def test_serves_on_loopback_only_until_a_sign_in_lands_then_frees_the_port(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fast(monkeypatch)
    monkeypatch.setattr(signin, "_SEEN_WAIT_SECS", 0.0)
    bound: list[Any] = []
    real_start = web.SockSite.start

    async def start(self: web.SockSite) -> None:
        bound.append(self._sock.getsockname())
        await real_start(self)

    monkeypatch.setattr(web.SockSite, "start", start)
    # The watch reads the sign-in only once the app is listening: signed out on
    # the first read, signed in on the second.
    reads: list[int] = []

    def read() -> bool:
        reads.append(1)
        return len(reads) > 1

    monkeypatch.setattr(signin, "signed_in", read)
    task = asyncio.ensure_future(signin.serve_until_signed_in(0, "", no_open=True))
    port = await await_until(task, what="the sign-in app to see the sign-in")
    assert reads == [1, 1]
    assert bound == [("127.0.0.1", port)]
    # The printed link names the bound address, never a name that may resolve to ::1.
    out = capsys.readouterr().out
    prefix = f"http://127.0.0.1:{port}?token="
    assert prefix in out
    with socket.socket() as sock:  # the sign-in app closed its socket
        sock.bind(("127.0.0.1", port))
    # The link is bound to this process: it works while the process (its boot id)
    # lives, and fails under any other. Other sessions are untouched.
    from urllib.parse import unquote

    from kiro_crew.dashboard import boot_id
    from kiro_crew.dashboard.token_auth_tokens import validate_token

    link = unquote(out.split(prefix, 1)[1].split()[0])
    other = _owner_cookie(5476)["mc_token_5476"]
    assert validate_token(link, use_session_exp=True)[0]
    monkeypatch.setattr(boot_id, "_boot_id", "a-later-process")
    assert not validate_token(link, use_session_exp=True)[0]
    assert validate_token(other, use_session_exp=True)[0]


@pytest.mark.asyncio
async def test_the_page_is_told_to_start_again_before_the_app_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast(monkeypatch)
    # Long enough that only the page's read can end the wait within this test.
    monkeypatch.setattr(signin, "_SEEN_WAIT_SECS", 600.0)
    monkeypatch.setattr(signin, "signed_in", lambda: True)
    apps: list[web.Application] = []
    real_build = signin.build_app

    async def build(port: int, owner_id: str) -> web.Application:
        apps.append(await real_build(port, owner_id))
        return apps[-1]

    monkeypatch.setattr(signin, "build_app", build)
    task = asyncio.ensure_future(signin.serve_until_signed_in(0, "", no_open=True))
    # The events exist once the app is built; the sign-in is seen on the first read.
    while not apps:
        await asyncio.sleep(0)  # flake-ok: yields to the app task until it has built the app
    await await_until(apps[0][signin._SIGNED_IN_KEY], what="the sign-in to be seen")
    port = apps[0]["port"]
    async with aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}") as http:
        resp = await http.get("/api/kiro-prerequisite", cookies=_owner_cookie(port))
        body = await resp.json()
        assert body["enterprise_signed_in_restart"] is True
        assert body["enterprise_start_command"] == signin.start_command()
        assert body["enterprise_start_command"].endswith("gateway")
    # That read ends the wait: the app stops and returns at once.
    assert await await_until(task, what="the app to stop after the page read") == port


def _no_serve(monkeypatch: pytest.MonkeyPatch, port: int = 4242) -> list[int]:
    """A stand-in sign-in app that 'signs in' at once."""
    served: list[int] = []

    async def serve(*_args: Any, **_kwargs: Any) -> int:
        served.append(port)
        return port

    serve.served = served  # type: ignore[attr-defined]
    monkeypatch.setattr(signin, "serve_until_signed_in", serve)
    return served


def _usable_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sign_in, "resolve", lambda **_kw: None)


def test_after_sign_in_it_exits_0_naming_the_start_command(
    enterprise: None,
    signed_out: list[bool],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_serve(monkeypatch)
    _usable_sign_in(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        signin.sign_in_then_exit(_SIGNED_OUT, port=0, owner_id="", no_open=True)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Signed in. Start Kiro Crew again:" in out
    assert "gateway" in out.splitlines()[-1]


def test_an_unusable_sign_in_exits_with_its_cause_not_success(
    enterprise: None,
    signed_out: list[bool],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def lapsed(*_args: Any, **_kwargs: Any) -> int:
        raise sign_in.SignInError("the sign-in has expired", sign_in.CAUSE_SIGN_IN_UNUSABLE)

    monkeypatch.setattr(signin, "serve_until_signed_in", lapsed)
    with pytest.raises(SystemExit) as exc:
        signin.sign_in_then_exit(_SIGNED_OUT, port=0, owner_id="", no_open=True)
    assert exc.value.code == enterprise_boot.EXIT_CODE
    captured = capsys.readouterr()
    assert "the sign-in has expired" in captured.err
    assert "Signed in." not in captured.out


def test_a_busy_port_exits(
    enterprise: None,
    signed_out: list[bool],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("kiro_crew.frontend.ensure_dev_dist_symlink", lambda: None)
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        with pytest.raises(SystemExit) as exc:
            signin.sign_in_then_exit(
                _SIGNED_OUT, port=held.getsockname()[1], owner_id="", no_open=True
            )
    assert exc.value.code == enterprise_boot.EXIT_CODE
    assert "sign-in screen could not open" in capsys.readouterr().err


def _cli_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, *argv: str
) -> tuple[Any, list[int], MagicMock]:
    """Run ``kirocrew gateway <argv>`` with a first boot that finds no sign-in."""
    from kiro_crew import cli, cli_server

    boots: list[int] = []

    def boot(_cfg: Any) -> None:
        boots.append(1)
        raise _SIGNED_OUT

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "boot_platform", boot)
    monkeypatch.setattr("kiro_crew.platform.boot_platform", boot)
    _no_serve(monkeypatch)
    monkeypatch.setattr(cli, "maybe_reexec", lambda _argv: None)
    monkeypatch.setattr(
        cli.platform_compat, "limit_malloc_arenas", cli.platform_compat.MallocArenaCap
    )
    monkeypatch.setattr(cli, "_setup_cli_logging", lambda *_args: None)
    _usable_sign_in(monkeypatch)
    gateway = MagicMock(side_effect=AssertionError("no gateway starts in the sign-in process"))
    monkeypatch.setattr(cli_server, "_gateway", gateway)
    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "gateway", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    return exc.value, boots, gateway


def test_cli_signed_out_signs_in_then_exits_0_without_starting_a_gateway(
    enterprise: None, signed_out: list[bool], monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    exit_, boots, gateway = _cli_gateway(monkeypatch, tmp_path, "--no-open")
    assert exit_.code == 0
    # One boot (the signed-out one); the rules are not loaded again here.
    assert boots == [1]
    gateway.assert_not_called()


def test_cli_the_sign_in_screen_takes_the_gateway_lock(
    enterprise: None,
    signed_out: list[bool],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from kiro_crew.config import config_dir
    from kiro_crew.gateway_lock import GatewayLock

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    held = GatewayLock(config_dir()).acquire()
    try:
        exit_, boots, gateway = _cli_gateway(monkeypatch, tmp_path, "--no-open")
    finally:
        held.release()
    # Another holder of this data home's lock: no sign-in screen is served.
    assert exit_.code != 0
    assert signin.serve_until_signed_in.served == []  # type: ignore[attr-defined]
    gateway.assert_not_called()


@pytest.mark.parametrize("flag", ["--test-mode", "--json-ready"])
def test_cli_test_and_scripted_starts_still_exit(
    enterprise: None,
    signed_out: list[bool],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    flag: str,
) -> None:
    exit_, boots, gateway = _cli_gateway(monkeypatch, tmp_path, flag)
    assert exit_.code == enterprise_boot.EXIT_CODE
    assert boots == [1]
    gateway.assert_not_called()


@pytest.mark.parametrize("port", ["abc", "99999"])
def test_cli_a_bad_port_exits_with_the_usual_message(
    enterprise: None,
    signed_out: list[bool],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
    port: str,
) -> None:
    exit_, boots, gateway = _cli_gateway(monkeypatch, tmp_path, "--port", port)
    assert exit_.code == 2
    assert "--port" in capsys.readouterr().err
    assert boots == [1]
    gateway.assert_not_called()


@pytest.mark.parametrize(
    ("env", "expect_no_open"),
    [
        ({"DISPLAY": ":0"}, False),
        ({"SSH_CONNECTION": "10.0.0.1 1 10.0.0.2 22"}, True),
    ],
)
def test_cli_opens_a_browser_only_where_the_gateway_would(
    enterprise: None,
    signed_out: list[bool],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    env: dict[str, str],
    expect_no_open: bool,
) -> None:
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("kiro_crew.cli.sys.platform", "linux")
    seen: dict[str, Any] = {}

    def sign_in_then_exit(_exc: BaseException, **kwargs: Any) -> None:
        seen.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(signin, "sign_in_then_exit", sign_in_then_exit)
    _cli_gateway(monkeypatch, tmp_path)
    assert seen["no_open"] is expect_no_open


def test_ctrl_c_at_the_sign_in_screen_exits_cleanly(
    enterprise: None,
    signed_out: list[bool],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def interrupted(*_args: Any, **_kwargs: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(signin, "serve_until_signed_in", interrupted)
    with pytest.raises(SystemExit) as exc:
        signin.sign_in_then_exit(_SIGNED_OUT, port=0, owner_id="", no_open=True)
    assert exc.value.code == 130
    assert exc.value.__cause__ is None
    assert "Stopped before sign-in" in capsys.readouterr().err


def test_cli_the_restart_command_repeats_the_launch_options(
    enterprise: None,
    signed_out: list[bool],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    seen: dict[str, Any] = {}

    def sign_in_then_exit(_exc: BaseException, **kwargs: Any) -> None:
        seen.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(signin, "sign_in_then_exit", sign_in_then_exit)
    _cli_gateway(monkeypatch, tmp_path, "--port", "4242", "--no-crons", "--no-open")
    assert seen["start_args"] == ("--no-crons", "--no-open", "--port", "4242")
    assert signin.start_command(seen["start_args"]).endswith(
        "gateway --no-crons --no-open --port 4242"
    )


def test_the_restart_command_leaves_out_one_shot_options() -> None:
    import argparse

    from kiro_crew import cli

    args = argparse.Namespace(
        no_tunnel=True, seed="demo", seed_replace=True, approval="reads", port=None
    )
    assert cli._restart_args(args) == ("--no-tunnel", "--approval", "reads")


def test_the_restart_command_names_the_data_home_it_signed_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sign_in, "_launcher", lambda: "kirocrew")
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    assert signin.start_command(platform="linux") == "kirocrew gateway"
    monkeypatch.setenv("KIROCREW_HOME", "/tmp/crew home")
    assert (
        signin.start_command(("--no-crons",), platform="linux")
        == "KIROCREW_HOME='/tmp/crew home' kirocrew gateway --no-crons"
    )
    assert (
        signin.start_command(platform="win32")
        == "$env:KIROCREW_HOME = '/tmp/crew home'; & kirocrew gateway"
    )
