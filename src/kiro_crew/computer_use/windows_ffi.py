"""The ONLY module in this package that touches Windows-native code.

Everything native for Windows lives here: the UI Automation COM client, its
vtable dispatch, VARIANT/BSTR marshalling, window enumeration, and the DPI
awareness scope. Every other Windows computer-use module talks to the OS through
the helpers below, so the FFI hazards are audited in one place — the same
property ``macos_ffi`` holds for macOS.

``import ctypes`` is a top-level import statement, but **no ``WinDLL`` runs at
module scope**. The libraries load inside :func:`_libraries`, cached in a module
global, and raise :class:`ComputerUseUnsupported` off Windows — so this module
imports cleanly on the macOS and Linux CI shards, and a test can exercise the
binding logic with a fake loader. ``from ctypes import wintypes`` supplies type
ALIASES only and imports cleanly on POSIX, which is what lets the structures
below sit at module scope on every platform.

UI Automation is COM-only — there is no C API — so every call is a manual vtable
dispatch. That makes the hazards here different in kind from the macOS ones:
**the slot index IS the ABI**, and a wrong index is an access violation that
kills the interpreter with no Python traceback, taking the gateway (and with it
cron, Slack and every session) down. Six hazards this module exists to contain,
each measured on Windows 11:

1. **A wrong vtable slot is a process abort, not an exception.** A blind slot
   sweep produced a real ``EXIT=139``. Hence :data:`_VTABLE`: every slot index in
   this package appears exactly once, here, and :func:`vcall` resolves by METHOD
   NAME so no call site can hand-roll an index.
2. **Two slots return ``S_OK`` while doing the WRONG thing.**
   ``ElementFromHandle`` is slot 6; slot 7 is ``ElementFromPoint``, and handed an
   HWND it reads it as a screen coordinate and cheerfully resolves to the whole
   DESKTOP. A desktop-scoped walk then reads every application's tree while
   looking like it worked, so :func:`element_from_hwnd` treats a NULL result as a
   hard error and never falls through.
3. **A missing ``argtypes`` truncates a 64-bit pointer to 32 bits.** An HWND
   passed as a bare Python int is the common form of this; every handle crosses
   the boundary as ``c_void_p``. :data:`_FN_SPECS` carries both ``argtypes`` and
   ``restype`` on every row and :func:`_bind` RAISES on ``argtypes=None`` rather
   than accepting the ctypes default.
4. **A BSTR must be decoded BEFORE ``VariantClear``.** Afterwards the freed block
   is reused, and different properties read back byte-identical — which looks
   exactly like a wrong-property bug and is not. :func:`variant_value` copies out
   first, with ``SysStringLen`` rather than a NUL scan so an embedded NUL cannot
   truncate a value.
5. **``restype`` must be ``c_long``, never ``ctypes.HRESULT``.** ``HRESULT``
   raises on a failure code, which turns an EXPECTED ``E_INVALIDARG`` — the
   normal answer for an unsupported property id — into an exception that reads
   like a crash.
6. **A ``ctypes.Structure`` declared in a function body leaks forever.**
   ``ctypes.POINTER`` memoises the type in a module-level dict with no eviction,
   so every call pins a fresh pair of type objects. Every structure and union
   here is at module scope.

**The secure-field decision is the security-critical function in this file.**
:func:`is_secure_element` gates three separate floors — value redaction, input
refusal and whole-window screenshot suppression — so it fails CLOSED. See its
docstring for the decision table and for why a cached value may never decide it.
"""

from __future__ import annotations

import ctypes
import logging
import math
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes  # type aliases only; imports cleanly on every platform
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

from kiro_crew import platform_compat
from kiro_crew.computer_use.types import ComputerUseUnsupported

logger = logging.getLogger(__name__)

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
    # is reached exclusively through the helper below it, so there is one audited
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
    focus**, so it is never used to "aim" an element-addressed action: the pattern
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


# ── DPI ──


@contextmanager
def dpi_awareness_scope() -> Iterator[None]:
    """Run a block per-monitor-DPI-aware, restoring the previous context.

    ONE awareness scope must cover the tree walk, the capture, the confinement
    lookup and the click. UIA rectangles follow the CALLER's awareness rather
    than always being physical pixels, and a mismatch does not merely offset a
    point: a physical coordinate fed to ``WindowFromPoint`` from an unaware
    thread resolved to a DIFFERENT APPLICATION than the same numbers from an
    aware thread. So the confinement check and the click can agree with each
    other while both pointing at the wrong window.

    Thread-scoped rather than process-wide: the process-wide setter is
    irreversible and would change behaviour for the whole gateway, including
    surfaces that have nothing to do with computer use.

    **A FAILED set raises rather than running the block unaware.** The setter
    returns NULL on failure, which is the same falsy value a successfully-restored
    default context can marshal as, so "the set failed" and "there is nothing to
    restore" cannot be told apart by the return value alone — and silently
    proceeding degrades into exactly the unaware state this scope exists to
    prevent, where the confinement check and the click agree with each other about
    the wrong window. ``GetLastError`` distinguishes them, and an unset awareness is
    a refusal (a caller sees ``ComputerUseUnsupported``) rather than a wrong answer.

    The restore is unconditional on a successful set, so this thread's awareness is
    never left changed. That matters because the pooled worker is shared with chat,
    terminal and browse work, and a leaked PER_MONITOR_AWARE_V2 would make later
    ``GetWindowRect`` / ``GetCursorPos`` calls from unrelated code read physical
    rather than virtualized coordinates.
    """
    libs = _libraries()
    setter = getattr(libs["user32"], "SetThreadDpiAwarenessContext", None)
    if setter is None:
        # Pre-1607 / 8.1: the API does not exist, so there is nothing to set and
        # nothing to restore. Reachable only if the symbol is absent while the
        # library bound successfully.
        yield
        return
    _ct.set_last_error(0)
    previous = setter(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
    if not previous:
        error = _ct.get_last_error()
        if error:
            raise ComputerUseUnsupported(
                f"SetThreadDpiAwarenessContext failed (GetLastError={error}); refusing to "
                "measure or click under an unknown DPI awareness"
            )
        # NULL with no error: the previous context marshalled falsy. The set
        # SUCCEEDED, so the block runs aware; there is simply nothing to restore.
        yield
        return
    try:
        yield
    finally:
        try:
            setter(previous)
        except Exception:
            logger.debug("restoring DPI awareness context failed", exc_info=True)


# ── Keyboard: SendInput ──
#
# **This is the one input path on Windows that is not confined to the target
# application.** ``SendInput`` takes no hwnd and no pid: it injects at the session
# level and delivery follows whatever holds focus, so unlike macOS's
# ``CGEventPostToPid`` there is no per-process keystroke route at all. Two
# consequences the callers must honour, both enforced in ``windows_driver``:
#
# * the target must be FOCUSED first (``set_element_focus``) and the result must
#   SAY the focus moved, because the operator's caret really did move;
# * a keystroke is never a fallback for a failed focus. If focus did not land where
#   the model aimed, the keystrokes would go to whatever the application had
#   focused — which can be the password field the secure check just refused.


def _vk_table() -> "dict[str, int]":
    """Canonical key name -> Windows virtual-key code.

    Keyed on :data:`keymap.KEY_ALIASES`' canonical names, which is what makes this
    table small enough to audit: every alias the model may spell (``esc``,
    ``pgdn``, ``arrowleft``) resolves to one entry here.

    Built by a function rather than written out so the 26 letters, 10 digits and 20
    function keys are generated instead of transcribed — three ranges where a typo
    is invisible on review and sends a neighbouring key at runtime.

    ``test_computer_use_windows_input.py`` asserts this table covers every canonical
    name ``keymap`` can produce, so a key added there cannot ship unmapped.
    """
    letters = {chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)}
    digits = {chr(c): c for c in range(ord("0"), ord("9") + 1)}
    named = {
        # Editing / whitespace. ``delete`` and ``backspace`` are DIFFERENT keys
        # here, which is why keymap refuses to fold them onto one canonical name.
        "backspace": 0x08,
        "tab": 0x09,
        "return": 0x0D,
        "escape": 0x1B,
        "space": 0x20,
        "spacebar": 0x20,
        " ": 0x20,
        "delete": 0x2E,  # VK_DELETE — forward delete
        "forwarddelete": 0x2E,
        "insert": 0x2D,
        "help": 0x2F,  # VK_HELP
        # Navigation
        "pageup": 0x21,
        "pagedown": 0x22,
        "end": 0x23,
        "home": 0x24,
        "arrowleft": 0x25,
        "arrowup": 0x26,
        "arrowright": 0x27,
        "arrowdown": 0x28,
        # Punctuation (OEM codes, US layout)
        "semicolon": 0xBA,
        "equals": 0xBB,
        "comma": 0xBC,
        "minus": 0xBD,
        "period": 0xBE,
        "slash": 0xBF,
        "backtick": 0xC0,
        "leftbracket": 0xDB,
        "backslash": 0xDC,
        "rightbracket": 0xDD,
        "apostrophe": 0xDE,
        # Keypad
        "keypadmultiply": 0x6A,
        "keypadplus": 0x6B,
        "keypadminus": 0x6D,
        "keypaddecimal": 0x6E,
        "keypaddivide": 0x6F,
        "keypadclear": 0x0C,  # VK_CLEAR
        "keypadenter": 0x0D,  # no distinct VK; Return with the extended flag
        "keypadequals": 0xBB,
        # Media
        "mute": 0xAD,
        "volumedown": 0xAE,
        "volumeup": 0xAF,
    }
    named.update({f"keypad{n}": 0x60 + n for n in range(10)})
    named.update({f"f{n}": 0x70 + (n - 1) for n in range(1, 21)})
    return {**letters, **digits, **named}


#: Canonical key name -> VK code. See :func:`_vk_table`.
VK_CODES: "dict[str, int]" = _vk_table()

#: Canonical modifier name -> VK code. Two names in the platform-free vocabulary are
#: deliberately ABSENT, and a spec naming either is refused rather than silently
#: dropped — dropping a modifier sends a DIFFERENT chord than the caller asked for.
#:
#: * ``fn`` has no virtual-key code at all; it is handled in keyboard firmware below
#:   the OS.
#: * ``capslock`` is a LOCK on Windows, not a holdable modifier. macOS models it as
#:   ``FLAG_ALPHA_SHIFT``, a per-event flag that applies to the one synthesized event
#:   and changes no machine state — but ``VK_CAPITAL`` down/up TOGGLES the lock, and
#:   the flip persists after the chord, so ``capslock+a`` would leave every subsequent
#:   keystroke the OPERATOR types capitalized. That is exactly the kind of residual
#:   state this driver must not leave behind, and unlike the other modifiers it cannot
#:   be undone by releasing the key. Refusing is the honest answer: a caller who wants
#:   a capital letter should send ``shift+a`` or the character itself.
VK_MODIFIERS: "dict[str, int]" = {
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "win": 0x5B,  # VK_LWIN
}

#: Modifiers absent from :data:`VK_MODIFIERS` for a REASON rather than an omission,
#: each with the refusal that says which. A caller told only "no virtual-key code"
#: would reasonably read it as a gap to work around; these are decisions, and the
#: message names the working alternative so the model spends no turn guessing.
_UNHOLDABLE_MODIFIERS: "dict[str, str]" = {
    "capslock": (
        "'capslock' cannot be used as a modifier on Windows: VK_CAPITAL TOGGLES a "
        "machine-wide lock rather than being held, so the flip would persist after "
        "this call and capitalize everything the operator types next. Send 'shift+<key>' "
        "for one capital, or type the character directly with computer_type_text"
    ),
    "fn": (
        "'fn' has no virtual-key code on Windows: it is handled in keyboard firmware "
        "below the OS, so no synthesized event can carry it. Name the key the "
        "combination produces instead (for example 'f5' or 'home')"
    ),
}

#: Keys that MUST carry ``KEYEVENTF_EXTENDEDKEY``. Without it the injected scan
#: code is the numpad twin of the same VK, so an arrow key moves the caret only
#: while NumLock happens to be off and Insert/Delete hit the keypad instead.
_EXTENDED_KEYS: frozenset[str] = frozenset(
    {
        "arrowleft",
        "arrowright",
        "arrowup",
        "arrowdown",
        "home",
        "end",
        "pageup",
        "pagedown",
        "insert",
        "delete",
        "forwarddelete",
        "keypadenter",
        "keypaddivide",
        "mute",
        "volumeup",
        "volumedown",
    }
)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_EXTENDEDKEY = 0x0001


class KEYBDINPUT(ctypes.Structure):
    """Module scope: a Structure in a function body leaks (hazard 6)."""

    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class MOUSEINPUT(ctypes.Structure):
    """Module scope: a Structure in a function body leaks (hazard 6)."""

    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    # BOTH arms are declared, and the size is what matters: ``SendInput`` is passed
    # ``sizeof(INPUT)`` as ``cbSize`` and rejects the whole call with
    # ERROR_INVALID_PARAMETER if it disagrees. A union missing the larger arm would
    # under-report that size and every call would silently fail.
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _key_event(vk: int, scan: int, flags: int) -> INPUT:
    """One INPUT record. ``dwExtraInfo`` stays NULL, which is what we inject with."""
    record = INPUT()
    record.type = INPUT_KEYBOARD
    record.u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
    return record


def _send(records: "Sequence[INPUT]") -> int:
    """Submit records in ONE ``SendInput`` call. Returns how many were accepted.

    One call rather than a loop per key, and that is required rather than an
    optimization: ``SendInput`` guarantees the batch is not interleaved with other
    threads' input, so a chord cannot have a foreign keystroke land between its
    modifier-down and key-down. A loop offers no such guarantee.
    """
    if not records:
        return 0
    libs = _libraries()
    count = len(records)
    array = (INPUT * count)(*records)
    return int(libs["user32"].SendInput(count, array, ctypes.sizeof(INPUT)))


#: How many times a RELEASE batch is retried before giving up. A release is not
#: optional — see :func:`_send_release` — so it is worth retrying, but not forever:
#: an unbounded loop against a permanently-blocking hook would spin a pooled worker.
_RELEASE_ATTEMPTS = 3


def _send_release(records: "Sequence[INPUT]", *, atomic: bool = False) -> None:
    """Submit RELEASE records, retrying, and raise if any stays unreleased.

    **A release is the one batch whose acceptance cannot be assumed.** ``SendInput``
    returns how many records it accepted and can stop early — a lower-level hook, or
    UIPI — and it returns NORMALLY, so a discarded count means a modifier or mouse
    button is left HELD on the operator's own machine. Nothing later un-holds it: a
    stuck Ctrl alters every keystroke they type next, and a stuck mouse button turns
    every motion of their hand into a drag, which on a file manager moves their files.

    So the count is checked and the unaccepted records are resubmitted. ``SendInput``
    accepts a prefix, so by default the retry is exactly ``records[accepted:]`` — resending
    the whole batch would re-release keys already up, which is harmless but muddies what
    failed. Retrying is worth it here precisely because the alternative is not "the
    action failed" but "the operator's hardware is now wrong".

    **``atomic=True`` resends the WHOLE batch instead**, and a drag's release requires it.
    That batch is ``[move(end), button_up]``: the records are not independent, because the
    move is what AIMS the release. A tail-only retry after a partial acceptance resends the
    bare ``button_up``, which carries no coordinate and therefore releases wherever the
    cursor happens to be by then — the drop lands outside the window
    ``hwnd_owns_point`` authorized, which is precisely the defect the aimed release exists
    to prevent. Re-sending the move first is idempotent (a cursor position is state, not an
    edge), so the only cost is one redundant record.

    Raises :class:`ComputerUseUnsupported` when a record is still unreleased after
    :data:`_RELEASE_ATTEMPTS`. The caller must NOT swallow that: reporting success
    over a stuck modifier is what turns a failed keystroke into corrupted input.
    """
    batch = list(records)
    pending = batch
    for _ in range(_RELEASE_ATTEMPTS):
        if not pending:
            return
        accepted = _send(pending)
        pending = batch if (atomic and accepted < len(pending)) else pending[accepted:]
    if pending:
        raise ComputerUseUnsupported(
            f"{len(pending)} release record(s) were rejected after {_RELEASE_ATTEMPTS} "
            "attempts (likely a lower-level hook or UIPI); a key or mouse button may "
            "still be held — press it physically to clear it"
        )


def send_key_chord(key: str, modifiers: "Sequence[str]") -> None:
    """Press *key* with *modifiers* held, then release everything.

    Verified against an instrumented WinForms window that logs every ``KeyDown`` it
    receives: ``a`` arrived as ``key=A``, ``cmd+a`` as ``key=A ctrl=True`` (the
    intent mapping — see ``keymap.MODIFIER_ALIASES``), ``backspace`` as ``key=Back``,
    and ``shift+home`` moved the selection to 0..10 read back through ``EM_GETSEL``.

    One measured non-result worth recording, so it is not mistaken for a bug here: a
    WinForms ``TextBox`` does NOT implement Ctrl+A. The chord arrives — the probe logs
    it — and the selection stays empty, because select-all is not a default binding on
    that control. Whether a chord DOES anything is the target's decision; this function
    is responsible only for delivering the one that was asked for.

    Releases in REVERSE order, and releases even when the key press was rejected:
    a modifier left down is a stuck Ctrl/Alt on the operator's real keyboard, which
    affects every subsequent keystroke they type themselves. That is why the release
    batch is unconditional.

    Raises :class:`ComputerUseUnsupported` for a key or modifier with no VK code
    rather than dropping it — a silently dropped modifier sends a DIFFERENT chord,
    and the whole point of routing through ``keymap.parse_spec`` upstream is that a
    keystroke the caller did not ask for never reaches a live window.
    """
    vk = VK_CODES.get(key)
    if vk is None:
        raise ComputerUseUnsupported(f"no Windows virtual-key code for {key!r}")
    mod_vks: list[int] = []
    for name in modifiers:
        mod_vk = VK_MODIFIERS.get(name)
        if mod_vk is None:
            if name in _UNHOLDABLE_MODIFIERS:
                raise ComputerUseUnsupported(_UNHOLDABLE_MODIFIERS[name])
            raise ComputerUseUnsupported(f"the {name!r} modifier has no Windows virtual-key code")
        mod_vks.append(mod_vk)

    flags = KEYEVENTF_EXTENDEDKEY if key in _EXTENDED_KEYS else 0
    down = [_key_event(m, 0, 0) for m in mod_vks] + [_key_event(vk, 0, flags)]
    up = [_key_event(vk, 0, flags | KEYEVENTF_KEYUP)] + [
        _key_event(m, 0, KEYEVENTF_KEYUP) for m in reversed(mod_vks)
    ]
    try:
        _send(down)
    finally:
        # Unconditional AND verified: a modifier left held would alter every keystroke
        # the OPERATOR types next, so it is not enough to submit the release — the
        # release batch can itself be truncated, and ``_send_release`` retries the
        # unaccepted tail and raises rather than letting that pass silently.
        _send_release(up)


def send_text(text: str) -> int:
    """Type *text* as UNICODE key events. Returns the characters accepted.

    ``KEYEVENTF_UNICODE`` rather than a per-character VK lookup, for the same
    reason macOS posts Unicode events: it is layout-INDEPENDENT and can emit
    characters no single US keystroke reaches. A VK table would type the wrong
    character on a non-US layout, silently.

    Surrogate pairs are submitted as two records, which is how a non-BMP character
    (an emoji) has to cross this API.
    """
    if not text:
        return 0
    records: list[INPUT] = []
    for char in text:
        for code in _utf16_units(char):
            records.append(_key_event(0, code, KEYEVENTF_UNICODE))
            records.append(_key_event(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    accepted = _send(records)
    if accepted == len(records):
        return len(text)
    # A SHORT accept, reported in CHARACTERS the caller would recognise. Dividing the
    # record count by two is wrong for any non-BMP character: an emoji is a surrogate
    # PAIR, so it costs two code units and four records. ``"<emoji>a"`` with only the
    # first four records accepted has dropped the ``a``, yet ``accepted // 2`` is 2 —
    # exactly ``len(text)`` — so the caller's ``sent < len(text)`` check passes and the
    # partial write is reported as complete.
    #
    # So walk the same records back and count only the characters whose units ALL
    # landed. A character half-written is not written.
    sent_chars = 0
    consumed = 0
    for char in text:
        needed = len(_utf16_units(char)) * 2
        if consumed + needed > accepted:
            break
        consumed += needed
        sent_chars += 1
    return sent_chars


def _utf16_units(char: str) -> "list[int]":
    """The UTF-16 code units of one character (two for a non-BMP codepoint)."""
    encoded = char.encode("utf-16-le")
    return [int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2)]


# ── Mouse: the REAL pointer ──
#
# **Every function below moves the operator's physical cursor.** There is no
# app-scoped mouse route on Windows — ``SendInput`` carries no hwnd — so unlike
# macOS, where ``app_post`` delivers to one pid without touching the pointer, a
# coordinate click here IS a pointer warp. That is why:
#
# * ``policy.resolve_click_method`` must never resolve ``auto`` onto ``global``, and
#   the Windows driver refuses ``auto`` + coordinates outright rather than picking
#   the only method that could serve it;
# * the driver runs ``apps_windows.hwnd_owns_point`` before every call here. A
#   global event lands on whatever owns the pixel, so an authorized app's grant
#   does not authorize the coordinate.

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

#: ``SendInput``'s absolute coordinates are normalized to 0..65535 across the
#: VIRTUAL desktop, NOT pixels — a pixel value passed here lands near the top-left
#: corner on any real screen.
_ABSOLUTE_RANGE = 65535

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

#: ``MONITOR_DEFAULTTONEAREST`` — a window is always on SOME monitor, so the
#: nearest one is the right answer even for a rect parked partly off-screen.
MONITOR_DEFAULTTONEAREST = 2
#: ``MONITOR_DPI_TYPE.MDT_EFFECTIVE_DPI`` — the scaling actually in effect,
#: which is what a rendered size has to be compared against.
MDT_EFFECTIVE_DPI = 0

#: ``DWMWA_CLOAKED``. A NON-ZERO value means the window is composited but not shown:
#: a suspended packaged app, or a window on another virtual desktop. Distinct from
#: minimized, which ``IsIconic`` reports, and from hidden, which ``IsWindowVisible``
#: reports — so all three have to be asked separately.
DWMWA_CLOAKED = 14

#: Button name -> (down flag, up flag). Names match ``types.MOUSE_BUTTONS``.
_MOUSE_FLAGS: "dict[str, tuple[int, int]]" = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def _virtual_screen() -> "tuple[int, int, int, int]":
    """``(left, top, width, height)`` of the whole virtual desktop, in pixels.

    Read from ``GetSystemMetrics`` rather than assumed to start at (0, 0): a
    secondary monitor placed left of or above the primary gives the virtual desktop
    a NEGATIVE origin, and normalizing against a (0, 0) origin would land every
    click on the wrong monitor.

    **MUST be called inside :func:`dpi_awareness_scope`**, because these metrics are
    virtualized exactly like every other coordinate. Measured on a 125% display:
    ``(0, 0, 1536, 960)`` unaware against ``(0, 0, 1920, 1200)`` aware — so the same
    element rect normalizes to 32768 in one state and 26214 in the other, a 25%
    error that puts the click ~480px from the target. The caller cannot detect it
    afterwards: both answers look like valid coordinates.
    """
    libs = _libraries()
    get = libs["user32"].GetSystemMetrics
    return (
        int(get(SM_XVIRTUALSCREEN)),
        int(get(SM_YVIRTUALSCREEN)),
        int(get(SM_CXVIRTUALSCREEN)),
        int(get(SM_CYVIRTUALSCREEN)),
    )


def _normalized(x: float, y: float) -> "tuple[int, int]":
    """Screen pixels -> ``SendInput``'s 0..65535 absolute space.

    Rounds to the nearest unit rather than truncating: on a 1920-wide desktop one
    normalized unit is ~0.03px, so truncation is harmless, but on a small virtual
    desktop it can shift the point a whole pixel — enough to miss a 1px border.
    """
    left, top, width, height = _virtual_screen()
    if width <= 0 or height <= 0:
        raise ComputerUseUnsupported("the virtual screen reported a zero extent")
    # ``ceil`` over ``extent - 1``, and BOTH halves were measured rather than derived:
    # the cursor was moved to real pixels and read back with ``GetCursorPos``, sweeping
    # 13 x values across a 1920-wide desktop.
    #
    #     round(x * 65535 / width)       10 of 13 wrong (every point 1px low)
    #     round(x * 65535 / (width-1))    3 of 13 wrong (small x still 1px low)
    #     ceil (x * 65535 / (width-1))    0 of 13 wrong
    #
    # The OS FLOORS when it maps the normalized value back, so ceiling is its inverse;
    # rounding lands short for any x whose quotient falls below the .5 boundary. Plain
    # arithmetic makes the round form look correct because it is exact at 0 and at the
    # centre — the two points a spot check picks.
    #
    # A 1px error is not cosmetic: it is the difference between the edge pixel of the
    # authorized window and the first pixel of whatever sits behind it, and
    # ``hwnd_owns_point`` validated the point we were ASKED for, not the one we send.
    #
    # A single-pixel extent has no span to divide by, so it degenerates to 0 — the only
    # coordinate such a screen has.
    span_x = width - 1
    span_y = height - 1
    # The SAME pixel the confinement check authorized — see ``quantize_point``.
    px, py = quantize_point(x, y)
    nx = 0 if span_x <= 0 else math.ceil((px - left) * _ABSOLUTE_RANGE / span_x)
    ny = 0 if span_y <= 0 else math.ceil((py - top) * _ABSOLUTE_RANGE / span_y)
    return max(0, min(_ABSOLUTE_RANGE, nx)), max(0, min(_ABSOLUTE_RANGE, ny))


def _mouse_event(flags: int, *, x: int = 0, y: int = 0, data: int = 0) -> INPUT:
    record = INPUT()
    record.type = INPUT_MOUSE
    record.u.mi = MOUSEINPUT(dx=x, dy=y, mouseData=data, dwFlags=flags, time=0, dwExtraInfo=None)
    return record


def _move_to(x: float, y: float) -> INPUT:
    """An absolute move record. ``VIRTUALDESK`` is required for a multi-monitor desktop."""
    nx, ny = _normalized(x, y)
    return _mouse_event(
        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, x=nx, y=ny
    )


def post_mouse_click(x: float, y: float, *, button: str, count: int) -> None:
    """Move the REAL cursor to the point and click it *count* times.

    The move and the button events go in ONE ``SendInput`` batch so no other
    thread's input can interleave between them — without that, a click can land at
    wherever the pointer was moved to next rather than where it was aimed.

    Raises for an unknown button rather than defaulting to left: substituting a
    button performs a different gesture (a left click activates where a right click
    would have opened a menu), which is the same reason ``keymap`` refuses an
    unknown modifier.

    **A TRUNCATED batch is released, not ignored.** ``SendInput`` returns how many
    records it accepted and can stop early — a lower-level hook rejecting the
    injection, or ``UIPI`` blocking it mid-batch — so a batch cut between a
    button-down and its button-up leaves the operator's real mouse button PHYSICALLY
    HELD. Nothing later un-holds it: every subsequent motion of their own hand becomes
    a drag, which on a file manager or a canvas moves or destroys their data. So the
    count is checked, any accepted-but-unreleased button is released, and the caller is
    told the click failed rather than being allowed to read silence as success. This is
    the mouse twin of ``send_key_chord``'s unconditional reverse release.
    """
    flags = _MOUSE_FLAGS.get(button)
    if flags is None:
        raise ComputerUseUnsupported(f"unknown mouse button {button!r}")
    down, up = flags
    records = [_move_to(x, y)]
    for _ in range(max(1, count)):
        records.append(_mouse_event(down))
        records.append(_mouse_event(up))
    try:
        accepted = _send(records)
    except Exception:
        # The batch may have been partly applied before the failure, so the release
        # runs on this path too — for the same reason as the truncation path below.
        _send_release([_mouse_event(up)])
        raise
    if accepted < len(records):
        # A press is at an ODD index (0 is the move) and its release at the next even
        # one, so an odd accepted count means the last thing delivered was a DOWN.
        # Release unconditionally rather than only then: an extra up on an already-up
        # button is a no-op, while a missed one is a stuck button.
        _send_release([_mouse_event(up)])
        raise ComputerUseUnsupported(
            f"the click was only partly delivered ({accepted} of {len(records)} input "
            "records accepted, likely a lower-level hook or UIPI); the button was "
            "released so the pointer is not stuck"
        )


def post_mouse_drag(
    start: "tuple[float, float]",
    end: "tuple[float, float]",
    *,
    button: str,
    points: "Sequence[tuple[float, float]] | None" = None,
) -> None:
    """Press at *start*, travel through *points*, release at the end — REAL cursor.

    *points* is the whole path including both endpoints; ``None`` means the
    two-point form ``(start, end)``. ``start`` and ``end`` are still taken
    separately, and are what the press and release are aimed at, so a caller that
    passes a path cannot accidentally press somewhere the confinement check did not
    authorize.

    **Each path point is submitted as its OWN ``SendInput`` call, and that is the
    difference between drawing a curve and drawing a polyline.** It looks like the
    opposite of what :func:`_send`'s docstring asks for, so the measurement is worth
    stating: 65 move records in one batch drew a visibly corner-y polyline, while the
    same 65 points as 65 separate calls drew a smooth curve. A ``WH_MOUSE_LL`` hook
    counted 65 of 65 events reaching the queue in BOTH shapes, so nothing is dropped
    — Windows synthesizes ``WM_MOUSEMOVE`` from queue *state* when the target's
    message pump asks, rather than queueing one message per injected event, so a
    single batch mutates the cursor position many times before the target samples it
    even once. Separate calls yield between them, which is what lets the pump observe
    the intermediate positions. An inter-call *sleep* was measured to add nothing (0ms,
    2ms and 8ms drew identically), so there is none: it would only cost wall-clock
    while the operator's mouse button is held.

    **The press and the release are each AIMED, in one batch with their own move.** The
    press is ``[move(start), down]`` and the release is ``[move(end), up]``, and the
    aimed release is not decoration: a bare ``up`` record carries no coordinate, so it
    releases wherever the cursor happens to be when the OS processes it. With the path
    now submitted as N separate calls there are N yield points where the old single
    batch had one, so a foreign mouse move — the operator's own hand, another injector —
    can land between the last path point and the release. Since a release is where a
    DROP lands, and ``start``/``end`` are the two points ``hwnd_owns_point``
    authorized, re-aiming immediately before the ``up`` is what keeps the drop inside
    the window that was checked. It costs one extra record.

    The release is submitted even if the press or any move was rejected, for the same
    reason the keyboard releases unconditionally: a mouse button left down is a stuck
    button on the operator's real mouse, and every subsequent motion of their own hand
    becomes a drag, which on a file manager moves their files. The ``finally`` covers
    an exception; the accepted counts cover the quieter case of a ``SendInput``
    truncated mid-way (a lower-level hook, or UIPI), which returns NORMALLY. Both end
    with the button released, and a short delivery is reported rather than passed off
    as done — a drag that pressed and moved but never released is exactly the
    stuck-button state, and reporting success would leave the caller believing the drop
    landed.

    The release batch is built BEFORE the travel loop, so the ``finally`` cannot fail
    while constructing it: ``_move_to`` normalizes against the virtual screen and can
    raise, and a release that raised on the way to being sent would leave the button
    held — the one outcome this whole discipline exists to prevent.
    """
    flags = _MOUSE_FLAGS.get(button)
    if flags is None:
        raise ComputerUseUnsupported(f"unknown mouse button {button!r}")
    down, up = flags
    path = list(points) if points else [start, end]
    # The endpoints are the caller's, not the path's: they are the two points
    # ``hwnd_owns_point`` authorized for the press and the release specifically, so a
    # path whose own ends drifted by a float epsilon must not move them.
    path[0], path[-1] = start, end

    # Built BEFORE the travel, so the ``finally`` below cannot fail while CONSTRUCTING
    # the release: ``_move_to`` normalizes against the virtual screen and raises on a
    # zero extent, and a release that raised on its way to being sent would leave the
    # button held — the one outcome the whole release discipline exists to prevent.
    release = [_move_to(*path[-1]), _mouse_event(up)]

    # The aim and the press in one batch (see _send: nothing may interleave between
    # them), then one call per remaining point, then the aimed release.
    submitted = 0
    accepted = 0
    try:
        first = [_move_to(*path[0]), _mouse_event(down)]
        submitted += len(first)
        accepted += _send(first)
        for point in path[1:]:
            submitted += 1
            accepted += _send([_move_to(*point)])
    finally:
        # Unconditional AND verified: ``_send_release`` retries the unaccepted tail
        # and RAISES rather than letting a held button pass silently. A failed release
        # raising from ``finally`` replaces whatever the travel raised, and that
        # precedence is deliberate — a held button is worse than a failed drag and is
        # the condition the operator has to act on.
        #
        # AIMED at ``end``, not a bare ``up``: a button-up record carries no coordinate,
        # so it releases wherever the cursor is when the OS processes it. The path now
        # goes out as N separate calls, so there are N yield points where a foreign
        # mouse move can land — and a release is where a DROP lands, at a point
        # ``hwnd_owns_point`` authorized.
        #
        # ``atomic`` because the two records are not independent: the move is what aims the
        # up. A tail-only retry after a partial acceptance would resend the bare ``up`` and
        # give back exactly the unaimed release this batch exists to prevent.
        _send_release(release, atomic=True)
    if accepted < submitted:
        raise ComputerUseUnsupported(
            f"the drag was only partly delivered ({accepted} of {submitted} input "
            "records accepted, likely a lower-level hook or UIPI); the button was "
            "released so the pointer is not stuck"
        )


# ── Window enumeration ──


def _exe_name(pid: int) -> str:
    """Best-effort image name for *pid*, or ``""``.

    ``PROCESS_QUERY_LIMITED_INFORMATION`` rather than a fuller right: this is the
    least privilege that answers the question, and it succeeds against processes
    a broader open would be denied on.
    """
    libs = _libraries()
    handle = libs["kernel32"].OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        written = libs["psapi"].GetModuleFileNameExW(handle, None, buf, 512)
        if not written:
            return ""
        path = buf.value or ""
        return path.replace("/", "\\").rsplit("\\", 1)[-1]
    finally:
        libs["kernel32"].CloseHandle(handle)


def window_text(hwnd: int) -> str:
    """The window's title, or ``""``."""
    libs = _libraries()
    handle = ctypes.c_void_p(int(hwnd))
    length = int(libs["user32"].GetWindowTextLengthW(handle))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    libs["user32"].GetWindowTextW(handle, buf, length + 1)
    return buf.value or ""


def window_class(hwnd: int) -> str:
    """The window's class name, or ``""``."""
    libs = _libraries()
    buf = ctypes.create_unicode_buffer(256)
    if not libs["user32"].GetClassNameW(ctypes.c_void_p(int(hwnd)), buf, 256):
        return ""
    return buf.value or ""


def window_pid(hwnd: int) -> int:
    """The pid owning *hwnd*, or 0."""
    libs = _libraries()
    pid = wintypes.DWORD()
    libs["user32"].GetWindowThreadProcessId(ctypes.c_void_p(int(hwnd)), ctypes.byref(pid))
    return int(pid.value)


def window_root(hwnd: int) -> int:
    """The top-level ancestor of *hwnd*, or *hwnd* itself."""
    libs = _libraries()
    root = libs["user32"].GetAncestor(ctypes.c_void_p(int(hwnd)), GA_ROOT)
    return int(root or int(hwnd))


def window_bounds(hwnd: int) -> "tuple[float, float, float, float] | None":
    """``(left, top, width, height)`` for *hwnd*, or ``None``.

    Top-left convention, matching every other coordinate in this package.
    """
    libs = _libraries()
    rect = RECT()
    if not libs["user32"].GetWindowRect(ctypes.c_void_p(int(hwnd)), ctypes.byref(rect)):
        return None
    return (
        float(rect.left),
        float(rect.top),
        float(rect.right - rect.left),
        float(rect.bottom - rect.top),
    )


def window_is_live(hwnd: int) -> bool:
    """Whether *hwnd* still exists and is visible."""
    libs = _libraries()
    handle = ctypes.c_void_p(int(hwnd))
    return bool(libs["user32"].IsWindow(handle)) and bool(libs["user32"].IsWindowVisible(handle))


def window_is_on_screen(hwnd: int) -> bool:
    """Whether *hwnd* is actually visible to the operator right now.

    Three states pass ``IsWindowVisible`` while being invisible, and each produces a
    window the model can read and photograph but the operator cannot see:

    * **minimized** (``IsIconic``) — the rect is parked at ``-32000, -32000``;
    * **cloaked by DWM** (``DWMWA_CLOAKED``) — a suspended packaged app, or a window
      belonging to another virtual desktop;
    * a zero-area rect, which no click can land inside.

    Fails OPEN on an unreadable cloak attribute: ``DwmGetWindowAttribute`` is absent on
    a host without DWM composition, and treating "cannot ask" as "not on screen" would
    empty the whole application list. The ordinary reads above already exclude the
    common cases, and every downstream gesture is still confined by
    ``apps_windows.hwnd_owns_point``.
    """
    libs = _libraries()
    handle = ctypes.c_void_p(int(hwnd))
    if libs["user32"].IsIconic(handle):
        return False
    cloaked = wintypes.DWORD(0)
    try:
        hr = libs["dwmapi"].DwmGetWindowAttribute(
            handle,
            DWMWA_CLOAKED,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
    except OSError:
        return True
    if hr == S_OK and cloaked.value:
        return False
    bounds = window_bounds(int(hwnd))
    if bounds is None or bounds[2] <= 0 or bounds[3] <= 0:
        return False
    return True


def window_is_minimized(hwnd: int) -> bool:
    """Whether *hwnd* is minimized, and therefore has no pixels to capture.

    A minimized window is still ``IsWindow`` and still ``IsWindowVisible`` — the
    "visible" flag means "not explicitly hidden", not "on screen" — so liveness
    cannot answer this. Its rect is also parked off-screen (measured at
    ``-32000, -32000``), so a capture succeeds and returns a uniform buffer, which
    the blank-frame gate then correctly rejects with no reason a caller can report.
    """
    libs = _libraries()
    return bool(libs["user32"].IsIconic(ctypes.c_void_p(int(hwnd))))


def window_render_scale(hwnd: int) -> float:
    """How much SMALLER *hwnd* draws itself than its DPI-aware rect. 1.0 when equal.

    **MUST be called inside :func:`dpi_awareness_scope`**, and both DPI reads have
    to happen in the same scope: ``GetDpiForMonitor`` is itself virtualized, so
    reading it unaware reports the window's DPI back and the ratio collapses to 1.0
    — the exact bug this function exists to correct, silently.

    ``PrintWindow`` asks a window to draw itself in ITS OWN coordinate space, not
    the caller's. A DPI-unaware window therefore renders at its logical size into
    whatever buffer it is given, and an aware-sized buffer is left with a black
    margin on two sides — an image that no longer maps linearly onto the window rect
    the element frames are expressed in. Measured on a 125% monitor: a WinForms
    window (``GetDpiForWindow`` 96, monitor 120) drew 620x392 into both a 620x400
    buffer and a 775x500 one, while a Chromium window (both 120) filled each buffer
    it was handed. So the ratio is the window's DPI over the monitor's, and it is
    1.0 for every per-monitor-aware application.
    """
    libs = _libraries()
    handle = ctypes.c_void_p(int(hwnd))
    window_dpi = int(libs["user32"].GetDpiForWindow(handle))
    monitor = libs["user32"].MonitorFromWindow(handle, MONITOR_DEFAULTTONEAREST)
    if not window_dpi or not monitor:
        # Either read failing means the ratio is unknown; 1.0 keeps the capture at
        # the caller's size, which is correct for an aware window and no worse than
        # the unscaled behaviour for any other.
        return 1.0
    dpi_x = wintypes.UINT(0)
    dpi_y = wintypes.UINT(0)
    hr = libs["shcore"].GetDpiForMonitor(
        ctypes.c_void_p(monitor),
        MDT_EFFECTIVE_DPI,
        ctypes.byref(dpi_x),
        ctypes.byref(dpi_y),
    )
    if hr != S_OK or not dpi_x.value:
        return 1.0
    return window_dpi / float(dpi_x.value)


def quantize_point(x: float, y: float) -> "tuple[int, int]":
    """The integer pixel a float coordinate refers to. **The single source of truth.**

    Confinement and delivery MUST agree on which pixel they mean, and they did not:
    ``WindowFromPoint`` took ``int(x)`` (truncation toward zero) while normalization
    scaled the raw float. For a negative coordinate — a monitor left of or above the
    primary — those disagree: ``int(-100.5)`` is -100 while the float scales toward
    -101. So the check authorized one pixel and the event landed on another, which at a
    window edge is a different application.

    ``floor``, not ``int``: truncation rounds toward zero, so it is asymmetric about the
    origin and only correct for positive coordinates. Flooring means "the pixel
    containing this point" on both sides.
    """
    return math.floor(x), math.floor(y)


def root_window_at_point(x: float, y: float) -> int:
    """The top-level window under the screen point, or 0.

    The confinement primitive. Runs inside :func:`dpi_awareness_scope` so the
    point is interpreted in the same space the element rects were measured in;
    without that, this resolves a different window than the one the model was
    shown.
    """
    libs = _libraries()
    with dpi_awareness_scope():
        px, py = quantize_point(x, y)
        hwnd = libs["user32"].WindowFromPoint(POINT(px, py))
        if not hwnd:
            return 0
        return int(libs["user32"].GetAncestor(ctypes.c_void_p(hwnd), GA_ROOT) or hwnd)


def window_list() -> list[WindowInfo]:
    """Every ON-SCREEN, titled top-level window, front to back.

    The pid comes from the WINDOW rather than from a process-name search, for the
    same reason the macOS driver reads it from the window list: a name search
    returns short-lived helper processes whose accessibility trees are empty.

    **``IsWindowVisible`` is not "on screen"** — it means "not explicitly hidden", so
    a minimized window (parked at -32000,-32000) and a DWM-CLOAKED one (a packaged app
    suspended in the background, or a window on another virtual desktop) both pass it.
    Measured on one desktop: 4 of 12 windows that passed the old filter were not
    visible to the operator. That is not cosmetic — ``render`` and the tool
    descriptions both say "on-screen windows", ``build_snapshot`` succeeds on a cloaked
    window and publishes an origin that OVERLAPS other applications' real pixels, and
    the capture returns a full bitmap of a window the operator cannot see. Confinement
    then fails closed correctly, but its refusal tells the model to re-read bounds that
    never change, so the retry cannot terminate.

    ``IsIconic`` plus ``DWMWA_CLOAKED`` is the Windows equivalent of the macOS list's
    ``kCGWindowListOptionOnScreenOnly``, which excludes other Spaces the same way.
    """
    libs = _libraries()
    found: list[WindowInfo] = []
    # CPython cannot raise out of a ctypes callback: it prints "Exception ignored on
    # calling ctypes callback function" to stderr and returns 0 to the caller, so an
    # unguarded failure inside ``_collect`` would drop that window AND every window
    # after it while ``EnumWindows`` still reported success. Measured: raising in
    # every second callback yielded 135 of 270 windows with no error reaching Python
    # — a short list indistinguishable from a genuinely small desktop. The failure
    # is recorded here instead and re-raised by the caller, which is what
    # ``apps_windows.list_apps`` relies on: it must never report "no applications on
    # screen" for a read it could not perform.
    failures: list[BaseException] = []

    # WINFUNCTYPE, not CFUNCTYPE: EnumWindows' callback is stdcall. The callback
    # object is held in a local for the duration of the call so it cannot be
    # collected while native code still holds the pointer.
    @ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, wintypes.LPARAM)  # type: ignore[attr-defined]
    def _collect(hwnd: Any, _param: Any) -> bool:
        try:
            handle = int(hwnd or 0)
            if not handle or not libs["user32"].IsWindowVisible(ctypes.c_void_p(handle)):
                return True
            title = window_text(handle)
            # An untitled top-level window is shell furniture, not an application
            # window: the desktop's own thumbnail helpers, the tray, and the XAML
            # islands all pass IsWindowVisible. Dropping them here mirrors how
            # ``apps_macos`` keeps only ``kCGWindowLayer == 0`` entries, and it means
            # the caller never has to know which class names are noise.
            if not title.strip():
                return True
            if not window_is_on_screen(handle):
                return True
            root = window_root(handle)
            pid = window_pid(handle)
            found.append(
                WindowInfo(
                    hwnd=handle,
                    root_hwnd=root,
                    pid=pid,
                    title=title,
                    class_name=window_class(handle),
                    exe_name=_exe_name(pid),
                    bounds=window_bounds(handle),
                )
            )
        except BaseException as exc:  # noqa: BLE001 - see the comment above
            failures.append(exc)
        # Enumeration CONTINUES after a failure: one unreadable window (an elevated
        # app under UIPI, a window dying mid-enumeration) should not decide the fate
        # of the rest. The recorded failure is what makes the partial list visible.
        return True

    with dpi_awareness_scope():
        libs["user32"].EnumWindows(_collect, 0)
    if failures:
        raise ComputerUseUnsupported(
            f"enumerating windows failed on {len(failures)} of {len(found) + len(failures)} "
            f"window(s): {failures[0]}"
        ) from failures[0]
    return found


__all__ = [
    "CACHED_WALK_PROPERTIES",
    "E_ACCESSDENIED",
    "MAX_WALK_SECS",
    "ExpandCollapseState_Collapsed",
    "ExpandCollapseState_Expanded",
    "ExpandCollapseState_LeafNode",
    "ScrollAmount_LargeDecrement",
    "ScrollAmount_LargeIncrement",
    "ScrollAmount_NoAmount",
    "WalkResult",
    "do_default_action",
    "expand_collapse_state",
    "expand_element",
    "invoke_element",
    "pattern",
    "pattern_action",
    "scroll_element",
    "select_element",
    "element_has_focus",
    "set_element_focus",
    "set_element_value",
    "toggle_element",
    "UIA_IsPasswordPropertyId",
    "VARIANT",
    "WindowInfo",
    "add_ref",
    "automation",
    "available",
    "bstr_value",
    "cached_prop",
    "create_cache_request",
    "dpi_awareness_scope",
    "element_from_hwnd",
    "is_secure_element",
    "libraries",
    "owned",
    "prop_value_ex",
    "release",
    "release_all",
    "reset_libraries",
    "release_all_clients",
    "reset_thread_state",
    "root_window_at_point",
    "variant_value",
    "walk_bounded",
    "window_bounds",
    "window_class",
    "window_is_live",
    "window_is_minimized",
    "window_list",
    "window_is_on_screen",
    "window_pid",
    "window_render_scale",
    "window_root",
    "window_text",
]
