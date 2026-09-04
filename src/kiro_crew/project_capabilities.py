"""Per-install activation of capabilities declared by portable Project bundles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home, kiro_agents_dir
from kiro_crew.project_git import GitProjectStore, ProjectGitError
from kiro_crew.project_manifest import ProjectManifest, ProjectManifestError, load_project_manifest
from kiro_crew.project_registry import ProjectRegistration, ProjectRegistry, RegisteredProject
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

_ACTIVATION_VERSION = 1
_MCP_SERVER_LIMIT = 20
_PROJECT_CAPABILITY_JSON_MAX_BYTES = 1024 * 1024
_PROJECT_CAPABILITY_MATCH_LIMIT = 256
_PROJECT_CAPABILITY_SCAN_ENTRY_LIMIT = 10_000
_PROJECT_SKILL_MAX_TREE_ENTRIES = 10_000
_PROJECT_SKILL_MAX_TREE_BYTES = 32 * 1024 * 1024
_PROJECT_SKILL_MAX_TREE_DEPTH = 64
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_PROJECT_MCP_MARKER = "x-kirocrew-project"
_INVALID_ACTIVATION_RECORD_ERROR = (
    "Project activation record is invalid; repair or remove it manually"
)


def _digest_field(digest: Any, value: bytes) -> None:
    """Hash one length-delimited field so arbitrary file bytes cannot alter framing."""
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def project_mcp_source_changed(before: object, after: object) -> bool:
    """Return whether Project-owned MCP entries changed between two source snapshots."""

    def project_entries(source: object) -> dict[str, dict[str, Any]]:
        if not isinstance(source, dict):
            return {}
        return {
            name: spec
            for name, spec in source.items()
            if isinstance(name, str)
            and isinstance(spec, dict)
            and isinstance(spec.get(_PROJECT_MCP_MARKER), str)
        }

    return project_entries(before) != project_entries(after)


def _remove_stale_mcp_references(config: dict[str, Any], removed: set[str]) -> None:
    """Remove tool references for MCP servers withdrawn from *config*."""
    stale_refs = {f"@{name}" for name in removed}
    for key in ("tools", "allowedTools"):
        refs = config.get(key)
        if isinstance(refs, list):
            config[key] = [ref for ref in refs if ref not in stale_refs]


def reconcile_project_mcp(config: dict[str, Any], source_servers: object) -> None:
    """Reconcile rendered Project MCP provenance with the current source snapshot.

    The caller runs this immediately before committing the rendered agent config.
    That final check closes a deactivation race with a rebuild that loaded the old
    source before withdrawal. A source entry whose marker was removed is reclaimed
    by the user: keep it, but release the rendered provenance marker as well.
    """
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return
    authoritative = source_servers if isinstance(source_servers, dict) else {}
    removed: set[str] = set()
    for name, entry in list(servers.items()):
        if not isinstance(entry, dict) or not isinstance(entry.get(_PROJECT_MCP_MARKER), str):
            continue
        source = authoritative.get(name)
        if not isinstance(source, dict):
            del servers[name]
            removed.add(name)
            continue
        if source.get(_PROJECT_MCP_MARKER) != entry.get(_PROJECT_MCP_MARKER):
            entry.pop(_PROJECT_MCP_MARKER, None)
    if not removed:
        return
    _remove_stale_mcp_references(config, removed)


class ProjectCapabilityError(ValueError):
    """A Project capability declaration cannot be activated safely."""


@dataclass(frozen=True)
class ProjectCapabilityInventory:
    """Counts of capability artifacts declared by one Project."""

    agents: int
    skills: int
    mcp_servers: int
    repos: int


@dataclass(frozen=True)
class ProjectCapabilityStatus:
    """Install-local activation state and the bundle inventory behind it."""

    active: bool
    review_key: str
    inventory: ProjectCapabilityInventory
    repositories: tuple[tuple[str, str], ...] = ()
    agent_names: tuple[str, ...] = ()
    mcp_servers: tuple[tuple[str, dict[str, Any]], ...] = ()


@dataclass(frozen=True)
class _ResolvedBundle:
    project: RegisteredProject
    root: Path
    manifest: ProjectManifest
    agent_payloads: tuple[tuple[Path, bytes], ...]
    skills: tuple[Path, ...]
    mcp_servers: dict[str, dict[str, Any]]
    mcp_payload: bytes | None


def _canonical_directory(path: Path) -> str:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectCapabilityError(f"Project bundle is unavailable: {exc}") from exc
    if not resolved.is_dir():
        raise ProjectCapabilityError("Project bundle is not a directory")
    return str(resolved)


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _assert_destination_unlinked(root: Path, path: Path) -> None:
    """Reject existing links in one install-owned destination path."""
    root = root.absolute()
    candidate = path.absolute()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ProjectCapabilityError(
            "Project capability destination escapes managed storage"
        ) from exc
    current = root
    if platform_compat.is_link_or_junction(current):
        raise ProjectCapabilityError("Project capability destination contains a link or junction")
    for part in relative.parts:
        current = current / part
        if platform_compat.is_link_or_junction(current):
            raise ProjectCapabilityError(
                "Project capability destination contains a link or junction"
            )


def _reject_links(path: Path, root: Path) -> None:
    if platform_compat.is_link_or_junction(path):
        raise ProjectCapabilityError(f"Project capability path is a link: {path}")
    current = path
    while current != root:
        if platform_compat.is_link_or_junction(current):
            raise ProjectCapabilityError(f"Project capability path is a link: {path}")
        current = current.parent
    if path.is_dir():
        entries = 0
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            base = Path(dirpath)
            for name in [*dirnames, *filenames]:
                entries += 1
                if entries > _PROJECT_CAPABILITY_SCAN_ENTRY_LIMIT:
                    raise ProjectCapabilityError(
                        "Project capability tree contains too many entries to inspect safely"
                    )
                candidate = base / name
                if platform_compat.is_link_or_junction(candidate):
                    raise ProjectCapabilityError(
                        f"Project capability tree contains a link: {candidate}"
                    )


def _expand_files(root: Path, patterns: tuple[str, ...], *, suffix: str) -> tuple[Path, ...]:
    found: dict[str, Path] = {}
    for pattern in patterns:
        try:
            matches = root.glob(pattern)
        except (OSError, ValueError) as exc:
            raise ProjectCapabilityError(f"Cannot expand Project capability path: {exc}") from exc
        for path in matches:
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ProjectCapabilityError(
                    f"Cannot resolve Project capability path: {exc}"
                ) from exc
            if not _inside(root, resolved):
                raise ProjectCapabilityError("Project capability path escapes the bundle")
            _reject_links(path, root)
            if path.is_file() and path.name.endswith(suffix):
                found[str(path)] = path
                if len(found) > _PROJECT_CAPABILITY_MATCH_LIMIT:
                    raise ProjectCapabilityError(
                        "Project context resolves to too many capability files"
                    )
    return tuple(found[key] for key in sorted(found))


def _expand_skills(root: Path, patterns: tuple[str, ...]) -> tuple[Path, ...]:
    found: dict[str, Path] = {}
    for pattern in patterns:
        for matched in root.glob(pattern):
            try:
                resolved = matched.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ProjectCapabilityError(f"Cannot resolve Project skill path: {exc}") from exc
            if not _inside(root, resolved):
                raise ProjectCapabilityError("Project skill path escapes the bundle")
            _reject_links(matched, root)
            candidates = [matched] if (matched / "SKILL.md").is_file() else []
            if matched.is_dir():
                candidates.extend(path.parent for path in matched.rglob("SKILL.md"))
            for candidate in candidates:
                _reject_links(candidate, root)
                found[str(candidate)] = candidate
                if len(found) > _PROJECT_CAPABILITY_MATCH_LIMIT:
                    raise ProjectCapabilityError(
                        "Project context resolves to too many skill packages"
                    )
    return tuple(found[key] for key in sorted(found))


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectCapabilityError(f"{label} is not readable JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProjectCapabilityError(f"{label} must be a JSON object")
    return raw


def _read_project_file_bytes(path: Path, root: Path, *, label: str) -> bytes:
    """Read a bounded bundle file from the same handle that passes link checks."""
    if not pinned_fs.supports_pinned_walk():
        from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

        try:
            payload = safe_read_file_bytes_nolink(
                str(path),
                within_root=str(root),
                max_bytes=_PROJECT_CAPABILITY_JSON_MAX_BYTES,
            )
        except FileTooLargeError as exc:
            raise ProjectCapabilityError(f"{label} is too large") from exc
        if payload is None:
            raise ProjectCapabilityError(f"{label} failed its hardened file read")
        return payload
    try:
        resolved_parent = path.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectCapabilityError(f"{label} parent is unavailable: {exc}") from exc
    if not _inside(root, resolved_parent):
        raise ProjectCapabilityError(f"{label} parent escapes the bundle")
    fd: int | None = None
    try:
        fd = pinned_fs.open_in_pinned_parent(
            str(resolved_parent),
            path.name,
            flags=os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            mode=0,
            what=label,
            refusal=ProjectCapabilityError,
        )
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ProjectCapabilityError(f"{label} must be one regular, unlinked file")
        if opened.st_size > _PROJECT_CAPABILITY_JSON_MAX_BYTES:
            raise ProjectCapabilityError(f"{label} is too large")
        with os.fdopen(fd, "rb") as handle:
            fd = None
            payload = handle.read(_PROJECT_CAPABILITY_JSON_MAX_BYTES + 1)
        if len(payload) > _PROJECT_CAPABILITY_JSON_MAX_BYTES:
            raise ProjectCapabilityError(f"{label} is too large")
    except ProjectCapabilityError:
        raise
    except OSError as exc:
        raise ProjectCapabilityError(f"{label} is not readable: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
    return payload


def _decode_project_json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    """Decode a retained bundle-file snapshot as one JSON object."""
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProjectCapabilityError(f"{label} is not readable JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProjectCapabilityError(f"{label} must be a JSON object")
    return raw


def _tree_digest(root: Path) -> str:
    """Hash one materialized skill tree without following mutable path names."""
    if not pinned_fs.supports_pinned_tree_walk():
        raise ProjectCapabilityError(
            "Project skill verification requires descriptor-pinned tree traversal"
        )

    root_fd = pinned_fs.open_dir_pinned(
        root,
        what="Project skill tree",
        refusal=ProjectCapabilityError,
    )
    files: list[tuple[str, ...]] = []
    entries = 0

    def _scan(directory_fd: int, relative: tuple[str, ...]) -> None:
        nonlocal entries
        for name in sorted(os.listdir(directory_fd)):
            entries += 1
            if entries > _PROJECT_SKILL_MAX_TREE_ENTRIES:
                raise ProjectCapabilityError(
                    "Project skill tree contains too many entries to verify safely"
                )
            child_relative = (*relative, name)
            try:
                opened = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise ProjectCapabilityError(
                    "Project skill tree changed while it was being verified"
                ) from exc
            if stat.S_ISLNK(opened.st_mode):
                raise ProjectCapabilityError("Project skill tree contains a link")
            if stat.S_ISREG(opened.st_mode):
                files.append(child_relative)
                continue
            if not stat.S_ISDIR(opened.st_mode):
                raise ProjectCapabilityError("Project skill tree contains a non-regular entry")
            if len(child_relative) > _PROJECT_SKILL_MAX_TREE_DEPTH:
                raise ProjectCapabilityError("Project skill tree is too deeply nested")
            try:
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                raise ProjectCapabilityError(
                    "Project skill tree changed while it was being verified"
                ) from exc
            try:
                _scan(child_fd, child_relative)
            finally:
                os.close(child_fd)

    def _open_file(relative: tuple[str, ...]) -> int:
        directory_fd = os.dup(root_fd)
        try:
            for component in relative[:-1]:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = next_fd
            return os.open(
                relative[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise ProjectCapabilityError(
                "Project skill tree changed while it was being verified"
            ) from exc
        finally:
            os.close(directory_fd)

    digest = hashlib.sha256()
    total_bytes = 0
    try:
        _scan(root_fd, ())
        for relative in sorted(files):
            fd = _open_file(relative)
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    raise ProjectCapabilityError(
                        "Project skill tree contains a linked or non-regular file"
                    )
                remaining = _PROJECT_SKILL_MAX_TREE_BYTES - total_bytes
                if opened.st_size > remaining:
                    raise ProjectCapabilityError(
                        "Project skill tree contains too many bytes to verify safely"
                    )
                _digest_field(digest, "/".join(relative).encode("utf-8"))
                file_digest = hashlib.sha256()
                with os.fdopen(fd, "rb") as handle:
                    fd = -1
                    while True:
                        chunk = handle.read(min(1024 * 1024, remaining + 1))
                        if not chunk:
                            break
                        total_bytes += len(chunk)
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise ProjectCapabilityError(
                                "Project skill tree contains too many bytes to verify safely"
                            )
                        file_digest.update(chunk)
                _digest_field(digest, file_digest.digest())
            finally:
                if fd >= 0:
                    os.close(fd)
    finally:
        os.close(root_fd)
    return digest.hexdigest()


def _capability_review_key(
    bundle: _ResolvedBundle,
    *,
    skill_digests: dict[Path, str] | None = None,
) -> str:
    """Bind an owner's review to the exact declarations and capability bytes."""
    digest = hashlib.sha256()
    _digest_field(digest, b"kiro-crew-project-capabilities-v1")
    root_key = _canonical_directory(bundle.root)
    _digest_field(digest, root_key.encode("utf-8"))
    declaration = {
        "sources": [
            {"id": source.id, "type": source.type, **source.config}
            for source in bundle.manifest.sources
        ],
        "context": {
            "agents": list(bundle.manifest.context.agents),
            "skills": list(bundle.manifest.context.skills),
            "mcp": bundle.manifest.context.mcp,
        },
    }
    _digest_field(
        digest,
        json.dumps(
            declaration,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
    )
    for path, payload in bundle.agent_payloads:
        relative = path.relative_to(bundle.root).as_posix()
        _digest_field(digest, b"agent")
        _digest_field(digest, relative.encode("utf-8"))
        _digest_field(digest, payload)
    for path in bundle.skills:
        relative = path.relative_to(bundle.root).as_posix()
        _digest_field(digest, b"skill")
        _digest_field(digest, relative.encode("utf-8"))
        if pinned_fs.supports_pinned_tree_walk():
            skill_digest = (
                skill_digests[path]
                if skill_digests is not None and path in skill_digests
                else _tree_digest(path)
            )
            _digest_field(digest, skill_digest.encode("ascii"))
        else:
            # Activation already fails closed for skills without a pinned walk.
            # Keep inventory/review rendering available on those platforms.
            _digest_field(digest, b"descriptor-pinned-skill-review-unavailable")
    if bundle.manifest.context.mcp:
        if bundle.mcp_payload is None:
            raise ProjectCapabilityError("Project MCP config snapshot is unavailable")
        _digest_field(digest, b"mcp")
        _digest_field(digest, bundle.manifest.context.mcp.encode("utf-8"))
        _digest_field(digest, bundle.mcp_payload)
    return f"{root_key}#{digest.hexdigest()}"


def _validate_agent(path: Path, payload: bytes) -> dict[str, Any]:
    spec = _decode_project_json_object(payload, label=f"Project agent {path.name}")
    if "hooks" in spec:
        raise ProjectCapabilityError(
            f"Project agent {path.name} cannot declare hooks; hooks stay install-local"
        )
    prompt = spec.get("prompt")
    if isinstance(prompt, str) and prompt.casefold().startswith("file:"):
        raise ProjectCapabilityError(
            f"Project agent {path.name} cannot declare a file-backed prompt"
        )
    resources = spec.get("resources")
    if isinstance(resources, list) and any(
        isinstance(resource, str) and resource.casefold().startswith("file:")
        for resource in resources
    ):
        raise ProjectCapabilityError(
            f"Project agent {path.name} cannot declare file-backed resources"
        )
    if "allowedTools" in spec:
        raise ProjectCapabilityError(
            f"Project agent {path.name} cannot declare allowedTools; grants stay install-local"
        )
    if "toolsSettings" in spec:
        raise ProjectCapabilityError(
            f"Project agent {path.name} cannot declare toolsSettings; grants stay install-local"
        )
    if "mcpServers" in spec:
        raise ProjectCapabilityError(
            f"Project agent {path.name} cannot declare mcpServers; use Project context.mcp"
        )
    name = spec.get("name", path.stem)
    if not isinstance(name, str) or not _SAFE_NAME_RE.fullmatch(name):
        raise ProjectCapabilityError(f"Project agent {path.name} has an invalid name")
    return spec


def _agent_review_name(path: Path, payload: bytes) -> str:
    """Return a bounded display name without making inventory an activation gate."""
    try:
        spec = _decode_project_json_object(payload, label=f"Project agent {path.name}")
    except ProjectCapabilityError:
        return path.stem
    name = spec.get("name")
    return name if isinstance(name, str) and _SAFE_NAME_RE.fullmatch(name) else path.stem


def _mcp_review_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Keep only executable fields that the owner must inspect before activation."""
    review: dict[str, Any] = {}
    command = spec.get("command")
    args = spec.get("args")
    url = spec.get("url")
    if isinstance(command, str):
        review["command"] = command
    if isinstance(args, list) and all(isinstance(argument, str) for argument in args):
        review["args"] = list(args)
    if isinstance(url, str):
        review["url"] = url
    return review


def _credential_free_mcp_url(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ProjectCapabilityError(f"{label} url must be text")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ProjectCapabilityError(f"{label} must use a credential-free http(s) URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProjectCapabilityError(f"{label} must use a credential-free http(s) URL")
    return value


def _review_equivalent_mcp_text(value: str, *, label: str) -> str:
    """Return installable MCP text only when dashboard review preserves it verbatim."""
    cleaned, exfiltration_warnings = redact_exfiltration_urls(value)
    cleaned, credential_warnings = redact_credentials(cleaned)
    if cleaned != value or exfiltration_warnings or credential_warnings:
        raise ProjectCapabilityError(
            f"{label} contains credential or suspicious exfiltration content; "
            "use install-local configuration"
        )
    return value


def _validate_mcp(payload: bytes) -> dict[str, dict[str, Any]]:
    document = _decode_project_json_object(payload, label="Project MCP config")
    raw_servers = document.get("mcpServers")
    if not isinstance(raw_servers, dict):
        raise ProjectCapabilityError("Project MCP config needs an mcpServers object")
    if len(raw_servers) > _MCP_SERVER_LIMIT:
        raise ProjectCapabilityError(
            f"Project MCP config has too many servers (max {_MCP_SERVER_LIMIT})"
        )
    servers: dict[str, dict[str, Any]] = {}
    for name, raw_spec in raw_servers.items():
        if not isinstance(name, str) or not _SAFE_NAME_RE.fullmatch(name):
            raise ProjectCapabilityError("Project MCP config has an invalid server name")
        if not isinstance(raw_spec, dict):
            raise ProjectCapabilityError(f"Project MCP server {name} must be an object")
        if "autoApprove" in raw_spec:
            raise ProjectCapabilityError(f"Project MCP server {name} cannot declare autoApprove")
        if any(key in raw_spec for key in ("env", "headers", "oauth", "clientId")):
            raise ProjectCapabilityError(
                f"Project MCP server {name} is credential-bearing; use install-local configuration"
            )
        has_command = "command" in raw_spec
        has_url = "url" in raw_spec
        if has_command == has_url:
            raise ProjectCapabilityError(
                f"Project MCP server {name} needs exactly one of command or url"
            )
        if has_command:
            command = raw_spec.get("command")
            args = raw_spec.get("args", [])
            if not isinstance(command, str) or not command.strip():
                raise ProjectCapabilityError(
                    f"Project MCP server {name} command must be non-empty text"
                )
            if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
                raise ProjectCapabilityError(
                    f"Project MCP server {name} args must be a list of strings"
                )
            unknown = set(raw_spec) - {"command", "args"}
            if unknown:
                raise ProjectCapabilityError(
                    f"Project MCP server {name} has unsupported field {sorted(unknown)[0]}"
                )
            command = _review_equivalent_mcp_text(
                command.strip(), label=f"Project MCP server {name} command"
            )
            args = [
                _review_equivalent_mcp_text(
                    arg, label=f"Project MCP server {name} argument {index}"
                )
                for index, arg in enumerate(args)
            ]
            servers[name] = {
                "command": command,
                **({"args": args} if args else {}),
            }
            continue
        url = _credential_free_mcp_url(raw_spec.get("url"), label=f"Project MCP server {name}")
        unknown = set(raw_spec) - {"url"}
        if unknown:
            raise ProjectCapabilityError(
                f"Project MCP server {name} has unsupported field {sorted(unknown)[0]}"
            )
        servers[name] = {
            "url": _review_equivalent_mcp_text(url, label=f"Project MCP server {name} url")
        }
    return servers


class ProjectCapabilityManager:
    """Inspect, activate, refresh, and deactivate Project-bundled capabilities."""

    def __init__(
        self,
        registry: ProjectRegistry | None = None,
        *,
        agents_dir: Path | None = None,
        skills_dir: Path | None = None,
        mcp_path: Path | None = None,
        trust_dir: Path | None = None,
    ) -> None:
        self.registry = registry or ProjectRegistry()
        self.agents_dir = agents_dir or kiro_agents_dir()
        home = data_home()
        self.skills_dir = skills_dir or home / "skills"
        self.mcp_path = mcp_path or home / "mcp.json"
        self.trust_dir = trust_dir or home / "trust" / "project-bundles"

    def _state_path(self, project_id: str) -> Path:
        return self.trust_dir / f"{project_id}.json"

    def guard_primary_change(
        self, current: RegisteredProject, _registration: ProjectRegistration
    ) -> None:
        """Refuse a new primary while any installed capability state remains."""
        with self._lock(current.id):
            if self._state_path(current.id).exists():
                raise ProjectCapabilityError(
                    "Deactivate the Project before registering the same Project from "
                    "another path"
                )

    def register_local(self, bundle_dir: str | Path) -> RegisteredProject:
        """Register a local bundle without allowing capabilities to become stale."""
        return self.registry.add_local(
            bundle_dir,
            before_primary_change=self.guard_primary_change,
        )

    def unregister(
        self,
        identifier: str,
        *,
        on_authorized: Callable[[], None] | None = None,
    ) -> RegisteredProject:
        """Withdraw capabilities and unregister one Project as one locked mutation."""
        project = self.registry.resolve(identifier)
        with self._lock(project.id):
            current = self.registry.resolve(project.id)
            state = self._read_state(project.id)
            if state:
                self._verify_materialized(state)
            if on_authorized is not None:
                on_authorized()
            if state:
                self._remove_materialized(state, strict=False)
                self._state_path(project.id).unlink(missing_ok=True)
            GitProjectStore(self.registry).remove_derived_state(current.id)
            return self.registry.unregister(current.id)

    @contextmanager
    def _lock(self, project_id: str) -> Iterator[None]:
        platform_compat.make_owner_only_dir(self.trust_dir)
        platform_compat.restrict_dir_to_owner(self.trust_dir)
        lock_path = self.trust_dir / f"{project_id}.lock"
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with platform_compat.file_lock(fd, exclusive=True, required=True):
                yield
        finally:
            os.close(fd)

    @staticmethod
    def _primary(project: RegisteredProject) -> Path:
        return project.registrations[-1].path

    def _resolve(self, identifier: str, *, validate_executable: bool = False) -> _ResolvedBundle:
        project = self.registry.resolve(identifier)
        root = self._primary(project).resolve()
        try:
            manifest = load_project_manifest(root)
        except ProjectManifestError as exc:
            raise ProjectCapabilityError(str(exc)) from exc
        agents = _expand_files(root, manifest.context.agents, suffix=".json")
        agent_payloads = tuple(
            (
                path,
                _read_project_file_bytes(path, root, label=f"Project agent {path.name}"),
            )
            for path in agents
        )
        skills = _expand_skills(root, manifest.context.skills)
        mcp_servers: dict[str, dict[str, Any]] = {}
        mcp_payload: bytes | None = None
        if manifest.context.mcp:
            mcp_path = root / manifest.context.mcp
            try:
                resolved_mcp = mcp_path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ProjectCapabilityError(f"Project MCP config is unavailable: {exc}") from exc
            if not _inside(root, resolved_mcp):
                raise ProjectCapabilityError("Project MCP config escapes the bundle")
            _reject_links(mcp_path, root)
            mcp_payload = _read_project_file_bytes(
                mcp_path,
                root,
                label="Project MCP config",
            )
            if validate_executable:
                mcp_servers = _validate_mcp(mcp_payload)
            else:
                document = _decode_project_json_object(mcp_payload, label="Project MCP config")
                raw_servers = document.get("mcpServers", {})
                if not isinstance(raw_servers, dict):
                    raise ProjectCapabilityError("Project MCP config needs an mcpServers object")
                mcp_servers = {
                    str(name): dict(spec)
                    for name, spec in raw_servers.items()
                    if isinstance(name, str) and isinstance(spec, dict)
                }
        return _ResolvedBundle(
            project=project,
            root=root,
            manifest=manifest,
            agent_payloads=agent_payloads,
            skills=skills,
            mcp_servers=mcp_servers,
            mcp_payload=mcp_payload,
        )

    def _read_state(self, project_id: str) -> dict[str, Any] | None:
        path = self._state_path(project_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ProjectCapabilityError(
                "Project activation record is unreadable; repair or remove it manually"
            ) from exc
        if (
            not isinstance(data, dict)
            or data.get("version") != _ACTIVATION_VERSION
            or data.get("project_id") != project_id
        ):
            raise ProjectCapabilityError(_INVALID_ACTIVATION_RECORD_ERROR)
        raw_agents = data.get("agents")
        agent_prefix = f"project--{project_id}--"
        if not isinstance(raw_agents, dict) or any(
            not isinstance(raw_path, str)
            or not isinstance(spec, dict)
            or Path(raw_path).parent != self.agents_dir
            or not Path(raw_path).name.startswith(agent_prefix)
            for raw_path, spec in raw_agents.items()
        ):
            raise ProjectCapabilityError(_INVALID_ACTIVATION_RECORD_ERROR)
        raw_skills = data.get("skills_root")
        expected_skills = self.skills_dir / "projects" / project_id
        if not isinstance(raw_skills, str) or (raw_skills and Path(raw_skills) != expected_skills):
            raise ProjectCapabilityError(_INVALID_ACTIVATION_RECORD_ERROR)
        raw_mcp = data.get("mcp")
        mcp_prefix = self._namespace(project_id, "")
        if not isinstance(raw_mcp, dict) or any(
            not isinstance(name, str)
            or not name.startswith(mcp_prefix)
            or not isinstance(spec, dict)
            or spec.get(_PROJECT_MCP_MARKER) != project_id
            for name, spec in raw_mcp.items()
        ):
            raise ProjectCapabilityError(_INVALID_ACTIVATION_RECORD_ERROR)
        raw_repositories = data.get("repositories")
        expected_sources = self.registry.projects_dir / "state" / project_id / "sources"
        if not isinstance(raw_repositories, dict) or any(
            not isinstance(source_id, str)
            or not isinstance(raw_path, str)
            or Path(raw_path).parent != expected_sources
            or Path(raw_path).name != source_id
            for source_id, raw_path in raw_repositories.items()
        ):
            raise ProjectCapabilityError(_INVALID_ACTIVATION_RECORD_ERROR)
        return data

    def _status_unlocked(self, identifier: str) -> ProjectCapabilityStatus:
        bundle = self._resolve(identifier)
        agent_names = tuple(
            _agent_review_name(path, payload) for path, payload in bundle.agent_payloads
        )
        key = _capability_review_key(bundle)
        state = self._read_state(bundle.project.id)
        active = bool(state and state.get("bundle_key") == _canonical_directory(bundle.root))
        if active and state is not None:
            self._verify_materialized(state, presence_only=True)
        raw_repositories = state.get("repositories", {}) if state else {}
        repositories = (
            tuple(
                sorted(
                    (str(source_id), str(path))
                    for source_id, path in raw_repositories.items()
                    if isinstance(source_id, str) and isinstance(path, str)
                )
            )
            if isinstance(raw_repositories, dict)
            else ()
        )
        return ProjectCapabilityStatus(
            active=active,
            review_key=key,
            inventory=ProjectCapabilityInventory(
                agents=len(bundle.agent_payloads),
                skills=len(bundle.skills),
                mcp_servers=len(bundle.mcp_servers),
                repos=sum(source.type == "repo" for source in bundle.manifest.sources),
            ),
            repositories=repositories,
            agent_names=agent_names,
            mcp_servers=tuple(
                (name, _mcp_review_spec(spec))
                for name, spec in sorted(bundle.mcp_servers.items())[:_MCP_SERVER_LIMIT]
            ),
        )

    def status(self, identifier: str) -> ProjectCapabilityStatus:
        project_id = self.registry.resolve(identifier).id
        with self._lock(project_id):
            return self._status_unlocked(project_id)

    @staticmethod
    def _namespace(project_id: str, name: str) -> str:
        return f"project-{project_id}-{name}"

    def _read_mcp_store(self) -> dict[str, Any]:
        if not self.mcp_path.exists():
            return {"mcpServers": {}}
        data = _read_json_object(self.mcp_path, label="install MCP config")
        if "mcpServers" not in data:
            data["mcpServers"] = {}
        if not isinstance(data["mcpServers"], dict):
            raise ProjectCapabilityError("install MCP config mcpServers must be an object")
        return data

    def _write_mcp_store(self, data: dict[str, Any]) -> None:
        platform_compat.make_owner_only_dir(self.mcp_path.parent)
        atomic_write(
            self.mcp_path,
            (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            restrict_to_owner=True,
        )

    def _verify_materialized(
        self,
        state: dict[str, Any],
        *,
        presence_only: bool = False,
    ) -> None:
        raw_agents = state.get("agents", {})
        if isinstance(raw_agents, dict):
            for raw_path, expected in raw_agents.items():
                if not isinstance(raw_path, str) or not isinstance(expected, dict):
                    continue
                path = Path(raw_path)
                if not path.exists():
                    raise ProjectCapabilityError(
                        f"Project agent {path.name} is missing; deactivate and activate again"
                    )
                if presence_only:
                    continue
                if platform_compat.is_link_or_junction(path):
                    raise ProjectCapabilityError(
                        f"Project agent {path.name} was replaced by a link; remove it manually"
                    )
                if _read_json_object(path, label="activated Project agent") != expected:
                    raise ProjectCapabilityError(
                        f"Project agent {path.name} was modified locally; remove it manually"
                    )
        raw_skills = state.get("skills_root")
        expected_digest = state.get("skills_digest")
        if raw_skills and isinstance(raw_skills, str) and isinstance(expected_digest, str):
            skills_root = Path(raw_skills)
            if not skills_root.exists():
                raise ProjectCapabilityError(
                    "Project skills are missing; deactivate and activate again"
                )
            if not presence_only and _tree_digest(skills_root) != expected_digest:
                raise ProjectCapabilityError(
                    "Project skills were modified locally; remove them manually"
                )
        tracked_mcp = state.get("mcp")
        if isinstance(tracked_mcp, dict):
            from kiro_crew.apps.bridges import _mcp_lock

            with _mcp_lock(target=self.mcp_path):
                servers = self._read_mcp_store()["mcpServers"]
                for name, spec in tracked_mcp.items():
                    if not isinstance(name, str) or not isinstance(spec, dict):
                        raise ProjectCapabilityError(
                            "Project activation record has invalid MCP state"
                        )
                    reclaimed = dict(spec)
                    reclaimed.pop(_PROJECT_MCP_MARKER, None)
                    if name not in servers:
                        raise ProjectCapabilityError(
                            f"Project MCP server {name} is missing; deactivate and activate again"
                        )
                    if not presence_only and servers[name] not in (spec, reclaimed):
                        raise ProjectCapabilityError(
                            f"Project MCP server {name} was modified locally; remove it manually"
                        )

    def _remove_materialized(
        self,
        state: dict[str, Any],
        *,
        strict: bool,
        revoke_modified: bool = False,
    ) -> None:
        if strict:
            self._verify_materialized(state)
        raw_agents = state.get("agents", {})
        if isinstance(raw_agents, list):
            agent_paths = raw_agents
        elif isinstance(raw_agents, dict):
            agent_paths = list(raw_agents)
        else:
            agent_paths = []
        for raw_path in agent_paths:
            if not isinstance(raw_path, str):
                continue
            path = Path(raw_path)
            if path.parent == self.agents_dir and path.name.startswith("project--"):
                path.unlink(missing_ok=True)
        raw_skills = state.get("skills_root")
        if isinstance(raw_skills, str):
            path = Path(raw_skills)
            expected_parent = self.skills_dir / "projects"
            if path.parent == expected_parent and path.name == state.get("project_id"):
                if revoke_modified and platform_compat.is_link_or_junction(path):
                    platform_compat.unlink_link_or_junction(path)
                else:
                    shutil.rmtree(path, ignore_errors=not strict)
        tracked_mcp = state.get("mcp")
        if isinstance(tracked_mcp, dict):
            from kiro_crew.apps.bridges import _mcp_lock

            with _mcp_lock(target=self.mcp_path):
                data = self._read_mcp_store()
                servers = data["mcpServers"]
                for name, spec in tracked_mcp.items():
                    if name not in servers:
                        continue
                    current = servers[name]
                    if current == spec or (
                        revoke_modified
                        and isinstance(current, dict)
                        and isinstance(current.get(_PROJECT_MCP_MARKER), str)
                    ):
                        del servers[name]
                self._write_mcp_store(data)
            self._remove_rendered_mcp(str(state.get("project_id", "")), tracked_mcp)

    def _remove_rendered_mcp(self, project_id: str, tracked_mcp: dict[str, Any]) -> None:
        """Remove only provenance-marked Project servers from the rendered agent."""
        agent_path = self.agents_dir / AGENT_FILENAME
        if not agent_path.exists():
            return
        from kiro_crew.apps.bridges import _mcp_lock

        with _mcp_lock(target=agent_path):
            data = _read_json_object(agent_path, label="installed agent config")
            servers = data.get("mcpServers")
            if not isinstance(servers, dict):
                raise ProjectCapabilityError("installed agent config mcpServers must be an object")
            removed: set[str] = set()
            for name in tracked_mcp:
                entry = servers.get(name)
                if isinstance(entry, dict) and entry.get(_PROJECT_MCP_MARKER) == project_id:
                    del servers[name]
                    removed.add(name)
            if not removed:
                return
            _remove_stale_mcp_references(data, removed)
            atomic_write(
                agent_path,
                (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                restrict_to_owner=True,
            )

    def _materialize_repositories(self, bundle: _ResolvedBundle) -> dict[str, str]:
        repositories: dict[str, str] = {}
        store = GitProjectStore(self.registry)
        for repo_source in bundle.manifest.sources:
            if repo_source.type != "repo":
                continue
            remote = repo_source.config.get("url")
            if not isinstance(remote, str) or not remote.strip():
                raise ProjectCapabilityError(f"Project repo source {repo_source.id} needs a URL")
            try:
                branch = repo_source.config.get("default_branch")
                checkout = store.materialize_source(
                    bundle.project.id,
                    repo_source.id,
                    remote.strip(),
                    branch.strip() if isinstance(branch, str) else "",
                    base_dir=bundle.root,
                )
            except ProjectGitError as exc:
                raise ProjectCapabilityError(str(exc)) from exc
            repositories[repo_source.id] = str(checkout)
        return repositories

    def activate(
        self,
        identifier: str,
        *,
        expected_key: object,
        on_authorized: Callable[[], None] | None = None,
        _require_active: bool = False,
    ) -> ProjectCapabilityStatus:
        project_id = self.registry.resolve(identifier).id
        with self._lock(project_id):
            previous = self._read_state(project_id)
            if _require_active and not previous:
                return self._status_unlocked(project_id)
            bundle = self._resolve(project_id, validate_executable=True)
            bundle_key = _canonical_directory(bundle.root)
            project_skills_root = self.skills_dir / "projects" / project_id
            if bundle.skills:
                _assert_destination_unlinked(self.skills_dir, project_skills_root)
            validated_agents = [
                (path, _validate_agent(path, payload)) for path, payload in bundle.agent_payloads
            ]
            skill_digests = {
                path: _tree_digest(path)
                for path in bundle.skills
                if pinned_fs.supports_pinned_tree_walk()
            }
            key = _capability_review_key(bundle, skill_digests=skill_digests)
            if not isinstance(expected_key, str) or expected_key != key:
                raise ProjectCapabilityError(
                    "Project bundle changed after review; inspect it again"
                )
            if on_authorized is not None:
                on_authorized()
            if (
                not _require_active
                and previous
                and previous.get("review_key") == key
                and previous.get("bundle_key") == bundle_key
            ):
                self._verify_materialized(previous)
                return self._status_unlocked(project_id)
            if _require_active and previous:
                self._verify_materialized(previous)
                refreshed_repositories = self._materialize_repositories(bundle)
                refreshed = {**previous, "repositories": refreshed_repositories}
                atomic_write(
                    self._state_path(project_id),
                    (json.dumps(refreshed, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                    restrict_to_owner=True,
                )
                return self._status_unlocked(project_id)
            if previous:
                self._remove_materialized(previous, strict=True)
                self._state_path(project_id).unlink(missing_ok=True)
            created_agents: dict[str, dict[str, Any]] = {}
            skills_created = False
            installed_mcp: dict[str, dict[str, Any]] = {}
            repositories: dict[str, str] = {}
            try:
                repositories = self._materialize_repositories(bundle)

                self.agents_dir.mkdir(parents=True, exist_ok=True)
                seen_agent_names: set[str] = set()
                for agent_source, spec in validated_agents:
                    source_name = str(spec.get("name", agent_source.stem))
                    installed_name = self._namespace(project_id, source_name)
                    if installed_name in seen_agent_names:
                        raise ProjectCapabilityError(
                            f"Project declares duplicate agent name {source_name}"
                        )
                    seen_agent_names.add(installed_name)
                    rendered = dict(spec)
                    rendered["name"] = installed_name
                    rendered["includeMcpJson"] = False
                    destination = (
                        self.agents_dir / f"project--{project_id}--{agent_source.stem}.json"
                    )
                    if destination.exists():
                        raise ProjectCapabilityError(
                            f"Project agent destination already exists: {destination.name}"
                        )
                    atomic_write(
                        destination,
                        (json.dumps(rendered, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                    )
                    created_agents[str(destination)] = rendered

                if bundle.skills:
                    _assert_destination_unlinked(self.skills_dir, project_skills_root)
                    project_skills_root.parent.mkdir(parents=True, exist_ok=True)
                    _assert_destination_unlinked(self.skills_dir, project_skills_root)
                    if project_skills_root.exists():
                        raise ProjectCapabilityError(
                            "Project skill destination already exists without an activation record"
                        )
                    staging = Path(
                        tempfile.mkdtemp(prefix=f".{project_id}-", dir=project_skills_root.parent)
                    )
                    try:
                        seen_skill_names: set[str] = set()
                        for skill_source in bundle.skills:
                            name = skill_source.name
                            if name in seen_skill_names:
                                raise ProjectCapabilityError(
                                    f"Project declares duplicate skill {name}"
                                )
                            seen_skill_names.add(name)
                            pinned_fs.stage_tree_pinned(
                                skill_source,
                                staging / name,
                                what=f"Project skill {name!r}",
                                on_skip=pinned_fs.fatal_skip_reporter(
                                    f"activation of Project skill {name!r}",
                                    refusal=ProjectCapabilityError,
                                ),
                                must_create=True,
                                max_entries=_PROJECT_SKILL_MAX_TREE_ENTRIES,
                                max_bytes=_PROJECT_SKILL_MAX_TREE_BYTES,
                                refusal=ProjectCapabilityError,
                            )
                            if _tree_digest(staging / name) != skill_digests[skill_source]:
                                raise ProjectCapabilityError(
                                    "Project skill changed after review; inspect it again"
                                )
                        _assert_destination_unlinked(self.skills_dir, project_skills_root)
                        os.replace(staging, project_skills_root)
                        skills_created = True
                    finally:
                        if staging.exists():
                            shutil.rmtree(staging, ignore_errors=True)

                from kiro_crew.apps.bridges import _mcp_lock

                with _mcp_lock(target=self.mcp_path):
                    data = self._read_mcp_store()
                    servers = data["mcpServers"]
                    for name, spec in bundle.mcp_servers.items():
                        installed_name = self._namespace(project_id, name)
                        if installed_name in servers:
                            raise ProjectCapabilityError(
                                f"Project MCP server name collides with {installed_name}"
                            )
                        rendered_spec = {**spec, _PROJECT_MCP_MARKER: project_id}
                        servers[installed_name] = rendered_spec
                        installed_mcp[installed_name] = rendered_spec
                    self._write_mcp_store(data)

                state = {
                    "version": _ACTIVATION_VERSION,
                    "project_id": project_id,
                    "bundle_key": bundle_key,
                    "review_key": key,
                    "agents": created_agents,
                    "skills_root": str(project_skills_root) if skills_created else "",
                    "skills_digest": _tree_digest(project_skills_root) if skills_created else "",
                    "mcp": installed_mcp,
                    "repositories": repositories,
                }
                atomic_write(
                    self._state_path(project_id),
                    (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                    restrict_to_owner=True,
                )
            except Exception:
                rollback = {
                    "project_id": project_id,
                    "agents": created_agents,
                    "skills_root": str(project_skills_root) if skills_created else "",
                    "mcp": installed_mcp,
                }
                self._remove_materialized(rollback, strict=False)
                self._state_path(project_id).unlink(missing_ok=True)
                raise
            return self._status_unlocked(project_id)

    def deactivate(
        self,
        identifier: str,
        *,
        on_authorized: Callable[[], None] | None = None,
    ) -> ProjectCapabilityStatus:
        project = self.registry.resolve(identifier)
        self.withdraw(project.id, on_authorized=on_authorized)
        return self.status(project.id)

    def withdraw(
        self,
        identifier: str,
        *,
        on_authorized: Callable[[], None] | None = None,
    ) -> None:
        """Remove tracked capabilities without requiring a readable manifest."""
        project = self.registry.resolve(identifier)
        with self._lock(project.id):
            state = self._read_state(project.id)
            if state:
                self._verify_materialized(state)
            if on_authorized is not None:
                on_authorized()
            if state:
                self._remove_materialized(state, strict=False)
                self._state_path(project.id).unlink(missing_ok=True)

    def refresh_if_active(self, identifier: str) -> ProjectCapabilityStatus:
        project = self.registry.resolve(identifier)
        with self._lock(project.id):
            state = self._read_state(project.id)
            try:
                status = self._status_unlocked(project.id)
            except ProjectCapabilityError:
                if state:
                    self._remove_materialized(
                        state,
                        strict=False,
                        revoke_modified=True,
                    )
                    self._state_path(project.id).unlink(missing_ok=True)
                raise
            if not state:
                return status
            if not status.active or state.get("review_key") != status.review_key:
                # A changed trust digest is a revocation boundary, not user-requested
                # cleanup. Local edits cannot keep that revoked capability set active;
                # _read_state has already confined every removal to this Project.
                self._remove_materialized(
                    state,
                    strict=False,
                    revoke_modified=True,
                )
                self._state_path(project.id).unlink(missing_ok=True)
                return self._status_unlocked(project.id)
        return self.activate(
            identifier,
            expected_key=status.review_key,
            _require_active=True,
        )
