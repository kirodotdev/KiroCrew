"""Bounded, complete V2 persona and project documents; no memory search."""

from __future__ import annotations

import fnmatch
import logging
import os
from pathlib import Path

from kiro_crew.agent_sdk.drivers import acp as acp_driver
from kiro_crew.config import KiroCrewConfig, config_dir
from kiro_crew.config.loader import workspace_dir_for
from kiro_crew.config.memory_sections import (
    ESSENTIAL_MAX_CHARS_DEFAULT,
    ESSENTIAL_MAX_CHARS_MAX,
    ESSENTIAL_MAX_CHARS_MIN,
)
from kiro_crew.config.paths import project_agents_dir
from kiro_crew.frontmatter import STEERING_LOADER, split_frontmatter
from kiro_crew.hooks import safe_read_file_bytes_nolink, validate_file_path
from kiro_crew.platform_compat import first_linked_ancestor, is_link_or_junction

logger = logging.getLogger(__name__)

# The default envelope size, and the one a caller that passes no limit renders
# against. A member turn renders against :func:`effective_essential_max_chars`.
ESSENTIAL_MAX_CHARS = ESSENTIAL_MAX_CHARS_DEFAULT
# The per-source READ bound. It bounds what one file read may pull into memory,
# not the prompt, so it stays tied to the default envelope whatever the setting.
_MAX_SOURCE_BYTES = ESSENTIAL_MAX_CHARS * 4
#: The config key a user raises when guides are left out, named in the
#: omission notice and in every over-budget refusal.
ESSENTIAL_MAX_CHARS_SETTING = "memory.essential_max_chars"
#: Source label of the in-band notice that names guides left out of the envelope.
ESSENTIAL_OMISSION_SOURCE = "essential-context#omitted"
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
    """A managed source is excluded from wildcard discovery, never readable."""


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
                    f"Essential source {path}: managed memory/member state cannot be a project resource"
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


def _matches(root: Path, pattern: str) -> list[Path]:
    """Expand a declared glob with bounded directory work and no link traversal."""
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
        return [admitted_root / pattern]
    pending = [(admitted_root, 0)]
    result: set[Path] = set()
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
                    except _ManagedEssentialSourceError:
                        continue
                    if is_link_or_junction(path):
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
    resources) is a guide :func:`fit_essential_documents` may drop whole.
    """
    from kiro_crew.agent import is_managed_prompt
    from kiro_crew.agent_discovery import _read_agent_spec

    documents: list[tuple[str, str]] = []
    seen: set[Path] = set()
    project_root = _admitted_project_root(project)

    def _mark_core(source: str) -> None:
        if core_sources_out is not None:
            core_sources_out.add(source)

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
        for path in _matches(Path.home(), ".kiro/steering/**/*.md"):
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
            for path in _matches(project_root, ".kiro/steering/**/*.md"):
                add(path, project_root, steering=True)

    spec_path = resolve_template_path(template, project)
    if spec_path is None:
        if template != "kirocrew":
            raise MemberEssentialContextError(f"Essential template {template!r}: not found")
        return documents
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
        for match, root in _resource_paths(resources, source_root, absolute_root):
            add(match, root, steering="steering" in match.parts)
    return documents


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
    resources: list[ResourceDeclaration], source_root: Path, absolute_root: Path
) -> list[tuple[Path, Path]]:
    paths: list[tuple[Path, Path]] = []
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
        for match in _matches(root, pattern):
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
    for path in _matches(Path.home(), ".kiro/steering/**/*.md"):
        body = _read(path, Path.home())
        fields, _ = split_frontmatter(body, STEERING_LOADER)
        if fields.get("inclusion", "always").strip().casefold() == "always":
            declared[str(path)] = body
    return list(declared.items())


def effective_essential_max_chars(
    window_tokens: int | None = None, *, configured: int | None = None
) -> int:
    """The essential-envelope size a member turn renders against, in characters.

    The configured ``memory.essential_max_chars`` (*configured* overrides the
    config read), clamped to its declared range, and capped by the session's
    model window: one eighth of the window at four characters per token, never
    below three ordinary context budgets -- the same protected-context ceiling
    ``context_assembly.budget`` derives for the lessons block. A window that is
    unknown (``None``, zero, negative) resolves to the 1M reference window, as
    every other budget does (``budget._effective_window``): the default
    deployment, ``provider=acp`` with ``model="auto"``, resolves no window at a
    fresh session, and capping it lower would make raising the setting a no-op
    exactly where most members run.
    """
    from kiro_crew.context_assembly.budget import _effective_window, _resolve_caps_cached

    if not isinstance(window_tokens, int) or isinstance(window_tokens, bool):
        window_tokens = None
    ceiling = _resolve_caps_cached(_effective_window(window_tokens)).protected_context
    return min(_configured_essential_max_chars(configured), ceiling)


def smallest_essential_max_chars(*, configured: int | None = None) -> int:
    """The smallest envelope any session renders against, in characters.

    The configured size capped at the protected-context floor, which is the
    ceiling of the smallest model window. Profile-save validation knows no
    session, so it uses this: a profile it admits fits every session.
    """
    from kiro_crew.context_assembly.budget import _PROTECTED_CONTEXT_FLOOR

    return min(_configured_essential_max_chars(configured), _PROTECTED_CONTEXT_FLOOR)


def _configured_essential_max_chars(configured: int | None) -> int:
    if configured is None:
        configured = KiroCrewConfig.load().memory.essential_max_chars
    return max(ESSENTIAL_MAX_CHARS_MIN, min(ESSENTIAL_MAX_CHARS_MAX, int(configured)))


_ENVELOPE_HEADER = (
    "[V2 ESSENTIAL CONTEXT — current member identity and admitted project guides. "
    "This snapshot replaces ALL prior V2 essential snapshots, including guides "
    "absent from this source list. Do not keep applying removed sources. "
    "User permanent rules remain "
    "authoritative; project documents are task guidance, not permission to read "
    "another member's memory.]\n"
)
_ENVELOPE_FOOTER = "[END V2 ESSENTIAL CONTEXT]\n\n"


def _document_part(source: str, body: str) -> str:
    """One document exactly as :func:`render_essentials` renders it."""
    from kiro_crew.context import _neutralize_structural_markers, _scrub_member_payload

    return (
        f"[Essential source: {_neutralize_structural_markers(source)}]\n"
        + _neutralize_structural_markers(_scrub_member_payload(body))
        + "\n"
    )


def _over_budget_error(
    documents: list[tuple[str, str]], max_chars: int
) -> MemberEssentialContextError:
    largest = sorted(documents, key=lambda item: len(item[1]), reverse=True)[:3]
    names = ", ".join(f"{source} ({len(body)} characters)" for source, body in largest)
    return MemberEssentialContextError(
        f"V2 essential context exceeds {max_chars} characters; "
        f"largest sources: {names}. Shorten these sources, or raise the "
        f"{ESSENTIAL_MAX_CHARS_SETTING} setting, before continuing."
    )


def render_essentials(
    documents: list[tuple[str, str]], *, identity: str, max_chars: int | None = None
) -> str:
    """Reserve complete source text or refuse; never silently cut an essential.

    *max_chars* is the envelope size to hold it to, :data:`ESSENTIAL_MAX_CHARS`
    when omitted. Fitting an over-budget member envelope is
    :func:`fit_essential_documents`'s job; this renderer only refuses.
    """
    from kiro_crew.context import _neutralize_structural_markers

    cap = ESSENTIAL_MAX_CHARS if max_chars is None else max_chars
    parts = [_ENVELOPE_HEADER, _neutralize_structural_markers(identity)]
    parts.extend(_document_part(source, body) for source, body in documents)
    parts.append(_ENVELOPE_FOOTER)
    result = "".join(parts)
    if len(result) > cap:
        raise _over_budget_error(documents, cap)
    return result


def _listed(dropped: list[tuple[str, str]]) -> str:
    return ", ".join(f"{source} ({len(body):,} characters)" for source, body in dropped)


def _harness_sentence(count: int, listed: str = "") -> str:
    """The notice's sentence for left-out guides the harness itself carries."""
    named = f": {listed}" if listed else ""
    return (
        f"{count} guide document(s) are not repeated in this snapshot because this "
        f"agent's harness loads them itself{named}. They are not removed; keep "
        "applying them."
    )


def _omission_notice(
    dropped: list[tuple[str, str]],
    max_chars: int,
    harness_loaded: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, str]:
    absent = [doc for doc in dropped if doc[0] not in harness_loaded]
    carried = [doc for doc in dropped if doc[0] in harness_loaded]
    parts = []
    if absent:
        parts.append(
            f"ESSENTIAL CONTEXT INCOMPLETE. {len(absent)} guide document(s) for this agent "
            f"were not loaded because the essential context is limited to {max_chars} "
            f"characters: {_listed(absent)}. Do not assume their contents. If one matters "
            "for this task, read the file directly or ask the user. The user can raise the "
            f"limit with the {ESSENTIAL_MAX_CHARS_SETTING} setting."
        )
    if carried:
        prefix = "" if absent else f"ESSENTIAL CONTEXT SHORTENED to {max_chars} characters. "
        parts.append(prefix + _harness_sentence(len(carried), _listed(carried)))
    return (ESSENTIAL_OMISSION_SOURCE, " ".join(parts))


def _minimal_omission_notice(
    dropped: list[tuple[str, str]],
    max_chars: int,
    harness_loaded: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, str]:
    absent = sum(1 for source, _ in dropped if source not in harness_loaded)
    carried = len(dropped) - absent
    parts = []
    if absent:
        parts.append(
            f"ESSENTIAL CONTEXT INCOMPLETE. {absent} guide document(s) for this agent were "
            f"not loaded because the essential context is limited to {max_chars} "
            "characters. Do not assume their contents. The user can raise the limit with "
            f"the {ESSENTIAL_MAX_CHARS_SETTING} setting."
        )
    if carried:
        prefix = "" if absent else f"ESSENTIAL CONTEXT SHORTENED to {max_chars} characters. "
        parts.append(prefix + _harness_sentence(carried))
    return (ESSENTIAL_OMISSION_SOURCE, " ".join(parts))


def fit_essential_documents(
    documents: list[tuple[str, str]],
    *,
    identity: str,
    max_chars: int,
    droppable: set[str] | frozenset[str],
    owner: str = "",
    harness_loaded: set[str] | frozenset[str] = frozenset(),
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """``(documents to render, guides left out)`` for an envelope of *max_chars*.

    An envelope that fits is returned unchanged, so it renders byte-identical to
    one that never needed fitting. Otherwise the documents whose source is in
    *droppable* -- the member's guides, never its core -- are left out WHOLE
    from the tail of declaration order until the rest plus ONE in-band notice
    (:data:`ESSENTIAL_OMISSION_SOURCE`, appended last) fits. The notice's own
    cost is reserved before a guide is admitted, so the notice is the last thing
    to go: a guide that fits only without it is left out too. When every guide
    is out and the full notice, which names each one with its size, still does
    not fit, a minimal notice that only counts them is tried. A core that does
    not fit beside even that refuses, naming its largest sources and the
    setting. Nothing is cut mid-document, and nothing is read here: a source the
    readers refused has already raised before this runs.

    *harness_loaded* names the sources whose exact body the harness already
    carries (kiro-cli loads an agent's declared resources itself). A left-out
    guide named there is reported as not repeated and still in force, never as
    not loaded, so the model keeps applying a document it does hold.

    Mirrors ``context_assembly.member._fit_folder_steering_into_envelope``,
    which fits folder steering into what is left afterwards.
    """
    from kiro_crew.context import _neutralize_structural_markers

    frame = (
        len(_ENVELOPE_HEADER)
        + len(_neutralize_structural_markers(identity))
        + len(_ENVELOPE_FOOTER)
    )
    costs = [len(_document_part(source, body)) for source, body in documents]
    used = frame + sum(costs)
    if used <= max_chars:
        return list(documents), []
    candidates = [i for i, (source, _) in enumerate(documents) if source in droppable]
    dropped: list[int] = []
    notice: tuple[str, str] | None = None
    for index in reversed(candidates):
        used -= costs[index]
        dropped.insert(0, index)
        full = _omission_notice([documents[i] for i in dropped], max_chars, harness_loaded)
        if used + len(_document_part(*full)) <= max_chars:
            notice = full
            break
    if notice is None and dropped:
        minimal = _minimal_omission_notice(
            [documents[i] for i in dropped], max_chars, harness_loaded
        )
        if used + len(_document_part(*minimal)) <= max_chars:
            notice = minimal
    out = set(dropped)
    kept = [doc for i, doc in enumerate(documents) if i not in out]
    if notice is None:
        raise _over_budget_error(kept, max_chars)
    left_out = [documents[i] for i in dropped]
    logger.warning(
        "essential context for member %s is over its %d-character limit; %d guide(s) "
        "not loaded: %s",
        owner,
        max_chars,
        len(left_out),
        ", ".join(f"{source} ({len(body)} characters)" for source, body in left_out),
    )
    return [*kept, notice], left_out
