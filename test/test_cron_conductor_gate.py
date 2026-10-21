# SPDX-License-Identifier: Apache-2.0
"""A crewmate's conductor schedules message reminders only.

``cron_add`` with a ``script`` or ``command`` from a conductor caller is refused
gateway-side, on the attested execution record; a message reminder passes, and so
does every other caller, the crewmate's own worker included.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.handlers import _shared
from kiro_crew.dashboard.handlers import cron_conductor_gate as gate
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT, MATE_WORKER_AGENT

CONDUCTOR = SimpleNamespace(selection_kind="member", template_id=MATE_CONDUCTOR_AGENT)
WORKER = SimpleNamespace(selection_kind="template", template_id=MATE_WORKER_AGENT)
PLAIN = SimpleNamespace(selection_kind="member", template_id="kirocrew")


def _run(monkeypatch, execution, name, arguments):
    async def scope(_request):
        return _shared.MemberScope("dashboard_member-ada", True, "member-ada", execution)

    monkeypatch.setattr(_shared, "member_request_scope", scope)
    return asyncio.run(gate.refuse_conductor_scripted_job(object(), name, arguments))


@pytest.mark.parametrize("field", ["script", "command"])
def test_a_conductor_asking_for_a_scripted_job_is_refused(monkeypatch, field):
    refused = _run(monkeypatch, CONDUCTOR, "cron_add", {"message": "x", field: "run.py:main"})
    assert refused is not None and refused.status == 403
    body = json.loads(refused.body)
    assert body["code"] == "conductor_scripted_job"
    assert "worker" in body["error"]


def test_a_conductor_reminder_passes(monkeypatch):
    assert _run(monkeypatch, CONDUCTOR, "cron_add", {"message": "stand up", "every": "1h"}) is None


@pytest.mark.parametrize("execution", [WORKER, PLAIN, None])
def test_every_other_caller_keeps_scripted_jobs(monkeypatch, execution):
    assert _run(monkeypatch, execution, "cron_add", {"command": "echo hi"}) is None


class _Request(dict):
    """The parts of an aiohttp request ``api_cron_tools`` reads."""

    def __init__(self, session: str) -> None:
        super().__init__(internal_auth=True)
        self.headers = {"X-Session-Key": session}
        self.app = {"state": SimpleNamespace(push_refresh=lambda *_a: None)}


def _drive_handler(monkeypatch, execution, arguments):
    from kiro_crew import mcp_cron, member_memory_auth
    from kiro_crew.dashboard.handlers import cron

    session = "dashboard_member-ada"
    dispatched = []

    async def store(_request, _state, _operation):
        return "member-ada", None

    async def body(_request, max_bytes=0):
        return {"name": "cron_add", "arguments": arguments}, None

    async def scope(_request):
        return _shared.MemberScope(session, True, "member-ada", execution)

    monkeypatch.setattr(member_memory_auth, "memory_request_identity", lambda r: (session, True))
    monkeypatch.setattr(cron, "resolve_lesson_memory_store", store)
    monkeypatch.setattr(cron, "read_bounded_json", body)
    monkeypatch.setattr(_shared, "member_request_scope", scope)
    monkeypatch.setattr(
        mcp_cron, "_call_tool_locally", lambda n, a: dispatched.append((n, a)) or "Scheduled."
    )
    response = asyncio.run(cron.api_cron_tools(_Request(session)))
    return response, dispatched


@pytest.mark.parametrize("field", ["script", "command"])
def test_the_cron_route_refuses_a_conductors_scripted_job_before_dispatch(monkeypatch, field):
    response, dispatched = _drive_handler(
        monkeypatch, CONDUCTOR, {"name": "probe", "every": "1h", "message": "x", field: "echo hi"}
    )
    assert response.status == 403
    assert json.loads(response.body)["code"] == "conductor_scripted_job"
    assert dispatched == []


def test_the_cron_route_still_schedules_a_conductors_reminder(monkeypatch):
    response, dispatched = _drive_handler(
        monkeypatch, CONDUCTOR, {"name": "stand-up", "every": "1h", "message": "stand up"}
    )
    assert response.status == 200
    assert [name for name, _ in dispatched] == ["cron_add"]
