"""Channel API handlers for the dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew.channel import ChannelManager, run_channel_agent
from kiro_crew.config.loader import config_path
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)


def _spawn_agent_task(agent, coro) -> asyncio.Task:
    """Create a task with error logging and store ref on agent for cancellation."""
    task = asyncio.create_task(coro)
    agent._task = task
    task.add_done_callback(
        lambda t: (
            logger.error("Agent task failed: %s", t.exception())
            if not t.cancelled() and t.exception()
            else None
        )
    )
    return task


_DEFAULT_PRESETS = [
    {
        "id": "incident",
        "label": "Incident Response",
        "agents": [
            {
                "role": "Orchestrator",
                "is_orchestrator": True,
                "task": "Coordinate investigation of {topic}",
            },
            {"role": "Logs Agent", "task": "Search logs related to {topic}"},
            {"role": "Code Agent", "task": "Check recent code changes related to {topic}"},
        ],
    },
    {
        "id": "review",
        "label": "Code Review",
        "agents": [
            {"role": "Reviewer", "is_orchestrator": True, "task": "Review code for {topic}"},
        ],
    },
    {
        "id": "research",
        "label": "Research",
        "agents": [
            {
                "role": "Orchestrator",
                "is_orchestrator": True,
                "task": "Research and synthesize findings on {topic}",
            },
            {"role": "Search Agent", "task": "Search documentation and code for {topic}"},
        ],
    },
    {"id": "custom", "label": "Custom (empty)", "agents": []},
]


def _mgr(request: web.Request) -> ChannelManager:
    state: DashboardState = request.app["state"]
    mgr = getattr(state, "channel_manager", None)
    assert mgr is not None, "ChannelManager not initialized"
    return mgr


async def _get_channel_body(request: web.Request):
    """Get channel + parsed JSON body, or raise web.HTTPException."""
    ch = _mgr(request).get(request.match_info["id"])
    if not ch:
        raise web.HTTPNotFound(text='{"error":"not found"}', content_type="application/json")
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error":"invalid JSON"}', content_type="application/json")
    return ch, body


# ── List / Get ──


async def api_channel_presets(request: web.Request) -> web.Response:
    """Return channel presets from config.json, falling back to built-in defaults.

    Reads ``~/.kirocrew/config.json`` fresh on each call so users can edit the
    ``channel_presets`` key without restarting the gateway.
    """
    config: dict = {}
    try:
        parsed = json.loads(config_path().read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            config = parsed
    except (OSError, json.JSONDecodeError):
        # Missing or malformed config — fall through to defaults
        pass
    presets = config.get("channel_presets", _DEFAULT_PRESETS)
    return web.json_response({"presets": presets})


async def api_channels_list(request: web.Request) -> web.Response:
    return web.json_response({"channels": _mgr(request).list_channels()})


async def api_channel_get(request: web.Request) -> web.Response:
    ch = _mgr(request).get(request.match_info["id"])
    if not ch:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(
        {
            **ch.to_dict(),
            "messages": [m.to_dict() for m in ch.messages[-50:]],
        }
    )


# ── Create / Close ──


async def api_channel_create(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    topic = body.get("topic", "").strip()[:500]
    if not topic:
        return web.json_response({"error": "topic required"}, status=400)

    ch = _mgr(request).create(topic)
    if not ch:
        return web.json_response(
            {"error": "Channel limit reached. Close an existing channel first."},
            status=429,
        )

    state: DashboardState = request.app["state"]

    # Spawn agents from preset
    agents_def = body.get("agents", [])
    has_orchestrator = any(a.get("is_orchestrator") for a in agents_def)
    if not has_orchestrator:
        agents_def = [
            {"role": "Orchestrator", "is_orchestrator": True, "task": topic},
            *agents_def,
        ]

    for agent_def in agents_def:
        agent = ch.add_agent(
            role=agent_def.get("role", "Agent"),
            agent_name=agent_def.get("agent", ""),
            task=agent_def.get("task", topic),
            is_orchestrator=agent_def.get("is_orchestrator", False),
            approval_policy=agent_def.get("approval", "writes"),
        )
        if agent:
            _spawn_agent_task(
                agent, run_channel_agent(agent, ch, state.sessions, is_yolo=lambda: state._yolo)
            )

    return web.json_response({"ok": True, "channel": ch.to_dict()})


async def api_channel_close(request: web.Request) -> web.Response:
    ok = _mgr(request).close(request.match_info["id"])
    return web.json_response({"ok": ok})


# ── Messages ──


async def api_channel_post(request: web.Request) -> web.Response:
    ch, body = await _get_channel_body(request)
    content = body.get("content", "").strip()[:10000]
    if not content:
        return web.json_response({"error": "content required"}, status=400)
    # Validate mentions
    raw_mention = body.get("mention")
    if raw_mention:
        if isinstance(raw_mention, list):
            raw_mention = [m for m in raw_mention if m in ch.members]
        elif raw_mention not in ch.members:
            raw_mention = None
    # Validate thread_id
    thread_id = body.get("thread_id")
    if thread_id and thread_id not in ch._msg_index:
        thread_id = None
    msg = await ch.post(
        "human",
        content,
        from_role="You",
        mention=raw_mention,
        msg_type="broadcast",
        thread_id=thread_id,
    )
    return web.json_response({"ok": True, "message": msg.to_dict()})


# ── Agent management ──


async def api_channel_add_agent(request: web.Request) -> web.Response:
    ch, body = await _get_channel_body(request)

    agent = ch.add_agent(
        role=body.get("role", "Agent")[:100],
        agent_name=body.get("agent", ""),
        task=body.get("task", ch.topic),
        is_orchestrator=body.get("is_orchestrator", False),
        approval_policy=body.get("approval", "writes"),
    )
    if not agent:
        return web.json_response(
            {"error": "Agent limit reached. Dismiss an agent first."},
            status=429,
        )

    state: DashboardState = request.app["state"]
    _spawn_agent_task(agent, run_channel_agent(agent, ch, state.sessions, is_yolo=lambda: state._yolo))
    return web.json_response({"ok": True, "agent": agent.to_dict()})


async def api_channel_update_agent(request: web.Request) -> web.Response:
    ch, body = await _get_channel_body(request)
    agent = ch.members.get(request.match_info["aid"])
    if not agent:
        return web.json_response({"error": "agent not found"}, status=404)

    if "approval" in body:
        from kiro_crew.channel import ApprovalPolicy

        try:
            agent.approval_policy = ApprovalPolicy(body["approval"])
        except ValueError:
            pass
    if "listen" in body:
        from kiro_crew.channel import ListenMode

        try:
            agent.listen_mode = ListenMode(body["listen"])
        except ValueError:
            pass
    ch._save()
    return web.json_response({"ok": True, "agent": agent.to_dict()})


async def api_channel_dismiss_agent(request: web.Request) -> web.Response:
    ch = _mgr(request).get(request.match_info["id"])
    if not ch:
        return web.json_response({"error": "not found"}, status=404)
    ok = ch.remove_agent(request.match_info["aid"])
    return web.json_response({"ok": ok})


async def api_channel_wake_agent(request: web.Request) -> web.Response:
    ch = _mgr(request).get(request.match_info["id"])
    if not ch:
        return web.json_response({"error": "not found"}, status=404)
    aid = request.match_info["aid"]
    agent = ch.members.get(aid)
    if not agent or agent.state not in ("done", "failed"):
        return web.json_response({"error": "agent not in terminal state"}, status=400)

    agent.state = "listening"
    ch._broadcast(
        "channel_agent_status",
        {"channel_id": ch.id, "agent_id": aid, "state": "listening"},
    )
    state: DashboardState = request.app["state"]
    _spawn_agent_task(agent, run_channel_agent(agent, ch, state.sessions, is_yolo=lambda: state._yolo))
    return web.json_response({"ok": True})


async def api_channel_approve_agent(request: web.Request) -> web.Response:
    ch = _mgr(request).get(request.match_info["id"])
    if not ch:
        return web.json_response({"error": "not found"}, status=404)
    agent = ch.members.get(request.match_info["aid"])
    if not agent:
        return web.json_response({"error": "agent not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    action = body.get("action", "rejected")  # approved|rejected|trust
    if action not in ("approved", "rejected", "trust"):
        return web.json_response({"error": "invalid action"}, status=400)
    if agent._approval_future and not agent._approval_future.done():
        agent._approval_future.set_result(action)
        if action == "trust":
            ch.trusted = True
            ch._save()
            st: DashboardState = request.app["state"]
            st.push_slots_update()
        return web.json_response({"ok": True})
    return web.json_response({"error": "no pending approval"}, status=400)


# ── Context Management ──


async def api_channel_clear_context(request: web.Request) -> web.Response:
    """Clear LLM context for one or all agents in a channel.

    Resets agent sessions (via SessionManager.reset) while preserving all
    channel configuration. Agents get a fresh context on their next message.

    Body: {"scope": "all"} or {"scope": "agent", "agent_id": "<id>"}

    Scope semantics:
      * scope=all   — resets every agent's LLM session AND wipes the channel's
                      shared message buffer + exchange counts. Persisted via _save().
      * scope=agent — resets ONLY the named agent's LLM session. The channel's
                      shared message history and exchange counts are preserved,
                      so the cleared agent will still see prior messages on its
                      next turn. To reset shared history use scope=all.

    Concurrency: this handler does not hold a per-channel lock. Sibling channel
    mutation handlers (api_channel_close, api_channel_dismiss_agent, api_channel_post)
    follow the same pattern and rely on the manager's serialized access. A concurrent
    api_channel_post during scope=all clear may produce a message that gets clobbered
    by the subsequent ``ch.messages.clear()``; this is consistent with the existing
    codebase pattern for channel mutations.

    Pending tool approvals: any in-flight tool-approval futures held by the agent
    are not cancelled here — they are owned by the agent task spawned by
    run_channel_agent and will resolve naturally (rejected on session reset).
    """
    ch = _mgr(request).get(request.match_info["id"])
    if not ch:
        sel().log_api_access(
            caller="dashboard", operation="channel.clear_context",
            outcome="denied", source="dashboard",
            resources=request.match_info["id"],
        )
        return web.json_response({"error": "not found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        sel().log_api_access(
            caller="dashboard", operation="channel.clear_context",
            outcome="denied", source="dashboard", resources=ch.id,
        )
        return web.json_response({"error": "invalid or missing request body"}, status=400)

    scope = body.get("scope", "all")
    agent_id = body.get("agent_id")
    state: DashboardState = request.app["state"]

    if scope not in ("all", "agent"):
        sel().log_api_access(
            caller="dashboard", operation="channel.clear_context",
            outcome="denied", source="dashboard", resources=f"{ch.id}:{scope}",
        )
        return web.json_response({"error": "invalid scope"}, status=400)

    cleared: list[str] = []

    if scope == "agent":
        if not agent_id:
            sel().log_api_access(
                caller="dashboard", operation="channel.clear_context",
                outcome="denied", source="dashboard", resources=ch.id,
            )
            return web.json_response({"error": "agent_id required"}, status=400)
        agent = ch.members.get(agent_id)
        if not agent:
            sel().log_api_access(
                caller="dashboard", operation="channel.clear_context",
                outcome="denied", source="dashboard",
                resources=f"{ch.id}:{agent_id}",
            )
            return web.json_response({"error": "agent not found"}, status=404)
        if agent.session_key:
            await state.sessions.reset(agent.session_key)
            cleared.append(agent.role or agent.id)
    else:
        for agent in ch.members.values():
            if agent.session_key:
                await state.sessions.reset(agent.session_key)
                cleared.append(agent.role or agent.id)
        ch.messages.clear()
        ch._msg_index.clear()
        ch.exchange_counts.clear()
        ch._save()

    sel().log_api_access(
        caller="dashboard", operation="channel.clear_context",
        outcome="allowed", source="dashboard",
        resources=f"{ch.id}:{scope}:{','.join(cleared)}",
    )

    # Notify other clients (multi-tab UX) so their stale message buffers refresh.
    ch._broadcast(
        "channel_context_cleared",
        {
            "channel_id": ch.id,
            "scope": scope,
            "agent_id": agent_id if scope == "agent" else None,
            "cleared": cleared,
        },
    )

    return web.json_response({"ok": True, "cleared": cleared})
