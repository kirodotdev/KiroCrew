# Browser Module Playwright Rewrite

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 3000-line CDP browser module with a thin auth shim (~300 lines) that delegates all browsing to Playwright MCP.

**Architecture:** KiroCrew provides only what Playwright MCP cannot: Amazon auth (Midway cookies, federate SSO, SPNEGO/Kerberos). The agent uses Playwright MCP tools (`browser_navigate`, `browser_click`, `browser_snapshot`, etc.) for all interaction. KiroCrew's role is (1) install Playwright + browsers during setup, (2) prepare auth before browsing starts, (3) inject cookies into the Playwright browser context, and (4) provide a skill/prompt that guides the agent through auth validation.

**Tech Stack:** Python 3.10+, Playwright (npm `@playwright/mcp`), existing Midway/MCS auth

---

## Current State (what we're cutting)

| File | Lines | Verdict |
|------|-------|---------|
| `browser/cdp.py` | 245 | **DELETE** — Playwright handles CDP |
| `browser/session.py` | 724 | **DELETE** — Playwright MCP does click/fill/type/snap |
| `browser/snapshot.py` | 199 | **DELETE** — `browser_snapshot` does this |
| `browser/chrome.py` | 499 | **SLIM** — keep only `find_chrome()` for profile location; Playwright installs its own |
| `browser/auth.py` | 477 | **KEEP** — federate flow, midway cookies, KRB5CCNAME, health check |
| `browser/cli.py` | 417 | **REWRITE** — slim to auth commands + Playwright install |
| `mcp_browser.py` | 689 | **DELETE** — Playwright MCP replaces our custom MCP server |
| `browser/__init__.py` | 1 | **KEEP** (update docstring) |
| Tests (7 files) | 3672 | **DELETE** most; keep auth tests, add new setup/skill tests |

## New File Structure

```
src/kiro_crew/browser/
├── __init__.py          # docstring only
├── auth.py              # KEEP: midway cookies, federate_auth, KRB5CCNAME, health
├── setup.py             # NEW (~80 lines): install playwright, install browsers, configure MCP
└── cli.py               # REWRITE (~100 lines): kirocrew browse auth|setup|install

src/kiro_crew/config/
├── prompt.md            # MODIFY: rewrite browser section for Playwright MCP
└── defaults.json        # MODIFY: add playwright MCP server entry

src/kiro_crew/skills/
└── browser-auth.md      # NEW: skill that guides agent through auth + Playwright usage

test/
├── test_browser_auth.py # KEEP (already covers federate, midway, health)
└── test_browser_setup.py # NEW: tests for playwright install + MCP config
```

## What the Agent Sees After This

When user clicks the 🌐 browse button:
1. Backend injects `[BROWSE]` marker (unchanged)
2. Agent's system prompt tells it to use the `browser-auth` skill
3. Skill instructs: run `kirocrew browse auth health` → validate → then use Playwright MCP tools
4. Agent calls `browser_navigate`, `browser_snapshot`, `browser_click`, etc. via Playwright MCP
5. If auth fails: agent runs `kirocrew browse auth inject` to push cookies into Playwright context

---

### Task 1: Delete Obsolete Files

**Files:**
- Delete: `src/kiro_crew/browser/cdp.py`
- Delete: `src/kiro_crew/browser/session.py`
- Delete: `src/kiro_crew/browser/snapshot.py`
- Delete: `src/kiro_crew/mcp_browser.py`
- Delete: `test/test_browser_cdp.py`
- Delete: `test/test_browser_session.py`
- Delete: `test/test_browser_snapshot.py`
- Delete: `test/test_mcp_browser.py`
- Delete: `test/test_browser_cli.py`

- [ ] **Step 1: Remove files**

```bash
git rm src/kiro_crew/browser/cdp.py
git rm src/kiro_crew/browser/session.py
git rm src/kiro_crew/browser/snapshot.py
git rm src/kiro_crew/mcp_browser.py
git rm test/test_browser_cdp.py
git rm test/test_browser_session.py
git rm test/test_browser_snapshot.py
git rm test/test_mcp_browser.py
git rm test/test_browser_cli.py
```

- [ ] **Step 2: Remove dead imports in auth.py**

`auth.py` imports `from kiro_crew.browser.cdp import CDPClient, CDPError`. The `inject_cookies` and `inject_mcs` functions use CDPClient directly. These functions are no longer needed since Playwright manages its own cookies. Remove:
- `inject_cookies()`
- `inject_from_file()`
- `inject_mcs()`
- `get_midway_token()`
- The `CDPClient`/`CDPError` import

Keep:
- `parse_netscape_cookies()` (used by setup to read midway cookies)
- `federate_auth()` (used by auth CLI to complete SSO)
- `_krb5_env()` / `_curl_cmd()` (used by federate_auth)
- `health()` / `ensure()` / `has_kerberos_ticket()` / `has_mcscli()`
- `refresh_cookie_via_mcs()` / `refresh_aea()` / `mcs_keys_process_running()`
- `cookie_path()` / `MIDWAY_COOKIE_PATH`

- [ ] **Step 3: Remove dead imports in other files**

In `src/kiro_crew/cli.py`:
- Remove the `mcp-browser` subparser (lines ~759-760)
- Remove the `elif args.command == "mcp-browser"` handler (lines ~1084-1088)
- Keep the `browse` subparser but simplify help text

In `src/kiro_crew/dashboard/handlers/messaging.py`:
- Remove `api_browser_auth_retry` function (uses `_get_session` from deleted mcp_browser)
- Or rewrite it to call `kirocrew browse auth inject` via subprocess

In `src/kiro_crew/cli_setup.py`:
- Remove `from kiro_crew.browser.chrome import find_chrome, profile_dir_for_session, sync_profile`
- Replace browser setup section with Playwright install check

- [ ] **Step 4: Run tests to confirm no import errors**

```bash
python -m pytest test/test_browser_auth.py -v
python -c "from kiro_crew.browser.auth import health, federate_auth, parse_netscape_cookies"
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(browser): remove CDP/session/snapshot — delegate to Playwright MCP"
```

---

### Task 2: Slim chrome.py → Keep Only What's Needed

**Files:**
- Modify: `src/kiro_crew/browser/chrome.py`
- Delete: `test/test_browser_chrome.py`

The only thing we still need from chrome.py is the Chrome profile path logic for cookie extraction. Playwright installs and manages its own browser. We don't need launch/kill/install/CDP-alive checks.

- [ ] **Step 1: Gut chrome.py down to essentials**

Keep only:
- `_KIROCREW_DIR` / `_PROFILES_DIR` constants
- `RSYNC_EXCLUDES` (for profile sync reference)

Actually — we don't even need chrome.py at all. The only remaining consumer is `cli_setup.py` which we're rewriting. **Delete chrome.py entirely.**

```bash
git rm src/kiro_crew/browser/chrome.py
git rm test/test_browser_chrome.py
```

- [ ] **Step 2: Commit**

```bash
git add -A
git commit -m "refactor(browser): remove chrome.py — Playwright manages its own browser"
```

---

### Task 3: Create browser/setup.py — Playwright Installation

**Files:**
- Create: `src/kiro_crew/browser/setup.py`
- Create: `test/test_browser_setup.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for kiro_crew.browser.setup — Playwright install and MCP config."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.browser.setup import (
    ensure_playwright_installed,
    get_playwright_mcp_config,
    is_playwright_installed,
)


class TestIsPlaywrightInstalled:
    def test_returns_true_when_npx_succeeds(self, monkeypatch):
        mock_run = MagicMock(returncode=0, stdout="Version 1.58.0")
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock_run)
        assert is_playwright_installed() is True

    def test_returns_false_when_npx_fails(self, monkeypatch):
        mock_run = MagicMock(returncode=1, stdout="")
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock_run)
        assert is_playwright_installed() is False

    def test_returns_false_when_npx_not_found(self, monkeypatch):
        def raise_fnf(*a, **kw):
            raise FileNotFoundError()
        monkeypatch.setattr("subprocess.run", raise_fnf)
        assert is_playwright_installed() is False


class TestEnsurePlaywrightInstalled:
    def test_installs_when_not_present(self, monkeypatch):
        calls = []
        def mock_run(cmd, **kw):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="")
        monkeypatch.setattr("subprocess.run", mock_run)
        monkeypatch.setattr("kiro_crew.browser.setup.is_playwright_installed", lambda: False)
        ensure_playwright_installed()
        assert any("playwright" in str(c) for c in calls)

    def test_skips_when_already_present(self, monkeypatch):
        calls = []
        def mock_run(cmd, **kw):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="")
        monkeypatch.setattr("subprocess.run", mock_run)
        monkeypatch.setattr("kiro_crew.browser.setup.is_playwright_installed", lambda: True)
        ensure_playwright_installed()
        assert not any("install" in str(c) for c in calls)


class TestGetPlaywrightMcpConfig:
    def test_returns_valid_mcp_entry(self):
        config = get_playwright_mcp_config()
        assert config["command"] == "npx"
        assert "@playwright/mcp" in config["args"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest test/test_browser_setup.py -v
```
Expected: ImportError

- [ ] **Step 3: Implement setup.py**

```python
"""Playwright browser setup — install and configure Playwright MCP for KiroCrew."""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


def is_playwright_installed() -> bool:
    """Check if Playwright MCP is available via npx."""
    try:
        r = subprocess.run(
            ["npx", "@playwright/mcp", "--version"],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def ensure_playwright_installed() -> None:
    """Install Playwright MCP and browsers if not already present."""
    if is_playwright_installed():
        logger.info("Playwright MCP already installed")
        return

    if not shutil.which("npx"):
        raise RuntimeError("npx not found — install Node.js first (toolbox install node)")

    logger.info("Installing Playwright MCP...")
    subprocess.run(
        ["aim", "mcp", "install", "npm:@playwright/mcp"],
        capture_output=True, text=True, timeout=120, check=True,
    )

    logger.info("Installing Playwright browsers (chromium)...")
    subprocess.run(
        ["npx", "playwright", "install", "chromium"],
        capture_output=True, text=True, timeout=180, check=True,
    )


def get_playwright_mcp_config() -> dict[str, Any]:
    """Return MCP server config entry for Playwright."""
    return {
        "command": "npx",
        "args": ["@playwright/mcp"],
    }


def inject_cookies_via_playwright(cookie_file: str | None = None) -> dict[str, Any]:
    """Prepare Midway cookies in Playwright-compatible format.

    Returns a dict that can be passed as launch config or used with
    browser_evaluate to inject cookies at runtime.
    """
    from kiro_crew.browser.auth import MIDWAY_COOKIE_PATH, parse_netscape_cookies
    from pathlib import Path

    path = Path(cookie_file) if cookie_file else MIDWAY_COOKIE_PATH
    raw_cookies = parse_netscape_cookies(path)

    # Convert to Playwright cookie format
    pw_cookies = []
    for c in raw_cookies:
        pw_cookies.append({
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path", "/"),
            "secure": c.get("secure", True),
            "httpOnly": c.get("httpOnly", False),
            "sameSite": "None",
            "expires": c.get("expires", -1),
        })
    return {"cookies": pw_cookies, "count": len(pw_cookies)}
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest test/test_browser_setup.py -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/kiro_crew/browser/setup.py test/test_browser_setup.py
git commit -m "feat(browser): add Playwright install and MCP config helper"
```

---

### Task 4: Rewrite browser/cli.py — Auth-Only Commands

**Files:**
- Rewrite: `src/kiro_crew/browser/cli.py`

The CLI is now just for auth operations and Playwright setup. All browsing happens through Playwright MCP.

- [ ] **Step 1: Rewrite cli.py**

```python
"""Browser CLI — auth management and Playwright setup.

Usage:
    kirocrew browse setup              # Install Playwright + browsers
    kirocrew browse auth health        # Check Midway/Kerberos auth status
    kirocrew browse auth inject        # Inject cookies into running Playwright browser
    kirocrew browse auth federate <url># Complete federate SSO flow for a URL
"""

from __future__ import annotations

import json
import sys
from typing import Any

from kiro_crew.browser.auth import (
    _krb5_env,
    federate_auth,
    has_kerberos_ticket,
    health as auth_health,
    parse_netscape_cookies,
    MIDWAY_COOKIE_PATH,
)
from kiro_crew.browser.setup import (
    ensure_playwright_installed,
    inject_cookies_via_playwright,
    is_playwright_installed,
)


def run_browse(args: list[str]) -> None:
    """Entry point for `kirocrew browse <subcommand>`."""
    if not args:
        _print_help()
        return

    cmd = args[0]

    if cmd == "setup":
        _cmd_setup()
    elif cmd == "auth" and len(args) >= 2:
        subcmd = args[1]
        if subcmd == "health":
            _cmd_auth_health()
        elif subcmd == "inject":
            _cmd_auth_inject()
        elif subcmd == "federate" and len(args) >= 3:
            _cmd_auth_federate(args[2])
        else:
            print(f"Unknown auth subcommand: {subcmd}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Unknown command: {cmd}. Run 'kirocrew browse' for help.", file=sys.stderr)
        sys.exit(1)


def _print_help() -> None:
    print("""kirocrew browse — auth management for Playwright MCP browsing

Commands:
  setup                Install Playwright + browsers
  auth health          Check Midway/Kerberos/MCS auth status
  auth inject          Print cookies in Playwright-injectable format (JSON)
  auth federate <url>  Complete federate SSO for a URL, print id_token URL

After setup, use Playwright MCP tools (browser_navigate, browser_click, etc.)
for all browsing. Auth cookies are injected automatically via the browser-auth skill.
""")


def _cmd_setup() -> None:
    """Install Playwright and browsers."""
    print("Installing Playwright MCP and browsers...")
    try:
        ensure_playwright_installed()
        print("✅ Playwright installed and ready")
    except Exception as exc:
        print(f"❌ Installation failed: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_auth_health() -> None:
    """Print auth health as JSON."""
    h = auth_health()
    print(json.dumps(h, indent=2))
    if not h.get("healthy"):
        sys.exit(1)


def _cmd_auth_inject() -> None:
    """Print cookies in Playwright format for injection."""
    result = inject_cookies_via_playwright()
    print(json.dumps(result, indent=2))


def _cmd_auth_federate(url: str) -> None:
    """Complete federate SSO and print the final URL."""
    result = federate_auth(url)
    print(json.dumps(result, indent=2))
    if not result.get("ok"):
        sys.exit(1)
```

- [ ] **Step 2: Update CLI registration in cli.py**

In `src/kiro_crew/cli.py`, update the browse help text:
```
kirocrew browse setup                        # Install Playwright + browsers
kirocrew browse auth health                  # Check auth status
kirocrew browse auth inject                  # Get cookies for injection
kirocrew browse auth federate <url>          # Complete federate SSO
```

Remove the `mcp-browser` subparser and handler entirely.

- [ ] **Step 3: Commit**

```bash
git add src/kiro_crew/browser/cli.py src/kiro_crew/cli.py
git commit -m "refactor(browser): rewrite CLI to auth-only commands"
```

---

### Task 5: Create Browser Auth Skill

**Files:**
- Create: `src/kiro_crew/skills/browser-auth.md`

This skill instructs the agent HOW to use Playwright MCP with Amazon auth. It's loaded when `[BROWSE]` marker is present.

- [ ] **Step 1: Write the skill**

```markdown
---
name: browser-auth
description: Authenticate and browse Amazon internal sites using Playwright MCP tools. Use when [BROWSE] marker is present.
---

# Browser Auth — Amazon Internal Browsing

You are browsing Amazon internal websites using Playwright MCP tools. Before navigating, validate auth.

## Step 1: Check Auth

Run this bash command first:
```bash
kirocrew browse auth health
```

**If healthy:** Proceed to Step 2.
**If unhealthy:** Tell the user what's missing:
- "no Kerberos ticket" → user must run `kinit -f`
- "midway expired" → user must run `mwinit -o`
- "no AEA posture" → user must run `mwinit --refresh-aea`

Do NOT proceed until auth is healthy.

## Step 2: Get Cookies

```bash
kirocrew browse auth inject
```

This prints Midway cookies in JSON format. Use `browser_evaluate` to inject them:

```javascript
// Inject cookies into browser context
const cookies = <PASTE_COOKIES_JSON>;
for (const c of cookies) {
  document.cookie = `${c.name}=${c.value}; domain=${c.domain}; path=${c.path}; secure; samesite=none`;
}
```

Or better — navigate to `midway-auth.amazon.com` first, inject cookies there, then navigate to your target.

## Step 3: Navigate

Use Playwright MCP tools:
- `browser_navigate` — go to URL
- `browser_snapshot` — get page structure (replaces `kirocrew browse snap`)
- `browser_click` — click elements
- `browser_fill_form` — fill inputs
- `browser_take_screenshot` — show user what you see

## Step 4: Handle Auth Gates

If after navigation you see:
- **midway-auth.amazon.com/login** in the URL → cookies expired, re-run Step 2
- **idp.federate.amazon.com** in the URL → run federate flow:
  ```bash
  kirocrew browse auth federate "<original_url>"
  ```
  Then navigate to the `final_url` from the JSON output.
- **/sentry/** in the URL → user needs `mwinit -s` for Sentry cookie

## Rules

- **ALWAYS take a screenshot** after navigation and significant actions — user can't see the browser
- **NEVER use `browser_evaluate` with `window.location`** for navigation — use `browser_navigate`
- If on ARM AL2 (aarch64) and Playwright install fails: fall back to `ReadInternalWebsites` MCP tool
- Playwright browser runs headless — `--auth-server-allowlist` is NOT available. Auth is cookie-based only.

## Prerequisites

- `kinit -f` (Kerberos ticket for federate flow)
- `mwinit -o -s` (Midway + Sentry cookies)
- `kirocrew browse setup` (one-time Playwright install)
```

- [ ] **Step 2: Commit**

```bash
git add src/kiro_crew/skills/browser-auth.md
git commit -m "feat(browser): add browser-auth skill for Playwright MCP guidance"
```

---

### Task 6: Update System Prompt

**Files:**
- Modify: `src/kiro_crew/config/prompt.md`

- [ ] **Step 1: Rewrite the browser section**

Replace the entire `kirocrew browse` section (lines ~123-182) with:

```markdown
## Browser (Playwright MCP)

You have access to Playwright MCP tools for browsing Amazon internal websites. Use them when the message contains the `[BROWSE]` marker (injected when user clicks the 🌐 button).

**IMPORTANT: You may ONLY browse when the message contains `[BROWSE]`.** Without this marker, use `ReadInternalWebsites` instead.

**How it works:** Playwright runs a headless Chromium instance. Amazon auth (Midway cookies, Kerberos, Sentry) must be injected manually since headless mode has no `--auth-server-allowlist`.

### Quick Start

1. Validate auth: `kirocrew browse auth health`
2. If unhealthy, tell user what to run (`kinit -f`, `mwinit -o`)
3. Get cookies: `kirocrew browse auth inject` → inject via `browser_evaluate`
4. Navigate: `browser_navigate` → `browser_take_screenshot` → show user
5. Interact: `browser_click`, `browser_fill_form`, `browser_snapshot`

### Auth Gate Recovery

- **midway-auth redirect** → re-inject cookies
- **federate redirect** → `kirocrew browse auth federate "<url>"` → navigate to `final_url`
- **sentry redirect** → user needs `mwinit -s`

### Rules

- **ALWAYS screenshot** after navigation/interaction — user can't see the browser
- **NEVER use `browser_evaluate('window.location = ...')`** — use `browser_navigate`
- On aarch64 AL2 where Playwright can't install: fall back to `ReadInternalWebsites`
```

- [ ] **Step 2: Commit**

```bash
git add src/kiro_crew/config/prompt.md
git commit -m "docs(browser): update system prompt for Playwright MCP browsing"
```

---

### Task 7: Update Setup Flow

**Files:**
- Modify: `src/kiro_crew/cli_setup.py`

- [ ] **Step 1: Replace browser setup section**

Replace the browser section (lines ~359-373) with:

```python
    # ── Browser (Playwright) ──
    print("\n── Browser (Playwright) ──")
    from kiro_crew.browser.setup import is_playwright_installed, ensure_playwright_installed

    if is_playwright_installed():
        print("  ✅ Playwright MCP installed")
    else:
        print("  Installing Playwright MCP and Chromium...")
        try:
            ensure_playwright_installed()
            print("  ✅ Playwright installed")
        except Exception as exc:
            print(f"  ⚠️  Playwright install failed: {exc}")
            print("  Browser features will use ReadInternalWebsites as fallback")
```

- [ ] **Step 2: Commit**

```bash
git add src/kiro_crew/cli_setup.py
git commit -m "feat(setup): install Playwright MCP during kirocrew setup"
```

---

### Task 8: Add Playwright MCP to Default Config

**Files:**
- Modify: `src/kiro_crew/config/defaults.json`

- [ ] **Step 1: Add Playwright MCP server entry**

Add to `mcpServers` in defaults.json (or add the key if it doesn't exist):

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp"]
    }
  }
}
```

This ensures Playwright MCP is available to all KiroCrew agents by default.

- [ ] **Step 2: Commit**

```bash
git add src/kiro_crew/config/defaults.json
git commit -m "feat(config): add Playwright MCP as default server"
```

---

### Task 9: Clean Up auth.py — Remove CDP Dependencies

**Files:**
- Modify: `src/kiro_crew/browser/auth.py`
- Modify: `test/test_browser_auth.py`

- [ ] **Step 1: Remove CDP-dependent functions from auth.py**

Remove these functions (they use CDPClient which is deleted):
- `inject_cookies(cdp, cookies)` 
- `inject_from_file(cdp, path)`
- `inject_mcs(cdp, targets)`
- `get_midway_token(site_url)`
- The import: `from kiro_crew.browser.cdp import CDPClient, CDPError`

Keep everything else (health, ensure, federate_auth, parse_netscape_cookies, etc.)

- [ ] **Step 2: Update test_browser_auth.py**

Remove test classes that test CDP injection:
- `TestInjectCookies` (all methods)
- Any test that mocks `CDPClient`

Keep:
- `TestParseNetscapeCookies`
- `TestHealth`
- `TestEnsure`
- `TestKrb5Env`
- `TestFederateAuth`

- [ ] **Step 3: Run tests**

```bash
python -m pytest test/test_browser_auth.py -v
```
Expected: All remaining tests PASS

- [ ] **Step 4: Commit**

```bash
git add src/kiro_crew/browser/auth.py test/test_browser_auth.py
git commit -m "refactor(auth): remove CDP injection — cookies go through Playwright"
```

---

### Task 10: Remove messaging.py Browser Handler

**Files:**
- Modify: `src/kiro_crew/dashboard/handlers/messaging.py`

- [ ] **Step 1: Remove or rewrite api_browser_auth_retry**

The `api_browser_auth_retry` function imports `_get_session` from the deleted `mcp_browser.py`. Either:
- Remove it entirely (simplest — the skill handles auth retry via CLI)
- Or rewrite to subprocess `kirocrew browse auth inject`

Simplest approach — remove the route and handler:

```python
# Remove the function api_browser_auth_retry entirely
# Remove any route registration for /api/browser-auth-retry
```

- [ ] **Step 2: Remove route registration**

Find where `api_browser_auth_retry` is registered as a route and remove that line.

- [ ] **Step 3: Verify no remaining imports of deleted modules**

```bash
grep -rn "from kiro_crew.mcp_browser\|from kiro_crew.browser.cdp\|from kiro_crew.browser.session\|from kiro_crew.browser.snapshot\|from kiro_crew.browser.chrome" src/ --include="*.py" | grep -v __pycache__
```
Expected: No matches (or only auth.py internal references)

- [ ] **Step 4: Commit**

```bash
git add src/kiro_crew/dashboard/handlers/messaging.py
git commit -m "refactor(dashboard): remove browser auth retry handler — handled by skill"
```

---

### Task 11: Update __init__.py and Verify

**Files:**
- Modify: `src/kiro_crew/browser/__init__.py`

- [ ] **Step 1: Update module docstring**

```python
"""KiroCrew browser auth — Midway/Kerberos/Federate SSO for Playwright MCP."""
```

- [ ] **Step 2: Full test suite run**

```bash
python -m pytest test/ -x --tb=short
```

Verify no import errors, no failures related to deleted modules.

- [ ] **Step 3: Flake8 check**

```bash
python -m flake8 src/kiro_crew/browser/ --max-line-length=120
```

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore(browser): final cleanup — verify all tests pass after rewrite"
```

---

## Summary of Changes

| Metric | Before | After |
|--------|--------|-------|
| Source lines (browser/) | 2,562 | ~580 |
| Source lines (mcp_browser.py) | 689 | 0 (deleted) |
| Test lines | 3,672 | ~700 |
| Total files | 8 source + 7 test | 4 source + 2 test |
| External deps | None (raw CDP) | Playwright MCP (npm) |

**What agents gain:** Battle-tested Playwright interaction, better reliability, tab management, form handling, file uploads, proper wait-for conditions — all maintained by the Playwright team, not us.

**What we maintain:** Only the Amazon auth layer (~300 lines) that no external tool can provide.
