"""Browser CLI — Playwright setup (OSS stub).

The browser auth subcommands (`health`, `inject`, `refresh`, `federate`) front
an enterprise SSO/cookie flow that is not shipped in the open-source build, so
they are recognized subcommands but report "not available in OSS".

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
  Attach mode (recommended for macOS): Playwright drives your real running
    Chromium-based browser (Chrome, Edge, Brave, Arc, Opera) and its existing
    logins. Requires the Playwright Extension (one Chrome Web Store listing
    covers the whole family):
      https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm
  Headless mode (default on Linux): Playwright launches its own browser. The
    engine (chromium/firefox/webkit) is chosen in Settings → Browser; firefox
    and webkit are Playwright's own builds, not your installed Firefox/Safari.
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

    # Deliberately does NOT enable Browser Mode: the durable enable flag is a
    # keystone the agent cannot write, and this CLI is reachable from an
    # auto-approved agent shell, so self-enabling here would be a consent bypass.
    # The dashboard Settings API is the SOLE enabler. register_playwright_proxy
    # therefore no-ops with "mode-disabled" until the operator flips the toggle;
    # this command still writes the config + reports launcher status so setup is
    # otherwise ready.

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
    print("Turn on Browser Mode in Settings → Browser (or the dashboard prompt).")
    print("Once it is on, the agent operates the browser directly — the Browser")
    print("panel shows a read-only live mirror of what it is doing.")


def _cmd_extension(action: str) -> None:
    """Enable or disable Playwright Chrome extension mode."""
    kirocrew_dir = config_dir()
    kirocrew_dir.mkdir(parents=True, exist_ok=True)
    flag_file = kirocrew_dir / "playwright-extension-mode"
    token_file = kirocrew_dir / "playwright-extension-token"

    if action == "on":
        print("Attach to your running browser (Playwright Extension)")
        print("=" * 40)
        print()
        print("1. Install the Playwright Extension for your Chromium-based browser")
        print("   (Chrome, Edge, Brave, Arc, Opera):")
        print("   https://chromewebstore.google.com/detail/"
              "playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm")
        print()
        print("2. When the agent connects, pick the browser tab it should drive.")
        print()
        print("The connection token is OPTIONAL — it only skips the per-connection")
        print("approval prompt. Click the extension icon to copy the")
        print("PLAYWRIGHT_MCP_EXTENSION_TOKEN value, or press Enter to skip.")
        token = input("3. Paste your extension token (optional): ").strip()
        if token.startswith("PLAYWRIGHT_MCP_EXTENSION_TOKEN="):
            token = token.split("=", 1)[1]
        if token:
            fd = os.open(str(token_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token)
        flag_file.touch()
        # Does NOT enable Browser Mode: that durable flag is a keystone the agent
        # cannot write, and enabling it here (a CLI reachable from an
        # auto-approved shell) would be a consent bypass. The dashboard Settings
        # toggle is the sole enabler; this only records the attach preference +
        # token so it is ready once the operator turns Browser Mode on.
        _reregister_proxy()
        print()
        print("Attach preference saved.")
        print("Turn on Browser Mode in Settings -> Browser to activate it, then")
        print("restart the gateway: kirocrew stop && kirocrew gateway")
    elif action == "off":
        flag_file.unlink(missing_ok=True)
        token_file.unlink(missing_ok=True)
        _reregister_proxy()
        print("Extension mode disabled. Using separate headless Chromium.")
        print("Restart gateway to apply: kirocrew stop && kirocrew gateway")
    else:
        print("Usage: kirocrew browse extension <on|off>", file=sys.stderr)
        sys.exit(1)


def _reregister_proxy() -> None:
    """Rewrite the proxy entry for the mode just recorded in the flag file.

    Goes through ``register_playwright_proxy`` rather than the patch primitives so
    the write takes the shared mcp.json lock, keeps a user-authored non-proxy
    entry under the canonical key, and creates the config when absent. The mode
    dispatch is read from the flag file, which the caller has already updated.
    """
    _, status = _setup.register_playwright_proxy()
    if status == "kept-user-entry":
        print("  Kept your existing playwright-mcp entry in mcp.json (left untouched).")
        print("  Remove it to let KiroCrew manage the browse server.")


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
