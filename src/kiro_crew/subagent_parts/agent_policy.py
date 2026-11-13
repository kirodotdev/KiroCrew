"""Which agent a spawn may name, and where it may run.

Reserved names and the refusal wire codes, the roster a refusal lists, app ownership of
an agent, the governance vetting of a spawn, the parent spec's
``toolsSettings.subagent.availableAgents`` allowlists, and working-directory validation.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import os

    from kiro_crew.subagent import (
        KIRO_CLI_BUILTIN_AGENTS,
        UNADVERTISED_AGENTS,
        Callable,
        Iterable,
        Path,
        _resolve_parent_spawn_allowlists,
        agent_matches_allowlist,
        cached_project_agent_names,
        is_internal_agent_spec,
        list_agents,
        logger,
        read_session_execution,
        readonly_base_name,
        redact_via_context,
        sandbox_plan,
        spawn_allowlist,
        visible_agent_names,
    )


#: Wire code for the unknown-agent refusal ``_validate_agent`` returns. It rides
#: ``SubagentInfo.error_code`` to ``POST /api/spawn``, which forwards the FIELD as
#: the response's ``code`` without naming the value -- so the gateway handler never
#: spells this identifier and stays agnostic about which code it is carrying. The
#: refusal's PROSE is advisory and free to be reworded (RFC 9457 3.1.3); this is
#: the contract ``spawn_run`` switches on, and here is the only place the literal
#: appears: ``mcp_tools.spawn`` imports it, under the same single-definition rule
#: as the reserved pair above, because a respelled literal is exactly the drift a
#: code exists to remove.
AGENT_NOT_FOUND_CODE = "agent_not_found"


#: Wire code for the refusal ``_vet_parent_available_agents`` returns: the target
#: agent exists, but the PARENT agent's spec declares
#: ``toolsSettings.subagent.availableAgents`` and the target matches none of its
#: globs. A second refusal kind, so it carries its own identifier rather than
#: inheriting ``agent_not_found`` (whose prose tells the caller the name does not
#: exist, which would send it looking for a typo instead of at its own spec).
#: Same single-definition rule: ``mcp_tools.spawn`` imports it for the wave
#: short-circuit, and the gateway handler forwards the field without naming it.
AGENT_NOT_AVAILABLE_CODE = "agent_not_available"


#: Wire code for the refusal ``_validate_agent`` returns when the named agent is
#: one of Kiro Crew's own generated specs (:func:`is_internal_agent_spec`): the
#: file exists, but it is machinery, not a sub-agent. A third refusal kind, so it
#: carries its own identifier: ``agent_not_found`` would send the caller looking
#: for a typo in a name it can see on disk. Same single-definition rule as the
#: two codes above.
AGENT_INTERNAL_CODE = "agent_internal"


def _internal_agent_refusal(requested: str, available: list[str]) -> str:
    """Refusal prose for a spawn that names a generated spec (see :func:`is_internal_agent_spec`).

    Names the base agent a read-only spec was derived from when that base is on
    offer -- it is the agent the caller most likely meant -- and otherwise leaves
    the roster to say what can be named. A base that is not offered (the host
    default, reached by omitting ``agent``) is not suggested by name.
    """
    base = readonly_base_name(requested)
    what = (
        f"the read-only spec Kiro Crew derives from {base!r} for side replies"
        if base
        else "a spec Kiro Crew generates for its own use"
    )
    suggestion = f"; name {base!r} to spawn that agent" if base in available else ""
    return (
        f"agent {requested!r} is {what}, not a sub-agent"
        f"{suggestion}{_available_agents_hint(available)}"
    )


#: Grammar an ``availableAgents`` glob must satisfy to be RENDERED into a refusal:
#: the agent-name alphabet plus the fnmatch metacharacters. Matching never
#: consults this; it only keeps instruction-shaped text out of a caller's context,
#: as ``_AGENT_NAME_RE`` does for plain names.
_AGENT_GLOB_RE = re.compile(r"^[A-Za-z0-9_*?!\[\]-]{1,64}$")


# How many valid names an unknown-agent refusal carries. The string reaches a WS
# frame, a tombstone and the caller's transcript, so it is bounded like every
# other rendered detail in this module; the remainder is reported as a count with
# a pointer to spawn_list, which lists them all.
_MAX_AVAILABLE_IN_ERROR = 12


def _available_agents_hint(available: list[str]) -> str:
    """Render the valid-name roster for an unknown-agent refusal.

    The names are computed anyway, to log the refusal. Withholding them from the
    RETURNED error leaves the caller unable to self-correct: it retries other
    invented names while every log line already holds the answer, and the
    log is not a surface the caller can read.

    Filtering, redaction and the bound are :func:`visible_agent_names`; the
    caller already sorted *available*, and that order is preserved.
    """
    shown, withheld = visible_agent_names(available, limit=_MAX_AVAILABLE_IN_ERROR)
    if not shown:
        # An empty roster is a different instruction than a truncated one: there
        # is no name to correct to, so the only valid move is to stop naming an
        # agent at all.
        return "; no other agents are installed - omit 'agent' to use the default"
    hint = "; available: " + ", ".join(shown)
    if withheld:
        hint += f" (+{withheld} more, call spawn_list)"
    return hint


def _validate_app_agent_ownership(agent: str, app: str) -> str:
    """The app-ownership proof the SpawnSDK runs at request time, repeated for
    a spawn that waited in the queue: *agent* must be one of *app*'s own
    materialized agents (``<app>--<agent>.json``). Returns the refusal reason,
    or ``""`` when the agent is the app's own."""
    prefix = f"{app}--"
    try:
        known = {
            a.name
            for a in list_agents()
            if a.filename.startswith(prefix) and not is_internal_agent_spec(a)
        }
    except Exception as exc:  # noqa: BLE001 - cannot confirm -> refuse
        return f"cannot verify agent {agent!r} for app {app!r}: {exc}"
    if agent not in known:
        return (
            f"app {app!r} may only spawn its OWN agents ({prefix}*); {agent!r} is not one "
            "(refusing to run the host default or another app's agent)"
        )
    return ""


#: ``(requested, cwd, app, owner_error, agent, error, code)``: the two agent
#: checks the gate makes -- app ownership (:func:`_validate_app_agent_ownership`)
#: and :func:`_validate_agent` -- taken off the loop by an event-loop caller and
#: handed to the gate, which uses them only when it would ask about the same
#: ``(requested, cwd, app)``. Both walk the agents directory, which must never
#: happen on the gateway loop.
AgentCheck = tuple[str, str, str, str, str, str, str]


def _validate_agent(requested: str, project_dir: str = "") -> tuple[str, str, str]:
    """Validate that an agent name is one kiro-cli can actually load.

    Runs ON the event loop when ``spawn`` is called synchronously, so it must not
    add filesystem work. The user-level ``list_agents()`` scan here is
    pre-existing; the event-loop callers run this function on a worker thread and
    hand the gate the answer (``AgentCheck``), and the app SpawnSDK skips it via
    ``_agent_prevalidated``. This deliberately does NOT widen the scan: the project scope is read from
    ``cached_project_agent_names()``, which performs no syscalls at all.

    Consequence, stated plainly: a project agent is accepted only once that
    project's cache is warm (any session that has already resolved bindings for it
    has warmed it). A cold cache means the name is reported unknown, which is
    fail-closed and matches this function's existing rule — refusing an unknown
    name rather than silently running the default agent, which would be a
    privilege escalation. Widening the on-loop scan to a second directory instead
    would stall the gateway on a slow or network checkout.

    *project_dir* must be the cwd the subagent will actually run in, because that
    is what kiro-cli resolves ``--agent`` against.

    Returns (agent_name, error, code). If the agent is found, error and code are
    both empty. If not, agent_name is empty, error explains what happened in prose
    and code is the machine-readable identifier for that decision. The code is
    returned rather than inferred by the caller so that a SECOND refusal kind
    added here has to choose its own identifier instead of silently inheriting
    this one.
    """
    if not requested:
        return "", "", ""
    agents = list_agents()
    # Kiro Crew's own generated specs are on disk but are not sub-agents (see
    # ``is_internal_agent_spec``): they are neither accepted nor offered. A
    # project agent that declares the same name is the user's own and still wins.
    internal = {a.name for a in agents if is_internal_agent_spec(a)}
    known = {a.name for a in agents} - internal
    if project_dir:
        known |= set(cached_project_agent_names(project_dir) or frozenset())
    if requested in known:
        return requested, "", ""
    available = sorted(known - UNADVERTISED_AGENTS)
    if requested in internal:
        logger.warning("Agent %r is a generated internal spec; refusing spawn", requested)
        return "", _internal_agent_refusal(requested, available), AGENT_INTERNAL_CODE
    # REFUSE a named-but-unknown agent rather than silently falling back to the
    # host default: that fallback runs the full default agent (frequently at
    # approval_mode="auto"), so a typo'd — or malicious — agent name was a silent
    # privilege escalation at the manager primitive. An EMPTY request still means
    # "use the default" (handled above); only a named agent that does not exist
    # is rejected, so a future caller cannot reintroduce the escalation.
    logger.warning("Agent %r not found; refusing spawn. Available: %s", requested, available)
    # The roster travels WITH the refusal, not only to the log: the caller acts on
    # the returned string, and a bare "not found" gives it nothing to correct to.
    return (
        "",
        f"agent {requested!r} not found{_available_agents_hint(available)}",
        AGENT_NOT_FOUND_CODE,
    )


def _inherited_member_agent(selection_kind: str, template_id: str, project_dir: str = "") -> str:
    if selection_kind != "member" or template_id in KIRO_CLI_BUILTIN_AGENTS:
        return ""
    if project_dir and cached_project_agent_names(project_dir) is None:
        return ""
    return template_id


def _spawning_agents(runs: Iterable[Any], parent_session_key: str) -> tuple[str, ...]:
    """The agent names whose profiles judge a spawn under *parent_session_key*.

    The spawning run is the live run the key names plus every live continuation
    of it, which has its own id and runs on the first run's ``conversation_key``.
    With no live one, the run the key names stands in. Each spawning run adds:

    1. its explicit ``agent``, when it was started with one;
    2. else the explicit ``agent`` of the run the key names, when the manager
       still holds that run, so a follow-up is judged as the agent it continues;
    3. else ``""``: the surface profile alone, as that run's own tool gate
       reads it. A template the run only inherits adds no name.

    A caller vets every name and refuses on the first refusal; ``""`` is vetted
    with no ``parent_agent``. Empty when the key names no sub-agent run this
    manager holds.
    """
    prefix = "subagent:"
    if not parent_session_key.startswith(prefix):
        return ()
    run_id = parent_session_key[len(prefix) :]
    records = list(runs)
    named = [a for a in records if a.id == run_id]
    live = [
        a
        for a in records
        if not a.done
        and (
            a.id == run_id
            or (getattr(a, "conversation_key", "") or f"{prefix}{a.id}") == parent_session_key
        )
    ]
    lineage_agent = named[0].agent if named else ""
    names: list[str] = []
    for a in live or named:
        names.append(a.agent or lineage_agent or "")
    return tuple(dict.fromkeys(names))


def _vet_spawn_governance(
    parent_session_key: str, agent: str, app: str = "", parent_agent: str = ""
) -> str | None:
    """Return a denial reason if governance forbids spawning, else None.

    ``parent_agent`` is the agent the SPAWNING sub-agent run was started as. A
    profile bound to that name (``{"type": "task", "id": parent_agent}``) is the
    one that run's tool calls are judged by, so it is the one judged here, ahead
    of the parent's surface profile. Empty, or a name with no profile, leaves the
    surface profile deciding.

    ``app`` binds the calling app's OWN profile (precedence #1 in
    ``resolve_active_scope``): an app spawning through the SpawnSDK must be
    contained by a profile written for that app, which is skipped entirely when
    the app identity is not threaded here — the Level-2 (PROFILE) half of the
    check would then never run and only the policy ceiling would apply.

    Two checks against the parent surface's ceiling ∩ profile:
    1. ``capabilities.spawn`` must be enabled.
    2. if enabled with an ``agents`` scope, the target *agent* must be permitted.

    Best-effort beyond the always-on guards: a ``PlatformCompositionError``
    propagates (fail-closed CPP); any other error returns a denial reason
    (fail-closed) rather than None/no-opinion.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        # Gate enabled?  (item ignored when no inner scope — checks ``enabled``.)
        gate = governance_permits(
            "capabilities.spawn", "", session_key=parent_session_key, agent=parent_agent, app=app
        )
        if not getattr(gate, "permitted", True):
            return getattr(gate, "reason", "spawn capability disabled")
        # Agent-scope check (capabilities.spawn.scopes.agents).
        if agent:
            scoped = governance_permits(
                "capabilities.spawn",
                f"agents:{agent}",
                session_key=parent_session_key,
                agent=parent_agent,
                app=app,
            )
            if not getattr(scoped, "permitted", True):
                return f"agent {agent!r} not permitted by spawn policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # Fail CLOSED: a governance evaluation error must DENY the spawn, not
        # silently permit it. PlatformCompositionError already propagates above;
        # every other error lands here and is audited before denial.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "subagent_spawn",
                session_key=parent_session_key,
                scope="capabilities.spawn",
                failed_closed=True,
            )
        except Exception:
            logger.debug("governance degrade audit unavailable", exc_info=True)
        return "subagent spawn denied: governance evaluation failed (fail-closed)"


#: Resolves a CALLING session key to every project directory its kiro-cli may
#: be running in: the live session's bound cwd AND the slot's staged project for
#: a chat (they differ while a project change waits for the next cold start), the
#: run's cwd for a sub-agent. Wired by the gateway (``DashboardState``); unset in
#: a process with no chat slots, where :func:`parent_project_dirs` answers ``()``.
_parent_project_resolver: Callable[[str], "Iterable[str | Path]"] | None = None


def set_parent_project_resolver(resolver: Callable[[str], "Iterable[str | Path]"] | None) -> None:
    """Install the gateway's session-key -> project-directories resolver."""
    global _parent_project_resolver
    _parent_project_resolver = resolver


def parent_project_dirs(parent_session_key: str) -> tuple[str, ...]:
    """The project directories the parent session may run in, ``()`` when unknown.

    Only the session's OWN bindings are used (its live runtime's cwd and its
    slot's project, or a sub-agent's cwd), never a guess such as "the one project
    every slot shares". Every directory returned is applied, so a binding that
    is stale can only add restrictions. A resolver that raises is "unknown",
    which keeps today's user-level-only resolution and is audited by
    :func:`_surface_unresolved_parent_spec` when it matters.
    """
    resolver = _parent_project_resolver
    if resolver is None or not parent_session_key:
        return ()
    try:
        values = resolver(parent_session_key)
        return tuple(dict.fromkeys(str(v) for v in (values or ()) if v))
    except Exception:  # noqa: BLE001 - a resolver failure is "unknown", never a crash
        logger.debug("parent project resolver failed for %s", parent_session_key, exc_info=True)
        return ()


class ParentRecordUnreadable(RuntimeError):
    """The calling session's execution record exists but could not be read.

    Raised by :func:`_parent_template_for_spawn` so :func:`parent_spawn_policy`
    can answer UNKNOWN (refuse) instead of "no parent" (allow all): the two are
    different facts, and a security gate must not collapse the first into the
    second.
    """


def _parent_template_for_spawn(parent_session_key: str) -> str:
    """The kiro agent template the calling session runs as, or ``""``.

    Read from the session's canonical execution record -- the same record
    ``resolve_spawn_execution`` derives the child's identity from on this path,
    served from the live in-process map for a running session. ``""`` means
    there is NO parent to honour: a caller with no session (a direct API post,
    a test) or a session that has no record. A record that exists but cannot be
    read raises :class:`ParentRecordUnreadable`, which the policy resolver turns
    into a refusal -- an unreadable parent is not a parent without a spec.
    """
    if not parent_session_key:
        return ""
    try:
        execution = read_session_execution(parent_session_key)
    except Exception as exc:  # noqa: BLE001 - every failure class is "unknown", never "none"
        logger.warning("parent execution record unreadable for %s: %s", parent_session_key, exc)
        raise ParentRecordUnreadable(str(exc)) from exc
    return execution.template_id if execution is not None else ""


#: What the spawn gate needs to know about the CALLING session's agent spec:
#: ``(parent_template, allowlists, unreadable)``. ``allowlists`` is ``()`` when
#: nothing is declared (allow all), one or more glob tuples when it is, and
#: ``None`` when the answer is unknown (refuse): the parent's spec candidate or
#: the parent's execution record could not be read -- see
#: :func:`parent_spawn_allowlists`. ``unreadable`` describes the spec files
#: behind a ``None`` answer (see :data:`_ParentSpawnResolution`), ``()``
#: otherwise; a two-item policy is read as having none.
ParentSpawnPolicy = tuple[str, "tuple[tuple[str, ...], ...] | None", "tuple[str, ...]"]


def _accepted_project_dirs(value: object) -> tuple[str, ...]:
    """The parent project directories a queued spawn row recorded at accept time.

    The row is read back from the durable store, so anything but a list of
    non-empty strings is ignored (``()``): a malformed value can only drop the
    recorded scope, and the drain then still applies the parent's live one.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(v for v in value if isinstance(v, str) and v)


def parent_spawn_policy(parent_session_key: str, accepted_dirs: object = ()) -> ParentSpawnPolicy:
    """Resolve the parent's template and its ``availableAgents`` declaration.

    Two reads that must not run on the gateway event loop -- the session
    record (a file when the session is not live) and the agents-directory
    snapshot (a ``scandir`` warm, a full parse cold). The event-loop entry
    points (``spawn_async``, the pump's ``_dispatch_async``) call this through
    ``asyncio.to_thread`` and hand the result to ``spawn_impl`` as
    ``_parent_spawn_policy``, the same shape ``_record`` / ``_execution_context``
    already use for the record read. The synchronous ``spawn()`` computes it
    inline for callers that are not on the loop.

    *accepted_dirs* are the project directories a queued row recorded when it
    was accepted; see :func:`parent_template_spawn_policy`.
    """
    try:
        template = _parent_template_for_spawn(parent_session_key)
    except ParentRecordUnreadable:
        return "", None, ()
    return parent_template_spawn_policy(parent_session_key, template, accepted_dirs)


def parent_template_allowlists(
    parent_session_key: str, template: str, accepted_dirs: object = ()
) -> tuple[tuple[str, ...], ...] | None:
    """The allowlists of :func:`parent_template_spawn_policy`'s answer."""
    return parent_template_spawn_policy(parent_session_key, template, accepted_dirs)[1]


def parent_template_spawn_policy(
    parent_session_key: str, template: str, accepted_dirs: object = ()
) -> ParentSpawnPolicy:
    """:func:`parent_spawn_allowlists` for *template*, in the parent session's project.

    Answers the whole :data:`ParentSpawnPolicy`, so the unreadable spec files
    behind an unknown answer reach the gate's refusal with it.

    The one spelling every gate entry point uses once it holds the parent's
    template, so each resolves the user scope AND the parent session's project
    scope (:func:`parent_project_dirs`) the same way. Blocking: call it off the
    event loop.

    A queued spawn passes the directories it was accepted under as
    *accepted_dirs*. They are applied together with the parent's live ones, so
    a parent that ended or moved project while the row waited still has the
    project-scope declaration it was admitted under re-read at the drain, and
    a directory in only one of the two sets can only add restrictions.
    """
    if not template:
        return template, (), ()
    dirs = tuple(
        dict.fromkeys(
            (*parent_project_dirs(parent_session_key), *_accepted_project_dirs(accepted_dirs))
        )
    )
    lists, unreadable = _resolve_parent_spawn_allowlists(
        template,
        dirs,
        parent_session_key=parent_session_key,
    )
    if lists is None:
        return template, None, unreadable
    worker = _bot_worker_allowlist(template, parent_session_key)
    if worker is None:
        return template, None, ()
    return template, lists + worker, unreadable


def _bot_worker_allowlist(
    template: str, parent_session_key: str
) -> tuple[tuple[str, ...], ...] | None:
    """The ``availableAgents`` list a Bot's worker session carries, as one more list.

    A Bot's worker runs its session's merged spec, which lives in the Bot's folder
    rather than ``~/.kiro/agents``, so the directory scan above never sees the
    list a template brought onto it (``mate_spec_policy``). ``()`` for any other
    template or a worker that declares nothing; ``None`` (refuse) when the merged
    spec cannot be read, since the list it may hold is then unknown.
    """
    from kiro_crew.mate_worker import MATE_WORKER_AGENT, mate_service_scope_spec

    if template != MATE_WORKER_AGENT:
        return ()
    try:
        scoped = mate_service_scope_spec(template, parent_session_key)
    except Exception:  # noqa: BLE001 - any failure leaves the list unknown
        logger.warning(
            "Bot worker %s: merged spec unreadable; refusing its spawns",
            parent_session_key,
            exc_info=True,
        )
        return None
    spec = scoped[0] if scoped is not None else None
    if spec is None:
        return None
    declared = spawn_allowlist(spec)
    return () if declared is None else (declared,)


def conductor_spawn_refusal(parent_template: str) -> str | None:
    """The refusal for a spawn whose parent runs on a crewmate's conductor, else ``None``.

    A crewmate's own chat runs on ``kirocrew-mate-conductor`` and hands its work
    to a worker session; it starts no sub-agents. Named positively by the
    parent's template, the one the spawn gate already resolves from the parent's
    execution record, so every entry point (``spawn_run``, ``spawn_sub_agents``,
    the durable pump's re-check) refuses it the same way. The conductor's spec
    also leaves the sub-agent tools out of its tool list
    (``mate_worker.MATE_CONDUCTOR_EXCLUDED_TOOLS``); this is the gate behind it.
    """
    from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT

    if parent_template != MATE_CONDUCTOR_AGENT:
        return None
    return (
        "a Bot's own chat starts no sub-agents: hand the work to a worker session "
        "with session_create, give it the brief with session_send, and read its answer "
        "with session_read_message"
    )


def _vet_parent_available_agents(
    policy: ParentSpawnPolicy, agent: str, *, app: str = ""
) -> str | None:
    """Return a denial reason when the parent's spec forbids spawning *agent*.

    *policy* is :func:`parent_spawn_policy`'s answer -- the kiro agent the
    CALLING session runs as and its declaration -- *agent* the template the
    child would run as (explicit, inherited or a member's), *app* the verified
    calling-app identity (see :func:`agent_matches_allowlist`).
    The denial is returned when the parent's spec declares
    ``toolsSettings.subagent.availableAgents`` AND *agent* matches none of its
    globs, or when that answer cannot be established (:func:`parent_spawn_allowlists`
    returns ``None``; a security gate does not fail open on a file it cannot read).
    That happens when no readable spec declares the parent's name while some spec
    file in the user or project agents directory could not be read, and the file
    need not be the parent's own; the refusal then names each such file, its
    scope and its error. No
    parent, no spec, or an omitted key is ``None`` -- kiro-cli's "omit to allow
    all" -- so a session whose agent never declared the key sees no change. An
    empty *agent* is ``None`` too: the gate resolves the effective template
    before asking, so there is nothing here to match.

    This is one half of an intersection with ``_vet_spawn_governance``
    (``capabilities.spawn.scopes.agents``): both must admit. It narrows what the
    agent spec grants and never widens what governance denies.
    """
    refused = conductor_spawn_refusal(policy[0])
    if refused:
        return refused
    if not agent:
        return None
    parent_template, allowlists = policy[0], policy[1]
    if allowlists is None:
        if not parent_template:
            return (
                "the parent session's execution record could not be read, so its agent "
                "spec's toolsSettings.subagent.availableAgents is unknown; refusing "
                "(fail-closed)"
            )
        unreadable = policy[2] if len(policy) > 2 else ()
        if unreadable:
            # Already bounded, with any ``(+N more)`` count, by _describe_unreadable_specs.
            named = "; ".join(redact_via_context(item) for item in unreadable)
            return (
                f"the parent agent {redact_via_context(parent_template)!r} spec could not be "
                "read, so its toolsSettings.subagent.availableAgents is unknown; refusing "
                f"(fail-closed) -- these agent spec files could not be read: {named}; "
                "fix or remove them"
            )
        return (
            f"the parent agent {redact_via_context(parent_template)!r} spec could not be "
            "read, so its toolsSettings.subagent.availableAgents is unknown; refusing "
            "(fail-closed) -- fix or remove the unreadable spec file"
        )
    if not allowlists:
        return None
    if all(agent_matches_allowlist(agent, allowlist, app=app) for allowlist in allowlists):
        return None
    # The globs travel with the refusal so the caller can self-correct, under the
    # same discipline as every rendered roster: grammar-checked (a glob adds
    # ``*?[]!`` to the agent-name alphabet, nothing else), redacted, bounded.
    patterns = sorted(
        {
            pattern
            for allowlist in allowlists
            for pattern in allowlist
            if _AGENT_GLOB_RE.fullmatch(pattern)
        }
    )
    shown = [redact_via_context(p) for p in patterns[:_MAX_AVAILABLE_IN_ERROR]]
    withheld = len(patterns) - len(shown)
    roster = ", ".join(shown) + (f" (+{withheld} more)" if withheld else "")
    # The two names are caller-supplied text: the refusal travels back to that
    # caller through ``info.error`` BEFORE ``_validate_agent`` has vetted the
    # target, and the warning lands in the log, so both surfaces get the same
    # redaction as the roster.
    shown_agent = redact_via_context(agent)
    shown_parent = redact_via_context(parent_template)
    logger.warning(
        "Agent %r is not in parent agent %r availableAgents; refusing spawn",
        shown_agent,
        shown_parent,
    )
    return (
        f"agent {shown_agent!r} is not in the parent agent {shown_parent!r} spec's "
        f"toolsSettings.subagent.availableAgents"
        + (f" (allowed: {roster})" if roster else " (the list is empty)")
    )


def validate_cwd(cwd: str, allowed_roots: list[str]) -> tuple[str, str]:
    """Validate a caller-supplied ``cwd`` for ``spawn_run``.

    Resolves symlinks and verifies the path is an existing directory under at
    least one entry in ``allowed_roots``. Empty ``allowed_roots`` disables the
    feature — any non-empty ``cwd`` is rejected.

    Args:
        cwd: Caller-supplied absolute path (may contain ``~``).
        allowed_roots: Permitted root paths from config (may contain ``~``).

    Returns:
        ``(resolved_cwd, error)``. On success ``error`` is empty and
        ``resolved_cwd`` is the canonical absolute path (realpath-resolved).
        On failure ``error`` is a reason string and ``resolved_cwd`` is empty.
    """
    if not cwd:
        return ("", "")
    if not allowed_roots:
        return ("", "cwd override is disabled (subagent_cwd_allowed_roots is empty)")
    try:
        expanded = os.path.expanduser(cwd)
        if not os.path.isabs(expanded):
            return ("", "cwd must be an absolute path")
        resolved = os.path.realpath(expanded)
    except (OSError, ValueError) as exc:
        return ("", f"cwd resolution failed: {exc}")
    if not os.path.isdir(resolved):
        return ("", "cwd does not exist or is not a directory")
    from kiro_crew.platform_compat import strip_extended_length_prefix

    resolved_roots = [os.path.realpath(os.path.expanduser(r)) for r in allowed_roots]
    # Windows' ``\\?\`` prefix is folded off both sides before the compare:
    # ``realpath`` can keep it on one operand and not the other, and the lone
    # prefix would read as an escape. Lexical, so no redirect is blessed.
    plain_resolved = strip_extended_length_prefix(resolved)
    for root in resolved_roots:
        # ``path_within`` strips a trailing separator before adding one, so a
        # filesystem-root entry (``/``, ``H:\``) admits the directories below it.
        if sandbox_plan.path_within(plain_resolved, strip_extended_length_prefix(root)):
            return (resolved, "")
    return ("", f"cwd is not under any allowed root: {allowed_roots}")
