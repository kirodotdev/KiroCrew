"""A host that fail-closes MCP on a governance read failure must fail the start fast.

On an enterprise account the KAS host reads governance during ``session/new``;
when that read fails it emits ``_kiro/mcp/governance_disabled`` with
``reason: "api_failure"`` under the new session's id, then reports an empty
``servers`` snapshot. The host does not re-read governance during that start,
so the managed servers will not report and the readiness wait spent its whole
budget on a verdict that already arrived. With the method in the KAS
``mcp_init`` aliases the frame is staged before the ``session/new`` response
and reaches the session queue, where an undeclared ``wait_mcp_ready`` raises
the transient failure at once. Declared registry mode keeps its settle
checkpoint, as for a registry drop, and ``admin_disabled`` and every other
reason keep the ordinary path. Wire shape from the KAS 2.32.0 / 2.37.0
engines: ``{sessionId, reason}``.
"""

from __future__ import annotations

import asyncio

import pytest
from acp_runtime_helpers import _drain, _feed, _make_runtime, _start_reader, _stop_reader

from kiro_crew.acp import session_handle_mcp
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_handle_mcp import GOVERNED_DROP_REASON
from kiro_crew.acp.session_handle_runtime_contract import (
    AcpRequestTimeout,
    AcpRuntimeError,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    METHOD_KAS_MCP_STATUS,
    JsonRpcMessage,
)

#: The host's wire spelling, pinned as written by the KAS engine rather than
#: read back from Crew's own constant.
METHOD_KAS_MCP_GOVERNANCE_DISABLED = "_kiro/mcp/governance_disabled"

_SID = "sess-governed"
_REQUIRED = ("kirocrew-core", "kirocrew-cron")


def _disabled(session_id: str, reason: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_GOVERNANCE_DISABLED,
        params={"sessionId": session_id, "reason": reason},
    )


def _connected_status(session_id: str = _SID) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS,
        params={
            "sessionId": session_id,
            "servers": [
                {
                    "name": name,
                    "status": "connected",
                    "tools": [{"name": "ping", "description": "probe", "disabled": False}],
                    "_meta": {"kiro": {"resource": {"source": {"origin": "client"}}}},
                }
                for name in _REQUIRED
            ],
        },
    )


def _handle(*frames: JsonRpcMessage) -> AcpSessionHandle:
    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._session_id = _SID
    handle._mcp_report = McpSessionReport()
    handle._mcp_report.begin_session([])
    handle._queue = asyncio.Queue()
    for frame in frames:
        handle._queue.put_nowait(frame)
    handle._apply_init_notification = lambda *_a, **_k: None  # type: ignore[method-assign]
    return handle


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
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "method": METHOD_KAS_MCP_GOVERNANCE_DISABLED,
                "params": {"sessionId": _SID, "reason": "api_failure"},
            },
        )
        await _drain(reader)
        assert [msg.method for msg in rt._pending_init_notifications] == staged
    finally:
        await _stop_reader(task)


class TestApiFailureFailsFast:
    @pytest.mark.asyncio
    async def test_api_failure_for_this_session_fails_at_once(self):
        handle = _handle(_disabled(_SID, "api_failure"))
        with pytest.raises(AcpRuntimeError, match="governance read failed") as raised:
            await handle.wait_mcp_ready(_REQUIRED, 30.0)
        assert "api_failure" in str(raised.value)
        assert "transient" in str(raised.value)
        assert "send the message again" in str(raised.value)

    @pytest.mark.asyncio
    async def test_api_failure_beats_a_pending_stale_frame(self):
        # A stale-counted queue entry still surfaces the governance verdict:
        # the count describes the PRE-switch roster and the frame is
        # session-scoped, so the skip must not swallow it.
        handle = _handle(_disabled(_SID, "api_failure"))
        with pytest.raises(AcpRuntimeError, match="governance read failed"):
            await handle.wait_mcp_ready(_REQUIRED, 30.0, stale_report_frames=1)

    @pytest.mark.asyncio
    async def test_api_failure_after_a_connecting_status_still_fails(self):
        connecting = JsonRpcMessage(
            method=METHOD_KAS_MCP_STATUS,
            params={
                "sessionId": _SID,
                "servers": [{"name": "kirocrew-core", "status": "connecting"}],
            },
        )
        handle = _handle(connecting, _disabled(_SID, "api_failure"))
        with pytest.raises(AcpRuntimeError, match="governance read failed"):
            await handle.wait_mcp_ready(_REQUIRED, 30.0)

    @pytest.mark.asyncio
    async def test_another_sessions_api_failure_is_not_ours(self):
        handle = _handle(_disabled("other", "api_failure"), _connected_status())
        await handle.wait_mcp_ready(_REQUIRED, 0.2)


class TestUnaffectedPaths:
    @pytest.mark.asyncio
    async def test_admin_disabled_keeps_the_ordinary_wait(self):
        handle = _handle(_disabled(_SID, "admin_disabled"))
        with pytest.raises(AcpRequestTimeout):
            await handle.wait_mcp_ready(_REQUIRED, 0.2)

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
    async def test_declared_registry_mode_keeps_its_settle_checkpoint(self, monkeypatch):
        """Governed, an api_failure start is decided as before: it starts without them.

        A declared install already accepts a start without Crew's servers, as the
        registry-drop branch does; failing it here would turn a start that worked
        into an error for exactly the operators who declared that posture.
        """
        monkeypatch.setattr(session_handle_mcp, "GOVERNED_SETTLE_SECS", 0)
        empty = JsonRpcMessage(
            method=METHOD_KAS_MCP_STATUS, params={"sessionId": _SID, "servers": []}
        )
        handle = _handle(_disabled(_SID, "api_failure"), empty)
        await handle.wait_mcp_ready(_REQUIRED, 30.0, governed=True)
        report = handle._mcp_report
        assert report._failed == list(_REQUIRED)
        assert all(report._failures[n] == GOVERNED_DROP_REASON for n in _REQUIRED)
