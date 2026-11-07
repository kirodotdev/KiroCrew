"""A cron turn refuses a directive only a person's turn may issue, out loud.

No session applier reads a cron turn's stream, so the refusal is what tells the
job's reader that ``set_project`` changed nothing, and its SEL row records it.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cron_gateway_integration_helpers import _make_gw_for_llm, _make_llm_job

from kiro_crew import session_directive
from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_RESULT, AcpEvent

_REQUESTED = "Project change requested for this session."

_MARKER = session_directive.encode(
    "set_project", {"project": "/srv/other", "clear": False}, _REQUESTED
)
# How each harness names a core-MCP call and returns its result: kiro-cli by the
# trusted ``_meta.kiro`` pair with the marker readable, KAS by the wire title
# alone with the result re-serialised so no marker decodes.
_SHAPES = {
    "kiro-cli": (
        {"tool_name": "set_project", "mcp_server_name": session_directive.CORE_MCP_SERVER},
        _MARKER,
    ),
    "kas": (
        {"wire_title": f"Running: @{session_directive.CORE_MCP_SERVER}/set_project"},
        json.dumps({"response": _MARKER, "imageBase64Urls": [], "message": _MARKER}),
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_SHAPES))
async def test_cron_set_project_is_refused_in_the_result_and_audited_denied(shape, monkeypatch):
    audits: list[dict[str, Any]] = []

    class _SelSpy:
        def log_tool_invocation(self, **kw: Any) -> None:
            audits.append(kw)

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _SelSpy())
    call, output = _SHAPES[shape]

    async def stream(_client: Any, _msg: str, *, on_event: Any = None, **_k: Any) -> str:
        if on_event is not None:
            await on_event(AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-1", **call))
            await on_event(
                AcpEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="tc-1",
                    tool_output=output,
                    tool_final=True,
                )
            )
        return _REQUESTED

    gw = _make_gw_for_llm()
    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    job = _make_llm_job()
    captured: list[Any] = []

    def capture_cron(on_job: Any = None, **_kw: Any) -> MagicMock:
        captured.append(on_job)
        svc = MagicMock()
        svc.start = AsyncMock()
        return svc

    with (
        patch("kiro_crew.slack.gateway.CronService") as cron_cls,
        patch(
            "kiro_crew.slack.gateway.run_in_embed_pool",
            AsyncMock(return_value=("full prompt", None)),
        ),
        patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream),
        patch("kiro_crew.slack.gateway.sel"),
        patch(
            "kiro_crew.slack.gateway.build_cron_session_context",
            return_value=(f"cron:{job.id}", job.message),
        ),
        patch("kiro_crew.cron_service.agent_presence.cron_agent_missing", return_value=False),
    ):
        cron_cls.create = AsyncMock(side_effect=capture_cron)
        await gw._init_cron()
        await captured[0](job)

    assert job.last_result.startswith(
        "Error: set_project only works from a user-facing session (dashboard or a "
        "messaging channel); headless callers such as cron jobs and sub-agents are "
        "refused (this turn is 'cron:lj1'). Nothing was changed."
    )
    assert job.last_result.endswith(_REQUESTED)
    directive_audits = [a for a in audits if a.get("source") == "mcp-directive"]
    assert [(a["session_key"], a["tool_name"], a["outcome"]) for a in directive_audits] == [
        ("cron:lj1", "set_project", "denied")
    ]
