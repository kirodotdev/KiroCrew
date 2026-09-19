"""Thin Project registry and read-only session attachment (no Git required)."""

from __future__ import annotations

from pathlib import Path

import pytest
from project_git_helpers import local_git_remote, requires_local_git_remote  # noqa: F401

from conftest import make_dir_link
from kiro_crew.project_manifest import create_project_manifest
from kiro_crew.project_registry import ProjectRegistry, ProjectRegistryError
from kiro_crew.project_sessions import (
    ProjectSessionError,
    resolve_project_attachment,
)


def _registry(tmp_path: Path) -> ProjectRegistry:
    return ProjectRegistry(
        projects_dir=tmp_path / "projects",
        registry_dir=tmp_path / "projects-registry",
    )


def _local_bundle(tmp_path: Path, name: str) -> Path:
    bundle = tmp_path / name
    create_project_manifest(bundle, name=name)
    return bundle


def test_add_local_get_list_resolve_unregister(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    bundle = _local_bundle(tmp_path, "alpha")
    project = registry.add_local(bundle)

    assert registry.get(project.id).name == "alpha"
    assert [p.id for p in registry.list_projects()] == [project.id]
    assert registry.resolve("alpha").id == project.id
    assert registry.resolve(project.id).id == project.id

    registry.unregister(project.id)
    with pytest.raises(ProjectRegistryError):
        registry.get(project.id)


def test_registry_storage_lives_under_the_fenced_dir(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.add_local(_local_bundle(tmp_path, "beta"))
    # registry.json is written under the fenced registry_dir, NOT the visible
    # projects_dir where materialized checkouts live.
    assert registry.registry_path.exists()
    assert registry.registry_path.parent == (tmp_path / "projects-registry")
    assert not (tmp_path / "projects" / "registry.json").exists()


def test_resolve_attachment_for_self_workspace_builds_a_brief(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    project = registry.add_local(_local_bundle(tmp_path, "gamma"))

    attachment = resolve_project_attachment(project.id, registry=registry)
    assert attachment.project_id == project.id
    assert attachment.name == "gamma"
    # A source-less local bundle is its own workspace ("self").
    assert attachment.workspace_dir == (tmp_path / "gamma").resolve()
    assert attachment.repositories == ()
    assert "Project: gamma" in attachment.brief
    assert f"Project id: {project.id}" in attachment.brief


def test_resolve_attachment_unknown_project_is_project_not_found(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(ProjectSessionError) as exc:
        resolve_project_attachment("11111111-1111-4111-8111-111111111111", registry=registry)
    assert exc.value.code == "project_not_found"


def test_readd_local_realpath_keeps_one_registration(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    bundle = _local_bundle(tmp_path, "same")
    project = registry.add_local(bundle)
    again = registry.add_local(bundle / ".." / "same")

    assert again == project
    assert registry.list_projects() == (project,)


def test_identical_manifests_at_different_locations_get_distinct_ids(tmp_path: Path) -> None:
    import shutil
    import uuid

    registry = _registry(tmp_path)
    bundle = _local_bundle(tmp_path, "same")
    copy = tmp_path / "copy"
    shutil.copytree(bundle, copy)
    first = registry.add_local(bundle)
    second = registry.add_local(copy)

    assert first.id != second.id
    assert uuid.UUID(first.id).version == uuid.UUID(second.id).version == 4
    assert len(registry.list_projects()) == 2
    with pytest.raises(ProjectRegistryError, match="multiple projects"):
        registry.resolve("same")


def test_identity_is_install_local(tmp_path: Path) -> None:
    bundle = _local_bundle(tmp_path, "bundle")
    first = _registry(tmp_path / "one").add_local(bundle)
    second = _registry(tmp_path / "two").add_local(bundle)
    assert first.id != second.id


def test_readd_local_symlink_uses_realpath(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    bundle = _local_bundle(tmp_path, "bundle")
    link = tmp_path / "alias"
    make_dir_link(link, bundle)
    project = registry.add_local(bundle)
    assert registry.add_local(link) == project
    assert len(registry.list_projects()) == 1


@requires_local_git_remote
def test_managed_coordinate_normalization_and_branch_identity(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    first_bundle = _local_bundle(registry.projects_dir / "managed", "one")
    second_bundle = _local_bundle(registry.projects_dir / "managed", "two")
    remote = tmp_path / "remote"
    first = registry.add_managed(first_bundle, remote=str(remote), default_branch="main")
    again = registry.add_managed(
        second_bundle, remote=f" {remote}/../remote ", default_branch=" main "
    )
    other_branch = registry.add_managed(second_bundle, remote=str(remote), default_branch="next")
    other_remote = registry.add_managed(
        second_bundle, remote=str(tmp_path / "other"), default_branch="main"
    )

    assert again == first
    assert len({first.id, other_branch.id, other_remote.id}) == 3
    assert len(registry.list_projects()) == 3


def test_refresh_changes_metadata_but_retains_identity_and_review(tmp_path: Path) -> None:
    import yaml

    registry = _registry(tmp_path)
    bundle = _local_bundle(tmp_path, "before")
    project = registry.add_local(bundle)
    project = registry.record_review(project.id, "sha256:reviewed", {"project.yaml": "hash"})
    manifest_path = bundle / "project.yaml"
    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    payload["name"] = "after"
    manifest_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    refreshed = registry.refresh(project.id)

    assert refreshed.id == project.id
    assert refreshed.name == "after"
    assert refreshed.reviewed_digest == project.reviewed_digest
    assert refreshed.reviewed_files == project.reviewed_files


@pytest.mark.parametrize("unavailable", [False, True])
def test_project_payload_has_registration_id_without_reserved_fields(
    tmp_path: Path, unavailable: bool
) -> None:
    from kiro_crew.dashboard.handlers_project import _project_payload

    registry = _registry(tmp_path)
    bundle = _local_bundle(tmp_path, "bundle")
    project = registry.add_local(bundle)
    if unavailable:
        (bundle / "project.yaml").unlink()
    payload = _project_payload(project, registry=registry)
    assert payload["id"] == project.id
    assert "mcp" not in payload
    assert "memory" not in payload
    assert payload["health"]["status"] == ("unavailable" if unavailable else "healthy")


def test_concurrent_local_adds_share_one_registration(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    registry = _registry(tmp_path)
    bundle = _local_bundle(tmp_path, "concurrent")
    with ThreadPoolExecutor(max_workers=4) as executor:
        projects = list(executor.map(registry.add_local, [bundle] * 8))
    assert len({project.id for project in projects}) == 1
    assert registry.list_projects() == (projects[0],)
