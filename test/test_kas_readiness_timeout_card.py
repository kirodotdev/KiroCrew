"""A chat whose agent's built-in tools never reported reads plainly.

The KAS readiness barrier tags its timeout, and the chat's terminal row reads
that tag, never the message, to carry the ``tools_not_ready`` notice. The
dashboard words the card from the token; the message stays the row's English
content and the log line. With no ``code`` in the meta the card keeps Resume.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_handle_runtime_contract import AcpRequestTimeout
from kiro_crew.dashboard.chat_turn.recovery import _terminal_error_meta


def _silent_handle() -> AcpSessionHandle:
    """A session whose host never sends a status frame."""
    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._session_id = "sess-readiness"
    handle._mcp_report = McpSessionReport()
    handle._mcp_report.begin_session([])
    handle._queue = asyncio.Queue()
    handle._apply_init_notification = lambda *_a, **_k: None  # type: ignore[method-assign]
    return handle


@pytest.mark.asyncio
async def test_the_readiness_timeout_row_carries_the_plain_card_token():
    with pytest.raises(AcpRequestTimeout) as raised:
        await _silent_handle().wait_mcp_ready(("kirocrew-core",), 0.05)
    assert "KAS managed MCP readiness timed out" in str(raised.value)
    assert _terminal_error_meta(raised.value) == {"notice": "tools_not_ready"}
    # Any other request timeout keeps its plain row.
    assert _terminal_error_meta(AcpRequestTimeout("initialize timed out")) is None
