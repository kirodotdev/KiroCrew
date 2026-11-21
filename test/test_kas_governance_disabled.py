"""A start whose MCP the host turned off on a failed governance read fails fast.

On an enterprise account the KAS host reads governance during ``session/new``.
When that read fails it fail-closes MCP: it emits
``_kiro/mcp/governance_disabled`` with ``reason: "api_failure"`` under the new
session's id and reports a status snapshot that lists no servers. Before this
fix the readiness wait spent its whole budget (90 s) on servers that could not
come.

The reason string alone is not proof: the host re-mounts a session's MCP and
sends a fresh status when a later governance read succeeds. So the frame arms a
short checkpoint, and the start fails only if by then the host has named none
of the required servers for this session. A server named before then keeps the
ordinary wait, so a start that would have succeeded still does. Declared
registry mode keeps its own checkpoint, and ``admin_disabled``, other reasons,
another session's frame and no frame keep the ordinary path. Wire shape read
from the KAS 3.4.0 engine: ``{sessionId, reason}``.
"""

from __future__ import annotations

import asyncio

import pytest
from acp_runtime_helpers import _drain, _feed, _make_runtime, _start_reader, _stop_reader
from session_handle_helpers import bare_session_handle

from kiro_crew.acp import session_handle_mcp
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_handle_mcp import GOVERNED_DROP_REASON, KasGovernanceOffError
from kiro_crew.acp.session_handle_runtime_contract import AcpRequestTimeout, AcpRuntimeDead
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    METHOD_KAS_MCP_STATUS,
    JsonRpcMessage,
)
from kiro_crew.llm_helpers import acp_error_is_transient

#: The host's wire spelling, pinned as written by the KAS engine rather than
#: read back from Crew's own constant.
METHOD_KAS_MCP_GOVERNANCE_DISABLED = "_kiro/mcp/governance_disabled"

_SID = "sess-governed"
_REQUIRED = ("kirocrew-core", "kirocrew-cron")
#: A short wait budget for the tests that expect the ordinary timeout.
_BUDGET = 0.2


@pytest.fixture(autouse=True)
def _no_settle(_floor_monkeypatch):
    """A zero checkpoint: it passes on the loop turn right after the frame arms it."""
    _floor_monkeypatch.setattr(session_handle_mcp, "GOVERNED_SETTLE_SECS", 0)


@pytest.fixture
def long_settle(monkeypatch):
    """A checkpoint the queued frames always beat, for tests where they must."""
    monkeypatch.setattr(
        session_handle_mcp,
        "GOVERNED_SETTLE_SECS",
        30.0,  # flake-ok: long on purpose, never waited out
    )


def _disabled(session_id: str, reason: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_GOVERNANCE_DISABLED,
        params={"sessionId": session_id, "reason": reason},
    )


def _status(servers: list[dict], session_id: str = _SID) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS, params={"sessionId": session_id, "servers": servers}
    )


def _empty_status(session_id: str = _SID) -> JsonRpcMessage:
    return _status([], session_id)


def _connected_status(session_id: str = _SID) -> JsonRpcMessage:
    return _status(
        [
            {
                "name": name,
                "status": "connected",
                "tools": [{"name": "ping", "description": "probe", "disabled": False}],
                "_meta": {"kiro": {"resource": {"source": {"origin": "client"}}}},
            }
            for name in _REQUIRED
        ],
        session_id,
    )


def _connecting_status() -> JsonRpcMessage:
    return _status(
        [
            {
                "name": name,
                "status": "connecting",
                "_meta": {"kiro": {"resource": {"source": {"origin": "client"}}}},
            }
            for name in _REQUIRED
        ]
    )


def _handle(*frames: JsonRpcMessage) -> AcpSessionHandle:
    handle = bare_session_handle(runtime=None)
    handle._session_id = _SID
    handle._mcp_report = McpSessionReport()
    handle._mcp_report.begin_session([])
    handle._queue = asyncio.Queue()
    for frame in frames:
        handle._queue.put_nowait(frame)
    handle._apply_init_notification = lambda *_a, **_k: None  # type: ignore[method-assign]
    return handle


_GOVERNANCE_FRAME = {
    "jsonrpc": "2.0",
    "method": METHOD_KAS_MCP_GOVERNANCE_DISABLED,
    "params": {"sessionId": _SID, "reason": "api_failure"},
}


@pytest.mark.parametrize(
    ("backend", "staged"),
    [
        (ACP_BACKEND_KAS, [METHOD_KAS_MCP_GOVERNANCE_DISABLED]),
        # The kiro harness keeps its legacy vocabulary; the frame is KAS's own.
        (ACP_BACKEND_KIRO, []),
    ],
)
@pytest.mark.asyncio
async def test_the_reader_stages_the_frame_before_the_session_is_known(backend, staged):
    """During session/new the id is not registered yet: an unstaged frame is lost."""
    rt, reader, _ = _make_runtime()
    rt._acp_backend = backend
    rt._session_inits_in_flight = 1
    task = await _start_reader(rt)
    try:
        _feed(reader, _GOVERNANCE_FRAME)
        await _drain(reader)
        assert [msg.method for msg in rt._pending_init_notifications] == staged
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_after_the_response_the_frame_reaches_the_session_queue():
    """Once the session is registered the frame routes to its own queue."""
    rt, reader, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KAS
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues[_SID] = queue
    task = await _start_reader(rt)
    try:
        _feed(reader, _GOVERNANCE_FRAME)
        await _drain(reader)
        assert queue.get_nowait().method == METHOD_KAS_MCP_GOVERNANCE_DISABLED
    finally:
        await _stop_reader(task)


class TestMcpOffFailsFast:
    @pytest.mark.parametrize(
        "frames",
        [
            pytest.param((_disabled(_SID, "api_failure"), _empty_status()), id="frame-then-empty"),
            pytest.param((_empty_status(), _disabled(_SID, "api_failure")), id="empty-then-frame"),
            pytest.param((_disabled(_SID, "api_failure"),), id="frame-alone"),
        ],
    )
    @pytest.mark.asyncio
    async def test_api_failure_with_no_server_named_fails_at_the_checkpoint(self, frames):
        # A 30 s budget: the deadline would raise AcpRequestTimeout, so the
        # AcpRuntimeError proves the checkpoint ended the wait.
        handle = _handle(*frames)
        with pytest.raises(KasGovernanceOffError) as raised:
            await handle.wait_mcp_ready(_REQUIRED, 30.0)
        assert "send the message again" in str(raised.value)

    @pytest.mark.asyncio
    async def test_the_governance_failure_stays_retry_eligible(self):
        # Retry-eligible like a readiness timeout: a new start re-reads
        # governance.
        handle = _handle(_disabled(_SID, "api_failure"))
        with pytest.raises(KasGovernanceOffError) as raised:
            await handle.wait_mcp_ready(_REQUIRED, 30.0)
        assert acp_error_is_transient(raised.value) is True

    @pytest.mark.asyncio
    async def test_a_stale_counted_frame_still_arms_the_checkpoint(self):
        # The stale count describes the PRE-switch roster; the governance frame
        # is session-scoped, so the skip must not swallow it.
        handle = _handle(_disabled(_SID, "api_failure"))
        with pytest.raises(KasGovernanceOffError):
            await handle.wait_mcp_ready(_REQUIRED, 30.0, stale_report_frames=1)


class TestTheMessageSpeaksToThePerson:
    """The chat shows this text: plain words, and a resend only while it can help."""

    @pytest.mark.asyncio
    async def test_the_message_names_no_internal_term(self):
        handle = _handle(_disabled(_SID, "api_failure"))
        with pytest.raises(KasGovernanceOffError) as raised:
            await handle.wait_mcp_ready(_REQUIRED, 30.0)
        text = str(raised.value)
        assert "on this machine" in text
        for internal in ("KAS", "managed MCP", "governance read", "api_failure", "host"):
            assert internal not in text

    @pytest.mark.asyncio
    async def test_a_repeat_names_what_to_check_instead_of_a_resend(self):
        for _ in range(2):
            with pytest.raises(KasGovernanceOffError) as raised:
                await _handle(_disabled(_SID, "api_failure")).wait_mcp_ready(_REQUIRED, 30.0)
        text = str(raised.value)
        assert "send the message again" not in text
        assert "A fresh start failed the same way" in text
        assert "signed in" in text
        assert "MCP settings" in text

    @pytest.mark.asyncio
    async def test_a_start_that_gets_through_clears_the_count(self):
        with pytest.raises(KasGovernanceOffError):
            await _handle(_disabled(_SID, "api_failure")).wait_mcp_ready(_REQUIRED, 30.0)
        await _handle(_connected_status()).wait_mcp_ready(_REQUIRED, 30.0)
        with pytest.raises(KasGovernanceOffError) as raised:
            await _handle(_disabled(_SID, "api_failure")).wait_mcp_ready(_REQUIRED, 30.0)
        assert "send the message again" in str(raised.value)


class TestAServerNamedKeepsTheOrdinaryWait:
    @pytest.mark.asyncio
    async def test_servers_connecting_after_api_failure_still_start(self, long_settle):
        handle = _handle(_disabled(_SID, "api_failure"), _connected_status())
        await handle.wait_mcp_ready(_REQUIRED, 30.0)

    @pytest.mark.asyncio
    async def test_a_status_inside_the_checkpoint_wins_over_an_empty_one(self, long_settle):
        # The host re-read governance and re-mounted MCP: its fresh status
        # arrives after the empty one, inside the checkpoint.
        handle = _handle(_disabled(_SID, "api_failure"), _empty_status(), _connected_status())
        await handle.wait_mcp_ready(_REQUIRED, 60.0)

    @pytest.mark.asyncio
    async def test_a_named_server_that_never_connects_times_out_as_before(self, long_settle):
        handle = _handle(_disabled(_SID, "api_failure"), _connecting_status())
        with pytest.raises(AcpRequestTimeout):
            await handle.wait_mcp_ready(_REQUIRED, _BUDGET)

    @pytest.mark.asyncio
    async def test_connected_after_the_checkpoint_keeps_waiting_once_named(self):
        # Named (connecting) before the zero checkpoint passes, connected after it.
        handle = _handle(_connecting_status(), _disabled(_SID, "api_failure"), _connected_status())
        await handle.wait_mcp_ready(_REQUIRED, 30.0)


class TestAReadyStartIsNeverFailed:
    """The engine can send the frame after a governance change, past readiness."""

    @pytest.mark.asyncio
    async def test_a_frame_after_ready_is_left_unread(self):
        frame = _disabled(_SID, "api_failure")
        handle = _handle(_connected_status(), frame)
        await handle.wait_mcp_ready(_REQUIRED, 30.0)
        assert handle._queue.get_nowait() is frame

    @pytest.mark.asyncio
    async def test_a_frame_after_one_server_connected_keeps_the_wait(self):
        # Only the first required server connected; the second is still pending.
        first = _REQUIRED[0]
        one = _status(
            [
                {
                    "name": first,
                    "status": "connected",
                    "tools": [{"name": "ping", "description": "probe", "disabled": False}],
                    "_meta": {"kiro": {"resource": {"source": {"origin": "client"}}}},
                }
            ]
        )
        handle = _handle(one, _disabled(_SID, "api_failure"), _connected_status())
        await handle.wait_mcp_ready(_REQUIRED, 30.0)


class TestQueuedFramesAreReadFirst:
    """The zero checkpoint has passed before the next queued frame is read."""

    @pytest.mark.asyncio
    async def test_a_queued_connected_status_still_starts(self):
        handle = _handle(_disabled(_SID, "api_failure"), _connected_status())
        await handle.wait_mcp_ready(_REQUIRED, 30.0)

    @pytest.mark.asyncio
    async def test_a_queued_death_marker_reports_runtime_death(self):
        handle = _handle(_disabled(_SID, "api_failure"), None)  # type: ignore[arg-type]
        with pytest.raises(AcpRuntimeDead):
            await handle.wait_mcp_ready(_REQUIRED, 30.0)


class TestUnaffectedPaths:
    @pytest.mark.asyncio
    async def test_another_sessions_api_failure_is_not_ours(self):
        handle = _handle(_disabled("other", "api_failure"), _empty_status())
        with pytest.raises(AcpRequestTimeout):
            await handle.wait_mcp_ready(_REQUIRED, _BUDGET)

    @pytest.mark.asyncio
    async def test_another_sessions_api_failure_does_not_block_our_start(self):
        handle = _handle(_disabled("other", "api_failure"), _connected_status())
        await handle.wait_mcp_ready(_REQUIRED, 0.2)

    @pytest.mark.asyncio
    async def test_admin_disabled_keeps_the_ordinary_wait(self):
        handle = _handle(_disabled(_SID, "admin_disabled"), _empty_status())
        with pytest.raises(AcpRequestTimeout):
            await handle.wait_mcp_ready(_REQUIRED, _BUDGET)

    @pytest.mark.asyncio
    async def test_no_governance_frame_times_out_as_before(self):
        handle = _handle()
        with pytest.raises(AcpRequestTimeout, match="kirocrew doctor"):
            await handle.wait_mcp_ready(_REQUIRED, 0.2)

    @pytest.mark.asyncio
    async def test_a_connected_roster_still_starts(self):
        handle = _handle(_connected_status())
        await handle.wait_mcp_ready(_REQUIRED, 30.0)

    @pytest.mark.asyncio
    async def test_declared_registry_mode_keeps_its_settle_checkpoint(self):
        """Governed, an api_failure start is decided as before: it starts without them.

        A declared install already accepts a start without Crew's servers, as the
        registry-drop branch does; failing it here would turn a start that worked
        into an error for exactly the operators who declared that posture.
        """
        handle = _handle(_disabled(_SID, "api_failure"), _empty_status())
        await handle.wait_mcp_ready(_REQUIRED, 30.0, governed=True)
        report = handle._mcp_report
        assert report._failed == list(_REQUIRED)
        assert all(report._failures[n] == GOVERNED_DROP_REASON for n in _REQUIRED)
