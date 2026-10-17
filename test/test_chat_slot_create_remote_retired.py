"""POST /api/chat/slots refuses to create a slot bound to a remote crew.

A session on a connected crew is created ON that crew and opened here through
its window. So a create body naming a crew (``instance_id``) or a peer session
to adopt (``adopt_remote_slot``) is refused with one fixed answer, and nothing is
created or touched on either machine.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

import kiro_crew

RETIRED_BODIES = [
    {"instance_id": "nobita"},
    {"adopt_remote_slot": "chat-9"},
    {"instance_id": "nobita", "adopt_remote_slot": "chat-9"},
    {"name": "chat-1", "instance_id": "nobita"},
]


def _create_app(state, *, app_name: str = "") -> web.Application:
    from kiro_crew.dashboard.chat import api_chat_slot_create

    async def handler(request: web.Request) -> web.Response:
        request["app"] = app_name
        request["user"] = "local-app"
        return await api_chat_slot_create(request)

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", handler)
    return app


class _NoPeer:
    """An instances manager that fails the test if anything reaches a peer."""

    def __getattr__(self, name):
        raise AssertionError(f"the create path touched the instances manager: {name}")


@pytest.mark.asyncio
@pytest.mark.parametrize("app_name", ["", "notes"])
@pytest.mark.parametrize("body", RETIRED_BODIES)
async def test_a_crew_bound_create_is_refused_and_creates_nothing(tmp_path, body, app_name):
    state = _make_state(tmp_path)
    state.instances_manager = _NoPeer()
    before = set(state._slots)
    async with TestClient(TestServer(_create_app(state, app_name=app_name))) as client:
        resp = await client.post("/api/chat/slots", json=body)
        assert resp.status == 400
        assert (await resp.json())["code"] == "remote_create_retired"
    assert set(state._slots) == before


@pytest.mark.asyncio
async def test_an_existing_slot_named_with_a_crew_is_left_alone(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    async with TestClient(TestServer(_create_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots", json={"name": "chat-1", "instance_id": "nobita"}
        )
        assert resp.status == 400
    assert slot.executor != "remote"
    assert slot.instance_id == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [{"instance_id": ""}, {"adopt_remote_slot": None}])
async def test_an_empty_binding_field_is_an_ordinary_local_create(tmp_path, empty):
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_create_app(state))) as client:
        resp = await client.post("/api/chat/slots", json={"name": "chat-local", **empty})
        assert resp.status == 200
        payload = await resp.json()
    assert payload.get("executor") != "remote"
    assert state._slots["chat-local"].executor != "remote"


def test_no_source_module_stamps_a_remote_executor_except_the_disk_reader():
    """The only ``executor = "remote"`` left is restoring an old slot from disk."""
    root = Path(kiro_crew.__file__).parent / "dashboard"
    stamps = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not (isinstance(node.value, ast.Constant) and node.value.value == "remote"):
                continue
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "executor":
                    stamps.append(path.relative_to(root).as_posix())
    assert stamps == ["slot_persistence/metadata_codec.py"]
