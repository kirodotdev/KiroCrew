"""Agent resolution: crew identity and agent bindings, read from the published snapshots.

An owner of the :mod:`kiro_crew.config.loader` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.config import sections as _sections
from kiro_crew.config.loader_snapshots import (
    _materialized_kiro_agent,
    default_template,
    dispatch_kiro_agent,
)
from kiro_crew.config.sections import (
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    ResolvedBindings,
    _safe_float,
    normalize_agent_model,
    resolve_memory_store_config,
)
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE

if TYPE_CHECKING:
    from kiro_crew.config.loader_config import KiroCrewConfig

logger = logging.getLogger("kiro_crew.config.loader")


def _workspace_name_for_dir(config: KiroCrewConfig, ws_dir: Path) -> str:
    """Find the workspace name whose dir matches *ws_dir*."""
    for name, ws_cfg in config.workspaces.items():
        if Path(ws_cfg.dir) == ws_dir:
            return name
    return "default"


def _crew_autocompact_pct(crew: "_sections.SectionReader") -> float:
    """Read one crew record's ``autocompact_pct``: 0 (inherit) or a value in range.

    config.json is hand-editable, so junk and a negative value collapse to 0
    (inherit the global) rather than crash the load. Any positive value is
    held to the global knob's documented range, so a per-agent default can
    neither thrash compaction (below the minimum) nor never fire (above the
    maximum).
    """
    value = crew.read("autocompact_pct", _safe_float, lo=0.0, hi=AUTOCOMPACT_PCT_MAX)
    if 0.0 < value < AUTOCOMPACT_PCT_MIN:
        return AUTOCOMPACT_PCT_MIN
    return value


def crew_autocompact_pct(config: "KiroCrewConfig", crew_name: str) -> float | None:
    """The compaction threshold crew *crew_name* declares, or ``None`` to inherit.

    *crew_name* is a canonical ``config.agents`` key, the identity a session's
    allocation resolved (:func:`resolve_crew_identity`). A name that is not a
    crew, or a crew that leaves the field at 0, inherits the global
    ``session.autocompact_pct``. The value is the one :func:`_crew_autocompact_pct`
    validated at load, so this is a lookup and nothing more.
    """
    crew = config.agents.get(crew_name) if crew_name else None
    if crew is None or not crew.autocompact_pct:
        return None
    return crew.autocompact_pct


def resolve_crew_identity(
    config: "KiroCrewConfig", agent: str | None, crew_agent: str | None
) -> str:
    """Canonical Kiro Crew identity (a ``config.agents`` key) for a session.

    One rule shared by every session-granting path (provider factory, warm-pool
    claim) so cold starts and claims can never disagree. An explicit
    ``crew_agent`` wins verbatim — including "" ("no crew"), which is how the
    dashboard, the one kiro-name-passing surface, opts out of the fallback —
    except that a crew's ``member_id`` answers with the key its record is
    stored under (:func:`_crew_key_for_member_id`).
    When absent, the surface convention documented on
    :func:`_resolve_model_for_agent` applies: Slack threads, cron jobs and
    spawned agents pass a CREW name as ``agent``, so crew-namespace membership
    makes it canonical — a membership check on names the surface owns, not a
    cross-namespace match.
    """
    if crew_agent is not None:
        return _crew_key_for_member_id(config, crew_agent) or crew_agent
    if agent and agent in config.agents:
        # DEBUG, not INFO: every Slack/cron session resolves here routinely.
        # The line exists so a kiro-template name that collides with a crew
        # key (which would silently inherit that crew's watchdog windows) is
        # diagnosable from logs.
        logger.debug("crew_agent %r resolved by crew-namespace fallback", agent)
        return agent
    return ""


def _crew_key_for_member_id(config: "KiroCrewConfig", name: str | None) -> str:
    """The ``config.agents`` key of the crew whose ``member_id`` is *name*, else ``""``.

    For a name the caller has already stated is a CREW (an explicit
    ``crew_agent``, a ``member`` selection), never for a bare ``agent`` that
    may be a template: the crew namespace is stated, so no template name can
    be taken for a crew here. A record from before crews were keyed by id is
    keyed by its display name and is never re-keyed, so its id answers with
    the key it is stored under. The match is
    ``execution_context.member_config_for_id``'s unique-id rule; a stored key
    answers ``""`` (callers have already matched it) and the ``display_name``
    label is never matched.
    """
    if not name or name in config.agents:
        return ""
    from kiro_crew.execution_context import member_config_for_id

    try:
        key, _ = member_config_for_id(config, name)
    except ValueError:
        return ""
    return key


def _resolve_agent_selection(config, agent_name=None, project_dir=None, *, selection_kind=""):
    """Select a crewmate record or a template without accessing any memory files.

    Returns ``(record, alias, template, requested_resolved)``. A crewmate pick
    (``agent_name`` is a ``config.agents`` key and the stated namespace is not
    ``template``) returns its record and alias, with ``template`` empty. Every
    other selection is a TEMPLATE session: the materialized template the name
    declares (an app's, a project's, one in ``~/.kiro/agents``), else
    :func:`default_template`, with ``record`` None and ``alias`` empty. A plain
    session -- no name at all -- is therefore the default template, never the
    ``default`` crewmate. ``requested_resolved`` is False when a
    non-empty name matched neither, so callers never advertise a name that
    is not running.
    """
    alias_hit = selection_kind != "template" and bool(agent_name) and agent_name in config.agents
    if alias_hit:
        return config.agents[agent_name], agent_name, "", True
    id_key = _crew_key_for_member_id(config, agent_name) if selection_kind == "member" else ""
    if id_key:
        return config.agents[id_key], id_key, "", True
    fallback = default_template(config)
    passthrough = (
        "" if selection_kind == "member" else _materialized_kiro_agent(agent_name, project_dir)
    )
    # The default template is the floor every session can stand on, so naming
    # it resolves whether or not the materialized snapshot lists it (a cold
    # snapshot, a bare data home): an agent-less session resolved to it before
    # the name was ever stamped, and stamping must not make it refusable.
    requested_resolved = (
        (not agent_name)
        or bool(passthrough)
        or (selection_kind != "member" and agent_name == fallback)
    )
    # The default template goes through the same stem-to-declared-name mapping a
    # crewmate's ``kiro_agent`` does below: a hand-edited or legacy
    # ``agent.default_agent`` can spell the spec's FILE stem (``KiroPkg-captain``)
    # while the file declares ``captain``, and kiro-cli starts agents by declared
    # name only. A materialized passthrough is already a declared name.
    return None, "", passthrough or dispatch_kiro_agent(fallback), requested_resolved


def resolve_agent_identity(config, agent_name=None, *, selection_kind="") -> tuple[str, str, str]:
    """Alias, provider template and model pin for display/configuration only.

    This does not authorize memory access. Runtime callers must resolve the full
    bindings; a model chip remains inspectable while learned memory is unavailable.
    """
    record, alias, template, _ = _resolve_agent_selection(
        config, agent_name, selection_kind=selection_kind
    )
    return (
        alias,
        template or dispatch_kiro_agent(record.kiro_agent),
        normalize_agent_model(record.model) if record else "",
    )


def resolve_agent_bindings(
    config: KiroCrewConfig,
    agent_name: str | None = None,
    project_dir: str | None = None,
    *,
    validate_memory_files: bool = True,
    selection_kind: str = "",
    execution_context=None,
) -> ResolvedBindings:
    """Resolve workspace, memory store, and kiro agent for a session.

    Resolution (see :func:`_resolve_agent_selection`):
    1. If agent_name is given and exists in config.agents → that crewmate's
       bindings: its template, workspace, memory store and model pin.
    2. Otherwise a TEMPLATE session on the default workspace and the default
       memory store, with no crewmate model pin: the template *agent_name*
       itself when a materialized kiro agent declares it (see
       :func:`_materialized_kiro_agent` — an app's agents are registered in
       ``~/.kiro/agents/`` and never added to ``config.agents``, so this is
       what stops an app-bound session from silently running something else),
       else :func:`default_template`. A plain session with no name is this
       case: it is never the ``default`` crewmate.

    *project_dir* is the session's active project directory, which widens step 2 to
    that project's own ``.kiro`` scope. It must be the same directory Kiro Crew
    passes as the kiro-cli cwd, so an agent found through it is one the backend
    will genuinely resolve; passing a directory the session does not run in would
    reintroduce the silent-substitution bug this lookup exists to prevent.
    """
    import dataclasses as _dc

    from kiro_crew.mate_worker import member_chat_agent

    agent_cfg, resolved_alias, template, requested_resolved = _resolve_agent_selection(
        config, agent_name, project_dir, selection_kind=selection_kind
    )

    def _effective_memory(store_name: str) -> dict:
        store_cfg = config.memory_stores.get(store_name)
        store_dict = _dc.asdict(store_cfg) if store_cfg else {}
        return resolve_memory_store_config(_dc.asdict(config.memory), store_dict)

    if agent_cfg is None:
        # A template session: the template on the default folder and the
        # default memory store. Nothing of any crewmate -- not the ``default``
        # row's workspace, store or model pin -- reaches it.
        default_ws = config.workspaces.get(config.default_workspace)
        ws_dir = Path(default_ws.dir) if default_ws else Path("workspace")
        if execution_context is not None:
            store_name = execution_context.store.store_id
            kiro_agent = execution_context.template_id
            if execution_context.selection_kind == "member":
                kiro_agent = member_chat_agent(kiro_agent)
        else:
            store_name = DEFAULT_MEMORY_STORE
            kiro_agent = template
        return ResolvedBindings(
            execution_context=execution_context,
            workspace_dir=ws_dir,
            memory_store_name=store_name,
            effective_memory_config=_effective_memory(store_name),
            kiro_agent=kiro_agent,
            requested_resolved=requested_resolved,
            selection_kind="template",
        )

    # Resolve workspace
    ws_name = agent_cfg.workspace
    if ws_name in config.workspaces:
        ws_dir = Path(config.workspaces[ws_name].dir)
    else:
        # When the agent already names default_workspace there is nothing to fall
        # back to, and "'x' not found, falling back to 'x'" would only mislead.
        log = logger.debug if ws_name == config.default_workspace else logger.warning
        log(
            "Agent workspace '%s' not found, falling back to default_workspace '%s'",
            ws_name,
            config.default_workspace,
        )
        fallback_ws = config.workspaces.get(config.default_workspace)
        ws_dir = Path(fallback_ws.dir) if fallback_ws else Path("workspace")

    from kiro_crew.memory_stores import require_member_memory_store

    # Existing V1 members keep their exact configured store binding.
    # Canonical member/store mismatches are rejected before legacy use.
    if execution_context is not None:
        store_name = execution_context.store.store_id
        kiro_agent = execution_context.template_id
        if execution_context.selection_kind == "member":
            kiro_agent = member_chat_agent(kiro_agent)
    else:
        store_name = require_member_memory_store(
            config, resolved_alias, require_directory=validate_memory_files
        )
        kiro_agent = member_chat_agent(agent_cfg.kiro_agent)

    return ResolvedBindings(
        execution_context=execution_context,
        workspace_dir=ws_dir,
        memory_store_name=store_name,
        effective_memory_config=_effective_memory(store_name),
        kiro_agent=kiro_agent,
        model=normalize_agent_model(agent_cfg.model),
        requested_resolved=requested_resolved,
        resolved_alias=resolved_alias,
        selection_kind="member",
    )
