"""Browser CLI — Playwright setup (OSS stub).

The browser auth subcommands (`health`, `inject`, `refresh`, `federate`) were
wired to an Amazon-internal SSO/cookie flow that is not shipped in the
open-source build. They are preserved as recognized subcommands but report
"not available in OSS".

Usage:
    kirocrew browse setup              # Generate Playwright MCP config (OSS)
    kirocrew browse auth health        # not available in OSS
    kirocrew browse auth inject        # not available in OSS
    kirocrew browse auth refresh       # not available in OSS
    kirocrew browse auth federate <url># not available in OSS
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from kiro_crew.browser import setup as _setup


def main() -> None:
    run_browse(sys.argv[1:])


def run_browse(args: list[str]) -> None:
    """Entry point for `kirocrew browse <subcommand>`."""
    if not args:
        _print_help()
        return

    cmd = args[0]

    if cmd == "setup":
        _cmd_setup()
        return
    elif cmd == "extension" and len(args) >= 2:
        _cmd_extension(args[1])
        return

    if cmd == "auth" and len(args) >= 2:
        subcmd = args[1]
        if subcmd == "health":
            _cmd_auth_health()
        elif subcmd == "inject":
            _cmd_auth_inject()
        elif subcmd == "refresh":
            _cmd_auth_refresh()
        elif subcmd == "federate" and len(args) >= 3:
            _cmd_auth_federate(args[2])
        else:
            print(f"Unknown auth subcommand: {subcmd}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Unknown command: {cmd}. Run 'kirocrew browse' for help.", file=sys.stderr)
        sys.exit(1)


def _print_help() -> None:
    print("""kirocrew browse — Playwright MCP browser setup

Commands:
  setup                Generate the Playwright MCP config (OSS)
  auth health          not available in OSS
  auth inject          not available in OSS
  auth refresh         not available in OSS
  auth federate <url>  not available in OSS
  extension on         Enable extension mode (attach to your running Chrome)
  extension off        Disable extension mode (use separate headless Chromium)

Modes:
  Extension mode (recommended for macOS): Playwright controls your real Chrome
    with all existing auth — no cookie injection needed. Requires the Playwright
    Chrome extension: https://chromewebstore.google.com/detail/mmlmfjhmonkocbjadbfplnigmagldckm
  Headless mode (default on Linux): Launches separate Chromium.
""")


def _cmd_setup() -> None:
    """Generate the Playwright MCP config (OSS).

    Automated Playwright MCP installation is not available in the open-source
    build. Install the public ``@playwright/mcp`` package yourself (for example
    via ``npx @playwright/mcp``); this command only writes the local config.
    """
    print("Generating Playwright MCP config...")
    _setup.ensure_playwright_installed()
    _setup.generate_playwright_config()
    print("Done. Wrote Playwright MCP config to ~/.kirocrew/playwright-config.json.")
    print("Install the public @playwright/mcp package separately (e.g. npx @playwright/mcp).")


def _cmd_extension(action: str) -> None:
    """Enable or disable Playwright Chrome extension mode."""
    kirocrew_dir = Path.home() / ".kirocrew"
    kirocrew_dir.mkdir(parents=True, exist_ok=True)
    flag_file = kirocrew_dir / "playwright-extension-mode"
    token_file = kirocrew_dir / "playwright-extension-token"

    if action == "on":
        print("Playwright Chrome Extension Setup")
        print("=" * 40)
        print()
        print("1. Install the extension from Chrome Web Store:")
        print("   https://chromewebstore.google.com/detail/mmlmfjhmonkocbjadbfplnigmagldckm")
        print()
        print("2. Click the extension icon in Chrome to see your connection token.")
        print("   It looks like: PLAYWRIGHT_MCP_EXTENSION_TOKEN=xxxxx...")
        print()
        token = input("3. Paste your extension token here: ").strip()
        if token.startswith("PLAYWRIGHT_MCP_EXTENSION_TOKEN="):
            token = token.split("=", 1)[1]
        if not token:
            print("No token provided. Aborting.", file=sys.stderr)
            sys.exit(1)
        fd = os.open(str(token_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
        flag_file.touch()
        _patch_mcp_config_extension(token)
        print()
        print("Extension mode enabled.")
        print("Restart gateway to apply: kirocrew stop && kirocrew gateway")
    elif action == "off":
        flag_file.unlink(missing_ok=True)
        token_file.unlink(missing_ok=True)
        _patch_mcp_config_headless()
        print("Extension mode disabled. Using separate headless Chromium.")
        print("Restart gateway to apply: kirocrew stop && kirocrew gateway")
    else:
        print("Usage: kirocrew browse extension <on|off>", file=sys.stderr)
        sys.exit(1)


def _patch_mcp_config_extension(token: str) -> None:
    """Update MCP config to use --extension with token env var."""
    _setup.patch_mcp_extension(token)


def _patch_mcp_config_headless() -> None:
    """Update MCP config to use headless mode with --config."""
    _setup.patch_mcp_headless()


def _cmd_auth_health() -> None:
    """Browser auth health check is not available in the open-source build."""
    print("browser auth health: not available in OSS")
    sys.exit(1)


def _cmd_auth_inject() -> None:
    """Cookie injection is not available in the open-source build."""
    print("browser auth inject: not available in OSS")
    sys.exit(1)


def _cmd_auth_refresh() -> None:
    """Storage-state refresh is not available in the open-source build."""
    print("browser auth refresh: not available in OSS")
    sys.exit(1)


def _cmd_auth_federate(url: str) -> None:
    """Federated SSO is not available in the open-source build."""
    print("browser auth federate: not available in OSS")
    sys.exit(1)
