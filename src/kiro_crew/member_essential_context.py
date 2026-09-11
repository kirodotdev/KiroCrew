"""Bounded, complete V2 persona and project documents; no memory search."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from kiro_crew.config import KiroCrewConfig, config_dir
from kiro_crew.config.loader import workspace_dir_for
from kiro_crew.frontmatter import STEERING_LOADER, split_frontmatter
from kiro_crew.hooks import safe_read_file_bytes_nolink, validate_file_path
from kiro_crew.memory_stores import UnknownMemoryStore, require_member_memory_store
from kiro_crew.platform_compat import first_linked_ancestor, is_link_or_junction

ESSENTIAL_MAX_CHARS = 64_000
_MAX_SOURCE_BYTES = ESSENTIAL_MAX_CHARS * 4
_MAX_DIRECTORY_ENTRIES = 2048
_MAX_DOCUMENTS = 64


class MemberEssentialContextError(ValueError):
    """A declared essential source cannot be included completely and safely."""


def _refuse_managed_source(path: Path) -> None:
    """Resources cannot reopen Global V1 or a peer's managed member state."""
    cfg = KiroCrewConfig.load()
    roots = [config_dir(), Path.home() / ".kiro/crew", Path.home() / ".kirocrew"]
    workspaces = [config_dir() / "workspace"]
    workspaces.extend(workspace_dir_for(name) for name in cfg.workspaces)
    candidate = Path(os.path.abspath(path))
    in_workspace = False
    for workspace in workspaces:
        workspace = Path(os.path.abspath(workspace))
        admin_overlap = False
        for admin in roots:
            admin = Path(os.path.abspath(admin))
            if admin.is_relative_to(workspace):
                admin_overlap = True
            elif workspace.is_relative_to(admin):
                top = workspace.relative_to(admin).parts[0].casefold()
                if top in {
                    "members",
                    "member-rules",
                    "member-memory-bindings",
                    "backups",
                    "trust",
                } or top.startswith(("memory", "lessons")):
                    admin_overlap = True
        if admin_overlap:
            continue
        if candidate.is_relative_to(workspace):
            parts = candidate.relative_to(workspace).parts
            if parts and parts[0].casefold().startswith(("memory", "lessons", ".lessons")):
                raise MemberEssentialContextError(
                    f"Essential source {path}: managed memory/member state cannot be a project resource"
                )
            in_workspace = True
    if not in_workspace and any(
        candidate.is_relative_to(Path(os.path.abspath(root))) for root in roots
    ):
        raise MemberEssentialContextError(
            f"Essential source {path}: managed memory/member state cannot be a project resource"
        )


def member_for_store(store: str | None, claimed_member: str = "") -> tuple[str, str]:
    """Derive identity from the validated owner, preserving every V1 route."""
    if not store or store == "default":
        return "", ""
    cfg = KiroCrewConfig.load()
    record = cfg.memory_stores.get(store)
    if record is None or (
        getattr(record, "memory_version", 1) != 2 and not getattr(record, "owner_member", "")
    ):
        return "", ""
    owner = record.owner_member
    if require_member_memory_store(cfg, owner) != store:
        raise UnknownMemoryStore(f"Private memory {store!r} does not match its member")
    if claimed_member and claimed_member != owner:
        raise UnknownMemoryStore(
            f"Private memory {store!r} belongs to {owner!r}, not {claimed_member!r}"
        )
    return owner, cfg.agents[owner].kiro_agent or "kirocrew"


def _read(path: Path, root: Path) -> str:
    try:
        _refuse_managed_source(path)
        admitted = validate_file_path(str(path))
        if admitted is None or not Path(admitted).is_relative_to(root):
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
    if not any(c in pattern for c in "*?["):
        return [root / pattern]
    pending = [(root, 0)]
    result: set[Path] = set()
    scanned = 0
    visited: set[tuple[Path, int]] = set()
    while pending:
        directory, offset = pending.pop()
        if (directory, offset) in visited:
            continue
        visited.add((directory, offset))
        admitted = validate_file_path(str(directory))
        if admitted is None or not Path(admitted).is_relative_to(root):
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


def documents_for_member(
    template: str, project: str | None, *, include_project: bool = True
) -> list[tuple[str, str]]:
    """Read actual project instructions and the owner's declared template sources.

    Native project steering defaults to always; manual, auto and fileMatch
    documents are deliberately left to their native trigger. Generic product
    prompts keep their existing provider/session-start path.
    """
    from kiro_crew.agent import agent_spec_path
    from kiro_crew.agent_discovery import _read_agent_spec, project_agent_files

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
                return
            if inclusion != "always":
                raise MemberEssentialContextError(
                    f"Essential source {path}: unknown steering inclusion {inclusion!r}"
                )
        seen.add(Path(os.path.abspath(path)))
        documents.append((str(path), body))
        if len(documents) > _MAX_DOCUMENTS:
            raise MemberEssentialContextError(f"Essential source {path}: too many documents")

    if project_root is not None and include_project:
        for name in ("AGENTS.md", "SOUL.md"):
            path = project_root / name
            if path.exists() or path.is_symlink():
                add(path, project_root)
        for path in _matches(project_root, ".kiro/steering/**/*.md"):
            add(path, project_root, steering=True)

    spec_path: Path | None = None
    if project_root is not None:
        for path in project_agent_files(project_root):
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
    if template != "kirocrew" and isinstance(prompt, str) and prompt:
        if prompt.startswith("file://"):
            path = Path(prompt[7:]).expanduser()
            add(
                path if path.is_absolute() else source_root / path,
                absolute_root if path.is_absolute() else source_root,
            )
        else:
            documents.append((f"{spec_path}#prompt", prompt))
    resources = spec.get("resources", [])
    if include_project and (
        not isinstance(resources, list) or any(not isinstance(r, str) for r in resources)
    ):
        raise MemberEssentialContextError(
            f"Essential template {spec_path}: resources must be a list of strings"
        )
    if include_project and isinstance(resources, list):
        if len(resources) > _MAX_DOCUMENTS:
            raise MemberEssentialContextError(f"Essential template {spec_path}: too many resources")
        for resource in resources:
            if not isinstance(resource, str) or not resource.startswith("file://"):
                continue
            path = Path(resource[7:]).expanduser()
            root = absolute_root if path.is_absolute() else source_root
            if path.is_absolute():
                try:
                    pattern = str(path.relative_to(root))
                except ValueError as exc:
                    raise MemberEssentialContextError(
                        f"Essential source {path}: outside {root}"
                    ) from exc
            else:
                pattern = str(path)
            for match in _matches(root, pattern):
                if match.suffix.lower() == ".md":
                    add(match, root, steering="steering" in match.parts)
    return documents


def render_essentials(documents: list[tuple[str, str]], *, identity: str) -> str:
    """Reserve complete source text or refuse; never silently cut an essential."""
    from kiro_crew.context import _neutralize_structural_markers, _scrub_member_payload

    parts = [
        "[V2 ESSENTIAL CONTEXT — current member identity and admitted project guides. "
        "Use the current copy when an older copy differs. User permanent rules remain "
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
