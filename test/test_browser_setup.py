"""Tests for kiro_crew.browser.setup — Playwright MCP setup (OSS stub).

The upstream build installed Playwright MCP via an Amazon-internal package
manager (AIM) and wired an Amazon-auth cookie/storage-state flow. In the
open-source build those steps are neutralized: ``is_playwright_installed``
always reports False (no internal package manager) and
``ensure_playwright_installed`` is a no-op. The generic Netscape cookie
parsing, Playwright config generation and storage-state refresh still work.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.browser.setup as setup_mod
from kiro_crew.browser.setup import (
    ensure_playwright_installed,
    generate_playwright_config,
    get_playwright_mcp_args,
    inject_cookies_via_playwright,
    is_headed,
    is_playwright_installed,
    refresh_storage_state,
)

# ── Sample cookie data ────────────────────────────────────────────────────────

SAMPLE_COOKIES = """\
# Netscape HTTP Cookie File
midway-auth.amazon.com\tFALSE\t/\tTRUE\t9999999999\tuser_name\tbolichen
#HttpOnly_.midway-auth.amazon.com\tTRUE\t/\tTRUE\t9999999999\ttpm_metrics\teyJTdHVmZg==
"""


# ── TestIsPlaywrightInstalled ────────────────────────────────────────────────


class TestIsPlaywrightInstalled:
    def test_returns_false_in_oss(self):
        # The Amazon-internal package manager that backed this check is not
        # shipped in OSS, so the stub always reports the package as not
        # installed (rather than shelling out to an internal tool).
        assert is_playwright_installed() is False


# ── TestEnsurePlaywrightInstalled ────────────────────────────────────────────


class TestEnsurePlaywrightInstalled:
    def test_is_noop_in_oss(self):
        # The upstream flow installed Playwright MCP via an Amazon-internal
        # package manager; that path is removed in OSS, so this is a no-op
        # that neither raises nor returns a value.
        assert ensure_playwright_installed() is None


# ── TestIsHeaded / TestGetPlaywrightMcpArgs ──────────────────────────────────


class TestIsHeaded:
    def test_headed_on_macos(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        assert is_headed() is True

    def test_headless_on_linux(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        assert is_headed() is False


class TestGetPlaywrightMcpArgs:
    def test_includes_headed_on_macos(self, monkeypatch):
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        monkeypatch.setattr(setup_mod, "is_headed", lambda: True)
        args = get_playwright_mcp_args()
        assert "--headed" in args
        assert "@playwright/mcp" in args

    def test_no_headed_on_linux(self, monkeypatch):
        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        monkeypatch.setattr(setup_mod, "is_headed", lambda: False)
        args = get_playwright_mcp_args()
        assert "--headed" not in args
        assert "@playwright/mcp" in args


# ── TestInjectCookiesViaPlaywright ───────────────────────────────────────────


class TestInjectCookiesViaPlaywright:
    def test_returns_dict_with_cookies_and_count(self, tmp_path: Path):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        result = inject_cookies_via_playwright(str(p))
        assert "cookies" in result
        assert "count" in result

    def test_count_matches_cookies_length(self, tmp_path: Path):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        result = inject_cookies_via_playwright(str(p))
        assert result["count"] == len(result["cookies"])
        assert result["count"] == 2

    def test_parses_cookie_fields(self, tmp_path: Path):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        result = inject_cookies_via_playwright(str(p))
        names = {c["name"] for c in result["cookies"]}
        assert "user_name" in names
        assert "tpm_metrics" in names

    def test_default_path_used_when_no_cookie_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        monkeypatch.setattr(setup_mod, "MIDWAY_COOKIE_PATH", p)
        result = inject_cookies_via_playwright()
        assert result["count"] == 2

    def test_missing_file_returns_empty_cookies(self, tmp_path: Path):
        missing = tmp_path / "no_such_cookie"
        result = inject_cookies_via_playwright(str(missing))
        assert result["cookies"] == []
        assert result["count"] == 0

    def test_httponly_cookie_parsed_correctly(self, tmp_path: Path):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        result = inject_cookies_via_playwright(str(p))
        httponly_cookies = [c for c in result["cookies"] if c.get("httpOnly")]
        assert len(httponly_cookies) == 1
        assert httponly_cookies[0]["name"] == "tpm_metrics"

    def test_empty_cookie_file_returns_zero_count(self, tmp_path: Path):
        p = tmp_path / "cookie"
        p.write_text("# Netscape HTTP Cookie File\n# just comments\n")
        result = inject_cookies_via_playwright(str(p))
        assert result["count"] == 0
        assert result["cookies"] == []


# ── TestGeneratePlaywrightConfig ─────────────────────────────────────────────


class TestGeneratePlaywrightConfig:
    def test_creates_config_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_path = generate_playwright_config()
        assert config_path.exists()

    def test_does_not_write_remote_debugging_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # B-minus dropped the CDP debug port — the live mirror now rides the
        # proxy's existing screenshot path, so no remote-debugging port is opened.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = json.loads(generate_playwright_config().read_text())
        args = config["browser"]["launchOptions"]["args"]
        assert not any("remote-debugging-port" in a for a in args)

    def test_config_has_correct_structure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_path = generate_playwright_config()
        config = json.loads(config_path.read_text())
        assert "browser" in config
        assert "capabilities" in config
        assert config["browser"]["browserName"] == "chromium"
        assert "storageState" in config["browser"]["contextOptions"]

    def test_storage_state_path_is_absolute(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_path = generate_playwright_config()
        config = json.loads(config_path.read_text())
        storage_state = config["browser"]["contextOptions"]["storageState"]
        assert storage_state.startswith(str(tmp_path))
        assert "playwright-storage-state.json" in storage_state

    def test_config_written_to_kirocrew_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_path = generate_playwright_config()
        assert ".kirocrew" in str(config_path)
        assert config_path.name == "playwright-config.json"

    def test_parent_dir_created_if_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        kirocrew_dir = tmp_path / ".kirocrew"
        assert not kirocrew_dir.exists()
        generate_playwright_config()
        assert kirocrew_dir.exists()

    def test_config_pins_chromium_channel(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # Without this pin @playwright/mcp defaults launchOptions.channel to the
        # branded "chrome" channel, which overrides browserName and is absent on
        # headless/Cloud Desktop hosts; pin it to bundled "chromium".
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = json.loads(generate_playwright_config().read_text())
        assert config["browser"]["launchOptions"]["channel"] == "chromium"


# ── TestRefreshStorageState ──────────────────────────────────────────────────


class TestRefreshStorageState:
    def test_returns_error_when_cookie_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        missing = tmp_path / "no_cookie"
        monkeypatch.setattr(setup_mod, "MIDWAY_COOKIE_PATH", missing)
        result = refresh_storage_state()
        assert result["ok"] is False
        # OSS build has no bundled browser-auth cookie source.
        assert "not available in OSS" in result["error"]

    def test_returns_error_when_no_cookies_parsed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        p = tmp_path / "cookie"
        p.write_text("# Netscape HTTP Cookie File\n# just comments\n")
        monkeypatch.setattr(setup_mod, "MIDWAY_COOKIE_PATH", p)
        result = refresh_storage_state()
        assert result["ok"] is False
        assert "no cookies" in result["error"]

    def test_success_creates_storage_state_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        midway_dir = tmp_path / ".midway"
        midway_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(setup_mod, "MIDWAY_COOKIE_PATH", p)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = refresh_storage_state()
        assert result["ok"] is True
        assert result["count"] == 2
        storage_path = Path(result["path"])
        assert storage_path.exists()

    def test_success_storage_state_valid_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        midway_dir = tmp_path / ".midway"
        midway_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(setup_mod, "MIDWAY_COOKIE_PATH", p)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = refresh_storage_state()
        storage_path = Path(result["path"])
        data = json.loads(storage_path.read_text())
        assert "cookies" in data
        assert "origins" in data
        assert len(data["cookies"]) == 2

    def test_success_returns_expired_count(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        p = tmp_path / "cookie"
        p.write_text(SAMPLE_COOKIES)
        midway_dir = tmp_path / ".midway"
        midway_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(setup_mod, "MIDWAY_COOKIE_PATH", p)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = refresh_storage_state()
        assert "expired" in result
        assert isinstance(result["expired"], int)


# ── TestGetPlaywrightMcpArgsWithConfig ───────────────────────────────────────


class TestGetPlaywrightMcpArgsWithConfig:
    def test_includes_config_flag_when_file_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "is_headed", lambda: False)
        # Create the config file
        config_path = tmp_path / ".kirocrew" / "playwright-config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("{}")
        args = get_playwright_mcp_args()
        assert "--config" in args
        assert str(config_path) in args

    def test_no_config_flag_when_file_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod, "is_headed", lambda: False)
        args = get_playwright_mcp_args()
        assert "--config" not in args
        assert "@playwright/mcp" in args
