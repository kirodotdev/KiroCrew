"""A change card that empties a list has a spelling that reaches the server.

A client that drops empty containers delivers ``steering_dirs: []`` and
``fields: {"allowedTools": []}`` with the key missing, so ``folder.steering`` takes
``clear: true`` and ``template.update`` takes ``clear: [<list field>]``. The catalog
normalizes both to the plain empty list, so the preview, the apply step, the person's
edit and Undo read one shape.

The catalog half is pure. The end-to-end half runs the ``propose_change`` tool from its
own entry (schema validation and handler) into the REAL card routes on a loopback test
server, with the tool's transport bridged to that server, and lets the owner apply a
``folder.steering`` card through the REAL folder route.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_change_card_folder_steering import SK, _needs_pinned_walk, _State, _step

from kiro_crew import agent
from kiro_crew import change_card_catalog as catalog
from kiro_crew import mcp_core, mcp_guide
from kiro_crew.dashboard.chat_folders import api_chat_folder_update
from kiro_crew.dashboard.handlers import change_cards as routes

STEERING = "folder.steering"
TEMPLATE = "template.update"
#: The internal credential the test server accepts from the tool's bridged transport.
SECRET = "test-internal-secret"


@pytest.fixture(autouse=True)
def _guide_transport_refuses(_floor_monkeypatch):
    """``mcp_guide`` binds ``_get``/``_post`` at import, below the conftest's ``_post``
    recorder, so an unbridged call here would dial whatever gateway is listening. Each
    end-to-end test installs its own bridge; anything else fails here, locally."""

    def _refuse(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("propose_change reached a transport this test did not bridge")

    _floor_monkeypatch.setattr(mcp_guide, "_get", _refuse)
    _floor_monkeypatch.setattr(mcp_guide, "_post", _refuse)
    _floor_monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: SK)


# ── catalog ──


@pytest.mark.parametrize(
    "params",
    [
        {"folder": "f1", "clear": True},
        {"folder": "f1", "clear": True, "steering_dirs": []},
    ],
)
def test_folder_clear_normalizes_to_the_empty_list(params):
    assert catalog.validate_params(STEERING, params) == {"folder": "f1", "steering_dirs": []}
    assert catalog.validate_params(STEERING, params) == catalog.validate_params(
        STEERING, {"folder": "f1", "steering_dirs": []}
    )


def test_folder_list_without_clear_is_unchanged():
    for flag in ({}, {"clear": False}):
        assert catalog.validate_params(
            STEERING, {"folder": "f1", "steering_dirs": ["/a"], **flag}
        ) == {
            "folder": "f1",
            "steering_dirs": ["/a"],
        }


@pytest.mark.parametrize(
    "params",
    [
        {"folder": "f1", "clear": True, "steering_dirs": ["/a"]},
        {"folder": "f1", "clear": "yes"},
        {"folder": "f1"},
    ],
)
def test_folder_clear_beside_a_list_or_neither_is_refused(params):
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(STEERING, params)
    assert exc.value.code == "invalid_param"
    assert "clear" in exc.value.message


def test_a_missing_list_names_the_clear_spelling():
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(STEERING, {"folder": "f1"})
    assert "'clear': true" in exc.value.message


@pytest.mark.parametrize(
    ("params", "fields"),
    [
        ({"template": "t", "clear": ["allowedTools"]}, {"allowedTools": []}),
        ({"template": "t", "fields": {}, "clear": ["allowedTools"]}, {"allowedTools": []}),
        (
            {"template": "t", "fields": {"model": "m"}, "clear": ["allowedTools", "tools"]},
            {"tools": [], "allowedTools": [], "model": "m"},
        ),
        (
            {"template": "t", "fields": {"allowedTools": []}, "clear": ["allowedTools"]},
            {"allowedTools": []},
        ),
    ],
)
def test_template_clear_normalizes_to_an_empty_replacement(params, fields):
    assert catalog.validate_params(TEMPLATE, params) == {"template": "t", "fields": fields}


def test_template_fields_without_clear_are_unchanged():
    params = {"template": "t", "fields": {"allowedTools": ["fs_read"], "model": "m"}}
    assert catalog.validate_params(TEMPLATE, params) == params


@pytest.mark.parametrize(
    "params",
    [
        {"template": "t", "fields": {"allowedTools": ["fs_read"]}, "clear": ["allowedTools"]},
        {"template": "t", "clear": ["model"]},
        {"template": "t", "clear": "allowedTools"},
        {"template": "t"},
        {"template": "t", "fields": {}},
    ],
)
def test_template_clear_beside_items_or_naming_no_list_field_is_refused(params):
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(TEMPLATE, params)
    assert exc.value.code == "invalid_param"
    assert "clear" in exc.value.message


def test_the_kinds_advertise_clear_and_require_only_their_target():
    kinds = {k["id"]: k for k in catalog.list_kinds()}
    steering = kinds[STEERING]["params_schema"]
    template = kinds[TEMPLATE]["params_schema"]
    assert steering["properties"]["clear"] == {"type": "boolean"}
    assert steering["required"] == ["folder"]
    assert template["properties"]["clear"]["items"]["enum"] == ["tools", "allowedTools", "skills"]
    assert template["required"] == ["template"]
    assert "clear" in kinds[STEERING]["summary"] and "clear" in kinds[TEMPLATE]["summary"]


# ── end to end: the tool's own entry, real card routes, real folder route ──


@web.middleware
async def _internal_secret_auth(request: web.Request, handler):
    """What the gateway's auth layer leaves on a request: the tool's internal
    credential marks an agent call; anything else is the owner's browser."""
    request["app"] = ""
    request["user"] = "local-app"
    if request.headers.get("X-Internal-Secret") == SECRET:
        request["internal_auth"] = True
        request["peer_verified"] = True
    return await handler(request)


def _with_card_server(state: _State, monkeypatch, fn):
    """Run *fn(client, tool)* against a loopback server holding the real card routes.

    ``tool(params_or_kind, ...)`` calls ``propose_change`` through ``mcp_guide``'s own
    entry, with its ``_post`` bridged onto this server's test client.
    """

    async def main():
        app = web.Application(middlewares=[_internal_secret_auth, routes.change_card_middleware])
        app["state"] = state
        routes.register_change_card_routes(app)
        app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
        async with TestClient(TestServer(app)) as client:
            addrs = [a[0] for a in client.server.runner.addresses]
            assert addrs and all(a == "127.0.0.1" for a in addrs), addrs
            loop = asyncio.get_running_loop()

            def post(path: str, body: Any = None, *, session_key: Any = None, **_kw: Any):
                async def send() -> dict[str, Any]:
                    headers = {"X-Internal-Secret": SECRET, "X-Session-Key": session_key or ""}
                    response = await client.post(path, json=body, headers=headers)
                    payload = await response.json()
                    assert isinstance(payload, dict)
                    return payload

                return asyncio.run_coroutine_threadsafe(send(), loop).result(timeout=30)

            monkeypatch.setattr(mcp_guide, "_post", post)

            async def tool(kind: str, params: dict[str, Any]) -> str:
                args = {"kind": kind, "params": params}
                return await asyncio.wait_for(
                    asyncio.to_thread(mcp_guide._call_tool, "propose_change", args), timeout=30
                )

            out = await fn(client, tool)
            if routes._BACKGROUND:
                await asyncio.wait_for(asyncio.gather(*list(routes._BACKGROUND)), timeout=30)
            return out

    return asyncio.run(main())


def _folder_state(tmp_path, dirs: list[str]) -> tuple[_State, list[str]]:
    paths = []
    for name in dirs:
        d = tmp_path / name
        d.mkdir()
        paths.append(str(d.resolve()))
    state = _State([{"id": "f1", "name": "Docs", "parent_id": None, "steering_dirs": paths[:1]}])
    return state, paths


@_needs_pinned_walk
def test_folder_clear_from_the_tool_reaches_the_folder(tmp_path, monkeypatch):
    state, (keep,) = _folder_state(tmp_path, ["keep"])

    async def go(c, tool):
        reply = await tool(STEERING, {"folder": "f1", "clear": True})
        card = json.loads(reply)
        assert card["kind"] == STEERING and card["risk"] == "tighten", card
        step = card["plan"]["apply"][0]
        assert step["body"] == {"steering_dirs": []}
        # Nothing changes until the person presses Apply.
        assert state._folders[0]["steering_dirs"] == [keep]
        r = await c.patch(step["path"], json=step["body"], headers=_step(card))
        assert r.status == 200, await r.text()

    _with_card_server(state, monkeypatch, go)
    # Read the way the gateway reads a folder's list: the route stores "none" by
    # dropping the key.
    assert catalog.string_items(state._folders[0].get("steering_dirs")) == []


@_needs_pinned_walk
def test_a_folder_list_from_the_tool_is_still_the_new_list(tmp_path, monkeypatch):
    state, (keep, new) = _folder_state(tmp_path, ["keep", "new"])

    async def go(c, tool):
        card = json.loads(await tool(STEERING, {"folder": "f1", "steering_dirs": [keep, new]}))
        assert card["risk"] == "widen", card
        step = card["plan"]["apply"][0]
        assert step["body"] == {"steering_dirs": [keep, new]}
        r = await c.patch(step["path"], json=step["body"], headers=_step(card))
        assert r.status == 200, await r.text()

    _with_card_server(state, monkeypatch, go)
    assert state._folders[0]["steering_dirs"] == [keep, new]


@_needs_pinned_walk
def test_folder_clear_beside_a_list_from_the_tool_is_refused_and_no_card_lands(
    tmp_path, monkeypatch
):
    state, (keep, new) = _folder_state(tmp_path, ["keep", "new"])

    async def go(c, tool):
        return await tool(STEERING, {"folder": "f1", "clear": True, "steering_dirs": [new]})

    reply = _with_card_server(state, monkeypatch, go)
    assert reply.startswith("Error:") and "clear" in reply, reply
    assert state._change_card_store.pending(None) == []
    assert state._folders[0]["steering_dirs"] == [keep]


@pytest.fixture
def helper_template(tmp_path, monkeypatch):
    """A template named ``helper`` that auto-approves ``fs_read``."""
    specs = tmp_path / "agents"
    specs.mkdir()
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", specs)
    (specs / "helper.json").write_text(
        json.dumps(
            {"name": "helper", "tools": ["fs_read", "web_fetch"], "allowedTools": ["fs_read"]}
        ),
        encoding="utf-8",
    )
    return specs


@pytest.mark.parametrize(
    ("params", "body"),
    [
        ({"template": "helper", "clear": ["allowedTools"]}, {"allowedTools": []}),
        (
            {"template": "helper", "fields": {"model": "m2"}, "clear": ["allowedTools"]},
            {"allowedTools": [], "model": "m2"},
        ),
    ],
)
def test_template_clear_from_the_tool_proposes_emptying_the_list(
    helper_template, monkeypatch, params, body
):
    async def go(c, tool):
        return json.loads(await tool(TEMPLATE, params))

    card = _with_card_server(_State([]), monkeypatch, go)
    assert card["kind"] == TEMPLATE and card["risk"] == "tighten", card
    assert {"label": "allowedTools", "add": [], "remove": ["fs_read"]} in card["changes"]
    assert card["plan"]["apply"] == [
        {"method": "PATCH", "path": "/api/agents/detail/helper", "body": body}
    ]


def test_template_items_from_the_tool_are_still_a_replacement(helper_template, monkeypatch):
    async def go(c, tool):
        params = {"template": "helper", "fields": {"allowedTools": ["fs_read", "web_fetch"]}}
        return json.loads(await tool(TEMPLATE, params))

    card = _with_card_server(_State([]), monkeypatch, go)
    assert card["risk"] == "widen", card
    assert card["plan"]["apply"][0]["body"] == {"allowedTools": ["fs_read", "web_fetch"]}


def test_template_clear_beside_items_from_the_tool_is_refused_and_no_card_lands(
    helper_template, monkeypatch
):
    state = _State([])

    async def go(c, tool):
        params = {
            "template": "helper",
            "fields": {"allowedTools": ["x"]},
            "clear": ["allowedTools"],
        }
        return await tool(TEMPLATE, params)

    reply = _with_card_server(state, monkeypatch, go)
    assert reply.startswith("Error:") and "clear" in reply, reply
    assert state._change_card_store.pending(None) == []
