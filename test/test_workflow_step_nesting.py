"""A workflow step cannot start a workflow, and a reused pool worker replays no earlier step.

A workflow step is a session a run opens for one of its agents, named by a key the
workflow service mints (``constants.WORKFLOW_STEP_KEY_PREFIXES``). The three routes the
``workflow_run`` tool reaches, and the rerun route, refuse a call from such a session; a sub-agent and a
dashboard session still start runs. A pool worker (``wf-pool:``) serves several steps
of its run, and the results of runs an earlier step started are written to its
transcript. A later step on that worker reads none of it back as context.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_workflow_definition_handlers import FakeService
from test_workflow_definition_handlers import _app as _definition_app

from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers.workflows import (
    api_workflow_run,
    api_workflow_run_intent,
    api_workflow_run_rerun,
)
from kiro_crew.history import ConversationLog
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

STEP_KEYS = (
    "wf:run1:0",
    # A named chain in a run without a memory scope (``agent_exec.named_session_key``).
    "wf:run1:named:" + hashlib.sha256(b"named:chain").hexdigest(),
    "wf-pool:run1:0",
    "wf-unpooled:run1:0",
    "wf-worker:run1:abc",
    "wf-author:run1:a0",
)
ALLOWED_KEYS = ("subagent:abc123", "dashboard:chat-1")
REFUSED_CODE = "workflow_step_cannot_start_workflow"

ROUTES = (
    ("/api/workflows/run", api_workflow_run, "start", {"source": "print('ok')"}),
    (
        "/api/workflows/run_intent",
        api_workflow_run_intent,
        "start_from_intent",
        {"intent": "ship it"},
    ),
)


async def _post(route, handler, method_name, payload, session_key):
    method = AsyncMock(return_value={"run_id": "wf_2"})
    app = web.Application()
    app["state"] = SimpleNamespace(workflow_service=SimpleNamespace(**{method_name: method}))
    app.router.add_post(route, handler)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(route, json=payload, headers={"X-Session-Key": session_key})
        body = await response.json()
    return response.status, body, method


@pytest.mark.asyncio
@pytest.mark.parametrize("session_key", STEP_KEYS)
@pytest.mark.parametrize(("route", "handler", "method_name", "payload"), ROUTES)
async def test_a_workflow_step_cannot_start_a_workflow(
    route, handler, method_name, payload, session_key
) -> None:
    status, body, method = await _post(route, handler, method_name, payload, session_key)
    assert (status, body["code"]) == (403, REFUSED_CODE)
    assert "directly in this step" in body["error"]
    method.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("session_key", ALLOWED_KEYS)
@pytest.mark.parametrize(("route", "handler", "method_name", "payload"), ROUTES)
async def test_a_subagent_or_dashboard_session_still_starts_a_workflow(
    route, handler, method_name, payload, session_key
) -> None:
    status, _body, method = await _post(route, handler, method_name, payload, session_key)
    assert status == 200
    method.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_key", "refused"), [(k, True) for k in STEP_KEYS] + [(k, False) for k in ALLOWED_KEYS]
)
async def test_a_saved_workflow_run_is_refused_only_for_a_step(session_key, refused) -> None:
    service = FakeService()
    async with TestClient(TestServer(_definition_app(service, app_name=None))) as client:
        response = await client.post(
            "/api/workflows/definitions/debug/run",
            json={},
            headers={"X-Session-Key": session_key},
        )
        body = await response.json()
    if refused:
        assert (response.status, body["code"]) == (403, REFUSED_CODE)
        assert service.started is None
    else:
        assert response.status == 200
        assert service.started is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_key", "refused"),
    [(k, True) for k in STEP_KEYS] + [(k, False) for k in ALLOWED_KEYS],
)
@pytest.mark.parametrize("payload", [{"from_index": 0}, {"from_index": 0, "source": "print(1)"}])
async def test_a_rerun_is_refused_only_for_a_step(session_key, refused, payload) -> None:
    """A rerun from index 0, or with an edited script, starts a whole new run, so a
    step is refused it like ``workflow_run``."""
    method = AsyncMock(return_value={"run_id": "wf_2"})
    app = web.Application()
    app["state"] = SimpleNamespace(workflow_service=SimpleNamespace(rerun_subtree=method))
    app.router.add_post("/api/workflows/runs/{run_id}/rerun", api_workflow_run_rerun)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/workflows/runs/wf_1/rerun", json=payload, headers={"X-Session-Key": session_key}
        )
        body = await response.json()
    if refused:
        assert (response.status, body["code"]) == (403, REFUSED_CODE)
        method.assert_not_awaited()
    else:
        assert response.status == 200
        method.assert_awaited_once()


# --------------------------------------------------------------------------- #
# A reused pool worker
# --------------------------------------------------------------------------- #

POOL_KEY = "wf-pool:run1:0"
EARLIER_RESULT = (
    "[Workflow completion event]\nWorkflow `audit` (wf_000099) → **finished**\n\n"
    "Result: EARLIER-STEP-CHILD-RESULT"
)


@pytest.fixture(autouse=True)
def _close_skills_loaders(close_skills_loaders):
    """The replay tests build a ``ContextBuilder``: close its ``SkillsLoader``."""


def _builder(tmp_path, *keys: str) -> ContextBuilder:
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    for key in keys:
        log.append(key, "user", "earlier step task", source_thread="thread-earlier")
        log.append(key, "assistant", EARLIER_RESULT, source_thread="thread-earlier")
        log.append(key, "system", '{"kind": "stop_event", "state": "stopped"}')
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        conversation_log=log,
    )


def test_a_reused_pool_worker_does_not_see_an_earlier_steps_child_result(tmp_path) -> None:
    """The worker's next step starts a new conversation (``is_new``), and its
    session context carries nothing from the worker's transcript."""
    builder = _builder(tmp_path, POOL_KEY)
    full, *_ = builder.build_message("next step task", True, POOL_KEY, runtime_source="workflow")
    assert "EARLIER-STEP-CHILD-RESULT" not in full
    assert "earlier step task" not in full
    assert "THREAD CONVERSATION HISTORY" not in full
    assert "Recent Session Context" not in full
    assert "User stopped the previous turn" not in full


def test_any_other_session_still_replays_its_own_transcript(tmp_path) -> None:
    builder = _builder(tmp_path, "dashboard:tab-1", "subagent:abc123")
    for key in ("dashboard:tab-1", "subagent:abc123"):
        ctx = builder.build_session_context(key)
        assert "THREAD CONVERSATION HISTORY" in ctx
        assert "EARLIER-STEP-CHILD-RESULT" in ctx
        assert "Recent Session Context" in ctx
        assert "User stopped the previous turn" in ctx


def _prior_turns_seen_by_skill_selection(tmp_path, monkeypatch, session_key: str) -> list[dict]:
    """The prior turns the skill-selection point reads for *session_key*'s turn.

    Skill selection is forced on by standing in for the two seams around it: the
    loader always asks the point for a pick, and the point always reads its
    history source, so the real ``prior_turns`` read in ``build_message`` runs.
    """
    from kiro_crew.decisions.points import skills_select

    builder = _builder(tmp_path, session_key)
    seen: list[list[dict]] = []

    def selected_skills(_skills, _text, _project, *, history_source, **_kw):
        seen.append(history_source())
        return []

    def get_triggered_skills(_text, *, select=None, **_kw):
        assert select is not None
        select()
        return []

    monkeypatch.setattr(skills_select, "selected_skills", selected_skills)
    monkeypatch.setattr(builder.skills, "get_triggered_skills", get_triggered_skills)
    builder.build_message("next step task", True, session_key, runtime_source="workflow")
    assert len(seen) == 1, "the skill-selection point was not reached"
    return seen[0]


def test_skill_selection_on_a_reused_pool_worker_reads_no_earlier_step(
    tmp_path, monkeypatch
) -> None:
    assert _prior_turns_seen_by_skill_selection(tmp_path, monkeypatch, POOL_KEY) == []


def test_skill_selection_on_any_other_session_reads_its_prior_turns(tmp_path, monkeypatch) -> None:
    turns = _prior_turns_seen_by_skill_selection(tmp_path, monkeypatch, "dashboard:tab-1")
    assert any("earlier step task" in str(t.get("content", "")) for t in turns)
