"""A Shared Crew guest does everything the owner does in the Crew, except the pinned list.

``is_owner_dashboard_request`` answers true for a guest on every route outside
``guest_scope``'s owner-only list, and ``is_host_owner_dashboard_request`` never
does. The list is copied in full below, so changing it means changing this file.
"""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from conftest import make_dir_link, requires_symlinks
from kiro_crew.dashboard import routes as routes_pkg
from kiro_crew.dashboard.handlers import source_providers as sp
from kiro_crew.shared_crew import guest_scope
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
)

_PINNED_PREFIXES = (
    # Who may reach the Crew, and over which network path.
    "/api/shared-crew/access",
    "/api/shared-crew/preview",
    "/api/shared-crew/tailscale",
    "/api/shared-crew/ssm",
    "/api/tailnet",
    "/api/tunnel",
    "/api/mobile-connect",
    "/api/instances",
    # config.json and security policy.
    "/api/config",
    "/api/dashboard/config",
    "/api/agent/config",
    "/api/security",
    "/api/governance",
    "/api/redaction",
    "/api/admin",
    "/api/sel",
    "/api/decisions/consent",
    "/api/decisions/provider",
    "/api/decisions/local-models",
    "/api/aws",
    "/api/computer-use",
    "/api/telemetry/collection",
    "/api/debug",
    "/api/logs",
    "/api/diagnostics",
    "/api/system",
    # Credentials, connection secrets and the channels that hold them.
    "/api/secrets",
    "/api/connections",
    "/api/kas-login",
    "/api/kas-bundle",
    "/api/sso-login",
    "/api/sso-ttl",
    "/api/token",
    "/api/relay-viewer",
    "/api/auth",
    "/api/webhooks",
    "/api/channels",
    "/api/channel",
    "/api/channel-folders",
    "/api/session-environments",
    "/api/wakatime",
    "/api/onboarding",
    "/api/stt/ffmpeg",
    "/api/slack",
    "/api/telegram",
    "/api/discord",
    "/api/feishu",
    "/api/imessage",
    "/api/teams",
    "/api/messaging",
    "/api/webex",
    "/api/wecom",
    "/api/weixin",
    "/api/whatsapp",
    "/api/voice/config",
    "/api/notifications/push",
    "/api/cloud",
    "/api/usage",
    "/api/sessions/usage",
    "/api/deploy",
    "/api/publish-providers",
    "/api/remote-artifacts",
    "/api/send-message",
    "/api/delete-message",
    "/api/slack-profile",
    "/api/mediated-secret-request",
    "/api/mediated-secret-capability",
    "/api/connection-proxy",
    "/api/slack-history",
    "/api/app-store",
    "/api/mcp-gateway",
    "/api/skills/-/trust",
    "/api/mcp/oauth",
    "/api/monitors",
    "/api/autonudge",
    # The gateway process and code the host runs outside an agent's approvals.
    "/api/restart",
    "/api/shutdown",
    "/api/update",
    "/api/terminal",
    "/api/ws/terminal",
    "/api/mcp",
    "/api/capability",
    "/api/app-tools",
    "/api/mcp-apps",
    "/api/hooks",
    "/api/kiro-hooks",
    "/api/apps",
    "/api/browser",
    "/apps",
    "/api/themes/install",
    "/api/themes/install-zip",
    "/api/portability",
    "/api/worktree",
    "/api/project-scaffold",
    # The host filesystem outside the Crew's own stores.
    "/api/directory-upload",
    "/api/browse-dirs",
    "/api/browse-files",
    "/api/path-complete",
    "/api/reveal",
    "/api/project/git",
    "/api/project/tree",
    "/api/project/session-files",
    "/api/project/session-file-diff",
    "/api/knowledge/pick-folder",
    "/api/knowledge/remote-search",
    "/api/knowledge/import",
    "/api/knowledge/ingest",
    "/api/knowledge/sources",
    "/api/memory/stores",
    "/api/memory/backup",
    "/api/memory/backups",
    "/api/memory/restore",
    "/api/memory/import",
    "/api/memory/migrate",
    "/api/memory/enable-embeddings",
    "/api/memory/disable-embeddings",
    "/api/memory/embedding-model",
    "/api/ui-prefs",
    "/api/chat/headroom",
)
_PINNED_RAW_PREFIXES = ("/api/file-",)
_PINNED_PATTERNS = (
    r"/api/chat/slots/[^/]+/work-item",
    r"/api/crons/[^/]+/(?:script|secrets)",
    r"/api/artifacts/[^/]+/(?:publish|sharing|pull-latest|upstream-status|overwrite-remote)(?:/.*)?",
    r"/api/chat/slots/[^/]+/environment/(?:browse|file)(?:/.*)?",
)
_PINNED_WRITE_PATTERNS = (
    r"/api/agents(?:/.*)?",
    r"/api/workspaces(?:/.*)?",
    r"/api/projects(?:/.*)?",
    r"/api/chat/slots/[^/]+/environment",
    r"/api/agent-spec-pins(?:/.*)?",
    r"/api/workflows/(?:run|run_intent)",
    r"/api/workflows/definitions(?:/.*)?",
    r"/api/workflows/runs/[^/]+/(?:rerun|promote|update-definition)",
    r"/api/memory/settings",
    r"/api/notifications/channels/settings",
    r"/api/members/[^/]+/goals/schedules",
    r"/api/chat/slots/[^/]+/(?:slack|mirror)-(?:link|pause|unlink)",
    r"/api/taskrunner/[^/]+/(?:tasks/[^/]+|plan)",
    r"/api/artifacts/[^/]+/relocate",
)
_PINNED_METHOD_PATHS = (
    ("DELETE", "/api/sessions"),
    ("POST", "/api/upload"),
    ("POST", "/api/screenshot"),
)


def test_the_owner_only_list_is_pinned() -> None:
    assert guest_scope.OWNER_ONLY_PATH_PREFIXES == _PINNED_PREFIXES
    assert guest_scope.OWNER_ONLY_RAW_PREFIXES == _PINNED_RAW_PREFIXES
    assert guest_scope.OWNER_ONLY_PATH_PATTERNS == _PINNED_PATTERNS
    assert guest_scope.OWNER_ONLY_WRITE_PATTERNS == _PINNED_WRITE_PATTERNS
    assert guest_scope.OWNER_ONLY_METHOD_PATHS == _PINNED_METHOD_PATHS


def _registered_paths() -> set[str]:
    """Every ``/api`` and ``/apps`` path literal in the dashboard and apps packages."""
    out: set[str] = set()
    dashboard = Path(routes_pkg.__file__).parent.parent
    for src in [*dashboard.rglob("*.py"), *(dashboard.parent / "apps").glob("routes*.py")]:
        for node in ast.walk(ast.parse(src.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith(("/api/", "/apps/"))
            ):
                out.add(node.value)
    return out


#: Owner-only on purpose before any build registers them.
_NOT_YET_REGISTERED = {"/api/apps", "/api/shared-crew/ssm"}


def test_every_owner_only_entry_still_names_a_registered_route() -> None:
    """A renamed route must not leave a stale entry that quietly stops protecting it."""
    registered = _registered_paths()
    # Builtin apps mount under /api/apps through the apps registrar, not a slice.
    unmatched = [
        prefix
        for prefix in _PINNED_PREFIXES
        if prefix not in _NOT_YET_REGISTERED
        and not any(p == prefix or p.startswith(prefix + "/") for p in registered)
    ]
    assert unmatched == []
    assert any(p.startswith("/api/file-") for p in registered)
    assert any(guest_scope.is_owner_only_path(p.replace("{job_id}", "j1")) for p in registered)


@pytest.mark.parametrize(
    "path",
    [
        "/api/chat",
        "/api/chat/slots",
        "/api/chat/slots/s1/agent",
        "/api/chat/slots/s1/approve",
        "/api/chat/mode",
        "/api/approvals/a1/approve",
        "/api/members",
        "/api/members/mate/threads",
        "/api/agents",
        "/api/agents/detail",
        "/api/artifacts",
        "/api/artifacts/doc/versions",
        "/api/skills",
        "/api/steering",
        "/api/prompts",
        "/api/workflows/run",
        "/api/taskrunner",
        "/api/crons",
        "/api/crons/j1/run",
        "/api/update-message",
        "/api/upload/file",
        "/api/outbox",
        "/api/sessions",
        "/api/sessions/s1",
        "/api/shared-crew/people",
        "/api/shared-crew/presence",
        "/api/ask-question/pending",
    ],
)
def test_in_crew_routes_are_open_to_a_guest(path: str) -> None:
    assert not guest_scope.is_owner_only_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "/api/shared-crew/tailscale/invites",
        "/api/shared-crew/tailscale/credential",
        "/api/shared-crew/access",
        "/api/shared-crew/preview",
        "/api/config/kirocrew",
        "/api/security/posture",
        "/api/secrets/token",
        "/api/connections/mint",
        "/api/restart",
        "/api/shutdown",
        "/api/update/arm",
        "/api/mcp/custom",
        "/api/mcp-gateway/servers",
        "/api/terminal/sessions",
        "/api/ws/terminal",
        "/api/file-read",
        "/api/file-write",
        "/api/browse-dirs",
        "/api/crons/j1/script",
        "/api/crons/j1/secrets",
        "/api/apps/install",
        "/api/apps/issue-radar/crew",
        "/api/chat/../config/kirocrew",
        "//api//config/kirocrew",
        "/api/config/",
    ],
)
def test_share_management_and_host_routes_are_closed_to_a_guest(path: str) -> None:
    assert guest_scope.is_owner_only_path(path)


@pytest.mark.parametrize(
    "path", ["/api/apps/../uninstall", "/api/mcp/..", "/api/chat/./x", "/api/apps/foo/../.."]
)
def test_a_dot_segment_is_closed_to_a_guest(path: str) -> None:
    assert guest_scope.is_owner_only_path(path)


def test_only_the_bulk_session_delete_is_closed_by_method() -> None:
    assert guest_scope.is_owner_only_path("/api/sessions", "DELETE")
    assert not guest_scope.is_owner_only_path("/api/sessions", "GET")
    assert not guest_scope.is_owner_only_path("/api/sessions/s1", "DELETE")


# ── the predicate ──


def _person(login: str, provenance: Provenance) -> ActorContext:
    return ActorContext(
        principal=Principal(
            id=principal_id_for("tailscale", "tailnet", login),
            kind=PrincipalKind.HUMAN,
            provider="tailscale",
            issuer="tailnet",
            subject=login,
            provenance=provenance,
            display=DisplaySnapshot(name=login, login=login),
        ),
        request_id="r",
        transport=Transport.HTTP,
    )


GUEST = _person("guest@example.test", Provenance.TAILSCALE_SERVE)
LISTENER_GUEST = _person("lee@example.test", Provenance.TAILSCALE_LISTENER)
DIRECT_PEER = _person("dave@example.test", Provenance.TAILSCALE_DIRECT)
SLACK_LINK = _person("slack-user", Provenance.LOCAL_OPERATOR)


class _Request(dict):
    def __init__(self, path: str, actor: ActorContext | None, **claims: Any) -> None:
        super().__init__(
            {"app": "", "user": "guest@example.test", "is_dashboard_user": True, **claims}
        )
        if actor is not None:
            self[ACTOR_REQUEST_KEY] = actor
        self.path = path
        self.method = "POST"
        self.app = {"state": MagicMock(owner_id="owner")}


@pytest.fixture(autouse=True)
def _no_revocation_fence() -> Any:
    """The fence is process state: a revoke in another test must not mark this guest."""
    guest_scope._REVOKED_AT.clear()
    yield
    guest_scope._REVOKED_AT.clear()


@pytest.fixture(autouse=True)
def _preview_on(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")


@pytest.mark.parametrize("actor", [GUEST, LISTENER_GUEST])
def test_a_guest_acts_as_the_owner_on_an_open_route_only(actor: ActorContext) -> None:
    assert sp.is_owner_dashboard_request(_Request("/api/chat/slots", actor))
    assert not sp.is_owner_dashboard_request(_Request("/api/config/kirocrew", actor))
    assert not sp.is_owner_dashboard_request(_Request("/api/restart", actor))


def test_a_guest_is_never_the_host_owner() -> None:
    assert not sp.is_host_owner_dashboard_request(_Request("/api/chat/slots", GUEST))


@pytest.mark.parametrize(
    "request_",
    [
        _Request("/api/chat/slots", DIRECT_PEER),
        _Request("/api/chat/slots", SLACK_LINK),
        _Request("/api/chat/slots", None),
        _Request("/api/chat/slots", GUEST, app="some-app"),
        _Request("/api/chat/slots", GUEST, internal_auth=True),
        _Request("/api/chat/slots", GUEST, is_dashboard_user=False),
        _Request("/api/chat/slots", GUEST, user=""),
    ],
)
def test_only_an_admitted_person_on_their_own_session_is_a_guest(request_: _Request) -> None:
    assert not sp.is_owner_dashboard_request(request_)


def test_the_preview_gate_keeps_a_guest_out_when_off(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    _floor_monkeypatch.delenv("KIROCREW_SHARED_CREW_PREVIEW")
    with patch("kiro_crew.shared_crew.preview.preview_enabled", return_value=False):
        assert not sp.is_owner_dashboard_request(_Request("/api/chat/slots", GUEST))


def test_the_host_owner_is_unchanged() -> None:
    request = _Request("/api/config/kirocrew", None, user="owner")
    assert sp.is_owner_dashboard_request(request)
    assert sp.is_host_owner_dashboard_request(request)


# ── chat attachment preview ──

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _client(actor: ActorContext | None, *, user: str) -> TestClient:
    @web.middleware
    async def mw(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        request["user"] = user
        request["is_dashboard_user"] = True
        if actor is not None:
            request[ACTOR_REQUEST_KEY] = actor
        return await handler(request)

    app = web.Application(middlewares=[mw])
    app["state"] = MagicMock(owner_id="owner")
    from kiro_crew.dashboard.handlers import api_file_raw

    app.router.add_get("/api/file-raw", api_file_raw)
    return TestClient(TestServer(app))


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "uploads").mkdir(parents=True)
    (tmp_path / "home" / "sessions" / "s1.attachments").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    with patch("kiro_crew.sel.sel", return_value=MagicMock()):
        yield tmp_path


async def _get(client: TestClient, path: Path | str) -> int:
    async with client:
        return (await client.get("/api/file-raw", params={"path": str(path)})).status


@pytest.mark.asyncio
async def test_a_guest_previews_an_uploaded_picture(home: Path) -> None:
    pic = home / "home" / "uploads" / "abc_pic.png"
    pic.write_bytes(_PNG)
    assert await _get(_client(GUEST, user="guest@example.test"), pic) == 200


@pytest.mark.asyncio
async def test_a_guest_previews_a_persisted_session_picture(home: Path) -> None:
    pic = home / "home" / "sessions" / "s1.attachments" / "0123456789abcdef_pic.png"
    pic.write_bytes(_PNG)
    assert await _get(_client(GUEST, user="guest@example.test"), pic) == 200


@pytest.mark.asyncio
async def test_a_guest_cannot_read_a_picture_elsewhere_on_the_host(home: Path) -> None:
    pic = home / "outside" / "private.png"
    pic.write_bytes(_PNG)
    assert await _get(_client(GUEST, user="guest@example.test"), pic) == 403


@pytest.mark.asyncio
async def test_the_owner_still_reads_a_picture_elsewhere_on_the_host(home: Path) -> None:
    pic = home / "outside" / "mine.png"
    pic.write_bytes(_PNG)
    assert await _get(_client(None, user="owner"), pic) == 200


@pytest.mark.asyncio
async def test_a_dot_dot_path_out_of_the_store_is_refused(home: Path) -> None:
    (home / "outside" / "private.png").write_bytes(_PNG)
    sneaky = home / "home" / "uploads" / ".." / ".." / "outside" / "private.png"
    assert await _get(_client(GUEST, user="guest@example.test"), sneaky) == 403


@requires_symlinks
@pytest.mark.asyncio
async def test_a_symlink_out_of_the_store_is_refused(home: Path) -> None:
    target = home / "outside" / "private.png"
    target.write_bytes(_PNG)
    link = home / "home" / "uploads" / "link.png"
    link.symlink_to(target)
    assert await _get(_client(GUEST, user="guest@example.test"), link) == 403


@pytest.mark.asyncio
async def test_a_symlinked_store_directory_is_refused(home: Path) -> None:
    (home / "outside" / "private.png").write_bytes(_PNG)
    make_dir_link(home / "home" / "uploads" / "dir", home / "outside")
    path = home / "home" / "uploads" / "dir" / "private.png"
    assert await _get(_client(GUEST, user="guest@example.test"), path) == 403


@pytest.mark.asyncio
async def test_a_nested_path_inside_a_store_is_refused(home: Path) -> None:
    nested = home / "home" / "uploads" / "sub"
    nested.mkdir()
    (nested / "pic.png").write_bytes(_PNG)
    assert await _get(_client(GUEST, user="guest@example.test"), nested / "pic.png") == 403


@pytest.mark.asyncio
async def test_a_session_file_outside_an_attachments_directory_is_refused(home: Path) -> None:
    transcript = home / "home" / "sessions" / "s1.png"
    transcript.write_bytes(_PNG)
    assert await _get(_client(GUEST, user="guest@example.test"), transcript) == 403


@pytest.mark.asyncio
async def test_a_non_guest_dashboard_session_gets_no_attachment_access(home: Path) -> None:
    pic = home / "home" / "uploads" / "abc_pic.png"
    pic.write_bytes(_PNG)
    assert await _get(_client(SLACK_LINK, user="slack-user"), pic) == 403
    assert await _get(_client(DIRECT_PEER, user="tailnet:uid:1"), pic) == 403


@pytest.mark.asyncio
async def test_a_guest_gets_only_pictures_from_a_store(home: Path) -> None:
    text = home / "home" / "uploads" / "notes.txt"
    text.write_bytes(b"secret notes")
    assert await _get(_client(GUEST, user="guest@example.test"), text) == 403


# ── the route table's allow-list ──

_OPEN_ROUTES_FILE = Path(__file__).parent / "data" / "shared_crew_guest_open_routes.txt"


async def _live_route_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> set[tuple[str, str]]:
    """Every ``(METHOD, path)`` the running dashboard registers, apps and slices alike."""
    from test_dashboard_server_startup_coverage import _dashboard

    async with _dashboard(tmp_path, monkeypatch) as (runner, _state, _spies):
        return {
            (route.method, route.resource.canonical)
            for route in runner.app.router.routes()
            if route.resource is not None and route.method not in ("HEAD", "OPTIONS")
        }


def _guest_open_routes(pairs: set[tuple[str, str]]) -> list[str]:
    return sorted(
        f"{method} {path}"
        for method, path in pairs
        if not guest_scope.is_owner_only_path(path, method)
    )


@pytest.mark.asyncio
async def test_the_routes_a_guest_may_use_are_an_explicit_allow_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every route the running dashboard registers is classified.

    A new route is open to a guest unless ``guest_scope`` lists it, so adding one
    must show up here. Add the line to the data file after deciding the route is
    one a guest may use; otherwise list it in ``guest_scope`` and the pinned list above.
    """
    pairs = await _live_route_pairs(tmp_path, monkeypatch)
    assert len(pairs) > 900
    expected = _OPEN_ROUTES_FILE.read_text(encoding="utf-8").split("\n")
    assert _guest_open_routes(pairs) == [line for line in expected if line]


# ── the shared gates ──


@pytest.mark.asyncio
async def test_the_owner_gates_admit_a_guest_on_an_open_route_only() -> None:
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    with patch("kiro_crew.sel.sel", return_value=MagicMock()):
        assert await require_owner_dashboard_request(_Request("/api/members", GUEST), "t") is None
        denied = await require_owner_dashboard_request(_Request("/api/secrets", GUEST), "t")
    assert denied is not None and denied.status == 403


def test_the_apps_proxy_never_names_a_guest_as_the_owner() -> None:
    from kiro_crew.apps.routes_proxy import PRINCIPAL_NONE, _proxy_principal

    request = _Request("/apps/x/api/y", GUEST, auth_from_query_token=False)
    assert _proxy_principal(request) == (PRINCIPAL_NONE, "")


def test_a_guest_socket_is_not_in_the_owner_set() -> None:
    import inspect

    from kiro_crew.dashboard import ws

    source = inspect.getsource(ws._serve_ws)
    assert "state.register_ws(ws, owner=host_owner_request)" in source


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["guest", "owner", "slack-link"])
async def test_a_guest_is_refused_the_gateway_log_stream_over_http(
    monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    """``GET /api/logs`` is the host owner's, as the socket's ``subscribe_logs`` is."""
    from kiro_crew.dashboard.handlers import updates, updates_streams

    stopped = asyncio.Event()
    stopped.set()
    monkeypatch.setattr(updates, "shutdown_event", stopped)

    @web.middleware
    async def identity(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        request["user"] = {"guest": "guest@example.test", "slack-link": "slack-user"}.get(
            who, "owner"
        )
        request["is_dashboard_user"] = True
        # A dashboard user who is neither the host owner nor a guest reads the stream as on main.
        actor = {"guest": GUEST, "slack-link": SLACK_LINK}.get(who)
        if actor is not None:
            request[ACTOR_REQUEST_KEY] = actor
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = MagicMock(owner_id="owner")
    app.router.add_get("/api/logs", updates_streams.api_logs)
    audit = MagicMock()
    with patch("kiro_crew.sel.sel", return_value=audit):
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/logs", params={"lines": "1"})
            body = await resp.json() if who == "guest" else None
    if who == "guest":
        assert resp.status == 403
        assert body is not None and body["code"] == "owner_only"
        # The refusal is audited, as the socket's is.
        assert audit.log_api_access.call_args.kwargs["operation"] == "logs.stream"
        assert audit.log_api_access.call_args.kwargs["outcome"] == "denied"
    else:
        assert resp.status == 200
        assert resp.content_type == "text/event-stream"
        audit.log_api_access.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["guest", "owner", "slack-link"])
async def test_a_guest_socket_is_refused_the_gateway_log_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    """``subscribe_logs`` on the real ``/api/ws`` handler: the owner gets the ring replay
    and joins the live tail; a guest's dashboard socket gets neither."""
    import asyncio
    import json

    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import ws_chunk_scope as chunk_scope
    from kiro_crew.dashboard.handlers import _log_ring, updates
    from kiro_crew.dashboard.ws import api_ws
    from kiro_crew.testing.wait import finished

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    stopped = asyncio.Event()
    monkeypatch.setattr(updates, "shutdown_event", stopped)
    monkeypatch.setattr("kiro_crew.dashboard.ws.shutdown_event", stopped)
    monkeypatch.setattr(
        "kiro_crew.dashboard.ws.load_declared_events_for_connect", lambda _app: (True, frozenset())
    )
    state = _make_state(tmp_path)
    monkeypatch.setattr(state, "status_snapshot", lambda **_kwargs: {})
    ring_before = list(_log_ring)
    _log_ring.append(json.dumps({"message": "gateway log line"}))

    @web.middleware
    async def identity(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        request["user"] = {"guest": "guest@example.test", "slack-link": "slack-user"}.get(
            who, "local-app"
        )
        request["is_dashboard_user"] = True
        request["internal_auth"] = False
        actor = {"guest": GUEST, "slack-link": SLACK_LINK}.get(who)
        if actor is not None:
            request[ACTOR_REQUEST_KEY] = actor
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = state
    app["allowed_origins"] = set()
    app.router.add_get("/api/ws", api_ws)
    try:
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            async with TestClient(TestServer(app)) as client:
                origin = str(client.make_url("/")).rstrip("/")
                app["allowed_origins"].add(origin)
                client.session.headers["Origin"] = origin
                caps = f"slot_patch,{chunk_scope.CHUNK_SCOPE_CAPABILITY}"
                socket = await client.ws_connect("/api/ws", params={"caps": caps})
                await socket.send_json({"type": "subscribe_logs"})
                # Frames are handled in order: the ack proves the subscribe was handled.
                await socket.send_json({"type": chunk_scope.CHUNK_WATCH_FRAME, "slots": []})
                seen: list[dict[str, Any]] = []
                while not seen or seen[-1].get("type") != chunk_scope.CHUNK_WATCH_ACK_FRAME:
                    seen.append(await finished(socket.receive_json(), "the chunk watch ack"))
                logs = [f for f in seen if f.get("type") == "log"]
                subscribed = len(state._ws_log_subscribers)
                await socket.close()
    finally:
        stopped.set()
        _log_ring.clear()
        _log_ring.extend(ring_before)
    if who == "guest":
        assert logs == [] and subscribed == 0
    else:
        assert {"message": "gateway log line"} in [f["data"] for f in logs]
        assert subscribed == 1


def test_a_guest_read_frame_never_clears_the_owners_unread_badges() -> None:
    import inspect

    from kiro_crew.dashboard import ws

    source = inspect.getsource(ws._serve_ws)
    assert 'data.get("read_ts"),\n                            owner=host_owner_request,' in source


@pytest.mark.asyncio
async def test_a_hard_link_planted_in_a_store_is_refused(home: Path) -> None:
    target = home / "outside" / "private.png"
    target.write_bytes(_PNG)
    (home / "home" / "uploads" / "linked.png").hardlink_to(target)
    assert (
        await _get(
            _client(GUEST, user="guest@example.test"), home / "home" / "uploads" / "linked.png"
        )
        == 403
    )


def _swap_at_the_chokepoint_open(path: Path, swap: Any) -> Any:
    """Patch the read chokepoint's own open so *swap* runs right before it opens *path*.

    The chokepoint resolves the name first, so a swap here lands after every check that
    looked at the name and before the descriptor is opened.
    """
    from kiro_crew import platform_compat

    real_open = platform_compat.open_file_no_reparse
    wanted = os.path.realpath(path)
    swapped: list[bool] = []

    def swap_then_open(opened: Any, *a: Any, **kw: Any) -> int:
        if os.fspath(opened) == wanted and not swapped:
            swapped.append(True)
            swap()
        return real_open(opened, *a, **kw)

    return patch.object(platform_compat, "open_file_no_reparse", swap_then_open), swapped


@pytest.mark.asyncio
@pytest.mark.parametrize("link", [pytest.param("symlink", marks=requires_symlinks), "hardlink"])
async def test_a_file_swapped_for_a_link_after_the_check_is_not_followed(
    home: Path, link: str
) -> None:
    pic = home / "home" / "uploads" / "abc_pic.png"
    pic.write_bytes(_PNG)
    target = home / "outside" / "private.png"
    target.write_bytes(_PNG + b"secret")

    def swap() -> None:
        pic.unlink()
        if link == "symlink":
            pic.symlink_to(target)
        else:
            pic.hardlink_to(target)

    patcher, swapped = _swap_at_the_chokepoint_open(pic, swap)
    with patcher:
        assert await _get(_client(GUEST, user="guest@example.test"), pic) == 403
    assert swapped


@requires_symlinks
@pytest.mark.asyncio
async def test_a_link_planted_in_a_store_that_leaves_it_is_refused(home: Path) -> None:
    target = home / "outside" / "private.png"
    target.write_bytes(_PNG)
    (home / "home" / "uploads" / "linked.png").symlink_to(target)
    assert (
        await _get(
            _client(GUEST, user="guest@example.test"), home / "home" / "uploads" / "linked.png"
        )
        == 403
    )


@pytest.mark.asyncio
async def test_a_store_directory_swapped_for_a_link_after_the_check_is_not_followed(
    home: Path,
) -> None:
    pic = home / "home" / "sessions" / "s1.attachments" / "0123456789abcdef_pic.png"
    pic.write_bytes(_PNG)
    elsewhere = home / "outside" / "images"
    elsewhere.mkdir()
    (elsewhere / pic.name).write_bytes(_PNG + b"secret")
    store = pic.parent

    def swap() -> None:
        store.rename(str(store) + ".old")
        make_dir_link(store, elsewhere)

    patcher, swapped = _swap_at_the_chokepoint_open(pic, swap)
    with patcher:
        assert await _get(_client(GUEST, user="guest@example.test"), pic) == 403
    assert swapped


@pytest.mark.parametrize("raw", ["//attacker/share/x.png", "\\\\attacker\\share\\x.png"])
def test_a_network_path_is_refused_before_anything_resolves_it(raw: str) -> None:
    with patch.object(guest_scope.os.path, "realpath") as realpath:
        assert guest_scope.attachment_read(raw, 1024) is None
    realpath.assert_not_called()


# ── schedules and skill trust ──


async def _cron_call(
    actor: ActorContext, method: str, route: str, path: str, handler: Any, body: Any
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    @web.middleware
    async def mw(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        request["user"] = "guest@example.test"
        request["is_dashboard_user"] = True
        request[ACTOR_REQUEST_KEY] = actor
        return await handler(request)

    job = SimpleNamespace(id="j1", job_id="j1", name="j", approval_mode="", channel="C0STORED")
    crons = MagicMock()
    crons.add_job_async = AsyncMock(return_value=job)
    crons.update_job_async = AsyncMock(return_value=job)
    crons.get_job_async = AsyncMock(return_value=job)
    app = web.Application(middlewares=[mw])
    app["state"] = MagicMock(crons=crons, owner_id="owner")
    app.router.add_route(method, route, handler)
    async with TestClient(TestServer(app)) as client:
        resp = await client.request(method, path, json=body)
        payload = await resp.json()
    return resp.status, payload, crons


@pytest.mark.asyncio
async def test_a_guest_cannot_turn_on_auto_approval_when_creating_a_schedule() -> None:
    from kiro_crew.dashboard.handlers import cron as handlers

    body = {"name": "x", "message": "m", "every": 3600, "approval_mode": "auto"}
    status, payload, crons = await _cron_call(
        GUEST, "POST", "/api/crons", "/api/crons", handlers.api_crons_create, body
    )
    assert (status, payload["code"]) == (403, "owner_only")
    crons.add_job_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_guest_cannot_turn_on_auto_approval_when_editing_a_schedule() -> None:
    from kiro_crew.dashboard.handlers import cron as handlers

    status, payload, crons = await _cron_call(
        GUEST,
        "PATCH",
        "/api/crons/{job_id}",
        "/api/crons/j1",
        handlers.api_cron_update,
        {"approval_mode": "auto"},
    )
    assert (status, payload["code"]) == (403, "owner_only")
    crons.update_job_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_guest_creates_a_schedule_on_the_default_mode_stamped_with_their_principal() -> (
    None
):
    from kiro_crew.dashboard.handlers import cron as handlers

    body = {"name": "x", "message": "m", "every": 3600}
    status, _payload, crons = await _cron_call(
        GUEST, "POST", "/api/crons", "/api/crons", handlers.api_crons_create, body
    )
    assert status != 403
    kwargs = crons.add_job_async.await_args.kwargs
    assert kwargs["created_by"] == guest_scope.guest_job_tag(GUEST.principal.id)
    assert "approval_mode" not in kwargs


@pytest.mark.asyncio
async def test_revoking_a_guest_pauses_the_schedules_they_created() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew.shared_crew.routes_invites import pause_revoked_schedules

    theirs = SimpleNamespace(id="j1", created_by=guest_scope.guest_job_tag(GUEST.principal.id))
    others = SimpleNamespace(id="j2", created_by=guest_scope.guest_job_tag("someone-else"))
    owners = SimpleNamespace(id="j3", created_by="")
    crons = MagicMock()
    crons.list_jobs_async = AsyncMock(return_value=[theirs, others, owners])
    crons.enable_job_async = AsyncMock(return_value=True)
    app = {"state": MagicMock(crons=crons)}

    paused = await pause_revoked_schedules(app, frozenset({GUEST.principal.id}))

    assert paused == 1
    crons.enable_job_async.assert_awaited_once_with("j1", enabled=False)


def test_skill_trust_is_closed_to_a_guest() -> None:
    assert guest_scope.is_owner_only_path("/api/skills/-/trust", "POST")
    assert guest_scope.is_owner_only_path("/api/skills/-/trust", "DELETE")
    assert not guest_scope.is_owner_only_path("/api/skills/-/pending")


@pytest.mark.parametrize(
    "path",
    [
        "/api/deploy/deploy",
        "/api/deploy/pending/p1/confirm",
        "/api/deploy/profiles",
        "/api/artifacts/doc/publish",
        "/api/artifacts/doc/publish/refresh",
    ],
)
def test_deploy_and_artifact_publish_are_closed_to_a_guest(path: str) -> None:
    assert guest_scope.is_owner_only_path(path)
    assert not guest_scope.is_owner_only_path("/api/artifacts/doc/versions")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/api/agents/detail/mate"),
        ("PUT", "/api/agents/mate/capabilities"),
        ("DELETE", "/api/agents/mate"),
        ("POST", "/api/workspaces"),
        ("PUT", "/api/chat/slots/s1/environment"),
        ("GET", "/api/chat/slots/s1/environment/browse"),
        ("PUT", "/api/chat/slots/s1/environment/file"),
    ],
)
def test_agent_workspace_and_environment_writes_are_closed_to_a_guest(
    method: str, path: str
) -> None:
    assert guest_scope.is_owner_only_path(path, method)


@pytest.mark.parametrize(
    "path",
    [
        "/api/agents",
        "/api/agents/detail/mate",
        "/api/workspaces",
        "/api/chat/slots/s1/environment",
        "/api/chat/slots/s1/environment/health",
    ],
)
def test_a_guest_still_reads_the_agent_picker_workspaces_and_environment(path: str) -> None:
    assert not guest_scope.is_owner_only_path(path, "GET")


def test_a_guest_turn_never_carries_host_owner_provenance() -> None:
    """The mark a turn inherits (``_directive_owner_origin``) names the host owner only."""
    root = Path(routes_pkg.__file__).parent.parent
    producers = [
        "chat_api/chat_send.py",
        "chat_regenerate.py",
        "chat_rewind.py",
        "chat_api/prompt_queue.py",
        "responsive_chat.py",
    ]
    for name in producers:
        source = (root / name).read_text(encoding="utf-8")
        assert "is_host_owner_dashboard_request(request)" in source, name
    chat_send = (root / "chat_api/chat_send.py").read_text(encoding="utf-8")
    assert "_owner_sent = _person_send and is_host_owner_dashboard_request(request)" in chat_send
    for name in producers[1:]:
        assert "is_owner_dashboard_request(" not in (root / name).read_text(encoding="utf-8"), name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored", [{"approval_mode": "auto"}, {"script": "x.py:f"}, {"command": "ls"}]
)
async def test_a_guest_cannot_edit_an_unattended_schedule(stored: dict[str, str]) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew.dashboard.handlers import cron as handlers

    @web.middleware
    async def mw(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        request["user"] = "guest@example.test"
        request["is_dashboard_user"] = True
        request[ACTOR_REQUEST_KEY] = GUEST
        return await handler(request)

    crons = MagicMock()
    crons.get_job_async = AsyncMock(
        return_value=SimpleNamespace(**{"approval_mode": "", "script": "", "command": "", **stored})
    )
    crons.update_job_async = AsyncMock()
    app = web.Application(middlewares=[mw])
    app["state"] = MagicMock(crons=crons, owner_id="owner")
    app.router.add_patch("/api/crons/{job_id}", handlers.api_cron_update)
    async with TestClient(TestServer(app)) as client:
        resp = await client.patch("/api/crons/j1", json={"message": "new prompt"})
    assert resp.status == 403
    crons.update_job_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_checkpoint_refuses_a_guest_write_to_an_owner_only_route_without_a_handler_gate() -> (
    None
):
    from kiro_crew.dashboard.slot_ownership import slot_ownership_middleware

    @web.middleware
    async def mw(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        request["user"] = "guest@example.test"
        request["is_dashboard_user"] = True
        request[ACTOR_REQUEST_KEY] = GUEST
        return await handler(request)

    reached: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        reached.append(request.path)
        return web.json_response({})

    app = web.Application(middlewares=[mw, slot_ownership_middleware])
    app["state"] = MagicMock(owner_id="owner", _slots={})
    for method, path in (
        ("POST", "/api/themes/install"),
        ("POST", "/api/deploy/deploy"),
        ("GET", "/api/themes/install"),
        ("POST", "/api/auth/logout"),
        ("POST", "/api/members"),
    ):
        app.router.add_route(method, path, handler)
    async with TestClient(TestServer(app)) as client:
        statuses = {
            (m, p): (await client.request(m, p)).status
            for m, p in (
                ("POST", "/api/themes/install"),
                ("POST", "/api/deploy/deploy"),
                ("GET", "/api/themes/install"),
                ("POST", "/api/auth/logout"),
                ("POST", "/api/members"),
            )
        }
    assert statuses == {
        ("POST", "/api/themes/install"): 403,
        ("POST", "/api/deploy/deploy"): 403,
        ("GET", "/api/themes/install"): 200,
        ("POST", "/api/auth/logout"): 200,
        ("POST", "/api/members"): 200,
    }
    assert "/api/themes/install" in reached  # the GET reached its handler


def test_the_browser_snapshot_goes_only_to_the_host_owner_socket() -> None:
    import inspect

    from kiro_crew.dashboard import ws

    source = inspect.getsource(ws._serve_ws)
    assert (
        "if host_owner_request:\n                try:\n                    await _send_browser_live_snapshot"
        in source
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/api/artifacts/doc/sharing"),
        ("POST", "/api/artifacts/doc/overwrite-remote"),
        ("POST", "/api/artifacts/doc/pull-latest"),
        ("GET", "/api/artifacts/doc/upstream-status"),
        ("POST", "/api/themes/install-zip"),
        ("POST", "/api/upload"),
        ("POST", "/api/screenshot"),
        ("GET", "/api/sessions/usage"),
        ("POST", "/api/sessions/usage/refresh"),
        ("GET", "/api/usage/series"),
    ],
)
def test_publishing_installs_native_pickers_and_billing_are_closed_to_a_guest(
    method: str, path: str
) -> None:
    assert guest_scope.is_owner_only_path(path, method)


def test_the_composer_upload_stays_open_to_a_guest() -> None:
    assert not guest_scope.is_owner_only_path("/api/upload/file", "POST")
    assert not guest_scope.is_owner_only_path("/api/upload", "GET")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/mcp-apps/call"),
        ("POST", "/api/projects"),
        ("PUT", "/api/projects/p1"),
    ],
)
def test_tool_calls_and_project_writes_are_closed_to_a_guest(method: str, path: str) -> None:
    assert guest_scope.is_owner_only_path(path, method)
    if not path.startswith("/api/mcp-apps"):
        assert not guest_scope.is_owner_only_path(path, "GET")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/ui-prefs"),
        ("PUT", "/api/ui-prefs"),
        ("GET", "/api/chat/headroom"),
        ("GET", "/api/chat/slots/s1/work-item"),
        ("PATCH", "/api/chat/slots/s1/work-item"),
    ],
)
def test_the_owner_s_dashboard_prefs_and_remote_dispatch_are_closed_to_a_guest(
    method: str, path: str
) -> None:
    # The prefs backup names host paths; a work item and the headroom act for the
    # owner's hub account.
    assert guest_scope.is_owner_only_path(path, method)


@pytest.mark.asyncio
async def test_a_guest_slot_create_cannot_bind_a_hub_work_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard.handlers import remote_work_item

    async def preview_on() -> bool:
        return True

    monkeypatch.setattr(remote_work_item, "_preview_on", preview_on)
    item, refusal = await remote_work_item.work_item_for_create(
        _Request("/api/chat/slots", GUEST), {"work_item": {"id": "w1"}}
    )
    assert item is None
    assert refusal is not None and refusal.status == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/api/themes"), ("PUT", "/api/themes/x"), ("DELETE", "/api/themes/x")],
)
def test_theme_writes_stay_open_to_a_guest(method: str, path: str) -> None:
    # A custom theme is the Crew's own look, not the host's: with no roles file every
    # admitted person keeps equal rights (the one-role-per-person decision under
    # docs/decisions). Installing a theme pack unpacks host code, and stays the owner's.
    assert not guest_scope.is_owner_only_path(path, method)
    assert guest_scope.is_owner_only_path("/api/themes/install", "POST")
    assert guest_scope.is_owner_only_path("/api/themes/install-zip", "POST")


@pytest.mark.parametrize(
    "posture",
    ["yolo", "trust", "trust_reads", "trust_command", "trust_base", "approved_trust_reads", "auto"],
)
def test_a_guest_cannot_turn_on_an_unattended_approval_posture(posture: str) -> None:
    refusal = guest_scope.guest_posture_refusal(_Request("/api/chat/mode", GUEST), posture)
    assert refusal is not None and refusal.status == 403


@pytest.mark.parametrize("posture", ["normal", "approved", "rejected", ["yolo"], None])
def test_a_guest_still_approves_and_rejects_a_call(posture: object) -> None:
    assert guest_scope.guest_posture_refusal(_Request("/api/chat/mode", GUEST), posture) is None


def test_the_host_owner_keeps_every_approval_posture() -> None:
    request = _Request("/api/chat/mode", None, user="owner")
    assert guest_scope.guest_posture_refusal(request, "yolo") is None


@pytest.mark.asyncio
async def test_a_guest_gets_no_auto_approve_on_a_task_run() -> None:
    from kiro_crew.dashboard.handlers.taskrunner import _gate_auto_approve

    request = _Request("/api/taskrunner", GUEST)
    with patch("kiro_crew.dashboard.handlers.taskrunner._sel", return_value=MagicMock()):
        granted = await _gate_auto_approve(request, True, "dashboard", "start")
    assert granted is False


def test_only_signing_out_and_refreshing_are_exempt_from_the_central_refusal() -> None:
    for path, exempt in (
        ("/api/auth/logout", True),
        ("/api/auth/refresh", True),
        ("/api/logout", True),
        ("/api/auth/mobile-link", False),
    ):
        request = _Request(path, GUEST)
        denial = guest_scope.guest_write_denial(request)
        assert (denial is None) is exempt, path


@pytest.mark.asyncio
async def test_auth_me_tells_the_dashboard_a_guest_is_a_guest() -> None:
    import json

    from kiro_crew.dashboard.handlers.auth_refresh import api_auth_me

    class _Me(_Request):
        cookies: dict[str, str] = {}
        headers: dict[str, str] = {}
        host = "127.0.0.1:1"
        url = MagicMock()

    guest_body = None
    with patch(
        "kiro_crew.dashboard.handlers.auth_refresh.validate_refresh_token",
        return_value=(False, "", "", "", "", 0.0),
    ):
        for actor, expected in ((GUEST, True), (None, False)):
            request = _Me("/api/auth/me", actor)
            request["auth_token"] = ""
            resp = await api_auth_me(request)
            guest_body = json.loads(resp.text)
            assert guest_body["is_guest"] is expected


def test_a_guest_schedule_shows_the_guest_by_name_and_never_the_stamp() -> None:
    from kiro_crew.shared_crew import people

    people.reset_for_tests()
    people.observe(GUEST.principal)
    tag = guest_scope.guest_job_tag(GUEST.principal.id)
    assert guest_scope.guest_creator_name(tag) == "guest@example.test"
    assert guest_scope.guest_creator_name(guest_scope.guest_job_tag("p_unseen")) == ""
    assert guest_scope.guest_creator_name("") is None
    assert guest_scope.guest_creator_name("app:papyrus") is None
    assert guest_scope.guest_creator_name("U0SLACKUSER") is None
