"""Linux computer-use backend: AT-SPI 2 for the tree and actions, X11 for the rest.

Scope: an X11 session (including a virtual one such as Amazon DCV's ``Xdcv``) gets
the full tool set. A Wayland session gets the tree-only subset: AT-SPI works there,
but Wayland gives an unprivileged client no window capture and no input injection, so
those verbs refuse with the reason instead of half-working.

The shape follows the Windows driver closely, because the input situation is the
same: X11 has no per-process input route applications honour, so

* **element-addressed actions are the SAFE form**: an AT-SPI ``Action.DoAction``,
  ``EditableText`` write or ``Value`` write runs inside the target application and
  touches neither the operator's pointer nor their keyboard focus. ``auto`` only
  ever resolves to this.
* **the pointer route is opt-in by name** (``click_method: "global"``) and is
  confined to the authorized window by comparing X window IDs at every point.
  ``app_post``/``sky_click`` are refused by name, as on Windows.
* **keyboard verbs take focus and say so**, and refuse when focus did not verifiably
  land on the addressed element, because XTest keystrokes go wherever focus is.
* **the secure-field floor is re-read live** before every write: a role of
  ``password text`` refuses, and a secure node's text is never even requested.

Element indices map to AT-SPI object references through the most recent walk of
the same window (the drift-verification walk the service runs before every
mutating action), and every action re-reads the live node and compares its role and
name with the record the model addressed before doing anything.
"""

from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
import unicodedata
from typing import Callable

from kiro_crew.computer_use import (
    cursor_motion,
    keymap,
    linux_atspi,
    linux_input,
    linux_launch,
    linux_x11,
)
from kiro_crew.computer_use.backend import ComputerUseBackend, run_launch
from kiro_crew.computer_use.linux_atspi import (
    ACTION,
    COMPONENT,
    COORD_SCREEN,
    COORD_WINDOW,
    EDITABLE_TEXT,
    ROLE_PASSWORD_TEXT,
    STATE_ACTIVE,
    STATE_CHECKED,
    STATE_DEFUNCT,
    STATE_EDITABLE,
    STATE_ENABLED,
    STATE_EXPANDED,
    STATE_FOCUSED,
    STATE_HORIZONTAL,
    STATE_MANAGES_DESCENDANTS,
    STATE_PRESSED,
    STATE_READ_ONLY,
    STATE_SELECTED,
    STATE_SENSITIVE,
    STATE_SHOWING,
    STATE_VERTICAL,
    TEXT,
    VALUE,
    CallError,
    Node,
    Ref,
)
from kiro_crew.computer_use.types import (
    CLICK_METHOD_ACCESSIBILITY,
    CLICK_METHOD_APP_POST,
    CLICK_METHOD_AUTO,
    CLICK_METHOD_GLOBAL,
    CLICK_METHOD_SKY_CLICK,
    DRAG_SHAPE_NOTE,
    ERR_APP_NOT_FOUND,
    ERR_POINT_REQUIRED,
    ERR_UNKNOWN_CLICK_METHOD,
    MOUSE_BUTTON_LEFT,
    MOUSE_BUTTON_RIGHT,
    PERMISSION_GRANTED,
    PERMISSION_MISSING,
    PERMISSION_UNSUPPORTED,
    PLATFORM_LINUX,
    REFUSAL_ACCESSIBILITY_NEEDS_INDEX,
    REFUSAL_SECURE_TARGET,
    TOOL_LIST_APPS,
    AppRef,
    BackendStatus,
    ClickRequest,
    ComputerUseError,
    DragRequest,
    DriverResult,
    ElementRec,
    LaunchIdentity,
    PermissionProbe,
    Snapshot,
    SnapshotRequest,
)

logger = logging.getLogger(__name__)

MAX_CHILDREN_PER_NODE = 400
#: How long a focus request may take to be reflected in the element's state.
FOCUS_SETTLE_SECS = 0.6
_FOCUS_POLL_SECS = 0.05

WAYLAND_INPUT_REASON = (
    "this is a Wayland session, which gives an unprivileged client no route for synthetic "
    "{what}; {verb} is unavailable here. Act on an element by index instead "
    "(computer_click / computer_set_value / computer_type_text / computer_scroll / "
    "computer_perform_action all work through the accessibility tree)"
)

#: AT-SPI roles a top-level window has, for the Wayland window list.
_TOPLEVEL_ROLES = frozenset({"frame", "window", "dialog", "alert", "file chooser"})


def wayland_session() -> bool:
    """Whether the operator's desktop is a Wayland session.

    An explicit ``KIROCREW_CU_DISPLAY`` pins the X11 route. Otherwise
    ``XDG_SESSION_TYPE`` decides when it is set; a gateway started as a service often
    has neither it nor ``WAYLAND_DISPLAY``, so a live ``wayland-*`` socket in this
    user's runtime dir is the last signal. Xwayland's ``DISPLAY`` does NOT count as
    X11: through it the driver would see only the few X clients and none of the
    native Wayland windows, and its capture and input would reach nothing else.
    """
    if os.environ.get("KIROCREW_CU_DISPLAY"):
        return False
    kind = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if kind:
        return kind == "wayland"
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{linux_x11._uid()}"
    try:
        return any(
            n.startswith("wayland-") and not n.endswith(".lock") for n in os.listdir(runtime)
        )
    except OSError:
        return False


# ── refusals (each names the working alternative) ──
_REFUSE_APP_POST = (
    "click_method 'app_post' is macOS-only: X11 has no per-process mouse route that "
    "applications honour. Use an element_index (no pointer is moved), or name "
    "click_method 'global' to move the real cursor"
)
_REFUSE_SKY_CLICK = (
    "click_method 'sky_click' is macOS-only. Use an element_index, or name " "click_method 'global'"
)
_REFUSE_AUTO_POINT = (
    "click_method 'auto' with coordinates cannot be served on Linux: the only "
    "coordinate route (XTest) moves the operator's real cursor, and 'auto' never "
    "resolves onto a pointer-moving method. Pass an element_index instead, or name "
    "click_method 'global' explicitly to accept the cursor move"
)
_REFUSE_DRAG_METHOD = (
    "dragging on Linux moves the operator's real cursor, so it must be requested "
    "explicitly: pass click_method 'global'. The default '{method}' is the macOS "
    "app-scoped route, which has no X11 equivalent"
)
_REFUSE_ELEMENT_BUTTON = (
    "a {button}-button click cannot be delivered to element {index} in {app}: it "
    "advertises no menu action. Name click_method 'global' with coordinates to accept "
    "the cursor move"
)
_ERR_NO_ACTION = (
    "element {index} in {app} advertises no action this platform can perform. Try a "
    "parent or child element from computer_get_state"
)
_ERR_CAPS_LOCK = (
    "computer_type_text refused: Caps Lock is on, so synthetic key presses would flip "
    "the case of every letter. Ask the user to turn Caps Lock off, or target an editable "
    "text field, which takes the text directly"
)
_ERR_CONTROL_CHAR = (
    "computer_type_text refused: the text contains the control character {char}, which "
    "could move keyboard focus to another element mid-string. Type the text without it "
    "and send navigation keys (Tab, Return, Escape) with computer_press_key"
)
_ERR_ACTION_FAILED = "{verb} on element {index} in {app} failed ({detail})"
_ERR_POINT_NOT_OWNED = (
    "the point ({x}, {y}) is not on {app}'s own window (another window is on top of "
    "it, it is the window frame, or the window is not on screen), so a real-cursor "
    "click there would land somewhere else. Address the control by element_index "
    "instead (no pointer is moved), or bring the window to the front first"
)
_ERR_ELEMENT_GONE = (
    "element {index} is no longer in {app}'s tree. Call computer_get_state to re-read it"
)
_ERR_ELEMENT_CHANGED = (
    "element {index} in {app} is now {now}, not the {was} you addressed. Call "
    "computer_get_state again"
)
_ERR_NO_WALK = "no current tree for {app}. Call computer_get_state first"
_ERR_FOCUS_NOT_HELD = (
    "element {index} in {app} did not take keyboard focus, so no keystrokes were "
    "sent. Address the control that actually accepts typing, or use "
    "computer_set_value which needs no focus"
)
_ERR_NO_VALUE = (
    "element {index} in {app} has no settable value. Use computer_type_text to send "
    "keystrokes to it instead"
)
_ERR_READ_ONLY = "element {index} in {app} is read-only"
_ERR_NO_SCROLL = (
    "element {index} in {app} has no scroll bar on that axis. Scroll a parent "
    "container, or use computer_press_key with pagedown/pageup on a focusable element"
)
_ERR_UNKNOWN_ACTION = "unknown action {action!r} for element {index}. It advertises: {supported}"
_ERR_UNKNOWN_DIRECTION = "unknown scroll direction {direction!r}. Supported: up, down, left, right"
_ERR_NO_TREE = (
    "{app} exposes no accessibility tree. It was probably started before "
    "accessibility was enabled on this session: restart the application (Chromium "
    "and Electron apps additionally need --force-renderer-accessibility)"
)
_ERR_WINDOW_GONE = "the window of {app} is no longer on screen. Call {tool} again"
_FOCUS_NOTE = " (keyboard focus moved to it; X11 has no per-process key route)"

#: AT-SPI role names -> the compact role rendered to the model. Anything not listed
#: renders as its AT-SPI name with spaces removed.
_ROLE_MAP: dict[str, str] = {
    "frame": "window",
    "push button": "button",
    "push button menu": "menubutton",
    "toggle button": "togglebutton",
    "check box": "checkbox",
    "radio button": "radiobutton",
    "check menu item": "menuitem",
    "radio menu item": "menuitem",
    "menu item": "menuitem",
    "menu bar": "menubar",
    "popup menu": "menu",
    "tool bar": "toolbar",
    "page tab": "tab",
    "page tab list": "tablist",
    "combo box": "combobox",
    "spin button": "spinbutton",
    "text": "textfield",
    "entry": "textfield",
    "password text": "textfield",
    "table cell": "cell",
    "table row": "row",
    "table column header": "columnheader",
    "column header": "columnheader",
    "list item": "listitem",
    "list box": "list",
    "tree item": "treeitem",
    "tree table": "treetable",
    "scroll pane": "scrollarea",
    "scroll bar": "scrollbar",
    "split pane": "splitgroup",
    "status bar": "statusbar",
    "document web": "webarea",
    "document frame": "document",
    "link": "link",
}

#: Structural wrappers dropped from the numbering when they carry no name, value or
#: action. Their children stay, at their own depth.
_ELIDABLE = frozenset(
    {"filler", "panel", "section", "unknown", "redundant object", "glass pane",
     "root pane", "layered pane", "viewport", "grouping", "invalid", "static"}
)  # fmt: skip

#: Raw AT-SPI roles whose Value interface carries a number worth showing.
_RANGE_ROLES = frozenset(
    {"scroll bar", "slider", "spin button", "progress bar", "level bar", "dial"}
)

#: Action names, in order of preference, for a left-button "press this control".
_PRESS_ACTIONS = ("click", "press", "activate", "jump", "toggle", "open", "select")
_MENU_ACTIONS = ("showcontextmenu", "showmenu", "show menu", "menu", "contextmenu", "context menu")

# Control characters plus U+FFFC, the "embedded object" placeholder Chromium and
# WebKit put in a container's text for every child element.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\ufffc\ue000-\uf8ff]")

#: Actions Chromium attaches to nearly every node. They say nothing about what the
#: node is, so they are not rendered and do not keep an unnamed node in the tree
#: (the live action list still carries them, so a right-click by element works).
_GENERIC_ACTIONS = frozenset({"clickancestor", "showcontextmenu", "dodefault"})


def _meaningful_actions(actions: "tuple[str, ...]") -> tuple[str, ...]:
    return tuple(a for a in actions if a and a.lower() not in _GENERIC_ACTIONS)


def _focus_moving_char(text: str) -> str:
    """The first control character in *text*, rendered for an error, or ``""``."""
    for char in text:
        if unicodedata.category(char) == "Cc":
            return repr(char)
    return ""


def _is_secure(node: Node) -> bool:
    """Fail closed: a node whose role could not be read may be a password entry."""
    return node.role_unknown or node.role_id == ROLE_PASSWORD_TEXT or "password" in node.role


def _compact_role(role: str) -> str:
    return _ROLE_MAP.get(role, role.replace(" ", "") or "unknown")


def _clean(text: str, limit: int) -> str:
    text = _CONTROL.sub(" ", text or "").strip()
    return text[:limit] + "…" if len(text) > limit else text


def _valid_extents(ext: "tuple[int, int, int, int] | None") -> bool:
    if ext is None:
        return False
    x, y, w, h = ext
    return w > 0 and h > 0 and x > -1_000_000 and y > -1_000_000


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _guarded(label: str, run: "Callable[[], DriverResult]") -> DriverResult:
    """No exception crosses the backend seam (see the Windows driver)."""
    try:
        return run()
    except ComputerUseError as exc:
        return DriverResult(ok=False, text=str(exc))
    except CallError as exc:
        return DriverResult(
            ok=False, text=f"{label} failed: the application did not answer ({exc.name})"
        )
    except Exception as exc:
        logger.warning("computer-use %s failed: %s", label, exc, exc_info=True)
        return DriverResult(
            ok=False, text=f"{label} failed unexpectedly ({type(exc).__name__}: {exc})"
        )


def _comm(pid: int) -> str:
    return linux_x11.read_comm(pid)


def _app_ref(info: linux_x11.WindowInfo) -> AppRef:
    return AppRef(
        name=info.wm_class or _comm(info.pid) or info.wm_instance or "unknown",
        pid=info.pid,
        bundle_id="",
        window_id=info.xid,
        window_title=info.title,
    )


class LinuxBackend(ComputerUseBackend):
    """AT-SPI 2 + X11 driver."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: window_key -> AT-SPI ref of each element, by element index
        self._walks: dict[str, list[Ref]] = {}
        #: Wayland has no window ids a client can see, so each AT-SPI top-level frame
        #: gets a synthetic one, stable for this process's lifetime.
        self._wl_ids: dict[Ref, int] = {}
        self._wl_refs: dict[int, Ref] = {}
        self.wayland = wayland_session()
        linux_atspi.default_coord_type = COORD_WINDOW if self.wayland else COORD_SCREEN
        linux_atspi.client().coord_type = linux_atspi.default_coord_type

    @property
    def platform_id(self) -> str:
        return PLATFORM_LINUX

    # ── status ──

    def _unavailable_reason(self) -> str:
        if not self.wayland:
            ok, why = linux_x11.available()
            if not ok:
                return f"no usable X11 display ({why})"
        ok, why = linux_atspi.client().available()
        if not ok:
            return why
        return ""

    def status(self) -> BackendStatus:
        reason = self._unavailable_reason()
        return BackendStatus(supported=not reason, platform_id=PLATFORM_LINUX, reason=reason)

    def probe_permissions(self) -> PermissionProbe:
        """No TCC-style grants exist on X11: any client of the display may read it."""
        ok, _ = linux_atspi.client().available()
        return PermissionProbe(
            accessibility=PERMISSION_GRANTED if ok else PERMISSION_MISSING,
            screen_recording=PERMISSION_UNSUPPORTED,
            responsible_hint="",
        )

    # ── apps ──

    def _windows(self) -> list[linux_x11.WindowInfo]:
        return [w for w in linux_x11.client_windows() if w.viewable or w.hidden]

    def _wl_id(self, ref: Ref) -> int:
        with self._lock:
            wid = self._wl_ids.get(ref)
            if wid is None:
                wid = len(self._wl_ids) + 1
                self._wl_ids[ref] = wid
                self._wl_refs[wid] = ref
            return wid

    def _wayland_windows(self) -> list[AppRef]:
        """Showing top-level frames from the accessibility registry, active one first."""
        client = linux_atspi.client()
        found: list[tuple[bool, AppRef]] = []
        for root, pid, name in client.applications():
            node = Node(ref=root, depth=0)
            client.read_basic([node])
            if node.failed:
                continue
            kids = [Node(ref=k, depth=1) for k in node.children]
            client.read_basic(kids)
            for kid in kids:
                if kid.failed or kid.role not in _TOPLEVEL_ROLES:
                    continue
                if STATE_SHOWING not in kid.states:
                    continue
                app = AppRef(
                    name=name or _comm(pid) or "unknown",
                    pid=pid,
                    window_id=self._wl_id(kid.ref),
                    window_title=_clean(kid.name, 500),
                )
                found.append((STATE_ACTIVE in kid.states, app))
        found.sort(key=lambda pair: 0 if pair[0] else 1)
        # Chromium keeps invisible helper frames "showing" with no name; drop them
        # when the same process also has a named window the model can recognise.
        named = {app.pid for _active, app in found if app.window_title}
        return [app for _active, app in found if app.window_title or app.pid not in named]

    def list_apps(self) -> DriverResult:
        if self.wayland:
            return _guarded(
                "list_apps", lambda: DriverResult(ok=True, apps=tuple(self._wayland_windows()))
            )
        return _guarded(
            "list_apps",
            lambda: DriverResult(ok=True, apps=tuple(_app_ref(w) for w in self._windows())),
        )

    def _resolve_wayland(self, query: str) -> AppRef:
        wanted = _norm(query)
        apps = self._wayland_windows()
        if wanted:
            for matcher in (
                lambda a: wanted in (_norm(a.name), _norm(_comm(a.pid))),
                lambda a: _norm(a.window_title) == wanted,
                lambda a: wanted in _norm(a.window_title),
            ):
                hits = [a for a in apps if matcher(a)]
                if hits:
                    return hits[0]
        raise ComputerUseError(ERR_APP_NOT_FOUND.format(query=query, tool=TOOL_LIST_APPS))

    def _resolve(self, query: str) -> AppRef:
        if self.wayland:
            return self._resolve_wayland(query)
        wanted = _norm(query)
        windows = self._windows()
        active = linux_x11.active_window()
        windows.sort(key=lambda w: 0 if w.xid == active else 1)  # stable: keeps stacking order
        if wanted:
            for matcher in (
                lambda w: wanted in (_norm(w.wm_class), _norm(w.wm_instance), _norm(_comm(w.pid))),
                lambda w: _norm(w.title) == wanted,
                lambda w: wanted in _norm(w.title) or wanted in _norm(w.wm_instance),
            ):
                hits = [w for w in windows if matcher(w)]
                if hits:
                    return _app_ref(hits[0])
        raise ComputerUseError(ERR_APP_NOT_FOUND.format(query=query, tool=TOOL_LIST_APPS))

    def resolve_app(self, query: str) -> DriverResult:
        return _guarded("resolve_app", lambda: DriverResult(ok=True, app=self._resolve(query)))

    def _find_window_for(self, display: str) -> "AppRef | None":
        keys = linux_launch.match_keys(display)
        if self.wayland:
            for app in self._wayland_windows():
                own = {_norm(app.name), _norm(_comm(app.pid))} - {""}
                if keys & own or _norm(display) and _norm(display) in _norm(app.window_title):
                    return app
            return None
        for w in self._windows():
            own = {_norm(w.wm_class), _norm(w.wm_instance), _norm(_comm(w.pid))} - {""}
            if keys & own or _norm(display) and _norm(display) in _norm(w.title):
                return _app_ref(w)
        return None

    def launch_app(
        self,
        query: str,
        *,
        permit: "Callable[[LaunchIdentity], str | None] | None" = None,
        refuse_launched: "Callable[[AppRef], str | None] | None" = None,
    ) -> DriverResult:
        def run() -> DriverResult:
            return run_launch(
                query,
                resolve=linux_launch.resolve_target,
                find=self._find_window_for,
                spawn=linux_launch.spawn_detached,
                permit=permit,
                identity=linux_launch.target_identity,
                refuse_launched=refuse_launched,
            )

        return _guarded("launch_app", run)

    # ── tree ──

    def _frame_for(self, app: AppRef) -> Ref:
        """The AT-SPI frame that IS *app*'s window."""
        client = linux_atspi.client()
        if self.wayland:
            with self._lock:
                ref = self._wl_refs.get(app.window_id)
            if ref is None:
                raise ComputerUseError(_ERR_WINDOW_GONE.format(app=app.name, tool=TOOL_LIST_APPS))
            return ref
        roots = [ref for ref, pid, _name in client.applications() if pid == app.pid]
        if not roots:
            raise ComputerUseError(_ERR_NO_TREE.format(app=app.name))
        frames: list[Node] = []
        for root in roots:
            node = Node(ref=root, depth=0)
            client.read_basic([node])
            kids = [Node(ref=k, depth=1) for k in node.children]
            client.read_basic(kids)
            frames.extend(k for k in kids if not k.failed)
        if not frames:
            raise ComputerUseError(_ERR_NO_TREE.format(app=app.name))
        title = (app.window_title or "").strip()
        by_title = [f for f in frames if title and f.name.strip() == title]
        if len(by_title) == 1:
            return by_title[0].ref
        bounds = linux_x11.window_bounds(app.window_id)
        candidates = by_title or frames
        if bounds is not None and len(candidates) > 1:
            client.read_details(candidates, text_limit=0, skip_text=set(range(len(candidates))))

            def overlap(node: Node) -> float:
                if not _valid_extents(node.extents):
                    return -1.0
                x, y, w, h = node.extents  # type: ignore[misc]
                bx, by, bw, bh = bounds
                dx = max(0.0, min(x + w, bx + bw) - max(x, bx))
                dy = max(0.0, min(y + h, by + bh) - max(y, by))
                return dx * dy

            candidates.sort(key=overlap, reverse=True)
        return candidates[0].ref

    def _walk(self, app: AppRef, req: SnapshotRequest) -> Snapshot:
        info = None if self.wayland else linux_x11.window_info(app.window_id)
        if info is None and not self.wayland:
            raise ComputerUseError(_ERR_WINDOW_GONE.format(app=app.name, tool=TOOL_LIST_APPS))
        client = linux_atspi.client()
        root = Node(ref=self._frame_for(app), depth=0)
        kept: list[Node] = []
        frontier = [root]
        truncated = depth_truncated = False
        while frontier:
            client.read_basic(frontier, child_limit=MAX_CHILDREN_PER_NODE)
            next_frontier: list[Node] = []
            for node in frontier:
                if node.failed:
                    # An unreadable node could be a password entry: the walk cannot
                    # vouch for the window, so it loses its screenshot.
                    truncated = True
                    continue
                if STATE_DEFUNCT in node.states:
                    continue
                if node.children_failed:
                    truncated = True
                if node.depth > 0 and STATE_SHOWING not in node.states:
                    continue  # off-screen or collapsed: not something the model can act on
                if len(kept) >= req.max_nodes:
                    truncated = True
                    continue
                kept.append(node)
                if not node.children:
                    continue
                if node.depth + 1 > req.max_depth:
                    depth_truncated = True
                    continue
                kids = node.children
                if node.children_capped and STATE_MANAGES_DESCENDANTS not in node.states:
                    # A partial child list of an ORDINARY container is a real cut: a
                    # password field could sit beyond it, so the window loses its
                    # screenshot exactly like a node-budget truncation. A container that
                    # MANAGES its descendants (a spreadsheet, a large tree view) creates
                    # cells on demand and never holds an entry field among them, so its
                    # bounded read is not reported as a truncated walk.
                    truncated = True
                next_frontier.extend(Node(ref=k, depth=node.depth + 1) for k in kids)
            frontier = next_frontier
        secure_positions = {i for i, n in enumerate(kept) if _is_secure(n)}
        client.read_details(kept, text_limit=req.text_limit, skip_text=secure_positions)

        # Pre-order assembly over the kept set, so indentation is containment.
        by_ref = {n.ref: n for n in kept}
        if info is not None:
            ox, oy = float(info.x), float(info.y)
            width, height, win_title = float(info.width), float(info.height), info.title
        else:
            # Wayland: extents are already window-relative, and the window's own
            # frame says how big it is. Its position on screen is unknowable.
            ox = oy = 0.0
            width = height = 0.0
            if _valid_extents(root.extents):
                width, height = float(root.extents[2]), float(root.extents[3])  # type: ignore[index]
            win_title = ""
        records: list[ElementRec] = []
        refs: list[Ref] = []
        focused_index = -1
        focused_node: "Node | None" = None
        # Each stack entry carries the RENDERED depth: an elided wrapper passes its own
        # depth through to its children, so indentation is containment among the
        # records the model sees, not among AT-SPI nodes it never sees.
        stack: list[tuple[Node, int]] = [(root, 0)]
        while stack:
            node, depth = stack.pop()
            secure = _is_secure(node)
            title = "" if secure else _clean(node.name, req.text_limit)
            value = ""
            if not secure:
                if node.text and _clean(node.text, req.text_limit) != title:
                    value = _clean(node.text, req.text_limit)
                elif (
                    node.value is not None
                    and VALUE in node.interfaces
                    and TEXT not in node.interfaces
                    and node.role in _RANGE_ROLES
                ):
                    # Only range controls carry a meaningful number. A node that also
                    # speaks Text has said what it shows (an empty Calc cell reports 0),
                    # and LibreOffice exposes Value on every toolbar button, always 0.
                    value = f"{node.value:g}"
            child_nodes = [by_ref[k] for k in node.children if k in by_ref]
            elide = (
                not secure
                and node.depth > 0
                and not title
                and not value
                and (
                    # An unnamed wrapper is dropped when it has no action, or when it
                    # has children to carry the structure: Chromium gives nearly every
                    # div a generic click action, and keeping those would bury the real
                    # controls under hundreds of anonymous "panel" lines.
                    (
                        node.role in _ELIDABLE
                        and (not _meaningful_actions(node.actions) or bool(child_nodes))
                    )
                    or (
                        not _meaningful_actions(node.actions)
                        and child_nodes
                        and node.role in ("static", "section", "image")
                    )
                    # Unnamed AND unplaced: nothing a model could recognise or aim at
                    # (GTK tree views expose one per renderer cell).
                    or (not _valid_extents(node.extents) and not child_nodes)
                )
            )
            if elide:
                stack.extend((kid, depth) for kid in reversed(child_nodes))
                continue
            stack.extend((kid, depth + 1) for kid in reversed(child_nodes))
            traits: list[str] = []
            if not secure:
                if STATE_SELECTED in node.states:
                    traits.append("selected")
                if STATE_EXPANDED in node.states:
                    traits.append("expanded")
                if STATE_CHECKED in node.states:
                    traits.append("checked")
                if STATE_PRESSED in node.states:
                    traits.append("pressed")
                if STATE_EDITABLE in node.states and STATE_READ_ONLY not in node.states:
                    traits.append("editable")
            frame = None
            if not secure and _valid_extents(node.extents):
                x, y, w, h = node.extents  # type: ignore[misc]
                frame = (float(x) - ox, float(y) - oy, float(w), float(h))
            index = len(records)
            focused = STATE_FOCUSED in node.states
            if focused:
                focused_index, focused_node = index, node
            records.append(
                ElementRec(
                    index=index,
                    role=_compact_role(node.role),
                    subrole="AXSecureTextField" if secure else "",
                    title=title,
                    value=value,
                    actions=() if secure else _meaningful_actions(node.actions),
                    depth=depth,
                    secure=secure,
                    enabled=STATE_ENABLED in node.states or STATE_SENSITIVE in node.states,
                    frame=frame,
                    traits=tuple(traits),
                    focused=False,
                )
            )
            refs.append(node.ref)
        if focused_index >= 0:
            from dataclasses import replace

            records[focused_index] = replace(records[focused_index], focused=True)
        selected = ""
        if (
            focused_node is not None
            and not _is_secure(focused_node)
            and TEXT in focused_node.interfaces
        ):
            selected = self._selected_text(focused_node.ref, req.text_limit)
        with self._lock:
            self._walks[app.window_key] = refs
        return Snapshot(
            app=app,
            elements=tuple(records),
            window_title=win_title or app.window_title,
            captured_at=time.monotonic(),
            truncated=truncated,
            depth_truncated=depth_truncated,
            has_secure=bool(secure_positions),
            window_bounds=(ox, oy, width, height),
            selected_text=selected,
            walk_budget=req,
        )

    @staticmethod
    def _selected_text(ref: Ref, limit: int) -> str:
        client = linux_atspi.client()
        try:
            count = int(client.call(ref, TEXT, "GetNSelections")[0])
            if count <= 0:
                return ""
            start, end = client.call(ref, TEXT, "GetSelection", "i", (0,))
            if end <= start:
                return ""
            text = client.call(
                ref, TEXT, "GetText", "ii", (int(start), int(min(end, start + limit + 1)))
            )[0]
            return _clean(str(text), limit)
        except CallError:
            return ""

    @staticmethod
    def _attach_image(snap: Snapshot, req: SnapshotRequest) -> Snapshot:
        """Capture and encode the window, honouring the same floors as macOS/Windows."""
        from dataclasses import replace

        if snap.has_secure or snap.truncated or snap.depth_truncated or not snap.app.window_id:
            return snap
        try:
            captured = linux_x11.capture_window(snap.app.window_id)
            if captured is None:
                return snap
            raw, width, height, stride = captured
            from PIL import Image

            image = Image.frombuffer("RGB", (width, height), raw, "raw", "BGRX", stride, 1)
            longest = max(width, height)
            if longest > req.image_max_px:
                scale = req.image_max_px / float(longest)
                image = image.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    Image.Resampling.LANCZOS,
                )
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=int(req.image_quality))
            attached = replace(
                snap, image_jpeg=buf.getvalue(), image_width=image.width, image_height=image.height
            )
        except Exception:
            logger.debug("computer-use X11 capture failed", exc_info=True)
            return snap
        try:
            from kiro_crew.computer_use import screencast

            screencast.emit_snapshot_frame(attached)
        except Exception:
            logger.debug("computer-use live-view relay failed", exc_info=True)
        return attached

    def snapshot(self, app: AppRef, req: SnapshotRequest) -> DriverResult:
        def run() -> DriverResult:
            snap = self._walk(app, req)
            if req.want_image and not self.wayland:
                # Wayland: no unprivileged capture, so the snapshot is tree-only, which
                # the contract allows (want_image is a request, not a requirement).
                snap = self._attach_image(snap, req)
            return DriverResult(ok=True, app=app, snapshot=snap)

        return _guarded("snapshot", run)

    # ── element resolution ──

    def _live(self, app: AppRef, rec: ElementRec, *, for_write: bool = False) -> Node:
        label = app.bundle_id or app.name
        with self._lock:
            walk = self._walks.get(app.window_key)
        if walk is None:
            raise ComputerUseError(_ERR_NO_WALK.format(app=label))
        refs = walk
        if rec.index >= len(refs):
            raise ComputerUseError(_ERR_ELEMENT_GONE.format(index=rec.index, app=label))
        client = linux_atspi.client()
        node = Node(ref=refs[rec.index], depth=rec.depth)
        client.read_basic([node])
        if node.failed or STATE_DEFUNCT in node.states:
            raise ComputerUseError(_ERR_ELEMENT_GONE.format(index=rec.index, app=label))
        secure = _is_secure(node)
        if secure or rec.secure:
            if for_write:
                raise ComputerUseError(
                    REFUSAL_SECURE_TARGET.format(
                        index=rec.index, app=label, subrole="AXSecureTextField"
                    )
                )
        elif _compact_role(node.role) != rec.role or not _clean(node.name, 100000).startswith(
            rec.title.rstrip("…")
        ):
            raise ComputerUseError(
                _ERR_ELEMENT_CHANGED.format(
                    index=rec.index,
                    app=label,
                    now=f"{_compact_role(node.role)} {node.name!r}",
                    was=f"{rec.role} {rec.title!r}",
                )
            )
        client.read_details([node], text_limit=0, skip_text={0})
        return node

    # ── click ──

    def click(self, app: AppRef, rec: "ElementRec | None", req: ClickRequest) -> DriverResult:
        def run() -> DriverResult:
            if req.method == CLICK_METHOD_ACCESSIBILITY:
                if rec is None:
                    return DriverResult(ok=False, text=REFUSAL_ACCESSIBILITY_NEEDS_INDEX)
                return self._press(app, rec, req.button)
            if req.method == CLICK_METHOD_APP_POST:
                return DriverResult(ok=False, text=_REFUSE_APP_POST)
            if req.method == CLICK_METHOD_SKY_CLICK:
                return DriverResult(ok=False, text=_REFUSE_SKY_CLICK)
            if req.method == CLICK_METHOD_AUTO:
                return DriverResult(ok=False, text=_REFUSE_AUTO_POINT)
            if req.method == CLICK_METHOD_GLOBAL:
                if req.point is None:
                    return DriverResult(ok=False, text=ERR_POINT_REQUIRED.format(method=req.method))
                return self._click_global(app, req)
            return DriverResult(ok=False, text=ERR_UNKNOWN_CLICK_METHOD.format(method=req.method))

        return _guarded("click", run)

    def _do_action(self, node: Node, wanted: "tuple[str, ...]") -> "str | None":
        lowered = [a.lower() for a in node.actions]
        for name in wanted:
            if name in lowered:
                index = lowered.index(name)
                ok = linux_atspi.client().call(node.ref, ACTION, "DoAction", "i", (index,))[0]
                return node.actions[index] if ok else f"!{node.actions[index]}"
        return None

    def _press(self, app: AppRef, rec: ElementRec, button: str) -> DriverResult:
        label = app.bundle_id or app.name
        node = self._live(app, rec)
        if button == MOUSE_BUTTON_LEFT:
            done = self._do_action(node, _PRESS_ACTIONS)
            if done is None and node.actions:
                done = self._do_action(node, (node.actions[0].lower(),))
        elif button == MOUSE_BUTTON_RIGHT:
            done = self._do_action(node, _MENU_ACTIONS)
            if done is None:
                return DriverResult(
                    ok=False,
                    text=_REFUSE_ELEMENT_BUTTON.format(button=button, index=rec.index, app=label),
                )
        else:
            return DriverResult(
                ok=False,
                text=_REFUSE_ELEMENT_BUTTON.format(button=button, index=rec.index, app=label),
            )
        if done is None:
            return DriverResult(ok=False, text=_ERR_NO_ACTION.format(index=rec.index, app=label))
        if done.startswith("!"):
            return DriverResult(
                ok=False,
                text=_ERR_ACTION_FAILED.format(
                    verb=done[1:], index=rec.index, app=label, detail="the application refused it"
                ),
            )
        return DriverResult(ok=True, text=f"pressed element {rec.index} via {done}", app=app)

    def _confine(self, app: AppRef, points: "list[tuple[float, float]]") -> "str | None":
        for x, y in points:
            if not linux_x11.owns_point(app.window_id, x, y):
                return _ERR_POINT_NOT_OWNED.format(
                    app=app.bundle_id or app.name, x=int(x), y=int(y)
                )
        return None

    def _click_global(self, app: AppRef, req: ClickRequest) -> DriverResult:
        assert req.point is not None
        if self.wayland:
            return DriverResult(
                ok=False,
                text=WAYLAND_INPUT_REASON.format(what="pointer input", verb="a coordinate click"),
            )
        x, y = req.point
        refusal = self._confine(app, [(x, y)])
        if refusal:
            return DriverResult(ok=False, text=refusal)
        linux_input.click(x, y, button=req.button, count=req.count)
        return DriverResult(
            ok=True,
            text=f"{req.button}-clicked ({int(x)}, {int(y)}) in {app.bundle_id or app.name} {req.count}x; the real cursor moved",
            app=app,
        )

    def drag(self, app: AppRef, req: DragRequest) -> DriverResult:
        def run() -> DriverResult:
            if req.method != CLICK_METHOD_GLOBAL:
                return DriverResult(ok=False, text=_REFUSE_DRAG_METHOD.format(method=req.method))
            if self.wayland:
                return DriverResult(
                    ok=False,
                    text=WAYLAND_INPUT_REASON.format(what="pointer input", verb="a drag"),
                )
            points = cursor_motion.drag_points(req.start, req.end, steps=req.steps, path=req.path)
            refusal = self._confine(app, list(points))
            if refusal:
                return DriverResult(ok=False, text=refusal)
            linux_input.drag(points, button=req.button)
            shape = (
                "" if len(points) <= 2 else DRAG_SHAPE_NOTE.format(count=len(points), path=req.path)
            )
            return DriverResult(
                ok=True,
                text=(
                    f"dragged from ({int(req.start[0])}, {int(req.start[1])}) to "
                    f"({int(req.end[0])}, {int(req.end[1])}){shape}; the real cursor moved"
                ),
                app=app,
            )

        return _guarded("drag", run)

    # ── keyboard ──

    def _focus(self, app: AppRef, rec: ElementRec) -> "str | None":
        """Move focus to *rec* and VERIFY it holds it. A refusal string, or None.

        Refusing on an unverified focus is the secure-field floor: XTest keystrokes
        go wherever focus is, which may be the password field the policy refused.
        """
        label = app.bundle_id or app.name
        node = self._live(app, rec, for_write=True)
        if linux_x11.active_window() != app.window_id:
            linux_input.activate_window(app.window_id)
        client = linux_atspi.client()
        if COMPONENT in node.interfaces:
            try:
                client.call(node.ref, COMPONENT, "GrabFocus")
            except CallError:
                pass
        deadline = time.monotonic() + FOCUS_SETTLE_SECS
        while True:
            live = client.live(node.ref)
            if not live.failed and STATE_FOCUSED in live.states and not _is_secure(live):
                break
            if time.monotonic() >= deadline:
                return _ERR_FOCUS_NOT_HELD.format(index=rec.index, app=label)
            time.sleep(_FOCUS_POLL_SECS)
        if linux_x11.active_window() != app.window_id:
            return _ERR_FOCUS_NOT_HELD.format(index=rec.index, app=label)
        return None

    @staticmethod
    def _char_count(ref: Ref) -> "int | None":
        try:
            return int(linux_atspi.client().get_prop(ref, TEXT, "CharacterCount"))
        except (CallError, TypeError, ValueError):
            return None

    def type_text(self, app: AppRef, rec: "ElementRec | None", text: str) -> DriverResult:
        def run() -> DriverResult:
            if rec is None:
                return DriverResult(ok=False, text=REFUSAL_ACCESSIBILITY_NEEDS_INDEX)
            node = self._live(app, rec, for_write=True)
            client = linux_atspi.client()
            # Preferred: insert at the caret through EditableText. No focus, no pointer.
            if EDITABLE_TEXT in node.interfaces and STATE_EDITABLE in node.states:
                before = self._char_count(node.ref)
                try:
                    caret = int(client.get_prop(node.ref, TEXT, "CaretOffset"))
                except (CallError, TypeError, ValueError):
                    caret = before if before is not None else 0
                caret = max(0, caret)
                try:
                    client.call(
                        node.ref, EDITABLE_TEXT, "InsertText", "isi", (caret, text, len(text))
                    )
                except CallError:
                    pass
                after = self._char_count(node.ref)
                if before is not None and after is not None and after != before:
                    return DriverResult(
                        ok=True,
                        text=f"inserted {len(text)} character(s) into element {rec.index} at the caret (no focus moved)",
                        app=app,
                    )
            if self.wayland:
                # The keystroke fallback needs synthetic input; InsertText above was the
                # only route, and it did not take (the element is not editable text).
                return DriverResult(
                    ok=False,
                    text=WAYLAND_INPUT_REASON.format(
                        what="keystrokes", verb="typing into a non-text element"
                    ),
                )
            control = _focus_moving_char(text)
            if control:
                # Focus is verified ONCE, on this element. A Tab, newline or Escape in
                # the keystroke stream can move focus mid-string (into a password field
                # the next element over), and every later character would land there
                # unchecked. Navigation goes through computer_press_key, which
                # re-checks the target each time.
                return DriverResult(ok=False, text=_ERR_CONTROL_CHAR.format(char=control))
            if linux_input.caps_lock_conflict(text):
                return DriverResult(ok=False, text=_ERR_CAPS_LOCK)
            refusal = self._focus(app, rec)
            if refusal:
                return DriverResult(ok=False, text=refusal)
            linux_input.type_text(text)
            return DriverResult(
                ok=True,
                text=f"typed {len(text)} character(s) into element {rec.index}{_FOCUS_NOTE}",
                app=app,
            )

        return _guarded("type_text", run)

    def press_key(self, app: AppRef, rec: "ElementRec | None", key: str) -> DriverResult:
        def run() -> DriverResult:
            if rec is None:
                return DriverResult(ok=False, text=REFUSAL_ACCESSIBILITY_NEEDS_INDEX)
            spec = keymap.parse_spec(key)
            if self.wayland:
                return DriverResult(
                    ok=False,
                    text=WAYLAND_INPUT_REASON.format(what="keystrokes", verb="computer_press_key"),
                )
            chord = linux_input.chord(spec.key, spec.modifiers)
            refusal = self._focus(app, rec)
            if refusal:
                return DriverResult(ok=False, text=refusal)
            linux_input.send_chord(chord)
            return DriverResult(ok=True, text=f"sent {key}{_FOCUS_NOTE}", app=app)

        return _guarded("press_key", run)

    def set_value(self, app: AppRef, rec: ElementRec, value: str) -> DriverResult:
        def run() -> DriverResult:
            label = app.bundle_id or app.name
            node = self._live(app, rec, for_write=True)
            client = linux_atspi.client()
            if EDITABLE_TEXT in node.interfaces:
                if STATE_EDITABLE not in node.states or STATE_READ_ONLY in node.states:
                    return DriverResult(
                        ok=False, text=_ERR_READ_ONLY.format(index=rec.index, app=label)
                    )
                ok = client.call(node.ref, EDITABLE_TEXT, "SetTextContents", "s", (value,))[0]
                if not ok:
                    return DriverResult(
                        ok=False,
                        text=_ERR_ACTION_FAILED.format(
                            verb="set value", index=rec.index, app=label, detail="refused"
                        ),
                    )
                return DriverResult(ok=True, text=f"set element {rec.index}", app=app)
            if VALUE in node.interfaces:
                try:
                    number = float(value)
                except ValueError:
                    return DriverResult(
                        ok=False,
                        text=f"element {rec.index} in {label} takes a number, not {value!r}",
                    )
                client.set_prop(node.ref, VALUE, "CurrentValue", "d", number)
                return DriverResult(ok=True, text=f"set element {rec.index} to {number:g}", app=app)
            return DriverResult(ok=False, text=_ERR_NO_VALUE.format(index=rec.index, app=label))

        return _guarded("set_value", run)

    # ── scroll / named actions ──

    def _scrollbars(self, ref: Ref, limit: int = 300) -> list[Node]:
        client = linux_atspi.client()
        found: list[Node] = []
        frontier = [Node(ref=ref, depth=0)]
        seen = 0
        while frontier and seen < limit:
            client.read_basic(frontier)
            seen += len(frontier)
            nxt: list[Node] = []
            for node in frontier:
                if node.failed:
                    continue
                if node.role == "scroll bar" and VALUE in node.interfaces:
                    found.append(node)
                    continue
                if node.depth < 6:
                    nxt.extend(Node(ref=k, depth=node.depth + 1) for k in node.children[:60])
            frontier = nxt
        return found

    def scroll(self, app: AppRef, rec: ElementRec, direction: str, pages: float) -> DriverResult:
        def run() -> DriverResult:
            label = app.bundle_id or app.name
            wanted = direction.strip().lower()
            if wanted not in ("up", "down", "left", "right"):
                return DriverResult(
                    ok=False, text=_ERR_UNKNOWN_DIRECTION.format(direction=direction)
                )
            node = self._live(app, rec)
            vertical = wanted in ("up", "down")
            # Chromium exposes scrolling as named actions on the scrollable node
            # (scrollDown, scrollUp, ...); prefer them, one action per page.
            named = f"scroll{wanted}"
            lowered = [a.lower() for a in node.actions]
            if named in lowered:
                index = lowered.index(named)
                client = linux_atspi.client()
                steps = max(1, int(round(pages)))
                for _ in range(steps):
                    client.call(node.ref, ACTION, "DoAction", "i", (index,))
                return DriverResult(
                    ok=True,
                    text=f"scrolled element {rec.index} {wanted} {steps} page(s) via {node.actions[index]}",
                    app=app,
                )
            bars = self._scrollbars(node.ref)
            axis = STATE_VERTICAL if vertical else STATE_HORIZONTAL
            bars = [b for b in bars if axis in b.states] or []
            if not bars:
                return DriverResult(
                    ok=False, text=_ERR_NO_SCROLL.format(index=rec.index, app=label)
                )
            bar = bars[0]
            client = linux_atspi.client()
            current = float(client.get_prop(bar.ref, VALUE, "CurrentValue"))
            low = float(client.get_prop(bar.ref, VALUE, "MinimumValue"))
            high = float(client.get_prop(bar.ref, VALUE, "MaximumValue"))
            # A page is the visible extent of the scrolled area, in the same units GTK
            # and Qt use for their adjustments (pixels). Clamp to the range.
            page = 0.0
            if rec.frame is not None:
                page = (rec.frame[3] if vertical else rec.frame[2]) * 0.9
            if page <= 0 or page > (high - low):
                page = (high - low) / 5.0
            sign = -1.0 if wanted in ("up", "left") else 1.0
            target = min(high, max(low, current + sign * page * max(0.1, pages)))
            client.set_prop(bar.ref, VALUE, "CurrentValue", "d", target)
            if abs(target - current) < 1e-9:
                return DriverResult(
                    ok=True, text=f"element {rec.index} is already at its {wanted} limit", app=app
                )
            return DriverResult(
                ok=True, text=f"scrolled element {rec.index} {wanted} {pages:g} page(s)", app=app
            )

        return _guarded("scroll", run)

    def perform_action(self, app: AppRef, rec: ElementRec, action: str) -> DriverResult:
        def run() -> DriverResult:
            label = app.bundle_id or app.name
            node = self._live(app, rec)
            wanted = action.strip().lower()
            if wanted in ("press", "click") and wanted not in [a.lower() for a in node.actions]:
                return self._press(app, rec, MOUSE_BUTTON_LEFT)
            done = self._do_action(node, (wanted,))
            if done is None:
                return DriverResult(
                    ok=False,
                    text=_ERR_UNKNOWN_ACTION.format(
                        action=action,
                        index=rec.index,
                        supported=", ".join(node.actions) or "nothing",
                    ),
                )
            if done.startswith("!"):
                return DriverResult(
                    ok=False,
                    text=_ERR_ACTION_FAILED.format(
                        verb=done[1:], index=rec.index, app=label, detail="refused"
                    ),
                )
            return DriverResult(ok=True, text=f"performed {done} on element {rec.index}", app=app)

        return _guarded("perform_action", run)

    def close(self) -> None:
        with self._lock:
            self._walks.clear()
        try:
            linux_atspi.reset_client()
        except Exception:
            logger.debug("closing the AT-SPI client failed", exc_info=True)
        try:
            linux_x11.close()
        except Exception:
            logger.debug("closing the X display failed", exc_info=True)


__all__ = ["LinuxBackend"]
