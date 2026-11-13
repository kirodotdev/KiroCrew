"""The ``session_harness`` feature preview gates ``backend`` on session creation.

Off (the default): ``tools/list`` does not offer ``backend`` on ``session_create`` or
``work_dispatch``; both tools refuse one without calling the gateway; the HTTP create
route refuses one with ``session_harness_preview_off`` and creates nothing. On: the
schema offers it and the route takes it, as ``test_session_create_backend.py`` pins.

Each guarantee and the one-line change that reddens its test:

* schema drop -- ``with_backend_schema`` returning *tools* unchanged reddens
  ``test_off_the_schema_does_not_offer_backend``;
* route refusal -- deleting the preview check in ``api_session_control_create``
  reddens ``test_off_the_route_refuses_a_backend_and_creates_nothing``;
* tool refusal -- ``mcp_refusal`` returning ``None`` reddens both ``TestTheTools`` off tests;
* default off -- ``session_harness_enabled`` returning ``True`` on a missing name
  reddens ``test_the_preview_reads_off_unless_set``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from chat_test_helpers import _make_state
from member_memory_helpers import make_request

from kiro_crew import session_harness_preview
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers.session_control import api_session_control_create


@pytest.fixture(autouse=True)
def _fresh_create_budget():
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


@pytest.fixture
def preview(monkeypatch):
    def _set(on: bool) -> None:
        monkeypatch.setattr(session_harness_preview, "session_harness_enabled", lambda: on)

    return _set


@pytest.fixture
def selectable(monkeypatch):
    from kiro_crew import acp_backends
    from kiro_crew.agent_sdk import backend_install
    from kiro_crew.agent_sdk.backend_install import INSTALLED, BackendInstallState

    monkeypatch.setattr(acp_backends, "selectable_backend_values", lambda: ["", "claude"])
    monkeypatch.setattr(
        backend_install,
        "probe_backend",
        lambda b: BackendInstallState(b, b or "kiro", INSTALLED),
    )


def _post(state, caller, body: dict):
    async def go():
        return await api_session_control_create(
            make_request(
                state,
                "/api/session-control/create",
                body={"title": "worker", **body},
                internal=True,
                session=slot_history_key(caller),
            )
        )

    response = asyncio.run(go())
    return response.status, json.loads(response.text)


def _props(tool_name: str) -> dict:
    from kiro_crew import mcp_dashboard

    tools = {t["name"]: t for t in mcp_dashboard._list_tools()}
    return tools[tool_name]["inputSchema"]["properties"]


class TestTheSchema:
    @pytest.mark.parametrize("tool", ["session_create", "work_dispatch"])
    def test_off_the_schema_does_not_offer_backend(self, preview, tool):
        preview(False)
        props = _props(tool)
        assert "backend" not in props
        assert "title" in props

    @pytest.mark.parametrize("tool", ["session_create", "work_dispatch"])
    def test_on_the_schema_offers_backend(self, preview, tool):
        preview(True)
        assert "backend" in _props(tool)

    def test_off_leaves_spawn_runs_own_backend_alone(self):
        from kiro_crew.session_harness_preview import with_backend_schema

        spawn = {"name": "spawn_run", "inputSchema": {"properties": {"backend": {}}}}
        assert with_backend_schema([spawn], False) == [spawn]


class TestTheRoute:
    def test_off_the_route_refuses_a_backend_and_creates_nothing(self, tmp_path, preview):
        preview(False)
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        before = state.live_slot_count()

        status, payload = _post(state, caller, {"backend": "claude"})

        assert status == 403
        assert payload["code"] == "session_harness_preview_off"
        assert "feature preview is off" in payload["error"]
        assert state.live_slot_count() == before

    def test_off_a_create_with_no_backend_still_works(self, tmp_path, preview, selectable):
        preview(False)
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")

        status, payload = _post(state, caller, {"backend": ""})

        assert status == 200, payload
        assert "backend" not in payload

    def test_on_the_route_takes_the_backend(self, tmp_path, preview, selectable):
        preview(True)
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")

        status, payload = _post(state, caller, {"backend": "claude"})

        assert status == 200, payload
        assert payload["backend"] == "claude"


class TestTheTools:
    def _call(self, tool: str, args: dict):
        from kiro_crew.mcp_dashboard import TABLE
        from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
        from kiro_crew.mcp_tools.table import Caller, ToolContext

        dash = InMemoryDashboardClient(
            {
                "GET /api/chat/slots": [{"key": "chat-1-100", "title": "Caller"}],
                "GET /api/chat/folders": [],
                "POST /api/session-control/create": lambda req: {
                    "ok": True,
                    "target": "chat-9",
                    "title": "w",
                    "backend": "claude",
                },
                "POST /api/work-ledger/record": lambda req: {"item": {"item_id": "it_1"}},
                "POST /api/session-control/send": lambda req: {"ok": True},
            }
        )
        out = TABLE.call(tool, args, ToolContext(dash, Caller.strict("dashboard:chat-1-100")))
        writes = [r.path for r in dash.requests if r.method == "POST"]
        return out, writes

    def test_off_session_create_refuses_a_backend_without_calling_the_gateway(self, preview):
        preview(False)
        out, writes = self._call("session_create", {"title": "w", "backend": "claude"})
        assert out.startswith("Error: backend is not taken")
        assert "(code `session_harness_preview_off`)" in out
        assert writes == []

    def test_off_work_dispatch_refuses_before_it_creates_the_item(self, preview):
        preview(False)
        out, writes = self._call(
            "work_dispatch",
            {
                "title": "item",
                "acceptance": {"kind": "human_approval"},
                "agent": "kirocrew-worker",
                "seed": "go",
                "backend": "claude",
            },
        )
        assert "(code `session_harness_preview_off`)" in out
        assert writes == []

    def test_on_session_create_forwards_the_backend(self, preview):
        preview(True)
        out, writes = self._call("session_create", {"title": "w", "backend": "claude"})
        assert "/api/session-control/create" in writes
        assert "on harness `claude`" in out


@pytest.mark.parametrize(
    "previews, on",
    [
        ({}, False),
        ({"session_harness": False}, False),
        ({"session_harness": True}, True),
        ({"crew_mode": True}, False),
    ],
)
def test_the_preview_reads_off_unless_set(monkeypatch, previews, on):
    from kiro_crew.config import KiroCrewConfig

    cfg = SimpleNamespace(dashboard=SimpleNamespace(feature_previews=previews))
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    assert session_harness_preview.session_harness_enabled() is on


def test_an_unreadable_config_reads_as_off(monkeypatch):
    from kiro_crew.config import KiroCrewConfig

    def _boom(cls):
        raise OSError("unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
    assert session_harness_preview.session_harness_enabled() is False
