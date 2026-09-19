"""Resolve a registered Project into the small attachment used by a session."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kiro_crew.project_git import GitProjectStore, ProjectGitError
from kiro_crew.project_manifest import (
    PROJECT_MANIFEST_NAME,
    ProjectManifest,
    ProjectManifestError,
    ProjectSource,
    load_project_manifest,
)
from kiro_crew.project_registry import ProjectRegistry, ProjectRegistryError, RegisteredProject
from kiro_crew.project_review import (
    compute_review_digest,
    pending_review_sources,
    stale_review_paths,
)
from kiro_crew.security import is_sensitive_path, redact

PROJECT_BRIEF_MAX_CHARS = 4000
PROJECT_REPO_LOOKUP_WORKERS = 4


class ProjectSessionError(ValueError):
    """A registered Project cannot safely back a session."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class _SourceCheckoutStore(Protocol):
    def resolve_source(
        self,
        project_id: str,
        source_id: str,
        *,
        remote: str | None = None,
        default_branch: str = "",
        base_dir: Path | None = None,
    ) -> Path | None: ...


@dataclass(frozen=True)
class ProjectRepository:
    """One declared repository resolved for use from an attached session."""

    source_id: str
    path: Path | None
    is_workspace: bool


@dataclass(frozen=True)
class ProjectAttachment:
    """Stable Project identity plus its resolved workspace and repositories."""

    project_id: str
    name: str
    bundle_dir: Path
    workspace_dir: Path
    repositories: tuple[ProjectRepository, ...]
    brief: str


def _load_registered_bundle(project: RegisteredProject) -> tuple[Path, ProjectManifest]:
    for registration in reversed(project.registrations):
        try:
            bundle_dir = registration.path.resolve()
        except (OSError, RuntimeError):
            continue
        try:
            manifest = load_project_manifest(bundle_dir)
        except ProjectManifestError as exc:
            raise ProjectSessionError(str(exc), code="project_manifest_invalid") from exc
        return bundle_dir, manifest
    raise ProjectSessionError(
        f"Project {project.name} has no readable registered bundle",
        code="project_bundle_unavailable",
    )


def _usable_directory(path: Path) -> Path | None:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    resolved_text = str(resolved)
    # The resolved path is inserted into the trusted session preamble. Refuse
    # line and terminal controls rather than allowing a local directory name to
    # forge a new prompt section outside the screened Project brief.
    if any(not character.isprintable() for character in resolved_text):
        return None
    if not resolved.is_dir() or is_sensitive_path(resolved_text):
        return None
    return resolved


def _resolve_repo(
    project_id: str,
    manifest: ProjectManifest,
    source_id: str,
    source_store: _SourceCheckoutStore,
    *,
    bundle_dir: Path,
    required: bool,
    stale_sources: set[str],
) -> Path | None:
    # Session attachment is deliberately read-only. Repository cloning belongs to
    # add/sync, where the owner reviews the source. Looking up only an existing
    # derived checkout prevents an agent edit to project.yaml from turning a
    # restored session into a fresh outbound request. The lookup carries the
    # CURRENT declaration: a checkout whose provenance record names another
    # remote or branch (a sync repointed the source) is unavailable here, never
    # silently served.
    source = next((entry for entry in manifest.sources if entry.id == source_id), None)
    remote = source.config.get("url") if source is not None else None
    branch = source.config.get("default_branch") if source is not None else None
    try:
        checkout = source_store.resolve_source(
            project_id,
            source_id,
            remote=remote if isinstance(remote, str) else "",
            default_branch=branch.strip() if isinstance(branch, str) else "",
            base_dir=bundle_dir,
        )
    except (ProjectGitError, OSError, RuntimeError) as exc:
        if required:
            raise ProjectSessionError(
                f"Project workspace source {source_id!r} could not be resolved",
                code="project_workspace_unavailable",
            ) from exc
        return None
    resolved = _usable_directory(checkout) if checkout is not None else None
    if resolved is None and required:
        raise ProjectSessionError(
            f"Project workspace source {source_id!r} is unavailable",
            code="project_workspace_unavailable",
        )
    if resolved is not None:
        stale_sources.add(source_id)
    return resolved


def _resolve_repositories(
    project_id: str,
    manifest: ProjectManifest,
    bundle_dir: Path,
    source_store: _SourceCheckoutStore,
) -> tuple[Path, tuple[ProjectRepository, ...], frozenset[str]]:
    stale_sources: set[str] = set()
    workspace_source = None
    if manifest.workspace_source == "self":
        workspace = _usable_directory(bundle_dir)
        if workspace is None:
            raise ProjectSessionError(
                f"Project workspace is unavailable: {bundle_dir}",
                code="project_workspace_unavailable",
            )
    else:
        workspace_source = next(
            (source for source in manifest.sources if source.id == manifest.workspace_source),
            None,
        )
        if workspace_source is None or workspace_source.type != "repo":
            raise ProjectSessionError(
                f"Project workspace source {manifest.workspace_source!r} is unavailable",
                code="project_workspace_unavailable",
            )
        workspace = _resolve_repo(
            project_id,
            manifest,
            workspace_source.id,
            source_store,
            bundle_dir=bundle_dir,
            required=True,
            stale_sources=stale_sources,
        )
        if workspace is None:
            raise ProjectSessionError(
                f"Project workspace source {workspace_source.id!r} is unavailable",
                code="project_workspace_unavailable",
            )

    resolved: dict[str, Path | None] = {}
    if workspace_source is not None:
        resolved[workspace_source.id] = workspace
    pending_sources = [
        source for source in manifest.sources if source.type == "repo" and source.id not in resolved
    ]

    def _resolve_optional(
        source: ProjectSource,
    ) -> tuple[str, Path | None, set[str]]:
        source_stale: set[str] = set()
        path = _resolve_repo(
            project_id,
            manifest,
            source.id,
            source_store,
            bundle_dir=bundle_dir,
            required=False,
            stale_sources=source_stale,
        )
        return source.id, path, source_stale

    if pending_sources:
        workers = min(PROJECT_REPO_LOOKUP_WORKERS, len(pending_sources))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for source_id, path, source_stale in executor.map(_resolve_optional, pending_sources):
                resolved[source_id] = path
                stale_sources.update(source_stale)

    repositories: list[ProjectRepository] = []
    for source in manifest.sources:
        if source.type != "repo":
            continue
        repositories.append(
            ProjectRepository(
                source_id=source.id,
                path=resolved[source.id],
                is_workspace=source.id == manifest.workspace_source,
            )
        )
    return workspace, tuple(repositories), frozenset(stale_sources)


def _build_brief(
    project_id: str,
    manifest: ProjectManifest,
    workspace_dir: Path,
    repositories: tuple[ProjectRepository, ...],
    stale_sources: frozenset[str],
) -> str:
    lines = [
        f"Project: {manifest.name}",
        f"Project id: {project_id}",
        f"Workspace: {manifest.workspace_source} ({workspace_dir})",
    ]
    if repositories:
        lines.extend(("", "Repositories:"))
        for repository in repositories:
            label = (
                f"{repository.source_id} (workspace)"
                if repository.is_workspace
                else repository.source_id
            )
            location = str(repository.path) if repository.path is not None else "unavailable"
            lines.append(f"- {label}: {location}")
    if stale_sources:
        lines.extend(("", "Repository refresh status:"))
        lines.extend(
            f"- {source_id}: using cached checkout; refresh unavailable"
            for source_id in sorted(stale_sources)
        )
    other_sources = [source for source in manifest.sources if source.type != "repo"]
    if other_sources:
        lines.extend(("", "Other sources:"))
        lines.extend(f"- {source.id} ({source.type})" for source in other_sources)
    if manifest.description.strip():
        lines.extend(("", "Description:", manifest.description.strip()))
    return redact("\n".join(lines))[:PROJECT_BRIEF_MAX_CHARS]


def resolve_project_attachment(
    project_id: str,
    *,
    registry: ProjectRegistry | None = None,
    git_store: _SourceCheckoutStore | None = None,
) -> ProjectAttachment:
    """Resolve *project_id* without inferring identity from a directory path."""
    project_registry = registry or ProjectRegistry()
    try:
        project = project_registry.get(project_id)
    except ProjectRegistryError as exc:
        raise ProjectSessionError(str(exc), code="project_not_found") from exc
    bundle_dir, manifest = _load_registered_bundle(project)
    if pending_project_sources(project, bundle_dir, manifest):
        # Checked BEFORE resolution: a pending source has no checkout, so
        # resolving first would report a missing workspace for a Project whose
        # real state is "the manifest naming that source is unaccepted".
        raise ProjectSessionError(
            f"Project {manifest.name} has unreviewed changes in {PROJECT_MANIFEST_NAME}"
            "; re-review it before starting a session",
            code="project_review_stale",
        )
    source_store = git_store or GitProjectStore(project_registry)
    workspace_dir, repositories, stale_sources = _resolve_repositories(
        project.id, manifest, bundle_dir, source_store
    )
    stale_files = review_stale_files(project, bundle_dir, workspace_dir)
    if stale_files:
        # The primary checkout is the cwd kiro-cli reads its own
        # ``.kiro/settings/mcp.json`` and ``.kiro/agents/`` from, so starting a
        # session here would execute definitions the owner has not seen. Both
        # the create path and the per-turn re-resolve come through here, which
        # is why the refusal is one check rather than one per caller.
        raise ProjectSessionError(
            f"Project {manifest.name} has unreviewed changes in "
            + ", ".join(stale_files)
            + "; re-review it before starting a session",
            code="project_review_stale",
        )
    return ProjectAttachment(
        project_id=project.id,
        name=manifest.name,
        bundle_dir=bundle_dir,
        workspace_dir=workspace_dir,
        repositories=repositories,
        brief=_build_brief(project.id, manifest, workspace_dir, repositories, stale_sources),
    )


def review_stale_files(
    project: RegisteredProject, bundle_dir: Path, workspace_dir: Path | None
) -> tuple[str, ...]:
    """Return unaccepted changes and surfaces that cannot be safely reviewed.

    A Project declaring sources has ``project.yaml`` itself in the stale set until
    it is accepted, because the manifest is what authorizes cloning the hosts it
    names. A Project declaring none keeps the original rule: a manifest with
    nothing executable beside it is not a surface the owner has to look at.
    """
    _digest, hashes = compute_review_digest(bundle_dir, workspace_dir)
    declares_sources = bool(load_project_manifest(bundle_dir).sources)
    return stale_review_paths(
        project.reviewed_digest,
        project.reviewed_files,
        hashes,
        require_manifest_review=declares_sources,
    )


def pending_project_sources(
    project: RegisteredProject, bundle_dir: Path, manifest: ProjectManifest
) -> tuple[str, ...]:
    """Declared sources not yet cloned, because their manifest is unaccepted."""
    return pending_review_sources(
        bundle_dir, manifest, project.reviewed_digest, project.reviewed_files
    )


def describe_project_sources(
    project: RegisteredProject,
    *,
    registry: ProjectRegistry | None = None,
    git_store: _SourceCheckoutStore | None = None,
) -> tuple[Path, Path | None, tuple[str, ...]]:
    """Describe a Project's checkouts WITHOUT the review gate.

    Returns the bundle directory, the primary checkout (``None`` when it cannot
    be resolved) and the ids of every declared repo source with no usable
    checkout. Used by the paths that must describe or re-review a Project whose
    state is exactly what a session start would refuse -- health, and the review
    recorder -- so each source is probed on its own and one failure never hides
    the rest.
    """
    project_registry = registry or ProjectRegistry()
    bundle_dir, manifest = _load_registered_bundle(project)
    source_store = git_store or GitProjectStore(project_registry)
    resolved: dict[str, Path | None] = {}
    pending = pending_project_sources(project, bundle_dir, manifest)
    for source in manifest.sources:
        if source.type != "repo":
            continue
        if source.id in pending:
            resolved[source.id] = None
            continue
        remote = source.config.get("url")
        branch = source.config.get("default_branch")
        try:
            checkout = source_store.resolve_source(
                project.id,
                source.id,
                remote=remote if isinstance(remote, str) else "",
                default_branch=branch.strip() if isinstance(branch, str) else "",
                base_dir=bundle_dir,
            )
        except (ProjectGitError, OSError, RuntimeError):
            checkout = None
        resolved[source.id] = _usable_directory(checkout) if checkout is not None else None
    unavailable = tuple(sorted(sid for sid, path in resolved.items() if path is None))
    if manifest.workspace_source == "self":
        primary = _usable_directory(bundle_dir)
    else:
        primary = resolved.get(manifest.workspace_source)
    return bundle_dir, primary, unavailable


def resolve_primary_checkout(
    project: RegisteredProject,
    *,
    registry: ProjectRegistry | None = None,
    git_store: _SourceCheckoutStore | None = None,
) -> tuple[Path, Path | None]:
    """The bundle directory and the primary checkout, without the review gate.

    The primary checkout is the declared ``role: primary`` source's tree when the
    manifest names one, and the bundle clone itself otherwise. It is the session
    workspace AND the root the review digest reads the executable surfaces from,
    so the two can never disagree about which tree was reviewed.
    """
    bundle_dir, primary, _unavailable = describe_project_sources(
        project, registry=registry, git_store=git_store
    )
    return bundle_dir, primary
