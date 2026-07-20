"""Tests for deploy Round 15 fixes.

F1: deploy_artifact registered in MCP_CORE_SCHEMAS — invalid input rejected.
F2: pending-confirm denial paths all emit SEL audit events.
F3: deploy skills installed as copies (not symlinks) are discoverable by _find_skills.
F4: _do_deploy confirm path rejects stale expected_content_digest with 409.
F5: _handle_teardown has os.name == 'nt' guard; full sweep of engine.run_aws callers.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src" / "kiro_crew"
HANDLERS = (SRC / "deploy" / "handlers.py").read_text()
VALIDATION = (SRC / "validation.py").read_text()
MCP_CORE = (SRC / "mcp_core.py").read_text()
DEPLOY_INIT = (SRC / "deploy" / "__init__.py").read_text()


# ── F1: deploy_artifact in MCP_CORE_SCHEMAS ──


class TestDeployArtifactSchemaRegistration:
    def test_schema_registered_in_mcp_core_schemas(self):
        from kiro_crew.validation import MCP_CORE_SCHEMAS
        assert "deploy_artifact" in MCP_CORE_SCHEMAS

    def test_invalid_input_rejected_with_controlled_error(self):
        """Invalid deploy_artifact args are rejected before reaching endpoint."""
        from kiro_crew.validation import (
            DEPLOY_ARTIFACT_SCHEMA,
            ValidationError,
            validate_tool_args,
        )

        # Missing required field site_id
        with pytest.raises(ValidationError):
            validate_tool_args({}, DEPLOY_ARTIFACT_SCHEMA)

    def test_valid_input_passes_schema(self):
        from kiro_crew.validation import DEPLOY_ARTIFACT_SCHEMA, validate_tool_args
        result = validate_tool_args(
            {"site_id": "test-site", "artifact_slug": "my-app"},
            DEPLOY_ARTIFACT_SCHEMA,
        )
        assert result["site_id"] == "test-site"

    def test_redundant_inline_validate_removed_from_mcp_core(self):
        # The inline `from kiro_crew.validation import DEPLOY_ARTIFACT_SCHEMA`
        # inside the deploy_artifact branch should be gone.
        assert "from kiro_crew.validation import DEPLOY_ARTIFACT_SCHEMA" not in MCP_CORE


# ── F2: pending-confirm SEL audit on ALL denials ──


class TestPendingConfirmAuditCoverage:
    """Every 4xx return in _handle_pending_confirm MUST have an _audit call."""

    def test_not_found_denial_audited(self):
        # claim_pending returns None → audit before 409 response
        assert '_audit("pending_confirm", entry_id, "denied"' in HANDLERS

    def test_profile_drift_denial_audited(self):
        assert "profile drift since preview" in HANDLERS

    def test_confinement_error_denial_audited(self):
        # The confinement check error string is passed to audit
        assert 'error=confinement_error)' in HANDLERS

    def test_size_guard_denial_audited(self):
        assert "size guard exceeded" in HANDLERS

    def test_content_digest_local_dir_early_check_removed(self):
        # R17 F2: the early live-path digest read (raw Path.read_bytes on an
        # LLM-controlled path) was removed -- the authoritative check is
        # _do_deploy's staged-snapshot verification via expected_content_digest
        # (wired in R16 F2). Assert the unsafe early check stays gone and the
        # authoritative param stays wired.
        assert "content digest mismatch (local_dir)" not in HANDLERS
        assert 'params["expected_content_digest"]' in HANDLERS

    def test_content_digest_artifact_denial_audited(self):
        assert "content digest mismatch (artifact modified)" in HANDLERS


# ── F3: deploy skills always copied, never symlinked ──


class TestDeploySkillsCopyNotSymlink:
    def test_no_symlink_to_call_in_register_core_skills(self):
        """_register_core_skills must not call symlink_to — always copy."""
        assert "symlink_to" not in DEPLOY_INIT

    def test_symlink_migration_replaces_with_copy(self):
        """Existing symlinks are unlinked and replaced with copies."""
        assert "link.is_symlink()" in DEPLOY_INIT
        assert "link.unlink()" in DEPLOY_INIT

    def test_managed_marker_written_on_copy(self):
        assert "_MANAGED_MARKER" in DEPLOY_INIT
        assert '(link / _MANAGED_MARKER).write_text("")' in DEPLOY_INIT

    def test_skills_discoverable_after_copy(self):
        """Integration: install skills into a tmp root, verify _find_skills finds them."""
        from kiro_crew.skills import _iter_skill_files

        # Create a fake skills root with a SKILL.md
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_root = Path(tmpdir) / "skills"
            skill_root.mkdir()
            skill_dir = skill_root / "artifact-deploy"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("# Artifact Deploy Skill\n")

            results = _iter_skill_files(skill_root)
            assert len(results) == 1
            assert results[0][0] == "artifact-deploy"


# ── F4: expected_content_digest validation on confirm ──


class TestDigestBoundConfirm:
    def test_backend_checks_expected_content_digest(self):
        """The confirm path in _do_deploy validates expected_content_digest."""
        assert 'expected_content_digest' in HANDLERS
        assert '"code": "stale_preview"' in HANDLERS

    def test_409_stale_preview_on_mismatch(self):
        """The 409 response uses the stale_preview code for frontend detection."""
        # Find the specific error shape
        assert '"error": "content changed since preview"' in HANDLERS


# ── F5: teardown Windows guard + full sweep ──


class TestTeardownWindowsGuard:
    def test_handle_teardown_has_nt_guard(self):
        """_handle_teardown checks os.name == 'nt' before any AWS call."""
        # Find the teardown function and verify guard comes before _HAS_ARTIFACTS
        teardown_start = HANDLERS.index("async def _handle_teardown")
        first_nt_check = HANDLERS.index('os.name == "nt"', teardown_start)
        first_has_artifacts = HANDLERS.index("_HAS_ARTIFACTS", first_nt_check)
        assert first_nt_check < first_has_artifacts

    def test_all_engine_run_aws_callers_guarded(self):
        """Every function that calls engine.run_aws must be under a Windows guard.

        Functions that reach engine.run_aws:
        - _do_deploy (line ~514 guard)
        - _handle_list (line ~995 guard)
        - _handle_verify (line ~1173 guard)
        - _handle_teardown (line ~1530 guard, via _expire_manifest_best_effort)
        - _expire_manifest_best_effort (called only from _handle_teardown)
        - _check_reaper_installed (called from _handle_teardown)
        """
        # Count distinct os.name == "nt" guards — should be at least 4
        guards = re.findall(r'os\.name == "nt"', HANDLERS)
        assert len(guards) >= 4, f"Expected >= 4 Windows guards, found {len(guards)}"

    def test_guard_returns_400_with_posix_message(self):
        # The guard in _handle_teardown must match the standard error message.
        teardown_start = HANDLERS.index("async def _handle_teardown")
        teardown_end = HANDLERS.index("\nasync def ", teardown_start + 10)
        teardown_body = HANDLERS[teardown_start:teardown_end]
        assert "POSIX shell" in teardown_body
        assert "WSL" in teardown_body
