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
    language: str = "",
):
    """Build a mock KiroCrewConfig with dashboard display fields."""
    cfg = MagicMock()
    cfg.dashboard.theme_mode = theme_mode
    cfg.dashboard.theme_color = theme_color
    cfg.dashboard.onboarded = onboarded
    cfg.dashboard.import_onboarded = import_onboarded
    cfg.dashboard.language = language
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
        "language": "",
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
        "language": "",
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
        "language": "",
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
        "language": "",
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
        "language": "",
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
            self.dashboard.language = persisted["language"]
            self.dashboard.onboarded = persisted["onboarded"]
            self.dashboard.import_onboarded = persisted["import_onboarded"]

        def save(self) -> None:
            persisted.update(
                {
                    "mode": self.dashboard.theme_mode,
                    "color": self.dashboard.theme_color,
                    "language": self.dashboard.language,
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


# ── UI language (dashboard.language) ──────────────────────────────────────────
#
# The language field rides on the EXISTING theme endpoints rather than a new
# pair, so these tests cover the field's own validation and the round trip. The
# empty string is a first-class value ("follow the browser"), not a missing
# value, so clearing a choice must be writable.


@pytest.mark.asyncio
async def test_theme_boot_exposes_language() -> None:
    """GET /api/theme/boot surfaces the configured UI language.

    Boot is unauthenticated, so the SPA can pick the right language before the
    token flow completes -- this is what prevents an English flash on load.
    """
    cfg = _make_cfg(language="zh-CN")
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        resp = await core_mod.api_theme_boot(req)
    assert json.loads(resp.body)["language"] == "zh-CN"


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["en", "zh-CN", "pt-BR", "zh-Hans-CN", "fr"])
async def test_theme_config_put_accepts_valid_language_tags(tag: str) -> None:
    """A well-formed BCP-47 tag is accepted, including ones with no catalog.

    Shape is validated, not membership: keeping the shipped-language list a pure
    frontend concern means adding a language never needs a backend change.
    """
    cfg = _make_cfg()
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        req.method = "PUT"
        req.json = AsyncMock(return_value={"language": tag})
        resp = await core_mod.api_theme_config(req)
    assert resp.status == 200
    assert json.loads(resp.body)["language"] == tag
    cfg.save.assert_called_once()


@pytest.mark.asyncio
async def test_theme_config_put_clears_language_to_auto() -> None:
    """Writing '' clears the stored choice back to browser auto-detect."""
    cfg = _make_cfg(language="zh-CN")
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        req.method = "PUT"
        req.json = AsyncMock(return_value={"language": ""})
        resp = await core_mod.api_theme_config(req)
    assert resp.status == 200
    assert json.loads(resp.body)["language"] == ""
    assert cfg.dashboard.language == ""
    cfg.save.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "e",  # too short
        "english-language-name",  # subtag over 8 chars
        "en_US",  # underscore is not BCP-47
        "en-US-x-toolong-extra",  # too many subtags
        "../../etc/passwd",  # path traversal shape
        "<script>",  # markup
        "zh CN",  # whitespace
    ],
)
async def test_theme_config_put_rejects_malformed_language(bad: str) -> None:
    """A malformed tag is a 400 and never reaches the config file."""
    cfg = _make_cfg()
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        req.method = "PUT"
        req.json = AsyncMock(return_value={"language": bad})
        with pytest.raises(web.HTTPBadRequest):
            await core_mod.api_theme_config(req)
    cfg.save.assert_not_called()


@pytest.mark.asyncio
async def test_theme_config_put_rejects_non_string_language() -> None:
    """A non-string language is a 400, not a coerced value."""
    cfg = _make_cfg()
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        req.method = "PUT"
        req.json = AsyncMock(return_value={"language": ["zh-CN"]})
        with pytest.raises(web.HTTPBadRequest):
            await core_mod.api_theme_config(req)
    cfg.save.assert_not_called()


@pytest.mark.asyncio
async def test_theme_config_put_omitting_language_leaves_it_untouched() -> None:
    """A PUT that doesn't mention language must not reset it.

    The frontend patches single fields, so an unrelated theme write must never
    clobber the user's language choice.
    """
    cfg = _make_cfg(language="zh-CN")
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        req.method = "PUT"
        req.json = AsyncMock(return_value={"color": "monokai"})
        resp = await core_mod.api_theme_config(req)
    assert json.loads(resp.body)["language"] == "zh-CN"
    assert cfg.dashboard.language == "zh-CN"
