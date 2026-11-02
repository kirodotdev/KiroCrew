"""The ``browser`` config section (``config/sections_browser.py``) and its write path.

Settings -> Browser is the one writer of every key here, through the generic
``PATCH /api/config/kirocrew``; ``browser_harness`` reads them with these defaults.
Pinned:

* the shipped defaults, which are what the RFC promises a fresh install (auto
  discovery, live view on at 60 fps with passwords masked);
* the loader NORMALIZES rather than rejects: a hand-edited ``config.json`` degrades
  to a value that cannot make things worse, with one warning per process;
* the section round-trips through ``load()``/``to_dict()`` and is a known section
  (so an operator's values are never captured as "unknown" and dropped);
* the PATCH route accepts exactly the Settings-contract keys, refuses a value
  outside each closed enum, and holds ``dashboard.browser_local_origins`` to the
  ``host:port`` loopback shape the Preview tab's allowlist means.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config import loader as L
from kiro_crew.config import sections_browser as SB
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.resolution import _KNOWN_CONFIG_SECTIONS
from kiro_crew.config.schema import SCHEMA_REGISTRY
from kiro_crew.config.sections import (
    BROWSER_CHOICES,
    LIVE_MAX_FPS_CHOICES,
    LIVE_QUALITIES,
    BrowserConfig,
    BrowserLiveConfig,
    coerce_browser_local_origins,
)
from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

SETTINGS_CONTRACT_KEYS = (
    "browser.choice",
    "browser.live.enabled",
    "browser.live.quality",
    "browser.live.max_fps",
    "browser.live.mask_passwords",
    "browser.credentials.enabled",
    "browser.credentials.domains",
    "dashboard.browser_local_origins",
)


@pytest.fixture(autouse=True)
def _reset_warn_once():
    SB._WARNED_BROWSER_VALUES.clear()
    yield
    SB._WARNED_BROWSER_VALUES.clear()


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    cfgp = tmp_path / "config.json"
    monkeypatch.setattr(L, "config_path", lambda: cfgp)
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")
    return cfgp


class TestDefaults:
    def test_a_fresh_config_matches_the_rfc(self):
        cfg = KiroCrewConfig().browser
        assert cfg.choice == "auto"
        assert cfg.live == BrowserLiveConfig(
            enabled=True, quality="auto", max_fps=60, mask_passwords=True
        )

    def test_the_enums_are_the_contract_values(self):
        assert BROWSER_CHOICES == ("auto", "user_chrome", "kiro_chromium", "headless")
        assert LIVE_QUALITIES == ("auto", "high", "medium", "low")
        assert LIVE_MAX_FPS_CHOICES == (15, 30, 60)

    def test_every_field_carries_ui_metadata(self):
        for cls in (BrowserConfig, BrowserLiveConfig):
            for f in cls.__dataclass_fields__.values():
                assert f.metadata.get("label") and f.metadata.get(
                    "help"
                ), f"{cls.__name__}.{f.name}"

    def test_every_contract_key_is_in_the_schema_registry(self):
        paths = {e.path for e in SCHEMA_REGISTRY}
        for key in SETTINGS_CONTRACT_KEYS:
            assert key in paths, key

    def test_browser_is_a_known_section(self):
        assert "browser" in _KNOWN_CONFIG_SECTIONS
        assert KiroCrewConfig().to_dict()["browser"] == asdict(BrowserConfig())


class TestNormalization:
    @pytest.mark.parametrize("raw", [None, [], "auto", 7])
    def test_a_non_dict_section_builds_the_defaults(self, raw):
        assert BrowserConfig.from_raw(raw) == BrowserConfig()

    def test_valid_values_are_kept(self):
        cfg = BrowserConfig.from_raw(
            {
                "choice": "headless",
                "live": {
                    "enabled": False,
                    "quality": "low",
                    "max_fps": 15,
                    "mask_passwords": False,
                },
            }
        )
        assert cfg.choice == "headless"
        assert cfg.live == BrowserLiveConfig(
            enabled=False, quality="low", max_fps=15, mask_passwords=False
        )

    def test_unknown_values_degrade_with_one_warning_each(self, caplog):
        caplog.set_level(logging.WARNING, logger="kiro_crew.config.loader")
        raw = {"choice": "firefox", "live": {"quality": "ultra", "max_fps": 144}}
        cfg = BrowserConfig.from_raw(raw)
        again = BrowserConfig.from_raw(raw)
        assert cfg == again
        assert cfg.choice == "auto"
        assert cfg.live.quality == "auto"
        assert cfg.live.max_fps == 60
        messages = [r.getMessage() for r in caplog.records]
        assert sum("browser.choice" in m for m in messages) == 1
        assert sum("browser.live.quality" in m for m in messages) == 1
        assert sum("browser.live.max_fps" in m for m in messages) == 1

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("30", 30), (" 15 ", 15), (True, 60), (60.0, 60), ("fast", 60), (None, 60)],
    )
    def test_max_fps_spellings(self, raw, expected, caplog):
        caplog.set_level(logging.WARNING, logger="kiro_crew.config.loader")
        assert BrowserLiveConfig.from_raw({"max_fps": raw}).max_fps == expected
        warned = any("browser.live.max_fps" in r.getMessage() for r in caplog.records)
        # A digit string is an accepted spelling and an absent key says nothing;
        # anything else is worth a line.
        assert warned is (raw not in ("30", " 15 ", None))

    def test_a_non_bool_flag_reads_as_its_default(self):
        cfg = BrowserConfig.from_raw({"live": {"enabled": "no", "mask_passwords": 0}})
        assert cfg.live.enabled is True
        assert cfg.live.mask_passwords is True

    def test_null_values_are_the_absent_key(self, caplog):
        caplog.set_level(logging.WARNING, logger="kiro_crew.config.loader")
        cfg = BrowserConfig.from_raw({"choice": None, "live": {"quality": None, "max_fps": None}})
        assert cfg == BrowserConfig()
        assert not caplog.records


class TestRoundTrip:
    def test_load_reads_the_section_and_save_writes_it_back(self, cfg_home):
        cfg_home.write_text(
            json.dumps(
                {
                    "browser": {
                        "choice": "kiro_chromium",
                        "live": {"max_fps": 30, "quality": "medium"},
                    }
                }
            ),
            encoding="utf-8",
        )
        cfg = KiroCrewConfig.load()
        assert cfg.browser.choice == "kiro_chromium"
        assert cfg.browser.live.max_fps == 30
        assert cfg.browser.live.quality == "medium"
        assert cfg.browser.live.enabled is True
        cfg.save()
        after = json.loads(cfg_home.read_text(encoding="utf-8"))
        assert after["browser"] == {
            "choice": "kiro_chromium",
            "live": {"enabled": True, "quality": "medium", "max_fps": 30, "mask_passwords": True},
            # Unset: ``None`` and ``[]`` both mean "follow the edition's default".
            "credentials": {"enabled": None, "domains": []},
            "recording": {"enabled": True, "max_session_mb": 200, "max_total_mb": 1024},
        }

    def test_the_retired_first_run_flag_is_dropped_on_save(self, cfg_home):
        # Pre-release builds wrote browser.first_run_dismissed for a step list
        # that is gone; a config carrying it loads and loses it on the next save.
        cfg_home.write_text(
            json.dumps({"browser": {"choice": "auto", "first_run_dismissed": True}}),
            encoding="utf-8",
        )
        cfg = KiroCrewConfig.load()
        assert not hasattr(cfg.browser, "first_run_dismissed")
        cfg.save()
        after = json.loads(cfg_home.read_text(encoding="utf-8"))
        assert "first_run_dismissed" not in after["browser"]

    def test_an_absent_section_loads_the_defaults(self, cfg_home):
        cfg_home.write_text(json.dumps({"dashboard": {}}), encoding="utf-8")
        assert KiroCrewConfig.load().browser == BrowserConfig()


class TestLocalOrigins:
    def test_the_coercer_caps_the_list_at_the_write_bound(self):
        raw = [f"localhost:{1000 + i}" for i in range(SB_MAX() + 5)]
        assert len(coerce_browser_local_origins(raw)) == SB_MAX()

    def test_the_patch_spec_and_the_coercer_agree_on_shape(self):
        import re

        spec = _EDITABLE_CONFIG["dashboard.browser_local_origins"]
        assert spec["type"] == "str_list"
        assert spec["max_items"] == SB_MAX()
        pattern = spec["item_pattern"]
        for ok in ("localhost:5173", "127.0.0.1:3000", "[::1]:8080"):
            assert re.fullmatch(pattern, ok), ok
            assert coerce_browser_local_origins([ok]) == [ok]
        for bad in (
            "http://localhost:5173",
            "localhost",
            "example.com:80",
            "0.0.0.0:80",
            "LOCALHOST:1",
        ):
            assert not re.fullmatch(pattern, bad), bad


def SB_MAX() -> int:
    from kiro_crew.config.sections import BROWSER_LOCAL_ORIGINS_MAX

    return BROWSER_LOCAL_ORIGINS_MAX


def _app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return as_owner(app)


class TestPatchRoute:
    def test_exactly_the_contract_keys_are_editable(self):
        for key in SETTINGS_CONTRACT_KEYS:
            assert key in _EDITABLE_CONFIG, key
        assert _EDITABLE_CONFIG["browser.choice"]["values"] == list(BROWSER_CHOICES)
        assert _EDITABLE_CONFIG["browser.live.quality"]["values"] == list(LIVE_QUALITIES)
        assert _EDITABLE_CONFIG["browser.live.max_fps"]["values"] == list(LIVE_MAX_FPS_CHOICES)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path", "value"),
        [
            ("browser.choice", "user_chrome"),
            ("browser.live.enabled", False),
            ("browser.live.quality", "low"),
            ("browser.live.max_fps", 30),
            ("browser.live.mask_passwords", False),
            ("browser.credentials.enabled", True),
            ("browser.credentials.domains", ["example.com", "intranet.example.org"]),
            ("dashboard.browser_local_origins", ["localhost:5173", "127.0.0.1:3000"]),
        ],
    )
    async def test_a_contract_value_is_written(self, cfg_home, path, value) -> None:
        cfg_home.write_text(json.dumps({"dashboard": {}}), encoding="utf-8")
        async with TestClient(TestServer(_app())) as c:
            resp = await c.patch("/api/config/kirocrew", json={"path": path, "value": value})
            assert resp.status == 200, await resp.text()
        written = json.loads(cfg_home.read_text(encoding="utf-8"))
        node = written
        for part in path.split("."):
            node = node[part]
        assert node == value

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path", "value"),
        [
            ("browser.choice", "firefox"),
            ("browser.live.quality", "ultra"),
            ("browser.live.max_fps", 144),
            ("browser.live.max_fps", "60"),
            ("browser.live.enabled", "yes"),
            ("dashboard.browser_local_origins", ["example.com:80"]),
            ("dashboard.browser_local_origins", ["http://localhost:5173"]),
            ("dashboard.browser_local_origins", "localhost:5173"),
            ("browser.credentials.enabled", "on"),
            ("browser.credentials.domains", ["com"]),
            ("browser.credentials.domains", ["*.example.com"]),
            ("browser.credentials.domains", ["example.com:443"]),
            ("browser.credentials.domains", ["Example.com"]),
            ("browser.credentials.domains", "example.com"),
        ],
    )
    async def test_a_value_outside_the_contract_is_refused(self, cfg_home, path, value) -> None:
        cfg_home.write_text(json.dumps({"dashboard": {}}), encoding="utf-8")
        async with TestClient(TestServer(_app())) as c:
            resp = await c.patch("/api/config/kirocrew", json={"path": path, "value": value})
            assert resp.status == 400, await resp.text()
        written = json.loads(cfg_home.read_text(encoding="utf-8"))
        assert "browser" not in written
        assert "browser_local_origins" not in written["dashboard"]


class TestCredentials:
    def test_unset_is_none_and_a_list_is_kept_lowercase_and_unique(self) -> None:
        from kiro_crew.config.sections_browser import BrowserConfig

        assert BrowserConfig.from_raw({}).credentials.enabled is None
        cfg = BrowserConfig.from_raw(
            {"credentials": {"enabled": True, "domains": [".Example.com", "example.com", 7]}}
        )
        assert cfg.credentials.enabled is True
        assert cfg.credentials.domains == ["example.com"]
        junk = BrowserConfig.from_raw({"credentials": {"enabled": "yes", "domains": "x"}})
        assert junk.credentials.enabled is None and junk.credentials.domains == []
