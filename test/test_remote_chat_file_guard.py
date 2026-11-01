"""The gateway's file, diff and Git routes refuse a chat that runs remotely.

``kiro_crew.dashboard.remote_chat_guard`` wraps each route that reads or writes this
machine's files. These rows register the real route table over stand-in handlers, so
each one shows which requests reach the route and which are refused before it runs:
a remote chat named by ``?slot=`` is refused on every guarded route; a local chat, a
request naming no chat, an unknown chat, a non-owner caller and the developer flag
off all reach the route exactly as before; and the refusal names no path.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard import chat_utils, handlers
from kiro_crew.dashboard import remote_chat_guard as guard
from kiro_crew.dashboard.handlers import source_providers
from kiro_crew.dashboard.routes import taskrunner

pytestmark = pytest.mark.asyncio

#: Route -> (handler name, method). Each one serves a path on this machine.
GUARDED = {
    "/api/file-read": ("api_file_read", "GET"),
    "/api/file-raw": ("api_file_raw", "GET"),
    "/api/file-stream": ("api_file_stream", "GET"),
    "/api/file-watch": ("api_file_watch", "GET"),
    "/api/file-write": ("api_file_write", "POST"),
    "/api/file-diff": ("api_file_diff", "GET"),
    "/api/project/git": ("api_project_git", "GET"),
    "/api/project/git/status": ("api_project_git_status", "GET"),
    "/api/project/git/log": ("api_project_git_log", "GET"),
}
SECRET_PATH = "/srv/owner-private/notes/plan.md"
REMOTE, LOCAL = "chat-remote", "chat-local"


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch):
    """The registered routes over stand-in handlers, with one remote and one local chat."""
    reached: list[str] = []

    def stand_in(name: str):
        async def handler(request: web.Request) -> web.Response:
            reached.append(name)
            return web.json_response({"served": name})

        handler.__name__ = name
        return handler

    for name, _method in GUARDED.values():
        monkeypatch.setattr(handlers, name, stand_in(name))
    app = web.Application()
    taskrunner.register(app)
    slots = {REMOTE: SimpleNamespace(key=REMOTE), LOCAL: SimpleNamespace(key=LOCAL)}
    app["state"] = SimpleNamespace(get_slot=slots.get)
    monkeypatch.setattr(chat_utils, "effective_session_key", lambda slot: slot.key)
    monkeypatch.setattr("kiro_crew.session_environment.session_environments_enabled", lambda: True)
    monkeypatch.setattr(
        "kiro_crew.session_environment.session_runs_remotely", lambda key: key == REMOTE
    )
    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda request: True)
    routes = {
        (route.method, route.resource.canonical): route.handler
        for route in app.router.routes()
        if route.resource is not None
    }
    return SimpleNamespace(app=app, routes=routes, reached=reached)


async def _call(table, path: str, query: str) -> web.Response:
    name, method = GUARDED[path]
    request = make_mocked_request(method, f"{path}?path={SECRET_PATH}{query}", app=table.app)
    return await table.routes[(method, path)](request)


@pytest.mark.parametrize("path", sorted(GUARDED))
async def test_a_remote_chat_is_refused_before_the_route_runs(table, path: str) -> None:
    response = await _call(table, path, f"&slot={REMOTE}")
    assert response.status == 409
    body = json.loads(response.body)
    assert body["code"] == guard.CHAT_RUNS_REMOTELY
    assert table.reached == []


@pytest.mark.parametrize("path", sorted(GUARDED))
@pytest.mark.parametrize("query", [f"&slot={LOCAL}", "", "&slot=", "&slot=chat-unknown"])
async def test_a_local_unknown_or_unnamed_chat_reaches_the_route(
    table, path: str, query: str
) -> None:
    response = await _call(table, path, query)
    assert json.loads(response.body) == {"served": GUARDED[path][0]}


@pytest.mark.parametrize("path", sorted(GUARDED))
async def test_with_the_developer_flag_off_a_remote_chat_reaches_the_route(
    table, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never_read(key: str) -> bool:
        raise AssertionError("placement read with the flag off")

    monkeypatch.setattr("kiro_crew.session_environment.session_environments_enabled", lambda: False)
    monkeypatch.setattr("kiro_crew.session_environment.session_runs_remotely", never_read)
    response = await _call(table, path, f"&slot={REMOTE}")
    assert json.loads(response.body) == {"served": GUARDED[path][0]}


async def test_a_non_owner_gets_the_routes_own_answer_not_the_chats_placement(
    table, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda request: False)
    response = await _call(table, "/api/file-read", f"&slot={REMOTE}")
    assert json.loads(response.body) == {"served": "api_file_read"}


async def test_an_unreadable_placement_is_refused(table, monkeypatch: pytest.MonkeyPatch) -> None:
    """``session_runs_remotely`` answers True for an unreadable store; the guard keeps it."""
    monkeypatch.setattr("kiro_crew.session_environment.session_runs_remotely", lambda key: True)
    response = await _call(table, "/api/file-diff", f"&slot={LOCAL}")
    assert response.status == 409


async def test_the_refusal_names_no_path(table) -> None:
    response = await _call(table, "/api/file-read", f"&slot={REMOTE}")
    text = response.body.decode()
    assert SECRET_PATH not in text and "owner-private" not in text
    assert "/" not in json.loads(text)["error"]


async def test_the_repository_list_and_other_routes_are_not_wrapped(table) -> None:
    """``git/repos`` answers a remote chat with an empty list itself; the rest are not chat-scoped."""
    for (method, path), name in {
        ("GET", "/api/project/git/repos"): "api_project_git_repos",
        ("GET", "/api/file-search"): "api_file_search",
    }.items():
        assert table.routes[(method, path)] is getattr(handlers, name), path
