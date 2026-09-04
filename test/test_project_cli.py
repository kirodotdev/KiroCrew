from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from kiro_crew.cli import main
from kiro_crew.project_manifest import create_project_manifest, load_project_manifest
from kiro_crew.project_registry import ProjectRegistration, RegisteredProject


def _run(argv: list[str]) -> None:
    with patch("sys.argv", ["kirocrew", *argv]):
        main()


class TestProjectCli:
    def test_create_makes_local_bundle_and_registers_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        data_home = tmp_path / "data"
        bundle = tmp_path / "payments"
        monkeypatch.setenv("KIROCREW_HOME", str(data_home))

        audit = Mock()
        with patch(
            "kiro_crew.cli_projects.sel",
            return_value=SimpleNamespace(log_api_access=audit),
        ):
            _run(["project", "create", str(bundle), "--name", "Payments"])

        manifest = load_project_manifest(bundle)
        raw_registry = json.loads(
            (data_home / "trust" / "project-registry" / "registry.json").read_text(encoding="utf-8")
        )
        project_id = next(iter(raw_registry["projects"]))
        assert manifest.id == project_id
        assert str(uuid.UUID(project_id)) == project_id
        assert (bundle / "project.yaml").exists()
        output = capsys.readouterr().out
        assert "Created Project Payments" in output
        assert project_id in output
        audit.assert_called_once_with(
            caller="local-cli",
            operation="project_create",
            outcome="allowed",
            source="cli",
            resources=f"project={project_id}",
            error="",
        )

    def test_add_list_and_show_local_bundle(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        data_home = tmp_path / "data"
        bundle = tmp_path / "payments"
        monkeypatch.setenv("KIROCREW_HOME", str(data_home))
        manifest = create_project_manifest(bundle, name="Payments")

        _run(["project", "add", str(bundle)])
        assert "Added Project Payments" in capsys.readouterr().out

        _run(["project", "list"])
        listed = capsys.readouterr().out
        assert "Payments" in listed
        assert manifest.id in listed

        _run(["project", "show", manifest.id])
        shown = capsys.readouterr().out
        assert "Name: Payments" in shown
        assert f"ID: {manifest.id}" in shown
        assert str(bundle.resolve()) in shown

    def test_show_unknown_project_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))

        with pytest.raises(SystemExit) as exc_info:
            _run(["project", "show", "missing"])

        assert exc_info.value.code == 1
        assert "project is not registered" in capsys.readouterr().err

    def test_add_git_url_uses_managed_clone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
        managed = tmp_path / "data" / "projects" / "managed" / "id" / "bundle"
        result = RegisteredProject(
            id="018f4f4a-760f-7a8b-a5d4-5a7e0f130d4e",
            name="Payments",
            registrations=(
                ProjectRegistration(
                    path=managed,
                    origin="managed_git",
                    remote="https://example.invalid/payments.git",
                ),
            ),
        )
        with (
            patch("kiro_crew.cli_projects.GitProjectStore") as store_class,
            patch("kiro_crew.cli_projects.ProjectCapabilityManager") as manager_class,
        ):
            store_class.return_value.add.return_value = result
            _run(["project", "add", "https://example.invalid/payments.git"])

        store_class.return_value.add.assert_called_once_with(
            "https://example.invalid/payments.git",
            before_primary_change=manager_class.return_value.guard_primary_change,
        )
        assert "Added Project Payments" in capsys.readouterr().out

    def test_sync_routes_to_managed_git_store(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
        project_id = "018f4f4a-760f-7a8b-a5d4-5a7e0f130d4e"
        result = RegisteredProject(
            id=project_id,
            name="Payments",
            registrations=(
                ProjectRegistration(
                    path=tmp_path / "managed",
                    origin="managed_git",
                    remote="https://example.invalid/payments.git",
                ),
            ),
        )
        with (
            patch("kiro_crew.cli_projects.GitProjectStore") as store_class,
            patch("kiro_crew.cli_projects.ProjectCapabilityManager") as manager_class,
        ):
            store_class.return_value.sync.return_value = result
            manager_class.return_value.refresh_if_active.return_value.active = False

            _run(["project", "sync", project_id])

        store_class.return_value.sync.assert_called_once_with(project_id)
        manager_class.return_value.refresh_if_active.assert_called_once_with(project_id)
        assert "Synced Project Payments" in capsys.readouterr().out
