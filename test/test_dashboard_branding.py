"""Tests for dashboard branding: custom avatar and bot name."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from _hot_reload_helpers import write_config as _write
from config_live_helpers import (  # noqa: F401 -- fixtures are used by name
    cfg_file_fixture,
)

from kiro_crew.config.loader import DashboardConfig, KiroCrewConfig
from kiro_crew.testing.wait import async_wait_until


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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("present", [True, False])
    async def test_branding_reports_whether_the_host_has_a_desktop_launcher(self, present):
        # The dashboard offers "Show in file manager" and "Open with default app"
        # only when this is true: on a host that cannot open a file manager (a
        # headless Linux box, which an `ssh -L` browser still reaches as
        # direct-local) /api/reveal can only answer a clipboard copy.
        from kiro_crew import platform_compat
        from kiro_crew.dashboard.handlers import api_branding

        req = MagicMock()
        with patch(
            "kiro_crew.dashboard.handlers.KiroCrewConfig.load", return_value=KiroCrewConfig()
        ), patch.object(platform_compat, "desktop_launcher_available", return_value=present):
            resp = await api_branding(req)
        assert json.loads(resp.body)["desktop_launcher"] is present

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("display", "manager", "expected"),
        [
            (None, None, False),
            (None, ["DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus"], False),
            (":0", None, True),
            (None, ["WAYLAND_DISPLAY=wayland-0"], True),
        ],
        ids=["headless", "headless-with-a-user-bus", "desktop-session", "service-under-a-desktop-login"],
    )
    async def test_branding_says_a_linux_host_needs_a_desktop_session(
        self, monkeypatch, tmp_path, display, manager, expected
    ):
        # The whole chain the dashboard reads, on a Linux host that HAS xdg-open:
        # without a display server named in the gateway's environment or by the
        # user's systemd manager there is nothing to open a file manager into, so
        # the rows must not be offered. The installed service names none of its
        # own, so a desktop login reaches it through the manager. `manager` is
        # that manager's environment (None: no manager to ask).
        from kiro_crew import platform_compat, platform_compat_trust
        from kiro_crew.dashboard.handlers import api_branding

        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        if display is None:
            monkeypatch.delenv("DISPLAY", raising=False)
        else:
            monkeypatch.setenv("DISPLAY", display)
        (tmp_path / "wayland-0").touch()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.setattr(
            platform_compat_trust, "_user_manager_environment", lambda: manager, raising=False
        )
        monkeypatch.setattr(platform_compat_trust, "_desktop_session_cache", None, raising=False)
        req = MagicMock()
        with patch(
            "kiro_crew.dashboard.handlers.KiroCrewConfig.load", return_value=KiroCrewConfig()
        ), patch("sys.platform", "linux"), patch.object(
            platform_compat, "IS_WINDOWS", False
        ), patch(
            "os.path.isfile", return_value=True
        ):
            resp = await api_branding(req)
        assert json.loads(resp.body)["desktop_launcher"] is expected


class TestBrandingReadsTheLiveConfig:
    """api_branding() serves the watcher's adopted config instead of rebuilding one.

    ``build_config`` is the step that constructs every section dataclass, so the
    tests count its calls rather than timing the route.
    """

    @staticmethod
    def _count_builds():
        from kiro_crew.config import loader_pipeline

        return patch.object(
            loader_pipeline, "build_config", side_effect=loader_pipeline.build_config
        )

    @pytest.mark.asyncio
    async def test_unchanged_requests_build_the_config_once(self, cfg_file: Path) -> None:
        from kiro_crew.config import live
        from kiro_crew.dashboard.handlers import api_branding

        _write(cfg_file, {"dashboard": {"bot_name": "Jarvis"}})
        with self._count_builds() as build:
            await live.watch().refresh_now()
            for _ in range(100):
                resp = await api_branding(MagicMock())
                assert json.loads(resp.body)["bot_name"] == "Jarvis"
        assert build.call_count == 1

    @pytest.mark.asyncio
    async def test_a_config_write_reaches_the_next_request(self, cfg_file: Path) -> None:
        from kiro_crew.config import live
        from kiro_crew.config.loader import update_config_locked
        from kiro_crew.dashboard.handlers import api_branding

        watcher = live.watch()
        await watcher.start()
        try:
            resp = await api_branding(MagicMock())
            assert json.loads(resp.body)["bot_name"] == "Kiro Crew"

            def _rename(doc: dict) -> dict:
                doc.setdefault("dashboard", {})["bot_name"] = "Jarvis"
                return doc

            # The loader's writer wakes the started watcher, which reloads off the
            # loop; the route reads whatever it adopted.
            await asyncio.to_thread(update_config_locked, cfg_file, mutate=_rename)
            await async_wait_until(
                lambda: (snap := live.snapshot()) is not None
                and snap.dashboard.bot_name == "Jarvis"
            )
            resp = await api_branding(MagicMock())
            assert json.loads(resp.body)["bot_name"] == "Jarvis"
        finally:
            await watcher.stop()

    @pytest.mark.asyncio
    async def test_without_a_watcher_each_request_loads_from_disk(self, cfg_file: Path) -> None:
        from kiro_crew.dashboard.handlers import api_branding

        _write(cfg_file, {"dashboard": {"bot_name": "Jarvis"}})
        resp = await api_branding(MagicMock())
        assert json.loads(resp.body)["bot_name"] == "Jarvis"
        _write(cfg_file, {"dashboard": {"bot_name": "Friday"}})
        resp = await api_branding(MagicMock())
        assert json.loads(resp.body)["bot_name"] == "Friday"


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
