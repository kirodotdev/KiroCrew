# SPDX-License-Identifier: Apache-2.0
"""A crewmate's conductor may schedule a scripted job.

The conductor keeps the default template's built-in tools, the shell included,
so a ``script`` or ``command`` cron from it is dispatched like any caller's.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.handlers import _shared
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT

CONDUCTOR = SimpleNamespace(selection_kind="member", template_id=MATE_CONDUCTOR_AGENT)


class _Request(dict):
    """The parts of an aiohttp request ``api_cron_tools`` reads."""

    def __init__(self, session: str) -> None:
        super().__init__(internal_auth=True)
        self.headers = {"X-Session-Key": session}
        self.app = {"state": SimpleNamespace(push_refresh=lambda *_a: None)}


@pytest.mark.parametrize("field", ["script", "command"])
def test_a_scripted_cron_from_the_conductor_is_dispatched(monkeypatch, field):
    from kiro_crew import mcp_cron, member_memory_auth
    from kiro_crew.dashboard.handlers import cron

    session = "dashboard_member-ada"
    dispatched = []
    arguments = {"name": "probe", "every": "1h", "message": "x", field: "echo hi"}

    async def store(_request, _state, _operation):
        return "member-ada", None

    async def body(_request, max_bytes=0):
        return {"name": "cron_add", "arguments": arguments}, None

    async def scope(_request):
        return _shared.MemberScope(session, True, "member-ada", CONDUCTOR)

    monkeypatch.setattr(member_memory_auth, "memory_request_identity", lambda r: (session, True))
    monkeypatch.setattr(cron, "resolve_lesson_memory_store", store)
    monkeypatch.setattr(cron, "read_bounded_json", body)
    monkeypatch.setattr(_shared, "member_request_scope", scope)
    monkeypatch.setattr(
        mcp_cron, "_call_tool_locally", lambda n, a: dispatched.append((n, a)) or "Scheduled."
    )

    response = asyncio.run(cron.api_cron_tools(_Request(session)))

    assert response.status == 200
    assert [name for name, _ in dispatched] == ["cron_add"]
