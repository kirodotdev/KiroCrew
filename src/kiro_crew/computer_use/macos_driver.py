"""The macOS :class:`NativeHelper` — what the agent-desktop driver cannot do itself.

agent-desktop reads the accessibility tree and performs every element action. This
module keeps the rest: the on-screen window inventory and identity rules
(:mod:`apps_macos`), the vetted ``open -a`` launch (:mod:`launch_macos`), coordinate
input, and keystrokes posted to the target process. agent-desktop's own ``press``
resolves its target through an app inventory that shells out to ``ps``, which the
gateway's macOS sandbox denies, so keys go through here.

**No exception crosses this seam.** Each public method wraps its work in
:func:`_guarded`: the MCP loop dispatches tool calls on a worker thread, and an
exception escaping into it takes the call down.

**Three coordinate paths, and only one of them touches the operator's cursor.**
``app_post`` posts a located mouse event with ``CGEventPostToPid``, so the target app
sees a click at a point while the physical cursor stays put. ``sky_click`` routes a
click to a background window through the private window-server path
(:mod:`macos_skylight`). ``global`` is the one path that warps the real cursor
(``CGWarpMouseCursorPosition`` + a global ``CGEventPost``); it is reachable only when
the model NAMES it — ``policy.resolve_click_method`` never resolves ``auto`` onto it.
A helper must therefore never upgrade a method on its own.
"""

from __future__ import annotations

import logging
from typing import Callable

from kiro_crew.computer_use import (
    apps_macos,
    cursor_motion,
    keymap,
    launch_macos,
    macos_ffi,
    macos_skylight,
    permissions,
)
from kiro_crew.computer_use.backend import NativeHelper, run_launch
from kiro_crew.computer_use.types import (
    CLICK_METHOD_APP_POST,
    CLICK_METHOD_GLOBAL,
    CLICK_METHOD_SKY_CLICK,
    DEFAULT_DRAG_PATH,
    DEFAULT_DRAG_STEPS,
    ERR_POINT_NOT_OWNED,
    ERR_POINT_REQUIRED,
    ERR_UNKNOWN_CLICK_METHOD,
    PERMISSION_UNKNOWN,
    PLATFORM_MACOS,
    AppRef,
    BackendStatus,
    ClickRequest,
    ComputerUseError,
    DragRequest,
    DriverResult,
    LaunchIdentity,
    PermissionProbe,
)

logger = logging.getLogger(__name__)

# ``sky_click`` routes by window id, so a vanished window is a distinct failure: the
# model must re-snapshot to get a live id, and telling it that is what stops it
# retrying the same dead id.
_REASON_SKY_WINDOW_GONE = (
    "the target window is no longer on screen (or no longer belongs to that app), so "
    "click_method 'sky_click' has no window to address. Call computer_get_state again"
)


def _point_not_owned(app: AppRef, x: float, y: float) -> str:
    """Refusal text for a confined ``global`` point outside the authorized app.

    Appends the window a hit-test by rectangle awards the point, so the operator
    can tell a genuinely-covered point from the case where the Dock's
    full-screen layer-20 backing window is reported first in z-order even though a
    physical click passes through it — without reimplementing the window walk from
    the tool result. ``topmost_window_label`` returns ``""`` when it cannot name a
    window, and the ``{winner}`` slot then collapses to nothing.
    """
    label = apps_macos.topmost_window_label(x, y)
    winner = f"; the topmost window there is {label}" if label else ""
    return ERR_POINT_NOT_OWNED.format(app=app.name, x=int(x), y=int(y), winner=winner)


class MacOSHelper(NativeHelper):
    """CoreGraphics window inventory, launch and input for macOS.

    Constructing this class does NOT load a framework: the FFI layer binds lazily
    on first real use, so a ``MacOSHelper()`` on a machine where
    ApplicationServices will not load still constructs, and the first call returns
    a typed refusal instead of exploding at import time.
    """

    def __init__(self) -> None:
        # Cached because ``status()`` is called for every dashboard render and each
        # miss would otherwise re-run ``find_library`` five times.
        self._available: "bool | None" = None

    @property
    def platform_id(self) -> str:
        return PLATFORM_MACOS

    def status(self) -> BackendStatus:
        """Whether the native frameworks load here."""
        if self._available is None:
            self._available = macos_ffi.available()
        if self._available:
            return BackendStatus(supported=True, platform_id=PLATFORM_MACOS, reason="")
        return BackendStatus(
            supported=False,
            platform_id=PLATFORM_MACOS,
            reason=(
                "the macOS accessibility frameworks could not be loaded "
                "(ApplicationServices / CoreGraphics / ImageIO)"
            ),
        )

    def probe_permissions(self) -> PermissionProbe:
        """ADVISORY permission hints — never a gate. See :mod:`permissions`."""
        try:
            return permissions.probe()
        except Exception:
            logger.debug("permission probe failed", exc_info=True)
            return PermissionProbe(
                accessibility=PERMISSION_UNKNOWN, screen_recording=PERMISSION_UNKNOWN
            )

    def list_apps(self) -> DriverResult:
        def run() -> DriverResult:
            return DriverResult(ok=True, apps=apps_macos.list_apps())

        return _guarded("list_apps", run)

    def resolve_app(self, query: str) -> DriverResult:
        def run() -> DriverResult:
            return DriverResult(ok=True, app=apps_macos.resolve_app(query))

        return _guarded("resolve_app", run)

    def launch_app(
        self,
        query: str,
        *,
        permit: "Callable[[LaunchIdentity], str | None] | None" = None,
        refuse_launched: "Callable[[AppRef], str | None] | None" = None,
    ) -> DriverResult:
        """Start an installed application through ``open -a``, then await its window.

        The flow is :func:`backend.run_launch`, shared with the Windows driver so the
        refusals cannot drift between platforms. macOS supplies only its own three
        pieces: :mod:`launch_macos` resolves a ``.app`` bundle under the conventional
        roots, ``_running_app`` answers from the on-screen window list, and the launcher
        is a pinned ``/usr/bin/open -a``.
        """

        def run() -> DriverResult:
            return run_launch(
                query,
                resolve=launch_macos.resolve_target,
                find=_running_app,
                spawn=launch_macos.spawn_detached,
                permit=permit,
                identity=launch_macos.target_identity,
                refuse_launched=refuse_launched,
            )

        return _guarded("launch_app", run)

    # ── input ──

    def click(self, app: AppRef, req: ClickRequest) -> DriverResult:
        """Dispatch one coordinate click onto the method *req* already resolved to.

        Element clicks are agent-desktop's; this is the point form only.

        ``req.method`` is CONCRETE here — ``auto`` was resolved at the dispatch
        chokepoint (``policy.resolve_click_method``), so this method never chooses
        one and in particular can never select the pointer-warping path on its own.
        An unrecognised method is REFUSED rather than falling back to
        accessibility: a fallback would silently perform a different gesture than
        the caller asked for.
        """

        def run() -> DriverResult:
            if req.point is None:
                return DriverResult(ok=False, text=ERR_POINT_REQUIRED.format(method=req.method))
            if req.method == CLICK_METHOD_APP_POST:
                # App-scoped: the target app sees a click at the point and the
                # operator's physical cursor does NOT move.
                macos_ffi.post_mouse_click(
                    app.pid,
                    req.point[0],
                    req.point[1],
                    button=req.button,
                    count=req.count,
                )
                return DriverResult(ok=True, text=_click_text(req, app), app=app)
            if req.method == CLICK_METHOD_SKY_CLICK:
                # The PRIVATE background-window path. Reached only when the model
                # NAMED it (``auto`` never resolves here), and implemented entirely
                # in the quarantined ``macos_skylight`` module.
                #
                # No ``pid_owns_point`` check, and that is correct rather than an
                # omission: this path carries the TARGET pid and window id in the
                # event itself, so the window server routes to that window whatever
                # is on top. The confinement ``global`` needs — "the pixel must
                # belong to the authorized app" — exists because a global event has
                # no pid to route by. Here the routing IS the confinement, and
                # ``policy.check_app`` already authorized this pid.
                bounds = apps_macos.window_bounds(app.window_id, app.pid)
                if bounds is None:
                    return DriverResult(ok=False, text=_REASON_SKY_WINDOW_GONE)
                left, top, width, height = bounds
                return _sky_click(app, req, left, top, width, height)
            if req.method == CLICK_METHOD_GLOBAL:
                # THE pointer-moving path. Both permits were checked upstream; this
                # driver must not re-derive or relax them.
                #
                # It MUST, however, confine the click to the app those permits were
                # granted for. A global event carries no pid and lands on whatever
                # owns the pixel, so naming an allowed app while passing coordinates
                # over a denied one (a terminal, a password manager) would pass the
                # policy check on app A and click app B. Every other input path is
                # app-scoped and needs no such check.
                if not apps_macos.pid_owns_point(app.pid, req.point[0], req.point[1]):
                    return DriverResult(
                        ok=False,
                        text=_point_not_owned(app, req.point[0], req.point[1]),
                    )
                macos_ffi.post_mouse_global(
                    req.point[0],
                    req.point[1],
                    button=req.button,
                    count=req.count,
                )
                return DriverResult(ok=True, text=_click_text(req, app), app=app)
            return DriverResult(ok=False, text=ERR_UNKNOWN_CLICK_METHOD.format(method=req.method))

        return _guarded("click", run)

    def drag(self, app: AppRef, req: DragRequest) -> DriverResult:
        """Drag between two screen points, app-scoped unless *req* moves the pointer.

        ``req.steps`` / ``req.path`` shape the motion — a straight sweep for a slider
        or a range selection, a bowed one for a hand-drawn stroke. Only the
        pointer-moving path confines them, and only because it is the one that leaves
        the target application: ``app_post`` delivers to ``app.pid`` by construction,
        so where the path travels cannot reach another process.

        **The FFI's own interpolation is the FLOOR, and a caller's path only replaces
        it when it is denser.** macOS synthesizes ``macos_ffi.DRAG_STEPS`` intermediate
        ``MouseDragged`` events, tuned live against TextEdit — which registered no
        selection at all without them — so a path with FEWER points than that is not a
        refinement, it is a downgrade that stops the gesture being recognised as a drag.

        Two shapes make that concrete, and both are things a model will send:
        ``path: "curved"`` with no ``steps`` is a 2-point path (``steps`` defaults to 1),
        and ``steps: 3`` is a 4-point one. Passing either straight through would deliver
        0 and 2 dragged events where the platform has always sent 5. So the caller's
        ``steps`` is raised to the FFI's floor before the path is built: a model asking
        for a shape gets at least the fidelity it would have got by asking for nothing.
        """

        def run() -> DriverResult:
            # ``max`` rather than a boolean "did the caller ask for a shape": the
            # question is whether the caller's path is at least as dense as the
            # platform's own, and only ``steps`` answers that.
            steps = max(req.steps, macos_ffi.DRAG_STEPS)
            shaped = steps > DEFAULT_DRAG_STEPS or req.path != DEFAULT_DRAG_PATH
            points = (
                cursor_motion.drag_points(req.start, req.end, steps=steps, path=req.path)
                if shaped
                else None
            )
            if req.method == CLICK_METHOD_GLOBAL:
                # EVERY point, not just the endpoints. Two authorized endpoints were
                # sufficient while a drag was a straight chord — a segment between two
                # points inside one rectangle stays inside it — but a ``curved`` path
                # bows AWAY from the chord by up to 110px, so a stroke near an edge can
                # travel over the window beside it with the button held. A default
                # request has no path, so its two endpoints are the whole gesture.
                for point in points or (req.start, req.end):
                    if not apps_macos.pid_owns_point(app.pid, point[0], point[1]):
                        return DriverResult(
                            ok=False,
                            text=_point_not_owned(app, point[0], point[1]),
                        )
                macos_ffi.post_mouse_drag_global(
                    req.start, req.end, button=req.button, points=points
                )
            elif req.method == CLICK_METHOD_APP_POST:
                macos_ffi.post_mouse_drag(
                    app.pid, req.start, req.end, button=req.button, points=points
                )
            else:
                # ``accessibility`` has no drag form at all — no AX action expresses
                # a sweep between two points — so an unusable method is refused
                # rather than approximated.
                return DriverResult(
                    ok=False, text=ERR_UNKNOWN_CLICK_METHOD.format(method=req.method)
                )
            return DriverResult(
                ok=True,
                text=(
                    f"dragged with the {req.button} button from "
                    f"({req.start[0]:.0f}, {req.start[1]:.0f}) to "
                    f"({req.end[0]:.0f}, {req.end[1]:.0f}) in '{app.name}' "
                    f"({req.method})"
                ),
                app=app,
            )

        return _guarded("drag", run)

    def type_text(self, app: AppRef, text: str) -> DriverResult:
        """Type *text* into whatever holds *app*'s keyboard focus.

        The agent-desktop driver moved focus to the addressed field and checked it
        landed and is not secure before calling this. Unicode key events rather than
        a per-character keycode lookup: layout-independent, and able to emit
        characters the US layout cannot reach in one keystroke.
        """

        def run() -> DriverResult:
            macos_ffi.post_text(app.pid, text)
            return DriverResult(
                ok=True, text=f"typed {len(text)} character(s) into the focused element", app=app
            )

        return _guarded("type_text", run)

    def press_key(self, app: AppRef, key: str) -> DriverResult:
        """Send one key spec to *app*'s process.

        ``parse_key`` raises KeyParseError (a ComputerUseError) for an unknown key or
        modifier, which ``_guarded`` turns into a refusal: silently dropping an
        unrecognised modifier would send a DIFFERENT keystroke into a live app.
        """

        def run() -> DriverResult:
            keycode, flags = keymap.parse_key(key)
            macos_ffi.post_key(app.pid, keycode, flags)
            return DriverResult(ok=True, text=f"sent {key}", app=app)

        return _guarded("press_key", run)

    def close(self) -> None:
        """Release the FFI handles, the event source and the identity cache.

        Safe to call repeatedly. Called through the agent-desktop driver's own
        ``close`` by ``reset_shared_backend``.
        """
        try:
            apps_macos.reset_identity_cache()
        except Exception:
            logger.debug("identity cache reset failed", exc_info=True)
        try:
            macos_ffi.reset_frameworks()
        except Exception:
            logger.debug("framework reset failed", exc_info=True)


def _running_app(name: str) -> "AppRef | None":
    """The on-screen app whose identity matches *name*, or ``None``.

    A NON-raising counterpart to ``apps_macos.resolve_app`` for the launch verb's two
    questions — "is it already running?" before launching, and "has it appeared yet?"
    while polling. Both are ordinary-negative: not-running is the case a launch exists
    to fix, so an exception would make the normal path the error path.

    Matched on the app's own name rather than its window title, because a
    freshly-launched window's title is whatever document it opened (``Untitled``)
    while the bundle name is what the catalog resolved.

    An enumeration failure resolves to ``None``: during a launch poll a transient
    failure is indistinguishable from "not there yet", and the poll's timeout bounds it.
    """
    wanted = (name or "").strip().casefold()
    if not wanted:
        return None
    try:
        apps = apps_macos.list_apps()
    except Exception:
        logger.debug("app lookup failed while matching %r", name, exc_info=True)
        return None
    for app in apps:
        if wanted in (app.name.casefold(), app.bundle_id.casefold()):
            return app
    return None


def _guarded(label: str, run: "Callable[[], DriverResult]") -> DriverResult:
    """Run *run*, converting every failure into ``DriverResult(ok=False, ...)``.

    ``ComputerUseError`` carries a message written for a model, so it is passed
    through verbatim (without the ``Error: `` prefix — the dispatch layer adds that
    exactly once). Anything else is logged with a traceback and reported
    generically: an unexpected exception's ``str`` can carry internal paths, and a
    model does not benefit from a stack-shaped message.

    A ctypes SEGFAULT is of course NOT catchable here. That is what the
    ``argtypes`` discipline and the bounds-guarded array reads in
    :mod:`macos_ffi` exist to prevent — this function handles the failures that
    *are* representable.
    """
    try:
        return run()
    except ComputerUseError as exc:
        return DriverResult(ok=False, text=str(exc))
    except Exception as exc:
        logger.warning("computer-use %s failed: %s", label, exc, exc_info=True)
        return DriverResult(ok=False, text=f"{label} failed unexpectedly ({type(exc).__name__})")


def _sky_click(
    app: AppRef,
    req: ClickRequest,
    left: float,
    top: float,
    width: float,
    height: float,
) -> DriverResult:
    """Dispatch one ``sky_click``, converting the screen point to window-local.

    The conversion is the whole reason this helper exists: the window server ignores
    the screen coordinate on this path and routes by the WINDOW-LOCAL point written
    into the private window-location field (see :mod:`macos_skylight`), so passing
    the screen point twice would click the wrong place in the window — off by exactly
    the window's origin.

    Both are still sent — the screen point in the event's own position field, the
    local one in the private field — because that is the shape the window server
    was observed to accept.
    """
    assert req.point is not None  # the caller checked; kept for the type narrowing
    macos_skylight.sky_click(
        pid=app.pid,
        window_id=app.window_id,
        screen_x=req.point[0],
        screen_y=req.point[1],
        window_x=req.point[0] - left,
        window_y=req.point[1] - top,
        window_width=width,
        window_height=height,
        click_count=req.count,
        # Passed rather than assumed: a right-click through this method must open
        # the context menu, not activate the control. A left-button-only path
        # would silently activate it on a background window the operator cannot
        # see. It is refused (not downgraded) inside ``macos_skylight``, and again
        # upstream at the chokepoint by ``policy.check_method_button`` so the model
        # gets the legible message.
        button=req.button,
    )
    return DriverResult(ok=True, text=_click_text(req, app), app=app)


def _click_text(req: ClickRequest, app: AppRef) -> str:
    """Confirmation prose for a coordinate click, naming the method it used.

    The METHOD is in the text on purpose: a model that asked for ``auto`` needs to
    know which path actually ran, because that determines whether a follow-up
    should re-aim (an accessibility press landed on a control) or re-measure (a
    coordinate click landed on a pixel). It is also the string the operator sees in
    a transcript when the agent took their mouse.
    """
    point = req.point or (0.0, 0.0)
    times = "" if req.count == 1 else f" x{req.count}"
    return (
        f"{req.button} click{times} at ({point[0]:.0f}, {point[1]:.0f}) "
        f"in '{app.name}' ({req.method})"
    )
