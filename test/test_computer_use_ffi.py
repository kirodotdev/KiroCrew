"""Behavioural tests for the ctypes layer, driven by FAKE frameworks.

Exercises the REAL bodies in ``computer_use/macos_ffi.py`` — the bind pass, the
CFString lifetime discipline, the type-checked AX reads, the private-event-source
key posting, the non-variadic scroll and the in-process ImageIO encode — against
in-Python stand-ins for CoreFoundation / ApplicationServices / CoreGraphics /
ImageIO / libproc. Nothing native is loaded and no real window, app or permission
grant is involved, so every case runs on the Linux CI shard.

Harness idiom copied verbatim from ``test/test_subagent_macos_probe.py``: a
``_FakeFn`` wrapper that is callable AND tolerant of the ``.restype`` /
``.argtypes`` assignments the binder performs, installed via
``monkeypatch.setattr(ctypes, "CDLL", ...)``. Two extra steps this module needs
that the probe test does not:

* ``macos_ffi`` caches its bound handles in a module global, so the fixture also
  calls ``reset_frameworks()`` (before AND after) to force a re-bind against the
  fakes — otherwise a real Mac's already-cached handles would be used and a Linux
  runner would never bind at all;
* ``platform_compat.IS_MACOS`` is forced True, because ``_frameworks()``
  deliberately refuses to load anything off macOS.

What is asserted, and why each one is worth a test:

* **Zero CFString leaks after a 200-node walk.** A tree walk creates thousands of
  CFStrings; leaking them is a real RSS bug the session watchdog would eventually
  recycle the process over. Asserted as an exact create/release balance.
* **The messaging timeout is applied BEFORE any attribute read.** ctypes releases
  the GIL around a C call, so an unresponsive target app parks the worker thread
  with no interruption path; the timeout is the only bound.
* **``-25204`` triggers ``AXManualAccessibility`` and EXACTLY ONE retry.** Without
  the opt-in, Electron apps (Slack, VS Code, Obsidian) look permanently empty;
  without the "exactly one" bound, a permanently-broken app costs the wait on
  every poll.
* **Every synthesized event gets ``CGEventSetFlags``, including a no-modifier
  one**, is built from a PRIVATE source, and is posted with ``CGEventPostToPid``.
  Skipping the explicit zero made a live prototype type ``' I Abc'`` for ``abc``.
* **Scroll never uses the variadic multi-axis constructor.** Declaring it made
  arm64 marshal ``axis2=0`` as ``30416`` — an arbitrary sideways scroll of a live
  window.
* **Capture passes BOTH ImageIO option keys and releases all four objects even
  when ``Finalize`` returns False.** The failure branch is exactly where an early
  return leaks.
"""

from __future__ import annotations

import ctypes
import struct

import pytest

from kiro_crew import platform_compat
from kiro_crew.computer_use import macos_ffi, snapshot_macos
from kiro_crew.computer_use.types import ComputerUseUnsupported, SnapshotRequest

# Sentinel handle values. Distinct and non-zero so a truthiness bug (treating a
# valid handle as NULL) shows up as a wrong branch rather than passing by luck.
_H_STRING = 0x5100
_H_ARRAY = 0x5200
_H_DICT = 0x5300
_H_NUMBER = 0x5400
_H_BOOL = 0x5500
_H_ELEMENT = 0x5600
_H_DATA = 0x5700
_H_IMAGE = 0x5800
_H_DEST = 0x5900
_H_EVENT = 0x5A00
_H_SOURCE = 0x5B00

# CFTypeIDs the fakes report. Arbitrary but distinct: the point is that
# ``cf_is`` compares them, so a value of the wrong type must not pass.
_TID_STRING = 7
_TID_ARRAY = 19
_TID_DICT = 18
_TID_NUMBER = 22
_TID_BOOL = 21
_TID_ELEMENT = 400
# ``AXValue`` — DISTINCT from ``_TID_ELEMENT`` on purpose. Handing an AXValue to a
# call expecting an element (or the reverse) is a segfault in production, so the
# fake must be able to tell them apart or the guard against it is untested.
_TID_AX_VALUE = 401

# Dimensions the fake ImageIO encodes into its JPEG. Deliberately the shipped
# default max width and a plausible 4:3-ish height, so the "dimensions come from
# the ENCODED bytes" assertion is checking a real downscale result rather than a
# number invented for the test.
ENCODED_WIDTH = 1280
ENCODED_HEIGHT = 1008


class _FakeFn:
    """Stand-in for a ctypes function pointer.

    Callable, and tolerant of the ``.restype`` / ``.argtypes`` assignments the
    binder performs — which is the whole reason the real ``_bind`` can run
    unmodified against these fakes.
    """

    def __init__(self, fn):
        self._fn = fn
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        return self._fn(*args)


class _Heap:
    """Shared object store for the fake frameworks.

    Real CF handles are opaque addresses; the fakes hand out small ints and keep
    the Python payload here. Holding it in ONE place lets the CF fake answer
    ``CFGetTypeID`` for an object the AX fake created, which is what makes the
    type-checking paths under test meaningful.
    """

    def __init__(self) -> None:
        self.objects: dict[int, tuple[int, object]] = {}
        self.next_handle = 0x9000
        # Every CFRelease/CFRetain, so leaks are an arithmetic assertion.
        self.created: list[int] = []
        self.released: list[int] = []
        # handle -> the handle it was COPIED from. ``AXUIElementCopyAttributeValue``
        # mints a fresh reference per read (the Create Rule, modelled below), so two
        # handles can name one element — which is exactly the situation ``CFEqual``
        # exists for. Without this map the fake would answer "different" for the
        # element the caret is in, and a focus-marker test would pass against a
        # comparison that can never fire in production.
        self.origins: dict[int, int] = {}

    def new(self, type_id: int, payload: object, *, origin: int = 0) -> int:
        self.next_handle += 0x10
        handle = self.next_handle
        self.objects[handle] = (type_id, payload)
        self.created.append(handle)
        if origin:
            self.origins[handle] = origin
        return handle

    def identity(self, handle: object) -> int:
        """Resolve *handle* to the ORIGINAL handle it was copied from.

        Follows the copy chain (a copy of a copy is still the same element) with a
        bounded walk, so a cyclic ``origins`` entry — only reachable if a future
        fake staged one — cannot hang the suite.
        """
        current = _addr(handle)
        for _ in range(16):
            nxt = self.origins.get(current)
            if not nxt or nxt == current:
                return current
            current = nxt
        return current

    def type_of(self, handle: object) -> int:
        entry = self.objects.get(_addr(handle))
        return entry[0] if entry else 0

    def payload(self, handle: object) -> object:
        entry = self.objects.get(_addr(handle))
        return entry[1] if entry else None

    @property
    def live(self) -> list[int]:
        """Handles created and not yet released — a leak, if any remain."""
        remaining = list(self.created)
        for handle in self.released:
            if handle in remaining:
                remaining.remove(handle)
        return remaining


def _addr(value: object) -> int:
    """Normalize a ctypes pointer-ish argument to the int the heap is keyed by."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    inner = getattr(value, "value", None)
    return int(inner) if isinstance(inner, int) else 0


def _num(value: object) -> int:
    """Normalize a scalar argument to ``int``.

    A REAL ``CDLL`` coerces arguments through the ``argtypes`` the binder set, so
    a callee sees plain Python numbers. These fakes are called directly, so a
    ``c_uint32(8801)`` or ``c_int32(-3)`` arrives unwrapped and every assertion
    would have to know which. Unwrapping here keeps the fakes' recorded calls
    comparable to the plain ints a test writes.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    inner = getattr(value, "value", None)
    if isinstance(inner, (int, float)):
        return int(inner)
    return 0


def _real(value: object) -> float:
    """Normalize a scalar argument to ``float`` (see :func:`_num`)."""
    if isinstance(value, (int, float)):
        return float(value)
    inner = getattr(value, "value", None)
    return float(inner) if isinstance(inner, (int, float)) else 0.0


def _flag(value: object) -> bool:
    """Normalize a ``c_bool``-ish argument to ``bool`` (see :func:`_num`)."""
    if isinstance(value, bool):
        return value
    inner = getattr(value, "value", None)
    return bool(inner) if inner is not None else bool(value)


class _FakeCF:
    """CoreFoundation: strings, arrays, dicts, numbers, booleans, data."""

    def __init__(self, heap: _Heap) -> None:
        self.heap = heap
        self.calls: list[tuple[str, tuple]] = []
        self.dict_entries: dict[int, dict[int, int]] = {}

        self.CFRelease = _FakeFn(self._release)
        self.CFRetain = _FakeFn(self._retain)
        self.CFGetTypeID = _FakeFn(lambda ref: heap.type_of(ref))
        # Identity, NOT handle equality — see ``_Heap.identity``. A Copy-Rule read
        # mints a fresh handle for an element that already has one, so comparing
        # addresses would answer "different" for the very case ``CFEqual`` exists
        # to resolve.
        self.CFEqual = _FakeFn(lambda a, b: bool(_addr(a)) and heap.identity(a) == heap.identity(b))
        self.CFStringCreateWithCString = _FakeFn(self._string_create)
        self.CFStringGetLength = _FakeFn(self._string_length)
        self.CFStringGetCString = _FakeFn(self._string_get)
        self.CFStringGetTypeID = _FakeFn(lambda: _TID_STRING)
        self.CFArrayGetTypeID = _FakeFn(lambda: _TID_ARRAY)
        self.CFArrayGetCount = _FakeFn(self._array_count)
        self.CFArrayGetValueAtIndex = _FakeFn(self._array_at)
        self.CFDictionaryGetTypeID = _FakeFn(lambda: _TID_DICT)
        self.CFDictionaryGetValue = _FakeFn(self._dict_get)
        self.CFDictionaryCreateMutable = _FakeFn(self._dict_create)
        self.CFDictionarySetValue = _FakeFn(self._dict_set)
        self.CFNumberGetTypeID = _FakeFn(lambda: _TID_NUMBER)
        self.CFNumberCreate = _FakeFn(self._number_create)
        self.CFNumberGetValue = _FakeFn(self._number_get)
        self.CFBooleanGetTypeID = _FakeFn(lambda: _TID_BOOL)
        self.CFBooleanGetValue = _FakeFn(lambda ref: bool(heap.payload(ref)))
        self.CFDataCreateMutable = _FakeFn(self._data_create)
        self.CFDataGetLength = _FakeFn(self._data_length)
        self.CFDataGetBytePtr = _FakeFn(self._data_bytes)
        # In-dll globals the module reads. ``kCFBooleanTrue`` is genuinely a
        # pointer; the dictionary callbacks are STRUCTS whose address is taken,
        # which is why they are real ctypes objects here.
        self.kCFBooleanTrue = _H_BOOL
        self.kCFTypeDictionaryKeyCallBacks = ctypes.c_void_p(0)
        self.kCFTypeDictionaryValueCallBacks = ctypes.c_void_p(0)
        self._data_buffers: dict[int, bytes] = {}

    # ── strings ──

    def _string_create(self, alloc, raw, encoding):
        self.calls.append(("CFStringCreateWithCString", (raw, _num(encoding))))
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        return self.heap.new(_TID_STRING, text)

    def _string_length(self, ref):
        payload = self.heap.payload(ref)
        return len(payload) if isinstance(payload, str) else 0

    def _string_get(self, ref, buf, size, encoding):
        payload = self.heap.payload(ref)
        if not isinstance(payload, str):
            return False
        encoded = payload.encode("utf-8")
        if len(encoded) + 1 > _num(size):
            return False
        buf.value = encoded
        return True

    def _release(self, ref):
        self.heap.released.append(_addr(ref))
        self.calls.append(("CFRelease", (_addr(ref),)))
        return None

    def _retain(self, ref):
        """Record a +1 and return the SAME handle, exactly as CoreFoundation does.

        Tracked as a create so the balance arithmetic counts it: a retained child
        that is never released is as much a leak as an un-released Copy result, and
        the walk's ``release_all`` is what pays it back.
        """
        handle = _addr(ref)
        if handle:
            self.heap.created.append(handle)
            self.calls.append(("CFRetain", (handle,)))
        return handle

    # ── arrays ──

    def _array_count(self, ref):
        payload = self.heap.payload(ref)
        return len(payload) if isinstance(payload, (list, tuple)) else 0

    def _array_at(self, ref, position):
        payload = self.heap.payload(ref)
        position = _num(position)
        self.calls.append(("CFArrayGetValueAtIndex", (_addr(ref), position)))
        if not isinstance(payload, (list, tuple)):
            return 0
        if position < 0 or position >= len(payload):
            # The real API raises an UNCATCHABLE ObjC NSRangeException here (a
            # process abort, verified EXIT=134). Raising a Python error is the
            # closest a fake can get, and makes an out-of-range read a loud test
            # failure instead of a silent zero.
            raise AssertionError(
                f"CFArrayGetValueAtIndex({position}) past end of {len(payload)} — "
                "the real API aborts the process here"
            )
        return payload[position]

    # ── dictionaries ──

    def _dict_create(self, alloc, capacity, key_cb, value_cb):
        handle = self.heap.new(_TID_DICT, {})
        self.dict_entries[handle] = {}
        self.calls.append(("CFDictionaryCreateMutable", (key_cb, value_cb)))
        return handle

    def _dict_set(self, ref, key, value):
        self.dict_entries.setdefault(_addr(ref), {})[_addr(key)] = _addr(value)
        self.calls.append(("CFDictionarySetValue", (_addr(ref), _addr(key), _addr(value))))
        return None

    def _dict_get(self, ref, key):
        payload = self.heap.payload(ref)
        if isinstance(payload, dict):
            key_text = self.heap.payload(key)
            return payload.get(key_text, 0)
        return self.dict_entries.get(_addr(ref), {}).get(_addr(key), 0)

    # ── numbers ──

    def _number_create(self, alloc, number_type, value_ref):
        obj = getattr(value_ref, "_obj", None)
        value = getattr(obj, "value", None)
        handle = self.heap.new(_TID_NUMBER, value)
        self.calls.append(("CFNumberCreate", (_num(number_type), value)))
        return handle

    def _number_get(self, ref, number_type, out_ref):
        payload = self.heap.payload(ref)
        if not isinstance(payload, (int, float)):
            return False
        obj = getattr(out_ref, "_obj", None)
        if obj is None:
            return False
        obj.value = int(payload)
        return True

    # ── data ──

    def _data_create(self, alloc, capacity):
        handle = self.heap.new(0, b"")
        self._data_buffers[handle] = b""
        return handle

    def _data_length(self, ref):
        return len(self._data_buffers.get(_addr(ref), b""))

    def _data_bytes(self, ref):
        raw = self._data_buffers.get(_addr(ref), b"")
        buf = ctypes.create_string_buffer(raw, len(raw))
        # ``string_at`` needs a real address; keep the buffer alive on self so it
        # is not collected between the return and the read.
        self._keepalive = buf
        return ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))


class _FakeAX:
    """ApplicationServices (Accessibility).

    ``attrs`` maps ``(element_handle, attribute_name)`` to either a value handle or
    an int AX error code, which is what lets a test stage ``-25204`` on the first
    read and a populated tree on the second.
    """

    def __init__(self, heap: _Heap) -> None:
        self.heap = heap
        self.calls: list[tuple[str, tuple]] = []
        self.attrs: dict[tuple[int, str], object] = {}
        self.actions: dict[int, object] = {}
        self.perform_results: dict[tuple[int, str], int] = {}
        self.set_results: dict[tuple[int, str], int] = {}
        self.trusted = True

        self.AXIsProcessTrusted = _FakeFn(lambda: self.trusted)
        self.AXUIElementGetTypeID = _FakeFn(lambda: _TID_ELEMENT)
        self.AXUIElementCreateApplication = _FakeFn(self._create_app)
        self.AXUIElementCopyAttributeValue = _FakeFn(self._copy_attr)
        self.AXUIElementSetAttributeValue = _FakeFn(self._set_attr)
        self.AXUIElementCopyActionNames = _FakeFn(self._copy_actions)
        self.AXUIElementPerformAction = _FakeFn(self._perform)
        self.AXUIElementSetMessagingTimeout = _FakeFn(self._set_timeout)
        self.AXValueGetTypeID = _FakeFn(lambda: _TID_AX_VALUE)
        self.AXValueGetType = _FakeFn(self._value_type)
        self.AXValueGetValue = _FakeFn(self._value_get)
        self.AXUIElementIsAttributeSettable = _FakeFn(self._is_settable)
        #: ``(element_handle, attribute_name)`` -> settable. Absent means NOT
        #: settable, which mirrors the production fail-closed default.
        self.settable: dict[tuple[int, str], bool] = {}
        #: ``(element_handle, attribute_name)`` -> the AX error
        #: ``AXUIElementIsAttributeSettable`` should return, for the error path.
        self.settable_errors: dict[tuple[int, str], int] = {}

    def ax_value(self, kind: int, *fields: float) -> int:
        """Stage an ``AXValue`` box of *kind* holding *fields*.

        A helper rather than raw ``heap.new`` calls at each test, because getting
        the kind and the field count to agree is precisely what
        ``macos_ffi._unbox_ax_value``'s type check defends against — a test that
        staged them inconsistently by hand would be testing its own typo.
        """
        return self.heap.new(_TID_AX_VALUE, (kind, tuple(float(f) for f in fields)))

    def _value_type(self, ref):
        payload = self.heap.payload(ref)
        return payload[0] if isinstance(payload, tuple) else 0

    def _value_get(self, ref, want_type, out_ref):
        """Unbox into *out_ref*, refusing on a type mismatch as the real API does."""
        payload = self.heap.payload(ref)
        if not isinstance(payload, tuple):
            return False
        kind, fields = payload
        if kind != _num(want_type):
            return False
        obj = getattr(out_ref, "_obj", None)
        if obj is None:
            return False
        # Assign by declared field order, so a CGPoint fills (x, y) and a CGSize
        # fills (width, height) — the transposition the production type check
        # exists to prevent would show up here as a wrong-field assertion.
        names = [name for name, _ in type(obj)._fields_]
        if len(names) != len(fields):
            return False
        for name, value in zip(names, fields):
            setattr(obj, name, value)
        return True

    def _is_settable(self, elem, key, out_ref):
        name = self.heap.payload(key)
        self.calls.append(("AXUIElementIsAttributeSettable", (self._id(elem), name)))
        err = self.settable_errors.get((self._id(elem), name), 0)
        if err:
            return err
        obj = getattr(out_ref, "_obj", None)
        if obj is not None:
            obj.value = self.settable.get((self._id(elem), name), False)
        return 0

    def _create_app(self, pid):
        self.calls.append(("AXUIElementCreateApplication", (_num(pid),)))
        return _H_ELEMENT

    def _set_timeout(self, elem, seconds):
        self.calls.append(("AXUIElementSetMessagingTimeout", (_addr(elem), _real(seconds))))
        return 0

    def _id(self, elem) -> int:
        """Resolve *elem* to the element it NAMES (see ``_Heap.identity``).

        Every handle-keyed lookup below goes through this, because a Copy-Rule read
        hands back a fresh reference to an element that already has staged
        attributes: keying on the raw address would make those attributes invisible
        through the copy, so a production path that legitimately reads an attribute
        off ``AXFocusedUIElement`` (or off an ``AXParent``) would see nothing here
        and the test would "pass" against a code path that never ran.
        """
        return self.heap.identity(elem)

    def _copy_attr(self, elem, key, out_ref):
        """Honour the CREATE RULE: every successful read returns a FRESH +1 ref.

        The symbol contains "Copy", so the real API hands back a reference the
        caller owns and must ``CFRelease``. Minting a new handle per read (rather
        than returning the staged one repeatedly) is what makes the leak-balance
        assertions meaningful: with a reused handle, N reads and ONE release would
        look balanced even though N-1 references had leaked.
        """
        name = self.heap.payload(key)
        self.calls.append(("AXUIElementCopyAttributeValue", (self._id(elem), name)))
        staged = self.attrs.get((self._id(elem), name), macos_ffi.AX_ERR_ATTRIBUTE_UNSUPPORTED)
        if isinstance(staged, int) and staged < 0:
            return staged
        obj = getattr(out_ref, "_obj", None)
        if obj is None:
            return 0
        if isinstance(staged, int) and staged in self.heap.objects:
            type_id, payload = self.heap.objects[staged]
            # ``origin`` records that the fresh handle NAMES the staged object, so
            # ``CFEqual`` can still recognise it — the Create Rule copies the
            # reference, not the element.
            obj.value = self.heap.new(type_id, payload, origin=staged)
        else:
            obj.value = staged if isinstance(staged, int) else _addr(staged)
        return 0

    def _set_attr(self, elem, key, value):
        name = self.heap.payload(key)
        payload = self.heap.payload(value)
        self.calls.append(("AXUIElementSetAttributeValue", (self._id(elem), name, payload)))
        return self.set_results.get((self._id(elem), name or ""), 0)

    def _copy_actions(self, elem, out_ref):
        self.calls.append(("AXUIElementCopyActionNames", (self._id(elem),)))
        staged = self.actions.get(self._id(elem))
        if staged is None:
            return macos_ffi.AX_ERR_ATTRIBUTE_UNSUPPORTED
        obj = getattr(out_ref, "_obj", None)
        if obj is not None:
            obj.value = _addr(staged)
        return 0

    def _perform(self, elem, name_ref):
        action = self.heap.payload(name_ref)
        self.calls.append(("AXUIElementPerformAction", (self._id(elem), action)))
        return self.perform_results.get((self._id(elem), action or ""), 0)


class _FakeCG:
    """CoreGraphics: window list, capture, event synthesis."""

    def __init__(self, heap: _Heap) -> None:
        self.heap = heap
        self.calls: list[tuple[str, tuple]] = []
        self.window_list_handle: object = 0
        self.image_handle: object = _H_IMAGE
        self.preflight = True

        self.CGWindowListCopyWindowInfo = _FakeFn(self._window_info)
        self.CGWindowListCreateImage = _FakeFn(self._create_image)
        self.CGImageGetWidth = _FakeFn(lambda ref: 1280)
        self.CGImageGetHeight = _FakeFn(lambda ref: 1008)
        self.CGImageRelease = _FakeFn(self._image_release)
        self.CGPreflightScreenCaptureAccess = _FakeFn(lambda: self.preflight)
        self.CGEventSourceCreate = _FakeFn(self._source_create)
        self.CGEventCreateKeyboardEvent = _FakeFn(self._key_event)
        self.CGEventKeyboardSetUnicodeString = _FakeFn(self._set_unicode)
        self.CGEventCreateScrollWheelEvent = _FakeFn(self._scroll_event)
        self.CGEventCreateMouseEvent = _FakeFn(self._mouse_event)
        self.CGEventSetIntegerValueField = _FakeFn(self._set_field)
        self.CGEventSetFlags = _FakeFn(self._set_flags)
        self.CGEventPostToPid = _FakeFn(self._post)
        self.CGEventPost = _FakeFn(self._post_global)
        self.CGWarpMouseCursorPosition = _FakeFn(self._warp)
        self.warp_result = 0
        self._event_seq = _H_EVENT

    def _window_info(self, options, window_id):
        self.calls.append(("CGWindowListCopyWindowInfo", (_num(options), _num(window_id))))
        return self.window_list_handle

    def _create_image(self, rect, options, window_id, image_options):
        self.calls.append(
            (
                "CGWindowListCreateImage",
                (
                    (rect.origin.x, rect.origin.y, rect.size.width, rect.size.height),
                    _num(options),
                    _num(window_id),
                    _num(image_options),
                ),
            )
        )
        return self.image_handle

    def _image_release(self, ref):
        self.calls.append(("CGImageRelease", (_addr(ref),)))
        return None

    def _source_create(self, state):
        self.calls.append(("CGEventSourceCreate", (_num(state),)))
        return _H_SOURCE

    def _key_event(self, source, keycode, is_down):
        self._event_seq += 0x10
        handle = self._event_seq
        self.heap.created.append(handle)
        self.calls.append(
            ("CGEventCreateKeyboardEvent", (_addr(source), _num(keycode), _flag(is_down), handle))
        )
        return handle

    def _set_unicode(self, event, count, buf):
        length = _num(count)
        units = [int(buf[i]) for i in range(length)]
        text = struct.pack(f"<{len(units)}H", *units).decode("utf-16-le", "replace")
        self.calls.append(("CGEventKeyboardSetUnicodeString", (_addr(event), length, text)))
        return None

    def _scroll_event(self, source, unit, wheel_count, delta):
        self._event_seq += 0x10
        handle = self._event_seq
        self.heap.created.append(handle)
        self.calls.append(
            (
                "CGEventCreateScrollWheelEvent",
                (_addr(source), _num(unit), _num(wheel_count), _num(delta), handle),
            )
        )
        return handle

    def _mouse_event(self, source, event_type, point, button):
        """Record the event TYPE, the ``CGPoint`` fields and the button number.

        The point is unpacked from the struct rather than stored as the object, so
        an assertion reads ``(x, y)`` — and so a regression that passed two bare
        doubles instead of the struct would fail here with an ``AttributeError``
        rather than being silently accepted.
        """
        self._event_seq += 0x10
        handle = self._event_seq
        self.heap.created.append(handle)
        self.calls.append(
            (
                "CGEventCreateMouseEvent",
                (_addr(source), _num(event_type), (point.x, point.y), _num(button), handle),
            )
        )
        return handle

    def _set_field(self, event, field, value):
        self.calls.append(("CGEventSetIntegerValueField", (_addr(event), _num(field), _num(value))))
        return None

    def _set_flags(self, event, flags):
        self.calls.append(("CGEventSetFlags", (_addr(event), _num(flags))))
        return None

    def _post(self, pid, event):
        self.calls.append(("CGEventPostToPid", (_num(pid), _addr(event))))
        return None

    def _post_global(self, tap, event):
        """The GLOBAL HID tap — only the pointer-moving path may reach this."""
        self.calls.append(("CGEventPost", (_num(tap), _addr(event))))
        return None

    def _warp(self, point):
        self.calls.append(("CGWarpMouseCursorPosition", ((point.x, point.y),)))
        return self.warp_result


class _FakeIO:
    """ImageIO: the in-process JPEG encode.

    ``finalize_ok`` is the knob for the branch that matters most — the failed
    encode, which is exactly where an early return leaks four objects.
    """

    def __init__(self, heap: _Heap, cf: _FakeCF) -> None:
        self.heap = heap
        self.cf = cf
        self.calls: list[tuple[str, tuple]] = []
        self.finalize_ok = True
        # A minimal but genuinely parseable JPEG: SOI, a baseline SOF0 declaring
        # the shipped default output size, EOI. Built from ``struct`` rather than
        # hand-typed hex so the dimensions the encode reports and the ones the
        # tests assert cannot drift apart.
        self.encoded = (
            b"\xff\xd8"
            + b"\xff\xc0\x00\x11\x08"
            + struct.pack(">HH", ENCODED_HEIGHT, ENCODED_WIDTH)
            + b"\x03\x01\x11\x00"
            + b"\xff\xd9"
        )
        self.dest_handle: object = _H_DEST

        self.CGImageDestinationCreateWithData = _FakeFn(self._create)
        self.CGImageDestinationAddImage = _FakeFn(self._add)
        self.CGImageDestinationFinalize = _FakeFn(self._finalize)
        # ImageIO option-key globals, read via ``c_void_p.in_dll``.
        self.kCGImageDestinationImageMaxPixelSize = 0xE001
        self.kCGImageDestinationLossyCompressionQuality = 0xE002
        self._data_ref = 0

    def _create(self, data, uti, count, options):
        self._data_ref = _addr(data)
        self.calls.append(
            ("CGImageDestinationCreateWithData", (_addr(data), self.heap.payload(uti), _num(count)))
        )
        return self.dest_handle

    def _add(self, dest, image, options):
        self.calls.append(
            ("CGImageDestinationAddImage", (_addr(dest), _addr(image), _addr(options)))
        )
        self._options_ref = _addr(options)
        return None

    def _finalize(self, dest):
        self.calls.append(("CGImageDestinationFinalize", (_addr(dest),)))
        if self.finalize_ok:
            self.cf._data_buffers[self._data_ref] = self.encoded
        return self.finalize_ok


class _FakeProc:
    """libproc: ``proc_pidpath``."""

    def __init__(self) -> None:
        self.paths: dict[int, str] = {}
        self.calls: list[int] = []
        self.proc_pidpath = _FakeFn(self._pidpath)

    def _pidpath(self, pid, buf, size):
        self.calls.append(_num(pid))
        path = self.paths.get(_num(pid), "")
        if not path:
            return 0
        buf.value = path.encode("utf-8")
        return len(path)


class _Fakes:
    """The five fakes plus the shared heap, as one fixture object."""

    def __init__(self) -> None:
        self.heap = _Heap()
        self.cf = _FakeCF(self.heap)
        self.ax = _FakeAX(self.heap)
        self.cg = _FakeCG(self.heap)
        self.io = _FakeIO(self.heap, self.cf)
        self.proc = _FakeProc()

    def by_name(self, path: str):
        """Route a ``CDLL(path)`` call to the matching fake."""
        lowered = (path or "").lower()
        if "corefoundation" in lowered:
            return self.cf
        if "applicationservices" in lowered:
            return self.ax
        if "coregraphics" in lowered:
            return self.cg
        if "imageio" in lowered:
            return self.io
        return self.proc

    def new_string(self, text: str) -> int:
        return self.heap.new(_TID_STRING, text)

    def new_array(self, items: list) -> int:
        return self.heap.new(_TID_ARRAY, list(items))

    def new_element(self) -> int:
        return self.heap.new(_TID_ELEMENT, None)

    def new_number(self, value: int) -> int:
        return self.heap.new(_TID_NUMBER, value)

    def new_bool(self, value: bool) -> int:
        """A CFBoolean, for the tri-state trait attributes.

        A real ``False`` matters as much as a ``True`` here: ``AXSelected=false``
        must render no trait while still being DISTINCT from the attribute being
        absent, and only a genuine CFBoolean exercises that.
        """
        return self.heap.new(_TID_BOOL, bool(value))

    def new_dict(self, mapping: dict) -> int:
        return self.heap.new(_TID_DICT, dict(mapping))

    def calls_named(self, fake, name: str) -> list[tuple]:
        return [args for (called, args) in fake.calls if called == name]


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> _Fakes:
    """Install fake frameworks and force ``macos_ffi`` to re-bind against them.

    Three steps, all required:

    1. ``IS_MACOS = True`` — ``_frameworks()`` refuses to load anything otherwise,
       which is the behaviour that keeps the Linux shard safe;
    2. ``ctypes.CDLL`` and ``find_library`` replaced, so no real dylib is opened;
    3. ``reset_frameworks()`` before AND after, because the bound handles are
       cached in a module global: without the reset a real Mac would keep serving
       its already-bound handles and none of the fakes would ever be used.
    """
    fake = _Fakes()
    monkeypatch.setattr(platform_compat, "IS_MACOS", True)
    monkeypatch.setattr(macos_ffi.platform_compat, "IS_MACOS", True, raising=False)
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: f"/fake/{name}")
    monkeypatch.setattr(ctypes, "CDLL", lambda path, *a, **k: fake.by_name(path))
    # ``c_void_p.in_dll`` reads a real symbol out of a real dylib; the fakes hold
    # plain attributes instead, so route the lookup at them. Restored by
    # monkeypatch on teardown.
    monkeypatch.setattr(
        ctypes.c_void_p,
        "in_dll",
        staticmethod(lambda lib, name: _in_dll(lib, name)),
        raising=False,
    )
    macos_ffi.reset_frameworks()
    yield fake
    macos_ffi.reset_frameworks()


def _in_dll(lib: object, name: str):
    """Fake ``c_void_p.in_dll``: read the attribute the fake lib exposes."""
    value = getattr(lib, name, None)
    if isinstance(value, ctypes.c_void_p):
        return value
    return ctypes.c_void_p(int(value or 0))


# ── framework loading ──


def test_refuses_to_load_off_macos(monkeypatch: pytest.MonkeyPatch):
    """``_frameworks()`` raises a TYPED refusal on a non-macOS host.

    This is what keeps the whole feature importable-but-inert on Linux/Windows:
    the backend seam converts ``ComputerUseUnsupported`` into a clean refusal
    rather than a traceback.
    """
    monkeypatch.setattr(platform_compat, "IS_MACOS", False)
    monkeypatch.setattr(macos_ffi.platform_compat, "IS_MACOS", False, raising=False)
    macos_ffi.reset_frameworks()
    try:
        with pytest.raises(ComputerUseUnsupported):
            macos_ffi.frameworks()
        assert macos_ffi.available() is False
    finally:
        macos_ffi.reset_frameworks()


def test_binds_every_symbol_in_one_pass(fakes: _Fakes):
    """After the first call, no symbol in the table is left un-bound.

    The single-pass design exists so a missing ``argtypes`` cannot be discovered
    at call time, deep inside a tree walk, as a segfault.
    """
    macos_ffi.frameworks()
    for lib_key, symbol, restype, argtypes in macos_ffi._FN_SPECS:
        lib = getattr(fakes, lib_key)
        fn = getattr(lib, symbol)
        assert fn.argtypes == list(argtypes), f"{symbol} argtypes not applied"
        assert fn.restype is (None if restype is macos_ffi._VOID else restype)


def test_unloadable_framework_raises_unsupported(monkeypatch: pytest.MonkeyPatch):
    """A framework that will not load disables the feature, it does not crash."""
    monkeypatch.setattr(platform_compat, "IS_MACOS", True)
    monkeypatch.setattr(macos_ffi.platform_compat, "IS_MACOS", True, raising=False)
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: None)
    macos_ffi.reset_frameworks()
    try:
        with pytest.raises(ComputerUseUnsupported):
            macos_ffi.frameworks()
    finally:
        macos_ffi.reset_frameworks()


def test_type_ids_are_cached_from_the_frameworks(fakes: _Fakes):
    """``CFTypeID``s are read once, not per attribute read.

    A tree walk performs thousands of type checks and each id is a C call.
    """
    ids = macos_ffi.type_ids()
    assert ids.string == _TID_STRING
    assert ids.array == _TID_ARRAY
    assert ids.ax_element == _TID_ELEMENT
    assert macos_ffi.type_ids() is ids


# ── CFString lifetime discipline ──


def test_cf_string_round_trip(fakes: _Fakes):
    """A CFString created from ``str`` decodes back identically, incl. non-ASCII."""
    for text in ("AXRole", "Documents — Finder", "日本語のタイトル"):
        with macos_ffi.cf_string(text) as ref:
            assert macos_ffi.cf_string_value(ref) == text


def test_cf_string_is_always_released(fakes: _Fakes):
    """The context manager releases on the normal path."""
    with macos_ffi.cf_string("AXTitle") as ref:
        handle = _addr(ref)
    assert handle in fakes.heap.released


def test_cf_string_is_released_even_when_the_body_raises(fakes: _Fakes):
    """A leak on the exception path is still a leak.

    An AX read can raise between create and use (a bad out-parameter, a type
    check), and that is precisely when a bare ``try`` without ``finally`` leaks.
    """
    with pytest.raises(RuntimeError):
        with macos_ffi.cf_string("AXValue") as ref:
            handle = _addr(ref)
            raise RuntimeError("boom")
    assert handle in fakes.heap.released


def test_two_hundred_node_walk_leaks_zero_cf_objects(fakes: _Fakes):
    """**Zero leaks after a 200-node walk.** Asserted as an exact create/release balance.

    Two independent leaks are covered by the one arithmetic:

    * the CFString created for each ATTRIBUTE NAME (``cf_string`` releases it);
    * the CFString the read RETURNS. ``AXUIElementCopyAttributeValue`` follows
      CoreFoundation's **Create Rule** — the symbol contains "Copy", so the value is
      +1 and the caller owns it. Measured before the fix: 30,000 un-released reads
      grew RSS by 9.4MB (0.0MB with the release), and a single 1,200-node walk
      performs ~5,000 reads, i.e. ~400KB of permanent growth per walk.

    Neither is cosmetic: RSS growth is what eventually makes the session watchdog
    recycle the process, and the symptom (a session that dies after a while) points
    nowhere near the cause. The fake mints a FRESH handle per read precisely so a
    reused-handle implementation could not make this balance by accident.
    """
    element = fakes.new_element()
    fakes.ax.attrs[(element, "AXRole")] = fakes.new_string("AXButton")
    fakes.ax.attrs[(element, "AXTitle")] = fakes.new_string("Save")
    fakes.ax.attrs[(element, "AXSubrole")] = fakes.new_string("")

    before_created = len(fakes.heap.created)
    before_released = len(fakes.heap.released)
    # The fixture objects staged above are legitimately alive (they stand in for
    # values the target application owns), so the assertion is on the DELTA the
    # walk itself introduced — which must be exactly zero.
    before_live = len(fakes.heap.live)
    for _ in range(200):
        macos_ffi.ax_str(element, "AXRole")
        macos_ffi.ax_str(element, "AXTitle")
        macos_ffi.ax_str(element, "AXSubrole")
    created = len(fakes.heap.created) - before_created
    released = len(fakes.heap.released) - before_released
    # 600 attribute-name strings + 600 returned values = 1,200 objects, all released.
    assert created == 1200
    assert released == created
    assert len(fakes.heap.live) == before_live, "the walk itself must leak nothing"


def test_copy_rule_value_is_released_by_every_typed_reader(fakes: _Fakes):
    """Each typed reader releases the +1 value the Copy call returned.

    Enumerated per reader rather than tested once, because the leak is per-call-site:
    a new reader added without ``ax_owned_attr`` would leak on its own hot path while
    every existing reader stayed clean.
    """
    element = fakes.new_element()
    fakes.ax.attrs[(element, "AXTitle")] = fakes.new_string("Save")
    fakes.ax.attrs[(element, "AXEnabled")] = fakes.heap.new(_TID_BOOL, True)

    for reader in (
        lambda: macos_ffi.ax_str(element, "AXTitle"),
        lambda: macos_ffi.ax_bool(element, "AXEnabled"),
        lambda: macos_ffi.ax_attr_error(element, "AXTitle"),
    ):
        before = len(fakes.heap.live)
        reader()
        assert len(fakes.heap.live) == before, f"{reader} leaked a Copy-rule reference"


def test_ax_owned_attr_releases_even_when_the_body_raises(fakes: _Fakes):
    """The context manager's release is in a ``finally``.

    A type check or a decode inside the ``with`` body can raise, and that is exactly
    when a hand-written ``try`` without ``finally`` leaks.
    """
    element = fakes.new_element()
    fakes.ax.attrs[(element, "AXTitle")] = fakes.new_string("Save")
    with pytest.raises(RuntimeError):
        with macos_ffi.ax_owned_attr(element, "AXTitle") as (value, _err):
            handle = _addr(value)
            raise RuntimeError("boom")
    assert handle in fakes.heap.released


def test_raw_ax_attr_is_documented_as_caller_owned(fakes: _Fakes):
    """The raw form deliberately does NOT release — the caller must.

    Kept for the callers that need the reference beyond one expression. Asserting
    the asymmetry keeps the two forms from being confused: a future edit that made
    ``ax_attr`` release would double-free every explicit caller.
    """
    element = fakes.new_element()
    fakes.ax.attrs[(element, "AXTitle")] = fakes.new_string("Save")
    value, err = macos_ffi.ax_attr(element, "AXTitle")
    assert err == macos_ffi.AX_ERR_SUCCESS
    assert _addr(value) not in fakes.heap.released
    # And the caller can settle the debt with the public helper.
    macos_ffi.release_all([value])
    assert _addr(value) in fakes.heap.released


def test_cf_string_value_of_null_is_empty(fakes: _Fakes):
    """A NULL reference decodes to ``""`` rather than dereferencing."""
    assert macos_ffi.cf_string_value(None) == ""
    assert macos_ffi.cf_string_value(ctypes.c_void_p(0)) == ""


# ── type-checked reads ──


def test_ax_attr_returns_the_error_and_never_dereferences(fakes: _Fakes):
    """On a non-zero AX error the out-parameter is NOT read.

    AX leaves the out-parameter untouched on failure, so reading it would
    dereference uninitialised stack memory.
    """
    element = fakes.new_element()
    fakes.ax.attrs[(element, "AXWindows")] = macos_ffi.AX_ERR_CANNOT_COMPLETE
    value, err = macos_ffi.ax_attr(element, "AXWindows")
    assert value is None
    assert err == macos_ffi.AX_ERR_CANNOT_COMPLETE


def test_ax_attr_on_a_null_element_short_circuits(fakes: _Fakes):
    """A NULL element is refused before any C call."""
    value, err = macos_ffi.ax_attr(None, "AXRole")
    assert value is None
    assert err == macos_ffi.AX_ERR_CANNOT_COMPLETE
    assert not fakes.calls_named(fakes.ax, "AXUIElementCopyAttributeValue")


def test_ax_str_returns_empty_for_a_non_string_value(fakes: _Fakes):
    """A present-but-wrong-type value yields ``""``, never a decode attempt.

    AX attributes are polymorphic — ``AXValue`` on a slider is a CFNumber — and
    handing a CFNumber to ``CFStringGetCString`` is a segfault, not a
    ``TypeError``. The type check is what stands between the two.
    """
    element = fakes.new_element()
    fakes.ax.attrs[(element, "AXValue")] = fakes.new_number(42)
    assert macos_ffi.ax_str(element, "AXValue") == ""


def test_ax_bool_falls_back_to_the_default_when_unsupported(fakes: _Fakes):
    """``AXEnabled`` is unsupported on many elements; absent means enabled.

    Rendering every window as "(disabled)" would be both wrong and noisy.
    """
    element = fakes.new_element()
    fakes.ax.attrs[(element, "AXEnabled")] = macos_ffi.AX_ERR_ATTRIBUTE_UNSUPPORTED
    assert macos_ffi.ax_bool(element, "AXEnabled", default=True) is True
    assert macos_ffi.ax_bool(element, "AXEnabled", default=False) is False


def test_ax_bool_reads_a_real_cf_boolean(fakes: _Fakes):
    element = fakes.new_element()
    fakes.ax.attrs[(element, "AXEnabled")] = fakes.heap.new(_TID_BOOL, False)
    assert macos_ffi.ax_bool(element, "AXEnabled", default=True) is False


def test_cf_array_items_reads_the_count_first_and_never_overruns(fakes: _Fakes):
    """Bounds are read from ``CFArrayGetCount``, never guessed.

    Indexing past the end raises an ObjC ``NSRangeException`` that ctypes cannot
    translate: the process aborts (verified ``EXIT=134`` on an EMPTY array). No
    caller may index an array directly, which is why this helper exists.
    """
    empty = fakes.new_array([])
    assert macos_ffi.cf_array_items(empty) == []
    three = fakes.new_array([1, 2, 3])
    assert macos_ffi.cf_array_items(three) == [1, 2, 3]
    # The fake raises if asked past the end; reaching here proves it was not.
    assert macos_ffi.cf_array_items(three, limit=2) == [1, 2]


def test_cf_array_items_rejects_a_non_array(fakes: _Fakes):
    """A CFString handed to the array reader yields ``[]``, not a bad index."""
    assert macos_ffi.cf_array_items(fakes.new_string("not an array")) == []


def test_retained_elements_filters_out_non_element_entries(fakes: _Fakes):
    """A malformed app answering ``AXWindows`` with strings must not slip through.

    Performing an action on a CFString pointer is a segfault, not a ``TypeError``,
    so every entry is type-checked against the ``AXUIElement`` type id.
    """
    element = fakes.new_element()
    real_window = fakes.new_element()
    fakes.ax.attrs[(element, "AXWindows")] = fakes.new_array(
        [real_window, fakes.new_string("impostor"), fakes.new_number(7)]
    )
    kept = macos_ffi.ax_retained_elements(element, "AXWindows")
    assert kept == [real_window]
    macos_ffi.release_all(kept)


def test_retained_elements_retains_each_child_and_releases_the_array(fakes: _Fakes):
    """**Two colliding ownership rules, both honoured.**

    The ARRAY comes from a ``Copy`` call so we own it and must release it; its
    ELEMENTS are owned by the array and die with it. A caller that keeps a child
    past the array's lifetime must therefore ``CFRetain`` it — verified live: a
    child read its ``AXRole`` back correctly after the parent array was released
    *because it had been retained*. Without the retain that is a use-after-free in
    another process's accessibility client, i.e. a crash rather than an exception.
    """
    element = fakes.new_element()
    child_a = fakes.new_element()
    child_b = fakes.new_element()
    fakes.ax.attrs[(element, "AXChildren")] = fakes.new_array([child_a, child_b])

    children = macos_ffi.ax_children(element)
    assert children == [child_a, child_b]
    # Each child was retained (+1) ...
    retained = [handle for (handle,) in fakes.calls_named(fakes.cf, "CFRetain")]
    assert retained == [child_a, child_b]
    # ... and the array itself was released, while the children were NOT.
    assert child_a not in fakes.heap.released
    assert child_b not in fakes.heap.released


def test_release_all_pays_back_every_retain(fakes: _Fakes):
    """``release_all`` is the counterpart the walk calls as it pops each node."""
    element = fakes.new_element()
    child = fakes.new_element()
    fakes.ax.attrs[(element, "AXChildren")] = fakes.new_array([child])
    children = macos_ffi.ax_children(element)
    macos_ffi.release_all(children)
    assert fakes.heap.released.count(child) == 1


def test_keep_index_walk_leaks_zero_cf_objects(fakes: _Fakes):
    """**The ``keep_index`` break path is leak-free too.** Same create/release
    arithmetic as the 200-node walk above, applied to the ONE path the walk's
    ``finally`` drain cannot cover.

    ``_walk`` reads (and therefore retains, +1 each) the addressed node's children
    BEFORE the elidable check, and on a ``keep_index`` match it breaks without ever
    pushing them onto the stack — so the drain never sees them. Before the fix every
    child CF ref of the addressed element leaked, on EVERY mutating action, because
    each action runs an addressing re-walk through this exact path
    (``resolve_element`` -> ``_walk(..., keep_index=...)``).

    The delta asserted is the walk's own, so the fixture handles staged below (which
    stand in for references the target application owns) do not confuse the balance.
    The one reference legitimately still alive afterwards is the kept element — it is
    handed to the caller as +1 by contract, and released here to prove it.
    """
    window = fakes.new_element()
    fakes.ax.attrs[(window, "AXRole")] = fakes.new_string("AXWindow")
    fakes.ax.attrs[(window, "AXTitle")] = fakes.new_string("Login")
    # The addressed node needs its OWN children: those are the refs that leaked.
    target = fakes.new_element()
    fakes.ax.attrs[(target, "AXRole")] = fakes.new_string("AXButton")
    fakes.ax.attrs[(target, "AXTitle")] = fakes.new_string("Save")
    grandkids = [fakes.new_element() for _ in range(5)]
    for kid in grandkids:
        fakes.ax.attrs[(kid, "AXRole")] = fakes.new_string("AXStaticText")
    fakes.ax.attrs[(target, "AXChildren")] = fakes.new_array(grandkids)
    # A sibling after the target, so the stack is genuinely non-empty at the break.
    sibling = fakes.new_element()
    fakes.ax.attrs[(sibling, "AXRole")] = fakes.new_string("AXButton")
    fakes.ax.attrs[(window, "AXChildren")] = fakes.new_array([target, sibling])

    req = SnapshotRequest(max_nodes=200, max_depth=32, want_image=False)
    before_live = len(fakes.heap.live)
    # Index 1 is the target (index 0 is the window itself).
    records, _, kept = snapshot_macos._walk(window, req, keep_index=1)
    assert kept, "the addressing walk returned no element"
    assert records[1].title == "Save"

    # Only the kept element (+1, the caller's by contract) is outstanding...
    assert len(fakes.heap.live) == before_live + 1
    macos_ffi.release_all([kept])
    assert len(fakes.heap.live) == before_live

    # ...and per-handle: every ``CFRetain`` of a child of the ADDRESSED element was
    # paid back. Asserted per handle as well as in aggregate because the aggregate
    # could in principle balance by over-releasing something else, which is a crash
    # rather than a leak and must not be allowed to look clean.
    for kid in grandkids:
        retains = fakes.calls_named(fakes.cf, "CFRetain").count((kid,))
        releases = fakes.calls_named(fakes.cf, "CFRelease").count((kid,))
        assert retains == 1, kid
        assert releases == retains, "a child of the addressed element leaked"


def test_release_all_skips_null_references(fakes: _Fakes):
    """A NULL in the list is not an error — ``CFRelease(NULL)`` would crash."""
    macos_ffi.release_all([None, 0, ctypes.c_void_p(0)])
    assert not fakes.calls_named(fakes.cf, "CFRelease")


def test_children_of_an_unreadable_attribute_is_empty(fakes: _Fakes):
    """A failed read yields no children and retains nothing."""
    element = fakes.new_element()
    fakes.ax.attrs[(element, "AXChildren")] = macos_ffi.AX_ERR_CANNOT_COMPLETE
    assert macos_ffi.ax_children(element) == []
    assert not fakes.calls_named(fakes.cf, "CFRetain")


def test_ax_actions_are_advisory_and_tuple_shaped(fakes: _Fakes):
    """The advertised action list is read, but it is only a hint.

    A real ``AXScrollArea`` advertised ``AXScrollDownByPage`` and then returned
    ``-25205`` when asked to perform it (see the scroll fallback tests).
    """
    element = fakes.new_element()
    fakes.ax.actions[element] = fakes.new_array(
        [fakes.new_string("AXPress"), fakes.new_string("AXShowMenu")]
    )
    assert macos_ffi.ax_actions(element) == ("AXPress", "AXShowMenu")


def test_ax_actions_of_an_element_without_any_is_empty(fakes: _Fakes):
    assert macos_ffi.ax_actions(fakes.new_element()) == ()
    assert macos_ffi.ax_actions(None) == ()


# ── the messaging timeout, and the Electron opt-in ──


def test_messaging_timeout_is_applied_before_any_attribute_read(fakes: _Fakes):
    """The timeout must be set immediately after creating the app element.

    ctypes releases the GIL around the C call, so an unresponsive target app
    parks the calling worker thread with **no interruption path** — not even a
    signal handler runs. The timeout is the only bound, and it only bounds calls
    made after it is applied, so ordering is the whole assertion.
    """
    element = macos_ffi.ax_app_element(637)
    assert _addr(element) == _H_ELEMENT
    names = [name for (name, _args) in fakes.ax.calls]
    assert names[0] == "AXUIElementCreateApplication"
    assert names[1] == "AXUIElementSetMessagingTimeout"
    assert "AXUIElementCopyAttributeValue" not in names

    # And the value reaches the C call as the configured float.
    ((_elem, seconds),) = fakes.calls_named(fakes.ax, "AXUIElementSetMessagingTimeout")
    assert seconds == pytest.approx(macos_ffi.AX_MESSAGING_TIMEOUT_SECS)


def test_timeout_is_skipped_for_a_null_element(fakes: _Fakes, monkeypatch: pytest.MonkeyPatch):
    """A failed element creation must not be followed by a call on NULL."""
    monkeypatch.setattr(fakes.ax, "AXUIElementCreateApplication", _FakeFn(lambda pid: 0))
    macos_ffi.frameworks()
    fakes.ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int32]
    element = macos_ffi.ax_app_element(1)
    assert not element
    assert not fakes.calls_named(fakes.ax, "AXUIElementSetMessagingTimeout")


def test_manual_accessibility_sets_the_boolean_true(fakes: _Fakes):
    """The Electron opt-in writes ``kCFBooleanTrue``, not a string or an int.

    Chrome went from a hard ``-25204`` on every attribute to 1,431 nodes once this
    was set; Obsidian from a 13-node stub to 574 nodes.
    """
    element = fakes.new_element()
    assert macos_ffi.ax_set_true(element, macos_ffi.AX_MANUAL_ACCESSIBILITY) == 0
    sets = fakes.calls_named(fakes.ax, "AXUIElementSetAttributeValue")
    assert (element, macos_ffi.AX_MANUAL_ACCESSIBILITY, None) in [
        (elem, name, payload) for (elem, name, payload) in sets
    ]


def test_cannot_complete_triggers_the_opt_in_and_exactly_one_retry(fakes: _Fakes):
    """``-25204`` -> set ``AXManualAccessibility`` -> retry ONCE.

    Both halves matter. Without the opt-in, every Electron app (Slack, VS Code,
    Obsidian, KiroCrew's own desktop app) looks permanently empty. Without the
    "exactly one retry" bound, a genuinely broken app costs the full poll wait on
    every single call.

    Driven here as the sequence the walker performs, so the assertion is about
    call ORDER rather than about a private helper's name.
    """
    app_elem = macos_ffi.ax_app_element(4242)
    handle = _addr(app_elem)
    fakes.ax.attrs[(handle, "AXWindows")] = macos_ffi.AX_ERR_CANNOT_COMPLETE

    # The diagnostic read distinguishes "-25204, retry after the opt-in" from a
    # genuinely absent attribute — and does so WITHOUT leaking the value.
    present, err = macos_ffi.ax_attr_error(app_elem, "AXWindows")
    assert present is False
    assert err == macos_ffi.AX_ERR_CANNOT_COMPLETE
    assert macos_ffi.ax_retained_elements(app_elem, "AXWindows") == []

    # The opt-in, then a single re-read.
    assert macos_ffi.ax_set_true(app_elem, macos_ffi.AX_MANUAL_ACCESSIBILITY) == 0
    real_window = fakes.new_element()
    fakes.ax.attrs[(handle, "AXWindows")] = fakes.new_array([real_window])
    windows = macos_ffi.ax_retained_elements(app_elem, "AXWindows")
    assert windows == [real_window]
    macos_ffi.release_all(windows)

    reads = [
        args
        for args in fakes.calls_named(fakes.ax, "AXUIElementCopyAttributeValue")
        if args[1] == "AXWindows"
    ]
    # The diagnostic probe, the pre-opt-in read, and exactly ONE re-read after it.
    # The bound is what matters: without it a permanently-broken app would pay the
    # full ELECTRON_OPT_IN_WAIT_SECS on every poll of every call.
    assert len(reads) == 3, "exactly one retry after the opt-in"
    opt_ins = [
        args
        for args in fakes.calls_named(fakes.ax, "AXUIElementSetAttributeValue")
        if args[1] == macos_ffi.AX_MANUAL_ACCESSIBILITY
    ]
    assert len(opt_ins) == 1


def test_ax_perform_returns_the_raw_error_code(fakes: _Fakes):
    """The RAW code is returned, because callers branch on its value.

    ``-25205`` means "advertised but unsupported — use the fallback"; anything
    else is a genuine failure whose number belongs in the message so a support
    thread is diagnosable.
    """
    element = fakes.new_element()
    fakes.ax.perform_results[(element, "AXScrollDownByPage")] = macos_ffi.AX_ERR_ACTION_UNSUPPORTED
    assert macos_ffi.ax_perform(element, "AXPress") == 0
    assert (
        macos_ffi.ax_perform(element, "AXScrollDownByPage") == macos_ffi.AX_ERR_ACTION_UNSUPPORTED
    )
    assert macos_ffi.ax_perform(None, "AXPress") == macos_ffi.AX_ERR_CANNOT_COMPLETE


def test_ax_set_str_passes_the_value_through(fakes: _Fakes):
    """``set_value``'s primitive writes the string it was given."""
    element = fakes.new_element()
    assert macos_ffi.ax_set_str(element, "AXValue", "quarterly report") == 0
    sets = fakes.calls_named(fakes.ax, "AXUIElementSetAttributeValue")
    assert (element, "AXValue", "quarterly report") in sets


# ── window list ──


def test_window_list_parses_entries_and_releases_the_array(fakes: _Fakes):
    """The window list is a ``Copy`` API: we own the array and must release it."""
    entry = fakes.new_dict(
        {
            macos_ffi.CG_WINDOW_NUMBER: fakes.new_number(2),
            macos_ffi.CG_WINDOW_OWNER_PID: fakes.new_number(637),
            macos_ffi.CG_WINDOW_OWNER_NAME: fakes.new_string("Google Chrome"),
            macos_ffi.CG_WINDOW_NAME: fakes.new_string("KiroCrew — GitHub"),
            macos_ffi.CG_WINDOW_LAYER: fakes.new_number(0),
        }
    )
    array = fakes.new_array([entry])
    fakes.cg.window_list_handle = array

    windows = macos_ffi.window_list()
    assert len(windows) == 1
    assert windows[0].pid == 637
    assert windows[0].window_id == 2
    assert windows[0].owner_name == "Google Chrome"
    assert windows[0].title == "KiroCrew — GitHub"
    assert windows[0].layer == 0
    assert array in fakes.heap.released


def test_window_list_uses_on_screen_and_exclude_desktop_options(fakes: _Fakes):
    """The option mask must exclude desktop elements and off-screen windows."""
    fakes.cg.window_list_handle = fakes.new_array([])
    macos_ffi.window_list()
    ((options, window_id),) = fakes.calls_named(fakes.cg, "CGWindowListCopyWindowInfo")
    assert options == (
        macos_ffi.K_CG_WINDOW_LIST_ON_SCREEN_ONLY | macos_ffi.K_CG_WINDOW_LIST_EXCLUDE_DESKTOP
    )
    assert window_id == macos_ffi.K_CG_NULL_WINDOW_ID


def test_window_list_skips_entries_missing_an_id_or_pid(fakes: _Fakes):
    """A malformed entry is skipped, not guessed at."""
    good = fakes.new_dict(
        {
            macos_ffi.CG_WINDOW_NUMBER: fakes.new_number(5),
            macos_ffi.CG_WINDOW_OWNER_PID: fakes.new_number(11),
            macos_ffi.CG_WINDOW_OWNER_NAME: fakes.new_string("Finder"),
            macos_ffi.CG_WINDOW_LAYER: fakes.new_number(0),
        }
    )
    bad = fakes.new_dict({macos_ffi.CG_WINDOW_OWNER_NAME: fakes.new_string("Nameless")})
    fakes.cg.window_list_handle = fakes.new_array([bad, good])
    assert [w.pid for w in macos_ffi.window_list()] == [11]


def test_window_list_of_null_is_empty(fakes: _Fakes):
    """A NULL window list (no capture permission) is empty, not a crash."""
    fakes.cg.window_list_handle = 0
    assert macos_ffi.window_list() == []


def test_executable_path_reads_proc_pidpath(fakes: _Fakes):
    """``proc_pidpath`` — one syscall, no ``/proc`` (macOS has none), no ``ps``."""
    fakes.proc.paths[637] = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    assert macos_ffi.executable_path(637).endswith("MacOS/Google Chrome")
    assert macos_ffi.executable_path(999) == ""


# ── key posting: the three load-bearing rules ──


def test_the_private_event_source_constant_is_the_value_apple_declares():
    """``kCGEventSourceStatePrivate`` is **-1**, asserted as a LITERAL.

    ``CGEventTypes.h``: ``{kCGEventSourceStatePrivate = -1,
    kCGEventSourceStateCombinedSessionState = 0,
    kCGEventSourceStateHIDSystemState = 1}``.

    Deliberately a literal and not ``macos_ffi.K_CG_EVENT_SOURCE_STATE_PRIVATE``:
    the sibling test below compares the recorded call against that same symbol, so
    it pins the constant against itself and passed happily while the value was
    ``1`` — i.e. ``kCGEventSourceStateHIDSystemState``, the shared table whose live
    modifier state produced the measured ``abc`` -> ``' I Abc'`` bug. Only an
    independent literal can catch that class of error.
    """
    assert macos_ffi.K_CG_EVENT_SOURCE_STATE_PRIVATE == -1


def test_post_key_uses_a_private_event_source(fakes: _Fakes):
    """``kCGEventSourceStatePrivate``, never NULL.

    An event from the default (HID) source inherits the user's LIVE modifier
    state: a live prototype asked to type ``abc`` produced ``' I Abc'``.
    """
    macos_ffi.post_key(637, 0, 0)
    sources = fakes.calls_named(fakes.cg, "CGEventSourceCreate")
    assert sources == [(macos_ffi.K_CG_EVENT_SOURCE_STATE_PRIVATE,)]
    for _src, _keycode, _down, _handle in fakes.calls_named(fakes.cg, "CGEventCreateKeyboardEvent"):
        assert _src == _H_SOURCE, "the event source must never be NULL"


def test_post_key_sets_flags_on_every_event_including_a_no_modifier_one(fakes: _Fakes):
    """``CGEventSetFlags`` runs on EVERY event, **even when flags is 0**.

    The explicit zero is what clears the inherited modifier state. Skipping it
    "because there are no modifiers" is precisely how the ``' I Abc'`` bug comes
    back, so a no-modifier post is the case asserted here.
    """
    macos_ffi.post_key(637, 0, 0)
    events = [
        handle for (_s, _k, _d, handle) in fakes.calls_named(fakes.cg, "CGEventCreateKeyboardEvent")
    ]
    flagged = fakes.calls_named(fakes.cg, "CGEventSetFlags")
    # One key-down and one key-up, each flagged exactly once, each with 0.
    assert len(events) == 2
    assert [handle for (handle, _flags) in flagged] == events
    assert {flags for (_handle, flags) in flagged} == {0}


def test_post_key_applies_a_modifier_mask_verbatim(fakes: _Fakes):
    """A requested mask reaches every event unchanged (no OR with live state)."""
    from kiro_crew.computer_use import keymap

    keycode, flags = keymap.parse_key("cmd+shift+a")
    macos_ffi.post_key(637, keycode, flags)
    assert {mask for (_handle, mask) in fakes.calls_named(fakes.cg, "CGEventSetFlags")} == {flags}
    assert flags == keymap.FLAG_COMMAND | keymap.FLAG_SHIFT


def test_post_key_delivers_a_down_up_pair_to_the_target_pid(fakes: _Fakes):
    """One key-down, one key-up, both ``CGEventPostToPid`` to the SAME pid.

    ``CGEventPost`` (the global tap) would land the keystroke in whatever window
    the user is actually typing in — the reason the plain symbol appears nowhere
    in the module.
    """
    macos_ffi.post_key(942, 8, 0)
    downs = [
        down for (_s, _k, down, _h) in fakes.calls_named(fakes.cg, "CGEventCreateKeyboardEvent")
    ]
    assert downs == [True, False]
    posts = fakes.calls_named(fakes.cg, "CGEventPostToPid")
    assert len(posts) == 2
    assert {pid for (pid, _event) in posts} == {942}


def test_post_key_releases_every_event(fakes: _Fakes):
    """Each synthesized event is ``CFRelease``d — a keystroke loop must not leak."""
    macos_ffi.post_key(637, 0, 0)
    events = {
        handle for (_s, _k, _d, handle) in fakes.calls_named(fakes.cg, "CGEventCreateKeyboardEvent")
    }
    assert events.issubset(set(fakes.heap.released))


def test_event_source_is_created_once_and_cached(fakes: _Fakes):
    """One private source per process, not one per keystroke."""
    macos_ffi.post_key(637, 0, 0)
    macos_ffi.post_key(637, 11, 0)
    macos_ffi.post_key(637, 8, 0)
    assert len(fakes.calls_named(fakes.cg, "CGEventSourceCreate")) == 1


def test_reset_frameworks_releases_the_cached_event_source(fakes: _Fakes):
    """The swap seam must not leak the event source it created."""
    macos_ffi.post_key(637, 0, 0)
    source = macos_ffi.event_source()
    macos_ffi.reset_frameworks()
    assert _addr(source) in fakes.heap.released


def test_post_text_sends_unicode_and_still_zeroes_the_flags(fakes: _Fakes):
    """Typing text uses the unicode payload but keeps the modifier hygiene.

    The unicode string replaces the *keystroke*, not the explicit flag zeroing —
    an inherited Shift would still uppercase a unicode-delivered character.
    """
    macos_ffi.post_text(637, "aA$")
    typed = [
        text
        for (_e, _count, text) in fakes.calls_named(fakes.cg, "CGEventKeyboardSetUnicodeString")
    ]
    # One down and one up event per character carry the same payload.
    assert typed == ["a", "a", "A", "A", "$", "$"]
    assert {flags for (_handle, flags) in fakes.calls_named(fakes.cg, "CGEventSetFlags")} == {0}
    assert {pid for (pid, _event) in fakes.calls_named(fakes.cg, "CGEventPostToPid")} == {637}


def test_post_text_delivers_an_astral_character_as_one_event(fakes: _Fakes):
    """A surrogate pair goes out in ONE event, not as two lone surrogates."""
    macos_ffi.post_text(637, "\U0001f600")
    counts = [
        count
        for (_e, count, _text) in fakes.calls_named(fakes.cg, "CGEventKeyboardSetUnicodeString")
    ]
    assert counts == [2, 2]


def test_post_text_types_NOTHING_when_a_character_cannot_be_encoded(fakes: _Fakes):
    """All-or-nothing: an unencodable character must not leave a partial string.

    ``str`` permits a lone surrogate and UTF-16 cannot represent one, so encoding
    inside the posting loop raised ``UnicodeEncodeError`` *after* the preceding
    characters had already been typed into a LIVE application. The caller saw a
    failure while the target held ``"ok"``, and a model told "typing failed" would
    retry and double the prefix. The whole string is now encoded before the first
    event is posted.
    """
    with pytest.raises(ComputerUseUnsupported):
        macos_ffi.post_text(637, "ok\ud800bad")
    assert fakes.calls_named(fakes.cg, "CGEventPostToPid") == [], (
        "characters were typed into the application before the encode failed — "
        "the caller is told nothing was sent while the target holds a partial string"
    )
    assert fakes.calls_named(fakes.cg, "CGEventKeyboardSetUnicodeString") == []


def test_post_text_still_types_the_whole_string_when_every_char_encodes(fakes: _Fakes):
    """The guard must not reject legitimate text (astral + accented + ASCII)."""
    macos_ffi.post_text(637, "a\U0001f600é")
    typed = [
        text
        for (_e, _count, text) in fakes.calls_named(fakes.cg, "CGEventKeyboardSetUnicodeString")
    ]
    assert typed == ["a", "a", "\U0001f600", "\U0001f600", "é", "é"]


# ── scroll: the variadic hazard and the AX fallback ──


def test_scroll_never_uses_the_variadic_multi_axis_constructor(fakes: _Fakes):
    """``wheelCount`` is pinned to 1 and axes are set through a NON-variadic call.

    ``CGEventCreateScrollWheelEvent`` is variadic and ctypes marshals its
    variadic tail incorrectly on arm64: the 5-argument form asked for
    ``(axis1=-3, axis2=0)`` and produced ``axis2=30416`` — an arbitrary sideways
    scroll of a live window. So both axes go through
    ``CGEventSetIntegerValueField``, which was verified exact for every input.
    """
    macos_ffi.post_scroll(637, -3, 0)
    ((source, unit, wheel_count, delta, _handle),) = fakes.calls_named(
        fakes.cg, "CGEventCreateScrollWheelEvent"
    )
    assert wheel_count == 1, "wheelCount must be 1 — the fixed, non-variadic prototype"
    assert delta == 0, "the delta must be written via SetIntegerValueField, not the constructor"
    assert unit == macos_ffi.K_CG_SCROLL_EVENT_UNIT_LINE
    assert source == _H_SOURCE

    fields = {
        field: value
        for (_e, field, value) in fakes.calls_named(fakes.cg, "CGEventSetIntegerValueField")
    }
    assert fields[macos_ffi.K_CG_SCROLL_DELTA_AXIS_1] == -3
    assert fields[macos_ffi.K_CG_SCROLL_DELTA_AXIS_2] == 0


def test_scroll_writes_the_horizontal_axis_exactly(fakes: _Fakes):
    """The horizontal axis is the one arm64 corrupted; assert it verbatim."""
    macos_ffi.post_scroll(942, 0, 5)
    fields = {
        field: value
        for (_e, field, value) in fakes.calls_named(fakes.cg, "CGEventSetIntegerValueField")
    }
    assert fields[macos_ffi.K_CG_SCROLL_DELTA_AXIS_2] == 5
    assert fields[macos_ffi.K_CG_SCROLL_DELTA_AXIS_1] == 0


def test_scroll_posts_to_the_pid_and_releases_the_event(fakes: _Fakes):
    macos_ffi.post_scroll(637, 1, 0)
    posts = fakes.calls_named(fakes.cg, "CGEventPostToPid")
    assert [pid for (pid, _event) in posts] == [637]
    ((_s, _u, _w, _d, handle),) = fakes.calls_named(fakes.cg, "CGEventCreateScrollWheelEvent")
    assert handle in fakes.heap.released


# ── mouse-event synthesis ──


def test_mouse_button_codes_refuses_an_unknown_button(fakes: _Fakes):
    """Refused, never defaulted to left.

    Silently substituting a button sends a DIFFERENT gesture into a live
    application — a right-click that becomes a left-click activates something
    instead of opening a context menu — which is the same class of defect as
    ``keymap.parse_key`` swallowing an unrecognised modifier.
    """
    with pytest.raises(ComputerUseUnsupported):
        macos_ffi.mouse_button_codes("pinky")


def test_every_button_has_a_distinct_event_triple(fakes: _Fakes):
    """There is no generic "mouse down": the event TYPE is per-button.

    Posting a right-click as ``kCGEventLeftMouseDown`` with ``button=1`` is
    delivered as a LEFT click by AppKit, so a shared type would be a silent
    wrong-gesture bug rather than an error.
    """
    left = macos_ffi.mouse_button_codes(macos_ffi.MOUSE_BUTTON_LEFT)
    right = macos_ffi.mouse_button_codes(macos_ffi.MOUSE_BUTTON_RIGHT)
    middle = macos_ffi.mouse_button_codes(macos_ffi.MOUSE_BUTTON_MIDDLE)
    assert len({left, right, middle}) == 3
    assert left[1] == macos_ffi.K_CG_EVENT_LEFT_MOUSE_DOWN
    assert right[1] == macos_ffi.K_CG_EVENT_RIGHT_MOUSE_DOWN
    assert middle[1] == macos_ffi.K_CG_EVENT_OTHER_MOUSE_DOWN


def test_app_post_click_carries_a_real_cgpoint_struct(fakes: _Fakes):
    """The point must reach the constructor as a ``CGPoint``, not two doubles.

    Two bare doubles have the same total size but a different ABI layout under the
    AArch64 convention, so the BUTTON argument would land in the wrong register and
    the event would be created for an arbitrary button. The fake unpacks
    ``point.x``/``point.y``, so a regression fails here with an ``AttributeError``.
    """
    macos_ffi.post_mouse_click(942, 640.5, 480.25)
    events = fakes.calls_named(fakes.cg, "CGEventCreateMouseEvent")
    assert events
    assert all(args[2] == (640.5, 480.25) for args in events)


def test_app_post_click_uses_post_to_pid_and_never_the_global_tap(fakes: _Fakes):
    """``app_post`` is the "click a background window without stealing the mouse"
    path, so it MUST stay on the per-pid delivery."""
    macos_ffi.post_mouse_click(942, 1.0, 2.0)
    assert [pid for (pid, _e) in fakes.calls_named(fakes.cg, "CGEventPostToPid")] == [942, 942]
    assert fakes.calls_named(fakes.cg, "CGEventPost") == []
    assert fakes.calls_named(fakes.cg, "CGWarpMouseCursorPosition") == []


def test_app_post_click_zeroes_the_flags_on_every_event(fakes: _Fakes):
    """Same hazard as ``post_key``: the private source is only half the fix, and the
    explicit zero is what clears the user's LIVE modifier state."""
    macos_ffi.post_mouse_click(942, 1.0, 2.0)
    flags = fakes.calls_named(fakes.cg, "CGEventSetFlags")
    assert len(flags) == len(fakes.calls_named(fakes.cg, "CGEventCreateMouseEvent"))
    assert all(mask == 0 for (_event, mask) in flags)


def test_multi_click_writes_an_increasing_click_state(fakes: _Fakes):
    """A triple click is three pairs whose click STATE rises 1, 2, 3.

    Not three independent pairs with no state: AppKit reads ``NSEvent.clickCount``
    from ``kCGMouseEventClickState``, so a repetition expressed only by posting more
    events is delivered as three single clicks.
    """
    macos_ffi.post_mouse_click(942, 1.0, 2.0, count=3)
    states = [
        value
        for (_event, field, value) in fakes.calls_named(fakes.cg, "CGEventSetIntegerValueField")
        if field == macos_ffi.K_CG_MOUSE_EVENT_CLICK_STATE
    ]
    assert states == [1, 1, 2, 2, 3, 3]


def test_click_releases_every_event(fakes: _Fakes):
    """A leaked CGEvent per click is a real RSS bug over a long session."""
    macos_ffi.post_mouse_click(942, 1.0, 2.0, count=2)
    handles = [args[4] for args in fakes.calls_named(fakes.cg, "CGEventCreateMouseEvent")]
    assert handles
    assert all(handle in fakes.heap.released for handle in handles)


def test_drag_interpolates_between_the_endpoints(fakes: _Fakes):
    """Down, DRAG_STEPS-1 intermediate ``MouseDragged``, up.

    The intermediates are load-bearing: a bare down/up pair is not a drag to most
    apps, because the gesture is recognized from the motion (TextEdit selected
    nothing until the interpolation was added).
    """
    macos_ffi.post_mouse_drag(942, (0.0, 0.0), (100.0, 200.0))
    events = fakes.calls_named(fakes.cg, "CGEventCreateMouseEvent")
    assert len(events) == macos_ffi.DRAG_STEPS + 1
    types = [args[1] for args in events]
    assert types[0] == macos_ffi.K_CG_EVENT_LEFT_MOUSE_DOWN
    assert types[-1] == macos_ffi.K_CG_EVENT_LEFT_MOUSE_UP
    assert set(types[1:-1]) == {macos_ffi.K_CG_EVENT_LEFT_MOUSE_DRAGGED}
    points = [args[2] for args in events]
    assert points[0] == (0.0, 0.0)
    assert points[-1] == (100.0, 200.0)
    # Monotonic along both axes, so the path is the straight line the caller asked
    # for rather than an arbitrary walk.
    assert points == sorted(points)


def test_drag_stays_on_the_per_pid_delivery(fakes: _Fakes):
    macos_ffi.post_mouse_drag(942, (0.0, 0.0), (5.0, 5.0))
    assert fakes.calls_named(fakes.cg, "CGEventPostToPid")
    assert fakes.calls_named(fakes.cg, "CGEventPost") == []
    assert fakes.calls_named(fakes.cg, "CGWarpMouseCursorPosition") == []


def test_global_click_warps_first_then_posts_to_the_hid_tap(fakes: _Fakes):
    """Order is load-bearing: a global event is delivered to whatever sits UNDER the
    cursor, so posting before the warp clicks wherever the operator left it."""
    macos_ffi.post_mouse_global(300.0, 400.0)
    names = [name for (name, _args) in fakes.cg.calls]
    assert names.index("CGWarpMouseCursorPosition") < names.index("CGEventPost")
    assert fakes.calls_named(fakes.cg, "CGWarpMouseCursorPosition") == [((300.0, 400.0),)]
    taps = [tap for (tap, _event) in fakes.calls_named(fakes.cg, "CGEventPost")]
    assert taps == [macos_ffi.K_CG_HID_EVENT_TAP, macos_ffi.K_CG_HID_EVENT_TAP]
    # And never the per-pid form: this path is simulating a physical mouse, which
    # has no pid.
    assert fakes.calls_named(fakes.cg, "CGEventPostToPid") == []


def test_a_refused_warp_still_posts_the_click(fakes: _Fakes):
    """The return code is advisory. A coherent click at the unchanged cursor is
    recoverable; refusing the whole action on an advisory code is worse."""
    fakes.cg.warp_result = 1
    macos_ffi.post_mouse_global(1.0, 1.0)
    assert fakes.calls_named(fakes.cg, "CGEventPost")


def test_global_drag_warps_before_every_single_event(fakes: _Fakes):
    """Warping once would leave the OS delivering the intermediate events to
    whatever sat under the START point, which is not a drag."""
    macos_ffi.post_mouse_drag_global((0.0, 0.0), (60.0, 60.0))
    warps = fakes.calls_named(fakes.cg, "CGWarpMouseCursorPosition")
    events = fakes.calls_named(fakes.cg, "CGEventCreateMouseEvent")
    assert len(warps) == len(events) == macos_ffi.DRAG_STEPS + 1
    # Each warp precedes its own event's creation.
    assert [w[0] for w in warps] == [e[2] for e in events]


def test_global_paths_release_every_event_too(fakes: _Fakes):
    macos_ffi.post_mouse_global(1.0, 1.0, count=2)
    macos_ffi.post_mouse_drag_global((0.0, 0.0), (3.0, 3.0))
    handles = [args[4] for args in fakes.calls_named(fakes.cg, "CGEventCreateMouseEvent")]
    assert handles
    assert all(handle in fakes.heap.released for handle in handles)


def test_scroll_falls_back_when_the_advertised_ax_action_is_unsupported(fakes: _Fakes):
    """``-25205`` on an ADVERTISED action -> synthesize a wheel event instead.

    A real ``AXScrollArea`` listed ``AXScrollDownByPage`` and then refused it, so
    the advertised action list is a hint and every caller must try, check the
    code, and fall back. Driven here as the sequence a driver performs.
    """
    element = fakes.new_element()
    fakes.ax.actions[element] = fakes.new_array([fakes.new_string("AXScrollDownByPage")])
    fakes.ax.perform_results[(element, "AXScrollDownByPage")] = macos_ffi.AX_ERR_ACTION_UNSUPPORTED

    assert "AXScrollDownByPage" in macos_ffi.ax_actions(element)
    err = macos_ffi.ax_perform(element, "AXScrollDownByPage")
    assert err == macos_ffi.AX_ERR_ACTION_UNSUPPORTED
    # The fallback path is reachable and produces a real event.
    macos_ffi.post_scroll(637, -1, 0)
    assert fakes.calls_named(fakes.cg, "CGEventCreateScrollWheelEvent")


# ── in-process capture ──


def test_capture_passes_both_image_io_option_keys(fakes: _Fakes):
    """BOTH ``MaxPixelSize`` and ``LossyCompressionQuality`` must be set.

    They are what make the capture 1280px/q55 (~8.3k tokens) instead of a
    native-resolution PNG (~41k). Dropping either silently reverts the whole
    compression decision, and the only visible symptom is a bigger context bill.
    """
    raw, width, height = macos_ffi.capture_window_jpeg(8801, max_px=1280, quality=0.55)
    assert raw
    assert (width, height) == (ENCODED_WIDTH, ENCODED_HEIGHT)

    numbers = fakes.calls_named(fakes.cf, "CFNumberCreate")
    assert (macos_ffi.K_CF_NUMBER_SINT32, 1280) in numbers
    assert any(
        kind == macos_ffi.K_CF_NUMBER_DOUBLE and value == pytest.approx(0.55)
        for (kind, value) in numbers
    )
    keys = {key for (_dict_ref, key, _value) in fakes.calls_named(fakes.cf, "CFDictionarySetValue")}
    assert fakes.io.kCGImageDestinationImageMaxPixelSize in keys
    assert fakes.io.kCGImageDestinationLossyCompressionQuality in keys


def test_capture_uses_a_null_rect_and_the_including_window_option(fakes: _Fakes):
    """A null ``CGRect`` means "this window's own bounds" — never the screen.

    Per-window capture is the mitigation for the "any window can be in frame"
    risk, so the option mask and the zero rect are both load-bearing.
    """
    macos_ffi.capture_window_jpeg(8801, max_px=1280, quality=0.55)
    ((rect, options, window_id, image_options),) = fakes.calls_named(
        fakes.cg, "CGWindowListCreateImage"
    )
    assert rect == (0.0, 0.0, 0.0, 0.0)
    assert options == macos_ffi.K_CG_WINDOW_LIST_INCLUDING_WINDOW
    assert window_id == 8801
    assert image_options == macos_ffi.K_CG_WINDOW_IMAGE_BOUNDS_IGNORE_FRAMING


def test_capture_creates_the_options_dict_with_real_callbacks(fakes: _Fakes):
    """``kCFTypeDictionaryKeyCallBacks`` is a STRUCT — its ADDRESS must be passed.

    ``c_void_p.in_dll(...).value`` reads the struct's first word (verified
    ``None``), and passing that means "no callbacks": CF would neither retain the
    CFNumbers we insert nor release them, so releasing ours leaves dangling
    entries. Non-zero addresses are the assertion.
    """
    macos_ffi.capture_window_jpeg(8801, max_px=1280, quality=0.55)
    ((key_cb, value_cb),) = fakes.calls_named(fakes.cf, "CFDictionaryCreateMutable")
    assert key_cb, "key callbacks must be a real address, not None"
    assert value_cb, "value callbacks must be a real address, not None"


def test_capture_releases_all_four_objects_on_success(fakes: _Fakes):
    """image, data, dest and options are all released on the happy path."""
    macos_ffi.capture_window_jpeg(8801, max_px=1280, quality=0.55)
    released = set(fakes.heap.released)
    dict_handle = next(
        handle for handle, (tid, _p) in fakes.heap.objects.items() if tid == _TID_DICT
    )
    assert _addr(fakes.io.dest_handle) in released
    assert dict_handle in released
    assert fakes.calls_named(fakes.cg, "CGImageRelease") == [(_H_IMAGE,)]


def test_capture_releases_everything_even_when_finalize_returns_false(fakes: _Fakes):
    """**The leak-prone branch.** A failed encode must still release all four.

    An early ``return`` on a failed ``Finalize`` is exactly where four CF/CG
    objects leak, and a failed encode is not hypothetical — a window closing
    mid-capture produces it.
    """
    fakes.io.finalize_ok = False
    raw, width, height = macos_ffi.capture_window_jpeg(8801, max_px=1280, quality=0.55)
    # Degrades to "no image": the tree is the primary channel, so a failed encode
    # must not fail the whole observation.
    assert (raw, width, height) == (b"", 0, 0)

    released = set(fakes.heap.released)
    dict_handle = next(
        handle for handle, (tid, _p) in fakes.heap.objects.items() if tid == _TID_DICT
    )
    assert _addr(fakes.io.dest_handle) in released, "destination leaked on the failure path"
    assert dict_handle in released, "options dict leaked on the failure path"
    assert fakes.calls_named(fakes.cg, "CGImageRelease") == [(_H_IMAGE,)]
    # And every CFNumber created for the options is released too.
    number_handles = {
        handle for handle, (tid, _p) in fakes.heap.objects.items() if tid == _TID_NUMBER
    }
    assert number_handles.issubset(released)


def test_capture_of_an_invalid_window_returns_no_image(fakes: _Fakes):
    """A closed or invalid window id yields a NULL image, not a crash."""
    fakes.cg.image_handle = 0
    assert macos_ffi.capture_window_jpeg(999, max_px=1280, quality=0.55) == (b"", 0, 0)
    # Nothing was allocated, so nothing needs releasing.
    assert not fakes.calls_named(fakes.cg, "CGImageRelease")


def test_capture_releases_the_image_when_the_destination_cannot_be_made(fakes: _Fakes):
    """A NULL destination must not leak the CGImage that was already created."""
    fakes.io.dest_handle = 0
    assert macos_ffi.capture_window_jpeg(8801, max_px=1280, quality=0.55) == (b"", 0, 0)
    assert fakes.calls_named(fakes.cg, "CGImageRelease") == [(_H_IMAGE,)]


# ── the JPEG dimension scan ──


def test_jpeg_dimensions_reports_the_encoded_size(fakes: _Fakes):
    """Dimensions come from the encoded bytes, not from ``CGImageGetWidth``.

    ImageIO downscales internally via ``MaxPixelSize``, so reading the INPUT
    image's width over-reports by the scale factor and the size line the model
    sees would be wrong (verified: a 1676x1320 window encoded to 1280x1008).
    """
    # A minimal SOF0: 0xFFC0, length, precision, height=1008, width=1264.
    sof = b"\xff\xd8\xff\xc0\x00\x11\x08" + struct.pack(">HH", 1008, 1264)
    assert macos_ffi.jpeg_dimensions(sof) == (1264, 1008)


def test_jpeg_dimensions_skips_intervening_segments(fakes: _Fakes):
    """APPn/DQT segments before the SOF must be walked over, not misread."""
    app0 = b"\xff\xe0\x00\x10" + b"JFIF\x00" + b"\x00" * 9
    sof = b"\xff\xc2\x00\x11\x08" + struct.pack(">HH", 604, 1280)
    assert macos_ffi.jpeg_dimensions(b"\xff\xd8" + app0 + sof) == (1280, 604)


def test_jpeg_dimensions_of_garbage_is_zero_zero(fakes: _Fakes):
    """An unparseable stream degrades cosmetically, never fatally."""
    assert macos_ffi.jpeg_dimensions(b"") == (0, 0)
    assert macos_ffi.jpeg_dimensions(b"not a jpeg") == (0, 0)
    assert macos_ffi.jpeg_dimensions(b"\xff\xd8\xff") == (0, 0)
    # A truncated SOF: the length says more bytes follow than exist.
    assert macos_ffi.jpeg_dimensions(b"\xff\xd8\xff\xc0\x00\x11\x08\x03") == (0, 0)


def test_jpeg_dimensions_rejects_a_zero_length_segment(fakes: _Fakes):
    """A segment length below 2 would make the scan loop forever."""
    assert macos_ffi.jpeg_dimensions(b"\xff\xd8\xff\xe0\x00\x00\x00\x00") == (0, 0)


# ── advisory probes ──


def test_permission_probes_are_plain_reads(fakes: _Fakes):
    """Both probes are pure reads; neither prompts.

    They are ADVISORY only: macOS attributes a TCC grant to the RESPONSIBLE
    PARENT of the process tree, so both can read "missing" while a full-fidelity
    capture succeeds — observed live. Nothing may gate on them.
    """
    fakes.ax.trusted = False
    fakes.cg.preflight = False
    assert macos_ffi.ax_is_process_trusted() is False
    assert macos_ffi.preflight_screen_capture() is False
    fakes.ax.trusted = True
    fakes.cg.preflight = True
    assert macos_ffi.ax_is_process_trusted() is True
    assert macos_ffi.preflight_screen_capture() is True


def test_shots_dir_default_uses_the_platform_tempdir(fakes: _Fakes):
    """``tempfile.gettempdir()``, never a hardcoded ``/tmp``.

    ``/tmp`` does not exist on Windows, and macOS hands a sandboxed process a
    private per-user temp dir that a hardcoded path would miss.
    """
    import tempfile

    from kiro_crew.computer_use.types import SCREENSHOT_DIR_NAME

    path = macos_ffi.shots_dir_default()
    assert path.startswith(tempfile.gettempdir())
    assert path.endswith(SCREENSHOT_DIR_NAME)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
