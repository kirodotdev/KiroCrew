"""Tests for portable Project bundle manifests."""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
import yaml

_PROJECT_ID = "018f4f4a-760f-7a8b-a5d4-5a7e0f130d4e"


def _write_manifest(bundle, **changes):
    payload = {
        "apiVersion": "crew.kiro/v1",
        "kind": "Project",
        "id": _PROJECT_ID,
        "name": "Payments Platform",
        "description": "Payment services",
        "workspace": {"source": "self"},
        "sources": [],
    }
    payload.update(changes)
    bundle.mkdir()
    (bundle / "project.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


class TestCreateProjectManifest:
    def test_local_bundle_round_trips_without_git(self, tmp_path):
        """Creating a bundle needs only a directory and preserves its generated identity."""
        from kiro_crew.project_manifest import create_project_manifest, load_project_manifest

        bundle = tmp_path / "payments"

        created = create_project_manifest(bundle, name="Payments Platform")
        loaded = load_project_manifest(bundle)

        assert created == loaded
        assert loaded.name == "Payments Platform"
        assert loaded.workspace_source == "self"
        assert loaded.sources == ()
        assert str(uuid.UUID(loaded.id)) == loaded.id
        assert (bundle / "project.yaml").is_file()

    def test_empty_name_is_rejected_before_writing(self, tmp_path):
        from kiro_crew.project_manifest import ProjectManifestError, create_project_manifest

        bundle = tmp_path / "payments"

        with pytest.raises(ProjectManifestError, match="name must not be empty"):
            create_project_manifest(bundle, name="  ")

        assert not (bundle / "project.yaml").exists()

    def test_sensitive_bundle_path_is_rejected_before_creation(self, tmp_path, monkeypatch):
        from kiro_crew.project_manifest import ProjectManifestError, create_project_manifest

        bundle = tmp_path / "sensitive"
        monkeypatch.setattr("kiro_crew.project_manifest.is_sensitive_path", lambda path: True)

        with pytest.raises(ProjectManifestError, match="sensitive path"):
            create_project_manifest(bundle, name="Payments Platform")

        assert not bundle.exists()

    @pytest.mark.skipif(os.name != "posix", reason="symlink behavior is POSIX-only")
    def test_dangling_manifest_symlink_is_not_replaced(self, tmp_path):
        from kiro_crew.project_manifest import ProjectManifestError, create_project_manifest

        bundle = tmp_path / "payments"
        bundle.mkdir()
        manifest = bundle / "project.yaml"
        manifest.symlink_to(tmp_path / "missing.yaml")

        with pytest.raises(ProjectManifestError, match="already exists"):
            create_project_manifest(bundle, name="Payments Platform")

        assert manifest.is_symlink()


class TestLoadProjectManifest:
    @pytest.mark.skipif(os.name != "posix", reason="symlink behavior is POSIX-only")
    def test_manifest_symlink_outside_bundle_is_rejected(self, tmp_path):
        from kiro_crew.project_manifest import ProjectManifestError, load_project_manifest

        outside = tmp_path / "outside.yaml"
        outside.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "crew.kiro/v1",
                    "kind": "Project",
                    "id": _PROJECT_ID,
                    "name": "Outside",
                    "workspace": {"source": "self"},
                    "sources": [],
                }
            ),
            encoding="utf-8",
        )
        bundle = tmp_path / "payments"
        bundle.mkdir()
        (bundle / "project.yaml").symlink_to(outside)

        with pytest.raises(ProjectManifestError, match="cannot read"):
            load_project_manifest(bundle)

    def test_alias_dag_has_a_shared_expansion_budget(self, tmp_path):
        from kiro_crew.project_manifest import ProjectManifestError, load_project_manifest

        anchors = ["    seed: &a0 [0, 1]"]
        anchors.extend(
            f"    level{level}: &a{level} [*a{level - 1}, *a{level - 1}]" for level in range(1, 18)
        )
        bundle = tmp_path / "payments"
        bundle.mkdir()
        (bundle / "project.yaml").write_text(
            f"""apiVersion: crew.kiro/v1
kind: Project
id: {_PROJECT_ID}
name: Payments Platform
workspace:
  source: self
sources:
  - id: expansive
    type: extension
{chr(10).join(anchors)}
""",
            encoding="utf-8",
        )

        with pytest.raises(ProjectManifestError, match="complex|deep|expand"):
            load_project_manifest(bundle)

    def test_deep_acyclic_yaml_is_reported_as_a_manifest_error(self):
        from kiro_crew.project_manifest import ProjectManifestError, load_project_manifest_text

        nested = "[" * 1200 + "0" + "]" * 1200
        content = f"""apiVersion: crew.kiro/v1
kind: Project
id: {_PROJECT_ID}
name: Payments Platform
workspace:
  source: self
sources:
  - id: deep
    type: extension
    config: {nested}
"""

        with pytest.raises(ProjectManifestError, match="cannot read|deep"):
            load_project_manifest_text(content)

    def test_recursive_source_alias_is_rejected_without_crashing(self, tmp_path):
        from kiro_crew.project_manifest import ProjectManifestError, load_project_manifest

        bundle = tmp_path / "payments"
        bundle.mkdir()
        (bundle / "project.yaml").write_text(
            f"""apiVersion: crew.kiro/v1
kind: Project
id: {_PROJECT_ID}
name: Payments Platform
workspace:
  source: self
sources:
  - id: recursive
    type: extension
    config: &loop
      - *loop
""",
            encoding="utf-8",
        )

        with pytest.raises(ProjectManifestError, match="recursive"):
            load_project_manifest(bundle)

    def test_recursive_source_alias_in_text_is_rejected_without_crashing(self):
        from kiro_crew.project_manifest import ProjectManifestError, load_project_manifest_text

        content = f"""apiVersion: crew.kiro/v1
kind: Project
id: {_PROJECT_ID}
name: Payments Platform
workspace:
  source: self
sources:
  - id: recursive
    type: extension
    config: &loop
      nested: *loop
"""

        with pytest.raises(ProjectManifestError, match="recursive"):
            load_project_manifest_text(content)

    def test_source_ids_drive_workspace_resolution(self, tmp_path):
        from kiro_crew.project_manifest import load_project_manifest

        bundle = tmp_path / "payments"
        _write_manifest(
            bundle,
            workspace={"source": "payments-api"},
            sources=[
                {
                    "id": "payments-api",
                    "type": "repo",
                    "url": "https://github.com/acme/payments-api",
                },
                {"id": "pay-board", "type": "jira", "board": "PAY"},
            ],
        )

        manifest = load_project_manifest(bundle)

        assert manifest.workspace_source == "payments-api"
        assert [(source.id, source.type) for source in manifest.sources] == [
            ("payments-api", "repo"),
            ("pay-board", "jira"),
        ]
        assert manifest.sources[0].config == {"url": "https://github.com/acme/payments-api"}
        assert manifest.sources[1].config == {"board": "PAY"}

    @pytest.mark.parametrize("url", [7, ""])
    def test_repo_source_requires_a_non_empty_text_url(self, tmp_path, url):
        from kiro_crew.project_manifest import ProjectManifestError, load_project_manifest

        bundle = tmp_path / "payments"
        _write_manifest(
            bundle,
            sources=[{"id": "payments-api", "type": "repo", "url": url}],
        )

        with pytest.raises(ProjectManifestError, match="source 1 url must not be empty"):
            load_project_manifest(bundle)

    def test_workspace_source_must_name_a_repository(self, tmp_path):
        from kiro_crew.project_manifest import ProjectManifestError, load_project_manifest

        bundle = tmp_path / "payments"
        _write_manifest(
            bundle,
            workspace={"source": "tickets"},
            sources=[{"id": "tickets", "type": "jira", "board": "PAY"}],
        )

        with pytest.raises(ProjectManifestError, match="workspace source 'tickets' must be a repo"):
            load_project_manifest(bundle)

    @pytest.mark.parametrize(
        "config",
        [
            {"opened": datetime.date(2026, 8, 30)},
            {"nested": {"token": "AKIAIOSFODNN7EXAMPLE"}},
        ],
    )
    def test_source_config_must_be_credential_free_json(self, tmp_path, config):
        from kiro_crew.project_manifest import ProjectManifestError, load_project_manifest

        bundle = tmp_path / "payments"
        _write_manifest(
            bundle,
            sources=[{"id": "pay-board", "type": "jira", **config}],
        )

        with pytest.raises(ProjectManifestError, match="source 1 configuration"):
            load_project_manifest(bundle)

    def test_agent_skill_and_mcp_context_is_normalized(self, tmp_path):
        from kiro_crew.project_manifest import load_project_manifest

        bundle = tmp_path / "payments"
        _write_manifest(
            bundle,
            context={
                "agents": ["agents/*.json"],
                "skills": ["skills/"],
                "mcp": "mcp.json",
            },
        )

        manifest = load_project_manifest(bundle)

        assert manifest.context.agents == ("agents/*.json",)
        assert manifest.context.skills == ("skills",)
        assert manifest.context.mcp == "mcp.json"

    def test_context_rejects_the_unused_crew_alias(self, tmp_path):
        from kiro_crew.project_manifest import ProjectManifestError, load_project_manifest

        bundle = tmp_path / "payments"
        _write_manifest(bundle, context={"crew": ["agents/*.json"]})

        with pytest.raises(ProjectManifestError, match="unsupported.*crew"):
            load_project_manifest(bundle)

    def test_context_rejects_recursive_globs(self, tmp_path):
        from kiro_crew.project_manifest import ProjectManifestError, load_project_manifest

        bundle = tmp_path / "payments"
        _write_manifest(bundle, context={"agents": ["agents/**/*.json"]})

        with pytest.raises(ProjectManifestError, match="recursive glob"):
            load_project_manifest(bundle)

    def test_context_path_count_is_bounded(self, tmp_path, monkeypatch):
        import kiro_crew.project_manifest as project_manifest

        bundle = tmp_path / "payments"
        _write_manifest(bundle, context={"agents": ["agents/one.json", "agents/two.json"]})
        monkeypatch.setattr(project_manifest, "_CONTEXT_PATH_LIMIT", 1)

        with pytest.raises(project_manifest.ProjectManifestError, match="too many paths"):
            project_manifest.load_project_manifest(bundle)

    def test_source_count_is_bounded(self, tmp_path, monkeypatch):
        import kiro_crew.project_manifest as project_manifest

        bundle = tmp_path / "payments"
        _write_manifest(
            bundle,
            sources=[
                {"id": "one", "type": "extension"},
                {"id": "two", "type": "extension"},
            ],
        )
        monkeypatch.setattr(project_manifest, "_PROJECT_SOURCE_LIMIT", 1)

        with pytest.raises(project_manifest.ProjectManifestError, match="too many sources"):
            project_manifest.load_project_manifest(bundle)

    @pytest.mark.parametrize(
        "context",
        [
            {"agents": ["../agents/*.json"]},
            {"skills": ["/tmp/skills"]},
            {"skills": ["C:/skills"]},
            {"mcp": "../mcp.json"},
            {"agents": ["agents\\*.json"]},
        ],
    )
    def test_context_paths_cannot_escape_the_bundle(self, tmp_path, context):
        from kiro_crew.project_manifest import ProjectManifestError, load_project_manifest

        bundle = tmp_path / "payments"
        _write_manifest(bundle, context=context)

        with pytest.raises(ProjectManifestError, match="bundle-relative"):
            load_project_manifest(bundle)

    @pytest.mark.parametrize(
        "changes, message",
        [
            ({"apiVersion": "crew.kiro/v2"}, "unsupported apiVersion"),
            ({"kind": "Workspace"}, "kind must be Project"),
            ({"id": "payments"}, "id must be a canonical UUID"),
            ({"name": ""}, "name must not be empty"),
            (
                {
                    "sources": [
                        {"id": "api", "type": "repo", "url": "https://example.invalid/api"},
                        {"id": "api", "type": "jira"},
                    ]
                },
                "duplicate source id",
            ),
            (
                {"workspace": {"source": "missing"}},
                "workspace source 'missing' is not declared",
            ),
            (
                {"sources": [{"id": "../api", "type": "repo", "url": "x"}]},
                "source 1 id is invalid",
            ),
            ({"owner": "alice"}, "must not declare Crew user identity"),
        ],
    )
    def test_invalid_contract_is_rejected(self, tmp_path, changes, message):
        from kiro_crew.project_manifest import ProjectManifestError, load_project_manifest

        bundle = tmp_path / "payments"
        _write_manifest(bundle, **changes)

        with pytest.raises(ProjectManifestError, match=message):
            load_project_manifest(bundle)
