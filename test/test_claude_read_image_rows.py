"""A Claude Code Read of an image file shows the tool row's thumbnail, as kiro-cli's does.

The dashboard's tool row previews the images a read looked at (``ToolImageThumbnails``,
fetched from ``/api/file-raw`` at view time). kiro-cli's read names them in
``operations[].image_paths``, a shape the row reads on its own. Claude Code's Read names
its one file in ``file_path``, an argument its Write and Edit carry too, so the row
previews it only when the backend marks the row: ``AcpClient`` sets
``AcpEvent.read_path_arg`` on a ``read``-kind ``tool_call`` from a member of
``ACP_BACKENDS_READ_PATH_ARG`` (``acp.read_path_arg``), and ``_tool_meta`` keeps it on
the row as ``meta.read_path_arg``.

The fixture ``claude/read-image.jsonl`` holds the tool frames and the reply of a live
turn on claude-agent-acp 0.107.0 whose model opened a PNG with Read, normalized as its
header says.

Section 1 replays it through the real ``AcpClient._dispatch_events``, section 2 the
gate, section 3 a real dashboard turn (fed the client's events, and fed a real
``AcpProvider`` stream) and a failed Read losing its mark, section 4 the endpoint the
thumbnail fetches through, unchanged, and section 5 the inline image blocks Crew does
not store.
"""

from __future__ import annotations

import base64
import copy
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from acp_frame_replay_harness import CORPUS, read_fixture
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from turn_harness import ScriptedProvider, TurnScript, run_turn

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.read_path_arg import READ_PATH_ARG, read_path_arg
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    STOP_REASON_END_TURN,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_READ_PATH_ARG,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.handlers import api_file_raw
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.testing.links import make_dir_link
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

FIXTURE = CORPUS / "claude" / "read-image.jsonl"
SESSION = "claude-session-1"
CALL_ID = "toolu_read_image_1"
READ_PATH = "/work/website/public/icon-192.png"


def _frames() -> list[dict[str, Any]]:
    meta, frames = read_fixture(FIXTURE)
    assert meta["backend"] == ACP_BACKEND_CLAUDE
    return frames


def _image_b64() -> str:
    """The base64 PNG the fixture's completed Read update carries."""
    [block] = _frames()[3]["params"]["update"]["content"]
    return block["content"]["data"]


def _client(backend: str = ACP_BACKEND_CLAUDE) -> AcpClient:
    client = AcpClient(acp_backend=backend)
    client._session_id = SESSION
    return client


def _replay(client: AcpClient, frames: list[dict[str, Any]]) -> None:
    """Have *client*'s next prompt loop read *frames*, then the prompt's answer."""
    script: list[tuple[str, JsonRpcMessage]] = [
        ("update", JsonRpcMessage.from_dict(frame)) for frame in frames
    ]
    script.append(("complete", JsonRpcMessage(id=1, result={"stopReason": STOP_REASON_END_TURN})))

    async def _fake_loop(req_id, timeout):
        for item in script:
            yield item

    client._prompt_loop = _fake_loop  # type: ignore[assignment]


async def _turn(client: AcpClient, frames: list[dict[str, Any]]) -> list[AcpEvent]:
    """Run one ``_dispatch_events`` turn over *frames*, then the prompt's answer."""
    _replay(client, frames)
    return [
        event async for event in client._dispatch_events(req_id=1, timeout=FALLBACK_TIMEOUT_SECS)
    ]


def _of(events: list[AcpEvent], kind: str) -> list[AcpEvent]:
    return [event for event in events if event.kind == kind]


def _with_kind(frame: dict[str, Any], kind: str) -> dict[str, Any]:
    out = copy.deepcopy(frame)
    out["params"]["update"]["kind"] = kind
    return out


# ── 1. The recorded Read, through the real client ──────────────────────────────


@pytest.mark.asyncio
async def test_the_read_tool_call_names_the_argument_its_file_is_in() -> None:
    events = await _turn(_client(), _frames())
    [call] = _of(events, EVENT_TOOL_CALL)
    assert (call.tool_call_id, call.tool_kind, call.read_path_arg) == (CALL_ID, "read", "file_path")
    # The first frame streams an empty rawInput; the refinement carries the path,
    # under the argument the call's mark names.
    [refined] = _of(events, EVENT_TOOL_CALL_UPDATE)
    assert json.loads(refined.tool_input) == {"file_path": READ_PATH}
    assert json.loads(refined.tool_input)[call.read_path_arg] == READ_PATH
    assert READ_PATH_ARG == "file_path"
    assert events[-1].kind == EVENT_COMPLETE


@pytest.mark.asyncio
async def test_the_mark_rides_the_tool_call_alone() -> None:
    """The kind is on the first frame; nothing else in the turn carries a mark."""
    events = await _turn(_client(), _frames())
    marked = [event for event in events if event.read_path_arg]
    assert [event.kind for event in marked] == [EVENT_TOOL_CALL]


# ── 2. The gate ────────────────────────────────────────────────────────────────


def test_the_read_path_set_names_claude_alone() -> None:
    assert ACP_BACKENDS_READ_PATH_ARG == frozenset({ACP_BACKEND_CLAUDE})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_OPENCODE, ACP_BACKEND_CODEX]
)
async def test_a_harness_outside_the_set_marks_no_read(backend: str) -> None:
    events = await _turn(_client(backend), _frames())
    assert [event for event in events if event.read_path_arg] == []
    # The same call still reaches the row, unmarked.
    assert [event.tool_call_id for event in _of(events, EVENT_TOOL_CALL)] == [CALL_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["edit", "execute", "other", "search", "fetch", ""])
async def test_a_call_of_another_kind_is_not_marked(kind: str) -> None:
    """Claude Code's Write and Edit name a ``file_path`` too; only a read is marked."""
    frames = _frames()
    events = await _turn(_client(), [_with_kind(frames[0], kind), *frames[1:]])
    assert [event for event in events if event.read_path_arg] == []


@pytest.mark.parametrize(
    "backend,kind",
    [
        (ACP_BACKEND_CLAUDE, "Read"),
        (ACP_BACKEND_CLAUDE, None),
        (ACP_BACKEND_CLAUDE, ["read"]),
        ("not-a-backend", "read"),
        (None, "read"),
        (["claude"], "read"),
    ],
)
def test_anything_but_a_member_and_a_read_answers_empty(backend: Any, kind: Any) -> None:
    assert read_path_arg(backend, kind) == ""


def test_the_answer_is_the_harness_argument_and_nothing_else() -> None:
    assert read_path_arg(ACP_BACKEND_CLAUDE, "read") == "file_path"


# ── 3. A dashboard turn keeps the mark on the row ──────────────────────────────


class _ClaudeProvider(ScriptedProvider):
    """A scripted provider whose capabilities are claude's."""

    capabilities = capabilities_for(ACP_BACKEND_CLAUDE)


class _KiroProvider(ScriptedProvider):
    """A scripted provider whose capabilities are kiro-cli's."""

    capabilities = capabilities_for(ACP_BACKEND_KIRO)


def _turn_events(events: list[AcpEvent]) -> list[AcpEvent]:
    """The replayed events a dashboard turn consumes, ending in its terminal one."""
    keep = (EVENT_TOOL_CALL, EVENT_TOOL_CALL_UPDATE, EVENT_TOOL_RESULT, EVENT_TEXT_CHUNK)
    done = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
    return [*(event for event in events if event.kind in keep), done]


def _tool_message_frames(record: Any) -> list[dict[str, Any]]:
    return [
        frame.payload
        for frame in record.frames("chat_message")
        if frame.payload.get("role") == "tool"
    ]


@pytest.mark.asyncio
async def test_a_dashboard_turn_keeps_the_mark_on_the_live_and_persisted_row() -> None:
    events = await _turn(_client(), _frames())
    record = await run_turn(TurnScript(events=_turn_events(events), provider=_ClaudeProvider))
    [row] = record.rows("tool")
    meta = row["meta"]
    assert (meta["kind"], meta["read_path_arg"]) == ("read", "file_path")
    # The refinement patched the input in; the row's mark names where the path is.
    assert json.loads(meta["input"])[meta["read_path_arg"]] == READ_PATH
    # The live row the open tab draws carries the mark from its first frame.
    [live] = _tool_message_frames(record)
    assert live["meta"]["read_path_arg"] == "file_path"
    # The input reaches the open tab through the refinement's patch.
    patches = [
        frame.payload["meta"]["input"]
        for frame in record.frames("chat_message_update")
        if "input" in frame.payload.get("meta", {})
    ]
    assert [json.loads(patch) for patch in patches] == [{"file_path": READ_PATH}]
    # A completed read keeps its mark: nothing withdraws it.
    assert _mark_patches(record) == []


def _mark_patches(record: Any) -> list[tuple[str, dict[str, Any]]]:
    """The ``(ts, meta)`` of every live patch that writes a row's read mark."""
    return [
        (frame.payload.get("ts"), frame.payload["meta"])
        for frame in record.frames("chat_message_update")
        if "read_path_arg" in frame.payload.get("meta", {})
    ]


def _claude_provider(frames: list[dict[str, Any]]) -> AcpProvider:
    """A real claude ``AcpProvider`` whose client answers its next prompt with *frames*."""
    client = _client()
    client.ensure_ready = AsyncMock()  # type: ignore[method-assign]
    client._send_prompt = AsyncMock(return_value=1)  # type: ignore[method-assign]
    _replay(client, frames)
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=ACP_BACKEND_CLAUDE)
    provider._client = client
    provider._ready_confirmed_session = AsyncMock()  # type: ignore[method-assign]
    return provider


def _dashboard_over(provider: AcpProvider) -> type[ScriptedProvider]:
    """The turn's provider port answered by *provider*'s own ``stream``."""

    class _Turn(ScriptedProvider):
        capabilities = capabilities_for(ACP_BACKEND_CLAUDE)

        def stream(self, message: str, *, allow_image: bool = True) -> AsyncIterator[AcpEvent]:
            self.record("stream", message)
            return provider.stream(message, allow_image=allow_image)

    return _Turn


@pytest.mark.asyncio
async def test_the_provider_stream_carries_the_mark_to_the_saved_row() -> None:
    """The live path: ``AcpProvider.stream`` rebuilds each client event field by field
    (``providers.acp_turns._to_llm_event``) before the dashboard turn reads it."""
    record = await run_turn(TurnScript(provider=_dashboard_over(_claude_provider(_frames()))))
    assert len(record.calls("stream")) == 1
    [row] = record.rows("tool")
    assert (row["meta"]["tool_call_id"], row["meta"]["read_path_arg"]) == (CALL_ID, "file_path")
    assert json.loads(row["meta"]["input"]) == {"file_path": READ_PATH}
    [live] = _tool_message_frames(record)
    assert live["meta"]["read_path_arg"] == "file_path"


def _failed_read(how: str) -> dict[str, Any]:
    """The fixture's Read ending ``failed``, in the two shapes claude-agent-acp 0.107.0
    sends: Claude Code's permission layer refused it (``tool-calls/renderer.js``
    ``permissionDenied``), or the tool ran and returned an error (``resultStatus`` on
    an ``is_error`` result, its text fenced by ``content.js`` ``toAcpContentUpdate``)."""
    frame = copy.deepcopy(_frames()[3])
    if how == "permission-denied":
        text = "Permission denied: Read of this path is denied by a permission rule"
        tool_response: dict[str, Any] = {"decisionReasonType": "rule"}
    else:
        text = "```\nFile does not exist.\n```"
        tool_response = {}
    update = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": CALL_ID,
        "status": "failed",
        "content": [{"type": "content", "content": {"type": "text", "text": text}}],
        "_meta": {"claudeCode": {"toolName": "Read", "toolResponse": tool_response}},
    }
    frame["params"]["update"] = update
    return frame


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["permission-denied", "tool-error"])
async def test_a_read_that_fails_loses_its_mark(how: str) -> None:
    """A failed Read read no file: the stored row and the open tab both lose the mark,
    so the row previews nothing. The mark rode the call's first frame until then."""
    frames = _frames()
    events = await _turn(_client(), [frames[0], frames[1], _failed_read(how)])
    [result] = _of(events, EVENT_TOOL_RESULT)
    assert (result.tool_status, result.tool_final) == ("failed", False)
    record = await run_turn(TurnScript(events=_turn_events(events), provider=_ClaudeProvider))
    [row] = record.rows("tool")
    assert "read_path_arg" not in row["meta"] and row["meta"]["done"] is True
    [live] = _tool_message_frames(record)
    assert live["meta"]["read_path_arg"] == "file_path"
    assert _mark_patches(record) == [(row["ts"], {"read_path_arg": ""})]


@pytest.mark.asyncio
async def test_a_kiro_turn_over_the_same_frames_writes_no_mark() -> None:
    events = await _turn(_client(ACP_BACKEND_KIRO), _frames())
    record = await run_turn(TurnScript(events=_turn_events(events), provider=_KiroProvider))
    [row] = record.rows("tool")
    assert "read_path_arg" not in row["meta"]
    assert all("read_path_arg" not in frame["meta"] for frame in _tool_message_frames(record))


@pytest.mark.asyncio
async def test_a_claude_write_of_an_image_name_writes_no_mark() -> None:
    frames = _frames()
    events = await _turn(_client(), [_with_kind(frames[0], "edit"), *frames[1:]])
    record = await run_turn(TurnScript(events=_turn_events(events), provider=_ClaudeProvider))
    [row] = record.rows("tool")
    assert "read_path_arg" not in row["meta"]


# ── 4. The thumbnail's endpoint, unchanged ─────────────────────────────────────
#
# The row's thumbnail is ``GET /api/file-raw?path=<the path the row names>``, the
# endpoint kiro-cli's image reads already fetch through. Nothing here patches its
# path validator or its sensitive-path floor: the row's path meets the same checks.


def _file_raw_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-raw", api_file_raw)
    return as_owner(app)


async def _fetch(path: str) -> tuple[int, str, bytes]:
    async with TestClient(TestServer(_file_raw_app())) as client:
        resp = await client.get("/api/file-raw", params={"path": path})
        return resp.status, resp.headers.get("Content-Type", ""), await resp.read()


@pytest.mark.asyncio
async def test_the_read_image_is_served_with_its_own_type(tmp_path) -> None:
    png = base64.b64decode(_image_b64())
    target = tmp_path / "icon-192.png"
    target.write_bytes(png)
    status, ctype, body = await _fetch(str(target))
    assert (status, ctype, body) == (200, "image/png", png)


def _credential_image(tmp_path, monkeypatch) -> Any:
    """A real PNG inside ``~/.ssh``, with the home pointed at *tmp_path* (``HOME``, and
    ``USERPROFILE``, which ``Path.home()`` reads on Windows)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    secret_dir = tmp_path / ".ssh"
    secret_dir.mkdir()
    target = secret_dir / "key.png"
    target.write_bytes(base64.b64decode(_image_b64()))
    return target


@pytest.mark.asyncio
async def test_a_credential_directory_path_is_still_refused(tmp_path, monkeypatch) -> None:
    target = _credential_image(tmp_path, monkeypatch)
    status, _ctype, body = await _fetch(str(target))
    # The path validator refuses it before the sensitive-path floor is reached.
    assert status == 400 and "forbidden" in json.loads(body)["error"]


@pytest.mark.asyncio
async def test_a_link_into_a_credential_directory_is_still_refused(tmp_path, monkeypatch) -> None:
    """The row names a path; the endpoint judges the file it resolves to."""
    target = _credential_image(tmp_path, monkeypatch)
    pictures = tmp_path / "pictures"
    make_dir_link(pictures, target.parent)
    status, _ctype, body = await _fetch(str(pictures / target.name))
    assert status == 400 and "forbidden" in json.loads(body)["error"]


@pytest.mark.asyncio
async def test_a_file_named_like_an_image_that_is_not_one_is_still_refused(tmp_path) -> None:
    target = tmp_path / "notes.png"
    target.write_text("not a picture")
    status, _ctype, body = await _fetch(str(target))
    assert status == 403
    assert "not a recognized format" in json.loads(body)["error"]


# ── 5. Inline image blocks are not stored or shown ─────────────────────────────
#
# Crew keeps no store for image bytes a harness sends inline (the claude-code
# provider spec, "Images Claude reads"), so a result's image block, whatever its bytes,
# size or claimed mime, reaches neither the event nor the row.


def _image_result(data: str, mime: str) -> dict[str, Any]:
    frame = copy.deepcopy(_frames()[3])
    update = frame["params"]["update"]
    update["content"] = [
        {"type": "content", "content": {"type": "image", "data": data, "mimeType": mime}}
    ]
    update["rawOutput"] = [
        {"type": "image", "source": {"type": "base64", "data": data, "media_type": mime}}
    ]
    return frame


def _large_png_b64() -> str:
    """The fixture's PNG padded past a megabyte: a block's size changes nothing."""
    return base64.b64encode(base64.b64decode(_image_b64()) + b"\x00" * (1024 * 1024)).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data,mime",
    [
        pytest.param(None, "image/png", id="the-read-image"),
        pytest.param(
            base64.b64encode(b"#!/bin/sh\necho hi\n").decode(), "image/png", id="not-an-image"
        ),
        pytest.param(None, "text/html", id="wrong-mime"),
        pytest.param("large", "image/png", id="oversize"),
    ],
)
async def test_an_inline_image_block_reaches_neither_event_nor_row(data: Any, mime: str) -> None:
    payload = _large_png_b64() if data == "large" else (data or _image_b64())
    frames = _frames()
    events = await _turn(_client(), [frames[0], frames[1], _image_result(payload, mime)])
    [result] = _of(events, EVENT_TOOL_RESULT)
    assert (result.tool_status, result.tool_output) == ("completed", "")
    for event in events:
        assert payload[:64] not in json.dumps(event.__dict__, default=str)
    record = await run_turn(TurnScript(events=_turn_events(events), provider=_ClaudeProvider))
    [row] = record.rows("tool")
    assert not row["meta"].get("output")
    assert payload[:64] not in json.dumps(record.history_rows, default=str)
    assert payload[:64] not in json.dumps([f.payload for f in record.ws_frames], default=str)


@pytest.mark.asyncio
async def test_an_assistant_image_chunk_adds_no_text() -> None:
    """sdk-to-acp.js sends an image the model returns as an agent_message_chunk."""
    chunk = copy.deepcopy(_frames()[4])
    chunk["params"]["update"]["content"] = {
        "type": "image",
        "data": _image_b64(),
        "mimeType": "image/png",
    }
    events = await _turn(_client(), [chunk, _frames()[5]])
    assert [event.text for event in _of(events, EVENT_TEXT_CHUNK)] == [" purple."]
