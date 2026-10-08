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

A tool matcher is written in kiro-cli's tool names (``execute_bash``, ``fs_write``),
and KAS names its tools differently. On a PreToolUse permission request the turn
loop therefore matches these hooks against
:func:`spec_hook_tool_names` of the id KAS states for the call, not against the
call's title; a call KAS names no tool for is matched on
its title, as a Hooks-page hook is. A PostToolUse still sees the
title, because KAS's tool-call frames carry no tool id. A PreToolUse matcher the
table does not know is kept, matched against KAS's id as written, and warned about
once.

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
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
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

#: The ``@server/tool`` form an MCP tool is matched by on every backend Crew fires
#: spec hooks for: ``hooks.pre_tool_match_names`` builds it from the trusted server
#: name, the goose/opencode tables state it, and the KAS projection reads an ``@``
#: matcher as MCP. The object form's character set has neither ``@`` nor ``/``, so
#: on its own it drops the documented spelling of a guard Crew can serve. The server
#: is one literal name, never a glob, which would widen the guard across servers; the
#: tool keeps the object form's own set, ``*`` included (``@server/*``).
_MCP_MATCHER_RE = re.compile(r"@[A-Za-z0-9_.\-]+/[A-Za-z0-9_.*\-]+")

#: Per spec content: the hooks that run, and how many ``confirm: true`` documents
#: were skipped (the session-start notice names the count).
_cache: dict[tuple[str, str], tuple[tuple[ScriptHook, ...], int]] = {}


def _diagnostic(value: object) -> str:
    """A spec-supplied value for a log line: escaped, then redacted."""
    # circular import: agent imports hooks, which this module imports at load time.
    from kiro_crew.agent import _hook_diagnostic

    return _hook_diagnostic(value)


def _matcher_ok(matcher: object) -> bool:
    """The object form's matcher rules, plus an MCP tool's ``@server/tool`` form.

    Both are length-capped. The extra form is accepted here only: kiro-cli runs its
    spec's ``hooks`` itself and never reaches this conversion, so the materialized
    spec's rule stays as it is.
    """
    # circular import: agent imports hooks, which this module imports at load time.
    from kiro_crew.agent import _MAX_MATCHER_LEN, _hook_matcher_ok

    if _hook_matcher_ok(matcher):
        return True
    return (
        isinstance(matcher, str)
        and len(matcher) <= _MAX_MATCHER_LEN
        and _MCP_MATCHER_RE.fullmatch(matcher) is not None
    )


def _matcher_names_a_kas_tool(matcher: str) -> bool:
    """Whether ``matcher`` can match any name in the kiro-cli/KAS/goose/opencode tables."""
    # circular import: the ACP layer imports the config loader, which sits below
    # this module; resolved at call time like the other driver seams here.
    from kiro_crew.acp.harness_tool_names import HARNESS_TOOL_MATCH_VOCABULARY
    from kiro_crew.acp.kas_permissions import KAS_TOOL_MATCH_VOCABULARY
    from kiro_crew.hooks import _tool_matches

    vocabulary = KAS_TOOL_MATCH_VOCABULARY | HARNESS_TOOL_MATCH_VOCABULARY
    return (
        matcher == "*"
        # An MCP tool: the tables spell no MCP names, but the gate builds this form.
        or _MCP_MATCHER_RE.fullmatch(matcher) is not None
        or any(_tool_matches(matcher, name) for name in vocabulary)
    )


def spec_hook_tool_names(tool_id: str) -> tuple[str, ...] | None:
    """The names a spec hook's tool matcher meets for a call the harness named ``tool_id``.

    A goose or opencode id is qualified by its backend (``goose#shell``) and read
    through that backend's table
    (:func:`kiro_crew.acp.harness_tool_names.harness_tool_match_names`); any other id
    is KAS's own, read through
    :func:`kiro_crew.acp.kas_permissions.kas_tool_match_names`. Both are read through
    this module because application code reaches the ACP layer only via
    ``agent_sdk``. ``None`` when the harness named no tool, so the caller keeps
    matching on the call's title, as for a Hooks-page hook, rather than treating
    the hook as not applying.
    """
    if not tool_id:
        return None
    # circular import: the ACP layer imports the config loader, which sits below
    # this module; resolved at call time like the other driver seams here.
    from kiro_crew.acp.harness_tool_names import harness_tool_match_names
    from kiro_crew.acp.kas_permissions import kas_tool_match_names

    harness_names = harness_tool_match_names(tool_id)
    if harness_names is not None:
        return harness_names
    return kas_tool_match_names(tool_id)


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
    if (
        event == HOOK_EVENT_PRE_TOOL_USE
        and isinstance(matcher, str)
        and matcher
        and not _matcher_names_a_kas_tool(matcher)
    ):
        # Kept, not dropped: the matcher still meets a harness tool of that name (an
        # MCP tool, a built-in no table has a row for), and dropping it would retire
        # a guard the harness can serve. Said once, since a kiro-cli-only name
        # (``use_aws``) never meets a harness call.
        logger.warning(
            "agent %r: spec hook matcher %s on %s names no tool in Crew's tool tables; "
            "it runs only for a harness tool id it matches as written",
            agent_id,
            _diagnostic(matcher),
            _diagnostic(event),
        )
    if event == HOOK_EVENT_POST_TOOL_USE and isinstance(matcher, str) and matcher not in ("", "*"):
        # A finished call reaches the turn loop with no harness tool id, so a
        # PostToolUse is matched on the call's title, which a kiro-cli tool name
        # rarely matches. Said once here rather than left to miss silently.
        logger.warning(
            "agent %r: spec postToolUse matcher %s is compared with the call's "
            "title, since the harness names no tool on a finished call",
            agent_id,
            _diagnostic(matcher),
        )
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


def _agent_spec(agent_id: str, project_dir: str | None = None) -> dict[str, Any]:
    """*agent_id*'s spec, or ``{}`` when it has none on disk.

    *project_dir* is the session's checkout on a backend that resolves its spec
    project-nearest first (goose, opencode; see ``overlay_project_scope``). There a
    project spec of that name is the one the session runs, but its hooks are
    commands, so they are read only when the checkout is trusted to choose them
    (``session_mcp.trusted_project_agent_spec``, the verdict that also gates its
    ``mcpServers``). An untrusted one yields the user-level spec of that name, and
    a project spec that cannot be read raises. KAS reads the user level alone, so
    it passes none.

    A KAS mode switch can move a session to one of KAS's own built-in modes
    (``vibe``), which has no Crew spec: it carries no spec hooks, and the Hooks
    page's hooks still apply to it. Reading that as an unreadable spec would
    refuse every call the session makes and leave it unusable. A spec that exists
    but cannot be read still raises, so callers keep failing closed on it.
    """
    # circular import: the ACP layer imports the config loader, which sits below
    # this module; resolved at call time like the other driver seams here.
    from kiro_crew.acp.kas_agents import agent_spec_absent, load_agent_spec
    from kiro_crew.acp.session_mcp import trusted_project_agent_spec
    from kiro_crew.config.paths import kiro_agents_dir

    if project_dir:
        declared, spec = trusted_project_agent_spec(agent_id, project_dir)
        if declared:
            if spec is None:
                raise ValueError(f"the project spec for agent {agent_id!r} could not be read")
            return spec
    agents_dir = kiro_agents_dir()
    if agent_spec_absent(agents_dir, agent_id):
        return {}
    return load_agent_spec(agents_dir, agent_id)


def kas_spec_load_error(agents_dir: Path, agent_id: str) -> str | None:
    """Why KAS session creation would refuse *agent_id*'s spec, or ``None``.

    Runs KAS's two LOADING steps and nothing else: ``load_agent_spec``, then
    ``resolve_prompt`` for a non-blank prompt. Either raising
    ``KasAgentTranslationError`` refuses a KAS session, and its message is
    returned. It stops before ``to_client_custom_agent``, which consults the
    governance ceiling and writes SEL records. Any other exception propagates.
    """
    # circular import: kas_agents imports this module at load time.
    from kiro_crew.acp.kas_agents import (
        KasAgentTranslationError,
        load_agent_spec,
        resolve_prompt,
    )

    try:
        spec = load_agent_spec(agents_dir, agent_id)
        prompt = spec.get("prompt")
        # A blank or absent prompt takes resolve_prompt's constant fallback and
        # cannot fail; skipping it avoids that function's warning per probe.
        if not (prompt is None or (isinstance(prompt, str) and not prompt.strip())):
            resolve_prompt(spec, agent_id=agent_id, agents_dir=agents_dir)
    except KasAgentTranslationError as exc:
        return str(exc)
    return None


def spec_project_dir(provider: object) -> str | None:
    """The checkout *provider*'s session resolves its agent spec against, or ``None``.

    The session's own work dir on a backend that resolves project-nearest first,
    as its MCP projection does (``agent_sdk.backends.overlay_project_scope``);
    ``None`` on one that reads the user level alone (KAS).
    """
    from kiro_crew.agent_sdk.backends import overlay_project_scope

    cwd = getattr(provider, "cwd", "")
    if not isinstance(cwd, str) or not cwd:
        return None
    scope = overlay_project_scope(capabilities_of(provider).backend, cwd)
    work_dir = scope.get("work_dir")
    return work_dir if isinstance(work_dir, str) and work_dir else None


def crew_fired_spec_hooks(
    agent_id: str, project_dir: str | None = None
) -> tuple[list[ScriptHook], list[str], int]:
    """The active agent spec's hooks as script hooks, the keys nothing carries, and
    how many ``confirm: true`` hooks are skipped.

    Reads the spec the KAS projection reads, through the same reader
    (:func:`kiro_crew.acp.kas_agents.load_agent_spec`). The second and third values
    are for the session-start notice: the spec keys a KAS session runs without, and
    the hooks that wait for a confirmation Crew cannot ask for. Raises when the spec
    cannot be read; the caller fails PreToolUse closed on that. *project_dir* is
    :func:`spec_project_dir` of the session.
    """
    # circular import: the ACP layer imports the config loader, which sits below
    # this module; resolved at call time like the other driver seams here.
    from kiro_crew.acp.kas_agents import spec_keys_without_carrier

    spec = _agent_spec(agent_id, project_dir)
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
        hooks, _lost, _unconfirmable = await asyncio.to_thread(
            crew_fired_spec_hooks, agent_id, spec_project_dir(provider)
        )
    except Exception:  # noqa: BLE001 - the caller fails permission requests closed
        logger.warning(
            "agent spec hooks for %r could not be read; tool calls are blocked",
            agent_id,
            exc_info=True,
        )
        return TurnSpecHooks([], work_dir, True, True)
    return TurnSpecHooks(hooks, work_dir, False, True)


async def floored_spec_hooks(
    base_agent: str, cwd: str | None, base_spec: dict[str, Any] | None
) -> TurnSpecHooks:
    """The PreToolUse hooks a floored run is gated on: its BASE template's.

    A floored run launches as the derived ``<agent>--readonly`` spec, which drops
    ``hooks`` so the backend runs no command it was not asked about. A denying
    PreToolUse hook in the template must still refuse, before the remote hub's
    person is asked, so Crew fires the base spec's PreToolUse hooks itself on
    every permission request (``gated``). Only PreToolUse: the floor withholds
    the template's other lifecycle commands.

    *base_spec* is the spec the derivation read (``PublishedSpec.base_spec``),
    and the hooks come from it alone. Resolving *base_agent* again could land on
    another file: ``crew_fired_spec_hooks`` trusts a project spec only under the
    project-agent trust verdict and otherwise reads the user-level spec of the
    same name, while the derivation reads the project spec first. A missing or
    unconvertible source refuses every request, since a deny hook that was never
    loaded gave no verdict.
    """
    work_dir = cwd or None
    if not isinstance(base_spec, dict):
        logger.warning(
            "floored base %r has no derivation source; tool calls are blocked", base_agent
        )
        return TurnSpecHooks([], work_dir, True, True)
    try:
        hooks, _unconfirmable = await asyncio.to_thread(_convert, base_agent, base_spec)
    except Exception:  # noqa: BLE001 - the caller fails permission requests closed
        logger.warning(
            "agent spec hooks for floored base %r could not be read; tool calls are blocked",
            base_agent,
            exc_info=True,
        )
        return TurnSpecHooks([], work_dir, True, True)
    pre_tool = [hook for hook in hooks if hook.event == HOOK_EVENT_PRE_TOOL_USE]
    return TurnSpecHooks(pre_tool, work_dir, False, True)


async def hook_projection_stale(provider: object, agent_id: str) -> bool:
    """Whether *provider*'s live session auto-approves a capability a PreToolUse hook
    now covers.

    A KAS session keeps the agent batch it registered, and that batch withheld
    auto-approval only for what the hooks covered then. A hook added since (on the
    Hooks page or in the spec) would never see an auto-approved call, so the
    session has to be re-projected before its next turn. False on a backend that
    runs the spec's hooks itself, and when the session records no batch. An agent
    with no spec on disk (a KAS built-in mode) has no spec hooks, so only the Hooks
    page's hooks count; a spec that exists but cannot be read answers True:
    re-projecting is the safe direction.
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
        from kiro_crew.acp.kas_agents import pre_tool_hook_matchers
        from kiro_crew.acp.kas_permissions import hook_gated_capabilities

        # A built-in mode with no spec: only the Hooks page's hooks count.
        spec = _agent_spec(agent_id)
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


def running_agent(provider: Any, fallback: str) -> str:
    """The agent *provider*'s session runs now, else *fallback*.

    A KAS mode switch moves the session to another agent and the handle records
    it (``kas_projected_agent``), so a step retried on the same session is gated by
    that agent's hooks, not by the agent the step was started with. A session that
    records no agent (kiro-cli, or no batch) answers *fallback*.
    """
    recorded = getattr(provider, "kas_projected_agent", "")
    return recorded if isinstance(recorded, str) and recorded else fallback


async def refuse_stale_switch(provider: Any, agent_id: str) -> None:
    """End the turn when a mid-turn agent switch left the session stale.

    A KAS mode switch activates a batch KAS already holds, so what the new agent
    auto-approves was fixed when the session started, and a PreToolUse hook that
    covers it would never see its calls: they never reach the permission request.
    The session cannot be re-projected mid-turn, so the turn is cancelled and
    :class:`StaleProjectionError` raised; the next claim finds the session stale
    and resets it (:func:`invalidate_stale_kas_session`). A switch that leaves
    nothing a hook covers auto-approved lets the turn go on.
    """
    if not await hook_projection_stale(provider, agent_id):
        return
    logger.warning(
        "agent %r: a mid-turn switch runs a batch that auto-approves a capability "
        "a PreToolUse hook covers; ending the turn",
        agent_id,
    )
    try:
        await provider.cancel()
    except Exception:  # noqa: BLE001 - the refusal below ends the turn anyway
        logger.debug("cancel after a stale agent switch failed", exc_info=True)
    raise StaleProjectionError(
        f"the switch to agent {agent_id!r} runs permissions its PreToolUse hooks "
        "cannot see; the turn was stopped and the next one starts a fresh session"
    )


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
