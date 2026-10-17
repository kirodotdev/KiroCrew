"""The gateway gate in front of the desktop shell's external-terminal launch.

Each refusal of ``POST /api/terminal/external`` is pinned with the one check it
exercises left failing and every other check passing, so dropping any single
check turns its test red.
"""

from __future__ import annotations

import pathlib
import re
import time
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import external_terminal as ext
from kiro_crew.dashboard.handlers import terminal

REPO = pathlib.Path(__file__).resolve().parents[1]
OWNER = "owner-user"


@pytest.fixture(autouse=True)
def _isolate(_floor_monkeypatch):
    ext.clear_grants()
    _floor_monkeypatch.setattr(terminal, "_enabled_cache", [True, time.monotonic()])
    yield
    ext.clear_grants()


def _app(monkeypatch, *, cfg=None, owner_id=OWNER, enabled=True):
    """The two routes behind a fake auth layer.

    ``X-Test-User`` names the caller (absent = the owner), ``X-Test-NoUser``
    makes the request unauthenticated, and ``X-Test-Internal`` stands for the
    middleware's verified internal-secret match.
    """
    config = {"enabled": enabled, "external_app": "kitty"} if cfg is None else cfg
    monkeypatch.setattr(terminal, "_get_config", lambda _request: config)
    monkeypatch.setattr(terminal, "_enabled_cache", [enabled, time.monotonic()])
    state = MagicMock()
    state.owner_id = owner_id

    @web.middleware
    async def fake_auth(request, handler):
        if "X-Test-NoUser" not in request.headers:
            request["user"] = request.headers.get("X-Test-User", OWNER)
        request["app"] = ""
        if "X-Test-Internal" in request.headers:
            request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[fake_auth])
    app["state"] = state
    app.router.add_post("/api/terminal/external", ext.api_terminal_external)
    app.router.add_post("/api/terminal/external-grant", ext.api_terminal_external_grant)
    return app


async def _open(client, cwd, headers=None):
    return await client.post("/api/terminal/external", json={"cwd": cwd}, headers=headers or {})


async def _redeem(client, grant, headers=None):
    h = {"X-Test-Internal": "1"}
    h.update(headers or {})
    return await client.post("/api/terminal/external-grant", json={"grant": grant}, headers=h)


# ── the approval: every refusal ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_checks_pass_mints_a_grant_for_the_resolved_directory(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    link = tmp_path / "link"
    link.symlink_to(project)
    async with TestClient(TestServer(_app(monkeypatch))) as client:
        resp = await _open(client, str(link))
        assert resp.status == 200
        body = await resp.json()
        assert body["app"] == "kitty"
        assert re.fullmatch(r"[A-Za-z0-9_-]{32,128}", body["grant"])
        assert "cwd" not in body, "the directory goes to the shell through the grant only"
        redeemed = await _redeem(client, body["grant"])
        assert redeemed.status == 200
        assert await redeemed.json() == {"app": "kitty", "cwd": str(project.resolve())}


@pytest.mark.asyncio
async def test_refused_when_unauthenticated(monkeypatch, tmp_path):
    async with TestClient(TestServer(_app(monkeypatch))) as client:
        resp = await _open(client, str(tmp_path), {"X-Test-NoUser": "1"})
        assert resp.status == 401
    assert ext._grants == {}


@pytest.mark.asyncio
async def test_refused_for_a_caller_who_is_not_the_dashboard_owner(monkeypatch, tmp_path):
    async with TestClient(TestServer(_app(monkeypatch))) as client:
        resp = await _open(client, str(tmp_path), {"X-Test-User": "someone-else"})
        assert resp.status == 403
        assert (await resp.json())["code"] == "owner_only"
    assert ext._grants == {}


@pytest.mark.asyncio
async def test_refused_when_the_integrated_terminal_is_disabled(monkeypatch, tmp_path):
    app = _app(monkeypatch, cfg={"enabled": False, "external_app": "kitty"}, enabled=False)
    async with TestClient(TestServer(app)) as client:
        resp = await _open(client, str(tmp_path))
        assert resp.status == 403
        assert (await resp.json())["code"] == "terminal_disabled"
    assert ext._grants == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    ["X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded", "X-Real-IP"],
)
async def test_refused_when_the_request_is_not_direct_local(monkeypatch, tmp_path, header):
    async with TestClient(TestServer(_app(monkeypatch))) as client:
        resp = await _open(client, str(tmp_path), {header: "203.0.113.9"})
        assert resp.status == 403
        assert (await resp.json())["code"] == "external_terminal_not_local"
    assert ext._grants == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cfg",
    [
        {"enabled": True},
        {"enabled": True, "external_app": ""},
        {"enabled": True, "external_app": "/usr/bin/xterm"},
        {"enabled": True, "external_app": "bash"},
        {"enabled": True, "external_app": ["kitty"]},
        {"enabled": True, "external_app": None},
    ],
    ids=["absent", "empty", "a-path", "unknown-id", "a-list", "null"],
)
async def test_refused_until_an_allowlisted_app_is_picked(monkeypatch, tmp_path, cfg):
    async with TestClient(TestServer(_app(monkeypatch, cfg=cfg))) as client:
        resp = await _open(client, str(tmp_path))
        assert resp.status == 409
        assert (await resp.json())["code"] == "external_terminal_not_configured"
    assert ext._grants == {}


@pytest.mark.asyncio
async def test_refused_for_a_project_path_that_is_not_an_existing_directory(monkeypatch, tmp_path):
    a_file = tmp_path / "payload.command"
    a_file.write_text("echo hi\n", encoding="utf-8")
    swapped = tmp_path / "swapped"
    swapped.symlink_to(a_file)
    async with TestClient(TestServer(_app(monkeypatch))) as client:
        for cwd in [str(a_file), str(swapped), str(tmp_path / "missing"), "relative/dir", "", 7]:
            resp = await client.post("/api/terminal/external", json={"cwd": cwd})
            assert resp.status == 400, cwd
            assert (await resp.json())["code"] == "external_terminal_bad_cwd"
        resp = await client.post("/api/terminal/external", data=b"not json")
        assert resp.status == 400
    assert ext._grants == {}


def test_the_request_body_never_names_the_program():
    # Only `cwd` is read from the body; the app comes from config alone.
    source = (REPO / "src/kiro_crew/dashboard/handlers/external_terminal.py").read_text(
        encoding="utf-8"
    )
    reads = set(re.findall(r'body\.get\("([a-z_]+)"\)', source))
    assert reads == {"cwd", "grant"}
    for banned in (
        "subprocess",
        "create_subprocess",
        "os.system",
        "Popen",
        "xdg-open",
        "osascript",
    ):
        assert banned not in source, f"the gate must not launch anything itself ({banned})"


# ── the redemption ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_grant_is_honoured_once(monkeypatch, tmp_path):
    async with TestClient(TestServer(_app(monkeypatch))) as client:
        grant = (await (await _open(client, str(tmp_path))).json())["grant"]
        assert (await _redeem(client, grant)).status == 200
        again = await _redeem(client, grant)
        assert again.status == 404
        assert (await again.json())["code"] == "external_terminal_no_grant"


@pytest.mark.asyncio
async def test_redemption_requires_the_internal_secret(monkeypatch, tmp_path):
    async with TestClient(TestServer(_app(monkeypatch))) as client:
        grant = (await (await _open(client, str(tmp_path))).json())["grant"]
        # A browser holding only its dashboard cookie cannot redeem.
        resp = await client.post("/api/terminal/external-grant", json={"grant": grant})
        assert resp.status == 403
        assert (await resp.json())["code"] == "internal_secret_required"
        # The refusal does not spend the grant.
        assert (await _redeem(client, grant)).status == 200


@pytest.mark.asyncio
async def test_an_unknown_or_expired_grant_is_refused(monkeypatch, tmp_path):
    async with TestClient(TestServer(_app(monkeypatch))) as client:
        for grant in ["x" * 43, "", None, 5]:
            resp = await client.post(
                "/api/terminal/external-grant",
                json={"grant": grant},
                headers={"X-Test-Internal": "1"},
            )
            assert resp.status == 404
        grant = (await (await _open(client, str(tmp_path))).json())["grant"]
        app, cwd, _exp = ext._grants[grant]
        ext._grants[grant] = (app, cwd, time.monotonic() - 1)
        assert (await _redeem(client, grant)).status == 404


@pytest.mark.asyncio
async def test_a_grant_lapses_when_the_pick_changes_before_redemption(monkeypatch, tmp_path):
    cfg = {"enabled": True, "external_app": "kitty"}
    async with TestClient(TestServer(_app(monkeypatch, cfg=cfg))) as client:
        grant = (await (await _open(client, str(tmp_path))).json())["grant"]
        cfg["external_app"] = "wezterm"
        resp = await _redeem(client, grant)
        assert resp.status == 409
        assert (await resp.json())["code"] == "external_terminal_not_configured"


def test_outstanding_grants_are_bounded():
    for i in range(ext._MAX_GRANTS + 5):
        ext._mint_grant("kitty", f"/p/{i}")
    assert len(ext._grants) == ext._MAX_GRANTS


@pytest.mark.asyncio
async def test_every_redemption_outcome_is_audited_by_app_only(monkeypatch, tmp_path):
    # Each refusal and the success of the redemption route writes one SEL event,
    # naming the refusal code or the app and never the directory.
    sel = MagicMock()
    monkeypatch.setattr(terminal, "_sel", lambda: sel)

    def redeems():
        return [
            (c.kwargs["outcome"], c.kwargs["resources"])
            for c in sel.log_api_access.call_args_list
            if c.kwargs["operation"] == "terminal.external.redeem"
        ]

    cfg = {"enabled": True, "external_app": "kitty"}
    async with TestClient(TestServer(_app(monkeypatch, cfg=cfg))) as client:
        grant = (await (await _open(client, str(tmp_path))).json())["grant"]
        assert (
            await client.post("/api/terminal/external-grant", json={"grant": grant})
        ).status == 403
        assert (await _redeem(client, "x" * 43)).status == 404
        assert (await _redeem(client, grant)).status == 200
        lapsed = (await (await _open(client, str(tmp_path))).json())["grant"]
        cfg["external_app"] = "wezterm"
        assert (await _redeem(client, lapsed)).status == 409
    assert redeems() == [
        ("denied", "internal_secret_required"),
        ("denied", "external_terminal_no_grant"),
        ("ok", "app=kitty"),
        ("denied", "external_terminal_not_configured"),
    ]
    for c in sel.log_api_access.call_args_list:
        assert str(tmp_path) not in repr(c), "the audit names the app only, never the directory"


# ── config and the desktop allowlist ──────────────────────────────────────


def test_the_gateway_allowlist_equals_the_desktop_allowlist():
    js = (REPO / "website/electron/external-terminal.js").read_text(encoding="utf-8")
    desktop = set(re.findall(r'^\s*id: "([a-z0-9-]+)",$', js, flags=re.M))
    assert desktop == set(ext.EXTERNAL_TERMINAL_APPS)


def test_the_setting_is_declared_unset_by_default_with_the_allowlist_as_its_choices():
    from kiro_crew.config.schema import SCHEMA_REGISTRY

    entry = next(e for e in SCHEMA_REGISTRY if e.path == "dashboard.terminal.external_app")
    assert entry.default_value == ""
    assert set(entry.enum_values) == {""} | set(ext.EXTERNAL_TERMINAL_APPS)


def test_the_dashboard_may_set_only_an_allowlisted_app():
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    spec = _EDITABLE_CONFIG["dashboard.terminal.external_app"]
    assert spec["type"] == "enum"
    assert set(spec["values"]) == {""} | set(ext.EXTERNAL_TERMINAL_APPS)


def test_the_redemption_route_is_machine_only_and_both_routes_are_registered():
    from kiro_crew.dashboard import server

    assert "/api/terminal/external-grant" in server._STRICT_INTERNAL_API_PATHS
    assert "/api/terminal/external" not in server._STRICT_INTERNAL_API_PATHS
    routes = (REPO / "src/kiro_crew/dashboard/routes/connections.py").read_text(encoding="utf-8")
    for path, handler in [
        ("/api/terminal/external", "api_terminal_external"),
        ("/api/terminal/external-grant", "api_terminal_external_grant"),
    ]:
        pattern = rf'add_post\(\s*"{re.escape(path)}",\s*external_terminal\.{handler}\s*\)'
        assert re.search(pattern, routes), path
