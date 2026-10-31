"""The Windows :class:`NativeHelper` — what the agent-desktop driver cannot do itself.

agent-desktop reads the UI Automation tree and performs every element action. This
module keeps the rest: the on-screen window inventory and identity rules
(:mod:`apps_windows`), the vetted launch catalog (:mod:`launch_windows`), the
real-cursor click and drag, and keystrokes through ``SendInput``.

**On Windows there is no per-process input delivery at all.** ``SendInput`` carries
no hwnd and no pid, so there is no way to hand one application a mouse event without
moving the operator's cursor, and no way to give it a keystroke without its window
holding focus. So:

* **The pointer path is opt-in by name.** ``click_method: "auto"`` with coordinates
  is REFUSED, because the only coordinate route here moves the real cursor and
  ``auto`` must never resolve onto a pointer-moving method. ``app_post`` and
  ``sky_click`` are refused BY NAME for the same reason.
* **Every pointer gesture is confined to the authorized window, and the check
  compares HANDLES.** A global mouse event lands on whatever owns the pixel. A pid
  cannot serve as the key: ``ApplicationFrameHost.exe`` was measured fronting two
  unrelated applications at once. A drag confines every point of its path.
* **Keys go to whatever holds focus.** The agent-desktop driver moves focus to the
  addressed field and checks it landed and is not secure before calling
  :meth:`WindowsHelper.type_text` or :meth:`WindowsHelper.press_key`.

**No native library loads when this module is imported.** Every ``WinDLL`` runs
behind a function, so this module is import-safe on macOS and Linux and CI can
exercise the whole path by flipping ``platform_compat.IS_WINDOWS``.
"""

from __future__ import annotations

import logging
from typing import Callable

from kiro_crew.computer_use import (
    apps_windows,
    cursor_motion,
    keymap,
    launch_windows,
    windows_ffi,
)
from kiro_crew.computer_use.backend import NativeHelper, run_launch
from kiro_crew.computer_use.types import (
    CLICK_METHOD_APP_POST,
    CLICK_METHOD_AUTO,
    CLICK_METHOD_GLOBAL,
    CLICK_METHOD_SKY_CLICK,
    DRAG_SHAPE_NOTE,
    ERR_POINT_REQUIRED,
    ERR_UNKNOWN_CLICK_METHOD,
    PERMISSION_GRANTED,
    PERMISSION_UNSUPPORTED,
    PLATFORM_WINDOWS,
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

#: Why the native surface is unavailable, when it is. Reported by ``status()``.
UNAVAILABLE_REASON = "the Windows input and window libraries could not be loaded on this host"

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
#: Names an ELEMENT as the next move rather than a re-read. "Re-read the bounds"
#: could not terminate: the check compares the window that OWNS the pixel, so a point
#: under another window refuses while ``computer_get_state`` keeps returning the same
#: bounds. An element-addressed action needs no coordinate at all.
_ERR_POINT_NOT_OWNED = (
    "the point ({x}, {y}) is not owned by {app}'s own window — another window is on "
    "top of it, or the window is not on screen — so a real-cursor click there would "
    "land on a different application. Address the control by element_index from "
    "computer_get_state instead (no pointer is moved), or bring the window to the "
    "front first; the same coordinates will keep being refused while another window "
    "owns them"
)
_ERR_TEXT_SHORT = (
    "only {sent} of {total} characters were accepted by the system input queue; the "
    "field may hold a partial value"
)


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


class WindowsHelper(NativeHelper):
    """Win32 window inventory, launch and ``SendInput`` for Windows."""

    def __init__(self) -> None:
        # Cached because ``status()`` is called for every dashboard render, and a
        # miss would re-run the library load each time.
        self._available: "bool | None" = None

    @property
    def platform_id(self) -> str:
        return PLATFORM_WINDOWS

    def status(self) -> BackendStatus:
        """Whether the Win32 input and window libraries load here."""
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

    # ── input ──

    def click(self, app: AppRef, req: ClickRequest) -> DriverResult:
        """Move the real cursor and click, for ``global`` only.

        ``req.method`` is CONCRETE here (``policy.resolve_click_method`` ran at the
        dispatch chokepoint). The other methods are REFUSED BY NAME rather than
        downgraded: substituting the global pointer would hand the model the
        operator's cursor for a call that never asked for it.
        """

        def run() -> DriverResult:
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

    def type_text(self, app: AppRef, text: str) -> DriverResult:
        """Type *text* as Unicode key events into whatever holds keyboard focus.

        Unicode events rather than a per-character VK lookup: layout-independent, and
        able to emit characters no single US keystroke reaches.
        """

        def run() -> DriverResult:
            with windows_ffi.dpi_awareness_scope():
                sent = windows_ffi.send_text(text)
            if sent < len(text):
                # A short accept means the system input queue rejected the tail, so
                # the field holds a PARTIAL value. Reporting success would leave the
                # model believing it typed something it did not.
                return DriverResult(
                    ok=False, text=_ERR_TEXT_SHORT.format(sent=sent, total=len(text))
                )
            return DriverResult(
                ok=True, text=f"typed {len(text)} character(s) into the focused element", app=app
            )

        return _guarded("type_text", run)

    def press_key(self, app: AppRef, key: str) -> DriverResult:
        """Send one key chord to whatever holds keyboard focus.

        The spec is re-parsed through ``keymap.parse_spec`` rather than trusted: the
        chokepoint already validated it, but a keystroke is the one thing that must
        never be synthesized from an unvalidated string.
        """

        def run() -> DriverResult:
            spec = keymap.parse_spec(key)
            with windows_ffi.dpi_awareness_scope():
                windows_ffi.send_key_chord(spec.key, sorted(spec.modifiers))
            return DriverResult(ok=True, text=f"sent {key}", app=app)

        return _guarded("press_key", run)

    def close(self) -> None:
        """Nothing to release: the bound libraries are process-wide and stateless."""
