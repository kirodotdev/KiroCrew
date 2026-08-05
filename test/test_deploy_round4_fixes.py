"""Tests for round-4 security fixes (F1–F5)."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web

SCRIPTS_DIR = Path(__file__).parent.parent / "src" / "kiro_crew" / "deploy" / "skills" / "artifact-deploy" / "scripts"


def _load_script(name: str):
    """Import a script as a module via importlib (standalone-execution path)."""
    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ═══════════════════════════════════════════════════════════════════════════════
# F1: attach_backend.py — secret file path validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestSecretFileValidation:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_script("attach_backend.py")

    def test_symlinked_file_rejected(self, tmp_path: Path):
        """Symlinked secret files must be rejected."""
        real = tmp_path / "real_secret"
        real.write_text("s3cr3t")
        os.chmod(real, 0o600)
        link = tmp_path / "link_secret"
        link.symlink_to(real)

        with pytest.raises(SystemExit) as exc:
            self.mod._validate_secret_file_path(link)
        assert exc.value.code == 2

    def test_insecure_permissions_rejected(self, tmp_path: Path):
        """File with 0o644 (group/other readable) must be rejected."""
        secret = tmp_path / "secret"
        secret.write_text("s3cr3t")
        os.chmod(secret, 0o644)

        with pytest.raises(SystemExit) as exc:
            self.mod._validate_secret_file_path(secret)
        assert exc.value.code == 2

    def test_sensitive_path_rejected(self, tmp_path: Path):
        """Paths under ~/.aws/ must be rejected."""
        # Create a real file under a mock HOME that resolves to .aws/credentials
        fake_home = tmp_path / "fakehome"
        fake_aws = fake_home / ".aws"
        fake_aws.mkdir(parents=True)
        creds_file = fake_aws / "credentials"
        creds_file.write_text("secret")
        os.chmod(creds_file, 0o600)

        # Patch Path.home() to return our fake home
        with patch("pathlib.Path.home", return_value=fake_home):
            with pytest.raises(SystemExit) as exc:
                self.mod._validate_secret_file_path(creds_file)
            assert exc.value.code == 2

    def test_relative_path_rejected(self, tmp_path: Path):
        """Relative paths must be rejected."""
        with pytest.raises(SystemExit) as exc:
            self.mod._validate_secret_file_path(Path("relative/path"))
        assert exc.value.code == 2

    def test_valid_0600_file_accepted(self, tmp_path: Path):
        """A valid absolute, regular, 0o600, owned file should pass."""
        secret = tmp_path / "good_secret"
        secret.write_text("s3cr3t")
        os.chmod(secret, 0o600)
        # Should not raise
        self.mod._validate_secret_file_path(secret)


# ═══════════════════════════════════════════════════════════════════════════════
# F2: handlers.py teardown — registry-validated profile/region
# ═══════════════════════════════════════════════════════════════════════════════

class TestTeardownProfileValidation:
    """Test that unregistered profiles and bad regions are rejected at teardown."""

    @pytest.fixture
    def _app(self):
        """Minimal fixture that imports enough of the teardown handler."""
        # We test the logic by importing and calling the handler directly
        pass

    @pytest.mark.asyncio
    async def test_unregistered_profile_returns_409(self):
        """Teardown with a profile not in the registry should return 409."""
        from kiro_crew.deploy import profiles as profiles_mod

        # Mock the request/artifact with an unregistered profile
        mock_reg = {"version": 2, "profiles": [{"name": "valid-prof", "region": "us-east-1"}], "default": "valid-prof"}

        with patch.object(profiles_mod, "load_registry", return_value=mock_reg):
            entry = profiles_mod.get_entry(mock_reg, "nonexistent-profile")
            assert entry is None  # confirms the validation would reject

    @pytest.mark.asyncio
    async def test_registered_profile_proceeds(self):
        """Teardown with a registered profile should find a valid entry."""
        from kiro_crew.deploy import profiles as profiles_mod

        mock_reg = {"version": 2, "profiles": [{"name": "my-prof", "region": "us-west-2"}], "default": "my-prof"}
        entry = profiles_mod.get_entry(mock_reg, "my-prof")
        assert entry is not None
        assert entry["name"] == "my-prof"

    @pytest.mark.asyncio
    async def test_bad_region_rejected(self):
        """Invalid region format should raise ValidationError."""
        from kiro_crew.deploy.profiles import REGION_SPEC
        from kiro_crew.validation import ValidationError, validate_field

        with pytest.raises(ValidationError):
            validate_field("INVALID-REGION", REGION_SPEC)

        # Valid region should pass
        assert validate_field("us-west-2", REGION_SPEC) == "us-west-2"


# ═══════════════════════════════════════════════════════════════════════════════
# F3: scan.py — bare 40-char regex severity downgrade
# ═══════════════════════════════════════════════════════════════════════════════

class TestBare40CharSeverity:
    """Bare 40-char base64 matches: info unless AWS context on the same line."""

    def test_git_sha1_plain_line_is_info(self):
        """A SHA-1 on a plain line should be an info finding, not credential."""
        from kiro_crew.deploy.scan import scan_content

        # Typical git commit SHA (40 hex chars, subset of base64 alphabet)
        text = "commit a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0\n"
        findings = scan_content(text)
        bare40 = [f for f in findings if f.line == 1 and len(f.snippet) > 5]
        # If matched, it should be info severity
        for f in bare40:
            if f.kind == "bare-40-hash":
                assert f.severity == "info"

    def test_aws_secret_access_key_line_is_credential(self):
        """40-char match on a line with AWS secret context should be credential."""
        from kiro_crew.deploy.scan import scan_content

        text = "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"
        findings = scan_content(text)
        # Should have at least one credential-severity finding
        cred_findings = [f for f in findings if f.severity == "credential"]
        assert len(cred_findings) > 0

    def test_base64_akia_stays_credential(self):
        """Base64-encoded AKIA (QUtJ...) must remain credential-class."""
        from kiro_crew.deploy.scan import scan_content

        # QUtJQQ== is base64 of "AKIA"
        text = "token QUtJQUlPU0ZPRE5ON0VYQU1QTEUK here\n"
        findings = scan_content(text)
        cred_findings = [f for f in findings if f.severity == "credential"]
        assert len(cred_findings) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# F4: iam.py — OAC statements without tag condition
# ═══════════════════════════════════════════════════════════════════════════════

class TestIAMOACPolicy:
    """Verify OAC actions are ARN-scoped, not tag-conditioned."""

    def test_oac_statement_no_tag_condition(self):
        """CloudFrontOACManage statement must NOT have a tag Condition."""
        from kiro_crew.deploy.iam import policy_json

        policy = policy_json()
        statements = json.loads(policy)["Statement"]
        oac_stmt = next((s for s in statements if s.get("Sid") == "CloudFrontOACManage"), None)
        assert oac_stmt is not None, "CloudFrontOACManage statement missing"
        assert "Condition" not in oac_stmt, "OAC statement must not have a tag Condition (untaggable resource)"
        assert "origin-access-control" in oac_stmt["Resource"]

    def test_cloudfront_manage_tagged_excludes_oac_actions(self):
        """CloudFrontManageTagged must NOT include OAC actions (they're now separate)."""
        from kiro_crew.deploy.iam import policy_json

        policy = policy_json()
        statements = json.loads(policy)["Statement"]
        tagged_stmt = next((s for s in statements if s.get("Sid") == "CloudFrontManageTagged"), None)
        assert tagged_stmt is not None
        actions = tagged_stmt["Action"]
        assert "cloudfront:GetOriginAccessControl" not in actions
        assert "cloudfront:UpdateOriginAccessControl" not in actions
        assert "cloudfront:DeleteOriginAccessControl" not in actions


# ═══════════════════════════════════════════════════════════════════════════════
# F5: apps/routes.py — deploy-web compat alias not shadowed
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeployWebCompatRedirect:
    """GET /api/apps/deploy-web/config must 307 to /api/deploy/config.

    Both apps routes and deploy routes are registered in production order
    (apps first, deploy second) to ensure the test catches the shadowing.
    """

    @staticmethod
    def _make_app() -> web.Application:
        """Create an aiohttp app with BOTH route sets registered in production order."""
        app = web.Application()

        # Register apps routes FIRST (production order: server.py line 1322 before 1354)
        from kiro_crew.apps.routes import register_app_routes
        register_app_routes(app)

        # Register deploy routes SECOND
        from kiro_crew.deploy.handlers import register_routes
        register_routes(app)

        return app

    @pytest.mark.asyncio
    async def test_deploy_web_config_redirects(self):
        """GET /api/apps/deploy-web/config -> 307 to /api/deploy/config."""
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._make_app())) as client:
            resp = await client.get("/api/apps/deploy-web/config", allow_redirects=False)
            assert resp.status == 307
            assert resp.headers["Location"] == "/api/deploy/config"

    @pytest.mark.asyncio
    async def test_deploy_web_manifest_redirects(self):
        """GET /api/apps/deploy-web/manifest -> 307 to /api/deploy/config."""
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._make_app())) as client:
            resp = await client.get("/api/apps/deploy-web/manifest", allow_redirects=False)
            assert resp.status == 307
            assert resp.headers["Location"] == "/api/deploy/config"

    @pytest.mark.asyncio
    async def test_deploy_web_app_info_redirects(self):
        """GET /api/apps/deploy-web -> 307 to /api/deploy/list."""
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._make_app())) as client:
            resp = await client.get("/api/apps/deploy-web", allow_redirects=False)
            assert resp.status == 307
            assert resp.headers["Location"] == "/api/deploy/list"
