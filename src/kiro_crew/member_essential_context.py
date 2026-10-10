"""Bounded, complete V2 persona and project documents; no memory search."""

from __future__ import annotations

import fnmatch
import logging
import os
from pathlib import Path

from kiro_crew.agent_sdk.drivers import acp as acp_driver
from kiro_crew.config import KiroCrewConfig, config_dir
from kiro_crew.config.loader import workspace_dir_for
from kiro_crew.config.paths import project_agents_dir
from kiro_crew.frontmatter import STEERING_LOADER, split_frontmatter
from kiro_crew.hooks import safe_read_file_bytes_nolink, validate_file_path
from kiro_crew.platform_compat import first_linked_ancestor, is_link_or_junction

logger = logging.getLogger(__name__)

ESSENTIAL_MAX_CHARS = 64_000
_MAX_SOURCE_BYTES = ESSENTIAL_MAX_CHARS * 4
#: Source label of the in-band notice that names guides left out of the envelope.
ESSENTIAL_OMISSION_SOURCE = "essential-context#omitted"
#: Source-label prefix of the in-band notice that names wildcard entries pruned as
#: managed state; the full label appends ``:<template>``.
ESSENTIAL_MANAGED_SKIP_SOURCE = "essential-context#managed-skipped"
#: Source label of the in-band notice that names linked entries the implicit
#: steering scan left out instead of refusing the whole turn.
ESSENTIAL_LINKED_SKIP_SOURCE = "essential-context#linked-skipped"
#: Source-label prefix of the in-band notice that names declared literal
#: resources skipped because they do not exist; the full label appends ``:<template>``.
ESSENTIAL_MISSING_SKIP_SOURCE = "essential-context#missing-skipped"
_MAX_SKIPPED_LISTED = 10
_MAX_DIRECTORY_ENTRIES = 2048
_MAX_DOCUMENTS = 64

# A resources entry is a URI string this reader may open, or an object whose
# keys are kiro-cli's schema. The alias records the shape, not those keys.
ResourceDeclaration = str | dict[str, object]


class MemberEssentialContextError(ValueError):
    """A declared essential source cannot be included completely and safely."""


def _declared_document_count(resources: list[ResourceDeclaration]) -> int:
    """How many declared resources can become essential documents.

    Only ``file://`` declarations are ever read; ``skill://`` strings and
    object-form ``knowledgeBase`` declarations stay on demand and never enter
    the essentials snapshot, so they must not consume the document budget
    either. An agent that declares seventy skills and no files loads zero
    documents.
    """
    return sum(1 for r in resources if isinstance(r, str) and r.startswith("file://"))


class _ManagedEssentialSourceError(MemberEssentialContextError):
    """A managed source is excluded from wildcard discovery, never readable.

    *workspace_entry* is the top-level workspace entry name that matched the
    managed-name prefix, or ``None`` when the source sits under an admin root.
    """

    def __init__(self, message: str, *, workspace_entry: str | None = None) -> None:
        super().__init__(message)
        self.workspace_entry = workspace_entry


#: Top-level workspace names the product itself writes as managed state. An entry
#: the prefix prune catches under one of these names is the store working as
#: intended; any other name is a prefix collision with ordinary project content.
_MANAGED_EXACT_NAMES = frozenset({"memory", "lessons", ".lessons"})
#: Managed files whose name may carry a suffix (SQLite ``-wal``/``-shm`` sidecars).
_MANAGED_NAME_STEMS = ("memory.db", "memory_index.db", "lessons.jsonl")


def _is_prefix_collision(name: str) -> bool:
    """Whether a pruned top-level *name* is ordinary content, not managed state."""
    folded = name.casefold()
    return folded not in _MANAGED_EXACT_NAMES and not folded.startswith(_MANAGED_NAME_STEMS)


def _refuse_managed_source(path: Path) -> None:
    """Resources cannot reopen Global V1 or a peer's managed member state.

    The ROOTS are compared in their realpath spelling; the CANDIDATE is only
    ``abspath``-normalized. The asymmetry is deliberate on both sides.

    Roots must resolve because ``abspath`` follows no link: a candidate that
    arrived resolved -- as an expanded glob match does -- matches no root whose
    spelling still carries a symlink, which skips this isolation entirely on a
    host whose home is reached through one. A workspace root is configuration,
    but configuration a dashboard caller can write (an absolute ``dir``), so it
    is resolved through the same ``validate_file_path`` screen as everything
    else here (:func:`_comparable_root`): a UNC-shaped root is compared
    lexically instead of being resolved, because ``realpath`` on it would be
    the outbound SMB probe.

    The candidate must NOT resolve, because it can be an unvalidated caller path
    and ``realpath`` on one is itself an outbound probe for a UNC target on
    Windows -- the same reason ``validate_file_path`` screens UNC shapes BEFORE
    resolving anything. Callers that hold a validated path pass it already
    resolved, so those comparisons are exact; ``_read`` additionally re-checks the
    validated path, so the pre-validation call never has to be the deciding one.
    """
    cfg = KiroCrewConfig.load()
    roots = [config_dir(), Path.home() / ".kiro/crew", Path.home() / ".kirocrew"]
    workspaces = [config_dir() / "workspace"]
    workspaces.extend(workspace_dir_for(name) for name in cfg.workspaces)
    candidate = Path(os.path.abspath(path))
    # Reuse only within this check. A later call must observe new configuration
    # and link targets through the same guarded resolver, never a cached grant.
    resolved_roots = {root: _comparable_root(root) for root in dict.fromkeys([*roots, *workspaces])}
    in_workspace = False
    for workspace in workspaces:
        workspace = resolved_roots[workspace]
        admin_overlap = False
        for admin in roots:
            admin = resolved_roots[admin]
            if admin.is_relative_to(workspace):
                admin_overlap = True
            elif workspace.is_relative_to(admin):
                top = workspace.relative_to(admin).parts[0].casefold()
                if top in {
                    "members",
                    "member-rules",
                    "backups",
                    "trust",
                } or top.startswith(("memory", "lessons")):
                    admin_overlap = True
        if admin_overlap:
            continue
        if candidate.is_relative_to(workspace):
            parts = candidate.relative_to(workspace).parts
            if parts and parts[0].casefold().startswith(("memory", "lessons", ".lessons")):
                raise _ManagedEssentialSourceError(
                    f"Essential source {path}: managed memory/member state cannot be a project resource",
                    workspace_entry=parts[0],
                )
            in_workspace = True
    if not in_workspace and any(candidate.is_relative_to(resolved_roots[root]) for root in roots):
        raise _ManagedEssentialSourceError(
            f"Essential source {path}: managed memory/member state cannot be a project resource"
        )


def member_context_identity(member: str, *, member_is_id: bool = True) -> tuple[str, str]:
    """Resolve an explicit ID or configured name without touching learned memory."""
    if not member:
        return "", ""
    from kiro_crew.execution_context import member_config_for_id

    cfg = KiroCrewConfig.load()
    member_id = member
    if not member_is_id:
        configured = cfg.agents.get(member)
        if configured is not None and not configured.member_id:
            return "", ""
        member_id = configured.member_id if configured else ""
    _, configured_member = member_config_for_id(cfg, member_id)
    return member_id, configured_member.kiro_agent or "kirocrew"


def _comparable_root(root: Path) -> Path:
    """The spelling a root is compared against in :func:`_refuse_managed_source`.

    Resolved through ``validate_file_path`` when that screen admits the root, so
    a symlinked spelling matches resolved candidates. A root the screen refuses
    (a UNC share not on the trusted list, a sensitive path) is never handed to
    ``realpath`` -- on Windows that resolution is itself the network probe --
    and keeps the lexical ``abspath`` comparison this check always had.
    """
    admitted = _admitted_root(root)
    return admitted if admitted is not None else Path(os.path.abspath(root))


def _admitted_root(root: Path) -> Path | None:
    """Normalize a declared root to the spelling admitted paths are compared against.

    ``validate_file_path`` returns a fully RESOLVED path, so a root that still
    carries a symlink in its own spelling matches no document at all: on a host
    whose ``$HOME`` is ``/home/<user>`` linking to ``/local/home/<user>``,
    ``Path.home()`` IS the link, every admitted path resolves past it, and the
    containment check below is false for every source. The ``project`` root is
    already stored resolved by ``documents_for_member``; this gives a root taken
    from ``Path.home()`` the same treatment instead of leaving the caller to
    remember it.

    Containment stays exact -- a document's real path must still sit inside the
    real root -- and this says nothing about paths BELOW the root, which the
    walk still refuses when they are, or sit under, a link. Only the declared
    root's own spelling is normalized, and that root comes from configuration
    rather than from scanned content.
    """
    admitted = validate_file_path(str(root))
    return None if admitted is None else Path(admitted)


def _read(path: Path, root: Path) -> str:
    try:
        _refuse_managed_source(path)
        admitted = validate_file_path(str(path))
        admitted_root = _admitted_root(root)
        if (
            admitted is None
            or admitted_root is None
            or not Path(admitted).is_relative_to(admitted_root)
        ):
            raise ValueError("outside the admitted document root")
        _refuse_managed_source(Path(admitted))
        # Pin this admitted parent, not the whole home. A racing ancestor
        # redirect cannot reach managed data allowed by the generic V1 gate.
        data = safe_read_file_bytes_nolink(
            str(path),
            within_root=str(Path(admitted).parent),
            max_bytes=_MAX_SOURCE_BYTES,
            allow_truncate=False,
        )
        if data is None:
            raise ValueError("missing, unreadable, or outside the admitted document root")
        return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, ValueError) as exc:
        raise MemberEssentialContextError(f"Essential source {path}: {exc}") from exc


def _read_implicit_guide(path: Path, root: Path) -> str | None:
    """Read a project-root guide the template did not declare, or ``None`` if refused.

    ``AGENTS.md`` and ``SOUL.md`` are picked up because they exist, not because
    the template names them, so a refused one must not refuse the whole turn:
    a guide symlinked to a repository outside the project, or a link whose
    target has gone, would otherwise abort every session start of the agent. The read is
    the same :func:`_read` every declared source goes through -- containment,
    managed-state isolation, the no-follow descriptor read -- so nothing it
    refuses is read here either; only the refusal's consequence differs. A
    declared source keeps failing closed, and an oversized guide still raises,
    because that is a guide the user can shorten rather than one this reader
    may not open.
    """
    try:
        return _read(path, root)
    except MemberEssentialContextError as exc:
        reason = str(exc.__cause__ or exc)
        logger.warning("Project guide %s not loaded: %s", path, reason)
        return None


def _omitted_guide(path: Path, root: Path) -> tuple[str, str]:
    """The in-band note that stands in for a guide :func:`_read_implicit_guide` refused."""
    return (
        f"{path}#omitted",
        f"PROJECT GUIDE NOT LOADED. {path.name} in {root} could not be read safely: it is "
        "a link to a file outside this project, a link whose target is missing, a managed "
        "memory file, a hard link, or otherwise unreadable. Do not assume its contents. "
        f"If it should apply, ask the user to make {path.name} a regular file inside {root}.",
    )


def _skipped_managed_note(skipped: list[Path], template: str) -> tuple[str, str] | None:
    """The in-band note naming wildcard entries pruned as managed state, or ``None``.

    The prune is a name-prefix heuristic and stays fail-safe: an entry whose
    top-level name starts with ``memory``/``lessons`` in a managed workspace is
    never read. An ordinary project entry can share that prefix, so -- like the
    ``#omitted`` note for a refused guide -- the agent is told which entries were
    left out instead of the snapshot dropping them without a trace.

    The source is keyed by *template*: a turn that merges the owner template's
    documents with a different execution template's must see two distinct
    sources, since each template's globs can skip different entries.
    """
    unique = list(dict.fromkeys(skipped))
    if not unique:
        return None
    listed = ", ".join(str(path) for path in unique[:_MAX_SKIPPED_LISTED])
    more = len(unique) - _MAX_SKIPPED_LISTED
    if more > 0:
        listed += f", and {more} more"
    return (
        f"{ESSENTIAL_MANAGED_SKIP_SOURCE}:{template}",
        f"PROJECT ENTRIES NOT LOADED. {len(unique)} entr{'y' if len(unique) == 1 else 'ies'} "
        f"matched a declared resource pattern but were skipped because the name matches "
        f"managed memory/lessons state: {listed}. Do not assume their contents. If one is "
        "project content, ask the user to rename it so its name does not start with "
        "'memory' or 'lessons'.",
    )


def _skipped_linked_note(linked: list[Path]) -> tuple[str, str] | None:
    """The in-band note naming steering links the implicit scan left out, or ``None``.

    Global and project steering are read because they exist, not because a
    template names them -- the same footing as the project-root ``AGENTS.md`` in
    :func:`_read_implicit_guide`. A link there therefore must not refuse every
    session start of the member; it is left out, still unread, and named here so
    the agent does not assume its contents. kiro-cli itself follows the link and
    may load the file natively.
    """
    unique = list(dict.fromkeys(linked))
    if not unique:
        return None
    listed = ", ".join(str(path) for path in unique[:_MAX_SKIPPED_LISTED])
    more = len(unique) - _MAX_SKIPPED_LISTED
    if more > 0:
        listed += f", and {more} more"
    return (
        ESSENTIAL_LINKED_SKIP_SOURCE,
        f"LINKED STEERING NOT IN THIS SNAPSHOT. {len(unique)} "
        f"entr{'y' if len(unique) == 1 else 'ies'} on the steering scan path "
        f"{'is a link' if len(unique) == 1 else 'are links'}, which this snapshot does "
        f"not read: {listed}. The host may still load linked steering natively, so its "
        "text can appear elsewhere in your context; this note only says the snapshot "
        "does not carry it.",
    )


def _is_absent(path: Path, admitted_root: Path) -> bool:
    """Whether a declared literal *path* below *admitted_root* simply does not exist.

    True only when some component of the path is missing and no component the
    probe reached on the way is a link: every ``lstat`` runs root-first and only
    after the components above it are known not to be links, so the probe never
    traverses one (the same order :func:`first_linked_ancestor` relies on). A
    link anywhere on the path -- dangling or not -- a present but unreadable
    file, and a managed-state path all answer False, so :func:`_read` refuses
    them exactly as it would have.
    """
    try:
        _refuse_managed_source(path)
    except _ManagedEssentialSourceError:
        return False
    if not path.is_relative_to(admitted_root):
        return False
    current = admitted_root
    for part in path.relative_to(admitted_root).parts:
        current = current / part
        if is_link_or_junction(current):
            return False
        try:
            os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            return True
        except (OSError, ValueError):
            # Present but not probeable (an unsearchable parent): not absent.
            return False
    return False


def _missing_declared_note(missing: list[Path], template: str) -> tuple[str, str] | None:
    """The in-band note naming declared resources that do not exist, or ``None``.

    kiro-cli skips a declared ``file://`` resource that is absent, so a template
    shipping ``file://AGENTS.md`` (or a subagent starting in an empty folder)
    must not refuse the session here either. Like the ``#omitted`` note for a
    refused guide, the agent is told which declared files were not there.
    """
    unique = list(dict.fromkeys(missing))
    if not unique:
        return None
    listed = ", ".join(str(path) for path in unique[:_MAX_SKIPPED_LISTED])
    more = len(unique) - _MAX_SKIPPED_LISTED
    if more > 0:
        listed += f", and {more} more"
    return (
        f"{ESSENTIAL_MISSING_SKIP_SOURCE}:{template}",
        f"DECLARED RESOURCES NOT FOUND. {len(unique)} "
        f"file{'' if len(unique) == 1 else 's'} the agent template declares "
        f"{'does' if len(unique) == 1 else 'do'} not exist and "
        f"{'was' if len(unique) == 1 else 'were'} skipped: {listed}. Do not assume "
        "their contents.",
    )


def _matches(
    root: Path,
    pattern: str,
    skipped: list[Path] | None = None,
    linked: list[Path] | None = None,
    missing: list[Path] | None = None,
) -> list[Path]:
    """Expand a declared glob with bounded directory work and no link traversal.

    A link the walk meets below *root* is refused, unless *linked* is given: then
    it is appended there and left out, unread and not descended into. Only the
    implicit steering scan passes *linked*; a declared resource keeps refusing.

    A glob-free *pattern* naming a path that does not exist is appended to
    *missing*, when given, and matches nothing -- kiro-cli skips an absent
    declared resource the same way. Only plain absence counts (see
    :func:`_is_absent`); a link, an unreadable file or an out-of-root path is
    still returned and refused by :func:`_read`.

    An entry the glob would have used but the managed-source check prunes for a
    prefix collision (a name like ``memory-notes`` that is not one of the store's
    own names) is logged as a warning and, when *skipped* is given, appended to
    it, so the caller can tell the agent which entries were left out. Pruning the
    store's real ``memory``/``lessons`` entries is logged at debug level only.
    """
    pieces = Path(pattern).parts
    if Path(pattern).is_absolute() or ".." in pieces:
        raise MemberEssentialContextError(
            f"Essential source {root / pattern}: outside admitted root"
        )
    # Walk the root in its admitted spelling: an unresolved root whose own path
    # contains a symlink would otherwise be refused as a linked directory on its
    # very first visit, before any document is considered.
    admitted_root = _admitted_root(root)
    if admitted_root is None:
        raise MemberEssentialContextError(f"Essential source {root}: outside admitted root")
    if not any(c in pattern for c in "*?["):
        path = admitted_root / pattern
        if missing is not None and _is_absent(path, admitted_root):
            missing.append(path)
            return []
        return [path]
    pending = [(admitted_root, 0)]
    result: set[Path] = set()
    pruned: set[Path] = set()
    scanned = 0
    visited: set[tuple[Path, int]] = set()
    while pending:
        directory, offset = pending.pop()
        if (directory, offset) in visited:
            continue
        visited.add((directory, offset))
        admitted = validate_file_path(str(directory))
        if admitted is None or not Path(admitted).is_relative_to(admitted_root):
            raise MemberEssentialContextError(
                f"Essential source {directory}: outside admitted root"
            )
        _refuse_managed_source(Path(admitted))
        if first_linked_ancestor(directory) or is_link_or_junction(directory):
            raise MemberEssentialContextError(f"Essential source {directory}: linked directory")
        component = pieces[offset]
        if not any(c in component for c in "*?["):
            # Literal prefixes need no directory listing. A large unrelated
            # project root must not exhaust a steering subtree's scan budget.
            path = directory / component
            if is_link_or_junction(path):
                if linked is not None:
                    linked.append(path)
                    continue
                raise MemberEssentialContextError(
                    f"Essential source {path}: linked document or directory"
                )
            if offset + 1 < len(pieces):
                pending.append((path, offset + 1))
            elif path.is_file():
                result.add(path)
            continue
        if component == "**" and offset + 1 < len(pieces):
            pending.append((directory, offset + 1))
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > _MAX_DIRECTORY_ENTRIES:
                        raise MemberEssentialContextError(
                            f"Essential source {root / pattern}: too many directory entries"
                        )
                    path = Path(entry.path)
                    matches = component == "**" or fnmatch.fnmatchcase(entry.name, component)
                    if not matches:
                        continue
                    # Wildcards discover project guides, not managed state. Prune
                    # before descent; literal prefixes and reads still refuse it.
                    try:
                        _refuse_managed_source(path)
                    except _ManagedEssentialSourceError as exc:
                        # The prune keeps the name-prefix heuristic fail-safe, so
                        # an ordinary project entry can be caught by it: say so
                        # instead of dropping it silently. Only an entry the glob
                        # would otherwise have descended into or returned counts,
                        # and only a prefix COLLISION is reported -- the real
                        # managed store pruned by a broad glob is routine.
                        is_dir = entry.is_dir(follow_symlinks=False)
                        wanted = (is_dir and (component == "**" or offset + 1 < len(pieces))) or (
                            not is_dir and offset == len(pieces) - 1
                        )
                        if wanted and path not in pruned:
                            pruned.add(path)
                            name = exc.workspace_entry
                            if name is not None and _is_prefix_collision(name):
                                logger.warning(
                                    "Essential source %s skipped by %s: its name matches "
                                    "managed memory/lessons state",
                                    path,
                                    pattern,
                                )
                                if skipped is not None:
                                    skipped.append(path)
                            else:
                                logger.debug("Managed state %s pruned from %s", path, pattern)
                        continue
                    if is_link_or_junction(path):
                        if linked is not None:
                            # Name only a link the glob could have used: a document
                            # matching the final component, or a directory to descend.
                            # The directory probe follows the link, so the target is
                            # screened first (a UNC target is never probed); a refused
                            # target is named without probing.
                            if (
                                fnmatch.fnmatchcase(entry.name, pieces[-1])
                                or validate_file_path(str(path)) is None
                                or entry.is_dir()
                            ):
                                linked.append(path)
                            continue
                        raise MemberEssentialContextError(
                            f"Essential source {path}: linked document or directory"
                        )
                    if entry.is_dir(follow_symlinks=False):
                        if component == "**" or offset + 1 < len(pieces):
                            pending.append((path, offset if component == "**" else offset + 1))
                    elif offset == len(pieces) - 1:
                        result.add(path)
                        if len(result) > _MAX_DOCUMENTS:
                            raise MemberEssentialContextError(
                                f"Essential source {root / pattern}: too many documents"
                            )
        except FileNotFoundError:
            continue  # A declared glob matching no existing source is valid.
        except OSError as exc:
            raise MemberEssentialContextError(f"Essential source {directory}: {exc}") from exc
    return sorted(result)


def resolve_template_path(template: str, project: str | None = None) -> Path | None:
    """Resolve one template, with a project override ahead of the global copy."""
    from kiro_crew.agent import agent_spec_path
    from kiro_crew.agent_discovery import _read_agent_spec, project_agent_files

    spec_path: Path | None = None
    if project:
        admitted = validate_file_path(project)
        if admitted is None:
            raise MemberEssentialContextError(f"Essential project {project}: cannot be read safely")
        for path in project_agent_files(
            Path(admitted), operation="member_essentials", source="context"
        ):
            spec = _read_agent_spec(path, operation="member_essentials", source="context")
            if spec is None and path.stem == template:
                raise MemberEssentialContextError(
                    f"Essential template {path}: cannot be read safely"
                )
            if spec is not None and spec.get("name", path.stem) == template:
                if spec_path is not None:
                    raise MemberEssentialContextError(f"Ambiguous essential template {template!r}")
                spec_path = path
    if spec_path is None:
        try:
            spec_path = agent_spec_path(template)
        except ValueError as exc:
            raise MemberEssentialContextError(f"Essential template {template!r}: {exc}") from exc
    return spec_path


def resolve_relative_prompt_path(
    source: Path, spec_path: Path, project: str | None
) -> tuple[Path, Path] | None:
    """Return a canonical relative prompt and the root that supplied its template."""
    try:
        root = _admitted_root(Path.home())
        if project:
            project_root = _admitted_root(Path(project))
            if project_root is None:
                raise ValueError("project root is not admitted")
            if spec_path.parent == project_agents_dir(project_root):
                root = project_root
        if root is None:
            raise ValueError("template root is not admitted")
        admitted = validate_file_path(str(root / source))
        if admitted is None or not Path(admitted).is_relative_to(root):
            raise ValueError("prompt is outside its template root")
        return Path(admitted), root
    except (OSError, ValueError):
        logger.debug("Skipping relative agent prompt outside its admitted root")
        return None


def native_prompt_document_key(agent: str) -> str:
    """The ``native_context_documents`` key holding the persona the harness loaded for *agent*."""
    return f"template://{agent}#prompt"


def native_spec_prompt_shape(prompt: object, spec_path: Path) -> bool:
    """Whether a spec ``prompt`` is one a harness loading specs natively delivers as-is.

    The one shape that can be shown identical to the harness's own copy: a
    USER-level spec whose prompt is inline text or an absolute ``file://`` not
    spelled with ``~``. The managed contract is a stub or pointer the harness
    reads literally while Crew delivers the resolved contract; a relative
    ``file://`` is anchored by Crew at the template root where the harnesses
    anchor it at the spec's own directory; kiro-cli documents no ``~`` expansion
    for ``prompt`` and drops an unreadable file silently; and a project-level
    spec is not the one a harness reading only the user level runs. So none of
    those qualify (kirodotdev/KiroCrew#13305). Nor does an absolute ``file://``
    whose target a Kiro Crew sandbox on this host may hide
    (:func:`kiro_crew.sandbox.sandbox_may_hide_path`): Crew reads it from the
    gateway, outside the sandbox, while a sandboxed harness may be denied it and
    drop it, so withholding the block against it could leave the session with
    no persona at all. Shared by the session-start load in ``context.py`` and
    the activation snapshot in ``acp/runtime.py``, so the two readers cannot
    drift apart.
    """
    from kiro_crew.agent import is_managed_prompt, kiro_agents_dir_path
    from kiro_crew.sandbox import sandbox_may_hide_path

    if not isinstance(prompt, str) or is_managed_prompt(prompt):
        return False
    try:
        if spec_path.parent != kiro_agents_dir_path():
            return False
    except (OSError, ValueError):
        return False
    if prompt.startswith("file://"):
        target = prompt[len("file://") :]
        if target.startswith("~") or not Path(target).is_absolute():
            return False
        return not sandbox_may_hide_path(target)
    return True


def native_spec_prompt_copy(
    agent: str, *, transport: str | None = None, work_dir: str | Path | None = None
) -> str | None:
    """The persona text a harness that loads *agent*'s spec natively delivers, or ``None``.

    Read at session activation so the session handle can snapshot what the
    harness actually loaded; the prompt builder's dedup then compares against
    that snapshot rather than against the file as it reads today, so a spec
    edited mid-session keeps its block. The spec read is the one the harness
    loads, named by *transport*: the skill-view alias
    (``kirocrew-skill-view-<digest>``, published into the user agents directory
    by :mod:`kiro_crew.acp.skill_projection` and the name kiro-cli is given at
    ``--agent`` and ``session/set_mode``) when a view was prepared for *agent*,
    otherwise *agent*'s own spec as kiro-cli resolves the plain name from its
    process cwd, *work_dir*: a ``.kiro/agents`` spec declaring the name in
    that checkout first, else the user-level spec. A checkout's copy is not one
    Crew's block can be shown to equal (:func:`native_spec_prompt_shape` admits
    the user level alone), so when the checkout declares the name nothing is
    recorded and the block is sent; reading the user level there instead would
    record text the harness never loaded and withhold the block while it runs
    the checkout's copy. The
    alias is a copy of the spec taken when the view was prepared, so after an
    edit of the source it is the alias, not the source, that says what the
    harness holds; reading the source there would record the revised text and
    withhold the block while the harness runs the old copy. The alias is read
    at its own path, never through the roster (which leaves aliases out), and
    only an alias-shaped, registry-valid name is accepted there. ``None`` for
    every shape :func:`native_spec_prompt_shape` rejects (judged on the spec
    read: an alias carries the authored prompt, a relative ``file://`` made
    absolute at the spec's own directory and a ``~`` one as written), for an
    empty prompt and for any read failure: recording nothing costs a repeated
    block, never the persona.
    """
    from kiro_crew.agent import is_registered_agent_name, kiro_agents_dir_path
    from kiro_crew.agent_discovery import _read_agent_spec
    from kiro_crew.agent_spec_format import is_native_skill_alias_name
    from kiro_crew.hooks import FileTooLargeError, safe_read_file

    try:
        if transport is not None and transport != agent:
            if not (is_native_skill_alias_name(transport) and is_registered_agent_name(transport)):
                return None
            path = kiro_agents_dir_path() / f"{transport}.json"
        else:
            # The plain name: kiro-cli resolves it against its process cwd before
            # the user level, so the read follows the same order. A checkout the
            # resolver refuses raises a ``ValueError`` and records nothing.
            source_path = resolve_template_path(
                agent, str(work_dir) if work_dir is not None else None
            )
            if source_path is None:
                return None
            path = source_path
        data = _read_agent_spec(path, operation="agent_prompt", source="context")
        if data is None:
            return None
        prompt = data.get("prompt")
        if not prompt or not native_spec_prompt_shape(prompt, path):
            return None
        if prompt.startswith("file://"):
            return safe_read_file(prompt[len("file://") :])
        return prompt
    except (OSError, ValueError, FileTooLargeError):
        logger.debug("No native prompt snapshot for agent %r", agent, exc_info=True)
        return None


def _admitted_project_root(project: str | None) -> Path | None:
    """The member's project as the essential readers may open it, or ``None``."""
    if not project:
        return None
    admitted = validate_file_path(project)
    if admitted is None:
        raise MemberEssentialContextError(f"Essential project {project}: cannot be read safely")
    project_root = Path(admitted)
    _refuse_managed_source(project_root)
    return project_root


def member_inherits_default_resources(project: str | None) -> bool:
    """Whether kiro-cli hands a member in its admitted *project* the default resources.

    Global and workspace steering plus ``AGENTS.md``. Settings that cannot be
    read keep inheritance; the caller decides whether kiro-cli serves the
    session at all, since a kiro-cli setting changes nothing on another harness.
    Computed once per member turn by the caller that knows the provider and
    handed to :func:`documents_for_member` and the folder-steering dedup, so a
    document under a ``.kiro/steering`` root is delivered by exactly one of them
    whichever way the workspace decides.
    """
    return acp_driver.inherits_default_resources(_admitted_project_root(project))


def documents_for_member(
    template: str,
    project: str | None,
    *,
    include_project: bool = True,
    native_only: bool = False,
    conditional_index: bool = False,
    context_settings: bool = False,
    trigger_text: str = "",
    inherits_default_resources: bool = True,
    core_sources_out: set[str] | None = None,
) -> list[tuple[str, str]]:
    """Read actual project instructions and the owner's declared template sources.

    Native project steering defaults to always; manual, auto and fileMatch
    documents are deliberately left to their native trigger. Generic product
    prompts keep their existing provider/session-start path.

    *inherits_default_resources* is the caller's verdict on whether the session's
    harness hands the member kiro-cli's default resources (global and workspace
    steering, ``AGENTS.md``). It defaults to inheriting because only a session
    kiro-cli serves can opt out, and only that caller knows which harness it has.

    *core_sources_out*, when given, receives the source label of every returned
    document that belongs to the member's CORE -- the persona prompt, ``SOUL.md``,
    the template context settings and any guide-not-loaded note -- so a caller
    fitting an over-budget envelope knows which documents it must never leave
    out. Every other returned document (global and project steering, the
    conditional-guide index, ``AGENTS.md`` and the declared ``file://``
    resources) is a guide the caller may drop whole.
    """
    from kiro_crew.agent import is_managed_prompt
    from kiro_crew.agent_discovery import _read_agent_spec

    documents: list[tuple[str, str]] = []
    seen: set[Path] = set()
    skipped: list[Path] = []
    linked: list[Path] = []
    missing: list[Path] = []
    project_root = _admitted_project_root(project)

    def _mark_core(source: str) -> None:
        if core_sources_out is not None:
            core_sources_out.add(source)

    def _finish() -> list[tuple[str, str]]:
        # A native-only read lists what the host itself loads; the note is not
        # a host-native source, so it rides only in the essentials envelope.
        notes = (
            ()
            if native_only
            else (
                _skipped_linked_note(linked),
                _skipped_managed_note(skipped, template),
                _missing_declared_note(
                    [path for path in missing if path.suffix.lower() == ".md"], template
                ),
            )
        )
        for note in notes:
            if note is not None:
                documents.append(note)
                _mark_core(note[0])
        return documents

    def add(path: Path, root: Path, *, steering: bool = False, body: str | None = None) -> None:
        if Path(os.path.abspath(path)) in seen:
            return
        if body is None:
            body = _read(path, root)
        if steering:
            fields, _ = split_frontmatter(body, STEERING_LOADER)
            inclusion = fields.get("inclusion", "always").strip().casefold()
            if inclusion in {"manual", "filematch", "auto"}:
                if conditional_index:
                    import hashlib
                    import re

                    named = bool(
                        re.search(
                            r"(?<![\w-])#" + re.escape(path.stem) + r"(?![\w-])", trigger_text
                        )
                    )
                    pattern = fields.get("fileMatchPattern", "").strip()
                    file_selected = (
                        inclusion == "filematch"
                        and bool(pattern)
                        and any(
                            fnmatch.fnmatchcase(token, pattern)
                            or (
                                pattern.startswith("**/")
                                and fnmatch.fnmatchcase(token, pattern[3:])
                            )
                            for token in re.findall(r"[\w./\\-]+", trigger_text)
                        )
                    )
                    if named or file_selected:
                        seen.add(Path(os.path.abspath(path)))
                        documents.append((str(path), body))
                        if len(documents) > _MAX_DOCUMENTS:
                            raise MemberEssentialContextError(
                                f"Essential source {path}: too many documents"
                            )
                        return

                    condition = {
                        "manual": f"Only when the user explicitly requests #{path.stem} or this guide.",
                        "filematch": "Only before working on a file matching fileMatchPattern; an empty pattern never matches.",
                        "auto": "Only when the description is relevant to the current task; an empty description requires an explicit request.",
                    }[inclusion]
                    documents.append(
                        (
                            f"{path}#selection",
                            "CONDITIONAL GUIDE, NOT ACTIVE INSTRUCTIONS. "
                            + condition
                            + f"\nRead {path} with the file tool when that condition holds, then apply its full current contents."
                            + f"\nDescription: {fields.get('description', '')}"
                            + f"\nfileMatchPattern: {fields.get('fileMatchPattern', '')}"
                            + f"\nContent version: {hashlib.sha256(body.encode('utf-8')).hexdigest()}",
                        )
                    )
                    seen.add(Path(os.path.abspath(path)))
                    if len(documents) > _MAX_DOCUMENTS:
                        raise MemberEssentialContextError(
                            f"Essential source {path}: too many documents"
                        )
                return
            if inclusion != "always":
                raise MemberEssentialContextError(
                    f"Essential source {path}: unknown steering inclusion {inclusion!r}"
                )
        seen.add(Path(os.path.abspath(path)))
        documents.append((str(path), body))
        if len(documents) > _MAX_DOCUMENTS:
            raise MemberEssentialContextError(f"Essential source {path}: too many documents")

    # kiro-cli appends its default resources (global and workspace steering,
    # AGENTS.md) to a custom agent only while the workspace inherits them. A
    # member whose workspace opts out loads just what its template declares, so
    # the snapshot must not re-add the operator's global steering behind it.
    inherits = include_project and not native_only and inherits_default_resources
    if inherits:
        for path in _matches(Path.home(), ".kiro/steering/**/*.md", skipped, linked):
            add(path, Path.home(), steering=True)

    if project_root is not None and include_project and not native_only:
        # SOUL.md is Crew's own member file, not a kiro-cli default resource, so
        # the opt-out leaves it in place.
        for name in ("AGENTS.md", "SOUL.md") if inherits else ("SOUL.md",):
            path = project_root / name
            if path.exists() or path.is_symlink():
                body = _read_implicit_guide(path, project_root)
                if body is None:
                    documents.append(_omitted_guide(path, project_root))
                    _mark_core(documents[-1][0])
                else:
                    add(path, project_root, body=body)
                    if name == "SOUL.md":
                        _mark_core(str(path))
        if inherits:
            for path in _matches(project_root, ".kiro/steering/**/*.md", skipped, linked):
                add(path, project_root, steering=True)

    spec_path = resolve_template_path(template, project)
    if spec_path is None:
        if template != "kirocrew":
            raise MemberEssentialContextError(f"Essential template {template!r}: not found")
        return _finish()
    spec = _read_agent_spec(spec_path, operation="member_essentials", source="context")
    if spec is None:
        raise MemberEssentialContextError(f"Essential template {spec_path}: cannot be read safely")
    # Native file resources are relative to the project cwd or user home.
    absolute_root = (
        project_root
        if project_root is not None and spec_path.is_relative_to(project_root)
        else Path.home()
    )
    source_root = project_root or Path.home()
    prompt = spec.get("prompt", "")
    if not isinstance(prompt, str):
        raise MemberEssentialContextError(f"Essential template {spec_path}: prompt must be text")
    # A fork inherits the managed contract; essentials omit it because the
    # session-start injection delivers it once, regardless of template name or
    # install directory (see is_managed_prompt).
    if prompt and not is_managed_prompt(prompt):
        if prompt.startswith("file://"):
            path = Path(prompt[7:]).expanduser()
            if path.is_absolute():
                add(path, absolute_root)
                _mark_core(str(path))
            else:
                resolved = resolve_relative_prompt_path(path, spec_path, project)
                if resolved is not None:
                    add(*resolved)
                    _mark_core(str(resolved[0]))
        else:
            documents.append((f"{spec_path}#prompt", prompt))
            _mark_core(documents[-1][0])
    if context_settings and not native_only:
        import json

        documents.append(
            (
                f"{spec_path}#context-settings",
                "Template context settings (descriptive, not authorization):\n"
                + json.dumps(
                    {
                        key: spec[key]
                        for key in (
                            "name",
                            "description",
                            "model",
                            "includeCrewContext",
                            "resources",
                        )
                        if key in spec
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        _mark_core(documents[-1][0])
    resources = spec.get("resources", [])
    if include_project and (
        not isinstance(resources, list) or any(not isinstance(r, (str, dict)) for r in resources)
    ):
        raise MemberEssentialContextError(
            f"Essential template {spec_path}: resources must be a list of declarations"
        )
    if include_project and isinstance(resources, list):
        if _declared_document_count(resources) > _MAX_DOCUMENTS:
            raise MemberEssentialContextError(f"Essential template {spec_path}: too many resources")
        for match, root in _resource_paths(resources, source_root, absolute_root, skipped, missing):
            add(match, root, steering="steering" in match.parts)
    return _finish()


def _resource_pattern(path: Path, root: Path) -> str:
    """The root-relative glob for an absolute declaration, in either root spelling.

    A declaration and its root can name the SAME directory in two spellings. An
    installer records an installed resource in its realpath spelling
    (``file:///local/home/<user>/.aim/...``) while ``Path.home()`` stays the link
    (``/home/<user>``) on a host whose home is reached through one, so the lexical
    ``relative_to`` below reports a resource genuinely inside home as outside it
    and refuses every absolute essential source on that host.

    The lexical comparison is tried FIRST, so nothing already admitted changes.
    The fallback compares against the same admitted spelling :func:`_matches` and
    :func:`_read` already anchor on, which is why it widens no root: the pattern
    it returns is still expanded under that one admitted root, and both reads
    re-screen the result. It only lets a caller name the root it is already
    confined to by its other spelling.

    The DECLARATION itself is never resolved -- ``realpath`` on an unvalidated
    caller path is itself the outbound probe a UNC target wants, the same
    asymmetry :func:`_refuse_managed_source` documents.
    """
    try:
        return str(path.relative_to(root))
    except ValueError:
        pass
    admitted_root = _admitted_root(root)
    if admitted_root is None:
        raise MemberEssentialContextError(f"Essential source {path}: outside {root}")
    try:
        return str(path.relative_to(admitted_root))
    except ValueError:
        pass
    # The reverse layout: the declaration carries the link spelling while the
    # root is already resolved (a project root is stored resolved). Only the
    # declaration's glob-free ANCESTORS are screened, never its tail, so a link
    # below the root is still refused: by the walk in :func:`_matches` for a
    # glob, and by the containment check in :func:`_read` for a literal path.
    for ancestor in reversed(path.parents):
        if any(c in ancestor.name for c in "*?["):
            break
        admitted = validate_file_path(str(ancestor))
        if admitted is not None and Path(admitted) == admitted_root:
            return str(path.relative_to(ancestor))
    raise MemberEssentialContextError(f"Essential source {path}: outside {root}")


def _resource_paths(
    resources: list[ResourceDeclaration],
    source_root: Path,
    absolute_root: Path,
    skipped: list[Path] | None = None,
    missing: list[Path] | None = None,
) -> list[tuple[Path, Path]]:
    """Declared ``file://`` matches; an absent literal one goes to *missing* instead."""
    paths: list[tuple[Path, Path]] = []
    absent: list[Path] = [] if missing is None else missing
    if _declared_document_count(resources) > _MAX_DOCUMENTS:
        raise MemberEssentialContextError(
            "Essential resource declaration exceeds the document limit"
        )
    for resource in resources:
        if not isinstance(resource, str) or not resource.startswith("file://"):
            continue
        path = Path(resource[7:]).expanduser()
        root = absolute_root if path.is_absolute() else source_root
        if path.is_absolute():
            pattern = _resource_pattern(path, root)
        else:
            pattern = str(path)
        for match in _matches(root, pattern, skipped, missing=absent):
            if match.suffix.lower() == ".md" and (match, root) not in paths:
                paths.append((match, root))
                if len(paths) > _MAX_DOCUMENTS:
                    raise MemberEssentialContextError(
                        "Essential resources exceed the document limit"
                    )
    return paths


def projected_resource_documents(definition: dict, cwd: str) -> dict[str, str]:
    """Snapshot only file resources present in the actual native wire definition.

    No implicit project scan and no template reread: project overrides cannot
    substitute their resources for the global definition KAS actually registers.
    Conditional inclusion stays with the native selector; skill URI resources
    and object-form ``knowledgeBase`` declarations keep their on-demand
    behavior and are never treated as full text.
    """
    resources = definition.get("resources", [])
    if not isinstance(resources, list) or any(not isinstance(r, (str, dict)) for r in resources):
        raise MemberEssentialContextError("Projected resources must be a list of declarations")
    documents: dict[str, str] = {}
    for path, root in _resource_paths(resources, Path(cwd), Path.home()):
        if str(path) in documents:
            continue
        body = _read(path, root)
        if "steering" in path.parts:
            fields, _ = split_frontmatter(body, STEERING_LOADER)
            inclusion = fields.get("inclusion", "always").strip().casefold()
            if inclusion in {"manual", "auto", "filematch"}:
                continue
            if inclusion != "always":
                raise MemberEssentialContextError(
                    f"Essential source {path}: unknown inclusion {inclusion!r}"
                )
        documents[str(path)] = body
    return documents


def kiro_launch_documents(template: str, project: str | None) -> list[tuple[str, str]]:
    """Selected resources plus Kiro's implicit AGENTS/always-steering scan.

    SOUL is not an implicit native source. Conditional modes vary by engine and
    version, so this responsibility includes only default/always steering. The
    implicit scan applies only while the workspace inherits kiro-cli's default
    resources, as it does natively; this model is kiro-cli's by construction, so
    it takes that verdict itself, once.
    """
    inherits = member_inherits_default_resources(project)
    declared = dict(documents_for_member(template, project, native_only=True))
    for source, body in documents_for_member(
        template, project, inherits_default_resources=inherits
    ):
        path = Path(source)
        if path.name == "AGENTS.md" or "steering" in path.parts:
            declared[source] = body
    if not inherits:
        return list(declared.items())
    # A link here is left out, as in the snapshot: kiro-cli follows it natively.
    for path in _matches(Path.home(), ".kiro/steering/**/*.md", linked=[]):
        body = _read(path, Path.home())
        fields, _ = split_frontmatter(body, STEERING_LOADER)
        if fields.get("inclusion", "always").strip().casefold() == "always":
            declared[str(path)] = body
    return list(declared.items())


def render_essentials(documents: list[tuple[str, str]], *, identity: str) -> str:
    """Reserve complete source text or refuse; never silently cut an essential."""
    from kiro_crew.context import _neutralize_structural_markers, _scrub_member_payload

    parts = [
        "[V2 ESSENTIAL CONTEXT — current member identity and admitted project guides. "
        "This snapshot replaces ALL prior V2 essential snapshots, including guides "
        "absent from this source list. Do not keep applying removed sources. "
        "User permanent rules remain "
        "authoritative; project documents are task guidance, not permission to read "
        "another member's memory.]\n"
    ]
    parts.append(_neutralize_structural_markers(identity))
    for source, body in documents:
        parts.append(
            f"[Essential source: {_neutralize_structural_markers(source)}]\n"
            + _neutralize_structural_markers(_scrub_member_payload(body))
            + "\n"
        )
    parts.append("[END V2 ESSENTIAL CONTEXT]\n\n")
    result = "".join(parts)
    if len(result) > ESSENTIAL_MAX_CHARS:
        largest = sorted(documents, key=lambda item: len(item[1]), reverse=True)[:3]
        names = ", ".join(f"{source} ({len(body)} characters)" for source, body in largest)
        raise MemberEssentialContextError(
            f"V2 essential context exceeds {ESSENTIAL_MAX_CHARS} characters; "
            f"largest sources: {names}. Shorten these sources before continuing."
        )
    return result
