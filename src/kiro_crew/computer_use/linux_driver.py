"""Linux computer-use backend: AT-SPI for reading, AT-SPI + X11/XTest for acting.

The Linux counterpart of :mod:`windows_driver`, and it mirrors that module
method-for-method because the layer above the driver (the keystone, schema
validation, the snapshot index, fingerprint drift, redaction, the SEL audit) is
platform-free and must see the same shape from every backend.

**The safety story is X11's, and it is the same shape as Windows's: there is no
reliable per-window input delivery.** XTest (``xtest_key``/``xtest_button``/
``xtest_motion``) is GLOBAL — it moves the operator's real pointer and takes their
real keyboard focus, exactly as ``SendInput`` does. ``XSendEvent`` is per-window
but toolkits may ignore a synthetic event (GTK4 does), so it is not a safe
substitute. That one fact produces every asymmetry below, and they match the
Windows driver's:

* **AT-SPI actions are the SAFE form, and the only one ``auto`` resolves to.** A
  ``do_action`` is performed by the toolkit INSIDE the target widget — no pointer,
  no focus — so an element-addressed click has no effect outside the control it
  names. :mod:`input_linux` walks an action ladder by SPECIFICITY (``click`` /
  ``press`` / ``activate``, then ``toggle`` for a checkbox, then the element's sole
  action), and a rung is tried only when the previous action is ABSENT, never when
  it was present and FAILED.
* **The pointer path is opt-in by name.** ``click_method: "auto"`` with coordinates
  is REFUSED, because the only coordinate route that moves no pointer is
  ``app_post`` (``XSendEvent``, which toolkits may ignore) and ``auto`` must never
  resolve onto the pointer-warping ``global``. ``sky_click`` is refused BY NAME: it
  is a macOS private-window-server route with no X11 equivalent.
* **Every pointer gesture is confined to the authorized window**, by comparing the
  top-level X window the point lands on (``x_window_at``) against ``app.window_id``
  — a HANDLE comparison, because a point names whatever owns the pixel and a
  real-pointer click on an allowed app's coordinates over a denied window would
  click the denied one. A drag confines EVERY point of its path, since a curved
  stroke bows away from the chord.
* **The keyboard verbs take focus and SAY SO**, and a focus failure REFUSES rather
  than typing — keystrokes delivered to a focus that did not move reach whatever
  the app already had focused, which can be the password field the policy check
  refused. An editable AT-SPI element is written through ``EditableText`` with NO
  focus change instead, which the result also states.
* **The secure-field floor is re-read LIVE at the driver.** A field can flip plain
  to password on the application's own timer between the walk the model was shown
  and the write, and this module is reachable without the dispatch chokepoint
  (``kirocrew computer call``). ``snapshot_linux.addressed(..., for_write=True)``
  raises :class:`SecureTargetError`, which reaches the model verbatim.
* **``perform_action`` takes a CLOSED vocabulary** — only the names the record's
  own ``actions`` list advertises (AT-SPI action strings are toolkit-defined, so
  there is no fixed table), re-addressed with a role+name check.

**No native library loads when this module is imported**, nor when a
``LinuxBackend`` is CONSTRUCTED. Every ``CDLL`` / ``XOpenDisplay`` / ``atspi_init``
lives behind a function in :mod:`linux_ffi`, and ``status()`` caches
``linux_ffi.available()`` lazily on first call — exactly as the Windows driver
caches ``_available`` — so the whole upstream Linux CI fleet (Linux with no
DISPLAY) builds a real ``LinuxBackend`` whose ``status()`` reports unsupported with
the no-display reason without loading anything.

The ffi, reading and capture layers land on sibling branches; this module codes
against their published contracts (:mod:`apps_linux`, :mod:`snapshot_linux`,
:mod:`capture_linux`, :mod:`input_linux`, :mod:`linux_ffi`).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from kiro_crew.computer_use import (
    apps_linux,
    capture_linux,
    cursor_motion,
    input_linux,
    keymap,
    linux_ffi,
    snapshot_linux,
)
from kiro_crew.computer_use.backend import ComputerUseBackend
from kiro_crew.computer_use.types import (
    CLICK_METHOD_ACCESSIBILITY,
    CLICK_METHOD_APP_POST,
    CLICK_METHOD_AUTO,
    CLICK_METHOD_GLOBAL,
    CLICK_METHOD_SKY_CLICK,
    DEFAULT_TEXT_LIMIT,
    DRAG_SHAPE_NOTE,
    ERR_POINT_REQUIRED,
    ERR_UNKNOWN_CLICK_METHOD,
    MAX_TREE_DEPTH_LIMIT,
    MAX_TREE_NODES_LIMIT,
    MOUSE_BUTTON_LEFT,
    PERMISSION_GRANTED,
    PERMISSION_UNSUPPORTED,
    PLATFORM_LINUX,
    REFUSAL_ACCESSIBILITY_NEEDS_INDEX,
    SCROLL_DIRECTIONS,
    AppRef,
    BackendStatus,
    ClickRequest,
    ComputerUseError,
    DragRequest,
    DriverResult,
    ElementRec,
    LaunchIdentity,
    PermissionProbe,
    SnapshotRequest,
)

logger = logging.getLogger(__name__)


#: Why the native surface is unavailable, when it is. Carries the LIVE first-failing
#: step from ``linux_ffi.unavailable_reason()`` (no DISPLAY, a Wayland-only session,
#: a missing library, an unreachable AT-SPI bus) so ``status()`` reports the real
#: cause rather than a generic "not implemented". Reserved for the case where the
#: client cannot be reached at all; its empty-string companion is "" when available.
#:
#: Distinct from ``backend.LINUX_REASON``, which is the fallback for a failed driver
#: IMPORT (a partial install) — this is the reason the loaded driver reports when it
#: imported fine but cannot reach X11/AT-SPI on this host.
def UNAVAILABLE_REASON() -> str:  # noqa: N802 — a function because the reason is LIVE
    """The live first-failing step, or empty when the client is reachable.

    A function rather than a constant because the reason is a runtime fact that
    changes with the environment (DISPLAY set or not, bus up or not), and reading it
    at import time would both load nothing useful and bake in whatever was true when
    the module first imported.
    """
    return linux_ffi.unavailable_reason()


# Refusals. Each NAMES the working alternative, because a refusal a model cannot act
# on costs it a turn and teaches it nothing — the Windows driver's rule.
_REFUSE_SKY_CLICK = (
    "click_method 'sky_click' is macOS-only (a private window-server path with no "
    "Linux equivalent). Use an element_index (no pointer is moved), or name "
    "click_method 'global' to move the real cursor"
)
_REFUSE_AUTO_POINT = (
    "click_method 'auto' with coordinates cannot be served on Linux: the only "
    "coordinate route that leaves the pointer alone is 'app_post', which many "
    "toolkits ignore, and 'auto' never resolves onto the pointer-moving 'global'. "
    "Pass an element_index (no pointer is moved), name click_method 'app_post' to "
    "post a window event, or name click_method 'global' to accept the cursor move"
)
#: A drag whose method is neither ``global`` (XTest, real cursor) nor ``app_post``
#: (XSendEvent, per-window). Unlike Windows — which has only the real-cursor route
#: and must refuse the app_post DEFAULT — Linux serves ``app_post`` as a legitimate
#: per-window drag, so this fires only for a method with no X11 drag route at all
#: (``sky_click``, ``accessibility``), and names the two that work.
_REFUSE_DRAG_METHOD = (
    "click_method '{method}' has no Linux drag route. Pass click_method 'global' to "
    "drag with the real cursor (XTest), or click_method 'app_post' to post X11 window "
    "events (which some toolkits ignore)"
)
#: A non-LEFT button on the element path. An AT-SPI action is a left-button
#: activation; there is no action that opens a context menu, so the only route for a
#: right/middle click is the real cursor, which the caller must name.
_REFUSE_ELEMENT_BUTTON = (
    "a {button}-button click cannot be delivered to element {index} in {app}: an "
    "accessibility action activates the control with the left button only, and there "
    "is no action that opens a context menu. Name click_method 'global' with "
    "coordinates to accept the cursor move"
)
_ERR_NO_ACTION = (
    "element {index} in {app} advertises no action this platform can perform (tried "
    "click, press, activate and toggle). Try a parent or child element from "
    "computer_get_state"
)
_ERR_ELEMENT_GONE = (
    "element {index} is no longer in {app}'s tree. Call computer_get_state to re-read it"
)
_ERR_POINT_NOT_OWNED = (
    "the point ({x}, {y}) is not owned by {app}'s own window — another window is on "
    "top of it, or the window is not on screen — so a click there would land on a "
    "different application. Address the control by element_index from "
    "computer_get_state instead (no pointer is moved), or bring the window to the "
    "front first; the same coordinates will keep being refused while another window "
    "owns them"
)
_ERR_APP_POST_IGNORED = (
    "posted an X11 button event to element {index}'s window, but the toolkit may have "
    "ignored it (GTK4 and some others discard synthetic events). If nothing changed, "
    "use an element_index (an accessibility action), or name click_method 'global'"
)
_ERR_FOCUS_FAILED = (
    "could not move keyboard focus to element {index} in {app}, so no keystrokes were "
    "sent. Sending them anyway would deliver them to whatever the application already "
    "had focused"
)
_ERR_WINDOW_NOT_ACTIVE = (
    "{app}'s window is not the active window, so no keystrokes were sent: X11 delivers "
    "synthesized keys to whichever window is active, not to the one named. Bring it to "
    "the front first (a click with click_method 'global' on one of its elements), then "
    "retry"
)
_ERR_TEXT_SHORT = (
    "only {sent} of {total} characters could be typed on this keyboard layout; the "
    "field may hold a partial value"
)
_ERR_NO_SET_VALUE = (
    "element {index} in {app} has no settable value (no accessibility EditableText or "
    "Value interface). Use computer_type_text to send keystrokes to it instead"
)
_ERR_NO_SCROLL = (
    "element {index} in {app} could not be scrolled on that axis. Scroll a parent "
    "container, or check the element list for one with a scrollbar"
)
_ERR_UNKNOWN_ACTION = (
    "unknown action {action!r} for element {index}. This element advertises: {supported}"
)
_ERR_ACTION_FAILED = "{action} on element {index} of {app} failed"
_ERR_LAUNCH_NOT_IMPLEMENTED = (
    "launching an application is not implemented on Linux yet: resolving a .desktop "
    "entry and bounding its Exec target is a later change. Use an application that is "
    "already running (call computer_list_apps to see what is on screen)"
)


def _guarded(label: str, run: "Callable[[], DriverResult]") -> DriverResult:
    """Run *run*, converting every failure into ``DriverResult(ok=False, ...)``.

    Identical in shape to ``windows_driver._guarded``: the ABC's contract is that no
    exception crosses the seam, because the gateway dispatches driver calls on a
    pooled worker thread and an exception escaping into it takes the tool call down.

    A :class:`ComputerUseError` (including :class:`SecureTargetError`) is written for
    a model, so its message passes through verbatim WITHOUT the ``Error: `` prefix
    (the dispatch layer adds that exactly once). Anything else is logged with a
    traceback and reported generically, because an arbitrary ``str(exc)`` can carry
    internal paths.
    """
    try:
        return run()
    except ComputerUseError as exc:
        return DriverResult(ok=False, text=str(exc))
    except Exception as exc:
        logger.warning("computer-use %s failed: %s", label, exc, exc_info=True)
        return DriverResult(ok=False, text=f"{label} failed unexpectedly ({type(exc).__name__})")


class LinuxBackend(ComputerUseBackend):
    """AT-SPI + X11 driver for Linux: observation and input, X11 only (no Wayland)."""

    def __init__(self) -> None:
        # Cached because ``status()`` is called for every dashboard render, and a
        # miss would re-run the availability probe each time. ``None`` means "not
        # probed yet" — the probe is deferred to first ``status()`` so CONSTRUCTION
        # loads no native library, which is what lets the no-DISPLAY CI fleet build a
        # real LinuxBackend without importing a lib.
        self._available: "bool | None" = None

    @property
    def platform_id(self) -> str:
        return PLATFORM_LINUX

    def status(self) -> BackendStatus:
        """Whether AT-SPI + X11 can be reached here, cached after the first probe.

        ``linux_ffi.available()`` is itself cached per process and never raises, so
        this is cheap after the first call and loads no library at construction.
        """
        if self._available is None:
            self._available = linux_ffi.available()
        if self._available:
            return BackendStatus(supported=True, platform_id=PLATFORM_LINUX, reason="")
        return BackendStatus(
            supported=False, platform_id=PLATFORM_LINUX, reason=UNAVAILABLE_REASON()
        )

    def probe_permissions(self) -> PermissionProbe:
        """ADVISORY only, and structurally simpler than macOS.

        X11 has no TCC: reading another window's accessibility tree needs no grant,
        and ``XGetImage`` needs no screen-recording permission, so when the client
        loads both are honestly ``granted``. The Linux failure mode that LOOKS like a
        missing permission is a disabled toolkit accessibility BRIDGE — an app whose
        window shows but exposes no controls — so ``responsible_hint`` names the three
        bridge env vars an operator would set, the way the macOS probe names the
        process to grant.
        """
        available = self._available if self._available is not None else linux_ffi.available()
        state = PERMISSION_GRANTED if available else PERMISSION_UNSUPPORTED
        return PermissionProbe(
            accessibility=state,
            screen_recording=state,
            responsible_hint=(
                "If an application's window shows but exposes no controls, enable its "
                "accessibility bridge: GTK_MODULES=gail:atk-bridge (GTK) or "
                "QT_ACCESSIBILITY=1 (Qt), with NO_AT_BRIDGE unset."
            ),
        )

    # ── observation ──

    def list_apps(self) -> DriverResult:
        def run() -> DriverResult:
            return DriverResult(ok=True, apps=apps_linux.list_apps())

        return _guarded("list_apps", run)

    def resolve_app(self, query: str) -> DriverResult:
        def run() -> DriverResult:
            return DriverResult(ok=True, app=apps_linux.resolve_app(query))

        return _guarded("resolve_app", run)

    def launch_app(
        self,
        query: str,
        *,
        permit: "Callable[[LaunchIdentity], str | None] | None" = None,
        refuse_launched: "Callable[[AppRef], str | None] | None" = None,
    ) -> DriverResult:
        """Not implemented on Linux in this cut — a typed refusal, never a raise.

        Resolving a ``.desktop`` entry and bounding its ``Exec`` target the way
        ``launch_windows`` / ``launch_macos`` bound a catalog lookup is a later
        change; starting a process without that bound would be exactly the "run an
        arbitrary program" hole the launch verb's whole design exists to close. So
        the honest answer is a refusal that points at the already-running path,
        rather than a half-built launcher.
        """
        return DriverResult(ok=False, text=_ERR_LAUNCH_NOT_IMPLEMENTED)

    def snapshot(self, app: AppRef, req: SnapshotRequest) -> DriverResult:
        def run() -> DriverResult:
            snap = snapshot_linux.build_snapshot(app, req)
            if req.want_image:
                # A separate step so the secure-field and truncated-walk
                # suppressions live in exactly one place rather than being threaded
                # into the walk. A capture failure degrades to a tree-only snapshot;
                # on X11 ``capture_linux`` is tree-OR-image, never raising.
                snap = capture_linux.capture_snapshot_image(
                    snap, max_px=req.image_max_px, quality=req.image_quality
                )
            return DriverResult(ok=True, app=app, snapshot=snap)

        return _guarded("snapshot", run)

    # ── input ──

    def _addressed(self, app: AppRef, rec: ElementRec, *, for_write: bool = False) -> Any:
        """The element-resolution context manager, with this driver's walk budget.

        Wrapped so every verb resolves an index the SAME way — the numbering only
        means something relative to the walk that produced it. ``for_write`` asks for
        the LIVE secure re-read (``snapshot_linux.addressed``); every verb that writes
        or moves focus passes it, a press/scroll does not.
        """
        return snapshot_linux.addressed(app, rec, self._action_budget(), for_write=for_write)

    @staticmethod
    def _action_budget() -> SnapshotRequest:
        """The walk budget an action's element lookup uses — the ceiling, no image.

        The full ceiling rather than the config default, because a model may address
        an element the full tree exposes: a narrower budget here renumbers the tree
        and refuses an index the model was shown. Matches ``WindowsBackend``.
        """
        return SnapshotRequest(
            max_nodes=MAX_TREE_NODES_LIMIT,
            max_depth=MAX_TREE_DEPTH_LIMIT,
            text_limit=DEFAULT_TEXT_LIMIT,
            want_image=False,
        )

    def _label(self, app: AppRef) -> str:
        """The app spelling used in result/refusal prose."""
        return app.bundle_id or app.name

    def click(
        self,
        app: AppRef,
        rec: "ElementRec | None",
        req: ClickRequest,
    ) -> DriverResult:
        """Activate an element through AT-SPI, post a window event, or warp the cursor.

        ``req.method`` is CONCRETE here (``policy.resolve_click_method`` ran at the
        dispatch chokepoint), so this method never re-chooses one and in particular
        can never select the pointer-warping path on its own. ``sky_click`` and
        ``auto`` + coordinates are REFUSED BY NAME, for the same reasons as Windows:
        ``sky_click`` is a macOS route, and ``auto`` must never resolve onto a
        pointer-moving method.
        """

        def run() -> DriverResult:
            if req.method == CLICK_METHOD_ACCESSIBILITY:
                if rec is None:
                    return DriverResult(ok=False, text=REFUSAL_ACCESSIBILITY_NEEDS_INDEX)
                if req.button != MOUSE_BUTTON_LEFT:
                    return DriverResult(
                        ok=False,
                        text=_REFUSE_ELEMENT_BUTTON.format(
                            button=req.button, index=rec.index, app=self._label(app)
                        ),
                    )
                return self._click_element(app, rec)
            if req.method == CLICK_METHOD_SKY_CLICK:
                return DriverResult(ok=False, text=_REFUSE_SKY_CLICK)
            if req.method == CLICK_METHOD_AUTO:
                # Only reachable with coordinates: ``resolve_click_method`` turns
                # ``auto`` + element_index into ``accessibility`` upstream.
                return DriverResult(ok=False, text=_REFUSE_AUTO_POINT)
            if req.method == CLICK_METHOD_APP_POST:
                if req.point is None:
                    return DriverResult(ok=False, text=ERR_POINT_REQUIRED.format(method=req.method))
                return self._click_app_post(app, rec, req)
            if req.method == CLICK_METHOD_GLOBAL:
                if req.point is None:
                    return DriverResult(ok=False, text=ERR_POINT_REQUIRED.format(method=req.method))
                return self._click_global(app, req)
            return DriverResult(ok=False, text=ERR_UNKNOWN_CLICK_METHOD.format(method=req.method))

        return _guarded("click", run)

    def _click_element(self, app: AppRef, rec: ElementRec) -> DriverResult:
        """The AT-SPI action ladder: no pointer, no focus.

        The toolkit performs the action inside the widget, so the operator's cursor
        and caret are untouched — which is why this is the SAFE form and the one
        ``auto`` resolves to. ``input_linux.perform_activate`` applies the ladder
        rules (specificity order, no fall-through on a FAILED action) and returns the
        action that won, which the result names.
        """
        with self._addressed(app, rec) as obj:
            if obj is None:
                return DriverResult(
                    ok=False, text=_ERR_ELEMENT_GONE.format(index=rec.index, app=self._label(app))
                )
            won = input_linux.perform_activate(obj, rec.role)
        if won is None:
            return DriverResult(
                ok=False, text=_ERR_NO_ACTION.format(index=rec.index, app=self._label(app))
            )
        return DriverResult(ok=True, text=f"pressed element {rec.index} via {won}", app=app)

    def _click_app_post(
        self, app: AppRef, rec: "ElementRec | None", req: ClickRequest
    ) -> DriverResult:
        """Post an X11 button event to the target window — no pointer, maybe ignored.

        ``XSendEvent`` is the only coordinate route that moves no pointer, but many
        toolkits discard a synthetic event (GTK4 does), so the result SAYS the toolkit
        may have ignored it rather than claiming a click landed. Confined by
        ``window_owns_point`` first, exactly like the global path: an event posted at
        a point another window owns would reach the wrong window.
        """
        assert req.point is not None
        x, y = req.point
        if not apps_linux.window_owns_point(app, x, y):
            return DriverResult(
                ok=False,
                text=_ERR_POINT_NOT_OWNED.format(app=self._label(app), x=int(x), y=int(y)),
            )
        button = {"left": 1, "middle": 2, "right": 3}.get(req.button, 1)
        for _ in range(max(1, req.count)):
            linux_ffi.x_send_button(app.window_id, int(x), int(y), button, True)
            linux_ffi.x_send_button(app.window_id, int(x), int(y), button, False)
        linux_ffi.x_flush()
        index = rec.index if rec is not None else -1
        return DriverResult(ok=True, text=_ERR_APP_POST_IGNORED.format(index=index), app=app)

    def _click_global(self, app: AppRef, req: ClickRequest) -> DriverResult:
        """THE pointer-moving path: warp the operator's real cursor and click, via XTest.

        Confined to the authorized window FIRST. An XTest button event lands on
        whatever owns the pixel, so naming an allowed app while passing coordinates
        over a denied one would pass ``policy.check_app`` on app A and click app B.
        ``window_owns_point`` compares the top-level X window the point lands on
        against ``app.window_id`` — a handle comparison.
        """
        assert req.point is not None
        x, y = req.point
        if not apps_linux.window_owns_point(app, x, y):
            return DriverResult(
                ok=False,
                text=_ERR_POINT_NOT_OWNED.format(app=self._label(app), x=int(x), y=int(y)),
            )
        button = {"left": 1, "middle": 2, "right": 3}.get(req.button, 1)
        linux_ffi.xtest_motion(int(x), int(y))
        for _ in range(max(1, req.count)):
            linux_ffi.xtest_button(button, True)
            linux_ffi.xtest_button(button, False)
        linux_ffi.x_flush()
        return DriverResult(
            ok=True,
            text=(
                f"{req.button}-clicked ({int(x)}, {int(y)}) in {self._label(app)} "
                f"{max(1, req.count)}x — the real cursor moved"
            ),
            app=app,
        )

    def drag(self, app: AppRef, req: DragRequest) -> DriverResult:
        """Drag between two screen points, confining EVERY point of the path.

        Coordinate-only by construction — no AT-SPI action expresses a sweep. The
        method must be ``global`` (XTest, real cursor) or ``app_post`` (XSendEvent
        Motion, per-window and toolkit-dependent) EXPLICITLY: ``DragRequest.method``
        defaults to the macOS ``app_post`` whose ``moves_pointer`` is False, so a
        ``global`` drag that was never named would warp the pointer on a gesture the
        SEL audit and the cursor overlay both skipped.

        Every point of the path is confined, not just the endpoints: a ``curved``
        path bows away from the chord, so a stroke between two authorized points near
        a window edge can otherwise travel over the window beside it with the button
        held.
        """

        def run() -> DriverResult:
            if req.method not in (CLICK_METHOD_GLOBAL, CLICK_METHOD_APP_POST):
                return DriverResult(ok=False, text=_REFUSE_DRAG_METHOD.format(method=req.method))
            points = cursor_motion.drag_points(req.start, req.end, steps=req.steps, path=req.path)
            for point in points:
                if not apps_linux.window_owns_point(app, point[0], point[1]):
                    return DriverResult(
                        ok=False,
                        text=_ERR_POINT_NOT_OWNED.format(
                            app=self._label(app), x=int(point[0]), y=int(point[1])
                        ),
                    )
            button = {"left": 1, "middle": 2, "right": 3}.get(req.button, 1)
            if req.method == CLICK_METHOD_GLOBAL:
                self._drag_global(points, button)
                moved = " — the real cursor moved"
            else:
                self._drag_app_post(app, points, button)
                moved = " — posted as X11 window events (the toolkit may ignore them)"
            shape = (
                "" if len(points) <= 2 else DRAG_SHAPE_NOTE.format(count=len(points), path=req.path)
            )
            return DriverResult(
                ok=True,
                text=(
                    f"dragged from ({int(req.start[0])}, {int(req.start[1])}) to "
                    f"({int(req.end[0])}, {int(req.end[1])}){shape}{moved}"
                ),
                app=app,
            )

        return _guarded("drag", run)

    @staticmethod
    def _drag_global(points: tuple, button: int) -> None:
        """XTest press at the first point, motion through the path, release at the last."""
        linux_ffi.xtest_motion(int(points[0][0]), int(points[0][1]))
        linux_ffi.xtest_button(button, True)
        for point in points[1:]:
            linux_ffi.xtest_motion(int(point[0]), int(point[1]))
        linux_ffi.xtest_button(button, False)
        linux_ffi.x_flush()

    @staticmethod
    def _drag_app_post(app: AppRef, points: tuple, button: int) -> None:
        """XSendEvent button-down at the first point, MotionNotify through every
        interior point, button-up at the last — per window, no pointer movement.

        The interior motion is what makes a ``curved`` path mean anything on this
        route: with only the two button events the toolkit sees a click-and-release at
        two places, never a stroke. Every point was already confined by the caller.
        """
        sx, sy = int(points[0][0]), int(points[0][1])
        ex, ey = int(points[-1][0]), int(points[-1][1])
        linux_ffi.x_send_button(app.window_id, sx, sy, button, True)
        for point in points[1:-1]:
            linux_ffi.x_send_motion(app.window_id, int(point[0]), int(point[1]), button)
        linux_ffi.x_send_button(app.window_id, ex, ey, button, False)
        linux_ffi.x_flush()

    def _focus(self, app: AppRef, rec: ElementRec) -> "tuple[Any, str | None]":
        """Resolve *rec* and make sure keyboard focus is on it. Returns ``(obj, refusal_or_None)``.

        ``for_write=True`` so a field that masks its value refuses the FOCUS rather
        than the typing — a secure re-read against the live element. A focus failure
        REFUSES, because the policy check cleared the element the model ADDRESSED and
        keystrokes delivered to a focus that did not move reach whatever the app had
        focused — possibly the password field that check refused.

        Focus is VERIFIED BY STATE, never by ``grab_focus``'s return. Measured on GTK
        4.16: ``atspi_component_grab_focus`` on the element that ALREADY has focus
        fails with ``atspi_error (1)``, so trusting the return refused every keystroke
        into the one field a user would type into. The same read makes the check
        honest in the other direction, exactly as the Windows driver's
        ``element_has_focus`` does: a provider can accept the request and leave focus
        where it was, and a True that was not read back is an unverified aim.

        Two levels have to agree, because XTest is GLOBAL. An element can hold its
        window's focus while that window is in the background — every toplevel
        remembers its focused widget — and the X server delivers synthesized keys to
        the ACTIVE window, not to the one the model named. So the frame must be the
        active X window too (``_NET_ACTIVE_WINDOW`` compared by handle); when the
        property is absent (measured: gnome-shell publishes none while the Activities
        overview is up) nothing can be proven and the keystrokes are refused.
        """
        with self._addressed(app, rec, for_write=True) as obj:
            if obj is None:
                return None, _ERR_ELEMENT_GONE.format(index=rec.index, app=self._label(app))
            refusal = self._verify_keyboard_target(app, obj, rec)
            return (None, refusal) if refusal else (obj, None)

    def _verify_keyboard_target(self, app: AppRef, obj: Any, rec: ElementRec) -> "str | None":
        """Make *obj* the keyboard target and PROVE it, or return the refusal.

        The one place both keystroke verbs (``type_text``'s fallback and
        ``press_key``) decide whether XTest may fire. See :meth:`_focus` for why the
        state is read back instead of trusting ``grab_focus`` and why the window has
        to be the active one as well.
        """
        focused = linux_ffi.has_state(obj, linux_ffi.ATSPI_STATE_FOCUSED)
        if not focused:
            linux_ffi.grab_focus(obj)  # return deliberately ignored: the state decides
            focused = linux_ffi.has_state(obj, linux_ffi.ATSPI_STATE_FOCUSED)
        if not focused:
            return _ERR_FOCUS_FAILED.format(index=rec.index, app=self._label(app))
        if not app.window_id or linux_ffi.x_active_window() != app.window_id:
            return _ERR_WINDOW_NOT_ACTIVE.format(app=self._label(app))
        return None

    def type_text(self, app: AppRef, rec: "ElementRec | None", text: str) -> DriverResult:
        """Type *text* — through ``EditableText`` when editable (no focus change), else keystrokes.

        The preferred route writes through the accessibility ``EditableText``
        interface with NO focus steal, and says so. When the element is not editable,
        focus is taken (and the result says that too) and the text is typed with
        XTest keystrokes; a focus failure REFUSES rather than typing blind.
        """

        def run() -> DriverResult:
            if rec is not None:
                # Try the focus-free EditableText write first. ``for_write=True`` so a
                # field that started masking refuses here.
                with self._addressed(app, rec, for_write=True) as obj:
                    if obj is None:
                        return DriverResult(
                            ok=False,
                            text=_ERR_ELEMENT_GONE.format(index=rec.index, app=self._label(app)),
                        )
                    if input_linux.write_text_via_atspi(obj, text):
                        return DriverResult(
                            ok=True,
                            text=(
                                f"typed {len(text)} character(s) into element "
                                f"{rec.index}{input_linux.NO_FOCUS_NOTE}"
                            ),
                            app=app,
                        )
                    # Not editable: the keystroke route. Same verification as
                    # ``_focus`` (state, then active window), for the same reason —
                    # XTest follows the server's focus, not the element we named.
                    refusal = self._verify_keyboard_target(app, obj, rec)
                if refusal:
                    return DriverResult(ok=False, text=refusal)
            elif not app.window_id or linux_ffi.x_active_window() != app.window_id:
                # No element named: the keystrokes go to whatever the app has focused,
                # which is only THIS app's control if its window is the active one.
                return DriverResult(
                    ok=False, text=_ERR_WINDOW_NOT_ACTIVE.format(app=self._label(app))
                )
            sent = input_linux.type_text_via_xtest(text)
            target = "the focused element" if rec is None else f"element {rec.index}"
            if sent < len(text):
                return DriverResult(
                    ok=False, text=_ERR_TEXT_SHORT.format(sent=sent, total=len(text))
                )
            return DriverResult(
                ok=True,
                text=f"typed {len(text)} character(s) into {target}{input_linux.FOCUS_NOTE}",
                app=app,
            )

        return _guarded("type_text", run)

    def press_key(self, app: AppRef, rec: "ElementRec | None", key: str) -> DriverResult:
        """Send one key chord through XTest, taking keyboard focus when an element is named.

        The spec is re-parsed through ``keymap.parse_spec`` rather than trusted: this
        driver is reachable from the CLI harness and from tests, and a keystroke must
        never be synthesized from an unvalidated string. A ``fn``/``capslock``
        modifier is refused here (``input_linux.LinuxInputError``) with the reason
        and the alternative, mirroring the Windows refusal.
        """

        def run() -> DriverResult:
            spec = keymap.parse_spec(key)
            note = ""
            if rec is not None:
                _obj, refusal = self._focus(app, rec)
                if refusal:
                    return DriverResult(ok=False, text=refusal)
                note = input_linux.FOCUS_NOTE
            elif not app.window_id or linux_ffi.x_active_window() != app.window_id:
                # No element named: the chord goes to the server's focus. Measured live
                # on the merged tree WITHOUT this check: ``press_key(calculator, None,
                # "9")`` reported success while the calculator was in the background,
                # and the "9" landed in the text editor's document. The policy check
                # authorized the calculator; the keystroke reached another application.
                return DriverResult(
                    ok=False, text=_ERR_WINDOW_NOT_ACTIVE.format(app=self._label(app))
                )
            try:
                input_linux.send_key_chord(spec)
            except input_linux.LinuxInputError as exc:
                return DriverResult(ok=False, text=str(exc))
            return DriverResult(ok=True, text=f"sent {key}{note}", app=app)

        return _guarded("press_key", run)

    def set_value(self, app: AppRef, rec: ElementRec, value: str) -> DriverResult:
        """Write a field through ``EditableText`` — no focus, no pointer.

        THE focus-free text route: ``set_text_contents`` replaces the field's
        contents through the accessibility interface, so neither the caret nor the
        cursor moves. ``for_write=True`` is a live secure re-read, because this is the
        verb that writes and the field may have started masking since the model saw it.
        """

        def run() -> DriverResult:
            with self._addressed(app, rec, for_write=True) as obj:
                if obj is None:
                    return DriverResult(
                        ok=False,
                        text=_ERR_ELEMENT_GONE.format(index=rec.index, app=self._label(app)),
                    )
                if not linux_ffi.set_text_contents(obj, value):
                    return DriverResult(
                        ok=False,
                        text=_ERR_NO_SET_VALUE.format(index=rec.index, app=self._label(app)),
                    )
            return DriverResult(ok=True, text=f"set element {rec.index}", app=app)

        return _guarded("set_value", run)

    def scroll(self, app: AppRef, rec: ElementRec, direction: str, pages: float) -> DriverResult:
        """Scroll an element — a named AT-SPI scroll action if present, else X11 wheel events.

        No pointer movement either way: a ``scroll*`` AT-SPI action is performed in
        the widget, and the fallback posts Button4/5 (vertical) or 6/7 (horizontal)
        wheel events to the window with ``XSendEvent``. ``pages`` becomes a repeat
        count (round up, min 1), matching the Windows per-page semantics.
        """

        def run() -> DriverResult:
            wanted = direction.strip().lower()
            if wanted not in SCROLL_DIRECTIONS:
                return DriverResult(
                    ok=False,
                    text=(
                        f"unknown scroll direction {direction!r}. Supported: "
                        f"{', '.join(SCROLL_DIRECTIONS)}"
                    ),
                )
            steps = max(1, int(abs(pages) + 0.999))
            button = input_linux.SCROLL_BUTTONS[wanted]
            with self._addressed(app, rec) as obj:
                if obj is None:
                    return DriverResult(
                        ok=False,
                        text=_ERR_ELEMENT_GONE.format(index=rec.index, app=self._label(app)),
                    )
                extents = linux_ffi.extents(obj)
                if extents is None:
                    return DriverResult(
                        ok=False,
                        text=_ERR_NO_SCROLL.format(index=rec.index, app=self._label(app)),
                    )
            cx = int(extents[0] + extents[2] / 2)
            cy = int(extents[1] + extents[3] / 2)
            for _ in range(steps):
                linux_ffi.x_send_button(app.window_id, cx, cy, button, True)
                linux_ffi.x_send_button(app.window_id, cx, cy, button, False)
            linux_ffi.x_flush()
            return DriverResult(
                ok=True, text=f"scrolled element {rec.index} {wanted} {steps} page(s)", app=app
            )

        return _guarded("scroll", run)

    def perform_action(self, app: AppRef, rec: ElementRec, action: str) -> DriverResult:
        """Perform one named AT-SPI action — a CLOSED vocabulary of the element's OWN actions.

        AT-SPI action names are toolkit strings (``click``, ``press``, ``activate``,
        ``toggle``, ``expand or contract``, …), so there is no fixed table the way the
        Windows ``perform_action`` has one. The vocabulary is instead the record's own
        ``actions`` list: an action is accepted only if the live element advertises it,
        which keeps this from being a free-form write path.
        """

        def run() -> DriverResult:
            wanted = (action or "").strip().lower()
            # for_write: a live secure re-read. On Linux the vocabulary is the
            # element's OWN AT-SPI actions, and for a text field those include
            # ``clipboard.copy`` / ``clipboard.paste`` / ``selection.delete`` — content
            # reads and writes — so a field that flipped plain→password after the
            # snapshot must refuse here exactly as it does in ``set_value``. (The
            # Windows ``perform_action`` has a closed press/toggle/select/expand table
            # with no content verb, which is why it does not need this.)
            with self._addressed(app, rec, for_write=True) as obj:
                if obj is None:
                    return DriverResult(
                        ok=False,
                        text=_ERR_ELEMENT_GONE.format(index=rec.index, app=self._label(app)),
                    )
                available = linux_ffi.actions(obj)
                lowered = {name.strip().lower(): i for i, name in enumerate(available)}
                if wanted not in lowered:
                    return DriverResult(
                        ok=False,
                        text=_ERR_UNKNOWN_ACTION.format(
                            action=action,
                            index=rec.index,
                            supported=", ".join(available) or "(none)",
                        ),
                    )
                ok = linux_ffi.do_action(obj, lowered[wanted])
            if not ok:
                return DriverResult(
                    ok=False,
                    text=_ERR_ACTION_FAILED.format(
                        action=wanted, index=rec.index, app=self._label(app)
                    ),
                )
            return DriverResult(ok=True, text=f"{wanted} element {rec.index}", app=app)

        return _guarded("perform_action", run)

    def close(self) -> None:
        """Release the X display and the AT-SPI connection. Safe to call repeatedly.

        ``linux_ffi.shutdown`` is idempotent (``XCloseDisplay`` + ``atspi_exit`` only
        when initialised), so a close before anything loaded, or a second close, is a
        no-op. A failure must not pin the driver in place during a backend swap.
        """
        try:
            linux_ffi.shutdown()
        except Exception:
            logger.debug("releasing the Linux computer-use client failed", exc_info=True)
