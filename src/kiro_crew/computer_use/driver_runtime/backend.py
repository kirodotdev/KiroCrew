"""``CuaBackend``: Kiro Crew's ``computer_*`` verbs on the upstream driver.

The model-facing contract does not change: element indexes come from OUR walk,
refusals are prose without the ``Error:`` prefix, results are text. This class
maps each verb onto the closed allowlist the supervisor enforces:

=====================  ===============================================
our verb               upstream call
=====================  ===============================================
list_apps/resolve      ``list_windows`` (on-screen), grouped by pid
snapshot               ``get_window_state`` ``tree_format: "elements"``
click (element)        ``click`` by ``element_token``
click (point)          ``get_window_state`` screenshot read, then ``click`` x/y
drag                   same capture read, then ``drag``
type_text / set_value  ``type_text`` / ``set_value`` by ``element_token``
press_key              ``press_key`` by ``element_token`` with modifiers
scroll                 ``scroll`` by ``element_token``
perform_action         ``click`` (press) or right ``click`` (menu)
launch_app             OUR catalog and launcher, never upstream ``launch_app``
=====================  ===============================================

Element tokens are kept per window for the LATEST walk only (the drift
verification walk the service runs before every mutation is the latest), and are
dropped whenever the driver restarts: a token never crosses a generation.

Delivery is background by default. ``click_method: "global"`` (named by the
model, separately audited upstream of here) is the only path to the driver's
foreground delivery; ``sky_click`` refuses by name. A ``background_unavailable``
refusal reaches the model as a typed refusal naming the alternative. An action
whose ``effect`` is not ``confirmed`` is reported as unverified ("re-read to
confirm"), and ``effect: "none"`` as not done.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from kiro_crew.computer_use import floors, keymap
from kiro_crew.computer_use.backend import ComputerUseBackend, run_launch
from kiro_crew.computer_use.driver_runtime import env as env_mod
from kiro_crew.computer_use.driver_runtime import install as install_mod
from kiro_crew.computer_use.driver_runtime import session as session_mod
from kiro_crew.computer_use.driver_runtime.pin import OS_LINUX, OS_MACOS, OS_WINDOWS
from kiro_crew.computer_use.driver_runtime.supervisor import (
    DriverError,
    DriverSupervisor,
    OutcomeUnknown,
    get_supervisor,
)
from kiro_crew.computer_use.types import (
    CLICK_METHOD_ACCESSIBILITY,
    CLICK_METHOD_APP_POST,
    CLICK_METHOD_GLOBAL,
    CLICK_METHOD_SKY_CLICK,
    DRAG_PATH_STRAIGHT,
    PERMISSION_GRANTED,
    PERMISSION_MISSING,
    PERMISSION_UNKNOWN,
    PERMISSION_UNSUPPORTED,
    PLATFORM_LINUX,
    PLATFORM_MACOS,
    PLATFORM_WINDOWS,
    REFUSAL_UNSUPPORTED,
    AppRef,
    BackendStatus,
    ClickRequest,
    ComputerUseError,
    DragRequest,
    DriverResult,
    ElementRec,
    KeyParseError,
    LaunchIdentity,
    PermissionProbe,
    Snapshot,
    SnapshotRequest,
)

logger = logging.getLogger(__name__)

#: Upstream tools this backend needs; checked against ``tools/list`` at start.
REQUIRED_TOOLS: frozenset[str] = frozenset(
    {"list_windows", "get_window_state", "click", "type_text", "press_key", "set_value", "scroll"}
)

_PLATFORM_FOR_OS = {
    OS_LINUX: PLATFORM_LINUX,
    OS_MACOS: PLATFORM_MACOS,
    OS_WINDOWS: PLATFORM_WINDOWS,
}

#: Walk deadline handed to the driver (it returns a partial tree, flagged, on expiry).
WALK_TIMEOUT_MS = 5000
#: Per-call deadline for the supervisor: the walk deadline plus capture and slack.
SNAPSHOT_CALL_TIMEOUT_SECS = 20.0
ACTION_CALL_TIMEOUT_SECS = 20.0

ERR_NOT_INSTALLED = (
    "the desktop driver is not installed; the owner installs it in Settings -> Computer Use"
)
#: A curved drag on the upstream driver, which draws only straight strokes.
ERR_CURVED_DRAG = (
    "this desktop driver draws straight drags only, so a '{path}' path was not sent. "
    'Use path "straight", or draw the stroke as several straight drags'
)
ERR_STALE_TOKEN = (
    "element [{index}] is not in the latest walk of this window; call computer_get_state "
    "and use an index from that result"
)
ERR_SKY_CLICK = (
    "click_method 'sky_click' is a macOS-native method this driver does not have; use "
    "'auto' (element press or a background click) or name 'global' to accept a "
    "foreground click"
)
ERR_ACTION_NONE = "the driver reports the {verb} had no effect; nothing changed"
NOTE_UNVERIFIED = (
    "the driver sent the {verb} but could not confirm its effect; re-read the state to "
    "confirm before repeating it"
)
ERR_BACKGROUND_UNAVAILABLE = "this desktop cannot deliver the {verb} in the background ({detail})"
HINT_FOREGROUND = '; name click_method "global" to bring the window forward and deliver it there'
HINT_ELEMENT = "; address an element by element_index instead"
ERR_UNSUPPORTED_KEY = "the key '{key}' is not supported by this driver"
ERR_UNSUPPORTED_ACTION = (
    "the action '{action}' is not supported by this driver; use press, AXPress, "
    "show_menu or AXShowMenu"
)
ERR_NO_WINDOW = "{app} has no on-screen window"

_PRESS_ACTIONS = frozenset({"press", "axpress", "click", "activate", "invoke", "toggle", "select"})
_MENU_ACTIONS = frozenset({"axshowmenu", "show_menu", "showmenu", "menu", "context_menu"})

#: Our canonical key names (``keymap.parse_spec``) to upstream ``press_key`` names.
_KEY_NAMES: dict[str, str] = {
    "return": "return",
    "tab": "tab",
    "escape": "escape",
    "space": "space",
    "spacebar": "space",
    " ": "space",
    "delete": "backspace",
    "backspace": "backspace",
    "forwarddelete": "delete",
    "insert": "insert",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "apostrophe": "'",
    "backslash": "\\",
    "backtick": "`",
    "comma": ",",
    "equals": "=",
    "leftbracket": "[",
    "rightbracket": "]",
    "minus": "-",
    "period": ".",
    "semicolon": ";",
    "slash": "/",
    "keypadenter": "return",
    **{f"f{n}": f"f{n}" for n in range(1, 13)},
    **{f"keypad{n}": str(n) for n in range(10)},
}

_MODIFIERS: dict[str, dict[str, str]] = {
    OS_LINUX: {"ctrl": "ctrl", "shift": "shift", "alt": "alt", "win": "super"},
    OS_WINDOWS: {"ctrl": "ctrl", "shift": "shift", "alt": "alt", "win": "win"},
    OS_MACOS: {"ctrl": "cmd", "shift": "shift", "alt": "option", "win": "ctrl"},
}

#: Control types whose secure-ness the Windows tree cannot prove (it does not
#: expose UIA ``IsPassword``). Treated as secure: their value is not shown, they
#: are not typed into, and their window is not captured.
_WINDOWS_UNPROVEN_SECURE_ROLES = frozenset({"edit"})

#: Linux text-entry roles. The driver marks a field secure only by the AT-SPI
#: role ``password text``; a GTK 4 password entry reports a plain ``text box``
#: (measured: zenity --password on GTK 4), and the tree carries no AT-SPI
#: ``protected`` state. GTK disables the copy and cut actions of a text whose
#: content is hidden, so an entry is treated as NOT secure only when it offers
#: one of them; every other entry is secure (fail closed).
_LINUX_TEXT_ENTRY_ROLES = frozenset({"text box", "entry", "text", "editable text"})
_LINUX_PROOF_OF_VISIBLE_TEXT = ("clipboard.copy", "clipboard.cut", "copy", "cut")


def _native_windows_password_free(app: AppRef) -> bool:
    """True only when Kiro Crew's own UI Automation walk proves *app*'s window holds
    no password field (complete walk, ``IsPassword`` read live). Any doubt is False."""
    from kiro_crew.computer_use import snapshot_windows
    from kiro_crew.computer_use.types import MAX_TREE_DEPTH_LIMIT, MAX_TREE_NODES_LIMIT

    try:
        snap = snapshot_windows.build_snapshot(
            app,
            SnapshotRequest(
                max_nodes=MAX_TREE_NODES_LIMIT, max_depth=MAX_TREE_DEPTH_LIMIT, want_image=False
            ),
        )
    except Exception:  # noqa: BLE001 - no proof
        logger.debug("native password cross-check failed", exc_info=True)
        return False
    return not (snap.has_secure or snap.truncated or snap.depth_truncated)


def is_secure_element(os_id: str, entry: Mapping[str, Any]) -> bool:
    """Whether an upstream element must be treated as a secure (password) field."""
    role = str(entry.get("role") or "").lower()
    subrole = str(entry.get("subrole") or "").lower()
    if "password" in role or "secure" in role or "password" in subrole or "secure" in subrole:
        return True
    if entry.get("is_password") is True or entry.get("protected") is True:
        return True
    if os_id == OS_WINDOWS and role in _WINDOWS_UNPROVEN_SECURE_ROLES:
        return True
    if os_id == OS_LINUX and role in _LINUX_TEXT_ENTRY_ROLES:
        actions = {str(a).lower() for a in entry.get("actions") or () if isinstance(a, str)}
        return not any(proof in actions for proof in _LINUX_PROOF_OF_VISIBLE_TEXT)
    return False


def _names_a_password(entry: Mapping[str, Any]) -> bool:
    """A secure signal the tree itself carries (as opposed to an unproven role)."""
    role = str(entry.get("role") or "").lower()
    subrole = str(entry.get("subrole") or "").lower()
    return (
        any(word in role or word in subrole for word in ("password", "secure"))
        or entry.get("is_password") is True
        or entry.get("protected") is True
    )


def _identity(rec: ElementRec) -> "tuple[str, ...]":
    """What a token was walked for: the element's role, subrole, depth and label."""
    return (rec.role, rec.subrole, str(rec.depth), rec.title)


def _clip(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[:limit] + "…"


def _bounds(raw: Any) -> "tuple[float, float, float, float] | None":
    if not isinstance(raw, Mapping):
        return None
    try:
        width = float(raw.get("width", raw.get("w")))
        height = float(raw.get("height", raw.get("h")))
        return (float(raw["x"]), float(raw["y"]), width, height)
    except (KeyError, TypeError, ValueError):
        return None


def _text_of(result: Mapping[str, Any]) -> str:
    parts = [
        str(c.get("text", ""))
        for c in result.get("content") or []
        if isinstance(c, Mapping) and c.get("type") == "text"
    ]
    return " ".join(p.strip() for p in parts if p.strip())


def _png_to_jpeg(png: bytes, *, max_px: int, quality: int) -> "tuple[bytes, int, int]":
    from PIL import Image

    with Image.open(io.BytesIO(png)) as img:
        img.load()
        rgb = img.convert("RGB")
    width, height = rgb.size
    scale = min(1.0, max_px / float(max(width, height) or 1))
    if scale < 1.0:
        rgb = rgb.resize((max(1, int(width * scale)), max(1, int(height * scale))))
    out = io.BytesIO()
    rgb.save(out, format="JPEG", quality=max(1, min(int(quality), 95)))
    return out.getvalue(), rgb.size[0], rgb.size[1]


class CuaBackend(ComputerUseBackend):
    """The upstream driver behind the seam. Never raises; thread-safe."""

    confines_points_natively = False

    def __init__(
        self,
        *,
        supervisor: "DriverSupervisor | None" = None,
        home: "str | os.PathLike[str] | None" = None,
        os_id: "str | None" = None,
        session: "Callable[[], session_mod.SessionInfo] | None" = None,
        launcher: "Mapping[str, Callable[..., Any]] | None" = None,
        launch_timeout: "float | None" = None,
        password_free: "Callable[[AppRef], bool] | None" = None,
    ) -> None:
        self._os = os_id or install_mod.current_os()
        self._home = Path(home) if home is not None else None
        self._supervisor = supervisor
        self._session = session or session_mod.detect
        #: An injected supervisor (tests, an embedding host) owns its own binary
        #: resolution; only the shared one is gated on the on-disk install.
        self._supervisor_is_default = supervisor is None
        self._launcher = launcher
        self._launch_timeout = launch_timeout
        #: Windows only: proves a window has no password field, because the upstream
        #: tree does not carry ``IsPassword`` (see ``is_secure_element``).
        self._password_free = password_free or _native_windows_password_free
        self._lock = threading.Lock()
        #: (pid, window_id) -> (generation, {our index: element token})
        self._tokens: dict[tuple[int, int], tuple[int, dict[int, tuple[str, tuple[str, ...]]]]] = {}
        #: (pid, window_id) -> screen bounds from the latest read
        self._bounds: dict[tuple[int, int], tuple[float, float, float, float]] = {}
        self._checked_generation = -1
        self._missing_tools: frozenset[str] = frozenset()

    # ── wiring ──

    @property
    def platform_id(self) -> str:
        return _PLATFORM_FOR_OS.get(self._os, "unsupported")

    def _data_home(self) -> Path:
        if self._home is None:
            from kiro_crew.config.paths import data_home

            self._home = Path(data_home())
        return self._home

    @property
    def supervisor(self) -> DriverSupervisor:
        if self._supervisor is None:
            self._supervisor = get_supervisor(self._data_home())
        return self._supervisor

    def _call(
        self, tool: str, args: Mapping[str, Any], *, timeout: float, idempotent: bool = False
    ) -> dict[str, Any]:
        sup = self.supervisor
        if (
            self._supervisor_is_default
            and not install_mod.install_state(self._data_home()).installed
        ):
            failed = install_mod.PROGRESS.error
            reason = f"{failed}; {ERR_NOT_INSTALLED}" if failed else ERR_NOT_INSTALLED
            raise ComputerUseError(
                REFUSAL_UNSUPPORTED.format(platform=self.platform_id, reason=reason)
            )
        info = self._session()
        if not info.supported:
            # Never report an empty desktop where the session cannot be seen at all.
            reason = info.limitations[0] if info.limitations else info.detail
            raise ComputerUseError(
                REFUSAL_UNSUPPORTED.format(platform=self.platform_id, reason=reason)
            )
        sup.ensure()
        self._check_tools(sup)
        if tool in self._missing_tools:
            raise ComputerUseError(
                f"this driver build does not provide '{tool}', which Kiro Crew needs for "
                "this action"
            )
        try:
            result = sup.call(tool, args, timeout=timeout, idempotent=idempotent)
        except OutcomeUnknown:
            self._drop_tokens()
            raise
        self._check_tools(sup)
        return result

    def _check_tools(self, sup: DriverSupervisor) -> None:
        generation = sup.generation
        if generation == self._checked_generation:
            return
        self._checked_generation = generation
        self._drop_tokens()
        listed = sup.available_tools()
        self._missing_tools = frozenset(REQUIRED_TOOLS - listed) if listed else frozenset()
        if self._missing_tools:
            logger.warning(
                "computer-use driver is missing tools: %s", ", ".join(sorted(self._missing_tools))
            )

    def _drop_tokens(self) -> None:
        with self._lock:
            self._tokens.clear()

    def _guard(self, label: str, run: Callable[[], DriverResult]) -> DriverResult:
        try:
            return run()
        except DriverError as exc:
            return DriverResult(ok=False, text=str(exc))
        except (ComputerUseError, KeyParseError) as exc:
            return DriverResult(ok=False, text=str(exc))
        except Exception as exc:  # noqa: BLE001 - nothing crosses the seam
            logger.debug("computer-use driver %s failed", label, exc_info=True)
            return DriverResult(ok=False, text=f"the desktop driver failed during {label}: {exc}")

    # ── status ──

    def status(self) -> BackendStatus:
        """Supported wherever a pinned driver exists for this host.

        A driver that is not installed yet, or a session it cannot use, is a
        SETUP state, reported by ``computer_use.setup`` and fixable from the
        Settings page; ``supported=False`` here would hide that page's setup flow.
        ``reason`` still names what is missing, and every tool call refuses until
        it is fixed.
        """
        try:
            from kiro_crew.computer_use.driver_runtime.pin import asset_for

            if self._os not in _PLATFORM_FOR_OS or asset_for(self._os) is None:
                return BackendStatus(
                    False, self.platform_id, "this operating system and CPU have no desktop driver"
                )
            if not install_mod.install_state(self._data_home()).installed:
                return BackendStatus(True, self.platform_id, ERR_NOT_INSTALLED)
            info = self._session()
            if not info.supported:
                reason = info.limitations[0] if info.limitations else info.detail
                return BackendStatus(True, self.platform_id, reason)
            return BackendStatus(True, self.platform_id, "")
        except Exception as exc:  # noqa: BLE001
            return BackendStatus(False, self.platform_id, f"the driver status is unknown ({exc})")

    def probe_permissions(self) -> PermissionProbe:
        if self._os != OS_MACOS:
            return PermissionProbe(PERMISSION_UNSUPPORTED, PERMISSION_UNSUPPORTED, "")
        try:
            result = self._call("check_permissions", {}, timeout=10.0, idempotent=True)
        except Exception:  # noqa: BLE001 - advisory only
            return PermissionProbe(PERMISSION_UNKNOWN, PERMISSION_UNKNOWN, "CuaDriver")
        sc = result.get("structuredContent") or {}

        def _state(*keys: str) -> str:
            for key in keys:
                value = sc.get(key)
                if value is True or value == "granted":
                    return PERMISSION_GRANTED
                if value is False or value in ("denied", "missing", "not_granted"):
                    return PERMISSION_MISSING
            return PERMISSION_UNKNOWN

        return PermissionProbe(
            accessibility=_state("accessibility", "ax", "accessibility_granted"),
            screen_recording=_state(
                "screen_recording", "screen_capture", "screen_recording_granted"
            ),
            responsible_hint="CuaDriver",
        )

    # ── windows ──

    def _windows(self) -> "list[AppRef]":
        result = self._call("list_windows", {"on_screen_only": True}, timeout=15.0, idempotent=True)
        if result.get("isError"):
            raise ComputerUseError(_text_of(result) or "the driver could not list windows")
        records = (result.get("structuredContent") or {}).get("windows") or []
        refs: list[AppRef] = []
        for rec in records:
            if not isinstance(rec, Mapping):
                continue
            pid = rec.get("pid")
            wid = rec.get("window_id")
            if not isinstance(pid, int) or not isinstance(wid, int) or pid <= 0:
                continue
            name = str(rec.get("app_name") or rec.get("owner") or "").strip()
            bundle = str(rec.get("bundle_id") or "").strip()
            if not name and not bundle:
                continue
            ref = AppRef(
                name=name or bundle,
                pid=pid,
                bundle_id=bundle,
                window_id=wid,
                window_title=str(rec.get("title") or ""),
            )
            bounds = _bounds(rec.get("bounds")) or _bounds(rec)
            if bounds is not None:
                with self._lock:
                    self._bounds[(pid, wid)] = bounds
            refs.append(ref)
        return refs

    def _per_process(self, windows: "list[AppRef]") -> "list[AppRef]":
        grouped: dict[int, list[AppRef]] = {}
        for ref in windows:
            grouped.setdefault(ref.pid, []).append(ref)
        out: list[AppRef] = []
        for refs in grouped.values():
            chosen = floors.representative_window(refs)
            if chosen is not None:
                out.append(chosen)
        return out

    def list_apps(self) -> DriverResult:
        return self._guard(
            "list_apps",
            lambda: DriverResult(ok=True, apps=tuple(self._per_process(self._windows()))),
        )

    def _find(self, query: str, windows: "list[AppRef] | None" = None) -> "AppRef | None":
        wanted = query.strip().casefold()
        if not wanted:
            return None
        apps = self._per_process(self._windows() if windows is None else windows)
        for match in (
            lambda a: a.name.casefold() == wanted or a.bundle_id.casefold() == wanted,
            lambda a: a.window_title.casefold() == wanted,
            lambda a: a.name.casefold().startswith(wanted),
        ):
            hits = [a for a in apps if match(a)]
            if len(hits) == 1:
                return hits[0]
            if hits:
                # Several processes answer to the name: prefer a denied title so the
                # self-window refusal cannot be dodged, else the first.
                return floors.representative_window(hits)
        return None

    def resolve_app(self, query: str) -> DriverResult:
        def run() -> DriverResult:
            found = self._find(query)
            if found is None:
                return DriverResult(
                    ok=False,
                    text=f"no application named '{query}' has an on-screen window; "
                    "call computer_list_apps to see what is running",
                )
            return DriverResult(ok=True, app=found)

        return self._guard("resolve_app", run)

    def window_bounds(self, app: AppRef) -> "tuple[float, float, float, float] | None":
        with self._lock:
            return self._bounds.get((app.pid, app.window_id))

    # ── launch ──

    def _launch_functions(self) -> Mapping[str, Callable[..., Any]]:
        if self._launcher is not None:
            return self._launcher
        if self._os == OS_LINUX:
            from kiro_crew.computer_use.driver_runtime import linux_apps

            return {
                "resolve": linux_apps.resolve_target,
                "identity": linux_apps.target_identity,
                "spawn": lambda target: linux_apps.spawn_detached(
                    target, env_mod.desktop_env(os.environ)
                ),
            }
        if self._os == OS_MACOS:
            from kiro_crew.computer_use import launch_macos

            return {
                "resolve": launch_macos.resolve_target,
                "identity": launch_macos.target_identity,
                "spawn": launch_macos.spawn_detached,
            }
        from kiro_crew.computer_use import launch_windows

        return {
            "resolve": launch_windows.resolve_target,
            "identity": launch_windows.target_identity,
            "spawn": launch_windows.spawn_detached,
        }

    def launch_app(
        self,
        query: str,
        *,
        permit: "Callable[[LaunchIdentity], str | None] | None" = None,
        refuse_launched: "Callable[[AppRef], str | None] | None" = None,
    ) -> DriverResult:
        def run() -> DriverResult:
            refusal = floors.check_launch_query(query)
            if refusal:
                return DriverResult(ok=False, text=refusal)
            fns = self._launch_functions()
            kwargs: dict[str, Any] = {}
            if self._launch_timeout is not None:
                kwargs["window_timeout"] = self._launch_timeout
            return run_launch(
                query,
                resolve=fns["resolve"],
                find=lambda name: self._find(name),
                spawn=fns["spawn"],
                permit=permit,
                identity=fns.get("identity"),
                refuse_launched=refuse_launched,
                **kwargs,
            )

        return self._guard("launch_app", run)

    # ── observation ──

    def _shot_dir(self) -> Path:
        path = install_mod.runtime_root(self._data_home()) / "capture"
        path.mkdir(parents=True, exist_ok=True)
        from kiro_crew import platform_compat

        platform_compat.chmod_safe(path, 0o700)
        return path

    def _read_window(self, app: AppRef, req: SnapshotRequest) -> Snapshot:
        args: dict[str, Any] = {
            "pid": app.pid,
            "tree_format": "elements",
            "include_screenshot": bool(req.want_image),
            "max_elements": int(req.max_nodes),
            "max_depth": int(req.max_depth),
            "timeout_ms": WALK_TIMEOUT_MS,
        }
        if app.window_id:
            args["window_id"] = app.window_id
        shot_path = ""
        if req.want_image:
            fd, shot_path = tempfile.mkstemp(prefix="state-", suffix=".png", dir=self._shot_dir())
            os.close(fd)
            args["screenshot_out_file"] = shot_path
            args["max_image_dimension"] = 0
        try:
            result = self._call(
                "get_window_state", args, timeout=SNAPSHOT_CALL_TIMEOUT_SECS, idempotent=True
            )
            if result.get("isError"):
                raise ComputerUseError(_text_of(result) or "the driver could not read the window")
            sc = result.get("structuredContent") or {}
            png = b""
            if shot_path and os.path.getsize(shot_path) > 0:
                png = Path(shot_path).read_bytes()
        finally:
            if shot_path:
                try:
                    os.unlink(shot_path)
                except OSError:
                    pass
        return self._to_snapshot(app, req, sc, png)

    def _to_snapshot(
        self, app: AppRef, req: SnapshotRequest, sc: Mapping[str, Any], png: bytes
    ) -> Snapshot:
        wid = sc.get("window_id") if isinstance(sc.get("window_id"), int) else app.window_id
        target = AppRef(
            name=str(sc.get("app_name") or app.name),
            pid=app.pid,
            bundle_id=app.bundle_id,
            window_id=int(wid or 0),
            window_title=str(sc.get("window_title") or app.window_title),
        )
        elements: list[ElementRec] = []
        # Each token keeps the identity of the element it was walked for, so a
        # later walk by another session that re-indexes the window cannot hand
        # this session's verified index a different control's token.
        tokens: dict[int, tuple[str, tuple[str, ...]]] = {}
        raw_elements = [e for e in sc.get("elements") or [] if isinstance(e, Mapping)]
        unproven = self._os == OS_WINDOWS and any(
            str(e.get("role") or "").lower() in _WINDOWS_UNPROVEN_SECURE_ROLES for e in raw_elements
        )
        proven_free = unproven and self._password_free(target)
        for raw in raw_elements:
            index = len(elements)
            secure = is_secure_element(self._os, raw)
            if secure and proven_free and not _names_a_password(raw):
                secure = False
            frame = _bounds(raw.get("frame"))
            traits = tuple(t for t in ("selected",) if raw.get(t) is True)
            token = raw.get("element_token")
            rec = ElementRec(
                index=index,
                role=str(raw.get("role") or ""),
                subrole=str(raw.get("subrole") or ""),
                title=_clip(raw.get("label") or raw.get("title"), req.text_limit),
                value="" if secure else _clip(raw.get("value"), req.text_limit),
                actions=tuple(str(a) for a in raw.get("actions") or () if isinstance(a, str)),
                depth=int(raw.get("depth") or 0),
                secure=secure,
                enabled=raw.get("enabled") is not False,
                frame=frame,
                traits=traits,
                focused=raw.get("focused") is True,
            )
            if isinstance(token, str) and token:
                tokens[index] = (token, _identity(rec))
            elements.append(rec)
        truncated = (
            sc.get("truncated") is True
            or sc.get("elements_complete") is False
            or sc.get("degraded") is True
            or len(elements) > req.max_nodes
        )
        elements = elements[: req.max_nodes]
        bounds = _bounds(sc.get("window_bounds"))
        if bounds is not None:
            with self._lock:
                self._bounds[(target.pid, target.window_id)] = bounds
        generation = self.supervisor.generation
        with self._lock:
            self._tokens[(target.pid, target.window_id)] = (generation, tokens)
            if (app.pid, app.window_id) != (target.pid, target.window_id):
                self._tokens[(app.pid, app.window_id)] = (generation, tokens)
        snap = Snapshot(
            app=target,
            elements=tuple(elements),
            window_title=target.window_title,
            captured_at=time.monotonic(),
            truncated=truncated,
            has_secure=any(e.secure for e in elements),
            window_bounds=bounds,
        )
        if png and floors.screenshot_permitted(snap):
            try:
                jpeg, width, height = _png_to_jpeg(
                    png, max_px=req.image_max_px, quality=req.image_quality
                )
                from dataclasses import replace

                snap = replace(snap, image_jpeg=jpeg, image_width=width, image_height=height)
            except Exception:  # noqa: BLE001 - the tree is the primary channel
                logger.debug("computer-use screenshot conversion failed", exc_info=True)
        return snap

    def snapshot(self, app: AppRef, req: SnapshotRequest) -> DriverResult:
        return self._guard(
            "snapshot", lambda: DriverResult(ok=True, snapshot=self._read_window(app, req))
        )

    # ── actions ──

    def _token(self, app: AppRef, rec: ElementRec) -> str:
        with self._lock:
            entry = self._tokens.get((app.pid, app.window_id))
        if entry is None or entry[0] != self.supervisor.generation or rec.index not in entry[1]:
            raise ComputerUseError(ERR_STALE_TOKEN.format(index=rec.index))
        token, identity = entry[1][rec.index]
        if identity != _identity(rec):
            # The window was walked again (by any session) and this index now
            # names another element: refuse rather than act on it.
            raise ComputerUseError(ERR_STALE_TOKEN.format(index=rec.index))
        return token

    def _act(self, verb: str, tool: str, args: Mapping[str, Any], *, element: bool) -> DriverResult:
        result = self._call(tool, args, timeout=ACTION_CALL_TIMEOUT_SECS)
        sc = result.get("structuredContent") or {}
        text = _text_of(result)
        if result.get("isError"):
            code = str(sc.get("code") or (sc.get("refusal") or {}).get("code") or "")
            if code == "background_unavailable":
                detail = str(sc.get("detail") or "no focus-free input route")
                hint = HINT_ELEMENT if not element else ""
                if tool in ("click", "drag"):
                    hint = HINT_FOREGROUND
                return DriverResult(
                    ok=False,
                    text=ERR_BACKGROUND_UNAVAILABLE.format(verb=verb, detail=detail) + hint,
                )
            if code == "stale_element_token":
                self._drop_tokens()
            return DriverResult(ok=False, text=text or f"the driver refused the {verb}")
        effect = str(sc.get("effect") or "")
        if effect == "none":
            return DriverResult(ok=False, text=ERR_ACTION_NONE.format(verb=verb))
        summary = str(sc.get("summary") or text or f"{verb} sent")
        if effect != "confirmed":
            summary = f"{summary} ({NOTE_UNVERIFIED.format(verb=verb)})"
        return DriverResult(ok=True, text=summary)

    def _delivery(self, method: str) -> str:
        return "foreground" if method == CLICK_METHOD_GLOBAL else "background"

    def _capture_frame(
        self, app: AppRef, points: "tuple[tuple[float, float], ...]" = ()
    ) -> "tuple[float, float, float, float, float]":
        """Read the window with a screenshot so x/y can be grounded; return bounds + scale."""
        fd, shot_path = tempfile.mkstemp(prefix="ground-", suffix=".png", dir=self._shot_dir())
        os.close(fd)
        try:
            args = {
                "pid": app.pid,
                "window_id": app.window_id,
                "include_screenshot": True,
                "include_accessibility_tree": False,
                "max_image_dimension": 0,
                "screenshot_out_file": shot_path,
            }
            result = self._call(
                "get_window_state", args, timeout=SNAPSHOT_CALL_TIMEOUT_SECS, idempotent=True
            )
        finally:
            try:
                os.unlink(shot_path)
            except OSError:
                pass
        if result.get("isError"):
            raise ComputerUseError(_text_of(result) or "the driver could not read the window")
        sc = result.get("structuredContent") or {}
        bounds = _bounds(sc.get("window_bounds")) or self.window_bounds(app)
        if bounds is None:
            raise ComputerUseError(floors.ERR_POINT_UNCONFINED.format(app=app.name))
        # The floor above the seam checked *points* against the last bounds it
        # saw; the window may have moved or shrunk since. Check again against
        # the bounds this gesture is mapped through, so a foreground gesture can
        # never land in another application.
        refusal = floors.check_point_in_window(app, bounds, points)
        if refusal is not None:
            raise ComputerUseError(refusal)
        with self._lock:
            self._bounds[(app.pid, app.window_id)] = bounds
        width = sc.get("screenshot_width")
        scale = (
            float(width) / bounds[2] if isinstance(width, (int, float)) and bounds[2] > 0 else 1.0
        )
        self._drop_tokens()
        return (*bounds, scale)

    @staticmethod
    def _local(
        frame: "tuple[float, float, float, float, float]", point: "tuple[float, float]"
    ) -> dict[str, float]:
        x, y, _w, _h, scale = frame
        return {"x": round((point[0] - x) * scale, 2), "y": round((point[1] - y) * scale, 2)}

    def click(self, app: AppRef, rec: "ElementRec | None", req: ClickRequest) -> DriverResult:
        def run() -> DriverResult:
            if req.method == CLICK_METHOD_SKY_CLICK:
                return DriverResult(ok=False, text=ERR_SKY_CLICK)
            args: dict[str, Any] = {"pid": app.pid, "button": req.button, "count": req.count}
            if rec is not None and req.point is None:
                args["element_token"] = self._token(app, rec)
                return self._act("click", "click", args, element=True)
            if req.point is None:
                return DriverResult(ok=False, text="a coordinate click needs x and y")
            if req.method not in (CLICK_METHOD_APP_POST, CLICK_METHOD_GLOBAL):
                return DriverResult(
                    ok=False, text=f"click_method '{req.method}' needs an element_index"
                )
            frame = self._capture_frame(app, (req.point,))
            args.update(self._local(frame, req.point))
            args["window_id"] = app.window_id
            args["delivery_mode"] = self._delivery(req.method)
            return self._act("click", "click", args, element=False)

        return self._guard("click", run)

    def drag(self, app: AppRef, req: DragRequest) -> DriverResult:
        def run() -> DriverResult:
            if req.method == CLICK_METHOD_SKY_CLICK:
                return DriverResult(ok=False, text=ERR_SKY_CLICK)
            if req.method == CLICK_METHOD_ACCESSIBILITY:
                return DriverResult(ok=False, text="a drag has no accessibility form; use x/y")
            if req.path != DRAG_PATH_STRAIGHT:
                # The upstream drag takes endpoints and a step count only; a curved
                # stroke would silently become a straight one. Refuse before input.
                return DriverResult(ok=False, text=ERR_CURVED_DRAG.format(path=req.path))
            frame = self._capture_frame(app, (req.start, req.end))
            start = self._local(frame, req.start)
            end = self._local(frame, req.end)
            args = {
                "pid": app.pid,
                "window_id": app.window_id,
                "from_x": start["x"],
                "from_y": start["y"],
                "to_x": end["x"],
                "to_y": end["y"],
                "button": req.button,
                "steps": max(1, int(req.steps)),
                "delivery_mode": self._delivery(req.method),
            }
            return self._act("drag", "drag", args, element=False)

        return self._guard("drag", run)

    def type_text(self, app: AppRef, rec: "ElementRec | None", text: str) -> DriverResult:
        def run() -> DriverResult:
            if rec is None:
                return DriverResult(
                    ok=False, text=floors.ERR_KEYS_NEED_ELEMENT.format(verb="computer_type_text")
                )
            args = {"pid": app.pid, "element_token": self._token(app, rec), "text": text}
            return self._act("typing", "type_text", args, element=True)

        return self._guard("type_text", run)

    def _key_args(self, key: str) -> dict[str, Any]:
        spec = keymap.parse_spec(key)
        name = _KEY_NAMES.get(spec.key)
        if name is None and len(spec.key) == 1 and spec.key.isascii() and spec.key.isprintable():
            name = spec.key
        if name is None:
            raise ComputerUseError(ERR_UNSUPPORTED_KEY.format(key=key))
        table = _MODIFIERS.get(self._os, _MODIFIERS[OS_LINUX])
        mods = []
        for mod in sorted(spec.modifiers):
            mapped = table.get(mod)
            if mapped is None:
                raise ComputerUseError(ERR_UNSUPPORTED_KEY.format(key=key))
            mods.append(mapped)
        args: dict[str, Any] = {"key": name}
        if mods:
            args["modifiers"] = mods
        return args

    def press_key(self, app: AppRef, rec: "ElementRec | None", key: str) -> DriverResult:
        def run() -> DriverResult:
            if rec is None:
                return DriverResult(
                    ok=False, text=floors.ERR_KEYS_NEED_ELEMENT.format(verb="computer_press_key")
                )
            args = {"pid": app.pid, "element_token": self._token(app, rec), **self._key_args(key)}
            return self._act("key press", "press_key", args, element=True)

        return self._guard("press_key", run)

    def set_value(self, app: AppRef, rec: ElementRec, value: str) -> DriverResult:
        def run() -> DriverResult:
            args = {"pid": app.pid, "element_token": self._token(app, rec), "value": value}
            return self._act("value change", "set_value", args, element=True)

        return self._guard("set_value", run)

    def scroll(self, app: AppRef, rec: ElementRec, direction: str, pages: float) -> DriverResult:
        def run() -> DriverResult:
            args = {
                "pid": app.pid,
                "element_token": self._token(app, rec),
                "direction": direction,
                "by": "page",
                "amount": max(1, int(round(pages))),
            }
            return self._act("scroll", "scroll", args, element=True)

        return self._guard("scroll", run)

    def perform_action(self, app: AppRef, rec: ElementRec, action: str) -> DriverResult:
        def run() -> DriverResult:
            name = action.strip().lower()
            if name in _PRESS_ACTIONS:
                button = "left"
            elif name in _MENU_ACTIONS:
                button = "right"
            else:
                return DriverResult(ok=False, text=ERR_UNSUPPORTED_ACTION.format(action=action))
            args = {"pid": app.pid, "element_token": self._token(app, rec), "button": button}
            return self._act(action, "click", args, element=True)

        return self._guard("perform_action", run)

    def close(self) -> None:
        self._drop_tokens()


__all__ = ["CuaBackend", "REQUIRED_TOOLS", "is_secure_element"]
