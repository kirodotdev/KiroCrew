"""A delegated (``via_app``) token reaches only the rows of ``delegated_scope.READ_ALLOW``.

This walks every route the gateway mounts, with its method: the page table, the MCP tool
routes, the dist and static routes, and the routes an edition contributes. Each (method,
route) is either a ``READ_ALLOW`` row or refused at runtime, and a row names a real route,
its real handler (``file:line``) and what it does. A WebSocket, an SSE stream or a file
download is never a row. The prompt and the card text the owner reads say what the rows
allow, and are pinned to them here.
"""

from __future__ import annotations

import inspect
import re
import typing
from pathlib import Path

import pytest
from aiohttp import web

from kiro_crew.dashboard.delegated_scope import READ_ALLOW, Row, allowed, row_for
from kiro_crew.dashboard.routes import register_all

_REPO = Path(__file__).resolve().parent.parent


def _table() -> web.Application:
    """Every route the gateway mounts, from each place that registers one."""
    from kiro_crew.dashboard.server import _register_mcp_routes
    from kiro_crew.dashboard.server_runtime.static_assets import _register_dist_static_routes
    from kiro_crew.platform.context import current_context, safe_context_call

    app = web.Application()
    _register_mcp_routes(app)
    register_all(app)
    safe_context_call(
        lambda: current_context().dashboard.contribute_routes(app),
        fallback=None,
        log_message="the edition routes could not be composed for the walk",
    )
    _register_dist_static_routes(app, _REPO / "src" / "kiro_crew" / "static" / "dist")
    app.router.add_static("/static", _REPO / "src" / "kiro_crew" / "static")
    return app


def _walk() -> list[tuple[str, str, object]]:
    out = []
    for route in _table().router.routes():
        resource = route.resource
        assert resource is not None
        out.append((route.method, resource.canonical, route.handler))
    return out


def test_the_walk_reaches_every_registration_source() -> None:
    seen = {(m, p) for m, p, _h in _walk()}
    for expected in [
        ("GET", "/api/ws"),  # the page table
        ("GET", "/api/ws/stt"),
        ("GET", "/api/stream"),
        ("POST", "/api/chat/slots/{slot}/stop"),
        ("GET", "/assets/{tail}"),  # the dist routes
        ("*", "/apps/{name}/api/{path}"),  # the app proxy
    ]:
        assert expected in seen, expected
    assert any(m == "GET" and p.startswith("/static") for m, p in seen), "the /static route"
    assert any(p.startswith("/api/agent-ask") for _m, p in seen), "the MCP tool routes"


def test_a_route_is_a_read_allow_row_or_refused_and_nothing_else() -> None:
    """The decision is one lookup: no route is allowed by a tag, a prefix or a guess."""
    open_routes = {(m, p) for m, p, _h in _walk() if allowed(m, p) and m != "HEAD"}
    rows = {(m, p) for (m, p) in READ_ALLOW if m != "*"}
    star = {p for (m, p) in READ_ALLOW if m == "*"}
    assert {(m, p) for m, p in open_routes if m != "*" and p not in star} == rows
    assert {p for m, p in open_routes if p in star} == star


def test_every_row_names_a_route_that_exists() -> None:
    real = {(m, p) for m, p, _h in _walk()}
    for method, canonical in READ_ALLOW:
        if method == "*":
            assert any(p == canonical for _m, p in real), canonical
        else:
            assert (method, canonical) in real, (method, canonical)


def test_an_unlisted_route_or_method_is_refused() -> None:
    assert row_for("GET", "/api/a-route-nobody-listed") is None
    assert row_for("PUT", "/api/ui-prefs") is None, "the PUT of a row's path"
    assert row_for("POST", "/api/auth/me") is None
    assert row_for("DELETE", "/api/chat/slots/{slot}/stop") is None
    assert row_for("HEAD", "/api/auth/me") is not None, "HEAD follows GET"


#: The whole list. A row here is a security decision and the owner's words change with it.
_EXPECTED = {
    ("GET", "/"),
    ("GET", "/api/auth/me"),
    ("GET", "/api/kiro-prerequisite"),
    ("GET", "/api/dashboard/branding"),
    ("GET", "/api/theme/boot"),
    ("GET", "/api/chat/slots"),
    ("POST", "/api/auth/refresh"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/chat/slots/{slot}/stop"),
    ("*", "/apps/{name}/api/{path}"),
    ("*", "/app-frame-fetch/{name}/{path}"),
}


def test_read_allow_is_exactly_the_reviewed_set() -> None:
    assert set(READ_ALLOW) == _EXPECTED


def test_every_row_carries_its_handlers_file_and_line_and_an_effect_note() -> None:
    handlers = {(m, p): h for m, p, h in _walk()}
    for (method, canonical), row in READ_ALLOW.items():
        handler = handlers.get((method, canonical)) or next(
            h for (m, p), h in handlers.items() if p == canonical
        )
        func = inspect.unwrap(handler)  # type: ignore[arg-type]
        where = f"{Path(inspect.getsourcefile(func) or '').relative_to(_REPO)}:"
        where += str(inspect.getsourcelines(func)[1])
        assert row.handler == where, f"{method} {canonical}: the handler is now at {where}"
        assert len(row.effect) > 30 and "\n" not in row.effect, canonical


#: A source that opens a stream, a socket or a file download.
_NOT_PLAIN = re.compile(
    r"StreamResponse\(|WebSocketResponse\(|EventSource|text/event-stream|FileResponse\(|"
    r"\.prepare\(|sendfile|read_bytes\("
)


def test_an_allowed_get_answers_a_plain_response_never_a_stream_or_a_file() -> None:
    handlers = {(m, p): h for m, p, h in _walk()}
    for (method, canonical), row in READ_ALLOW.items():
        if method != "GET" or row.stream:
            continue
        func = inspect.unwrap(handlers[(method, canonical)])  # type: ignore[arg-type]
        # ``from __future__ import annotations`` stores strings; resolve them without eval().
        returns = typing.get_type_hints(func).get("return")
        assert inspect.isclass(returns) and issubclass(
            returns, web.Response
        ), f"GET {canonical}: the handler is annotated {returns!r}, not web.Response"
        assert not _NOT_PLAIN.search(inspect.getsource(func)), f"GET {canonical} opens a stream"


def test_only_the_two_app_routes_may_stay_open() -> None:
    assert {(m, p) for (m, p), row in READ_ALLOW.items() if row.stream} == {
        ("*", "/apps/{name}/api/{path}"),
        ("*", "/app-frame-fetch/{name}/{path}"),
    }
    assert all(row.own_app for row in READ_ALLOW.values() if row.stream)


@pytest.mark.parametrize(
    "method,canonical",
    [
        ("GET", "/api/ws"),
        ("GET", "/api/ws/stt"),
        ("GET", "/api/stream"),
        ("GET", "/api/ws/terminal/{session_id}"),
        ("GET", "/api/sessions/{id}/agents/{agent_id}/stream"),
        ("GET", "/api/apps/auto-research/campaigns/{id}/stream"),
        ("GET", "/api/file-stream"),
        ("GET", "/api/file-watch"),
    ],
)
def test_no_socket_or_stream_route_is_open(method: str, canonical: str) -> None:
    assert (method, canonical) in {(m, p) for m, p, _h in _walk()}, "the route does not exist"
    assert allowed(method, canonical) is False


def test_a_handler_that_opens_a_socket_or_stream_is_not_a_row() -> None:
    """Every registered handler that builds a socket or a stream is refused, rows aside."""
    streams = []
    for method, canonical, handler in _walk():
        if (
            not allowed(method, canonical)
            or READ_ALLOW.get((method, canonical), Row("", "")).stream
        ):
            continue
        try:
            source = inspect.getsource(inspect.unwrap(handler))  # type: ignore[arg-type]
        except (OSError, TypeError):
            continue
        if re.search(r"WebSocketResponse\(|text/event-stream", source):
            streams.append(f"{method} {canonical}")
    assert not streams, streams


_REFUSED = [
    ("GET", "/api/token/local"),
    ("POST", "/api/auth/mobile-link"),
    ("POST", "/api/apps/{name}/token"),
    ("POST", "/api/apps/register"),
    ("POST", "/api/webhooks/tokens"),
    ("POST", "/api/relay-viewer/token"),
    ("POST", "/api/apps/install"),
    ("POST", "/api/apps/{name}/enable"),
    ("POST", "/api/apps/{name}/disable"),
    ("POST", "/api/apps/{name}/uninstall"),
    ("PUT", "/api/apps/{name}/bot-grants"),
    ("PUT", "/api/config/kirocrew"),
    ("POST", "/api/security/tool-grants"),
    ("POST", "/v1/crew/owner/approvals/{approval_id}/decision"),
    ("POST", "/api/restart"),
    ("POST", "/api/terminal/sessions"),
    ("GET", "/api/ws/terminal/{session_id}"),
    ("PUT", "/api/agent/config"),
    ("POST", "/api/skills"),
    ("POST", "/api/chat"),
    ("POST", "/api/chat/slots"),
    ("POST", "/api/chat/slots/{slot}/interrupt"),
    ("POST", "/api/chat/pinned-order"),
    ("POST", "/api/chat/folders/reorder"),
    ("POST", "/api/chat/manual-order"),
    ("PUT", "/api/ui-prefs"),
    ("GET", "/api/ui-prefs"),  # names real host paths; the shell boots with defaults
    ("GET", "/api/chat/slots/{slot}"),
    ("GET", "/api/version"),
    ("GET", "/api/status"),
    ("GET", "/api/sessions"),
    ("GET", "/api/apps/{name}/config"),
    ("GET", "/api/apps/{name}/manifest"),
    ("GET", "/app-frame/{name}/{point}/{point_id}/{frame_id}/{token}"),
    ("GET", "/api/file-read"),
    ("GET", "/api/portability/export"),
    ("GET", "/api/memory/provider/export"),
    ("GET", "/api/mcp/custom/{name}"),
    ("GET", "/api/slack/config"),
    ("GET", "/api/connections/mint"),
]


@pytest.mark.parametrize("method,canonical", _REFUSED)
def test_the_credential_control_plane_and_turn_starting_routes_are_refused(
    method: str, canonical: str
) -> None:
    assert (method, canonical) in {(m, p) for m, p, _h in _walk()}, "the route does not exist"
    assert allowed(method, canonical) is False


def test_a_star_row_opens_every_method_of_its_own_route_only() -> None:
    for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
        assert allowed(method, "/apps/{name}/api/{path}")
        assert allowed(method, "/app-frame-fetch/{name}/{path}")
        assert not allowed(method, "/apps/{name}/ui/{path}")


# --- What the owner reads is what the rows allow ------------------------------------------


def test_the_prompt_and_the_card_say_what_the_rows_allow_and_nothing_more() -> None:
    import json

    from kiro_crew.apps.dashboard_proxy import DASHBOARD_ACCESS_SCOPE

    low = DASHBOARD_ACCESS_SCOPE.lower()
    # One phrase per row family, so a row added without its words fails here.
    assert "dashboard page" in low and "list of your chats" in low  # the GET rows
    assert "titles" in low and "running" in low and "last changed" in low  # the list projection
    assert "stop a running chat turn" in low  # POST /api/chat/slots/{slot}/stop
    assert "its own backend" in low  # the two app rows
    for word in (
        "chat messages",
        "preferences",
        "settings",
        "credentials",
        "terminal",
        "approvals",
    ):
        assert word in low.split("cannot", 1)[1], word
    assert "turning the app off removes this access" in low
    for promised in (
        "read your chats",
        "read your sessions",
        "running agents",
        "interrupt",
        "your preferences and",
    ):
        assert promised not in low, promised
    card = json.loads(
        (_REPO / "website" / "src" / "i18n" / "locales" / "en.manual.json").read_text("utf-8")
    )
    scopes: list[str] = []

    def _find(node: object) -> None:
        if isinstance(node, dict):
            if "dashboard_access_scope" in node:
                scopes.append(node["dashboard_access_scope"])
            for value in node.values():
                _find(value)

    _find(card)
    assert scopes == [DASHBOARD_ACCESS_SCOPE]
