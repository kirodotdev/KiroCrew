"""Preparing a spawn's native skill view: :func:`prepare_native_skill_projection`.

It publishes, under this home's publication lock and lease, the views a spawn launches
with, and falls back to the authored agent once the projected-alias count reaches
``SKILL_VIEW_PROJECTION_CEILING``, warning once per process.

:mod:`kiro_crew.acp.skill_projection` stays the import path and the patch surface; see
:mod:`kiro_crew.acp.skill_projection_parts`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kiro_crew.acp import session_mcp
from kiro_crew.acp.skill_projection_parts.alias_reclaim import (
    _end_projection,
    _ensure_projection_reaper,
    _prune_stale_managed_aliases,
)
from kiro_crew.acp.skill_projection_parts.inheritance_overlay import (
    _INHERIT_SETTING,
    _INHERIT_SOURCE,
    _MANAGED_SETTING,
    _PREVIOUS_INHERITANCE,
    _inheritance_preference,
    _restore_inheritance,
    _settings,
)
from kiro_crew.acp.skill_projection_parts.native_projection import (
    NativeSkillProjection,
    _register_active_projection,
)
from kiro_crew.acp.skill_projection_parts.side_files import (
    _acquire_projection_lease,
    _ensure_projection_metadata_directory,
    _per_spawn_prune_due,
    _projection_alias_lock,
    _projection_side_dir,
)
from kiro_crew.acp.skill_projection_parts.view_ownership import (
    _MANAGED_AGENT,
    _MANAGED_ALIAS_SHA256,
    _MANAGED_CREW_HOME,
    _MANAGED_MARKER,
    _MANAGED_MARKER_VALUE,
    _MANAGED_SOURCE,
    _MANAGED_VIEW_SHA256,
    _alias_identity,
    _is_current_publication,
    _managed_view_description,
    _view_digest,
)
from kiro_crew.acp.skill_projection_parts.view_publication import (
    _SEARCH_TOOL,
    _announce_publication,
    _display_text_bytes,
    _strip_alias_display_text,
    _warn_on_display_text,
)
from kiro_crew.acp.skill_projection_parts.view_sources import (
    _record_view_ledger,
    _remember_view_sources,
)
from kiro_crew.agent_discovery import (
    SCOPE_PROJECT,
    _read_agent_spec,
    can_inherit_skills,
    inherited_skill_globs,
    list_agents,
    skill_search_refusal,
)
from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX, SKILL_VIEW_PROJECTION_CEILING
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home, kiro_agents_dir, kiro_home, project_agents_dir
from kiro_crew.user_json import copy_json_document
from kiro_crew.workspace_cli_settings import workspace_cli_settings_lock

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.acp.skill_projection")


# The alias count at or above which preparation stops minting new views and
# falls back to the authored agent, the same path ``KIROCREW_NATIVE_SKILL_PROJECTION=0``
# takes. This is the ONE constant the doctor's backlog warning
# (``doctor_checks.resources._SKILL_VIEW_BACKLOG_WARN``) reads, so the ceiling
# the projection enforces and the count the doctor flags are the same number: a
# host the doctor warns about is a host whose next spawn already fell back. It is
# a safety net for a regression that reopens the leak the root-cause fixes closed
# (a per-launch env value that mints a view per spawn, or one unreadable lease
# that keeps the reclaim from draining anything), NOT the mechanism that keeps a
# healthy host bounded -- the boot drain and per-spawn reclaim do that, far below
# this number. Deliberately well above a healthy host's authored-agents x
# workspaces so normal use never trips it.
#
# Defined in the ``agent_spec_format`` leaf (beside the alias prefix) and
# imported at the top of this module, so the doctor can share the number without
# importing the ACP layer: the agent-SDK import boundary is shrink-only and the
# doctor is a baselined consumer. The enforcement below reads it unqualified.

# One warning per process when the ceiling is in effect, not one per spawn: the
# fallback fires on every subsequent start until the backlog drains, and a line
# per spawn would bury the one that matters. The flag is set the first time the
# ceiling is tripped in this process and never reset.
_CEILING_FALLBACK_WARNED = False
_CEILING_FALLBACK_WARNED_LOCK = threading.Lock()


def _count_projected_aliases(directory: Path) -> int:
    """Count ``kirocrew-skill-view-*.json`` files in *directory*, up to the ceiling.

    One bounded ``os.scandir``. Counts regular files by name only -- no read, no
    lease probe, no sidecar -- because the ceiling asks only "how many views are
    on disk", which is what kiro-cli pays for at startup. The scan STOPS as soon
    as the count reaches :data:`SKILL_VIEW_PROJECTION_CEILING`: the only caller
    tests ``>= ceiling``, so an exact count past it buys nothing and every extra
    entry is wasted work on the spawn path. No result cache sits in front of it:
    on a healthy host it lists a near-empty directory in microseconds, and the
    ceiling only engages when the directory is already backlogged, where the scan
    is dwarfed by kiro-cli reading every one of those files at startup. A scan
    that cannot read the directory counts zero: an unreadable agents directory is
    not a backlog, and the publication writes below would raise on it anyway.
    """
    count = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if count >= SKILL_VIEW_PROJECTION_CEILING:
                    break
                if (
                    entry.name.startswith(NATIVE_SKILL_ALIAS_PREFIX)
                    and entry.name.endswith(".json")
                    and entry.is_file(follow_symlinks=False)
                ):
                    count += 1
    except OSError:
        logger.debug(
            "skill projection: cannot scan %s to count views for the ceiling",
            directory,
            exc_info=True,
        )
        count = 0
    return count


def _warn_ceiling_fallback_once(directory: Path, count: int) -> None:
    """Warn, at most once per process, that the view ceiling is now in effect."""
    global _CEILING_FALLBACK_WARNED
    with _CEILING_FALLBACK_WARNED_LOCK:
        if _CEILING_FALLBACK_WARNED:
            return
        _CEILING_FALLBACK_WARNED = True
    logger.warning(
        "skill projection: %d (cap %d reached) kirocrew-skill-view-*.json file(s) in %s; "
        "new spawns no longer create skill views and fall back to authored agents until the "
        "count drops (sessions already projecting keep refreshing their own view). The count "
        "is every such file in the directory -- kiro-cli reads them "
        "all at startup regardless of which Kiro Crew home wrote them -- so a backlog another "
        "home leaked trips this too, and this gateway cannot drain that home's files. Run "
        "`kirocrew doctor`: it names any foreign-home share and the remedy (stop every "
        "gateway using this agents directory, then move the files out). A gateway restart "
        "runs the boot drain for this home's own files.",
        count,
        SKILL_VIEW_PROJECTION_CEILING,
        directory,
    )


def project_agent_view(
    spec: dict[str, Any],
    source: Path,
    agent_name: str,
    *,
    crew_home_id: str,
    inherits_skills: bool,
    per_session_element: bool,
) -> tuple[dict[str, Any] | None, bool, str | None]:
    """*spec*'s native skill view: ``(view, is_search_agent, refusal)``.

    The view drops every ``skill://`` resource and serves the agent's skills through
    ``kirocrew-core``'s ``skill_search`` instead; every other field is the spec's own.
    *source* is the spec's file, which a relative ``file://`` prompt is anchored to.
    *inherits_skills* says the agent's workspace lends it inherited skill trees.
    ``view`` is ``None``, with the sentence that says why, when the view is refused.
    Shared by :func:`prepare_native_skill_projection` and the app launch copy
    (``apps.agent_launch``), so the two views cannot drift apart.
    """
    search = False
    # An iterative copy, not ``copy.deepcopy``: the reader admits a spec
    # nested hundreds of levels deep, and a recursive copy of it would
    # raise ``RecursionError`` out of the projection.
    view = copy_json_document(spec)
    _strip_alias_display_text(view)
    # The ONE display field a view carries, replacing the authored one: it
    # tells a reader that lists the agents directory (the Kiro IDE) what the
    # row is and which agent to pick instead, and it tags the publishing
    # home for the prune (see _MANAGED_VIEW_DESCRIPTION_PREFIX). Short, so
    # kiro-cli's per-session copy of it stays small.
    view["description"] = _managed_view_description(agent_name, crew_home_id)
    resources = view.get("resources", [])
    resources = resources if isinstance(resources, list) else []
    view["resources"] = [
        r for r in resources if not (isinstance(r, str) and r.startswith("skill://"))
    ]
    mapped = any(isinstance(r, str) and r.startswith("skill://") for r in resources)
    # An unmapped custom agent reaches the block below only through its
    # inherited skill trees. Where the block would refuse a mapped agent, this
    # one keeps the view it had and gets no search: it never relied on it, and
    # it is not a search agent, so no session of it is refused either.
    inherited_only = (
        agent_name != "kirocrew" and not mapped and inherits_skills and can_inherit_skills(spec)
    )
    if agent_name == "kirocrew" or mapped or inherited_only:
        refusal = skill_search_refusal(view)
        if refusal is not None:
            return None, False, refusal
        # The bounded directory must have a loading path even for a custom spec
        # whose authored resources rely on native skill activation. Expose
        # only the read/search capability; do not grant server-wide tools or
        # change the author's approval policy.
        from kiro_crew.agent import managed_mcp_spec_entry

        original_core = view.get("mcpServers", {}).get("kirocrew-core", {})
        # The verdict the mount will reach for the spec's part, reached here
        # first, over the declaration AS AUTHORED. The managed replacement
        # built below carries only the keys a per-session element can
        # express, so judging it would let a ``type`` or a key the element
        # cannot carry vanish unjudged and the control plane mount despite the
        # restriction; the mount's own arms read the declaration itself, and
        # so does this. A restriction authored in the spec refuses the view:
        # the view would drop this agent's skill resources on the promise of
        # an element the mount can never mount for it. The settings files are
        # deliberately NOT read here -- see the note above the loop -- so a
        # spec that is clean gets its view whatever the files say today, and
        # the mount asks the full question over the files at every session
        # start. A broker stub ``session/new`` would mount ahead of the
        # element is not a source either: a stub element carries a
        # kiro-cli-only restriction no better than the native element does,
        # and its overlay is written from the spec and the global file alone,
        # so the sources decide, and only the sources.
        #
        # The question exists only where an element will REPLACE the
        # declaration. The direct client mounts none: kiro-cli loads the
        # view's entry itself -- the managed launch plus the restrictions
        # copied onto it below -- and honours them natively, with the
        # session's identity on the process environment. There a
        # ``disabledTools`` naming other tools, or a ``timeout``, restricts
        # exactly what its author meant and leaves ``skill_search`` standing
        # (the checks above already refused the view if it did not), so
        # withholding the view for it would refuse a supported customization
        # and abort the spawn over a restriction that reaches the session.
        withheld = (
            session_mcp.native_mount_withholding("kirocrew-core", original_core, [])
            if per_session_element
            else None
        )
        entry = managed_mcp_spec_entry("kirocrew-core") if withheld is None else None
        if entry is None:
            if not inherited_only:
                return (
                    None,
                    False,
                    (
                        withheld.explain("skill search")
                        if withheld is not None
                        else "Crew's managed skill search server is unavailable"
                    ),
                )
        else:
            for key in ("autoApprove", "disabledTools", "timeout"):
                if key in original_core:
                    entry[key] = original_core[key]
            view.setdefault("mcpServers", {})["kirocrew-core"] = entry
            tools = view.get("tools", [])
            if tools != "*" and isinstance(tools, list):
                if not any(t in tools for t in ("*", "@kirocrew-core", _SEARCH_TOOL)):
                    view["tools"] = [*tools, _SEARCH_TOOL]
            if not inherited_only:
                search = True
    prompt = view.get("prompt")
    if isinstance(prompt, str) and prompt.startswith("file://"):
        path = Path(prompt[7:]).expanduser()
        if not path.is_absolute():
            view["prompt"] = "file://" + (source.parent / path).absolute().as_posix()
    return view, search, None


def prepare_native_skill_projection(
    work_dir: Path,
    *,
    enabled: bool | None = None,
    per_session_element: bool = True,
    on_lock_unavailable: Callable[[OSError], None] | None = None,
) -> NativeSkillProjection | None:
    """Prepare native views after spec freshness admission, before spawning.

    Uses the existing workspace CLI settings channel. No home, identity store,
    session store or authored agent file is relocated or rewritten.

    ``per_session_element`` says how ``kirocrew-core`` reaches a search agent's
    sessions with this session's identity. ``True`` is the shared runtime: the
    mount re-declares the server as a per-session ``mcpServers`` element
    (:func:`session_mcp.kiro_control_plane_servers`) that REPLACES the spec's
    declaration, so a declaration the element cannot carry -- a ``disabledTools``
    list, a ``timeout``, a transport that is not stdio -- withholds the element,
    the view would stand on nothing, and the spec's part of that verdict refuses
    the view here. ``False`` is the direct client: one kiro-cli process for one
    session, the identity on the process environment, and the declaration
    mounted natively from the view with every restriction the view copies
    honoured by kiro-cli itself. No element replaces anything there, so a
    restriction on OTHER tools -- ``disabledTools: ["learn_add"]`` -- refuses
    nothing; only ``skill_search`` itself disabled or excluded still does.

    ``on_lock_unavailable`` receives the error when the alias lock cannot be
    taken and ``None`` is returned for it, so a caller can say WHY: an
    :class:`InProcessProjectionWaitTimeout` is this process's own queue, a
    :class:`CrossProcessProjectionLockTimeout` another process holding the file
    lock, any other ``OSError`` a problem with the lock file itself.
    """
    directory = kiro_agents_dir()
    crew_home_id = data_home().absolute().as_posix()
    # Set only when the ceiling (not the env disable) is what turned projection
    # off: that path still runs the bounded per-spawn prune before falling back,
    # so this home's own backlog can drain BACK under the ceiling across spawns
    # without a gateway restart. Without it the safety net would switch off the
    # very reclaim that recovers from it, and the count could only fall at the
    # next boot drain.
    ceiling_tripped = False
    if enabled is None:
        enabled = os.environ.get("KIROCREW_NATIVE_SKILL_PROJECTION", "1") != "0"
        # The ceiling is a spawn-time admission decision, applied ONLY on this
        # ``enabled is None`` path -- the one that chooses whether to project at
        # all. An explicit ``enabled=True`` is NOT a request to re-decide: the
        # shared runtime's warm set_mode refresh passes it to re-prepare a view a
        # live session already runs under, and there a ``None`` return is fatal
        # (the runtime raises ``AcpRuntimeError`` and refuses the start rather
        # than activate an unverified view). Intercepting that explicit True with
        # the ceiling would convert a bounded backlog into a session abort for
        # every warm shared-runtime session -- the opposite of a graceful
        # fallback. So the ceiling never touches an explicit caller; it only
        # declines to START projecting when nothing asked for it specifically.
        if enabled:
            view_count = _count_projected_aliases(directory)
            if view_count >= SKILL_VIEW_PROJECTION_CEILING:
                # The safety net: the agents directory already holds at least the
                # ceiling of views, so minting more would deepen a backlog
                # kiro-cli reads in full on every start. Fall back to the authored
                # agent by taking the exact path ``enabled=False`` takes below --
                # the overlay is rolled back and no view is written -- rather than
                # a path of its own, so the two cannot diverge. An agent that
                # already has a valid view does NOT get to keep reusing it above
                # the ceiling on this path: no projection is prepared at all, so
                # every agent of this spawn runs under its authored name until the
                # backlog drains. That is deliberate -- it matches
                # ``enabled=False`` exactly and needs no per-agent accounting on
                # the hot path -- and the recorded choice in the PR. A healthy
                # host never reaches here; see the ceiling constant. The fallback
                # still prunes below (``ceiling_tripped``) so the count can fall
                # back under the ceiling without a restart.
                _warn_ceiling_fallback_once(directory, view_count)
                enabled = False
                ceiling_tripped = True
    if not enabled:
        if ceiling_tripped:
            # Drain this home's own stale aliases before falling back, so a
            # backlog this gateway owns can fall under the ceiling across spawns
            # rather than waiting for a restart's boot drain. Best-effort and
            # bounded exactly like the normal per-spawn prune: it takes the alias
            # lock, is skipped when another prune ran too recently
            # (``_per_spawn_prune_due``), and never raises into the spawn. keep is
            # empty because this spawn publishes no alias. A foreign home's files
            # are not reclaimed here (by design -- see the warning), so a foreign
            # backlog still falls back every spawn, which is the intended safe
            # degraded mode.
            try:
                with _projection_alias_lock(directory):
                    if _per_spawn_prune_due(directory):
                        _prune_stale_managed_aliases(directory, crew_home_id, keep=set())
            except OSError:
                logger.debug(
                    "skill projection: ceiling-fallback prune skipped; lock or I/O error",
                    exc_info=True,
                )
        if not (work_dir / ".kiro" / "settings" / "cli.json").exists():
            return None
        try:
            with workspace_cli_settings_lock(work_dir) as locked_settings:
                _restore_inheritance(locked_settings, _settings(locked_settings, may_pause=False))
        except OSError:
            logger.warning(
                "skill projection: workspace settings lock unavailable during rollback",
                exc_info=True,
            )
        return None
    global_settings = _settings(kiro_home() / "settings" / "cli.json")
    # Whether kirocrew-core's per-session element can be mounted is decided
    # from the agent spec HERE and from the settings files BY THE MOUNT at each
    # session start (:func:`session_mcp.kiro_control_plane_servers`, whose
    # verdict the runtime refuses the session on), through one predicate shared
    # with the mount. The split follows what each source is: the spec is this
    # preparation's input and a restriction authored there is static for the
    # view's life, so it refuses the view now with the reason; the settings files
    # are written while a runtime is warm (the dashboard's tool toggle) and are
    # shared by every agent of the process, so a restriction there refuses the
    # search agent's SESSIONS, one at a time, naming the file -- never the spawn,
    # which every other agent's sessions ride on. This module reads no settings
    # file: the mount is their one reader.
    aliases: dict[str, str] = {}
    specs: dict[str, dict[str, Any]] = {}
    sources: dict[str, str] = {}
    errors: dict[str, str] = {}
    search_agents: set[str] = set()
    display_sizes: dict[str, tuple[int, str]] = {}
    # The skill trees an unmapped custom agent inherits. Crew supplies them through
    # its bounded directory, so such an agent needs the same search tool a mapped
    # one gets -- see agent_discovery.inherited_skill_globs.
    inherited_globs = inherited_skill_globs(work_dir, kiro_home_dir=kiro_home())
    for agent in list_agents(project_dir=str(work_dir)):
        if not agent.filename:
            continue
        source_dir = (
            project_agents_dir(str(work_dir)) if agent.scope == SCOPE_PROJECT else directory
        )
        source = source_dir / agent.filename
        spec = _read_agent_spec(source, operation="native_skill_projection", source="acp")
        if spec is None:
            # A spec ``list_agents`` yielded but the reader refuses here changed
            # between the two reads; skip it, exactly as an absent spec is skipped.
            continue
        display_sizes[agent.name] = (_display_text_bytes(spec), source.absolute().as_posix())
        view, search, refusal = project_agent_view(
            spec,
            source,
            agent.name,
            crew_home_id=crew_home_id,
            inherits_skills=bool(inherited_globs),
            per_session_element=per_session_element,
        )
        if view is None:
            errors[agent.name] = refusal or ""
            continue
        if search:
            search_agents.add(agent.name)
        specs[agent.name] = view
        sources[agent.name] = source.absolute().as_posix()

    _warn_on_display_text(display_sizes)
    try:
        alias_lock = _projection_alias_lock(directory)
    except OSError as exc:
        # Lock contention is a routine, expected outcome on a busy host, not a
        # defect: the raised OSError already names what is known (how long the
        # acquire waited and that the lock was still held), so one warning line
        # carries the whole story. A full traceback adds only contextlib frames
        # that point at the lock helper, not at a cause, and repeats dozens of
        # times a day on a contended host.
        logger.warning(
            "skill projection: alias lock unavailable (%s); retaining aliases, settings, and "
            "using authored agents",
            exc,
        )
        if on_lock_unavailable is not None:
            on_lock_unavailable(exc)
        # `local` was read before agent enumeration and lock acquisition. A
        # concurrent projection can write a newer overlay or unrelated setting
        # while this process waits, so writing this stale snapshot would clobber
        # that update. Keep the current file byte-for-byte; a later successful
        # preparation or explicit rollback can update it under normal ownership.
        return None
    with alias_lock:
        try:
            settings_lock = workspace_cli_settings_lock(work_dir)
            with settings_lock as locked_settings:
                # This is the authoritative read for both projected resources and
                # the write below. Every in-product workspace cli.json writer uses
                # the same sidecar lock, so no effort or Tool Search update can land
                # between this read and commit.
                local = _settings(locked_settings, may_pause=False)
                inherited, preference_source, overlaid = _inheritance_preference(
                    local, global_settings
                )
                if not overlaid:
                    local[_PREVIOUS_INHERITANCE] = {
                        "present": _INHERIT_SETTING in local,
                        "value": local.get(_INHERIT_SETTING),
                    }

                if inherited:
                    for view in specs.values():
                        for resource in (
                            f"file://{kiro_home().as_posix()}/steering/**/*.md",
                            "file://.kiro/steering/**/*.md",
                            "file://AGENTS.md",
                        ):
                            if resource not in view["resources"]:
                                view["resources"].append(resource)

                # The alias is named by what the view says, not by where it is
                # used: spawns that derive the same view -- any run folder, any
                # session -- share one file, and the directory holds one view per
                # distinct view content instead of one per agent per run. Views
                # can still differ per workspace: a SCOPE_PROJECT agent's prompt
                # is a file:// path under its project, and workspace-local
                # inheritance shapes the resources, so those get one alias per
                # workspace. The agent name is hashed too, so two agents with
                # identical specs still get distinct aliases, and so is the Crew
                # data home, so two homes sharing one agents directory never
                # contend for (and re-own) the same file. MCP server env VALUES
                # are left out of the name (see _alias_identity): a tool that
                # rewrites a fresh per-write value into every agent file would
                # otherwise mint a new alias on every spawn. The source spec's
                # path is in the name instead, so two agent files that differ
                # only in env values -- the same project agent copied into two
                # workspaces with its own token each -- never share one alias.
                ownership: dict[str, dict[str, Any]] = {}
                for agent_name, view in list(specs.items()):
                    view.pop("name", None)
                    digest = hashlib.sha256(
                        json.dumps(
                            {
                                "agent": agent_name,
                                "home": crew_home_id,
                                "source": sources[agent_name],
                                "view": _alias_identity(view),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()[:24]
                    alias = NATIVE_SKILL_ALIAS_PREFIX + digest
                    specs[agent_name] = {"name": alias, **view}
                    aliases[agent_name] = alias
                    ownership[alias] = {
                        _MANAGED_MARKER: _MANAGED_MARKER_VALUE,
                        _MANAGED_CREW_HOME: crew_home_id,
                        _MANAGED_AGENT: agent_name,
                        _MANAGED_SOURCE: sources[agent_name],
                    }

                metadata_dir = _ensure_projection_metadata_directory() if aliases else None
                lease_stack = _acquire_projection_lease(directory, set(aliases.values()))
                try:
                    for agent_name, alias in aliases.items():
                        alias_raw = json.dumps(specs[agent_name], ensure_ascii=False)
                        metadata = {
                            **ownership[alias],
                            _MANAGED_ALIAS_SHA256: hashlib.sha256(alias_raw.encode()).hexdigest(),
                            _MANAGED_VIEW_SHA256: _view_digest(specs[agent_name]),
                        }
                        alias_path = directory / f"{alias}.json"
                        if _is_current_publication(directory, alias_path, alias_raw, crew_home_id):
                            # Another spawn already published this spec with
                            # this home's record; keep its inode as is.
                            continue
                        atomic_write(alias_path, alias_raw, restrict_to_owner=True)
                        # Renamed into place, which kiro-cli does not reload on.
                        _announce_publication(alias_path, alias_raw.encode())
                        assert metadata_dir is not None
                        atomic_write(
                            metadata_dir / f"{alias}.json",
                            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                            restrict_to_owner=True,
                        )
                    if metadata_dir is not None:
                        try:
                            _record_view_ledger(metadata_dir, aliases)
                        except OSError:
                            # The ledger only serves names stored before a
                            # restart; losing one write never blocks a spawn.
                            logger.debug("skill projection: view ledger not written", exc_info=True)
                    local[_MANAGED_SETTING] = inherited
                    local[_INHERIT_SOURCE] = preference_source
                    local[_INHERIT_SETTING] = True
                    atomic_write(locked_settings, json.dumps(local, indent=2))
                    prepared = NativeSkillProjection(
                        aliases,
                        specs,
                        errors,
                        search_agents,
                    )
                    _remember_view_sources(aliases)
                    # Releases the lease and prunes this home's unleased views
                    # as soon as the projection ends -- explicitly through
                    # ``release()`` at session end, or when it is dropped.
                    prepared._lease_finalizer = weakref.finalize(
                        prepared,
                        _end_projection,
                        lease_stack,
                        directory,
                        crew_home_id,
                        _projection_side_dir(),
                    )
                    # The thread a collected projection's end is queued for.
                    _ensure_projection_reaper()
                except BaseException:
                    lease_stack.close()
                    raise
        except OSError as exc:
            # Same routine contention as the alias-lock fallback above: the
            # OSError names the real wait, so one warning line suffices and a
            # traceback would only repeat the lock-helper frames.
            logger.warning(
                "skill projection: workspace settings or lease lock unavailable (%s); retaining "
                "aliases and using authored agents",
                exc,
            )
            return None
        _register_active_projection(prepared)
        if _per_spawn_prune_due(directory):
            _prune_stale_managed_aliases(directory, crew_home_id, keep=set(aliases.values()))

    return prepared
