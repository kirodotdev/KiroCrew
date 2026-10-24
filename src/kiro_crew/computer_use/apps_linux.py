"""Linux application enumeration, identity, and the confinement predicate.

The Linux counterpart of :mod:`apps_windows` / :mod:`apps_macos`. It joins two
views of the desktop that neither platform has to reconcile, because on Linux
the accessibility tree and the window stack are owned by DIFFERENT servers:

* **AT-SPI** (``libatspi``) owns the semantic tree — the applications, their
  frames and the controls inside them. This is where ``name``, ``pid``, roles
  and states come from, and it is the only view that can tell an application
  with a visible frame apart from a daemon with none (which is what drops the
  ``gsd-*`` background services).
* **X11** (``libX11``) owns the on-screen window stack — the ``_NET_CLIENT_LIST``
  of top-level windows, their geometry, and which window sits under a screen
  point. This is where ``window_id`` and the confinement check come from, because
  a pointer gesture lands on an X window, not on an AT-SPI node.

An :class:`AppRef` is the join of the two, keyed on the AT-SPI application's pid:
AT-SPI gives the semantic identity, X11 gives the top-level window whose
``_NET_WM_PID`` matches it. The pid is a usable identity key here — unlike
Windows, X11 has no single broker fronting many unrelated applications, and
AT-SPI reports the real application's pid — so ``AppRef.pid`` is both the AT-SPI
match key and part of ``window_key``.

The ``bundle_id`` is the basename of ``/proc/<pid>/exe`` (``gnome-calculator``,
not a path): the Linux spelling an operator's deny rule will carry, the analogue
of ``notepad.exe`` on Windows. It is read through ``os.readlink`` rather than the
accessibility bus because the toolkit name AT-SPI reports ("gtk") is not an
application identity.

Nothing in this module imports ctypes: all native work goes through
:mod:`linux_ffi`, the family's one native seam (see the Linux driver section of
``docs/system-specs/modules/computer-use.md``). This module must import on a machine with no X display
and none of the native libraries, so :mod:`linux_ffi` is imported at module
scope only for its name — every call into it happens inside a function.
"""

from __future__ import annotations

import logging
import os

from kiro_crew.computer_use import linux_ffi
from kiro_crew.computer_use.types import AppRef, ComputerUseError

logger = logging.getLogger(__name__)

#: AT-SPI roles whose presence as a SHOWING child marks an application as having
#: an on-screen window. An application with zero visible frames (the ``gsd-*``
#: settings daemons, the shell's own helpers) is not "an app with a window", and
#: this is what drops them — the Linux analogue of the ``IsWindowVisible`` +
#: cloaked filter ``apps_windows`` relies on ``window_list`` for. The three roles
#: are the top-level surfaces a user interacts with; a bare ``PANEL`` is interior
#: furniture and never a window of its own.
_FRAME_ROLES: frozenset[int] = frozenset(
    {
        linux_ffi.ATSPI_ROLE_FRAME,
        linux_ffi.ATSPI_ROLE_WINDOW,
        linux_ffi.ATSPI_ROLE_DIALOG,
    }
)

_ERR_NO_MATCH = (
    "no application with an on-screen window matches {query!r}. Call "
    "computer_list_apps to see what is available"
)


def _bundle_id_for_pid(pid: int) -> str:
    """Basename of the process executable, or ``""`` when unreadable.

    ``os.readlink("/proc/<pid>/exe")`` is the Linux spelling an operator writes in
    a deny rule, the counterpart of the ``.exe`` image name on Windows. It is read
    from ``/proc`` rather than from the accessibility bus because the toolkit name
    AT-SPI exposes ("gtk", "Qt") names a framework, not an application, so a rule
    on it would match everything built with that toolkit.

    Unreadable resolves to ``""`` rather than raising: a short-lived helper, a
    process the current uid cannot stat, or a kernel thread with no ``exe`` link is
    an ordinary negative, and the AT-SPI ``name`` still identifies the app.
    """
    if pid <= 0:
        return ""
    try:
        return os.path.basename(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        logger.debug("could not read /proc/%d/exe", pid, exc_info=True)
        return ""


def _showing_frames(app_obj: int) -> list[int]:
    """The application's frame/window/dialog children that are currently SHOWING.

    A SHOWING frame is one the user can see; a frame without that state is a
    window that exists in the tree but is minimized, withdrawn, or not yet mapped.
    Filtering on it is what keeps this to applications with an actual on-screen
    surface, matching the "on-screen windows" guarantee the renderer and the tool
    descriptions both make.
    """
    frames: list[int] = []
    count = linux_ffi.child_count(app_obj)
    for i in range(count):
        child = linux_ffi.child_at(app_obj, i)
        if child is None:
            continue
        try:
            if linux_ffi.role(child) in _FRAME_ROLES and linux_ffi.has_state(
                child, linux_ffi.ATSPI_STATE_SHOWING
            ):
                frames.append(child)
            else:
                linux_ffi.unref(child)
        except ComputerUseError:
            linux_ffi.unref(child)
    return frames


def _active_frame_of(frames: list[int]) -> "int | None":
    """The frame the user is focused on, else the first showing one.

    The ACTIVE frame (``ATSPI_STATE_ACTIVE``) is the one with the window
    manager's focus; its name is the current window title and its subtree is what
    a snapshot should walk. When nothing is active (a background application with a
    visible-but-unfocused window) the first showing frame is the honest fallback —
    it is a real on-screen surface, which is all the window-less daemons lacked.
    """
    for frame in frames:
        try:
            if linux_ffi.has_state(frame, linux_ffi.ATSPI_STATE_ACTIVE):
                return frame
        except ComputerUseError:
            continue
    return frames[0] if frames else None


def _match_window(pid: int, title: str, windows: "tuple[int, ...]") -> int:
    """The X11 top-level that belongs to *pid*, preferring a title match.

    Three tiers, tightest first, because an application legitimately owns more than
    one top-level X window (a main window plus a tooltip or a utility palette) and
    the one the model should address is the one whose title matches the AT-SPI
    active frame:

    1. the viewable window with this pid whose title equals the frame's name;
    2. the only viewable window with this pid, when there is exactly one;
    3. ``0`` — no X window could be matched, so pointer gestures are refused rather
       than aimed at a guessed handle.

    ``window_id == 0`` is a usable state: the snapshot still reads the AT-SPI tree,
    and :func:`window_owns_point` fails closed on it, so a gesture is refused
    instead of mis-delivered.
    """
    viewable: list[int] = []
    for w in windows:
        try:
            if linux_ffi.x_window_pid(w) != pid:
                continue
            if not linux_ffi.x_window_is_viewable(w):
                continue
        except ComputerUseError:
            continue
        viewable.append(w)
        if title and linux_ffi.x_window_title(w) == title:
            return w
    if len(viewable) == 1:
        return viewable[0]
    return 0


def _app_ref(app_obj: int, windows: "tuple[int, ...]") -> "AppRef | None":
    """Build an :class:`AppRef` from one AT-SPI application object, or ``None``.

    Returns ``None`` when the application has no showing frame — that is the
    window-less-daemon case, and it is dropped here rather than listed with an
    empty window. All owned frame references are released before returning: this
    reads the tree, it does not retain it.

    ``bundle_id`` carries the ``/proc`` executable basename and is what
    ``policy.check_app`` matches a deny rule against, mirroring how
    ``apps_windows`` puts the image name there. ``window_title`` is the ACTIVE
    frame's name, which is also the title the X match keys on.
    """
    frames = _showing_frames(app_obj)
    if not frames:
        return None
    try:
        active = _active_frame_of(frames)
        if active is None:
            return None
        pid = linux_ffi.pid(app_obj)
        try:
            name = linux_ffi.name(app_obj)
        except ComputerUseError:
            name = ""
        try:
            title = linux_ffi.name(active)
        except ComputerUseError:
            title = ""
        bundle_id = _bundle_id_for_pid(pid)
        window_id = _match_window(pid, title, windows)
        # An application with neither a readable name nor a title has nothing to
        # show the model — the same shape check ``apps_windows`` applies.
        if not name and not title and not bundle_id:
            return None
        return AppRef(
            name=name or bundle_id or title,
            pid=pid,
            bundle_id=bundle_id,
            window_id=window_id,
            window_title=title,
        )
    finally:
        for frame in frames:
            linux_ffi.unref(frame)


def list_apps() -> tuple[AppRef, ...]:
    """Applications with at least one SHOWING frame, joined to their X window.

    One entry per AT-SPI application (keyed on pid), unlike ``apps_windows`` which
    is per-window: X11 has no broker process fronting many apps, so the pid IS the
    application identity and the per-window split that platform needs would only
    fragment one app's palettes into separate entries here.

    An application whose frame trips the built-in denylist is still LISTED — the
    dispatch chokepoint refuses it, and hiding it would leave the model retrying a
    target it cannot see is blocked, exactly as on the other two platforms.

    Deterministic order: by X11 stacking order (topmost first) via
    ``x_client_list``, then applications with no X match, then by name — so the
    window the user is looking at is the first a model reads, and the order does
    not jitter between two calls.

    An enumeration failure PROPAGATES to the driver's ``_guarded`` seam rather than
    collapsing to ``()``: an empty tuple is indistinguishable from an empty desktop,
    and ``resolve_app`` would then name ``computer_list_apps`` as the remedy —
    pointing the model at the call that just failed.
    """
    windows = linux_ffi.x_client_list()
    desktop = linux_ffi.desktop()
    refs: list[AppRef] = []
    try:
        count = linux_ffi.child_count(desktop)
        for i in range(count):
            app_obj = linux_ffi.child_at(desktop, i)
            if app_obj is None:
                continue
            try:
                ref = _app_ref(app_obj, windows)
            finally:
                linux_ffi.unref(app_obj)
            if ref is not None:
                refs.append(ref)
    finally:
        linux_ffi.unref(desktop)

    # Stacking order: ``x_client_list`` is topmost-first (the stacking variant),
    # so the index of an AppRef's window in that list is its depth. Apps with no X
    # match (window_id 0) sort after all matched ones, then alphabetically, so the
    # order is total and stable.
    stacking = {w: pos for pos, w in enumerate(windows)}
    sentinel = len(windows)
    refs.sort(key=lambda r: (stacking.get(r.window_id, sentinel), r.name.lower()))
    return tuple(refs)


def resolve_app(query: str) -> AppRef:
    """Resolve *query* (name, bundle id, or a prefix) to one on-screen application.

    Layered rather than one fuzzy pass, so an exact name always beats a prefix: a
    bare substring match on a short query is a coincidence, not an intent, so only
    exact and prefix forms are honoured. Raises :class:`ComputerUseError` naming
    ``computer_list_apps`` when nothing matches, so the model's next move is
    obvious.
    """
    wanted = (query or "").strip().lower()
    if not wanted:
        raise ComputerUseError(_ERR_NO_MATCH.format(query=query))
    apps = list_apps()

    for app in apps:
        if app.name.lower() == wanted or app.bundle_id.lower() == wanted:
            return app
    for app in apps:
        if app.name.lower().startswith(wanted) or app.bundle_id.lower().startswith(wanted):
            return app
    raise ComputerUseError(_ERR_NO_MATCH.format(query=query))


def find_window_for(name: str) -> "AppRef | None":
    """The on-screen application *name* identifies, or ``None`` — non-raising.

    The counterpart of :func:`resolve_app` for callers whose negative case is
    ordinary rather than an error (a future launch verb's "is it running yet?").
    An enumeration failure resolves to ``None`` rather than propagating, because to
    such a caller a transient failure is indistinguishable from "not there yet".
    """
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    try:
        apps = list_apps()
    except Exception:
        logger.debug("application lookup failed while matching %r", name, exc_info=True)
        return None
    for app in apps:
        stem = app.bundle_id.lower()
        if wanted in (app.name.lower(), app.bundle_id.lower(), stem):
            return app
    for app in apps:
        if app.name.lower().startswith(wanted) or stem.startswith(wanted):
            return app
    return None


def window_bounds(app: AppRef) -> "tuple[float, float, float, float] | None":
    """Screen rect of *app*'s window CONTENT, or ``None`` if it is gone or unmatched.

    Content, not X geometry: a client-side-decorated toplevel (every GTK 4 window)
    is larger than what it draws — the X window carries the shadow margins, and
    ``_GTK_FRAME_EXTENTS`` says by how much (measured: ``482x748`` of X geometry
    around a ``360x626`` calculator). Element frames are WINDOW-local to that content
    surface, and the capture is taken over this rect, so publishing the content rect
    is what makes ``image pixel == frame coordinate`` hold. A window without the
    property (GTK 3, Qt, xterm — server-side decorated) is its own content.

    ``window_id == 0`` (no X window could be matched to the AT-SPI app) resolves to
    ``None`` without asking X, so element frames fall back to ``None`` rather than
    being measured against a window that is not this app's.
    """
    if not app.window_id:
        return None
    try:
        geom = linux_ffi.x_window_geometry(app.window_id)
        if geom is None:
            return None
        x, y, w, h = geom
        margins = linux_ffi.x_gtk_frame_extents(app.window_id)
        if margins is not None:
            left, right, top, bottom = margins
            if w - left - right > 0 and h - top - bottom > 0:
                return (
                    float(x + left),
                    float(y + top),
                    float(w - left - right),
                    float(h - top - bottom),
                )
        return (float(x), float(y), float(w), float(h))
    except Exception:
        logger.debug("window bounds lookup failed for 0x%X", app.window_id, exc_info=True)
        return None


def window_owns_point(app: AppRef, x: float, y: float) -> bool:
    """Does *app*'s own top-level window sit under the screen point?

    THE confinement check for any pointer-moving gesture, the Linux twin of
    ``apps_windows.hwnd_owns_point``. It compares the X11 top-level under the point
    (``x_window_at``) against ``app.window_id`` by HANDLE: a ``global`` click warps
    the real cursor and posts a system-wide event that carries no pid, so without
    this an app-authorized gesture plus coordinates over a DIFFERENT window would
    click that other window — the app-A-authorized / app-B-clicked hole.

    Fails CLOSED (``False``) on a zero window id, a point owned by nobody, a
    mismatched handle, or any unexpected error. Refusing a legitimate click costs the
    model one clear refusal; permitting a mis-aimed one is an irreversible action in
    an application the operator never authorized.

    A :class:`ComputerUseError` is RE-RAISED rather than folded into ``False``: it is
    the typed refusal the ffi raises BEFORE any native call on an unavailable host
    (``no X display (DISPLAY unset)``), and the driver's ``_guarded`` writes it into
    the result verbatim. Folding it would tell the model the point is owned by
    another window and to "bring the window to the front" on a host with no
    display at all — safe, but the wrong remedy. Every verb then refuses an
    unavailable host with the same reason.
    """
    if not app.window_id:
        return False
    try:
        root = linux_ffi.x_window_at(int(x), int(y))
        if not root:
            # The point belongs to no top-level window we can name, so we cannot
            # prove it belongs to the authorized one.
            return False
        return int(root) == int(app.window_id)
    except ComputerUseError:
        raise
    except Exception:
        logger.debug("point ownership check failed", exc_info=True)
        return False


def application_for(app: AppRef) -> "int | None":
    """The live AT-SPI application object for *app* (matched on pid), or ``None``.

    An action cannot reuse the application object a previous :func:`list_apps`
    returned — that walk released every reference — so reaching the tree again
    means re-finding the application by its stable identity, the pid. The caller
    OWNS the returned reference and must release it with ``linux_ffi.unref``; the
    non-matching desktop children are released here.

    ``None`` when no desktop child has this pid: the application exited between the
    snapshot and the action, which the driver turns into a refusal rather than an
    exception inside a native call.
    """
    desktop = linux_ffi.desktop()
    try:
        count = linux_ffi.child_count(desktop)
        for i in range(count):
            child = linux_ffi.child_at(desktop, i)
            if child is None:
                continue
            try:
                matched = linux_ffi.pid(child) == app.pid
            except ComputerUseError:
                matched = False
            if matched:
                return child
            linux_ffi.unref(child)
        return None
    finally:
        linux_ffi.unref(desktop)


def active_frame(app_obj: int) -> "int | None":
    """The active (or first showing) frame of a live AT-SPI application object.

    The subtree a snapshot walks. The caller owns the returned reference and
    releases it with ``linux_ffi.unref``; every other showing frame is released
    here. ``None`` when the application has no showing frame — it had one when it
    was listed and lost it since (minimized, closed), which the walk reports as a
    gone window.
    """
    frames = _showing_frames(app_obj)
    if not frames:
        return None
    active = _active_frame_of(frames)
    for frame in frames:
        if frame != active:
            linux_ffi.unref(frame)
    return active


__all__ = [
    "active_frame",
    "application_for",
    "find_window_for",
    "list_apps",
    "resolve_app",
    "window_bounds",
    "window_owns_point",
]
