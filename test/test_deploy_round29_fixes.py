"""Tests for deploy Round 29 fixes: pod real-deploy bugs.

F1: _allowed_local_roots includes config_dir workspace + registered workspaces
F2: Boundary preflight + dead-stack detection in deploy-backend.sh / install-reaper.sh
F3: IdentitySource is a YAML list (not scalar) in API GW templates
F4: cfn-lint gate (tested structurally via ci.yml presence)
F5: SKILL.md metadata-backfill ordering note
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from kiro_crew.deploy import handlers

SCRIPTS_DIR = (
    Path(__file__).parent.parent
    / "src"
    / "kiro_crew"
    / "deploy"
    / "skills"
    / "artifact-deploy"
    / "scripts"
)
TEMPLATES_DIR = (
    Path(__file__).parent.parent
    / "src"
    / "kiro_crew"
    / "deploy"
    / "skills"
    / "artifact-deploy"
    / "templates"
)
SKILL_MD = (
    Path(__file__).parent.parent
    / "src"
    / "kiro_crew"
    / "deploy"
    / "skills"
    / "artifact-deploy"
    / "SKILL.md"
)


# ── F1: _allowed_local_roots includes config_dir/workspace ──────────────


class TestF1AllowedLocalRoots:
    """_allowed_local_roots includes agent config-dir workspace and registered workspaces."""

    def test_config_dir_workspace_is_included(self, tmp_path, monkeypatch):
        """config_dir()/workspace is in allowed roots when it exists."""
        fake_config = tmp_path / "fake_kirocrew"
        ws_dir = fake_config / "workspace"
        ws_dir.mkdir(parents=True)

        monkeypatch.setenv("KIROCREW_HOME", str(fake_config))
        # Clear any cache on the config loader
        roots = handlers._allowed_local_roots()
        resolved = [r.resolve() for r in roots]
        assert ws_dir.resolve() in resolved

    def test_registered_workspace_dirs_are_included(self, tmp_path, monkeypatch):
        """Workspace dirs from cfg.workspaces are in allowed roots."""
        fake_config = tmp_path / "fake_kirocrew"
        fake_config.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(fake_config))

        custom_ws = tmp_path / "my-workspace"
        custom_ws.mkdir()

        # Write a config.json with a workspace entry
        import json
        config_file = fake_config / "config.json"
        config_file.write_text(json.dumps({
            "workspaces": {"myws": {"dir": str(custom_ws)}},
        }))

        roots = handlers._allowed_local_roots()
        resolved = [r.resolve() for r in roots]
        assert custom_ws.resolve() in resolved

    def test_unregistered_path_still_rejected(self, tmp_path, monkeypatch):
        """An arbitrary path not in any root set is rejected."""
        fake_config = tmp_path / "fake_kirocrew"
        fake_config.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(fake_config))

        rogue = tmp_path / "rogue" / "dir"
        rogue.mkdir(parents=True)

        roots = handlers._allowed_local_roots()
        resolved = [r.resolve() for r in roots]
        # The rogue dir should NOT be in roots (unless it happens to be
        # under one of the fallback paths, which tmp_path won't be).
        assert rogue.resolve() not in resolved


# ── F2: Boundary preflight + dead-stack detection in scripts ─────────────


class TestF2BoundaryPreflight:
    """deploy-backend.sh and install-reaper.sh contain boundary preflight."""

    @pytest.mark.parametrize("script_name", ["deploy-backend.sh", "install-reaper.sh"])
    def test_boundary_preflight_before_first_deploy(self, script_name):
        """Boundary check appears before any cloudformation deploy call."""
        script = SCRIPTS_DIR / script_name
        content = script.read_text()
        lines = content.splitlines()

        # Find line indices
        boundary_idx = None
        first_deploy_idx = None
        for i, line in enumerate(lines):
            if "kirocrew-deploy-app-boundary" in line and boundary_idx is None:
                boundary_idx = i
            if "cloudformation deploy" in line and first_deploy_idx is None:
                first_deploy_idx = i

        assert boundary_idx is not None, f"No boundary check in {script_name}"
        assert first_deploy_idx is not None, f"No cloudformation deploy in {script_name}"
        assert boundary_idx < first_deploy_idx, (
            f"Boundary check (line {boundary_idx}) must come BEFORE first "
            f"cloudformation deploy (line {first_deploy_idx}) in {script_name}"
        )

    @pytest.mark.parametrize("script_name", ["deploy-backend.sh", "install-reaper.sh"])
    def test_dead_stack_detection_present(self, script_name):
        """ROLLBACK_COMPLETE detection is present."""
        script = SCRIPTS_DIR / script_name
        content = script.read_text()
        assert "ROLLBACK_COMPLETE" in content, (
            f"Dead-stack ROLLBACK_COMPLETE detection missing from {script_name}"
        )

    @pytest.mark.parametrize("script_name", ["deploy-backend.sh", "install-reaper.sh"])
    def test_bash_syntax_valid(self, script_name):
        """bash -n validates script syntax."""
        script = SCRIPTS_DIR / script_name
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True
        )
        assert result.returncode == 0, f"bash -n failed for {script_name}: {result.stderr}"


# ── F3: IdentitySource is a list ─────────────────────────────────────────


def _load_cfn_yaml(path: Path) -> dict:
    """Load a CloudFormation YAML template, handling intrinsic function tags."""
    # Add constructors for common CFN tags so safe_load doesn't choke
    class CfnLoader(yaml.SafeLoader):
        pass

    cfn_tags = [
        "!Ref", "!Sub", "!GetAtt", "!Join", "!Select", "!Split",
        "!If", "!Not", "!Equals", "!And", "!Or", "!Condition",
        "!FindInMap", "!Base64", "!Cidr", "!ImportValue",
        "!GetAZs", "!Transform",
    ]
    for tag in cfn_tags:
        CfnLoader.add_constructor(
            tag, lambda loader, node: loader.construct_scalar(node)
            if isinstance(node, yaml.ScalarNode) else loader.construct_sequence(node)
            if isinstance(node, yaml.SequenceNode) else loader.construct_mapping(node)
        )
    # Also handle multi-constructor for tag variants
    CfnLoader.add_multi_constructor(
        "!", lambda loader, suffix, node: loader.construct_scalar(node)
        if isinstance(node, yaml.ScalarNode) else loader.construct_sequence(node)
        if isinstance(node, yaml.SequenceNode) else loader.construct_mapping(node)
    )

    with open(path) as f:
        return yaml.load(f, Loader=CfnLoader)


class TestF3IdentitySourceList:
    """IdentitySource must be a YAML list (CFN schema requires it)."""

    @pytest.mark.parametrize("template_name", ["app-apigw.yaml", "app-apigw-ddb.yaml"])
    def test_identity_source_is_list(self, template_name):
        """IdentitySource in Authorizer resource is a list, not a scalar."""
        template_path = TEMPLATES_DIR / template_name
        doc = _load_cfn_yaml(template_path)

        resources = doc.get("Resources", {})
        authorizer = resources.get("OriginVerifyAuthorizer", {})
        props = authorizer.get("Properties", {})
        identity_source = props.get("IdentitySource")

        assert isinstance(identity_source, list), (
            f"IdentitySource in {template_name} must be a list, "
            f"got {type(identity_source).__name__}: {identity_source!r}"
        )
        assert len(identity_source) >= 1


# ── F4: CI workflow has cfn-lint job ─────────────────────────────────────


class TestF4CfnLintCI:
    """CI workflow includes a cfn-lint job."""

    def test_ci_has_cfn_lint_job(self):
        """ci.yml contains a cfn-lint job that lints deploy templates."""
        ci_path = (
            Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
        )
        content = ci_path.read_text()
        assert "cfn-lint" in content
        assert "Lint deploy templates" in content


# ── F5: SKILL.md metadata-backfill ordering ──────────────────────────────


class TestF5SkillMdOrdering:
    """SKILL.md documents metadata-backfill-before-verify ordering."""

    def test_metadata_before_verify_documented(self):
        """The skill doc mentions backfilling metadata before endpoint verification."""
        content = SKILL_MD.read_text()
        # Check that the ordering note is present
        assert "metadata" in content.lower()
        assert "before" in content.lower()
        # Specifically check our added note
        assert "webapp_metadata" in content
        assert "endpoint verification" in content.lower() or "endpoint check" in content.lower()
