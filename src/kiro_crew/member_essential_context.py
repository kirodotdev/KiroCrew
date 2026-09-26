"""Bounded, complete V2 persona and project documents; no memory search."""

from __future__ import annotations

import fnmatch
import logging
import os
from pathlib import Path

from kiro_crew.config import KiroCrewConfig, config_dir
from kiro_crew.config.loader import workspace_dir_for
from kiro_crew.config.paths import default_work_dir, project_agents_dir
from kiro_crew.frontmatter import STEERING_LOADER, split_frontmatter
from kiro_crew.hooks import safe_read_file_bytes_nolink, validate_file_path
from kiro_crew.platform_compat import first_linked_ancestor, is_link_or_junction

logger = logging.getLogger(__name__)

ESSENTIAL_MAX_CHARS = 64_000
_MAX_SOURCE_BYTES = ESSENTIAL_MAX_CHARS * 4
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
    workspaces = [default_work_dir(config_dir())]
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


def documents_for_member(
    template: str,
    project: str | None,
    *,
    include_project: bool = True,
    native_only: bool = False,
    conditional_index: bool = False,
    context_settings: bool = False,
    trigger_text: str = "",
) -> list[tuple[str, str]]:
    """Read actual project instructions and the owner's declared template sources.

    Native project steering defaults to always; manual, auto and fileMatch
    documents are deliberately left to their native trigger. Generic product
    prompts keep their existing provider/session-start path.
    """
    from kiro_crew.agent import is_managed_prompt
    from kiro_crew.agent_discovery import _read_agent_spec

    documents: list[tuple[str, str]] = []
    seen: set[Path] = set()
    project_root = None
    if project:
        admitted = validate_file_path(project)
        if admitted is None:
            raise MemberEssentialContextError(f"Essential project {project}: cannot be read safely")
        project_root = Path(admitted)
        _refuse_managed_source(project_root)

    def add(path: Path, root: Path, *, steering: bool = False) -> None:
        if Path(os.path.abspath(path)) in seen:
            return
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

    if include_project and not native_only:
        for path in _matches(Path.home(), ".kiro/steering/**/*.md"):
            add(path, Path.home(), steering=True)

    if project_root is not None and include_project and not native_only:
        for name in ("AGENTS.md", "SOUL.md"):
            path = project_root / name
            if path.exists() or path.is_symlink():
                add(path, project_root)
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
            else:
                resolved = resolve_relative_prompt_path(path, spec_path, project)
                if resolved is not None:
                    add(*resolved)
        else:
            documents.append((f"{spec_path}#prompt", prompt))
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
    version, so this responsibility includes only default/always steering.
    """
    declared = dict(documents_for_member(template, project, native_only=True))
    for source, body in documents_for_member(template, project):
        path = Path(source)
        if path.name == "AGENTS.md" or "steering" in path.parts:
            declared[source] = body
    for path in _matches(Path.home(), ".kiro/steering/**/*.md"):
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
