"""X11 half of the Linux computer-use driver: windows, geometry, stacking, pixels.

Everything here goes through ``ctypes`` against ``libX11`` so the driver needs no
compiled extension and no python-xlib. Three properties matter more than the API
surface and drive the shape of this module:

* **Xlib's default error handler calls ``exit()``.** A ``BadWindow`` from a window
  that closed between enumeration and a property read would take the whole gateway
  down. :func:`_ensure_lib` installs a recording handler before the first display is
  opened, and every request that can fail is followed by an ``XSync`` and a check of
  the recorded error, so a vanished window becomes ``None`` rather than a dead process.
* **Every call is serialized on one lock and one connection.** The gateway calls the
  driver from pooled worker threads; an Xlib ``Display`` is not safe to share without
  ``XInitThreads`` plus external locking, and we want both.
* **Pixels of OTHER windows must never leak into a capture.** ``XGetImage`` on a
  window returns what is on screen at that rectangle. With a compositing manager the
  window has its own backing pixmap and the read is exact; without one, an
  overlapping window's pixels would come back as ours. :func:`capture_window` refuses
  (returns ``None``) when no compositor runs and any viewable sibling overlaps.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import glob
import logging
import os
import threading
from dataclasses import dataclass

from kiro_crew.platform_compat import effective_uid

logger = logging.getLogger(__name__)

# ── Xlib constants ──
_ANY_PROPERTY_TYPE = 0
_SUCCESS = 0
_IS_VIEWABLE = 2
_Z_PIXMAP = 2
_ALL_PLANES = 0xFFFFFFFFFFFFFFFF

_Window = ctypes.c_ulong
_Atom = ctypes.c_ulong


class _XErrorEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("resourceid", ctypes.c_ulong),
        ("serial", ctypes.c_ulong),
        ("error_code", ctypes.c_ubyte),
        ("request_code", ctypes.c_ubyte),
        ("minor_code", ctypes.c_ubyte),
    ]


class _XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", _Window),
        ("class_", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


class _XImage(ctypes.Structure):
    # Only the leading fields are read; the trailing function table is never touched,
    # so it is not declared (ctypes only needs the prefix to be laid out correctly).
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
    ]


_ERROR_HANDLER_TYPE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_XErrorEvent))

_lock = threading.RLock()
_lib: "ctypes.CDLL | None" = None
_display: "int | None" = None
_last_error = [0]
_atoms: dict[str, int] = {}


@ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_XErrorEvent))
def _record_error(_dpy, event):  # pragma: no cover - called from C
    try:
        _last_error[0] = int(event.contents.error_code) or 1
    except Exception:
        _last_error[0] = 1
    return 0


#: Set by the IO-error exit handler; :func:`_dpy` then drops the dead connection.
_connection_lost = [False]


@ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
def _io_error(_dpy):  # pragma: no cover - called from C
    logger.warning("X11 connection lost (IO error); computer use will reconnect")
    return 0


@ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
def _io_error_exit(_dpy, _user_data):  # pragma: no cover - called from C
    # libX11 calls exit() after the IO error handler UNLESS an exit handler is set
    # and returns (libX11 >= 1.7). Returning here keeps the gateway alive: Xlib marks
    # the display dead, later calls on it fail fast, and the next _dpy() reopens.
    _connection_lost[0] = True


class X11Unavailable(RuntimeError):
    """No usable X display (none configured, none reachable, or libX11 missing)."""


def _ensure_lib() -> ctypes.CDLL:
    global _lib
    if _lib is not None:
        return _lib
    path = ctypes.util.find_library("X11") or "libX11.so.6"
    lib = ctypes.CDLL(path)
    if not hasattr(lib, "XSetIOErrorExitHandler"):
        # Without it, losing the X server (a logout, a DCV session restart) would
        # exit() the WHOLE gateway from inside Xlib. Refuse rather than risk that.
        raise X11Unavailable(
            "libX11 is older than 1.7 (no XSetIOErrorExitHandler); a lost X connection "
            "would terminate the gateway, so computer use stays off"
        )
    lib.XInitThreads.restype = ctypes.c_int
    lib.XInitThreads()
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    lib.XDefaultRootWindow.restype = _Window
    lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    lib.XInternAtom.restype = _Atom
    lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.XGetWindowProperty.restype = ctypes.c_int
    lib.XGetWindowProperty.argtypes = [
        ctypes.c_void_p, _Window, _Atom, ctypes.c_long, ctypes.c_long, ctypes.c_int, _Atom,
        ctypes.POINTER(_Atom), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p),
    ]  # fmt: skip
    lib.XFree.argtypes = [ctypes.c_void_p]
    lib.XGetWindowAttributes.restype = ctypes.c_int
    lib.XGetWindowAttributes.argtypes = [
        ctypes.c_void_p,
        _Window,
        ctypes.POINTER(_XWindowAttributes),
    ]
    lib.XTranslateCoordinates.restype = ctypes.c_int
    lib.XTranslateCoordinates.argtypes = [
        ctypes.c_void_p, _Window, _Window, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(_Window),
    ]  # fmt: skip
    lib.XQueryTree.restype = ctypes.c_int
    lib.XQueryTree.argtypes = [
        ctypes.c_void_p, _Window, ctypes.POINTER(_Window), ctypes.POINTER(_Window),
        ctypes.POINTER(ctypes.POINTER(_Window)), ctypes.POINTER(ctypes.c_uint),
    ]  # fmt: skip
    lib.XGetImage.restype = ctypes.POINTER(_XImage)
    lib.XGetImage.argtypes = [
        ctypes.c_void_p, _Window, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
        ctypes.c_ulong, ctypes.c_int,
    ]  # fmt: skip
    lib.XGetSelectionOwner.restype = _Window
    lib.XGetSelectionOwner.argtypes = [ctypes.c_void_p, _Atom]
    lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.XSetErrorHandler.restype = ctypes.c_void_p
    lib.XSetErrorHandler.argtypes = [_ERROR_HANDLER_TYPE]
    lib.XSetIOErrorHandler.restype = ctypes.c_void_p
    lib.XSetIOErrorHandler.argtypes = [ctypes.c_void_p]
    lib.XSetIOErrorExitHandler.restype = None
    lib.XSetIOErrorExitHandler.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    lib.XSetErrorHandler(_record_error)
    lib.XSetIOErrorHandler(ctypes.cast(_io_error, ctypes.c_void_p))
    _lib = lib
    return lib


# ── session discovery ──


def _uid() -> int:
    """This process's uid, or -1 off POSIX (no per-user runtime paths exist there)."""
    uid = effective_uid()
    return -1 if uid is None else uid


def display_name() -> str:
    """The X display to drive.

    ``KIROCREW_CU_DISPLAY`` wins, then ``DISPLAY``. A gateway started as a systemd
    service usually has neither, so the fallback is the lowest-numbered display
    socket this user can see, preferring ``:1`` (an Amazon DCV or other virtual
    session) over ``:0`` (often the console greeter, owned by another user).
    """
    for var in ("KIROCREW_CU_DISPLAY", "DISPLAY"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    sockets = sorted(glob.glob("/tmp/.X11-unix/X*"))
    numbers = []
    for sock in sockets:
        try:
            numbers.append(int(sock.rsplit("X", 1)[1]))
        except ValueError:
            continue
    for preferred in (1, 0):
        if preferred in numbers:
            return f":{preferred}"
    return f":{numbers[0]}" if numbers else ""


def xauthority() -> str:
    """The X authority file: env first, then the DCV session cookie, then ~/.Xauthority."""
    value = os.environ.get("XAUTHORITY", "").strip()
    if value:
        return value
    for candidate in (
        f"/run/user/{_uid()}/dcv/desktop.xauth",
        os.path.expanduser("~/.Xauthority"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return ""


#: Session process name -> XDG_CURRENT_DESKTOP value. A gateway started as a service
#: has no desktop environment variables, and toolkits key their platform integration
#: (and with it their accessibility bridge) on it: LibreOffice without it falls back
#: to its bare X11 backend, which has no AT-SPI support at all.
_DESKTOP_SESSIONS = {
    "xfce4-session": "XFCE",
    "gnome-shell": "GNOME",
    "gnome-session-b": "GNOME",
    "plasmashell": "KDE",
    "mate-session": "MATE",
    "cinnamon-sessio": "X-Cinnamon",
    "lxqt-session": "LXQt",
}


def read_comm(pid: int) -> str:
    """``/proc/<pid>/comm``, decoded so a non-UTF-8 process name cannot raise."""
    try:
        with open(f"/proc/{pid}/comm", "rb") as handle:
            return handle.read().decode("utf-8", "surrogateescape").strip()
    except OSError:
        return ""


def _detect_desktop() -> str:
    uid = _uid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return ""
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid != uid:
                continue
            comm = read_comm(int(entry))
        except OSError:
            continue
        desktop = _DESKTOP_SESSIONS.get(comm)
        if desktop:
            return desktop
    return ""


def session_env() -> dict[str, str]:
    """Environment for an app launch that must reach the same desktop session."""
    env = dict(os.environ)
    display = display_name()
    if display:
        env["DISPLAY"] = display
    auth = xauthority()
    if auth:
        env["XAUTHORITY"] = auth
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{_uid()}/bus")
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{_uid()}")
    if not env.get("WAYLAND_DISPLAY") and env.get("XDG_SESSION_TYPE", "").lower() != "x11":
        # A service-started gateway on a Wayland desktop inherits no WAYLAND_DISPLAY.
        try:
            sockets = sorted(
                n
                for n in os.listdir(env["XDG_RUNTIME_DIR"])
                if n.startswith("wayland-") and not n.endswith(".lock")
            )
        except OSError:
            sockets = []
        if sockets and not os.environ.get("KIROCREW_CU_DISPLAY"):
            env["WAYLAND_DISPLAY"] = sockets[0]
    env.setdefault("XDG_SESSION_TYPE", "wayland" if env.get("WAYLAND_DISPLAY") else "x11")
    if not env.get("XDG_CURRENT_DESKTOP"):
        desktop = _detect_desktop()
        if desktop:
            env["XDG_CURRENT_DESKTOP"] = desktop
    # No toolkit accessibility switch is forced here: GTK, Qt and LibreOffice read
    # org.a11y.Status.IsEnabled, which connecting to the bus already sets.
    return env


def _dpy() -> int:
    global _display
    if _connection_lost[0]:
        # The server went away. XCloseDisplay on a dead connection would touch the
        # broken socket again, so the struct is abandoned (a one-off leak per lost
        # server) and a fresh connection is opened below.
        _display = None
        _atoms.clear()
        _connection_lost[0] = False
    if _display:
        return _display
    lib = _ensure_lib()
    name = display_name()
    if not name:
        raise X11Unavailable("no X display found (set DISPLAY or KIROCREW_CU_DISPLAY)")
    auth = xauthority()
    if auth and not os.environ.get("XAUTHORITY"):
        # libXau reads XAUTHORITY from the process environment and offers no API to
        # pass a path, so the only way to authenticate is to set it. Only ever set
        # when ABSENT, so an operator's explicit choice is never overridden.
        os.environ["XAUTHORITY"] = auth
    dpy = lib.XOpenDisplay(name.encode())
    if not dpy:
        raise X11Unavailable(f"cannot open X display {name!r}")
    lib.XSetIOErrorExitHandler(dpy, ctypes.cast(_io_error_exit, ctypes.c_void_p), None)
    _display = dpy
    _atoms.clear()
    return dpy


def available() -> "tuple[bool, str]":
    """Whether an X display can be opened, and why not."""
    try:
        with _lock:
            _dpy()
        return True, ""
    except (OSError, X11Unavailable) as exc:
        return False, str(exc)


def _atom(name: str) -> int:
    cached = _atoms.get(name)
    if cached is None:
        cached = int(_ensure_lib().XInternAtom(_dpy(), name.encode(), 0))
        _atoms[name] = cached
    return cached


def _begin() -> None:
    _last_error[0] = 0


def _failed() -> bool:
    lib = _ensure_lib()
    lib.XSync(_dpy(), 0)
    return _last_error[0] != 0


def _root() -> int:
    return int(_ensure_lib().XDefaultRootWindow(_dpy()))


def _get_property(window: int, name: str, max_len: int = 65536) -> "tuple[int, int, bytes] | None":
    """``(type_atom, format, raw_bytes)`` of *name* on *window*, or ``None``."""
    lib = _ensure_lib()
    actual_type = _Atom()
    actual_format = ctypes.c_int()
    nitems = ctypes.c_ulong()
    after = ctypes.c_ulong()
    data = ctypes.c_void_p()
    _begin()
    status = lib.XGetWindowProperty(
        _dpy(), window, _atom(name), 0, max_len, 0, _ANY_PROPERTY_TYPE,
        ctypes.byref(actual_type), ctypes.byref(actual_format), ctypes.byref(nitems),
        ctypes.byref(after), ctypes.byref(data),
    )  # fmt: skip
    try:
        if status != _SUCCESS or _failed() or not data.value or actual_type.value == 0:
            return None
        fmt = actual_format.value
        # Format-32 items are delivered as C longs (8 bytes on LP64), not 4 bytes.
        unit = {8: 1, 16: ctypes.sizeof(ctypes.c_short), 32: ctypes.sizeof(ctypes.c_long)}[fmt]
        raw = ctypes.string_at(data.value, nitems.value * unit)
        return int(actual_type.value), fmt, raw
    finally:
        if data.value:
            lib.XFree(data)


def _longs(raw: bytes) -> list[int]:
    size = ctypes.sizeof(ctypes.c_ulong)
    return [
        int.from_bytes(raw[i : i + size], "little", signed=False) for i in range(0, len(raw), size)
    ]


def _str_prop(window: int, *names: str) -> str:
    for name in names:
        got = _get_property(window, name)
        if got and got[1] == 8:
            return got[2].split(b"\0", 1)[0].decode("utf-8", "replace")
    return ""


@dataclass(frozen=True)
class WindowInfo:
    """One top-level client window as the window manager publishes it."""

    xid: int
    pid: int
    title: str
    wm_instance: str
    wm_class: str
    window_type: str
    x: int
    y: int
    width: int
    height: int
    viewable: bool
    hidden: bool


def _geometry(window: int) -> "tuple[int, int, int, int, bool] | None":
    lib = _ensure_lib()
    attrs = _XWindowAttributes()
    _begin()
    if not lib.XGetWindowAttributes(_dpy(), window, ctypes.byref(attrs)) or _failed():
        return None
    x = ctypes.c_int()
    y = ctypes.c_int()
    child = _Window()
    _begin()
    lib.XTranslateCoordinates(
        _dpy(), window, _root(), 0, 0, ctypes.byref(x), ctypes.byref(y), ctypes.byref(child)
    )
    if _failed():
        return None
    return x.value, y.value, attrs.width, attrs.height, attrs.map_state == _IS_VIEWABLE


def _window_info(xid: int) -> "WindowInfo | None":
    geo = _geometry(xid)
    if geo is None:
        return None
    pid_prop = _get_property(xid, "_NET_WM_PID")
    pid = _longs(pid_prop[2])[0] if pid_prop and pid_prop[2] else 0
    cls = _get_property(xid, "WM_CLASS")
    instance = klass = ""
    if cls and cls[1] == 8:
        parts = cls[2].split(b"\0")
        instance = parts[0].decode("utf-8", "replace") if parts else ""
        klass = parts[1].decode("utf-8", "replace") if len(parts) > 1 else ""
    wtype = ""
    type_prop = _get_property(xid, "_NET_WM_WINDOW_TYPE")
    if type_prop and type_prop[2]:
        wtype = _atom_name(_longs(type_prop[2])[0])
    hidden = False
    state = _get_property(xid, "_NET_WM_STATE")
    if state and state[2]:
        hidden = _atom("_NET_WM_STATE_HIDDEN") in _longs(state[2])
    return WindowInfo(
        xid=xid,
        pid=pid,
        title=_str_prop(xid, "_NET_WM_NAME", "WM_NAME"),
        wm_instance=instance,
        wm_class=klass,
        window_type=wtype,
        x=geo[0],
        y=geo[1],
        width=geo[2],
        height=geo[3],
        viewable=geo[4],
        hidden=hidden,
    )


def _atom_name(atom: int) -> str:
    for name, value in _atoms.items():
        if value == atom:
            return name
    for candidate in (
        "_NET_WM_WINDOW_TYPE_NORMAL",
        "_NET_WM_WINDOW_TYPE_DIALOG",
        "_NET_WM_WINDOW_TYPE_UTILITY",
        "_NET_WM_WINDOW_TYPE_DOCK",
        "_NET_WM_WINDOW_TYPE_DESKTOP",
        "_NET_WM_WINDOW_TYPE_SPLASH",
        "_NET_WM_WINDOW_TYPE_TOOLBAR",
        "_NET_WM_WINDOW_TYPE_MENU",
    ):
        if _atom(candidate) == atom:
            return candidate
    return ""


#: Window types that are an application's own window. Desktop and dock windows (the
#: wallpaper, panels) are shell furniture: listing them would invite the model to
#: drive the panel, and they are no application's document.
_APP_WINDOW_TYPES = frozenset(
    {"", "_NET_WM_WINDOW_TYPE_NORMAL", "_NET_WM_WINDOW_TYPE_DIALOG", "_NET_WM_WINDOW_TYPE_UTILITY"}
)


def client_windows() -> list[WindowInfo]:
    """Application windows, TOP-most first (the window manager's stacking order)."""
    with _lock:
        prop = _get_property(_root(), "_NET_CLIENT_LIST_STACKING") or _get_property(
            _root(), "_NET_CLIENT_LIST"
        )
        if not prop:
            return []
        out: list[WindowInfo] = []
        for xid in reversed(_longs(prop[2])):
            info = _window_info(xid)
            if info is None or info.window_type not in _APP_WINDOW_TYPES:
                continue
            out.append(info)
        return out


def window_info(xid: int) -> "WindowInfo | None":
    with _lock:
        return _window_info(xid)


def window_bounds(xid: int) -> "tuple[float, float, float, float] | None":
    with _lock:
        geo = _geometry(xid)
    if geo is None:
        return None
    return float(geo[0]), float(geo[1]), float(geo[2]), float(geo[3])


def active_window() -> int:
    with _lock:
        prop = _get_property(_root(), "_NET_ACTIVE_WINDOW")
    if not prop or not prop[2]:
        return 0
    return _longs(prop[2])[0]


# ── stacking and ownership ──


def _children(window: int) -> list[int]:
    lib = _ensure_lib()
    root = _Window()
    parent = _Window()
    kids = ctypes.POINTER(_Window)()
    count = ctypes.c_uint()
    _begin()
    ok = lib.XQueryTree(
        _dpy(), window, ctypes.byref(root), ctypes.byref(parent), ctypes.byref(kids),
        ctypes.byref(count),
    )  # fmt: skip
    try:
        if not ok or _failed():
            return []
        return [int(kids[i]) for i in range(count.value)]
    finally:
        if kids:
            lib.XFree(ctypes.cast(kids, ctypes.c_void_p))


def _parent(window: int) -> int:
    lib = _ensure_lib()
    root = _Window()
    parent = _Window()
    kids = ctypes.POINTER(_Window)()
    count = ctypes.c_uint()
    _begin()
    ok = lib.XQueryTree(
        _dpy(), window, ctypes.byref(root), ctypes.byref(parent), ctypes.byref(kids),
        ctypes.byref(count),
    )  # fmt: skip
    if kids:
        lib.XFree(ctypes.cast(kids, ctypes.c_void_p))
    if not ok or _failed():
        return 0
    return int(parent.value)


def _toplevel_of(xid: int) -> int:
    """The child of the root that contains *xid* (its WM frame, or itself)."""
    root = _root()
    current = xid
    for _ in range(16):
        parent = _parent(current)
        if not parent:
            return 0
        if parent == root:
            return current
        current = parent
    return 0


def _rect_contains(rect: "tuple[int, int, int, int]", x: float, y: float) -> bool:
    rx, ry, rw, rh = rect
    return rx <= x < rx + rw and ry <= y < ry + rh


def _viewable_toplevels_topmost_first() -> "list[tuple[int, tuple[int, int, int, int]]]":
    out = []
    for child in reversed(_children(_root())):
        geo = _geometry(child)
        if geo is None or not geo[4] or geo[2] <= 1 or geo[3] <= 1:
            continue
        out.append((child, (geo[0], geo[1], geo[2], geo[3])))
    return out


def owns_point(xid: int, x: float, y: float) -> bool:
    """Whether screen point (*x*, *y*) is on *xid*'s own client area, on top.

    Compares X window IDs, never pids: a real XTest pointer event lands on whatever
    owns the pixel, so naming an allowed application while passing coordinates over a
    denied one must be refused here. The topmost viewable top-level containing the
    point must be *xid*'s own frame, AND the point must be inside the client rect (a
    click on the frame's title bar or close button is a window-manager action, not the
    application's).
    """
    with _lock:
        top = _toplevel_of(xid)
        client = _geometry(xid)
        if not top or client is None or not client[4]:
            return False
        if not _rect_contains((client[0], client[1], client[2], client[3]), x, y):
            return False
        for window, rect in _viewable_toplevels_topmost_first():
            if _rect_contains(rect, x, y):
                return window == top
        return False


def compositing() -> bool:
    """Whether a compositing manager owns ``_NET_WM_CM_S0`` (windows have own pixmaps)."""
    with _lock:
        return bool(_ensure_lib().XGetSelectionOwner(_dpy(), _atom("_NET_WM_CM_S0")))


def is_obscured(xid: int) -> bool:
    """Whether any viewable top-level ABOVE *xid*'s frame overlaps its client rect."""
    with _lock:
        top = _toplevel_of(xid)
        client = _geometry(xid)
        if not top or client is None:
            return True
        cx, cy, cw, ch = client[:4]
        for window, (x, y, w, h) in _viewable_toplevels_topmost_first():
            if window == top:
                return False
            if x < cx + cw and cx < x + w and y < cy + ch and cy < y + h:
                return True
        return True


# ── pixels ──


def capture_window(xid: int) -> "tuple[bytes, int, int, int] | None":
    """``(bgrx_bytes, width, height, bytes_per_line)`` of *xid*'s client area, or None.

    Fails closed on every doubtful case: the window is not viewable, the read errored,
    the pixel format is not 32-bit TrueColor, or (with no compositor) another window
    overlaps it, in which case the bytes would include that window's content.
    """
    with _lock:
        geo = _geometry(xid)
        if geo is None or not geo[4] or geo[2] <= 0 or geo[3] <= 0:
            return None
        if not compositing() and is_obscured(xid):
            logger.debug("capture refused: window 0x%x is obscured and no compositor runs", xid)
            return None
        lib = _ensure_lib()
        width, height = geo[2], geo[3]
        _begin()
        image = lib.XGetImage(_dpy(), xid, 0, 0, width, height, _ALL_PLANES, _Z_PIXMAP)
        if not image or _failed():
            return None
        try:
            img = image.contents
            if img.bits_per_pixel != 32 or not img.data:
                return None
            raw = ctypes.string_at(img.data, img.bytes_per_line * img.height)
            return raw, img.width, img.height, img.bytes_per_line
        finally:
            if image.contents.data:
                lib.XFree(ctypes.c_void_p(image.contents.data))
            lib.XFree(ctypes.cast(image, ctypes.c_void_p))


def close() -> None:
    global _display
    with _lock:
        if _display and _lib is not None:
            try:
                _lib.XCloseDisplay(_display)
            except Exception:
                logger.debug("XCloseDisplay failed", exc_info=True)
        _display = None
        _atoms.clear()
