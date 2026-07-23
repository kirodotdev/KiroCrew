"""Playwright MCP browser setup (OSS stub).

The upstream build wired browser setup to a managed package installer and an
enterprise-SSO cookie/storage-state flow. In the open-source build those steps
are neutralized: every public symbol is preserved so importing modules keep
working, but SSO setup is a no-op and reports "not available in OSS".
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.browser.auth import SSO_COOKIE_PATH, parse_netscape_cookies


def is_playwright_installed() -> bool:
    """Check whether the Playwright MCP package is resolvable on PATH (OSS stub).

    The managed package manager that originally backed this check is not
    available in the open-source build, so this returns False gracefully.
    """
    return False


def ensure_playwright_installed() -> None:
    """Browser setup is not available in the open-source build (no-op stub).

    The upstream flow installed Playwright MCP via a managed package manager and
    wired enterprise-SSO cookie injection. Neither is shipped in OSS, so this is
    a no-op rather than raising.
    """
    return None


def is_headed() -> bool:
    """Return True if browser should run in headed mode.

    Headed on macOS and Windows (a desktop user session is available and a
    visible Chromium window is preferred so users can complete interactive SSO
    prompts). Headless on Linux, where the gateway typically runs on a
    server without an accessible display.
    """
    return platform.system() in ("Darwin", "Windows")


def has_playwright_extension() -> bool:
    """Check if user has opted into Playwright Chrome extension mode.

    Extension mode attaches to the user's running Chrome (with all existing auth)
    instead of launching a separate headless browser, which reuses whatever
    session and extensions the real Chrome already has.
    """
    flag_file = Path.home() / ".kirocrew" / "playwright-extension-mode"
    return flag_file.exists()


def get_extension_token() -> str | None:
    """Read the stored Playwright extension token."""
    token_file = Path.home() / ".kirocrew" / "playwright-extension-token"
    if token_file.exists():
        return token_file.read_text().strip() or None
    return None


def get_playwright_mcp_env() -> dict[str, str]:
    """Return env vars needed for Playwright MCP (extension token if set)."""
    env: dict[str, str] = {}
    if has_playwright_extension():
        token = get_extension_token()
        if token:
            env["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] = token
    return env


def generate_playwright_config() -> Path:
    """Generate ~/.kirocrew/playwright-config.json with correct absolute paths.

    The open-source build ships a generic Chromium config with no
    enterprise auth-server allowlist.
    """
    config_path = Path.home() / ".kirocrew" / "playwright-config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    storage_state = str(Path.home() / ".kirocrew" / "playwright-storage-state.json")

    config = {
        "browser": {
            "browserName": "chromium",
            "isolated": True,
            "launchOptions": {
                "channel": "chromium",
                "args": [],
            },
            "contextOptions": {
                "storageState": storage_state,
            },
        },
        "capabilities": ["network", "storage"],
    }

    config_path.write_text(json.dumps(config, indent=2))
    return config_path


def refresh_storage_state() -> dict[str, Any]:
    """Refresh the Playwright storage state from the browser cookie file.

    Reads cookies via the (OSS-stubbed) browser auth layer and writes them to
    a Playwright-compatible storage-state file. Returns a not-available result
    when no cookie source exists, which is the default in the open-source build.
    """
    if not SSO_COOKIE_PATH.exists():
        return {"ok": False, "error": "browser auth not available in OSS"}

    cookies = parse_netscape_cookies(SSO_COOKIE_PATH)
    if not cookies:
        return {"ok": False, "error": "no cookies parsed"}

    storage_state_path = Path.home() / ".kirocrew" / "playwright-storage-state.json"
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(storage_state_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"cookies": cookies, "origins": []}, f, indent=2)

    expired = [c for c in cookies if 0 < c.get("expires", -1) < time.time()]
    return {
        "ok": True,
        "path": str(storage_state_path),
        "count": len(cookies),
        "expired": len(expired),
    }


def get_playwright_mcp_args() -> list[str]:
    """Return Playwright MCP launch args based on platform and mode.

    Extension mode (--extension): attaches to user's running Chrome with existing auth.
    Config mode (--config): launches separate Chromium with cookie injection.
    """
    args = ["@playwright/mcp"]
    if has_playwright_extension():
        args.append("--extension")
        return args
    config_path = Path.home() / ".kirocrew" / "playwright-config.json"
    if config_path.exists():
        args.extend(["--config", str(config_path)])
    if is_headed():
        args.append("--headed")
    return args


def _kirocrew_bin() -> str:
    """Resolve path to the kirocrew binary."""
    return shutil.which("kirocrew") or "kirocrew"


def patch_mcp_extension(token: str) -> None:
    """Update MCP config to use proxy with --extension and token env var."""
    mcp_json = Path.home() / ".kiro" / "settings" / "mcp.json"
    if not mcp_json.exists():
        return
    try:
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        servers = data.setdefault("mcpServers", {})
        entry = {
            "command": _kirocrew_bin(),
            "args": ["mcp-playwright-proxy", "--extension"],
            "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": token},
        }
        servers["@playwright/mcp"] = entry
        mcp_json.write_text(json.dumps(data, indent=2), encoding="utf-8")
        platform_compat.chmod_safe(str(mcp_json), 0o600)
    except (json.JSONDecodeError, OSError):
        pass


def patch_mcp_headless() -> None:
    """Update MCP config to use proxy with headless mode config."""
    mcp_json = Path.home() / ".kiro" / "settings" / "mcp.json"
    if not mcp_json.exists():
        return
    try:
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        servers = data.setdefault("mcpServers", {})
        config_path = str(Path.home() / ".kirocrew" / "playwright-config.json")
        entry = {
            "command": _kirocrew_bin(),
            "args": ["mcp-playwright-proxy", "--config", config_path],
        }
        servers["@playwright/mcp"] = entry
        mcp_json.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except (json.JSONDecodeError, OSError):
        pass


def inject_cookies_via_playwright(cookie_file: str | None = None) -> dict[str, Any]:
    """Parse the browser cookie file and return cookies in Playwright format.

    Args:
        cookie_file: Path to Netscape cookie file. Defaults to SSO_COOKIE_PATH.

    Returns:
        Dict with "cookies" list and "count" integer.
    """
    path = Path(cookie_file) if cookie_file is not None else SSO_COOKIE_PATH
    cookies = parse_netscape_cookies(path)
    return {"cookies": cookies, "count": len(cookies)}
