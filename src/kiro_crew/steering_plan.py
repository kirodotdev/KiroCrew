"""Which steering files one chat receives: the verdicts the per-chat list shows.

A read-only plan, never a second loader. Where Crew writes the steering text, the
verdict is read from the writer itself; where a harness loads it, the plan says so:

1. A crew member's chat carries its steering in the member essentials envelope
   (``context_assembly.member``), which decides per guide: ``briefing``.
2. KAS reports ``native_steering`` and loads the trees itself, honouring
   ``inclusion``: ``harness``.
3. kiro-cli loads them from Crew's view while the workspace inherits its default
   resources (``context._project_steering_delivered``), and otherwise loads the
   documents the agent's own ``resources`` name: ``harness`` for those, ``no``
   for the rest, and ``harness`` with ``agent_spec_unread`` when the plan cannot
   tell which spec serves the chat.
4. The Claude Code seam and Codex load them through ``steering_resources_part``,
   only for the Kiro Crew default agent. The documents come from
   :func:`kiro_crew.context_assembly.agent_spec.steering_resource_documents`, the
   same list the loader joins: ``yes`` for each it keeps, ``no`` for one it reads
   and withholds, ``no`` for a listed file it does not pick up, and a row of its own
   for a document it loads from outside the two trees (``AGENTS.md`` on Claude
   Code; Codex reads the project's ``AGENTS.md`` itself, so the load and the list
   leave it out).
5. Every other harness has no path for them: ``no``.

Folder steering (``steering_dirs``) is rendered by Crew on every harness; the caller
renders it the way the turn does (the same delivery snapshot and cap) and passes
which documents the bounded section kept. A member chat's folder guides are
``briefing``: its essentials envelope decides them.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any, Final

#: Values of ``reaches``. ``harness`` and ``briefing`` mean another loader decides
#: by its own rules, so the list does not claim either way.
REACHES_YES: Final = "yes"
REACHES_NO: Final = "no"
REACHES_HARNESS: Final = "harness"
REACHES_BRIEFING: Final = "briefing"

#: Values of ``reason``, one per gate above; ``""`` for a ``yes``.
REASON_MEMBER_BRIEFING: Final = "member_briefing"
REASON_HARNESS_NATIVE: Final = "harness_native"
REASON_AGENT_RESOURCES: Final = "agent_resources"
REASON_AGENT_SPEC_UNREAD: Final = "agent_spec_unread"
REASON_INHERITANCE_OFF: Final = "inheritance_off"
REASON_CUSTOM_AGENT: Final = "custom_agent"
REASON_INCLUSION_WITHHELD: Final = "inclusion_withheld"
REASON_LINK_NOT_ADMITTED: Final = "link_not_admitted"
REASON_NOT_LOADED: Final = "not_loaded"
REASON_FOLDER_CAP: Final = "folder_cap"
REASON_HARNESS_HAS_NO_PATH: Final = "harness_has_no_path"

#: Values of a document's state in Crew's steering load.
LOADER_KEPT: Final = "kept"
LOADER_WITHHELD: Final = "withheld"

_ALWAYS: Final = "always"
#: ``applies`` for a document whose mode the listing could not read.
APPLIES_UNKNOWN: Final = "unknown"


def declared_steering_targets(agent: str | None, project: str | None) -> frozenset[Path] | None:
    """Resolved ``.md`` targets the agent's own ``file://`` resources match, or ``None``.

    Read the way :func:`kiro_crew.context._load_steering_resources` reads
    ``kirocrew.json``: through the hardened agent-spec reader, each entry anchored by
    ``_steering_glob_anchor``. ``None`` means the plan cannot tell which spec kiro-cli
    serves, so the caller must not claim either way: a pinned name, a project spec of
    that name (kiro-cli reads it first), or a spec that cannot be read.
    """
    from kiro_crew import context as ctx
    from kiro_crew.agent_discovery import _read_agent_spec
    from kiro_crew.agent_spec_pins import spec_pin_for
    from kiro_crew.config.paths import project_agents_dir
    from kiro_crew.context_assembly.agent_spec import _steering_glob_anchor

    name = agent or "kirocrew"
    if spec_pin_for(name) is not None:
        return None
    project_root = Path(project).expanduser() if project else None
    if project_root and (project_agents_dir(project_root) / f"{name}.json").exists():
        return None
    spec_path = ctx.kiro_agents_dir() / f"{name}.json"
    if not spec_path.is_file():
        return None
    spec = _read_agent_spec(spec_path, operation="steering_plan", source="dashboard")
    if spec is None:
        return None
    declared = spec.get("resources", [])
    targets: set[Path] = set()
    for res in declared if isinstance(declared, list) else []:
        if not isinstance(res, str) or not res.startswith("file://"):
            continue
        try:
            anchor, pattern = _steering_glob_anchor(res.removeprefix("file://"), project_root)
            targets.update(p.resolve() for p in anchor.glob(pattern) if p.suffix == ".md")
        except (OSError, ValueError, RuntimeError):
            continue
    return frozenset(targets)


def _loads_through_crew(
    provider_type: str, native_steering: bool, agent: str | None, is_member: bool
) -> bool:
    """Whether this chat's turns run Crew's steering load (``steering_resources_part``)."""
    from kiro_crew import context as ctx
    from kiro_crew.agent_files import is_kirocrew_default_agent

    return (
        not is_member
        and not native_steering
        and ctx.acp_driver.crew_loads_spec_steering(provider_type)
        and (not agent or is_kirocrew_default_agent(agent))
    )


def tree_verdict(
    *,
    provider_type: str,
    native_steering: bool,
    project: str | None,
    agent: str | None,
    is_member: bool,
    target: Path | None = None,
    declared: Collection[Path] | None = None,
    loaded: str | None = None,
    linked: bool = False,
) -> tuple[str, str]:
    """``(reaches, reason)`` for one global or project steering document.

    *provider_type* is the session's provider label (``provider_label``). *target* is
    the document's resolved path. *declared* is :func:`declared_steering_targets` for
    the chat's agent. *loaded* is the document's state in Crew's steering load
    (:data:`LOADER_KEPT`, :data:`LOADER_WITHHELD`, or ``None`` when that load does
    not pick it up), and *linked* whether the listing found it through a link.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    if is_member:
        return REACHES_BRIEFING, REASON_MEMBER_BRIEFING
    if native_steering:
        return REACHES_HARNESS, REASON_HARNESS_NATIVE
    if provider_type == ctx.PROVIDER_ACP:
        if ctx._project_steering_delivered(provider_type, False, project):
            return REACHES_HARNESS, REASON_HARNESS_NATIVE
        if declared is None:
            return REACHES_HARNESS, REASON_AGENT_SPEC_UNREAD
        if target is not None and target in declared:
            return REACHES_HARNESS, REASON_AGENT_RESOURCES
        return REACHES_NO, REASON_INHERITANCE_OFF
    if ctx.acp_driver.crew_loads_spec_steering(provider_type):
        if not _loads_through_crew(provider_type, native_steering, agent, is_member):
            return REACHES_NO, REASON_CUSTOM_AGENT
        if loaded == LOADER_KEPT:
            return REACHES_YES, ""
        if loaded == LOADER_WITHHELD:
            return REACHES_NO, REASON_INCLUSION_WITHHELD
        return REACHES_NO, REASON_LINK_NOT_ADMITTED if linked else REASON_NOT_LOADED
    return REACHES_NO, REASON_HARNESS_HAS_NO_PATH


def _resolved_target(entry: dict[str, Any], project: str | None) -> Path | None:
    """The resolved path of one listed entry, or ``None`` when it has none."""
    if entry.get("source") == "user":
        root = Path.home() / ".kiro" / "steering"
    elif project:
        root = Path(project) / ".kiro" / "steering"
    else:
        root = None
    rel = entry.get("rel")
    if root is None or not isinstance(rel, str) or not rel:
        return None
    try:
        return (root / rel).resolve()
    except (OSError, RuntimeError):
        return None


def chat_steering_plan(
    *,
    listed: Sequence[dict[str, Any]],
    folder_documents: Sequence[str],
    provider_type: str,
    native_steering: bool,
    project: str | None,
    agent: str | None,
    is_member: bool,
    folder_cut: Sequence[str] = (),
    loader_documents: Sequence[tuple[str, Path, bool]] = (),
) -> list[dict[str, str]]:
    """One row per steering file this chat may receive.

    *listed* is ``list_steering_blocking``'s ``files`` (sources ``user`` and
    ``workspace``). *folder_documents* is the display path of every folder document
    the bounded folder section keeps and *folder_cut* of every one it leaves out, in
    the same spelling. A listed file the folder delivers (a folder that declares a
    tree the harness does not load) keeps one row, marked ``yes``.
    *loader_documents* is ``(display path, resolved path, kept)`` for each document
    Crew's steering load admits.
    """
    from kiro_crew import context as ctx

    declared = (
        declared_steering_targets(agent, project)
        if provider_type == ctx.PROVIDER_ACP and not native_steering and not is_member
        else None
    )
    via_seam = _loads_through_crew(provider_type, native_steering, agent, is_member)
    load = {resolved: kept for _display, resolved, kept in loader_documents} if via_seam else {}
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    used: set[Path] = set()
    via_folder = set(folder_documents)
    cut_by_folder = set(folder_cut)
    # A member chat's essentials envelope decides its folder guides too.
    folder_yes = (REACHES_BRIEFING, REASON_MEMBER_BRIEFING) if is_member else (REACHES_YES, "")
    for entry in listed:
        source = "global" if entry.get("source") == "user" else "project"
        # A link the listing did not read carries the default, not its own mode.
        readable = bool(entry.get("readable", True))
        applies = str(entry.get("inclusion") or _ALWAYS) if readable else APPLIES_UNKNOWN
        path = str(entry.get("path") or "")
        target = _resolved_target(entry, project)
        if path in via_folder:
            reaches, reason = folder_yes
        else:
            kept = load.get(target) if target is not None else None
            if target is not None and kept is not None:
                used.add(target)
                if not readable and kept:
                    applies = _ALWAYS
            if kept is None:
                loaded = None
            elif kept:
                loaded = LOADER_KEPT
            else:
                loaded = LOADER_WITHHELD
            reaches, reason = tree_verdict(
                provider_type=provider_type,
                native_steering=native_steering,
                project=project,
                agent=agent,
                is_member=is_member,
                target=target,
                declared=declared,
                loaded=loaded,
                linked=bool(entry.get("linked")),
            )
            # The folder would have carried it, and its section cap is why it does not.
            if reaches == REACHES_NO and path in cut_by_folder:
                reason = REASON_FOLDER_CAP
        seen.add(path)
        rows.append(
            {
                "name": str(entry.get("name") or ""),
                "path": path,
                "source": source,
                "applies": applies,
                "reaches": reaches,
                "reason": reason,
            }
        )
    for display, resolved, kept in loader_documents if via_seam else ():
        if resolved in used or display in seen:
            continue
        seen.add(display)
        rows.append(
            {
                "name": display.replace("\\", "/").rsplit("/", 1)[-1],
                "path": display,
                "source": "agent",
                "applies": _ALWAYS if kept else APPLIES_UNKNOWN,
                "reaches": REACHES_YES if kept else REACHES_NO,
                "reason": "" if kept else REASON_INCLUSION_WITHHELD,
            }
        )
    for path, reaches, reason in [
        *((p, *folder_yes) for p in folder_documents),
        *((p, REACHES_NO, REASON_FOLDER_CAP) for p in folder_cut),
    ]:
        if path in seen:
            continue
        seen.add(path)
        rows.append(
            {
                "name": path.replace("\\", "/").rsplit("/", 1)[-1],
                "path": path,
                "source": "folder",
                "applies": _ALWAYS,
                "reaches": reaches,
                "reason": reason,
            }
        )
    return rows
