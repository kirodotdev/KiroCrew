"""Tests for /api/theme/boot and /api/config/theme endpoints."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import core as core_mod


def _make_cfg(
    theme_mode: str = "",
    theme_color: str = "",
    onboarded: bool = False,
    import_onboarded: bool = False,
):
    """Build a mock KiroCrewConfig with dashboard theme fields."""
    cfg = MagicMock()
    cfg.dashboard.theme_mode = theme_mode
    cfg.dashboard.theme_color = theme_color
    cfg.dashboard.onboarded = onboarded
    cfg.dashboard.import_onboarded = import_onboarded
    return cfg


@pytest.mark.asyncio
async def test_theme_boot_returns_defaults() -> None:
    """GET /api/theme/boot returns empty defaults when unconfigured."""
    cfg = _make_cfg()
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        resp = await core_mod.api_theme_boot(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {
        "mode": "",
        "color": "",
        "onboarded": False,
        "import_onboarded": False,
    }


@pytest.mark.asyncio
async def test_theme_boot_returns_configured_values() -> None:
    """GET /api/theme/boot returns workspace config values."""
    cfg = _make_cfg(
        theme_mode="dark",
        theme_color="kiro",
        onboarded=True,
        import_onboarded=True,
    )
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        resp = await core_mod.api_theme_boot(req)
    body = json.loads(resp.body)
    assert body == {
        "mode": "dark",
        "color": "kiro",
        "onboarded": True,
        "import_onboarded": True,
    }


@pytest.mark.asyncio
async def test_theme_config_get() -> None:
    """GET /api/config/theme returns current theme settings."""
    cfg = _make_cfg(
        theme_mode="light",
        theme_color="emerald",
        onboarded=True,
        import_onboarded=True,
    )
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        req.method = "GET"
        resp = await core_mod.api_theme_config(req)
    body = json.loads(resp.body)
    assert body == {
        "mode": "light",
        "color": "emerald",
        "onboarded": True,
        "import_onboarded": True,
    }


@pytest.mark.asyncio
async def test_theme_config_put_updates_and_saves() -> None:
    """PUT /api/config/theme updates config and calls save."""
    cfg = _make_cfg(theme_mode="", theme_color="", onboarded=False, import_onboarded=False)
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        req.method = "PUT"
        req.json = AsyncMock(
            return_value={
                "mode": "dark",
                "color": "monokai",
                "onboarded": True,
                "import_onboarded": True,
            }
        )
        resp = await core_mod.api_theme_config(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {
        "mode": "dark",
        "color": "monokai",
        "onboarded": True,
        "import_onboarded": True,
    }
    cfg.save.assert_called_once()


@pytest.mark.asyncio
async def test_theme_config_put_validates_mode() -> None:
    """PUT /api/config/theme rejects invalid mode."""
    cfg = _make_cfg()
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        req.method = "PUT"
        req.json = AsyncMock(return_value={"mode": "invalid"})
        with pytest.raises(web.HTTPBadRequest):
            await core_mod.api_theme_config(req)


@pytest.mark.asyncio
async def test_theme_config_put_validates_import_onboarded_boolean() -> None:
    """PUT /api/config/theme rejects truthy non-booleans for the import gate."""
    cfg = _make_cfg()
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        req.method = "PUT"
        req.json = AsyncMock(return_value={"import_onboarded": "false"})
        with pytest.raises(web.HTTPBadRequest):
            await core_mod.api_theme_config(req)


@pytest.mark.asyncio
async def test_theme_config_put_no_change_no_save() -> None:
    """PUT /api/config/theme with same values does not call save."""
    cfg = _make_cfg(
        theme_mode="dark",
        theme_color="kiro",
        onboarded=True,
        import_onboarded=True,
    )
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        req.method = "PUT"
        req.json = AsyncMock(
            return_value={
                "mode": "dark",
                "color": "kiro",
                "onboarded": True,
                "import_onboarded": True,
            }
        )
        resp = await core_mod.api_theme_config(req)
    assert resp.status == 200
    cfg.save.assert_not_called()


@pytest.mark.asyncio
async def test_theme_config_put_rejects_non_object_body() -> None:
    """PUT /api/config/theme rejects arrays instead of raising during key access."""
    req = MagicMock(spec=web.Request)
    req.method = "PUT"
    req.json = AsyncMock(return_value=["import_onboarded"])

    with pytest.raises(web.HTTPBadRequest):
        await core_mod.api_theme_config(req)


@pytest.mark.asyncio
async def test_theme_config_put_serializes_full_load_modify_save_transaction() -> None:
    """Concurrent writes preserve fields committed by the preceding writer."""
    persisted = {
        "mode": "",
        "color": "",
        "onboarded": False,
        "import_onboarded": False,
    }
    json_waiters = 0
    both_parsed = asyncio.Event()

    class Config:
        def __init__(self) -> None:
            self.dashboard = type("Dashboard", (), {})()
            self.dashboard.theme_mode = persisted["mode"]
            self.dashboard.theme_color = persisted["color"]
            self.dashboard.onboarded = persisted["onboarded"]
            self.dashboard.import_onboarded = persisted["import_onboarded"]

        def save(self) -> None:
            persisted.update(
                {
                    "mode": self.dashboard.theme_mode,
                    "color": self.dashboard.theme_color,
                    "onboarded": self.dashboard.onboarded,
                    "import_onboarded": self.dashboard.import_onboarded,
                }
            )

    async def body(value: dict[str, object]) -> dict[str, object]:
        nonlocal json_waiters
        json_waiters += 1
        if json_waiters == 2:
            both_parsed.set()
        await both_parsed.wait()
        return value

    first = MagicMock(spec=web.Request)
    first.method = "PUT"
    first.json = lambda: body({"mode": "dark"})
    second = MagicMock(spec=web.Request)
    second.method = "PUT"
    second.json = lambda: body({"import_onboarded": True})

    with patch.object(core_mod.KiroCrewConfig, "load", side_effect=Config):
        await asyncio.gather(
            core_mod.api_theme_config(first),
            core_mod.api_theme_config(second),
        )

    assert persisted["mode"] == "dark"
    assert persisted["import_onboarded"] is True
