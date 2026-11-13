"""``session_create(backend=...)``: a conductor starts each worker on its own harness.

Pinned here: the route refuses a pick that cannot start with the spawn endpoint's
own codes and creates nothing; an accepted pick is stored on the slot, in the
birth metadata and in the selection's pick table, and comes back on restore; the
model is judged against the pick; every allocation hands the pick to the one
selection gate as ``backend_override``; a cold slot reads as its pick; a live
session found on another harness is reset onto it; and the MCP tools forward it.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from chat_test_helpers import _make_state
from member_memory_helpers import make_request

from kiro_crew import members
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers.session_control import api_session_control_create


@pytest.fixture(autouse=True)
def _fresh_create_budget():
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


@pytest.fixture(autouse=True)
def _session_harness_preview_on(_floor_monkeypatch):
    """Everything here is the preview's ON behaviour; OFF is test_session_harness_preview.py."""
    from kiro_crew import session_harness_preview

    _floor_monkeypatch.setattr(session_harness_preview, "session_harness_enabled", lambda: True)


@pytest.fixture
def selectable(monkeypatch):
    """Kiro, Claude and Codex are allowed and installed; Pi is allowed but missing."""
    from kiro_crew import acp_backends
    from kiro_crew.agent_sdk import backend_install
    from kiro_crew.agent_sdk.backend_install import INSTALLED, MISSING, BackendInstallState

    installs = {"": INSTALLED, "claude": INSTALLED, "codex": INSTALLED, "pi": MISSING}
    monkeypatch.setattr(acp_backends, "selectable_backend_values", lambda: sorted(installs))
    monkeypatch.setattr(
        backend_install,
        "probe_backend",
        lambda b: BackendInstallState(
            b,
            b or "kiro",
            installs[b],
            missing_components=("pi-acp",) if installs[b] == MISSING else (),
            install_command="npm i -g pi-acp" if installs[b] == MISSING else "",
        ),
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


class TestTheRoute:
    def test_a_pick_is_stored_on_the_slot_the_birth_line_and_the_pick_table(
        self, tmp_path, selectable
    ):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")

        status, payload = _post(state, caller, {"backend": "claude"})

        assert status == 200, payload
        assert payload["backend"] == "claude"
        child = state.get_slot(payload["target"])
        assert child.backend == "claude"
        written = state.conversation_log.get_metadata(slot_history_key(child))
        assert written.get("backend") == "claude"
        assert members.session_backend_pick(f"dashboard:{child.key}") == "claude"

    def test_kiro_is_stored_by_name_and_selects_kiros_id(self, tmp_path, selectable):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")

        status, payload = _post(state, caller, {"backend": "kiro"})

        assert status == 200, payload
        child = state.get_slot(payload["target"])
        assert child.backend == "kiro"
        # "" is Kiro CLI's id, not "no pick".
        assert members.session_backend_pick(f"dashboard:{child.key}") == ""

    def test_no_pick_creates_what_it_did_before(self, tmp_path, selectable):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")

        status, payload = _post(state, caller, {})

        assert status == 200, payload
        assert "backend" not in payload
        child = state.get_slot(payload["target"])
        assert child.backend == ""
        assert members.session_backend_pick(f"dashboard:{child.key}") is None

    @pytest.mark.parametrize(
        "name, code",
        [
            ("nonesuch", "unknown_backend"),
            ("goose", "unknown_backend"),
            ("pi", "backend_not_installed"),
        ],
    )
    def test_a_pick_that_cannot_start_is_refused_and_creates_nothing(
        self, tmp_path, selectable, name, code
    ):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        before = state.live_slot_count()

        status, payload = _post(state, caller, {"backend": name})

        assert status == 400
        assert payload["code"] == code
        assert payload["backends"] == ["claude", "codex", "kiro"]
        assert state.live_slot_count() == before

    def test_a_non_string_backend_is_refused(self, tmp_path, selectable):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")

        status, payload = _post(state, caller, {"backend": 3})

        assert status == 400
        assert payload["code"] == "invalid_field_type"

    def test_a_pick_with_crew_is_refused(self, tmp_path, selectable):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")

        status, payload = _post(state, caller, {"backend": "claude", "crew": "elsewhere"})

        assert status == 400
        assert payload["code"] == "backend_with_crew"

    def test_the_model_is_judged_against_the_pick(self, tmp_path, selectable, monkeypatch):
        from kiro_crew.dashboard import chat_handlers

        judged: list[str] = []

        def _reason(model, backend):
            judged.append(backend)
            return "not served by claude" if backend == "claude" else ""

        monkeypatch.setattr(chat_handlers, "_model_rejected_reason", _reason)
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        before = state.live_slot_count()

        status, payload = _post(state, caller, {"backend": "claude", "model": "kiro-only-1"})

        assert status == 400
        assert payload["code"] == "model_rejected"
        assert judged == ["claude"]
        assert state.live_slot_count() == before


class TestTheSlotRunsOnItsPick:
    def test_the_allocation_hands_the_pick_to_the_gate(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import _slot_backend_allocation_kwargs

        slot = _make_state(tmp_path).get_or_create_slot("w")
        assert _slot_backend_allocation_kwargs(slot) == {}
        slot.backend = "codex"
        assert _slot_backend_allocation_kwargs(slot) == {"backend_override": "codex"}
        slot.backend = "kiro"
        assert _slot_backend_allocation_kwargs(slot) == {"backend_override": ""}

    def test_restore_reads_the_pick_back(self, tmp_path):
        from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

        slot = _make_state(tmp_path).get_or_create_slot("w")
        codec._read_backend(SimpleNamespace(meta={"backend": "claude"}, slot=slot))
        assert slot.backend == "claude"
        assert members.session_backend_pick("dashboard:w") == "claude"

    def test_a_cold_slot_reads_as_its_pick(self, tmp_path, monkeypatch):
        """The Context panel and the model picker read this before the session exists."""
        from kiro_crew.dashboard import chat_handlers

        slot = _make_state(tmp_path).get_or_create_slot("w")
        slot.backend = "claude"
        members.note_session_backend_pick(slot.key, "claude")
        monkeypatch.setattr(
            "kiro_crew.agent_sdk.backends.selectable_backends", lambda: {"", "claude"}
        )

        assert asyncio.run(chat_handlers._configured_backend_for_slot(slot)) == "claude"

    @pytest.mark.parametrize(
        "pick, live, reset",
        [
            ("claude", "", True),
            ("claude", "claude", False),
            ("", "", False),
        ],
    )
    def test_a_live_session_on_another_harness_is_reset(self, tmp_path, pick, live, reset):
        from kiro_crew.agent_sdk.capabilities import capabilities_for
        from kiro_crew.dashboard.chat_runner import _live_session_off_slot_backend

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("w")
        slot.backend = pick
        provider = SimpleNamespace(capabilities=capabilities_for(live))
        state.sessions = SimpleNamespace(get_provider=lambda key: provider)

        assert _live_session_off_slot_backend(state, slot) is reset

    @pytest.mark.parametrize("pick, sent", [("kiro", {"backend_override": ""}), ("", {})])
    def test_the_inherited_default_is_resolved_for_the_pick(
        self, tmp_path, monkeypatch, pick, sent
    ):
        """The turn's inherited-default check must resolve the model the allocation
        does, or every turn on a picked session resets a healthy runtime."""
        from kiro_crew import session
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard.chat_runner import _inherited_default_model

        seen: list[dict] = []

        def _resolver(cfg, agent, **kwargs):
            seen.append(kwargs)
            return "m"

        monkeypatch.setattr(session, "_session_model", _resolver)
        slot = _make_state(tmp_path).get_or_create_slot("w")
        slot.backend = pick
        sessions = SimpleNamespace(_cfg=KiroCrewConfig())

        asyncio.run(
            _inherited_default_model(sessions, slot, "dashboard:w", agent=None, crew_agent="")
        )

        assert {k: v for k, v in seen[0].items() if k == "backend_override"} == sent


class TestTheTools:
    def _call(self, tool: str, args: dict, reply: dict):
        from kiro_crew.mcp_dashboard import TABLE
        from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
        from kiro_crew.mcp_tools.table import Caller, ToolContext

        dash = InMemoryDashboardClient(
            {
                "GET /api/chat/slots": [{"key": "chat-1-100", "title": "Caller"}],
                "GET /api/chat/folders": [],
                "POST /api/session-control/create": lambda req: reply,
                "POST /api/work-ledger/record": lambda req: {"item": {"item_id": "it_1"}},
                "POST /api/session-control/send": lambda req: {"ok": True},
            }
        )
        out = TABLE.call(tool, args, ToolContext(dash, Caller.strict("dashboard:chat-1-100")))
        creates = [r.body for r in dash.requests if r.path == "/api/session-control/create"]
        return out, creates

    def test_session_create_forwards_the_pick_and_names_it(self):
        out, creates = self._call(
            "session_create",
            {"title": "w", "agent": "kirocrew-worker", "backend": "claude"},
            {"ok": True, "target": "chat-9", "title": "w", "backend": "claude"},
        )
        assert creates[-1]["backend"] == "claude"
        assert "on harness `claude`" in out

    def test_work_dispatch_forwards_the_pick(self):
        _out, creates = self._call(
            "work_dispatch",
            {
                "title": "item",
                "acceptance": {"kind": "human_approval"},
                "agent": "kirocrew-worker",
                "seed": "go",
                "backend": "codex",
            },
            {"ok": True, "target": "chat-9", "title": "item", "backend": "codex"},
        )
        assert creates and creates[-1]["backend"] == "codex"

    def test_a_backend_refusal_names_its_code(self):
        from kiro_crew.mcp_dashboard import TABLE
        from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
        from kiro_crew.mcp_tools.table import Caller, ToolContext

        def _refuse(_req):
            from kiro_crew.mcp_tools.dashboard_client import DashboardError

            raise DashboardError(
                {"error": "backend 'nonesuch' is not available", "code": "unknown_backend"}
            )

        dash = InMemoryDashboardClient(
            {
                "GET /api/chat/slots": [{"key": "chat-1-100", "title": "Caller"}],
                "GET /api/chat/folders": [],
                "POST /api/session-control/create": _refuse,
            }
        )
        out = TABLE.call(
            "session_create",
            {"title": "w", "backend": "nonesuch"},
            ToolContext(dash, Caller.strict("dashboard:chat-1-100")),
        )
        assert out.startswith("Error: could not create a session")
        assert "(code `unknown_backend`)" in out

    def test_session_create_refuses_a_pick_with_crew(self):
        out, creates = self._call(
            "session_create",
            {"title": "w", "crew": "elsewhere", "backend": "claude"},
            {"ok": True, "target": "chat-9"},
        )
        assert out.startswith("Error: backend is taken only")
        assert creates == []
