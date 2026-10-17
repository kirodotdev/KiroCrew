"""The standalone Python client's calls stay registered by the real Gateway."""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest
from aiohttp import web

from kiro_crew.dashboard.chat_handlers import (
    api_chat_mode,
    api_chat_slot_approve,
    apply_approval_mode,
)
from kiro_crew.dashboard.handlers.cron import api_crons, api_lessons
from kiro_crew.dashboard.handlers.memory import api_memory_episodic_search
from kiro_crew.dashboard.handlers.messaging import api_spawn_list
from kiro_crew.dashboard.routes import register_all
from kiro_crew.dashboard.server import _register_mcp_routes

_CLIENT_SOURCE = (
    Path(__file__).parents[1] / "packages" / "kirocrew-client-py" / "kirocrew_client" / "client.py"
)
_HELPER_METHODS = {
    "_get": "GET",
    "_post": "POST",
    "_put": "PUT",
    "_patch": "PATCH",
    "_delete": "DELETE",
    "_delete_with_body": "DELETE",
}
_EXPECTED_CLIENT_ROUTES = {
    ("POST", "/api/apps/{}/token"),
    ("GET", "/api/status"),
    ("GET", "/api/system"),
    ("POST", "/api/chat/slots"),
    ("GET", "/api/chat/slots"),
    ("DELETE", "/api/chat/slots/{}"),
    ("GET", "/api/chat/slots/{}"),
    ("POST", "/api/chat/slots/{}/stop"),
    ("POST", "/api/chat/slots/{}/edit-resend"),
    ("POST", "/api/chat"),
    ("POST", "/api/spawn"),
    ("GET", "/api/spawn"),
    ("GET", "/api/spawn/{}"),
    ("POST", "/api/crons"),
    ("GET", "/api/crons"),
    ("PATCH", "/api/crons/{}"),
    ("DELETE", "/api/crons/{}"),
    ("POST", "/api/crons/{}/enable"),
    ("POST", "/api/lessons"),
    ("GET", "/api/lessons"),
    ("DELETE", "/api/lessons"),
    ("POST", "/api/send-message"),
    ("GET", "/api/notifications"),
    ("POST", "/api/notifications/ack"),
    ("POST", "/api/notifications/ack-all"),
    ("GET", "/api/approvals"),
    ("POST", "/api/chat/slots/{}/approve"),
    ("POST", "/api/approvals/{}/{}"),
    ("POST", "/api/chat/mode"),
    ("GET", "/api/models"),
    ("POST", "/api/chat/slots/{}/model"),
    ("GET", "/api/config/{}"),
    ("PUT", "/api/config/{}"),
    ("POST", "/api/stt/transcribe"),
    ("GET", "/api/ws"),
    ("GET", "/api/mcp"),
    ("PUT", "/api/mcp/servers/{}"),
    ("DELETE", "/api/mcp/servers/{}"),
    ("GET", "/api/apps/{}/config"),
    ("PUT", "/api/apps/{}/config"),
    ("GET", "/api/memory/episodic/search"),
    ("POST", "/api/chat/slots/{}/context"),
}
_CONFIG_PATHS = {"kirocrew", "stt", "theme", "default-agent"}


def _path_template(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = node.value
    elif isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                parts.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                if isinstance(part.value, ast.Name) and part.value.id == "qs":
                    continue
                parts.append("{}")
        value = "".join(parts)
    else:
        return None
    api_start = value.find("/api/")
    if api_start < 0:
        return None
    return value[api_start:].split("?", 1)[0]


def _client_routes_from_source() -> set[tuple[str, str]]:
    source = _CLIENT_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    routes: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = _HELPER_METHODS.get(node.func.attr)
        if method is None and node.func.attr == "post":
            method = "POST"
        if method is None or not node.args:
            continue
        path = _path_template(node.args[0])
        if path is not None:
            routes.add((method, path))
    assert '"/api/ws"' in source, "WebSocket path disappeared from create_ws"
    routes.add(("GET", "/api/ws"))
    assert routes, "client route extraction must not pass with an empty inventory"
    return routes


def _public_async_route_methods(source: str) -> tuple[set[str], dict[str, set[tuple[str, str]]]]:
    """Return public wrappers with route calls and the routes extracted per wrapper."""
    tree = ast.parse(source)
    client = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "KiroCrewClient"
    )
    callers: set[str] = set()
    routes: dict[str, set[tuple[str, str]]] = {}
    for wrapper in client.body:
        if not isinstance(wrapper, ast.AsyncFunctionDef) or wrapper.name.startswith("_"):
            continue
        for call in ast.walk(wrapper):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            method = _HELPER_METHODS.get(call.func.attr)
            is_session_post = (
                call.func.attr == "post"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "session"
            )
            if method is None and is_session_post:
                method = "POST"
            if method is None:
                continue
            callers.add(wrapper.name)
            if not call.args:
                continue
            path = _path_template(call.args[0])
            if path is not None:
                routes.setdefault(wrapper.name, set()).add((method, path))
    return callers, routes


def test_every_public_async_route_wrapper_contributes_an_extracted_route() -> None:
    source = _CLIENT_SOURCE.read_text(encoding="utf-8")
    callers, routes = _public_async_route_methods(source)
    missing = sorted(callers - routes.keys())
    assert not missing, f"Python client wrappers have unextractable routes: {missing}"


def _normalize_gateway_path(path: str) -> str:
    return re.sub(r"\{[^{}]+\}", "{}", path)


def test_python_client_routes_exist_in_the_real_gateway_router() -> None:
    client_routes = _client_routes_from_source()
    assert client_routes == _EXPECTED_CLIENT_ROUTES

    app = web.Application()
    _register_mcp_routes(app)
    register_all(app)
    gateway_routes = {
        (route.method, _normalize_gateway_path(str(route.resource.canonical)))
        for route in app.router.routes()
        if route.method != "HEAD" and route.resource is not None
    }

    missing: set[tuple[str, str]] = set()
    for method, path in client_routes:
        if path == "/api/config/{}":
            missing.update(
                (method, f"/api/config/{key}")
                for key in _CONFIG_PATHS
                if (method, f"/api/config/{key}") not in gateway_routes
            )
        elif (method, path) not in gateway_routes:
            missing.add((method, path))

    assert not missing, f"Python client routes missing from Gateway: {sorted(missing)}"


def _client_frozenset(name: str) -> set[str]:
    tree = ast.parse(_CLIENT_SOURCE.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        assert isinstance(node.value, ast.Call)
        assert isinstance(node.value.func, ast.Name) and node.value.func.id == "frozenset"
        assert len(node.value.args) == 1
        value = ast.literal_eval(node.value.args[0])
        assert isinstance(value, set) and value
        return value
    raise AssertionError(f"client constant {name} not found")


def _executable_string_literals(handler: object) -> set[str]:
    tree = ast.parse(inspect.getsource(handler))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.add(id(first.value))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }


@pytest.mark.parametrize(
    ("handler", "key"),
    [
        (api_spawn_list, "agents"),
        (api_crons, "jobs"),
        (api_lessons, "lessons"),
        (api_memory_episodic_search, "results"),
    ],
)
def test_gateway_response_envelopes_match_client_reads(handler: object, key: str) -> None:
    assert key in _executable_string_literals(handler)


@pytest.mark.parametrize(
    ("client_constant", "handler"),
    [
        ("_SLOT_APPROVAL_ACTIONS", api_chat_slot_approve),
        # The handler checks the caller, then hands the mutation to
        # apply_approval_mode (shared with the crewmate profile write).
        ("_APPROVAL_MODES", (api_chat_mode, apply_approval_mode)),
    ],
)
def test_gateway_approval_vocabulary_matches_client(client_constant: str, handler: object) -> None:
    client_values = _client_frozenset(client_constant)
    handlers = handler if isinstance(handler, tuple) else (handler,)
    handled = set().union(*(_executable_string_literals(h) for h in handlers))
    missing = client_values - handled
    assert not missing, f"{client_constant} values missing from Gateway handler: {sorted(missing)}"
