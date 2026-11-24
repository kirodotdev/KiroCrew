"""The panel's two routes and its socket leg: who is refused, and by what.

Three layers, each with the real code of that layer:

* the handlers behind a stand-in identity (the way ``test_browser_live_routes``
  does), for the owner gate and the hub's own refusals;
* the handlers behind the REAL sign-in gate and the REAL origin check, for the
  denials that must come from those two and from nothing a panel added;
* the modules themselves, for what an agent can reach: nothing.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from browser_panel_helpers import MAIN_FRAME, WORLD, Client, make_hub, open_live

import kiro_crew
from kiro_crew.browser_harness import panel_browser as pb
from kiro_crew.browser_harness import panel_hub as ph
from kiro_crew.browser_harness import panel_page
from kiro_crew.browser_harness import takeover as ph_takeover
from kiro_crew.browser_harness import url_gate
from kiro_crew.browser_harness.relay import CdpRelay, RelayClosed
from kiro_crew.dashboard import revocation_gen, server_internal_paths, token_auth
from kiro_crew.dashboard.handlers import browser_live, browser_panels, browser_policy
from kiro_crew.dashboard.routes import browser_live as route_slice
from kiro_crew.dashboard.server import _make_csrf_middleware

pytestmark = pytest.mark.asyncio

INPUT, ASK = "/api/browser/panel/input", "/api/browser/panel/ask"
ROUTES = (INPUT, ASK)
TOKEN_A, TOKEN_B = "owner-session-token-A", "owner-session-token-B"
MOVE = {"type": "mouse", "action": "move", "x": 10, "y": 10}
SECRET = "the-agents-own-secret"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        browser_live, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
    )

    async def allowed(**_: Any) -> None:
        return None

    monkeypatch.setattr(browser_panels, "browse_refusal", allowed)
    ph.reset_panel_hub()
    yield
    ph.reset_panel_hub()


def _use(monkeypatch: pytest.MonkeyPatch, hub: ph.PanelHub) -> None:
    monkeypatch.setattr(browser_panels, "get_panel_hub", lambda: hub)


def _identity(kind: str, token: str = TOKEN_A):
    @web.middleware
    async def middleware(request: web.Request, handler):
        if kind == "owner":
            request["user"], request["app"], request["auth_token"] = "local-app", "", token
        elif kind == "app":
            request["user"], request["app"], request["auth_token"] = "some-app", "some-app", token
        elif kind == "internal":
            request["internal_auth"] = True
            request["user"], request["app"], request["auth_token"] = "local-app", "", token
        return await handler(request)

    return middleware


def _app(*middlewares: Any) -> web.Application:
    app = web.Application(middlewares=list(middlewares))
    app["state"] = SimpleNamespace(owner_id="", _slots={})
    # The origin check's own list: nothing beyond the request's own host.
    app["allowed_origins"] = set()
    app.router.add_post(INPUT, browser_panels.api_browser_panel_input)
    app.router.add_post(ASK, browser_panels.api_browser_panel_ask)
    return app


async def _post(client: TestClient, path: str, body: Any, **headers: str) -> tuple[int, Any, Any]:
    resp = await client.post(path, json=body, headers=headers)
    text = await resp.text()
    try:
        return resp.status, json.loads(text), resp.headers
    except ValueError:
        return resp.status, text, resp.headers


# ── the owner gate, and the hub behind it ───────────────────────────────────


@pytest.mark.parametrize("path", ROUTES)
async def test_the_agents_verified_secret_is_refused_on_both_routes(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, browser, _clock = make_hub()
    _use(monkeypatch, hub)
    panel = await open_live(hub, browser, Client(1, TOKEN_A))
    async with TestClient(TestServer(_app(_identity("internal")))) as client:
        status, body, _ = await _post(client, path, {"panel": panel.handle, "event": MOVE})
    assert status == 403 and body["code"] == "internal_secret_required"
    assert browser.relay.dispatched() == []


@pytest.mark.parametrize("kind", ["app", "anon"])
@pytest.mark.parametrize("path", ROUTES)
async def test_anyone_but_the_owner_is_refused_on_both_routes(
    kind: str, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, browser, _clock = make_hub()
    _use(monkeypatch, hub)
    panel = await open_live(hub, browser, Client(1, TOKEN_A))
    async with TestClient(TestServer(_app(_identity(kind)))) as client:
        status, body, _ = await _post(client, path, {"panel": panel.handle, "event": MOVE})
    assert status == 403 and body["code"] == "owner_only"
    assert browser.relay.dispatched() == []


async def test_the_owner_drives_the_panel_their_own_sign_in_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub, browser, _clock = make_hub()
    _use(monkeypatch, hub)
    panel = await open_live(hub, browser, Client(1, TOKEN_A))
    async with TestClient(TestServer(_app(_identity("owner")))) as client:
        status, body, _ = await _post(client, INPUT, {"panel": panel.handle, "event": MOVE})
        assert status == 200 and body["sent"] == 1 and body["target"] is None
        status, body, _ = await _post(client, ASK, {"panel": panel.handle, "question": "state"})
        assert status == 200 and body["answer"]["path"] == "/chat"
        status, body, _ = await _post(client, ASK, {"panel": panel.handle, "question": "html"})
        assert status == 400 and body["code"] == "unknown_question"
        status, body, _ = await _post(
            client, INPUT, {"panel": panel.handle, "event": {"type": "x"}}
        )
        assert status == 400 and body["code"] == "invalid_input_event"
        status, body, _ = await _post(client, INPUT, {"panel": "p999", "event": MOVE})
        assert status == 404 and body["code"] == "unknown_panel"


async def test_a_press_that_the_pages_connection_cuts_is_answered_as_not_all_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tab's connection closes between a click's press and its release: 200, one sent, ``lost``."""
    hub, browser, _clock = make_hub()
    _use(monkeypatch, hub)
    panel = await open_live(hub, browser, Client(1, TOKEN_A))
    relay, send = browser.relay, browser.relay.send
    pressed: list[str] = []

    async def cut_after_the_press(method: str, *args: Any, **kwargs: Any) -> Any:
        if method == "Input.dispatchMouseEvent":
            if pressed:
                raise RelayClosed("relay closed")
            pressed.append(method)
        return await send(method, *args, **kwargs)

    monkeypatch.setattr(relay, "send", cut_after_the_press)
    click = {"type": "mouse", "action": "click", "x": 500, "y": 300}
    async with TestClient(TestServer(_app(_identity("owner")))) as client:
        status, body, _ = await _post(client, INPUT, {"panel": panel.handle, "event": click})
    assert status == 200 and body == {"ok": True, "sent": 1, "lost": True}


@pytest.mark.parametrize("path", ROUTES)
async def test_another_sessions_panel_is_refused(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, browser, _clock = make_hub()
    _use(monkeypatch, hub)
    panel = await open_live(hub, browser, Client(1, TOKEN_A))
    async with TestClient(TestServer(_app(_identity("owner", TOKEN_B)))) as client:
        status, body, _ = await _post(
            client, path, {"panel": panel.handle, "event": MOVE, "question": "state"}
        )
    assert status == 403 and body["code"] == "not_your_panel"
    assert browser.relay.dispatched() == []


@pytest.mark.parametrize("path", ROUTES)
async def test_with_the_setting_off_the_routes_refuse(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, browser, _clock = make_hub(enabled=False)
    _use(monkeypatch, hub)
    async with TestClient(TestServer(_app(_identity("owner")))) as client:
        status, body, _ = await _post(client, path, {"panel": "p1", "event": MOVE})
    assert status == 403 and body["code"] == "off"
    assert browser.ensures == 0


async def test_a_browse_deny_refuses_the_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def denied(**_: Any) -> web.Response:
        return web.json_response({"error": "x", "code": "policy_denied"}, status=403)

    monkeypatch.setattr(browser_panels, "browse_refusal", denied)
    assert browser_panels.browse_refusal is not browser_policy.browse_refusal
    async with TestClient(TestServer(_app(_identity("owner")))) as client:
        status, body, _ = await _post(client, INPUT, {"panel": "p1", "event": MOVE})
    assert status == 403 and body["code"] == "policy_denied"


async def test_an_oversized_body_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    hub, _browser, _clock = make_hub()
    _use(monkeypatch, hub)
    async with TestClient(TestServer(_app(_identity("owner")))) as client:
        status, _body, _ = await _post(
            client, INPUT, {"panel": "p1", "event": {"type": "text", "text": "x" * 40_000}}
        )
    assert status == 413


# ── the real sign-in gate and the real origin check ─────────────────────────


@pytest.fixture
def real_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> web.Application:
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"panel-routes-signing-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    hub, _browser, _clock = make_hub()
    _use(monkeypatch, hub)
    gate = token_auth.token_auth_middleware(
        internal_paths=frozenset({"/api/spawn"}), internal_secret=SECRET
    )
    return _app(_make_csrf_middleware("dashboard_user"), gate)


@pytest.mark.parametrize("path", ROUTES)
async def test_no_sign_in_gets_the_gates_own_refusal(path: str, real_gate: web.Application) -> None:
    async with TestClient(TestServer(real_gate)) as client:
        status, body, headers = await _post(client, path, {"panel": "p1", "event": MOVE})
    assert status == 403 and headers.get("X-Auth-Required") == "true"
    assert body["code"] != "unknown_panel"


async def test_the_gate_verifies_the_credential_it_is_shown(real_gate: web.Application) -> None:
    good = token_auth.generate_token("local-app")
    forged = good[:-6] + ("A" * 6 if not good.endswith("A" * 6) else "B" * 6)
    async with TestClient(TestServer(real_gate)) as client:
        status, _body, headers = await client_post_token(client, forged)
        assert status == 403 and headers.get("X-Auth-Required") == "true"
        # The real credential reaches the handler, which then knows no such panel.
        status, body, _ = await client_post_token(client, good)
        assert status == 404 and body["code"] == "unknown_panel"


async def client_post_token(client: TestClient, token: str) -> tuple[int, Any, Any]:
    resp = await client.post(INPUT, params={"token": token}, json={"panel": "p1", "event": MOVE})
    text = await resp.text()
    try:
        return resp.status, json.loads(text), resp.headers
    except ValueError:
        return resp.status, text, resp.headers


@pytest.mark.parametrize("path", ROUTES)
async def test_a_wrong_origin_is_refused_before_the_handler(
    path: str, real_gate: web.Application
) -> None:
    token = token_auth.generate_token("local-app")
    async with TestClient(TestServer(real_gate)) as client:
        resp = await client.post(
            path,
            params={"token": token},
            json={"panel": "p1", "event": MOVE},
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status == 403
        assert "origin" in (await resp.text()).lower()


@pytest.mark.parametrize("path", ROUTES)
async def test_the_agents_secret_alone_does_not_pass_the_gate_on_these_paths(
    path: str, real_gate: web.Application
) -> None:
    """The secret is honoured only on the internal path list, which these are not on."""
    async with TestClient(TestServer(real_gate)) as client:
        status, _body, headers = await _post(
            client, path, {"panel": "p1", "event": MOVE}, **{"X-Internal-Secret": SECRET}
        )
    assert status == 403 and headers.get("X-Auth-Required") == "true"


async def test_the_panel_paths_are_on_no_internal_secret_list() -> None:
    lists = (
        server_internal_paths._STRICT_INTERNAL_API_PATHS
        | server_internal_paths._MIXED_INTERNAL_API_PATHS
    )
    assert not any(path.startswith("/api/browser/panel") for path in lists)
    assert not any(path.startswith("/api/browser/panel") for path in lists if path.endswith("/"))
    assert "/api/browser" not in lists and "/api/browser/" not in lists


async def test_the_slice_registers_the_two_routes_and_the_shutdown() -> None:
    app = web.Application()
    app["state"] = SimpleNamespace(deliver_ws_owners=None, deliver_ws_owners_bytes=None)
    route_slice.register(app)
    posts = {r.resource.canonical for r in app.router.routes() if r.method == "POST"}
    assert set(ROUTES) <= posts
    assert browser_panels.close_panel_hub in app.on_cleanup


# ── the socket leg ──────────────────────────────────────────────────────────


class _Ws(dict):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict[str, Any]] = []

    async def send_str(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def send_bytes(self, blob: bytes) -> None:
        self.sent.append({"bytes": len(blob)})


class _Request(dict):
    def __init__(self, port: int | None = 6777, host: str = "127.0.0.1", **claims: Any) -> None:
        super().__init__(claims)
        self.app = {"state": SimpleNamespace(owner_id="")}
        sockname: Any = (host, port, 0, 0)[: 4 if ":" in host else 2] if port else "/run/kc.sock"
        self.transport = SimpleNamespace(get_extra_info=lambda name: sockname)


def _owner(**more: Any) -> dict[str, Any]:
    return {"user": "local-app", "app": "", "auth_token": TOKEN_A, **more}


async def _settle(ws: _Ws) -> None:
    chain = ws.get(browser_panels._CHAIN_KEY)
    if chain is not None:
        await chain


OPEN = {"op": "open", "id": "a", "path": "/chat", "w": 1024, "h": 720}


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"user": "some-app", "app": "some-app", "auth_token": TOKEN_A},
        _owner(internal_auth=True),
        {"user": "local-app", "app": "", "auth_token": ""},
        {"user": "local-app", "auth_token": TOKEN_A},
    ],
)
async def test_a_socket_that_is_not_the_owners_own_sign_in_opens_nothing(
    claims: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, browser, _clock = make_hub()
    _use(monkeypatch, hub)
    ws = _Ws()
    browser_panels.handle_panel_frame(ws, _Request(**claims), dict(OPEN))  # type: ignore[arg-type]
    await _settle(ws)
    assert ws.sent == [
        {
            "type": "xr_panel",
            "data": {"op": "status", "id": "a", "status": "failed", "why": "owner_only"},
        }
    ]
    assert browser.ensures == 0 and hub.panel_count() == 0


async def test_the_owners_socket_opens_a_panel_on_the_gateways_own_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub, browser, _clock = make_hub()
    _use(monkeypatch, hub)
    ws = _Ws()
    request = _Request(**_owner())
    browser_panels.handle_panel_frame(ws, request, dict(OPEN))  # type: ignore[arg-type]
    await _settle(ws)
    assert ws.sent[-1]["data"]["op"] == "opened"
    socket = browser_panels.panel_socket(ws, request)  # type: ignore[arg-type]
    assert socket is not None
    assert (socket.origin, socket.cookie_name) == ("http://127.0.0.1:6777", "mc_token_6777")
    assert browser.relay.methods("Page.navigate")[0][1] == {"url": "http://127.0.0.1:6777/chat"}
    # The socket's close takes everything with it.
    await browser_panels.close_panel_socket(ws)  # type: ignore[arg-type]
    assert hub.panel_count() == 0 and browser.disposed == ["ctx1"] and socket.token == ""


async def test_a_listener_that_is_not_a_tcp_port_gets_no_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub, browser, _clock = make_hub()
    _use(monkeypatch, hub)
    ws = _Ws()
    browser_panels.handle_panel_frame(ws, _Request(port=None, **_owner()), dict(OPEN))  # type: ignore[arg-type]
    await _settle(ws)
    assert ws.sent[-1]["data"]["why"] == "owner_only" and browser.ensures == 0


@pytest.mark.parametrize(
    "host", ["10.0.0.5", "192.168.1.20", "100.64.0.7", "0.0.0.0", "fd7a::1", "::"]
)
async def test_a_socket_accepted_on_an_address_that_is_not_loopback_gets_no_panel(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, browser, _clock = make_hub()
    _use(monkeypatch, hub)
    ws = _Ws()
    browser_panels.handle_panel_frame(ws, _Request(host=host, **_owner()), dict(OPEN))  # type: ignore[arg-type]
    await _settle(ws)
    assert ws.sent[-1]["data"]["why"] == "owner_only" and browser.ensures == 0


async def test_the_tab_uses_the_loopback_address_the_owners_socket_came_in_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate pins a session to its caller's address: the tab must be that same caller."""
    hub, browser, _clock = make_hub()
    _use(monkeypatch, hub)
    ws = _Ws()
    request = _Request(host="::1", **_owner())
    browser_panels.handle_panel_frame(ws, request, dict(OPEN))  # type: ignore[arg-type]
    await _settle(ws)
    assert ws.sent[-1]["data"]["op"] == "opened"
    assert browser.relay.methods("Page.navigate")[0][1] == {"url": "http://[::1]:6777/chat"}
    (cookies,) = browser.relay.methods("Storage.setCookies")
    assert cookies[1]["cookies"][0]["url"] == "http://[::1]:6777/"
    assert cookies[1]["cookies"][0]["name"] == "mc_token_6777"


async def test_with_the_setting_off_the_socket_leg_starts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub, browser, _clock = make_hub(enabled=False)
    _use(monkeypatch, hub)
    ws = _Ws()
    browser_panels.handle_panel_frame(ws, _Request(**_owner()), dict(OPEN))  # type: ignore[arg-type]
    await _settle(ws)
    assert ws.sent[-1]["data"] == {"op": "status", "id": "a", "status": "failed", "why": "off"}
    assert browser.ensures == 0


@pytest.mark.parametrize("frame", [None, "open", {"op": "eval", "id": "a"}, {"id": "a"}, []])
async def test_a_frame_outside_the_contract_does_nothing(
    frame: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, browser, _clock = make_hub()
    _use(monkeypatch, hub)
    ws = _Ws()
    browser_panels.handle_panel_frame(ws, _Request(**_owner()), frame)  # type: ignore[arg-type]
    await _settle(ws)
    assert ws.sent == [] and browser.ensures == 0


# ── what an agent can reach: nothing ────────────────────────────────────────

_SRC = Path(kiro_crew.__file__).parent
_PANEL_MODULES = {
    "panel_hub",
    "panel_browser",
    "panel_stream",
    "panel_page",
    "panel_fence",
    "panel_cover",
    "panel_window",
    "browser_panels",
}
#: The only modules that may name a panel module: the panel's own, the dashboard's
#: socket and the route slice. No MCP tool, no agent op, no helper is among them.
_MAY_IMPORT = {
    "browser_harness/panel_browser.py",
    "browser_harness/panel_fence.py",
    "browser_harness/panel_hub.py",
    "browser_harness/panel_window.py",
    "dashboard/handlers/browser_panels.py",
    "dashboard/routes/browser_live.py",
    "dashboard/ws.py",
}


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
    return names


async def test_no_agent_reachable_module_imports_a_panel_module() -> None:
    importers = set()
    for path in _SRC.rglob("*.py"):
        if _imported_names(ast.parse(path.read_text(encoding="utf-8"))) & _PANEL_MODULES:
            importers.add(path.relative_to(_SRC).as_posix())
    assert sorted(importers) == sorted(_MAY_IMPORT)


async def test_the_panel_browser_gets_no_listener_and_no_debugging_port() -> None:
    class Upstream:
        async def send(self, raw: str) -> None: ...

        async def messages(self):  # noqa: ANN202
            return
            yield ""  # pragma: no cover

        async def close(self) -> None: ...

    relay = CdpRelay()
    assert await relay.start(Upstream(), serve=False) == ""
    assert relay.url == "" and relay.ws_url == "" and relay._runner is None
    await relay.close()
    source = Path(pb.__file__).read_text(encoding="utf-8")
    assert "serve=False" in source and "launch_chromium_pipe" in source
    assert "_launch_chromium_port" not in source and "DevToolsActivePort" not in source


async def test_the_panel_browser_refuses_a_host_without_the_pipe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pb.platform_compat, "IS_WINDOWS", True)
    with pytest.raises(pb.PanelUnavailable) as refused:
        await pb.PanelBrowser(tmp_path).ensure()
    assert refused.value.code == "unsupported_host"


async def test_the_agents_browser_still_refuses_the_gateways_own_port() -> None:
    allowed = frozenset({"http://localhost:6777", "http://127.0.0.1:6777"})
    assert url_gate.url_allowed("http://127.0.0.1:6777/chat", allowed, 6777) is False
    assert url_gate.url_allowed("http://localhost:6777/settings", allowed, 6777) is False
    assert url_gate.url_allowed("http://localhost:6777/chat", allowed, None) is False


# ── the page he looks at (``watch``) ────────────────────────────────────────


async def test_one_page_a_socket_is_watched_and_the_word_acts_only_on_its_own_panels() -> None:
    hub, browser, _clock = make_hub()
    mine, other = Client(1, TOKEN_A), Client(2, TOKEN_A)
    a = await open_live(hub, browser, mine, "a")
    b = await open_live(hub, browser, mine, "b", "/schedule")
    c = await open_live(hub, browser, other, "c")
    await hub.set_watched(mine.socket.key, "a", True)
    assert a.stream.watched and not b.stream.watched
    # Watching another page of the socket stops the first: one fast page, never twelve.
    await hub.set_watched(mine.socket.key, "b", True)
    assert b.stream.watched and not a.stream.watched
    # Another socket's watch leaves this socket's page as it is.
    await hub.set_watched(other.socket.key, "c", True)
    assert b.stream.watched and c.stream.watched
    # A panel of another socket, an unknown id, or a value that is not a boolean does nothing.
    for key, pid, on in (
        (mine.socket.key, "c", True),
        (mine.socket.key, "zz", True),
        (mine.socket.key, "a", "yes"),
        (mine.socket.key, 7, True),
    ):
        await hub.set_watched(key, pid, on)
    assert b.stream.watched and not a.stream.watched and c.stream.watched
    await hub.set_watched(mine.socket.key, "b", False)
    assert not b.stream.watched


async def test_a_watched_page_loses_the_watch_out_of_view_closed_or_at_its_sockets_end() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    a = await open_live(hub, browser, client, "a")
    await hub.set_watched(client.socket.key, "a", True)
    await hub.set_visible(client.socket.key, "a", False)
    assert not a.stream.watched
    # Out of view it is not watched again.
    await hub.set_watched(client.socket.key, "a", True)
    assert not a.stream.watched
    await hub.set_visible(client.socket.key, "a", True)
    await hub.set_watched(client.socket.key, "a", True)
    assert a.stream.watched
    await hub.close(client.socket.key, "a")
    assert not a.stream.watched
    b = await open_live(hub, browser, client, "b")
    await hub.set_watched(client.socket.key, "b", True)
    await hub.close_socket(client.socket.key)
    assert not b.stream.watched


async def test_the_watch_word_goes_through_the_owners_socket_like_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub, browser, _clock = make_hub()
    _use(monkeypatch, hub)
    ws = _Ws()
    request = _Request(**_owner())
    browser_panels.handle_panel_frame(ws, request, dict(OPEN))  # type: ignore[arg-type]
    await _settle(ws)
    panel = hub._by_handle[ws.sent[-1]["data"]["handle"]]
    browser_panels.handle_panel_frame(ws, request, {"op": "watch", "id": "a", "on": True, "extra": "x" * 9})  # type: ignore[arg-type]
    await _settle(ws)
    assert panel.stream.watched
    # A socket that is not the owner's own sign-in reaches no panel with it.
    stranger = _Ws()
    browser_panels.handle_panel_frame(stranger, _Request(user="someone", app="", auth_token="t"), {"op": "watch", "id": "a", "on": False})  # type: ignore[arg-type]
    await _settle(stranger)
    assert panel.stream.watched and stranger.sent == []


async def test_a_word_the_gateway_does_not_know_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """What an older gateway does with ``watch``: nothing, and the socket goes on."""
    hub, browser, _clock = make_hub()
    _use(monkeypatch, hub)
    ws = _Ws()
    request = _Request(**_owner())
    browser_panels.handle_panel_frame(ws, request, dict(OPEN))  # type: ignore[arg-type]
    await _settle(ws)
    sent = len(ws.sent)
    browser_panels.handle_panel_frame(ws, request, {"op": "not-a-word", "id": "a", "on": True})  # type: ignore[arg-type]
    await _settle(ws)
    assert len(ws.sent) == sent and hub.panel_count() == 1


# ── a wheel and a move on the socket ────────────────────────────────────────

WHEEL = {"type": "wheel", "x": 10, "y": 10, "dx": 0, "dy": 40}


async def _inputs_done(ws: _Ws) -> None:
    """The socket's line of input events is empty, and every answer is out."""
    line = ws.get(browser_panels._INPUT_KEY)
    if line is not None and line["task"] is not None:
        await line["task"]
    for _ in range(5):
        await asyncio.sleep(0)


def _answers(ws: _Ws) -> list[dict[str, Any]]:
    return [m["data"] for m in ws.sent if m.get("data", {}).get("op") in ("input", "hello")]


async def _live_on_socket(
    monkeypatch: pytest.MonkeyPatch, token: str = TOKEN_A
) -> tuple[_Ws, _Request, ph.PanelHub, Any, str]:
    """A panel that the owner's socket opened through the handler, walked to its loaded page."""
    hub, browser, _clock = make_hub()
    _use(monkeypatch, hub)
    ws = _Ws()
    request = _Request(**_owner(auth_token=token))
    browser_panels.handle_panel_frame(ws, request, dict(OPEN))  # type: ignore[arg-type]
    await _settle(ws)
    handle = ws.sent[-1]["data"]["handle"]
    panel = hub._by_handle[handle]
    relay = browser.relay
    url = panel.socket.origin + "/chat"
    await relay.push(
        panel.target_id, "Page.frameNavigated", {"frame": {"id": MAIN_FRAME, "url": url}}
    )
    context = {"id": WORLD, "name": panel_page.PANEL_WORLD, "auxData": {"frameId": MAIN_FRAME}}
    await relay.push(panel.target_id, "Runtime.executionContextCreated", {"context": context})
    await relay.push(panel.target_id, "Page.loadEventFired", {})
    assert panel.status == ph.LIVE
    return ws, request, hub, browser, handle


def _wheels(browser: Any) -> list[dict[str, Any]]:
    sent = browser.relay.methods("Input.dispatchMouseEvent")
    return [p for _, p, _ in sent if p.get("type") == "mouseWheel"]


async def test_the_gateway_says_that_it_takes_input_on_the_socket_to_the_owner_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, request, _hub, _browser, _handle = await _live_on_socket(monkeypatch)
    browser_panels.handle_panel_frame(ws, request, {"op": "hello"})  # type: ignore[arg-type]
    await _inputs_done(ws)
    assert _answers(ws) == [{"op": "hello", "input": True}]
    stranger = _Ws()
    browser_panels.handle_panel_frame(stranger, _Request(user="someone", app="", auth_token="t"), {"op": "hello"})  # type: ignore[arg-type]
    await _inputs_done(stranger)
    assert stranger.sent == []


async def test_a_wheel_on_the_owners_socket_reaches_the_page_and_is_answered_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, request, _hub, browser, handle = await _live_on_socket(monkeypatch)
    frame = {"op": "input", "handle": handle, "n": 7, "event": dict(WHEEL)}
    browser_panels.handle_panel_frame(ws, request, frame)  # type: ignore[arg-type]
    await _inputs_done(ws)
    assert [(p["x"], p["y"], p["deltaY"]) for p in _wheels(browser)] == [(10, 10, 40)]
    (answer,) = _answers(ws)
    assert answer["op"] == "input" and answer["handle"] == handle and answer["n"] == 7
    assert answer["sent"] >= 1 and "error" not in answer
    # A move is answered with what stands under it, as the route answers.
    browser_panels.handle_panel_frame(ws, request, {"op": "input", "handle": handle, "n": 8, "event": dict(MOVE)})  # type: ignore[arg-type]
    await _inputs_done(ws)
    assert _answers(ws)[-1]["n"] == 8 and "target" in _answers(ws)[-1]


@pytest.mark.parametrize(
    "event",
    [
        {"type": "mouse", "action": "click", "x": 10, "y": 10},
        {"type": "mouse", "action": "down", "x": 10, "y": 10},
        {"type": "key", "key": "Enter"},
        {"type": "text", "text": "hi"},
        {"type": "leave"},
    ],
)
async def test_a_press_a_key_and_a_text_are_not_taken_on_the_socket(
    event: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, request, _hub, browser, handle = await _live_on_socket(monkeypatch)
    before = len(browser.relay.calls)
    browser_panels.handle_panel_frame(ws, request, {"op": "input", "handle": handle, "n": 1, "event": event})  # type: ignore[arg-type]
    await _inputs_done(ws)
    assert _answers(ws) == [{"op": "input", "handle": handle, "n": 1, "error": "not_on_the_socket"}]
    assert len(browser.relay.calls) == before


async def test_a_socket_that_is_not_the_owners_sends_no_input_and_gets_no_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ws, _request, _hub, browser, handle = await _live_on_socket(monkeypatch)
    for claims in ({"user": "someone", "app": "", "auth_token": "t"}, _owner(internal_auth=True)):
        stranger = _Ws()
        frame = {"op": "input", "handle": handle, "n": 1, "event": dict(WHEEL)}
        browser_panels.handle_panel_frame(stranger, _Request(**claims), frame)  # type: ignore[arg-type]
        await _inputs_done(stranger)
        assert stranger.sent == []
    assert _wheels(browser) == []


async def test_another_sign_ins_socket_moves_no_panel_of_this_one_and_no_unknown_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ws, _request, _hub, browser, handle = await _live_on_socket(monkeypatch)
    other = _Ws()
    request = _Request(**_owner(auth_token=TOKEN_B))
    for name, code in ((handle, "not_your_panel"), ("no-such-panel", "unknown_panel")):
        frame = {"op": "input", "handle": name, "n": 3, "event": dict(WHEEL)}
        browser_panels.handle_panel_frame(other, request, frame)  # type: ignore[arg-type]
        await _inputs_done(other)
        assert _answers(other)[-1] == {"op": "input", "handle": name, "n": 3, "error": code}
    assert _wheels(browser) == []


async def test_under_a_browse_deny_an_input_on_the_socket_reaches_no_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, request, _hub, browser, handle = await _live_on_socket(monkeypatch)

    async def denied(**_: Any) -> web.Response:
        return web.json_response({"error": "denied"}, status=403)

    monkeypatch.setattr(browser_panels, "browse_refusal", denied)
    browser_panels.handle_panel_frame(ws, request, {"op": "input", "handle": handle, "n": 2, "event": dict(WHEEL)})  # type: ignore[arg-type]
    await _inputs_done(ws)
    assert _answers(ws) == [{"op": "input", "handle": handle, "n": 2, "error": "browse_denied"}]
    assert _wheels(browser) == []


@pytest.mark.parametrize(
    "frame",
    [
        {"op": "input", "handle": 5, "n": 1, "event": dict(WHEEL)},
        {"op": "input", "handle": "", "n": 1, "event": dict(WHEEL)},
        {"op": "input", "handle": "h" * 65, "n": 1, "event": dict(WHEEL)},
        {"op": "input", "handle": "h", "n": True, "event": dict(WHEEL)},
        {"op": "input", "handle": "h", "n": -1, "event": dict(WHEEL)},
        {"op": "input", "handle": "h", "n": 2**31 + 1, "event": dict(WHEEL)},
        {"op": "input", "handle": "h", "n": 1.5, "event": dict(WHEEL)},
        {"op": "input", "handle": "h", "n": 1, "event": "wheel"},
        {"op": "input", "handle": "h", "n": 1},
    ],
)
async def test_an_input_frame_outside_the_contract_does_nothing(
    frame: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, request, _hub, browser, _handle = await _live_on_socket(monkeypatch)
    before = len(browser.relay.calls)
    browser_panels.handle_panel_frame(ws, request, frame)  # type: ignore[arg-type]
    await _inputs_done(ws)
    assert _answers(ws) == [] and len(browser.relay.calls) == before
    assert browser_panels._INPUT_KEY not in ws


async def test_at_most_four_events_wait_wheels_add_up_and_the_newer_move_stands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, request, hub, _browser, handle = await _live_on_socket(monkeypatch)
    gate = asyncio.Event()
    took: list[tuple[str, dict[str, Any]]] = []

    async def slow_input(token: str, name: Any, event: Any) -> dict[str, Any]:
        assert token == TOKEN_A
        took.append((name, event))
        await gate.wait()
        return {"sent": 1}

    monkeypatch.setattr(hub, "input", slow_input)

    def send(name: str, n: int, event: dict[str, Any]) -> None:
        browser_panels.handle_panel_frame(ws, request, {"op": "input", "handle": name, "n": n, "event": event})  # type: ignore[arg-type]

    send(handle, 1, {**WHEEL, "dy": 10})
    await asyncio.sleep(0)
    # The first is on its way to the page. Behind it: two wheels (one line), two moves (one line), and two more panels' wheels.
    send(handle, 2, {**WHEEL, "dy": 20})
    send(handle, 3, {**WHEEL, "dy": 30, "x": 50})
    send(handle, 4, {**MOVE, "x": 1})
    send(handle, 5, {**MOVE, "x": 2})
    send("b", 6, dict(WHEEL))
    send("c", 7, dict(WHEEL))
    # The line is full: one more panel's wheel is dropped; a wheel for a panel that waits still adds up.
    send("d", 8, dict(WHEEL))
    send("c", 9, {**WHEEL, "dy": 5})
    gate.set()
    await _inputs_done(ws)
    assert took == [
        (handle, {**WHEEL, "dy": 10}),
        (handle, {**WHEEL, "dy": 50, "x": 50}),
        (handle, {**MOVE, "x": 2}),
        ("b", WHEEL),
        ("c", {**WHEEL, "dy": 45}),
    ]
    by_n = {a["n"]: a for a in _answers(ws)}
    assert by_n[2].get("merged") and by_n[4].get("merged") and by_n[7].get("merged")
    assert by_n[8] == {"op": "input", "handle": "d", "n": 8, "dropped": True}
    assert [by_n[n].get("sent") for n in (1, 3, 5, 6, 9)] == [1, 1, 1, 1, 1]
    assert len(by_n) == 9


async def test_an_input_on_the_socket_does_not_wait_behind_an_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, request, hub, browser, handle = await _live_on_socket(monkeypatch)
    never = asyncio.Event()

    async def slow_open(*_: Any, **__: Any) -> None:
        await never.wait()

    monkeypatch.setattr(hub, "open", slow_open)
    browser_panels.handle_panel_frame(ws, request, {**OPEN, "id": "b"})  # type: ignore[arg-type]
    browser_panels.handle_panel_frame(ws, request, {"op": "input", "handle": handle, "n": 1, "event": dict(WHEEL)})  # type: ignore[arg-type]
    await _inputs_done(ws)
    assert len(_wheels(browser)) == 1 and _answers(ws)[-1]["n"] == 1
    never.set()
    await _settle(ws)


async def test_an_input_frame_over_the_routes_sixteen_kilobytes_is_refused_and_reaches_no_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, request, hub, browser, handle = await _live_on_socket(monkeypatch)
    called: list[Any] = []

    async def never(*args: Any) -> dict[str, Any]:
        called.append(args)
        return {"sent": 1}

    monkeypatch.setattr(hub, "input", never)
    big = {**WHEEL, "pad": "x" * browser_panels._MAX_BODY_BYTES}
    browser_panels.handle_panel_frame(ws, request, {"op": "input", "handle": handle, "n": 4, "event": big})  # type: ignore[arg-type]
    await _inputs_done(ws)
    assert _answers(ws) == [{"op": "input", "error": "too_large", "handle": handle, "n": 4}]
    assert called == [] and browser_panels._INPUT_KEY not in ws
    # The largest frame that the bound lets through is taken as before.
    room = browser_panels._MAX_BODY_BYTES - len(
        json.dumps({"op": "input", "handle": handle, "n": 5, "event": {**WHEEL, "pad": ""}})
    )
    fits = {"op": "input", "handle": handle, "n": 5, "event": {**WHEEL, "pad": "x" * room}}
    assert len(json.dumps(fits).encode()) == browser_panels._MAX_BODY_BYTES
    browser_panels.handle_panel_frame(ws, request, fits)  # type: ignore[arg-type]
    await _inputs_done(ws)
    assert len(called) == 1 and _answers(ws)[-1] == {
        "op": "input",
        "handle": handle,
        "n": 5,
        "sent": 1,
    }
    # A frame that is too large and names no panel or count is refused all the same.
    browser_panels.handle_panel_frame(ws, request, {"op": "input", "handle": 7, "n": "x", "event": big})  # type: ignore[arg-type]
    await _inputs_done(ws)
    assert _answers(ws)[-1] == {"op": "input", "error": "too_large"} and len(called) == 1


async def test_wheels_that_wait_add_up_to_the_hubs_bound_for_one_wheel_and_the_rest_is_the_next_wheel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, request, hub, browser, handle = await _live_on_socket(monkeypatch)
    gate = asyncio.Event()
    real = hub.input

    async def gated(token: str, name: Any, event: Any) -> dict[str, Any]:
        await gate.wait()
        return await real(token, name, event)

    monkeypatch.setattr(hub, "input", gated)

    def send(n: int, dy: int) -> None:
        browser_panels.handle_panel_frame(ws, request, {"op": "input", "handle": handle, "n": n, "event": {**WHEEL, "dy": dy}})  # type: ignore[arg-type]

    send(1, 10)
    await asyncio.sleep(0)
    # Three wheels of 6,000 wait behind the first: 18,000 in all, more than one wheel may carry (16,384).
    for n in (2, 3, 4):
        send(n, 6000)
    gate.set()
    await _inputs_done(ws)
    assert [p["deltaY"] for p in _wheels(browser)] == [
        10,
        ph_takeover.MAX_DELTA,
        18000 - ph_takeover.MAX_DELTA,
    ]
    by_n = {a["n"]: a for a in _answers(ws)}
    assert not any("error" in a for a in by_n.values())
    # The second wheel was added to the third; the third is the full one, the fourth carries what was over.
    assert by_n[2].get("merged") and by_n[3]["sent"] >= 1 and by_n[4]["sent"] >= 1
    # Up as down: the bound holds for a sum below zero too.
    gate.clear()
    send(5, -10)
    await asyncio.sleep(0)
    for n in (6, 7, 8):
        send(n, -6000)
    gate.set()
    await _inputs_done(ws)
    assert [p["deltaY"] for p in _wheels(browser)][3:] == [
        -10,
        -ph_takeover.MAX_DELTA,
        -(18000 - ph_takeover.MAX_DELTA),
    ]


async def test_what_is_over_the_bound_is_dropped_when_the_line_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, request, hub, _browser, handle = await _live_on_socket(monkeypatch)
    gate = asyncio.Event()
    took: list[tuple[str, dict[str, Any]]] = []

    async def slow_input(_token: str, name: Any, event: Any) -> dict[str, Any]:
        took.append((name, event))
        await gate.wait()
        return {"sent": 1}

    monkeypatch.setattr(hub, "input", slow_input)

    def send(name: str, n: int, dy: int) -> None:
        browser_panels.handle_panel_frame(ws, request, {"op": "input", "handle": name, "n": n, "event": {**WHEEL, "dy": dy}})  # type: ignore[arg-type]

    send(handle, 1, 10)
    await asyncio.sleep(0)
    for i, name in enumerate((handle, "b", "c", "d")):
        send(name, 2 + i, 10000)
    # Four wait. One more wheel for the first of them fills it; what is over finds no place in the line.
    send(handle, 9, 10000)
    gate.set()
    await _inputs_done(ws)
    assert took[1] == (handle, {**WHEEL, "dy": ph_takeover.MAX_DELTA})
    assert len(took) == 5
    assert {a["n"]: a for a in _answers(ws)}[9] == {
        "op": "input",
        "handle": handle,
        "n": 9,
        "dropped": True,
    }


async def test_a_socket_that_closes_empties_its_line_of_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, request, hub, _browser, handle = await _live_on_socket(monkeypatch)
    gate = asyncio.Event()
    took: list[dict[str, Any]] = []

    async def slow_input(_token: str, _name: Any, event: Any) -> dict[str, Any]:
        took.append(event)
        await gate.wait()
        return {"sent": 1}

    monkeypatch.setattr(hub, "input", slow_input)
    for n, event in ((1, WHEEL), (2, MOVE), (3, {**WHEEL, "dy": 7})):
        browser_panels.handle_panel_frame(ws, request, {"op": "input", "handle": handle if n < 3 else "b", "n": n, "event": dict(event)})  # type: ignore[arg-type]
        await asyncio.sleep(0)
    assert len(took) == 1 and len(ws[browser_panels._INPUT_KEY]["waiting"]) == 2
    await browser_panels.close_panel_socket(ws)  # type: ignore[arg-type]
    assert ws[browser_panels._INPUT_KEY]["waiting"] == []
    gate.set()
    await _inputs_done(ws)
    # Only the event that was already on its way reached the hub.
    assert took == [dict(WHEEL)]
