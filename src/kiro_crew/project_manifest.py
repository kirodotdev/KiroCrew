"""Portable Project bundle manifest parsing and creation."""

from __future__ import annotations

import math
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import yaml  # type: ignore[import-untyped]

from kiro_crew import platform_compat
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls

PROJECT_API_VERSION = "crew.kiro/v1"
PROJECT_KIND = "Project"
PROJECT_MANIFEST_NAME = "project.yaml"
PROJECT_MANIFEST_MAX_BYTES = 1024 * 1024
_PROJECT_SOURCE_LIMIT = 256
_CONTEXT_PATH_LIMIT = 256
_SOURCE_CONFIG_MAX_DEPTH = 64
_SOURCE_CONFIG_MAX_NODES = 10_000
_USER_IDENTITY_FIELDS = frozenset(
    {
        "acl",
        "members",
        "membership",
        "memberships",
        "organization",
        "organizations",
        "org",
        "owner",
        "owners",
        "user",
        "users",
    }
)
_SOURCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class ProjectManifestError(ValueError):
    """The Project bundle manifest is missing or invalid."""


@dataclass(frozen=True)
class ProjectSource:
    """One source declaration with its provider-specific configuration."""

    id: str
    type: str
    config: dict[str, Any]


@dataclass(frozen=True)
class ProjectContext:
    """Bundle-relative declarations for capabilities installed with a Project."""

    agents: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    mcp: str = ""


@dataclass(frozen=True)
class ProjectManifest:
    """Validated identity and source declarations from one Project bundle."""

    id: str
    name: str
    description: str
    workspace_source: str
    sources: tuple[ProjectSource, ...]
    context: ProjectContext = ProjectContext()


@dataclass
class _SourceTraversalBudget:
    nodes: int = 0

    def consume(self, *, depth: int, location: str) -> None:
        self.nodes += 1
        if depth > _SOURCE_CONFIG_MAX_DEPTH or self.nodes > _SOURCE_CONFIG_MAX_NODES:
            raise ProjectManifestError(f"{location} is too deep or expands to too many values")


def _required_text(raw: dict[str, Any], key: str, *, location: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProjectManifestError(f"{location} {key} must not be empty")
    return value.strip()


def _parse_project_id(raw: dict[str, Any]) -> str:
    project_id = _required_text(raw, "id", location="project")
    try:
        canonical = str(uuid.UUID(project_id))
    except (ValueError, AttributeError) as exc:
        raise ProjectManifestError("project id must be a canonical UUID") from exc
    if canonical != project_id:
        raise ProjectManifestError("project id must be a canonical UUID")
    return project_id


def _parse_sources(raw: object) -> tuple[ProjectSource, ...]:
    if not isinstance(raw, list):
        raise ProjectManifestError("project sources must be a list")
    if len(raw) > _PROJECT_SOURCE_LIMIT:
        raise ProjectManifestError(
            f"project declares too many sources (max {_PROJECT_SOURCE_LIMIT})"
        )
    sources: list[ProjectSource] = []
    seen: set[str] = set()
    traversal_budget = _SourceTraversalBudget()
    for index, entry in enumerate(raw):
        location = f"source {index + 1}"
        if not isinstance(entry, dict):
            raise ProjectManifestError(f"{location} must be a mapping")
        if any(not isinstance(key, str) for key in entry):
            raise ProjectManifestError(f"{location} configuration keys must be text")
        source_id = _required_text(entry, "id", location=location)
        source_type = _required_text(entry, "type", location=location)
        if not _SOURCE_ID_RE.fullmatch(source_id):
            raise ProjectManifestError(f"{location} id is invalid")
        if source_id in seen:
            raise ProjectManifestError(f"duplicate source id: {source_id}")
        seen.add(source_id)
        config = {
            key: _source_config_value(
                value,
                location=f"{location} configuration {key}",
                traversal_budget=traversal_budget,
            )
            for key, value in entry.items()
            if key not in {"id", "type"}
        }
        if source_type == "repo":
            url = config.get("url")
            if not isinstance(url, str) or not url.strip():
                raise ProjectManifestError(f"{location} url must not be empty")
            default_branch = config.get("default_branch")
            if default_branch is not None and not isinstance(default_branch, str):
                raise ProjectManifestError(f"{location} default_branch must be text")
        sources.append(ProjectSource(id=source_id, type=source_type, config=config))
    return tuple(sources)


def _source_config_value(
    value: object,
    *,
    location: str,
    active_containers: set[int] | None = None,
    traversal_budget: _SourceTraversalBudget | None = None,
    depth: int = 0,
) -> Any:
    """Return a credential-free JSON value for provider-specific source data."""
    budget = traversal_budget if traversal_budget is not None else _SourceTraversalBudget()
    budget.consume(depth=depth, location=location)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ProjectManifestError(f"{location} must be valid JSON")
    if isinstance(value, str):
        redacted, exfiltration = redact_exfiltration_urls(value)
        redacted, credentials = redact_credentials(redacted)
        if exfiltration or credentials or redacted != value:
            raise ProjectManifestError(f"{location} must not contain credentials")
        return value
    active = active_containers if active_containers is not None else set()
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise ProjectManifestError(f"{location} must not contain recursive aliases")
        active.add(identity)
        try:
            return [
                _source_config_value(
                    item,
                    location=f"{location}[{index}]",
                    active_containers=active,
                    traversal_budget=budget,
                    depth=depth + 1,
                )
                for index, item in enumerate(value)
            ]
        finally:
            active.remove(identity)
    if isinstance(value, dict):
        identity = id(value)
        if identity in active:
            raise ProjectManifestError(f"{location} must not contain recursive aliases")
        active.add(identity)
        try:
            result: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ProjectManifestError(f"{location} keys must be text")
                result[key] = _source_config_value(
                    item,
                    location=f"{location}.{key}",
                    active_containers=active,
                    traversal_budget=budget,
                    depth=depth + 1,
                )
            return result
        finally:
            active.remove(identity)
    raise ProjectManifestError(f"{location} must be valid JSON")


def _bundle_relative_path(value: object, *, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectManifestError(f"{location} must be a non-empty bundle-relative path")
    normalized = value.strip().rstrip("/")
    path = Path(normalized)
    if (
        not normalized
        or "\x00" in normalized
        or "\\" in normalized
        # A portable manifest must remain relative on every supported host. A
        # host-native Path check alone accepts `C:/...` on POSIX and `/...` on
        # Windows, allowing the same bundle to escape its root after sharing.
        or PurePosixPath(normalized).is_absolute()
        or PureWindowsPath(normalized).is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ProjectManifestError(f"{location} must be a bundle-relative path")
    return normalized


def _context_paths(raw: object, *, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProjectManifestError(f"context {key} must be a list")
    if len(raw) > _CONTEXT_PATH_LIMIT:
        raise ProjectManifestError(
            f"context {key} declares too many paths (max {_CONTEXT_PATH_LIMIT})"
        )
    paths = tuple(
        _bundle_relative_path(value, location=f"context {key} entry {index + 1}")
        for index, value in enumerate(raw)
    )
    if any("**" in path for path in paths):
        raise ProjectManifestError(f"context {key} cannot use a recursive glob")
    return paths


def _parse_context(raw: object) -> ProjectContext:
    if raw is None:
        return ProjectContext()
    if not isinstance(raw, dict):
        raise ProjectManifestError("project context must be a mapping")
    supported = {"agents", "skills", "mcp"}
    unsupported = sorted(str(key) for key in raw if key not in supported)
    if unsupported:
        raise ProjectManifestError(
            "unsupported project context field(s): " + ", ".join(map(str, unsupported))
        )
    agents_raw = raw.get("agents")
    mcp_raw = raw.get("mcp")
    return ProjectContext(
        agents=_context_paths(agents_raw, key="agents"),
        skills=_context_paths(raw.get("skills"), key="skills"),
        mcp=(_bundle_relative_path(mcp_raw, location="context mcp") if mcp_raw is not None else ""),
    )


def _parse_manifest(raw: object, *, path: Path) -> ProjectManifest:
    if not isinstance(raw, dict):
        raise ProjectManifestError(f"{path} must contain a YAML mapping")
    identity_fields = sorted(_USER_IDENTITY_FIELDS.intersection(raw))
    if identity_fields:
        raise ProjectManifestError(
            "Project bundles must not declare Crew user identity fields: "
            + ", ".join(identity_fields)
        )
    if raw.get("apiVersion") != PROJECT_API_VERSION:
        raise ProjectManifestError(f"unsupported apiVersion: {raw.get('apiVersion')!r}")
    if raw.get("kind") != PROJECT_KIND:
        raise ProjectManifestError("project kind must be Project")
    project_id = _parse_project_id(raw)
    name = _required_text(raw, "name", location="project")
    description_raw = raw.get("description", "")
    if not isinstance(description_raw, str):
        raise ProjectManifestError("project description must be text")
    workspace = raw.get("workspace")
    if not isinstance(workspace, dict):
        raise ProjectManifestError("project workspace must be a mapping")
    workspace_source = _required_text(workspace, "source", location="workspace")
    sources = _parse_sources(raw.get("sources", []))
    context = _parse_context(raw.get("context"))
    source_ids = {source.id for source in sources}
    if workspace_source != "self" and workspace_source not in source_ids:
        raise ProjectManifestError(
            f"workspace source {workspace_source!r} is not declared in project sources"
        )
    workspace_declaration = next(
        (source for source in sources if source.id == workspace_source), None
    )
    if workspace_declaration is not None and workspace_declaration.type != "repo":
        raise ProjectManifestError(f"workspace source {workspace_source!r} must be a repo")
    return ProjectManifest(
        id=project_id,
        name=name,
        description=description_raw,
        workspace_source=workspace_source,
        sources=sources,
        context=context,
    )


def _manifest_path(bundle_dir: str | Path) -> tuple[Path, Path]:
    bundle = Path(bundle_dir).expanduser().resolve()
    return bundle, bundle / PROJECT_MANIFEST_NAME


def _read_manifest_bytes(bundle_dir: str | Path) -> tuple[Path, bytes]:
    bundle, path = _manifest_path(bundle_dir)
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

    try:
        content = safe_read_file_bytes_nolink(
            str(path),
            within_root=str(bundle),
            max_bytes=PROJECT_MANIFEST_MAX_BYTES,
        )
    except FileTooLargeError as exc:
        raise ProjectManifestError(f"cannot read {path}: manifest is too large") from exc
    if content is None:
        raise ProjectManifestError(f"cannot read {path}: manifest must be a regular local file")
    return path, content


def _load_yaml(content: bytes | str, *, path: Path) -> object:
    try:
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        if len(content.encode("utf-8")) > PROJECT_MANIFEST_MAX_BYTES:
            raise ProjectManifestError(f"cannot read {path}: manifest is too large")
        return yaml.safe_load(content)
    except ProjectManifestError:
        raise
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise ProjectManifestError(f"cannot read {path}: {exc}") from exc


def load_project_manifest(bundle_dir: str | Path) -> ProjectManifest:
    """Load the manifest at *bundle_dir* and return its normalized v1 fields."""
    path, content = _read_manifest_bytes(bundle_dir)
    raw = _load_yaml(content, path=path)
    return _parse_manifest(raw, path=path)


def load_project_manifest_text(
    content: str, *, source: str = PROJECT_MANIFEST_NAME
) -> ProjectManifest:
    """Parse manifest text obtained without reading a local bundle directory."""
    path = Path(source)
    raw = _load_yaml(content, path=path)
    return _parse_manifest(raw, path=path)


def _create_manifest_no_clobber(path: Path, rendered: str) -> None:
    """Publish a new manifest atomically without replacing any directory entry."""
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        platform_compat.fchmod_safe(fd, 0o600)
        payload = rendered.encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:  # pragma: no cover - os.write either progresses or raises
                raise OSError("manifest write made no progress")
            offset += written
        os.close(fd)
        fd = -1
        try:
            # A hard-link publish is an atomic create-if-absent operation. Unlike
            # replace(), it treats a dangling symlink as occupied and never clobbers it.
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ProjectManifestError(f"Project manifest already exists: {path}") from exc
        except OSError as exc:
            raise ProjectManifestError(
                f"Project manifest cannot be created safely at {path}"
            ) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except OSError:
            pass


def create_project_manifest(bundle_dir: str | Path, *, name: str) -> ProjectManifest:
    """Create a local Project bundle whose working directory is the bundle itself."""
    if not isinstance(name, str) or not name.strip():
        raise ProjectManifestError("project name must not be empty")
    bundle = Path(bundle_dir).expanduser().resolve()
    if is_sensitive_path(str(bundle)):
        raise ProjectManifestError("Project bundle path is a sensitive path")
    bundle.mkdir(parents=True, exist_ok=True)
    path = bundle / PROJECT_MANIFEST_NAME
    payload = {
        "apiVersion": PROJECT_API_VERSION,
        "kind": PROJECT_KIND,
        "id": str(uuid.uuid4()),
        "name": name.strip(),
        "description": "",
        "workspace": {"source": "self"},
        "sources": [],
        "context": {"agents": [], "skills": []},
    }
    _create_manifest_no_clobber(path, yaml.safe_dump(payload, sort_keys=False))
    return load_project_manifest(bundle)
