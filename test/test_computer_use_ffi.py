"""Behavioural tests for the ctypes layer, driven by FAKE frameworks.

Exercises the REAL bodies in ``computer_use/macos_ffi.py`` and its owners — the bind pass, the
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
from kiro_crew.computer_use import macos_ffi
from kiro_crew.computer_use.types import ComputerUseUnsupported

# This file replaces ctypes.CDLL, find_library or a cached libc handle for a test,
# so the shared macOS handle caches are put back after each one (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

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
        self.CFDataCreateMutable = _FakeFn(self._data_create)
        self.CFDataGetLength = _FakeFn(self._data_length)
        self.CFDataGetBytePtr = _FakeFn(self._data_bytes)
        # In-dll globals the module reads: the dictionary callbacks are STRUCTS
        # whose address is taken, which is why they are real ctypes objects here.
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
        # A double read (a scroll bar's 0.0-1.0 position) keeps its fraction, as
        # the real CFNumberGetValue does for kCFNumberDoubleType.
        obj.value = float(payload) if isinstance(obj, ctypes.c_double) else int(payload)
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

    After agent-desktop took over the accessibility tree, the only AX symbol
    ``macos_ffi`` binds is ``AXIsProcessTrusted`` (the advisory permission probe),
    so that one call plus the ``trusted`` flag it reads is all this fake carries.
    """

    def __init__(self, heap: _Heap) -> None:
        self.heap = heap
        self.calls: list[tuple[str, tuple]] = []
        self.trusted = True
        self.AXIsProcessTrusted = _FakeFn(lambda: self.trusted)


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
    assert ids.dictionary == _TID_DICT
    assert ids.number == _TID_NUMBER
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


def test_cf_string_value_of_null_is_empty(fakes: _Fakes):
    """A NULL reference decodes to ``""`` rather than dereferencing."""
    assert macos_ffi.cf_string_value(None) == ""
    assert macos_ffi.cf_string_value(ctypes.c_void_p(0)) == ""


# ── type-checked reads ──


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


# ── the messaging timeout, and the Electron opt-in ──


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
