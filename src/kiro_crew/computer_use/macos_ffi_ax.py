"""Accessibility (AX) calls in :mod:`kiro_crew.computer_use.macos_ffi`.

The application element and its messaging timeout; attribute reads that check the
``CFTypeID`` before use and discharge the Create Rule (:func:`ax_owned_attr`); frames,
parents, children and actions; settable writes; process trust; and the AX-to-CoreGraphics
window id.

:mod:`kiro_crew.computer_use.macos_ffi` stays the import path and the patch surface for
every name defined here.
"""

from __future__ import annotations

import ctypes
from contextlib import contextmanager
from ctypes import c_bool, c_float, c_uint32, c_void_p
from typing import Any, Iterator, Sequence

from kiro_crew.computer_use.macos_ffi_bind import (
    AX_ERR_CANNOT_COMPLETE,
    AX_ERR_SUCCESS,
    AX_MESSAGING_TIMEOUT_SECS,
    K_AX_VALUE_CG_POINT,
    K_AX_VALUE_CG_SIZE,
    CGPoint,
    CGSize,
    _frameworks,
    type_ids,
)
from kiro_crew.computer_use.macos_ffi_cf import (
    cf_array_items,
    cf_bool_value,
    cf_is,
    cf_string,
    cf_string_value,
    cf_true,
)
from kiro_crew.computer_use.types import AX_PARENT, AX_POSITION, AX_SIZE

# ── Accessibility helpers ──


def ax_app_element(pid: int) -> c_void_p:
    """Create the application-level ``AXUIElement`` for *pid*. **CALLER OWNS it.**

    ``AXUIElementCreateApplication`` is a Create-Rule call (+1, verified retain
    count 1), so the caller must ``CFRelease``. Prefer :func:`ax_application`,
    which does it.

    The messaging timeout is set here — immediately after creation and before any
    attribute read — because that is the ONLY place it can be guaranteed. Every
    subsequent AX call inherits it, and without it a hung target app parks the
    calling thread with no interruption path (ctypes releases the GIL for the
    duration of the C call, so not even a signal handler runs).
    """
    libs = _frameworks()
    elem = c_void_p(libs.ax.AXUIElementCreateApplication(int(pid)))
    if elem:
        libs.ax.AXUIElementSetMessagingTimeout(elem, c_float(AX_MESSAGING_TIMEOUT_SECS))
    return elem


@contextmanager
def ax_application(pid: int) -> Iterator[c_void_p]:
    """:func:`ax_app_element` with the mandatory release.

    Measured leak without it: 1.4MB per 30,000 creations. One per snapshot is not
    dramatic on its own, but a long-lived sidecar taking a snapshot every turn
    accumulates it forever, and the context-manager form makes the obligation
    impossible to forget.
    """
    libs = _frameworks()
    elem = ax_app_element(pid)
    try:
        yield elem
    finally:
        if elem:
            libs.cf.CFRelease(elem)


def ax_attr(elem: Any, name: str) -> "tuple[Any, int]":
    """Read one AX attribute: ``(value_or_None, ax_error)``. **CALLER OWNS the value.**

    ``AXUIElementCopyAttributeValue`` follows CoreFoundation's **Create Rule**: the
    name contains "Copy", so the returned reference is +1 and the caller MUST
    ``CFRelease`` it. Verified: the returned CFString had a retain count of 1, and
    30,000 reads without a release grew RSS by 9.4MB while the same 30,000 with a
    release grew it by 0.0MB.

    This is a genuine leak, not a theoretical one — a single 1,200-node walk
    performs ~5,000 of these reads, which measured at ~400KB of permanent growth
    per walk before the fix. A long session would have been recycled by the
    watchdog on RSS.

    Prefer :func:`ax_owned_attr` (a context manager that releases) or the typed
    helpers built on it. This raw form exists for the two callers that need the
    reference beyond one expression; each releases explicitly.

    Returns the raw CF reference WITHOUT interpreting it — callers type-check via
    :func:`cf_is` before use. A non-zero error yields ``(None, err)`` and the
    out-parameter is never dereferenced: on failure AX leaves it untouched, so
    reading it would dereference uninitialised stack memory.
    """
    if not elem:
        return None, AX_ERR_CANNOT_COMPLETE
    libs = _frameworks()
    out = c_void_p()
    with cf_string(name) as key:
        if not key:
            return None, AX_ERR_CANNOT_COMPLETE
        err = int(libs.ax.AXUIElementCopyAttributeValue(elem, key, ctypes.byref(out)))
    if err != AX_ERR_SUCCESS:
        return None, err
    return out.value, err


@contextmanager
def ax_owned_attr(elem: Any, name: str) -> Iterator["tuple[Any, int]"]:
    """:func:`ax_attr` with the mandatory ``CFRelease`` — the form to use.

    A context manager for the same reason :func:`cf_string` is one: the Create Rule
    makes the release obligatory, a tree walk performs thousands of these reads, and
    a ``try/finally`` at every call site is a rule that gets forgotten exactly once
    and then leaks ~400KB per walk (measured, before this existed).

    Note what is NOT safe to do with the yielded value: any CF object derived from
    it (an array's elements, a nested element) is only valid while it is alive, so a
    caller that needs to keep such a derivative must ``CFRetain`` it — which is what
    :func:`ax_retained_elements` does.
    """
    value, err = ax_attr(elem, name)
    try:
        yield value, err
    finally:
        if value:
            _frameworks().cf.CFRelease(value)


def ax_str(elem: Any, name: str) -> str:
    """Read an AX attribute as ``str``, or ``""``.

    Returns ``""`` for a missing attribute AND for a present-but-not-a-string
    one. Both are ordinary: a window answers ``-25205`` for ``AXValue``, and
    ``AXValue`` on a slider is a CFNumber.

    The value is decoded into a Python ``str`` and released before returning, so no
    CF reference escapes. This is THE hot path of a tree walk (~5 calls per node).
    """
    with ax_owned_attr(elem, name) as (value, err):
        if err != AX_ERR_SUCCESS or value is None:
            return ""
        if not cf_is(value, type_ids().string):
            return ""
        return cf_string_value(value)


def ax_bool(elem: Any, name: str, *, default: bool = True) -> bool:
    """Read an AX attribute as ``bool``, falling back to *default*.

    *default* matters: ``AXEnabled`` is unsupported on many elements (a window
    answers ``-25205``), and rendering every such node as "(disabled)" would be
    both wrong and noisy. Absent means unknown, and unknown reads as enabled.
    """
    with ax_owned_attr(elem, name) as (value, err):
        if err != AX_ERR_SUCCESS or value is None:
            return default
        parsed = cf_bool_value(value)
        return default if parsed is None else parsed


def ax_bool_opt(elem: Any, name: str) -> "bool | None":
    """Read a boolean AX attribute, distinguishing ABSENT (``None``) from ``False``.

    :func:`ax_bool` collapses "unsupported" into a caller-chosen default, which is
    right for ``AXEnabled`` (absent means enabled) and wrong for the trait
    attributes: ``AXSelected`` absent means "this element has no notion of
    selection", while ``AXSelected=false`` means "selectable, not selected". A
    renderer that printed "(not selected)" for every node lacking the attribute
    would bury the tree in noise, so the tri-state has to survive the read.
    """
    with ax_owned_attr(elem, name) as (value, err):
        if err != AX_ERR_SUCCESS or value is None:
            return None
        return cf_bool_value(value)


def ax_is_settable(elem: Any, name: str) -> bool:
    """True when *name* can be WRITTEN on *elem* (``AXUIElementIsAttributeSettable``).

    The only reliable "is this input editable?" signal. ``AXEnabled`` is about
    interactivity, not writability: a read-only text field (a disabled form input,
    a log pane, a computed cell) reports ``AXEnabled=true`` and a readable
    ``AXValue``, and typing into it silently does nothing. Surfacing settability
    lets the model pick the editable field on the first try instead of discovering
    the read-only one by failing against it.

    Fails CLOSED (``False``) on any error: an unknowable attribute is treated as
    not settable, so the trait is never advertised on speculation.
    """
    if not elem:
        return False
    libs = _frameworks()
    out = c_bool(False)
    with cf_string(name) as cf_name:
        if not cf_name:
            return False
        err = int(libs.ax.AXUIElementIsAttributeSettable(elem, cf_name, ctypes.byref(out)))
    return err == AX_ERR_SUCCESS and bool(out.value)


def ax_parent(elem: Any) -> Any:
    """The element's parent as a RETAINED reference, or ``None``.

    +1 like :func:`ax_retained_elements`, and for the same reason: the value comes
    from a ``Copy`` call whose result the caller must own, and ``ax_owned_attr``
    releases it on scope exit — using it afterwards without the retain is a
    use-after-free in another process's accessibility client.

    ``None`` for the root and for any element whose app does not answer
    ``AXParent`` (ordinary), so callers treat it as "stop walking up".
    """
    if not elem:
        return None
    with ax_owned_attr(elem, AX_PARENT) as (value, err):
        if err != AX_ERR_SUCCESS or value is None:
            return None
        if not cf_is(value, type_ids().ax_element):
            return None
        return retain(value)


def same_element(a: Any, b: Any) -> bool:
    """True when *a* and *b* name the SAME accessibility element.

    ``CFEqual``, not ``==``. ``AXFocusedUIElement`` returns a freshly created
    reference, so the pointer it yields differs from the one the same node got
    while being walked from ``AXChildren`` even though both address one UI element.
    Comparing addresses would therefore report "not focused" for every element —
    the focus marker would simply never appear, which is a silent no-op rather
    than a visible failure.

    Both NULL is ``False``, not ``True``: "no element" is not identity with "no
    element", and the one caller (the focus marker) must not light up every record
    when nothing is focused.
    """
    if not a or not b:
        return False
    return bool(_frameworks().cf.CFEqual(a, b))


def ax_frame(elem: Any) -> "tuple[float, float, float, float] | None":
    """``(x, y, width, height)`` of *elem* in TOP-LEFT SCREEN coordinates, or ``None``.

    ``AXPosition`` and ``AXSize`` come back as ``AXValue`` boxes rather than plain
    numbers, so each is type-checked (``AXValueGetType``) before it is unboxed into
    the matching struct — reading a ``CGPoint`` box into a ``CGSize`` buffer would
    transpose ``y`` into ``width`` and produce a plausible-looking wrong rect,
    which is the worst failure mode for a coordinate a click is aimed at.

    Returns ``None`` unless BOTH reads succeed. A partial frame is worse than no
    frame: the caller would have to invent the missing half, and every consumer
    (rendering, hit-testing, the click ladder) would then be reasoning about a
    rectangle that does not exist. Many elements legitimately have no geometry
    (a menu bar item of a background app, an off-screen row), so ``None`` is an
    ordinary answer and not an error.

    Top-left screen coordinates: this is the AX convention AND the convention this
    whole package uses, so no flip happens here. The window-relative conversion is
    the caller's job, because only the caller knows which window the element
    belongs to.
    """
    if not elem:
        return None
    point = _ax_value_point(elem, AX_POSITION)
    if point is None:
        return None
    size = _ax_value_size(elem, AX_SIZE)
    if size is None:
        return None
    return (point[0], point[1], size[0], size[1])


def _ax_value_point(elem: Any, name: str) -> "tuple[float, float] | None":
    """Unbox a ``CGPoint``-typed ``AXValue`` attribute, or ``None``."""
    out = CGPoint()
    if not _unbox_ax_value(elem, name, K_AX_VALUE_CG_POINT, out):
        return None
    return (float(out.x), float(out.y))


def _ax_value_size(elem: Any, name: str) -> "tuple[float, float] | None":
    """Unbox a ``CGSize``-typed ``AXValue`` attribute, or ``None``."""
    out = CGSize()
    if not _unbox_ax_value(elem, name, K_AX_VALUE_CG_SIZE, out):
        return None
    return (float(out.width), float(out.height))


def _unbox_ax_value(elem: Any, name: str, want_type: int, out: Any) -> bool:
    """Read *name* off *elem* and unbox it into *out*, iff it really is *want_type*.

    Three checks, none of them optional:

    * the attribute read succeeded and returned something;
    * the something is an ``AXValue`` (``cf_is`` — an app may answer with a
      CFNumber or a string, and ``AXValueGetType`` on a non-``AXValue`` is
      undefined behaviour, not an error return);
    * the box's own type matches *want_type*, so the bytes land in the right
      struct fields.

    ``AXValueGetValue`` also returns false when the type disagrees, but relying on
    that alone would mean calling it on a pointer that may not be an ``AXValue``
    at all — the same class of crash ``cf_string_value``'s type guard exists to
    prevent.
    """
    with ax_owned_attr(elem, name) as (value, err):
        if err != AX_ERR_SUCCESS or value is None:
            return False
        if not cf_is(value, type_ids().ax_value):
            return False
        libs = _frameworks()
        if int(libs.ax.AXValueGetType(value)) != want_type:
            return False
        return bool(libs.ax.AXValueGetValue(value, want_type, ctypes.byref(out)))


def ax_children(elem: Any, *, limit: int = 0) -> list[Any]:
    """The element's children as RETAINED references — caller must release.

    See :func:`ax_retained_elements` for why retaining is required rather than
    optional. Every caller passes the result to :func:`release_all` when done; a
    tree walk does so as it pops each node.
    """
    return ax_retained_elements(elem, "AXChildren", limit=limit)


def ax_retained_elements(elem: Any, name: str, *, limit: int = 0) -> list[Any]:
    """Read an element-array attribute (``AXChildren``, ``AXWindows``) as +1 refs.

    Two ownership rules collide here and both have to be honoured:

    * the ARRAY comes from a ``Copy`` call, so we own it and must release it;
    * its ELEMENTS are owned by the array, so they die with it.

    A caller that wants to keep a child after the array is gone must therefore
    ``CFRetain`` it. Verified live: a child read back its ``AXRole`` correctly after
    the parent array was released *because it had been retained* — without the
    retain that is a use-after-free in another process's accessibility client, which
    is a crash rather than an exception.

    So this returns +1 references and the caller MUST call :func:`release_all`.
    Each entry is also type-checked to be an ``AXUIElement``: a malformed app could
    answer with an array of strings, and performing an action on a CFString pointer
    is a segfault.
    """
    libs = _frameworks()
    with ax_owned_attr(elem, name) as (value, err):
        if err != AX_ERR_SUCCESS:
            return []
        element_type = type_ids().ax_element
        return [
            libs.cf.CFRetain(item)
            for item in cf_array_items(value, limit=limit)
            if cf_is(item, element_type)
        ]


def retain(ref: Any) -> Any:
    """``CFRetain(ref)`` and return it — for a reference that must outlive its owner.

    Used when the walk hands one element back to its caller: the element belongs to
    a parent array the walk is about to release, and using it afterwards without a
    retain is a use-after-free in another process's accessibility client (a crash,
    not an exception).
    """
    if not ref:
        return ref
    return _frameworks().cf.CFRetain(ref)


def ax_attr_error(elem: Any, name: str) -> "tuple[bool, int]":
    """``(present, ax_error)`` for an attribute, WITHOUT leaking the value.

    For the caller that only needs to distinguish *why* a read came back empty —
    ``-25204`` ("Electron has not opted in, retry") versus a genuinely absent
    attribute. Releasing here means the diagnostic path cannot become a leak of its
    own.
    """
    with ax_owned_attr(elem, name) as (value, err):
        return bool(value), err


def release_all(refs: "Sequence[Any]") -> None:
    """``CFRelease`` every non-NULL reference in *refs*.

    The counterpart to :func:`ax_retained_elements`. Kept as a named helper so the
    release side of the walk reads as deliberately as the retain side.
    """
    libs = _frameworks()
    for ref in refs:
        if ref:
            libs.cf.CFRelease(ref)


def ax_actions(elem: Any, *, limit: int = 0) -> tuple[str, ...]:
    """The element's advertised action names, SANITIZED.

    Advisory only. A real ``AXScrollArea`` advertised ``AXScrollDownByPage`` and
    then returned ``-25205`` when asked to perform it, so every caller must try
    the action, check the error and fall back.

    Sanitized at this boundary — see :func:`_sanitize_action` — because an action
    name is arbitrary app-controlled text, not the ``AX*`` identifier one expects.
    """
    if not elem:
        return ()
    libs = _frameworks()
    out = c_void_p()
    err = int(libs.ax.AXUIElementCopyActionNames(elem, ctypes.byref(out)))
    if err != AX_ERR_SUCCESS:
        return ()
    try:
        names = [
            _sanitize_action(cf_string_value(item))
            for item in cf_array_items(out.value, limit=limit)
        ]
    finally:
        # ``Copy`` in the name => Create Rule => we own the array. The name strings
        # are decoded into Python above and belong to the array, so releasing it
        # here is the complete cleanup.
        if out.value:
            libs.cf.CFRelease(out.value)
    return tuple(name for name in names if name)


# Bounds for a sanitized action name. Real identifiers are short (``AXPress``,
# ``AXShowMenu``); the cap only bites on the app-generated descriptions below.
MAX_ACTION_NAME_LEN = 48


def _sanitize_action(name: str) -> str:
    """Collapse whitespace in an action name and clip it.

    **A live-data finding, and a real injection vector.** Action names are NOT
    guaranteed to be ``AX*`` identifiers — macOS Notes advertises
    ``'Name:remove pin\\nTarget:0x0\\nSelector:(null)'`` on its table rows, i.e. an
    app-generated description containing EMBEDDED NEWLINES.

    The rendered tree's structure IS its indentation, and the renderer joins action
    names into the element's line. An embedded newline therefore lets app content
    forge additional tree lines — fabricating elements with indices the model would
    then try to address. Values and titles are already collapsed when rendered;
    actions were not, and the safest place to fix it is here at the boundary that
    produces the data, so no downstream renderer has to remember.

    Clipped as well as collapsed: a 200-character action description crowds out
    the rest of the line for no informational gain.
    """
    flat = " ".join(name.split())
    return flat[:MAX_ACTION_NAME_LEN] if len(flat) > MAX_ACTION_NAME_LEN else flat


def ax_perform(elem: Any, action: str) -> int:
    """Perform a named accessibility action. Returns the raw AX error code.

    The RAW code is returned rather than a bool because the callers branch on it:
    ``-25205`` means "advertised but unsupported — use the fallback", while other
    codes are genuine failures whose number belongs in the error message so a
    support thread is diagnosable.
    """
    if not elem:
        return AX_ERR_CANNOT_COMPLETE
    with cf_string(action) as name:
        if not name:
            return AX_ERR_CANNOT_COMPLETE
        return int(_frameworks().ax.AXUIElementPerformAction(elem, name))


def ax_set_str(elem: Any, name: str, value: str) -> int:
    """Set a string-valued AX attribute (the ``set_value`` primitive)."""
    if not elem:
        return AX_ERR_CANNOT_COMPLETE
    with cf_string(name) as key:
        if not key:
            return AX_ERR_CANNOT_COMPLETE
        with cf_string(value) as payload:
            if not payload:
                return AX_ERR_CANNOT_COMPLETE
            return int(_frameworks().ax.AXUIElementSetAttributeValue(elem, key, payload))


def ax_set_true(elem: Any, name: str) -> int:
    """Set a boolean AX attribute to ``kCFBooleanTrue``.

    Used for ``AXManualAccessibility`` (the Electron opt-in) and ``AXFocused``
    (so typed keystrokes land in the addressed field rather than wherever the
    target app last had focus).
    """
    if not elem:
        return AX_ERR_CANNOT_COMPLETE
    with cf_string(name) as key:
        if not key:
            return AX_ERR_CANNOT_COMPLETE
        return int(_frameworks().ax.AXUIElementSetAttributeValue(elem, key, cf_true()))


def ax_is_process_trusted() -> bool:
    """``AXIsProcessTrusted()`` — ADVISORY. See :mod:`permissions`."""
    return bool(_frameworks().ax.AXIsProcessTrusted())


def ax_window_id(window: Any) -> int:
    """The CoreGraphics window id for an AX window element, or ``0``.

    Correlating the two window namespaces is a CORRECTNESS requirement.
    ``AXWindows`` and the CoreGraphics window list are independently ordered:
    verified live on Notes, ``AXWindows[0]`` was an ``AXDialog`` with an empty
    title exposing 3 nodes, while the frontmost CG window was the real 103-node
    document window. Walking one and capturing the other would hand the model a
    tree and a screenshot of DIFFERENT WINDOWS — a silent, confidently-wrong
    observation, which is worse than an error.

    Returns ``0`` when the private symbol is unavailable or the element is not a
    window (AX answers ``-25201`` for the application element, verified). Callers
    fall back to matching on title/``AXMain``.
    """
    libs = _frameworks()
    getter = getattr(libs.ax, "_AXUIElementGetWindow", None)
    if getter is None or not window:
        return 0
    out = c_uint32(0)
    if int(getter(window, ctypes.byref(out))) != AX_ERR_SUCCESS:
        return 0
    return int(out.value)
