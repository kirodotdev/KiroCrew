"""Run an agent spec's own ``hooks`` from Crew's turn loop, for a harness that cannot.

kiro-cli reads the spec off disk and runs its ``hooks`` itself. KAS takes the agent
over the wire as ``_meta.kiro.customAgents``, whose schema has no slot for them
(``acp.kas_agents.UNSUPPORTED_SPEC_KEYS``), so on KAS nothing runs them. The backends
in ``ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS`` get them from Crew instead: the turn loop
already fires the five Crew hook events for every backend, and this module turns the
spec's field into :class:`kiro_crew.hooks.ScriptHook` records that ride the same
``ScriptHookStore.fire`` call. So a spec hook meets exactly the gates a Hooks-page
hook meets -- ``capabilities.script_hooks`` and its SEL audit, the sandboxed spawn,
the timeout, the PreToolUse fail-closed rule -- and none that a Hooks-page hook does
not.

Membership is the whole double-fire guard. A non-member's harness runs the field
itself, so the turn loop asks :func:`spec_script_hooks` only when the session's
capabilities say Crew owns the job.

Both spec shapes are read:

* the object form kiro-cli uses and Crew materializes, ``{event: [{command,
  matcher?, timeout_ms?}]}`` on the five camelCase event names;
* the array of KAS hook documents, validated by
  :func:`kiro_crew.agent.normalize_spec_hooks`. ``enabled: false`` is skipped, and
  so is ``confirm: true``, because no prompt can be shown from here; an ``agent``
  action and a trigger with no Crew event are skipped too.

The result is cached by the field's content, so a spec that stays the same costs
one conversion and logs its warnings once, not once per turn.

The turn loop is not the only place a tool runs: a subagent run and a task-runner
step fire the same hook store for their own tool calls. :func:`turn_spec_hooks`
answers for those, keyed on the SUBAGENT's agent and its own provider, so a kiro-cli
subagent under a KAS parent still gets nothing from Crew.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

from kiro_crew.agent_sdk.capabilities import capabilities_of
from kiro_crew.hooks import (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_POST_TOOL_USE,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHook,
    _normalize_hook_timeout,
)

logger = logging.getLogger(__name__)

#: kiro-cli's object-form event names, onto the events the hook store fires.
_OBJECT_EVENT_TO_HOOK_EVENT = {
    "agentSpawn": HOOK_EVENT_AGENT_SPAWN,
    "userPromptSubmit": HOOK_EVENT_USER_PROMPT_SUBMIT,
    "preToolUse": HOOK_EVENT_PRE_TOOL_USE,
    "postToolUse": HOOK_EVENT_POST_TOOL_USE,
    "stop": HOOK_EVENT_STOP,
}

#: The events whose matcher names a tool. kiro-cli reads a matcher on these only,
#: so a matcher on any other event is dropped rather than applied to the context.
_TOOL_EVENTS = frozenset({HOOK_EVENT_PRE_TOOL_USE, HOOK_EVENT_POST_TOOL_USE})

#: Bound on the hooks taken from one spec, the same total the materialized spec is
#: capped at, so a hand-written spec cannot make every turn spawn without limit.
_MAX_SPEC_HOOKS = 20

#: Bound on a retained command, matching the document validator's payload cap.
_MAX_COMMAND_LEN = 4096

#: Bound on the conversion cache. A spec edit adds an entry, so the cache is
#: cleared rather than grown once it reaches this.
_CACHE_MAX = 64

#: Per spec content: the hooks that run, and how many ``confirm: true`` documents
#: were skipped (the session-start notice names the count).
_cache: dict[tuple[str, str], tuple[tuple[ScriptHook, ...], int]] = {}


def _diagnostic(value: object) -> str:
    """A spec-supplied value for a log line: escaped, then redacted."""
    # circular import: agent imports hooks, which this module imports at load time.
    from kiro_crew.agent import _hook_diagnostic

    return _hook_diagnostic(value)


def _matcher_ok(matcher: object) -> bool:
    """The object form's matcher rules: a string, length-capped, safe characters."""
    # circular import: agent imports hooks, which this module imports at load time.
    from kiro_crew.agent import _hook_matcher_ok

    return _hook_matcher_ok(matcher)


def _reject(agent_id: str, event: object, value: object, reason: str) -> None:
    """Warn about, and SEL-audit, a spec hook that will not run.

    The same audit the kiro-cli merge writes for a hook it leaves out
    (``agent._sel_hook_rejected``, which redacts before it truncates), because what
    runs differs from what was authored either way. The conversion is cached by
    content, so an unchanged spec audits once, not once per turn.
    """
    # circular import: agent imports hooks, which this module imports at load time.
    from kiro_crew.agent import _sel_hook_rejected

    logger.warning(
        "agent %r: spec hook on %s does not run on this backend: %s",
        agent_id,
        _diagnostic(event),
        reason,
    )
    _sel_hook_rejected(str(event), str(value), f"spec hook for {agent_id}: {reason}")


def _hook(agent_id: str, event: str, index: int, entry: dict, timeout: int) -> ScriptHook | None:
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > _MAX_COMMAND_LEN:
        _reject(agent_id, event, command, "no usable command")
        return None
    matcher = entry.get("matcher") if event in _TOOL_EVENTS else None
    if matcher is not None and not _matcher_ok(matcher):
        # Dropped whole, as the materialized spec's merge drops it: running the
        # hook with no matcher would widen it to every tool.
        _reject(agent_id, event, command, "invalid matcher")
        return None
    return ScriptHook(
        id=f"spec:{agent_id}:{event}:{index}",
        name=f"{agent_id} spec hook ({event} #{index + 1})",
        event=event,
        matcher=matcher if isinstance(matcher, str) else "",
        command=command,
        timeout=timeout,
    )


def _from_object_form(agent_id: str, hooks: dict) -> list[ScriptHook]:
    out: list[ScriptHook] = []
    for key, entries in hooks.items():
        if len(out) > _MAX_SPEC_HOOKS:
            break
        event = _OBJECT_EVENT_TO_HOOK_EVENT.get(key) if isinstance(key, str) else None
        if event is None or not isinstance(entries, list):
            _reject(agent_id, key, entries, "not a hook event list")
            continue
        for index, entry in enumerate(entries):
            if len(out) > _MAX_SPEC_HOOKS:
                break
            if not isinstance(entry, dict):
                _reject(agent_id, event, entry, "entry is not an object")
                continue
            timeout_ms = entry.get("timeout_ms")
            timeout = (
                _normalize_hook_timeout(math.ceil(timeout_ms / 1000))
                if isinstance(timeout_ms, (int, float)) and not isinstance(timeout_ms, bool)
                else _normalize_hook_timeout(None)
            )
            hook = _hook(agent_id, event, index, entry, timeout)
            if hook is not None:
                out.append(hook)
    return out


def _from_documents(agent_id: str, hooks: list, unconfirmable: list[int]) -> list[ScriptHook]:
    # circular import: agent imports hooks, which this module imports at load time.
    from kiro_crew.agent import _event_for_hook_trigger, normalize_spec_hooks

    out: list[ScriptHook] = []
    for index, doc in enumerate(normalize_spec_hooks(hooks)):
        if len(out) > _MAX_SPEC_HOOKS:
            break
        trigger = doc.get("trigger")
        raw_action = doc.get("action")
        action: dict = raw_action if isinstance(raw_action, dict) else {}
        command = action.get("command")
        if doc.get("enabled") is False:
            _reject(agent_id, trigger, command, "hook is disabled")
            continue
        if doc.get("confirm") is True:
            unconfirmable.append(index)
            _reject(
                agent_id,
                trigger,
                command,
                "hook asks to be confirmed, which Crew cannot prompt for",
            )
            continue
        event = _OBJECT_EVENT_TO_HOOK_EVENT.get(_event_for_hook_trigger(trigger) or "")
        if event is None or action.get("type") != "command":
            _reject(agent_id, trigger, command, "no Crew hook event can run this trigger or action")
            continue
        # The document's timeout is in seconds, clamped like a Hooks-page hook's.
        hook = _hook(
            agent_id,
            event,
            index,
            {"command": command, "matcher": doc.get("matcher")},
            _normalize_hook_timeout(doc.get("timeout")),
        )
        if hook is not None:
            out.append(hook)
    return out


def spec_script_hooks(agent_id: str, spec: dict[str, Any]) -> list[ScriptHook]:
    """The spec's own ``hooks`` as script hooks the hook store can fire.

    Empty when the spec carries none. Either shape is read (see the module
    docstring); anything else is warned about, audited once, and yields nothing.
    """
    return list(_convert(agent_id, spec)[0])


def _convert(agent_id: str, spec: dict[str, Any]) -> tuple[tuple[ScriptHook, ...], int]:
    value = spec.get("hooks")
    if not value:
        return (), 0
    try:
        digest = hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
    except (TypeError, ValueError):
        digest = ""
    key = (agent_id, digest)
    if digest and key in _cache:
        return _cache[key]
    unconfirmable: list[int] = []
    if isinstance(value, dict):
        hooks = _from_object_form(agent_id, value)
    elif isinstance(value, list):
        hooks = _from_documents(agent_id, value, unconfirmable)
    else:
        _reject(agent_id, "hooks", value, "hooks is neither an object nor an array")
        hooks = []
    if len(hooks) > _MAX_SPEC_HOOKS:
        _reject(
            agent_id,
            "hooks",
            len(hooks),
            f"more than {_MAX_SPEC_HOOKS} spec hooks, ignoring the remainder",
        )
        hooks = hooks[:_MAX_SPEC_HOOKS]
    result = (tuple(hooks), len(unconfirmable))
    if digest:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[key] = result
    return result


def crew_fired_spec_hooks(agent_id: str) -> tuple[list[ScriptHook], list[str], int]:
    """The active agent spec's hooks as script hooks, the keys nothing carries, and
    how many ``confirm: true`` hooks are skipped.

    Reads the spec the KAS projection reads, through the same reader
    (:func:`kiro_crew.acp.kas_agents.load_agent_spec`). The second and third values
    are for the session-start notice: the spec keys a KAS session runs without, and
    the hooks that wait for a confirmation Crew cannot ask for. Raises when the spec
    cannot be read; the caller fails PreToolUse closed on that.
    """
    # circular import: the ACP layer imports the config loader, which sits below
    # this module; resolved at call time like the other driver seams here.
    from kiro_crew.acp.kas_agents import load_agent_spec, spec_keys_without_carrier
    from kiro_crew.config.paths import kiro_agents_dir

    spec = load_agent_spec(kiro_agents_dir(), agent_id)
    hooks, unconfirmable = _convert(agent_id, spec)
    return list(hooks), spec_keys_without_carrier(spec), unconfirmable


class TurnSpecHooks(NamedTuple):
    """What a subagent or task-runner turn needs to gate its permission requests."""

    #: The agent spec's own hooks, as script hooks.
    hooks: list[ScriptHook]
    #: The session's workspace, the ``extra_hooks_cwd`` the hooks run in
    #: (``None`` for the gateway's own).
    cwd: str | None
    #: The spec could not be read: every permission request is refused, as the chat
    #: turn loop does, because a deny hook that was never loaded gave no verdict.
    unreadable: bool
    #: Crew gates this turn's permission requests on PreToolUse hooks (stored and
    #: spec) itself, because its backend's projection sent the calls they cover
    #: there. False for a backend that runs the spec's hooks itself.
    gated: bool


_NOT_GATED = TurnSpecHooks([], None, False, False)


def session_agent(provider: object, agent_id: str) -> str:
    """The agent whose spec a turn on *provider* meets.

    The one the caller named, which is what the turn runs (an in-turn switch
    passes the agent it switched to). A turn that names none runs the runtime's
    default, and only the session records which that is, so it falls back to the
    agent the session is running (see ``AcpSessionHandle.kas_projected_agent``).
    """
    if agent_id:
        return agent_id
    projected = getattr(provider, "kas_projected_agent", "")
    return projected if isinstance(projected, str) else ""


async def turn_spec_hooks(provider: object, agent_id: str) -> TurnSpecHooks:
    """The spec hooks a subagent or task-runner turn gates on, and how.

    *provider* and *agent_id* are the turn's OWN: a subagent runs its own agent on
    its own backend, so a kiro-cli subagent under a KAS parent is not gated here and
    its spec is not read, as its harness already runs the field. The agent is
    resolved through :func:`session_agent`; when none can be named, every
    permission request is refused, since whose hooks apply is unknown.
    """
    if not capabilities_of(provider).crew_fires_spec_hooks:
        return _NOT_GATED
    cwd = getattr(provider, "cwd", "")
    work_dir = cwd if isinstance(cwd, str) and cwd else None
    agent_id = session_agent(provider, agent_id)
    if not agent_id:
        logger.warning("no agent is known for this KAS turn; its tool calls are blocked")
        return TurnSpecHooks([], work_dir, True, True)
    try:
        hooks, _lost, _unconfirmable = await asyncio.to_thread(crew_fired_spec_hooks, agent_id)
    except Exception:  # noqa: BLE001 - the caller fails permission requests closed
        logger.warning(
            "agent spec hooks for %r could not be read; tool calls are blocked",
            agent_id,
            exc_info=True,
        )
        return TurnSpecHooks([], work_dir, True, True)
    return TurnSpecHooks(hooks, work_dir, False, True)


async def hook_projection_stale(provider: object, agent_id: str) -> bool:
    """Whether *provider*'s live session auto-approves a capability a PreToolUse hook
    now covers.

    A KAS session keeps the agent batch it registered, and that batch withheld
    auto-approval only for what the hooks covered then. A hook added since (on the
    Hooks page or in the spec) would never see an auto-approved call, so the
    session has to be re-projected before its next turn. False on a backend that
    runs the spec's hooks itself, and when the session records no batch. A spec
    that cannot be read answers True: re-projecting is the safe direction.
    """
    if not capabilities_of(provider).crew_fires_spec_hooks:
        return False
    approved = getattr(provider, "kas_auto_approved_capabilities", None)
    if not approved:
        return False
    agent_id = session_agent(provider, agent_id)
    if not agent_id:
        return True

    def _covered() -> set[str]:
        # circular import: the ACP layer imports the config loader, which sits
        # below this module; resolved at call time like the other seams here.
        from kiro_crew.acp.kas_agents import load_agent_spec, pre_tool_hook_matchers
        from kiro_crew.acp.kas_permissions import hook_gated_capabilities
        from kiro_crew.config.paths import kiro_agents_dir

        spec = load_agent_spec(kiro_agents_dir(), agent_id)
        return hook_gated_capabilities(pre_tool_hook_matchers(agent_id, spec))

    try:
        covered = await asyncio.to_thread(_covered)
    except Exception:  # noqa: BLE001 - re-projecting is the safe direction
        logger.warning("PreToolUse hook coverage for %r could not be read; re-projecting", agent_id)
        return True
    return bool(covered & approved)


#: :func:`invalidate_stale_kas_session`'s answers.
PROJECTION_FRESH = "fresh"
PROJECTION_RESET = "reset"
PROJECTION_BUSY = "busy"

#: Resets :func:`reproject_claimed_session` makes before it gives up. One is the
#: ordinary case; a second covers a hook edit landing during the first.
_MAX_REPROJECTIONS = 2


class StaleProjectionError(RuntimeError):
    """A claimed KAS session still auto-approves what a PreToolUse hook covers."""


async def invalidate_stale_kas_session(sessions: Any, session_key: str, agent_id: str) -> str:
    """End *session_key*'s idle KAS session when its projection has gone stale.

    See :func:`hook_projection_stale`. The claim that follows registers a fresh
    batch. Answers :data:`PROJECTION_FRESH` when nothing needs doing (no live
    session, or its batch still matches the hooks), :data:`PROJECTION_RESET` when
    it was reset, and :data:`PROJECTION_BUSY` when it is stale but another turn
    holds it. BUSY is not a pass: the caller's claim waits for that turn and must
    then go through :func:`reproject_claimed_session`, which decides on the
    session the claim actually holds.
    """
    provider = sessions.get_provider(session_key)
    if provider is None or not await hook_projection_stale(provider, agent_id):
        return PROJECTION_FRESH
    logger.info(
        "agent %r: a PreToolUse hook now covers a capability the live session "
        "auto-approves; re-projecting it before the next turn",
        agent_id,
    )
    try:
        reset = await sessions.reset(session_key, skip_if_busy=True)
    except Exception:  # noqa: BLE001 - the claim-time re-check still decides
        logger.warning("re-projection reset for %s failed", session_key, exc_info=True)
        return PROJECTION_BUSY
    return PROJECTION_RESET if reset else PROJECTION_BUSY


async def reproject_claimed_session(
    sessions: Any,
    session_key: str,
    agent_id: str,
    claimed: tuple[Any, bool, bool],
    claim: Callable[[], Awaitable[tuple[Any, bool, bool]]],
) -> tuple[Any, bool, bool]:
    """The claim to run the turn on, re-projected when the claimed session is stale.

    Called with the lease held, so no other turn can change what the session
    runs between this check and the tools. A session that went stale while this
    turn waited for it (the pre-claim reset was declined because another turn
    held it) is reset here, under the lease, and *claim* registers a fresh batch.
    Raises :class:`StaleProjectionError` when the session is still stale after
    :data:`_MAX_REPROJECTIONS` resets, rather than running the turn on a batch
    that would auto-approve a call a hook covers.
    """
    for _ in range(_MAX_REPROJECTIONS):
        if not await hook_projection_stale(claimed[0], agent_id):
            return claimed
        logger.info(
            "agent %r: the claimed session auto-approves a capability a PreToolUse "
            "hook now covers; re-projecting it before the turn runs",
            agent_id,
        )
        await sessions.reset(session_key)
        claimed = await claim()
        # The reset recorded this task as the holder of the permit it popped; this
        # claim's permit is the one its release is for (a claim that allocated has
        # already said so, and saying it again changes nothing).
        adopt = getattr(sessions, "adopt_turn", None)
        if callable(adopt):
            adopt(session_key)
    if await hook_projection_stale(claimed[0], agent_id):
        raise StaleProjectionError(
            f"agent {agent_id!r}: the session could not be re-projected for the "
            "PreToolUse hooks now in place; the turn was not run"
        )
    return claimed


async def replace_stale_shared_session(
    provider: Any,
    agent_id: str,
    recreate: Callable[[], Awaitable[Any]],
) -> Any:
    """*provider*, or a replacement for it when its batch is already stale.

    For a session a run creates and owns outright (a subagent's session on its
    parent's shared runtime), where no other turn can hold it: a stale one is shut
    down and *recreate* registers a fresh batch. Raises
    :class:`StaleProjectionError` when it is still stale after
    :data:`_MAX_REPROJECTIONS` replacements.
    """
    for _ in range(_MAX_REPROJECTIONS):
        if not await hook_projection_stale(provider, agent_id):
            return provider
        logger.info(
            "agent %r: the new shared session auto-approves a capability a "
            "PreToolUse hook now covers; replacing it before the run",
            agent_id,
        )
        await provider.shutdown()
        provider = await recreate()
    if await hook_projection_stale(provider, agent_id):
        await provider.shutdown()
        raise StaleProjectionError(
            f"agent {agent_id!r}: the shared session could not be re-projected for "
            "the PreToolUse hooks now in place; the run was not started"
        )
    return provider
