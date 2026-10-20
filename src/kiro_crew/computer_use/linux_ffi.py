"""The ONLY module in this package that touches Linux-native code.

Everything native for Linux lives here: AT-SPI 2 (``libatspi``) for the
accessibility tree, Xlib (``libX11``/``libXtst``) for window geometry, capture
and global input synthesis, and ``libgdk_pixbuf`` for JPEG encoding. Every other
Linux computer-use module reaches the OS through the helpers below, so the FFI
hazards are audited in one place — the property ``windows_ffi`` and ``macos_ffi``
hold for their platforms.

**No ``CDLL`` runs at module scope.** Each library loads inside a cached
``_lib_<name>()`` helper that raises :class:`OSError` when it is absent, so this
module imports cleanly on a host with none of the libs and no ``DISPLAY`` — which
is what keeps the package importable on the headless CI fleet. A test pins it by
importing under a monkeypatched ``ctypes.CDLL`` that raises.

There is no COM vtable to mis-index here (these are plain C symbols), but the
SAME marshalling hazard applies: with no ``argtypes`` ctypes marshals a Python
int as a 32-bit C int and TRUNCATES the 64-bit handle, and the callee
dereferences garbage. Every row of :data:`_FN_SPECS` carries both ``argtypes``
and ``restype``, ``_bind`` RAISES on a missing ``argtypes``, and
``test_computer_use_linux_ffi.py`` pins every row.

Three Linux-specific hazards this module exists to contain:

1. **A BadWindow is a PROCESS ABORT, not an exception.** Xlib's default error
   handler calls ``exit()``, and a window can vanish between the enumeration that
   named it and the geometry read that measures it. :func:`_install_x_error_handler`
   replaces the default with one that RECORDS the error for the next checked call,
   so a dead XID degrades to ``None`` rather than taking the gateway (cron, Slack,
   every session) down with a SIGABRT. The handler is installed when the display
   first opens and the ``XErrorHandler`` object is held in a module global for the
   process lifetime — ctypes cannot raise out of a C callback, so letting it be
   collected while Xlib holds the pointer is itself a fault.

2. **A ``GError`` left unchecked leaks.** AT-SPI's ``get_name``/``get_extents``/…
   take a ``GError**`` out-param. :func:`_gerror` checks it, converts a set error
   to :class:`ComputerUseError`, and ALWAYS frees it with ``g_error_free`` — the
   error is owned by the caller the moment it is set.

3. **The Create/own rule.** Every AT-SPI accessor returning an object
   (``get_desktop``, ``get_child_at_index``, the ``*_iface`` getters) returns a
   reference the caller OWNS and drops with :func:`unref`; every ``gchar*`` is heap
   and is ``g_free``d after it is copied out; the ``GArray`` from
   ``state_set_get_states`` is ``g_array_free``d. A tree walk mints thousands, so a
   leak is a real RSS bug — :func:`owned` discharges the obligation, the twin of
   ``windows_ffi.owned``.

**The secure-field decision is role-driven and FAILS CLOSED.** There is no
``ATSPI_STATE_PROTECTED`` in at-spi2 2.54 — GTK, Qt and Firefox all expose a
password box as role ``PASSWORD_TEXT`` — so :data:`ATSPI_ROLE_PASSWORD_TEXT`
drives ``secure`` in ``snapshot_linux``, and an UNREADABLE role (a defunct node, a
raised ``GError``) resolves to secure. This module provides the raw reads;
``snapshot_linux`` owns the policy, as ``snapshot_windows`` owns it over
``windows_ffi.is_secure_element``.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# The owner modules, lowest layer first: the first one that binds a name answers a read
# of it. ``kiro_crew.computer_use.linux_ffi.<name>`` stays the import path and the patch surface for every name
# they define: a patch through this module reaches every owner that binds the name (see
# :mod:`kiro_crew._facade_mirror`).
_OWNERS = (
    "kiro_crew.computer_use.linux_ffi_core",
    "kiro_crew.computer_use.linux_ffi_atspi",
    "kiro_crew.computer_use.linux_ffi_x11",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.computer_use.linux_ffi_atspi import (  # noqa: F401
        actions,
        child_at,
        child_count,
        description,
        desktop,
        do_action,
        extents,
        generate_keyboard_event,
        generate_mouse_event,
        grab_focus,
        has_state,
        insert_text,
        name,
        parent,
        pid,
        role,
        role_name,
        set_text_contents,
        states,
        text,
        toolkit,
        value,
    )
    from kiro_crew.computer_use.linux_ffi_core import (  # noqa: F401
        _FN_SPECS,
        _GDK_INTERP_BILINEAR,
        _GECARG,
        _LIB_NAMES,
        ATSPI_COORD_SCREEN,
        ATSPI_COORD_WINDOW,
        ATSPI_ROLE_APPLICATION,
        ATSPI_ROLE_CHECK_BOX,
        ATSPI_ROLE_DIALOG,
        ATSPI_ROLE_ENTRY,
        ATSPI_ROLE_FILLER,
        ATSPI_ROLE_FRAME,
        ATSPI_ROLE_LABEL,
        ATSPI_ROLE_LINK,
        ATSPI_ROLE_MENU_ITEM,
        ATSPI_ROLE_PANEL,
        ATSPI_ROLE_PASSWORD_TEXT,
        ATSPI_ROLE_PUSH_BUTTON,
        ATSPI_ROLE_TEXT,
        ATSPI_ROLE_WINDOW,
        ATSPI_STATE_ACTIVE,
        ATSPI_STATE_CHECKED,
        ATSPI_STATE_DEFUNCT,
        ATSPI_STATE_EDITABLE,
        ATSPI_STATE_ENABLED,
        ATSPI_STATE_EXPANDED,
        ATSPI_STATE_FOCUSABLE,
        ATSPI_STATE_FOCUSED,
        ATSPI_STATE_ICONIFIED,
        ATSPI_STATE_MODAL,
        ATSPI_STATE_SELECTED,
        ATSPI_STATE_SENSITIVE,
        ATSPI_STATE_SHOWING,
        ATSPI_STATE_VISIBLE,
        KEYSYNTH_PRESSRELEASE,
        KEYSYNTH_STRING,
        KEYSYNTH_SYM,
        LIB_ATSPI,
        LIB_GDK_PIXBUF,
        LIB_GLIB,
        LIB_GOBJECT,
        LIB_X11,
        LIB_XTST,
        AtspiRect,
        Button1,
        Button2,
        Button3,
        Button4,
        Button5,
        ControlMask,
        GArray,
        Mod1Mask,
        Mod4Mask,
        ShiftMask,
        XErrorEvent,
        XImage,
        XWindowAttributes,
        _AllPlanes,
        _AnyPropertyType,
        _atspi,
        _atspi_initialised,
        _available,
        _bind,
        _ButtonMotionMask,
        _ButtonPress,
        _ButtonPressMask,
        _ButtonRelease,
        _ButtonReleaseMask,
        _compute_availability,
        _CurrentTime,
        _display,
        _display_ptr,
        _gchar_to_str,
        _gerror,
        _install_x_error_handler,
        _IsViewable,
        _KeyPress,
        _KeyRelease,
        _last_x_error,
        _lib,
        _lib_atspi,
        _lib_gdk_pixbuf,
        _lib_glib,
        _lib_gobject,
        _lib_x11,
        _lib_xtst,
        _libs,
        _libs_lock,
        _load_libraries,
        _MotionNotify,
        _open_display,
        _PropModeReplace,
        _require_available,
        _Success,
        _take_x_error,
        _unavailable_reason,
        _x_error_handler,
        _XErrorHandlerType,
        _ZPixmap,
        available,
        display_name,
        logger,
        owned,
        reset_libraries,
        shutdown,
        unavailable_reason,
        unref,
    )
    from kiro_crew.computer_use.linux_ffi_x11 import (  # noqa: F401
        _XEVENT_SIZE,
        _atom,
        _button_mask,
        _channel_offset,
        _send_event_buffer,
        _unpack_rgb,
        _window_attributes,
        _window_prop_cardinals,
        _window_prop_text,
        _XButtonEvent,
        _XKeyEvent,
        encode_jpeg_rgb,
        x_active_window,
        x_client_list,
        x_flush,
        x_get_image_rgb,
        x_gtk_frame_extents,
        x_keycode_needs_shift,
        x_keysym_to_keycode,
        x_root,
        x_screen_size,
        x_send_button,
        x_send_key,
        x_send_motion,
        x_string_to_keysym,
        x_sync,
        x_window_at,
        x_window_geometry,
        x_window_is_viewable,
        x_window_pid,
        x_window_title,
        xtest_available,
        xtest_button,
        xtest_key,
        xtest_motion,
    )

__all__ = [
    # constants
    "ATSPI_ROLE_APPLICATION",
    "ATSPI_ROLE_CHECK_BOX",
    "ATSPI_ROLE_DIALOG",
    "ATSPI_ROLE_ENTRY",
    "ATSPI_ROLE_FILLER",
    "ATSPI_ROLE_FRAME",
    "ATSPI_ROLE_LABEL",
    "ATSPI_ROLE_LINK",
    "ATSPI_ROLE_MENU_ITEM",
    "ATSPI_ROLE_PANEL",
    "ATSPI_ROLE_PASSWORD_TEXT",
    "ATSPI_ROLE_PUSH_BUTTON",
    "ATSPI_ROLE_TEXT",
    "ATSPI_ROLE_WINDOW",
    "ATSPI_STATE_ACTIVE",
    "ATSPI_STATE_CHECKED",
    "ATSPI_STATE_DEFUNCT",
    "ATSPI_STATE_EDITABLE",
    "ATSPI_STATE_ENABLED",
    "ATSPI_STATE_EXPANDED",
    "ATSPI_STATE_FOCUSABLE",
    "ATSPI_STATE_FOCUSED",
    "ATSPI_STATE_ICONIFIED",
    "ATSPI_STATE_MODAL",
    "ATSPI_STATE_SELECTED",
    "ATSPI_STATE_SENSITIVE",
    "ATSPI_STATE_SHOWING",
    "ATSPI_STATE_VISIBLE",
    "ATSPI_COORD_SCREEN",
    "ATSPI_COORD_WINDOW",
    "KEYSYNTH_PRESSRELEASE",
    "KEYSYNTH_STRING",
    "KEYSYNTH_SYM",
    "Button1",
    "Button2",
    "Button3",
    "Button4",
    "Button5",
    "ShiftMask",
    "ControlMask",
    "Mod1Mask",
    "Mod4Mask",
    # lifecycle
    "available",
    "unavailable_reason",
    "display_name",
    "shutdown",
    "reset_libraries",
    # at-spi
    "desktop",
    "child_count",
    "child_at",
    "parent",
    "name",
    "description",
    "role",
    "role_name",
    "pid",
    "toolkit",
    "states",
    "has_state",
    "extents",
    "actions",
    "do_action",
    "text",
    "value",
    "set_text_contents",
    "insert_text",
    "grab_focus",
    "unref",
    "owned",
    "generate_keyboard_event",
    "generate_mouse_event",
    # x11
    "x_root",
    "x_screen_size",
    "x_client_list",
    "x_active_window",
    "x_gtk_frame_extents",
    "x_window_pid",
    "x_window_title",
    "x_window_geometry",
    "x_window_is_viewable",
    "x_window_at",
    "x_get_image_rgb",
    "x_send_button",
    "x_send_motion",
    "x_send_key",
    "x_keysym_to_keycode",
    "x_string_to_keysym",
    "x_keycode_needs_shift",
    "x_flush",
    "x_sync",
    # xtest
    "xtest_available",
    "xtest_motion",
    "xtest_button",
    "xtest_key",
    # gdk-pixbuf
    "encode_jpeg_rgb",
]
