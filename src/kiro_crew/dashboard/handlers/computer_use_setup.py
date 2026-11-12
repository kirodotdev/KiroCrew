"""Computer-use setup REST API: the document behind Settings -> Computer Use setup.

``GET  /api/computer-use/setup``  browser-called, cookie-authed, owner only
``POST /api/computer-use/setup``  browser-called, cookie-authed, owner only, CSRF

Both return the same setup document (state, platform, driver, session,
permissions, capabilities, limitations, steps, user_action, progress). The work
behind them lives in ``kiro_crew.computer_use.setup``: ``get_setup_status()`` and
``run_setup_action(action)``. This module only gates the caller, bounds the time a
request may take, and shapes what goes back to the browser.

Nothing here turns the feature on. The primary enable stays the keystone written by
``PUT /api/computer-use/config``, and ``ready`` needs both that enable and a passed
verify, which the setup module decides.

Three properties the handler owns, because the setup module cannot:

* **Bounded.** Each call runs on the bounded subprocess pool (the driver can
  block on a wedged desktop) under a per-action deadline. When the deadline
  passes the response is the current status instead, so the panel moves to
  the state the slow action left behind (``installing``) and polls from there.
  A thread cannot be cancelled, so a timed-out action keeps running; a second
  POST while it runs does not start another one.
* **Never raises.** Any failure, including a missing or broken setup module,
  becomes a well-formed document in the ``error`` (or ``temporarily_unavailable``)
  state.
* **Shaped.** Only the documented keys and value types leave this handler, and
  every string is length-capped. A raw driver error, a path or a token that the
  setup module put in an undocumented key is dropped rather than shown.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import logging
import threading
from typing import Any

from aiohttp import web

from kiro_crew.executors import subprocess_executor

logger = logging.getLogger(__name__)

SETUP_MODULE = "kiro_crew.computer_use.setup"

OP_SETUP_READ = "computer_use.setup_read"
OP_SETUP_ACTION = "computer_use.setup_action"

#: Wire states, in the order the setup contract lists them.
SETUP_STATES: tuple[str, ...] = (
    "disabled",
    "not_configured",
    "installing",
    "install_failed",
    "requesting_accessibility",
    "accessibility_denied",
    "requesting_screen_recording",
    "screen_recording_denied",
    "waiting_for_system_settings",
    "restart_required",
    "validating",
    "ready",
    "temporarily_unavailable",
    "permission_revoked",
    "unsupported",
    "error",
)
_STATE_SET = frozenset(SETUP_STATES)

SETUP_ACTIONS: tuple[str, ...] = (
    "install",
    "recheck",
    "verify",
    "restart_driver",
    "reset",
    "use_builtin_driver",
)

#: Seconds a status read may take. A status read asks the driver for its
#: permission and health report; slower than this means it is wedged, and the
#: panel must still render.
STATUS_TIMEOUT_SECS = 8.0

#: Seconds each action may hold the request. ``install`` downloads and verifies
#: the release archive, which can take longer than this on a slow link: the
#: response is then the current status (``installing``), and the panel polls.
#: Actions that rewrite the keystone and so run under the shared config lock.
_KEYSTONE_ACTIONS = frozenset({"use_builtin_driver"})

ACTION_TIMEOUT_SECS: dict[str, float] = {
    "install": 30.0,
    "recheck": 10.0,
    "verify": 25.0,
    "restart_driver": 20.0,
    "reset": 10.0,
    "use_builtin_driver": 10.0,
}

_PLATFORMS = frozenset({"macos", "windows", "linux", "unknown"})
_DRIVER_KINDS = frozenset({"upstream", "native", "none"})
_SESSION_KINDS = frozenset({"x11", "wayland", "windows_interactive", "session0", "aqua", "none"})
_PERMISSION_VALUES = frozenset({"granted", "missing", "unknown", "not_applicable"})
_STEP_IDS = frozenset(
    {"install", "session", "accessibility", "screen_recording", "verify", "ready"}
)
_STEP_STATUSES = frozenset({"done", "current", "pending", "failed", "not_applicable"})
_ACTION_KINDS = frozenset({"open_pane", "post"})
_PANES = frozenset({"accessibility", "screen_recording"})
#: ``setup.FAILURE_STATES``: the states that always carry a ``problem``.
_FAILURE_STATES = frozenset(
    {
        "install_failed",
        "accessibility_denied",
        "screen_recording_denied",
        "temporarily_unavailable",
        "permission_revoked",
        "unsupported",
        "error",
    }
)
#: ``setup.PROBLEM_CODES``, restated so this module stays importable on its own.
_PROBLEM_CODES = frozenset(
    {
        "no_native_driver",
        "unsupported_platform",
        "no_desktop_session",
        "unsupported_session",
        "install_failed",
        "install_blocked",
        "launch_blocked",
        "crash_loop",
        "driver_check_failed",
        "verify_failed",
        "permission_denied",
        "permission_revoked",
        "status_unreadable",
        "keystone_unreadable",
        "action_failed",
    }
)
_CAPABILITY_KEYS = (
    "observe_tree",
    "screenshot",
    "background_input",
    "foreground_input",
    "window_capture",
)

MAX_TEXT = 400
MAX_SHORT = 64
MAX_LIMITATIONS = 12
MAX_STEPS = 8

# One action at a time. A thread cannot be cancelled, so an action that outlived
# its deadline is still in this slot until it finishes; a POST that arrives in
# the meantime reads status instead of starting a second install.
_inflight_lock = threading.Lock()
_inflight: concurrent.futures.Future[Any] | None = None


def _text(value: object, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split())
    return cleaned[:limit]


def _token(value: object, allowed: frozenset[str], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def _bool(value: object) -> bool:
    return value is True


def _fallback(state: str, user_action: str) -> dict[str, Any]:
    """A complete document for a failure the setup module could not describe."""
    return shape_document({"state": state, "user_action": user_action})


def _problem(value: object, state: str, user_action: str) -> "dict[str, Any] | None":
    """``{code, message, recoverable}`` for a failure, else ``None``.

    The document's failure reason lives here and nowhere else (``limitations``
    holds standing platform limits only). A failure state that arrives without a
    problem still gets one, so the panel always has an error to show.
    """
    if isinstance(value, dict):
        return {
            "code": _token(value.get("code"), _PROBLEM_CODES, "driver_check_failed"),
            "message": _text(value.get("message")) or user_action,
            "recoverable": value.get("recoverable") is not False,
        }
    if state in _FAILURE_STATES:
        return {"code": "driver_check_failed", "message": user_action, "recoverable": True}
    return None


def _sub(doc: dict[str, Any], key: str) -> dict[str, Any]:
    value = doc.get(key)
    return value if isinstance(value, dict) else {}


def shape_document(raw: object) -> dict[str, Any]:
    """Reduce whatever the setup module returned to the documented shape.

    Unknown keys are dropped, enumerations outside their vocabulary fall back to a
    neutral value, and strings are whitespace-collapsed and capped. An unknown
    ``state`` becomes ``error``: the panel renders every documented state
    specifically, and an invented one would otherwise read as nothing at all.
    """
    doc: dict[str, Any] = raw if isinstance(raw, dict) else {}
    driver = _sub(doc, "driver")
    session = _sub(doc, "session")
    perms = _sub(doc, "permissions")
    caps = _sub(doc, "capabilities")
    progress = _sub(doc, "progress")

    steps: list[dict[str, Any]] = []
    raw_steps = doc.get("steps")
    for step in raw_steps[:MAX_STEPS] if isinstance(raw_steps, list) else []:
        if not isinstance(step, dict) or step.get("id") not in _STEP_IDS:
            continue
        shaped: dict[str, Any] = {
            "id": step["id"],
            "status": _token(step.get("status"), _STEP_STATUSES, "pending"),
        }
        action = step.get("action")
        if isinstance(action, dict) and action.get("kind") in _ACTION_KINDS:
            if action["kind"] == "open_pane" and action.get("pane") in _PANES:
                shaped["action"] = {"kind": "open_pane", "pane": action["pane"]}
            elif action["kind"] == "post" and action.get("action") in SETUP_ACTIONS:
                shaped["action"] = {"kind": "post", "action": action["action"]}
        steps.append(shaped)

    limitations_raw = doc.get("limitations")
    limitations = [
        text
        for text in (
            _text(item) for item in (limitations_raw if isinstance(limitations_raw, list) else [])
        )
        if text
    ][:MAX_LIMITATIONS]

    return {
        "state": _token(doc.get("state"), _STATE_SET, "error"),
        "platform": _token(doc.get("platform"), _PLATFORMS, "unknown"),
        "driver": {
            "kind": _token(driver.get("kind"), _DRIVER_KINDS, "none"),
            "version": _text(driver.get("version"), MAX_SHORT),
            "installed": _bool(driver.get("installed")),
            "healthy": _bool(driver.get("healthy")),
        },
        "session": {
            "kind": _token(session.get("kind"), _SESSION_KINDS, "none"),
            "detail": _text(session.get("detail")),
        },
        "permissions": {
            "accessibility": _token(perms.get("accessibility"), _PERMISSION_VALUES, "unknown"),
            "screen_recording": _token(
                perms.get("screen_recording"), _PERMISSION_VALUES, "unknown"
            ),
            "owner_name": _text(perms.get("owner_name"), MAX_SHORT),
        },
        "capabilities": {key: _bool(caps.get(key)) for key in _CAPABILITY_KEYS},
        "limitations": limitations,
        "steps": steps,
        "user_action": _text(doc.get("user_action")),
        "problem": _problem(
            doc.get("problem"),
            _token(doc.get("state"), _STATE_SET, "error"),
            _text(doc.get("user_action")),
        ),
        "progress": {"last_step": _token(progress.get("last_step"), _STEP_IDS, "")},
        # How the system stopped the upstream driver at launch ("" when it did not).
        "blocked": _text(doc.get("blocked"), MAX_SHORT),
    }


def _setup_module() -> Any:
    return importlib.import_module(SETUP_MODULE)


def _status_sync() -> object:
    return _setup_module().get_setup_status()


def _action_sync(action: str) -> object:
    return _setup_module().run_setup_action(action)


async def _await_bounded(future: concurrent.futures.Future[Any], timeout: float) -> object:
    """Await a pool future for at most ``timeout`` seconds without cancelling it.

    ``shield`` keeps the timeout from cancelling the wrapper: a thread cannot be
    stopped, and a cancelled wrapper would only hide that it is still running.
    """
    return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout)


async def read_status() -> dict[str, Any]:
    """The current setup document. Never raises."""
    try:
        future = subprocess_executor().submit(_status_sync)
        return shape_document(await _await_bounded(future, STATUS_TIMEOUT_SECS))
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("computer-use setup status timed out after %ss", STATUS_TIMEOUT_SECS)
        return _fallback(
            "temporarily_unavailable",
            "The computer-use driver did not answer in time. Check again in a moment.",
        )
    except Exception:
        logger.warning("computer-use setup status failed", exc_info=True)
        return _fallback("error", "Kiro Crew could not read the computer-use setup status.")


def _start_action(action: str) -> concurrent.futures.Future[Any] | None:
    """Submit ``action`` unless another one is still running; ``None`` when busy."""
    global _inflight
    with _inflight_lock:
        if _inflight is not None and not _inflight.done():
            return None
        _inflight = subprocess_executor().submit(_action_sync, action)
        return _inflight


async def run_action(action: str) -> dict[str, Any]:
    """Run one setup action under its deadline and return the resulting document.

    Never raises. Past the deadline, or while an earlier action is still running,
    the answer is the current status, which names the state that action is in.
    """
    if action in _KEYSTONE_ACTIONS:
        # It rewrites ``computer_use.json``: serialize with every other keystone
        # writer (the config PUT, pending-edit apply/discard) and hold the lock
        # until the worker finishes, so a concurrent "turn off" is never undone.
        from kiro_crew.dashboard.handlers.agents import _get_config_lock

        async with _get_config_lock():
            try:
                keystone_future = _start_action(action)
            except Exception:
                logger.warning(
                    "computer-use setup action %s could not start", action, exc_info=True
                )
                return _fallback("error", "Kiro Crew could not start that setup step.")
            if keystone_future is None:
                return await read_status()
            try:
                return shape_document(await asyncio.wrap_future(keystone_future))
            except Exception:
                logger.warning("computer-use setup action %s failed", action, exc_info=True)
                return _fallback(
                    "error", "That setup step did not finish. Check again, or reset setup."
                )
    try:
        future = _start_action(action)
    except Exception:
        logger.warning("computer-use setup action %s could not start", action, exc_info=True)
        return _fallback("error", "Kiro Crew could not start that setup step.")
    if future is None:
        return await read_status()
    try:
        return shape_document(await _await_bounded(future, ACTION_TIMEOUT_SECS[action]))
    except (asyncio.TimeoutError, TimeoutError):
        logger.info("computer-use setup action %s still running after its deadline", action)
        return await read_status()
    except Exception:
        logger.warning("computer-use setup action %s failed", action, exc_info=True)
        return _fallback("error", "That setup step did not finish. Check again, or reset setup.")


def _audit(request: web.Request, operation: str, outcome: str, resources: str = "") -> None:
    # Late import: ``computer_use`` resolves the SEL singleton through the handler
    # package, which imports this module.
    from kiro_crew.dashboard.handlers import computer_use

    computer_use._audit(request, operation=operation, outcome=outcome, resources=resources)


async def _gate(request: web.Request, operation: str) -> web.Response | None:
    """Refuse an app token, then anyone who is not the dashboard owner.

    Same order and answers as ``PUT /api/computer-use/config``: an app token keeps
    its own ``dashboard_user_required`` code, and a dashboard user who is not the
    owner gets the shared ``owner_only`` refusal.
    """
    if request.get("app"):
        _audit(request, operation, "denied", "app_token")
        return web.json_response(
            {"error": "dashboard user required", "code": "dashboard_user_required"},
            status=403,
        )
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    return await require_owner_dashboard_request(request, operation)


async def api_computer_use_setup_get(request: web.Request) -> web.Response:
    """GET /api/computer-use/setup: the current setup document."""
    denied = await _gate(request, OP_SETUP_READ)
    if denied is not None:
        return denied
    return web.json_response(await read_status())


async def api_computer_use_setup_post(request: web.Request) -> web.Response:
    """POST /api/computer-use/setup ``{"action": ...}``: run one step, return the document.

    ``400`` for a malformed body or an action outside :data:`SETUP_ACTIONS`;
    ``403`` for an app token or a non-owner. Every other outcome is ``200`` with a
    document, including a failed or slow step, because the document is what tells
    the panel what to show next.
    """
    denied = await _gate(request, OP_SETUP_ACTION)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        _audit(request, OP_SETUP_ACTION, "denied", "invalid_json")
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    action = body.get("action") if isinstance(body, dict) else None
    if not isinstance(action, str) or action not in SETUP_ACTIONS:
        _audit(request, OP_SETUP_ACTION, "denied", "unknown_action")
        return web.json_response(
            {"error": "unknown setup action", "code": "unknown_action"},
            status=400,
        )
    document = await run_action(action)
    _audit(request, OP_SETUP_ACTION, "allowed", f"action={action} state={document['state']}")
    return web.json_response(document)
