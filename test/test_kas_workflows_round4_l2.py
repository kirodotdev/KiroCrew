"""Round 4, lane L2: structured questions, the person-only wait, and the rewind guard.

- Gap 13: KAS's ``_kiro/userInput`` client request (rich questions: descriptions,
  the recommended answer, sub-option toggles, free-form questions), announced
  while the Kiro Workflows switch is on and answered from a person's card.
- Gap 4: a chat's own person-only question / plan approval waits for the person,
  bounded only by the turn's own ceiling, not the 600 s approval window.
- Gap 3: Edit & resend is refused while a run started from the edited message or
  after it is live.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state
from turn_harness import APPROVED, SlotSpec, TurnRecord, TurnScript, run_turn

from kiro_crew.acp import kas_wire
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    KAS_CLIENT_CAPABILITIES,
    STOP_REASON_END_TURN,
    AcpEvent,
    JsonRpcMessage,
    kas_client_capabilities,
)
from kiro_crew.config.loader import APPROVAL_TURN_MARGIN_SECS
from kiro_crew.dashboard import turn_dispatch as td
from kiro_crew.kas_workflows import asks
from kiro_crew.kas_workflows import chat as kas_chat

# ── fixtures ────────────────────────────────────────────────────────────────

_RICH = {
    "sessionId": "sess-1",
    "toolCallId": "tc-1",
    "question": "How should I ship it?",
    "options": [
        {"title": "Draft PR", "description": "Open it as a draft"},
        {
            "title": "Full PR",
            "description": "Ready for review",
            "recommended": True,
            "subOptionsLabel": "Include:",
            "subOptions": [
                {"title": "Tests", "description": "unit tests"},
                {"title": "Docs"},
                {"title": "Changelog"},
            ],
        },
        {"title": "Later", "recommended": True},  # a second recommendation is ignored
        {"title": "Draft PR"},  # duplicate title dropped
        {"description": "no title: dropped"},
    ],
}

_FREE = {
    "sessionId": "sess-1",
    "toolCallId": "tc-2",
    "question": "What is the ticket id?",
    "options": [],
}


def _rich(**over: Any) -> asks.PendingQuestion:
    return asks.question_from_user_input(
        {**_RICH, **over},
        key="k",
        request_id=7,
        session_id="sess-1",
        owner=("wf_1", "step"),
        created_at=1.0,
        runtime=None,
        path=None,
        slot="chat-1",
    )


def _free() -> asks.PendingQuestion:
    return asks.question_from_user_input(
        _FREE,
        key="k2",
        request_id=8,
        session_id="sess-1",
        owner=("", ""),
        created_at=1.0,
        runtime=None,
        path=None,
        slot="chat-1",
    )


# ── gap 13: the question model ──────────────────────────────────────────────


class TestTheRichQuestion:
    def test_the_payload_keeps_descriptions_recommended_and_sub_options(self) -> None:
        payload = _rich().to_payload()
        (item,) = payload["questions"]
        assert item["id"] == "tc-1"
        assert item["prompt"] == "How should I ship it?"
        assert item["multiSelect"] is False
        assert item["freeText"] is False
        assert item["options"] == [
            {"label": "Draft PR", "description": "Open it as a draft"},
            {
                "label": "Full PR",
                "description": "Ready for review",
                "recommended": True,
                "subOptions": [
                    {"label": "Tests", "description": "unit tests"},
                    {"label": "Docs"},
                    {"label": "Changelog"},
                ],
                "subOptionsLabel": "Include:",
            },
            # Only the first recommendation counts, so "pre-select" is unambiguous.
            {"label": "Later"},
        ]

    def test_a_question_with_no_options_is_free_form(self) -> None:
        (item,) = _free().to_payload()["questions"]
        assert item["options"] == []
        assert item["freeText"] is True

    def test_the_legacy_bridge_stays_single_choice_with_no_free_text(self) -> None:
        q = asks.chat_question(
            request_id="r",
            tool_call_id="tc",
            title="Pick",
            options=[{"id": "a", "label": "A"}],
            slot="chat-1",
            created_at=1.0,
        )
        assert q is not None and q.via == asks.VIA_BRIDGE
        assert q.to_payload()["questions"][0]["freeText"] is False
        assert q.result_for("a") == {"outcome": {"outcome": "selected", "optionId": "a"}}
        assert q.dismiss_result() == {"outcome": {"outcome": "cancelled"}}

    def test_the_chat_question_is_built_from_the_user_input_params(self) -> None:
        q = asks.chat_question(
            request_id="r",
            tool_call_id="tc-1",
            title="ignored",
            options=[],
            slot="chat-1",
            created_at=1.0,
            user_input=_RICH,
        )
        assert q is not None and q.is_user_input
        assert q.key == "r"
        assert [o.label for o in q.options] == ["Draft PR", "Full PR", "Later"]

    @pytest.mark.parametrize(
        ("answer", "text"),
        [
            ("Draft PR", "Draft PR"),
            (["Draft PR"], "Draft PR"),
            # Sub-options ride KAS's own notation, in offered order.
            (["Full PR", "Docs", "Tests"], "Full PR [Tests, Docs]"),
            (["Full PR"], "Full PR"),
            ("Full PR", "Full PR"),
        ],
    )
    def test_an_answer_becomes_kas_answer_text(self, answer: Any, text: str) -> None:
        q = _rich()
        assert q.answer_value({"tc-1": answer}) == text
        assert q.answer_result({"tc-1": answer}) == {"action": "answered", "answer": text}

    @pytest.mark.parametrize(
        "answers",
        [
            {"other": "Draft PR"},
            {"tc-1": "Nope"},
            {"tc-1": ["Full PR", "Nope"]},
            {"tc-1": ["Full PR", "Docs", "Docs"]},
            {"tc-1": ["Draft PR", "Docs"]},  # a sub-option of ANOTHER answer
            {"tc-1": []},
            {"tc-1": ""},
        ],
    )
    def test_an_answer_it_does_not_offer_is_refused(self, answers: dict) -> None:
        with pytest.raises(ValueError):
            _rich().answer_value(answers)

    def test_a_free_form_answer_is_the_typed_text(self) -> None:
        q = _free()
        assert q.answer_value({"tc-2": "  JIRA-12  "}) == "JIRA-12"
        with pytest.raises(ValueError):
            q.answer_value({"tc-2": "   "})
        with pytest.raises(ValueError):
            q.answer_value({"tc-2": "x" * 4001})

    def test_dismissing_answers_dismissed(self) -> None:
        assert _rich().dismiss_result() == {"action": "dismissed"}
        assert asks.dismiss_result_for(kas_wire.METHOD_USER_INPUT) == {"action": "dismissed"}
        assert asks.dismiss_result_for("session/request_permission") == {
            "outcome": {"outcome": "cancelled"}
        }

    def test_a_bare_allow_takes_the_recommended_answer_with_every_sub_option(self) -> None:
        assert _rich().default_answer() == "Full PR [Tests, Docs, Changelog]"
        assert _free().default_answer() is None

    def test_question_text_is_redacted_and_controls_neutralized(self, monkeypatch) -> None:
        monkeypatch.setattr(asks, "_redact", lambda text: text.replace("SECRET", "[redacted]"))
        q = _rich(
            question="Use SECRET?",
            options=[{"title": "a\u202eb SECRET", "description": "x\u0000y"}],
        )
        assert q.prompt == "Use [redacted]?"
        assert q.options[0].label == "a b [redacted]"
        assert q.options[0].description == "x y"
        # The answer sent back is the title KAS sent, not the display copy.
        assert q.answer_value({"tc-1": q.options[0].label}) == "a\u202eb SECRET"


# ── gap 13: the handshake ───────────────────────────────────────────────────


class TestTheCapabilityFollowsTheSwitch:
    def test_announced_only_when_asked(self) -> None:
        assert kas_client_capabilities(KAS_CLIENT_CAPABILITIES, user_input=False) is (
            KAS_CLIENT_CAPABILITIES
        )
        on = kas_client_capabilities(KAS_CLIENT_CAPABILITIES, user_input=True)
        assert on["_meta"]["kiro"]["userInput"] is True
        assert on["_meta"]["kiro"]["openExternalUrl"] is True
        assert "userInput" not in KAS_CLIENT_CAPABILITIES["_meta"]["kiro"], "constant not mutated"

    @pytest.mark.parametrize("enabled", [True, False])
    def test_the_kas_harness_reads_the_kiro_workflows_switch(self, monkeypatch, enabled) -> None:
        from kiro_crew.acp.harness.kas import KasHarness

        monkeypatch.setattr(kas_chat, "chat_workflows_enabled", lambda: enabled)
        caps = KasHarness().client_capabilities
        assert ("userInput" in caps["_meta"]["kiro"]) is enabled


# ── gap 13: the session handle answers the request in its own shape ─────────


class _CapturingRuntime:
    def __init__(self, backend: str = ACP_BACKEND_KAS) -> None:
        self.responses: list[tuple[Any, dict]] = []
        self.errors: list[tuple[Any, int, str]] = []
        self.acp_backend = backend

    async def send_response(self, request_id: Any, result: dict) -> None:
        self.responses.append((request_id, result))

    async def send_error(self, request_id: Any, code: int, message: str) -> None:
        self.errors.append((request_id, code, message))


def _request(params: dict, rid: Any = 41) -> JsonRpcMessage:
    return JsonRpcMessage(id=rid, method=kas_wire.METHOD_USER_INPUT, params=params)


def _handle(backend: str = ACP_BACKEND_KAS) -> AcpSessionHandle:
    return AcpSessionHandle("sess-1", asyncio.Queue(), _CapturingRuntime(backend))


class TestTheSessionHandle:
    def test_only_a_native_workflow_backend_request_is_taken(self) -> None:
        assert _handle()._is_kas_user_input_request(_request(_RICH))
        assert not _handle(ACP_BACKEND_KIRO)._is_kas_user_input_request(_request(_RICH))
        notification = JsonRpcMessage(id=None, method=kas_wire.METHOD_USER_INPUT, params=_RICH)
        assert not _handle()._is_kas_user_input_request(notification)

    def test_the_request_becomes_a_person_only_permission_event(self) -> None:
        event = _handle()._build_user_input_event(_request(_RICH))
        assert event.kind == EVENT_PERMISSION_REQUEST
        assert event.request_id == 41
        assert event.title == "How should I ship it?"
        assert event.options == [
            {"id": "answer-0", "label": "Draft PR"},
            {"id": "answer-1", "label": "Full PR"},
            {"id": "answer-2", "label": "Later"},
        ]
        assert event.user_input == _RICH
        # Every channel and approval mode already holds this shape for a person.
        assert kas_chat.is_question(event) and kas_chat.needs_a_person(event)

    @pytest.mark.asyncio
    async def test_a_typed_answer_goes_back_as_text(self) -> None:
        handle = _handle()
        handle._build_user_input_event(_request(_RICH))
        assert await handle.answer_user_input(41, "Full PR [Docs]") is True
        assert handle._runtime.responses == [
            (41, {"action": "answered", "answer": "Full PR [Docs]"})
        ]
        # Answered once: a second answer finds nothing.
        assert await handle.answer_user_input(41, "x") is False

    @pytest.mark.asyncio
    async def test_reject_dismisses_and_never_answers_an_option(self) -> None:
        handle = _handle()
        handle._build_user_input_event(_request(_RICH))
        await handle.reject_tool(41)
        assert handle._runtime.responses == [(41, {"action": "dismissed"})]

    @pytest.mark.asyncio
    async def test_a_bare_approve_takes_the_recommended_answer(self) -> None:
        handle = _handle()
        handle._build_user_input_event(_request(_RICH))
        assert await handle.approve_tool(41, "allow_once") is True
        assert handle._runtime.responses == [
            (41, {"action": "answered", "answer": "Full PR [Tests, Docs, Changelog]"})
        ]

    @pytest.mark.asyncio
    async def test_an_approve_naming_an_answer_takes_it(self) -> None:
        handle = _handle()
        handle._build_user_input_event(_request(_RICH))
        assert await handle.approve_tool(41, "answer-0") is True
        assert handle._runtime.responses == [(41, {"action": "answered", "answer": "Draft PR"})]

    @pytest.mark.asyncio
    async def test_a_bare_approve_on_a_free_form_question_dismisses(self) -> None:
        handle = _handle()
        handle._build_user_input_event(_request(_FREE, rid=42))
        assert await handle.approve_tool(42) is False
        assert handle._runtime.responses == [(42, {"action": "dismissed"})]


# ── gap 13: the chat's own card answers through the right transport call ────


class _Slot:
    agent = "kirocrew"

    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def append(self, role: str, content: str, *_a: Any, **_k: Any) -> None:
        self.rows.append((role, content))


@pytest.mark.asyncio
async def test_settle_answers_a_user_input_question_with_text(monkeypatch) -> None:
    from kiro_crew.dashboard import chat_runner

    monkeypatch.setattr(chat_runner, "sel", lambda: MagicMock())
    client = SimpleNamespace(
        answer_user_input=AsyncMock(return_value=True),
        select_permission_option=AsyncMock(return_value=True),
        reject_tool=AsyncMock(),
    )
    event = AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id=41, title="t", tool_kind="other")
    slot = _Slot()
    await chat_runner._settle_kas_question(
        client, slot, event, _rich(), "Full PR [Docs]", session_key="dashboard:chat-1"
    )
    client.answer_user_input.assert_awaited_once_with(41, "Full PR [Docs]")
    client.select_permission_option.assert_not_awaited()
    assert slot.rows[-1][1].endswith("→ Full PR [Docs]")


@pytest.mark.asyncio
async def test_the_approve_route_records_the_answer_text(tmp_path) -> None:
    from test_chat_mode_security import _make_mode_app

    from kiro_crew.dashboard.chat_handlers import api_chat_slot_approve
    from kiro_crew.dashboard.state import SlotOrigin

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    question = asks.chat_question(
        request_id="req-q",
        tool_call_id="tc-1",
        title="",
        options=[],
        slot="s1",
        created_at=1.0,
        user_input=_RICH,
    )
    assert question is not None
    meta = {"request_id": "req-q", "person_only": "1", "kas_question": question.to_payload()}
    row = slot.append("permission", question.prompt, json.dumps(meta))
    future = asyncio.get_running_loop().create_future()
    kas_chat.hold_question(future, question)
    slot.register_approval("req-q", future, row)
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        bad = await client.post(
            "/api/chat/slots/s1/approve",
            json={"action": "answer", "request_id": "req-q", "answers": {"tc-1": ["Full PR", "X"]}},
        )
        assert bad.status == 400
        resp = await client.post(
            "/api/chat/slots/s1/approve",
            json={
                "action": "answer",
                "request_id": "req-q",
                "answers": {"tc-1": ["Full PR", "Changelog"]},
            },
        )
        assert resp.status == 200
    assert future.result() == "approved"
    assert kas_chat.take_answer(future) == "Full PR [Changelog]"


# ── gap 4: a person-only ask waits for the person ───────────────────────────

_CEILING = 3600
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
_AFTER = AcpEvent(kind=EVENT_TEXT_CHUNK, text="after")


def _question_event() -> AcpEvent:
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id="req-1",
        title="Which branch?",
        tool_kind="other",
        options=[{"id": "a", "label": "main"}, {"id": "b", "label": "dev"}],
        kas_builtin_ids=True,
        answers_only=True,
    )


def _declined_at(record: TurnRecord) -> float:
    [reject] = [call for call in record.calls("reject_tool") if call.args == ("req-1",)]
    return reject.at


@pytest.fixture(autouse=True)
def _isolate_turn_deadline():
    prev = td._TURN_DEADLINE.get()
    td._TURN_DEADLINE.set(None)
    try:
        yield
    finally:
        td._TURN_DEADLINE.set(prev)


class TestThePersonOnlyWait:
    def test_the_window_is_the_turn_ceiling_less_the_margin(self, monkeypatch) -> None:
        monkeypatch.setattr(td, "current_turn_limit_secs", lambda: _CEILING)
        assert td.person_wait_window_secs() == _CEILING - APPROVAL_TURN_MARGIN_SECS

    @pytest.mark.asyncio
    async def test_a_question_outlives_the_approval_window(self) -> None:
        record = await run_turn(
            TurnScript(events=[_question_event(), _AFTER, _DONE]),
            config={
                "agent": {"tool_approval_timeout_secs": 300, "chat_turn_timeout_secs": _CEILING}
            },
        )
        # Not 300 s: the person had the whole turn.
        assert _declined_at(record) == _CEILING - APPROVAL_TURN_MARGIN_SECS
        assert record.stop_reason == STOP_REASON_END_TURN

    @pytest.mark.asyncio
    async def test_an_ordinary_approval_keeps_the_window(self) -> None:
        tool = AcpEvent(
            kind=EVENT_PERMISSION_REQUEST, request_id="req-1", title="fs_write", tool_kind="edit"
        )
        record = await run_turn(
            TurnScript(events=[tool, _AFTER, _DONE]),
            config={
                "agent": {"tool_approval_timeout_secs": 300, "chat_turn_timeout_secs": _CEILING}
            },
        )
        assert _declined_at(record) == 300

    @pytest.mark.asyncio
    async def test_an_unattended_slot_keeps_its_deny_fast_window(self) -> None:
        record = await run_turn(
            TurnScript(events=[_question_event(), _AFTER, _DONE]),
            slot=SlotSpec(key="worker-1", app="issue-radar"),
        )
        assert _declined_at(record) == 180

    @pytest.mark.asyncio
    async def test_a_person_still_answers_it(self) -> None:
        record = await run_turn(
            TurnScript(events=[_question_event(), _AFTER, _DONE], answers={"req-1": APPROVED}),
            config={
                "agent": {"tool_approval_timeout_secs": 300, "chat_turn_timeout_secs": _CEILING}
            },
        )
        # Approved with no picked answer is a dismissal, never the first answer.
        assert [c.args for c in record.calls("reject_tool")] == [("req-1",)]


# ── gap 3: editing an earlier message while a run is live ───────────────────


class _Router:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def rows(self) -> list[dict]:
        return self._rows


def _row(status: str, created: str, slot: str = "src", wid: str = "wf_1") -> dict:
    return {
        "workflowId": wid,
        "name": "build",
        "status": status,
        "createdAt": created,
        "slotKey": slot,
    }


@pytest.fixture
def router(monkeypatch):
    from kiro_crew.kas_workflows import router as router_mod

    def _install(rows: list[dict]) -> None:
        monkeypatch.setattr(router_mod, "_ROUTER", _Router(rows))

    return _install


class TestRunsSince:
    def test_no_router_means_nothing_is_guarded(self, monkeypatch) -> None:
        from kiro_crew.kas_workflows import router as router_mod

        monkeypatch.setattr(router_mod, "_ROUTER", None)
        assert kas_chat.runs_since("src", "2026-05-21T16:00:02Z") == ([], [])

    def test_runs_before_the_message_and_other_chats_do_not_count(self, router) -> None:
        router(
            [
                _row("running", "2026-05-21T16:00:01Z", wid="before"),
                _row("running", "2026-05-21T16:00:05Z", slot="other", wid="elsewhere"),
                _row("running", "2026-05-21T16:00:05Z", wid="after"),
                _row("completed", "2026-05-21T16:00:06Z", wid="done"),
            ]
        )
        live, ended = kas_chat.runs_since("src", "2026-05-21T16:00:02Z")
        assert [r["workflowId"] for r in live] == ["after"]
        assert [r["workflowId"] for r in ended] == ["done"]

    def test_an_unreadable_time_counts_as_after(self, router) -> None:
        router([_row("paused", "not a time")])
        assert [r["workflowId"] for r in kas_chat.runs_since("src", "2026-05-21T16:00:02Z")[0]] == [
            "wf_1"
        ]


def _populate(state, key: str = "src"):
    slot = state.get_or_create_slot(key)
    slot.title = "My Chat"
    slot._titled = True
    slot.append("user", "first", "msg msg-u", ts="2026-05-21T16:00:00Z")
    slot.append("assistant", "a1", "msg msg-a", ts="2026-05-21T16:00:01Z")
    slot.append("user", "second", "msg msg-u", ts="2026-05-21T16:00:02Z")
    slot.append("assistant", "a2", "msg msg-a", ts="2026-05-21T16:00:03Z")
    slot.drain()
    return slot


class TestTheRewindRoute:
    @pytest.fixture(autouse=True)
    def _no_run(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(
            "kiro_crew.dashboard.chat_rewind._run_chat", AsyncMock(return_value=None)
        )

    @pytest.mark.asyncio
    async def test_a_live_run_since_the_message_refuses_the_edit(self, tmp_path, router) -> None:
        router([_row("paused", "2026-05-21T16:00:02.500Z")])
        state = _make_state(tmp_path)
        slot = _populate(state)
        state.sessions._session_map.get = MagicMock(return_value="")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind", json={"at_message_index": 2, "content": "edited"}
            )
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == kas_chat.REWIND_REFUSED_CODE
        assert body["runs"] == [{"workflowId": "wf_1", "name": "build"}]
        # Nothing changed: the history and the conversation are as they were.
        assert [m["content"] for m in slot.messages] == ["first", "a1", "second", "a2"]
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_earlier_message_than_the_run_is_still_refused(self, tmp_path, router) -> None:
        router([_row("running", "2026-05-21T16:00:02.500Z")])
        state = _make_state(tmp_path)
        _populate(state)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind", json={"at_message_index": 0, "content": "edited"}
            )
            assert resp.status == 409

    @pytest.mark.asyncio
    async def test_an_ended_run_or_an_older_one_lets_the_edit_through(
        self, tmp_path, router
    ) -> None:
        router(
            [
                _row("completed", "2026-05-21T16:00:02.500Z", wid="ended"),
                _row("running", "2026-05-21T15:00:00Z", wid="older"),
            ]
        )
        state = _make_state(tmp_path)
        _populate(state)
        state.sessions._session_map.get = MagicMock(return_value="")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind", json={"at_message_index": 2, "content": "edited"}
            )
            assert resp.status == 200


class _UnreadableRouter:
    def rows(self) -> list[dict]:
        raise RuntimeError("mirror torn down mid-read")


class TestTheGuardHasNoOpenDoor:
    """Round 5: the rewind guard fails closed and covers edit-resend too."""

    def test_an_unreadable_router_refuses_rather_than_allows(self, monkeypatch) -> None:
        from kiro_crew.kas_workflows import router as router_mod

        monkeypatch.setattr(router_mod, "_ROUTER", _UnreadableRouter())
        with pytest.raises(kas_chat.RunsUnreadable):
            kas_chat.runs_since("src", "2026-05-21T16:00:02Z")
        body = kas_chat.rewind_refusal("src", "2026-05-21T16:00:02Z")
        assert body is not None
        assert body["code"] == kas_chat.REWIND_REFUSED_CODE
        assert body["runs"] == []

    def test_no_live_run_means_no_refusal(self, router) -> None:
        router([_row("completed", "2026-05-21T16:00:05Z")])
        assert kas_chat.rewind_refusal("src", "2026-05-21T16:00:02Z") is None

    @pytest.mark.asyncio
    async def test_edit_resend_refuses_a_live_run_since_the_message(
        self, tmp_path, router, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.chat_regenerate import api_chat_slot_edit_resend

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_regenerate._run_chat", AsyncMock(return_value=None)
        )
        router([_row("running", "2026-05-21T16:00:02.500Z")])
        state = _make_state(tmp_path)
        state.sessions.discard_conversation = AsyncMock(return_value=True)
        state.sessions.aflush = AsyncMock()
        state.sessions._session_map.get = MagicMock(return_value="")
        slot = _populate(state)
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/edit-resend", api_chat_slot_edit_resend)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/edit-resend",
                json={"ts": "2026-05-21T16:00:02Z", "content": "edited"},
            )
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == kas_chat.REWIND_REFUSED_CODE
        assert body["runs"] == [{"workflowId": "wf_1", "name": "build"}]
        assert [m["content"] for m in slot.messages] == ["first", "a1", "second", "a2"]
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rewind_refuses_when_the_router_is_unreadable(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.kas_workflows import router as router_mod

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_rewind._run_chat", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(router_mod, "_ROUTER", _UnreadableRouter())
        state = _make_state(tmp_path)
        slot = _populate(state)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind", json={"at_message_index": 2, "content": "edited"}
            )
            assert resp.status == 409
        assert [m["content"] for m in slot.messages] == ["first", "a1", "second", "a2"]
