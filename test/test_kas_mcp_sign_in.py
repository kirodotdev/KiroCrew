"""MCP OAuth sign-in on the KAS backend.

KAS reports an unauthorized remote MCP server as a failed ``_kiro/mcp/status``
entry with ``failedAuthorization``. The ``authorizationUrl`` on that entry comes
from a passive connect whose callback listener is already closed, so it cannot
complete. A sign-in that can complete is started by the client with
``_kiro/mcp/resetServer`` and ``startOAuth``; the engine then sends the live
consent URL as a ``_kiro/openExternalUrl`` request that names neither session nor
server. These tests pin that Crew starts the sign-in, delivers the live URL to the
session that started it as an ordinary OAuth request, and never the dead one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import BUCKET_CAP, AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INITIALIZED,
    METHOD_KAS_MCP_STATUS,
    METHOD_MCP_OAUTH_REQUEST,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.config.paths import kiro_agents_dir

_DEAD_URL = "https://auth.example.com/authorize?state=passive"
_LIVE_URL = "https://auth.example.com/authorize?state=explicit"

#: A KAS-shaped stub: after session/new it reports one remote server as needing
#: authorization; a resetServer with startOAuth makes it ask the client to open
#: the live URL, then report the server connected, then answer the reset.
_STUB_AGENT = """
import json, os, sys

CAPTURE = os.environ["KIROCREW_TEST_SIGN_IN_CAPTURE"]
seen = {}

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

def status(sid, **server):
    send({"jsonrpc": "2.0", "method": "_kiro/mcp/status",
          "params": {"sessionId": sid, "servers": [{"name": "remote", **server}]}})

pending_reset = None
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        seen["clientMeta"] = params.get("clientCapabilities", {}).get("_meta")
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion"),
            "agentCapabilities": {"loadSession": True}}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": "kas-stub-session", "modes": {"currentModeId": "default"}}})
        status("kas-stub-session", status="failed", failedAuthorization=True,
               authorizationUrl=%(dead)r, errorMessage="Unauthorized")
    elif method == "_kiro/mcp/resetServer":
        seen["reset"] = params
        pending_reset = mid
        status(params["sessionId"], status="connecting")
        send({"jsonrpc": "2.0", "id": 900, "method": "_kiro/openExternalUrl",
              "params": {"url": %(live)r}})
    elif mid == 900 and method is None:
        seen["openReply"] = msg.get("result")
        status("kas-stub-session", status="connected", tools=[{"name": "ping"}])
        send({"jsonrpc": "2.0", "id": pending_reset, "result": {"success": True}})
    elif mid is not None and method is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    with open(CAPTURE, "w") as fh:
        json.dump(seen, fh)
""" % {"dead": _DEAD_URL, "live": _LIVE_URL}

_STUB_AGENT_SPEC = {
    "name": "kirocrew",
    "description": "sign-in stub",
    "prompt": "You are a test agent.",
    "tools": [],
    "allowedTools": [],
}


@pytest.fixture
def kas_sign_in_stub(tmp_path, monkeypatch):
    """The KAS relay argv, served by the stub agent on this interpreter."""
    script = tmp_path / "kas_stub.py"
    script.write_text(_STUB_AGENT)
    launcher = tmp_path / "kiro-cli-stub"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    launcher.chmod(0o755)

    async def fake_bin(*, environ=None, home=None) -> str:
        return str(launcher)

    monkeypatch.setattr("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", fake_bin)
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro-home"))
    capture = tmp_path / "capture.json"
    monkeypatch.setenv("KIROCREW_TEST_SIGN_IN_CAPTURE", str(capture))
    from kiro_crew.agent import kiro_agents_dir_path

    for agents_dir in {kiro_agents_dir(), kiro_agents_dir_path()}:
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / f"{_STUB_AGENT_SPEC['name']}.json").write_text(
            json.dumps(_STUB_AGENT_SPEC), encoding="utf-8"
        )
    return capture


@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
@pytest.mark.asyncio
async def test_an_unauthorized_server_gets_the_live_consent_url(kas_sign_in_stub, tmp_path):
    runtime = AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off", acp_backend=ACP_BACKEND_KAS)
    try:
        await runtime.spawn()
        handle = await runtime.create_session(cwd=tmp_path / "ws")
        # Session start reads the failed status without offering; the offer
        # is a turn's. Make it here, then drain the frames it produces.
        assert handle._mcp_sign_in_needed == {"remote"}
        assert runtime._mcp_sign_in is None
        handle._offer_mcp_sign_in()
        requests: list[dict[str, str]] = []
        for _ in range(20):
            requests += handle.pop_pending_oauth_requests()
            if requests and runtime._mcp_sign_in is None:
                break
            await handle.drain_init(duration=0.5, idle_exit=0.2)
        seen = json.loads(kas_sign_in_stub.read_text())
    finally:
        await runtime.kill()

    assert seen["clientMeta"]["kiro"]["openExternalUrl"] is True
    assert seen["reset"] == {
        "sessionId": "kas-stub-session",
        "serverName": "remote",
        "startOAuth": True,
    }
    assert seen["openReply"] == {"success": True}
    assert requests == [{"serverName": "remote", "oauthUrl": _LIVE_URL}]
    assert runtime._mcp_sign_in is None
    assert handle._mcp_sign_in_needed == set()


# ── runtime: the consent URL and the one sign-in slot ─────────────────────────


def _runtime() -> AcpRuntime:
    runtime = AcpRuntime(acp_backend=ACP_BACKEND_KAS)
    runtime._session_queues["s1"] = asyncio.Queue()
    runtime._session_queues["s2"] = asyncio.Queue()
    return runtime


def _capture_answers(runtime: AcpRuntime, monkeypatch) -> list[tuple]:
    answers: list[tuple] = []

    async def send_response(request_id, result):
        answers.append(("result", request_id, result))

    async def send_error(request_id, code, message):
        answers.append(("error", request_id, code))

    monkeypatch.setattr(runtime, "send_response", send_response)
    monkeypatch.setattr(runtime, "send_error", send_error)
    return answers


@pytest.mark.asyncio
async def test_a_url_with_no_sign_in_in_flight_is_not_opened(monkeypatch):
    runtime = _runtime()
    replies = _capture_answers(runtime, monkeypatch)
    await runtime._answer_open_external_url(7, {"url": _LIVE_URL})
    assert replies == [("error", 7, -32000)]
    assert runtime._session_queues["s1"].empty() and runtime._session_queues["s2"].empty()


@pytest.mark.asyncio
async def test_the_url_goes_to_the_session_that_started_the_sign_in(monkeypatch):
    runtime = _runtime()
    replies = _capture_answers(runtime, monkeypatch)
    runtime._mcp_sign_in = ("s2", "remote")
    await runtime._answer_open_external_url(8, {"url": _LIVE_URL})
    assert replies == [("result", 8, {"success": True})]
    assert runtime._session_queues["s1"].empty()
    frame = runtime._session_queues["s2"].get_nowait()
    assert frame.method == METHOD_MCP_OAUTH_REQUEST
    assert frame.params == {"sessionId": "s2", "serverName": "remote", "oauthUrl": _LIVE_URL}


@pytest.mark.asyncio
async def test_only_one_sign_in_runs_per_process(monkeypatch):
    runtime = _runtime()
    gate = asyncio.Event()

    async def send_and_await(method, params, timeout=0):
        await gate.wait()
        return {}

    monkeypatch.setattr(runtime, "_send_and_await", send_and_await)
    assert runtime.begin_mcp_sign_in("s1", "a") is True
    assert runtime.begin_mcp_sign_in("s2", "b") is False
    gate.set()
    await asyncio.gather(*runtime._answer_tasks)
    assert runtime._mcp_sign_in is None
    assert runtime.begin_mcp_sign_in("s2", "b") is True


@pytest.mark.asyncio
async def test_a_departed_sessions_link_is_refused_not_handed_on(monkeypatch):
    runtime = _runtime()
    replies = _capture_answers(runtime, monkeypatch)
    runtime._mcp_sign_in = ("s1", "a")
    runtime.unregister_session("s1")
    assert runtime._mcp_sign_in == ("s1", "a")
    assert runtime.begin_mcp_sign_in("s2", "b") is False
    await runtime._answer_open_external_url(9, {"url": _LIVE_URL})
    assert replies == [("error", 9, -32000)]
    assert runtime._session_queues["s2"].empty()


@pytest.mark.asyncio
async def test_a_non_http_url_is_refused_and_not_queued(monkeypatch):
    runtime = _runtime()
    replies = _capture_answers(runtime, monkeypatch)
    runtime._mcp_sign_in = ("s1", "remote")
    await runtime._answer_open_external_url(10, {"url": "javascript:alert(1)"})
    assert replies == [("error", 10, -32000)]
    assert runtime._session_queues["s1"].empty()


def test_a_host_without_the_capability_starts_nothing():
    runtime = AcpRuntime()
    runtime._session_queues["s1"] = asyncio.Queue()
    assert runtime.begin_mcp_sign_in("s1", "a") is False


# ── session handle: which servers still need a sign-in ────────────────────────


def _handle(begun: list, accept: bool = True) -> AcpSessionHandle:
    def begin(session_id, name):
        begun.append((session_id, name))
        return accept

    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._session_id = "s1"
    handle._runtime = SimpleNamespace(begin_mcp_sign_in=begin)
    handle._mcp_sign_in_needed = set()
    handle._mcp_sign_in_completed = set()
    handle._mcp_sign_in_last_offered = ""
    handle._mcp_sign_in_dropped = 0
    handle._oauth_emitted_servers = {"remote"}
    return handle


def _status(*servers, session_id="s1") -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS,
        params={"sessionId": session_id, "servers": list(servers)},
    )


def test_an_authorization_failure_starts_a_sign_in_and_clears_the_old_link():
    begun: list = []
    handle = _handle(begun)
    handle._note_mcp_sign_in_status(
        _status({"name": "remote", "status": "failed", "failedAuthorization": True}),
        offer=True,
    )
    assert begun == [("s1", "remote")]
    assert handle._oauth_emitted_servers == set()


def test_a_timed_out_sign_in_is_offered_again_until_the_server_connects():
    begun: list = []
    handle = _handle(begun, accept=False)
    handle._note_mcp_sign_in_status(
        _status({"name": "remote", "status": "failed", "failedAuthorization": True}),
        offer=True,
    )
    handle._note_mcp_sign_in_status(
        _status({"name": "remote", "status": "failed", "failedAuthorization": False}),
        offer=True,
    )
    assert handle._mcp_sign_in_needed == {"remote"}
    handle._note_mcp_sign_in_status(_status({"name": "remote", "status": "connected"}), offer=True)
    assert handle._mcp_sign_in_needed == set()


def test_a_status_read_without_offer_tracks_the_server_and_starts_nothing():
    begun: list = []
    handle = _handle(begun)
    handle._note_mcp_sign_in_status(
        _status({"name": "remote", "status": "failed", "failedAuthorization": True}),
        offer=False,
    )
    assert begun == [] and handle._mcp_sign_in_needed == {"remote"}
    handle._offer_mcp_sign_in()
    assert begun == [("s1", "remote")]


def test_an_oversized_server_name_is_not_tracked():
    begun: list = []
    handle = _handle(begun)
    handle._note_mcp_sign_in_status(
        _status({"name": "x" * 129, "status": "failed", "failedAuthorization": True}),
        offer=True,
    )
    assert begun == [] and handle._mcp_sign_in_needed == set()


def test_entries_past_the_cap_are_counted_and_logged_once(caplog):
    begun: list = []
    handle = _handle(begun, accept=False)
    servers = [{"name": f"s{i}", "status": "connected"} for i in range(66)]
    with caplog.at_level("WARNING", logger="kiro_crew.acp.session_handle"):
        handle._note_mcp_sign_in_status(_status(*servers), offer=True)
        handle._note_mcp_sign_in_status(_status(*servers), offer=True)
    warnings = [r for r in caplog.records if "skipped 2 status entries" in r.getMessage()]
    assert len(warnings) == 1
    assert handle._mcp_sign_in_dropped == 2


def test_a_completion_past_the_cap_is_dropped_and_counted(caplog):
    begun: list = []
    handle = _handle(begun, accept=False)
    handle._mcp_sign_in_completed = {f"done{i}" for i in range(BUCKET_CAP)}
    handle._mcp_sign_in_needed = {"remote"}
    with caplog.at_level("WARNING", logger="kiro_crew.acp.session_handle"):
        handle._note_mcp_sign_in_status(
            _status({"name": "remote", "status": "connected"}), offer=True
        )
    assert len(handle._mcp_sign_in_completed) == BUCKET_CAP
    assert "remote" not in handle._mcp_sign_in_completed
    assert handle._mcp_sign_in_needed == set()
    assert handle._mcp_sign_in_dropped == 1
    assert any("skipped 1 status entry" in r.getMessage() for r in caplog.records)


def test_a_completion_already_held_is_kept_at_the_cap():
    begun: list = []
    handle = _handle(begun, accept=False)
    handle._mcp_sign_in_completed = {f"done{i}" for i in range(BUCKET_CAP)}
    handle._mcp_sign_in_needed = {"done0"}
    handle._note_mcp_sign_in_status(_status({"name": "done0", "status": "connected"}), offer=True)
    assert len(handle._mcp_sign_in_completed) == BUCKET_CAP
    assert handle._mcp_sign_in_dropped == 0


def test_another_sessions_status_is_ignored():
    begun: list = []
    handle = _handle(begun)
    handle._note_mcp_sign_in_status(
        _status(
            {"name": "remote", "status": "failed", "failedAuthorization": True},
            session_id="s2",
        ),
        offer=True,
    )
    assert begun == [] and handle._mcp_sign_in_needed == set()


# ── session handle: the turn boundary ─────────────────────────────────────────


def _turn_handle(begun: list, accept: bool = True) -> tuple[AcpSessionHandle, asyncio.Queue]:
    """A real handle on a KAS runtime whose sign-in start is recorded."""
    runtime = AcpRuntime(acp_backend=ACP_BACKEND_KAS)
    runtime._initialized = True
    queue: asyncio.Queue = asyncio.Queue()
    runtime._session_queues["s1"] = queue
    runtime.send_request = AsyncMock(return_value=9)

    def begin(session_id, name):
        begun.append((session_id, name))
        return accept

    runtime.begin_mcp_sign_in = begin
    return AcpSessionHandle("s1", queue, runtime), queue


async def _first_event(handle: AcpSessionHandle) -> AcpEvent | None:
    """Start one turn; the first event its dispatch loop yields, if any."""
    gen = handle.prompt("hi", timeout=0.2)
    event = None
    with contextlib.suppress(StopAsyncIteration, asyncio.TimeoutError):
        event = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    await gen.aclose()
    return event


@pytest.mark.asyncio
async def test_the_turn_start_offers_a_sign_in_the_session_still_needs():
    begun: list = []
    handle, _queue = _turn_handle(begun)
    handle._mcp_sign_in_needed = {"remote"}
    handle._oauth_emitted_servers = {"remote"}
    await _first_event(handle)
    assert begun == [("s1", "remote")]
    assert handle._oauth_emitted_servers == set()


@pytest.mark.asyncio
async def test_a_server_connected_during_build_is_not_reset_after_the_frame(monkeypatch):
    order: list = []
    handle, queue = _turn_handle([])

    def begin(session_id, name):
        order.append(("begin", name))
        return True

    handle._runtime.begin_mcp_sign_in = begin
    handle._mcp_sign_in_needed = {"remote"}
    real_to_thread = asyncio.to_thread

    async def to_thread(func, *args, **kwargs):
        result = await real_to_thread(func, *args, **kwargs)
        if getattr(func, "__name__", "") == "build_prompt_blocks":
            # The server connects while the prompt is being built.
            order.append(("connected", "remote"))
            queue.put_nowait(_status({"name": "remote", "status": "connected"}))
        return result

    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    await _first_event(handle)
    assert order == [("begin", "remote"), ("connected", "remote")]
    assert handle._mcp_sign_in_needed == set()


@pytest.mark.asyncio
async def test_the_pre_turn_drain_keeps_this_sessions_consent_url_for_the_turn():
    begun: list = []
    handle, queue = _turn_handle(begun, accept=False)
    handle._mcp_sign_in_needed = {"remote"}
    handle._runtime._mcp_sign_in = ("s1", "remote")
    queue.put_nowait(
        JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"sessionId": "s1", "serverName": "remote", "oauthUrl": _LIVE_URL},
        )
    )
    # A leftover the abandoned turn owes to nobody: still dropped.
    queue.put_nowait(
        JsonRpcMessage.from_dict({"jsonrpc": "2.0", "id": 4, "result": {"stopReason": "cancelled"}})
    )
    event = await _first_event(handle)
    assert event is not None and event.kind == EVENT_MCP_OAUTH_REQUEST
    assert (event.server_name, event.oauth_url, event.runtime_global) == (
        "remote",
        _LIVE_URL,
        False,
    )
    assert queue.empty()


@pytest.mark.asyncio
async def test_the_pre_turn_drain_drops_another_sessions_consent_url():
    begun: list = []
    handle, queue = _turn_handle(begun, accept=False)
    queue.put_nowait(
        JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"sessionId": "s2", "serverName": "remote", "oauthUrl": _LIVE_URL},
        )
    )
    event = await _first_event(handle)
    assert event is None or event.kind != EVENT_MCP_OAUTH_REQUEST
    assert queue.empty()


@pytest.mark.asyncio
async def test_a_server_that_connected_between_turns_is_not_reset():
    begun: list = []
    handle, queue = _turn_handle(begun)
    handle._mcp_sign_in_needed = {"remote"}
    queue.put_nowait(_status({"name": "remote", "status": "connected"}))
    await _first_event(handle)
    assert begun == []
    assert handle._mcp_sign_in_needed == set()


async def _events(handle: AcpSessionHandle, limit: int = 8) -> list[AcpEvent]:
    """Start one turn; every event its dispatch loop yields, up to ``limit``."""
    gen = handle.prompt("hi", timeout=0.2)
    events: list[AcpEvent] = []
    with contextlib.suppress(StopAsyncIteration, asyncio.TimeoutError):
        while len(events) < limit:
            events.append(await asyncio.wait_for(gen.__anext__(), timeout=1.0))
    await gen.aclose()
    return events


def _initialized(events: list[AcpEvent]) -> list[tuple]:
    return [
        (e.server_name, e.runtime_global) for e in events if e.kind == EVENT_MCP_SERVER_INITIALIZED
    ]


def _arrives_mid_turn(handle: AcpSessionHandle, queue: asyncio.Queue, *frames) -> None:
    """Put ``frames`` on the queue once the prompt is written, after the drain."""

    async def send_request(method, params):
        for frame in frames:
            queue.put_nowait(frame)
        return 9

    handle._runtime.send_request = send_request


@pytest.mark.asyncio
async def test_a_tracked_server_that_connects_mid_turn_completes_its_sign_in():
    handle, queue = _turn_handle([], accept=False)
    handle._mcp_sign_in_needed = {"remote"}
    handle._oauth_emitted_servers = {"remote"}
    _arrives_mid_turn(
        handle,
        queue,
        _status({"name": "remote", "status": "connected"}),
        _status({"name": "remote", "status": "connected"}),
    )
    events = await _events(handle)
    assert _initialized(events) == [("remote", False)]
    assert handle._mcp_sign_in_needed == set()
    assert handle._mcp_sign_in_completed == set()
    assert handle._oauth_emitted_servers == set()


@pytest.mark.asyncio
async def test_an_untracked_connected_server_completes_nothing():
    handle, queue = _turn_handle([], accept=False)
    handle._oauth_emitted_servers = {"other"}
    _arrives_mid_turn(handle, queue, _status({"name": "other", "status": "connected"}))
    events = await _events(handle)
    assert _initialized(events) == []
    assert handle._oauth_emitted_servers == {"other"}


@pytest.mark.asyncio
async def test_a_sign_in_completed_between_turns_is_yielded_on_the_next_turn():
    begun: list = []
    handle, queue = _turn_handle(begun)
    handle._mcp_sign_in_needed = {"remote"}
    queue.put_nowait(_status({"name": "remote", "status": "connected"}))
    # The turn's first frame: an unrelated snapshot carrying no completion.
    _arrives_mid_turn(handle, queue, _status({"name": "other", "status": "connected"}))
    events = await _events(handle)
    assert begun == []
    assert _initialized(events) == [("remote", False)]


@pytest.mark.parametrize("slot", [None, ("s2", "remote"), ("s1", "other")])
@pytest.mark.asyncio
async def test_the_pre_turn_drain_drops_a_consent_url_without_its_slot(slot):
    handle, queue = _turn_handle([], accept=False)
    handle._runtime._mcp_sign_in = slot
    queue.put_nowait(
        JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"sessionId": "s1", "serverName": "remote", "oauthUrl": _DEAD_URL},
        )
    )
    event = await _first_event(handle)
    assert event is None or event.kind != EVENT_MCP_OAUTH_REQUEST
    assert "remote" not in handle._oauth_emitted_servers
    assert queue.empty()


@pytest.mark.asyncio
async def test_the_pre_turn_drain_drops_a_link_when_the_runtime_has_no_slot_query():
    handle, queue = _turn_handle([], accept=False)
    handle._runtime.mcp_sign_in_holds = None
    queue.put_nowait(
        JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"sessionId": "s1", "serverName": "remote", "oauthUrl": _DEAD_URL},
        )
    )
    event = await _first_event(handle)
    assert event is None or event.kind != EVENT_MCP_OAUTH_REQUEST
    assert queue.empty()


@pytest.mark.asyncio
async def test_a_stale_link_does_not_deduplicate_the_fresh_turn_start_link():
    begun: list = []
    handle, queue = _turn_handle(begun)
    handle._mcp_sign_in_needed = {"remote"}
    queue.put_nowait(
        JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"sessionId": "s1", "serverName": "remote", "oauthUrl": _DEAD_URL},
        )
    )

    def begin(session_id, name):
        begun.append((session_id, name))
        handle._runtime._mcp_sign_in = (session_id, name)
        queue.put_nowait(
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"sessionId": session_id, "serverName": name, "oauthUrl": _LIVE_URL},
            )
        )
        return True

    handle._runtime.begin_mcp_sign_in = begin
    event = await _first_event(handle)
    assert event is not None and event.kind == EVENT_MCP_OAUTH_REQUEST
    assert event.oauth_url == _LIVE_URL
    assert begun == [("s1", "remote")]
    assert queue.empty()


@pytest.mark.asyncio
async def test_timed_out_servers_take_turns_in_the_sign_in_slot(monkeypatch):
    runtime = _runtime()
    begun: list = []

    async def timeout(method, params, **kwargs):
        begun.append((params["sessionId"], params["serverName"]))
        raise asyncio.TimeoutError

    monkeypatch.setattr(runtime, "_send_and_await", timeout)
    handle = AcpSessionHandle("s1", runtime._session_queues["s1"], runtime)
    handle._mcp_sign_in_needed = {"a", "b"}
    for name in ("a", "b", "a", "b"):
        handle._offer_mcp_sign_in()
        assert runtime.mcp_sign_in_holds("s1", name)
        assert not runtime.mcp_sign_in_holds("s2", name)
        assert not runtime.mcp_sign_in_holds("s1", "other")
        await asyncio.gather(*runtime._answer_tasks)
        assert not runtime.mcp_sign_in_holds("s1", name)
    assert begun == [("s1", "a"), ("s1", "b"), ("s1", "a"), ("s1", "b")]


def test_rotation_continues_after_the_last_offered_server_leaves():
    begun: list = []
    handle = _handle(begun)
    handle._mcp_sign_in_last_offered = "b"
    handle._mcp_sign_in_needed = {"a", "c"}
    handle._offer_mcp_sign_in()
    assert begun == [("s1", "c")]
