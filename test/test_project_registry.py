from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from conftest import make_dir_link
from kiro_crew.project_registry import (
    PROJECT_REGISTRY_MAX_BYTES,
    ProjectRegistration,
    ProjectRegistry,
    ProjectRegistryError,
    RegisteredProject,
)

_PROJECT_A = "018f4f4a-760f-7a8b-a5d4-5a7e0f130d4e"
_PROJECT_B = "018f4f4a-760f-7a8b-a5d4-5a7e0f130d4f"


def _write_bundle(path: Path, *, project_id: str, name: str) -> Path:
    path.mkdir(parents=True)
    manifest = {
        "apiVersion": "crew.kiro/v1",
        "kind": "Project",
        "id": project_id,
        "name": name,
        "workspace": {"source": "self"},
        "sources": [],
    }
    (path / "project.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


class TestProjectRegistry:
    def test_local_registration_survives_reload(self, tmp_path: Path) -> None:
        bundle = _write_bundle(tmp_path / "bundle", project_id=_PROJECT_A, name="Payments")
        registry_dir = tmp_path / "data" / "projects"

        registered = ProjectRegistry(registry_dir).add_local(bundle)
        reloaded = ProjectRegistry(registry_dir).get(_PROJECT_A)

        assert registered == reloaded
        assert registered.id == _PROJECT_A
        assert registered.name == "Payments"
        assert registered.registrations[0].path == bundle.resolve()
        assert registered.registrations[0].origin == "local"

    def test_same_name_with_different_ids_coexists(self, tmp_path: Path) -> None:
        first = _write_bundle(tmp_path / "first", project_id=_PROJECT_A, name="Payments")
        second = _write_bundle(tmp_path / "second", project_id=_PROJECT_B, name="Payments")
        registry = ProjectRegistry(tmp_path / "registry")

        registry.add_local(first)
        registry.add_local(second)

        assert [project.id for project in registry.list_projects()] == [_PROJECT_A, _PROJECT_B]

    def test_same_id_retains_distinct_materializations(self, tmp_path: Path) -> None:
        first = _write_bundle(tmp_path / "first", project_id=_PROJECT_A, name="Payments")
        second = _write_bundle(tmp_path / "second", project_id=_PROJECT_A, name="Payments Renamed")
        registry = ProjectRegistry(tmp_path / "registry")

        registry.add_local(first)
        registry.add_local(first)
        project = registry.add_local(second, before_primary_change=lambda _old, _new: None)

        assert project.name == "Payments Renamed"
        assert [entry.path for entry in project.registrations] == [
            first.resolve(),
            second.resolve(),
        ]

    def test_same_id_new_primary_requires_a_capability_aware_guard(self, tmp_path: Path) -> None:
        first = _write_bundle(tmp_path / "first", project_id=_PROJECT_A, name="Payments")
        second = _write_bundle(tmp_path / "second", project_id=_PROJECT_A, name="Payments")
        registry = ProjectRegistry(tmp_path / "registry")
        registry.add_local(first)

        with pytest.raises(ProjectRegistryError, match="capability-aware registration"):
            registry.add_local(second)

        assert registry.get(_PROJECT_A).registrations[0].path == first.resolve()

    def test_existing_git_checkout_is_not_managed(self, tmp_path: Path) -> None:
        bundle = _write_bundle(tmp_path / "bundle", project_id=_PROJECT_A, name="Payments")
        (bundle / ".git").mkdir()

        registered = ProjectRegistry(tmp_path / "registry").add_local(bundle)

        assert registered.registrations[0].origin == "existing_git"

    def test_local_registration_rejects_a_sensitive_resolved_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bundle = _write_bundle(tmp_path / "bundle", project_id=_PROJECT_A, name="Payments")
        monkeypatch.setattr("kiro_crew.project_registry.is_sensitive_path", lambda path: True)

        with pytest.raises(ProjectRegistryError, match="sensitive path"):
            ProjectRegistry(tmp_path / "registry").add_local(bundle)

    def test_registry_wraps_a_registered_path_resolution_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bundle = _write_bundle(tmp_path / "bundle", project_id=_PROJECT_A, name="Payments")
        registry = ProjectRegistry(tmp_path / "registry")
        registry.add_local(bundle)
        real_resolve = Path.resolve

        def fail_registered_path(path: Path, *args, **kwargs):
            if path == bundle:
                raise RuntimeError("Symlink loop")
            return real_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", fail_registered_path)

        with pytest.raises(ProjectRegistryError, match="cannot be resolved"):
            registry.list_projects()

    def test_registry_load_rejects_a_sensitive_stored_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A planted registry entry cannot bypass the registration-time path gate."""
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        registry.registry_path.parent.mkdir(parents=True)
        registry.registry_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "projects": {
                        _PROJECT_A: {
                            "name": "Payments",
                            "registrations": [
                                {"path": str(tmp_path / "blocked"), "origin": "local"}
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.project_registry.is_sensitive_path", lambda path: True)

        with pytest.raises(ProjectRegistryError, match="sensitive path"):
            registry.list_projects()

    def test_default_registry_state_lives_under_the_keystone_trust_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An agent must not be able to rewrite the Project authority registry."""
        from kiro_crew.security import is_sensitive_path

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))

        registry = ProjectRegistry()

        assert registry.registry_path.parent == tmp_path / "data" / "trust" / "project-registry"
        assert is_sensitive_path(str(registry.registry_path)) is True

    def test_oversized_registry_is_rejected_before_json_parsing(self, tmp_path: Path) -> None:
        registry = ProjectRegistry(tmp_path / "data" / "projects")
        registry.registry_path.parent.mkdir(parents=True)
        registry.registry_path.write_bytes(b"{" + b" " * (1024 * 1024) + b"}")

        with pytest.raises(ProjectRegistryError, match="too large"):
            registry.list_projects()

    def test_oversized_registry_is_rejected_before_replacing_valid_state(
        self, tmp_path: Path
    ) -> None:
        bundle = _write_bundle(tmp_path / "bundle", project_id=_PROJECT_A, name="Payments")
        registry = ProjectRegistry(tmp_path / "registry")
        registry.add_local(bundle)
        valid_state = registry.registry_path.read_bytes()
        oversized = RegisteredProject(
            id=_PROJECT_A,
            name="Payments",
            registrations=(
                ProjectRegistration(
                    path=bundle,
                    origin="managed_git",
                    remote="https://example.invalid/" + "x" * PROJECT_REGISTRY_MAX_BYTES,
                ),
            ),
        )

        with pytest.raises(ProjectRegistryError, match="too large"):
            registry._save_unlocked({_PROJECT_A: oversized})

        assert registry.registry_path.read_bytes() == valid_state

    def test_registry_directory_link_is_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "outside"
        target.mkdir()
        registry_dir = tmp_path / "registry"
        make_dir_link(registry_dir, target)

        with pytest.raises(ProjectRegistryError, match="directory must not be a link"):
            ProjectRegistry(registry_dir).list_projects()

    def test_unregister_forgets_project_without_removing_bundle(self, tmp_path: Path) -> None:
        bundle = _write_bundle(tmp_path / "bundle", project_id=_PROJECT_A, name="Payments")
        registry = ProjectRegistry(tmp_path / "registry")
        registry.add_local(bundle)

        removed = registry.unregister(_PROJECT_A)

        assert removed.id == _PROJECT_A
        assert registry.list_projects() == ()
        assert (bundle / "project.yaml").is_file()

    def test_unregister_rejects_an_unknown_project(self, tmp_path: Path) -> None:
        registry = ProjectRegistry(tmp_path / "registry")

        with pytest.raises(ProjectRegistryError, match="project is not registered"):
            registry.unregister(_PROJECT_A)

    def test_resolve_requires_id_when_name_is_ambiguous(self, tmp_path: Path) -> None:
        first = _write_bundle(tmp_path / "first", project_id=_PROJECT_A, name="Payments")
        second = _write_bundle(tmp_path / "second", project_id=_PROJECT_B, name="Payments")
        registry = ProjectRegistry(tmp_path / "registry")
        registry.add_local(first)
        registry.add_local(second)

        try:
            registry.resolve("Payments")
        except ProjectRegistryError as exc:
            assert "matches multiple projects" in str(exc)
        else:
            raise AssertionError("ambiguous names must require a stable project id")

        assert registry.resolve(_PROJECT_B).id == _PROJECT_B

    def test_invalid_registry_fails_loudly(self, tmp_path: Path) -> None:
        registry_dir = tmp_path / "registry"
        registry_dir.mkdir()
        (registry_dir / "registry.json").write_text(
            json.dumps({"version": 999, "projects": {}}), encoding="utf-8"
        )

        try:
            ProjectRegistry(registry_dir).list_projects()
        except ProjectRegistryError as exc:
            assert "unsupported project registry version" in str(exc)
        else:
            raise AssertionError("invalid registry must not be treated as empty")
