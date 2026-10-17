"""The served-default check after ``session/set_mode``, off the pooled start paths.

kiro-cli's ``session/set_mode`` puts the session on the model the activated
agent's spec pins and reports it only in the ``_kiro.dev/agent/switched`` frame it
sends before answering ``{}``. A spec pinning a model the account is not served
then fails the first prompt with "no access to model".

Two places activate an agent without judging that model:

* ``AcpClient._initialize_session`` (the direct client the knowledge LLM pool
  drives) sends ``set_mode`` without waiting for its answer, so the frame lands
  wherever the next read reaches it;
* ``AcpSessionHandle.set_mode`` switches a live pooled session's agent.

Each is driven here against a scripted kiro-cli that answers ``set_mode`` the way
kiro-cli does, and the order of what goes on the wire is asserted: an unserved
activated model is followed by a ``set_model`` to a served one before the next
prompt, a served one is adopted with no write, and a frame without a model, or
one a later ``set_model`` superseded, changes nothing.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KIRO,
    EVENT_AGENT_SWITCHED,
    EVENT_COMPLETE,
    METHOD_AGENT_SWITCHED,
    METHOD_PROMPT,
    METHOD_SET_MODE,
    METHOD_SET_MODEL,
    JsonRpcMessage,
)

ADVERTISED = ["gpt-5.6-sol", "glm-5"]
SESSION_NEW_MODEL = "gpt-5.6-sol"
AGENT = "ops"
SID = "sess-123"


class _FakeKiro:
    """kiro-cli's side of an ``AcpClient`` wire, answering what it is sent.

    ``session/set_mode`` gets an ``agent/switched`` frame (with ``model`` when one
    is scripted, as kiro-cli 2.29.1 sends it) and then ``{}``. With
    ``frame_on_set_mode`` False the frame is held back until :meth:`release`,
    for a frame the start drain does not read.
    """

    def __init__(self, activated_model: str | None, *, frame_on_set_mode: bool = True) -> None:
        self.activated_model = activated_model
        self.frame_on_set_mode = frame_on_set_mode
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.inbox: deque[JsonRpcMessage] = deque()

    def switched_frame(self) -> JsonRpcMessage:
        params: dict[str, Any] = {
            "sessionId": SID,
            "agentName": AGENT,
            "previousAgentName": "kiro_default",
        }
        if self.activated_model is not None:
            params["model"] = self.activated_model
        return JsonRpcMessage(method=METHOD_AGENT_SWITCHED, params=params)

    async def write(self, data: bytes, request_id: int, method: str) -> None:
        frame = json.loads(data)
        self.sent.append((frame["method"], frame.get("params") or {}))
        if frame["method"] == METHOD_SET_MODE:
            if self.frame_on_set_mode:
                self.inbox.append(self.switched_frame())
            self.inbox.append(JsonRpcMessage(id=frame["id"], result={}))

    async def read(self, timeout: float | None = None) -> JsonRpcMessage | None:
        if self.inbox:
            return self.inbox.popleft()
        # A loop-turn yield: the drain polls this until its idle window passes.
        await asyncio.sleep(0)
        return None

    def methods(self) -> list[str]:
        return [method for method, _params in self.sent]

    def model_writes(self) -> list[str]:
        return [params["modelId"] for method, params in self.sent if method == METHOD_SET_MODEL]


def _session_new_answer(rid, timeout=None, *, method="", expected_mcp=None):
    """The answer to every awaited init request: kiro-cli's session/new shape."""
    return {
        "protocolVersion": "1.0",
        "sessionId": SID,
        "models": {
            "currentModelId": SESSION_NEW_MODEL,
            "availableModels": [{"modelId": m, "name": m} for m in ADVERTISED],
        },
        "modes": {
            "currentModeId": "kiro_default",
            "availableModes": [{"id": "kiro_default"}, {"id": AGENT}],
        },
    }


def _client(tmp_path, fake: _FakeKiro, *, model: str = "") -> AcpClient:
    client = AcpClient(work_dir=tmp_path, agent=AGENT, model=model)
    # No session yet: the start sends session/new, whose answer is captured.
    client._session_id = None
    client._resume_session_id = None
    process = MagicMock()
    process.returncode = None
    client._process = process
    client._work_dir_ready = True
    # The real _send_request and _drain_notifications run; only the pipe is fake.
    client._write_request_bounded = fake.write  # type: ignore[method-assign]
    client._read_message = fake.read  # type: ignore[method-assign]
    client._wait_for_response = AsyncMock(side_effect=_session_new_answer)  # type: ignore[method-assign]

    async def _no_probe_evidence():
        return [], 0.0

    client._probe_advertised_models = _no_probe_evidence  # type: ignore[method-assign]
    return client


async def _start(client: AcpClient) -> None:
    with patch("pathlib.Path.exists", return_value=False), patch("pathlib.Path.stat"):
        await client._initialize_session()


def _after(methods: list[str], first: str) -> list[str]:
    return methods[methods.index(first) + 1 :]


class TestClientStart:
    """``AcpClient._initialize_session`` judges the activation's frame."""

    @pytest.mark.asyncio
    async def test_unserved_activated_model_is_moved_before_the_first_prompt(self, tmp_path):
        fake = _FakeKiro("claude-unserved-9")
        client = _client(tmp_path, fake)
        intent = client._model

        await _start(client)
        await client._send_prompt("hi")

        # set_mode, then the move to a served model, then the prompt.
        assert _after(fake.methods(), METHOD_SET_MODE) == [METHOD_SET_MODEL, METHOD_PROMPT]
        assert fake.model_writes() == ["gpt-5.6-sol"]
        assert client._resolved_model_id == "gpt-5.6-sol"
        # The slot still inherits: the wire is corrected, the intent is not.
        assert client._model == intent

    @pytest.mark.asyncio
    async def test_served_activated_model_is_adopted_without_a_write(self, tmp_path):
        fake = _FakeKiro("glm-5")
        client = _client(tmp_path, fake)

        await _start(client)

        assert fake.model_writes() == []
        assert client._resolved_model_id == "glm-5"

    @pytest.mark.asyncio
    async def test_frame_without_a_model_changes_nothing(self, tmp_path):
        fake = _FakeKiro(None)
        client = _client(tmp_path, fake)

        await _start(client)

        assert fake.model_writes() == []
        assert client._resolved_model_id == SESSION_NEW_MODEL

    @pytest.mark.asyncio
    async def test_pin_sent_after_set_mode_supersedes_the_frame(self, tmp_path):
        """The startup pin goes out after set_mode, so the session runs the pin."""
        fake = _FakeKiro("claude-unserved-9")
        client = _client(tmp_path, fake, model="glm-5")

        await _start(client)

        assert fake.model_writes() == ["glm-5"]
        assert client._resolved_model_id == "glm-5"

    @pytest.mark.asyncio
    async def test_frame_read_inside_a_prompt_is_judged_before_the_next_one(self, tmp_path):
        fake = _FakeKiro("claude-unserved-9", frame_on_set_mode=False)
        client = _client(tmp_path, fake)
        await _start(client)
        assert fake.model_writes() == []

        switched = fake.switched_frame()
        complete = JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

        async def _turn(req_id, timeout):
            yield "agent_switched", switched
            yield "complete", complete

        client._prompt_loop = _turn  # type: ignore[method-assign]
        kinds = [ev.kind async for ev in client.stream_events("first")]
        assert kinds[0] == EVENT_AGENT_SWITCHED
        assert kinds[-1] == EVENT_COMPLETE

        before = len(fake.sent)
        await client.ensure_ready()
        await client._send_prompt("second")

        assert [m for m, _p in fake.sent[before:]] == [METHOD_SET_MODEL, METHOD_PROMPT]
        assert fake.model_writes() == ["gpt-5.6-sol"]
        assert client._resolved_model_id == "gpt-5.6-sol"

    @pytest.mark.asyncio
    async def test_explicit_switch_drops_a_pending_frame(self, tmp_path):
        """A set_model after the frame was read is what the session runs."""
        fake = _FakeKiro("claude-unserved-9", frame_on_set_mode=False)
        client = _client(tmp_path, fake)
        await _start(client)
        client._note_agent_switched(fake.switched_frame().params)

        await client.set_model("glm-5")
        await client.ensure_ready()

        assert fake.model_writes() == ["glm-5"]
        assert client._resolved_model_id == "glm-5"


class _Wire:
    """A shared runtime's process, as one pooled kiro session sees it.

    The per-session ``agent/switched`` record is the runtime's own code. Each
    written request is answered on the session queue; ``set_mode`` the way
    kiro-cli answers it: the reader records the frame's model as it routes the
    frame (``_note_switched_model``), then the ``{}`` answer follows.
    """

    _switched_model_record = AcpRuntime._switched_model_record
    _note_switched_model = AcpRuntime._note_switched_model
    take_switched_model = AcpRuntime.take_switched_model

    def __init__(
        self, queue: asyncio.Queue, activated_model: str | None, *, hold_frame: bool = False
    ):
        self.pid = None
        self.is_alive = MagicMock(return_value=True)
        self.supports_image_prompt = False
        self.acp_backend = ACP_BACKEND_KIRO
        self._last_activity = time.monotonic()
        self.queue = queue
        self.activated_model = activated_model
        self.hold_frame = hold_frame
        self.held: JsonRpcMessage | None = None
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.next_id = 100

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        pass

    async def send_notification(self, method: str, params: dict) -> None:
        self.sent.append((method, params))

    def _route(self, frame: JsonRpcMessage) -> None:
        self._note_switched_model(SID, frame.params)
        self.queue.put_nowait(frame)

    def release_frame(self) -> None:
        """Deliver a held ``agent/switched`` frame, as a late read would."""
        assert self.held is not None
        self._route(self.held)
        self.held = None

    async def send_request(self, method, params, *, on_reserved=None) -> int:
        self.next_id += 1
        rid = self.next_id
        if on_reserved is not None:
            on_reserved(rid)
        self.sent.append((method, params))
        if method == METHOD_SET_MODE:
            frame_params: dict[str, Any] = {"sessionId": SID, "agentName": params["modeId"]}
            if self.activated_model is not None:
                frame_params["model"] = self.activated_model
            frame = JsonRpcMessage(method=METHOD_AGENT_SWITCHED, params=frame_params)
            if self.hold_frame:
                self.held = frame
            else:
                self._route(frame)
        if method == METHOD_PROMPT:
            self.queue.put_nowait(JsonRpcMessage(id=rid, result={"stopReason": "end_turn"}))
        else:
            self.queue.put_nowait(JsonRpcMessage(id=rid, result={}))
        return rid

    def methods(self) -> list[str]:
        return [method for method, _params in self.sent]

    def model_writes(self) -> list[str]:
        return [params["modelId"] for method, params in self.sent if method == METHOD_SET_MODEL]


def _pooled(
    activated_model: str | None, *, hold_frame: bool = False
) -> tuple[AcpSessionHandle, _Wire]:
    queue: asyncio.Queue = asyncio.Queue()
    wire = _Wire(queue, activated_model, hold_frame=hold_frame)
    handle = AcpSessionHandle(SID, queue, wire)
    handle._available_models = [{"modelId": m, "name": m} for m in ADVERTISED]
    handle._resolved_model_id = SESSION_NEW_MODEL
    return handle, wire


async def _no_source_rename(name: str) -> str:
    return name


class TestPooledMidSessionSwitch:
    """``AcpSessionHandle.set_mode`` on a live session, then the next turn."""

    @pytest.fixture(autouse=True)
    def _agent_names_pass_through(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(
            "kiro_crew.acp.skill_projection.resolve_source_agent", _no_source_rename
        )

    @staticmethod
    async def _turn(handle: AcpSessionHandle) -> None:
        kinds = [ev.kind async for ev in handle.prompt("hello")]
        assert kinds[-1] == EVENT_COMPLETE

    @pytest.mark.asyncio
    async def test_unserved_switched_model_is_moved_before_the_next_prompt(self):
        handle, wire = _pooled("claude-unserved-9")

        await handle.set_mode("reviewer")
        await self._turn(handle)

        assert wire.methods() == [METHOD_SET_MODE, METHOD_SET_MODEL, METHOD_PROMPT]
        assert wire.model_writes() == ["gpt-5.6-sol"]
        assert handle._resolved_model_id == "gpt-5.6-sol"

    @pytest.mark.asyncio
    async def test_served_switched_model_is_adopted_without_a_write(self):
        handle, wire = _pooled("glm-5")

        await handle.set_mode("reviewer")
        await self._turn(handle)

        assert wire.methods() == [METHOD_SET_MODE, METHOD_PROMPT]
        assert handle._resolved_model_id == "glm-5"

    @pytest.mark.asyncio
    async def test_frame_without_a_model_changes_nothing(self):
        handle, wire = _pooled(None)

        await handle.set_mode("reviewer")
        await self._turn(handle)

        assert wire.methods() == [METHOD_SET_MODE, METHOD_PROMPT]
        assert handle._resolved_model_id == SESSION_NEW_MODEL

    @pytest.mark.asyncio
    async def test_record_from_before_the_switch_is_not_judged(self):
        handle, wire = _pooled(None)
        wire._note_switched_model(SID, {"model": "claude-unserved-9"})

        await handle.set_mode("reviewer")
        await self._turn(handle)

        assert wire.methods() == [METHOD_SET_MODE, METHOD_PROMPT]
        assert handle._resolved_model_id == SESSION_NEW_MODEL

    @pytest.mark.asyncio
    async def test_explicit_set_model_after_the_switch_wins(self):
        handle, wire = _pooled("claude-unserved-9")

        await handle.set_mode("reviewer")
        await handle.set_model("glm-5")
        await self._turn(handle)

        assert wire.methods() == [METHOD_SET_MODE, METHOD_SET_MODEL, METHOD_PROMPT]
        assert wire.model_writes() == ["glm-5"]
        assert handle._resolved_model_id == "glm-5"

    @pytest.mark.asyncio
    async def test_frame_that_lands_after_a_turn_is_judged_at_the_next(self):
        handle, wire = _pooled("claude-unserved-9", hold_frame=True)

        await handle.set_mode("reviewer")
        await self._turn(handle)
        assert wire.model_writes() == []

        wire.release_frame()
        await self._turn(handle)

        assert wire.methods() == [
            METHOD_SET_MODE,
            METHOD_PROMPT,
            METHOD_SET_MODEL,
            METHOD_PROMPT,
        ]
        assert wire.model_writes() == ["gpt-5.6-sol"]
        assert handle._resolved_model_id == "gpt-5.6-sol"
