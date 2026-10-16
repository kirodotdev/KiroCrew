"""CoreFoundation memory and type discipline in :mod:`kiro_crew.computer_use.macos_ffi`.

:func:`cf_string`, a context manager that always releases; the CFString read; the
type-checked number, boolean and array reads (:func:`cf_array_items` reads the count first
and never indexes past it); and the CF dictionary built with the real key and value
callbacks.

:mod:`kiro_crew.computer_use.macos_ffi` stays the import path and the patch surface for
every name defined here.
"""

from __future__ import annotations

import ctypes
from contextlib import contextmanager
from ctypes import c_double, c_int32, c_void_p
from typing import Any, Iterator

from kiro_crew.computer_use.macos_ffi_bind import (
    K_CF_NUMBER_DOUBLE,
    K_CF_NUMBER_SINT32,
    K_CF_STRING_ENCODING_UTF8,
    _frameworks,
    type_ids,
)

# ── CoreFoundation helpers ──


@contextmanager
def cf_string(text: str) -> Iterator[c_void_p]:
    """Create a CFString for *text* and ALWAYS ``CFRelease`` it on exit.

    A context manager rather than a plain factory because a single tree walk
    creates thousands of these (one per attribute name per node): leaking them is
    a genuine RSS bug the session watchdog would eventually recycle the process
    over, and a bare ``try/finally`` at every call site would be forgotten once.

    Yields a NULL ``c_void_p`` when creation fails, which every caller treats as
    "attribute unavailable" — CF calls tolerate NULL, so a failed allocation
    degrades to a missing value rather than a crash.
    """
    libs = _frameworks()
    ref = c_void_p(
        libs.cf.CFStringCreateWithCString(None, text.encode("utf-8"), K_CF_STRING_ENCODING_UTF8)
    )
    try:
        yield ref
    finally:
        if ref:
            libs.cf.CFRelease(ref)


def cf_string_value(ref: Any) -> str:
    """Decode a CFString into ``str``, or ``""``.

    Sizes the buffer from ``CFStringGetLength`` * 4 + 1: the length is in UTF-16
    code units and a code unit can expand to at most 4 UTF-8 bytes (surrogate
    pairs count as two units, so this is a safe over-estimate). Undersizing makes
    ``CFStringGetCString`` fail, which would silently drop the value.

    **The type check is a crash guard, not a nicety, and it lives HERE rather than
    at each call site.** ``CFStringGetLength`` on a non-CFString raises an ObjC
    ``NSInvalidArgumentException`` (verified on darwin-arm64:
    ``-[__NSCFNumber length]: unrecognized selector`` -> ``libc++abi: terminating``,
    exit 134). ctypes cannot translate an ObjC exception into a Python one, so it
    is an UNCATCHABLE process abort — ``macos_driver._guarded`` cannot stop it and
    the whole gateway dies with every session on it. The contents of an AX array are
    arbitrary app-controlled data (``AXActionNames`` really does carry odd payloads;
    see ``_sanitize_action``), so a caller that forgets the check is a
    remote-crash primitive. Centralising it means no future reader can reintroduce
    the hazard; the redundant ``cf_is`` checks at the typed readers stay as cheap
    short-circuits.
    """
    if not ref:
        return ""
    libs = _frameworks()
    if not cf_is(ref, type_ids().string):
        return ""
    size = int(libs.cf.CFStringGetLength(ref)) * 4 + 1
    if size <= 1:
        return ""
    buf = ctypes.create_string_buffer(size)
    if not libs.cf.CFStringGetCString(ref, buf, size, K_CF_STRING_ENCODING_UTF8):
        return ""
    return buf.value.decode("utf-8", "replace")


def cf_is(ref: Any, type_id: int) -> bool:
    """True when *ref* is non-NULL and its ``CFTypeID`` equals *type_id*.

    Called before EVERY use of an AX-returned value. AX attributes are
    polymorphic (``AXValue`` can be a string, a number, an ``AXValue`` struct or
    an element) and handing a CFNumber to ``CFStringGetCString`` is a segfault,
    not a ``TypeError``.
    """
    if not ref:
        return False
    return int(_frameworks().cf.CFGetTypeID(ref)) == type_id


def cf_array_items(ref: Any, *, limit: int = 0) -> list[Any]:
    """Return a CFArray's elements as a list, or ``[]`` for a non-array.

    **The count is read first and never exceeded.** Indexing a CFArray past its
    end raises an ObjC ``NSRangeException``, which ctypes cannot translate: the
    process aborts with ``libc++abi: terminating due to uncaught exception``
    (reproduced live, ``EXIT=134``, on an empty array). Since a caller cannot
    recover from that, no caller is allowed to index an array directly.

    *limit* caps how many entries are materialised, so a pathological child list
    cannot blow memory before the walk's own node budget notices.
    """
    if not cf_is(ref, type_ids().array):
        return []
    libs = _frameworks()
    count = int(libs.cf.CFArrayGetCount(ref))
    if count <= 0:
        return []
    if limit > 0:
        count = min(count, limit)
    return [libs.cf.CFArrayGetValueAtIndex(ref, i) for i in range(count)]


def cf_number_int(ref: Any) -> "int | None":
    """Read a CFNumber as a 32-bit int, or ``None`` for a non-number."""
    if not cf_is(ref, type_ids().number):
        return None
    out = c_int32()
    if not _frameworks().cf.CFNumberGetValue(ref, K_CF_NUMBER_SINT32, ctypes.byref(out)):
        return None
    return int(out.value)


def cf_number_double(ref: Any) -> "float | None":
    """Read a CFNumber as a double, or ``None`` for a non-number.

    Separate from :func:`cf_number_int` because the window-list ``kCGWindowBounds``
    components are CGFloats: reading them through the SInt32 path would truncate a
    fractional origin (Retina windows routinely sit on half-pixel boundaries) and
    could place a rect edge a pixel away from where it really is — which matters
    when the rect decides whether a click is inside an authorized
    application's window.
    """
    if not cf_is(ref, type_ids().number):
        return None
    out = c_double()
    if not _frameworks().cf.CFNumberGetValue(ref, K_CF_NUMBER_DOUBLE, ctypes.byref(out)):
        return None
    return float(out.value)


def cf_bool_value(ref: Any) -> "bool | None":
    """Read a CFBoolean, or ``None`` for a non-boolean."""
    if not cf_is(ref, type_ids().boolean):
        return None
    return bool(_frameworks().cf.CFBooleanGetValue(ref))


def cf_true() -> c_void_p:
    """``kCFBooleanTrue``.

    Read through ``c_void_p.in_dll``: the symbol IS a pointer to the shared
    singleton, so ``.value`` is the right read here — unlike the dictionary
    callback STRUCTS, which need :func:`_cf_type_dict`'s ``addressof``.
    """
    return c_void_p.in_dll(_frameworks().cf, "kCFBooleanTrue")


def _cf_type_dict() -> c_void_p:
    """Create an empty mutable CFDictionary with the standard CFType callbacks.

    ``kCFTypeDictionaryKeyCallBacks`` is a ``CFDictionaryKeyCallBacks`` STRUCT,
    not a pointer to one. ``c_void_p.in_dll(...).value`` therefore reads the
    struct's first word — verified ``None`` — and passing that means "no
    callbacks": CF would neither retain the keys/values we insert nor release
    them, so a CFNumber we release becomes a dangling entry. ``addressof`` on the
    ``in_dll`` view yields the address of the framework's own static storage,
    which is what the API actually wants and stays valid for the process
    lifetime.
    """
    libs = _frameworks()
    key_cb = c_void_p.in_dll(libs.cf, "kCFTypeDictionaryKeyCallBacks")
    value_cb = c_void_p.in_dll(libs.cf, "kCFTypeDictionaryValueCallBacks")
    return c_void_p(
        libs.cf.CFDictionaryCreateMutable(
            None, 0, ctypes.addressof(key_cb), ctypes.addressof(value_cb)
        )
    )
