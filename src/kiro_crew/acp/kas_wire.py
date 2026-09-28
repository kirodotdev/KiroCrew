"""Localized parsing for the second ACP backend's KAS-specific wire surfaces.

Two surfaces live here, and they share one property: the shapes are the second
backend's, not ordinary ACP, so this module is the ONE place Kiro Crew reads or
builds them and an adjustment is a single-file edit.

1. ``session/update`` display/telemetry frames, whose discriminant blob arrives
   in a different shape than ``kiro-cli``'s. The handlers that act on a parsed
   frame live in :mod:`kiro_crew.acp.session_handle`.
2. The **hooks** surface: the backend can delegate hook extraction AND
   execution to its ACP client, asking ``_kiro/hooks/list`` for the hooks
   matching a trigger, ``_kiro/hooks/sessionStart`` for results computed before
   the turn, and ``_kiro/hooks/executeHook`` to run one listed hook's command.
   Kiro Crew answers all three from its own hook store, so the matcher stays on
   the side where the UI that authored the hook lives, and the command is spawned
   by Kiro Crew -- in its process, behind its own deny floor and governance gate
   -- rather than by the backend. The handshake does not announce the surface
   (see ``KAS_CLIENT_CAPABILITIES``), so today the backend asks none of them.

The literals below are only what Kiro Crew must match to route a frame or answer
a request; the backend's own internals are not documented here.

The ids this module mints are Kiro Crew's own, and the execute path runs only one
that a list answer produced for the Kiro Crew session that asks:
:class:`ListedHookStore` is that record, keyed by the OWNING Kiro Crew session
key and never by the host-supplied ``sessionId`` (see
``docs/system-specs/modules/agent-host-contract.md``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# The event vocabulary and the matcher are VALUES: bound directly, because nothing
# substitutes them. The store GETTER is reached through this module instead
# (``hooks_mod.get_global_hook_store()``) -- a local binding would freeze whichever
# store existed at import time, which for a gateway that builds its store during
# boot is none, and it could not be patched at its definition site either.
from kiro_crew import hooks as hooks_mod
from kiro_crew.config import KiroCrewConfig
from kiro_crew.hooks import (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_POST_TOOL_USE,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    TOOL_DENY,
    _tool_matches,
    hook_gate_kwargs,
)
from kiro_crew.sel import sel
from kiro_crew.validation import sanitize_string

logger = logging.getLogger(__name__)

# ── Envelope keys ──
# Where a frame's discriminant blob sits; both hops are guarded on read.
META_KEY = "_meta"
KIRO_KEY = "kiro"

# ── Frame discriminants Crew matches on ──
KIND_CONTEXT_USAGE = "context_usage"
KIND_TURN_COMPLETION = "turn_completion"
KIND_SUMMARIZATION_STARTED = "summarization_started"
KIND_SUMMARIZATION_COMPLETED = "summarization_completed"
KIND_SUMMARIZATION_FAILED = "summarization_failed"
KIND_STEERING_QUEUED = "steering_queued"
KIND_STEERING_INJECTED = "steering_injected"
KIND_STEERING_CLEARED = "steering_cleared"
KIND_AGENT_SUBTASK = "agent-subtask"  # hyphenated, not underscored

# Summarization maps to Crew's compaction status; steering to mid-turn steer.
SUMMARIZATION_KINDS = frozenset(
    {KIND_SUMMARIZATION_STARTED, KIND_SUMMARIZATION_COMPLETED, KIND_SUMMARIZATION_FAILED}
)
STEERING_KINDS = frozenset(
    {KIND_STEERING_QUEUED, KIND_STEERING_INJECTED, KIND_STEERING_CLEARED}
)

# ── Payload fields Crew reads ──
FIELD_KIND = "kind"
FIELD_USAGE_PERCENTAGE = "usagePercentage"
FIELD_CONVERSATION_SUMMARY = "conversationSummary"
FIELD_CONTENT = "content"
FIELD_PROMPT_TURN_SUMMARIES = "promptTurnSummaries"
FIELD_AGENT_SUBTASK_ID = "agentSubtaskId"
FIELD_PIPELINE = "pipeline"
FIELD_STAGES = "stages"
FIELD_UNIT = "unit"
FIELD_USAGE = "usage"
UNIT_CREDIT = "credit"


def kiro_meta(update: dict) -> dict[str, Any] | None:
    """Return the frame's discriminant blob, or ``None`` when absent.

    Both hops are ``isinstance``-guarded so a frame without it (a different
    backend, or a malformed one) falls through to the shared parser rather than
    raising. The single extraction step every handler shares — do not inline the
    two-step ``.get`` elsewhere.
    """
    meta = update.get(META_KEY)
    kiro = meta.get(KIRO_KEY) if isinstance(meta, dict) else None
    return kiro if isinstance(kiro, dict) else None


def turn_credits(kiro: dict) -> float | None:
    """Sum the per-turn credit cost from a ``turn_completion`` frame.

    Only ``credit``-unit entries contribute (the acp provider bills in credits).
    Returns the total to ASSIGN — the frame carries the whole turn's summary, so
    a replayed/duplicate frame reports the same total and must not accumulate —
    or ``None`` when there is no summaries list, so the caller leaves the prior
    value untouched rather than zeroing it.
    """
    summaries = kiro.get(FIELD_PROMPT_TURN_SUMMARIES)
    if not isinstance(summaries, list):
        return None
    # Deferred import: _token_count lives in the dispatch module, which imports
    # types; importing it at module scope would risk an import cycle once
    # session_handle imports this module.
    from kiro_crew.acp._dispatch import _token_count

    total = 0.0
    for entry in summaries:
        if not isinstance(entry, dict) or entry.get(FIELD_UNIT) != UNIT_CREDIT:
            continue
        value = _token_count(entry.get(FIELD_USAGE))
        if value is not None:
            total += float(value)
    return total


# ── The hooks surface ──
#
# Method names.
METHOD_HOOKS_LIST = "_kiro/hooks/list"
METHOD_HOOKS_SESSION_START = "_kiro/hooks/sessionStart"
METHOD_HOOKS_EXECUTE = "_kiro/hooks/executeHook"

#: JSON-RPC error code for an execute request Kiro Crew REFUSED. A refusal is
#: answered as an error, never as a result: a result carries an ``exitCode``, and a
#: command that never started has none to report. In the server-defined range,
#: one below the auth-callback code the KAS transport already answers with.
HOOK_EXECUTE_REFUSED_CODE = -32001

#: Cap on the context string an execute request hands the command. It reaches the
#: hook as an environment variable, which the OS bounds, and it is host-supplied.
#: The dashboard's own Test path caps its context at the same length.
HOOK_CONTEXT_MAX = 10000

# The trigger spellings THIS surface uses. They are not the agent-profile
# aliases: ``promptSubmit`` / ``agentStop`` / ``sessionStart`` stand where a
# profile writes ``userPromptSubmit`` / ``stop`` / ``agentSpawn``. Both sets
# normalize on the backend's side, but only these seven are what it asks for
# over this channel, so these seven are what Crew emits.
ACP_TRIGGER_PRE_TOOL_USE = "preToolUse"
ACP_TRIGGER_POST_TOOL_USE = "postToolUse"
ACP_TRIGGER_PROMPT_SUBMIT = "promptSubmit"
ACP_TRIGGER_AGENT_STOP = "agentStop"
ACP_TRIGGER_PRE_TASK_EXECUTION = "preTaskExecution"
ACP_TRIGGER_POST_TASK_EXECUTION = "postTaskExecution"
ACP_TRIGGER_SESSION_START = "sessionStart"

#: Every trigger this surface defines, in the order the covenant lists them.
ACP_HOOK_TRIGGERS: tuple[str, ...] = (
    ACP_TRIGGER_PRE_TOOL_USE,
    ACP_TRIGGER_POST_TOOL_USE,
    ACP_TRIGGER_PROMPT_SUBMIT,
    ACP_TRIGGER_AGENT_STOP,
    ACP_TRIGGER_PRE_TASK_EXECUTION,
    ACP_TRIGGER_POST_TASK_EXECUTION,
    ACP_TRIGGER_SESSION_START,
)

#: Crew event -> the trigger spelling this surface asks for.
#:
#: One key per member of ``HOOK_EVENTS``, and nothing else. The store's event
#: vocabulary is closed on both write paths -- ``validate_hook_fields`` refuses a
#: create outside it, and the loader skips an entry outside it -- so a key for any
#: other spelling could not be reached by a stored hook. A wider trigger
#: vocabulary belongs to the source that can carry it.
#:
#: Two of the seven request triggers therefore have no Crew event:
#: ``preTaskExecution`` and ``postTaskExecution`` are not in ``HOOK_EVENTS``, and
#: a request naming either is answered with an empty list rather than an error.
_CREW_EVENT_TO_ACP_TRIGGER: dict[str, str] = {
    HOOK_EVENT_AGENT_SPAWN: ACP_TRIGGER_SESSION_START,
    HOOK_EVENT_USER_PROMPT_SUBMIT: ACP_TRIGGER_PROMPT_SUBMIT,
    HOOK_EVENT_PRE_TOOL_USE: ACP_TRIGGER_PRE_TOOL_USE,
    HOOK_EVENT_POST_TOOL_USE: ACP_TRIGGER_POST_TOOL_USE,
    HOOK_EVENT_STOP: ACP_TRIGGER_AGENT_STOP,
}

#: The two triggers whose request carries a tool identity, so they are the two
#: where a hook's matcher is a TOOL matcher rather than a context one.
_TOOL_TRIGGERS = frozenset({ACP_TRIGGER_PRE_TOOL_USE, ACP_TRIGGER_POST_TOOL_USE})

#: Cap on one list response. The request is per trigger and per tool call, so an
#: unbounded answer would put the whole hook store on the wire inside a turn.
HOOKS_LIST_MAX = 200

#: Bounds on the two host-visible strings a hook carries. A hook past either is
#: WITHHELD rather than truncated: a truncated command is a different command, and
#: a hook whose own text is this far out of range is not one an execute path should
#: be handed. Generous against anything hand-authored -- the store's UI writes a
#: single shell command -- so reaching one means the file was written by something
#: other than a person.
HOOK_COMMAND_MAX = 4096
HOOK_NAME_MAX = 512

#: Bound on the store id, the third string that crosses the wire. The store mints
#: its own as eight hex characters, so anything near this bound is already
#: hand-written. Withheld like the other two: an id that cannot be trusted to be an
#: id is not one to put on the wire.
HOOK_ID_MAX = 128

#: Id prefix, so an id Crew minted is distinguishable from one the backend derived
#: from a file path in its own loader. An execute path decides from what this
#: surface LISTED rather than by parsing an id it was handed.
_ID_PREFIX = "crew"


@dataclass(frozen=True)
class NormalizedHook:
    """One hook, in the shape this surface's reader consumes.

    The reader deliberately does not know how a hook was WRITTEN. Crew's stored
    hooks and an agent spec's hooks are different shapes, and a spec's own shape
    is itself widening; every one of them reaches selection and projection as
    this dataclass, so a new source is one adapter and no change to the three
    steps below it.

    ``event`` keeps Crew's own spelling and :attr:`acp_trigger` resolves it, so
    a hook whose event maps to no trigger on this channel is visibly unserved
    rather than silently renamed.

    The matcher travels; the matcher MODE does not. A mode only matters for a
    matcher evaluated against message context, and this surface never has that
    context -- the request carries a tool identity or nothing at all. So a
    context matcher is not evaluated here, it is a reason to withhold the hook
    (see :func:`select_hooks`), and a field nothing reads would be a field a
    reader has to check.
    """

    id: str
    name: str
    event: str
    command: str = ""
    matcher: str = ""
    timeout: int | None = None
    enabled: bool = True
    #: The store's own id for the hook. Never on the wire; the execute path reads
    #: it back out of :class:`ListedHookStore` so it resolves what was LISTED
    #: rather than parsing an id the host handed it.
    source_id: str = ""

    @property
    def acp_trigger(self) -> str | None:
        """The trigger spelling this hook answers on, or ``None`` for neither."""
        return _CREW_EVENT_TO_ACP_TRIGGER.get(self.event)


#: The one hook source this surface serves: Crew's script-hook store.
_ID_SOURCE = "script"


def wire_hook_id(native_id: str) -> str:
    """The id Crew puts on the wire for one hook.

    The source segment is a literal rather than a parameter. One source exists, so
    a parameter would be a claim about a second one no caller can make.
    """
    return f"{_ID_PREFIX}:{_ID_SOURCE}:{native_id}"


def normalize_script_hook(hook: Any) -> NormalizedHook | None:
    """Adapt one stored script hook, or ``None`` when it cannot be served.

    ``None`` covers the cases that are not errors: a hook whose event has no trigger
    on this channel, a hook with no command, a hook past one of the wire bounds, and
    a hook whose MATCHER cannot be read. A commandless hook is an injection of skill
    text that the store's own fire path composes, so listing it here would promise an
    action this surface has no shape for.

    Which fields may degrade and which must withhold is decided by one question:
    could falling back WIDEN the hook? A matcher restricts, and an empty matcher
    means no restriction, so an unreadable one must withhold — the store persists
    this field uncoerced, so a hand-edited file can carry a list or an int here. A
    name restricts nothing, so it falls back to the id; a timeout restricts nothing
    either, and its absence means the executor's own default rather than no bound.
    """
    event = getattr(hook, "event", "")
    command = getattr(hook, "command", "")
    native_id = getattr(hook, "id", "")
    if not isinstance(event, str) or not isinstance(command, str) or not isinstance(native_id, str):
        return None
    if not native_id or not command.strip():
        return None
    if len(native_id) > HOOK_ID_MAX:
        logger.warning(
            "hook withheld: id is %d chars, past the %d bound",
            len(native_id),
            HOOK_ID_MAX,
        )
        return None
    if event not in _CREW_EVENT_TO_ACP_TRIGGER:
        return None
    if len(command) > HOOK_COMMAND_MAX:
        logger.warning(
            "hook %r withheld: command is %d chars, past the %d bound",
            native_id,
            len(command),
            HOOK_COMMAND_MAX,
        )
        return None
    timeout = getattr(hook, "timeout", None)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        timeout = None
    matcher = getattr(hook, "matcher", "")
    if not isinstance(matcher, str):
        logger.warning(
            "hook %r withheld: matcher is %s, not a string",
            native_id,
            type(matcher).__name__,
        )
        return None
    name = getattr(hook, "name", "")
    if isinstance(name, str) and len(name) > HOOK_NAME_MAX:
        logger.warning(
            "hook %r withheld: name is %d chars, past the %d bound",
            native_id,
            len(name),
            HOOK_NAME_MAX,
        )
        return None
    return NormalizedHook(
        id=wire_hook_id(native_id),
        source_id=native_id,
        name=name if isinstance(name, str) and name else native_id,
        event=event,
        command=command,
        matcher=matcher,
        timeout=timeout,
        # The literal boolean, not truthiness. The store persists this field
        # uncoerced -- ``ScriptHook.from_dict`` takes ``data.get("enabled", True)``
        # as written -- so a hand-edited ``"enabled": "false"`` arrives as that
        # string, and a truthiness test would read the operator's "off" as "on".
        # Anything that is not ``True`` is treated as off, which is the direction
        # that cannot list a hook its owner switched off.
        enabled=getattr(hook, "enabled", True) is True,
    )


def crew_hooks() -> list[NormalizedHook]:
    """Every Crew hook this surface can serve, normalized.

    The store is the one the dashboard's hook UI writes. When it has not been
    initialized there are no hooks to serve, which answers as an empty list
    rather than an error: an unanswered request would park the agent's turn.
    """
    store = hooks_mod.get_global_hook_store()
    if store is None:
        return []
    out: list[NormalizedHook] = []
    for hook in store.list_all():
        normalized = normalize_script_hook(hook)
        if normalized is not None:
            out.append(normalized)
    return out


def select_hooks(
    hooks: Iterable[NormalizedHook],
    *,
    trigger: str,
    tool_id: str = "",
    include_disabled: bool = False,
) -> list[NormalizedHook]:
    """The hooks that answer one request, in store order.

    Four filters, in the order a caller can reason about:

    * an unrecognized trigger selects nothing. A trigger Crew does not model is
      one it cannot claim a hook matches;
    * the hook's event must resolve to exactly that trigger;
    * a disabled hook is excluded unless the request asked for it. The default
      is the execution-facing one, so a disabled hook is never returned to a
      caller that would act on it;
    * a matcher this request cannot evaluate withholds the hook. On the two tool
      triggers the matcher is a TOOL matcher, so a named tool decides it through
      Crew's own matcher and a request that names none cannot: returning the hook
      there would let it run for a tool its author excluded. On every other
      trigger the matcher is a CONTEXT matcher and the request carries no
      context, so it cannot be decided at all. Withholding is the only direction
      that cannot run a hook against something its matcher rules out; a hook with
      no matcher is unrestricted and is always returned.
    """
    if trigger not in ACP_HOOK_TRIGGERS:
        return []
    out: list[NormalizedHook] = []
    truncated = 0
    for hook in hooks:
        if hook.acp_trigger != trigger:
            continue
        if not hook.enabled and not include_disabled:
            continue
        if hook.matcher:
            if trigger not in _TOOL_TRIGGERS or not tool_id:
                continue
            if not _tool_matches(hook.matcher, tool_id):
                continue
        if len(out) >= HOOKS_LIST_MAX:
            truncated += 1
            continue
        out.append(hook)
    if truncated:
        # Said out loud: a silent cut reads downstream as "the user has no more
        # hooks for this trigger", which is a different fact.
        logger.warning(
            "hooks list for trigger %r truncated at %d; %d further hook(s) withheld",
            trigger,
            HOOKS_LIST_MAX,
            truncated,
        )
    return out


def project_hook(hook: NormalizedHook) -> dict[str, Any]:
    """One hook in the wire shape: ``{id, name, action}``.

    ``approved`` is never sent. It is the field that tells the agent this
    command already carries an approval, and Crew has no approval to report for
    a hook it has not been asked to run.
    """
    action: dict[str, Any] = {"type": "runCommand", "command": hook.command}
    if hook.timeout is not None:
        action["timeout"] = hook.timeout
    return {"id": hook.id, "name": hook.name, "action": action}


def _params_object(params: Any) -> Mapping[str, Any]:
    """The request's params as a mapping, or an empty one.

    Guarded HERE and not only at the route, because the route is one caller and
    a params object that is absent, null or a scalar is a shape the host can send
    on any of them. An answer built from an empty mapping is the same answer as
    one built from an empty params object, which is what makes this safe to
    degrade rather than raise inside a dispatch loop.
    """
    return params if isinstance(params, Mapping) else {}


def _str_param(params: Mapping[str, Any], key: str) -> str:
    """One string field of a host-supplied params object, or ``""``."""
    value = params.get(key)
    return value if isinstance(value, str) else ""


def hooks_announced() -> bool:
    """Whether the KAS handshake announces the hooks capability.

    Read at call time from the constant the handshake sends, so the execute route
    opens and closes with the one value that decides whether the agent may ask.
    """
    from kiro_crew.acp.types import KAS_CLIENT_CAPABILITIES

    return "hooks" in KAS_CLIENT_CAPABILITIES.get("_meta", {}).get("kiro", {})


class ListedHookStore:
    """Which hook ids a list answer produced for ONE owning Kiro Crew session.

    The execute path's first gate: an id is run only if a list answer Kiro Crew
    built produced it for the SAME owning session. The key is the Kiro Crew
    session key the handle holds, never the request's ``sessionId`` -- that one is
    host-supplied, so keying by it would let one session's request name another
    session's set.

    One handle serves one owning session, so the record holds one key's ids. A
    record under a different key replaces them, which is what makes ids listed
    before a warm-pool claim unreachable after it.

    A hit is NECESSARY and never SUFFICIENT: the id is resolved back to the live
    store, whose current command and ``enabled`` state decide, and every later
    gate still runs. Bounded, oldest first; eviction only ever makes an id
    unlisted, which the execute path refuses, so a full record fails closed.
    """

    #: Ids kept. A list answer is capped at ``HOOKS_LIST_MAX`` and a session asks
    #: once per trigger, so this holds several full answers.
    MAX_IDS = 1024

    def __init__(self) -> None:
        self._session_key = ""
        self._ids: OrderedDict[str, str] = OrderedDict()

    def record(self, session_key: str, hooks: Iterable[NormalizedHook]) -> None:
        """Remember each ENABLED hook in ``hooks`` as listed for ``session_key``.

        A disabled hook is listed only for a caller that asked to see it, and a
        hook its owner switched off is not one to run; an empty key names no owner
        and records nothing.
        """
        if not session_key:
            return
        if session_key != self._session_key:
            self._session_key = session_key
            self._ids = OrderedDict()
        for hook in hooks:
            if not hook.enabled or not hook.source_id:
                continue
            self._ids.pop(hook.id, None)
            self._ids[hook.id] = hook.source_id
            while len(self._ids) > self.MAX_IDS:
                self._ids.popitem(last=False)

    def source_id(self, session_key: str, hook_id: str) -> str | None:
        """The store id ``hook_id`` was listed under for ``session_key``, or ``None``."""
        if not session_key or session_key != self._session_key:
            return None
        return self._ids.get(hook_id)


def hooks_list_response(
    params: Any,
    *,
    session_key: str = "",
    listed: ListedHookStore | None = None,
) -> dict[str, Any]:
    """Answer ``_kiro/hooks/list``.

    Every field of ``params`` is host-supplied, so each is read through an
    isinstance guard and a shape that is not the expected one degrades to
    absent. ``toolTags`` and ``workspacePaths`` are read and deliberately
    ignored: Crew's hooks carry no tag matcher, and they are not scoped to a
    workspace root, so narrowing by either would drop hooks their author expects
    to fire.

    Records the enabled hooks it answers in ``listed`` under ``session_key``, the
    OWNING Kiro Crew session, so the execute path can refuse an id this session
    was never handed. ``params.sessionId`` is never the key: it is host-supplied.

    Does NOT consult the ``capabilities.script_hooks`` governance gate, and that is
    a placement decision rather than an omission. The gate is a decision about
    RUNNING a hook, so it sits on the execute path (:func:`hooks_execute`), which
    asks it on the owning session's key before anything spawns.
    """
    params = _params_object(params)
    trigger = _str_param(params, "trigger")
    tool_id = _str_param(params, "toolId")
    include_disabled = params.get("includeDisabled") is True
    selected = select_hooks(
        crew_hooks(),
        trigger=trigger,
        tool_id=tool_id,
        include_disabled=include_disabled,
    )
    if listed is not None:
        listed.record(session_key, selected)
    return {"hooks": [project_hook(hook) for hook in selected]}


def hooks_session_start_response(params: Any) -> dict[str, Any]:
    """Answer ``_kiro/hooks/sessionStart`` with the results buffered for a turn.

    Empty for every trigger, which is the honest answer for this build rather
    than a stub: a precomputed result is the OUTPUT of a hook, every Crew hook
    this surface can serve carries a command as its payload, and no command runs
    from here. The request is still ANSWERED, because an unanswered one parks
    the agent's turn waiting for a response that never arrives, and the agent's
    own policy for this surface is to treat a failure as "no hooks" — which is
    indistinguishable from a refusal and is the reason a refusal must never be
    expressed that way.

    ``params`` is accepted and not read: there is no field whose value could
    make the answer non-empty.
    """
    return {"results": []}


class HookExecuteRefused(Exception):
    """An execute request Kiro Crew refused before any command started.

    Raised rather than returned so the one route that answers it has one place to
    turn it into a JSON-RPC error, and so a refusal cannot be mistaken for an empty
    result by a caller that forgot to check a flag.
    """


#: The SEL tool kind every execute outcome is recorded under, matching the
#: dashboard's Test path for the same hooks.
_SEL_TOOL_KIND = "script_hook"


def _audit_execute(
    session_key: str,
    agent: str,
    hook_label: str,
    outcome: str,
    *,
    error: str = "",
    metadata: dict[str, Any] | None = None,
    critical: bool = False,
) -> None:
    """Record one execute outcome in the SEL.

    ``critical`` is set for the one outcome that authorizes a spawn: the write is
    synchronous and a failure raises, so a command never runs unaudited. Every
    refusal is recorded best-effort, since the refusal already stops the spawn.
    """
    try:
        sel().log_tool_invocation(
            session_key=session_key,
            agent=agent or "kirocrew",
            source="kas_execute_hook",
            tool_name=hook_label,
            tool_kind=_SEL_TOOL_KIND,
            outcome=outcome,
            error=error,
            metadata=metadata,
            critical=critical,
        )
    except Exception:
        if critical:
            raise
        logger.debug("KAS executeHook audit (%s) failed", outcome, exc_info=True)


def audit_execute_refusal(session_key: str, agent: str, params: Any, reason: str) -> None:
    """Record a refusal made before :func:`hooks_execute` was reached."""
    hook_id = _str_param(_params_object(params), "hookId")
    _audit_execute(session_key, agent, _audit_label(hook_id), "refused", error=reason)


def _audit_label(hook_id: str) -> str:
    """The SEL tool name for a request, from a host-supplied id bounded before retention."""
    return f"kas_execute_hook:{sanitize_string(hook_id)[:HOOK_ID_MAX] or '?'}"


def _resolve_listed_hook(session_key: str, hook_id: str, listed: ListedHookStore) -> Any:
    """A SNAPSHOT of the stored hook behind a listed id, or raise :class:`HookExecuteRefused`.

    The id must have been listed for THIS owning session, the hook must still be
    in the store, and it must still normalize to the SAME wire id and be enabled:
    the store is the authority for what runs, and a hook switched off or rewritten
    past a bound after it was listed is not one this surface would list now.

    A copy, not the store's object: a dashboard edit updates that object in place,
    so the command the gates judge and the command that spawns must be the same
    value, read once.
    """
    source_id = listed.source_id(session_key, hook_id)
    if source_id is None:
        raise HookExecuteRefused("hook id was not listed for this session")
    store = hooks_mod.get_global_hook_store()
    live = store.get(source_id) if store is not None else None
    hook = dataclasses.replace(live) if live is not None else None
    normalized = normalize_script_hook(hook) if hook is not None else None
    if normalized is None or normalized.id != hook_id:
        raise HookExecuteRefused("hook not found")
    if not normalized.enabled:
        raise HookExecuteRefused("hook is disabled")
    return hook


def _gate_hook_command(command: str, *, session_key: str, agent: str) -> str | None:
    """The refusal reason Kiro Crew's own gates give ``command``, or ``None``.

    The two gates a shell tool call and a dashboard-run hook already pass, reused
    rather than restated:

    * ``HookManager.on_tool_call`` with the command as a shell tool's -- the deny
      floor, the IMDS and env-credential checks, the exfiltration audit, and the
      governance ceiling's tool scopes. As for any shell tool, the command text is
      not put through the path resolver: the spawn is sandboxed, which is the
      control that exemption leans on. Only ``deny`` refuses:
      ``allow`` means no tier matched and a human decides, and the human who
      decided is the one who authored this hook in Kiro Crew's own store;
    * ``capabilities.script_hooks``, the governance switch that stops every
      script hook, asked on the owning session's key and audited the way
      ``run_script_hook`` audits it.

    Synchronous: both read config and governance state, so the caller runs this
    off the event loop.
    """
    manager = hooks_mod.HookManager(
        hooks_mod.hooks_config_from_config_dict(KiroCrewConfig.load().hooks)
    )
    # The command IS the tool: it is the title a shell tool carries and the raw
    # command the gate's shell tiers read, so a rule written against either
    # spelling matches.
    decision = manager.on_tool_call(
        command,
        session_key=session_key,
        agent=agent,
        **hook_gate_kwargs(None, tool_kind="execute", command=command, is_shell=_SHELL_TOOL),
    )
    if decision.action == TOOL_DENY:
        return decision.reason or "denied by the Kiro Crew tool gate"
    gov_denied = hooks_mod._script_hooks_capability_denied(session_key)
    if gov_denied:
        hooks_mod._audit_governance_hook_decision(
            session_key, "kas_execute_hook", "denied", gov_denied
        )
        return f"Blocked by governance policy: {gov_denied}"
    return None


#: The gate judges the hook command as a shell tool's command.
_SHELL_TOOL = True

#: The two triggers whose ``userPrompt`` carries a tool call, as JSON.
_TOOL_EVENTS = frozenset({HOOK_EVENT_PRE_TOOL_USE, HOOK_EVENT_POST_TOOL_USE})


def _hook_event(hook: Any, context: str, session_key: str) -> dict[str, Any]:
    """The stdin payload, in the shape ``ScriptHookStore.fire`` gives the same event.

    On the two tool triggers the request's ``userPrompt`` is the tool call as
    JSON: the tool's arguments for PreToolUse, and ``{toolName, toolArgs,
    toolResult, toolSuccess}`` for PostToolUse. A tool hook reads ``tool_input``
    from stdin, so a request whose context is not that object is refused rather
    than run with the key missing.
    """
    event: dict[str, Any] = {
        "hook_event_name": hook.event,
        "cwd": os.getcwd(),
        "session_key": session_key,
    }
    if hook.event == HOOK_EVENT_USER_PROMPT_SUBMIT and context:
        event["prompt"] = context
    elif hook.event == HOOK_EVENT_STOP:
        event["assistant_text"] = context
    elif hook.event in _TOOL_EVENTS:
        try:
            call = json.loads(context) if context else None
        except ValueError:
            call = None
        if not isinstance(call, dict):
            raise HookExecuteRefused("tool hook request carries no tool call")
        if hook.event == HOOK_EVENT_PRE_TOOL_USE:
            event["tool_input"] = call
        else:
            tool_name = call.get("toolName")
            if isinstance(tool_name, str) and tool_name:
                event["tool_name"] = tool_name
            event["tool_input"] = call.get("toolArgs")
            event["tool_response"] = {
                "result": call.get("toolResult"),
                "success": call.get("toolSuccess"),
            }
    return event


def _record_run(snapshot: Any) -> None:
    """Copy a run's bookkeeping from the snapshot onto the live store entry.

    ``run_script_hook`` writes ``last_run`` / ``last_status`` / ``last_error`` /
    ``run_count`` on the object it ran, which is the snapshot; the Hooks UI reads
    the store's. Only those four fields move, so an edit made mid-run keeps its
    command. Best-effort, as ``fire()``'s own bookkeeping write is.
    """
    store = hooks_mod.get_global_hook_store()
    if store is None:
        return
    live = store.get(snapshot.id)
    if live is None:
        return
    live.last_run = snapshot.last_run
    live.last_status = snapshot.last_status
    live.last_error = snapshot.last_error
    live.run_count = live.run_count + 1
    store._persist_current()


def _hook_output(result: Any) -> str:
    """The text a hook run answers with, chosen by exit code.

    Exit 0 answers with stdout. Any other exit answers with stderr, then the
    runner's own error: an exit-2 hook's deny reason is on stderr, and it must
    not be hidden by a line the same hook printed to stdout.
    """
    if result.exit_code == 0:
        return result.stdout or ""
    return result.stderr or result.error or result.stdout or ""


async def hooks_execute(
    params: Any,
    *,
    session_key: str,
    agent: str,
    listed: ListedHookStore,
) -> dict[str, Any]:
    """Run one listed hook for ``_kiro/hooks/executeHook``; return the result.

    Raises :class:`HookExecuteRefused` for every refusal, before anything spawns.
    Every outcome, refusals included, is recorded in the SEL.

    What runs is the STORED hook's command under the STORED hook's timeout. Of the
    request's fields, ``hookId`` picks the listed hook and ``userPrompt`` becomes
    the hook's context -- sanitized and capped, as the dashboard's Test path does.
    The request's ``command`` and ``timeout`` are host-supplied and not read.

    Four gates, in order, and a request must clear all four:

    1. an owning Kiro Crew session is known -- governance resolves per surface, and
       an unowned session has no surface to resolve;
    2. the id was listed for that session and still resolves to an enabled hook;
    3. the command clears the tool gate (:func:`_gate_hook_command`);
    4. ``capabilities.script_hooks`` permits it on that session's key.

    The spawn itself is ``run_script_hook`` on the same snapshot the gates judged:
    the same sandbox, environment allowlist, output cap, redaction and timeout a
    dashboard-run hook gets, and it asks the governance switch once more
    immediately before the process starts.
    """
    params = _params_object(params)
    hook_id = _str_param(params, "hookId")
    label = _audit_label(hook_id)
    try:
        if not session_key:
            raise HookExecuteRefused("no owning Kiro Crew session for this ACP session")
        if not hook_id:
            raise HookExecuteRefused("hookId is missing")
        hook = _resolve_listed_hook(session_key, hook_id, listed)
        label = f"kas_execute_hook:{hook.name or hook.id}"
        refusal = await asyncio.to_thread(
            _gate_hook_command, hook.command, session_key=session_key, agent=agent
        )
        if refusal is not None:
            raise HookExecuteRefused(refusal)
        prompt = sanitize_string(_str_param(params, "userPrompt"))
        context = prompt[:HOOK_CONTEXT_MAX]
        # The stdin payload is built from the whole prompt, as ``fire()`` builds it
        # from the whole context: a tool call's JSON cut mid-token is not a tool call.
        hook_event = _hook_event(hook, prompt, session_key)
    except HookExecuteRefused as exc:
        await asyncio.to_thread(
            _audit_execute, session_key, agent, label, "refused", error=str(exc)
        )
        raise
    # Audit-or-deny: the record of an authorized spawn is written before it runs.
    await asyncio.to_thread(
        _audit_execute,
        session_key,
        agent,
        label,
        "approved",
        metadata={"hook_id": hook.id, "hook_event": hook.event},
        critical=True,
    )
    result = await hooks_mod.run_script_hook(hook, context, hook_event)
    await asyncio.to_thread(_record_run, hook)
    await asyncio.to_thread(
        _audit_execute,
        session_key,
        agent,
        label,
        "executed",
        metadata={
            "hook_id": hook.id,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
        },
    )
    response: dict[str, Any] = {"exitCode": result.exit_code, "cancelled": False}
    output = _hook_output(result)
    if output:
        response["output"] = output
    return response
