"""Always-on computer-use floors, platform-free, applied to EVERY backend.

A rule kept inside one driver's files would bind only that driver, and a new
backend (the upstream driver, a remote executor, a test double) could skip it by
not re-implementing it. The floors below are enforced here, by :mod:`.service`, on
whatever a backend returns, so swapping the driver cannot weaken them. Two helpers
are NOT service floors: :func:`check_launch_query` and :func:`representative_window`
are called by the upstream backend (``driver_runtime.backend.CuaBackend``) only,
because the native backends resolve launches and windows through their own
catalogs:

* **Screenshot suppression** (:func:`screenshot_permitted`,
  :func:`harden_snapshot`): no pixels leave a backend for a window that holds a
  secure element, whose walk was truncated or depth-truncated (``has_secure``
  then means "none seen", not "none present"), or whose window id is unknown.
  Whole-window, because a sub-rectangle of an encoded JPEG cannot be reliably
  blanked.
* **Secure values never travel**: a secure element's value is dropped, and the
  snapshot's ``has_secure`` is recomputed from the elements rather than trusted
  from the backend.
* **Live-view mirror** (:func:`emit_live_frame`): only the frame that survived
  the floor and was spooled is offered to the dashboard, once, from one place.
* **Pointer confinement** (:func:`check_point_in_window`): a coordinate gesture
  must land inside the target window, so a click cleared for app A cannot land in
  app B. A backend with an occlusion-aware native hit test says so with
  ``confines_points_natively``; every other backend is held to the window bounds,
  and a backend that cannot report bounds is refused (fail closed).
* **Self-window preference** (:func:`representative_window`): when one process
  owns several windows, the one whose title trips the built-in denylist (Kiro
  Crew's own dashboard in a browser tab) represents the process, so the deny
  cannot be dodged by asking about a sibling window.
* **Launch by name only** (:func:`check_launch_query`): a launch query is an
  application NAME; paths, argument vectors and URLs are refused before any
  catalog is consulted.

The JPEG spool rules (``mkstemp`` 0o600 inside a 0o700 directory, ring trim)
are in ``service._persist_image``, which every backend that returns bytes goes
through.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import replace

from kiro_crew.computer_use import policy
from kiro_crew.computer_use.types import AppRef, Snapshot

logger = logging.getLogger(__name__)

#: Why a screenshot was withheld (diagnostics and tests; never shown raw).
WITHHELD_SECURE = "secure"
WITHHELD_INCOMPLETE = "incomplete"
WITHHELD_NO_WINDOW = "no_window"

ERR_POINT_OUTSIDE_WINDOW = (
    "the point ({x}, {y}) is outside {app}'s window, so the click could land in another "
    "application; call computer_get_state and aim inside the window"
)
ERR_POINT_UNCONFINED = (
    "{app}'s window position is unknown, so a coordinate gesture cannot be confined to it; "
    "call computer_get_state again, or address an element by element_index"
)
ERR_LAUNCH_NOT_A_NAME = (
    "'{query}' is not an application name: give the app's name as the OS shows it "
    "(for example 'Calculator'), never a path, a command line or a URL"
)

#: Characters that make a launch query something other than a plain name.
_LAUNCH_FORBIDDEN = frozenset('/\\:*?"<>|%$`;&~\n\r\t\0')


def screenshot_withheld_reason(snap: Snapshot) -> "str | None":
    """Why *snap*'s window must not be captured, or ``None`` when it may be."""
    if snap.has_secure or any(e.secure for e in snap.elements):
        return WITHHELD_SECURE
    if snap.truncated or snap.depth_truncated:
        return WITHHELD_INCOMPLETE
    if snap.app.window_id <= 0:
        return WITHHELD_NO_WINDOW
    return None


def screenshot_permitted(snap: Snapshot) -> bool:
    """True when *snap*'s window may be captured. The one predicate every capture reads."""
    return screenshot_withheld_reason(snap) is None


def harden_snapshot(snap: Snapshot) -> Snapshot:
    """Apply the secure and capture floors to a snapshot a backend returned.

    * ``has_secure`` becomes true when ANY element is secure, whatever the
      backend claimed;
    * a secure element's value is dropped;
    * pixels (bytes, a path the backend spooled, dimensions) are dropped when the
      window may not be captured.
    """
    elements = snap.elements
    if any(e.secure and e.value for e in elements):
        elements = tuple(replace(e, value="") if e.secure and e.value else e for e in elements)
    has_secure = snap.has_secure or any(e.secure for e in elements)
    hardened = replace(snap, elements=elements, has_secure=has_secure)
    if hardened.selected_text and any(e.secure and e.focused for e in elements):
        hardened = replace(hardened, selected_text="")
    if (hardened.image_jpeg or hardened.image_path) and not screenshot_permitted(hardened):
        if hardened.image_path:
            # A frame a backend already spooled for a window it should not have
            # captured: remove it, not just the reference to it.
            try:
                os.unlink(hardened.image_path)
            except OSError:
                logger.debug("could not remove a withheld frame", exc_info=True)
        hardened = replace(hardened, image_jpeg=b"", image_path="", image_width=0, image_height=0)
    return hardened


def emit_live_frame(snap: Snapshot) -> bool:
    """Offer *snap*'s spooled frame to the dashboard live view. Never raises."""
    if not snap.image_jpeg or not snap.image_path or not screenshot_permitted(snap):
        return False
    try:
        from kiro_crew.computer_use import screencast

        return bool(screencast.emit_snapshot_frame(snap))
    except Exception:
        logger.debug("computer-use live-view relay failed", exc_info=True)
        return False


def point_in_bounds(
    bounds: "tuple[float, float, float, float] | None", point: "tuple[float, float]"
) -> bool:
    """Whether the screen *point* lies inside *bounds* ``(x, y, w, h)``."""
    if bounds is None:
        return False
    x, y, w, h = bounds
    if w <= 0 or h <= 0:
        return False
    px, py = point
    return x <= px < x + w and y <= py < y + h


def check_point_in_window(
    app: AppRef,
    bounds: "tuple[float, float, float, float] | None",
    points: "Sequence[tuple[float, float]]",
) -> "str | None":
    """Refuse a gesture whose points are not all inside *app*'s window."""
    if bounds is None:
        return ERR_POINT_UNCONFINED.format(app=app.name)
    for point in points:
        if not point_in_bounds(bounds, point):
            return ERR_POINT_OUTSIDE_WINDOW.format(x=int(point[0]), y=int(point[1]), app=app.name)
    return None


def representative_window(windows: Sequence[AppRef]) -> "AppRef | None":
    """The window that stands for its process: a denied title wins, else the first."""
    if not windows:
        return None
    for window in windows:
        if policy.title_is_denied(window.window_title):
            return window
    return windows[0]


def check_launch_query(query: str) -> "str | None":
    """Refuse a launch query that is not a plain application name."""
    text = query.strip()
    if not text or text.startswith(("-", ".")) or any(ch in _LAUNCH_FORBIDDEN for ch in text):
        return ERR_LAUNCH_NOT_A_NAME.format(query=query[:64])
    return None


__all__ = [
    "ERR_LAUNCH_NOT_A_NAME",
    "ERR_POINT_OUTSIDE_WINDOW",
    "ERR_POINT_UNCONFINED",
    "WITHHELD_INCOMPLETE",
    "WITHHELD_NO_WINDOW",
    "WITHHELD_SECURE",
    "check_launch_query",
    "check_point_in_window",
    "emit_live_frame",
    "harden_snapshot",
    "point_in_bounds",
    "representative_window",
    "screenshot_permitted",
    "screenshot_withheld_reason",
]
