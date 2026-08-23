"""Channel API handlers for the dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew.channel import (
    ApprovalPolicy,
    ChannelManager,
    ListenMode,
    _shell_base_binary,
    run_channel_agent,
)
from kiro_crew.config.loader import config_path
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)


def _deny_trust_grant(agent: Any, action: str, code: str, error: str) -> web.Response:
    """Refuse a per-command trust request, SEL-logging the denial.

    Every trust decision — grant OR refusal — must land in the audit trail;
    an unlogged denial hides a stale-card click or a consent-proof mismatch
    from the security record.
    """
    sel().log_tool_invocation(
        session_key=agent.session_key,
        agent=agent.agent_name,
        source="channel",
        tool_name=action,
        outcome="trust_pattern_denied",
        metadata={"code": code},
    )
    return web.json_response({"error": error, "code": code}, status=400)


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


async def _json_object(request: web.Request) -> dict:
    """Parse a JSON request body and require a top-level object."""
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(
            text='{"error":"invalid JSON","code":"invalid_json"}',
            content_type="application/json",
        )
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(
            text=('{"error":"request body must be a JSON object",' '"code":"body_not_object"}'),
            content_type="application/json",
        )
    return body


async def _get_channel_body(request: web.Request):
    """Get channel + parsed JSON body, or raise web.HTTPException."""
    ch = _mgr(request).get(request.match_info["id"])
    if not ch:
        raise web.HTTPNotFound(text='{"error":"not found"}', content_type="application/json")
    body = await _json_object(request)
    return ch, body


# ── List / Get ──


#: Cached ``channel_presets`` value, keyed on config.json's
#: ``(path, st_mtime_ns, st_size)``. Reading, decoding and JSON-parsing the
#: whole config file on the event loop on every call is what lets an edit land
#: without a gateway restart; the stat signature preserves that contract
#: exactly while making the repeat calls (the channel UI refetches on
#: every panel open) free.
_presets_cache: tuple[tuple[str, int, int], object] | None = None


def _load_presets() -> object:
    """Return ``channel_presets`` from config.json, re-reading only on change."""
    global _presets_cache
    path = config_path()
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        # Missing config — built-in defaults, nothing to cache against.
        return _DEFAULT_PRESETS
    cached = _presets_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    config: dict = {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            config = parsed
    except (OSError, json.JSONDecodeError):
        # Malformed config — fall through to defaults
        pass
    presets = config.get("channel_presets", _DEFAULT_PRESETS)
    _presets_cache = (key, presets)
    return presets


async def api_channel_presets(request: web.Request) -> web.Response:
    """Return channel presets from config.json, falling back to built-in defaults.

    Picks up an edit to the ``channel_presets`` key without a gateway restart:
    the read is cached on config.json's stat signature, so a changed file is
    re-read on the next call.
    """
    return web.json_response({"presets": _load_presets()})


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
    body = await _json_object(request)
    raw_topic = body.get("topic", "")
    if not isinstance(raw_topic, str):
        return web.json_response(
            {"error": "topic must be a string", "code": "channel_topic_type_invalid"},
            status=400,
        )
    topic = raw_topic.strip()[:500]
    if not topic:
        return web.json_response(
            {"error": "topic required", "code": "channel_topic_required"}, status=400
        )

    agents_def = body.get("agents", [])
    if not isinstance(agents_def, list):
        return web.json_response(
            {"error": "agents must be an array", "code": "channel_agents_type_invalid"},
            status=400,
        )
    valid_policies = {policy.value for policy in ApprovalPolicy}
    for agent_def in agents_def:
        if not isinstance(agent_def, dict):
            return web.json_response(
                {
                    "error": "each agent must be an object",
                    "code": "channel_agent_type_invalid",
                },
                status=400,
            )
        for field in ("role", "agent", "task"):
            if field in agent_def and not isinstance(agent_def[field], str):
                return web.json_response(
                    {
                        "error": f"agent {field} must be a string",
                        "code": "channel_agent_field_type_invalid",
                    },
                    status=400,
                )
        if "is_orchestrator" in agent_def and not isinstance(agent_def["is_orchestrator"], bool):
            return web.json_response(
                {
                    "error": "agent is_orchestrator must be a boolean",
                    "code": "channel_agent_orchestrator_type_invalid",
                },
                status=400,
            )
        approval = agent_def.get("approval", "writes")
        if not isinstance(approval, str) or approval not in valid_policies:
            return web.json_response(
                {
                    "error": "invalid agent approval policy",
                    "code": "channel_agent_approval_invalid",
                },
                status=400,
            )

    ch = _mgr(request).create(topic)
    if not ch:
        return web.json_response(
            {
                "error": "Channel limit reached. Close an existing channel first.",
                "code": "channel_limit_reached",
            },
            status=429,
        )

    state: DashboardState = request.app["state"]

    # Spawn agents from preset
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
    raw_content = body.get("content", "")
    if not isinstance(raw_content, str):
        return web.json_response(
            {
                "error": "content must be a string",
                "code": "channel_message_content_type_invalid",
            },
            status=400,
        )
    content = raw_content.strip()[:10000]
    if not content:
        return web.json_response({"error": "content required"}, status=400)
    # Validate mentions. Membership is a dict lookup, so an unhashable value
    # here raises TypeError rather than simply failing to match.
    raw_mention = body.get("mention")
    if raw_mention is not None:
        if isinstance(raw_mention, list):
            if not all(isinstance(name, str) for name in raw_mention):
                return _agent_field_error(
                    "mention entries must be strings",
                    "channel_message_mention_type_invalid",
                )
            raw_mention = [name for name in raw_mention if name in ch.members]
        elif not isinstance(raw_mention, str):
            return _agent_field_error(
                "mention must be a string or an array of strings",
                "channel_message_mention_type_invalid",
            )
        elif raw_mention not in ch.members:
            raw_mention = None
    # Validate thread_id
    thread_id = body.get("thread_id")
    if thread_id is not None:
        if not isinstance(thread_id, str):
            return _agent_field_error(
                "thread_id must be a string",
                "channel_message_thread_id_type_invalid",
            )
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


def _agent_field_error(error: str, code: str) -> web.Response:
    return web.json_response({"error": error, "code": code}, status=400)


async def api_channel_add_agent(request: web.Request) -> web.Response:
    ch, body = await _get_channel_body(request)

    role = body.get("role", "Agent")
    if not isinstance(role, str):
        return _agent_field_error("role must be a string", "channel_agent_role_type_invalid")
    agent_name = body.get("agent", "")
    if not isinstance(agent_name, str):
        return _agent_field_error("agent must be a string", "channel_agent_name_type_invalid")
    task = body.get("task", ch.topic)
    if not isinstance(task, str):
        return _agent_field_error("task must be a string", "channel_agent_task_type_invalid")
    is_orchestrator = body.get("is_orchestrator", False)
    if not isinstance(is_orchestrator, bool):
        return _agent_field_error(
            "is_orchestrator must be a boolean",
            "channel_agent_orchestrator_type_invalid",
        )
    try:
        approval_policy = ApprovalPolicy(body.get("approval", "writes"))
    except (TypeError, ValueError):
        return _agent_field_error(
            "approval must be a valid policy", "channel_agent_approval_invalid"
        )

    agent = ch.add_agent(
        role=role[:100],
        agent_name=agent_name,
        task=task,
        is_orchestrator=is_orchestrator,
        approval_policy=approval_policy,
    )
    if not agent:
        return web.json_response(
            {"error": "Agent limit reached. Dismiss an agent first."},
            status=429,
        )

    state: DashboardState = request.app["state"]
    _spawn_agent_task(
        agent, run_channel_agent(agent, ch, state.sessions, is_yolo=lambda: state._yolo)
    )
    return web.json_response({"ok": True, "agent": agent.to_dict()})


async def api_channel_update_agent(request: web.Request) -> web.Response:
    ch, body = await _get_channel_body(request)
    agent = ch.members.get(request.match_info["aid"])
    if not agent:
        return web.json_response({"error": "agent not found"}, status=404)

    approval_policy = agent.approval_policy
    listen_mode = agent.listen_mode
    if "approval" in body:
        try:
            approval_policy = ApprovalPolicy(body["approval"])
        except (TypeError, ValueError):
            return _agent_field_error(
                "approval must be a valid policy", "channel_agent_approval_invalid"
            )
    if "listen" in body:
        try:
            listen_mode = ListenMode(body["listen"])
        except (TypeError, ValueError):
            return _agent_field_error("listen must be a valid mode", "channel_agent_listen_invalid")
    agent.approval_policy = approval_policy
    agent.listen_mode = listen_mode
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
    _spawn_agent_task(
        agent, run_channel_agent(agent, ch, state.sessions, is_yolo=lambda: state._yolo)
    )
    return web.json_response({"ok": True})


async def api_channel_approve_agent(request: web.Request) -> web.Response:
    ch = _mgr(request).get(request.match_info["id"])
    if not ch:
        return web.json_response({"error": "not found"}, status=404)
    agent = ch.members.get(request.match_info["aid"])
    if not agent:
        return web.json_response({"error": "agent not found"}, status=404)
    body = await _json_object(request)
    action = body.get("action", "rejected")  # approved|rejected|trust|trust_command|trust_base
    if action not in ("approved", "rejected", "trust", "trust_command", "trust_base"):
        return web.json_response({"error": "invalid action"}, status=400)
    if agent._approval_future and not agent._approval_future.done():
        if action in ("trust_command", "trust_base"):
            # Per-command grant scoped to THIS agent, derived SERVER-SIDE
            # from the pending approval's canonical shell command (stashed by
            # ``_stream_task`` from the provider's ``tool_input``). The
            # request-body ``pattern`` is the CONSENT PROOF: it must agree
            # with the pending command, so a click on a stale card (whose
            # pattern describes an older command) or a card whose
            # LLM-influenced title diverged from the real command fails
            # closed instead of granting trust for a command the user never
            # read. Grants are OPAQUE LITERALS (see ``ChannelAgent``): the
            # exact tier stores the whole command text, matched by string
            # equality; the base tier stores one shlex-derived binary name,
            # refused outright for compound / quoted / env-prefixed /
            # unparseable commands. No pattern language, no derived
            # sub-patterns — every derivation scheme reviewed on this
            # surface widened scope beyond what the card displayed.
            cmd = agent._pending_approval_command
            if not cmd:
                return _deny_trust_grant(
                    agent,
                    action,
                    "pattern_underivable",
                    "per-command trust needs a pending shell "
                    "command; use approve or trust instead",
                )
            pattern = body.get("pattern", "")
            if not isinstance(pattern, str) or not pattern:
                return _deny_trust_grant(
                    agent, action, "pattern_required", "pattern required for " + action
                )
            if action == "trust_command":
                if pattern != cmd:
                    return _deny_trust_grant(
                        agent,
                        action,
                        "approval_superseded",
                        "pattern does not match the pending " "command; the approval card is stale",
                    )
                agent._trusted_commands.add(cmd)
                granted = f"command:{cmd}"
            else:
                # trust_base: the card consents to one binary ("Trust all
                # <base> commands"). The binary must be derivable from the
                # pending command as a SIMPLE invocation — a compound command
                # has no single base to consent to, and its later standalone
                # segment would run outside the shell context the user read
                # ("cd /tmp/safe && rm target" does not license a bare
                # "rm target" elsewhere).
                base = _shell_base_binary(cmd)
                if base is None:
                    return _deny_trust_grant(
                        agent,
                        action,
                        "pattern_underivable",
                        "per-command trust needs a pending shell "
                        "command; use approve or trust instead",
                    )
                if pattern not in (base, f"{base} *"):
                    return _deny_trust_grant(
                        agent,
                        action,
                        "approval_superseded",
                        "pattern does not match the pending " "command; the approval card is stale",
                    )
                agent._trusted_bases.add(base)
                granted = f"base:{base}"
            sel().log_tool_invocation(
                session_key=agent.session_key,
                agent=agent.agent_name,
                source="channel",
                tool_name=action,
                outcome="trust_pattern_granted",
                metadata={"granted": granted},
            )
            # The waiter maps "approved" to approving the pending tool; the
            # grant above governs subsequent requests.
            agent._approval_future.set_result("approved")
            return web.json_response({"ok": True})
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
            caller="dashboard",
            operation="channel.clear_context",
            outcome="denied",
            source="dashboard",
            resources=request.match_info["id"],
        )
        return web.json_response({"error": "not found"}, status=404)

    try:
        body = await _json_object(request)
    except web.HTTPBadRequest:
        sel().log_api_access(
            caller="dashboard",
            operation="channel.clear_context",
            outcome="denied",
            source="dashboard",
            resources=ch.id,
        )
        return web.json_response({"error": "invalid or missing request body"}, status=400)

    scope = body.get("scope", "all")
    agent_id = body.get("agent_id")
    state: DashboardState = request.app["state"]

    if scope not in ("all", "agent"):
        sel().log_api_access(
            caller="dashboard",
            operation="channel.clear_context",
            outcome="denied",
            source="dashboard",
            resources=f"{ch.id}:{scope}",
        )
        return web.json_response({"error": "invalid scope"}, status=400)

    cleared: list[str] = []

    if scope == "agent":
        if not agent_id:
            sel().log_api_access(
                caller="dashboard",
                operation="channel.clear_context",
                outcome="denied",
                source="dashboard",
                resources=ch.id,
            )
            return web.json_response({"error": "agent_id required"}, status=400)
        agent = ch.members.get(agent_id)
        if not agent:
            sel().log_api_access(
                caller="dashboard",
                operation="channel.clear_context",
                outcome="denied",
                source="dashboard",
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
        caller="dashboard",
        operation="channel.clear_context",
        outcome="allowed",
        source="dashboard",
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
