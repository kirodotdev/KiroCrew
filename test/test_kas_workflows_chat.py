"""KAS's own workflow surface in dashboard chats (``agent.kas_workflows``).

Covers the three places the setting reaches: the ``_meta`` a KAS session start
carries, which chats Crew's own workflow tool then redirects, and the gateway
config that turns it on.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.acp.harness import SessionExtras
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_SESSION_LOAD
from kiro_crew.kas_workflows import chat
from kiro_crew.testing.ids import UNALLOCATABLE_PID


@pytest.fixture(autouse=True)
def _clean_enabled_chats(_floor_monkeypatch):
    _floor_monkeypatch.setattr(chat, "_ENABLED_CHATS", {})


@pytest.fixture(autouse=True)
def _fast_runtime_start(_floor_monkeypatch):
    """The same two pins test_acp_runtime applies to a faked session start."""
    import kiro_crew.acp.session_handle as sh
    from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

    _floor_monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 0.05, raising=False)
    _floor_monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: SPEC_PERMISSIONS_MIN_VERSION
    )


def _kas_runtime(monkeypatch, *, sink: bool = True):
    rt = AcpRuntime(work_dir="/tmp")
    proc = MagicMock()
    proc.stdout = asyncio.StreamReader()
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = UNALLOCATABLE_PID
    rt._initialized = True
    rt._can_load_session = True
    rt._acp_backend = ACP_BACKEND_KAS
    rt._workflow_sink = MagicMock() if sink else None
    sent: list[tuple[str, dict]] = []

    async def _fake_send(method, params, timeout=None):
        sent.append((method, params))
        if method == METHOD_SESSION_LOAD:
            return {"modes": {"currentModeId": "kirocrew"}, "models": []}
        return {}

    async def _fake_agents(agent, *, member_dispatch=False, crew_panel=False, session_key=""):
        return SessionExtras(custom_agents=[{"id": agent, "prompt": "p", "tools": []}])

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)
    return rt, sent


def _workflows_meta(params: dict):
    return params.get("_meta", {}).get("kiro", {}).get("settings", {}).get("workflows")


# ── The session-start envelope ──


@pytest.mark.asyncio
async def test_an_opted_in_chat_resumes_with_workflows_on_beside_its_agents(monkeypatch):
    monkeypatch.setattr(chat, "chat_workflows_enabled", lambda: True)
    rt, sent = _kas_runtime(monkeypatch)
    await rt.load_session("", "sid-1", cwd="/work", agent="kirocrew", session_key="chat-abc")
    params = sent[0][1]
    assert _workflows_meta(params) == {"enabled": True}
    # The agent batch shares _meta.kiro and must survive the merge, and its
    # allowlist must name KAS's workflow tools: it filters native tools too.
    assert params["_meta"]["kiro"]["customAgents"] == [
        {"id": "kirocrew", "prompt": "p", "tools": list(chat.WORKFLOW_CHAT_TOOLS)}
    ]
    assert chat.has_kas_workflows("chat-abc")
    assert chat.has_kas_workflows("dashboard:chat-abc")


@pytest.mark.asyncio
async def test_the_setting_off_sends_enabled_false_and_forgets_the_chat(monkeypatch):
    chat._ENABLED_CHATS["chat-abc"] = None
    monkeypatch.setattr(chat, "chat_workflows_enabled", lambda: False)
    rt, sent = _kas_runtime(monkeypatch)
    await rt.load_session("", "sid-1", cwd="/work", agent="kirocrew", session_key="chat-abc")
    # Gap 5b: the switch-off state is sent EXPLICITLY. Without it KAS falls back
    # to the persisted value, and a chat that once ran with the surface on would
    # keep its /workflow-* commands and steering across a reload. The agent batch
    # still names no workflow tools.
    assert _workflows_meta(sent[0][1]) == {"enabled": False}
    assert sent[0][1]["_meta"]["kiro"]["customAgents"][0]["tools"] == []
    assert not chat.has_kas_workflows("chat-abc")


def test_workflow_tools_join_only_the_active_agents_list_without_mutating_it():
    mine = {"id": "kirocrew", "tools": ["fs_read", "run_workflow"]}
    other = {"id": "helper", "tools": ["fs_read"]}
    star = {"id": "wild", "tools": "*"}
    out = chat.with_workflow_tools([mine, other, star], "kirocrew")
    assert out is not None
    assert out[0]["tools"] == [
        "fs_read",
        "run_workflow",
        "inspect_workflow",
        "update_workflow",
        "save_workflow_definition",
        "validate_workflow",
    ]
    assert mine["tools"] == ["fs_read", "run_workflow"], "the caller's batch is not mutated"
    assert out[1] is other and out[2] is star
    assert chat.with_workflow_tools([star], "wild")[0]["tools"] == "*"
    assert chat.with_workflow_tools(None, "kirocrew") is None
    assert "send_message" not in chat.WORKFLOW_CHAT_TOOLS


@pytest.mark.parametrize("session_key", ["subagent:abc", "cron:daily", "taskrunner_1", ""])
@pytest.mark.asyncio
async def test_only_a_dashboard_chat_gets_the_surface(monkeypatch, session_key):
    monkeypatch.setattr(chat, "chat_workflows_enabled", lambda: True)
    rt, sent = _kas_runtime(monkeypatch)
    await rt.load_session("", "sid-1", cwd="/work", agent="kirocrew", session_key=session_key)
    assert _workflows_meta(sent[0][1]) is None
    assert not chat.has_kas_workflows(session_key)


@pytest.mark.asyncio
async def test_a_runtime_with_no_sink_never_turns_it_on(monkeypatch):
    monkeypatch.setattr(chat, "chat_workflows_enabled", lambda: True)
    rt, sent = _kas_runtime(monkeypatch, sink=False)
    await rt.load_session("", "sid-1", cwd="/work", agent="kirocrew", session_key="chat-abc")
    assert _workflows_meta(sent[0][1]) is None


def test_attach_merges_into_an_existing_kiro_envelope():
    params = {"_meta": {"kiro": {"customAgents": [1], "settings": {"other": 2}}, "x": 3}}
    chat.attach_workflow_settings(params)
    assert params == {
        "_meta": {
            "kiro": {"customAgents": [1], "settings": {"other": 2, "workflows": {"enabled": True}}},
            "x": 3,
        }
    }


def test_enabled_chats_stay_bounded(monkeypatch):
    monkeypatch.setattr(chat, "chat_workflows_enabled", lambda: True)
    monkeypatch.setattr(chat, "_MAX_ENABLED_CHATS", 2)
    for n in range(3):
        asyncio.run(chat.wants_workflows(f"chat-{n}"))
    assert list(chat._ENABLED_CHATS) == ["chat-1", "chat-2"]


# ── Crew's own workflow tool in such a chat ──


def _request(session_key: str):
    headers = {"X-Session-Key": session_key} if session_key else {}
    return make_mocked_request("POST", "/api/workflows/run_intent", headers=headers)


def test_crew_authoring_is_redirected_only_in_a_chat_with_the_surface():
    from kiro_crew.dashboard.handlers.workflows import _kas_chat_redirect

    chat._ENABLED_CHATS["chat-abc"] = None
    refused = _kas_chat_redirect(_request("chat-abc"))
    assert refused is not None and refused.status == 409
    assert b"run_workflow" in refused.body and b"kas_workflows_in_chat" in refused.body
    # The MCP stub names the chat with the dashboard: prefix; same chat.
    assert _kas_chat_redirect(_request("dashboard:chat-abc")) is not None
    assert _kas_chat_redirect(_request("chat-other")) is None
    assert _kas_chat_redirect(_request("")) is None


# ── The config that turns it on ──


def test_the_setting_is_off_by_default_and_patchable(tmp_path, monkeypatch):
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.dashboard.handlers.core import _editable_config_spec

    assert KiroCrewConfig().agent.kas_workflows is False
    assert _editable_config_spec("agent.kas_workflows") == {"type": "bool"}


# ── The chat's approval mode, as a workflow step reads it ──


class _Slot:
    def __init__(self, *, trust=False, trust_reads=False, scope=""):
        self._trust = trust
        self._trust_reads = trust_reads
        self._trust_scope = scope


class _State:
    def __init__(self, yolo=False):
        self.yolo = yolo

    def is_yolo_active(self):
        return self.yolo


@pytest.mark.parametrize(
    ("state", "slot", "expected"),
    [
        (_State(), _Slot(), ("normal", "")),
        (_State(), _Slot(trust_reads=True), ("trust_reads", "trust_reads")),
        (_State(), _Slot(trust=True, trust_reads=True), ("trust", "trust")),
        (_State(yolo=True), _Slot(), ("trust", "yolo")),
    ],
)
def test_step_mode_reader_uses_the_chats_own_precedence(state, slot, expected):
    from kiro_crew.dashboard.chat_runner import _kas_step_mode_reader

    assert _kas_step_mode_reader(state, slot)() == expected


def test_step_mode_reader_reads_live_not_at_capture():
    from kiro_crew.dashboard.chat_runner import _kas_step_mode_reader

    slot = _Slot()
    read = _kas_step_mode_reader(_State(), slot)
    assert read()[0] == "normal"
    slot._trust = True
    assert read()[0] == "trust"


# ── a question the chat's own KAS session asks (N5) ──


def _legacy_question_request(*, options: list[dict]) -> object:
    """kiro-agent ``handleUserInputViaPermissionRequest``'s request, as it reaches Crew.

    ``toolCall`` carries the question as its title and no ``_meta``; one
    ``allow_once`` option per answer. Shape from ``acp-event-adapter.ts``.
    """
    from kiro_crew.acp.types import JsonRpcMessage

    return JsonRpcMessage(
        id=7,
        method="session/request_permission",
        params={
            "sessionId": "sess-chat",
            "toolCall": {
                "toolCallId": "tc-question",
                "title": "Which branch should I target?",
                "status": "pending",
            },
            "options": options,
        },
    )


_ANSWERS = [
    {"optionId": "q1-main", "name": "main", "kind": "allow_once"},
    {"optionId": "q1-dev", "name": "dev", "kind": "allow_once"},
]


def _event(msg, *, kas: bool = True):
    from kiro_crew.acp._dispatch import build_permission_event

    event, _recorded = build_permission_event(
        msg,
        identity_unreadable_cache={} if kas else None,
        kas_consent_meta=kas,
        harness_backend=ACP_BACKEND_KAS if kas else "",
    )
    assert event is not None
    return event


def test_a_kas_legacy_question_is_held_for_a_person():
    event = _event(_legacy_question_request(options=_ANSWERS))

    assert event.answers_only is True
    assert event.harness_tool_id == "", "the bridge stamps no toolId: the plan check misses it"
    assert chat.is_plan_handoff(event.harness_tool_id) is False
    assert chat.is_question(event) is True
    assert chat.needs_a_person(event) is True


@pytest.mark.parametrize(
    "options",
    [
        # A tool approval: it offers a reject.
        [
            {"optionId": "allow_once", "name": "Yes", "kind": "allow_once"},
            {"optionId": "reject_once", "name": "No", "kind": "reject_once"},
        ],
        # An allow_always is a grant, not an answer.
        [{"optionId": "a", "name": "Always", "kind": "allow_always"}],
        # No stated kind: never inferred from the id.
        [{"optionId": "allow_once", "name": "Yes"}],
        # Nothing offered.
        [],
    ],
)
def test_a_request_that_is_not_answers_only_is_not_a_question(options):
    event = _event(_legacy_question_request(options=options))

    assert event.answers_only is False
    assert chat.is_question(event) is False
    assert chat.needs_a_person(event) is False


def test_answers_only_off_kas_is_not_a_kas_question():
    event = _event(_legacy_question_request(options=_ANSWERS), kas=False)

    assert event.answers_only is True
    assert chat.is_question(event) is False


def test_a_plan_handoff_still_needs_a_person():
    plan = MagicMock(harness_tool_id="update_workflow", kas_builtin_ids=True, answers_only=False)
    assert chat.needs_a_person(plan) is True


def test_a_bare_test_double_is_not_a_kas_question():
    """A MagicMock's unset attributes are truthy; only real ``True`` flags count.

    Other suites build permission events as bare mocks; reading them as KAS
    questions would hold ordinary approvals for a person.
    """
    bare = MagicMock()
    bare.harness_tool_id = ""
    assert chat.is_question(bare) is False
    assert chat.needs_a_person(bare) is False


def test_the_provider_carries_answers_only_to_the_chat():
    """The chat reads the provider's copy of the event; a dropped field reopens N5."""
    from kiro_crew.providers.acp import AcpProvider

    event = _event(_legacy_question_request(options=_ANSWERS))
    copied = AcpProvider._to_llm_event(event)

    assert copied.answers_only is True
    assert copied.kas_builtin_ids is True
    assert chat.is_question(copied) is True
