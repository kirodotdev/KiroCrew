"""A guest's prompt attaches an image only from the attachment stores.

``acp.prompt_blocks.build_prompt_blocks`` is the one place a path in a prompt's text is
read and sent to the model as a picture. A prompt a guest's request started (a spawn, an
import replayed by regenerate, a send, a queued or steered message) reads it through
``guest_scope.attachment_read``; anything else stays plain text and is never opened.
The owner's prompts are unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_shared_crew_guest_parity import GUEST

from kiro_crew import guest_prompt_origin
from kiro_crew.acp import prompt_blocks
from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def _preview_on(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    uploads = tmp_path / "home" / "uploads"
    uploads.mkdir(parents=True)
    (tmp_path / "home" / "sessions" / "s1.attachments").mkdir(parents=True)
    host = tmp_path / "private.png"
    host.write_bytes(_PNG)
    stored = uploads / "abc_pic.png"
    stored.write_bytes(_PNG)
    kept = tmp_path / "home" / "sessions" / "s1.attachments" / "pic.png"
    kept.write_bytes(_PNG)
    return {"host": host, "upload": stored, "session": kept}


def _images(message: str) -> list[dict[str, Any]]:
    return [b for b in prompt_blocks.build_prompt_blocks(message) if b["type"] == "image"]


def test_an_owner_prompt_attaches_a_host_image(stores: dict[str, Path]) -> None:
    assert len(_images(f"look at {stores['host']}")) == 1


def test_a_guest_prompt_attaches_nothing_from_a_host_path_and_reads_nothing(
    stores: dict[str, Path],
) -> None:
    message = f"look at {stores['host']}"
    token = guest_prompt_origin.bind(True)
    try:
        with (
            patch.object(prompt_blocks, "safe_read_file_bytes", side_effect=AssertionError("read")),
            patch.object(Path, "is_file", side_effect=AssertionError("probe")),
            patch.object(Path, "stat", side_effect=AssertionError("probe")),
        ):
            blocks = prompt_blocks.build_prompt_blocks(message)
    finally:
        guest_prompt_origin.reset(token)
    assert [b["type"] for b in blocks] == ["text"]
    assert blocks[0]["text"] == message


@pytest.mark.parametrize("which", ["upload", "session"])
def test_a_guest_prompt_still_attaches_an_attachment_store_image(
    stores: dict[str, Path], which: str
) -> None:
    token = guest_prompt_origin.bind(True)
    try:
        blocks = prompt_blocks.build_prompt_blocks(f"look at {stores[which]}")
    finally:
        guest_prompt_origin.reset(token)
    assert [b["type"] for b in blocks] == ["text", "image"]
    assert f"[image: {stores[which].name}]" in blocks[0]["text"]


def test_a_guest_path_through_a_store_name_that_climbs_out_stays_text(
    stores: dict[str, Path],
) -> None:
    climbing = f"{stores['upload'].parent}/../{stores['host'].name}"
    token = guest_prompt_origin.bind(True)
    try:
        assert [b["type"] for b in prompt_blocks.build_prompt_blocks(climbing)] == ["text"]
    finally:
        guest_prompt_origin.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/api/spawn", "/api/chat/slots/s/regenerate", "/api/send"])
@pytest.mark.parametrize("as_guest", [True, False])
async def test_a_task_a_request_starts_builds_its_prompt_for_that_requests_origin(
    stores: dict[str, Path], route: str, as_guest: bool
) -> None:
    """Spawn, regenerate and a send all start their turn's task inside the request."""
    from kiro_crew.dashboard.slot_ownership import slot_ownership_middleware

    @web.middleware
    async def identity(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        request["user"] = "guest@example.test" if as_guest else "owner"
        request["is_dashboard_user"] = True
        if as_guest:
            request[ACTOR_REQUEST_KEY] = GUEST
        return await handler(request)

    async def start_a_turn(request: web.Request) -> web.Response:
        message = f"look at {stores['host']}"
        task = asyncio.create_task(asyncio.to_thread(prompt_blocks.build_prompt_blocks, message))
        return web.json_response([b["type"] for b in await task])

    app = web.Application(middlewares=[identity, slot_ownership_middleware])
    app["state"] = MagicMock(owner_id="owner", _slots={})
    app.router.add_post(route, start_a_turn)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(route, json={})
        kinds = await resp.json()
    assert kinds == (["text"] if as_guest else ["text", "image"])


def test_a_message_that_waits_in_the_queue_keeps_its_guest_origin() -> None:
    owner_meta: dict[str, Any] = {}
    guest_prompt_origin.mark(owner_meta)
    assert owner_meta == {}

    token = guest_prompt_origin.bind(True)
    try:
        guest_meta: dict[str, Any] = {}
        guest_prompt_origin.mark(guest_meta)
    finally:
        guest_prompt_origin.reset(token)
    assert guest_meta == {guest_prompt_origin.GUEST_ORIGIN_META: True}
    assert guest_prompt_origin.marked([{"meta": owner_meta}, {"meta": guest_meta}])
    assert not guest_prompt_origin.marked([{"meta": owner_meta}, {}])


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("kiro_crew.dashboard.chat_delivery", "queue_for_next_turn"),
        ("kiro_crew.dashboard.chat_delivery", "steer_into_running_turn"),
        ("kiro_crew.dashboard.chat_turn.queue_drain", "_start_next_queued_turn"),
    ],
)
def test_every_path_that_holds_a_message_stamps_or_reads_the_guest_origin(
    module: str, name: str
) -> None:
    """Queue, steer and drain are where a guest's request has ended and its turn has not begun."""
    import importlib

    assert "guest_prompt_origin" in inspect.getsource(
        getattr(importlib.import_module(module), name)
    )
