"""The AT-SPI tree walk: one application frame into an indexed element tree.

The Linux counterpart of :mod:`snapshot_windows` / :mod:`snapshot_macos`.
Everything platform-specific about reaching the tree lives in :mod:`linux_ffi`;
this module owns the SHAPE of the result — which nodes get an index, what a role
is called, which of the walk's own limits have to be reported, and the secure
floor. The three invariants the layers above depend on are inherited verbatim
from the other two platforms:

* **The walk is ITERATIVE.** A deep tree must not ``RecursionError`` inside a
  ctypes call, where the failure is not a clean Python exception. An explicit
  stack of ``(obj, depth)`` entries carries the depth-first pre-order so indices
  are stable and ``depth`` is meaningful.
* **Indices are DENSE.** A structural container (``Pane``/``Filler``) with no
  title and no value is elided WITHOUT consuming an index, so the numbering a
  model addresses stays compact. ``index.resolve`` looks a record up by its
  ``index`` field rather than list position, so the two are not interchangeable —
  and :func:`addressed` must reproduce this numbering EXACTLY or it resolves a
  different control.
* **A cut-off walk says so.** ``truncated`` / ``depth_truncated`` are not
  diagnostics: the capture gate refuses a screenshot when either is set, because a
  silently-dropped subtree would let the secure-field scan reflect only the nodes
  it reached, and a password field past the cut would get the whole window
  photographed.

**Secure is role-driven and fails closed.** There is no ``ATSPI_STATE_PROTECTED``
in at-spi2 2.54 (the state enum has no such member), and GTK, Qt and Firefox all
expose a password box as role ``PASSWORD_TEXT``. So ``secure`` is: role is
``PASSWORD_TEXT`` OR the role could not be read. An unreadable role fails CLOSED
to secure, matching every downstream protection (value redaction, input refusal,
screenshot suppression) that keys off the flag. The ``subrole`` field carries
``"PasswordText"`` for a secure node so ``render.fingerprint`` has a stable value
to pin drift against (AT-SPI has no subrole of its own).

Frames are WINDOW-LOCAL, with ``Snapshot.window_bounds`` publishing the origin
they are relative to. AT-SPI's Component interface reports SCREEN extents, so this
module subtracts the window origin: a screen-absolute rect could not be related to
the screenshot the model may also be reading (a crop of the window), and a
window-local frame survives the user dragging the window between turns.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

from kiro_crew.computer_use import apps_linux, linux_ffi
from kiro_crew.computer_use.types import (
    DEFAULT_TEXT_LIMIT,
    TRAIT_EDITABLE,
    TRAIT_EXPANDED,
    TRAIT_SELECTED,
    AppRef,
    ComputerUseError,
    ElementRec,
    Snapshot,
    SnapshotRequest,
)

logger = logging.getLogger(__name__)

#: Per-node child cap, the Linux twin of ``snapshot_windows.MAX_CHILDREN_PER_NODE``
#: and the same value for the same reason: it bounds ONE pathological container (a
#: data grid with 100k rows) before the global node budget notices, so a single
#: element cannot make one walk materialise a huge list of accessible objects.
MAX_CHILDREN_PER_NODE = 512

#: AT-SPI ``ATSPI_ROLE_*`` int -> the human role name the renderer already shows,
#: chosen so a model reads the SAME words it sees on macOS/Windows
#: (``Button``/``Edit``/``CheckBox``/...). Unknown roles fall back to
#: ``role_name()`` title-cased (see :func:`_role_for`), so an unmapped role is
#: still named rather than blank.
_ROLE_NAMES: dict[int, str] = {
    linux_ffi.ATSPI_ROLE_PUSH_BUTTON: "Button",
    linux_ffi.ATSPI_ROLE_ENTRY: "Edit",
    linux_ffi.ATSPI_ROLE_TEXT: "Edit",
    linux_ffi.ATSPI_ROLE_PASSWORD_TEXT: "Edit",
    linux_ffi.ATSPI_ROLE_CHECK_BOX: "CheckBox",
    linux_ffi.ATSPI_ROLE_MENU_ITEM: "MenuItem",
    linux_ffi.ATSPI_ROLE_LINK: "Hyperlink",
    linux_ffi.ATSPI_ROLE_LABEL: "Text",
    linux_ffi.ATSPI_ROLE_PANEL: "Pane",
    linux_ffi.ATSPI_ROLE_FILLER: "Pane",
    linux_ffi.ATSPI_ROLE_FRAME: "Window",
    linux_ffi.ATSPI_ROLE_WINDOW: "Window",
    linux_ffi.ATSPI_ROLE_DIALOG: "Dialog",
    linux_ffi.ATSPI_ROLE_APPLICATION: "Application",
}

#: The subrole stamped on a secure record. AT-SPI has no subrole of its own, but
#: ``render.fingerprint`` includes the field, so a stable value is pinned here
#: rather than left empty — a drift check comparing a secure node must have
#: something that does not change between two walks.
_SECURE_SUBROLE = "PasswordText"

#: Human role names elided when they carry no title and no value, the Linux
#: analogue of ``types.ELIDABLE_ROLES`` / ``snapshot_windows.ELIDABLE_WINDOWS_ROLES``.
#: ``Pane`` is what ``PANEL``/``FILLER`` map to — the structural wrappers every
#: toolkit nests several deep.
_ELIDABLE_ROLES: frozenset[str] = frozenset({"Pane"})

#: Fallback when a role int is absent from the table AND ``role_name`` is empty.
#: Distinct from a mapped name so an unmapped role is visible as OUR gap.
_UNKNOWN_ROLE = "Unknown"

#: Roles that accept typed input, and therefore the only roles where an
#: ``editable`` trait is meaningful — the gate that keeps a ``Button`` reporting
#: ``EDITABLE`` just because the state read defaulted. Mirrors
#: ``snapshot_windows._EDITABLE_ROLES``.
_EDITABLE_ROLES: frozenset[str] = frozenset({"Edit"})

_ERR_WINDOW_GONE = "the window for '{app}' is no longer on screen. Call computer_list_apps again"
_ERR_SECURE_TARGET = (
    "refusing to write to element {index} of '{app}': it is a secure (password) "
    "field that masks its value"
)


class SecureTargetError(ComputerUseError):
    """A write was aimed at a field that masks its value.

    Its own type rather than a bare ``ComputerUseError`` so the driver can let it
    reach the model verbatim while still telling it apart from a provider failure —
    the two need different next moves ("this field is off limits" versus "retry").
    Mirrors ``snapshot_windows.SecureTargetError``.
    """


def _role_for(obj: int) -> "tuple[str, bool]":
    """``(human_role_name, secure)`` for one accessible object.

    Secure is role-driven and FAILS CLOSED: ``PASSWORD_TEXT`` is secure, and a role
    that cannot be read at all is treated as secure too, because an unreadable role
    is exactly where a masked field could hide. A readable non-password role maps
    through :data:`_ROLE_NAMES`, falling back to the localised ``role_name()``
    title-cased, then to :data:`_UNKNOWN_ROLE`.
    """
    try:
        role_int = linux_ffi.role(obj)
    except ComputerUseError:
        # Unreadable role fails closed: see the module docstring.
        return _UNKNOWN_ROLE, True
    if role_int == linux_ffi.ATSPI_ROLE_PASSWORD_TEXT:
        return "Edit", True
    name = _ROLE_NAMES.get(role_int)
    if name is not None:
        return name, False
    try:
        localised = linux_ffi.role_name(obj)
    except ComputerUseError:
        localised = ""
    return (localised.title() if localised else _UNKNOWN_ROLE), False


def _clip(text: str, limit: int) -> str:
    """Flatten newlines and clip to *limit*: a record is one tree line."""
    flat = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()
    return flat[:limit] if limit > 0 else flat


def _title(obj: int, limit: int) -> str:
    """The element's name, clipped — never raises (empty on an unreadable name)."""
    try:
        return _clip(linux_ffi.name(obj), limit)
    except ComputerUseError:
        return ""


def _local_frame(
    obj: int, origin: "tuple[float, float, float, float] | None"
) -> "tuple[float, float, float, float] | None":
    """AT-SPI WINDOW extents as a window-local ``(x, y, w, h)``.

    Read in ``ATSPI_COORD_WINDOW`` rather than SCREEN-minus-origin, and the reason is
    measured, not stylistic: GTK 4 (4.16 on X11) answers SCREEN with ``x = y = 0`` for
    EVERY widget, the toplevel included, so a subtraction gave every control the
    window's own origin and the tree carried no positions at all. WINDOW is the
    coordinate type every toolkit implements against its own surface, which is
    exactly the frame contract (:attr:`ElementRec.frame` is window-local).

    **No origin means NO FRAME**, matching the other two platforms: an unlabelled
    coordinate is worse than no coordinate, because a consumer cannot tell a
    window-local ``(12, 40)`` from a screen-absolute one and the difference is the
    whole position of the window. The origin is not subtracted here — it is the
    proof that the window's rect is known, so that ``Snapshot.window_bounds`` can map
    a frame back to the screen. A zero-area or missing-Component rect yields ``None``
    rather than a point a coordinate click could aim at.
    """
    if origin is None:
        return None
    try:
        extents = linux_ffi.extents(obj, linux_ffi.ATSPI_COORD_WINDOW)
    except ComputerUseError:
        return None
    if extents is None:
        return None
    x, y, w, h = extents
    if w <= 0 or h <= 0:
        return None
    return (float(x), float(y), float(w), float(h))


def _states(obj: int) -> frozenset[int]:
    """The element's state set — empty on an unreadable set (never raises)."""
    try:
        return linux_ffi.states(obj)
    except ComputerUseError:
        return frozenset()


def _traits(states: frozenset[int], role: str, *, secure: bool) -> tuple[str, ...]:
    """Trait words for one element, derived from its AT-SPI state set.

    ``editable`` is gated on a text-bearing role for the same reason as on
    Windows: a non-text control that happens to report ``EDITABLE`` would invite a
    wasted ``computer_type_text`` turn. ``selected`` and ``expanded`` come straight
    from their states.

    A secure element contributes NO traits: ``editable`` would confirm the password
    box accepts input, which is the disclosure the value redaction exists to
    prevent — same posture as ``snapshot_windows._traits``.
    """
    if secure:
        return ()
    out: list[str] = []
    if role in _EDITABLE_ROLES and linux_ffi.ATSPI_STATE_EDITABLE in states:
        out.append(TRAIT_EDITABLE)
    if linux_ffi.ATSPI_STATE_SELECTED in states:
        out.append(TRAIT_SELECTED)
    if linux_ffi.ATSPI_STATE_EXPANDED in states:
        out.append(TRAIT_EXPANDED)
    return tuple(out)


def _actions(obj: int, *, secure: bool) -> tuple[str, ...]:
    """The AT-SPI Action-interface action names this element can serve.

    The shipped skill tells the model to read this list before choosing a call, so
    it must reflect what the element actually offers rather than being left empty.
    A secure element advertises NOTHING, for the reason it carries no traits:
    confirming the masked field is interactive is part of what the redaction
    withholds. Never raises — an unreadable Action interface yields ``()``.
    """
    if secure:
        return ()
    try:
        return linux_ffi.actions(obj)
    except ComputerUseError:
        return ()


def _is_elidable(role: str, title: str, value: str, *, secure: bool = False) -> bool:
    """Whether a node is a structural wrapper worth dropping from the numbering.

    **A SECURE node is never elided**, even when it is otherwise an empty wrapper,
    for the exact reason ``snapshot_windows._is_elidable`` keeps one: ``secure``
    flips ``has_secure``, which suppresses the whole-window screenshot, so eliding
    the node as well leaves a window with NO tree and NO pixels — a suppression
    with no visible cause, which reads to the model as an empty window and sends it
    down the skill's "ask the user to type the password" path. Keeping the node
    visible gives the suppression a stated cause and something to address.
    """
    if secure:
        return False
    return role in _ELIDABLE_ROLES and not title and not value


def _value(obj: int, *, secure: bool, text_limit: int = DEFAULT_TEXT_LIMIT) -> str:
    """The element's readable contents: its Text, else its numeric Value, else ``""``.

    This is the ``ElementRec.value`` channel — ``AXValue`` on macOS, the UIA Value
    pattern on Windows — and on AT-SPI it is split across two interfaces. Text comes
    FIRST because it is what a field holds: measured on gnome-calculator after
    ``7 + 8 =``, the result field (role TEXT) carried ``"15"`` in its Text interface
    and nothing in Value, so a Value-only read showed the model an empty display
    after every calculation. Value is the fallback for sliders, spin buttons and
    progress bars, whose reading is numeric and has no Text interface.

    Never read for a secure node (the caller passes ``secure``): the Text interface
    of a password box returns the plaintext, and ``has_secure`` keys every downstream
    protection off this not having happened. Clipped to *text_limit*, the same budget
    the title honours, so a document body cannot cross the seam whole.
    """
    if secure:
        return ""
    try:
        txt = linux_ffi.text(obj, text_limit)
    except ComputerUseError:
        txt = ""
    if txt:
        return txt
    try:
        v = linux_ffi.value(obj)
    except ComputerUseError:
        return ""
    if v is None:
        return ""
    return f"{v:g}"


def _walk(frame: int, req: SnapshotRequest) -> "tuple[list[int], list[int], bool, bool]":
    """Depth-first pre-order over *frame*'s descendants, bounded by the budgets.

    Returns ``(objects, depths, truncated, depth_truncated)``. The frame itself is
    the ROOT and is NOT recorded — the tree the model addresses is its contents,
    the same convention as ``snapshot_windows`` (which walks the root HWND's
    children) and ``snapshot_macos``. The frame's direct children are depth 0, so
    ``depth`` measures nesting WITHIN the window. ITERATIVE by an explicit stack so
    a deep tree cannot ``RecursionError`` inside a ctypes call.

    Every object in the returned list is OWNED by the caller and released by it;
    objects created here but NOT returned (budget-dropped children, non-showing or
    defunct subtrees) are released here. The *frame* argument is never unref'd here
    — the caller owns that reference.

    Three prunings keep the walk to the visible window and bound its cost:

    * a DEFUNCT node and its subtree are skipped — the object is being torn down;
    * a subtree that is not SHOWING is skipped — it is off-screen, which is what
      confines the walk to the on-screen window rather than the whole app tree;
    * ``max_nodes`` sets ``truncated``, ``max_depth`` sets ``depth_truncated``, and
      ``MAX_CHILDREN_PER_NODE`` bounds one pathological container.
    """
    objects: list[int] = []
    depths: list[int] = []
    truncated = False
    depth_truncated = False

    def _children_of(obj: int) -> list[int]:
        try:
            count = min(linux_ffi.child_count(obj), MAX_CHILDREN_PER_NODE)
        except ComputerUseError:
            return []
        out: list[int] = []
        for i in range(count):
            child = linux_ffi.child_at(obj, i)
            if child is not None:
                out.append(child)
        return out

    # Seed the stack with the frame's children at depth 0; the frame is the root
    # and is not itself a record. Push in reverse so they pop in document order.
    stack: list[tuple[int, int]] = [(c, 0) for c in reversed(_children_of(frame))]
    while stack:
        obj, depth = stack.pop()
        states = _states(obj)
        if linux_ffi.ATSPI_STATE_DEFUNCT in states:
            linux_ffi.unref(obj)
            continue
        if linux_ffi.ATSPI_STATE_SHOWING not in states:
            # Off-screen: its subtree is pruned, so the walk stays on the visible
            # window.
            linux_ffi.unref(obj)
            continue
        if len(objects) >= req.max_nodes:
            truncated = True
            linux_ffi.unref(obj)
            continue
        objects.append(obj)
        depths.append(depth)
        if depth + 1 > req.max_depth:
            depth_truncated = True
            continue
        for child in reversed(_children_of(obj)):
            stack.append((child, depth + 1))
    return objects, depths, truncated, depth_truncated


def _release_all(objects: list[int], keep: "int | None" = None) -> None:
    """Release every walked object except *keep* (the one yielded for a write)."""
    for obj in objects:
        if obj != keep:
            linux_ffi.unref(obj)


def _record_for(
    obj: int,
    index: int,
    depth: int,
    req: SnapshotRequest,
    origin: "tuple[float, float, float, float] | None",
) -> "ElementRec | None":
    """One :class:`ElementRec` for a walked object, or ``None`` if it elides.

    The single place role/secure/title/value/traits/actions/frame are derived, so
    :func:`build_snapshot` and :func:`addressed` cannot drift in how they number a
    node. A secure record carries no value, no actions, no frame and no traits, and
    its ``subrole`` is :data:`_SECURE_SUBROLE`. *origin* is the window's screen
    rect — identical for every node of one walk — passed in rather than threaded
    through the stack, exactly as ``snapshot_windows`` passes it to ``_local_frame``.
    """
    role, secure = _role_for(obj)
    title = _title(obj, req.text_limit)
    value = _value(obj, secure=secure, text_limit=req.text_limit)
    if _is_elidable(role, title, value, secure=secure):
        return None
    states = _states(obj)
    enabled = linux_ffi.ATSPI_STATE_SENSITIVE in states and linux_ffi.ATSPI_STATE_ENABLED in states
    return ElementRec(
        index=index,
        role=role,
        subrole=_SECURE_SUBROLE if secure else "",
        title=title,
        value=value,
        actions=_actions(obj, secure=secure),
        depth=depth,
        secure=secure,
        enabled=enabled,
        frame=None if secure else _local_frame(obj, origin),
        traits=_traits(states, role, secure=secure),
        focused=linux_ffi.ATSPI_STATE_FOCUSED in states,
    )


def build_snapshot(app: AppRef, req: SnapshotRequest) -> Snapshot:
    """Walk *app*'s active frame into a :class:`Snapshot`, honouring every budget.

    One AT-SPI application lookup (by pid), one iterative pre-order walk of its
    active frame, then one pass turning objects into records. Role and secure are
    read live per node; a secure node keeps its index but has value/actions/frame
    withheld.

    Raises :class:`ComputerUseError` when the application or its frame is gone,
    which the driver converts into a refusal; every other failure degrades to the
    tree it managed to read.
    """
    app_obj = apps_linux.application_for(app)
    if app_obj is None:
        raise ComputerUseError(_ERR_WINDOW_GONE.format(app=app.bundle_id or app.name))
    frame: "int | None" = None
    objects: list[int] = []
    try:
        frame = apps_linux.active_frame(app_obj)
        if frame is None:
            raise ComputerUseError(_ERR_WINDOW_GONE.format(app=app.bundle_id or app.name))
        try:
            title = linux_ffi.name(frame)
        except ComputerUseError:
            title = app.window_title
        origin = apps_linux.window_bounds(app)

        objects, depths, truncated, depth_truncated = _walk(frame, req)
        records: list[ElementRec] = []
        saw_secure = False
        index = 0
        for obj, depth in zip(objects, depths):
            rec = _record_for(obj, index, depth, req, origin)
            if rec is None:
                continue
            if rec.secure:
                saw_secure = True
            records.append(rec)
            index += 1
    finally:
        _release_all(objects)
        if frame is not None:
            linux_ffi.unref(frame)
        linux_ffi.unref(app_obj)

    return Snapshot(
        app=app,
        elements=tuple(records),
        window_title=title,
        # ``time.monotonic``, never wall clock: a clock adjustment must not make a
        # stale snapshot look fresh.
        captured_at=time.monotonic(),
        truncated=truncated,
        depth_truncated=depth_truncated,
        has_secure=saw_secure,
        window_bounds=origin,
        walk_budget=req,
    )


@contextmanager
def addressed(
    app: AppRef, rec: ElementRec, req: SnapshotRequest, *, for_write: bool = False
) -> "Iterator[int | None]":
    """Yield the live AT-SPI object at ``rec.index``, or ``None``, then release it.

    An action cannot reuse the walk that produced the model's tree — that walk
    released every object it returned — so reaching element 7 means REPRODUCING the
    numbering that produced index 7. This re-walks with the caller's own budget for
    exactly that reason: a different budget yields a different numbering, so element
    7 would be a different control. The index is matched by re-deriving the SAME
    elision and numbering rules :func:`build_snapshot` applies (via the shared
    :func:`_record_for`), not by list position — a wrapper is dropped without
    consuming an index, so position and index are not interchangeable.

    The role AND the title are re-checked against the record. Both are needed and
    the reason is data loss rather than tidiness: a toolbar that swapped Save for
    Delete leaves both as ``Button`` at the same index, so a role-only check accepts
    Delete and the action presses it.

    ``for_write`` re-reads the SECURE verdict off the resolved live object and
    raises :class:`SecureTargetError` on a password/unreadable role. Duplicating the
    chokepoint's secure check here is deliberate: a field can flip plain to password
    on the application's own timer between the walk the model saw and this action,
    and this module is reachable without the chokepoint (a debug ``kirocrew computer
    call``). Fails CLOSED, because :func:`_role_for` does — an unreadable role is
    secure, so an unreadable field cannot get a write through.

    Yields ``None`` when the tree has no such index: the drift check upstream
    normally catches this first, but a UI can change between the check and the
    action, and ``None`` makes that a clean refusal rather than an error inside a
    native call.
    """
    app_obj = apps_linux.application_for(app)
    if app_obj is None:
        raise ComputerUseError(_ERR_WINDOW_GONE.format(app=app.bundle_id or app.name))
    frame: "int | None" = None
    objects: list[int] = []
    found: "int | None" = None
    try:
        frame = apps_linux.active_frame(app_obj)
        if frame is None:
            raise ComputerUseError(_ERR_WINDOW_GONE.format(app=app.bundle_id or app.name))
        objects, depths, _trunc, _dtrunc = _walk(frame, req)
        index = 0
        for obj in objects:
            role, secure = _role_for(obj)
            title = _title(obj, req.text_limit)
            value = _value(obj, secure=secure, text_limit=req.text_limit)
            # ``secure=`` is REQUIRED here: this walk has to reproduce the numbering
            # ``build_snapshot`` produced, and a secure node is KEPT there. Omitting
            # it would drop a node this side only, shifting every later index by one,
            # so ``rec.index`` would resolve to a DIFFERENT control.
            if _is_elidable(role, title, value, secure=secure):
                continue
            if index == rec.index:
                if role != rec.role or title != rec.title:
                    # Role AND title: a swapped-in sibling of the same role at this
                    # index would otherwise be acted on.
                    break
                if for_write and secure:
                    # Read LIVE off this object, not from ``rec``: the field may have
                    # started masking since the model saw it.
                    raise SecureTargetError(
                        _ERR_SECURE_TARGET.format(index=rec.index, app=app.bundle_id or app.name)
                    )
                found = obj
                break
            index += 1
        try:
            yield found
        finally:
            # ``found`` is released by ``_release_all`` along with the rest; it is
            # not AddRef'd because the whole walk is released only after the
            # caller's ``with`` body runs, so the yielded object stays live for it.
            _release_all(objects)
            objects = []
    finally:
        _release_all(objects)
        if frame is not None:
            linux_ffi.unref(frame)
        linux_ffi.unref(app_obj)


def refresh_fingerprints(app: AppRef, req: SnapshotRequest) -> Snapshot:
    """Re-walk *app* for the pre-action drift check.

    A full walk rather than a targeted read: an index only means anything relative
    to a complete walk, so verifying "index 7 is still the Save button" requires
    reproducing the numbering that produced index 7. ``want_image`` is ignored; the
    verification walk never captures pixels.
    """
    return build_snapshot(app, req)


__all__ = [
    "MAX_CHILDREN_PER_NODE",
    "SecureTargetError",
    "addressed",
    "build_snapshot",
    "refresh_fingerprints",
]
