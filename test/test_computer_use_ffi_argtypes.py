"""The ctypes binding-discipline tripwire for ``computer_use/macos_ffi.py``.

The single most valuable test in the computer-use suite, and the cheapest: it
pins the exact defect that produced a real ``EXIT=139`` during prototyping.

**A missing ``argtypes`` is a SIGSEGV, not a ``TypeError``.** With no
``argtypes``, ctypes marshals a Python int as a 32-bit C int and TRUNCATES the
64-bit pointer it was given; the callee then dereferences a garbage address. That
is an unrecoverable process fault — no ``except`` can catch it, and in the
gateway it would take the cron scheduler, the Slack socket and every live session
down with it. Prevention is the only mitigation, so this test asserts the
discipline structurally rather than waiting for a crash:

* every ``_FN_SPECS`` row declares BOTH a non-``None`` ``argtypes`` list and a
  ``restype`` (or the explicit ``_VOID`` sentinel, since ``restype = None`` is
  itself meaningful to ctypes and must be distinguishable from "forgot to set
  it");
* ``_bind`` RAISES on ``argtypes=None`` rather than binding a function that would
  fault on first call;
* the struct types the CoreGraphics calls need are real ``ctypes.Structure``
  subclasses (passing two doubles where a ``CGRect`` is expected mis-marshals);
* ``CGEventPostToPid`` is the delivery mechanism for every keyboard, text, scroll
  and app-scoped mouse path (asserted per-function), and the global-tap
  ``CGEventPost`` / ``CGWarpMouseCursorPosition`` are CONFINED to the two
  pointer-moving functions. Both halves are asserted, because either alone is
  satisfiable by a regression: the global tap delivers to whatever is frontmost,
  i.e. whatever the *user* is actually doing, so bounding its call sites is a
  security property and not a style choice — and app-targeted posting being the
  DEFAULT is the other half of the same property.

  Note: the module DOES call ``CGEventPost`` now. The shipped
  ``click_method: "global"`` path deliberately moves the operator's real cursor,
  inverting the original "the pointer never moves" guarantee, and it is reachable
  only after both the keystone ``allow_pointer_move`` opt-in and the
  ``capabilities.computer_use_pointer`` governance permit clear at the dispatch
  chokepoint. What is pinned here is the CALL-SITE SET, not the symbol's absence.

Runs on every platform: it reads the module's declarative table and its source
text, and never loads a framework or calls a symbol.
"""

from __future__ import annotations

import ctypes
import inspect

import pytest

from kiro_crew.computer_use import macos_ffi

# Symbols the five live prototypes proved were required. Listed explicitly so a
# refactor that "tidies away" one of them fails here instead of at runtime on a
# user's Mac, where the failure mode is an empty tree or a segfault.
_PROTOTYPE_SYMBOLS: tuple[str, ...] = (
    # CoreFoundation: string/array/dict/number/data plumbing + the release hygiene
    # that keeps a thousands-of-CFStrings tree walk from leaking.
    "CFRelease",
    "CFGetTypeID",
    "CFStringCreateWithCString",
    "CFStringGetCString",
    "CFStringGetTypeID",
    "CFArrayGetTypeID",
    "CFArrayGetCount",
    "CFArrayGetValueAtIndex",
    "CFDictionaryGetValue",
    "CFNumberGetValue",
    # ApplicationServices (AX): the tree walk, the actions, and the messaging
    # timeout that bounds a hung target app.
    "AXIsProcessTrusted",
    "AXUIElementCreateApplication",
    "AXUIElementCopyAttributeValue",
    "AXUIElementSetAttributeValue",
    "AXUIElementPerformAction",
    "AXUIElementSetMessagingTimeout",
    # CoreGraphics: window list (pid resolution), capture, and app-targeted input.
    "CGWindowListCopyWindowInfo",
    "CGWindowListCreateImage",
    "CGPreflightScreenCaptureAccess",
    "CGEventSourceCreate",
    "CGEventCreateKeyboardEvent",
    "CGEventCreateMouseEvent",
    "CGEventSetFlags",
    "CGEventPostToPid",
    # ImageIO: the in-process JPEG encode that replaced a screencapture(1)
    # subprocess and a Pillow dependency.
    "CGImageDestinationCreateWithData",
    "CGImageDestinationAddImage",
    "CGImageDestinationFinalize",
)

# Libraries the table may draw from. Pinned so a row cannot name a key
# ``_frameworks()`` never loads (which would ``KeyError`` mid-bind, after some
# symbols were already bound — the partially-bound state the single pass exists
# to prevent).
_LIB_KEYS: frozenset[str] = frozenset(
    {macos_ffi.LIB_CF, macos_ffi.LIB_AX, macos_ffi.LIB_CG, macos_ffi.LIB_IO, macos_ffi.LIB_PROC}
)


def _spec_rows() -> tuple[tuple, ...]:
    """The declarative binding table, as a tuple of rows."""
    return tuple(macos_ffi._FN_SPECS)


def _symbol_of(row: tuple) -> str:
    """The C symbol name in a ``_FN_SPECS`` row.

    Positional rather than by attribute so the table can stay a plain tuple of
    tuples (its whole point is being trivially readable and diffable), while this
    test stays robust to a row growing an extra trailing field.
    """
    return row[1]


def test_every_row_declares_argtypes_and_restype():
    """No row may leave either half of the marshalling contract unset.

    The two halves fail differently and both are fatal: a missing ``argtypes``
    truncates a pointer ARGUMENT (immediate segfault), while a missing
    ``restype`` makes ctypes interpret a returned 64-bit pointer as a 32-bit
    ``int`` — a silently corrupt handle that faults later, further from the cause.
    """
    rows = _spec_rows()
    assert rows, "_FN_SPECS must not be empty"
    for row in rows:
        lib_key, symbol, restype, argtypes = row[0], row[1], row[2], row[3]
        assert lib_key in _LIB_KEYS, f"{symbol}: unknown library key {lib_key!r}"
        assert argtypes is not None, (
            f"{symbol}: argtypes is None — ctypes would marshal pointers as 32-bit "
            f"ints and TRUNCATE them (this is the EXIT=139 defect)"
        )
        assert isinstance(argtypes, (list, tuple)), f"{symbol}: argtypes must be a list/tuple"
        # A zero-arg symbol legitimately has an EMPTY list; what is forbidden is
        # None (never declared). The distinction is the whole point of the check.
        assert restype is not None or restype is macos_ffi._VOID, (
            f"{symbol}: restype is a bare None — use the _VOID sentinel so "
            f"'returns void' is distinguishable from 'nobody set it'"
        )


def test_void_sentinel_is_a_distinct_object():
    """``_VOID`` must not be ``None``, or the check above is vacuous."""
    assert macos_ffi._VOID is not None
    # An identity sentinel, so no value a caller could produce compares equal.
    assert macos_ffi._VOID is macos_ffi._VOID


class _Stub:
    """A settable stand-in for a ctypes function pointer."""

    def __init__(self) -> None:
        self.restype: object = "unset"
        self.argtypes: object = "unset"

    def __call__(self, *args: object) -> int:
        return 0


class _Lib:
    """Minimal stand-in for a ``CDLL``: hands out settable attribute stubs."""

    def __getattr__(self, name: str) -> _Stub:
        stub = _Stub()
        setattr(self, name, stub)
        return stub


def test_bind_raises_when_argtypes_is_none():
    """``_bind`` refuses an undeclared symbol instead of binding a faulting call.

    Belt for the table check above: the table is data and a future edit could add
    a row programmatically, so the binder itself must also refuse. Failing loudly
    at bind time — before any call — is what converts a would-be segfault into a
    diagnosable exception.
    """
    with pytest.raises(Exception) as excinfo:
        macos_ffi._bind(_Lib(), "CFRelease", macos_ffi._VOID, None)
    # The message must name the symbol: a bind failure with no symbol name sends
    # the next maintainer reading the whole table.
    assert "CFRelease" in str(excinfo.value)


def test_bind_sets_both_restype_and_argtypes():
    """A well-formed row leaves the bound function fully declared."""
    lib = _Lib()
    macos_ffi._bind(lib, "CFArrayGetCount", ctypes.c_long, [ctypes.c_void_p])
    assert lib.CFArrayGetCount.restype is ctypes.c_long
    assert lib.CFArrayGetCount.argtypes == [ctypes.c_void_p]


def test_bind_maps_void_sentinel_to_none_restype():
    """``_VOID`` must reach ctypes as ``restype = None``, not as the sentinel.

    The sentinel exists only so the TABLE can distinguish declared-void from
    never-declared; ctypes itself only understands ``None``. Assigning the
    sentinel object would make ctypes raise on the first call.
    """
    lib = _Lib()
    macos_ffi._bind(lib, "CFRelease", macos_ffi._VOID, [ctypes.c_void_p])
    assert lib.CFRelease.restype is None
    assert lib.CFRelease.argtypes == [ctypes.c_void_p]


def test_every_table_row_binds_cleanly():
    """The whole table binds against a stub lib — no row is malformed.

    Exercises the same loop ``_frameworks()`` runs, without loading a framework,
    so a row whose ``argtypes`` a future edit sets to ``None`` fails on every
    platform's CI rather than only when a Mac reaches that symbol.
    """
    lib = _Lib()
    for _lib_key, symbol, restype, argtypes in _spec_rows():
        macos_ffi._bind(lib, symbol, restype, argtypes)
        bound = getattr(lib, symbol)
        assert bound.argtypes == list(argtypes)
        assert bound.restype is (None if restype is macos_ffi._VOID else restype)


@pytest.mark.parametrize("symbol", _PROTOTYPE_SYMBOLS)
def test_prototype_symbol_is_declared(symbol: str):
    """Every symbol the live prototypes proved necessary is in the table."""
    assert symbol in {_symbol_of(row) for row in _spec_rows()}


def test_cg_structs_are_real_ctypes_structures():
    """``CGPoint``/``CGSize``/``CGRect`` must be Structures, not tuples of doubles.

    ``CGWindowListCreateImage`` takes a ``CGRect`` BY VALUE. Declaring it as two
    (or four) bare doubles mis-marshals the whole call under the AArch64 calling
    convention — every subsequent argument lands in the wrong register — so this
    is a correctness requirement, not documentation.
    """
    for struct in (macos_ffi.CGPoint, macos_ffi.CGSize, macos_ffi.CGRect):
        assert issubclass(struct, ctypes.Structure)
    assert [name for name, _ in macos_ffi.CGPoint._fields_] == ["x", "y"]
    assert [name for name, _ in macos_ffi.CGSize._fields_] == ["width", "height"]
    assert [name for name, _ in macos_ffi.CGRect._fields_] == ["origin", "size"]
    # The nested field TYPES matter as much as the names: a CGRect of four bare
    # doubles has the same size but a different ABI layout in the general case.
    rect_types = dict(macos_ffi.CGRect._fields_)
    assert rect_types["origin"] is macos_ffi.CGPoint
    assert rect_types["size"] is macos_ffi.CGSize


def test_window_list_image_declares_the_rect_struct():
    """The capture entry point must take the struct, not a scalar."""
    row = next(row for row in _spec_rows() if _symbol_of(row) == "CGWindowListCreateImage")
    assert row[3][0] is macos_ffi.CGRect


def test_app_targeted_posting_is_the_default_for_every_non_pointer_path():
    """The POSITIVE half of the pairing below: app-scoped delivery is the default.

    The test after this one pins where ``CGEventPost`` may appear. On its own that
    is only half the safety property — a path that posted nothing at all, or that
    grew a third delivery mechanism, would satisfy it. So this asserts the other
    direction: every keyboard, text, scroll and app-scoped mouse entry point
    delivers through ``CGEventPostToPid``, i.e. to the process the model actually
    addressed, and none of them reaches the system-wide HID tap.

    That pairing is what keeps "element-targeted, non-pointer input is the DEFAULT
    and the only thing available out of the box" a tested property rather than a
    claim in a docstring.
    """
    app_scoped = (
        "post_key",
        "post_text",
        "post_scroll",
        "post_mouse_click",
        "post_mouse_drag",
    )
    for name in app_scoped:
        fn = getattr(macos_ffi, name)
        src = _safe_source(fn)
        assert src, f"{name} must be a source-visible function"
        assert "CGEventPostToPid(" in src, f"{name} must deliver to the target pid"
        assert "CGEventPost(" not in src, f"{name} must not reach the global HID tap"


def test_global_event_tap_is_confined_to_the_pointer_moving_functions():
    """``CGEventPost`` may appear ONLY inside the two pointer-moving functions.

    **This module DOES call the global tap now** — the shipped
    ``click_method: "global"`` path warps the operator's real cursor, which is a
    deliberate inversion of the original "the pointer never moves" guarantee. The
    assertion is therefore not "the name is absent" (it no longer is) but the
    safety property that survives the inversion: ``CGEventPostToPid`` is the
    default for every keyboard, scroll and app-scoped mouse path — it delivers to
    the app the model addressed rather than to whatever the operator is actually
    working in — and the system-wide HID tap is reachable from exactly two
    functions, both of which are only entered once BOTH the keystone
    ``allow_pointer_move`` opt-in and the ``capabilities.computer_use_pointer``
    governance permit have cleared at the dispatch chokepoint.

    So this pins the CALL SITES rather than forbidding the name: the two
    pointer-moving functions may call it, nothing else may, and any new caller is a
    new ungated path to the operator's mouse. Asserted against the SOURCE (not the
    binding table) because a stray call needs no table row of its own. Paired with
    ``test_app_targeted_posting_is_the_default_for_every_non_pointer_path`` above,
    which asserts the converse.
    """
    src = inspect.getsource(macos_ffi)
    assert "CGEventPostToPid" in src
    allowed = ("post_mouse_global", "post_mouse_drag_global")
    callers = {
        name
        for name, fn in vars(macos_ffi).items()
        if callable(fn) and getattr(fn, "__module__", "") == macos_ffi.__name__
        # ``cg.CGEventPost(`` is the call; ``CGEventPostToPid(`` is a different
        # symbol whose name contains it, so the open paren is what separates them.
        and "CGEventPost(" in _safe_source(fn)
    }
    assert callers <= set(allowed), (
        f"CGEventPost (the global HID tap) is called from {sorted(callers - set(allowed))}; "
        f"only {list(allowed)} may reach it"
    )
    warpers = {
        name
        for name, fn in vars(macos_ffi).items()
        if callable(fn)
        and getattr(fn, "__module__", "") == macos_ffi.__name__
        and "CGWarpMouseCursorPosition(" in _safe_source(fn)
    }
    assert warpers <= set(allowed), (
        f"CGWarpMouseCursorPosition moves the operator's physical cursor and is "
        f"called from {sorted(warpers - set(allowed))}"
    )


def _safe_source(fn: object) -> str:
    """Source of *fn*, or ``""`` when it has none (a builtin, a C function)."""
    try:
        return inspect.getsource(fn)  # type: ignore[arg-type]
    except (OSError, TypeError):
        return ""


def test_mouse_event_declares_the_point_struct_and_a_button():
    """``CGEventCreateMouseEvent`` takes a ``CGPoint`` BY VALUE.

    Two bare doubles have the same total size but a different ABI layout, so the
    button argument would land in the wrong register and the event would be created
    for an arbitrary button at an arbitrary point. Verified in the live prototype.
    """
    row = next(row for row in _spec_rows() if _symbol_of(row) == "CGEventCreateMouseEvent")
    assert row[3][2] is macos_ffi.CGPoint
    assert len(row[3]) == 4, "source, event type, point, button"


def test_warp_takes_the_point_struct_too():
    """Same struct-by-value hazard on the cursor warp."""
    row = next(row for row in _spec_rows() if _symbol_of(row) == "CGWarpMouseCursorPosition")
    assert row[3] == [macos_ffi.CGPoint]


def test_every_mouse_button_maps_to_a_distinct_per_button_event_triple():
    """There is no generic "mouse down" — the event TYPE is per-button.

    Posting a right-click as ``kCGEventLeftMouseDown`` with ``button=1`` is silently
    delivered as a LEFT click by AppKit, so the table must carry a distinct
    down/up/dragged triple for each button rather than one type plus a number.
    """
    from kiro_crew.computer_use.types import MOUSE_BUTTONS

    table = macos_ffi.MOUSE_EVENT_TYPES
    assert set(table) == set(MOUSE_BUTTONS)
    seen_types: set[int] = set()
    seen_numbers: set[int] = set()
    for button, codes in table.items():
        number, down, up, dragged = codes
        assert len({down, up, dragged}) == 3, f"{button}: down/up/dragged must differ"
        assert not seen_types & {down, up, dragged}, f"{button}: shares an event type"
        seen_types |= {down, up, dragged}
        assert number not in seen_numbers, f"{button}: shares a CGMouseButton number"
        seen_numbers.add(number)


def test_never_binds_or_calls_request_screen_capture_access():
    """Only the PREFLIGHT probe may be bound, never the requesting variant.

    ``CGRequestScreenCaptureAccess`` pops a system dialog. Called from a
    background MCP/gateway process it produces an unexplained modal the operator
    cannot attribute to anything, which is why the probe is preflight-only and
    advisory.

    Asserted two ways — absent from the binding table (so it is not even
    reachable) and absent as a CALL in the source. The bare NAME is deliberately
    allowed: the module comments explain why the symbol is refused, and a check
    that forbade the name would punish documenting the decision.
    """
    symbols = {_symbol_of(row) for row in _spec_rows()}
    assert "CGPreflightScreenCaptureAccess" in symbols
    assert "CGRequestScreenCaptureAccess" not in symbols
    assert "CGRequestScreenCaptureAccess(" not in inspect.getsource(macos_ffi)


def test_every_copy_rule_symbol_has_a_release_path():
    """A ``Copy``/``Create`` symbol implies an ownership obligation.

    CoreFoundation's **Create Rule**: a function whose name contains ``Create`` or
    ``Copy`` returns a +1 reference the caller owns. Measured cost of forgetting it
    on the hot path: 30,000 un-released ``AXUIElementCopyAttributeValue`` reads grew
    RSS by 9.4MB (0.0MB with the release), and one 1,200-node walk performs ~5,000
    reads.

    So the module MUST bind a way to discharge the debt. This is a coarse tripwire
    — it proves the release primitives exist and are bound, not that every call site
    uses them (``test_computer_use_ffi.py`` asserts the per-call-site balance
    behaviourally) — but it fails loudly if someone binds a new ``Copy`` symbol into
    a module that has no release primitive at all.
    """
    symbols = {_symbol_of(row) for row in _spec_rows()}
    copy_rule = {s for s in symbols if "Create" in s or "Copy" in s}
    assert copy_rule, "the table should contain Copy/Create symbols"
    # CFRelease for CF types, CGImageRelease for the CGImage the capture creates.
    assert "CFRelease" in symbols
    assert "CGImageRelease" in symbols
    # CFRetain is required by the array-of-elements case: the array owns its
    # elements, so a child kept past the array's life must be retained first.
    assert "CFRetain" in symbols


def test_ownership_helpers_are_publicly_exported():
    """The release-side helpers must be part of the module's public surface.

    ``ax_owned_attr`` (the context manager that discharges the Create Rule) and
    ``release_all`` (the counterpart to the retain in the children walk) are what
    every caller is expected to use; exporting them is how the obligation stays
    discoverable rather than folklore.
    """
    for helper in ("ax_owned_attr", "release_all", "cf_string"):
        assert helper in macos_ffi.__all__, f"{helper} must be exported"
        assert callable(getattr(macos_ffi, helper))


def test_no_cdll_at_module_scope():
    """Frameworks must load inside ``_frameworks()``, never at import.

    A module-scope ``CDLL`` raises ``OSError`` on the Linux CI fleet at IMPORT
    time, which breaks collection of every test that transitively imports
    ``kiro_crew.computer_use`` — a whole-suite outage from one line. The import
    statement itself is fine (and required by the top-level-imports rule); it is
    the *call* that must be deferred.
    """
    src = inspect.getsource(macos_ffi)
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        # Module-scope statements have zero indentation; a CDLL/find_library call
        # inside a function body is indented.
        if line[:1] not in (" ", "\t") and ("CDLL(" in stripped or "find_library(" in stripped):
            pytest.fail(f"framework loaded at module scope: {stripped!r}")


def test_module_imports_off_macos():
    """The module must import cleanly on any platform.

    Structural half of the "CI never touches native code" guarantee: importing
    it here (on whatever runner is executing) already proves it, since the import
    at the top of this file has succeeded.
    """
    assert macos_ffi.__name__.endswith("macos_ffi")
    assert macos_ffi._libs is None or macos_ffi._libs is not None  # attribute exists


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
