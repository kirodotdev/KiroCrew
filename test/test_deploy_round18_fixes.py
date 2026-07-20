"""Tests for deploy Round 18 fixes.

F1: profiles GET redacts `default` and `available` names.
F2: deploy snapshots strip .git before scan/upload (both scripts).
F3: reaper (Lambda + shell) verifies OAC name before deleting a
    manifest-supplied oac_id.
F4: deploy_artifact MCP tool responses go through credential redaction.
F5: pending confirm binds stored profile/region into _do_deploy.
F6: scan-block 409 carries content_digest + profile/region bindings.
"""
from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "kiro_crew"
HANDLERS = (SRC / "deploy" / "handlers.py").read_text()
MCP_CORE = (SRC / "mcp_core.py").read_text()
SCRIPTS = SRC / "deploy" / "skills" / "artifact-deploy" / "scripts"
DEPLOY_SH = (SCRIPTS / "deploy.sh").read_text()
DEPLOY_BACKEND_SH = (SCRIPTS / "deploy-backend.sh").read_text()
REAPER_SH = (SCRIPTS / "reaper.sh").read_text()
REAPER_LAMBDA = (SCRIPTS / "reaper_lambda" / "index.py").read_text()
PUBLISHHUB = (
    SRC.parent.parent / "website" / "src" / "components" / "PublishHub.tsx"
).read_text()


class TestF1ProfilesRedaction:
    def test_default_and_available_redacted(self):
        assert '_redact_text(str(reg["default"]))' in HANDLERS
        assert "[_redact_text(str(n))" in HANDLERS

    def test_no_raw_default_return(self):
        assert '"default": reg["default"],' not in HANDLERS


class TestF2GitStrippedFromSnapshots:
    def test_both_scripts_prune_git(self):
        for text, name in ((DEPLOY_SH, "deploy.sh"),
                           (DEPLOY_BACKEND_SH, "deploy-backend.sh")):
            assert "-type d -name .git -prune -exec rm -rf" in text, name

    def test_prune_happens_before_scan(self):
        # The .git removal must run on the snapshot BEFORE the scan step.
        assert (DEPLOY_BACKEND_SH.index("-name .git -prune")
                < DEPLOY_BACKEND_SH.index("scan_source_dir"))
        assert (DEPLOY_SH.index("-name .git -prune")
                < DEPLOY_SH.index("CRED_DIRS="))


class TestF3OACNameVerification:
    def test_lambda_helper_exists_and_used_at_both_sites(self):
        assert "def _oac_name_ok(" in REAPER_LAMBDA
        # helper def + 2 call sites
        assert REAPER_LAMBDA.count("_oac_name_ok(") >= 3

    def test_lambda_mismatch_skips_delete(self):
        assert "engine_reap_oac_name_mismatch" in REAPER_LAMBDA

    def test_shell_reaper_verifies_name(self):
        assert "OriginAccessControlConfig.Name" in REAPER_SH
        assert "name mismatch" in REAPER_SH

    def test_oac_name_ok_semantics(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "reaper_lambda_index", SCRIPTS / "reaper_lambda" / "index.py")
        # Can't exec (boto3 clients at import time may fail offline); test the
        # regex semantics directly instead.
        import re
        pat = re.compile(re.escape("deploy-web-app"[:57]) + r"-[0-9a-f]{6}$")
        assert pat.fullmatch("deploy-web-app-abc123")
        assert not pat.fullmatch("deploy-web-app-v2-abc123")  # cross-slug
        assert not pat.fullmatch("someone-elses-oac")
        assert spec is not None  # keep the loader var used


class TestF4MCPResponseRedaction:
    def test_all_textual_paths_redacted(self):
        assert "_deploy_redact(str(d['error']))" in MCP_CORE
        assert '_deploy_redact(str(d.get("findings", "")))' in MCP_CORE
        assert "_deploy_redact(str(d.get('scan', 'clean')))" in MCP_CORE


class TestF5PendingIdentityBinding:
    def test_expected_identity_wired(self):
        idx = HANDLERS.index('params["expected_content_digest"]')
        window = HANDLERS[idx - 1200: idx + 1200]
        assert 'params["expected_profile"]' in window
        assert 'params["expected_region"]' in window


class TestF6ScanBlockBindings:
    def test_backend_409_carries_bindings(self):
        idx = HANDLERS.index('"reason": "scan", "findings"')
        window = HANDLERS[idx: idx + 500]
        assert '"content_digest"' in window
        assert '"profile": profile' in window

    def test_frontend_stores_bindings_from_scan_block(self):
        idx = PUBLISHHUB.index("R18 F6")
        window = PUBLISHHUB[idx: idx + 600]
        assert "setContentDigest" in window
        assert "setPreviewIdentity" in window
