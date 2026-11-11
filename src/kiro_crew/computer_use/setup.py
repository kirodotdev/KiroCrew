"""Computer Use setup: one state machine over install, session, permissions and a check.

The dashboard's setup route calls exactly two functions, both blocking, bounded
and never raising:

* :func:`get_setup_status` reads state (install record, in-flight install, the
  session, persisted progress) and NEVER starts the driver;
* :func:`run_setup_action` runs one owner action (``install``,
  ``request_permissions``, ``recheck``, ``verify``, ``restart_driver``,
  ``reset``) and returns the same document.

Nothing here enables the feature. The keystone enable stays the existing
owner-only write, and ``ready`` requires BOTH the keystone and a passed
``verify`` against the binary that is installed now. ``verify`` is
non-destructive: it lists windows, reads one accessibility tree, takes one
capture into a private file that is deleted at once, and confirms the runtime
answers its health report. It never clicks or types.

Progress (what was requested when, whether a grant was ever seen, the last
check's outcome) persists in ``<data-home>/computer-use/setup-state.json`` so
closing the dialog loses nothing.

Linux and Windows have no permission step: their steps are ``install``,
``session``, ``verify``, ``ready``. macOS adds ``accessibility`` and
``screen_recording``, whose grants belong to the signed driver bundle
(``CuaDriver``), asked through the driver itself; Kiro Crew never writes the
permission database or simulates consent.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.computer_use.driver_runtime import install as install_mod
from kiro_crew.computer_use.driver_runtime import session as session_mod
from kiro_crew.computer_use.driver_runtime.pin import DRIVER_VERSION, OS_MACOS, asset_for
from kiro_crew.computer_use.types import (
    DRIVER_AUTO,
    DRIVER_CHOICES,
    DRIVER_NATIVE,
    DRIVER_UPSTREAM,
    PLATFORM_LINUX,
    PLATFORM_MACOS,
    PLATFORM_WINDOWS,
    STATE_KEY_DRIVER,
)

logger = logging.getLogger(__name__)

# ── wire vocabulary (DECISIONS D5) ──

DISABLED = "disabled"
NOT_CONFIGURED = "not_configured"
INSTALLING = "installing"
INSTALL_FAILED = "install_failed"
REQUESTING_ACCESSIBILITY = "requesting_accessibility"
ACCESSIBILITY_DENIED = "accessibility_denied"
REQUESTING_SCREEN_RECORDING = "requesting_screen_recording"
SCREEN_RECORDING_DENIED = "screen_recording_denied"
WAITING_FOR_SYSTEM_SETTINGS = "waiting_for_system_settings"
RESTART_REQUIRED = "restart_required"
VALIDATING = "validating"
READY = "ready"
TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
PERMISSION_REVOKED = "permission_revoked"
UNSUPPORTED = "unsupported"
ERROR = "error"

STATES: tuple[str, ...] = (
    DISABLED,
    NOT_CONFIGURED,
    INSTALLING,
    INSTALL_FAILED,
    REQUESTING_ACCESSIBILITY,
    ACCESSIBILITY_DENIED,
    REQUESTING_SCREEN_RECORDING,
    SCREEN_RECORDING_DENIED,
    WAITING_FOR_SYSTEM_SETTINGS,
    RESTART_REQUIRED,
    VALIDATING,
    READY,
    TEMPORARILY_UNAVAILABLE,
    PERMISSION_REVOKED,
    UNSUPPORTED,
    ERROR,
)

ACTIONS: tuple[str, ...] = (
    "install",
    "request_permissions",
    "recheck",
    "verify",
    "restart_driver",
    "reset",
)

GRANTED = "granted"
MISSING = "missing"
UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"

STEP_DONE = "done"
STEP_CURRENT = "current"
STEP_PENDING = "pending"
STEP_FAILED = "failed"
STEP_NA = "not_applicable"

PANE_ACCESSIBILITY = "accessibility"
PANE_SCREEN_RECORDING = "screen_recording"

OWNER_NAME = "CuaDriver"

#: What the macOS upstream path cannot promise yet, shown on the setup card.
MACOS_PREVIEW_LIMITS: tuple[str, ...] = (
    "the open-source driver on macOS is a preview: download, signature checks and "
    "refusals are proven, granting permissions and real control are not yet",
    "the driver app lives at /Applications/CuaDriver.app and runs one shared daemon "
    "per user; another tool that uses the same driver shares that daemon",
)
PROGRESS_FILE = "setup-state.json"
#: How long after a permission request the state stays "waiting for System Settings".
WAITING_WINDOW_SECS = 600.0

_lock = threading.Lock()
_verify_running = threading.Event()
_install_thread: "threading.Thread | None" = None


# ── context ──


def _home(home: "str | os.PathLike[str] | None") -> Path:
    if home is not None:
        return Path(home)
    from kiro_crew.config.paths import data_home

    return Path(data_home())


def _platform() -> str:
    os_id = install_mod.current_os()
    return {"linux": PLATFORM_LINUX, "macos": PLATFORM_MACOS, "windows": PLATFORM_WINDOWS}.get(
        os_id, "unknown"
    )


def _keystone() -> dict:
    from kiro_crew.computer_use import enable_state

    return enable_state.load_state()


def effective_driver(platform: str, choice: str, *, installed: bool = False) -> str:
    """``upstream`` or ``native`` for *platform* under the owner's *choice* (D3).

    Windows ``auto`` follows the installed upstream driver once the owner installed
    it, and keeps the native driver until then; macOS ``auto`` stays native.
    """
    from kiro_crew.computer_use import backend as backend_mod

    if choice == DRIVER_UPSTREAM:
        return DRIVER_UPSTREAM
    if choice == DRIVER_NATIVE:
        return DRIVER_NATIVE
    if platform == PLATFORM_LINUX:
        return DRIVER_UPSTREAM
    if platform == PLATFORM_WINDOWS and backend_mod.WINDOWS_AUTO_UPSTREAM and installed:
        return DRIVER_UPSTREAM
    return DRIVER_NATIVE


# ── persisted progress ──


def _progress_path(home: Path) -> Path:
    return install_mod.runtime_root(home) / PROGRESS_FILE


def load_progress(home: Path) -> dict[str, Any]:
    try:
        data = json.loads(_progress_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_progress(home: Path, data: dict[str, Any]) -> None:
    path = _progress_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _update_progress(home: Path, **changes: Any) -> dict[str, Any]:
    with _lock:
        data = load_progress(home)
        data.update(changes)
        save_progress(home, data)
        return data


def _dict_of(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return dict(value) if isinstance(value, dict) else {}


# ── the document ──


def _step(step_id: str, status: str, action: "dict | None" = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": step_id, "status": status}
    if action is not None:
        out["action"] = action
    return out


def _pane(pane: str) -> dict[str, str]:
    return {"kind": "open_pane", "pane": pane}


def _perm_state(progress: dict[str, Any], key: str) -> str:
    perms = progress.get("permissions")
    value = perms.get(key) if isinstance(perms, dict) else None
    return value if value in (GRANTED, MISSING) else UNKNOWN


def _ever_granted(progress: dict[str, Any], key: str) -> bool:
    seen = progress.get("ever_granted")
    return isinstance(seen, dict) and seen.get(key) is True


def _requested_at(progress: dict[str, Any], key: str) -> float:
    req = progress.get("requested")
    value = req.get(key) if isinstance(req, dict) else None
    return float(value) if isinstance(value, (int, float)) else 0.0


def _verified_for_current_install(
    progress: dict[str, Any], state: install_mod.InstallState
) -> "bool | None":
    """True / False for a check on THIS binary, ``None`` when it was never run on it."""
    verify = progress.get("verify")
    if not isinstance(verify, dict) or verify.get("binary_sha256") != state.binary_sha256:
        return None
    return verify.get("ok") is True


def build_status(
    *,
    platform: str,
    choice: str,
    keystone_enabled: bool,
    locked: bool,
    install_state: install_mod.InstallState,
    install_progress: dict[str, Any],
    session: session_mod.SessionInfo,
    progress: dict[str, Any],
    supervisor_status: "dict[str, Any] | None" = None,
    verifying: bool = False,
    has_asset: bool = True,
    native_permissions: "dict[str, str] | None" = None,
    now: "float | None" = None,
) -> dict[str, Any]:
    """The D5 document from already-read inputs. Pure; every branch is unit-tested.

    *native_permissions* is the native macOS driver's ADVISORY probe
    (``permissions.probe_dict()`` plus ``owner_name``); it is shown, never used
    to refuse, because a grant to the launching process is invisible to it.
    """
    now = time.time() if now is None else now
    kind = effective_driver(platform, choice, installed=install_state.installed)
    macos = platform == PLATFORM_MACOS
    perms = {
        "accessibility": _perm_state(progress, "accessibility") if macos else NOT_APPLICABLE,
        "screen_recording": _perm_state(progress, "screen_recording") if macos else NOT_APPLICABLE,
        "owner_name": OWNER_NAME if macos else "",
        "previously_granted": macos
        and (
            _ever_granted(progress, "accessibility") or _ever_granted(progress, "screen_recording")
        ),
    }
    sup = supervisor_status or {}
    verified = (
        _verified_for_current_install(progress, install_state) if install_state.installed else None
    )
    healthy = bool(
        install_state.installed
        and verified
        and sup.get("state") not in ("crash_loop", "unavailable")
    )
    doc: dict[str, Any] = {
        "state": NOT_CONFIGURED,
        "platform": platform,
        "driver": {
            "kind": kind,
            "version": DRIVER_VERSION if kind == DRIVER_UPSTREAM else "",
            "installed": bool(install_state.installed),
            "healthy": healthy,
        },
        "choice": choice,
        "session": session.as_dict(),
        "permissions": perms,
        "capabilities": dict(session.capabilities) or dict(session_mod.NO_CAPS),
        "limitations": list(session.limitations)
        + (list(MACOS_PREVIEW_LIMITS) if macos and kind == DRIVER_UPSTREAM else []),
        "steps": [],
        "user_action": "",
        "progress": {"last_step": str(progress.get("last_step") or "")},
    }
    if install_progress.get("running"):
        doc["progress"]["install_stage"] = install_progress.get("stage", "")
        doc["progress"]["bytes_done"] = install_progress.get("bytes_done", 0)
        doc["progress"]["bytes_total"] = install_progress.get("bytes_total", 0)

    perm_steps = ["accessibility", "screen_recording"] if macos else []
    order = ["install", *(perm_steps or ["session"]), "verify", "ready"]
    status_of = {name: STEP_PENDING for name in order}
    actions: dict[str, dict] = {}
    #: Steps not yet done even though a later step is the current or failed one.
    held: set[str] = set() if install_state.installed else {"install"}

    def finish(
        state: str, user_action: str = "", current: str = "", failed: str = ""
    ) -> dict[str, Any]:
        doc["state"] = state
        doc["user_action"] = user_action
        reached = True
        for name in order:
            if name == failed:
                status_of[name] = STEP_FAILED
                reached = False
            elif name == current:
                status_of[name] = STEP_CURRENT
                reached = False
            elif (
                reached
                and status_of[name] == STEP_PENDING
                and (current or failed)
                and name not in held
            ):
                status_of[name] = STEP_DONE
        if not current and not failed and state == READY:
            for name in order:
                status_of[name] = STEP_DONE
        doc["steps"] = [_step(n, status_of[n], actions.get(n)) for n in order]
        return doc

    # The native driver has no install or driver-owned permission step here.
    if kind == DRIVER_NATIVE:
        order = ["verify", "ready"]
        status_of = {name: STEP_PENDING for name in order}
        doc["driver"]["installed"] = True
        if locked:
            return finish(DISABLED, "Computer use is turned off by your organization's policy.")
        if not session.supported:
            # No desktop session: the native driver cannot work either.
            unavailable = session.kind in (session_mod.KIND_SESSION0, session_mod.KIND_NONE)
            return finish(
                TEMPORARILY_UNAVAILABLE if unavailable else UNSUPPORTED,
                session.limitations[0] if session.limitations else session.detail,
                failed="verify",
            )
        if macos and native_permissions:
            # Advisory readings for the Settings rows and grant shortcuts. A
            # "missing" here is not authoritative (the grant follows the process
            # that launched Kiro Crew), so it adds a hint, never a refusal.
            for key in ("accessibility", "screen_recording"):
                value = native_permissions.get(key, UNKNOWN)
                perms[key] = value if value in (GRANTED, MISSING) else UNKNOWN
            perms["owner_name"] = native_permissions.get("owner_name") or perms["owner_name"]
            if MISSING in (perms["accessibility"], perms["screen_recording"]):
                hint = native_permissions.get("responsible_hint") or ""
                if hint:
                    doc["limitations"].append(hint)
        if not keystone_enabled:
            return finish(
                DISABLED, "Turn on Computer Use to let the agent use your desktop.", current="ready"
            )
        return finish(READY)

    if not has_asset:
        doc["capabilities"] = dict(session_mod.NO_CAPS)
        return finish(UNSUPPORTED, "This operating system has no desktop driver.", failed="install")
    if locked:
        return finish(DISABLED, "Computer use is turned off by your organization's policy.")
    # Session first (Linux / Windows) or console (macOS): installing into a session
    # that can never drive the desktop would only add a step that cannot help.
    if not session.supported:
        first = "session" if not macos else "accessibility"
        if session.kind == session_mod.KIND_SESSION0 or session.kind == session_mod.KIND_NONE:
            return finish(
                TEMPORARILY_UNAVAILABLE,
                session.limitations[0] if session.limitations else session.detail,
                failed=first,
            )
        return finish(
            UNSUPPORTED,
            session.limitations[0] if session.limitations else session.detail,
            failed=first,
        )

    if install_progress.get("running"):
        return finish(INSTALLING, "", current="install")
    if not install_state.installed:
        install_error = install_progress.get("error") or progress.get("install_error")
        if install_error:
            doc["limitations"].append(str(install_error))
            return finish(
                INSTALL_FAILED,
                "The driver download or its checks failed; try the install again.",
                failed="install",
            )
        if install_state.version and install_state.version != DRIVER_VERSION:
            return finish(
                NOT_CONFIGURED, f"Update the desktop driver to {DRIVER_VERSION}.", current="install"
            )
        return finish(
            NOT_CONFIGURED,
            "Install the open-source desktop driver (downloaded from its official release).",
            current="install",
        )

    if sup.get("state") == "crash_loop":
        return finish(
            TEMPORARILY_UNAVAILABLE,
            "The desktop driver keeps stopping; restart it once the cause is fixed.",
            failed="verify",
        )
    if sup.get("state") == "unavailable" and sup.get("last_error"):
        doc["limitations"].append(str(sup.get("last_error")))
        return finish(
            ERROR, "The installed driver failed its checks; reinstall it.", failed="install"
        )

    if macos:
        for key, pane, requesting, denied in (
            ("accessibility", PANE_ACCESSIBILITY, REQUESTING_ACCESSIBILITY, ACCESSIBILITY_DENIED),
            (
                "screen_recording",
                PANE_SCREEN_RECORDING,
                REQUESTING_SCREEN_RECORDING,
                SCREEN_RECORDING_DENIED,
            ),
        ):
            state_now = perms[key]
            if state_now == GRANTED:
                status_of[key] = STEP_DONE
                continue
            actions[key] = _pane(pane)
            label = "Accessibility" if key == "accessibility" else "Screen Recording"
            if state_now == UNKNOWN:
                return finish(
                    requesting,
                    f"Check {label} for {OWNER_NAME}, the open-source driver Kiro Crew uses.",
                    current=key,
                )
            if _ever_granted(progress, key):
                return finish(
                    PERMISSION_REVOKED,
                    f"{label} for {OWNER_NAME} was turned off; turn it back on in System Settings.",
                    failed=key,
                )
            asked = _requested_at(progress, key)
            if asked and now - asked < WAITING_WINDOW_SECS:
                return finish(
                    WAITING_FOR_SYSTEM_SETTINGS,
                    f"Turn on {OWNER_NAME} under Privacy & Security, {label}, then check again.",
                    current=key,
                )
            if asked:
                return finish(
                    denied,
                    f"{OWNER_NAME} does not have {label}; allow it in System Settings, then check again.",
                    failed=key,
                )
            return finish(
                requesting,
                f"Allow {label} for {OWNER_NAME}, the open-source driver Kiro Crew uses.",
                current=key,
            )
        if progress.get("restart_required") is True:
            return finish(
                RESTART_REQUIRED,
                "Restart the desktop driver so it picks up the new permission.",
                current="verify",
            )
    else:
        status_of["session"] = STEP_DONE

    if verifying:
        return finish(VALIDATING, "", current="verify")
    if verified is False:
        verify = progress.get("verify") or {}
        if isinstance(verify, dict) and verify.get("detail"):
            doc["limitations"].append(str(verify.get("detail")))
        return finish(
            ERROR, "The desktop check failed; see the reason, then check again.", failed="verify"
        )
    if verified is None:
        return finish(NOT_CONFIGURED, "Run the desktop check.", current="verify")
    if not keystone_enabled:
        return finish(
            DISABLED, "Turn on Computer Use to let the agent use your desktop.", current="ready"
        )
    return finish(READY)


def _native_permissions(choice: str, root: Path) -> "dict[str, str] | None":
    """The native macOS driver's advisory grant probe, or ``None`` elsewhere.

    In-process and non-prompting (``AXIsProcessTrusted`` and the screen-capture
    preflight), so it is cheap enough for every status read.
    """
    if _platform() != PLATFORM_MACOS:
        return None
    installed = install_mod.install_state(root).installed
    if effective_driver(PLATFORM_MACOS, choice, installed=installed) != DRIVER_NATIVE:
        return None
    from kiro_crew.computer_use import permissions as perms_mod

    found = perms_mod.probe_dict()
    found["owner_name"] = perms_mod.responsible_process_name()
    return found


def get_setup_status(home: "str | os.PathLike[str] | None" = None) -> dict[str, Any]:
    """The D5 setup document. Never raises; never starts the driver."""
    try:
        root = _home(home)
        from kiro_crew.computer_use import enable_state

        keystone = _keystone()
        sup_status: "dict[str, Any] | None" = None
        from kiro_crew.computer_use.driver_runtime import supervisor as sup_mod

        if sup_mod._shared is not None:
            sup_status = sup_mod._shared.status()
        return build_status(
            platform=_platform(),
            choice=enable_state.driver_choice(keystone),
            keystone_enabled=enable_state.is_enabled(keystone),
            locked=enable_state.locked_by_policy(),
            install_state=install_mod.install_state(root),
            install_progress=install_mod.PROGRESS.snapshot(),
            session=session_mod.detect(),
            progress=load_progress(root),
            supervisor_status=sup_status,
            verifying=_verify_running.is_set(),
            has_asset=asset_for(install_mod.current_os()) is not None,
            native_permissions=_native_permissions(enable_state.driver_choice(keystone), root),
        )
    except Exception as exc:  # noqa: BLE001 - the handler must always get a document
        logger.debug("computer-use setup status failed", exc_info=True)
        return {
            "state": ERROR,
            "platform": _platform(),
            "driver": {"kind": "", "version": DRIVER_VERSION, "installed": False, "healthy": False},
            "session": {"kind": session_mod.KIND_NONE, "detail": ""},
            "permissions": {
                "accessibility": UNKNOWN,
                "screen_recording": UNKNOWN,
                "owner_name": OWNER_NAME,
                "previously_granted": False,
            },
            "capabilities": dict(session_mod.NO_CAPS),
            "limitations": [f"the setup state could not be read ({exc})"],
            "steps": [],
            "user_action": "Try again.",
            "progress": {"last_step": ""},
        }


# ── actions ──


def _start_install(root: Path) -> None:
    global _install_thread
    if install_mod.PROGRESS.running or (_install_thread is not None and _install_thread.is_alive()):
        return

    def run() -> None:
        try:
            install_mod.install(root)
            _update_progress(root, last_step="install", install_error="")
        except install_mod.InstallError as exc:
            _update_progress(root, last_step="install", install_error=exc.detail)
        except Exception as exc:  # noqa: BLE001
            logger.warning("computer-use driver install failed", exc_info=True)
            _update_progress(root, last_step="install", install_error=str(exc))

    # Mark running before the thread starts so the returned document says so.
    install_mod.PROGRESS.running = True
    install_mod.PROGRESS.stage = install_mod.STAGE_PREPARING
    install_mod.PROGRESS.error = ""
    _install_thread = threading.Thread(target=run, name="cu-driver-install", daemon=True)
    try:
        _install_thread.start()
    except Exception:
        install_mod.PROGRESS.running = False
        raise


def _supervisor(root: Path) -> Any:
    from kiro_crew.computer_use.driver_runtime import supervisor as sup_mod

    return sup_mod.get_supervisor(root)


def _parse_permissions(sc: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, names in (
        ("accessibility", ("accessibility", "ax", "accessibility_granted")),
        ("screen_recording", ("screen_recording", "screen_capture", "screen_recording_granted")),
    ):
        for name in names:
            value = sc.get(name)
            if isinstance(value, dict):
                value = value.get("granted", value.get("status"))
            if value is True or value == "granted":
                out[key] = GRANTED
                break
            if value is False or value in ("denied", "missing", "not_granted", "not_determined"):
                out[key] = MISSING
                break
    return out


def _recheck(root: Path, *, prompt: bool = False) -> None:
    if install_mod.current_os() != OS_MACOS:
        _update_progress(root, last_step="session", checked_at=time.time())
        return
    sup = _supervisor(root)
    result = sup.call(
        "check_permissions", {"prompt": prompt} if prompt else {}, timeout=20.0, idempotent=True
    )
    perms = _parse_permissions(result.get("structuredContent") or {})
    with _lock:
        data = load_progress(root)
        before = _dict_of(data, "permissions")
        ever = _dict_of(data, "ever_granted")
        for key, value in perms.items():
            if value == GRANTED:
                ever[key] = True
                if before.get(key) == MISSING:
                    # The running driver may still answer from a cached decision.
                    data["restart_required"] = True
        data.update(permissions={**before, **perms}, ever_granted=ever, checked_at=time.time())
        data["last_step"] = "accessibility"
        save_progress(root, data)


def _denied_window(window: dict[str, Any]) -> bool:
    """True for a window the built-in deny floor names (Kiro Crew's own dashboard)."""
    from kiro_crew.computer_use import policy
    from kiro_crew.computer_use.types import AppRef

    ref = AppRef(
        name=str(window.get("app_name") or ""),
        pid=int(window.get("pid") or 0),
        bundle_id=str(window.get("bundle_id") or ""),
        window_title=str(window.get("title") or ""),
    )
    return policy.denied_rule_for(ref) is not None or policy.title_is_denied(ref.window_title)


def _verify(root: Path) -> dict[str, Any]:
    """Non-destructive desktop check: list, read one tree, one capture, health."""
    sup = _supervisor(root)
    state = install_mod.install_state(root)
    detail = ""
    ok = False
    try:
        windows = (
            sup.call("list_windows", {"on_screen_only": True}, timeout=20.0, idempotent=True).get(
                "structuredContent"
            )
            or {}
        ).get("windows") or []
        # Never read a window the built-in deny floor names (Kiro Crew's own
        # dashboard): the check needs any ordinary window, not that one.
        target = next(
            (
                w
                for w in windows
                if isinstance(w, dict) and isinstance(w.get("pid"), int) and not _denied_window(w)
            ),
            None,
        )
        if target is None:
            detail = "no on-screen window was found to read; open any app and check again"
        else:
            shot_dir = install_mod.runtime_root(root) / "capture"
            shot_dir.mkdir(parents=True, exist_ok=True)
            platform_compat.chmod_safe(shot_dir, 0o700)
            fd, shot = tempfile.mkstemp(prefix="verify-", suffix=".png", dir=shot_dir)
            os.close(fd)
            try:
                read = sup.call(
                    "get_window_state",
                    {
                        "pid": target["pid"],
                        "window_id": target.get("window_id"),
                        "tree_format": "elements",
                        "include_screenshot": True,
                        "screenshot_out_file": shot,
                        "max_elements": 200,
                    },
                    timeout=30.0,
                    idempotent=True,
                )
                captured = os.path.getsize(shot) > 0
            finally:
                try:
                    os.unlink(shot)
                except OSError:
                    pass
            if read.get("isError"):
                detail = "the accessibility tree could not be read"
            elif not captured:
                detail = "the window capture produced no image"
            else:
                health = sup.call("health_report", {}, timeout=30.0, idempotent=True)
                overall = (health.get("structuredContent") or {}).get("overall")
                if overall in ("ok", "degraded"):
                    ok = True
                    if overall == "degraded":
                        detail = "the driver reports a degraded desktop; some actions may refuse"
                else:
                    detail = f"the driver health report is {overall or 'unavailable'}"
    except Exception as exc:  # noqa: BLE001 - a failed check is a state, not a crash
        detail = str(exc)
    verify = {"ok": ok, "detail": detail, "at": time.time(), "binary_sha256": state.binary_sha256}
    changes: dict[str, Any] = {"verify": verify, "last_step": "verify"}
    if ok:
        changes["restart_required"] = False
    _update_progress(root, **changes)
    return verify


def run_setup_action(action: str, home: "str | os.PathLike[str] | None" = None) -> dict[str, Any]:
    """Run one owner setup action and return the new document. Never raises."""
    try:
        root = _home(home)
        if action not in ACTIONS:
            doc = get_setup_status(root)
            doc["limitations"] = [*doc.get("limitations", []), f"unknown setup action {action!r}"]
            return doc
        from kiro_crew.computer_use import enable_state

        if action != "reset" and enable_state.locked_by_policy():
            # The organization's ceiling turns computer use off: nothing is
            # downloaded or run. Removing what is already there stays allowed.
            return get_setup_status(root)
        if action == "install":
            _start_install(root)
        elif action == "reset":
            # Remove the driver Kiro Crew installed (on macOS, the /Applications copy
            # only if Kiro Crew placed it and it is unchanged) and start over.
            stop = getattr(_supervisor(root), "stop", None)
            if callable(stop):
                stop()
            note = install_mod.uninstall(root)
            logger.info("computer-use setup reset: %s", note)
            with _lock:
                try:
                    _progress_path(root).unlink()
                except FileNotFoundError:
                    pass
            install_mod.PROGRESS.error = ""
        elif action in ("request_permissions", "recheck", "verify", "restart_driver"):
            if not install_mod.install_state(root).installed:
                return get_setup_status(root)
            if action == "restart_driver":
                _supervisor(root).restart()
                _update_progress(root, restart_required=False, last_step="verify")
                _recheck(root)
            elif action == "request_permissions":
                if install_mod.current_os() == OS_MACOS:
                    now = time.time()
                    data = load_progress(root)
                    asked = _dict_of(data, "requested")
                    for key in ("accessibility", "screen_recording"):
                        if _perm_state(data, key) != GRANTED:
                            asked[key] = now
                    _update_progress(root, requested=asked, last_step="accessibility")
                    _recheck(root, prompt=True)
            elif action == "recheck":
                _recheck(root)
            else:
                _verify_running.set()
                try:
                    _verify(root)
                finally:
                    _verify_running.clear()
        return get_setup_status(root)
    except Exception as exc:  # noqa: BLE001
        logger.debug("computer-use setup action %s failed", action, exc_info=True)
        doc = get_setup_status(home)
        doc["limitations"] = [*doc.get("limitations", []), f"{action} failed: {exc}"]
        return doc


def set_driver_choice(choice: str) -> None:
    """Persist the owner's driver choice in the keystone (owner-only callers)."""
    from kiro_crew.computer_use import backend as backend_mod
    from kiro_crew.computer_use import enable_state

    if choice not in DRIVER_CHOICES:
        raise ValueError(f"driver must be one of {', '.join(DRIVER_CHOICES)}")
    state = enable_state.load_state()
    state[STATE_KEY_DRIVER] = choice
    enable_state.save_state(state)
    backend_mod.reset_shared_backend()


__all__ = [
    "ACTIONS",
    "DRIVER_AUTO",
    "STATES",
    "build_status",
    "effective_driver",
    "get_setup_status",
    "load_progress",
    "run_setup_action",
    "set_driver_choice",
]
