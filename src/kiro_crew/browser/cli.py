"""Browser CLI — Playwright setup (OSS stub).

The browser auth subcommands (`health`, `inject`, `refresh`, `federate`) were
wired to an enterprise SSO/cookie flow that is not shipped in the open-source
build. They are preserved as recognized subcommands but report
"not available in OSS".

Usage:
    kirocrew browse setup              # Generate Playwright MCP config (OSS)
    kirocrew browse auth health        # not available in OSS
    kirocrew browse auth inject        # not available in OSS
    kirocrew browse auth refresh       # not available in OSS
    kirocrew browse auth federate <url># not available in OSS
"""

from __future__ import annotations

import json
import os
import sys

from kiro_crew.browser import setup as _setup
from kiro_crew.config.paths import config_dir


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
    print(
        """kirocrew browse — Playwright MCP browser setup

Commands:
  setup                Configure the Browser panel: write config, register the
                       proxy, and check the @playwright/mcp launcher (OSS)
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
"""
    )


def _cmd_setup() -> None:
    """Guided one-command Playwright MCP setup for the Browser panel.

    Writes the headless Chromium config, registers the compression proxy in
    kiro's ``mcp.json`` (creating it if absent), and checks that a Playwright
    launcher is resolvable — then prints a ✓/✗ checklist and the single
    remaining action (restart the gateway). The public ``@playwright/mcp``
    package still installs separately in the OSS build; this reports whether it
    is resolvable and gives the exact install command when it is not.
    """
    print("Setting up Playwright MCP for the Browser panel...\n")

    cfg = _setup.generate_playwright_config()
    print(f"  \u2713 Playwright config (headless)   {cfg}")

    mcp_json, reg_status = _setup.register_playwright_proxy()
    if reg_status == "kept-user-entry":
        print(f"  \u00b7 Proxy NOT registered           {mcp_json}")
        print("    (you already have your own 'playwright-mcp' server — left untouched)")
    else:
        print(f"  \u2713 Proxy registered               {mcp_json}")

    ok, detail = _setup.check_playwright_launchable()
    mark = "\u2713" if ok else "\u2717"
    print(f"  {mark} @playwright/mcp launcher       {detail}")

    storage_state = config_dir() / "playwright-storage-state.json"
    if storage_state.exists():
        print(f"  \u2713 Storage state                  {storage_state}")
    else:
        print("  \u00b7 Storage state (optional)       not set — public sites work without it")

    print()
    if not ok:
        print("Action needed — install the Playwright MCP package, then re-run this:")
        print("  npm install -g @playwright/mcp        # or: npx @playwright/mcp\n")
    print("Restart the gateway to apply:   kirocrew stop && kirocrew gateway\n")
    print("The Browser panel is a read-only live mirror (view-only). Toggle")
    print('"Browser use" (the Globe) to let the agent operate the page.')


def _cmd_extension(action: str) -> None:
    """Enable or disable Playwright Chrome extension mode."""
    kirocrew_dir = config_dir()
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
    """Browser auth health check — delegates to the auth layer.

    In the OSS build the auth layer reports "not available in OSS"; an edition
    that registered a ``BrowserAuthProvider`` surfaces its real health here.
    """
    from kiro_crew.browser import auth as _auth

    result = _auth.health()
    if result.get("available"):
        print(json.dumps(result, indent=2))
        return
    print("browser auth health: not available in OSS")
    sys.exit(1)


def _cmd_auth_inject() -> None:
    """Cookie injection — delegates to the auth layer (OSS: not available)."""
    from kiro_crew.browser import auth as _auth

    result = _auth.ensure()
    if result.get("available"):
        print(json.dumps(result, indent=2))
        return
    print("browser auth inject: not available in OSS")
    sys.exit(1)


def _cmd_auth_refresh() -> None:
    """Storage-state refresh — delegates to the auth layer (OSS: not available)."""
    from kiro_crew.browser import auth as _auth

    if _auth.refresh_cookie_via_sso():
        print("browser auth refresh: ok")
        return
    print("browser auth refresh: not available in OSS")
    sys.exit(1)


def _cmd_auth_federate(url: str) -> None:
    """Federated SSO — delegates to the auth layer (OSS: not available)."""
    from kiro_crew.browser import auth as _auth

    result = _auth.federated_login(url)
    if result.get("ok"):
        print(json.dumps({k: v for k, v in result.items() if k != "cookies"}, indent=2))
        return
    print("browser auth federate: not available in OSS")
    sys.exit(1)
