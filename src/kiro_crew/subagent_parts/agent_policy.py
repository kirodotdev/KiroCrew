"""Which agent a spawn may name, and where it may run.

Reserved names and the refusal wire codes, the roster a refusal lists, app ownership of
an agent, the governance vetting of a spawn, the parent spec's
``toolsSettings.subagent.availableAgents`` allowlists, and working-directory validation.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from kiro_crew.agent_discovery import AgentsDirMemo

if TYPE_CHECKING:
    import os

    from kiro_crew.subagent import (
        _AGENT_NAME_RE,
        KIRO_CLI_BUILTIN_AGENTS,
        Any,
        Callable,
        Container,
        Iterable,
        Mapping,
        Path,
        Sequence,
        _kiro_agents_dir,
        _read_agent_spec,
        cached_project_agent_names,
        fnmatch,
        is_internal_agent_spec,
        is_markdown_spec,
        is_registered_agent_name,
        iter_agent_spec_files,
        list_agents,
        logger,
        plain_markdown_document,
        project_agents_dir,
        read_session_execution,
        readonly_base_name,
        redact_via_context,
        sel,
    )


#: Agent names a roster never suggests: the host default and the conductors are
#: reached by OMITTING ``agent``, not by naming one. Every roster inherits this
#: as :func:`visible_agent_names`' default ``exclude``, so no other module names
#: the set and it cannot drift when a reserved name appears.
UNADVERTISED_AGENTS = frozenset(
    {
        "kirocrew",
        "kirocrew-conductor",
        "kirocrew-pipeline-conductor",
        "kirocrew-security-conductor",
    }
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


def visible_agent_names(
    names: Iterable[str],
    *,
    exclude: Container[str] = UNADVERTISED_AGENTS,
    limit: int | None = None,
) -> tuple[list[str], int]:
    """Make a roster of agent names safe to render, and bound it.

    Returns ``(shown, withheld)``: the names that may be rendered, and how many
    the *limit* dropped (``0`` when nothing was dropped, so a caller appends its
    "+N more" only when there is a remainder to report).

    Three surfaces render this roster -- an unknown-agent refusal, the spawn
    tools' parameter descriptions, and ``spawn_list``'s output -- and all three
    come through the pipeline below rather than re-implementing it. Duplicated
    copies drift, so the SAFETY half lives here where a fourth surface cannot
    omit it:

    * **Grammar.** Every name must match the registered-agent grammar
      (``is_registered_agent_name``: the slot grammar or a published dotted
      template) before it is rendered. This is the load-bearing filter, not a tidiness check: an agent
      spec's ``name`` field is taken verbatim by
      ``agent_discovery._global_agent_info`` with no validation, so a spec can
      declare a name containing a newline plus instruction-shaped text -- which
      is pure ASCII, so an ``isascii`` check passes it -- and it would ride this
      string into a model's context. ``SPAWN_RUN_SCHEMA`` gates the ``agent``
      parameter on the same grammar, so a name that fails it could never have
      been dispatched anyway: offering it would advertise an unusable name.
    * **Redaction.** A grammar-valid name can still be credential-shaped (an
      AWS access key is pure alphanumerics), so each name goes through the
      canonical context-aware shim rather than being trusted.
    * **Bound.** Every rendered roster that reaches always-on context is capped,
      and the remainder is returned as a count instead of being silently lost.

    Order is the CALLER's: this returns names in the order it received them, so
    each surface keeps the presentation its own copy promises. *exclude* defaults
    to the reserved pair (reached by omitting ``agent``, never by naming one);
    ``spawn_list`` passes an empty set on purpose, because the two bounded
    rosters point at it as the surface that lists everything.
    """
    kept = [
        redact_via_context(n)
        for n in names
        if n and n not in exclude and is_registered_agent_name(n)
    ]
    if limit is None or len(kept) <= limit:
        return kept, 0
    return kept[:limit], len(kept) - limit


# How many valid names an unknown-agent refusal carries. The string reaches a WS
# frame, a tombstone and the caller's transcript, so it is bounded like every
# other rendered detail in this module; the remainder is reported as a count with
# a pointer to spawn_list, which lists them all.
_MAX_AVAILABLE_IN_ERROR = 12


#: Bounds on a spec's ``toolsSettings.subagent.availableAgents`` list as
#: RETAINED by :func:`spawn_allowlist`: patterns beyond the count, and any
#: pattern longer than the character cap, are dropped (never matched, so the
#: overflow fails closed) and logged. The list is matched on the event loop on
#: every spawn, and the spec reader's only other ceiling is the file size cap.
#: 64 characters is the agent-name alphabet's own cap (``_AGENT_NAME_RE``,
#: ``_AGENT_GLOB_RE``); 256 patterns is far beyond any real roster.
_MAX_AVAILABLE_AGENTS_GLOBS = 256


_MAX_AVAILABLE_AGENTS_GLOB_CHARS = 64


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


def _vet_spawn_governance(parent_session_key: str, agent: str, app: str = "") -> str | None:
    """Return a denial reason if governance forbids spawning, else None.

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
        gate = governance_permits("capabilities.spawn", "", session_key=parent_session_key, app=app)
        if not getattr(gate, "permitted", True):
            return getattr(gate, "reason", "spawn capability disabled")
        # Agent-scope check (capabilities.spawn.scopes.agents).
        if agent:
            scoped = governance_permits(
                "capabilities.spawn",
                f"agents:{agent}",
                session_key=parent_session_key,
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


def spawn_allowlist(spec: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The ``toolsSettings.subagent.availableAgents`` globs *spec* declares.

    This is kiro-cli's own key, defined for its built-in ``subagent`` tool: "glob
    patterns for agents this agent can spawn; omit to allow all". Kiro Crew's
    sub-agents come through ``spawn_run`` instead, so the same declaration is
    honoured at the spawn gate rather than silently ignored.

    Returns ``None`` when the key is OMITTED -- that is "allow all", and it is
    the only value under which the gate stays out of the way, so every spec that
    never wrote the key keeps the behaviour it had. A declared list is returned
    as written (non-string entries dropped), bounded to the first
    :data:`_MAX_AVAILABLE_AGENTS_GLOBS` patterns of at most
    :data:`_MAX_AVAILABLE_AGENTS_GLOB_CHARS` characters each -- the overflow is
    dropped and logged, which only narrows what is allowed. A declared value
    that is not a list is an EMPTY allowlist: the operator meant to restrict and
    the shape is wrong, so nothing is allowed rather than everything.

    ``trustedAgents``, the sibling key, is NOT an allowlist and is not read
    here: upstream it means "run these sub-agents without permission prompts",
    and reading it as a restriction would refuse spawns an operator never meant
    to forbid.
    """
    settings = spec.get("toolsSettings")
    if not isinstance(settings, Mapping):
        return None
    subagent = settings.get("subagent")
    if not isinstance(subagent, Mapping) or "availableAgents" not in subagent:
        return None
    declared = subagent.get("availableAgents")
    if not isinstance(declared, (list, tuple)):
        return ()
    globs = tuple(entry for entry in declared if isinstance(entry, str) and entry)
    # Bound what is retained: the list is matched on the event loop on every
    # spawn (one ``fnmatchcase`` compile per pattern, then a regex pass and a
    # sort on the denial path), and the spec reader's only ceiling is the file
    # size cap, so a valid-size spec could carry hundreds of thousands of
    # patterns and stall the loop past the watchdog. Dropping entries only ever
    # NARROWS what is allowed, so the overflow fails closed: the first
    # ``_MAX_AVAILABLE_AGENTS_GLOBS`` patterns of at most
    # ``_MAX_AVAILABLE_AGENTS_GLOB_CHARS`` characters each are kept, the rest
    # are logged and refused.
    kept = tuple(
        g for g in globs[:_MAX_AVAILABLE_AGENTS_GLOBS] if len(g) <= _MAX_AVAILABLE_AGENTS_GLOB_CHARS
    )
    if len(kept) != len(globs):
        logger.warning(
            "availableAgents declares %d glob(s); keeping %d (limit %d patterns of at most %d "
            "chars each), the rest are not matched (fail-closed)",
            len(globs),
            len(kept),
            _MAX_AVAILABLE_AGENTS_GLOBS,
            _MAX_AVAILABLE_AGENTS_GLOB_CHARS,
        )
    return kept


def agent_matches_allowlist(agent: str, allowlist: Iterable[str], *, app: str = "") -> bool:
    """kiro-cli's glob semantics for ``availableAgents``: case-sensitive fnmatch.

    *app* is the VERIFIED identity of the calling app (``execution.app``, bound
    by the SpawnSDK and re-proved by ``_validate_app_agent_ownership``), never a
    prefix parsed out of *agent*. Only when *agent* is that app's own
    materialized ``<app>--<name>`` is the bare ``<name>`` tried as well, because
    that is the name the app's spec lists and kiro-cli's own gate matches. A
    ``--`` in any other name is just part of the name: an installed
    ``rogue--reviewer`` does not satisfy a ``reviewer``-only allowlist.
    """
    candidates = {agent}
    if app and agent.startswith(f"{app}--"):
        candidates.add(agent[len(app) + 2 :])
    return any(
        fnmatch.fnmatchcase(candidate, pattern) for pattern in allowlist for candidate in candidates
    )


def parent_spawn_allowlists(
    parent_template: str,
    project_dirs: Sequence[str | Path] = (),
    *,
    parent_session_key: str = "",
) -> tuple[tuple[str, ...], ...] | None:
    """Every ``availableAgents`` list the specs naming *parent_template* declare.

    *project_dirs* are the directories the PARENT session's kiro-cli may be
    running in (:func:`parent_project_dirs`). For each,
    ``<project_dir>/.kiro/agents`` is resolved too, by the same rules and the
    same hardened reader under its own SEL label, and its lists are ADDED to the
    user-level ones: the target must satisfy every list from every scope. kiro-cli lets a project spec shadow a
    user-level one of the same name; the gate does not pick the winner, it
    applies both, so honouring the project scope can only narrow what a parent
    may spawn, never widen it. ``None`` from either scope is ``None``.

    *parent_session_key* is used only to audit the case this function cannot
    resolve: no project directory is known for the parent and no user-level spec
    names it, so a project-scope declaration may exist that is not honoured
    (:func:`_surface_unresolved_parent_spec`).

    Resolution follows :func:`kiro_crew.agent.agent_spec_path`: a spec whose
    DECLARED ``name`` is *parent_template* wins; ``<parent_template>.json`` (or
    ``.md``) is read only when no spec declares the name. Two specs declaring
    the same name are both kept -- the target must then satisfy each, which is
    tightest-wins rather than a guess at which one kiro-cli loaded.

    ``()`` when *parent_template* is empty, names no spec, or its spec omits the
    key: each of those is "no declaration to honour", the unchanged case.

    ``None`` means the answer is UNKNOWN and the caller must refuse: the
    directory exists but cannot be scanned (probed before the snapshot read,
    which folds a walk failure into "no specs"), or no readable spec declares
    the name while some spec file in the directory could not be read under the
    hardened reader (invalid JSON, oversized, a broken link, a symlink to a
    sensitive target). kiro-cli resolves a name by its DECLARED ``name`` first,
    under any filename, so the parent's declaration may be inside exactly the
    file that did not parse; the reader folds it into "no spec", and a
    restrictive declaration inside it would otherwise fail OPEN. A parent whose
    declaration IS readable is never refused by an unrelated broken file. The
    cost is that a parent with no user-level spec at all (a project-scope or
    edition agent) is refused too while any spec file in the directory is
    unreadable; the refusal names the file, and repairing or removing it
    restores admission. AppleDouble ``._`` sidecars, which the reader rejects by
    design, do not count.

    Reads the user-level agents directory with one fresh walk through the
    hardened reader (:func:`_scan_parent_spawn_allowlists`), memoized per
    directory in :data:`_PARENT_ALLOWLIST_MEMO` and pinned to the directory's
    :func:`kiro_crew.agent_discovery.agents_dir_revision` -- the fingerprint
    that refuses to pin whenever freshness cannot be proven. The catalog
    snapshots are NOT used: ``parsed_agent_specs`` revalidates on entry names
    and mtime only, so an mtime-preserving rewrite of a parent's spec
    (``cp -p``, ``rsync -t``) would serve its previous, permissive allowlist for
    ever; ``cached_agent_specs`` serves EMPTY rows on a cold cache, and an empty
    read here would admit a spawn the declaration forbids. It is a blocking
    read, so the event-loop entry points call it through ``asyncio.to_thread``
    (see :func:`parent_spawn_policy`) rather than from the gate. A parent living
    only in a project's ``.kiro/agents`` is not resolved here (its declaration
    is resolved only when *project_dirs* is given -- see :func:`parent_project_dirs`.
    """
    # ``match``, not ``fullmatch``: the pattern is anchored, and the roster
    # ratchet reserves the ``fullmatch`` spelling for ``visible_agent_names``.
    if not parent_template or not _AGENT_NAME_RE.match(parent_template):
        return ()
    user_lists, user_found = _scope_spawn_allowlists(
        _kiro_agents_dir(), parent_template, _PARENT_ALLOWLIST_MEMO, "spawn_available_agents"
    )
    project_dirs = tuple(dict.fromkeys(str(d) for d in project_dirs if d))
    if not project_dirs:
        if user_lists is not None and not user_found and parent_session_key:
            _surface_unresolved_parent_spec(parent_template, parent_session_key)
        return user_lists
    if user_lists is None:
        return None
    lists = user_lists
    for directory in project_dirs:
        project_lists, _project_found = _scope_spawn_allowlists(
            project_agents_dir(directory),
            parent_template,
            _PARENT_PROJECT_ALLOWLIST_MEMO,
            "spawn_available_agents_project",
        )
        if project_lists is None:
            return None
        # Every scope applies (tightest-wins): an added list can only refuse more.
        lists = lists + project_lists
    return lists


#: ``(allowlists, found)`` for one agents directory: *allowlists* as
#: :func:`parent_spawn_allowlists` answers it for that directory alone, *found*
#: whether any readable spec there resolved the parent's name at all.
_ScopeAllowlists = tuple["tuple[tuple[str, ...], ...] | None", bool]


def _scope_spawn_allowlists(
    agents_dir: Path,
    parent_template: str,
    memo: "AgentsDirMemo[_ScopeAllowlists]",
    operation: str,
) -> _ScopeAllowlists:
    """Resolve *parent_template* in one agents directory (user or project scope)."""
    # Probe the walk first: a directory that is absent declares nothing
    # (nothing could have loaded the parent from it either), while one that
    # exists but cannot be scanned -- unreadable, or the path replaced by a
    # file -- is UNKNOWN, never "no declaration" (allow all).
    try:
        with os.scandir(agents_dir):
            pass
    except FileNotFoundError:
        return (), False
    except OSError as exc:
        logger.warning("agents directory %s is not scannable (%s); refusing spawn", agents_dir, exc)
        return None, False
    try:
        return memo.get(
            agents_dir,
            parent_template,
            lambda: _scan_parent_spawn_allowlists(agents_dir, parent_template, operation),
        )
    except OSError as exc:  # the directory walk itself failed: an unknown answer
        logger.warning("agents directory %s is not scannable (%s); refusing spawn", agents_dir, exc)
        return None, False


#: Parent templates already warned about by :func:`_surface_unresolved_parent_spec`
#: in this process, so the log line is said once per name rather than per spawn.
#: Bounded: past the cap nothing more is recorded and the warning keeps firing,
#: which is noisier, never quieter.
_UNRESOLVED_PARENT_WARNED: set[str] = set()


_UNRESOLVED_PARENT_WARNED_MAX = 256


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


def _surface_unresolved_parent_spec(parent_template: str, parent_session_key: str) -> None:
    """Say that a parent's ``availableAgents`` may be unhonoured, loudly.

    Reached when no project directory is known for the parent session and no
    user-level spec names its template: the parent may be a project-scope agent
    whose declaration this gate cannot see. The spawn keeps today's admission
    (there is nothing readable to refuse on), and the gap is logged at WARNING
    once per name and audited to the SEL on every occurrence.
    """
    shown = redact_via_context(parent_template)
    if parent_template not in _UNRESOLVED_PARENT_WARNED:
        if len(_UNRESOLVED_PARENT_WARNED) < _UNRESOLVED_PARENT_WARNED_MAX:
            _UNRESOLVED_PARENT_WARNED.add(parent_template)
        logger.warning(
            "parent agent %r has no user-level spec and the parent session's project "
            "directory is unknown, so a project-scope "
            "toolsSettings.subagent.availableAgents declaration (if any) is not honoured "
            "for this spawn",
            shown,
        )
    try:
        sel().log_tool_invocation(
            session_key=parent_session_key,
            source="subagent",
            tool_name="spawn_run",
            outcome="parent_spec_unresolved",
            metadata={"parent_agent": shown, "scope": "user", "project_dir": "unknown"},
        )
    except Exception:  # noqa: BLE001 - the audit is best-effort; the warning above stands
        logger.debug("SEL audit failed for unresolved parent spec", exc_info=True)


#: The gate's answers, one set per agents directory, each pinned to the
#: directory's :func:`kiro_crew.agent_discovery.agents_dir_revision`
#: (entry names, mtime AND ctime, size, inode, mode, link count, the in-process
#: spec generation, and a digest of the bytes of anything younger than the racy
#: window). The catalog snapshot
#: (``parsed_agent_specs``) revalidates on names and mtime alone, so a rewrite
#: that keeps the mtime -- ``cp -p``, ``rsync -t``, a restore -- would serve a
#: PERMISSIVE allowlist for ever after the operator tightened it; a security
#: gate cannot be answered from it. The revision refuses to pin (``None``)
#: whenever freshness cannot be proven, and the walk then runs uncached. Its
#: own instance, not shared with the KAS projection's or the tool-policy read's:
#: the three reads carry different SEL ``operation`` labels.
_PARENT_ALLOWLIST_MEMO: AgentsDirMemo[_ScopeAllowlists] = AgentsDirMemo()


#: The same, for a parent session's ``<project>/.kiro/agents``: its own
#: instance because its reads carry their own SEL ``operation`` label.
_PARENT_PROJECT_ALLOWLIST_MEMO: AgentsDirMemo[_ScopeAllowlists] = AgentsDirMemo()


def _scan_parent_spawn_allowlists(
    agents_dir: Path, parent_template: str, operation: str = "spawn_available_agents"
) -> _ScopeAllowlists:
    """One fresh walk of *agents_dir* for :func:`parent_spawn_allowlists`.

    Answers ``(allowlists, found)``; *found* is whether a readable spec resolved
    the parent's name (by declared ``name`` or filename stem).

    Every spec file goes through the hardened reader under the gate's own SEL
    labels (*operation* tells the user scope from the project scope), and a file the reader refuses is kept as UNREADABLE rather than
    folded into "no spec" -- except a markdown file with no opening frontmatter
    fence, which is not a spec at all (:func:`plain_markdown_document`) and is
    skipped like the AppleDouble sidecar. Propagates ``OSError`` from the
    directory walk.
    """
    rows: list[tuple[dict[str, Any], Path]] = []
    unreadable: list[Path] = []
    for path in iter_agent_spec_files(agents_dir):
        if path.name.startswith("._"):
            # AppleDouble sidecar: rejected by design, not by failure.
            continue
        # Literal labels at each call, so the reader's label ratchet attributes both.
        if operation == "spawn_available_agents_project":
            data = _read_agent_spec(
                path, operation="spawn_available_agents_project", source="subagent"
            )
        else:
            data = _read_agent_spec(path, operation="spawn_available_agents", source="subagent")
        if data is None:
            if is_markdown_spec(path) and plain_markdown_document(path):
                # No opening frontmatter fence: a README or a shared prompt
                # fragment, not a spec. It cannot carry a ``name`` kiro-cli
                # would resolve, so it can hide no declaration and the
                # fail-closed rule for an unreadable spec does not reach it.
                # A FENCED document that fails to parse announced itself as a
                # spec and stays UNREADABLE.
                continue
            unreadable.append(path)
        else:
            rows.append((data, path))
    declared = [data for data, _path in rows if data.get("name") == parent_template]
    if not declared:
        # No readable spec declares the name. kiro-cli resolves a name by the
        # DECLARED ``name`` first, so the declaration may sit in a spec file the
        # hardened reader refused -- invalid JSON, oversized, a broken link, a
        # symlink to a sensitive target -- under ANY filename, not only
        # ``<parent_template>.json``. Such a file is known to exist and cannot
        # be read, so the answer is UNKNOWN: a gate does not fail open on a
        # document it could not open. Only when every spec-shaped file parsed
        # is "no readable spec declares the name" the same fact as "no spec
        # declares the name", and the direct-filename fallback below applies.
        if unreadable:
            logger.warning(
                "parent agent %r has no readable spec while %s could not be read; "
                "refusing spawn (fail-closed)",
                parent_template,
                ", ".join(repr(path.name) for path in unreadable),
            )
            return None, False
        declared = [data for data, path in rows if path.stem == parent_template]
    lists = (spawn_allowlist(data) for data in declared)
    return tuple(allowlist for allowlist in lists if allowlist is not None), bool(declared)


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
#: ``(parent_template, allowlists)``. ``allowlists`` is ``()`` when nothing is
#: declared (allow all), one or more glob tuples when it is, and ``None`` when
#: the answer is unknown (refuse): the parent's spec candidate or the parent's
#: execution record could not be read -- see :func:`parent_spawn_allowlists`.
ParentSpawnPolicy = tuple[str, "tuple[tuple[str, ...], ...] | None"]


def parent_spawn_policy(parent_session_key: str) -> ParentSpawnPolicy:
    """Resolve the parent's template and its ``availableAgents`` declaration.

    Two reads that must not run on the gateway event loop -- the session
    record (a file when the session is not live) and the agents-directory
    snapshot (a ``scandir`` warm, a full parse cold). The event-loop entry
    points (``spawn_async``, the pump's ``_dispatch_async``) call this through
    ``asyncio.to_thread`` and hand the result to ``spawn_impl`` as
    ``_parent_spawn_policy``, the same shape ``_record`` / ``_execution_context``
    already use for the record read. The synchronous ``spawn()`` computes it
    inline for callers that are not on the loop.
    """
    try:
        template = _parent_template_for_spawn(parent_session_key)
    except ParentRecordUnreadable:
        return "", None
    return template, parent_template_allowlists(parent_session_key, template)


def parent_template_allowlists(
    parent_session_key: str, template: str
) -> tuple[tuple[str, ...], ...] | None:
    """:func:`parent_spawn_allowlists` for *template*, in the parent session's project.

    The one spelling every gate entry point uses once it holds the parent's
    template, so each resolves the user scope AND the parent session's project
    scope (:func:`parent_project_dirs`) the same way. Blocking: call it off the
    event loop.
    """
    if not template:
        return ()
    return parent_spawn_allowlists(
        template,
        parent_project_dirs(parent_session_key),
        parent_session_key=parent_session_key,
    )


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
        "a crewmate's own chat starts no sub-agents: hand the work to a worker session "
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
    globs, or when that answer cannot be established because the parent's own
    spec file is present but unreadable (:func:`parent_spawn_allowlists` returns
    ``None``; a security gate does not fail open on a file it cannot read). No
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
    parent_template, allowlists = policy
    if allowlists is None:
        if not parent_template:
            return (
                "the parent session's execution record could not be read, so its agent "
                "spec's toolsSettings.subagent.availableAgents is unknown; refusing "
                "(fail-closed)"
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
    resolved_roots = [os.path.realpath(os.path.expanduser(r)) for r in allowed_roots]
    for root in resolved_roots:
        if resolved == root or resolved.startswith(root + os.sep):
            return (resolved, "")
    return ("", f"cwd is not under any allowed root: {allowed_roots}")
