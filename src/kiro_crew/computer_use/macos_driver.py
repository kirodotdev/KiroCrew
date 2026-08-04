"""The macOS :class:`ComputerUseBackend` — thin glue over the Stage-2 modules.

Deliberately thin. Discovery lives in :mod:`apps_macos`, the walk in
:mod:`snapshot_macos`, capture in :mod:`capture_macos`, the FFI in
:mod:`macos_ffi`, and every *policy* decision lives upstream at the dispatch
chokepoint. This class only translates the ABC's method signatures into those
calls and, critically, converts every failure into a :class:`DriverResult`.

**No exception crosses this seam.** Each public method wraps its work in
:func:`_guarded`. That is not defensive habit: the MCP loop dispatches tool calls
on a worker thread, and an exception escaping into it takes the call down, while an
un-caught ``ComputerUseUnsupported`` at import time would make the whole backend
look broken rather than the one action.

Two design notes that a reader will otherwise question:

* **``element`` handles are not cached across calls.** A mutating action re-walks
  the tree to find the element at the requested index. Holding a
  ``AXUIElement`` pointer between tool calls would mean acting on a stale handle
  whose widget may have been destroyed — a use-after-free in another process's
  address space, not a Python error. Re-walking costs 25-70ms and is what makes
  the caller's fingerprint check meaningful, since the fingerprint must come from
  the same walk as the element being acted on.
* **Three click paths, and only one of them touches the operator's cursor.**
  ``accessibility`` is ``AXUIElementPerformAction(elem, "AXPress")`` — no pointer
  involved at all, and still the default whenever an element index is available.
  ``app_post`` posts a located mouse event with ``CGEventPostToPid``, so the target
  app sees a click at a point while the physical cursor stays put (verified live).
  ``global`` is the one path that warps the real cursor
  (``CGWarpMouseCursorPosition`` + a global ``CGEventPost``); it exists because
  some UI is only reachable by a physical click, and it is reachable only when the
  model NAMES it — ``policy.resolve_click_method`` never resolves ``auto`` onto it.
  This driver does not evaluate that; it is settled at the dispatch chokepoint
  before ``req.moves_pointer`` can be True here. A driver must therefore never
  upgrade a method on its own.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Callable, Iterator

from kiro_crew.computer_use import (
    apps_macos,
    capture_macos,
    keymap,
    macos_ffi,
    macos_skylight,
    permissions,
    snapshot_macos,
)
from kiro_crew.computer_use.backend import ComputerUseBackend
from kiro_crew.computer_use.types import (
    AX_MENU_LADDER,
    AX_PRESS_ACTION,
    AX_PRESS_LADDER,
    AX_VALUE,
    CLICK_METHOD_ACCESSIBILITY,
    CLICK_METHOD_APP_POST,
    CLICK_METHOD_GLOBAL,
    CLICK_METHOD_SKY_CLICK,
    ERR_ACTION_FAILED,
    ERR_POINT_NOT_OWNED,
    ERR_POINT_REQUIRED,
    ERR_UNKNOWN_CLICK_METHOD,
    MAX_ANCESTOR_AREA_RATIO,
    MAX_ANCESTOR_PRESS_HOPS,
    MOUSE_BUTTON_LEFT,
    MOUSE_BUTTON_RIGHT,
    PERMISSION_UNKNOWN,
    PLATFORM_MACOS,
    REFUSAL_ACCESSIBILITY_NEEDS_INDEX,
    SCROLL_DOWN,
    SCROLL_LEFT,
    SCROLL_RIGHT,
    SCROLL_UP,
    AppRef,
    BackendStatus,
    ClickRequest,
    ComputerUseError,
    DragRequest,
    DriverResult,
    ElementRec,
    PermissionProbe,
    SnapshotRequest,
)

# The AX attribute set so typed keystrokes land in the addressed element rather
# than wherever the target app last had focus.
AX_FOCUSED = "AXFocused"

logger = logging.getLogger(__name__)

# The accessibility action tried for each scroll direction BEFORE falling back to
# a synthesized wheel event. Tried first because an AX scroll is scoped to the
# addressed element, while a wheel event goes to whatever the target app decides is
# under its scroll focus.
_SCROLL_ACTIONS: dict[str, str] = {
    SCROLL_UP: "AXScrollUpByPage",
    SCROLL_DOWN: "AXScrollDownByPage",
    SCROLL_LEFT: "AXScrollLeftByPage",
    SCROLL_RIGHT: "AXScrollRightByPage",
}
# Wheel deltas per page for each direction. macOS wheel deltas are inverted
# relative to the reading direction: a NEGATIVE axis-1 delta scrolls the content
# DOWN (the gesture of pushing the wheel away from you).
_SCROLL_DELTAS: dict[str, tuple[int, int]] = {
    SCROLL_UP: (1, 0),
    SCROLL_DOWN: (-1, 0),
    SCROLL_LEFT: (0, 1),
    SCROLL_RIGHT: (0, -1),
}
# Wheel-event page deltas are integers, so a fractional ``pages`` request is
# rounded up to at least one page — asking to scroll and being told "ok" while
# nothing moved is worse than over-scrolling by a fraction.
_MIN_WHEEL_PAGES = 1

_REASON_UNSUPPORTED_TARGET = "element advertises no {action} action and none succeeded"
_REASON_NO_ELEMENT = "element {index} is no longer present in '{app}'"
_REASON_AX_ERROR = "accessibility error {code}"
# Keyboard input whose addressed element could not be focused. ACTIONABLE on purpose
# (it names the fix) because the alternative — typing into whatever the app happened
# to have focused — is how a keystroke reaches a password field that
# ``policy.check_input_target`` already refused.
_REASON_FOCUS_FAILED = (
    "could not focus element {index} of '{app}' (accessibility error {code}), so the "
    "keystrokes were NOT sent — they would have gone to whatever that app currently "
    "has focused, which is not the target you named. Call computer_get_state again "
    "and address a focusable control, or click it first"
)
# ``sky_click`` routes by window id, so a vanished window is a distinct failure from
# a vanished element: the model must re-snapshot to get a live id, and telling it
# that is what stops it retrying the same dead id.
_REASON_SKY_WINDOW_GONE = (
    "the target window is no longer on screen (or no longer belongs to that app), so "
    "click_method 'sky_click' has no window to address. Call computer_get_state again"
)


class MacOSBackend(ComputerUseBackend):
    """Accessibility + CoreGraphics driver for macOS.

    Constructing this class does NOT load a framework: the FFI layer binds lazily
    on first real use, so a ``MacOSBackend()`` on a machine where
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

    # ── observation ──

    def list_apps(self) -> DriverResult:
        def run() -> DriverResult:
            return DriverResult(ok=True, apps=apps_macos.list_apps())

        return _guarded("list_apps", run)

    def resolve_app(self, query: str) -> DriverResult:
        def run() -> DriverResult:
            return DriverResult(ok=True, app=apps_macos.resolve_app(query))

        return _guarded("resolve_app", run)

    def snapshot(self, app: AppRef, req: SnapshotRequest) -> DriverResult:
        def run() -> DriverResult:
            snap = snapshot_macos.build_snapshot(app, req)
            if req.want_image:
                # Capture is a separate step so the secure-field suppression rule
                # lives in exactly one place (``capture_snapshot_image`` refuses on
                # ``has_secure``) instead of being an argument threaded into the
                # walk. A capture failure degrades to a tree-only snapshot.
                snap = capture_macos.capture_snapshot_image(
                    snap, max_px=req.image_max_px, quality=req.image_quality
                )
            return DriverResult(ok=True, app=app, snapshot=snap)

        return _guarded("snapshot", run)

    # ── input ──

    def click(
        self,
        app: AppRef,
        rec: "ElementRec | None",
        req: ClickRequest,
    ) -> DriverResult:
        """Dispatch one click onto the method *req* already resolved to.

        ``req.method`` is CONCRETE here — ``auto`` was resolved at the dispatch
        chokepoint (``policy.resolve_click_method``), so this method never chooses
        one and in particular can never select the pointer-warping path on its own.
        An unrecognised method is REFUSED rather than falling back to
        accessibility: a fallback would silently perform a different gesture than
        the caller asked for.
        """

        def run() -> DriverResult:
            if req.method == CLICK_METHOD_ACCESSIBILITY:
                return self._click_accessibility(app, rec, req.button)
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
                        text=ERR_POINT_NOT_OWNED.format(
                            app=app.name, x=int(req.point[0]), y=int(req.point[1])
                        ),
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
        """Drag between two screen points, app-scoped unless *req* moves the pointer."""

        def run() -> DriverResult:
            if req.method == CLICK_METHOD_GLOBAL:
                # BOTH endpoints, for the same reason as the click above: a sweep
                # that starts inside the authorized window and ends over a denied
                # app would otherwise release the button there.
                for point in (req.start, req.end):
                    if not apps_macos.pid_owns_point(app.pid, point[0], point[1]):
                        return DriverResult(
                            ok=False,
                            text=ERR_POINT_NOT_OWNED.format(
                                app=app.name, x=int(point[0]), y=int(point[1])
                            ),
                        )
                macos_ffi.post_mouse_drag_global(req.start, req.end, button=req.button)
            elif req.method == CLICK_METHOD_APP_POST:
                macos_ffi.post_mouse_drag(app.pid, req.start, req.end, button=req.button)
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

    def _click_accessibility(
        self, app: AppRef, rec: "ElementRec | None", button: str = MOUSE_BUTTON_LEFT
    ) -> DriverResult:
        """Activate the addressed element through accessibility — no pointer at all.

        Tries a LADDER of actions rather than ``AXPress`` alone. A single press was
        the biggest avoidable refusal in the driver: ``AXPress`` returns
        ``-25206`` (action unsupported) on a disclosure triangle, a Finder row and
        many web controls, all of which answer ``AXConfirm`` or ``AXOpen`` — so the
        model was told "the click failed" for elements that were perfectly
        clickable, and its only remaining move was to guess coordinates.

        A RIGHT button takes a different ladder (``AXShowMenu`` only) and never
        falls back to a press: quietly turning "open the context menu" into
        "activate the control" would perform a different action than the model
        asked for, which is the same class of defect as
        :func:`keymap.parse_key` refusing an unknown modifier.

        The winning action is NAMED in the result, so a model that sees ``AXOpen``
        learns what this element responds to instead of re-deriving it next turn.
        """
        if rec is None:
            return DriverResult(ok=False, text=REFUSAL_ACCESSIBILITY_NEEDS_INDEX)
        ladder = AX_MENU_LADDER if button == MOUSE_BUTTON_RIGHT else AX_PRESS_LADDER
        with _addressed(app, rec) as element:
            if element is None:
                return _missing_element(app, rec)
            # ``advertised`` is what the element SAYS it supports. Preferred over
            # blind attempts so an unsupported action is skipped without a
            # round-trip — but an advertised action can still fail, so the result
            # code is what decides, not the advertisement.
            advertised = set(rec.actions or ())
            last_action, last_err = ladder[0], macos_ffi.AX_ERR_ACTION_UNSUPPORTED
            for action in ladder:
                if advertised and action not in advertised:
                    continue
                err = macos_ffi.ax_perform(element, action)
                if err == macos_ffi.AX_ERR_SUCCESS:
                    return DriverResult(ok=True, text=_pressed_text(rec.index, action), app=app)
                last_action, last_err = action, err
                if err != macos_ffi.AX_ERR_ACTION_UNSUPPORTED:
                    # A REAL failure (a wedged app, a destroyed widget), not "wrong
                    # verb" — trying the next verb would report the wrong cause.
                    break
            else:
                # Nothing in the ladder was advertised at all: attempt the primary
                # verb anyway. Advertisement is unreliable on web content, where a
                # node can perform a press it never listed.
                if not any(a in advertised for a in ladder):
                    last_err = macos_ffi.ax_perform(element, ladder[0])
                    last_action = ladder[0]
                    if last_err == macos_ffi.AX_ERR_SUCCESS:
                        return DriverResult(
                            ok=True, text=_pressed_text(rec.index, last_action), app=app
                        )
            # LAST rung: the element itself is unpressable, so try the container
            # that visually IS it. Web content renders a clickable row as a plain
            # text node inside a pressable ancestor, which leaves the whole row
            # dead to an element click while a coordinate click on it works fine —
            # and coordinates are exactly what this path exists to avoid.
            #
            # Only for a LEFT click, and only when the element carries a frame to
            # compare against: without geometry there is no way to tell the row
            # from the page, and pressing the page would activate something the
            # model never addressed. See :func:`_ancestor_press`.
            if button != MOUSE_BUTTON_RIGHT and last_err in (
                macos_ffi.AX_ERR_ACTION_UNSUPPORTED,
                macos_ffi.AX_ERR_ATTRIBUTE_UNSUPPORTED,
            ):
                pressed = _ancestor_press(element, rec)
                if pressed:
                    return DriverResult(ok=True, text=_ancestor_pressed_text(rec.index), app=app)
        return _action_failed(app, rec, last_action, last_err)

    def type_text(self, app: AppRef, rec: "ElementRec | None", text: str) -> DriverResult:
        def run() -> DriverResult:
            if rec is not None:
                with _addressed(app, rec) as element:
                    if element is None:
                        return _missing_element(app, rec)
                    # Focus the addressed element first, so the keystrokes land
                    # where the model aimed rather than wherever the target app
                    # last had focus.
                    focus_err = macos_ffi.ax_set_true(element, AX_FOCUSED)
                # REFUSE on a focus failure — do not fall through to app-scoped
                # input. ``policy.check_input_target`` cleared the element the model
                # ADDRESSED (that is where ``ElementRec.secure`` comes from), so if
                # focus did not move, ``post_text`` delivers to whatever the app
                # already had focused — which can be the password field the check
                # just refused to write into. Typing blind past a failed aim is the
                # one case where "best effort" defeats the secure-field floor.
                if focus_err != macos_ffi.AX_ERR_SUCCESS:
                    return _focus_failed(app, rec, focus_err)
            # Unicode key events rather than a per-character keycode lookup: this
            # is layout-independent and can emit characters the US layout cannot
            # reach in one keystroke. Verified byte-identical round-trip.
            macos_ffi.post_text(app.pid, text)
            target = "the focused element" if rec is None else f"element {rec.index}"
            return DriverResult(
                ok=True, text=f"typed {len(text)} character(s) into {target}", app=app
            )

        return _guarded("type_text", run)

    def press_key(self, app: AppRef, rec: "ElementRec | None", key: str) -> DriverResult:
        def run() -> DriverResult:
            # ``parse_key`` raises KeyParseError (a ComputerUseError) for an
            # unknown key or modifier, which ``_guarded`` turns into a refusal.
            # Refusing loudly is right: silently dropping an unrecognised modifier
            # would send a DIFFERENT keystroke than requested into a live app.
            keycode, flags = keymap.parse_key(key)
            if rec is not None:
                # Focus the addressed element first, same as ``type_text``, and
                # best-effort for the same reason.
                with _addressed(app, rec) as element:
                    if element is None:
                        return _missing_element(app, rec)
                    focus_err = macos_ffi.ax_set_true(element, AX_FOCUSED)
                # Same hole, same fix, the other keyboard verb — a keystroke
                # delivered to a focus the model did not address can reach the
                # secure field the policy check just cleared us away from.
                if focus_err != macos_ffi.AX_ERR_SUCCESS:
                    return _focus_failed(app, rec, focus_err)
            macos_ffi.post_key(app.pid, keycode, flags)
            return DriverResult(ok=True, text=f"sent {key}", app=app)

        return _guarded("press_key", run)

    def set_value(self, app: AppRef, rec: ElementRec, value: str) -> DriverResult:
        def run() -> DriverResult:
            with _addressed(app, rec) as element:
                if element is None:
                    return _missing_element(app, rec)
                err = macos_ffi.ax_set_str(element, AX_VALUE, value)
            if err != macos_ffi.AX_ERR_SUCCESS:
                return _action_failed(app, rec, f"set {AX_VALUE}", err)
            return DriverResult(ok=True, text=f"set element {rec.index}", app=app)

        return _guarded("set_value", run)

    def scroll(self, app: AppRef, rec: ElementRec, direction: str, pages: float) -> DriverResult:
        def run() -> DriverResult:
            action = _SCROLL_ACTIONS.get(direction)
            if action is None:
                return DriverResult(ok=False, text=f"unknown scroll direction '{direction}'")
            whole_pages = max(_MIN_WHEEL_PAGES, int(round(pages)))
            with _addressed(app, rec) as element:
                if element is None:
                    return _missing_element(app, rec)
                # Try the AX action first — it is element-scoped. But NEVER trust
                # the advertised action list: a real AXScrollArea listed
                # ``AXScrollDownByPage`` and then returned
                # ``kAXErrorActionUnsupported`` when asked to perform it. So the
                # advertised list is not even consulted; we just try and check.
                err = macos_ffi.AX_ERR_SUCCESS
                for _ in range(whole_pages):
                    err = macos_ffi.ax_perform(element, action)
                    if err != macos_ffi.AX_ERR_SUCCESS:
                        break
            if err == macos_ffi.AX_ERR_SUCCESS:
                return DriverResult(
                    ok=True,
                    text=f"scrolled element {rec.index} {direction} {whole_pages} page(s)",
                    app=app,
                )

            # Fall back to a synthesized wheel event, posted to the target pid with
            # the private event source.
            # The wheel event is in LINE units — ``CGScrollEventUnit`` has only
            # ``Pixel`` and ``Line``, no page unit — so a requested PAGE has to be
            # expressed as a line count. Sending the raw page number scrolled one
            # single line and reported "scrolled 1 page(s)", which is the kind of
            # silent no-op that makes an agent loop.
            delta_y, delta_x = _SCROLL_DELTAS[direction]
            lines = whole_pages * macos_ffi.CG_SCROLL_LINES_PER_PAGE
            macos_ffi.post_scroll(app.pid, delta_y * lines, delta_x * lines)
            return DriverResult(
                ok=True,
                text=(
                    f"scrolled '{app.name}' {direction} {whole_pages} page(s) with a wheel "
                    f"event (the accessibility action was refused with error {err})"
                ),
                app=app,
            )

        return _guarded("scroll", run)

    def perform_action(self, app: AppRef, rec: ElementRec, action: str) -> DriverResult:
        def run() -> DriverResult:
            with _addressed(app, rec) as element:
                if element is None:
                    return _missing_element(app, rec)
                err = macos_ffi.ax_perform(element, action)
            if err != macos_ffi.AX_ERR_SUCCESS:
                return _action_failed(app, rec, action, err)
            return DriverResult(ok=True, text=f"performed {action} on element {rec.index}", app=app)

        return _guarded("perform_action", run)

    def close(self) -> None:
        """Release the FFI handles, the event source and the identity cache.

        Safe to call repeatedly. Called by ``reset_shared_backend``, which is also
        what drops the snapshot index — element indices from one driver's walk are
        meaningless against another's.
        """
        try:
            apps_macos.reset_identity_cache()
        except Exception:
            logger.debug("identity cache reset failed", exc_info=True)
        try:
            macos_ffi.reset_frameworks()
        except Exception:
            logger.debug("framework reset failed", exc_info=True)


@contextmanager
def _addressed(app: AppRef, rec: ElementRec) -> "Iterator[object | None]":
    """Yield the live element at ``rec.index``, or ``None``; always releases it.

    Re-resolves on every call rather than caching a handle across tool calls: a
    stored ``AXUIElement`` may point at a widget the target app has since
    destroyed, and acting on it is a use-after-free in another process's address
    space rather than a Python error.

    A context manager because the resolution creates several CoreFoundation
    references (the application element, the window, the element itself) and every
    one is a Create-Rule +1 that must be released — skipping them measured at
    ~400KB of permanent growth per walk. ``snapshot_macos.resolve_element`` owns
    that bookkeeping; this wrapper adds the identity check.

    ``None`` is yielded when the index no longer resolves OR when the control now
    at that index is not the one the model addressed
    (:func:`_same_identity`). The addressing walk uses the widest budgets rather
    than the original request's, so the two numberings are not guaranteed
    identical when the original walk was truncated — the identity check, not the
    index alone, is what makes this safe. It is a second line of defence in any
    case: the dispatch chokepoint already refuses on fingerprint drift.
    """
    with snapshot_macos.resolve_element(app, rec) as (element, found):
        if element is None or found is None or not _same_identity(found, rec):
            yield None
            return
        yield element


def _ancestor_press(element: object, rec: ElementRec) -> bool:
    """Press the nearest ancestor that plausibly IS the control *rec* names.

    The last rung of the click ladder, and the one that recovers web content. A
    clickable row in a WebArea is frequently a plain ``AXStaticText`` inside a
    pressable wrapper: the text node advertises no actions and refuses every verb,
    so an element click reports failure for a row a human clicks without thinking —
    and the model's only remaining move is to guess coordinates, which is what this
    whole path exists to avoid.

    **Two guards, because an unguarded version is a wrong-click generator.**

    * *Bounded hops* (:data:`MAX_ANCESTOR_PRESS_HOPS`). Walking to the top would
      eventually find something pressable — the page, the window — and press it.
    * *Bounded area* (:data:`MAX_ANCESTOR_AREA_RATIO`). The real signal that an
      ancestor is "the same control" is that it is roughly the same SIZE. A row
      wrapping a text node is a small multiple of its area; a scroll area or page
      body is orders of magnitude larger. Without the target's own frame there is
      nothing to compare against, so the fallback declines rather than guessing.

    Only ``AXPress`` is attempted, not the full ladder: ``AXOpen`` on a container
    can mean something quite different from activating the row inside it
    (open the folder vs. select the file), and this rung is already a heuristic —
    stacking a second one on top of it widens the blast radius for no clear gain.

    Every ancestor reference is released, including on the success path.
    """
    if rec.frame is None:
        return False
    target_area = _area(rec.frame)
    if target_area <= 0:
        # A zero-area target makes the ratio test meaningless (everything is
        # infinitely larger), so there is no way to bound the walk. Decline.
        return False
    current = element
    owned: list = []
    try:
        for _ in range(MAX_ANCESTOR_PRESS_HOPS):
            parent = macos_ffi.ax_parent(current)
            if parent is None:
                return False
            owned.append(parent)
            current = parent
            frame = macos_ffi.ax_frame(parent)
            if frame is None:
                # No geometry to judge by: keep climbing rather than pressing
                # blind. A wrapper with no frame is common in web content and its
                # own parent may well have one.
                continue
            if _area(frame) > target_area * MAX_ANCESTOR_AREA_RATIO:
                # Too big to be the control the model addressed, and every further
                # ancestor is bigger still — stop, do not keep climbing.
                return False
            if macos_ffi.ax_perform(parent, AX_PRESS_ACTION) == macos_ffi.AX_ERR_SUCCESS:
                return True
        return False
    finally:
        macos_ffi.release_all(owned)


def _area(frame: tuple[float, float, float, float]) -> float:
    """Area of a ``(x, y, width, height)`` rect, clamped at 0.

    Clamped because AX occasionally reports a negative dimension for an off-screen
    or collapsed element, and a negative area would compare as "smaller than
    everything" and wave the ancestor guard through.
    """
    return max(0.0, frame[2]) * max(0.0, frame[3])


def _same_identity(found: ElementRec, expected: ElementRec) -> bool:
    """True when the record at an index is still the control the model addressed.

    Role, subrole and title only — deliberately NOT ``value``. A text field's value
    changes as the user types without the control's identity changing at all, and
    folding it in would refuse almost every legitimate action. This mirrors
    ``render.fingerprint``'s field selection for the same reason, and neither reads
    a secure record's bytes.
    """
    return (
        found.role == expected.role
        and found.subrole == expected.subrole
        and found.title == expected.title
    )


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


def _pressed_text(index: int, action: str) -> str:
    """Result prose that NAMES the winning action.

    The verb is included so a model that needed ``AXOpen`` on a Finder row learns
    that from the result instead of rediscovering the ladder on the next element of
    the same kind.
    """
    verb = "pressed" if action == AX_PRESS_ACTION else f"activated via {action}"
    return f"{verb} element {index}"


def _ancestor_pressed_text(index: int) -> str:
    """Result prose for the ancestor fallback — SAYS that it was the container.

    Not reported as a plain "pressed element N". The model has to know the press
    landed on the enclosing control rather than the node it addressed, because that
    is the difference between "my click worked" and "something near my click
    worked": if the observable outcome is wrong, this sentence is the only clue as
    to why, and without it the model would re-try an action that already succeeded
    on the wrong target.
    """
    return (
        f"element {index} could not be activated directly; pressed its enclosing "
        "control instead — verify the result is what you intended"
    )


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


def _missing_element(app: AppRef, rec: ElementRec) -> DriverResult:
    """Refusal for an index that no longer resolves to an element."""
    return DriverResult(
        ok=False,
        text=_REASON_NO_ELEMENT.format(index=rec.index, app=app.bundle_id or app.name),
    )


def _focus_failed(app: AppRef, rec: ElementRec, err: int) -> DriverResult:
    """Refusal for keyboard input whose target could not be focused.

    Fail-CLOSED rather than best-effort. ``check_input_target`` validated the element
    the model ADDRESSED — ``ElementRec.secure`` is read from that element — so falling
    through to app-scoped ``post_text``/``post_key`` after a failed focus delivers the
    input to the app's EXISTING focus, which the policy layer never inspected and
    which can be the secure field it just refused. Some elements genuinely are not
    focusable and did accept typed input before this refusal; that legitimate case is
    the cost, and it is recoverable (click the control, then type) whereas a
    credential typed into the wrong field is not.
    """
    return DriverResult(
        ok=False,
        text=_REASON_FOCUS_FAILED.format(index=rec.index, app=app.bundle_id or app.name, code=err),
    )


def _action_failed(app: AppRef, rec: ElementRec, action: str, err: int) -> DriverResult:
    """Refusal for an accessibility action that returned a non-zero code.

    The RAW AX code is included. ``-25205`` in a support thread is immediately
    diagnosable ("the element refused an action it advertised"); "the click
    failed" is not.
    """
    detail = (
        _REASON_UNSUPPORTED_TARGET.format(action=action)
        if err == macos_ffi.AX_ERR_ACTION_UNSUPPORTED
        else _REASON_AX_ERROR.format(code=err)
    )
    return DriverResult(
        ok=False,
        text=ERR_ACTION_FAILED.format(
            action=action,
            index=rec.index,
            app=app.bundle_id or app.name,
            detail=detail,
        ),
    )


__all__ = ["MacOSBackend"]
