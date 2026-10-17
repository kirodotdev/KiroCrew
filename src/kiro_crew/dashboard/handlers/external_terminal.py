"""The gateway gate for opening the user's own terminal app in a project directory.

The desktop shell can start an allowlisted terminal emulator (kitty, WezTerm, …)
in a session's project directory. The shell does the spawning, because the window
must open on the machine in front of the user, but it does so only on this
module's approval:

``POST /api/terminal/external``
    The dashboard asks to open ``cwd``. Refused unless the caller is the dashboard
    owner (the auth the integrated terminal requires, with that terminal enabled),
    the request is direct-local (loopback with no forwarding headers, the rule the
    channel-credential settings use), and the user has picked an app in
    ``dashboard.terminal.external_app``, which is unset by default. The directory is
    resolved with ``realpath`` and must be an existing directory. The answer is a
    single-use grant naming the app and the resolved directory.

``POST /api/terminal/external-grant``
    The desktop shell's main process redeems a grant. Machine-only: loopback plus
    a verified ``X-Internal-Secret``, the channel the native browser panel drains
    on. A grant is honoured once, within :data:`GRANT_TTL_SECS`, and only while the
    picked app is still the one it names.

The renderer only ever holds the opaque grant, so neither the binary nor any argv
comes from a request: the app id is read from config and checked against
:data:`EXTERNAL_TERMINAL_APPS`, and the shell resolves that id to a fixed install
path from its own copy of the same allowlist.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import threading
import time

from aiohttp import web

from kiro_crew.config.sections import EXTERNAL_TERMINAL_APPS
from kiro_crew.dashboard.handlers import terminal
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.origin import is_direct_local_request
from kiro_crew.dashboard.urls import is_loopback

logger = logging.getLogger(__name__)

__all__ = [
    "EXTERNAL_TERMINAL_APPS",
    "GRANT_TTL_SECS",
    "api_terminal_external",
    "api_terminal_external_grant",
    "clear_grants",
    "configured_app",
    "resolve_directory",
]

#: How long a grant stays redeemable. The shell redeems it as soon as the
#: renderer hands it over, so this only bounds a grant nobody redeemed.
GRANT_TTL_SECS = 30.0

#: Outstanding grants kept at most; the oldest is dropped past this.
_MAX_GRANTS = 16

# grant -> (app id, resolved directory, monotonic expiry). Guarded by a thread
# lock because the directory check runs off the event loop.
_grants: dict[str, tuple[str, str, float]] = {}
_grants_lock = threading.Lock()


def configured_app(cfg: dict) -> str:
    """The picked app id from a ``dashboard.terminal`` object, or ``""``.

    Any value that is not an allowlisted id, including a non-string, reads as
    unset, so a hand-edited typo can never name a program.
    """
    value = cfg.get("external_app") if isinstance(cfg, dict) else None
    return value if isinstance(value, str) and value in EXTERNAL_TERMINAL_APPS else ""


def resolve_directory(cwd: object) -> str | None:
    """The realpath of ``cwd`` when it is an existing directory, else ``None``.

    A relative path is refused rather than resolved against the gateway's own
    working directory.
    """
    if not isinstance(cwd, str) or not cwd or "\x00" in cwd or not os.path.isabs(cwd):
        return None
    try:
        real = os.path.realpath(cwd, strict=True)
    except (OSError, ValueError):
        return None
    return real if os.path.isdir(real) else None


def _mint_grant(app: str, cwd: str) -> str:
    grant = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _grants_lock:
        for key in [k for k, (_a, _c, exp) in _grants.items() if exp <= now]:
            del _grants[key]
        while len(_grants) >= _MAX_GRANTS:
            del _grants[next(iter(_grants))]
        _grants[grant] = (app, cwd, now + GRANT_TTL_SECS)
    return grant


def _take_grant(grant: str) -> tuple[str, str] | None:
    with _grants_lock:
        entry = _grants.pop(grant, None)
    if entry is None:
        return None
    app, cwd, expiry = entry
    if expiry <= time.monotonic():
        return None
    return app, cwd


def clear_grants() -> None:
    """Drop every outstanding grant (test isolation)."""
    with _grants_lock:
        _grants.clear()


def _audit(request: web.Request, operation: str, outcome: str, resources: str) -> None:
    try:
        terminal._sel().log_api_access(
            caller=str(request.get("user") or "unknown"),
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


def _refuse(request: web.Request, status: int, code: str, error: str) -> web.Response:
    _audit(request, "terminal.external.open", "denied", code)
    return web.json_response({"error": error, "code": code}, status=status)


async def api_terminal_external(request: web.Request) -> web.Response:
    """POST /api/terminal/external — approve opening the picked terminal app.

    Body: ``{"cwd": "<absolute project directory>"}``. Answers
    ``{"grant": "…", "app": "<id>"}``, or a refusal whose ``code`` names the
    failed check. The checks run in this order and every one is required.
    """
    if not request.get("user"):
        _audit(request, "terminal.external.open", "denied", "unauthenticated")
        return web.Response(status=401, text="Unauthorized")
    owner_denied = await require_owner_dashboard_request(request, "terminal.external.open")
    if owner_denied is not None:
        return owner_denied
    if not terminal._is_enabled(request):
        return _refuse(request, 403, "terminal_disabled", "Terminal panel disabled")
    if not is_direct_local_request(request):
        return _refuse(
            request,
            403,
            "external_terminal_not_local",
            "an external terminal opens only from the machine running the gateway",
        )
    app = configured_app(await asyncio.to_thread(terminal._get_config, request))
    if not app:
        return _refuse(
            request,
            409,
            "external_terminal_not_configured",
            "no external terminal app is picked in Settings",
        )
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    cwd = body.get("cwd") if isinstance(body, dict) else None
    real = await asyncio.to_thread(resolve_directory, cwd)
    if real is None:
        return _refuse(
            request, 400, "external_terminal_bad_cwd", "the project directory does not exist"
        )
    grant = _mint_grant(app, real)
    _audit(request, "terminal.external.open", "ok", f"app={app}")
    return web.json_response({"grant": grant, "app": app})


async def api_terminal_external_grant(request: web.Request) -> web.Response:
    """POST /api/terminal/external-grant — the desktop shell redeems a grant.

    Body: ``{"grant": "…"}``. Answers ``{"app": "<id>", "cwd": "<realpath>"}``
    once per grant. Refused without a verified internal secret on loopback, for
    an unknown, spent or expired grant, and when the picked app has changed or
    been cleared since the grant was minted. Every outcome is audited, naming the
    refusal code or the app and never the directory.
    """

    def refuse(status: int, code: str, error: str) -> web.Response:
        _audit(request, "terminal.external.redeem", "denied", code)
        return web.json_response({"error": error, "code": code}, status=status)

    if request.get("internal_auth") is not True or not is_loopback(request.remote or ""):
        return refuse(403, "internal_secret_required", "forbidden")
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    grant = body.get("grant") if isinstance(body, dict) else None
    entry = _take_grant(grant) if isinstance(grant, str) else None
    if entry is None:
        return refuse(404, "external_terminal_no_grant", "no such grant")
    app, cwd = entry
    if (
        not terminal._is_enabled(request)
        or configured_app(await asyncio.to_thread(terminal._get_config, request)) != app
    ):
        return refuse(409, "external_terminal_not_configured", "the picked app changed")
    _audit(request, "terminal.external.redeem", "ok", f"app={app}")
    return web.json_response({"app": app, "cwd": cwd})
