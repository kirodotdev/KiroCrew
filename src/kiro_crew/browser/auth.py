"""Browser authentication helpers for Playwright MCP browsing.

In the open-source build there is no bundled enterprise SSO integration, so the
SSO/cookie-refresh helpers below are inert stubs that report "not available in
OSS". The generic Netscape cookie-jar parser remains fully functional for any
browser cookie file a user wants to inject into Playwright.

All public symbols are preserved so that ``browser/setup.py``, ``browser/cli.py``
and the dashboard handlers continue to import and call them safely.

**Edition seam.** A companion edition supplies its real enterprise SSO/cookie
flow by registering a provider via
:func:`register_browser_auth_provider` at boot — the structural twin of
``register_acp_backends`` / ``register_publish_providers`` /
``embeddings.register_embedding_backend``. Every helper below delegates to the
registered provider when one is present and otherwise returns today's OSS
"not available" default, so the standalone build is byte-identical. A provider
only needs to implement the subset of methods it supports; a missing method
falls back to the OSS default for that helper (``getattr`` duck-typing keeps the
companion free of an import-inherit dependency on this module).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Path of a Netscape cookie jar, if the user maintains one. Kept generic; the
# default location is under the KiroCrew home and nothing requires it to exist.
SSO_COOKIE_PATH = Path.home() / ".kirocrew" / "browser-cookies.txt"

_NOT_AVAILABLE = {"available": False, "reason": "not available in OSS"}


@runtime_checkable
class BrowserAuthProvider(Protocol):
    """The edition-supplied browser-auth flow (all methods optional).

    A companion registers an instance via :func:`register_browser_auth_provider`.
    Each method mirrors the module-level helper of the same name; a provider that
    omits a method leaves that helper at its OSS default. Structural (``Protocol``)
    so the companion need not import-inherit this class.
    """

    def cookie_path(self) -> Path: ...

    def has_sso_helper(self) -> bool: ...

    def sso_keys_process_running(self) -> bool: ...

    def refresh_cookie_via_sso(self) -> bool: ...

    def refresh_device_posture(self) -> bool: ...

    def has_sso_ticket(self) -> bool: ...

    def health(self) -> dict[str, Any]: ...

    def ensure(self) -> dict[str, Any]: ...

    def federated_login(self, target_url: str) -> dict[str, Any]: ...


# Module-level provider slot. ``None`` → the OSS stubs below apply unchanged.
_provider: Optional[BrowserAuthProvider] = None


def register_browser_auth_provider(provider: Optional[BrowserAuthProvider]) -> None:
    """Register (or clear, with ``None``) the edition browser-auth provider.

    Called once from an edition's boot composition with a hardcoded provider
    instance — never from an agent-reachable path. Idempotent; a later call
    replaces the previous provider (tests pass ``None`` to reset).
    """
    global _provider
    _provider = provider


def _delegate(method: str, default: Callable[[], Any]) -> Any:
    """Call ``provider.<method>()`` if a provider supplies it, else ``default()``.

    A provider exception is logged and degrades to the OSS default rather than
    propagating — a broken edition helper must not crash a browse/dashboard path.
    """
    prov = _provider
    if prov is not None:
        fn = getattr(prov, method, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                logger.warning("browser-auth provider %s() failed; using default", method,
                               exc_info=True)
    return default()


def cookie_path() -> Path:
    return _delegate("cookie_path", lambda: SSO_COOKIE_PATH)


def parse_netscape_cookies(path: Path) -> list[dict[str, Any]]:
    """Parse a Netscape/Mozilla cookie jar into Playwright-style cookie dicts."""
    if not path.exists():
        return []
    cookies: list[dict[str, Any]] = []
    # errors="replace": a single non-UTF-8 byte anywhere in the jar would
    # otherwise raise UnicodeDecodeError and abort parsing of ALL cookies.
    # Degrade the bad byte to U+FFFD on its line instead.
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
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
        # str.isdigit() is True for unicode digits ("²", "٣") that int()
        # rejects, so the old `isdigit() and int(...)` gate still raised
        # ValueError on those. Guard the coercion and degrade a malformed or
        # non-positive expiry to a session cookie (-1).
        try:
            parsed_exp = int(parts[4])
        except ValueError:
            parsed_exp = -1
        cookie["expires"] = parsed_exp if parsed_exp > 0 else -1
        cookies.append(cookie)
    return cookies


def has_sso_helper() -> bool:
    """No enterprise credential helper in the OSS build (edition may override)."""
    return _delegate("has_sso_helper", lambda: False)


def sso_keys_process_running() -> bool:
    """No enterprise credential helper in the OSS build (edition may override)."""
    return _delegate("sso_keys_process_running", lambda: False)


def refresh_cookie_via_sso() -> bool:
    """Cookie refresh via an enterprise helper (edition may override)."""
    return _delegate("refresh_cookie_via_sso", lambda: False)


def refresh_device_posture() -> bool:
    """Device-posture refresh (edition may override)."""
    return _delegate("refresh_device_posture", lambda: False)


def has_sso_ticket() -> bool:
    """SSO/SPNEGO ticket presence (edition may override)."""
    return _delegate("has_sso_ticket", lambda: False)


def health() -> dict[str, Any]:
    """Report auth health. OSS default: no bundled SSO (edition may override)."""
    return _delegate("health", lambda: dict(_NOT_AVAILABLE))


def ensure() -> dict[str, Any]:
    """One-stop auth check. OSS default: nothing to refresh (edition may override)."""
    return _delegate("ensure", lambda: dict(_NOT_AVAILABLE))


def federated_login(target_url: str) -> dict[str, Any]:
    """Enterprise federated SSO flow (edition may override).

    OSS default returns ``ok=False`` so callers degrade gracefully and simply
    navigate without injected SSO cookies. When a companion provider is present
    its ``federated_login(target_url)`` is called with the target URL.
    """
    prov = _provider
    if prov is not None:
        fn = getattr(prov, "federated_login", None)
        if callable(fn):
            try:
                return fn(target_url)
            except Exception:
                logger.warning("browser-auth provider federated_login() failed; using default",
                               exc_info=True)
    return {"ok": False, "error": "not available in OSS", "cookies": []}
