"""Install-local registrations for portable Project bundles."""

from __future__ import annotations

import json
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.project_manifest import ProjectManifest, load_project_manifest
from kiro_crew.security import is_sensitive_path

PROJECT_REGISTRY_VERSION = 1
PROJECT_REGISTRY_NAME = "registry.json"
PROJECT_REGISTRY_LOCK_NAME = "registry.lock"
PROJECT_REGISTRY_MAX_BYTES = 1024 * 1024

ProjectOrigin = Literal["local", "existing_git", "managed_git"]
_PROJECT_ORIGINS = {"local", "existing_git", "managed_git"}


class ProjectRegistryError(ValueError):
    """The local Project registry is missing requested data or is invalid."""


@dataclass(frozen=True)
class ProjectRegistration:
    """One local materialization of a logical Project."""

    path: Path
    origin: ProjectOrigin
    remote: str = ""


@dataclass(frozen=True)
class RegisteredProject:
    """One logical Project and all of its local materializations."""

    id: str
    name: str
    registrations: tuple[ProjectRegistration, ...]


class ProjectRegistry:
    """Read and atomically update the registry beneath one Kiro Crew data home."""

    def __init__(
        self,
        projects_dir: str | Path | None = None,
        *,
        registry_dir: str | Path | None = None,
    ) -> None:
        if projects_dir is None:
            from kiro_crew.config.paths import config_dir

            home = config_dir()
            projects_dir = home / "projects"
            registry_dir = registry_dir or home / "trust" / "project-registry"
        elif registry_dir is None:
            registry_dir = Path(projects_dir)
        self.projects_dir = Path(projects_dir)
        self.registry_dir = Path(registry_dir)
        self.registry_path = self.registry_dir / PROJECT_REGISTRY_NAME
        self.lock_path = self.registry_dir / PROJECT_REGISTRY_LOCK_NAME

    @contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[None]:
        if platform_compat.is_link_or_junction(self.registry_dir):
            raise ProjectRegistryError("project registry directory must not be a link")
        platform_compat.make_owner_only_dir(self.registry_dir)
        platform_compat.restrict_dir_to_owner(self.registry_dir)
        if platform_compat.is_link_or_junction(self.lock_path):
            raise ProjectRegistryError("project registry lock must not be a link")
        fd = os.open(
            str(self.lock_path),
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ProjectRegistryError("project registry lock must be one regular file")
            with platform_compat.file_lock(fd, exclusive=exclusive, required=True):
                yield
        finally:
            os.close(fd)

    def _read_registry_bytes(self) -> bytes:
        if platform_compat.is_link_or_junction(self.registry_path):
            raise ProjectRegistryError("project registry must not be a link")
        fd = os.open(
            str(self.registry_path),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ProjectRegistryError("project registry must be one regular file")
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                content = handle.read(PROJECT_REGISTRY_MAX_BYTES + 1)
        finally:
            if fd >= 0:
                os.close(fd)
        if len(content) > PROJECT_REGISTRY_MAX_BYTES:
            raise ProjectRegistryError("project registry is too large")
        return content

    def _load_unlocked(self) -> dict[str, RegisteredProject]:
        if not self.registry_path.exists():
            return {}
        try:
            raw = json.loads(self._read_registry_bytes().decode("utf-8"))
        except ProjectRegistryError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectRegistryError(f"cannot read project registry: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") != PROJECT_REGISTRY_VERSION:
            version = raw.get("version") if isinstance(raw, dict) else None
            raise ProjectRegistryError(f"unsupported project registry version: {version!r}")
        raw_projects = raw.get("projects")
        if not isinstance(raw_projects, dict):
            raise ProjectRegistryError("project registry projects must be a mapping")
        return {
            project_id: self._parse_project(project_id, entry)
            for project_id, entry in raw_projects.items()
        }

    @staticmethod
    def _parse_project(project_id: object, raw: object) -> RegisteredProject:
        if not isinstance(project_id, str):
            raise ProjectRegistryError("project registry ids must be text")
        try:
            canonical_id = str(uuid.UUID(project_id))
        except ValueError as exc:
            raise ProjectRegistryError(f"invalid project registry id: {project_id!r}") from exc
        if canonical_id != project_id:
            raise ProjectRegistryError(f"invalid project registry id: {project_id!r}")
        if not isinstance(raw, dict):
            raise ProjectRegistryError(f"project registry entry {project_id} must be a mapping")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProjectRegistryError(f"project registry entry {project_id} has no name")
        raw_registrations = raw.get("registrations")
        if not isinstance(raw_registrations, list) or not raw_registrations:
            raise ProjectRegistryError(f"project registry entry {project_id} has no registrations")
        registrations = tuple(
            ProjectRegistry._parse_registration(project_id, entry) for entry in raw_registrations
        )
        return RegisteredProject(id=project_id, name=name.strip(), registrations=registrations)

    @staticmethod
    def _parse_registration(project_id: str, raw: object) -> ProjectRegistration:
        if not isinstance(raw, dict):
            raise ProjectRegistryError(
                f"project registry registration for {project_id} must be a mapping"
            )
        path = raw.get("path")
        origin = raw.get("origin")
        remote = raw.get("remote", "")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ProjectRegistryError(
                f"project registry registration for {project_id} needs an absolute path"
            )
        if not isinstance(origin, str) or origin not in _PROJECT_ORIGINS:
            raise ProjectRegistryError(
                f"project registry registration for {project_id} has invalid origin"
            )
        if not isinstance(remote, str):
            raise ProjectRegistryError(
                f"project registry registration for {project_id} has invalid remote"
            )
        try:
            normalized = Path(path).resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ProjectRegistryError(
                f"project registry registration for {project_id} cannot be resolved"
            ) from exc
        if is_sensitive_path(str(normalized)):
            raise ProjectRegistryError(
                f"project registry registration for {project_id} uses a sensitive path"
            )
        return ProjectRegistration(path=normalized, origin=origin, remote=remote)  # type: ignore[arg-type]

    def _save_unlocked(self, projects: dict[str, RegisteredProject]) -> None:
        payload: dict[str, Any] = {
            "version": PROJECT_REGISTRY_VERSION,
            "projects": {
                project.id: {
                    "name": project.name,
                    "registrations": [
                        {
                            "path": str(registration.path),
                            "origin": registration.origin,
                            **({"remote": registration.remote} if registration.remote else {}),
                        }
                        for registration in project.registrations
                    ],
                }
                for project in projects.values()
            },
        }
        content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if len(content.encode("utf-8")) > PROJECT_REGISTRY_MAX_BYTES:
            raise ProjectRegistryError("project registry is too large")
        atomic_write(self.registry_path, content, newline="\n", restrict_to_owner=True)

    def _register(
        self,
        manifest: ProjectManifest,
        registration: ProjectRegistration,
        *,
        before_primary_change: (
            Callable[[RegisteredProject, ProjectRegistration], None] | None
        ) = None,
    ) -> RegisteredProject:
        with self._lock(exclusive=True):
            projects = self._load_unlocked()
            current = projects.get(manifest.id)
            registrations = list(current.registrations) if current else []
            if registration not in registrations:
                if current is not None:
                    if before_primary_change is None:
                        raise ProjectRegistryError(
                            "Project is already registered at another path; use the "
                            "capability-aware registration flow"
                        )
                    before_primary_change(current, registration)
                registrations.append(registration)
            project = RegisteredProject(
                id=manifest.id,
                name=manifest.name,
                registrations=tuple(registrations),
            )
            projects[manifest.id] = project
            self._save_unlocked(projects)
            return project

    def add_local(
        self,
        bundle_dir: str | Path,
        *,
        before_primary_change: (
            Callable[[RegisteredProject, ProjectRegistration], None] | None
        ) = None,
    ) -> RegisteredProject:
        """Register a local bundle without taking ownership of its files."""
        bundle = Path(bundle_dir).expanduser().resolve()
        if is_sensitive_path(str(bundle)):
            raise ProjectRegistryError("Project bundle path is a sensitive path")
        manifest = load_project_manifest(bundle)
        origin: ProjectOrigin = "existing_git" if (bundle / ".git").exists() else "local"
        return self._register(
            manifest,
            ProjectRegistration(path=bundle, origin=origin),
            before_primary_change=before_primary_change,
        )

    def add_managed(
        self,
        bundle_dir: str | Path,
        *,
        remote: str,
        before_primary_change: (
            Callable[[RegisteredProject, ProjectRegistration], None] | None
        ) = None,
    ) -> RegisteredProject:
        """Register a Git clone stored beneath this registry's managed directory."""
        bundle = Path(bundle_dir).resolve()
        managed_root = (self.projects_dir / "managed").resolve()
        if bundle == managed_root or managed_root not in bundle.parents:
            raise ProjectRegistryError(f"managed Project clone is outside {managed_root}")
        manifest = load_project_manifest(bundle)
        return self._register(
            manifest,
            ProjectRegistration(path=bundle, origin="managed_git", remote=remote),
            before_primary_change=before_primary_change,
        )

    def list_projects(self) -> tuple[RegisteredProject, ...]:
        """Return logical Projects sorted by display name and stable id."""
        with self._lock(exclusive=False):
            projects = self._load_unlocked()
        return tuple(sorted(projects.values(), key=lambda item: (item.name.casefold(), item.id)))

    def refresh(self, project_id: str) -> RegisteredProject:
        """Refresh registry display metadata from the primary manifest."""
        with self._lock(exclusive=True):
            projects = self._load_unlocked()
            try:
                current = projects[project_id]
            except KeyError as exc:
                raise ProjectRegistryError(f"project is not registered: {project_id}") from exc
            manifest = load_project_manifest(current.registrations[-1].path)
            if manifest.id != project_id:
                raise ProjectRegistryError("project manifest identity changed")
            refreshed = RegisteredProject(
                id=current.id,
                name=manifest.name,
                registrations=current.registrations,
            )
            projects[project_id] = refreshed
            self._save_unlocked(projects)
            return refreshed

    def get(self, project_id: str) -> RegisteredProject:
        """Return a Project by stable id."""
        with self._lock(exclusive=False):
            projects = self._load_unlocked()
        try:
            return projects[project_id]
        except KeyError as exc:
            raise ProjectRegistryError(f"project is not registered: {project_id}") from exc

    def unregister(self, project_id: str) -> RegisteredProject:
        """Forget one Project registration without removing any bundle files."""
        with self._lock(exclusive=True):
            projects = self._load_unlocked()
            try:
                project = projects.pop(project_id)
            except KeyError as exc:
                raise ProjectRegistryError(f"project is not registered: {project_id}") from exc
            self._save_unlocked(projects)
            return project

    def resolve(self, identifier: str) -> RegisteredProject:
        """Resolve a stable id or an unambiguous display name."""
        with self._lock(exclusive=False):
            projects = self._load_unlocked()
        if identifier in projects:
            return projects[identifier]
        matches = [project for project in projects.values() if project.name == identifier]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ProjectRegistryError(
                f"project name {identifier!r} matches multiple projects; use a project id"
            )
        raise ProjectRegistryError(f"project is not registered: {identifier}")
