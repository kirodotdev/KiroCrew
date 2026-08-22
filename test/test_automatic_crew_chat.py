from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.automatic_routing import (
    ROUTE_E2E_VALIDATION,
    ROUTE_RETRIEVAL_AUDIT_RISK,
    ROUTE_SMALL_CHANGE,
)
from kiro_crew.dashboard import chat_handlers


async def _response_text(response) -> str:
    try:
        return await response.text()
    finally:
        response.close()


async def _answer_pairing(client, slot, answer: str = "normal") -> dict:
    response = await client.post(
        "/api/chat?ws=1",
        json={"message": answer, "slot": slot.key},
        timeout=None,
    )
    data = await response.json()
    response.close()
    return data


@pytest.mark.asyncio
async def test_api_chat_classifies_automatic_route_off_loop(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("classifier-offload-slot")
    slot._titled = True
    slot._auto_tagged = True
    run_chat = AsyncMock()
    monkeypatch.setattr(chat_handlers, "_run_chat", run_chat)

    calls: list[tuple[object, tuple, dict]] = []

    async def fake_to_thread(fn, *args, **kwargs):
        calls.append((fn, args, kwargs))
        return fn(*args, **kwargs)

    monkeypatch.setattr(chat_handlers.asyncio, "to_thread", fake_to_thread)

    async with TestClient(TestServer(_make_app(state))) as client:
        response = await client.post(
            "/api/chat?ws=1",
            json={"message": "hello", "slot": slot.key},
            timeout=None,
        )
        data = await response.json()
        response.close()
        if slot.task is not None:
            await slot.task

    assert data == {"ok": True, "slot": slot.key}
    assert any(call[0] is chat_handlers.classify_message for call in calls)
    run_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_chat_non_trivial_waits_for_pairing_before_route(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("preflight-slot")
    slot.project = str(tmp_path)
    slot._titled = True
    slot._auto_tagged = True

    service = MagicMock()
    service.start = AsyncMock(return_value={"run_id": "should-not-start"})
    state.workflow_service = service
    run_chat = AsyncMock()
    monkeypatch.setattr(chat_handlers, "_run_chat", run_chat)

    async with TestClient(TestServer(_make_app(state))) as client:
        response = await client.post(
            "/api/chat",
            json={"message": "Implement a new feature and add acceptance tests", "slot": slot.key},
            timeout=None,
        )
        body = await _response_text(response)

    assert "data: [DONE]" in body
    assert service.start.await_count == 0
    assert run_chat.await_count == 0
    pending = state.pairing_pending(slot.key)
    assert pending is not None
    assert pending["eligible"] is True
    assert pending["mode"] is None
    assert pending["decision_source"] == "classifier"
    assert pending["message"] == "Implement a new feature and add acceptance tests"
    question = next(iter(slot._question_pending.values()))
    assert question["questions"][0]["header"] == "PAIRING"
    assert [option["label"] for option in question["questions"][0]["options"]] == [
        "Guided",
        "Practice",
        "ทำงานปกติ",
    ]


@pytest.mark.asyncio
async def test_api_chat_invalid_pairing_answer_reposts_without_dispatch(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("invalid-pairing-slot")
    slot.project = str(tmp_path)
    slot._titled = True
    slot._auto_tagged = True
    service = MagicMock()
    service.start = AsyncMock(return_value={"run_id": "should-not-start"})
    state.workflow_service = service
    run_chat = AsyncMock()
    monkeypatch.setattr(chat_handlers, "_run_chat", run_chat)

    async with TestClient(TestServer(_make_app(state))) as client:
        first = await client.post(
            "/api/chat",
            json={"message": "Refactor the routing boundary", "slot": slot.key},
            timeout=None,
        )
        await _response_text(first)

        answer = await client.post(
            "/api/chat?ws=1",
            json={"message": "maybe", "slot": slot.key},
            timeout=None,
        )
        answer_data = await answer.json()
        answer.close()

    assert answer_data == {"ok": True, "slot": slot.key, "pairing_preflight": True}
    assert state.pairing_pending(slot.key) is not None
    assert service.start.await_count == 0
    assert run_chat.await_count == 0
    assert len(slot._question_pending) == 1


@pytest.mark.asyncio
async def test_api_chat_trivial_request_bypasses_pairing(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("trivial-slot")
    slot._titled = True
    slot._auto_tagged = True
    state.workflow_service = MagicMock()
    default_messages: list[str] = []

    async def fake_run_chat(_state, _slot, message):
        default_messages.append(message)

    monkeypatch.setattr(chat_handlers, "_run_chat", fake_run_chat)

    async with TestClient(TestServer(_make_app(state))) as client:
        response = await client.post(
            "/api/chat?ws=1",
            json={
                "message": "Explain what this function does without changing files",
                "slot": slot.key,
            },
            timeout=None,
        )
        data = await response.json()
        response.close()
        if slot.task is not None:
            await slot.task

    assert data == {"ok": True, "slot": slot.key}
    assert state.pairing_pending(slot.key) is None
    assert state.pairing_task(slot.key) is None
    assert default_messages == ["Explain what this function does without changing files"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "route", "workflow_name"),
    [
        (
            "Fix the API endpoint in the project",
            ROUTE_SMALL_CHANGE,
            "__kirocrew.crew.software-delivery",
        ),
        (
            "Audit Knowledge retrieval WAL safety",
            ROUTE_RETRIEVAL_AUDIT_RISK,
            "__kirocrew.crew.knowledge-quality",
        ),
    ],
)
async def test_api_chat_starts_high_confidence_crew_route(
    tmp_path, monkeypatch, message, route, workflow_name
):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("route-slot")
    slot.project = str(tmp_path)
    slot._titled = True
    slot._auto_tagged = True

    service = MagicMock()
    service.start = AsyncMock(return_value={"run_id": "run-1"})
    state.workflow_service = service
    monkeypatch.setattr(chat_handlers, "_run_chat", AsyncMock())

    async with TestClient(TestServer(_make_app(state))) as client:
        first = await client.post(
            "/api/chat",
            json={"message": message, "slot": slot.key},
            timeout=None,
        )
        first_body = await _response_text(first)
        assert "data: [DONE]" in first_body
        decision_data = await _answer_pairing(client, slot)

    assert decision_data == {"ok": True, "slot": slot.key, "automatic_route": True}
    assert slot._has_reader is False
    assert state._automatic_route_runs == {slot.key: "run-1"}
    service.start.assert_awaited_once()
    call = service.start.await_args
    assert call.args[0]
    assert call.kwargs["name"] == "automatic-crew-routing"
    assert call.kwargs["author"] == "automatic-router"
    assert call.kwargs["_allow_native_crew"] is True
    assert call.kwargs["session_key"] == f"dashboard:{slot.key}"
    assert call.kwargs["args"] == {
        "__crew_workflow": workflow_name,
        "route": route,
        "request": message,
        "candidate_workspace": str(tmp_path.resolve()),
    }


@pytest.mark.asyncio
async def test_api_chat_starts_quality_engineering_e2e_route(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("quality-route-slot")
    slot.project = str(tmp_path)
    slot._titled = True
    slot._auto_tagged = True

    service = MagicMock()
    service.start = AsyncMock(return_value={"run_id": "quality-run-1"})
    state.workflow_service = service
    monkeypatch.setattr(chat_handlers, "_run_chat", AsyncMock())

    async with TestClient(TestServer(_make_app(state))) as client:
        first = await client.post(
            "/api/chat",
            json={"message": "Review Playwright browser flow", "slot": slot.key},
            timeout=None,
        )
        await _response_text(first)
        decision_data = await _answer_pairing(client, slot)

    assert decision_data == {"ok": True, "slot": slot.key, "automatic_route": True}
    assert state._automatic_route_runs == {slot.key: "quality-run-1"}
    call = service.start.await_args
    assert call.kwargs["args"] == {
        "__crew_workflow": "__kirocrew.crew.quality-engineering",
        "route": ROUTE_E2E_VALIDATION,
        "request": "Review Playwright browser flow",
        "project_path": str(tmp_path.resolve()),
        "check_ids": ["playwright_cli_capability"],
    }
    assert call.kwargs["author"] == "automatic-router"
    assert call.kwargs["_allow_native_crew"] is True


@pytest.mark.asyncio
async def test_api_chat_asks_once_then_default_fallback_for_unresolved_route(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("clarify-slot")
    slot.project = str(tmp_path)
    slot._titled = True
    slot._auto_tagged = True

    service = MagicMock()
    service.start = AsyncMock(return_value={"run_id": "unexpected"})
    state.workflow_service = service
    default_messages: list[str] = []

    async def fake_run_chat(_state, _slot, message):
        default_messages.append(message)

    monkeypatch.setattr(chat_handlers, "_run_chat", fake_run_chat)

    async with TestClient(TestServer(_make_app(state))) as client:
        first = await client.post(
            "/api/chat",
            json={"message": "Please update the docs", "slot": slot.key},
            timeout=None,
        )
        await _response_text(first)
        normal_data = await _answer_pairing(client, slot)

        assert normal_data == {"ok": True, "slot": slot.key, "route_clarification": True}
        assert service.start.await_count == 0
        assert state._automatic_route_pending[slot.key]["message"] == "Please update the docs"
        question = next(iter(slot._question_pending.values()))
        assert question["questions"][0]["question"] == (
            "Is this a code change, a retrieval/Knowledge audit, or ordinary chat?"
        )

        second = await client.post(
            "/api/chat?ws=1",
            json={"message": "Something else", "slot": slot.key},
            timeout=None,
        )
        second_data = await second.json()
        second.close()
        if slot.task is not None:
            await slot.task

    assert second_data == {"ok": True, "slot": slot.key}
    assert slot.key not in state._automatic_route_pending
    assert slot._question_pending == {}
    assert service.start.await_count == 0
    assert default_messages == ["Please update the docs\nClarification: Something else"]


@pytest.mark.asyncio
async def test_api_chat_preserves_explicit_agent_and_default_fallback(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("explicit-agent-slot")
    slot._titled = True
    slot._auto_tagged = True
    state.workflow_service = MagicMock()
    default_messages: list[str] = []

    async def fake_run_chat(_state, _slot, message):
        default_messages.append(message)

    monkeypatch.setattr(chat_handlers, "_run_chat", fake_run_chat)

    async with TestClient(TestServer(_make_app(state))) as client:
        response = await client.post(
            "/api/chat?ws=1",
            json={
                "message": "Fix the API endpoint in the project",
                "slot": slot.key,
                "agent": "developer",
            },
            timeout=None,
        )
        data = await response.json()
        response.close()
        if slot.task is not None:
            await slot.task

    assert data == {"ok": True, "slot": slot.key}
    assert slot.agent == "developer"
    assert default_messages == ["Fix the API endpoint in the project"]
    assert not state._automatic_route_runs


@pytest.mark.asyncio
async def test_api_chat_queues_while_automatic_route_is_active(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("queue-slot")
    slot.project = str(tmp_path)
    slot._titled = True
    slot._auto_tagged = True

    service = MagicMock()
    service.start = AsyncMock(return_value={"run_id": "run-active"})
    service.status = MagicMock(return_value={"status": "running"})
    state.workflow_service = service
    monkeypatch.setattr(chat_handlers, "_run_chat", AsyncMock())

    async with TestClient(TestServer(_make_app(state))) as client:
        first = await client.post(
            "/api/chat",
            json={"message": "Fix the API endpoint in the project", "slot": slot.key},
            timeout=None,
        )
        await _response_text(first)
        first_data = await _answer_pairing(client, slot)

        second = await client.post(
            "/api/chat",
            json={"message": "What changed?", "slot": slot.key},
            timeout=None,
        )
        second_data = await second.json()
        second.close()

    assert first_data == {"ok": True, "slot": slot.key, "automatic_route": True}
    assert second_data == {"ok": True, "queued": True}
    assert service.start.await_count == 1
    assert slot._queue and slot._queue[0]["content"] == "What changed?"
    assert state._automatic_route_runs[slot.key] == "run-active"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "mode", "checkpoint"),
    [("Guided", "guided", "P1"), ("Practice", "practice", "P2")],
)
async def test_pairing_modes_use_default_agent_without_direct_crew_dispatch(
    tmp_path, monkeypatch, answer, mode, checkpoint
):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(f"{mode}-slot")
    slot.project = str(tmp_path)
    slot._titled = True
    slot._auto_tagged = True
    service = MagicMock()
    service.start = AsyncMock(return_value={"run_id": "must-not-start"})
    state.workflow_service = service
    monkeypatch.setattr(chat_handlers, "_pairing_skill_available", lambda _state: True)
    default_messages: list[str] = []

    async def fake_run_chat(_state, _slot, message):
        default_messages.append(message)

    monkeypatch.setattr(chat_handlers, "_run_chat", fake_run_chat)

    async with TestClient(TestServer(_make_app(state))) as client:
        first = await client.post(
            "/api/chat",
            json={"message": "Fix the API endpoint in the project", "slot": slot.key},
            timeout=None,
        )
        await _response_text(first)
        decision_data = await _answer_pairing(client, slot, answer)
        if slot.task is not None:
            await slot.task

    assert decision_data == {"ok": True, "slot": slot.key}
    assert service.start.await_count == 0
    assert len(default_messages) == 1
    assert default_messages[0].count("$learning-pairing") == 1
    assert f"pairing.mode: {mode}" in default_messages[0]
    assert f"checkpoint: {checkpoint}" in default_messages[0]
    assert state.pairing_task(slot.key) is not None
    assert state.pairing_task(slot.key)["mode"] == mode


@pytest.mark.asyncio
async def test_pairing_skill_unavailable_blocks_without_normal_downgrade(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("missing-skill-slot")
    slot.project = str(tmp_path)
    slot._titled = True
    slot._auto_tagged = True
    service = MagicMock()
    service.start = AsyncMock(return_value={"run_id": "must-not-start"})
    state.workflow_service = service
    monkeypatch.setattr(chat_handlers, "_pairing_skill_available", lambda _state: False)
    run_chat = AsyncMock()
    monkeypatch.setattr(chat_handlers, "_run_chat", run_chat)

    async with TestClient(TestServer(_make_app(state))) as client:
        first = await client.post(
            "/api/chat",
            json={"message": "Fix the API endpoint in the project", "slot": slot.key},
            timeout=None,
        )
        await _response_text(first)
        answer_data = await _answer_pairing(client, slot, "Guided")

    assert answer_data == {"ok": True, "slot": slot.key, "pairing_preflight": True}
    assert service.start.await_count == 0
    assert run_chat.await_count == 0
    assert state.pairing_pending(slot.key) is not None
    assert state.pairing_task(slot.key) is None
    assert any(
        message["role"] == "assistant"
        and "Pairing Preflight capability is unavailable" in message["content"]
        for message in slot.messages
    )


def test_pairing_cleanup_is_compare_safe_and_workflow_scoped(tmp_path):
    state = _make_state(tmp_path)
    state.set_pairing_task(
        "slot-a",
        {"task_id": "task-old", "workflow_run_id": "run-old", "mode": "normal"},
    )
    assert state.clear_pairing_task("slot-a", "task-new") is False
    assert state.pairing_task("slot-a")["task_id"] == "task-old"

    state.set_pairing_task(
        "slot-a",
        {"task_id": "task-new", "workflow_run_id": "run-new", "mode": "normal"},
    )
    state.set_pairing_task(
        "slot-b",
        {"task_id": "task-other", "workflow_run_id": "run-old", "mode": "normal"},
    )

    assert state.clear_pairing_for_workflow_run("run-old") == ["slot-b"]
    assert state.pairing_task("slot-a")["task_id"] == "task-new"
    assert state.pairing_task("slot-b") is None

    state.set_pairing_pending("slot-c", {"task_id": "pending-task"})
    state.set_pairing_task("slot-c", {"task_id": "active-task", "mode": "guided"})
    assert state.clear_all_pairing() == 3
    assert state.pairing_pending("slot-c") is None
    assert state.pairing_task("slot-c") is None


@pytest.mark.asyncio
async def test_active_pairing_turn_queues_without_showing_a_second_preflight(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("active-pairing-slot")
    slot._titled = True
    slot._auto_tagged = True
    slot.task = MagicMock(done=lambda: False)
    state.set_pairing_task(
        slot.key,
        {
            "task_id": "task-active",
            "message": "Implement the feature",
            "mode": "guided",
            "workflow_run_id": "",
        },
    )

    async with TestClient(TestServer(_make_app(state))) as client:
        response = await client.post(
            "/api/chat",
            json={"message": "Also add a regression test", "slot": slot.key},
            timeout=None,
        )
        data = await response.json()
        response.close()

    assert data == {"ok": True, "queued": True}
    assert state.pairing_pending(slot.key) is None
    assert state.pairing_task(slot.key)["task_id"] == "task-active"
    assert len(slot._question_pending) == 0


@pytest.mark.asyncio
async def test_slot_delete_clears_pending_and_active_pairing_state(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("delete-pairing-slot")
    state.set_pairing_pending(slot.key, {"task_id": "pending-task", "message": "new feature"})
    state.set_pairing_task(
        slot.key,
        {"task_id": "active-task", "mode": "guided", "workflow_run_id": ""},
    )

    async with TestClient(TestServer(_make_app(state))) as client:
        response = await client.delete(f"/api/chat/slots/{slot.key}")
        data = await response.json()
        response.close()

    assert data == {"ok": True}
    assert state.pairing_pending(slot.key) is None
    assert state.pairing_task(slot.key) is None
    assert slot.key not in state._slots


@pytest.mark.asyncio
async def test_pairing_skill_resolver_exception_requires_explicit_normal_choice(
    tmp_path, monkeypatch
):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("resolver-error-slot")
    slot.project = str(tmp_path)
    slot._titled = True
    slot._auto_tagged = True
    service = MagicMock()
    service.start = AsyncMock(return_value={"run_id": "normal-after-gap"})
    state.workflow_service = service
    run_chat = AsyncMock()
    monkeypatch.setattr(chat_handlers, "_run_chat", run_chat)

    def raise_resolver(_state):
        raise RuntimeError("resolver unavailable")

    monkeypatch.setattr("kiro_crew.dashboard.handlers._get_skills", raise_resolver)

    async with TestClient(TestServer(_make_app(state))) as client:
        first = await client.post(
            "/api/chat",
            json={"message": "Fix the API endpoint in the project", "slot": slot.key},
            timeout=None,
        )
        await _response_text(first)
        blocked = await _answer_pairing(client, slot, "Guided")

        assert blocked == {"ok": True, "slot": slot.key, "pairing_preflight": True}
        assert service.start.await_count == 0
        assert run_chat.await_count == 0
        assert state.pairing_pending(slot.key) is not None

        normal = await _answer_pairing(client, slot, "ทำงานปกติ")

    assert normal == {"ok": True, "slot": slot.key, "automatic_route": True}
    assert service.start.await_count == 1
    assert run_chat.await_count == 0
    assert state.pairing_pending(slot.key) is None
    assert state.pairing_task(slot.key)["mode"] == "normal"


@pytest.mark.asyncio
async def test_normal_route_clarification_binds_workflow_to_pairing_task(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("normal-clarification-slot")
    slot.project = str(tmp_path)
    slot._titled = True
    slot._auto_tagged = True
    service = MagicMock()
    service.start = AsyncMock(return_value={"run_id": "clarified-run"})
    state.workflow_service = service
    monkeypatch.setattr(chat_handlers, "_run_chat", AsyncMock())

    async with TestClient(TestServer(_make_app(state))) as client:
        first = await client.post(
            "/api/chat",
            json={"message": "Please update the docs", "slot": slot.key},
            timeout=None,
        )
        await _response_text(first)
        clarification = await _answer_pairing(client, slot, "ทำงานปกติ")
        assert clarification == {"ok": True, "slot": slot.key, "route_clarification": True}
        state._automatic_route_pending[slot.key]["project_path"] = str(tmp_path)
        task_id = state.pairing_task(slot.key)["task_id"]

        routed = await _answer_pairing(client, slot, "Fix API endpoint")

    assert routed == {"ok": True, "slot": slot.key, "automatic_route": True}
    assert state.pairing_task(slot.key) == {
        "task_id": task_id,
        "message": "Please update the docs",
        "project_path": str(tmp_path),
        "eligible": True,
        "mode": "normal",
        "scope": "task",
        "decision_source": "user",
        "workflow_run_id": "clarified-run",
        "pairing": {
            "eligible": True,
            "mode": "normal",
            "scope": "task",
            "decision_source": "user",
        },
    }


@pytest.mark.asyncio
async def test_api_chat_blocks_recognized_route_when_workflow_start_fails(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("blocked-route-slot")
    slot.project = str(tmp_path)
    slot._titled = True
    slot._auto_tagged = True

    service = MagicMock()
    service.start = AsyncMock(return_value={"error": "workflow unavailable"})
    state.workflow_service = service
    run_chat = AsyncMock()
    monkeypatch.setattr(chat_handlers, "_run_chat", run_chat)

    async with TestClient(TestServer(_make_app(state))) as client:
        first = await client.post(
            "/api/chat",
            json={"message": "Fix the API endpoint in the project", "slot": slot.key},
            timeout=None,
        )
        await _response_text(first)
        blocked = await _answer_pairing(client, slot)

    assert blocked == {"ok": True, "slot": slot.key, "automatic_route_blocked": True}
    assert service.start.await_count == 1
    assert run_chat.await_count == 0
    assert state.pairing_task(slot.key) is None
    assert any(
        message["role"] == "assistant"
        and "Automatic Crew routing is unavailable" in message["content"]
        for message in slot.messages
    )


@pytest.mark.asyncio
async def test_api_chat_greeting_bypasses_pairing(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("greeting-slot")
    slot._titled = True
    slot._auto_tagged = True
    state.workflow_service = MagicMock()
    default_messages: list[str] = []

    async def fake_run_chat(_state, _slot, message):
        default_messages.append(message)

    monkeypatch.setattr(chat_handlers, "_run_chat", fake_run_chat)

    async with TestClient(TestServer(_make_app(state))) as client:
        response = await client.post(
            "/api/chat?ws=1",
            json={"message": "hello", "slot": slot.key},
            timeout=None,
        )
        data = await response.json()
        response.close()
        if slot.task is not None:
            await slot.task

    assert data == {"ok": True, "slot": slot.key}
    assert state.pairing_pending(slot.key) is None
    assert default_messages == ["hello"]
