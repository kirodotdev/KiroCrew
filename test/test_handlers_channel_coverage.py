"""``dashboard/handlers_channel.py``: the routes the ``test_channel_*`` and approve suites skip.

Those suites pin input validation, trust tiers, capacity status and clear-context. This one covers
the agent task wrapper, list/get/close, create with its preset agents, and the 404 and refusal
answers. A real ``ChannelManager`` runs in ``tmp_path``; the agent runner is replaced, so no agent
ever starts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

import kiro_crew.dashboard.handlers_channel as handlers
from kiro_crew.channel import ChannelManager, ChannelMessage

pytestmark = pytest.mark.asyncio


def _request(manager: ChannelManager, body: dict | None = None, **match_info: str) -> MagicMock:
    request = MagicMock()
    request.app = {
        "state": SimpleNamespace(channel_manager=manager, sessions=MagicMock(), _yolo=False)
    }
    request.match_info = match_info
    request.json = AsyncMock(return_value=body or {})
    return request


async def _call(handler, request) -> tuple[int, dict]:
    try:
        response = await handler(request)
    except web.HTTPException as exc:  # an unknown channel is raised, not returned
        response = exc
    return response.status, json.loads(response.text)


@pytest.fixture
def events() -> list[tuple[str, dict]]:
    return []


@pytest.fixture
def manager(tmp_path, events) -> ChannelManager:
    return ChannelManager(
        broadcast_fn=lambda kind, data: events.append((kind, data)),
        channels_dir=str(tmp_path / "channels"),
    )


@pytest.fixture
def spawned(monkeypatch) -> SimpleNamespace:
    """The agent runner and task spawner, replaced: a run is the tuple ``("run", agent.id)``."""
    run = MagicMock(side_effect=lambda agent, ch, sessions, is_yolo: ("run", agent.id))
    spawn = MagicMock()
    monkeypatch.setattr(handlers, "run_channel_agent", run)
    monkeypatch.setattr(handlers, "_spawn_agent_task", spawn)
    return SimpleNamespace(run=run, spawn=spawn)


@pytest.mark.parametrize("outcome", ["finishes", "fails", "is cancelled"])
async def test_spawn_agent_task_keeps_the_task_and_logs_only_a_real_failure(caplog, outcome):
    agent, forever = SimpleNamespace(), asyncio.Event()

    async def work() -> str:
        if outcome == "fails":
            raise RuntimeError("model unreachable")
        if outcome == "is cancelled":
            await forever.wait()
        return "done"

    with caplog.at_level(logging.ERROR, logger=handlers.logger.name):
        task = handlers._spawn_agent_task(agent, work())
        if outcome == "is cancelled":
            task.cancel()
        (result,) = await asyncio.gather(task, return_exceptions=True)
    assert agent._task is task
    logged = [r.getMessage() for r in caplog.records if r.name == handlers.logger.name]
    assert logged == (["Agent task failed: model unreachable"] if outcome == "fails" else [])
    assert (result == "done") is (outcome == "finishes")
    assert task.cancelled() is (outcome == "is cancelled")


async def test_list_get_and_close_a_channel(manager, events):
    channel = manager.create("incident")
    channel.messages.extend(
        ChannelMessage(id=f"m{n}", from_id="human", from_role="You", content=f"msg {n}")
        for n in range(55)
    )

    status, body = await _call(handlers.api_channels_list, _request(manager))
    assert (status, [c["id"] for c in body["channels"]]) == (200, [channel.id])

    status, body = await _call(handlers.api_channel_get, _request(manager, id=channel.id))
    assert (status, body["topic"]) == (200, "incident")
    assert [m["content"] for m in body["messages"]] == [f"msg {n}" for n in range(5, 55)]
    assert await _call(handlers.api_channel_get, _request(manager, id="nope")) == (
        404,
        {"error": "not found"},
    )

    close = handlers.api_channel_close
    assert await _call(close, _request(manager, id=channel.id)) == (200, {"ok": True})
    assert ("channel_closed", {"channel_id": channel.id}) in events
    assert await _call(close, _request(manager, id=channel.id)) == (200, {"ok": False})
    assert manager.count == 0


@pytest.mark.parametrize("preset_lead", [True, False])
async def test_create_adds_the_preset_agents_and_an_orchestrator_when_it_has_none(
    manager, spawned, preset_lead
):
    agents = [{"role": "Logs Agent", "task": "Search logs"}]
    if preset_lead:
        agents.insert(0, {"role": "Lead", "is_orchestrator": True, "task": "Coordinate"})
    request = _request(manager, {"topic": "  outage  ", "agents": agents})
    request.app["state"]._yolo = True

    status, body = await _call(handlers.api_channel_create, request)

    channel = manager.get(body["channel"]["id"])
    members = list(channel.members.values())
    lead = ("Lead", "Coordinate") if preset_lead else ("Orchestrator", "outage")
    assert (status, body["ok"], channel.topic) == (200, True, "outage")
    assert [(a.role, a.task) for a in members] == [lead, ("Logs Agent", "Search logs")]
    assert channel.orchestrator_id == members[0].id
    assert [call.args[0] for call in spawned.spawn.call_args_list] == members
    assert spawned.run.call_args.args[2] is request.app["state"].sessions
    assert spawned.run.call_args.kwargs["is_yolo"]() is True  # read live, not captured


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"topic": "   "}, "channel_topic_required"),
        ({"topic": "t", "agents": "all"}, "channel_agents_type_invalid"),
        ({"topic": "t", "agents": [{"role": 7}]}, "channel_agent_field_type_invalid"),
        (
            {"topic": "t", "agents": [{"is_orchestrator": "yes"}]},
            "channel_agent_orchestrator_type_invalid",
        ),
    ],
)
async def test_create_rejects_a_bad_body_before_opening_a_channel(manager, spawned, payload, code):
    status, body = await _call(handlers.api_channel_create, _request(manager, payload))
    assert (status, body["code"]) == (400, code)
    assert manager.count == 0
    spawned.spawn.assert_not_called()


async def test_post_requires_content_and_keeps_only_mentions_of_members(manager):
    channel = manager.create("incident")
    worker = channel.add_agent(role="Worker")
    post = handlers.api_channel_post

    blank = await _call(post, _request(manager, {"content": " \n "}, id=channel.id))
    assert blank == (400, {"error": "content required"})

    body = {"content": "ping", "mention": [worker.id, "ghost"]}
    status, reply = await _call(post, _request(manager, body, id=channel.id))
    assert (status, reply["message"]["mention"]) == (200, [worker.id])


async def test_add_agent_starts_a_runner_for_the_new_agent(manager, spawned):
    channel = manager.create("incident")
    request = _request(manager, {"role": "Logs", "task": "grep"}, id=channel.id)

    status, body = await _call(handlers.api_channel_add_agent, request)

    (agent,) = channel.members.values()
    assert (status, body) == (200, {"ok": True, "agent": agent.to_dict()})
    assert (agent.role, agent.task) == ("Logs", "grep")
    spawned.spawn.assert_called_once_with(agent, ("run", agent.id))


@pytest.mark.parametrize(
    "handler",
    [handlers.api_channel_post, handlers.api_channel_add_agent, handlers.api_channel_update_agent],
)
async def test_body_routes_answer_404_for_an_unknown_channel(manager, handler):
    request = _request(manager, {"content": "hi"}, id="nope", aid="x")
    assert await _call(handler, request) == (404, {"error": "not found"})


async def test_update_agent_answers_404_for_an_unknown_agent(manager):
    channel = manager.create("incident")
    request = _request(manager, {"approval": "writes"}, id=channel.id, aid="ghost")
    assert await _call(handlers.api_channel_update_agent, request) == (
        404,
        {"error": "agent not found"},
    )


async def test_dismiss_removes_an_agent_once(manager, events):
    channel = manager.create("incident")
    agent = channel.add_agent(role="Worker")
    dismiss = handlers.api_channel_dismiss_agent

    assert await _call(dismiss, _request(manager, id="nope", aid=agent.id)) == (
        404,
        {"error": "not found"},
    )
    assert await _call(dismiss, _request(manager, id=channel.id, aid=agent.id)) == (
        200,
        {"ok": True},
    )
    left = {"channel_id": channel.id, "agent_id": agent.id, "reason": "dismissed"}
    assert ("channel_agent_left", left) in events
    assert channel.members == {}
    assert await _call(dismiss, _request(manager, id=channel.id, aid=agent.id)) == (
        200,
        {"ok": False},
    )


async def test_wake_restarts_a_finished_agent_and_refuses_everything_else(manager, events, spawned):
    channel = manager.create("incident")
    agent = channel.add_agent(role="Worker")
    wake = handlers.api_channel_wake_agent
    refusal = (400, {"error": "agent not in terminal state"})

    assert await _call(wake, _request(manager, id="nope", aid=agent.id)) == (
        404,
        {"error": "not found"},
    )
    assert await _call(wake, _request(manager, id=channel.id, aid="ghost")) == refusal
    assert await _call(wake, _request(manager, id=channel.id, aid=agent.id)) == refusal  # pending
    spawned.spawn.assert_not_called()

    agent.state = "failed"
    assert await _call(wake, _request(manager, id=channel.id, aid=agent.id)) == (200, {"ok": True})
    # `pending`, as at boot: a clear that takes the lock before the task runs must refuse it.
    assert agent.state == "pending"
    status = {"channel_id": channel.id, "agent_id": agent.id, "state": "pending"}
    assert ("channel_agent_status", status) in events
    spawned.spawn.assert_called_once_with(agent, ("run", agent.id))


async def test_approve_answers_404_for_an_unknown_channel_or_agent(manager):
    channel = manager.create("incident")
    approve = handlers.api_channel_approve_agent

    assert await _call(approve, _request(manager, id="nope", aid="x")) == (
        404,
        {"error": "not found"},
    )
    assert await _call(approve, _request(manager, id=channel.id, aid="ghost")) == (
        404,
        {"error": "agent not found"},
    )


async def test_clear_context_refuses_an_unknown_scope_and_audits_the_denial(manager, monkeypatch):
    channel = manager.create("incident")
    audit = MagicMock()
    monkeypatch.setattr(handlers, "sel", lambda: audit)
    request = _request(manager, {"scope": "everything"}, id=channel.id)

    assert await _call(handlers.api_channel_clear_context, request) == (
        400,
        {"error": "invalid scope"},
    )
    audit.log_api_access.assert_called_once_with(
        caller="dashboard",
        operation="channel.clear_context",
        outcome="denied",
        source="dashboard",
        resources=f"{channel.id}:everything",
    )
