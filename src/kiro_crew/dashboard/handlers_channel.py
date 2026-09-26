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
    has_queued_work,
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


def _closed_under_us(request: web.Request, ch) -> bool:
    """Whether *ch* left the manager while this request waited for its lock.

    A handler resolves the channel before taking `_log_lock`, so a close can win the lock,
    pop the channel and delete its file in between. Mutating the detached object then calls
    `_save()`, which writes the file back and `_load_all` restores a channel the user closed.
    """
    return _mgr(request).get(ch.id) is not ch


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
    """Close a channel, waiting for any in-flight clear to finish first.

    The close deletes the channel's file, while a clear-context holds `_log_lock` across an
    awaited teardown and persists through `_save()` afterwards. Taking the same lock orders
    the two, so the clear's write lands before the delete rather than the delete landing
    first and the write recreating the file for `_load_all` to restore.
    """
    channel_id = request.match_info["id"]
    ch = _mgr(request).get(channel_id)
    if not ch:
        return web.json_response({"ok": False})
    async with ch._log_lock:
        ok = _mgr(request).close(channel_id)
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
    if msg is None:
        return web.json_response(
            {"error": "channel was closed", "code": "channel_closed"}, status=404
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

    async with ch._log_lock:
        if _closed_under_us(request, ch):
            return web.json_response(
                {"error": "channel was closed", "code": "channel_closed"}, status=404
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
    async with ch._log_lock:
        if _closed_under_us(request, ch):
            return web.json_response(
                {"error": "channel was closed", "code": "channel_closed"}, status=404
            )
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
            # sub-patterns: a derived pattern widens scope beyond what the
            # card displayed.
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


#: Bound on the clear's own work while `_log_lock` is held, since `post` shares that lock. It
#: does NOT bound the whole clear: the grace below is spent after this deadline expires.
_CLEAR_DISCARD_TIMEOUT_SECS = 30.0

#: How long a cancelled teardown gets to answer. Separate from the clear's own deadline: this
#: one is spent only on the refusal path, and it is what makes a reported refusal true.
_CANCEL_GRACE_SECS = 2.0


async def _resumable(state, key: str, *, on_error: bool) -> bool:
    """Whether a persisted resume SID survives for *key*, read OFF the event loop.

    `resumable_sid` reaches `SessionMap.get`, which stats the transcript files and can rewrite
    the map to prune a stale entry. That is store I/O, so it runs in a thread.

    `on_error` is the caller's safe direction, and the two callers differ: as a PRESENCE probe
    a True would credit a clear nobody confirmed, while as a REFUSAL probe a False would claim
    one. Neither default is safe for both, so each states its own.
    """
    try:
        return bool(await asyncio.to_thread(state.sessions.resumable_sid, key))
    except Exception:
        return on_error


async def _note_reset(state, agent, cleared: list, busy: list, deadline: float) -> None:
    """Reset one member's session and record it as cleared or refused.

    A channel member holds its lifecycle lease across its whole listening life, so refusing on
    the lease alone would refuse every clear forever; `refuse_only_on_active_turn=True` narrows
    it to a declared lifecycle turn. A member holding an acknowledged-but-undequeued message
    declares no turn yet, so it is refused ahead of everything below or the wipe erases a
    prompt it still runs. A key with nothing registered takes the teardown path and answers
    True, so presence is probed separately and an absent session is NOT reported as cleared.

    Only a LIVE member can be refused. A member in a terminal state holds any message that
    queued during its last turn for good, because `Channel.post` skips it and nothing else
    drains its inbox, so probing the queue alone refuses that member's clear on every later
    request -- and the wedge notice tells the user to clear its context to recover.

    A REPORTED REFUSAL MUST BE TRUE, so the deadline path cancels rather than leaving the
    shielded teardown running: the discard pops the session and clears the SID before the slow
    `provider.shutdown()`, so a member whose wait expires before its own pop would be reported
    busy while the surviving task discards the session the API said it kept. The registry is
    therefore read AFTER the teardown settles.
    """
    label = agent.role or agent.id
    if agent.state not in ("done", "failed") and (
        # The STATE too, not the queue alone: a dequeued message leaves the queue empty while
        # the turn is only declared later, and the wipe in that window is unrecoverable.
        has_queued_work(agent)
        or agent.state == "working"
    ):
        busy.append(label)
        return
    # `reset` keeps the persisted resume SID, so an idle or expired session reloads the very
    # conversation this endpoint reports cleared. `discard_conversation` drops it.
    # BOTH probes: `has_session` sees only a LIVE session, and the discard also drops the
    # persisted resume SID, so a restored member's clear would be reported as nothing.
    had_session = bool(state.sessions.has_session(agent.session_key)) or await _resumable(
        state, agent.session_key, on_error=False
    )
    _teardown = asyncio.ensure_future(
        state.sessions.discard_conversation(
            agent.session_key, skip_if_busy=True, refuse_only_on_active_turn=True
        )
    )
    try:
        discarded = await asyncio.wait_for(
            asyncio.shield(_teardown),
            timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
        )
    except (Exception, asyncio.CancelledError) as exc:
        if isinstance(exc, asyncio.CancelledError) and not _teardown.done():
            # This request is being cancelled, not the teardown: stay out of its way.
            raise
        if not isinstance(exc, asyncio.TimeoutError):
            # The SID is already cleared by the time a shutdown can raise, so letting it out
            # answers 500 for a destructive request that partly succeeded. Reconcile instead.
            logger.warning(
                "Clear for %s: teardown raised %s; reconciling from the registry",
                agent.session_key,
                type(exc).__name__,
            )
            live = bool(state.sessions.has_session(agent.session_key))
            if live or await _resumable(state, agent.session_key, on_error=True):
                busy.append(label)
            elif had_session:
                cleared.append(label)
            return
        # The shield keeps the teardown alive past the deadline, so a refusal answered now
        # would be contradicted by it -- see this function's docstring.
        _teardown.cancel()
        # OBSERVE, never cancel a second time: `wait_for` cancels what it waits on, and that
        # lands inside the teardown's own `finally`, skipping the runtime release it owes.
        done, _still_running = await asyncio.wait({_teardown}, timeout=_CANCEL_GRACE_SECS)
        # Empty means an uncancellable shutdown. Releasing the lock matters more than waiting
        # it out -- `post` is behind it -- so the answer is decided below without it.
        settled = bool(done)
        # Read AFTER it settled: with nothing still running, this is the final state.
        try:
            still_registered = bool(state.sessions.has_session(agent.session_key))
        except Exception:
            # Unreadable registry: refuse rather than claim a clear nothing confirmed.
            still_registered = True
        if settled and (
            still_registered or await _resumable(state, agent.session_key, on_error=True)
        ):
            # A TRUE refusal: nothing was discarded. The SID counts as well as the registry,
            # cleared LATE, so a cancellation before it leaves a "cleared" member resumable.
            busy.append(label)
            return
        if not settled:
            # Cannot promise either outcome, so it reports the one that cannot lose a
            # conversation: a false refusal hides a context being destroyed.
            logger.warning(
                "Clear for %s: teardown did not answer cancellation within %.1fs; reporting "
                "cleared because a refusal cannot be guaranteed once it may still commit",
                agent.session_key,
                _CANCEL_GRACE_SECS,
            )
            if had_session:
                cleared.append(label)
            return
        # The context IS gone, so reporting a refusal would tell the user their history
        # survived while the next turn starts empty. Slowness is logged, not reported as one.
        logger.warning(
            "Clear for %s: provider shutdown still running when the clear's %.0fs deadline "
            "expired; the conversation was already discarded, so it is reported as done",
            agent.session_key,
            _CLEAR_DISCARD_TIMEOUT_SECS,
        )
        if had_session:
            cleared.append(label)
        return
    if discarded:
        # Only a session that EXISTED can have been cleared. An absent one had no context, so
        # naming it cleared would credit this endpoint with work it did not do.
        if had_session:
            cleared.append(label)
    else:
        busy.append(label)


async def api_channel_clear_context(request: web.Request) -> web.Response:
    """Clear LLM context for one or all agents in a channel.

    Discards agent conversations (via SessionManager.discard_conversation) while preserving
    all channel configuration. `reset` would keep the persisted resume SID, so the very
    conversation this endpoint reports cleared would reload; the discard drops it. Agents get
    a fresh context on their next message.

    Body: {"scope": "all"} or {"scope": "agent", "agent_id": "<id>"}

    Scope semantics:
      * scope=all   -- discards every agent's LLM session AND wipes the channel's
                      shared message buffer + exchange counts, but ONLY when every
                      member was idle. A PARTIAL clear leaves the shared buffer
                      intact, because a busy member keeps the LLM context that
                      references it. Persisted via _save().
      * scope=agent -- discards ONLY the named agent's LLM session. The channel's
                      shared message history and exchange counts are preserved,
                      so the cleared agent will still see prior messages on its
                      next turn. To reset shared history use scope=all.

    Refusal contract: a member with a declared lifecycle turn in flight is refused rather
    than destroyed. Any refusal, total or partial, answers 409 with code "turn_in_flight"
    and names the refused members in `error`, which the dashboard renders verbatim, so a
    caller that only branches on the status still reports the refusal. The total refusal is
    answered before the buffer wipe and the partial path skips that wipe, so no response
    reports shared state as kept after destroying it. Only a fully clean clear answers 200
    and broadcasts channel_context_cleared.

    Concurrency: the whole clear runs under the channel's `_log_lock`, the same lock
    `Channel.post` holds across its append, delivery and `_save()`, so a post concurrent with
    a scope=all clear is safe from ``ch.messages.clear()``: it either precedes the clear or
    refuses the member it targets. Each member's discard is bounded by
    _CLEAR_DISCARD_TIMEOUT_SECS and a cancelled teardown gets _CANCEL_GRACE_SECS on top, but
    neither is a bound on the lock hold: the grace is spent per member, and the wipe's
    synchronous `_save()` still holds the lock. The grace wait itself RETURNS at its deadline,
    but the teardown it was watching may still be running when it does. So the constants bound
    the discard waits only: the wipe's `_save()` is unbounded, so a waiting post has no
    guaranteed ceiling. Membership and lifetime changes take the same lock, so `add_agent`,
    `remove_agent` and a channel close cannot land mid-clear.

    Pending tool approvals: an in-flight tool-approval future keeps its member `working`, so
    that member is REFUSED rather than cleared. The future is not cancelled here either way --
    it is owned by the agent task spawned by run_channel_agent.
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
    # A clear-context click is USER-COMMANDED, so a refused reset is reported rather than
    # swallowed -- declining is right, but pretending it cleared is not.
    busy: list[str] = []

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
    # The whole clear runs under the channel's log lock: the resets below AWAIT, so a post
    # cannot reach the inbox mid-clear and one already there refuses its member.
    async with ch._log_lock:
        if _closed_under_us(request, ch):
            # Every other exit of this handler writes a row; this one refuses a destructive
            # request, so it owes one too.
            sel().log_api_access(
                caller="dashboard",
                operation="channel.clear_context",
                outcome="denied",
                source="dashboard",
                resources=f"{ch.id}:{scope}:channel_closed",
            )
            return web.json_response(
                {"error": "channel was closed", "code": "channel_closed"}, status=404
            )
        # ONE deadline for the whole clear, not one per member: `post` waits on this lock, so
        # an N-member channel with a per-member bound stalls every message for up to N x 30s.
        deadline = asyncio.get_running_loop().time() + _CLEAR_DISCARD_TIMEOUT_SECS
        if scope == "agent":
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
                await _note_reset(state, agent, cleared, busy, deadline)
        else:
            # A SNAPSHOT: the body awaits, and a mutation mid-iteration raises RuntimeError
            # after some members are already discarded.
            for agent in list(ch.members.values()):
                if agent.session_key:
                    await _note_reset(state, agent, cleared, busy, deadline)

        # BEFORE the buffer wipe below: that is shared state `_save()` persists, so a 409
        # answered after it destroys the log this response reports as untouched.
        if busy and not cleared:
            sel().log_api_access(
                caller="dashboard",
                operation="channel.clear_context",
                outcome="denied",
                source="dashboard",
                resources=f"{ch.id}:{scope}:busy={','.join(busy)}",
            )
            return web.json_response(
                {
                    "error": (
                        "context not cleared: "
                        + ", ".join(busy)
                        + " had a turn in flight. Nothing was cleared -- retry when idle."
                    ),
                    "code": "turn_in_flight",
                },
                status=409,
            )

        # Gated on a FULLY clean clear: the log is shared, and a busy member keeps the LLM
        # context that references it, so wiping it here would strand that member's replies.
        cleared_shared_log = scope != "agent" and not busy
        if cleared_shared_log:
            ch.messages.clear()
            ch._msg_index.clear()
            ch.exchange_counts.clear()
            ch._save()

    # `partial` is its own outcome: a 409-answered request audited as "allowed" reads as a
    # clean clear, and the refused roles then appear in no row on any path.
    sel().log_api_access(
        caller="dashboard",
        operation="channel.clear_context",
        outcome="partial" if busy else "allowed",
        source="dashboard",
        resources=(
            f"{ch.id}:{scope}:{','.join(cleared)}" + (f":busy={','.join(busy)}" if busy else "")
        ),
    )

    # Only when the shared log actually emptied. The listener REPLACES its retained transcript
    # with an empty list, so announcing a partial clear wipes the log this request just kept.
    if cleared_shared_log:
        # Carries what the listener reads and nothing else: the gate above forces `scope` to
        # "all", so a per-agent id, the cleared roles and the busy roles are all dead here.
        ch._broadcast(
            "channel_context_cleared",
            {
                "channel_id": ch.id,
                "scope": scope,
            },
        )

    if busy:
        return web.json_response(
            {
                "error": (
                    "context not cleared for "
                    + ", ".join(busy)
                    + ": a turn was in flight. Cleared "
                    + ", ".join(cleared)
                    + "; the shared message log was kept. Retry when idle."
                ),
                "code": "turn_in_flight",
            },
            status=409,
        )
    return web.json_response({"ok": True, "cleared": cleared}, status=200)
