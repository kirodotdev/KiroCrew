"""An unknown slot gets the shared ``slot_not_found()`` 404 from these handlers.

``slot_ownership.slot_not_found()`` is the 404 the app-isolation refusals answer
with. The handlers below answer a slot that does not exist through the same
helper, so their response is pinned to the helper's rather than to a literal: a
handler whose 404 differs from the helper's, or that calls a name its module does
not bind, fails here. A source scan keeps the dashboard from growing a new
hand-built copy of that 404 beside the helper.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard import chat_folders, chat_fork, chat_slack
from kiro_crew.dashboard import handlers as handlers_pkg
from kiro_crew.dashboard import slot_ownership
from kiro_crew.dashboard.handlers import prompts_caller_gates, side
from kiro_crew.dashboard.routes import chat as routes_chat
from kiro_crew.dashboard.slot_ownership import slot_not_found

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _request(method: str, path: str) -> web.Request:
    app = web.Application()
    app["state"] = SimpleNamespace(_slots={}, get_slot=lambda _name: None)
    return make_mocked_request(method, path, match_info={"slot": "nope"}, app=app)


def _shape(resp: Any) -> tuple[int, bytes, str]:
    return resp.status, resp.body, resp.content_type


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "method", "path"),
    [
        (chat_folders.api_chat_slot_muted, "PATCH", "/api/chat/slots/nope/muted"),
        (chat_folders.api_chat_slot_mutes_opened, "PATCH", "/api/chat/slots/nope/mutes-opened"),
        (chat_slack.api_chat_slot_slack_pause, "POST", "/api/chat/slots/nope/slack-pause"),
        (side.api_side_stop, "POST", "/api/chat/slots/nope/side/stop"),
        (chat_fork.api_chat_slot_fork, "POST", "/api/chat/slots/nope/fork"),
    ],
    ids=["muted", "mutes-opened", "slack-pause", "side-stop", "fork"],
)
async def test_an_unknown_slot_gets_the_shared_not_found(
    handler: Handler, method: str, path: str
) -> None:
    resp = await handler(_request(method, path))
    assert _shape(resp) == _shape(slot_not_found())


@pytest.mark.asyncio
async def test_the_owner_card_read_gets_the_shared_not_found() -> None:
    # The card read refuses anyone but the dashboard owner before it looks
    # the slot up, so only the owner's request reaches the lookup.
    request = _request("GET", "/api/chat/slots/nope/dashboard-card")
    request["app"] = ""
    request["user"] = "local-app"
    resp = await routes_chat.api_dashboard_card(request)
    assert _shape(resp) == _shape(slot_not_found())


def test_an_app_naming_no_slot_gets_the_shared_not_found_from_the_skill_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handlers_pkg, "sel", lambda: SimpleNamespace(log_api_access=lambda **_kw: None)
    )
    request = _request("GET", "/api/skills")
    request["app"] = "some-app"
    state = SimpleNamespace(_slots={}, get_slot=lambda _name: None)
    resp = prompts_caller_gates._deny_foreign_app_skill_slot(request, state, "", "skills.read")
    assert resp is not None and _shape(resp) == _shape(slot_not_found())


def _is_hand_built_slot_404(node: ast.Call) -> bool:
    if not (isinstance(node.func, ast.Attribute) and node.func.attr == "json_response"):
        return False
    if not node.args or not isinstance(node.args[0], ast.Dict):
        return False
    body = {
        key.value: value
        for key, value in zip(node.args[0].keys, node.args[0].values)
        if isinstance(key, ast.Constant)
    }
    if set(body) != {"error", "code"}:
        return False
    error, code = body["error"], body["code"]
    if not (isinstance(error, ast.Constant) and error.value == "not found"):
        return False
    if isinstance(code, ast.Constant):
        return code.value == "slot_not_found"
    return isinstance(code, ast.Name) and code.id.endswith("SLOT_NOT_FOUND")


def test_no_dashboard_module_hand_builds_the_slot_404() -> None:
    helper_file = Path(slot_ownership.__file__)
    hits = []
    for path in sorted(helper_file.parent.rglob("*.py")):
        if path == helper_file:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_hand_built_slot_404(node):
                hits.append(f"{path.relative_to(helper_file.parent)}:{node.lineno}")
    assert hits == [], f"call slot_ownership.slot_not_found() instead: {hits}"
