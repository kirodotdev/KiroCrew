"""``list_agents``: the roster of installed agents, and two per-agent answers beside it.

The cached scan of both scopes, the rows built for a global and a project spec, the
edition seam that adds an edition's own agents, the name-to-model map (built by its own
scan of the agents directory), and the welcome message of the spec the roster resolves.

:mod:`kiro_crew.agent_discovery` stays the import path and the patch surface; see
:mod:`kiro_crew.agent_discovery_parts`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from kiro_crew import agent_state
from kiro_crew.agent_discovery_parts.project_agents import (
    _project_scope_denied,
    _project_signature,
    _scan_project_agent_files,
    project_agent_name,
)
from kiro_crew.agent_discovery_parts.spec_fields import (
    SCOPE_GLOBAL,
    SCOPE_PROJECT,
    AgentInfo,
    _extract_skills,
    spec_model,
    spec_str,
    spec_welcome_message,
)
from kiro_crew.agent_discovery_parts.spec_reads import (
    _kiro_agents_dir,
    _read_agent_spec,
    _warn_on_systematic_scan_failure,
)
from kiro_crew.agent_discovery_parts.spec_snapshot import (
    _LIST_AGENTS_CACHE,
    _dir_signature,
    _ListAgentsSig,
)
from kiro_crew.agent_files import (
    AGENT_FILENAME,
    GUEST_AGENT_FILENAME,
    LITE_AGENT_FILENAME,
    OWNED_KIRO_AGENT_FILES,
)
from kiro_crew.agent_spec_format import (
    is_app_agent_launch_name,
    is_markdown_spec,
    iter_agent_spec_files,
    shadowed_markdown_specs,
)
from kiro_crew.config.paths import project_agents_dir

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.agent_discovery")


def agent_model_map(
    agents_dir: Path | None = None,
    *,
    operation: str,
    source: str,
) -> dict[str, str]:
    """Build the full agent name/stem-to-model map for legacy history restore.

    Restore needs every entry, so this intentionally scans the complete scope.
    It keeps that surface on the hardened reader while preserving
    security-event attribution through the required *operation* and *source*
    arguments. A missing or non-string model stays ``""`` here so a restored
    legacy session continues to inherit its crew/global model; this deliberately
    differs from session's targeted runtime resolver, where no pin means
    ``"auto"``.

    A refused spec is skipped like an absent one.  If every candidate in a
    non-empty directory is refused, the scan-level warning used by discovery is
    emitted so a systematic gate failure is not mistaken for an empty install.
    """
    directory = agents_dir or _kiro_agents_dir()
    if not directory.is_dir():
        return {}
    try:
        files = iter_agent_spec_files(directory)
    except OSError:
        return {}

    candidates = 0
    parsed = 0
    result: dict[str, str] = {}
    for spec_file in files:
        if not spec_file.name.startswith("._"):
            candidates += 1
        data = _read_agent_spec(
            spec_file,
            operation=operation,
            source=source,
        )
        if data is None:
            continue
        model = spec_str(data, "model", "")
        declared_name = spec_str(data, "name")
        if declared_name:
            result[declared_name] = model
        result[spec_file.stem] = model
        parsed += 1
    _warn_on_systematic_scan_failure(directory, candidates, parsed)
    return result


def agent_welcome_message(
    agent: str,
    *,
    project: str | Path | None = None,
    agents_dir: Path | None = None,
) -> str:
    """*agent*'s ``welcomeMessage`` as display-ready text, or ``""``.

    The one reader of the field. Blocking (it scans agent directories), so an
    event-loop caller must offload it — the dashboard chat runner does.

    WHICH spec is live is answered by :func:`list_agents`, not re-decided here.
    That roster is what the agent picker shows and what the backend activates, so
    the hint has to come from the row it selected or the greeting describes an
    agent that is not running. Every rule that choice needs already lives there
    and nowhere else: project scope shadowing the user directory, a declared
    ``name`` outranking a matching filename, package-installed winning a
    duplicate name, and last-seen winning among duplicate project specs. Reading
    the winner instead of reproducing the rules is what keeps the two from
    drifting; a second copy of the precedence, however well tested, is a copy
    that can disagree.

    Only the winning file is then parsed, through the same hardened reader under
    this function's own *operation* label, so a denial is attributed to the hint
    rather than to a listing. That read applies the full guard set again (size
    cap, sidecars, sensitive symlink targets, non-object JSON), so reopening by
    name is not an unguarded second read.

    Best-effort and never raises: an unknown agent, an unreadable or oversized
    spec, or a roster row whose file is gone all yield ``""``. A missing hint and
    an unreadable one are deliberately the same answer — the field is decoration,
    and no chat turn should fail over it.
    """
    if not agent:
        return ""
    project_dir = str(project) if project else None
    try:
        rows = list_agents(agents_dir=agents_dir, project_dir=project_dir)
    except Exception:  # noqa: BLE001 - decoration must never fail a turn
        logger.debug("Agent roster unreadable for welcomeMessage %r", agent, exc_info=True)
        return ""
    winner = next((row for row in rows if row.name == agent), None)
    if winner is None or not winner.filename:
        return ""
    # The roster records a bare filename plus the scope it was found in, which is
    # what says which of the two directories to reopen it from.
    if winner.scope == SCOPE_PROJECT:
        if not project_dir:
            return ""
        directory = project_agents_dir(project_dir)
    elif agents_dir is not None:
        directory = agents_dir
    else:
        directory = _kiro_agents_dir()
    data = _read_agent_spec(
        directory / winner.filename,
        operation="agent_welcome_message",
        source="unknown",
    )
    if data is None:
        return ""
    return spec_welcome_message(data)


def _with_edition_agents(disk_agents: list[AgentInfo]) -> list[AgentInfo]:
    """Merge edition-contributed agent-catalog rows onto the on-disk scan.

    Reads ``AgentCatalogProvider.builtin_agents()`` through the platform context
    (deferred import so this module never imports the platform package at load
    time; fails closed to no extra agents). ADD-only and de-duped by name — an
    on-disk agent of the same name wins. The public Default returns ``[]`` so
    this is a no-op for the standalone edition.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    rows: list[dict[str, Any]] = safe_context_call(
        lambda: list(current_context().agent_catalog.builtin_agents()),
        fallback_factory=list,
        log_message="builtin_agents lookup failed; using none",
    )
    if not rows:
        return list(disk_agents)
    by_name: dict[str, AgentInfo] = {a.name: a for a in disk_agents}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        # A row keyed by a non-string name has no usable identity: the name IS
        # the dedup key, the React list key, and the argument every mutation
        # (agentDetail / agentPatch / setDefaultAgent) is addressed by. Blanking
        # it via __post_init__ would yield an unselectable row that also collides
        # with any other nameless row, so such a row is skipped outright — unlike
        # the cosmetic fields, which degrade.
        if not isinstance(name, str) or not name or name in by_name:
            continue
        try:
            by_name[name] = AgentInfo(
                name=name,
                filename=row.get("filename", ""),
                # Every other field is passed through as-is: this seam is
                # out-of-tree, so ``__post_init__`` — not this call site — is what
                # guarantees the declared ``str`` / ``list[str]`` types hold.
                # ``model`` still goes through spec_model() because its "defer"
                # spelling is domain knowledge, not a generic type fallback.
                description=row.get("description", ""),
                model=spec_model(row),
                skills=list(row.get("skills") or []),
                mcp_servers=list(row.get("mcp_servers") or []),
                source=row.get("source", "builtin"),
                package=row.get("package", ""),
                kirocrew_owned=row.get("kirocrew_owned", False),
            )
        except Exception:
            logger.debug("Skipping malformed edition agent row: %r", row)
    return list(by_name.values())


def _global_agent_info(f: Path, data: dict[str, Any]) -> AgentInfo:
    """Build an :class:`AgentInfo` for a user-level (``~/.kiro/agents``) config."""
    # Coerced BEFORE the package-detection below, which does
    # ``stem.endswith(agent_name)``: a non-string name raised TypeError there, and
    # the broad ``except`` around the caller's loop turned that into a silently
    # DROPPED agent rather than a degraded one. Falling back to the filename stem
    # keeps the row selectable under the name its file already implies.
    agent_name = spec_str(data, "name", f.stem)
    stem = f.stem

    package = ""
    # Package-installed agents follow the "{package}-{name}.json" filename
    # convention (a generic package-manager convention, not tied to any specific
    # tool). A plain "{name}.json" is built-in.
    is_package_filename = agent_name and stem.endswith(agent_name) and stem != agent_name
    if is_package_filename:
        pkg_stem = f.stem
        if pkg_stem.startswith("local-"):
            pkg_stem = pkg_stem[len("local-") :]
        package = pkg_stem[: -(len(agent_name) + 1)]

    if f.name in (AGENT_FILENAME, LITE_AGENT_FILENAME, GUEST_AGENT_FILENAME):
        source = "kirocrew"
    elif is_package_filename:
        source = "package"
    else:
        source = "builtin"

    return AgentInfo(
        name=agent_name,
        filename=f.name,
        description=spec_str(data, "description"),
        model=spec_model(data),
        skills=_extract_skills(data),
        mcp_servers=_mcp_server_names(data),
        source=source,
        package=package,
        scope=SCOPE_GLOBAL,
        kirocrew_owned=f.name in OWNED_KIRO_AGENT_FILES,
    )


def _project_agent_info(f: Path, data: dict[str, Any]) -> AgentInfo:
    """Build an :class:`AgentInfo` for a project-level (``<project>/.kiro``) config.

    The ``{package}-{name}`` filename convention is deliberately NOT applied here:
    a project checkout has no package manager installing into it, so a hyphenated
    filename is just a filename and reading a package out of it would invent one.
    """
    return AgentInfo(
        name=project_agent_name(f),
        filename=f.name,
        description=spec_str(data, "description"),
        model=spec_model(data),
        skills=_extract_skills(data),
        mcp_servers=_mcp_server_names(data),
        source="builtin",
        package="",
        scope=SCOPE_PROJECT,
        kirocrew_owned=False,
    )


def _mcp_server_names(data: dict[str, Any]) -> list[str]:
    """The ``mcpServers`` keys of a spec, or ``[]`` when the field is not a mapping."""
    mcp = data.get("mcpServers") or {}
    return list(mcp.keys()) if isinstance(mcp, dict) else []


def list_agents(
    agents_dir: Path | None = None,
    project_dir: str | Path | None = None,
) -> list[AgentInfo]:
    """Scan the agent directories for all installed agents.

    Returns a list of ``AgentInfo`` objects. Each agent corresponds to a kiro-cli
    agent config file that can be selected via ``session/set_mode`` in the ACP
    protocol.

    Two scopes are searched when *project_dir* is given: the user-level directory
    (``~/.kiro/agents``, or *agents_dir*) and the project's own
    (``<project>/.kiro/agents`` plus ``<project>/.kiro/*.agent-spec.json``). A
    project agent SHADOWS a user-level agent of the same name and the shadowing is
    logged, mirroring kiro-cli — which resolves ``--agent`` against its cwd first,
    and is spawned by Kiro Crew with the session's project dir as that cwd. The
    losing entry is not returned: it is unreachable for this session, so listing it
    would offer an agent that cannot run.

    Omitting *project_dir* preserves the user-level-only behavior, which is what
    callers with no session context (and therefore no project) want. A
    *project_dir* under a protected tree is refused whole and answered the same
    way: the value arrives from a caller-supplied session field, so nothing --
    not the spec scan, not the cache signature's stats -- reads under it.

    Results are cached per scope pair and reused while both directory signatures
    are unchanged, so repeated calls avoid re-reading and re-parsing every agent
    JSON on the event loop.
    """
    d = agents_dir or _kiro_agents_dir()
    # The project scope's sensitivity is decided BEFORE anything reads under it.
    # The signature stats below are a `scandir` plus a `stat` per entry, so
    # building them for a refused root would read the tree this refusal exists to
    # protect — and record a denial for it. A refused scope is dropped whole
    # rather than refused per read: it contributes no specs, no signature and no
    # cache key, leaving the user-level result it shares with a project-less call.
    scope: str | Path | None = project_dir or None
    if scope is not None and _project_scope_denied(
        scope, operation="list_agents", source="unknown"
    ):
        scope = None
    # One verdict on this scope, and BOTH the spec scan here and the signature
    # below are taken on it. Calling the public reader instead would decide a
    # second time on the same scope: the two answers can differ, and this frame
    # would then stat -- for the cache signature -- a tree that second verdict
    # had just recorded a denial for.
    project_files = _scan_project_agent_files(scope) if scope else []
    cache_key = (str(d), str(scope or ""))
    signature: tuple[_ListAgentsSig, ...] = (
        _dir_signature(d),
        # The same pair :func:`_project_signature` computes, through that helper
        # so the two entry points cannot diverge again.
        *(_project_signature(scope) if scope else ((), ())),
    )
    cached = _LIST_AGENTS_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return _with_edition_agents(list(cached[1]))

    agents: list[AgentInfo] = []

    if d.is_dir():
        for hidden_md in shadowed_markdown_specs(d):
            # The one user-facing surface every author reads, so this is where
            # the JSON-wins rule is announced rather than silently applied.
            logger.warning(
                "agent %r: %s is shadowed by its JSON twin %s.json and is not read; "
                "delete one of the two files",
                hidden_md.stem,
                hidden_md.name,
                hidden_md.stem,
            )
        user_candidates = 0
        user_parsed = 0
        for f in iter_agent_spec_files(d):
            # An app launch copy is the spec kiro-cli runs a palette-opened app
            # agent under, never an agent to offer: the app's own agent is
            # listed under its own name already (apps.agent_launch).
            if is_app_agent_launch_name(f.name):
                continue
            # AppleDouble sidecars are rejected by design, not by failure — a
            # directory holding only sidecars is empty of specs, not broken.
            if not f.name.startswith("._"):
                user_candidates += 1
            try:
                data = _read_agent_spec(f, operation="list_agents", source="unknown")
                if data is None:
                    continue
                agents.append(_global_agent_info(f, data))
                # Counted AFTER the append: a spec that parses but whose row
                # construction raises into the handler below still ends in
                # "discovery listed nothing", which is exactly what the
                # systematic-failure warning exists to surface.
                user_parsed += 1
            except Exception:
                logger.debug("Skipping invalid agent config: %s", f)
                continue
        _warn_on_systematic_scan_failure(d, user_candidates, user_parsed)
        # One sidecar read for the whole scan, not one per row. Global scope
        # only: forks are made from (and recorded against) user-level templates.
        # Lenient on purpose: a corrupt sidecar degrades the roster to "no fork
        # info" rather than failing the whole listing — the strict readers are
        # the mutators and the governance paths, where {} would be a hazard.
        try:
            forks = agent_state.all_fork_info()
        except (OSError, ValueError):
            logger.warning("fork sidecar unreadable; roster shows no fork info", exc_info=True)
            forks = {}
        if forks:
            for a in agents:
                fork_info = forks.get(a.name)
                if fork_info:
                    a.forked_from = fork_info["forked_from"]
                    a.private_to = fork_info["private_to"]

    # Deduplicate by name — prefer package-installed (has package) over fallback
    seen: dict[str, AgentInfo] = {}
    for a in agents:
        existing = seen.get(a.name)
        if existing is None:
            seen[a.name] = a
        elif a.package and not existing.package:
            seen[a.name] = a
        elif a.package and existing.package:
            if a.package == existing.package:
                # Package managers publish a locally-built package as BOTH
                # "{package}-{name}.json" AND "local-{package}-{name}.json";
                # stripping the "local-" prefix above makes the twin files
                # collide on the same (name, package). That is the EXPECTED
                # on-disk shape for every locally published package — warning
                # on it produced a self-contradictory "from packages 'X' and
                # 'X'" line per agent per scan (dozens per startup), drowning
                # real signals. Keep the first-seen file (unchanged first-wins
                # policy; sort order decides which twin that is) and note the
                # twin at debug.
                logger.debug(
                    "Agent '%s': keeping '%s' over same-package twin '%s'",
                    a.name,
                    existing.filename,
                    a.filename,
                )
            else:
                logger.warning(
                    "Duplicate agent name '%s' from packages '%s' and '%s'; keeping '%s'",
                    a.name,
                    existing.package,
                    a.package,
                    existing.package,
                )

    # Markdown-only is decided across BOTH scopes, by the rule
    # ``agent.markdown_spec_for_agent`` applies on the refusal branch: kiro-cli
    # sees JSON alone, so a markdown row it can still run (a JSON spec for the
    # name in the other scope, or a differently named JSON file declaring it)
    # is not markdown-only. Names come off rows already parsed, so no extra
    # read. A global row that survives has no project row of its name, so the
    # user-level JSON names decide it here; project rows are marked after their
    # scan, once that scope's JSON names are known too.
    json_names = {a.name for a in agents if not is_markdown_spec(a.filename)}
    for a in seen.values():
        a.markdown_only = is_markdown_spec(a.filename) and a.name not in json_names

    # Project scope LAST so it shadows: kiro-cli resolves --agent against its cwd
    # before the user-level dir, and Kiro Crew spawns it with the session's project
    # dir as cwd, so the project entry is what would actually run. The warning
    # mirrors kiro-cli's own conflict notice — shadowing is correct here, silent
    # shadowing is not, because the two configs can differ in tools and permissions.
    project_candidates = 0
    project_parsed = 0
    project_rows: list[AgentInfo] = []
    for pf in project_files:
        if not pf.name.startswith("._"):
            project_candidates += 1
        try:
            data = _read_agent_spec(pf, operation="list_agents", source="unknown")
            if data is None:
                continue
            info = _project_agent_info(pf, data)
            project_rows.append(info)
            shadowed = seen.get(info.name)
            if shadowed is not None and shadowed.scope == SCOPE_GLOBAL:
                logger.warning(
                    "Project agent '%s' (%s) shadows the user-level agent in %s",
                    info.name,
                    pf,
                    shadowed.filename,
                )
            seen[info.name] = info
            project_parsed += 1
        except Exception:
            logger.debug("Skipping invalid project agent config: %s", pf)
            continue
    if scope:  # type narrowing only; without it candidates is 0 anyway
        _warn_on_systematic_scan_failure(
            project_agents_dir(scope), project_candidates, project_parsed
        )
    json_names |= {p.name for p in project_rows if not is_markdown_spec(p.filename)}
    for p in project_rows:
        p.markdown_only = is_markdown_spec(p.filename) and p.name not in json_names

    result = list(seen.values())
    _LIST_AGENTS_CACHE[cache_key] = (signature, result)
    return _with_edition_agents(result)
