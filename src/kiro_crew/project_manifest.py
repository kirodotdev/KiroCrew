"""Portable Project bundle manifest parsing and creation.

The manifest is the small, human-authored, credential-free declaration of a
Project's intent: its name, description and repositories. Install-local
identity is assigned by the registry, never carried in the bundle.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from kiro_crew import platform_compat
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls

PROJECT_API_VERSION = "crew.kiro/v1"
PROJECT_KIND = "Project"
PROJECT_MANIFEST_NAME = "project.yaml"
PROJECT_MANIFEST_MAX_BYTES = 1024 * 1024
_PROJECT_SOURCE_LIMIT = 256
_SOURCE_ROLES = frozenset({"primary", "reference"})
# Only the built-in provider ships in v1. A manifest naming another type is a
# validation error today rather than a silently-ignored source.
_SUPPORTED_SOURCE_TYPES = frozenset({"repo"})
_TOP_LEVEL_KEYS = frozenset({"apiVersion", "kind", "name", "description", "sources"})
_REPO_SOURCE_KEYS = frozenset({"type", "url", "default_branch", "role"})
_SOURCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SELF_WORKSPACE = "self"


class ProjectManifestError(ValueError):
    """The Project bundle manifest is missing or invalid."""


@dataclass(frozen=True)
class ProjectSource:
    """One repo source declaration, keyed by a synthesized install-stable id."""

    id: str
    type: str
    config: dict[str, Any]

    @property
    def role(self) -> str:
        role = self.config.get("role")
        return role if isinstance(role, str) else "reference"


@dataclass(frozen=True)
class ProjectManifest:
    """Validated intent from one thin Project bundle."""

    name: str
    description: str
    workspace_source: str
    sources: tuple[ProjectSource, ...]


def _required_text(raw: dict[str, Any], key: str, *, location: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProjectManifestError(f"{location} {key} must not be empty")
    return value.strip()


def _reject_unknown_keys(raw: dict[str, Any], allowed: frozenset[str], *, location: str) -> None:
    if any(not isinstance(key, str) for key in raw):
        raise ProjectManifestError(f"{location} keys must be text")
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ProjectManifestError(f"unsupported {location} field(s): " + ", ".join(unknown))


def _synthesize_source_id(url: str) -> str:
    """Derive a stable, reorder-proof id for a repo source from its URL.

    The manifest does not declare a per-source id; the derived checkout under
    ``state/<project_id>/sources/<source_id>/`` is keyed by this instead, so it
    stays put across syncs and manifest reorders, and its provenance record is
    still what decides reuse-vs-reclone when the URL changes.
    """
    trimmed = url.strip().rstrip("/")
    tail = trimmed.rsplit("/", 1)[-1] or trimmed.rsplit(":", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", tail).strip("-.") or "repo"
    digest = hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:8]
    candidate = f"{slug}-{digest}"
    if not _SOURCE_ID_RE.fullmatch(candidate):
        candidate = f"repo-{digest}"
    return candidate


def _parse_sources(raw: object) -> tuple[ProjectSource, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProjectManifestError("project sources must be a list")
    if len(raw) > _PROJECT_SOURCE_LIMIT:
        raise ProjectManifestError(
            f"project declares too many sources (max {_PROJECT_SOURCE_LIMIT})"
        )
    sources: list[ProjectSource] = []
    seen: set[str] = set()
    primary_seen = False
    for index, entry in enumerate(raw):
        location = f"source {index + 1}"
        if not isinstance(entry, dict):
            raise ProjectManifestError(f"{location} must be a mapping")
        source_type = _required_text(entry, "type", location=location)
        if source_type not in _SUPPORTED_SOURCE_TYPES:
            raise ProjectManifestError(
                f"{location} type {source_type!r} is not supported (only 'repo' in v1)"
            )
        _reject_unknown_keys(entry, _REPO_SOURCE_KEYS, location=location)
        url = _required_text(entry, "url", location=location)
        redacted_url, exfiltration = redact_exfiltration_urls(url)
        redacted_url, credentials = redact_credentials(redacted_url)
        if exfiltration or credentials or redacted_url != url:
            raise ProjectManifestError(f"{location} url must not contain credentials")
        config: dict[str, Any] = {"url": url}
        default_branch = entry.get("default_branch")
        if default_branch is not None:
            if not isinstance(default_branch, str):
                raise ProjectManifestError(f"{location} default_branch must be text")
            config["default_branch"] = default_branch
        role = entry.get("role", "reference")
        if not isinstance(role, str) or role not in _SOURCE_ROLES:
            raise ProjectManifestError(f"{location} role must be 'primary' or 'reference'")
        if role == "primary":
            if primary_seen:
                raise ProjectManifestError("project declares more than one primary source")
            primary_seen = True
        config["role"] = role
        source_id = _synthesize_source_id(url)
        if source_id in seen:
            raise ProjectManifestError(f"duplicate source url resolves to {source_id}")
        seen.add(source_id)
        sources.append(ProjectSource(id=source_id, type=source_type, config=config))
    return tuple(sources)


def _parse_manifest(raw: object, *, path: Path) -> ProjectManifest:
    if not isinstance(raw, dict):
        raise ProjectManifestError(f"{path} must contain a YAML mapping")
    _reject_unknown_keys(raw, _TOP_LEVEL_KEYS, location="project")
    if raw.get("apiVersion") != PROJECT_API_VERSION:
        raise ProjectManifestError(f"unsupported apiVersion: {raw.get('apiVersion')!r}")
    if raw.get("kind") != PROJECT_KIND:
        raise ProjectManifestError("project kind must be Project")
    name = _required_text(raw, "name", location="project")
    description_raw = raw.get("description", "")
    if not isinstance(description_raw, str):
        raise ProjectManifestError("project description must be text")
    sources = _parse_sources(raw.get("sources"))
    # The primary repo is the session's project directory. With no sources the
    # bundle directory itself is the workspace ("self"), which is how a local
    # bundle with nothing declared yet still attaches.
    primary = next((source for source in sources if source.role == "primary"), None)
    if primary is not None:
        workspace_source = primary.id
    elif not sources:
        workspace_source = _SELF_WORKSPACE
    elif len(sources) == 1:
        workspace_source = sources[0].id
    else:
        raise ProjectManifestError("project declares multiple sources but none has role: primary")
    return ProjectManifest(
        name=name,
        description=description_raw,
        workspace_source=workspace_source,
        sources=sources,
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
        "name": name.strip(),
        "description": "",
        "sources": [],
    }
    _create_manifest_no_clobber(path, yaml.safe_dump(payload, sort_keys=False))
    return load_project_manifest(bundle)
