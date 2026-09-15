"""Windows computer-use backend: UI Automation for reading AND for acting.

Observation and input both work. Reading drives real UIA through
:mod:`windows_ffi`, :mod:`apps_windows`, :mod:`snapshot_windows` and
:mod:`capture_windows`; the input verbs go through UIA control patterns where an
element was addressed, and through ``SendInput`` where the caller asked for the
real pointer or a keystroke.

**The safety story here is different from macOS's, and it drives the whole shape
of this module: on Windows there is no per-process input delivery at all.**
``SendInput`` carries no hwnd and no pid, so unlike ``CGEventPostToPid`` there is
no way to hand one application a mouse event without moving the operator's cursor,
and no way to give it a keystroke without taking the operator's focus. That single
fact produces every asymmetry below:

* **Element-addressed actions are the SAFE form, and the only one ``auto``
  resolves to.** A UIA pattern method needs neither the pointer nor focus — the
  provider performs the action inside the target application — so a press, toggle,
  select, expand or ``SetValue`` has no effect outside the control it names.
  :meth:`~WindowsBackend._press_element` walks a ladder by SPECIFICITY: ``Invoke``,
  then ``Toggle`` / ``Select``, then ``LegacyIAccessiblePattern.DoDefaultAction``
  last, because that one performs whatever the provider nominated. A rung is tried
  only when the previous pattern is ABSENT, never when it was present and FAILED —
  falling onward from a failure would perform a different gesture than the one
  requested.
* **The pointer path is opt-in by name.** ``click_method: "auto"`` with
  coordinates is REFUSED rather than served, because the only coordinate route here
  moves the real cursor and ``auto`` must never resolve onto a pointer-moving
  method. ``app_post`` and ``sky_click`` are refused BY NAME for the same reason:
  they are macOS routes, the only Windows approximation is the global pointer, and
  substituting it would hand the model the operator's cursor for a call that never
  asked for it.
* **Every pointer gesture is confined to the authorized window, and the check
  compares HANDLES.** A global mouse event lands on whatever owns the pixel, so
  naming an allowed app while passing coordinates over a denied one would pass
  ``policy.check_app`` on app A and click app B. A pid cannot serve as the key:
  ``ApplicationFrameHost.exe`` was measured fronting two unrelated applications at
  once. A drag confines BOTH endpoints, since the release is where a drop lands.
* **The keyboard verbs take focus and SAY SO.** A model told only "typed 12
  characters" would not know the operator's caret moved; the operator is the one who
  finds out. And a focus failure REFUSES rather than typing anyway — the policy
  check cleared the element the model ADDRESSED, so keystrokes delivered to a focus
  that did not move can reach the password field that check just refused.
* **The secure-field floor is re-read LIVE at the driver, not inherited from the
  snapshot.** A field can flip plain to password on the application's own timer
  between the walk the model was shown and the write, and this module is reachable
  without the dispatch chokepoint (``kirocrew computer call`` is a debug harness).
  See ``snapshot_windows.addressed``'s ``for_write``.
* **``perform_action`` takes a CLOSED vocabulary.** The obvious implementation —
  forward the caller's action name to ``LegacyIAccessiblePattern`` — would be an
  un-gated write path, because that interface also exposes ``SetValue`` and sits on
  nearly every node of a real Win32 tree. Each supported name maps to one specific
  pattern method instead, and the Legacy ``SetValue`` slot is not bound anywhere in
  this package.

**No native library loads when this module is imported.** The driver imports its
native helpers, but every ``WinDLL`` inside them runs behind a function, so this
module is import-safe on macOS and Linux and CI can exercise the whole path by
flipping ``platform_compat.IS_WINDOWS``.

The remaining hazards live with the code that contains them: the COM vtable slot
map, the fail-closed secure read, the bounded walk, the DPI scope and the
``SendInput`` records are all in :mod:`windows_ffi` (the only module here that
touches native code — see its docstring for why a wrong slot index is a process
abort rather than an exception), the tree shape is in :mod:`snapshot_windows`, HWND
confinement is in :mod:`apps_windows`, and the blank-frame gate is in
:mod:`capture_windows`. The measured cross-cutting findings are collected in
``docs/system-specs/modules/computer-use.md`` § The Windows driver.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from kiro_crew.computer_use import (
    apps_windows,
    capture_windows,
    cursor_motion,
    keymap,
    launch_windows,
    snapshot_windows,
    windows_ffi,
)
from kiro_crew.computer_use.backend import ComputerUseBackend, run_launch
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
    PLATFORM_WINDOWS,
    REFUSAL_ACCESSIBILITY_NEEDS_INDEX,
    WINDOWS_ACTION_COLLAPSE,
    WINDOWS_ACTION_EXPAND,
    WINDOWS_ACTION_PRESS,
    WINDOWS_ACTION_SELECT,
    WINDOWS_ACTION_TOGGLE,
    WINDOWS_SUPPORTED_ACTIONS,
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

#: Why the native surface is unavailable, when it is. Reported by ``status()`` so
#: the Settings panel can render a reason instead of a dead toggle.
UNAVAILABLE_REASON = (
    "the Windows UI Automation client could not be created (UIAutomationCore is "
    "unavailable on this host)"
)

# Refusals. Each NAMES the working alternative, because a refusal a model cannot
# act on costs it a turn and teaches it nothing.
_REFUSE_APP_POST = (
    "click_method 'app_post' is macOS-only: it delivers a mouse event to one "
    "process, and Windows has no per-process mouse route at all. Use an "
    "element_index (no pointer is moved), or name click_method 'global' to move the "
    "real cursor"
)
_REFUSE_SKY_CLICK = (
    "click_method 'sky_click' is macOS-only (a private window-server path with no "
    "Windows equivalent). Use an element_index, or name click_method 'global'"
)
_REFUSE_AUTO_POINT = (
    "click_method 'auto' with coordinates cannot be served on Windows: the only "
    "coordinate route here moves the operator's real cursor, and 'auto' never "
    "resolves onto a pointer-moving method. Pass an element_index instead (no "
    "pointer is moved), or name click_method 'global' explicitly to accept the "
    "cursor move"
)
#: A drag whose method is not ``global``. Windows has only the real-cursor route, and
#: ``DragRequest.method`` defaults to the macOS ``app_post`` — for which
#: ``moves_pointer`` is False, so the SEL audit and the cursor overlay both skip the
#: call. Serving it would move the operator's pointer on a gesture nothing recorded as
#: pointer-moving.
_REFUSE_DRAG_METHOD = (
    "dragging on Windows moves the operator's real cursor, so it must be requested "
    "explicitly: pass click_method 'global'. The default '{method}' is the macOS "
    "app-scoped route, which has no Windows equivalent"
)
#: A non-LEFT button on the element path. UIA has no pattern that opens a context
#: menu — there is no ``AXShowMenu`` equivalent and no ShowMenu pattern at all — so
#: the only route is the real cursor, which the caller must name.
_REFUSE_ELEMENT_BUTTON = (
    "a {button}-button click cannot be delivered to element {index} in {app}: UIA "
    "exposes no pattern that opens a context menu, so there is no route that leaves "
    "the operator's pointer alone. Name click_method 'global' with coordinates to "
    "accept the cursor move, or address the command you want directly if the menu it "
    "would open is itself in computer_get_state"
)
_ERR_NO_PATTERN = (
    "element {index} in {app} advertises no action this platform can perform "
    "(tried invoke, toggle, select and the legacy default action). Try a parent or "
    "child element from computer_get_state"
)
_ERR_ACTION_FAILED = "{verb} on element {index} in {app} failed (0x{hr:08X})"
#: Names an ELEMENT as the next move rather than a re-read. "Re-read the bounds"
#: could not terminate: the check compares the window that OWNS the pixel, so a point
#: inside the rect but under another window (or belonging to an off-screen window)
#: refuses while ``computer_get_state`` keeps returning the same bounds — the model
#: retries the identical call forever. An element-addressed action needs no
#: coordinate at all, so it is the move that can actually succeed.
_ERR_POINT_NOT_OWNED = (
    "the point ({x}, {y}) is not owned by {app}'s own window — another window is on "
    "top of it, or the window is not on screen — so a real-cursor click there would "
    "land on a different application. Address the control by element_index from "
    "computer_get_state instead (no pointer is moved), or bring the window to the "
    "front first; the same coordinates will keep being refused while another window "
    "owns them"
)
_ERR_NO_VALUE_PATTERN = (
    "element {index} in {app} has no settable value. Use computer_type_text to "
    "send keystrokes to it instead"
)
_ERR_READ_ONLY = "element {index} in {app} is read-only"
_ERR_NO_SCROLL = (
    "element {index} in {app} does not scroll on that axis. Scroll a parent "
    "container, or check the element list for one with a scrollbar"
)
#: Focus was REQUESTED successfully but the element does not hold it. Distinct from
#: the request failing: the provider accepted and redirected, so retrying the same
#: call will not help — the model needs a different target.
_ERR_FOCUS_NOT_HELD = (
    "element {index} in {app} did not take keyboard focus (the request succeeded but "
    "the element does not hold it), so no keystrokes were sent. Address the control "
    "that actually accepts typing, or use computer_set_value which needs no focus"
)
_ERR_FOCUS_FAILED = (
    "could not move keyboard focus to element {index} in {app} (0x{hr:08X}), so no "
    "keystrokes were sent. Sending them anyway would deliver them to whatever the "
    "application already had focused"
)
_ERR_UNKNOWN_ACTION = "unknown action {action!r} for element {index}. Supported: {supported}"
_ERR_ELEMENT_GONE = (
    "element {index} is no longer in {app}'s tree. Call computer_get_state to " "re-read it"
)
_ERR_UNKNOWN_DIRECTION = "unknown scroll direction {direction!r}. Supported: {supported}"

#: Scroll direction -> ``(horizontal, vertical)`` ScrollAmount pair. The axis NOT
#: being moved must be ``NoAmount``: a zero there is ``LargeDecrement``, so passing
#: zero for the idle axis would scroll it backwards on every call.
_SCROLL_AXES: "dict[str, tuple[int, int]]" = {
    "up": (windows_ffi.ScrollAmount_NoAmount, windows_ffi.ScrollAmount_LargeDecrement),
    "down": (windows_ffi.ScrollAmount_NoAmount, windows_ffi.ScrollAmount_LargeIncrement),
    "left": (windows_ffi.ScrollAmount_LargeDecrement, windows_ffi.ScrollAmount_NoAmount),
    "right": (windows_ffi.ScrollAmount_LargeIncrement, windows_ffi.ScrollAmount_NoAmount),
}
_ERR_TEXT_SHORT = (
    "only {sent} of {total} characters were accepted by the system input queue; the "
    "field may hold a partial value"
)

#: The focus warning every keyboard verb carries. The operator's caret really did
#: move, and a model told only "typed 5 characters" would not know that.
_FOCUS_NOTE = " (keyboard focus moved to it — Windows has no per-process key route)"

#: Named actions ``computer_perform_action`` accepts, mapped to the pattern each uses.
#: Defined in :mod:`types` because ``snapshot_windows`` publishes the same names in
#: ``ElementRec.actions`` and this module already imports it, so one shared spelling
#: cannot live here. Re-exported under the local names the driver reads.
ACTION_PRESS = WINDOWS_ACTION_PRESS
ACTION_TOGGLE = WINDOWS_ACTION_TOGGLE
ACTION_SELECT = WINDOWS_ACTION_SELECT
ACTION_EXPAND = WINDOWS_ACTION_EXPAND
ACTION_COLLAPSE = WINDOWS_ACTION_COLLAPSE
SUPPORTED_ACTIONS: tuple[str, ...] = WINDOWS_SUPPORTED_ACTIONS


def _guarded(label: str, run: "Callable[[], DriverResult]") -> DriverResult:
    """Run *run*, converting every failure into ``DriverResult(ok=False, ...)``.

    The ABC's contract is that no exception crosses the seam: the gateway
    dispatches driver calls on a pooled worker thread, and an exception escaping
    into it takes the tool call down rather than returning a result the model can
    reason about.

    A ``ComputerUseError`` message is written for a model, so it passes through
    verbatim (WITHOUT the ``Error: `` prefix — the dispatch layer adds that exactly
    once). Anything else is logged with a traceback and reported generically: an
    unexpected exception's ``str`` can carry internal paths.

    A ctypes access violation is of course NOT catchable here. That is what
    :mod:`windows_ffi`'s single vtable table and its ``argtypes`` discipline exist
    to prevent; this function handles the failures that are representable.
    """
    try:
        return run()
    except ComputerUseError as exc:
        return DriverResult(ok=False, text=str(exc))
    except Exception as exc:
        logger.warning("computer-use %s failed: %s", label, exc, exc_info=True)
        return DriverResult(ok=False, text=f"{label} failed unexpectedly ({type(exc).__name__})")


class WindowsBackend(ComputerUseBackend):
    """UI Automation driver for Windows: observation implemented, input refused."""

    def __init__(self) -> None:
        # Cached because ``status()`` is called for every dashboard render, and a
        # miss would re-run the library load each time.
        self._available: "bool | None" = None

    @property
    def platform_id(self) -> str:
        return PLATFORM_WINDOWS

    def status(self) -> BackendStatus:
        """Whether UI Automation can be reached here."""
        if self._available is None:
            self._available = windows_ffi.available()
        if self._available:
            return BackendStatus(supported=True, platform_id=PLATFORM_WINDOWS, reason="")
        return BackendStatus(
            supported=False, platform_id=PLATFORM_WINDOWS, reason=UNAVAILABLE_REASON
        )

    def probe_permissions(self) -> PermissionProbe:
        """ADVISORY only, and structurally different from macOS.

        There is no TCC on Windows: a process needs no grant to read another
        window's UI Automation tree, so there is nothing for an operator to enable
        and nothing that could be "missing". Reporting ``granted`` for
        accessibility when the client actually loads is therefore honest rather
        than optimistic, and ``screen_recording`` is ``unsupported`` because the
        concept does not exist — ``PrintWindow`` needs no permission.

        The real Windows boundary is UIPI: a non-elevated process cannot read or
        drive an ELEVATED window, and the secure desktop (the UAC prompt, the logon
        screen) is unreachable to any application. That is a security property
        worth having, and it is not something a permission row can express, so it
        is documented rather than reported as a grant the operator could change.
        """
        accessibility = PERMISSION_GRANTED if windows_ffi.available() else PERMISSION_UNSUPPORTED
        return PermissionProbe(
            accessibility=accessibility,
            screen_recording=PERMISSION_UNSUPPORTED,
            responsible_hint="",
        )

    # ── observation ──

    def list_apps(self) -> DriverResult:
        def run() -> DriverResult:
            return DriverResult(ok=True, apps=apps_windows.list_apps())

        return _guarded("list_apps", run)

    def resolve_app(self, query: str) -> DriverResult:
        def run() -> DriverResult:
            return DriverResult(ok=True, app=apps_windows.resolve_app(query))

        return _guarded("resolve_app", run)

    def launch_app(
        self,
        query: str,
        *,
        permit: "Callable[[LaunchIdentity], str | None] | None" = None,
        refuse_launched: "Callable[[AppRef], str | None] | None" = None,
    ) -> DriverResult:
        """Start an installed application, then wait for its window to appear.

        The flow is :func:`backend.run_launch` — shared with macOS, because the four
        refusals it encodes (near-miss suggestions, already-running rather than a second
        copy, no-window as a success) have to hold identically or the model learns a
        different contract per OS. What is Windows-specific is only WHICH functions it
        is handed: :mod:`launch_windows` resolves the catalog and verifies the target
        (see that module for the measurements), and ``find_window_for`` answers from the
        same on-screen window list every other verb resolves through.
        """

        def run() -> DriverResult:
            return run_launch(
                query,
                resolve=launch_windows.resolve_target,
                find=apps_windows.find_window_for,
                spawn=launch_windows.spawn_detached,
                permit=permit,
                identity=launch_windows.target_identity,
                refuse_launched=refuse_launched,
            )

        return _guarded("launch_app", run)

    def snapshot(self, app: AppRef, req: SnapshotRequest) -> DriverResult:
        def run() -> DriverResult:
            snap = snapshot_windows.build_snapshot(app, req)
            if req.want_image:
                # A separate step so the secure-field and truncated-walk
                # suppressions live in exactly one place rather than being
                # arguments threaded into the walk. A capture failure degrades to a
                # tree-only snapshot.
                snap = capture_windows.capture_snapshot_image(
                    snap, max_px=req.image_max_px, quality=req.image_quality
                )
            return DriverResult(ok=True, app=app, snapshot=snap)

        return _guarded("snapshot", run)

    # ── input ──

    def _addressed(self, app: AppRef, rec: ElementRec, *, for_write: bool = False) -> Any:
        """The element-resolution context manager, with this driver's walk budget.

        Wrapped so every verb resolves an index the SAME way — the numbering only
        means something relative to the walk that produced it, so a verb using a
        different budget would address a different control.

        ``for_write`` asks for the LIVE secure re-read (see
        ``snapshot_windows.addressed``). Every verb that writes text or moves
        keyboard focus passes it; a press, toggle, select or scroll does not, because
        those neither read nor set a value and refusing them on a masked field would
        block ordinary interaction with a login form.
        """
        return snapshot_windows.addressed(app, rec, self._action_budget(), for_write=for_write)

    @staticmethod
    def _action_budget() -> SnapshotRequest:
        """The walk budget an action's element lookup uses.

        The ceiling rather than a small default, because a model may legitimately
        address an element the full tree exposes: a narrower budget here would
        renumber the tree and refuse index 900 as "not present" while the model was
        looking right at it. ``want_image`` is off — a lookup never captures pixels.
        """
        return SnapshotRequest(
            max_nodes=MAX_TREE_NODES_LIMIT,
            max_depth=MAX_TREE_DEPTH_LIMIT,
            text_limit=DEFAULT_TEXT_LIMIT,
            want_image=False,
        )

    def _element_action(
        self,
        app: AppRef,
        rec: ElementRec,
        verb: str,
        run: "Callable[[Any], int | None]",
        *,
        won: "list[str] | None" = None,
    ) -> DriverResult:
        """Resolve *rec*, apply *run* to the live element, and report the HRESULT.

        One place where "the element is gone", "the pattern is unsupported" and "the
        call failed" become three DIFFERENT refusals. Collapsing them would tell a
        model to retry a call that can never work, or to give up on one that would
        have worked from a sibling element.

        *won* lets a multi-rung *run* record WHICH pattern succeeded, so the result
        names it. A model that learns this element answers ``DoDefaultAction`` does
        not have to re-derive that next turn, and the same information tells it a
        ``press`` landed as a toggle — which the bare word "pressed" hides. It stays
        optional because a single-pattern verb has nothing to disambiguate.
        """

        def guarded() -> DriverResult:
            with self._addressed(app, rec) as element:
                if element is None:
                    return DriverResult(
                        ok=False,
                        text=_ERR_ELEMENT_GONE.format(
                            index=rec.index, app=app.bundle_id or app.name
                        ),
                    )
                hr = run(element)
            if hr is None:
                return DriverResult(
                    ok=False,
                    text=_ERR_NO_PATTERN.format(index=rec.index, app=app.bundle_id or app.name),
                )
            if hr != windows_ffi.S_OK:
                return DriverResult(
                    ok=False,
                    text=_ERR_ACTION_FAILED.format(
                        verb=verb,
                        index=rec.index,
                        app=app.bundle_id or app.name,
                        hr=hr & 0xFFFFFFFF,
                    ),
                )
            if won:
                return DriverResult(
                    ok=True, text=f"{verb} element {rec.index} via {won[0]}", app=app
                )
            return DriverResult(ok=True, text=f"{verb} element {rec.index}", app=app)

        return _guarded(verb, guarded)

    def click(
        self,
        app: AppRef,
        rec: "ElementRec | None",
        req: ClickRequest,
    ) -> DriverResult:
        """Press an element through UIA, or move the real cursor for ``global``.

        ``req.method`` is CONCRETE here (``policy.resolve_click_method`` ran at the
        dispatch chokepoint), so this method never chooses one — and in particular
        can never select the pointer-warping path on its own.

        Two methods are REFUSED BY NAME rather than downgraded. ``app_post`` and
        ``sky_click`` are macOS routes with no Windows equivalent, and the only
        approximation available for either is the global pointer — substituting it
        would hand the model the operator's cursor for a call that never asked for
        it. ``auto`` + coordinates is refused for the same reason: ``auto`` must
        never resolve onto a pointer-moving method, and on Windows that is the only
        coordinate route there is.

        A non-LEFT button on the ELEMENT path is refused on the same principle. UIA
        has no pattern that opens a context menu, so the pattern ladder would
        activate the control instead — performing a different action than the model
        asked for, which is exactly what macOS's ``AXShowMenu``-only right-button
        ladder exists to avoid. The button is honoured on the coordinate path, where
        a real right-click is what ``global`` means.
        """

        def run() -> DriverResult:
            if req.method == CLICK_METHOD_ACCESSIBILITY:
                if rec is None:
                    return DriverResult(ok=False, text=REFUSAL_ACCESSIBILITY_NEEDS_INDEX)
                if req.button != MOUSE_BUTTON_LEFT:
                    return DriverResult(
                        ok=False,
                        text=_REFUSE_ELEMENT_BUTTON.format(
                            button=req.button,
                            index=rec.index,
                            app=app.bundle_id or app.name,
                        ),
                    )
                return self._press_element(app, rec)
            if req.method == CLICK_METHOD_APP_POST:
                return DriverResult(ok=False, text=_REFUSE_APP_POST)
            if req.method == CLICK_METHOD_SKY_CLICK:
                return DriverResult(ok=False, text=_REFUSE_SKY_CLICK)
            if req.method == CLICK_METHOD_AUTO:
                # Only reachable with coordinates: ``resolve_click_method`` turns
                # ``auto`` + element_index into ``accessibility`` upstream.
                return DriverResult(ok=False, text=_REFUSE_AUTO_POINT)
            if req.method == CLICK_METHOD_GLOBAL:
                if req.point is None:
                    return DriverResult(ok=False, text=ERR_POINT_REQUIRED.format(method=req.method))
                return self._click_global(app, req)
            return DriverResult(ok=False, text=ERR_UNKNOWN_CLICK_METHOD.format(method=req.method))

        return _guarded("click", run)

    def _press_element(self, app: AppRef, rec: ElementRec) -> DriverResult:
        """The pattern ladder: Invoke, then Toggle / Select, then the legacy default.

        No pointer and no focus at any rung — the provider performs the action inside
        the target application, so the operator's cursor and caret are untouched.
        This is why an element-addressed click is the SAFE form on Windows and the
        one ``auto`` resolves to.

        Every rung here is a LEFT-button activation; :meth:`click` refuses any other
        button before reaching this method, because UIA offers no pattern that opens
        a context menu and activating the control instead would be a different
        gesture than the one requested.

        The order is by SPECIFICITY, not by likelihood. ``Invoke`` means "activate
        this control" and is unambiguous; ``DoDefaultAction`` performs whatever the
        provider nominated and sits last for exactly that reason. A rung is tried
        only when the previous pattern is ABSENT — never when it was present and
        FAILED, because a failure means the right action was attempted and rejected,
        and falling onward would perform a different gesture than the one requested.

        The winning rung is NAMED in the result, matching the macOS driver: a press
        that landed as ``Toggle`` flipped a checkbox, and "pressed element 3" alone
        does not say so — the model would have to re-read the tree to find out what
        its own call did.
        """
        won: list[str] = []

        def ladder(element: Any) -> "int | None":
            for name, attempt in (
                ("Invoke", windows_ffi.invoke_element),
                ("Toggle", windows_ffi.toggle_element),
                ("SelectionItem.Select", windows_ffi.select_element),
                ("LegacyIAccessible.DoDefaultAction", windows_ffi.do_default_action),
            ):
                hr = attempt(element)
                # ``None`` is "this element does not implement that pattern" and is
                # the ONLY condition that advances; an int means the method RAN and
                # its HRESULT is the verdict, success or not.
                if hr is not None:
                    won.append(name)
                    return hr
            return None

        return self._element_action(app, rec, "pressed", ladder, won=won)

    def _click_global(self, app: AppRef, req: ClickRequest) -> DriverResult:
        """THE pointer-moving path: warp the operator's real cursor and click.

        Confined to the authorized window FIRST. A ``SendInput`` mouse event carries
        no hwnd and lands on whatever owns the pixel, so naming an allowed app while
        passing coordinates over a denied one (a terminal, a password manager) would
        pass ``policy.check_app`` on app A and click app B. ``hwnd_owns_point``
        compares top-level HANDLES rather than pids, because one broker process
        fronts many packaged apps here.

        Both the check and the click run inside ONE DPI awareness scope, and that is
        load-bearing: the virtual-screen metrics a coordinate is normalized against
        are themselves virtualized, measured as (1536, 960) unaware against
        (1920, 1200) aware on a 125% display. Normalizing in the wrong state puts
        the click ~25% off — far enough to land in another window, which the
        confinement check cannot catch because it agreed with the click.
        """
        assert req.point is not None  # checked by the caller
        x, y = req.point

        def run() -> DriverResult:
            with windows_ffi.dpi_awareness_scope():
                if not apps_windows.hwnd_owns_point(app, x, y):
                    return DriverResult(
                        ok=False,
                        text=_ERR_POINT_NOT_OWNED.format(
                            app=app.bundle_id or app.name, x=int(x), y=int(y)
                        ),
                    )
                windows_ffi.post_mouse_click(x, y, button=req.button, count=req.count)
            return DriverResult(
                ok=True,
                text=(
                    f"{req.button}-clicked ({int(x)}, {int(y)}) in "
                    f"{app.bundle_id or app.name} {req.count}x — the real cursor moved"
                ),
                app=app,
            )

        return _guarded("click", run)

    def drag(self, app: AppRef, req: DragRequest) -> DriverResult:
        """Drag between two screen points with the REAL cursor.

        Coordinate-only by construction — a drag's meaning IS the path between two
        points, and no UIA pattern expresses that. So unlike a click there is no
        safe element form to prefer, and every drag here moves the pointer.

        **Which is why the method must be ``global`` EXPLICITLY.**
        ``DragRequest.method`` defaults to ``app_post``, the macOS app-scoped route
        that moves no pointer — and ``moves_pointer`` is ``False`` for it, so the
        upstream SEL audit and the cursor-motion overlay both skip a call carrying
        that default. On macOS that is honest; here the only drag route available is
        the real cursor, so accepting the default would warp the operator's pointer
        on a call that was never audited as doing so. Refusing keeps
        ``moves_pointer`` a true statement about what actually happens, which is what
        every gate upstream reads.

        **EVERY point of the path is confined, not just the endpoints.** The endpoints
        alone were sufficient while a drag was a straight two-point gesture, because a
        chord between two points inside one rectangle stays inside it. A ``curved``
        path does not: its arc bows AWAY from the chord (up to 110px, see
        ``types.CURVE_AMOUNT_MAX``), so a stroke between two authorized points near an
        edge can bow out over the window beside it — and a pointer travelling with the
        button held across another application is a drag through that application.
        Confining the whole path is what keeps "a drag stays inside the authorized
        window" true for the shape that made it false.
        """

        def run() -> DriverResult:
            if req.method != CLICK_METHOD_GLOBAL:
                return DriverResult(ok=False, text=_REFUSE_DRAG_METHOD.format(method=req.method))
            points = cursor_motion.drag_points(req.start, req.end, steps=req.steps, path=req.path)
            with windows_ffi.dpi_awareness_scope():
                for point in points:
                    if not apps_windows.hwnd_owns_point(app, point[0], point[1]):
                        return DriverResult(
                            ok=False,
                            text=_ERR_POINT_NOT_OWNED.format(
                                app=app.bundle_id or app.name,
                                x=int(point[0]),
                                y=int(point[1]),
                            ),
                        )
                windows_ffi.post_mouse_drag(req.start, req.end, button=req.button, points=points)
            shape = (
                "" if len(points) <= 2 else DRAG_SHAPE_NOTE.format(count=len(points), path=req.path)
            )
            return DriverResult(
                ok=True,
                text=(
                    f"dragged from ({int(req.start[0])}, {int(req.start[1])}) to "
                    f"({int(req.end[0])}, {int(req.end[1])}){shape} — the real cursor "
                    "moved"
                ),
                app=app,
            )

        return _guarded("drag", run)

    def _focus(self, app: AppRef, rec: ElementRec) -> "str | None":
        """Move keyboard focus to *rec*. Returns a refusal string, or ``None``.

        **Refusing on a focus failure is a security requirement, not robustness.**
        ``policy.check_input_target`` cleared the element the model ADDRESSED — that
        is where ``ElementRec.secure`` comes from — so if focus did not move,
        ``SendInput`` delivers to whatever the application already had focused, which
        can be the password field the check just refused to write into. Typing blind
        past a failed aim is the one case where "best effort" defeats the
        secure-field floor.
        """
        # for_write: focus is what makes the following keystrokes land here, so a
        # field that masks its value must refuse the focus rather than the typing.
        with self._addressed(app, rec, for_write=True) as element:
            if element is None:
                return _ERR_ELEMENT_GONE.format(index=rec.index, app=app.bundle_id or app.name)
            hr = windows_ffi.set_element_focus(element)
            if hr != windows_ffi.S_OK:
                return _ERR_FOCUS_FAILED.format(
                    index=rec.index, app=app.bundle_id or app.name, hr=hr & 0xFFFFFFFF
                )
            # ``S_OK`` means the REQUEST was accepted, not that focus moved. A provider
            # may redirect it (a container handing focus to a child, a dialog pulling it
            # back) and still answer success — measured on a real window whose focus
            # stayed put across an S_OK ``SetFocus``. So the flag is read back LIVE off
            # the element we aimed at, and anything but an explicit True refuses.
            #
            # Fails CLOSED, and that is the point: ``SendInput`` follows whatever holds
            # focus, so an unverified aim delivers the keystrokes to a control the
            # policy check never saw — which can be the password field it just refused.
            if not windows_ffi.element_has_focus(element):
                return _ERR_FOCUS_NOT_HELD.format(index=rec.index, app=app.bundle_id or app.name)
        return None

    def type_text(self, app: AppRef, rec: "ElementRec | None", text: str) -> DriverResult:
        """Type *text* as Unicode key events, taking keyboard focus.

        The result SAYS the focus moved. A model told only "typed 12 characters"
        would not know the operator's caret is now somewhere else, and the operator
        is the one who finds out.

        Unicode events rather than a per-character VK lookup: layout-independent, and
        able to emit characters no single US keystroke reaches. A VK table would type
        the wrong character on a non-US layout, silently.
        """

        def run() -> DriverResult:
            note = ""
            if rec is not None:
                refusal = self._focus(app, rec)
                if refusal:
                    return DriverResult(ok=False, text=refusal)
                note = _FOCUS_NOTE
            with windows_ffi.dpi_awareness_scope():
                sent = windows_ffi.send_text(text)
            target = "the focused element" if rec is None else f"element {rec.index}"
            if sent < len(text):
                # A short accept means the system input queue rejected the tail, so
                # the field holds a PARTIAL value. Reporting success would leave the
                # model believing it typed something it did not.
                return DriverResult(
                    ok=False, text=_ERR_TEXT_SHORT.format(sent=sent, total=len(text))
                )
            return DriverResult(
                ok=True, text=f"typed {len(text)} character(s) into {target}{note}", app=app
            )

        return _guarded("type_text", run)

    def press_key(self, app: AppRef, rec: "ElementRec | None", key: str) -> DriverResult:
        """Send one key chord, taking keyboard focus.

        The spec is re-parsed through ``keymap.parse_spec`` rather than trusted: the
        dispatch chokepoint already validated it, but this driver is reachable from
        the CLI harness and from tests, and a keystroke is the one thing that must
        never be synthesized from an unvalidated string.
        """

        def run() -> DriverResult:
            spec = keymap.parse_spec(key)
            note = ""
            if rec is not None:
                refusal = self._focus(app, rec)
                if refusal:
                    return DriverResult(ok=False, text=refusal)
                note = _FOCUS_NOTE
            with windows_ffi.dpi_awareness_scope():
                windows_ffi.send_key_chord(spec.key, sorted(spec.modifiers))
            return DriverResult(ok=True, text=f"sent {key}{note}", app=app)

        return _guarded("press_key", run)

    def set_value(self, app: AppRef, rec: ElementRec, value: str) -> DriverResult:
        """Write a field through ``ValuePattern`` — no focus, no pointer.

        THE preferred way to enter text on Windows, and the reason it is preferred is
        structural rather than stylistic: it neither takes the operator's caret nor
        moves their cursor, so it is the only text route with no side effect outside
        the target control.
        """

        def run() -> DriverResult:
            # for_write: a live secure re-read, because this is the verb that writes
            # a value and the field may have started masking since the model saw it.
            with self._addressed(app, rec, for_write=True) as element:
                if element is None:
                    return DriverResult(
                        ok=False,
                        text=_ERR_ELEMENT_GONE.format(
                            index=rec.index, app=app.bundle_id or app.name
                        ),
                    )
                hr = windows_ffi.set_element_value(element, value)
            if hr is None:
                return DriverResult(
                    ok=False,
                    text=_ERR_NO_VALUE_PATTERN.format(
                        index=rec.index, app=app.bundle_id or app.name
                    ),
                )
            if hr == windows_ffi.E_ACCESSDENIED:
                return DriverResult(
                    ok=False,
                    text=_ERR_READ_ONLY.format(index=rec.index, app=app.bundle_id or app.name),
                )
            if hr != windows_ffi.S_OK:
                return DriverResult(
                    ok=False,
                    text=_ERR_ACTION_FAILED.format(
                        verb="set value",
                        index=rec.index,
                        app=app.bundle_id or app.name,
                        hr=hr & 0xFFFFFFFF,
                    ),
                )
            return DriverResult(ok=True, text=f"set element {rec.index}", app=app)

        return _guarded("set_value", run)

    def scroll(self, app: AppRef, rec: ElementRec, direction: str, pages: float) -> DriverResult:
        """Scroll an element through ``ScrollPattern`` — no pointer.

        A page per unit, via ``LargeIncrement``/``LargeDecrement``, because that is
        what the pattern offers and what "pages" means to the caller. The axis the
        caller is NOT moving must be passed ``NoAmount``: a zero there is
        ``LargeDecrement`` and would scroll the other axis backwards.
        """

        def run() -> DriverResult:
            wanted = direction.strip().lower()
            axis = _SCROLL_AXES.get(wanted)
            if axis is None:
                return DriverResult(
                    ok=False,
                    text=_ERR_UNKNOWN_DIRECTION.format(
                        direction=direction, supported=", ".join(sorted(_SCROLL_AXES))
                    ),
                )
            horizontal, vertical = axis
            steps = max(1, int(abs(pages)))
            with self._addressed(app, rec) as element:
                if element is None:
                    return DriverResult(
                        ok=False,
                        text=_ERR_ELEMENT_GONE.format(
                            index=rec.index, app=app.bundle_id or app.name
                        ),
                    )
                hr: "int | None" = None
                for _ in range(steps):
                    hr = windows_ffi.scroll_element(
                        element, horizontal=horizontal, vertical=vertical
                    )
                    if hr is None or hr != windows_ffi.S_OK:
                        break
            if hr is None or hr == windows_ffi.E_ACCESSDENIED:
                return DriverResult(
                    ok=False,
                    text=_ERR_NO_SCROLL.format(index=rec.index, app=app.bundle_id or app.name),
                )
            if hr != windows_ffi.S_OK:
                return DriverResult(
                    ok=False,
                    text=_ERR_ACTION_FAILED.format(
                        verb="scroll",
                        index=rec.index,
                        app=app.bundle_id or app.name,
                        hr=hr & 0xFFFFFFFF,
                    ),
                )
            return DriverResult(
                ok=True, text=f"scrolled element {rec.index} {wanted} {steps} page(s)", app=app
            )

        return _guarded("scroll", run)

    def perform_action(self, app: AppRef, rec: ElementRec, action: str) -> DriverResult:
        """Perform one NAMED action from :data:`SUPPORTED_ACTIONS`.

        A closed vocabulary, deliberately. The obvious implementation — pass the
        caller's string to ``LegacyIAccessiblePattern`` — would be an un-gated write
        path: that interface also exposes ``SetValue``, and it sits on nearly every
        node of a real Win32 tree, so a free-form action could write the password
        field ``set_value`` is refused on. Each name below maps to ONE specific
        pattern method instead, and the Legacy interface's ``SetValue`` slot is not
        bound anywhere in this package.
        """

        def run() -> DriverResult:
            wanted = action.strip().lower()
            if wanted not in SUPPORTED_ACTIONS:
                return DriverResult(
                    ok=False,
                    text=_ERR_UNKNOWN_ACTION.format(
                        action=action,
                        index=rec.index,
                        supported=", ".join(SUPPORTED_ACTIONS),
                    ),
                )
            if wanted == ACTION_PRESS:
                return self._press_element(app, rec)
            if wanted == ACTION_TOGGLE:
                return self._element_action(app, rec, "toggled", windows_ffi.toggle_element)
            if wanted == ACTION_SELECT:
                return self._element_action(app, rec, "selected", windows_ffi.select_element)
            expand = wanted == ACTION_EXPAND
            return self._element_action(
                app,
                rec,
                "expanded" if expand else "collapsed",
                lambda element: windows_ffi.expand_element(element, expand=expand),
            )

        return _guarded("perform_action", run)

    def close(self) -> None:
        """Release every worker thread's COM client. Safe to call repeatedly.

        ``release_all_clients`` rather than ``reset_thread_state``: ``close`` runs
        on the caller's thread (the event loop) during a backend swap, while the
        clients were created on pooled worker threads whose thread-local state it
        cannot reach. A thread-local-only reset would release none of them, and
        the swap would leave the old driver's clients live.
        """
        try:
            windows_ffi.release_all_clients()
        except Exception:
            logger.debug("releasing the UI Automation clients failed", exc_info=True)
