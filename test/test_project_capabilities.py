"""Trusted materialization for Project-bundled capabilities."""

from __future__ import annotations

import json
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from conftest import make_dir_link
from kiro_crew import pinned_fs, platform_compat
from kiro_crew.project_registry import ProjectRegistry

_PROJECT_ID = "018f4f4a-760f-7a8b-a5d4-5a7e0f130d4e"
_COLLIDING_PREFIX_PROJECT_ID = "018f4f4a-760f-7a8b-a5d4-5a7e0f130d5f"


def _bundle(
    path: Path,
    *,
    agent: dict | None = None,
    mcp: dict | None = None,
    sources: list[dict] | None = None,
    include_skills: bool = True,
) -> Path:
    (path / "agents").mkdir(parents=True)
    (path / "skills" / "deploy").mkdir(parents=True)
    (path / "agents" / "reviewer.json").write_text(
        json.dumps(agent or {"name": "reviewer", "description": "Reviews changes"}),
        encoding="utf-8",
    )
    (path / "skills" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Deploy safely\n---\n\nFollow the runbook.\n",
        encoding="utf-8",
    )
    (path / "mcp.json").write_text(
        json.dumps(
            mcp
            or {
                "mcpServers": {
                    "docs": {"url": "https://mcp.example.invalid"},
                }
            }
        ),
        encoding="utf-8",
    )
    (path / "project.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "crew.kiro/v1",
                "kind": "Project",
                "id": _PROJECT_ID,
                "name": "Payments",
                "workspace": {"source": "self"},
                "sources": sources or [],
                "context": {
                    "agents": ["agents/*.json"],
                    "skills": ["skills"] if include_skills else [],
                    "mcp": "mcp.json",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _manager(tmp_path: Path, bundle: Path):
    from kiro_crew.project_capabilities import ProjectCapabilityManager

    registry = ProjectRegistry(tmp_path / "data" / "projects")
    registry.add_local(bundle)
    return ProjectCapabilityManager(
        registry,
        agents_dir=tmp_path / "kiro" / "agents",
        skills_dir=tmp_path / "data" / "skills",
        mcp_path=tmp_path / "data" / "mcp.json",
    )


def test_manager_does_not_expose_deferred_manifest_editing_seams() -> None:
    from kiro_crew.project_capabilities import ProjectCapabilityManager

    assert not hasattr(ProjectCapabilityManager, "has_activation_state")
    assert not hasattr(ProjectCapabilityManager, "update_inactive")
    assert not hasattr(ProjectCapabilityManager, "withdraw_if_primary_changed")


def test_inventory_is_visible_before_project_is_trusted(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    manager = _manager(tmp_path, _bundle(tmp_path / "bundle"))

    status = manager.status(_PROJECT_ID)

    assert status.active is False
    assert status.inventory.agents == 1
    assert status.inventory.skills == 1
    assert status.inventory.mcp_servers == 1
    assert status.agent_names == ("reviewer",)
    assert status.mcp_servers == (("docs", {"url": "https://mcp.example.invalid"}),)
    assert not (tmp_path / "kiro" / "agents").exists()


def test_activation_refuses_capability_content_changed_after_review(tmp_path, monkeypatch):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle", include_skills=False)
    manager = _manager(tmp_path, bundle)
    reviewed_key = manager.status(_PROJECT_ID).review_key
    (bundle / "agents" / "reviewer.json").write_text(
        '{"name":"reviewer","description":"Run unreviewed instructions"}',
        encoding="utf-8",
    )

    with pytest.raises(ProjectCapabilityError, match="changed after review"):
        manager.activate(_PROJECT_ID, expected_key=reviewed_key)

    assert manager.status(_PROJECT_ID).active is False
    assert not (tmp_path / "kiro" / "agents" / f"project--{_PROJECT_ID}--reviewer.json").exists()


@pytest.mark.parametrize(
    "relative_path,unreviewed_payload",
    [
        (
            "agents/reviewer.json",
            b'{"name":"reviewer","description":"Unreviewed agent"}',
        ),
        (
            "mcp.json",
            b'{"mcpServers":{"docs":{"url":"https://unreviewed.example.invalid"}}}',
        ),
    ],
)
def test_activation_hashes_the_same_capability_snapshot_it_validates(
    tmp_path, monkeypatch, relative_path, unreviewed_payload
):
    import kiro_crew.project_capabilities as capabilities
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle", include_skills=False)
    manager = _manager(tmp_path, bundle)
    reviewed_key = manager.status(_PROJECT_ID).review_key
    target = bundle / relative_path
    reviewed_payload = target.read_bytes()
    real_read = capabilities._read_project_file_bytes
    target_reads = 0

    def toggle_after_validation(path, root, *, label):
        nonlocal target_reads
        if path == target:
            target_reads += 1
            return unreviewed_payload if target_reads == 1 else reviewed_payload
        return real_read(path, root, label=label)

    monkeypatch.setattr(capabilities, "_read_project_file_bytes", toggle_after_validation)

    with pytest.raises(ProjectCapabilityError, match="changed after review"):
        manager.activate(_PROJECT_ID, expected_key=reviewed_key)

    assert target_reads == 1
    assert not (tmp_path / "kiro" / "agents" / f"project--{_PROJECT_ID}--reviewer.json").exists()
    assert not manager.mcp_path.exists()


def test_activation_resolves_executable_content_under_the_project_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    manager = _manager(tmp_path, _bundle(tmp_path / "bundle", include_skills=False))
    reviewed_key = manager.status(_PROJECT_ID).review_key
    real_lock = manager._lock
    real_resolve = manager._resolve
    lock_depth = 0

    @contextmanager
    def tracked_lock(project_id: str):
        nonlocal lock_depth
        with real_lock(project_id):
            lock_depth += 1
            try:
                yield
            finally:
                lock_depth -= 1

    def guarded_resolve(identifier: str, *, validate_executable: bool = False):
        if validate_executable:
            assert lock_depth == 1
        return real_resolve(identifier, validate_executable=validate_executable)

    monkeypatch.setattr(manager, "_lock", tracked_lock)
    monkeypatch.setattr(manager, "_resolve", guarded_resolve)

    status = manager.activate(_PROJECT_ID, expected_key=reviewed_key)

    assert status.active is True


def test_review_key_covers_every_installable_declaration(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle")
    manager = _manager(tmp_path, bundle)
    keys = [manager.status(_PROJECT_ID).review_key]

    (bundle / "mcp.json").write_text(
        '{"mcpServers":{"docs":{"url":"https://changed.example.invalid"}}}',
        encoding="utf-8",
    )
    keys.append(manager.status(_PROJECT_ID).review_key)
    (bundle / "agents" / "reviewer.json").write_text(
        '{"name":"reviewer","description":"Changed agent"}',
        encoding="utf-8",
    )
    keys.append(manager.status(_PROJECT_ID).review_key)
    manifest_path = bundle / "project.yaml"
    document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    document["sources"] = [
        {"id": "payments-api", "type": "repo", "url": "https://example.invalid/api.git"}
    ]
    manifest_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    keys.append(manager.status(_PROJECT_ID).review_key)
    if pinned_fs.supports_pinned_tree_walk():
        (bundle / "skills" / "deploy" / "SKILL.md").write_text(
            "---\nname: deploy\ndescription: Changed skill\n---\n",
            encoding="utf-8",
        )
        keys.append(manager.status(_PROJECT_ID).review_key)

    assert len(set(keys)) == len(keys)


def test_refresh_withdraws_capabilities_changed_by_bundle_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle", include_skills=False)
    manager = _manager(tmp_path, bundle)
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    installed = tmp_path / "kiro" / "agents" / f"project--{_PROJECT_ID}--reviewer.json"
    (bundle / "agents" / "reviewer.json").write_text(
        '{"name":"reviewer","description":"Changed upstream"}',
        encoding="utf-8",
    )

    status = manager.refresh_if_active(_PROJECT_ID)

    assert status.active is False
    assert not installed.exists()
    assert not manager._state_path(_PROJECT_ID).exists()


def test_refresh_revokes_locally_modified_capabilities_when_bundle_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle", include_skills=False)
    manager = _manager(tmp_path, bundle)
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    installed_agent = tmp_path / "kiro" / "agents" / f"project--{_PROJECT_ID}--reviewer.json"
    installed_agent.write_text('{"name":"locally-modified"}', encoding="utf-8")
    mcp_name = f"project-{_PROJECT_ID}-docs"
    mcp = json.loads(manager.mcp_path.read_text(encoding="utf-8"))
    mcp["mcpServers"][mcp_name]["url"] = "https://local.example.invalid"
    mcp["mcpServers"][mcp_name]["x-kirocrew-project"] = "locally-modified-owner"
    manager.mcp_path.write_text(json.dumps(mcp), encoding="utf-8")
    (bundle / "agents" / "reviewer.json").write_text(
        '{"name":"reviewer","description":"Changed upstream"}',
        encoding="utf-8",
    )

    status = manager.refresh_if_active(_PROJECT_ID)

    assert status.active is False
    assert not installed_agent.exists()
    after = json.loads(manager.mcp_path.read_text(encoding="utf-8"))
    assert mcp_name not in after["mcpServers"]
    assert not manager._state_path(_PROJECT_ID).exists()


def test_refresh_revokes_active_capabilities_when_synced_bundle_is_malformed(tmp_path, monkeypatch):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle", include_skills=False)
    manager = _manager(tmp_path, bundle)
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    installed_agent = tmp_path / "kiro" / "agents" / f"project--{_PROJECT_ID}--reviewer.json"
    mcp_name = f"project-{_PROJECT_ID}-docs"
    (bundle / "mcp.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(ProjectCapabilityError, match="readable JSON"):
        manager.refresh_if_active(_PROJECT_ID)

    assert not installed_agent.exists()
    assert not manager._state_path(_PROJECT_ID).exists()
    after = json.loads(manager.mcp_path.read_text(encoding="utf-8"))
    assert mcp_name not in after["mcpServers"]


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="Project skill activation requires descriptor-pinned tree traversal",
)
def test_refresh_unlinks_a_replaced_skill_root_without_deleting_its_target(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle")
    manager = _manager(tmp_path, bundle)
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    installed = tmp_path / "data" / "skills" / "projects" / _PROJECT_ID
    moved = tmp_path / "locally-replaced-skills"
    installed.rename(moved)
    make_dir_link(installed, moved)
    (bundle / "agents" / "reviewer.json").write_text(
        '{"name":"reviewer","description":"Changed upstream"}',
        encoding="utf-8",
    )

    status = manager.refresh_if_active(_PROJECT_ID)

    assert status.active is False
    assert not installed.exists()
    assert moved.is_dir()
    assert not manager._state_path(_PROJECT_ID).exists()


def test_refresh_only_activation_does_not_restore_a_revoked_project(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    manager = _manager(tmp_path, _bundle(tmp_path / "bundle", include_skills=False))
    reviewed_key = manager.status(_PROJECT_ID).review_key

    status = manager.activate(
        _PROJECT_ID,
        expected_key=reviewed_key,
        _require_active=True,
    )

    assert status.active is False
    assert not manager._state_path(_PROJECT_ID).exists()


def test_failed_repository_refresh_preserves_the_previous_activation(tmp_path, monkeypatch):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(
        tmp_path / "bundle",
        include_skills=False,
        sources=[{"id": "api", "type": "repo", "url": "https://example.invalid/api.git"}],
    )
    manager = _manager(tmp_path, bundle)
    checkout = manager.registry.projects_dir / "state" / _PROJECT_ID / "sources" / "api"
    checkout.mkdir(parents=True)
    monkeypatch.setattr(
        "kiro_crew.project_git.GitProjectStore.materialize_source",
        lambda *_args, **_kwargs: checkout,
    )
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    installed = tmp_path / "kiro" / "agents" / f"project--{_PROJECT_ID}--reviewer.json"

    def fail_refresh(*_args, **_kwargs):
        raise ProjectCapabilityError("repository refresh failed")

    monkeypatch.setattr(
        "kiro_crew.project_git.GitProjectStore.materialize_source",
        fail_refresh,
    )

    with pytest.raises(ProjectCapabilityError, match="repository refresh failed"):
        manager.refresh_if_active(_PROJECT_ID)

    assert installed.is_file()
    assert manager._state_path(_PROJECT_ID).is_file()
    assert manager.status(_PROJECT_ID).active is True


def test_repeated_activation_keeps_the_existing_materialization(tmp_path, monkeypatch):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(
        tmp_path / "bundle",
        include_skills=False,
        sources=[{"id": "api", "type": "repo", "url": "https://example.invalid/api.git"}],
    )
    manager = _manager(tmp_path, bundle)
    checkout = manager.registry.projects_dir / "state" / _PROJECT_ID / "sources" / "api"
    checkout.mkdir(parents=True)
    materializations = 0

    def materialize(*_args, **_kwargs):
        nonlocal materializations
        materializations += 1
        if materializations > 1:
            raise ProjectCapabilityError("second materialization failed")
        return checkout

    monkeypatch.setattr(
        "kiro_crew.project_git.GitProjectStore.materialize_source",
        materialize,
    )
    review_key = manager.status(_PROJECT_ID).review_key

    first = manager.activate(_PROJECT_ID, expected_key=review_key)
    second = manager.activate(_PROJECT_ID, expected_key=review_key)

    assert first.active is True
    assert second.active is True
    assert materializations == 1
    assert (tmp_path / "kiro" / "agents" / f"project--{_PROJECT_ID}--reviewer.json").is_file()
    assert manager._state_path(_PROJECT_ID).is_file()


def test_malformed_bracketed_mcp_url_is_a_capability_error() -> None:
    from kiro_crew.project_capabilities import ProjectCapabilityError, _credential_free_mcp_url

    with pytest.raises(ProjectCapabilityError, match="credential-free http"):
        _credential_free_mcp_url("http://[::1", label="Project MCP server docs")


@pytest.mark.parametrize(
    "mcp",
    [
        {"mcpServers": {"docs": {"command": "ghp_" + "A" * 36, "args": ["server.js"]}}},
        {
            "mcpServers": {
                "docs": {
                    "command": "node",
                    "args": [f"https://example.invalid/collect?data={'A' * 250}"],
                }
            }
        },
        {"mcpServers": {"docs": {"url": f"https://example.invalid/ghp_{'A' * 36}"}}},
    ],
)
def test_inventory_rejects_mcp_text_hidden_by_review_redaction(tmp_path, monkeypatch, mcp):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    manager = _manager(tmp_path, _bundle(tmp_path / "bundle", mcp=mcp))

    review_key = manager.status(_PROJECT_ID).review_key
    with pytest.raises(ProjectCapabilityError, match="credential or suspicious exfiltration"):
        manager.activate(_PROJECT_ID, expected_key=review_key)


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="Project skill activation requires descriptor-pinned tree traversal",
)
def test_activation_materializes_namespaced_agents_skills_and_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(
        tmp_path / "bundle",
        agent={
            "name": "reviewer",
            "description": "Reviews changes",
            "includeMcpJson": True,
        },
    )
    manager = _manager(tmp_path, bundle)

    status = manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)

    assert status.active is True
    agent_path = tmp_path / "kiro" / "agents" / f"project--{_PROJECT_ID}--reviewer.json"
    materialized_agent = json.loads(agent_path.read_text(encoding="utf-8"))
    assert materialized_agent["name"] == f"project-{_PROJECT_ID}-reviewer"
    assert materialized_agent["includeMcpJson"] is False
    assert (
        tmp_path / "data" / "skills" / "projects" / _PROJECT_ID / "deploy" / "SKILL.md"
    ).is_file()
    mcp = json.loads((tmp_path / "data" / "mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"] == {
        f"project-{_PROJECT_ID}-docs": {
            "url": "https://mcp.example.invalid",
            "x-kirocrew-project": _PROJECT_ID,
        }
    }


@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="descriptor-pinned tree traversal is POSIX-only",
)
def test_activation_refuses_a_skill_file_swapped_to_a_sensitive_symlink(tmp_path, monkeypatch):
    from kiro_crew import pinned_fs
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle")
    manager = _manager(tmp_path, bundle)
    skill_file = bundle / "skills" / "deploy" / "SKILL.md"
    secret = tmp_path / "sensitive.txt"
    secret.write_text("operator secret\n", encoding="utf-8")
    real_copy = pinned_fs.copy_file_pinned
    swapped = False
    reviewed_key = manager.status(_PROJECT_ID).review_key

    def swap_before_open(*args, **kwargs):
        nonlocal swapped
        if not swapped and kwargs.get("name") == "SKILL.md":
            swapped = True
            skill_file.unlink()
            skill_file.symlink_to(secret)
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(pinned_fs, "copy_file_pinned", swap_before_open)

    with pytest.raises(ProjectCapabilityError, match="could not be copied"):
        manager.activate(_PROJECT_ID, expected_key=reviewed_key)

    installed = tmp_path / "data" / "skills" / "projects" / _PROJECT_ID
    assert not installed.exists()
    assert not any(
        path.read_text(encoding="utf-8") == "operator secret\n"
        for path in (tmp_path / "data").rglob("*")
        if path.is_file()
    )


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="descriptor-pinned tree traversal is POSIX-only",
)
def test_activation_refuses_skill_content_changed_during_materialization(tmp_path, monkeypatch):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle")
    manager = _manager(tmp_path, bundle)
    reviewed_key = manager.status(_PROJECT_ID).review_key
    skill_file = bundle / "skills" / "deploy" / "SKILL.md"
    real_stage = pinned_fs.stage_tree_pinned
    changed = False

    def change_before_stage(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            skill_file.write_text(
                "---\nname: deploy\ndescription: Changed after review\n---\n",
                encoding="utf-8",
            )
        return real_stage(*args, **kwargs)

    monkeypatch.setattr(pinned_fs, "stage_tree_pinned", change_before_stage)

    with pytest.raises(ProjectCapabilityError, match="skill changed after review"):
        manager.activate(_PROJECT_ID, expected_key=reviewed_key)

    assert not (tmp_path / "data" / "skills" / "projects" / _PROJECT_ID).exists()


@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="descriptor-pinned file reads are POSIX-only",
)
@pytest.mark.parametrize("source_name", ["reviewer.json", "mcp.json"])
def test_activation_refuses_json_swapped_to_a_sensitive_symlink(tmp_path, monkeypatch, source_name):
    from kiro_crew import pinned_fs
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle", include_skills=False)
    manager = _manager(tmp_path, bundle)
    source = bundle / ("agents/reviewer.json" if source_name == "reviewer.json" else source_name)
    secret = tmp_path / "sensitive.json"
    secret.write_text('{"name":"operator-secret","mcpServers":{}}', encoding="utf-8")
    real_open = pinned_fs.open_in_pinned_parent
    swapped = False
    reviewed_key = manager.status(_PROJECT_ID).review_key

    def swap_before_open(*args, **kwargs):
        nonlocal swapped
        if not swapped and args[1] == source_name:
            swapped = True
            source.unlink()
            source.symlink_to(secret)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(pinned_fs, "open_in_pinned_parent", swap_before_open)

    with pytest.raises(ProjectCapabilityError, match="pinned|link|readable"):
        manager.activate(_PROJECT_ID, expected_key=reviewed_key)

    assert not (tmp_path / "kiro" / "agents" / f"project--{_PROJECT_ID}--reviewer.json").exists()


def test_activation_uses_hardened_fallback_without_descriptor_pinned_json_reads(
    tmp_path, monkeypatch
):
    from kiro_crew import pinned_fs

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle", include_skills=False)
    manager = _manager(tmp_path, bundle)
    reviewed_key = manager.status(_PROJECT_ID).review_key
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)

    status = manager.activate(_PROJECT_ID, expected_key=reviewed_key)

    assert status.active is True


def test_activation_fails_closed_when_hardened_json_read_refuses(tmp_path, monkeypatch):
    from kiro_crew import hooks, pinned_fs
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle", include_skills=False)
    manager = _manager(tmp_path, bundle)
    reviewed_key = manager.status(_PROJECT_ID).review_key
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    real_read = hooks.safe_read_file_bytes_nolink

    def refuse_capability_json(path, *args, **kwargs):
        if Path(path).name == "project.yaml":
            return real_read(path, *args, **kwargs)
        return None

    monkeypatch.setattr(hooks, "safe_read_file_bytes_nolink", refuse_capability_json)

    with pytest.raises(ProjectCapabilityError, match="hardened file read"):
        manager.activate(_PROJECT_ID, expected_key=reviewed_key)


def test_inventory_rejects_oversized_capability_json(tmp_path, monkeypatch):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle", include_skills=False)
    (bundle / "mcp.json").write_text(
        json.dumps({"padding": "x" * (1024 * 1024), "mcpServers": {}}),
        encoding="utf-8",
    )
    manager = _manager(tmp_path, bundle)

    with pytest.raises(ProjectCapabilityError, match="too large"):
        manager.status(_PROJECT_ID)


def test_inventory_bounds_declared_skill_tree_scan(tmp_path, monkeypatch):
    import kiro_crew.project_capabilities as capabilities
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle")
    manager = _manager(tmp_path, bundle)
    monkeypatch.setattr(capabilities, "_PROJECT_CAPABILITY_SCAN_ENTRY_LIMIT", 1)

    with pytest.raises(ProjectCapabilityError, match="too many entries"):
        manager.status(_PROJECT_ID)


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="Project skill activation requires descriptor-pinned tree traversal",
)
def test_activation_bounds_skill_tree_bytes_during_the_pinned_copy(tmp_path, monkeypatch):
    import kiro_crew.project_capabilities as capabilities
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle")
    (bundle / "skills" / "deploy" / "payload.bin").write_bytes(b"123456789")
    manager = _manager(tmp_path, bundle)
    reviewed_key = manager.status(_PROJECT_ID).review_key
    monkeypatch.setattr(capabilities, "_PROJECT_SKILL_MAX_TREE_BYTES", 8)

    with pytest.raises(ProjectCapabilityError, match="too many bytes"):
        manager.activate(_PROJECT_ID, expected_key=reviewed_key)

    assert not (tmp_path / "data" / "skills" / "projects" / _PROJECT_ID).exists()


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="Project skill activation requires descriptor-pinned tree traversal",
)
def test_activation_bounds_skill_tree_entries_during_the_pinned_copy(tmp_path, monkeypatch):
    import kiro_crew.project_capabilities as capabilities
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle")
    (bundle / "skills" / "deploy" / "second.txt").write_text("second", encoding="utf-8")
    manager = _manager(tmp_path, bundle)
    reviewed_key = manager.status(_PROJECT_ID).review_key
    monkeypatch.setattr(capabilities, "_PROJECT_SKILL_MAX_TREE_ENTRIES", 1, raising=False)

    with pytest.raises(ProjectCapabilityError, match="too many entries"):
        manager.activate(_PROJECT_ID, expected_key=reviewed_key)

    assert not (tmp_path / "data" / "skills" / "projects" / _PROJECT_ID).exists()


def test_capability_namespaces_do_not_collide_on_a_shared_uuid_prefix():
    from kiro_crew.project_capabilities import ProjectCapabilityManager

    first = ProjectCapabilityManager._namespace(_PROJECT_ID, "reviewer")
    second = ProjectCapabilityManager._namespace(_COLLIDING_PREFIX_PROJECT_ID, "reviewer")

    assert first != second


def test_deactivation_removes_only_the_projects_materialized_capabilities(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle", include_skills=False)
    manager = _manager(tmp_path, bundle)
    unrelated = tmp_path / "kiro" / "agents" / "personal.json"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text('{"name":"personal"}', encoding="utf-8")
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    mcp_name = f"project-{_PROJECT_ID}-docs"
    rendered = {
        "mcpServers": {
            mcp_name: {
                "url": "https://mcp.example.invalid",
                "x-kirocrew-project": _PROJECT_ID,
            },
            "personal": {"url": "https://personal.example.invalid"},
        },
        "tools": [f"@{mcp_name}", "@personal"],
        "allowedTools": [f"@{mcp_name}"],
    }
    (tmp_path / "kiro" / "agents" / "kirocrew.json").write_text(
        json.dumps(rendered), encoding="utf-8"
    )

    status = manager.deactivate(_PROJECT_ID)

    assert status.active is False
    assert unrelated.is_file()
    assert not (tmp_path / "kiro" / "agents" / f"project--{_PROJECT_ID}--reviewer.json").exists()
    mcp = json.loads((tmp_path / "data" / "mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"] == {}
    rendered_after = json.loads(
        (tmp_path / "kiro" / "agents" / "kirocrew.json").read_text(encoding="utf-8")
    )
    assert rendered_after["mcpServers"] == {"personal": {"url": "https://personal.example.invalid"}}
    assert rendered_after["tools"] == ["@personal"]
    assert rendered_after["allowedTools"] == []


def test_deactivation_refuses_to_orphan_capabilities_from_corrupt_state(tmp_path, monkeypatch):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle", include_skills=False)
    manager = _manager(tmp_path, bundle)
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    agent_path = tmp_path / "kiro" / "agents" / f"project--{_PROJECT_ID}--reviewer.json"
    manager._state_path(_PROJECT_ID).write_text("{", encoding="utf-8")

    with pytest.raises(ProjectCapabilityError, match="activation record"):
        manager.deactivate(_PROJECT_ID)

    assert agent_path.is_file()
    assert manager._state_path(_PROJECT_ID).is_file()


def test_deactivation_rejects_activation_state_copied_from_another_project(tmp_path, monkeypatch):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    first = _bundle(tmp_path / "first", include_skills=False)
    second = _bundle(tmp_path / "second", include_skills=False)
    second_manifest = second / "project.yaml"
    second_manifest.write_text(
        second_manifest.read_text(encoding="utf-8").replace(
            _PROJECT_ID, _COLLIDING_PREFIX_PROJECT_ID
        ),
        encoding="utf-8",
    )
    manager = _manager(tmp_path, first)
    manager.registry.add_local(second)
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    agent_path = tmp_path / "kiro" / "agents" / f"project--{_PROJECT_ID}--reviewer.json"
    shutil.copy2(
        manager._state_path(_PROJECT_ID), manager._state_path(_COLLIDING_PREFIX_PROJECT_ID)
    )

    with pytest.raises(ProjectCapabilityError, match="activation record"):
        manager.deactivate(_COLLIDING_PREFIX_PROJECT_ID)

    assert agent_path.is_file()


def test_deactivation_rejects_activation_state_with_another_projects_output_namespace(
    tmp_path, monkeypatch
):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    manager = _manager(tmp_path, _bundle(tmp_path / "bundle", include_skills=False))
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    state_path = manager._state_path(_PROJECT_ID)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    original_path = next(iter(state["agents"]))
    foreign_path = str(tmp_path / "kiro" / "agents" / "project--foreign--reviewer.json")
    state["agents"] = {foreign_path: state["agents"][original_path]}
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ProjectCapabilityError, match="activation record"):
        manager.deactivate(_PROJECT_ID)

    assert Path(original_path).is_file()


def test_deactivation_preserves_a_reclaimed_markerless_project_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle", include_skills=False)
    manager = _manager(tmp_path, bundle)
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    mcp_name = f"project-{_PROJECT_ID}-docs"
    mcp = json.loads(manager.mcp_path.read_text(encoding="utf-8"))
    mcp["mcpServers"][mcp_name].pop("x-kirocrew-project")
    manager.mcp_path.write_text(json.dumps(mcp), encoding="utf-8")

    status = manager.deactivate(_PROJECT_ID)

    assert status.active is False
    after = json.loads(manager.mcp_path.read_text(encoding="utf-8"))
    assert after["mcpServers"][mcp_name] == {"url": "https://mcp.example.invalid"}


def test_rebuild_reconciliation_drops_revoked_project_mcp_from_a_stale_snapshot():
    from kiro_crew.project_capabilities import reconcile_project_mcp

    mcp_name = f"project-{_PROJECT_ID}-docs"
    stale = {
        "mcpServers": {
            mcp_name: {
                "url": "https://mcp.example.invalid",
                "x-kirocrew-project": _PROJECT_ID,
            },
            "personal": {"url": "https://personal.example.invalid"},
        },
        "tools": [f"@{mcp_name}", "@personal"],
        "allowedTools": [f"@{mcp_name}"],
    }

    reconcile_project_mcp(stale, {})

    assert stale["mcpServers"] == {"personal": {"url": "https://personal.example.invalid"}}
    assert stale["tools"] == ["@personal"]
    assert stale["allowedTools"] == []


def test_rebuild_detects_same_project_marker_configuration_change():
    from kiro_crew.project_capabilities import project_mcp_source_changed

    name = f"project-{_PROJECT_ID}-docs"
    before = {
        name: {
            "url": "https://old.example.invalid",
            "x-kirocrew-project": _PROJECT_ID,
        }
    }
    after = {
        name: {
            "url": "https://new.example.invalid",
            "x-kirocrew-project": _PROJECT_ID,
        }
    }

    assert project_mcp_source_changed(before, after) is True
    assert project_mcp_source_changed(after, after) is False


def test_rebuild_detects_a_project_mcp_reclaimed_during_its_snapshot():
    from kiro_crew.project_capabilities import project_mcp_source_changed

    name = f"project-{_PROJECT_ID}-docs"
    before = {
        name: {
            "url": "https://old.example.invalid",
            "x-kirocrew-project": _PROJECT_ID,
        }
    }
    reclaimed = {name: {"url": "https://user.example.invalid"}}

    assert project_mcp_source_changed(before, reclaimed) is True


def test_rebuild_reconciliation_releases_locally_reclaimed_project_mcp():
    from kiro_crew.project_capabilities import reconcile_project_mcp

    mcp_name = f"project-{_PROJECT_ID}-docs"
    rendered = {
        "mcpServers": {
            mcp_name: {
                "url": "https://user.example.invalid",
                "x-kirocrew-project": _PROJECT_ID,
            }
        },
        "tools": [f"@{mcp_name}"],
    }
    source = {mcp_name: {"url": "https://user.example.invalid"}}

    reconcile_project_mcp(rendered, source)

    assert rendered["mcpServers"][mcp_name] == {"url": "https://user.example.invalid"}
    assert rendered["tools"] == [f"@{mcp_name}"]


def test_deactivation_preserves_a_locally_modified_project_agent(tmp_path, monkeypatch):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle", include_skills=False)
    manager = _manager(tmp_path, bundle)
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    agent_path = tmp_path / "kiro" / "agents" / f"project--{_PROJECT_ID}--reviewer.json"
    agent_path.write_text('{"name":"my-local-edit"}', encoding="utf-8")

    with pytest.raises(ProjectCapabilityError, match="modified locally"):
        manager.deactivate(_PROJECT_ID)

    assert agent_path.read_text(encoding="utf-8") == '{"name":"my-local-edit"}'
    assert manager.status(_PROJECT_ID).active is True


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="Project skill activation requires descriptor-pinned tree traversal",
)
def test_deactivation_refuses_a_replaced_project_skill_root(tmp_path, monkeypatch):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle")
    manager = _manager(tmp_path, bundle)
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    installed = tmp_path / "data" / "skills" / "projects" / _PROJECT_ID
    moved = tmp_path / "moved-skills"
    installed.rename(moved)
    make_dir_link(installed, moved)

    with pytest.raises(ProjectCapabilityError, match="link"):
        manager.deactivate(_PROJECT_ID)

    assert moved.is_dir()
    assert manager.status(_PROJECT_ID).active is True


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="Project skill activation requires descriptor-pinned tree traversal",
)
def test_deactivation_bounds_digest_of_a_tampered_project_skill_tree(tmp_path, monkeypatch):
    import kiro_crew.project_capabilities as capabilities
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle")
    manager = _manager(tmp_path, bundle)
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    installed = tmp_path / "data" / "skills" / "projects" / _PROJECT_ID
    (installed / "oversized.bin").write_bytes(b"123456789")
    monkeypatch.setattr(capabilities, "_PROJECT_SKILL_MAX_TREE_BYTES", 8)

    with pytest.raises(ProjectCapabilityError, match="too many bytes"):
        manager.deactivate(_PROJECT_ID)

    assert installed.is_dir()
    assert manager._state_path(_PROJECT_ID).is_file()


def test_activation_preserves_an_untracked_skill_destination(tmp_path, monkeypatch):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle")
    manager = _manager(tmp_path, bundle)
    destination = tmp_path / "data" / "skills" / "projects" / _PROJECT_ID
    destination.mkdir(parents=True)
    sentinel = destination / "personal.txt"
    sentinel.write_text("keep me\n", encoding="utf-8")

    with pytest.raises(ProjectCapabilityError, match="destination already exists"):
        manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)

    assert sentinel.read_text(encoding="utf-8") == "keep me\n"


def test_activation_rejects_a_planted_skill_destination_ancestor(tmp_path, monkeypatch):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    manager = _manager(tmp_path, _bundle(tmp_path / "bundle"))
    reviewed_key = manager.status(_PROJECT_ID).review_key
    projects_root = tmp_path / "data" / "skills" / "projects"
    projects_root.parent.mkdir(parents=True)
    external = tmp_path / "external-skills"
    external.mkdir()
    make_dir_link(projects_root, external)

    with pytest.raises(ProjectCapabilityError, match="link"):
        manager.activate(_PROJECT_ID, expected_key=reviewed_key)

    assert not any(external.iterdir())
    assert not (tmp_path / "kiro" / "agents" / f"project--{_PROJECT_ID}--reviewer.json").exists()
    assert not manager._state_path(_PROJECT_ID).exists()


@pytest.mark.skipif(
    platform_compat.trusted_git_bin() is None,
    reason="trusted Git executable is unavailable",
)
def test_activation_materializes_declared_repo_sources(tmp_path, monkeypatch):
    def passthrough(
        argv: list[str], *, mode: str, env: dict[str, str] | None = None
    ) -> tuple[list[str], dict[str, str], None]:
        return list(argv), dict(env or {}), None

    monkeypatch.setattr("kiro_crew.project_git.sandboxed_spawn_argv", passthrough)
    git = platform_compat.trusted_git_bin()
    assert git is not None
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "README.md").write_text("payments api\n", encoding="utf-8")
    subprocess.run([git, "init"], cwd=remote, check=True, capture_output=True)
    subprocess.run([git, "add", "README.md"], cwd=remote, check=True, capture_output=True)
    subprocess.run(
        [
            git,
            "-c",
            "user.name=Project Tests",
            "-c",
            "user.email=projects@example.invalid",
            "commit",
            "-m",
            "initial",
        ],
        cwd=remote,
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(
        tmp_path / "bundle",
        sources=[{"id": "payments-api", "type": "repo", "url": remote.as_uri()}],
        include_skills=False,
    )
    manager = _manager(tmp_path, bundle)

    status = manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)

    assert status.inventory.repos == 1
    expected = tmp_path / "data" / "projects" / "state" / _PROJECT_ID / "sources" / "payments-api"
    assert status.repositories == (("payments-api", str(expected)),)
    assert (expected / "README.md").read_text(encoding="utf-8") == "payments api\n"


@pytest.mark.parametrize(
    "agent,message",
    [
        (
            {"name": "reviewer", "hooks": {"preToolUse": [{"command": "read-secret"}]}},
            "hooks",
        ),
        (
            {"name": "reviewer", "prompt": "file:///project-context/prompt.md"},
            "file-backed prompt",
        ),
        (
            {
                "name": "reviewer",
                "resources": ["file:///project-context/resource.md"],
            },
            "file-backed resources",
        ),
    ],
)
def test_activation_rejects_agent_hooks_and_file_backed_context(
    tmp_path, monkeypatch, agent, message
):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    manager = _manager(
        tmp_path,
        _bundle(tmp_path / "bundle", agent=agent, include_skills=False),
    )

    with pytest.raises(ProjectCapabilityError, match=message):
        manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)

    assert not (tmp_path / "kiro" / "agents").exists()


def test_status_refuses_a_missing_materialized_project_agent(tmp_path, monkeypatch):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    manager = _manager(tmp_path, _bundle(tmp_path / "bundle", include_skills=False))
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    installed = tmp_path / "kiro" / "agents" / f"project--{_PROJECT_ID}--reviewer.json"
    installed.unlink()

    with pytest.raises(ProjectCapabilityError, match="missing"):
        manager.status(_PROJECT_ID)


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="Project skill activation requires descriptor-pinned tree traversal",
)
def test_status_refuses_a_missing_materialized_project_skill_tree(tmp_path, monkeypatch):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    manager = _manager(tmp_path, _bundle(tmp_path / "bundle"))
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    installed = tmp_path / "data" / "skills" / "projects" / _PROJECT_ID
    shutil.rmtree(installed)

    with pytest.raises(ProjectCapabilityError, match="missing"):
        manager.status(_PROJECT_ID)


def test_status_refuses_a_missing_materialized_project_mcp_entry(tmp_path, monkeypatch):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    manager = _manager(tmp_path, _bundle(tmp_path / "bundle", include_skills=False))
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    manager.mcp_path.unlink()

    with pytest.raises(ProjectCapabilityError, match="missing"):
        manager.status(_PROJECT_ID)


@pytest.mark.parametrize(
    "agent,mcp,message",
    [
        ({"name": "reviewer", "allowedTools": ["shell"]}, None, "allowedTools"),
        (
            {"name": "reviewer", "toolsSettings": {"shell": {"autoAllowReadonly": True}}},
            None,
            "toolsSettings",
        ),
        (
            {
                "name": "reviewer",
                "mcpServers": {"docs": {"command": "node", "args": ["server.js"]}},
            },
            None,
            "cannot declare mcpServers",
        ),
        (
            None,
            {
                "mcpServers": {
                    "docs": {
                        "url": "https://example.invalid",
                        "headers": {"Authorization": "secret"},
                    }
                }
            },
            "credential-bearing",
        ),
        (
            None,
            {"mcpServers": {"docs": {"command": "node", "args": [], "autoApprove": ["read"]}}},
            "autoApprove",
        ),
        (
            {
                "name": "reviewer",
                "mcpServers": {"docs": {"url": "https://token@example.invalid/context"}},
            },
            None,
            "cannot declare mcpServers",
        ),
    ],
)
def test_activation_rejects_embedded_grants_and_credentials(
    tmp_path, monkeypatch, agent, mcp, message
):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    bundle = _bundle(tmp_path / "bundle", agent=agent, mcp=mcp)
    manager = _manager(tmp_path, bundle)

    with pytest.raises(ProjectCapabilityError, match=message):
        manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)

    assert manager.status(_PROJECT_ID).active is False


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="Project skill depth verification requires descriptor-pinned tree traversal",
)
def test_skill_tree_depth_is_bounded_before_python_recursion(tmp_path):
    from kiro_crew.project_capabilities import ProjectCapabilityError, _tree_digest

    root = tmp_path / "skill"
    root.mkdir()
    current = root
    for _ in range(65):
        current /= "d"
        current.mkdir()

    with pytest.raises(ProjectCapabilityError, match="too deeply nested"):
        _tree_digest(root)


def test_active_project_refuses_a_primary_materialization_change(tmp_path, monkeypatch):
    from kiro_crew.project_capabilities import ProjectCapabilityError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    first = _bundle(tmp_path / "first", include_skills=False)
    manager = _manager(tmp_path, first)
    manager.activate(_PROJECT_ID, expected_key=manager.status(_PROJECT_ID).review_key)
    second = _bundle(tmp_path / "second", include_skills=False)

    with pytest.raises(ProjectCapabilityError, match="Deactivate the Project"):
        manager.register_local(second)

    project = manager.registry.get(_PROJECT_ID)
    assert [registration.path for registration in project.registrations] == [first.resolve()]
    assert (tmp_path / "kiro" / "agents" / f"project--{_PROJECT_ID}--reviewer.json").exists()
    assert manager.status(_PROJECT_ID).active is True
