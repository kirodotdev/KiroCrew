"""X11, XTest and gdk-pixbuf, an owner of :mod:`kiro_crew.computer_use.linux_ffi`.

Root/screen/window reads (EWMH properties, geometry, viewability, hit test), window
capture (``XGetImage``), synthetic per-window events (``XSendEvent`` button, motion
and key) and the keymap, XTest GLOBAL input, and JPEG encoding. Every helper reaches
the display through ``_display_ptr()`` in :mod:`kiro_crew.computer_use.linux_ffi_core`, which refuses before any
native call on an unavailable host.

:mod:`kiro_crew.computer_use.linux_ffi` stays the import path and the patch surface for every name defined
here (see :mod:`kiro_crew._facade_mirror`); an owner never imports its facade.
"""

from __future__ import annotations

from kiro_crew.computer_use.linux_ffi_core import (  # noqa: F401 - the shared bindings this owner reads
    _GDK_INTERP_BILINEAR,
    Any,
    XImage,
    XWindowAttributes,
    _AllPlanes,
    _AnyPropertyType,
    _ButtonMotionMask,
    _ButtonPress,
    _ButtonPressMask,
    _ButtonRelease,
    _ButtonReleaseMask,
    _CurrentTime,
    _display_ptr,
    _IsViewable,
    _KeyPress,
    _KeyRelease,
    _lib_gdk_pixbuf,
    _lib_glib,
    _lib_x11,
    _lib_xtst,
    _MotionNotify,
    _Success,
    _take_x_error,
    _ZPixmap,
    ctypes,
    logger,
    unref,
)

# ── X11: root / screen / windows ──


def x_root() -> int:
    """The root window XID of the default screen."""
    return int(_lib_x11().XDefaultRootWindow(_display_ptr()))


def x_screen_size() -> "tuple[int, int]":
    """``(width, height)`` of the default screen in pixels."""
    x11 = _lib_x11()
    disp = _display_ptr()
    screen = int(x11.XDefaultScreen(disp))
    return int(x11.XDisplayWidth(disp, screen)), int(x11.XDisplayHeight(disp, screen))


def _atom(name_str: str, *, only_if_exists: bool = True) -> int:
    """Intern an X atom by name (0 when it does not exist)."""
    return int(
        _lib_x11().XInternAtom(_display_ptr(), name_str.encode("ascii"), 1 if only_if_exists else 0)
    )


def _window_prop_cardinals(window: int, prop_name: str) -> "list[int]":
    """Read a CARDINAL/WINDOW/ATOM array property off *window* as a list of ints.

    Handles the ``XGetWindowProperty`` two-call protocol in one shot by asking for a
    generous ``long_length``. The returned buffer is ``XFree``d. An unset property,
    a BadWindow (recorded by the error handler), or a zero-item result all yield
    ``[]``.
    """
    atom = _atom(prop_name)
    if not atom:
        return []
    x11 = _lib_x11()
    actual_type = ctypes.c_ulong(0)
    actual_format = ctypes.c_int(0)
    nitems = ctypes.c_ulong(0)
    bytes_after = ctypes.c_ulong(0)
    prop = ctypes.c_void_p(0)
    _take_x_error()
    status = int(
        x11.XGetWindowProperty(
            _display_ptr(),
            ctypes.c_ulong(window),
            ctypes.c_ulong(atom),
            0,
            1024,
            0,
            _AnyPropertyType,
            ctypes.byref(actual_type),
            ctypes.byref(actual_format),
            ctypes.byref(nitems),
            ctypes.byref(bytes_after),
            ctypes.byref(prop),
        )
    )
    if status != _Success or not prop.value or _take_x_error():
        if prop.value:
            x11.XFree(prop)
        return []
    try:
        count = int(nitems.value)
        fmt = int(actual_format.value)
        if count <= 0 or fmt == 0:
            return []
        # A 32-bit X property comes back as an array of C ``long`` (NOT int32) on a
        # 64-bit host — Xlib widens 32-bit items to ``long``. 8-bit stays a byte.
        buf: Any
        if fmt == 8:
            buf = ctypes.cast(prop, ctypes.POINTER(ctypes.c_ubyte))
        elif fmt == 16:
            buf = ctypes.cast(prop, ctypes.POINTER(ctypes.c_uint16))
        else:
            buf = ctypes.cast(prop, ctypes.POINTER(ctypes.c_ulong))
        return [int(buf[i]) for i in range(count)]
    finally:
        x11.XFree(prop)


def x_client_list() -> "tuple[int, ...]":
    """Top-level client XIDs, TOPMOST first.

    Prefers ``_NET_CLIENT_LIST_STACKING`` (stacking order, bottom-to-top in the
    spec, so reversed here to put the topmost window first), falling back to
    ``_NET_CLIENT_LIST`` (initial-mapping order) when the stacking variant is
    absent.
    """
    stacking = _window_prop_cardinals(x_root(), "_NET_CLIENT_LIST_STACKING")
    if stacking:
        return tuple(reversed(stacking))
    return tuple(_window_prop_cardinals(x_root(), "_NET_CLIENT_LIST"))


def x_active_window() -> "int | None":
    """The ``_NET_ACTIVE_WINDOW`` XID, or ``None``."""
    items = _window_prop_cardinals(x_root(), "_NET_ACTIVE_WINDOW")
    return items[0] if items and items[0] else None


def x_gtk_frame_extents(window: int) -> "tuple[int, int, int, int] | None":
    """``_GTK_FRAME_EXTENTS`` of *window* as ``(left, right, top, bottom)``, or ``None``.

    A client-side-decorated (CSD) toplevel — every GTK 4 window — is LARGER than its
    content: the X window carries the shadow margins, and this property says by how
    much. Measured on gnome-calculator: X geometry ``482x748`` at ``(-61, -23)`` with
    extents ``61, 61, 55, 67`` gives the ``360x626`` content rect the AT-SPI frame
    reports. Without it the capture includes the transparent shadow band and the
    image-to-element mapping is off by the margin.
    """
    items = _window_prop_cardinals(window, "_GTK_FRAME_EXTENTS")
    if not items or len(items) < 4:
        return None
    return int(items[0]), int(items[1]), int(items[2]), int(items[3])


def x_window_pid(window: int) -> "int | None":
    """The ``_NET_WM_PID`` of *window*, or ``None``."""
    items = _window_prop_cardinals(window, "_NET_WM_PID")
    return items[0] if items and items[0] else None


def _window_prop_text(window: int, prop_name: str) -> str:
    """Read a string property (UTF8_STRING or STRING) off *window*, or ``""``."""
    atom = _atom(prop_name)
    if not atom:
        return ""
    x11 = _lib_x11()
    actual_type = ctypes.c_ulong(0)
    actual_format = ctypes.c_int(0)
    nitems = ctypes.c_ulong(0)
    bytes_after = ctypes.c_ulong(0)
    prop = ctypes.c_void_p(0)
    _take_x_error()
    status = int(
        x11.XGetWindowProperty(
            _display_ptr(),
            ctypes.c_ulong(window),
            ctypes.c_ulong(atom),
            0,
            4096,
            0,
            _AnyPropertyType,
            ctypes.byref(actual_type),
            ctypes.byref(actual_format),
            ctypes.byref(nitems),
            ctypes.byref(bytes_after),
            ctypes.byref(prop),
        )
    )
    if status != _Success or not prop.value or _take_x_error():
        if prop.value:
            x11.XFree(prop)
        return ""
    try:
        count = int(nitems.value)
        if count <= 0:
            return ""
        raw = ctypes.string_at(prop.value, count)
        return raw.decode("utf-8", "replace")
    finally:
        x11.XFree(prop)


def x_window_title(window: int) -> str:
    """The window's title: ``_NET_WM_NAME`` (UTF8), falling back to ``WM_NAME``."""
    title = _window_prop_text(window, "_NET_WM_NAME")
    if title:
        return title
    return _window_prop_text(window, "WM_NAME")


def _window_attributes(window: int) -> "XWindowAttributes | None":
    """``XGetWindowAttributes`` for *window*, or ``None`` on a dead window.

    The error handler converts a BadWindow (the window vanished between enumeration
    and this read) into a recorded code rather than an ``exit()``; the recorded code
    is read with :func:`_take_x_error` and makes the result ``None``.
    """
    attrs = XWindowAttributes()
    _take_x_error()
    status = int(
        _lib_x11().XGetWindowAttributes(_display_ptr(), ctypes.c_ulong(window), ctypes.byref(attrs))
    )
    if status == 0 or _take_x_error():
        return None
    return attrs


def x_window_geometry(window: int) -> "tuple[int, int, int, int] | None":
    """Absolute root-coordinate ``(x, y, width, height)`` of *window*, or ``None``.

    Translates the window's own origin to the root via ``XTranslateCoordinates``, so
    a reparenting window manager's frame offset is accounted for. A BadWindow at
    either step (the window is gone) yields ``None`` rather than aborting the process.
    """
    attrs = _window_attributes(window)
    if attrs is None:
        return None
    root = x_root()
    dest_x = ctypes.c_int(0)
    dest_y = ctypes.c_int(0)
    child = ctypes.c_ulong(0)
    _take_x_error()
    ok = int(
        _lib_x11().XTranslateCoordinates(
            _display_ptr(),
            ctypes.c_ulong(window),
            ctypes.c_ulong(root),
            0,
            0,
            ctypes.byref(dest_x),
            ctypes.byref(dest_y),
            ctypes.byref(child),
        )
    )
    if ok == 0 or _take_x_error():
        return None
    return (int(dest_x.value), int(dest_y.value), int(attrs.width), int(attrs.height))


def x_window_is_viewable(window: int) -> bool:
    """Whether *window* is mapped and viewable (``map_state == IsViewable``)."""
    attrs = _window_attributes(window)
    return attrs is not None and int(attrs.map_state) == _IsViewable


def x_window_at(x: int, y: int) -> "int | None":
    """The top-level client window owning screen point ``(x, y)``, or ``None``.

    Walks down from the root with ``XTranslateCoordinates`` (whose ``child_return``
    is the child under the point), then maps the resolved descendant up to its
    ancestor in the client list — the toplevel confinement keys on. A point over no
    client (the desktop) yields ``None``.
    """
    root = x_root()
    dest_x = ctypes.c_int(0)
    dest_y = ctypes.c_int(0)
    child = ctypes.c_ulong(0)
    _take_x_error()
    ok = int(
        _lib_x11().XTranslateCoordinates(
            _display_ptr(),
            ctypes.c_ulong(root),
            ctypes.c_ulong(root),
            int(x),
            int(y),
            ctypes.byref(dest_x),
            ctypes.byref(dest_y),
            ctypes.byref(child),
        )
    )
    if ok == 0 or _take_x_error() or not child.value:
        return None
    hit = int(child.value)
    clients = set(x_client_list())
    if hit in clients:
        return hit
    # The point landed on a descendant of a client (a reparented frame). Walk the
    # geometry: the client whose root-rect contains the point and is topmost wins.
    for candidate in x_client_list():  # topmost first
        geo = x_window_geometry(candidate)
        if geo is None:
            continue
        gx, gy, gw, gh = geo
        if gx <= x < gx + gw and gy <= y < gy + gh:
            return candidate
    return None


# ── X11: capture ──


def x_get_image_rgb(x: int, y: int, w: int, h: int) -> "tuple[bytes, int, int] | None":
    """Capture a root rect as packed 24-bit RGB rows. ``(rgb, width, height)`` or ``None``.

    ``XGetImage`` on the ROOT over ``(x, y, w, h)`` returns an ``XImage`` whose pixel
    layout is read from its OWN header — ``bits_per_pixel`` (32-bpp BGRX or 24-bpp
    RGB), ``bytes_per_line`` (the row stride, which is NOT ``w * bpp/8`` in general),
    ``byte_order`` and the colour masks — never with the per-image ``get_pixel``
    function table. The pixels are copied out BEFORE ``XDestroyImage``, which owns and
    frees the whole image.

    Returns ``None`` for a non-positive rect, a failed grab, or an unsupported depth
    (logged at debug). On X11 ``XGetImage`` over the root captures what is ON SCREEN,
    so an overlapping window shows through — ``capture_linux`` documents that honestly.
    """
    if w <= 0 or h <= 0:
        return None
    x11 = _lib_x11()
    _take_x_error()
    img_ptr = x11.XGetImage(
        _display_ptr(),
        ctypes.c_ulong(x_root()),
        int(x),
        int(y),
        int(w),
        int(h),
        _AllPlanes,
        _ZPixmap,
    )
    if not img_ptr or _take_x_error():
        return None
    try:
        img = img_ptr.contents
        width = int(img.width)
        height = int(img.height)
        bpp = int(img.bits_per_pixel)
        stride = int(img.bytes_per_line)
        data_addr = img.data
        if not data_addr or width <= 0 or height <= 0 or stride <= 0:
            return None
        if bpp not in (24, 32):
            logger.debug("XGetImage returned unsupported bits_per_pixel=%d", bpp)
            return None
        raw = ctypes.string_at(data_addr, stride * height)
        return _unpack_rgb(raw, width, height, bpp, stride, img), width, height
    finally:
        x11.XDestroyImage(img_ptr)


def _unpack_rgb(raw: bytes, width: int, height: int, bpp: int, stride: int, img: "XImage") -> bytes:
    """Pack an XImage pixel buffer into tight 24-bit RGB rows.

    The common server layout is little-endian BGRX (32-bpp) or BGR (24-bpp), so the
    channel order is derived from the image's colour MASKS rather than assumed: the
    byte offset of each channel inside a pixel is ``mask.bit_length()//8 - 1`` for a
    little-endian image. This handles both the 32-bpp BGRX and the 24-bpp cases the
    contract names, and is exercised by the test against a synthetic XImage of each
    shape.
    """
    bytes_per_pixel = bpp // 8
    r_off = _channel_offset(int(img.red_mask))
    g_off = _channel_offset(int(img.green_mask))
    b_off = _channel_offset(int(img.blue_mask))
    out = bytearray(width * height * 3)
    o = 0
    for row in range(height):
        base = row * stride
        for col in range(width):
            p = base + col * bytes_per_pixel
            out[o] = raw[p + r_off]
            out[o + 1] = raw[p + g_off]
            out[o + 2] = raw[p + b_off]
            o += 3
    return bytes(out)


def _channel_offset(mask: int) -> int:
    """Byte offset of a colour channel within a little-endian pixel, from its mask.

    A mask of ``0x00FF0000`` (red in XRGB/BGRX little-endian) occupies byte 2;
    ``0x000000FF`` occupies byte 0. ``(mask.bit_length() + 7) // 8 - 1`` is the index
    of the highest set byte, which is the channel's little-endian byte position.
    Clamped to ``[0, 3]`` so a zero or malformed mask degrades to byte 0 rather than
    indexing out of a pixel.
    """
    if mask <= 0:
        return 0
    return max(0, min(3, (mask.bit_length() + 7) // 8 - 1))


# ── X11: synthetic per-window events + keymap ──


class _XButtonEvent(ctypes.Structure):
    """``XButtonEvent`` for ``XSendEvent``. Fields past ``button`` are padding."""

    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("button", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


class _XKeyEvent(ctypes.Structure):
    """``XKeyEvent`` for ``XSendEvent``. Same prefix as ``XButtonEvent``."""

    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("keycode", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


# ``XEvent`` is a union whose largest member is 24 ``long``s. Both event structs
# above are shorter, so a union-sized buffer is allocated and the typed view writes
# into its head — a short struct handed to ``XSendEvent`` would let the server read
# past it.
_XEVENT_SIZE = ctypes.sizeof(ctypes.c_long) * 24


def _send_event_buffer(typed: "ctypes.Structure") -> "ctypes.Array":
    """A full ``XEvent``-sized buffer carrying *typed* at its head."""
    buf = (ctypes.c_byte * _XEVENT_SIZE)()
    ctypes.memmove(buf, ctypes.byref(typed), ctypes.sizeof(typed))
    return buf


def x_send_button(window: int, x: int, y: int, button: int, press: bool) -> bool:
    """``XSendEvent`` a ButtonPress/ButtonRelease to *window*. False on failure.

    Carries both window-relative ``(x, y)`` and root coordinates (resolved via the
    window's geometry), ``time=CurrentTime``, ``same_screen=True``, and the matching
    event mask. Toolkits MAY ignore synthetic button events (GTK4 does) — the driver
    measures and reports that; this is the raw post.
    """
    geo = x_window_geometry(window)
    root_x = (geo[0] + x) if geo else x
    root_y = (geo[1] + y) if geo else y
    ev = _XButtonEvent(
        type=_ButtonPress if press else _ButtonRelease,
        send_event=1,
        display=_display_ptr(),
        window=ctypes.c_ulong(window).value,
        root=ctypes.c_ulong(x_root()).value,
        subwindow=0,
        time=_CurrentTime,
        x=int(x),
        y=int(y),
        x_root=int(root_x),
        y_root=int(root_y),
        state=0,
        button=int(button),
        same_screen=1,
    )
    mask = _ButtonPressMask if press else _ButtonReleaseMask
    buf = _send_event_buffer(ev)
    _take_x_error()
    rc = int(
        _lib_x11().XSendEvent(_display_ptr(), ctypes.c_ulong(window), 1, ctypes.c_long(mask), buf)
    )
    _lib_x11().XFlush(_display_ptr())
    return rc != 0 and not _take_x_error()


def x_send_motion(window: int, x: int, y: int, button: int) -> bool:
    """``XSendEvent`` a MotionNotify to *window* with *button* held. False on failure.

    The interior of an ``app_post`` drag: ``XMotionEvent`` has ``XButtonEvent``'s
    layout with ``is_hint`` (a char) in the ``button`` slot, so the one structure
    serves both; ``state`` carries the held button's ``ButtonNMask`` so a toolkit
    that honours synthetic motion sees a drag and not a hover. As with
    :func:`x_send_button`, toolkits MAY ignore it — this is the raw post.
    """
    geo = x_window_geometry(window)
    root_x = (geo[0] + x) if geo else x
    root_y = (geo[1] + y) if geo else y
    ev = _XButtonEvent(
        type=_MotionNotify,
        send_event=1,
        display=_display_ptr(),
        window=ctypes.c_ulong(window).value,
        root=ctypes.c_ulong(x_root()).value,
        subwindow=0,
        time=_CurrentTime,
        x=int(x),
        y=int(y),
        x_root=int(root_x),
        y_root=int(root_y),
        state=_button_mask(button),
        button=0,  # XMotionEvent.is_hint = NotifyNormal
        same_screen=1,
    )
    buf = _send_event_buffer(ev)
    _take_x_error()
    rc = int(
        _lib_x11().XSendEvent(
            _display_ptr(), ctypes.c_ulong(window), 1, ctypes.c_long(_ButtonMotionMask), buf
        )
    )
    _lib_x11().XFlush(_display_ptr())
    return rc != 0 and not _take_x_error()


def _button_mask(button: int) -> int:
    """``Button1Mask`` .. ``Button5Mask`` for *button* (1-5), else 0."""
    return (1 << (7 + int(button))) if 1 <= int(button) <= 5 else 0


def x_send_key(window: int, keycode: int, state: int, press: bool) -> bool:
    """``XSendEvent`` a KeyPress/KeyRelease to *window*. False on failure."""
    ev = _XKeyEvent(
        type=_KeyPress if press else _KeyRelease,
        send_event=1,
        display=_display_ptr(),
        window=ctypes.c_ulong(window).value,
        root=ctypes.c_ulong(x_root()).value,
        subwindow=0,
        time=_CurrentTime,
        x=1,
        y=1,
        x_root=1,
        y_root=1,
        state=int(state),
        keycode=int(keycode),
        same_screen=1,
    )
    buf = _send_event_buffer(ev)
    _take_x_error()
    rc = int(
        _lib_x11().XSendEvent(_display_ptr(), ctypes.c_ulong(window), 1, ctypes.c_long(0), buf)
    )
    _lib_x11().XFlush(_display_ptr())
    return rc != 0 and not _take_x_error()


def x_keysym_to_keycode(keysym: int) -> int:
    """The hardware keycode producing *keysym* (0 when none)."""
    return int(_lib_x11().XKeysymToKeycode(_display_ptr(), ctypes.c_ulong(keysym)))


def x_string_to_keysym(name_str: str) -> int:
    """``XStringToKeysym`` — a keysym name (``"Return"``, ``"U0041"``) to its value.

    ``0`` (``NoSymbol``) when the name is unknown.
    """
    return int(_lib_x11().XStringToKeysym(name_str.encode("ascii")))


def x_keycode_needs_shift(keycode: int, keysym: int) -> bool:
    """Whether *keysym* is on the SHIFTED level of *keycode*.

    True when the UNshifted mapping of the keycode is not *keysym* but the shifted
    one is — i.e. producing the symbol requires Shift held. Used so a typed character
    whose keysym sits on the upper level gets a Shift modifier.
    """
    x11 = _lib_x11()
    disp = _display_ptr()
    unshifted = int(x11.XkbKeycodeToKeysym(disp, ctypes.c_ubyte(keycode), 0, 0))
    shifted = int(x11.XkbKeycodeToKeysym(disp, ctypes.c_ubyte(keycode), 0, 1))
    return unshifted != keysym and shifted == keysym


def x_flush() -> None:
    """Flush the X output buffer (deliver queued requests without blocking)."""
    _lib_x11().XFlush(_display_ptr())


def x_sync() -> None:
    """Flush AND wait for the server to process all requests (``XSync(discard=False)``)."""
    _lib_x11().XSync(_display_ptr(), 0)


# ── XTest: GLOBAL input ──


def xtest_available() -> bool:
    """Whether the XTEST extension is present on the server."""
    try:
        event_base = ctypes.c_int(0)
        error_base = ctypes.c_int(0)
        major = ctypes.c_int(0)
        minor = ctypes.c_int(0)
        return bool(
            _lib_xtst().XTestQueryExtension(
                _display_ptr(),
                ctypes.byref(event_base),
                ctypes.byref(error_base),
                ctypes.byref(major),
                ctypes.byref(minor),
            )
        )
    except Exception:
        logger.debug("XTestQueryExtension failed", exc_info=True)
        return False


def xtest_motion(x: int, y: int) -> None:
    """Warp the REAL pointer to screen ``(x, y)`` (``XTestFakeMotionEvent``). GLOBAL."""
    _lib_xtst().XTestFakeMotionEvent(_display_ptr(), -1, int(x), int(y), _CurrentTime)
    _lib_x11().XFlush(_display_ptr())


def xtest_button(button: int, press: bool) -> None:
    """Press/release a REAL mouse button (``XTestFakeButtonEvent``). GLOBAL."""
    _lib_xtst().XTestFakeButtonEvent(
        _display_ptr(), ctypes.c_uint(button), 1 if press else 0, _CurrentTime
    )
    _lib_x11().XFlush(_display_ptr())


def xtest_key(keycode: int, press: bool) -> None:
    """Press/release a REAL key by keycode (``XTestFakeKeyEvent``). GLOBAL."""
    _lib_xtst().XTestFakeKeyEvent(
        _display_ptr(), ctypes.c_uint(keycode), 1 if press else 0, _CurrentTime
    )
    _lib_x11().XFlush(_display_ptr())


# ── gdk-pixbuf: JPEG encoding ──


def encode_jpeg_rgb(
    rgb: bytes, width: int, height: int, *, max_px: int, quality: int
) -> "tuple[bytes, int, int] | None":
    """Downscale packed RGB and encode it to JPEG. ``(jpeg, out_w, out_h)`` or ``None``.

    Wraps the RGB rows in a ``GdkPixbuf`` (no alpha, 8 bits/sample, rowstride
    ``width*3``), scales so the long edge is at most *max_px* with
    ``GDK_INTERP_BILINEAR``, and encodes with ``gdk_pixbuf_save_to_bufferv`` at
    *quality*. Every pixbuf created is unref-ed; the ``GError`` out-param is checked
    and freed; the encode buffer is copied out and ``g_free``d. Returns ``None`` on
    any failure (logged at debug) — the caller treats a missing image as "tree only".
    """
    if width <= 0 or height <= 0 or len(rgb) < width * height * 3:
        return None
    gdk = _lib_gdk_pixbuf()
    glib = _lib_glib()
    # ``gdk_pixbuf_new_from_data`` does NOT copy — it keeps the data pointer — so the
    # source bytes must outlive the pixbuf. A Python ``bytes`` kept in a local for the
    # duration does that; the destroy callback is NULL (we free nothing on its behalf).
    src = ctypes.create_string_buffer(rgb, len(rgb))
    pixbuf = gdk.gdk_pixbuf_new_from_data(
        ctypes.cast(src, ctypes.c_char_p),
        0,
        0,
        8,
        int(width),
        int(height),
        int(width) * 3,
        None,
        None,
    )
    if not pixbuf:
        return None
    scaled = 0
    try:
        long_edge = max(width, height)
        if long_edge > max_px > 0:
            scale = max_px / float(long_edge)
            out_w = max(1, int(round(width * scale)))
            out_h = max(1, int(round(height * scale)))
            scaled = gdk.gdk_pixbuf_scale_simple(
                ctypes.c_void_p(int(pixbuf)), out_w, out_h, _GDK_INTERP_BILINEAR
            )
            target = int(scaled) if scaled else int(pixbuf)
        else:
            target = int(pixbuf)
        out_w = int(gdk.gdk_pixbuf_get_width(ctypes.c_void_p(target)))
        out_h = int(gdk.gdk_pixbuf_get_height(ctypes.c_void_p(target)))

        keys = (ctypes.c_char_p * 2)(b"quality", None)
        q = max(0, min(100, int(quality)))
        values = (ctypes.c_char_p * 2)(str(q).encode("ascii"), None)
        buf_out = ctypes.c_void_p(0)
        size_out = ctypes.c_size_t(0)
        err = ctypes.c_void_p(0)
        ok = int(
            gdk.gdk_pixbuf_save_to_bufferv(
                ctypes.c_void_p(target),
                ctypes.byref(buf_out),
                ctypes.byref(size_out),
                b"jpeg",
                keys,
                values,
                ctypes.byref(err),
            )
        )
        if err.value:
            glib.g_error_free(err)
            return None
        if not ok or not buf_out.value or size_out.value <= 0:
            if buf_out.value:
                glib.g_free(buf_out)
            return None
        try:
            data = ctypes.string_at(buf_out.value, int(size_out.value))
        finally:
            glib.g_free(buf_out)
        return data, out_w, out_h
    except Exception:
        logger.debug("encode_jpeg_rgb failed", exc_info=True)
        return None
    finally:
        if scaled:
            unref(int(scaled))
        unref(int(pixbuf))
