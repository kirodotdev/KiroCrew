"""Re-attempt of an MCP server that failed to start for want of a credential.

A stdio MCP server whose signer resolves credentials lazily fails its first
``initialize`` when the session starts before a credential exists, and the engine
never initializes it again. On KAS, Crew asks the engine to connect it again with
``_kiro/mcp/resetServer`` and ``startOAuth`` false, at a turn start, at most
``MAX_ATTEMPTS`` times per server per session. These tests pin which failures
qualify, the budget and its bound on names, and that a re-attempt never shares
the consent-URL slot with an OAuth sign-in.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp.mcp_reattempt import MAX_ATTEMPTS, is_recoverable_auth_failure
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import BUCKET_CAP, AcpSessionHandle
from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_KAS_MCP_STATUS, JsonRpcMessage

_NO_CREDS = "JSON-RPC error: -32603 No AWS credentials available"


# ── which failures qualify ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        _NO_CREDS,
        "UnauthorizedException: Unauthorized",
        "HTTP 401",
        "HTTP 403 Forbidden",
        "The security token included in the request is expired",
        "ExpiredToken: token has expired",
        "AccessDenied: access denied",
        "authentication required",
        "Unable to locate credentials",
        "NoCredentialsError: Unable to locate credentials",
        "botocore: the credentials have expired",
        "AWS credentials not found",
        "invalid credentials",
        "AccessDeniedException: Invalid token",
        "status code 403",
        "HTTP 403: Forbidden",
    ],
)
def test_a_credential_failure_is_recoverable(message):
    assert is_recoverable_auth_failure(message) is True


@pytest.mark.parametrize(
    "message",
    [
        None,
        "",
        "spawn uvx ENOENT",
        "Connection closed",
        "server exited with code 1",
        "listening on port 4013",
        "unknown author field",
        42,
    ],
)
def test_any_other_failure_is_left_alone(message):
    assert is_recoverable_auth_failure(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "loaded credentials from profile default; server exited with code 1",
        "ModuleNotFoundError: No module named 'credentials_helper'",
        "using credential_process from config, then spawn uvx ENOENT",
        "credentials ok; Connection closed",
        "ModuleNotFoundError: authentication module not found",
        "upstream answered with a forbidden content type",
        "parsed 403 rows, then exited with code 1",
        "listening on port 401",
    ],
)
def test_a_failure_that_only_mentions_a_credential_is_left_alone(message):
    # A credential or auth word alone is not a credential failure: re-attempting
    # these spends the budget on a server a later credential cannot fix.
    assert is_recoverable_auth_failure(message) is False


# ── session handle: tracking and the budget ───────────────────────────────────


def _handle(begun: list, accept: bool = True) -> AcpSessionHandle:
    def begin(session_id, name):
        begun.append((session_id, name))
        return accept

    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._session_id = "s1"
    handle._runtime = SimpleNamespace(
        begin_mcp_reattempt=begin,
        begin_mcp_sign_in=lambda *_: False,
        reattempts_mcp_servers=True,
    )
    handle._mcp_sign_in_needed = set()
    handle._mcp_sign_in_completed = set()
    handle._mcp_sign_in_last_offered = ""
    handle._mcp_sign_in_dropped = 0
    handle._mcp_reattempt_waiting = set()
    handle._mcp_reattempt_counts = {}
    handle._oauth_emitted_servers = set()
    return handle


def _status(*servers, session_id="s1") -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS,
        params={"sessionId": session_id, "servers": list(servers)},
    )


def _failed(name="aws", error=_NO_CREDS, **extra) -> dict:
    return {"name": name, "status": "failed", "errorMessage": error, **extra}


def test_a_credential_failure_is_re_attempted_at_most_the_budget():
    begun: list = []
    handle = _handle(begun)
    handle._note_mcp_sign_in_status(_status(_failed()), offer=False)
    assert handle._mcp_reattempt_waiting == {"aws"}
    for _ in range(MAX_ATTEMPTS + 2):
        handle._reattempt_failed_mcp_servers()
        handle._note_mcp_sign_in_status(_status(_failed()), offer=False)
    assert begun == [("s1", "aws")] * MAX_ATTEMPTS
    assert handle._mcp_reattempt_counts == {"aws": MAX_ATTEMPTS}


def test_a_refused_start_spends_no_attempt():
    begun: list = []
    handle = _handle(begun, accept=False)
    handle._note_mcp_sign_in_status(_status(_failed()), offer=False)
    for _ in range(MAX_ATTEMPTS + 1):
        handle._reattempt_failed_mcp_servers()
    assert len(begun) == MAX_ATTEMPTS + 1
    assert handle._mcp_reattempt_counts == {}


def test_a_non_credential_failure_is_never_re_attempted():
    begun: list = []
    handle = _handle(begun)
    handle._note_mcp_sign_in_status(_status(_failed(error="spawn uvx ENOENT")), offer=False)
    handle._reattempt_failed_mcp_servers()
    assert begun == [] and handle._mcp_reattempt_waiting == set()


def test_an_oauth_failure_goes_to_the_sign_in_not_the_re_attempt():
    begun: list = []
    handle = _handle(begun)
    handle._note_mcp_sign_in_status(
        _status(_failed(error="Unauthorized", failedAuthorization=True)), offer=False
    )
    handle._reattempt_failed_mcp_servers()
    assert begun == []
    assert handle._mcp_sign_in_needed == {"aws"}
    assert handle._mcp_reattempt_waiting == set()


def test_a_healthy_session_starts_nothing():
    begun: list = []
    handle = _handle(begun)
    handle._note_mcp_sign_in_status(_status({"name": "aws", "status": "connected"}), offer=False)
    handle._reattempt_failed_mcp_servers()
    assert begun == [] and handle._mcp_reattempt_waiting == set()


def test_a_re_attempted_server_that_connects_completes():
    begun: list = []
    handle = _handle(begun)
    handle._note_mcp_sign_in_status(_status(_failed()), offer=False)
    handle._reattempt_failed_mcp_servers()
    handle._note_mcp_sign_in_status(_status({"name": "aws", "status": "connected"}), offer=False)
    assert handle._mcp_reattempt_waiting == set()
    assert handle._mcp_sign_in_completed == {"aws"}


def test_a_server_never_re_attempted_completes_nothing_when_it_connects():
    handle = _handle([])
    handle._note_mcp_sign_in_status(_status(_failed()), offer=False)
    handle._note_mcp_sign_in_status(_status({"name": "aws", "status": "connected"}), offer=False)
    assert handle._mcp_sign_in_completed == set()


def test_a_server_that_leaves_the_snapshot_stops_waiting():
    begun: list = []
    handle = _handle(begun)
    handle._note_mcp_sign_in_status(_status(_failed()), offer=False)
    handle._note_mcp_sign_in_status(_status(), offer=False)
    handle._reattempt_failed_mcp_servers()
    assert begun == [] and handle._mcp_reattempt_waiting == set()


def test_another_sessions_failure_is_ignored():
    begun: list = []
    handle = _handle(begun)
    handle._note_mcp_sign_in_status(_status(_failed(), session_id="s2"), offer=False)
    handle._reattempt_failed_mcp_servers()
    assert begun == []


# ── session handle: the bound on names ────────────────────────────────────────


def test_no_more_than_the_cap_of_servers_is_ever_counted():
    begun: list = []
    handle = _handle(begun)
    handle._mcp_reattempt_counts = {f"done{i}": MAX_ATTEMPTS for i in range(BUCKET_CAP)}
    handle._mcp_reattempt_waiting = {"aws", "done0"}
    handle._reattempt_failed_mcp_servers()
    assert begun == []
    assert len(handle._mcp_reattempt_counts) == BUCKET_CAP


def test_a_server_already_counted_keeps_its_budget_at_the_cap():
    begun: list = []
    handle = _handle(begun)
    handle._mcp_reattempt_counts = {f"done{i}": MAX_ATTEMPTS for i in range(BUCKET_CAP - 1)}
    handle._mcp_reattempt_counts["aws"] = 1
    handle._mcp_reattempt_waiting = {"aws"}
    handle._reattempt_failed_mcp_servers()
    assert begun == [("s1", "aws")]
    assert handle._mcp_reattempt_counts["aws"] == 2


# ── runtime: the reset and the consent-URL slot ───────────────────────────────


def _runtime() -> AcpRuntime:
    runtime = AcpRuntime(acp_backend=ACP_BACKEND_KAS)
    runtime._session_queues["s1"] = asyncio.Queue()
    return runtime


@pytest.mark.asyncio
async def test_the_re_attempt_sends_a_reset_without_oauth(monkeypatch):
    runtime = _runtime()
    sent: list = []

    async def send_and_await(method, params, timeout=0):
        sent.append((method, params))
        return {}

    monkeypatch.setattr(runtime, "_send_and_await", send_and_await)
    assert runtime.begin_mcp_reattempt("s1", "aws") is True
    await asyncio.gather(*runtime._answer_tasks)
    assert sent == [
        (
            "_kiro/mcp/resetServer",
            {"sessionId": "s1", "serverName": "aws", "startOAuth": False},
        )
    ]
    assert runtime._mcp_reattempts == set()


@pytest.mark.asyncio
async def test_the_re_attempt_holds_the_slot_for_a_short_bound(monkeypatch):
    runtime = _runtime()
    timeouts: list = []

    async def send_and_await(method, params, timeout=0):
        timeouts.append(timeout)
        return {}

    monkeypatch.setattr(runtime, "_send_and_await", send_and_await)
    assert runtime.begin_mcp_reattempt("s1", "aws") is True
    await asyncio.gather(*runtime._answer_tasks)
    assert timeouts and timeouts[0] <= 90.0


@pytest.mark.asyncio
async def test_a_re_attempt_and_a_sign_in_never_overlap(monkeypatch):
    runtime = _runtime()
    gate = asyncio.Event()

    async def send_and_await(method, params, timeout=0):
        await gate.wait()
        return {}

    monkeypatch.setattr(runtime, "_send_and_await", send_and_await)
    assert runtime.begin_mcp_reattempt("s1", "aws") is True
    assert runtime.begin_mcp_reattempt("s1", "aws") is False  # same server, unanswered
    assert runtime.begin_mcp_sign_in("s1", "remote") is False  # its URL would be misread
    gate.set()
    await asyncio.gather(*runtime._answer_tasks)
    gate.clear()
    assert runtime.begin_mcp_sign_in("s1", "remote") is True
    assert runtime.begin_mcp_reattempt("s1", "aws") is False  # the slot is held
    gate.set()
    await asyncio.gather(*runtime._answer_tasks)


@pytest.mark.asyncio
async def test_a_failed_reset_frees_the_server_for_a_later_turn(monkeypatch):
    runtime = _runtime()
    monkeypatch.setattr(runtime, "_send_and_await", AsyncMock(side_effect=TimeoutError()))
    assert runtime.begin_mcp_reattempt("s1", "aws") is True
    await asyncio.gather(*runtime._answer_tasks)
    assert runtime._mcp_reattempts == set()


def test_a_host_without_the_reset_starts_nothing():
    runtime = AcpRuntime()
    runtime._session_queues["s1"] = asyncio.Queue()
    assert runtime.begin_mcp_reattempt("s1", "aws") is False


def test_an_unregistered_session_starts_nothing():
    runtime = _runtime()
    assert runtime.begin_mcp_reattempt("gone", "aws") is False


# ── session handle: the turn start ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_turn_start_re_attempts_a_waiting_server():
    runtime = AcpRuntime(acp_backend=ACP_BACKEND_KAS)
    runtime._initialized = True
    queue: asyncio.Queue = asyncio.Queue()
    runtime._session_queues["s1"] = queue
    begun: list = []

    class _Written(Exception):
        """Raised by the prompt write: the turn start has run by then."""

    runtime.send_request = AsyncMock(side_effect=_Written())
    runtime.begin_mcp_sign_in = lambda *_: False
    runtime.begin_mcp_reattempt = lambda sid, name: begun.append((sid, name)) or True
    handle = AcpSessionHandle("s1", queue, runtime)
    queue.put_nowait(_status(_failed()))  # read by the pre-turn drain
    with pytest.raises(_Written):
        async for _event in handle.prompt("hi", timeout=5):
            pass
    assert begun == [("s1", "aws")]
    assert handle._mcp_reattempt_counts == {"aws": 1}


# ── session handle: the restart hint waits for the budget ─────────────────────


def test_the_report_names_the_server_for_a_new_session_only_once_the_budget_is_spent():
    from kiro_crew.acp.mcp_session_report import McpSessionReport

    handle = _handle([])
    handle._mcp_report = McpSessionReport()
    frame = _status(_failed())
    handle._note_mcp_sign_in_status(frame, offer=False)
    handle._mcp_report.record_frame(frame, owned=True)
    assert handle._mcp_report.restart_to_load() == []
    for _ in range(MAX_ATTEMPTS):
        handle._reattempt_failed_mcp_servers()
    assert handle._mcp_report.restart_to_load() == ["aws"]


def test_a_host_without_the_reset_names_the_server_at_once():
    from kiro_crew.acp.mcp_session_report import McpSessionReport

    handle = _handle([])
    handle._runtime = SimpleNamespace(begin_mcp_sign_in=lambda *_: False)
    handle._mcp_report = McpSessionReport()
    frame = _status(_failed())
    handle._note_mcp_sign_in_status(frame, offer=False)
    handle._mcp_report.record_frame(frame, owned=True)
    assert handle._mcp_report.restart_to_load() == ["aws"]


def test_a_kiro_cli_session_names_a_credential_failure_for_a_new_session():
    from kiro_crew.acp.mcp_session_report import McpSessionReport

    runtime = AcpRuntime()  # kiro-cli: no per-server reset
    runtime._session_queues["s1"] = asyncio.Queue()
    assert runtime.reattempts_mcp_servers is False
    handle = _handle([])
    handle._runtime = runtime
    handle._mcp_report = McpSessionReport()
    frame = _status(_failed())
    handle._note_mcp_sign_in_status(frame, offer=False)
    handle._mcp_report.record_frame(frame, owned=True)
    handle._reattempt_failed_mcp_servers()
    assert handle._mcp_reattempt_counts == {}
    assert handle._mcp_report.restart_to_load() == ["aws"]


def test_a_kas_runtime_reattempts():
    assert _runtime().reattempts_mcp_servers is True
