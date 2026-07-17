"""Browser authentication helpers for Playwright MCP browsing (OSS stub).

In the open-source build there is no bundled enterprise SSO integration, so the
SSO/cookie-refresh helpers below are inert stubs that report "not available in
OSS". The generic Netscape cookie-jar parser remains fully functional for any
browser cookie file a user wants to inject into Playwright.

All public symbols are preserved so that ``browser/setup.py``, ``browser/cli.py``
and the dashboard handlers continue to import and call them safely.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Path of a Netscape cookie jar, if the user maintains one. Kept generic; the
# default location matches the historic name but nothing requires it to exist.
MIDWAY_COOKIE_PATH = Path.home() / ".midway" / "cookie"

_NOT_AVAILABLE = {"available": False, "reason": "not available in OSS"}


def cookie_path() -> Path:
    return MIDWAY_COOKIE_PATH


def parse_netscape_cookies(path: Path) -> list[dict[str, Any]]:
    """Parse a Netscape/Mozilla cookie jar into Playwright-style cookie dicts."""
    if not path.exists():
        return []
    cookies: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        is_secure = parts[3].upper() == "TRUE"
        cookie: dict[str, Any] = {
            "name": parts[5],
            "value": parts[6],
            "domain": parts[0],
            "path": parts[2],
            "secure": is_secure,
            "httpOnly": http_only,
            "sameSite": "None" if is_secure else "Lax",
        }
        expires = parts[4]
        cookie["expires"] = int(expires) if expires.isdigit() and int(expires) > 0 else -1
        cookies.append(cookie)
    return cookies


def has_mcscli() -> bool:
    """No enterprise credential helper in the OSS build."""
    return False


def mcs_keys_process_running() -> bool:
    """No enterprise credential helper in the OSS build."""
    return False


def refresh_cookie_via_mcs() -> bool:
    """Cookie refresh via an enterprise helper is not available in OSS."""
    return False


def refresh_aea() -> bool:
    """Device-posture refresh is not available in OSS."""
    return False


def has_kerberos_ticket() -> bool:
    """Kerberos/SPNEGO is not wired up in the OSS build."""
    return False


def health() -> dict[str, Any]:
    """Report auth health. The OSS build ships no bundled SSO integration."""
    return dict(_NOT_AVAILABLE)


def ensure() -> dict[str, Any]:
    """One-stop auth check. No-op in OSS: nothing to refresh."""
    return dict(_NOT_AVAILABLE)


def federate_auth(target_url: str) -> dict[str, Any]:
    """Enterprise federate SSO flow — not available in the OSS build.

    Returns a result dict with ``ok=False`` so callers degrade gracefully and
    simply navigate without injected SSO cookies.
    """
    return {"ok": False, "error": "not available in OSS", "cookies": []}
