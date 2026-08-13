"""Windows application enumeration, identity, and the confinement predicate.

The Windows counterpart of :mod:`apps_macos`, and it diverges from it in one
structural way that is worth stating before the code: **on Windows a pid is not
an application identity.**

``apps_macos`` can key everything on the pid because macOS delivers synthesized
input per-process (``CGEventPostToPid``), so "this pid is authorized" and "this
app is authorized" are the same statement. Windows has no per-process input
delivery at all, and worse, one broker process fronts many unrelated
applications: ``ApplicationFrameHost.exe`` was measured here hosting *Settings*
and *HP Audio Control* at the same time under a single pid. Keying identity or
confinement on the pid would therefore let the agent be authorized for one app
and act on another — exactly the failure the confinement check exists to prevent.

So everything here keys on the **top-level HWND**, and the pid is carried only as
a secondary, non-sufficient signal. ``AppRef.window_id`` holds that handle, which
is also what makes ``AppRef.window_key`` (app identity plus pid plus window id)
correctly distinguish two windows of the same application.

Nothing in this module imports ctypes: all native work goes through
:mod:`windows_ffi`.
"""

from __future__ import annotations

import logging

from kiro_crew.computer_use import policy, windows_ffi
from kiro_crew.computer_use.types import AppRef, ComputerUseError

logger = logging.getLogger(__name__)

#: Window classes that are shell furniture rather than application windows, even
#: when they carry a title. Dropped for the same reason ``apps_macos`` keeps only
#: layer-0 windows: they are not addressable application surfaces, and their
#: owning process is usually a system agent whose tree is empty.
_SHELL_CLASSES: frozenset[str] = frozenset(
    {
        "Shell_TrayWnd",
        "Progman",
        "WorkerW",
        "Windows.UI.Core.CoreWindow",
        "ThumbnailDeviceHelperWnd",
        "XamlExplorerHostIslandWindow",
        "PseudoConsoleWindow",
        "ForegroundStaging",
        "MultitaskingViewFrame",
        "NarratorHelperWindow",
    }
)

_ERR_NO_MATCH = (
    "no application matching {query!r} is on screen. Call computer_list_apps to see "
    "what is available"
)


#: Processes that HOST other applications' windows rather than being an application.
#: A window fronted by one of these must not take the host's name, because that name
#: is what ``policy.check_app`` matches an operator's allow/deny patterns against.
#:
#: Measured on this desktop: *Settings* and *HP Audio Control* both report
#: ``ApplicationFrameHost.exe``, so an operator who blocks "Settings" gets no match
#: at all, and the only pattern that DOES match — the host name — blocks every
#: packaged app at once. Neither is what they asked for, and the permissive half is
#: silent.
#:
#: Lowercase, compared against the image name.
_WINDOW_HOST_PROCESSES: frozenset[str] = frozenset(
    {
        "applicationframehost.exe",  # packaged / UWP apps
        "runtimebroker.exe",
    }
)


def _is_hosted(info: windows_ffi.WindowInfo) -> bool:
    """Whether this window belongs to an app FRONTED by a host process."""
    return info.exe_name.lower() in _WINDOW_HOST_PROCESSES


def _display_name(info: windows_ffi.WindowInfo) -> str:
    """The name the operator would recognise for this window's app.

    The executable name is preferred over the window title because it is stable
    across documents — a title changes with the open file, so a model that
    resolved an app by title once could not address it again after the user
    switched tabs. Falls back to the title when the image name is unreadable,
    which happens for a process the current token cannot open.

    **A HOSTED window uses its title instead**, and that is a policy requirement
    rather than a display preference: the host's name identifies no application, so
    taking it would make an operator's deny rule on the real app match nothing
    while their only working alternative blocked every packaged app. The title is
    the one identity such a window has that the operator would actually write down.
    Its instability across documents is the accepted cost — a wrong answer here is
    an action in an app the operator excluded.
    """
    if _is_hosted(info) and info.title.strip():
        return info.title
    if info.exe_name:
        # ``chrome.exe`` reads worse than ``chrome`` in a tool result, and the
        # extension carries no information the model can act on.
        return info.exe_name[:-4] if info.exe_name.lower().endswith(".exe") else info.exe_name
    return info.title


def _app_ref(info: windows_ffi.WindowInfo) -> AppRef:
    """Build an :class:`AppRef` for one window.

    ``bundle_id`` holds the executable NAME rather than a path or a fabricated
    reverse-DNS string. Windows has no bundle identifier, and the field is what
    ``policy.check_app`` matches its patterns against and what ``render`` prints
    as the app label — so it has to be the thing an operator would actually write
    in an allow/deny list. A path would make those patterns machine-specific and
    an invented identifier would match nothing a user could guess.

    **For a HOSTED window, ``bundle_id`` is the title too.** ``check_app`` matches
    BOTH ``bundle_id`` and ``name``, so leaving the host's image name in either field
    keeps the bypass open: an operator's deny rule on the real app would still match
    nothing, and a rule on the host would still catch every packaged app. Both
    policy-matched fields therefore carry the only identity such a window has.
    """
    hosted = _is_hosted(info) and bool(info.title.strip())
    return AppRef(
        name=_display_name(info),
        pid=info.pid,
        bundle_id=info.title if hosted else info.exe_name,
        window_id=info.root_hwnd,
        window_title=info.title,
    )


def _denied_title(title: str) -> bool:
    """Whether *title* trips the built-in denylist (Kiro Crew's own window)."""
    probe = AppRef(name="", pid=0, window_title=title)
    return policy.denied_rule_for(probe) is not None


def list_apps() -> tuple[AppRef, ...]:
    """Applications with at least one visible, titled top-level window.

    One entry per top-level WINDOW rather than per process, which is the opposite
    of ``apps_macos``' choice and follows from the frame-host finding in the module
    docstring: two windows under one broker pid are two different applications, so
    collapsing by pid would merge them into one addressable target and make the
    second unreachable.

    A window whose title trips the built-in denylist is still LISTED — the
    dispatch chokepoint refuses it, and hiding it would leave the model retrying
    a target it cannot see is blocked. But a denied window is never overwritten by
    an innocuous sibling sharing the same handle, so the refusal cannot be dodged.

    Shell furniture is dropped: the tray, the desktop, thumbnail helpers and XAML
    islands all pass ``IsWindowVisible`` and are not application windows.
    """
    # An enumeration failure PROPAGATES: the driver's _guarded seam turns it into
    # DriverResult(ok=False, reason), matching apps_macos. Catching it and
    # returning () would report "no applications on screen" — indistinguishable
    # from an empty desktop — and resolve_app would then name computer_list_apps
    # as the remedy, pointing the model at the call that just lied to it.
    seen: dict[int, AppRef] = {}
    for info in windows_ffi.window_list():
        if info.class_name in _SHELL_CLASSES:
            continue
        if info.pid <= 0:
            continue
        # An app that can be named by neither its image nor its title has nothing
        # to show, the same shape check ``computer_list_apps`` applies on macOS.
        if not info.exe_name and not info.title.strip():
            continue
        existing = seen.get(info.root_hwnd)
        if existing is not None and _denied_title(existing.window_title):
            continue
        seen[info.root_hwnd] = _app_ref(info)
    return tuple(seen.values())


def resolve_app(query: str) -> AppRef:
    """Resolve *query* (display name, image name, or a title fragment) to one app.

    Matching is deliberately layered rather than one fuzzy pass, so an exact name
    always beats a partial title: without that ordering, asking for ``chrome``
    could resolve to a File Explorer window whose title happens to contain the
    word.

    Raises :class:`ComputerUseError` naming ``computer_list_apps`` when nothing
    matches, so the model's next move is obvious.
    """
    wanted = (query or "").strip().lower()
    if not wanted:
        raise ComputerUseError(_ERR_NO_MATCH.format(query=query))
    apps = list_apps()

    for app in apps:
        if app.name.lower() == wanted or app.bundle_id.lower() == wanted:
            return app
    # ``chrome`` should find ``chrome.exe``.
    for app in apps:
        stem = app.bundle_id.lower()
        if stem.endswith(".exe") and stem[:-4] == wanted:
            return app
    for app in apps:
        if wanted in app.name.lower() or wanted in app.bundle_id.lower():
            return app
    for app in apps:
        if wanted in app.window_title.lower():
            return app
    raise ComputerUseError(_ERR_NO_MATCH.format(query=query))


def window_bounds(app: AppRef) -> "tuple[float, float, float, float] | None":
    """Screen rect of *app*'s window, or ``None`` if it is gone.

    Read inside the DPI scope, so the origin published to the model is in the same
    coordinate space as the element frames measured against it.
    """
    try:
        with windows_ffi.dpi_awareness_scope():
            if not windows_ffi.window_is_live(app.window_id):
                return None
            return windows_ffi.window_bounds(app.window_id)
    except Exception:
        logger.debug("window bounds lookup failed for 0x%X", app.window_id, exc_info=True)
        return None


def hwnd_owns_point(app: AppRef, x: float, y: float) -> bool:
    """Does *app*'s own top-level window sit under the screen point?

    THE confinement check for any pointer-moving gesture, and the Windows
    replacement for ``apps_macos.pid_owns_point``. It compares HANDLES, not pids,
    and that is the whole point of the function:

    * a pid does not identify an application here. ``ApplicationFrameHost.exe``
      was measured fronting *Settings* and *HP Audio Control* simultaneously, so a
      pid check would pass for a click that lands on a different app than the one
      ``policy.check_app`` authorized — the app-A-authorized/app-B-clicked hole
      this function exists to close;
    * ``WindowFromPoint`` returns the deepest child at the pixel, so the result is
      lifted with ``GA_ROOT`` before comparison. A window's direct pid and its
      root's pid legitimately differ (a Chromium child surface is hosted by
      another process), which is a second reason the pid is not the key.

    Runs inside the DPI awareness scope, and that is load-bearing rather than
    tidy: a physical coordinate interpreted by an unaware thread resolved to a
    DIFFERENT APPLICATION in testing, so a mismatch here does not fail closed —
    it authorizes and clicks the wrong window while both halves agree with each
    other.

    Fails CLOSED (``False``) on a zero handle, a mismatched root, a window that is
    no longer live, or any error. Refusing a legitimate click costs the model one
    clear refusal; permitting a mis-aimed one is an irreversible action in an app
    the operator never authorized.
    """
    if not app.window_id:
        return False
    try:
        with windows_ffi.dpi_awareness_scope():
            if not windows_ffi.window_is_live(app.window_id):
                return False
            root = windows_ffi.root_window_at_point(x, y)
            if not root:
                # The point belongs to nobody we can name, so we cannot prove it
                # belongs to the authorized window.
                return False
            return int(root) == int(app.window_id)
    except Exception:
        logger.debug("point ownership check failed", exc_info=True)
        return False


__all__ = [
    "hwnd_owns_point",
    "list_apps",
    "resolve_app",
    "window_bounds",
]
