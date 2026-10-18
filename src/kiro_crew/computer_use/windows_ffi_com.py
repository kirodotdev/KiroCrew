"""The COM and Win32 binding layer of :mod:`kiro_crew.computer_use.windows_ffi`.

The ABI constants and structures; the two declarative tables, :data:`_FN_SPECS` for the
flat Win32 functions and :data:`_VTABLE`, the one place a COM slot index may appear; the
library loader and :func:`vcall`; the COM reference discipline (:func:`release`,
:func:`owned`); and the per-thread apartment and ``IUIAutomation`` client, with the
cross-thread release a backend ``close()`` needs.

:mod:`kiro_crew.computer_use.windows_ffi` stays the import path and the patch surface for
every name defined here. The hazards the comments below cite by number ("hazard 6 in the
module docstring") are the ones its docstring lists.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from contextlib import contextmanager
from ctypes import wintypes  # type aliases only; imports cleanly on every platform
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

from kiro_crew import platform_compat
from kiro_crew.computer_use.types import ComputerUseUnsupported

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.computer_use.windows_ffi")

#: ``ctypes.get_last_error`` / ``set_last_error`` exist only in the Windows ctypes
#: stubs, and CI runs mypy on Linux. Reached through an ``Any``-typed alias — the
#: same approach ``mcp_gateway/transport.py`` takes — so a POSIX type check does not
#: flag a call site that only ever runs on Windows.
_ct: Any = ctypes

# ── COM plumbing constants ──
S_OK = 0
#: Returned by the pattern helpers for a target the provider says cannot accept the
#: action (a read-only field, a leaf node asked to expand, a non-scrolling axis).
#: A real HRESULT rather than a sentinel of our own, so a caller that only checks
#: ``!= S_OK`` still treats it as the failure it is.
E_ACCESSDENIED = -2147024891  # 0x80070005
COINIT_MULTITHREADED = 0x0
CLSCTX_INPROC_SERVER = 0x1
# RPC_E_CHANGED_MODE: this thread already has an apartment of the other kind.
# Not an error for us — the thread is usable, we simply must not uninitialize it.
RPC_E_CHANGED_MODE = 0x80010106

# VARIANT type tags, the only ones a UIA property read can return here.
VT_EMPTY = 0
VT_NULL = 1
VT_I2 = 2
VT_I4 = 3
VT_R8 = 5
VT_BSTR = 8
VT_BOOL = 11
VT_UNKNOWN = 13
VT_I8 = 20
VT_ARRAY = 0x2000
#: What ``BoundingRectangle`` actually comes back as: a SAFEARRAY of four
#: doubles, ``[left, top, WIDTH, HEIGHT]`` (verified against ``GetWindowRect`` on a
#: real window — NOT ``[left, top, right, bottom]``). A read that lacks this
#: branch returns ``None`` for every element frame, so nothing spatial works.
VT_ARRAY_R8 = VT_ARRAY | VT_R8

# ── UIA property ids ──
# Numeric ids rather than names because that is what the ABI takes. Each one is
# spelled once, here, and each one is READ somewhere: an id that nothing reads still
# costs a cross-process fetch per node when it sits in CACHED_WALK_PROPERTIES, which
# measured 399ms against 298ms on a real WPF window for five such ids.
UIA_ControlTypePropertyId = 30003
UIA_NamePropertyId = 30005
UIA_HasKeyboardFocusPropertyId = 30008
UIA_IsEnabledPropertyId = 30010
UIA_BoundingRectanglePropertyId = 30001
UIA_ValueValuePropertyId = 30045
UIA_ValueIsReadOnlyPropertyId = 30046
#: ``SelectionItem.IsSelected`` — list/table selection, which is what
#: ``types.TRAIT_SELECTED`` means. NOT ``HasKeyboardFocus``: a listbox row can be
#: selected without holding focus, and a focused element is usually not "selected"
#: in this sense, so sourcing the trait from focus reports one signal under two
#: names and leaves "which row is selected?" always naming the caret's element.
UIA_SelectionItemIsSelectedPropertyId = 30079
#: ``ExpandCollapse.ExpandCollapseState`` — what ``types.TRAIT_EXPANDED`` means, and
#: the parity counterpart of the macOS walk's ``AXDisclosing`` read. Without it a
#: model cannot tell an open tree node from a closed one and spends a turn expanding
#: something already open. Read TRI-STATE: a provider without the pattern answers the
#: reserved not-supported sentinel rather than ``Collapsed``, so only an explicit
#: ``Expanded`` publishes the trait — verified on a real ``TreeView`` where two nodes
#: read ``Collapsed`` and ``Expanded`` while every non-expandable element in the same
#: window answered ``VT_UNKNOWN``.
UIA_ExpandCollapseExpandCollapseStatePropertyId = 30070
#: ``Is<Pattern>PatternAvailable`` — which control patterns an element implements,
#: readable as ordinary CACHED properties. This is what makes ``ElementRec.actions``
#: publishable at all: probing a pattern with ``GetCurrentPattern`` is a live
#: cross-process round trip PER ELEMENT, which is exactly the cost the cached walk
#: exists to avoid, while these ride along in the same snapshot. Verified against
#: ``GetCurrentPattern`` on real controls: a ``Button`` reported Invoke, a ``CheckBox``
#: Invoke + Toggle, a ``ListItem`` Invoke + SelectionItem, an ``Edit`` Value, and a
#: scrollable ``Pane`` Scroll.
UIA_IsInvokePatternAvailablePropertyId = 30031
UIA_IsTogglePatternAvailablePropertyId = 30041
UIA_IsSelectionItemPatternAvailablePropertyId = 30036
UIA_IsExpandCollapsePatternAvailablePropertyId = 30026
#: **30019, not 30097.** Scored against ``get_CurrentIsPassword`` over 15 real
#: ``Edit`` controls on four UI frameworks, 30019 agreed 15/15 and 30097 agreed
#: 0/15 — 30097 is ``LegacyIAccessibleHelp``, a ``VT_BSTR``. A wrong id still
#: returns ``S_OK``, and that string is truthy for a placeholder-bearing search
#: box but EMPTY, hence falsy, for a real password box with no placeholder: the
#: secure floor would fail OPEN precisely where it has to hold.
UIA_IsPasswordPropertyId = 30019

# TreeScope flags. Subtree = element + all descendants.
TreeScope_Element = 1
TreeScope_Children = 2
TreeScope_Descendants = 4
TreeScope_Subtree = 7

#: ``AutomationElementMode.Full``. ``None`` (0) returns elements stripped of
#: their live reference, which makes every subsequent live read ``E_FAIL`` — and
#: the secure flag MUST be read live, so ``Full`` is not optional here.
AutomationElementMode_Full = 1

# ── UIA control-pattern ids ──
# The pattern ladder for element-addressed input. These need NEITHER the pointer
# nor keyboard focus, which is what makes an element-addressed action on Windows
# safe in a way a coordinate click can never be: the provider performs it inside
# the target application and the operator's cursor and focus are untouched.
UIA_InvokePatternId = 10000
UIA_ValuePatternId = 10002
UIA_ScrollPatternId = 10004
UIA_ExpandCollapsePatternId = 10005
UIA_SelectionItemPatternId = 10010
UIA_TogglePatternId = 10015
UIA_LegacyIAccessiblePatternId = 10018

#: ``ExpandCollapseState``. ``LeafNode`` means the control cannot expand at all,
#: so an expand request on it is a refusal rather than a no-op that reports success.
ExpandCollapseState_Collapsed = 0
ExpandCollapseState_Expanded = 1
ExpandCollapseState_PartiallyExpanded = 2
ExpandCollapseState_LeafNode = 3

#: ``ScrollAmount``. ``NoAmount`` is the "do not move this axis" value — required,
#: because ``Scroll`` takes both axes in one call and a scroll of one axis must pass
#: it for the other rather than a zero, which is ``LargeDecrement``.
ScrollAmount_LargeDecrement = 0
ScrollAmount_SmallDecrement = 1
ScrollAmount_NoAmount = 2
ScrollAmount_LargeIncrement = 4

#: ``UIA_ScrollPatternNoScroll`` — the sentinel ``ScrollPercent`` reports for an
#: axis that does not scroll. A real percentage is 0..100, so this is unambiguous.
UIA_ScrollPatternNoScroll = -1.0

# ── DPI awareness contexts (SetThreadDpiAwarenessContext) ──
# Negative sentinels, passed as handles.
DPI_AWARENESS_CONTEXT_UNAWARE = -1
DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = -2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = -3
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

# ── Win32 misc ──
GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class GUID(ctypes.Structure):
    """A COM GUID. Module scope: see hazard 6 in the module docstring."""

    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _VariantUnion(ctypes.Union):
    """The VARIANT payload. Only the members a UIA property read can produce."""

    _fields_ = [
        ("llVal", ctypes.c_longlong),
        ("lVal", ctypes.c_long),
        ("iVal", ctypes.c_short),
        ("boolVal", ctypes.c_short),
        ("bstrVal", ctypes.c_void_p),
        ("punkVal", ctypes.c_void_p),
        ("dblVal", ctypes.c_double),
    ]


class VARIANT(ctypes.Structure):
    """x64 VARIANT: 24 bytes with the union at offset 8.

    The layout is load-bearing rather than incidental. A declaration that puts
    the union anywhere else reads the payload from the wrong offset and reports a
    plausible-looking type tag for every property — and a short declaration lets
    the callee write past the end of the object.
    """

    _fields_ = [
        ("vt", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("wReserved2", wintypes.WORD),
        ("wReserved3", wintypes.WORD),
        ("u", _VariantUnion),
        ("tail", ctypes.c_longlong),
    ]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


@dataclass(frozen=True)
class WindowInfo:
    """One visible top-level window, as the enumerator saw it.

    ``root_hwnd`` is what confinement and identity key on: a window's DIRECT pid
    and its ``GA_ROOT`` pid legitimately differ (a Chromium child surface is
    hosted by another process), and a pid is not an app identity at all on
    Windows — one broker fronts many packaged apps.
    """

    hwnd: int
    root_hwnd: int
    pid: int
    title: str
    class_name: str
    exe_name: str
    bounds: "tuple[float, float, float, float] | None"


# ── Flat Win32 function table ──
# (library key, symbol, restype, argtypes). Every row carries BOTH, and the bind
# pass raises on a missing argtypes: see hazard 3.
_FN_SPECS: tuple[tuple[str, str, Any, list[Any]], ...] = (
    ("ole32", "CoInitializeEx", ctypes.c_long, [ctypes.c_void_p, wintypes.DWORD]),
    ("ole32", "CoUninitialize", None, []),
    (
        "ole32",
        "CoCreateInstance",
        ctypes.c_long,
        [
            ctypes.POINTER(GUID),
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ],
    ),
    ("ole32", "CLSIDFromString", ctypes.c_long, [ctypes.c_wchar_p, ctypes.POINTER(GUID)]),
    ("oleaut32", "VariantInit", None, [ctypes.POINTER(VARIANT)]),
    ("oleaut32", "VariantClear", ctypes.c_long, [ctypes.POINTER(VARIANT)]),
    ("oleaut32", "SysStringLen", ctypes.c_uint, [ctypes.c_void_p]),
    # A BSTR is LENGTH-PREFIXED, so a string crossing into a COM method that
    # expects one must be allocated by the OS. See ``set_element_value``.
    ("oleaut32", "SysAllocString", ctypes.c_void_p, [ctypes.c_wchar_p]),
    ("oleaut32", "SysFreeString", None, [ctypes.c_void_p]),
    # SAFEARRAY access, for the BoundingRectangle double[4]. Both the RANK and the
    # bounds are read rather than assumed, so a malformed array underflows to "no
    # frame" rather than an out-of-range element read into another process's memory.
    ("oleaut32", "SafeArrayGetDim", ctypes.c_uint, [ctypes.c_void_p]),
    (
        "oleaut32",
        "SafeArrayGetLBound",
        ctypes.c_long,
        [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_long)],
    ),
    (
        "oleaut32",
        "SafeArrayGetUBound",
        ctypes.c_long,
        [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_long)],
    ),
    (
        "oleaut32",
        "SafeArrayGetElement",
        ctypes.c_long,
        [ctypes.c_void_p, ctypes.POINTER(ctypes.c_long), ctypes.c_void_p],
    ),
    ("user32", "EnumWindows", wintypes.BOOL, [ctypes.c_void_p, wintypes.LPARAM]),
    ("user32", "IsWindow", wintypes.BOOL, [ctypes.c_void_p]),
    ("user32", "IsWindowVisible", wintypes.BOOL, [ctypes.c_void_p]),
    ("user32", "IsIconic", wintypes.BOOL, [ctypes.c_void_p]),
    ("user32", "GetWindowTextW", ctypes.c_int, [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]),
    ("user32", "GetWindowTextLengthW", ctypes.c_int, [ctypes.c_void_p]),
    ("user32", "GetClassNameW", ctypes.c_int, [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]),
    (
        "user32",
        "GetWindowThreadProcessId",
        wintypes.DWORD,
        [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)],
    ),
    ("user32", "GetAncestor", ctypes.c_void_p, [ctypes.c_void_p, wintypes.UINT]),
    ("user32", "GetWindowRect", wintypes.BOOL, [ctypes.c_void_p, ctypes.POINTER(RECT)]),
    ("user32", "WindowFromPoint", ctypes.c_void_p, [POINT]),
    ("user32", "SetThreadDpiAwarenessContext", ctypes.c_void_p, [ctypes.c_void_p]),
    # The DPI pair below exists for ONE purpose: sizing a capture buffer. See
    # :func:`window_render_scale`.
    ("user32", "GetDpiForWindow", wintypes.UINT, [ctypes.c_void_p]),
    ("user32", "MonitorFromWindow", ctypes.c_void_p, [ctypes.c_void_p, wintypes.DWORD]),
    # DWM cloak state: the half of "is this window on screen" that IsWindowVisible
    # cannot answer. See :func:`window_is_on_screen`.
    (
        "dwmapi",
        "DwmGetWindowAttribute",
        ctypes.c_long,
        [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD],
    ),
    (
        "shcore",
        "GetDpiForMonitor",
        ctypes.c_long,
        [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
        ],
    ),
    # SendInput: the keyboard path. ``cbSize`` MUST be sizeof(INPUT) — a wrong size
    # is rejected wholesale with ERROR_INVALID_PARAMETER rather than partially
    # applied, which is the safe failure but silent if the return value is dropped.
    ("user32", "SendInput", ctypes.c_uint, [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]),
    ("user32", "GetSystemMetrics", ctypes.c_int, [ctypes.c_int]),
    ("user32", "GetWindowLongW", ctypes.c_long, [ctypes.c_void_p, ctypes.c_int]),
    ("user32", "GetWindowDC", ctypes.c_void_p, [ctypes.c_void_p]),
    ("user32", "ReleaseDC", ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p]),
    (
        "user32",
        "PrintWindow",
        wintypes.BOOL,
        [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT],
    ),
    (
        "kernel32",
        "OpenProcess",
        ctypes.c_void_p,
        [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
    ),
    ("kernel32", "CloseHandle", wintypes.BOOL, [ctypes.c_void_p]),
    # Global-memory helpers for draining the GDI+ encode stream. Declared here
    # rather than beside the encoder so every bound symbol in the package sits in
    # one table with an explicit signature.
    ("kernel32", "GlobalSize", ctypes.c_size_t, [ctypes.c_void_p]),
    ("kernel32", "GlobalLock", ctypes.c_void_p, [ctypes.c_void_p]),
    ("kernel32", "GlobalUnlock", wintypes.BOOL, [ctypes.c_void_p]),
    (
        "ole32",
        "CreateStreamOnHGlobal",
        ctypes.c_long,
        [ctypes.c_void_p, wintypes.BOOL, ctypes.POINTER(ctypes.c_void_p)],
    ),
    (
        "ole32",
        "GetHGlobalFromStream",
        ctypes.c_long,
        [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
    ),
    (
        "psapi",
        "GetModuleFileNameExW",
        wintypes.DWORD,
        [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, wintypes.DWORD],
    ),
)

# ── COM vtable table: the ONLY place a slot index may appear ──
# (interface, method, slot, restype, argtypes-after-the-this-pointer).
# Verified live on Windows 11. Two entries are here specifically so a call site
# cannot pick the wrong neighbour: ``ElementFromPoint`` sits next to
# ``ElementFromHandle`` and silently resolves to the desktop when handed a
# handle, and ``GetCurrentPropertyValueEx`` sits next to the plain form and is
# the only one that can distinguish "unsupported" from the property's default.
_VTABLE: dict[tuple[str, str], tuple[int, Any, list[Any]]] = {
    # IUIAutomation
    ("IUIAutomation", "ElementFromHandle"): (
        6,
        ctypes.c_long,
        [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
    ),
    ("IUIAutomation", "ElementFromPoint"): (
        7,
        ctypes.c_long,
        [POINT, ctypes.POINTER(ctypes.c_void_p)],
    ),
    ("IUIAutomation", "CreateCacheRequest"): (
        20,
        ctypes.c_long,
        [ctypes.POINTER(ctypes.c_void_p)],
    ),
    # IUIAutomationElement
    ("IUIAutomationElement", "BuildUpdatedCache"): (
        9,
        ctypes.c_long,
        [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
    ),
    ("IUIAutomationElement", "GetCurrentPropertyValueEx"): (
        11,
        ctypes.c_long,
        [ctypes.c_int, wintypes.BOOL, ctypes.POINTER(VARIANT)],
    ),
    ("IUIAutomationElement", "GetCachedPropertyValue"): (
        12,
        ctypes.c_long,
        [ctypes.c_int, ctypes.POINTER(VARIANT)],
    ),
    ("IUIAutomationElement", "GetCachedChildren"): (
        19,
        ctypes.c_long,
        [ctypes.POINTER(ctypes.c_void_p)],
    ),
    ("IUIAutomationElement", "get_CurrentIsPassword"): (
        35,
        ctypes.c_long,
        [ctypes.POINTER(ctypes.c_int)],
    ),
    ("IUIAutomationElement", "SetFocus"): (3, ctypes.c_long, []),
    ("IUIAutomationElement", "GetCurrentPattern"): (
        16,
        ctypes.c_long,
        [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
    ),
    # IUIAutomationCacheRequest
    ("IUIAutomationCacheRequest", "AddProperty"): (3, ctypes.c_long, [ctypes.c_int]),
    ("IUIAutomationCacheRequest", "put_TreeScope"): (7, ctypes.c_long, [ctypes.c_int]),
    ("IUIAutomationCacheRequest", "put_AutomationElementMode"): (
        11,
        ctypes.c_long,
        [ctypes.c_int],
    ),
    # IUIAutomationElementArray
    ("IUIAutomationElementArray", "get_Length"): (
        3,
        ctypes.c_long,
        [ctypes.POINTER(ctypes.c_int)],
    ),
    ("IUIAutomationElementArray", "GetElement"): (
        4,
        ctypes.c_long,
        [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
    ),
    # ── Control patterns ──
    # Every slot below was validated against a REAL window rather than transcribed,
    # by probing a read-only getter in a subprocess (a wrong slot is an access
    # violation, so a crash is data) and requiring TWO independent signals: a value
    # whose SHAPE could only come from the method named, and a neighbouring slot that
    # answers E_INVALIDARG or E_FAIL. Measured on WPF (ACME), Win32 (Explorer),
    # Chromium and the shell.
    #
    # **A plausible value is not enough, and two interfaces here prove it.** A wrong
    # action slot does not necessarily fault — it can land on a neighbour that returns
    # S_OK while doing something else entirely, which no return-code check and no
    # behavioural test of ours would catch:
    #
    # * ``ScrollPattern.Scroll`` at slot 4 is ``SetScrollPercent``. It answered S_OK
    #   for every call while the panel never moved, so the driver reported "scrolled
    #   2 page(s)" over an unchanged window. Caught only by reading the panel's own
    #   scroll percentage back and seeing it pinned.
    # * ``LegacyIAccessiblePattern.DoDefaultAction`` at slot 4 is ``Select``.
    #
    # So each ACTION slot below is fixed by anchoring the GETTERS around it, never by
    # counting members from the header. The anchor is named on each interface that
    # needed one.
    #
    # ``Invoke``/``Toggle``/``Select``/``SetValue``/``Expand``/``Collapse``/``Scroll``
    # are the ONLY slots in this table that change another application's state. Each
    # is reached exclusively through its helper in ``windows_ffi_uia``, so there is one audited
    # call site per verb.
    ("IUIAutomationInvokePattern", "Invoke"): (3, ctypes.c_long, []),
    ("IUIAutomationTogglePattern", "Toggle"): (3, ctypes.c_long, []),
    # ``c_void_p``, NOT ``c_wchar_p``: the parameter is a BSTR, and ctypes
    # marshals ``c_wchar_p`` as a bare ``LPCWSTR`` with no length prefix. A
    # provider that calls ``SysStringLen`` on it reads the 4 bytes BEFORE the
    # buffer as the length — measured returning 282 for an 11-character string,
    # i.e. ~540 bytes of adjacent heap written into the target field. The caller
    # allocates a real BSTR (see ``set_element_value``).
    ("IUIAutomationValuePattern", "SetValue"): (3, ctypes.c_long, [ctypes.c_void_p]),
    ("IUIAutomationValuePattern", "get_CurrentIsReadOnly"): (
        5,
        ctypes.c_long,
        [ctypes.POINTER(ctypes.c_int)],
    ),
    ("IUIAutomationExpandCollapsePattern", "Expand"): (3, ctypes.c_long, []),
    ("IUIAutomationExpandCollapsePattern", "Collapse"): (4, ctypes.c_long, []),
    ("IUIAutomationExpandCollapsePattern", "get_CurrentExpandCollapseState"): (
        5,
        ctypes.c_long,
        [ctypes.POINTER(ctypes.c_int)],
    ),
    ("IUIAutomationSelectionItemPattern", "Select"): (3, ctypes.c_long, []),
    # Scroll takes BOTH axes in one call, hence two ScrollAmount arguments.
    #
    # This interface's layout was the OTHER one that did not match a naive reading of
    # the header, and it failed silently: ``Scroll`` at slot 4 is ``SetScrollPercent``,
    # which returned S_OK for every call while moving nothing, and the percent getters
    # read one slot early. Pinned by two shape-distinguishable anchors on a real
    # scrollable panel: slot 8 returned 20.13 (a ViewSize FRACTION — the visible part
    # of 25 rows, so not a percent), and slot 10 returned 1 for
    # ``VerticallyScrollable`` (a VARIANT_BOOL, so not a double). Those fix
    # ``Scroll`` at 3 and the percents at 5/6.
    ("IUIAutomationScrollPattern", "Scroll"): (
        3,
        ctypes.c_long,
        [ctypes.c_int, ctypes.c_int],
    ),
    ("IUIAutomationScrollPattern", "get_CurrentHorizontalScrollPercent"): (
        5,
        ctypes.c_long,
        [ctypes.POINTER(ctypes.c_double)],
    ),
    ("IUIAutomationScrollPattern", "get_CurrentVerticalScrollPercent"): (
        6,
        ctypes.c_long,
        [ctypes.POINTER(ctypes.c_double)],
    ),
    ("IUIAutomationScrollPattern", "get_CurrentVerticallyScrollable"): (
        10,
        ctypes.c_long,
        [ctypes.POINTER(ctypes.c_int)],
    ),
    # ``DoDefaultAction`` is the ladder's LAST rung, and deliberately never a
    # generic write path: this interface also exposes ``SetValue``, which would be
    # an un-gated route around ValuePattern, so that slot is NOT bound here.
    #
    # This interface's layout is the one that did NOT match a naive reading of the
    # header, which is why the anchors are named: ``get_CurrentRole`` (slot 10)
    # returned 43 = ``ROLE_SYSTEM_PUSHBUTTON`` on a real Button, and
    # ``get_CurrentDefaultAction`` (slot 15) returned ``'Press'`` on the same
    # element. Two independent anchors agreeing is what fixes ``DoDefaultAction`` at
    # slot 3; an off-by-one here would have called ``Select``.
    ("IUIAutomationLegacyIAccessiblePattern", "DoDefaultAction"): (3, ctypes.c_long, []),
    ("IUIAutomationLegacyIAccessiblePattern", "get_CurrentRole"): (
        10,
        ctypes.c_long,
        [ctypes.POINTER(ctypes.c_int)],
    ),
    ("IUIAutomationLegacyIAccessiblePattern", "get_CurrentDefaultAction"): (
        15,
        ctypes.c_long,
        [ctypes.POINTER(ctypes.c_void_p)],
    ),
    # IUnknown, for the reference discipline below.
    ("IUnknown", "AddRef"): (1, ctypes.c_ulong, []),
    ("IUnknown", "Release"): (2, ctypes.c_ulong, []),
}

_LIB_NAMES = ("ole32", "oleaut32", "user32", "kernel32", "psapi", "shcore", "dwmapi")

_libs: "dict[str, Any] | None" = None
_libs_lock = threading.Lock()
#: COM apartment state is PER THREAD, and the gateway dispatches every driver
#: call on a worker thread from a bounded executor — so initialization has to be
#: per-thread rather than once per process.
_thread_state = threading.local()
#: Every ``IUIAutomation`` client built by :func:`automation`, across ALL worker
#: threads. ``_thread_state`` is a ``threading.local`` and so is unreachable from
#: another thread, but the backend's ``close()`` runs on the event-loop thread
#: while the clients were created on pooled workers — so a thread-local-only
#: cleanup would release NONE of them and leak a client per worker for the process
#: lifetime. The clients are MTA (``COINIT_MULTITHREADED``), which is exactly what
#: makes a cross-thread ``Release`` legal. Guarded by its own lock.
_clients_lock = threading.Lock()
_clients: "list[Any]" = []
#: Bumped by :func:`release_all_clients`. A worker's cached client is stamped with
#: the generation it was built in, and :func:`automation` rebuilds when the stamp
#: is stale. This is what makes the cross-thread cleanup SAFE: a released
#: ``c_void_p`` keeps its address, so truthiness cannot detect a freed client and a
#: worker would hand the dead pointer straight back to ``vcall`` — an access
#: violation no ``try`` can catch. The counter is the only signal that survives the
#: release, and it is read under ``_clients_lock``.
_clients_generation = 0


def _bind(lib: Any, symbol: str, restype: Any, argtypes: "Sequence[Any] | None") -> None:
    """Bind one flat function, refusing an unspecified signature.

    Raises rather than accepting ``argtypes=None``: the ctypes default marshals a
    Python int as a 32-bit C int, which truncates every handle and pointer.
    """
    if argtypes is None:
        raise ComputerUseUnsupported(f"{symbol} declared without argtypes")
    fn = getattr(lib, symbol)
    fn.argtypes = list(argtypes)
    fn.restype = restype


def _libraries() -> "dict[str, Any]":
    """Load and bind the Win32 libraries, once per process.

    The load lives here rather than at module scope so importing this module on
    macOS or Linux costs nothing and cannot fail — which is what keeps the whole
    package importable on the CI fleet.
    """
    global _libs
    with _libs_lock:
        if _libs is not None:
            return _libs
        if not platform_compat.IS_WINDOWS:
            raise ComputerUseUnsupported("the Windows UI Automation driver needs Windows")
        loaded: dict[str, Any] = {}
        for name in _LIB_NAMES:
            try:
                # The ignore is required because CI runs mypy on Linux, where
                # ctypes has no WinDLL attribute at all.
                loaded[name] = ctypes.WinDLL(name, use_last_error=True)  # type: ignore[attr-defined]
            except OSError as exc:
                raise ComputerUseUnsupported(f"could not load {name}: {exc}") from exc
        for lib_key, symbol, restype, argtypes in _FN_SPECS:
            try:
                _bind(loaded[lib_key], symbol, restype, argtypes)
            except AttributeError as exc:
                raise ComputerUseUnsupported(f"{lib_key}!{symbol} is unavailable") from exc
        _libs = loaded
        return _libs


def libraries() -> "dict[str, Any]":
    """The bound libraries, raising :class:`ComputerUseUnsupported` off Windows."""
    return _libraries()


def reset_libraries() -> None:
    """Drop the cached handles. For tests and a backend swap."""
    global _libs
    with _libs_lock:
        _libs = None


def available() -> bool:
    """Whether the Windows native surface can be used here. Never raises."""
    if not platform_compat.IS_WINDOWS:
        return False
    try:
        _libraries()
        return True
    except Exception:
        logger.debug("Windows computer-use libraries unavailable", exc_info=True)
        return False


def _guid(text: str) -> GUID:
    libs = _libraries()
    out = GUID()
    if libs["ole32"].CLSIDFromString(text, ctypes.byref(out)) != S_OK:
        raise ComputerUseUnsupported(f"malformed GUID {text}")
    return out


CLSID_CUIAutomation_STR = "{ff48dba4-60ef-4201-aa87-54103eef594e}"
IID_IUIAutomation_STR = "{30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}"


def vcall(ptr: Any, interface: str, method: str) -> Any:
    """Build a callable for *interface*'s *method* on the COM object at *ptr*.

    Resolution is by NAME against :data:`_VTABLE`, never by a literal index at
    the call site. That indirection is the whole point: a mistyped index is an
    access violation with no traceback, and two of the slots in this package's
    working set have a neighbour that returns ``S_OK`` while doing something
    else entirely.
    """
    try:
        slot, restype, argtypes = _VTABLE[(interface, method)]
    except KeyError:
        raise ComputerUseUnsupported(f"no vtable entry for {interface}::{method}") from None
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0]
    fn_ptr = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[slot]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)  # type: ignore[attr-defined]
    return proto(fn_ptr)


def add_ref(ptr: Any) -> None:
    """Take a reference on a COM object."""
    if ptr:
        vcall(ptr, "IUnknown", "AddRef")(ptr)


def release(ptr: Any) -> None:
    """Drop a reference, tolerating a NULL and never raising.

    A failure to release leaks one object; raising here would abort a tree walk
    partway through and leak all of them.
    """
    if not ptr:
        return
    try:
        vcall(ptr, "IUnknown", "Release")(ptr)
    except Exception:
        logger.debug("COM Release failed", exc_info=True)


@contextmanager
def owned(ptr: Any) -> Iterator[Any]:
    """Own a COM reference for the duration of a block, always releasing it.

    A tree walk mints thousands of element references; leaking them is a real RSS
    bug the session watchdog would eventually recycle the process over. Same
    rationale as ``macos_ffi.cf_string``.
    """
    try:
        yield ptr
    finally:
        release(ptr)


def release_all(ptrs: "Sequence[Any]") -> None:
    """Release every reference in *ptrs*, tolerating NULLs."""
    for ptr in ptrs:
        release(ptr)


def _ensure_apartment() -> None:
    """Initialize COM for THIS thread, at most once.

    Per-thread rather than per-process because the gateway runs every driver call
    on a pooled worker thread. ``RPC_E_CHANGED_MODE`` means the thread already
    has an apartment of the other kind: the thread is still usable, so it is not
    an error — but we must not later uninitialize an apartment we did not create.
    """
    if getattr(_thread_state, "com_ready", False):
        return
    libs = _libraries()
    hr = libs["ole32"].CoInitializeEx(None, COINIT_MULTITHREADED)
    # S_FALSE (1) means already initialized on this thread, which is fine.
    if hr < 0 and (hr & 0xFFFFFFFF) != RPC_E_CHANGED_MODE:
        raise ComputerUseUnsupported(f"CoInitializeEx failed (0x{hr & 0xFFFFFFFF:08X})")
    _thread_state.com_ready = True


def automation() -> Any:
    """The per-thread ``IUIAutomation`` client.

    Cached per thread alongside the apartment: a COM interface pointer obtained
    in one apartment must not be used from another, so a process-wide singleton
    would be the wrong lifetime.

    The cached pointer is only reused when its GENERATION still matches. Testing
    truthiness is not enough and the difference is a crash rather than a leak:
    ``release`` does not alter the ``c_void_p``, so a client freed by
    :func:`release_all_clients` still reads as a live pointer and the next call
    would dispatch through a freed vtable.
    """
    with _clients_lock:
        generation = _clients_generation
    existing = getattr(_thread_state, "automation", None)
    if existing and getattr(_thread_state, "automation_generation", -1) == generation:
        return existing
    # Stale: the pointer was released underneath this thread. Drop the slot WITHOUT
    # releasing it again — the reference is already gone, and a second Release
    # would be on freed memory.
    _thread_state.automation = None
    _ensure_apartment()
    libs = _libraries()
    out = ctypes.c_void_p()
    hr = libs["ole32"].CoCreateInstance(
        ctypes.byref(_guid(CLSID_CUIAutomation_STR)),
        None,
        CLSCTX_INPROC_SERVER,
        ctypes.byref(_guid(IID_IUIAutomation_STR)),
        ctypes.byref(out),
    )
    if hr != S_OK or not out:
        raise ComputerUseUnsupported(
            f"CoCreateInstance(CUIAutomation) failed (0x{hr & 0xFFFFFFFF:08X})"
        )
    _thread_state.automation = out
    with _clients_lock:
        _clients.append(out)
        _thread_state.automation_generation = _clients_generation
    return out


def reset_thread_state() -> None:
    """Forget THIS thread's client and apartment flag. For tests."""
    ptr = getattr(_thread_state, "automation", None)
    if ptr:
        release(ptr)
        with _clients_lock:
            if ptr in _clients:
                _clients.remove(ptr)
    _thread_state.automation = None
    _thread_state.automation_generation = -1
    _thread_state.com_ready = False


def release_all_clients() -> None:
    """Release every ``IUIAutomation`` client built on any worker thread.

    This is what a backend ``close()`` must call: it runs on the event-loop
    thread, while the clients were created on pooled workers whose
    ``threading.local`` it cannot see. Without this a backend swap would leave the
    old driver's clients live and its ``automation()`` returning a cached pointer,
    so the swap would be a no-op.

    Two things make the cross-thread release safe, and each one is a process abort
    if it is missing rather than a leak:

    * **This thread enters the MTA first.** A cross-apartment ``Release`` on an MTA
      object is legal only while the MTA is still alive. Once the last pooled
      worker exits, the apartment is torn down and ``UIAutomationCore`` can be
      unloaded, so dispatching ``Release`` through the object's vtable would read a
      slot in an unmapped page. ``_ensure_apartment`` on THIS thread keeps the MTA
      referenced for the duration. A host where the apartment cannot be entered
      leaves the clients alone: leaking them is recoverable, faulting is not.
    * **The generation is bumped**, which is what actually invalidates each
      worker's cached pointer. The freed ``c_void_p`` keeps its address and still
      reads as truthy, so nothing about the pointer itself can tell a worker its
      client is gone — see :func:`automation`.

    Does not ``CoUninitialize`` the workers' apartments — that can only be done on
    the thread that initialized each one, which is not reachable here. The
    apartments cost nothing while idle and are torn down when the worker exits.
    """
    global _clients_generation
    with _clients_lock:
        pending = list(_clients)
        _clients.clear()
        # Bumped even if the release below is skipped: the caller asked for these
        # clients to stop being used, and a stale generation is the only thing that
        # stops a worker handing one back.
        _clients_generation += 1
    if not pending:
        return
    try:
        _ensure_apartment()
    except Exception:
        logger.debug(
            "could not enter an apartment to release %d UI Automation client(s); "
            "leaking them rather than releasing across a torn-down MTA",
            len(pending),
            exc_info=True,
        )
        return
    for ptr in pending:
        release(ptr)
