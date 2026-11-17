"""A managed MCP server that ran out of time starting is re-attempted once, in band.

On a small or busy KAS host a restart cold-starts every session's private backends
at once, so ``kirocrew-core`` or ``kirocrew-cron`` can miss a start deadline: the
gateway's first-``initialize`` deadline, or KAS's own connect timeout. Either one
ends in a ``failed`` status entry, and a failed entry ends the session start. These
tests pin the one re-attempt the readiness barrier makes for such a failure through
``_kiro/mcp/resetServer``: which failures qualify, that a status frame sent before
the host processed the reset cannot end the retry, and that a second real failure,
a non-timeout failure or a refused re-attempt fails the start with the same error
as a start with no re-attempt.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_handle_runtime_contract import AcpRequestTimeout, AcpRuntimeError
from kiro_crew.acp.types import METHOD_KAS_MCP_STATUS, METHOD_KAS_TOOLS_CHANGED, JsonRpcMessage
from kiro_crew.mcp_gateway import backend_handshake

# The classifier is imported per call, so a tree without it fails each test that asks
# it something rather than stopping the whole run at collection.


def is_start_timeout_failure(error_message: object) -> bool:
    from kiro_crew.acp import mcp_reattempt

    return mcp_reattempt.is_start_timeout_failure(error_message)


_SID = "sess-start-reattempt"
_CLIENT = {"kiro": {"resource": {"source": {"origin": "client"}}}}
_GATEWAY_DEADLINE = "backend init failed: initialize did not complete within 30s"
_KAS_CONNECT_TIMEOUT = "MCP error -32001: connection timed out after 60000ms"


def _status(*servers: dict) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS, params={"sessionId": _SID, "servers": list(servers)}
    )


def _entry(name: str, status: str, error: str | None = None, **extra: object) -> dict:
    entry: dict = {"name": name, "status": status, "_meta": _CLIENT, **extra}
    if status == "connected":
        entry["tools"] = [{"name": "ping", "description": "probe", "disabled": False}]
    if error is not None:
        entry["errorMessage"] = error
    return entry


class _Recorder:
    """``begin_mcp_reattempt`` stand-in: records each call, answers *accept*."""

    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.calls: list[tuple[str, str]] = []

    def __call__(self, session_id: str, server_name: str) -> bool:
        self.calls.append((session_id, server_name))
        return self.accept


def _handle(
    *frames: JsonRpcMessage, recorder: _Recorder | None = None, reattempts: bool = True
) -> AcpSessionHandle:
    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._session_id = _SID
    handle._mcp_report = McpSessionReport()
    handle._mcp_report.begin_session([])
    handle._queue = asyncio.Queue()
    for frame in frames:
        handle._queue.put_nowait(frame)
    handle._apply_init_notification = lambda *_a, **_k: None  # type: ignore[method-assign]
    if recorder is not None:
        handle._runtime = SimpleNamespace(
            reattempts_mcp_servers=reattempts, begin_mcp_reattempt=recorder
        )
    return handle


# ── which failures qualify ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        _GATEWAY_DEADLINE,
        _KAS_CONNECT_TIMEOUT,
        "Connection Timed Out",
        "request timed out while connecting",
    ],
)
def test_a_start_deadline_failure_qualifies(message):
    assert is_start_timeout_failure(message) is True


@pytest.mark.parametrize(
    "message",
    [
        None,
        "",
        42,
        "spawn kirocrew ENOENT",
        "MCP error -32000: Connection closed",
        "invalid timeout value in config",
        "outtimed",
        "No AWS credentials available",
    ],
)
def test_any_other_failure_does_not_qualify(message):
    assert is_start_timeout_failure(message) is False


@pytest.mark.asyncio
async def test_the_gateway_deadline_reason_qualifies_as_the_stub_reports_it():
    # Drive the gateway's own deadline so the predicate is pinned to the reason it
    # really builds, not a copy of it.
    reasons: list[str] = []

    async def _fail_init(reason: str, **_kw: object) -> None:
        reasons.append(reason)

    async def _shutdown() -> None:
        return None

    # A live process: the deadline takes the timed-out branch, not the died-in-handshake one.
    backend = SimpleNamespace(
        _init_state="in_flight",
        _dead_reason=None,
        _gateway_ending=False,
        process=SimpleNamespace(returncode=None),
        _fail_init=_fail_init,
        shutdown=_shutdown,
    )
    await backend_handshake._init_deadline(backend, 0)

    assert len(reasons) == 1
    assert reasons[0].startswith(backend_handshake.INITIALIZE_DEADLINE_REASON)
    assert is_start_timeout_failure(f"backend init failed: {reasons[0]}") is True


# ── the barrier ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [_GATEWAY_DEADLINE, _KAS_CONNECT_TIMEOUT])
async def test_a_timed_out_managed_server_is_reattempted_once_and_the_start_succeeds(error):
    recorder = _Recorder()
    handle = _handle(
        _status(_entry("kirocrew-core", "failed", error), _entry("kirocrew-cron", "connected")),
        _status(_entry("kirocrew-core", "connecting"), _entry("kirocrew-cron", "connected")),
        _status(_entry("kirocrew-core", "connected"), _entry("kirocrew-cron", "connected")),
        recorder=recorder,
    )

    await handle.wait_mcp_ready(("kirocrew-core", "kirocrew-cron"), 5.0)

    assert recorder.calls == [(_SID, "kirocrew-core")]


@pytest.mark.asyncio
async def test_each_server_timed_out_in_one_snapshot_gets_its_own_reattempt():
    recorder = _Recorder()
    handle = _handle(
        _status(
            _entry("kirocrew-core", "failed", _GATEWAY_DEADLINE),
            _entry("kirocrew-cron", "failed", _KAS_CONNECT_TIMEOUT),
        ),
        _status(_entry("kirocrew-core", "connecting"), _entry("kirocrew-cron", "connecting")),
        _status(_entry("kirocrew-core", "connected"), _entry("kirocrew-cron", "connected")),
        recorder=recorder,
    )

    await handle.wait_mcp_ready(("kirocrew-core", "kirocrew-cron"), 5.0)

    assert recorder.calls == [(_SID, "kirocrew-core"), (_SID, "kirocrew-cron")]


@pytest.mark.asyncio
async def test_a_second_failure_after_the_reattempt_fails_the_start():
    recorder = _Recorder()
    handle = _handle(
        _status(_entry("kirocrew-core", "failed", _GATEWAY_DEADLINE)),
        _status(_entry("kirocrew-core", "connecting")),
        _status(_entry("kirocrew-core", "failed", _GATEWAY_DEADLINE)),
        recorder=recorder,
    )

    with pytest.raises(AcpRuntimeError, match="KAS managed MCP initialization failed"):
        await handle.wait_mcp_ready(("kirocrew-core",), 5.0)

    assert recorder.calls == [(_SID, "kirocrew-core")]


@pytest.mark.asyncio
async def test_a_failed_frame_sent_before_the_reset_does_not_end_the_reattempt():
    recorder = _Recorder()
    builtin_tags = JsonRpcMessage(
        method=METHOD_KAS_TOOLS_CHANGED,
        params={"sessionId": _SID, "tags": [{"source": "builtin", "tag": "read"}]},
    )
    handle = _handle(
        _status(_entry("kirocrew-core", "failed", _GATEWAY_DEADLINE)),
        # The host sent these before it processed the reset: still the old failure.
        _status(_entry("kirocrew-core", "failed", _GATEWAY_DEADLINE)),
        builtin_tags,
        _status(_entry("kirocrew-core", "failed", _GATEWAY_DEADLINE)),
        _status(_entry("kirocrew-core", "connecting")),
        _status(_entry("kirocrew-core", "connected")),
        recorder=recorder,
    )

    await handle.wait_mcp_ready(("kirocrew-core",), 5.0)

    assert recorder.calls == [(_SID, "kirocrew-core")]


@pytest.mark.asyncio
async def test_a_snapshot_that_omits_the_server_does_not_count_as_the_reset_acted_on():
    recorder = _Recorder()
    handle = _handle(
        _status(_entry("kirocrew-core", "failed", _GATEWAY_DEADLINE)),
        # Says nothing about the re-attempted server, so the next failed entry
        # can still be the one from before the reset.
        _status(_entry("kirocrew-cron", "connected")),
        _status(_entry("kirocrew-core", "failed", _GATEWAY_DEADLINE)),
        _status(_entry("kirocrew-core", "connecting")),
        _status(_entry("kirocrew-core", "connected")),
        recorder=recorder,
    )

    await handle.wait_mcp_ready(("kirocrew-core",), 5.0)

    assert recorder.calls == [(_SID, "kirocrew-core")]


@pytest.mark.asyncio
async def test_a_reattempt_the_host_never_acts_on_times_out_naming_it():
    recorder = _Recorder()
    handle = _handle(
        _status(_entry("kirocrew-core", "failed", _GATEWAY_DEADLINE)),
        _status(_entry("kirocrew-core", "failed", _GATEWAY_DEADLINE)),
        recorder=recorder,
    )

    with pytest.raises(AcpRequestTimeout, match="kirocrew-core: re-attempting"):
        await handle.wait_mcp_ready(("kirocrew-core",), 0.2)

    assert recorder.calls == [(_SID, "kirocrew-core")]


@pytest.mark.asyncio
async def test_a_reattempted_server_a_snapshot_leaves_out_still_reads_as_reattempting():
    recorder = _Recorder()
    handle = _handle(
        _status(_entry("kirocrew-core", "failed", _GATEWAY_DEADLINE)),
        # Lists another server only: nothing here says the host acted on the reset.
        _status(_entry("kirocrew-cron", "connected")),
        recorder=recorder,
    )

    with pytest.raises(AcpRequestTimeout, match="kirocrew-core: re-attempting"):
        await handle.wait_mcp_ready(("kirocrew-core",), 0.2)

    assert recorder.calls == [(_SID, "kirocrew-core")]


@pytest.mark.asyncio
async def test_a_failure_that_is_not_a_timeout_is_not_reattempted():
    recorder = _Recorder()
    handle = _handle(
        _status(_entry("kirocrew-core", "failed", "spawn kirocrew ENOENT")),
        _status(_entry("kirocrew-core", "connected")),
        recorder=recorder,
    )

    with pytest.raises(AcpRuntimeError, match="ENOENT"):
        await handle.wait_mcp_ready(("kirocrew-core",), 5.0)

    assert recorder.calls == []


@pytest.mark.asyncio
async def test_a_failed_authorization_is_not_reattempted_here():
    recorder = _Recorder()
    handle = _handle(
        _status(_entry("kirocrew-core", "failed", _KAS_CONNECT_TIMEOUT, failedAuthorization=True)),
        _status(_entry("kirocrew-core", "connected")),
        recorder=recorder,
    )

    with pytest.raises(AcpRuntimeError, match="authorization failed"):
        await handle.wait_mcp_ready(("kirocrew-core",), 5.0)

    assert recorder.calls == []


@pytest.mark.asyncio
async def test_a_host_without_the_reset_request_fails_the_start_as_before():
    recorder = _Recorder()
    handle = _handle(
        _status(_entry("kirocrew-core", "failed", _GATEWAY_DEADLINE)),
        _status(_entry("kirocrew-core", "connected")),
        recorder=recorder,
        reattempts=False,
    )

    with pytest.raises(AcpRuntimeError, match="did not complete within 30s"):
        await handle.wait_mcp_ready(("kirocrew-core",), 5.0)

    assert recorder.calls == []


@pytest.mark.asyncio
async def test_a_refused_reattempt_fails_the_start_as_before():
    recorder = _Recorder(accept=False)
    handle = _handle(
        _status(_entry("kirocrew-core", "failed", _GATEWAY_DEADLINE)),
        _status(_entry("kirocrew-core", "connected")),
        recorder=recorder,
    )

    with pytest.raises(AcpRuntimeError, match="KAS managed MCP initialization failed"):
        await handle.wait_mcp_ready(("kirocrew-core",), 5.0)

    assert recorder.calls == [(_SID, "kirocrew-core")]


@pytest.mark.asyncio
async def test_a_handle_with_no_runtime_fails_the_start_as_before():
    handle = _handle(
        _status(_entry("kirocrew-core", "failed", _GATEWAY_DEADLINE)),
        _status(_entry("kirocrew-core", "connected")),
    )

    with pytest.raises(AcpRuntimeError, match="KAS managed MCP initialization failed"):
        await handle.wait_mcp_ready(("kirocrew-core",), 5.0)
