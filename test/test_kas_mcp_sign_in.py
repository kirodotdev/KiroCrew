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
from kiro_crew.testing.wait import finished

_DEAD_URL = "https://auth.example.com/authorize?state=passive"
_LIVE_URL = "https://auth.example.com/authorize?state=explicit"

#: A KAS-shaped stub: after session/new it reports one remote server as needing
#: authorization; a resetServer with startOAuth makes it ask the client to open
#: the live URL, then report the server connected, then answer the reset.
#:
#: Each capture is written before the reply it belongs to and swapped in whole,
#: so the test, which the reset's answer unblocks, never reads a missing or
#: half-written file. ``KIROCREW_TEST_SIGN_IN_STALL`` holds the stub inside its
#: last capture write for that many seconds, standing in for a descheduled process.
_STUB_AGENT = """
import json, os, sys, time

CAPTURE = os.environ["KIROCREW_TEST_SIGN_IN_CAPTURE"]
STALL = float(os.environ.get("KIROCREW_TEST_SIGN_IN_STALL") or 0)
seen = {}

def record(key, value, stall=0.0):
    seen[key] = value
    partial = CAPTURE + ".partial"
    with open(partial, "w") as fh:
        json.dump(seen, fh)
        time.sleep(stall)
    os.replace(partial, CAPTURE)

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
        record("clientMeta", params.get("clientCapabilities", {}).get("_meta"))
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion"),
            "agentCapabilities": {"loadSession": True}}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": "kas-stub-session", "modes": {"currentModeId": "default"}}})
        status("kas-stub-session", status="failed", failedAuthorization=True,
               authorizationUrl=%(dead)r, errorMessage="Unauthorized")
    elif method == "_kiro/mcp/resetServer":
        record("reset", params)
        pending_reset = mid
        status(params["sessionId"], status="connecting")
        send({"jsonrpc": "2.0", "id": 900, "method": "_kiro/openExternalUrl",
              "params": {"url": %(live)r}})
    elif mid == 900 and method is None:
        record("openReply", msg.get("result"), stall=STALL)
        status("kas-stub-session", status="connected", tools=[{"name": "ping"}])
        send({"jsonrpc": "2.0", "id": pending_reset, "result": {"success": True}})
    elif mid is not None and method is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
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
    await _sign_in_through_the_stub(kas_sign_in_stub, tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
@pytest.mark.asyncio
async def test_the_capture_is_whole_however_long_the_stub_takes_to_write_it(
    kas_sign_in_stub, tmp_path, monkeypatch
):
    # Longer than the drain's idle exit, so a capture written after the
    # reset's answer would still be empty when the test reads it.
    monkeypatch.setenv("KIROCREW_TEST_SIGN_IN_STALL", "0.5")
    await _sign_in_through_the_stub(kas_sign_in_stub, tmp_path)


async def _sign_in_through_the_stub(kas_sign_in_stub, tmp_path) -> None:
    runtime = AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off", acp_backend=ACP_BACKEND_KAS)
    try:
        await runtime.spawn()
        handle = await runtime.create_session(cwd=tmp_path / "ws")
        # Session start reads the failed status without offering, and an
        # offer point no message asked for starts nothing. A message naming
        # the server does at the next offer point; then drain its frames.
        assert handle._mcp_sign_in_needed == {"remote"}
        assert runtime._mcp_sign_in is None
        handle._offer_mcp_sign_in()
        assert runtime._mcp_sign_in is None
        handle.want_mcp_sign_in_named("sign in to remote")
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
    handle._mcp_sign_in_wanted = set()
    handle._mcp_sign_in_ask = ""
    handle._mcp_sign_in_asked = set()
    handle._mcp_sign_in_completed = set()
    handle._mcp_sign_in_dropped = 0
    handle._mcp_reattempt_waiting = set()
    handle._mcp_reattempt_counts = {}
    handle._oauth_emitted_servers = {"remote"}
    return handle


def _status(*servers, session_id="s1") -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS,
        params={"sessionId": session_id, "servers": list(servers)},
    )


def test_an_authorization_failure_alone_starts_no_sign_in():
    begun: list = []
    handle = _handle(begun)
    handle._note_mcp_sign_in_status(
        _status({"name": "remote", "status": "failed", "failedAuthorization": True}),
        offer=True,
    )
    handle._offer_mcp_sign_in()
    assert begun == []
    assert handle._mcp_sign_in_needed == {"remote"}
    assert handle._oauth_emitted_servers == {"remote"}


def test_a_message_naming_the_server_starts_its_sign_in_and_clears_the_old_link():
    begun: list = []
    handle = _handle(begun)
    handle._note_mcp_sign_in_status(
        _status({"name": "remote", "status": "failed", "failedAuthorization": True}),
        offer=True,
    )
    handle.want_mcp_sign_in_named("can you sign me in to Remote?")
    # Nothing starts until an offer point, where a turn reads the link.
    assert begun == []
    handle._offer_mcp_sign_in()
    assert begun == [("s1", "remote")]
    assert handle._oauth_emitted_servers == set()
    assert handle._mcp_sign_in_wanted == set()


@pytest.mark.parametrize(
    "text",
    [
        "summarize this file",
        "the remotely hosted build failed",
        "notional numbers only",
        "",
    ],
)
def test_a_message_that_names_no_unsigned_server_starts_nothing(text):
    begun: list = []
    handle = _handle(begun)
    handle._mcp_sign_in_needed = {"remote", "notion"}
    handle.want_mcp_sign_in_named(text)
    handle._offer_mcp_sign_in()
    assert begun == []
    assert handle._mcp_sign_in_wanted == set()


def test_a_message_naming_a_connected_server_starts_nothing():
    begun: list = []
    handle = _handle(begun)
    handle._mcp_sign_in_needed = {"remote"}
    handle.want_mcp_sign_in_named("search notion for the roadmap")
    handle._offer_mcp_sign_in()
    assert begun == []


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("notion", "Use NOTION to find it"),
        ("power-figma-figma", "pull the frame from Figma"),
        ("power-figma-figma", "sign in to power figma figma"),
        ("lennysdata", "check lennysdata for that"),
        ("beehive:beehive-mcp", "open beehive"),
        ("docs_server", "ask the docs server"),
        # The configured name counts at any length and even when generic.
        ("gh", "sign in to gh"),
        ("GH", "Sign in to gh"),
        ("mcp", "sign in to mcp"),
        ("server", "use the server"),
    ],
)
def test_a_server_is_named_by_its_name_or_an_obvious_alias(name, text):
    from kiro_crew.acp.session_handle_mcp import text_names_mcp_server

    assert text_names_mcp_server(text, name)


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("power-figma-figma", "a power user"),
        # A multi-word name's last word alone is everyday prose, not a name.
        ("power-aws-docs-search", "please search the web"),
        ("acme:api", "call the api"),
        ("acme:api-server", "hit the api"),
        ("acme:api-mcp", "hit the api"),
        ("beehive:beehive-mcp", "start an mcp server"),
        ("docs_server", "the server is down"),
        ("notion", "notions of done"),
        ("gh", "open github"),
        ("mcp", "the mcpserver config"),
    ],
)
def test_a_generic_word_or_a_longer_word_does_not_name_a_server(name, text):
    from kiro_crew.acp.session_handle_mcp import text_names_mcp_server

    assert not text_names_mcp_server(text, name)


@pytest.mark.parametrize(
    ("name", "aliases"),
    [
        ("gh", ("gh",)),
        ("mcp", ("mcp",)),
        # Derived aliases stay filtered: ``gh`` (suffix-stripped) and
        # ``ab`` (power reduction) are under the minimum, ``mcp`` is generic.
        ("gh-mcp", ("gh mcp",)),
        ("power-ab-mcp", ("power ab mcp", "power ab", "ab mcp")),
        ("power-aws-docs-search", ("power aws docs search", "aws docs search")),
        ("acme:api", ("acme api",)),
        ("figma:figma", ("figma figma", "figma")),
    ],
)
def test_the_configured_name_is_kept_and_short_derived_aliases_are_dropped(name, aliases):
    from kiro_crew.acp.session_handle_mcp import mcp_server_aliases

    assert mcp_server_aliases(name) == aliases


def test_a_dismissed_server_is_offered_again_only_when_named_again():
    begun: list = []
    handle = _handle(begun)
    handle._mcp_sign_in_needed = {"remote"}
    handle.want_mcp_sign_in_named("sign in to remote")
    handle._offer_mcp_sign_in()
    # The attempt lapsed: the user dismissed or ignored the card. Unrelated
    # messages and the status that keeps reporting it unsigned offer nothing.
    handle._note_mcp_sign_in_status(
        _status({"name": "remote", "status": "failed", "failedAuthorization": False}),
        offer=True,
    )
    for text in ("thanks", "what else is on my list?"):
        handle.want_mcp_sign_in_named(text)
        handle._offer_mcp_sign_in()
    assert begun == [("s1", "remote")]
    handle.want_mcp_sign_in_named("ok, remote again please")
    handle._offer_mcp_sign_in()
    assert begun == [("s1", "remote"), ("s1", "remote")]


def test_naming_a_server_whose_card_is_live_starts_no_second_one():
    begun: list = []
    handle = _handle(begun)
    handle._runtime.mcp_sign_in_holds = lambda sid, name: (sid, name) == ("s1", "remote")
    handle._mcp_sign_in_needed = {"remote"}
    handle.want_mcp_sign_in_named("remote, remote, remote")
    handle._offer_mcp_sign_in()
    assert begun == []
    assert handle._mcp_sign_in_wanted == set()


def test_several_named_servers_take_the_slot_one_at_a_time():
    begun: list = []
    accept = {"value": False}
    handle = _handle(begun)

    def begin(session_id, name):
        begun.append((session_id, name))
        return accept["value"]

    handle._runtime = SimpleNamespace(begin_mcp_sign_in=begin)
    handle._mcp_sign_in_needed = {"ax1", "bx1", "cx1"}
    handle.want_mcp_sign_in_named("sign in to bx1 and ax1")
    assert handle._mcp_sign_in_wanted == {"ax1", "bx1"}
    # The slot is busy: both stay wanted. "cx1" was never named.
    handle._offer_mcp_sign_in()
    accept["value"] = True
    begun.clear()
    for _ in range(4):
        handle._offer_mcp_sign_in()
    assert begun == [("s1", "ax1"), ("s1", "bx1")]
    assert handle._mcp_sign_in_wanted == set()


def test_a_wanted_server_that_connects_is_no_longer_wanted():
    handle = _handle([], accept=False)
    handle._mcp_sign_in_needed = {"remote"}
    handle.want_mcp_sign_in_named("remote")
    handle._note_mcp_sign_in_status(_status({"name": "remote", "status": "connected"}), offer=True)
    assert handle._mcp_sign_in_wanted == set()


def test_the_wanted_set_is_bounded_and_a_refusal_is_logged(caplog):
    handle = _handle([], accept=False)
    names = [f"srv{i:03d}" for i in range(BUCKET_CAP + 1)]
    handle._mcp_sign_in_needed = set(names)
    with caplog.at_level("INFO", logger="kiro_crew.acp.session_handle"):
        handle.want_mcp_sign_in_named(" ".join(names))
    assert len(handle._mcp_sign_in_wanted) == BUCKET_CAP
    assert any("not asked for" in r.getMessage() for r in caplog.records)


def test_a_timed_out_sign_in_keeps_the_server_tracked_until_it_connects():
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
    handle._mcp_sign_in_wanted = {"remote"}
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
        event = await finished(gen.__anext__(), "the turn's first event")
    await gen.aclose()
    return event


@pytest.mark.asyncio
async def test_the_turn_start_offers_a_sign_in_a_tool_call_asked_for():
    begun: list = []
    handle, _queue = _turn_handle(begun)
    handle._mcp_sign_in_needed = {"remote"}
    handle._mcp_sign_in_wanted = {"remote"}
    handle._oauth_emitted_servers = {"remote"}
    await _first_event(handle)
    assert begun == [("s1", "remote")]
    assert handle._oauth_emitted_servers == set()


async def _user_turn(handle: AcpSessionHandle, text: str) -> list[AcpEvent]:
    """One turn as the dashboard drives it: the user's words, then the prompt."""
    return await _events(handle, sign_in_ask=text)


@pytest.mark.asyncio
async def test_a_chat_that_never_names_the_server_is_never_offered_it():
    begun: list = []
    handle, queue = _turn_handle(begun)
    handle._mcp_sign_in_needed = {"remote", "other"}
    failed = {"status": "failed", "failedAuthorization": True}
    _arrives_mid_turn(
        handle,
        queue,
        _status({"name": "remote", **failed}, {"name": "other", **failed}),
    )
    for text in ("hello", "summarize the doc", "what is a remotely hosted build?"):
        await _user_turn(handle, text)
    assert begun == []


@pytest.mark.asyncio
async def test_a_message_naming_the_server_starts_one_sign_in_at_turn_start():
    begun: list = []
    handle, queue = _turn_handle(begun)
    handle._mcp_sign_in_needed = {"remote", "other"}
    _arrives_mid_turn(
        handle,
        queue,
        _status({"name": "remote", "status": "failed", "failedAuthorization": False}),
    )
    await _user_turn(handle, "please sign in to remote")
    assert begun == [("s1", "remote")]
    # Dismissed, then unrelated messages: nothing more is offered.
    await _user_turn(handle, "never mind")
    await _user_turn(handle, "what's next?")
    assert begun == [("s1", "remote")]


_UNSIGNED = {"name": "remote", "status": "failed", "failedAuthorization": True}


@pytest.mark.asyncio
async def test_a_named_server_whose_status_still_waits_in_the_queue_starts_at_turn_start():
    begun: list = []
    handle, queue = _turn_handle(begun)
    # Not tracked yet when the message is handed over: the pre-turn drain
    # reads this status, and the held ask is matched after it.
    queue.put_nowait(_status(_UNSIGNED))
    assert handle._mcp_sign_in_needed == set()
    await _user_turn(handle, "please sign in to remote")
    assert begun == [("s1", "remote")]


@pytest.mark.asyncio
async def test_a_named_server_whose_status_arrives_mid_turn_starts_its_sign_in():
    begun: list = []
    handle, queue = _turn_handle(begun)
    _arrives_mid_turn(handle, queue, _status(_UNSIGNED), _status(_UNSIGNED))
    await _user_turn(handle, "please sign in to remote")
    # Started once: the second snapshot finds the ask already answered.
    assert begun == [("s1", "remote")]


@pytest.mark.asyncio
async def test_a_carried_over_sign_in_is_not_restarted_by_a_mid_turn_status(monkeypatch):
    begun: list = []
    handle, queue = _turn_handle(begun)
    runtime = handle._runtime

    def begin(session_id, name):
        begun.append((session_id, name))
        return AcpRuntime.begin_mcp_sign_in(runtime, session_id, name)

    async def timeout(method, params, **kwargs):
        raise asyncio.TimeoutError

    monkeypatch.setattr(runtime, "begin_mcp_sign_in", begin)
    monkeypatch.setattr(runtime, "_send_and_await", timeout)
    handle._mcp_sign_in_needed = {"remote"}
    runtime._mcp_sign_in = ("s2", "other")
    await _user_turn(handle, "please sign in to remote")
    assert begun == [("s1", "remote")]
    assert handle._mcp_sign_in_wanted == {"remote"}
    assert handle._mcp_sign_in_ask == "" and handle._mcp_sign_in_asked == set()

    begun.clear()
    runtime._mcp_sign_in = None
    _arrives_mid_turn(handle, queue, _status(_UNSIGNED))
    deliver_status = runtime.send_request

    async def send_request(method, params):
        # The carried-over request started at turn start, then timed out
        # before the queued status still reports it unauthorized.
        assert begun == [("s1", "remote")]
        await asyncio.gather(*runtime._answer_tasks)
        assert runtime._mcp_sign_in is None
        return await deliver_status(method, params)

    monkeypatch.setattr(runtime, "send_request", send_request)
    await _user_turn(handle, "please sign in to remote again")
    await asyncio.gather(*runtime._answer_tasks)
    assert begun == [("s1", "remote")]
    assert handle._mcp_sign_in_wanted == set()


@pytest.mark.asyncio
async def test_a_later_turn_does_not_match_an_earlier_turns_message():
    begun: list = []
    handle, queue = _turn_handle(begun)
    await _user_turn(handle, "please sign in to remote")
    assert begun == [] and handle._mcp_sign_in_ask == ""
    # A cron or sub-agent turn hands no message: the old one is gone.
    queue.put_nowait(_status(_UNSIGNED))
    _arrives_mid_turn(handle, queue, _status(_UNSIGNED))
    await _events(handle)
    assert begun == []
    assert handle._mcp_sign_in_needed == {"remote"}
    assert handle._mcp_sign_in_ask == "" and handle._mcp_sign_in_asked == set()


def _sign_in_provider(seen: list):
    """Real provider hand-off, recording both old field and new argument paths."""
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.providers.acp import AcpProvider

    async def prompt(_message, *, sign_in_ask=""):
        seen.append(sign_in_ask)
        if False:
            yield None

    async def deliver(message, send, _incarnation):
        async for event in send(message):
            yield event

    inner = AcpSessionProvider.__new__(AcpSessionProvider)
    inner._handle = SimpleNamespace(prompt=prompt, want_mcp_sign_in_named=seen.append)
    inner._runtime = SimpleNamespace(acp_backend=ACP_BACKEND_KAS)
    inner.reclaim = lambda: None
    inner._claim_shared_turn = lambda: None
    inner._end_shared_turn = AsyncMock()
    inner.apply_pending_mcp_reinjection = AsyncMock()
    delivery = SimpleNamespace(stream=deliver, prepare_command=lambda _command: None)
    inner.essential_delivery = delivery
    outer = AcpProvider.__new__(AcpProvider)
    outer._client = inner
    outer.essential_delivery = delivery
    return outer, inner


async def _drain_provider(events):
    async for _event in events:
        pass


@pytest.mark.asyncio
async def test_the_provider_hands_the_users_message_to_the_new_handle_once():
    old: list = []
    seen: list = []
    outer, inner = _sign_in_provider(old)
    _other, replacement = _sign_in_provider(seen)
    outer.note_user_message("sign in to notion")
    assert old == seen == []

    async def reinject():
        inner._handle = replacement._handle

    inner.apply_pending_mcp_reinjection = reinject
    await _drain_provider(outer.stream("human turn"))
    await _drain_provider(outer.stream("automated turn"))
    assert (old, seen) == ([], ["sign in to notion", ""])


@pytest.mark.asyncio
async def test_a_failed_reinjection_does_not_reuse_the_ask_on_an_automated_turn():
    seen: list = []
    outer, inner = _sign_in_provider(seen)
    outer.note_user_message("sign in to notion")
    inner.apply_pending_mcp_reinjection = AsyncMock(side_effect=RuntimeError("reinjection failed"))
    with pytest.raises(RuntimeError, match="reinjection failed"):
        await _drain_provider(outer.stream("human turn"))
    assert seen == []
    inner.apply_pending_mcp_reinjection = AsyncMock()
    await _drain_provider(outer.stream("automated turn"))
    assert seen == [""]
    assert inner._end_shared_turn.await_count == 2


@pytest.mark.asyncio
async def test_a_failed_ready_confirmation_does_not_reuse_the_ask_on_an_automated_turn():
    seen: list = []
    outer, _inner = _sign_in_provider(seen)
    outer.note_user_message("sign in to notion")
    outer._ready_confirmed_session = AsyncMock(side_effect=RuntimeError("confirmation failed"))
    with pytest.raises(RuntimeError, match="confirmation failed"):
        await _drain_provider(outer.stream("human turn"))
    assert seen == []
    outer._ready_confirmed_session = AsyncMock()
    await _drain_provider(outer.stream("automated turn"))
    assert seen == [""]


@pytest.mark.asyncio
async def test_a_command_discards_a_noted_ask_for_itself_and_the_next_turn():
    seen: list = []
    outer, _inner = _sign_in_provider(seen)
    outer.note_user_message("sign in to notion")
    await _drain_provider(outer.stream_command("/help"))
    await _drain_provider(outer.stream("automated turn"))
    assert seen == ["", ""]


@pytest.mark.asyncio
async def test_a_failed_command_discards_a_noted_ask_before_confirmation():
    seen: list = []
    outer, _inner = _sign_in_provider(seen)
    outer.note_user_message("sign in to notion")
    outer._ready_confirmed_session = AsyncMock(side_effect=RuntimeError("confirmation failed"))
    with pytest.raises(RuntimeError, match="confirmation failed"):
        await _drain_provider(outer.stream_command("/help"))
    outer._ready_confirmed_session = AsyncMock()
    await _drain_provider(outer.stream("automated turn"))
    assert seen == [""]


def test_the_dashboards_provider_holds_the_users_message_only_for_shared_sessions():
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.providers.base import LLMProvider

    seen: list = []
    outer, inner = _sign_in_provider(seen)
    outer.note_user_message("sign in to notion")
    assert outer._sign_in_ask == "sign in to notion"
    assert not hasattr(inner, "_pending_sign_in_ask")
    assert seen == []
    # The direct client (kiro-cli) has no trigger; nothing is called or held.
    outer._sign_in_ask = ""
    outer._client = AcpClient.__new__(AcpClient)
    outer.note_user_message("notion")
    assert outer._sign_in_ask == ""
    # Every other provider takes the declared no-op.
    assert LLMProvider.note_user_message(outer, "notion") is None


@pytest.mark.asyncio
async def test_a_server_connected_during_build_is_not_reset_after_the_frame(monkeypatch):
    order: list = []
    handle, queue = _turn_handle([])

    def begin(session_id, name):
        order.append(("begin", name))
        return True

    handle._runtime.begin_mcp_sign_in = begin
    handle._mcp_sign_in_needed = {"remote"}
    handle._mcp_sign_in_wanted = {"remote"}
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
    handle._mcp_sign_in_wanted = {"remote"}
    queue.put_nowait(_status({"name": "remote", "status": "connected"}))
    await _first_event(handle)
    assert begun == []
    assert handle._mcp_sign_in_needed == set()


async def _events(
    handle: AcpSessionHandle, limit: int = 8, *, sign_in_ask: str = ""
) -> list[AcpEvent]:
    """Start one turn; every event its dispatch loop yields, up to ``limit``."""
    gen = handle.prompt("hi", timeout=0.2, sign_in_ask=sign_in_ask)
    events: list[AcpEvent] = []
    with contextlib.suppress(StopAsyncIteration, asyncio.TimeoutError):
        while len(events) < limit:
            events.append(await finished(gen.__anext__(), "the turn's next event"))
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
    handle._mcp_sign_in_wanted = {"remote"}
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
async def test_named_servers_take_the_slot_one_at_a_time_and_lapse_once(monkeypatch):
    runtime = _runtime()
    begun: list = []

    async def timeout(method, params, **kwargs):
        begun.append((params["sessionId"], params["serverName"]))
        raise asyncio.TimeoutError

    monkeypatch.setattr(runtime, "_send_and_await", timeout)
    handle = AcpSessionHandle("s1", runtime._session_queues["s1"], runtime)
    handle._mcp_sign_in_needed = {"alpha", "beta"}
    handle.want_mcp_sign_in_named("alpha")
    handle._offer_mcp_sign_in()
    assert runtime.mcp_sign_in_holds("s1", "alpha")
    # Naming "alpha" again while its card is live asks for nothing more; the
    # slot is held, so "beta" waits for the next offer point.
    handle.want_mcp_sign_in_named("alpha and beta")
    handle._offer_mcp_sign_in()
    assert handle._mcp_sign_in_wanted == {"beta"}
    assert not runtime.mcp_sign_in_holds("s1", "beta")
    await asyncio.gather(*runtime._answer_tasks)
    handle._offer_mcp_sign_in()
    assert runtime.mcp_sign_in_holds("s1", "beta")
    assert not runtime.mcp_sign_in_holds("s2", "beta")
    await asyncio.gather(*runtime._answer_tasks)
    # Both lapsed unanswered: neither is offered again until named again.
    handle._offer_mcp_sign_in()
    assert runtime._mcp_sign_in is None
    assert begun == [("s1", "alpha"), ("s1", "beta")]
