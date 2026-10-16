"""UI Automation elements in :mod:`kiro_crew.computer_use.windows_ffi`: reading, acting, walking.

VARIANT and BSTR marshalling and the property reads built on it, including the fail-closed
:func:`is_secure_element`; the control-pattern ladder, keyboard focus and
:func:`element_from_hwnd`; and the bounded walk over a UI Automation cache request
(:func:`create_cache_request`, :func:`walk_bounded`, :func:`cached_prop`) with the budgets
that bound it.

:mod:`kiro_crew.computer_use.windows_ffi` stays the import path and the patch surface for
every name defined here.
"""

from __future__ import annotations

import ctypes
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

from kiro_crew.computer_use.types import ComputerUseUnsupported
from kiro_crew.computer_use.windows_ffi_com import (
    E_ACCESSDENIED,
    S_OK,
    VARIANT,
    VT_ARRAY_R8,
    VT_BOOL,
    VT_BSTR,
    VT_EMPTY,
    VT_I2,
    VT_I4,
    VT_I8,
    VT_NULL,
    VT_R8,
    VT_UNKNOWN,
    AutomationElementMode_Full,
    ExpandCollapseState_LeafNode,
    ScrollAmount_NoAmount,
    TreeScope_Subtree,
    UIA_BoundingRectanglePropertyId,
    UIA_ControlTypePropertyId,
    UIA_ExpandCollapseExpandCollapseStatePropertyId,
    UIA_ExpandCollapsePatternId,
    UIA_HasKeyboardFocusPropertyId,
    UIA_InvokePatternId,
    UIA_IsEnabledPropertyId,
    UIA_IsExpandCollapsePatternAvailablePropertyId,
    UIA_IsInvokePatternAvailablePropertyId,
    UIA_IsPasswordPropertyId,
    UIA_IsSelectionItemPatternAvailablePropertyId,
    UIA_IsTogglePatternAvailablePropertyId,
    UIA_LegacyIAccessiblePatternId,
    UIA_NamePropertyId,
    UIA_ScrollPatternId,
    UIA_ScrollPatternNoScroll,
    UIA_SelectionItemIsSelectedPropertyId,
    UIA_SelectionItemPatternId,
    UIA_TogglePatternId,
    UIA_ValueIsReadOnlyPropertyId,
    UIA_ValuePatternId,
    UIA_ValueValuePropertyId,
    _libraries,
    automation,
    logger,
    release,
    release_all,
    vcall,
)

# ── VARIANT / BSTR ──


def bstr_value(ptr: Any) -> str:
    """Copy a BSTR into a Python ``str``.

    Length comes from ``SysStringLen`` rather than a NUL scan: a BSTR carries its
    length, may contain an embedded NUL, and a scan would silently truncate.
    """
    if not ptr:
        return ""
    libs = _libraries()
    length = int(libs["oleaut32"].SysStringLen(ptr))
    if length <= 0:
        return ""
    return ctypes.wstring_at(ptr, length)


def variant_value(var: VARIANT) -> Any:
    """Decode a VARIANT, copying any string out BEFORE the caller clears it.

    Returns ``None`` for ``VT_EMPTY``/``VT_NULL``. A ``VT_UNKNOWN`` yields the
    raw pointer, which is what the reserved "not supported" sentinel comparison
    needs.
    """
    vt = var.vt
    if vt == VT_BOOL:
        # VARIANT_BOOL TRUE is -1, not 1 — test against zero, never equality.
        return var.u.boolVal != 0
    if vt == VT_I4:
        return int(var.u.lVal)
    if vt == VT_I2:
        return int(var.u.iVal)
    if vt == VT_I8:
        return int(var.u.llVal)
    if vt == VT_R8:
        return float(var.u.dblVal)
    if vt == VT_BSTR:
        return bstr_value(var.u.bstrVal)
    if vt == VT_ARRAY_R8:
        # BoundingRectangle. The SAFEARRAY* lives in the union's pointer slot.
        return _safearray_doubles(var.u.punkVal)
    if vt == VT_UNKNOWN:
        return var.u.punkVal
    if vt in (VT_EMPTY, VT_NULL):
        return None
    return None


def _safearray_doubles(arr: Any) -> "list[float] | None":
    """Copy a ONE-dimensional SAFEARRAY of doubles into a Python list.

    The array's own RANK and bounds are both queried rather than assumed to be a
    ``[0, 3]`` vector: a provider that returns a differently-shaped array must
    underflow to ``None``, never make ``SafeArrayGetElement`` read past the end
    into the target process's memory.

    The rank check is not redundant with the bounds check, and it is the one that
    guards the read below. ``rgIndices`` must hold one ``LONG`` per DIMENSION, so a
    single ``c_long`` handed to a 2-D array makes the callee read 8 bytes from a
    4-byte object — 4 bytes of adjacent heap, used as the second index. The bounds
    calls do not catch it: they are asked about dimension 1 only and both return
    ``S_OK`` on a 2-D array, so they bound the element COUNT while the RANK is what
    is wrong.

    Ownership stays with the VARIANT — ``VariantClear`` frees the array — so nothing
    is released here.
    """
    if not arr:
        return None
    libs = _libraries()
    if int(libs["oleaut32"].SafeArrayGetDim(arr)) != 1:
        return None
    lower, upper = ctypes.c_long(), ctypes.c_long()
    if libs["oleaut32"].SafeArrayGetLBound(arr, 1, ctypes.byref(lower)) != S_OK:
        return None
    if libs["oleaut32"].SafeArrayGetUBound(arr, 1, ctypes.byref(upper)) != S_OK:
        return None
    out: list[float] = []
    value = ctypes.c_double()
    for i in range(lower.value, upper.value + 1):
        index = ctypes.c_long(i)
        if (
            libs["oleaut32"].SafeArrayGetElement(arr, ctypes.byref(index), ctypes.byref(value))
            != S_OK
        ):
            return None
        out.append(float(value.value))
    return out


@contextmanager
def _variant() -> Iterator[VARIANT]:
    """An initialized VARIANT that is always cleared."""
    libs = _libraries()
    var = VARIANT()
    libs["oleaut32"].VariantInit(ctypes.byref(var))
    try:
        yield var
    finally:
        libs["oleaut32"].VariantClear(ctypes.byref(var))


def prop_value_ex(elem: Any, prop_id: int) -> "tuple[int, int, Any]":
    """Read one live property WITHOUT the default-value substitution.

    Returns ``(hresult, vt, decoded value)``. The plain read folds "this provider
    does not implement the property" into the property's DEFAULT, so an
    unsupported boolean is indistinguishable from ``False``. Only this form can
    tell them apart, by returning the reserved not-supported sentinel — which is
    why the secure decision uses it.
    """
    with _variant() as var:
        hr = int(
            vcall(elem, "IUIAutomationElement", "GetCurrentPropertyValueEx")(
                elem, prop_id, True, ctypes.byref(var)
            )
        )
        return hr, int(var.vt), (variant_value(var) if hr == S_OK else None)


def is_secure_element(elem: Any) -> bool:
    """Whether *elem* is a masked (password) field. **Fails CLOSED.**

    THE security-critical read in this module: three separate floors key off it —
    value redaction, keyboard-input refusal, and whole-window screenshot
    suppression — so a wrong answer defeats all three at once.

    Only an explicit, supported, LIVE ``VARIANT_BOOL`` False is treated as plain.
    Everything else means "the provider did not tell me this is safe", which is
    not "this is safe"::

        read fails                          -> secure
        reserved not-supported sentinel     -> secure
        type is not VT_BOOL                 -> secure
        boolean is true                     -> secure
        supported, explicit False           -> plain

    **Read LIVE, never from a cached walk.** A field can flip from plain to
    password on the application's own timer with no user input, and a cached
    verdict then reports False while the cache still holds the CLEARTEXT.

    A caveat this function cannot fix, stated so a caller does not over-trust it:
    the property means "framework-masked", NOT "holds a secret". A CSS
    ``-webkit-text-security`` input and a ``contenteditable`` div both report
    plain while exposing their contents, so the content-based redaction
    downstream stays mandatory.
    """
    try:
        hr, vt, value = prop_value_ex(elem, UIA_IsPasswordPropertyId)
    except Exception:
        logger.debug("secure-flag read raised; treating as secure", exc_info=True)
        return True
    if hr != S_OK:
        return True
    if vt == VT_UNKNOWN:
        # The reserved sentinel: the provider does not implement the property.
        return True
    if vt != VT_BOOL:
        return True
    return bool(value)


# ── Control patterns: element-addressed input ──


def pattern(elem: Any, pattern_id: int) -> "Any | None":
    """The requested control pattern on *elem*, or ``None`` when unsupported.

    ``None`` for "this element does not implement that pattern" is the normal,
    expected answer — it is what makes the action ladder a ladder — so an
    unsupported pattern is not an error and is not logged as one. The caller owns
    the returned reference and must :func:`release` it.
    """
    out = ctypes.c_void_p()
    try:
        hr = int(
            vcall(elem, "IUIAutomationElement", "GetCurrentPattern")(
                elem, pattern_id, ctypes.byref(out)
            )
        )
    except Exception:
        logger.debug("GetCurrentPattern(%d) raised", pattern_id, exc_info=True)
        return None
    if hr != S_OK or not out:
        return None
    return out


def pattern_action(elem: Any, pattern_id: int, iface: str, method: str) -> "int | None":
    """Invoke a no-argument pattern method. Returns its HRESULT, or ``None``.

    ``None`` means the pattern is not supported, which the ladder reads as "try the
    next rung"; an ``int`` means the method RAN and its HRESULT is the verdict. The
    two must not collapse: a failed invoke that looked unsupported would fall
    through and perform a DIFFERENT gesture than the one the caller asked for.

    **There is NO duration bound on this call, and unlike macOS that is a platform
    limit rather than an omission.** ``macos_ffi`` caps every AX call at
    ``AX_MESSAGING_TIMEOUT_SECS`` because ctypes releases the GIL for the duration of a
    C call, so an unresponsive target parks the calling worker thread with no
    interruption path — not even a signal handler runs. The same hazard applies here: a
    provider that pumps a modal inside ``Invoke`` retires one pooled worker until that
    dialog closes. UIA's equivalent knob lives on ``IUIAutomation2``
    (``put_TransactionTimeout``), and it is not reachable from this client: measured on
    Windows 11, ``QueryInterface`` for ``IUIAutomation2`` returns ``E_NOINTERFACE`` from
    both ``CLSID_CUIAutomation`` and ``CLSID_CUIAutomation8`` — while the same
    ``CUIAutomation8`` returns plain ``IUIAutomation`` with ``S_OK``, so the class
    exists and only the derived interface is absent.

    What contains it instead: the executor is bounded and every verb is one call, so a
    wedged provider costs a worker rather than the loop, and the gateway keeps serving.
    Do NOT paper over this with a watchdog thread that abandons the call — an abandoned
    COM call still holds its apartment, so the thread never returns and the "timeout"
    would only hide the leak.
    """
    pat = pattern(elem, pattern_id)
    if pat is None:
        return None
    try:
        return int(vcall(pat, iface, method)(pat))
    finally:
        release(pat)


def invoke_element(elem: Any) -> "int | None":
    """``InvokePattern.Invoke`` — the primary press. Needs no pointer and no focus."""
    return pattern_action(elem, UIA_InvokePatternId, "IUIAutomationInvokePattern", "Invoke")


def toggle_element(elem: Any) -> "int | None":
    """``TogglePattern.Toggle`` — a checkbox/radio press.

    Deliberately NOT a fallback for a failed :func:`invoke_element`: toggling
    flips state relative to whatever it is now, so retrying a toggle whose
    HRESULT was lost can leave the control where it started while reporting
    success. It is a rung reached only when Invoke is UNSUPPORTED.
    """
    return pattern_action(elem, UIA_TogglePatternId, "IUIAutomationTogglePattern", "Toggle")


def select_element(elem: Any) -> "int | None":
    """``SelectionItemPattern.Select`` — select a list/tab/tree item."""
    return pattern_action(
        elem, UIA_SelectionItemPatternId, "IUIAutomationSelectionItemPattern", "Select"
    )


def do_default_action(elem: Any) -> "int | None":
    """``LegacyIAccessiblePattern.DoDefaultAction`` — the ladder's LAST rung.

    Present on nearly every node of a real Win32 tree, which is exactly why it is
    last: it performs whatever the provider calls its default action, which is less
    specific than the pattern rungs above it. Only ``DoDefaultAction`` is reachable
    here — this interface's ``SetValue`` is deliberately unbound, since it would be
    an un-gated write path around ``ValuePattern`` and the secure-field check.
    """
    return pattern_action(
        elem,
        UIA_LegacyIAccessiblePatternId,
        "IUIAutomationLegacyIAccessiblePattern",
        "DoDefaultAction",
    )


def set_element_value(elem: Any, value: str) -> "int | None":
    """``ValuePattern.SetValue`` — write a field WITHOUT focus or the pointer.

    Returns the HRESULT, or ``None`` when the element has no ValuePattern. The
    read-only flag is checked FIRST and reported as a distinct refusal: a
    ``SetValue`` on a read-only field returns a failure the caller cannot
    distinguish from "the provider rejected the text", and a model told only
    "failed" retries the same call.
    """
    pat = pattern(elem, UIA_ValuePatternId)
    if pat is None:
        return None
    try:
        read_only = ctypes.c_int()
        hr = int(
            vcall(pat, "IUIAutomationValuePattern", "get_CurrentIsReadOnly")(
                pat, ctypes.byref(read_only)
            )
        )
        if hr == S_OK and read_only.value:
            return E_ACCESSDENIED
        # A REAL BSTR, allocated by the OS. ``SetValue`` takes a length-prefixed
        # BSTR, and a bare ``LPCWSTR`` makes a provider's own ``SysStringLen`` read
        # the 4 bytes before the buffer as the length — measured at 282 for an
        # 11-character string, which writes ~540 bytes of adjacent process heap into
        # the field. Freed unconditionally: the callee copies what it needs.
        libs = _libraries()
        bstr = libs["oleaut32"].SysAllocString(value)
        if not bstr:
            raise ComputerUseUnsupported("SysAllocString failed (out of memory)")
        try:
            return int(vcall(pat, "IUIAutomationValuePattern", "SetValue")(pat, bstr))
        finally:
            libs["oleaut32"].SysFreeString(bstr)
    finally:
        release(pat)


def expand_collapse_state(elem: Any) -> "int | None":
    """``ExpandCollapseState``, or ``None`` when the pattern is absent."""
    pat = pattern(elem, UIA_ExpandCollapsePatternId)
    if pat is None:
        return None
    try:
        out = ctypes.c_int()
        hr = int(
            vcall(
                pat,
                "IUIAutomationExpandCollapsePattern",
                "get_CurrentExpandCollapseState",
            )(pat, ctypes.byref(out))
        )
        return int(out.value) if hr == S_OK else None
    finally:
        release(pat)


def expand_element(elem: Any, *, expand: bool) -> "int | None":
    """``ExpandCollapsePattern.Expand``/``Collapse``.

    A ``LeafNode`` is refused rather than attempted: the control cannot expand at
    all, and the provider's own error for that case is indistinguishable from a
    transient failure.
    """
    state = expand_collapse_state(elem)
    if state == ExpandCollapseState_LeafNode:
        return E_ACCESSDENIED
    return pattern_action(
        elem,
        UIA_ExpandCollapsePatternId,
        "IUIAutomationExpandCollapsePattern",
        "Expand" if expand else "Collapse",
    )


def scroll_element(elem: Any, *, horizontal: int, vertical: int) -> "int | None":
    """``ScrollPattern.Scroll`` on one or both axes.

    BOTH axes go in one call, so an axis the caller is not moving must be passed
    :data:`ScrollAmount_NoAmount` — a zero there is ``LargeDecrement`` and would
    scroll the other axis backwards.

    An axis that does not scroll is refused rather than attempted: ``ScrollPercent``
    reports :data:`UIA_ScrollPatternNoScroll` for it, and asking a non-scrolling
    axis to move returns a failure a model reads as "retry".
    """
    pat = pattern(elem, UIA_ScrollPatternId)
    if pat is None:
        return None
    try:
        wanted = "get_CurrentVerticalScrollPercent"
        if horizontal != ScrollAmount_NoAmount:
            wanted = "get_CurrentHorizontalScrollPercent"
        percent = ctypes.c_double()
        hr = int(vcall(pat, "IUIAutomationScrollPattern", wanted)(pat, ctypes.byref(percent)))
        if hr == S_OK and percent.value == UIA_ScrollPatternNoScroll:
            return E_ACCESSDENIED
        return int(vcall(pat, "IUIAutomationScrollPattern", "Scroll")(pat, horizontal, vertical))
    finally:
        release(pat)


def element_has_focus(elem: Any) -> bool:
    """Whether *elem* holds keyboard focus RIGHT NOW. **Fails CLOSED.**

    Read live rather than from the walk's cache, and read at all because
    ``SetFocus`` returning ``S_OK`` means the REQUEST was accepted — not that focus
    moved. A container can hand focus to a child, a dialog can pull it back, and both
    answer success.

    Only an explicit, supported ``VARIANT_BOOL`` True counts. Anything else — a failed
    read, the reserved not-supported sentinel, an unexpected type — is "I cannot prove
    this element has focus", which is not "it does". ``SendInput`` follows whatever
    holds focus, so a wrong answer here delivers keystrokes to a control the policy
    check never inspected.
    """
    try:
        hr, vt, value = prop_value_ex(elem, UIA_HasKeyboardFocusPropertyId)
    except Exception:
        logger.debug("focus read raised; treating as not focused", exc_info=True)
        return False
    if hr != S_OK or vt != VT_BOOL:
        return False
    return bool(value)


def set_element_focus(elem: Any) -> int:
    """``IUIAutomationElement::SetFocus``. Returns the HRESULT.

    **This is the one observation-adjacent call that takes the operator's keyboard
    focus**, so it is never called to "aim" an element-addressed action: the pattern
    rungs above need no focus, and calling this first would move the caret for a
    gesture that did not require it. It exists for the keyboard verbs, which have
    no focus-free route on Windows at all.
    """
    return int(vcall(elem, "IUIAutomationElement", "SetFocus")(elem))


def element_from_hwnd(hwnd: int) -> Any:
    """Resolve a top-level window handle to its UIA element.

    The handle crosses as ``c_void_p``: a bare Python int is marshalled as a
    32-bit C int and truncated. A NULL or failed result is a hard error rather
    than a fallback to the desktop root — a desktop-scoped element would walk
    every application's tree while looking like it worked.
    """
    auto = automation()
    out = ctypes.c_void_p()
    hr = int(
        vcall(auto, "IUIAutomation", "ElementFromHandle")(
            auto, ctypes.c_void_p(int(hwnd)), ctypes.byref(out)
        )
    )
    if hr != S_OK or not out:
        raise ComputerUseUnsupported(
            f"could not resolve window 0x{int(hwnd):X} (0x{hr & 0xFFFFFFFF:08X})"
        )
    return out


#: Aggregate ceiling on ONE :func:`walk_bounded`, the Windows twin of
#: ``snapshot_macos.MAX_WALK_SECS`` and the same value.
#:
#: The node and depth budgets bound how MANY cross-process calls a walk makes, not
#: how long they take, so they cannot bound a wedged provider: measured per-node cost
#: ranges from ~2ms (UWP) to ~20ms (WPF), and at 20ms the shipped 1200-node budget is
#: already 24s of legitimate work — a provider an order of magnitude slower would
#: park a POOLED worker (shared with chat and terminal work) for minutes. This
#: deadline is the only thing that bounds the total.
#:
#: Generous against reality — a real Chrome window reached the 1200-node budget in
#: 630ms, so a healthy application has ~16x headroom and can never hit it. It exists
#: purely to cut a pathological one loose. Kept here beside the other walk budgets
#: rather than in ``types.py`` because it bounds this walk implementation, not the
#: request schema the MCP layer validates.
MAX_WALK_SECS = 10.0

#: Properties the bounded walk registers BEFORE walking. The cache is a snapshot, so
#: ``AddProperty`` cannot backfill it afterwards — and each entry costs a
#: cross-process fetch per node, so every one here is read somewhere.
CACHED_WALK_PROPERTIES: tuple[int, ...] = (
    UIA_ControlTypePropertyId,
    UIA_NamePropertyId,
    UIA_IsEnabledPropertyId,
    UIA_HasKeyboardFocusPropertyId,
    UIA_BoundingRectanglePropertyId,
    UIA_ValueValuePropertyId,
    UIA_ValueIsReadOnlyPropertyId,
    UIA_SelectionItemIsSelectedPropertyId,
    UIA_ExpandCollapseExpandCollapseStatePropertyId,
    UIA_IsInvokePatternAvailablePropertyId,
    UIA_IsTogglePatternAvailablePropertyId,
    UIA_IsSelectionItemPatternAvailablePropertyId,
    UIA_IsExpandCollapsePatternAvailablePropertyId,
)


def create_cache_request(
    prop_ids: "Sequence[int]" = CACHED_WALK_PROPERTIES,
    *,
    scope: int = TreeScope_Subtree,
) -> Any:
    """Build a cache request for *prop_ids*.

    The cache is a SNAPSHOT: every property has to be registered before the walk,
    because ``AddProperty`` on an already-built cache is refused rather than
    extending it. ``AutomationElementMode`` is ``Full`` so the returned elements
    keep a live reference — the secure flag is read live off them, and a
    ``None``-mode element makes every live read fail.

    Caching is not an optimization here. A live per-property read is a
    cross-process RPC at roughly 0.6-1.5ms, so a naive walk of a Chromium window
    measured 4.4s against 0.75s cached for the identical 1294 nodes — and a
    mutating action walks the tree twice.
    """
    auto = automation()
    out = ctypes.c_void_p()
    hr = int(vcall(auto, "IUIAutomation", "CreateCacheRequest")(auto, ctypes.byref(out)))
    if hr != S_OK or not out:
        raise ComputerUseUnsupported(f"CreateCacheRequest failed (0x{hr & 0xFFFFFFFF:08X})")
    # Every HRESULT here is checked, and a failure RAISES rather than returning a
    # half-configured request. Both settings are load-bearing, and each fails in a
    # way that looks like working code:
    #
    # * a dropped ``AddProperty`` makes ``cached_prop`` return ``None`` for that id,
    #   so the walk reports role=Unknown / no frame / enabled=True over the whole
    #   tree and still answers ``ok=True`` — a wrong-shaped observation a model acts
    #   on;
    # * a dropped ``put_AutomationElementMode`` leaves the mode ``None``, which
    #   makes every LIVE read off a walked element fail, so ``is_secure_element``
    #   fails closed on every text node: every window's screenshot suppressed and
    #   every value redacted, with no reason surfaced anywhere.
    try:
        for prop_id in prop_ids:
            hr = int(vcall(out, "IUIAutomationCacheRequest", "AddProperty")(out, prop_id))
            if hr != S_OK:
                raise ComputerUseUnsupported(
                    f"CacheRequest::AddProperty({prop_id}) failed (0x{hr & 0xFFFFFFFF:08X})"
                )
        hr = int(vcall(out, "IUIAutomationCacheRequest", "put_TreeScope")(out, scope))
        if hr != S_OK:
            raise ComputerUseUnsupported(
                f"CacheRequest::put_TreeScope failed (0x{hr & 0xFFFFFFFF:08X})"
            )
        hr = int(
            vcall(out, "IUIAutomationCacheRequest", "put_AutomationElementMode")(
                out, AutomationElementMode_Full
            )
        )
        if hr != S_OK:
            raise ComputerUseUnsupported(
                f"CacheRequest::put_AutomationElementMode failed (0x{hr & 0xFFFFFFFF:08X})"
            )
    except BaseException:
        # The request is owned by this function until it is returned, so a failure
        # must release it rather than leaking a COM reference on every retry.
        release(out)
        raise
    return out


@dataclass
class WalkResult:
    """One bounded walk: the elements reached, and whether anything was cut off.

    ``truncated`` and ``depth_truncated`` map straight onto the ``Snapshot``
    fields of the same names. They are not diagnostics: a walk that dropped part
    of a tree without saying so let the secure-field scan reflect only the nodes
    it happened to reach, so a password field beyond the cut got the whole window
    captured.
    """

    elements: list[Any]
    depths: list[int]
    truncated: bool = False
    depth_truncated: bool = False
    #: The aggregate :data:`MAX_WALK_SECS` deadline stopped this walk. Reported
    #: separately from ``truncated`` (which it also sets, because the tree really is
    #: incomplete) so a diagnostic can tell "this provider is wedged" apart from "the
    #: model asked for a small budget".
    time_truncated: bool = False


def walk_bounded(
    root: Any,
    cache: Any,
    *,
    max_nodes: int,
    max_depth: int,
    max_children_per_node: int,
) -> WalkResult:
    """Breadth-first walk of *root*, stopping at the caller's budgets.

    **This is the walk, not :func:`find_all_cached`, and the difference is the
    whole performance story on Windows.** ``FindAllBuildCache(Subtree)`` is
    all-or-nothing: it materializes every descendant before it returns, so it
    ignores a node budget entirely. Measured on a real Chrome window it built
    2966 nodes in **13.7 seconds**; this function reached the shipped 1200-node
    budget in **630ms** on the same window — 22x — because it stops when the
    budget is met. A mutating action walks twice, so the difference is ~27s
    against ~1.3s per click.

    The traversal is ITERATIVE (an explicit frontier, never recursion): a
    pathological tree must not ``RecursionError`` inside a ctypes call.

    An aggregate :data:`MAX_WALK_SECS` deadline bounds the whole walk, the Windows
    twin of ``snapshot_macos.MAX_WALK_SECS`` and needed MORE here: the node budget
    bounds the COUNT of cross-process calls, not their duration, and per-node cost
    was measured from ~2ms (UWP) to ~20ms (WPF), so a partially-wedged provider can
    park a pooled worker for minutes without any single call being slow enough to
    notice. Hitting it sets ``truncated``, because the tree really is incomplete and
    the screenshot gate must see that.

    Every returned reference is owned by the caller, who must
    :func:`release_all` both ``elements`` and any it adds. Parent handles fetched
    only to reach their children are released here.
    """
    result = WalkResult(elements=[], depths=[])
    scratch: list[Any] = []
    frontier: list[Any] = [root]
    depth = 0
    # ``monotonic``, never wall clock: a clock adjustment mid-walk must not
    # retroactively expire or extend the deadline.
    deadline = time.monotonic() + MAX_WALK_SECS
    try:
        while frontier:
            if time.monotonic() >= deadline:
                result.truncated = True
                result.time_truncated = True
                break
            if depth >= max_depth:
                # There is more tree below, and the caller has to know: this is
                # what makes the screenshot suppression honest.
                if _any_has_children(frontier, cache, scratch):
                    result.depth_truncated = True
                    result.truncated = True
                break
            next_frontier: list[Any] = []
            for parent in frontier:
                if len(result.elements) >= max_nodes:
                    result.truncated = True
                    break
                # Checked per PARENT, not only per level: one frontier level can hold
                # hundreds of nodes, and a level-only check would let a wedged
                # provider overrun the deadline by that level's whole cost.
                if time.monotonic() >= deadline:
                    result.truncated = True
                    result.time_truncated = True
                    break
                children, capped, failed = _cached_children(
                    parent, cache, max_children_per_node, scratch
                )
                if capped or failed:
                    # A provider failure means this parent's subtree was never
                    # scanned, so the secure-field scan cannot have covered it.
                    result.truncated = True
                for position, child in enumerate(children):
                    if len(result.elements) >= max_nodes:
                        result.truncated = True
                        # The remaining children of THIS parent are real and
                        # unreported, so release them to avoid leaking their COM
                        # references. Index by the child's position within
                        # ``children`` — a running total across parents (the size
                        # of ``next_frontier``) would slice the wrong element and,
                        # once it exceeds this parent's child count, release
                        # nothing.
                        release_all(children[position:])
                        break
                    result.elements.append(child)
                    result.depths.append(depth + 1)
                    next_frontier.append(child)
            if result.truncated and len(result.elements) >= max_nodes:
                break
            frontier = next_frontier
            depth += 1
    finally:
        release_all(scratch)
    return result


def _cached_children(
    parent: Any, cache: Any, limit: int, scratch: list[Any]
) -> "tuple[list[Any], bool, bool]":
    """Children of *parent* via one cached refresh. ``(children, capped, failed)``.

    ``capped`` is detected as ``count >= limit`` rather than by re-reading the
    array for a true count: re-reading would reintroduce the cost the cap exists
    to avoid. A node with exactly the cap many children is therefore reported as
    capped too — a false positive costs one suppressed screenshot, a false
    negative is a disclosure.

    ``failed`` distinguishes "this node has no children" from "the provider would
    not tell me its children" — a ``BuildUpdatedCache`` or ``GetCachedChildren``
    that returns an error, which a Chromium/WPF pane does mid-relayout and an
    elevated child does under UIPI. The two must not collapse: a failed read that
    reported "no children AND nothing cut off" would let a subtree that was never
    scanned pass the screenshot gate, so ``walk_bounded`` marks the walk truncated
    on it. The caller fails closed, matching the node/depth caps.
    """
    updated = ctypes.c_void_p()
    hr = int(
        vcall(parent, "IUIAutomationElement", "BuildUpdatedCache")(
            parent, cache, ctypes.byref(updated)
        )
    )
    if hr != S_OK or not updated:
        return [], False, True
    scratch.append(updated)
    arr = ctypes.c_void_p()
    if (
        int(vcall(updated, "IUIAutomationElement", "GetCachedChildren")(updated, ctypes.byref(arr)))
        != S_OK
    ):
        return [], False, True
    if not arr:
        # A genuine leaf: the call SUCCEEDED and reported no child array. Not a
        # failure — this is the normal terminal case a walk must not flag.
        return [], False, False
    try:
        count = ctypes.c_int()
        # The HRESULT is CHECKED, and that is the whole point of ``failed``. A
        # fresh ``c_int`` is 0, so dropping the return on a failing call yields
        # ``([], False, False)`` — byte-identical to the genuine-leaf return above
        # — and the walk would then report "no children AND nothing cut off" over a
        # subtree it never scanned, opening the screenshot gate on it.
        if (
            int(vcall(arr, "IUIAutomationElementArray", "get_Length")(arr, ctypes.byref(count)))
            != S_OK
        ):
            return [], False, True
        total = int(count.value)
        capped = total >= limit
        out: list[Any] = []
        # A PER-CHILD failure counts as ``failed`` too. Skipping one silently and
        # still answering ``failed=False`` says "these are all the children" about a
        # list that is missing one — and if the missing node was a password field, the
        # secure scan never sees it, ``has_secure`` stays False, and
        # ``capture_snapshot_image`` photographs the window with the rendered
        # credential in frame. The count came from ``get_Length``, so a short read here
        # is a real discrepancy rather than the normal terminal case.
        missed = False
        for i in range(min(total, limit)):
            child = ctypes.c_void_p()
            if (
                int(
                    vcall(arr, "IUIAutomationElementArray", "GetElement")(
                        arr, i, ctypes.byref(child)
                    )
                )
                == S_OK
                and child
            ):
                out.append(child)
            else:
                missed = True
        return out, capped, missed
    finally:
        release(arr)


def _any_has_children(frontier: "Sequence[Any]", cache: Any, scratch: list[Any]) -> bool:
    """Whether the depth cap actually cut a subtree off.

    True when any frontier element has a child OR its child read failed — a
    failed read at the depth boundary is unproven-empty, so it counts as "there
    may be more below" and makes the depth truncation honest rather than assuming
    a clean leaf.
    """
    for parent in frontier:
        children, _capped, failed = _cached_children(parent, cache, 1, scratch)
        if children or failed:
            release_all(children)
            return True
    return False


def cached_prop(elem: Any, prop_id: int) -> Any:
    """Read one property from the element's CACHE. Bulk data only.

    Never route the secure flag through here: see :func:`is_secure_element`.
    """
    with _variant() as var:
        hr = int(
            vcall(elem, "IUIAutomationElement", "GetCachedPropertyValue")(
                elem, prop_id, ctypes.byref(var)
            )
        )
        return variant_value(var) if hr == S_OK else None
