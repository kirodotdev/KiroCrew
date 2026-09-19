"""The thin Project manifest accepts intent, not install-local identity."""

from __future__ import annotations

import textwrap

import pytest
import yaml

from kiro_crew.project_manifest import (
    ProjectManifestError,
    _synthesize_source_id,
    create_project_manifest,
    load_project_manifest,
    load_project_manifest_text,
)


def _manifest(body: str) -> str:
    return textwrap.dedent(body).strip() + "\n"


def _valid() -> str:
    return _manifest("""
        apiVersion: crew.kiro/v1
        kind: Project
        name: payments-platform
        description: The payments platform team's working context.
        sources:
          - type: repo
            url: https://github.com/acme/payments-api
            default_branch: main
            role: primary
          - type: repo
            url: https://github.com/acme/payments-infra
            role: reference
        """)


def test_valid_manifest_parses_reduced_shape() -> None:
    manifest = load_project_manifest_text(_valid())
    assert manifest.name == "payments-platform"
    assert len(manifest.sources) == 2
    primary = next(s for s in manifest.sources if s.role == "primary")
    assert manifest.workspace_source == primary.id
    assert primary.config["url"] == "https://github.com/acme/payments-api"
    assert primary.config["default_branch"] == "main"
    assert set(vars(manifest)) == {"name", "description", "workspace_source", "sources"}


@pytest.mark.parametrize("key", ["context", "knowledge", "credentials", "workspace"])
def test_removed_top_level_keys_are_rejected_by_name(key: str) -> None:
    with pytest.raises(ProjectManifestError, match=rf"unsupported project field\(s\): {key}"):
        load_project_manifest_text(_valid() + f"{key}: {{}}\n")


@pytest.mark.parametrize(
    "key",
    [
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
    ],
)
def test_identity_keys_are_rejected_as_unknown_by_name(key: str) -> None:
    with pytest.raises(ProjectManifestError, match=rf"^unsupported project field\(s\): {key}$"):
        load_project_manifest_text(_valid() + f"{key}: {{}}\n")


def test_id_is_rejected_by_name() -> None:
    with pytest.raises(ProjectManifestError, match=r"unsupported project field\(s\): id"):
        load_project_manifest_text(_valid() + "id: 11111111-1111-4111-8111-111111111111\n")


def test_mcp_is_rejected_by_name() -> None:
    with pytest.raises(ProjectManifestError, match=r"unsupported project field\(s\): mcp"):
        load_project_manifest_text(_valid() + "mcp: [{name: atlassian}]\n")


def test_memory_is_rejected_by_name() -> None:
    with pytest.raises(ProjectManifestError, match=r"unsupported project field\(s\): memory"):
        load_project_manifest_text(_valid() + "memory: {mode: project}\n")


def test_non_repo_source_type_rejected() -> None:
    body = _manifest("""
        apiVersion: crew.kiro/v1
        kind: Project
        name: p
        sources:
          - type: jira
            url: https://acme.atlassian.net
        """)
    with pytest.raises(ProjectManifestError, match="not supported"):
        load_project_manifest_text(body)


def test_unknown_source_key_rejected() -> None:
    body = _manifest("""
        apiVersion: crew.kiro/v1
        kind: Project
        name: p
        sources:
          - type: repo
            url: https://example.com/r
            jql: "x = y"
        """)
    with pytest.raises(ProjectManifestError, match="jql"):
        load_project_manifest_text(body)


def test_multiple_primary_rejected() -> None:
    body = _manifest("""
        apiVersion: crew.kiro/v1
        kind: Project
        name: p
        sources:
          - type: repo
            url: https://example.com/a
            role: primary
          - type: repo
            url: https://example.com/b
            role: primary
        """)
    with pytest.raises(ProjectManifestError, match="more than one primary"):
        load_project_manifest_text(body)


def test_multiple_sources_without_primary_rejected() -> None:
    body = _manifest("""
        apiVersion: crew.kiro/v1
        kind: Project
        name: p
        sources:
          - type: repo
            url: https://example.com/a
          - type: repo
            url: https://example.com/b
        """)
    with pytest.raises(ProjectManifestError, match="none has role: primary"):
        load_project_manifest_text(body)


def test_zero_sources_is_self_workspace() -> None:
    body = _manifest("""
        apiVersion: crew.kiro/v1
        kind: Project
        name: p
        """)
    assert load_project_manifest_text(body).workspace_source == "self"


def test_single_source_is_workspace_without_role() -> None:
    body = _manifest("""
        apiVersion: crew.kiro/v1
        kind: Project
        name: p
        sources:
          - type: repo
            url: https://example.com/only
        """)
    manifest = load_project_manifest_text(body)
    assert manifest.workspace_source == manifest.sources[0].id


def test_credentials_in_url_rejected() -> None:
    body = _manifest("""
        apiVersion: crew.kiro/v1
        kind: Project
        name: p
        sources:
          - type: repo
            url: https://user:secretpassword@example.com/r
        """)
    with pytest.raises(ProjectManifestError, match="credentials"):
        load_project_manifest_text(body)


def test_source_id_is_stable_and_reorder_proof() -> None:
    url = "https://github.com/acme/payments-api.git"
    first = _synthesize_source_id(url)
    assert first == _synthesize_source_id(url)
    assert first.startswith("payments-api-")


def test_create_and_load_round_trip(tmp_path) -> None:
    created = create_project_manifest(tmp_path, name="Local Bundle")
    loaded = load_project_manifest(tmp_path)
    assert created == loaded
    assert loaded.name == "Local Bundle"
    assert loaded.workspace_source == "self"
    assert set(yaml.safe_load((tmp_path / "project.yaml").read_text(encoding="utf-8"))) == {
        "apiVersion",
        "kind",
        "name",
        "description",
        "sources",
    }
