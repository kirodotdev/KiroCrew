"""AT-SPI reads and actions, an owner of :mod:`kiro_crew.computer_use.linux_ffi`.

The tree (desktop, children, parent, name, role, pid, toolkit), states, component
geometry and focus, the action interface, text/value, editable text, and the AT-SPI
GLOBAL input generators (XTest under the hood). Every helper takes a raw accessible
handle and reaches the library through the guarded ``_atspi()`` accessor in
:mod:`kiro_crew.computer_use.linux_ffi_core`, so no native call happens before availability is proven.

:mod:`kiro_crew.computer_use.linux_ffi` stays the import path and the patch surface for every name defined
here (see :mod:`kiro_crew._facade_mirror`); an owner never imports its facade.
"""

from __future__ import annotations

from kiro_crew.computer_use.linux_ffi_core import (  # noqa: F401 - the shared bindings this owner reads
    ATSPI_COORD_SCREEN,
    ComputerUseError,
    _atspi,
    _gchar_to_str,
    _gerror,
    _lib_glib,
    ctypes,
    unref,
)

# ── AT-SPI: tree ──


def desktop() -> int:
    """The AT-SPI desktop (root of the application list). Caller owns the ref."""
    return int(_atspi().atspi_get_desktop(0) or 0)


def child_count(obj: int) -> int:
    """Number of children of *obj* (0 on error)."""
    err = ctypes.c_void_p(0)
    n = int(_atspi().atspi_accessible_get_child_count(ctypes.c_void_p(obj), ctypes.byref(err)))
    _gerror(err)
    return max(0, n)


def child_at(obj: int, i: int) -> "int | None":
    """The *i*-th child of *obj* (caller owns the ref), or ``None``."""
    err = ctypes.c_void_p(0)
    child = _atspi().atspi_accessible_get_child_at_index(
        ctypes.c_void_p(obj), int(i), ctypes.byref(err)
    )
    _gerror(err)
    return int(child) if child else None


def parent(obj: int) -> "int | None":
    """The parent of *obj* (caller owns the ref), or ``None``."""
    err = ctypes.c_void_p(0)
    p = _atspi().atspi_accessible_get_parent(ctypes.c_void_p(obj), ctypes.byref(err))
    _gerror(err)
    return int(p) if p else None


def name(obj: int) -> str:
    """The accessible name of *obj* (``""`` when absent)."""
    err = ctypes.c_void_p(0)
    ptr = _atspi().atspi_accessible_get_name(ctypes.c_void_p(obj), ctypes.byref(err))
    _gerror(err)
    return _gchar_to_str(int(ptr) if ptr else 0)


def description(obj: int) -> str:
    """The accessible description of *obj* (``""`` when absent)."""
    err = ctypes.c_void_p(0)
    ptr = _atspi().atspi_accessible_get_description(ctypes.c_void_p(obj), ctypes.byref(err))
    _gerror(err)
    return _gchar_to_str(int(ptr) if ptr else 0)


def role(obj: int) -> int:
    """The ``AtspiRole`` int of *obj*."""
    err = ctypes.c_void_p(0)
    r = int(_atspi().atspi_accessible_get_role(ctypes.c_void_p(obj), ctypes.byref(err)))
    _gerror(err)
    return r


def role_name(obj: int) -> str:
    """The LOCALISED role name of *obj* (display only; prefer :func:`role` for logic)."""
    err = ctypes.c_void_p(0)
    ptr = _atspi().atspi_accessible_get_role_name(ctypes.c_void_p(obj), ctypes.byref(err))
    _gerror(err)
    return _gchar_to_str(int(ptr) if ptr else 0)


def pid(obj: int) -> int:
    """The owning process id of *obj* (0 on error)."""
    err = ctypes.c_void_p(0)
    p = int(_atspi().atspi_accessible_get_process_id(ctypes.c_void_p(obj), ctypes.byref(err)))
    _gerror(err)
    return max(0, p)


def toolkit(obj: int) -> str:
    """The toolkit name of *obj* (``"GTK"``, ``"Qt"``, …), ``""`` when absent."""
    err = ctypes.c_void_p(0)
    ptr = _atspi().atspi_accessible_get_toolkit_name(ctypes.c_void_p(obj), ctypes.byref(err))
    _gerror(err)
    return _gchar_to_str(int(ptr) if ptr else 0)


# ── AT-SPI: states ──


def states(obj: int) -> "frozenset[int]":
    """The ``AtspiStateType`` ids set on *obj*.

    ``atspi_state_set_get_states`` returns a ``GArray`` of ``gint`` the caller owns;
    it is read out and ``g_array_free``d. The state SET object is itself a reference
    the caller owns and is unref-ed. An empty set on error (an unreadable node) is
    what makes ``snapshot_linux`` fail closed on ``secure``.
    """
    atspi = _atspi()
    state_set = atspi.atspi_accessible_get_state_set(ctypes.c_void_p(obj))
    if not state_set:
        return frozenset()
    try:
        arr = atspi.atspi_state_set_get_states(ctypes.c_void_p(int(state_set)))
        if not arr:
            return frozenset()
        try:
            count = int(arr.contents.len)
            data = arr.contents.data
            if not data or count <= 0:
                return frozenset()
            ints = ctypes.cast(data, ctypes.POINTER(ctypes.c_int))
            return frozenset(int(ints[i]) for i in range(count))
        finally:
            # g_array_free(arr, free_segment=TRUE): frees the GArray AND its data.
            _lib_glib().g_array_free(ctypes.c_void_p(ctypes.cast(arr, ctypes.c_void_p).value), 1)
    finally:
        unref(int(state_set))


def has_state(obj: int, state: int) -> bool:
    """Whether *obj* has ``AtspiStateType`` *state*.

    Goes through the state SET's ``contains`` rather than :func:`states` so a single
    membership test does not materialise the whole set, and unref-s the set.
    """
    state_set = _atspi().atspi_accessible_get_state_set(ctypes.c_void_p(obj))
    if not state_set:
        return False
    try:
        return bool(_atspi().atspi_state_set_contains(ctypes.c_void_p(int(state_set)), int(state)))
    finally:
        unref(int(state_set))


# ── AT-SPI: component (geometry + focus) ──


def extents(obj: int, coord: int = ATSPI_COORD_SCREEN) -> "tuple[int, int, int, int] | None":
    """``(x, y, width, height)`` of *obj* in *coord* (SCREEN or WINDOW), or ``None``.

    Callers that want a window-local frame should pass ``ATSPI_COORD_WINDOW`` rather
    than subtracting the window origin from a SCREEN read: GTK 4 (4.16 measured, on
    X11) answers SCREEN with ``x = y = 0`` for every widget, so a subtraction turns
    every frame into the window's own negative origin and loses all position.

    ``None`` when *obj* exposes no Component interface (ordinary — a label, a
    menu scaffold) or the read failed. ``atspi_component_get_extents`` returns a
    HEAP ``AtspiRect*`` the caller must ``g_free``; the Component interface object
    is itself a reference and is unref-ed.
    """
    comp = _atspi().atspi_accessible_get_component_iface(ctypes.c_void_p(obj))
    if not comp:
        return None
    try:
        err = ctypes.c_void_p(0)
        rect = _atspi().atspi_component_get_extents(
            ctypes.c_void_p(int(comp)), int(coord), ctypes.byref(err)
        )
        try:
            _gerror(err)
        except ComputerUseError:
            if rect:
                _lib_glib().g_free(ctypes.cast(rect, ctypes.c_void_p))
            return None
        if not rect:
            return None
        try:
            r = rect.contents
            return (int(r.x), int(r.y), int(r.width), int(r.height))
        finally:
            _lib_glib().g_free(ctypes.cast(rect, ctypes.c_void_p))
    finally:
        unref(int(comp))


def grab_focus(obj: int) -> bool:
    """Ask *obj* to take keyboard focus (Component.grab_focus). False on failure."""
    comp = _atspi().atspi_accessible_get_component_iface(ctypes.c_void_p(obj))
    if not comp:
        return False
    try:
        err = ctypes.c_void_p(0)
        ok = bool(
            _atspi().atspi_component_grab_focus(ctypes.c_void_p(int(comp)), ctypes.byref(err))
        )
        try:
            _gerror(err)
        except ComputerUseError:
            return False
        return ok
    finally:
        unref(int(comp))


# ── AT-SPI: action ──


def actions(obj: int) -> "tuple[str, ...]":
    """The NON-localised action names *obj* exposes (``()`` when none).

    ``atspi_action_get_action_name`` returns the canonical name (``"click"``,
    ``"press"``, ``"toggle"``), NOT the localised ``get_name`` — the canonical name
    is what ``input_linux`` matches against. The Action interface is a reference and
    is unref-ed.
    """
    atspi = _atspi()
    action = atspi.atspi_accessible_get_action_iface(ctypes.c_void_p(obj))
    if not action:
        return ()
    try:
        err = ctypes.c_void_p(0)
        n = int(atspi.atspi_action_get_n_actions(ctypes.c_void_p(int(action)), ctypes.byref(err)))
        try:
            _gerror(err)
        except ComputerUseError:
            return ()
        out: list[str] = []
        for i in range(max(0, n)):
            e2 = ctypes.c_void_p(0)
            ptr = atspi.atspi_action_get_action_name(
                ctypes.c_void_p(int(action)), i, ctypes.byref(e2)
            )
            try:
                _gerror(e2)
            except ComputerUseError:
                if ptr:
                    _lib_glib().g_free(ctypes.c_void_p(int(ptr)))
                continue
            text = _gchar_to_str(int(ptr) if ptr else 0)
            if text:
                out.append(text)
        return tuple(out)
    finally:
        unref(int(action))


def do_action(obj: int, index: int) -> bool:
    """Perform action *index* on *obj* (Action.do_action). False on failure."""
    action = _atspi().atspi_accessible_get_action_iface(ctypes.c_void_p(obj))
    if not action:
        return False
    try:
        err = ctypes.c_void_p(0)
        ok = bool(
            _atspi().atspi_action_do_action(
                ctypes.c_void_p(int(action)), int(index), ctypes.byref(err)
            )
        )
        try:
            _gerror(err)
        except ComputerUseError:
            return False
        return ok
    finally:
        unref(int(action))


# ── AT-SPI: text / value ──


def text(obj: int, limit: int) -> str:
    """Up to *limit* characters of *obj*'s text (``""`` when no Text interface).

    Clips to ``min(character_count, limit)`` so a huge document does not cross the
    boundary whole. NEVER call this on a secure element — ``snapshot_linux`` guards
    that; this is the raw read.
    """
    atspi = _atspi()
    text_iface = atspi.atspi_accessible_get_text_iface(ctypes.c_void_p(obj))
    if not text_iface:
        return ""
    try:
        err = ctypes.c_void_p(0)
        count = int(
            atspi.atspi_text_get_character_count(
                ctypes.c_void_p(int(text_iface)), ctypes.byref(err)
            )
        )
        try:
            _gerror(err)
        except ComputerUseError:
            return ""
        end = min(max(0, count), max(0, int(limit)))
        if end <= 0:
            return ""
        e2 = ctypes.c_void_p(0)
        ptr = atspi.atspi_text_get_text(ctypes.c_void_p(int(text_iface)), 0, end, ctypes.byref(e2))
        try:
            _gerror(e2)
        except ComputerUseError:
            if ptr:
                _lib_glib().g_free(ctypes.c_void_p(int(ptr)))
            return ""
        return _gchar_to_str(int(ptr) if ptr else 0)
    finally:
        unref(int(text_iface))


def value(obj: int) -> "float | None":
    """*obj*'s Value-interface current value, or ``None`` when it has none."""
    val_iface = _atspi().atspi_accessible_get_value_iface(ctypes.c_void_p(obj))
    if not val_iface:
        return None
    try:
        err = ctypes.c_void_p(0)
        v = float(
            _atspi().atspi_value_get_current_value(
                ctypes.c_void_p(int(val_iface)), ctypes.byref(err)
            )
        )
        try:
            _gerror(err)
        except ComputerUseError:
            return None
        return v
    finally:
        unref(int(val_iface))


# ── AT-SPI: editable text ──


def set_text_contents(obj: int, s: str) -> bool:
    """Replace *obj*'s text (EditableText.set_text_contents). False when not editable."""
    editable = _atspi().atspi_accessible_get_editable_text_iface(ctypes.c_void_p(obj))
    if not editable:
        return False
    try:
        err = ctypes.c_void_p(0)
        ok = bool(
            _atspi().atspi_editable_text_set_text_contents(
                ctypes.c_void_p(int(editable)), s.encode("utf-8"), ctypes.byref(err)
            )
        )
        try:
            _gerror(err)
        except ComputerUseError:
            return False
        return ok
    finally:
        unref(int(editable))


def insert_text(obj: int, s: str) -> bool:
    """Insert *s* at *obj*'s caret (EditableText.insert_text). False when not editable.

    Position -1 means "at the caret"; the byte length is passed as the character
    count AT-SPI expects, computed over the UTF-8 encoding it receives.
    """
    editable = _atspi().atspi_accessible_get_editable_text_iface(ctypes.c_void_p(obj))
    if not editable:
        return False
    try:
        encoded = s.encode("utf-8")
        err = ctypes.c_void_p(0)
        ok = bool(
            _atspi().atspi_editable_text_insert_text(
                ctypes.c_void_p(int(editable)), -1, encoded, len(s), ctypes.byref(err)
            )
        )
        try:
            _gerror(err)
        except ComputerUseError:
            return False
        return ok
    finally:
        unref(int(editable))


# ── AT-SPI: GLOBAL input (XTest under the hood) ──
#
# ``atspi_generate_keyboard_event`` and ``atspi_generate_mouse_event`` drive XTest,
# which is SESSION-GLOBAL: the event lands wherever focus/the pointer is, exactly
# like Windows ``SendInput``. ``input_linux`` confines every use with
# ``window_owns_point`` the way the Windows driver does, and only a request whose
# method the model NAMED as ``global`` ever reaches them.


def generate_keyboard_event(keyval: int, text_arg: "str | None", synth_type: int) -> bool:
    """Synthesize a keyboard event (``atspi_generate_keyboard_event``). GLOBAL."""
    err = ctypes.c_void_p(0)
    encoded = text_arg.encode("utf-8") if text_arg is not None else None
    ok = bool(
        _atspi().atspi_generate_keyboard_event(
            int(keyval), encoded, int(synth_type), ctypes.byref(err)
        )
    )
    try:
        _gerror(err)
    except ComputerUseError:
        return False
    return ok


def generate_mouse_event(x: int, y: int, event_name: str) -> bool:
    """Synthesize a mouse event (``atspi_generate_mouse_event``). GLOBAL, moves pointer.

    *event_name* is the AT-SPI spelling: ``"b1c"`` (button-1 click), ``"b1p"``/``"b1r"``
    (press/release), ``"abs"`` (absolute move).
    """
    err = ctypes.c_void_p(0)
    ok = bool(
        _atspi().atspi_generate_mouse_event(
            int(x), int(y), event_name.encode("ascii"), ctypes.byref(err)
        )
    )
    try:
        _gerror(err)
    except ComputerUseError:
        return False
    return ok
