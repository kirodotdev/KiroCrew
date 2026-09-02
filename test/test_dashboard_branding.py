"""Tests for dashboard branding: custom avatar and bot name."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.config.loader import DashboardConfig, KiroCrewConfig


class TestBrandingConfig:
    """Config loader correctly reads dashboard branding fields."""

    def test_defaults(self):
        cfg = KiroCrewConfig()
        assert cfg.dashboard.bot_name == ""
        assert cfg.dashboard.avatar == ""

    def test_load_from_dict(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: tmp_path / "config.json")
        data = {"dashboard": {"url": "", "bot_name": "Jarvis", "avatar": "/tmp/a.png"}}
        (tmp_path / "config.json").write_text(json.dumps(data))
        cfg = KiroCrewConfig.load()
        assert cfg.dashboard.bot_name == "Jarvis"
        assert cfg.dashboard.avatar == "/tmp/a.png"

    def test_to_dict_roundtrip(self):
        cfg = KiroCrewConfig(dashboard=DashboardConfig(bot_name="Bot", avatar="~/pic.png"))
        d = cfg.to_dict()
        assert d["dashboard"]["bot_name"] == "Bot"
        assert d["dashboard"]["avatar"] == "~/pic.png"


class TestLogoHandler:
    """logo() serves custom avatar when configured."""

    @pytest.mark.asyncio
    async def test_logo_custom_avatar(self, tmp_path):
        from kiro_crew.dashboard.handlers import logo

        avatar = tmp_path / "custom.png"
        avatar.write_bytes(b"\x89PNG")
        cfg = KiroCrewConfig(dashboard=DashboardConfig(avatar=str(avatar)))
        req = MagicMock()
        with patch("kiro_crew.dashboard.handlers.KiroCrewConfig.load", return_value=cfg):
            resp = await logo(req)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_logo_missing_custom_and_default_returns_404(self, tmp_path):
        from kiro_crew.dashboard.handlers import logo

        cfg = KiroCrewConfig(dashboard=DashboardConfig(avatar="/nonexistent/path.png"))
        req = MagicMock()
        with patch("kiro_crew.dashboard.handlers.KiroCrewConfig.load", return_value=cfg), patch(
            "kiro_crew.dashboard.handlers._STATIC_DIR", tmp_path
        ):
            resp = await logo(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_logo_missing_custom_falls_back_to_default(self, tmp_path):
        from kiro_crew.dashboard.handlers import logo

        default_logo = tmp_path / "kirocrew-logo.png"
        default_logo.write_bytes(b"\x89PNG")
        cfg = KiroCrewConfig(dashboard=DashboardConfig(avatar="/nonexistent/path.png"))
        req = MagicMock()
        with patch("kiro_crew.dashboard.handlers.KiroCrewConfig.load", return_value=cfg), patch(
            "kiro_crew.dashboard.handlers._STATIC_DIR", tmp_path
        ):
            resp = await logo(req)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_logo_sensitive_path_rejected(self, tmp_path):
        from kiro_crew.dashboard.handlers import logo

        sensitive = tmp_path / "secret_key"
        sensitive.write_bytes(b"\x89PNG")
        cfg = KiroCrewConfig(dashboard=DashboardConfig(avatar=str(sensitive)))
        req = MagicMock()
        with patch("kiro_crew.dashboard.handlers.KiroCrewConfig.load", return_value=cfg), patch(
            "kiro_crew.hooks.validate_file_path", return_value=None
        ), patch("kiro_crew.dashboard.handlers._STATIC_DIR", tmp_path / "empty"):
            resp = await logo(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_logo_nightly_version_serves_nightly_variant(self, tmp_path, monkeypatch):
        """Nightly-stamped builds serve the night-sky logo so in-app branding
        (sidebar, favicon, notification avatar -- all /logo.png) matches the
        nightly app's Dock/tray identity."""
        from kiro_crew.dashboard.handlers import logo

        (tmp_path / "kirocrew-logo.png").write_bytes(b"\x89PNG-default")
        (tmp_path / "kirocrew-logo-nightly.png").write_bytes(b"\x89PNG-nightly")
        monkeypatch.setattr("kiro_crew.__version__", "0.1.0-nightly.20260722223306")
        cfg = KiroCrewConfig(dashboard=DashboardConfig(avatar=""))
        req = MagicMock()
        with patch("kiro_crew.dashboard.handlers.KiroCrewConfig.load", return_value=cfg), patch(
            "kiro_crew.dashboard.handlers._STATIC_DIR", tmp_path
        ):
            resp = await logo(req)
        assert resp.status == 200
        assert resp._path.name == "kirocrew-logo-nightly.png"

    @pytest.mark.asyncio
    async def test_logo_nightly_missing_asset_falls_back_to_default(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import logo

        (tmp_path / "kirocrew-logo.png").write_bytes(b"\x89PNG-default")
        monkeypatch.setattr("kiro_crew.__version__", "0.1.0-nightly.20260722223306")
        cfg = KiroCrewConfig(dashboard=DashboardConfig(avatar=""))
        req = MagicMock()
        with patch("kiro_crew.dashboard.handlers.KiroCrewConfig.load", return_value=cfg), patch(
            "kiro_crew.dashboard.handlers._STATIC_DIR", tmp_path
        ):
            resp = await logo(req)
        assert resp.status == 200
        assert resp._path.name == "kirocrew-logo.png"

    @pytest.mark.asyncio
    async def test_logo_stable_version_ignores_nightly_variant(self, tmp_path, monkeypatch):
        """Insider/stable stamps keep the production logo even when the
        nightly asset is present in the static dir."""
        from kiro_crew.dashboard.handlers import logo

        (tmp_path / "kirocrew-logo.png").write_bytes(b"\x89PNG-default")
        (tmp_path / "kirocrew-logo-nightly.png").write_bytes(b"\x89PNG-nightly")
        monkeypatch.setattr("kiro_crew.__version__", "0.1.0")
        cfg = KiroCrewConfig(dashboard=DashboardConfig(avatar=""))
        req = MagicMock()
        with patch("kiro_crew.dashboard.handlers.KiroCrewConfig.load", return_value=cfg), patch(
            "kiro_crew.dashboard.handlers._STATIC_DIR", tmp_path
        ):
            resp = await logo(req)
        assert resp.status == 200
        assert resp._path.name == "kirocrew-logo.png"

    @pytest.mark.asyncio
    async def test_logo_no_config_serves_default(self, tmp_path):
        from kiro_crew.dashboard.handlers import logo

        default_logo = tmp_path / "kirocrew-logo.png"
        default_logo.write_bytes(b"\x89PNG")
        cfg = KiroCrewConfig()
        req = MagicMock()
        with patch("kiro_crew.dashboard.handlers.KiroCrewConfig.load", return_value=cfg), patch(
            "kiro_crew.dashboard.handlers._STATIC_DIR", tmp_path
        ):
            resp = await logo(req)
        assert resp.status == 200


class TestBrandingEndpoint:
    """api_branding() returns correct JSON."""

    @pytest.mark.asyncio
    async def test_branding_defaults(self):
        from kiro_crew.dashboard.handlers import api_branding

        cfg = KiroCrewConfig()
        req = MagicMock()
        with patch("kiro_crew.dashboard.handlers.KiroCrewConfig.load", return_value=cfg):
            resp = await api_branding(req)
        body = json.loads(resp.body)
        assert body["bot_name"] == "Kiro Crew"
        assert body["avatar"] == "/logo.png"

    @pytest.mark.asyncio
    async def test_branding_custom_name(self):
        from kiro_crew.dashboard.handlers import api_branding

        cfg = KiroCrewConfig(dashboard=DashboardConfig(bot_name="Jarvis"))
        req = MagicMock()
        with patch("kiro_crew.dashboard.handlers.KiroCrewConfig.load", return_value=cfg):
            resp = await api_branding(req)
        body = json.loads(resp.body)
        assert body["bot_name"] == "Jarvis"


class TestLogoAssetInvariant:
    """The dashboard-served logo and the PWA 512 icon are the same image.

    ``src/kiro_crew/static/kirocrew-logo.png`` (served at ``/logo.png`` for the
    sidebar, favicon, and notification avatar) is kept byte-identical to
    ``website/public/icon-512.png`` (the PWA/manifest 512 icon) by hand — they
    are two copies of one brand mark. Nothing else asserts that equality, so an
    icon update that touches one copy and forgets the other would silently ship
    a mismatched favicon vs. installed-app icon. This guard fails that drift in
    CI. If the two are intentionally decoupled in future, delete this test in
    the same change.
    """

    def test_logo_matches_pwa_512_icon(self):
        import hashlib
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        served = repo_root / "src" / "kiro_crew" / "static" / "kirocrew-logo.png"
        pwa = repo_root / "website" / "public" / "icon-512.png"
        # Skip rather than fail on a python-only checkout that lacks website/.
        if not pwa.is_file():
            import pytest

            pytest.skip("website/public not present (python-only checkout)")
        assert served.is_file(), f"missing served logo at {served}"
        served_sha = hashlib.sha256(served.read_bytes()).hexdigest()
        pwa_sha = hashlib.sha256(pwa.read_bytes()).hexdigest()
        assert served_sha == pwa_sha, (
            "kirocrew-logo.png and icon-512.png diverged — update both copies of "
            "the brand mark together (they are served as /logo.png and the PWA "
            "512 icon respectively)."
        )
