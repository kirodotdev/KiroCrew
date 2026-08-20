"""The Windows UI Automation tree walk: one window into an indexed element tree.

The counterpart of :mod:`snapshot_macos`. Everything platform-specific about
*reaching* the tree lives in :mod:`windows_ffi`; this module owns the shape of
the result — which nodes get an index, what a role is called, and which of the
walk's own limits have to be reported to the caller.

Three properties are inherited from the macOS walk because the layers above
depend on them, not because they are stylistic:

* **The walk is ITERATIVE.** A pathological tree must not ``RecursionError``
  inside a ctypes call, where the failure is not a clean Python exception.
* **Indices are DENSE.** Structural containers that carry no information of their
  own are dropped WITHOUT consuming an index, so the numbering a model addresses
  stays compact. ``index.resolve`` looks a record up by its ``index`` field
  rather than by list position, so the two are not interchangeable.
* **A cut-off walk says so.** ``truncated`` / ``depth_truncated`` are not
  diagnostics: the capture gate refuses a screenshot when either is set, because
  a walk that silently dropped a subtree would let the secure-field scan reflect
  only the nodes it reached — and a password field past the cut would get the
  whole window photographed.

Frames are WINDOW-LOCAL, with ``Snapshot.window_bounds`` publishing the origin
they are relative to. UIA reports a screen rectangle, so this module subtracts
the window origin: a screen-absolute rect could not be related to the screenshot
the model may also be reading (which is a crop of the window), and a
window-local frame survives the user dragging the window between turns.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

from kiro_crew.computer_use import windows_ffi
from kiro_crew.computer_use.types import (
    TRAIT_EDITABLE,
    TRAIT_EXPANDED,
    TRAIT_SELECTED,
    WINDOWS_ACTION_COLLAPSE,
    WINDOWS_ACTION_EXPAND,
    WINDOWS_ACTION_PRESS,
    WINDOWS_ACTION_SELECT,
    WINDOWS_ACTION_TOGGLE,
    AppRef,
    ComputerUseError,
    ElementRec,
    Snapshot,
    SnapshotRequest,
)

logger = logging.getLogger(__name__)

#: Per-node child cap, the Windows twin of ``snapshot_macos.MAX_CHILDREN_PER_NODE``
#: and the same value for the same reason: it bounds ONE pathological container (a
#: data grid with 100k rows) before the global node budget notices, so a single
#: element cannot make one walk materialise a huge list of COM pointers.
#:
#: Declared here rather than imported from the macOS module: importing that module
#: on Windows would pull in the whole Accessibility surface, and the two walks are
#: free to diverge if measurement ever says they should.
MAX_CHILDREN_PER_NODE = 512

#: UIA ``ControlType`` id -> role name. Deliberately UIA's own vocabulary rather
#: than the ``AX*`` spellings: a shared name would imply a shared meaning that
#: does not hold (a UIA ``Pane`` is not an ``AXGroup``), and
#: ``ElementRec.short_role`` already lowercases these for rendering, so a model
#: sees ``button`` / ``edit`` / ``listitem`` on both platforms.
CONTROL_TYPE_ROLES: dict[int, str] = {
    50000: "Button",
    50001: "Calendar",
    50002: "CheckBox",
    50003: "ComboBox",
    50004: "Edit",
    50005: "Hyperlink",
    50006: "Image",
    50007: "ListItem",
    50008: "List",
    50009: "Menu",
    50010: "MenuBar",
    50011: "MenuItem",
    50012: "ProgressBar",
    50013: "RadioButton",
    50014: "ScrollBar",
    50015: "Slider",
    50016: "Spinner",
    50017: "StatusBar",
    50018: "Tab",
    50019: "TabItem",
    50020: "Text",
    50021: "ToolBar",
    50022: "ToolTip",
    50023: "Tree",
    50024: "TreeItem",
    50025: "Custom",
    50026: "Group",
    50027: "Thumb",
    50028: "DataGrid",
    50029: "DataItem",
    50030: "Document",
    50031: "SplitButton",
    50032: "Window",
    50033: "Pane",
    50034: "Header",
    50035: "HeaderItem",
    50036: "Table",
    50037: "TitleBar",
    50038: "Separator",
    50039: "SemanticZoom",
    50040: "AppBar",
}

#: Roles with no information of their own: elided when they carry no name and no
#: value, without consuming an index. The Windows analogue of
#: ``types.ELIDABLE_ROLES``. ``Pane`` and ``Group`` are the structural wrappers
#: every framework nests several deep; ``Custom`` is what a framework reports when
#: it has declined to describe a control at all, so an unnamed one tells a model
#: nothing it can act on.
ELIDABLE_WINDOWS_ROLES: frozenset[str] = frozenset({"Pane", "Group", "Custom", "Thumb"})

#: Fallback when a control type is absent from the table above. Named rather than
#: empty so a tree line is never blank, and distinct from ``Custom`` so an
#: unmapped id is visible as OUR gap rather than the framework's.
_UNKNOWN_ROLE = "Unknown"

#: Roles where an INCONCLUSIVE secure read must fail CLOSED (treated as secure).
#: These are the roles that can hold a MASKED value, plus the roles we cannot
#: identify — the only places a hidden password field can actually be:
#:
#: * ``Edit`` — the classic password box; a WPF ``PasswordBox`` reports as Edit.
#: * ``ComboBox`` / ``Document`` — an editable combo and a rich-edit surface can
#:   both mask input.
#: * ``Custom`` — a custom-drawn control is unknowable, and a framework masking a
#:   value without implementing ``IsPassword`` would appear here, so an
#:   inconclusive read must not be trusted.
#: * ``Unknown`` — an unmapped control type is OUR gap; guessing "that cannot hold
#:   a secret" about a control we failed to identify is the wrong way to fail.
#:
#: A definite ``True`` is honoured for ANY role (see :func:`_secure_for_role`), so
#: this set changes only the INCONCLUSIVE case. It deliberately excludes display
#: roles — ``Text``, ``TreeItem``, ``DataItem`` — whose value IS their visible
#: label rather than a masked secret. That exclusion is what stops an ordinary
#: File Explorer window from suppressing its screenshot: Explorer's tree provider
#: does not implement ``IsPassword``, so 11 of its ``TreeItem`` nodes read
#: inconclusive, and treating those as secure photographed nothing that could
#: leak while blocking a legitimate capture.
_SECURE_QUESTION_APPLIES: frozenset[str] = frozenset(
    {"Edit", "ComboBox", "Document", "Custom", _UNKNOWN_ROLE}
)

#: Roles that accept typed input, and therefore the only roles where an
#: ``editable`` trait is meaningful. A cached ``ValueIsReadOnly`` is ``False`` by
#: DEFAULT on a provider that does not implement ValuePattern, so without this
#: gate a ``Button`` or ``TitleBar`` advertises ``editable``.
_EDITABLE_ROLES: frozenset[str] = frozenset({"Edit", "ComboBox", "Document"})

_ERR_WINDOW_GONE = "the window for '{app}' is no longer on screen. Call computer_list_apps again"
#: Refusal for a write aimed at a masked field. Names the ROLE rather than a
#: subrole: UIA has no subrole, so the role is the most specific thing there is.
_ERR_SECURE_TARGET = (
    "refusing to write to element {index} of '{app}': it is a secure ({role}) field "
    "that masks its value"
)


def _role_for(control_type: object) -> str:
    """Role name for a cached ``ControlType`` value."""
    if isinstance(control_type, int):
        return CONTROL_TYPE_ROLES.get(control_type, _UNKNOWN_ROLE)
    return _UNKNOWN_ROLE


def _text(value: object, limit: int) -> str:
    """A cached string property, clipped to *limit*."""
    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()
    return text[:limit] if limit > 0 else text


def _local_frame(
    rect: object, origin: "tuple[float, float, float, float] | None"
) -> "tuple[float, float, float, float] | None":
    """Convert UIA's SCREEN rect to a window-local ``(x, y, w, h)``.

    UIA hands back ``[left, top, WIDTH, HEIGHT]`` as a SAFEARRAY of doubles —
    verified against ``GetWindowRect`` on a real window; it is NOT
    ``[left, top, right, bottom]``. Reading it as right/bottom and subtracting
    yields a negative height on any element positioned below its parent's origin,
    which the guard below then silently discards, and a plausible-but-wrong size
    on the rest — the exact "worse than no rect" hazard, because a model acts on
    it. A malformed array yields ``None`` rather than a partial rectangle.

    **No origin means NO FRAME**, matching ``snapshot_macos._local_frame``, for the
    two reasons it gives: an unlabelled coordinate is worse than no coordinate,
    because a consumer cannot tell a window-local ``(12, 40)`` from a
    screen-absolute one and the difference is the whole position of the window; and
    the screenshot the model may also be reading is a CROP of this window, so a
    screen-absolute number relates to no pixel it can see. ``window_bounds`` is
    ``None`` exactly when the origin is unavailable (a window closing mid-walk,
    minimized, or UIPI-restricted), so the model would have neither the origin nor
    any way to know the numbers had changed meaning.
    """
    if origin is None:
        return None
    if not isinstance(rect, (list, tuple)) or len(rect) != 4:
        return None
    try:
        left, top, width, height = (float(v) for v in rect)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        # An off-screen or collapsed element. Reporting a zero-area rect would
        # invite a coordinate click at a point that cannot receive one.
        return None
    return (left - origin[0], top - origin[1], width, height)


#: ``Is<Pattern>PatternAvailable`` property -> the ``perform_action`` name it enables.
#: Deliberately the DRIVER's vocabulary rather than UIA's pattern names: this list is
#: what the model reads before choosing a call, so publishing "InvokePattern" would
#: advertise a word ``perform_action`` refuses. ``expand`` and ``collapse`` come from
#: the same property because one pattern serves both directions; which one is
#: applicable is the ``expanded`` trait's job, not this list's.
_PATTERN_ACTIONS: "tuple[tuple[int, tuple[str, ...]], ...]" = (
    (windows_ffi.UIA_IsInvokePatternAvailablePropertyId, (WINDOWS_ACTION_PRESS,)),
    (windows_ffi.UIA_IsTogglePatternAvailablePropertyId, (WINDOWS_ACTION_TOGGLE,)),
    (windows_ffi.UIA_IsSelectionItemPatternAvailablePropertyId, (WINDOWS_ACTION_SELECT,)),
    (
        windows_ffi.UIA_IsExpandCollapsePatternAvailablePropertyId,
        (WINDOWS_ACTION_EXPAND, WINDOWS_ACTION_COLLAPSE),
    ),
)


def _actions(element: object, *, secure: bool) -> tuple[str, ...]:
    """The ``perform_action`` names this element can actually serve.

    The shipped computer-use skill tells the model to read this list before choosing
    a call, so leaving it empty made that channel dead on Windows while it worked on
    macOS: the model had to attempt a verb to discover whether the element supports
    it, and a refusal is indistinguishable from a wrong element.

    Sourced from the cached ``Is<Pattern>PatternAvailable`` properties, NOT from a
    live ``GetCurrentPattern`` probe — that is a cross-process round trip per element,
    which is the cost the cached walk exists to avoid, while these ride along in the
    walk that already happened.

    Read TRI-STATE, only a definite ``True``: a provider that does not implement the
    property answers the reserved not-supported sentinel, and treating that as False
    is harmless while treating it as True would advertise a verb the element refuses.

    A secure element advertises NOTHING, for the same reason it carries no traits:
    ``press`` on a masked field would confirm the control is interactive, which is
    part of what the redaction exists to withhold.
    """
    if secure:
        return ()
    out: list[str] = []
    for prop_id, names in _PATTERN_ACTIONS:
        if windows_ffi.cached_prop(element, prop_id) is True:
            out.extend(names)
    return tuple(out)


def _traits(element: object, role: str, *, secure: bool) -> tuple[str, ...]:
    """Trait words for one element.

    ``editable`` comes from ``ValueIsReadOnly`` rather than ``IsEnabled``, because
    those answer different questions: a read-only text field reports enabled with a
    readable value, so a model would type into it, get an ``ok`` result, and watch
    the text go nowhere. Settability is the only signal that separates them.

    But the read is GATED on a text-bearing role. A cached ``ValueIsReadOnly`` read
    folds "this provider does not implement ValuePattern" into its default of
    ``False`` — the same default hazard the secure read avoids with the ``Ex``
    form — so a ``TitleBar`` or a ``Button`` reads as ``editable`` and a model
    wastes a turn on ``computer_type_text``. Restricting the trait to roles that
    actually accept typed input turns that false positive off, and the role is
    already the module's authority for every other value-sensitive decision.

    ``selected`` comes from ``SelectionItem.IsSelected``, NOT from
    ``HasKeyboardFocus``. ``types.TRAIT_SELECTED`` means list/table selection — what
    the operator has highlighted — and focus is already reported separately as
    ``ElementRec.focused`` and the ``<focused>`` marker. Deriving it from focus
    published one signal twice under two names while a genuinely selected but
    unfocused row carried no trait at all, so "which row is selected?" always named
    the element holding the caret.

    ``expanded`` comes from ``ExpandCollapseState``, the counterpart of the macOS
    walk's ``AXDisclosing`` read — without it a model cannot tell an open tree node
    from a closed one and spends a turn expanding something already open. Only an
    explicit ``Expanded`` publishes it: ``PartiallyExpanded`` is not open, and
    ``LeafNode`` means the control cannot expand at all, so either would misdescribe
    what a collapse would do.

    A secure element contributes NO traits: ``editable`` would confirm the
    password box accepts input, which is the same disclosure the value redaction
    exists to prevent.
    """
    if secure:
        return ()
    out: list[str] = []
    if role in _EDITABLE_ROLES:
        read_only = windows_ffi.cached_prop(element, windows_ffi.UIA_ValueIsReadOnlyPropertyId)
        if read_only is False:
            out.append(TRAIT_EDITABLE)
    selected = windows_ffi.cached_prop(element, windows_ffi.UIA_SelectionItemIsSelectedPropertyId)
    # Only a definite True: a provider without SelectionItemPattern reads as the
    # property's default, and the tri-state check is what keeps that from becoming a
    # trait on every node.
    if selected is True:
        out.append(TRAIT_SELECTED)
    state = windows_ffi.cached_prop(
        element, windows_ffi.UIA_ExpandCollapseExpandCollapseStatePropertyId
    )
    # Only ``Expanded``. ``PartiallyExpanded`` is not "open" (a tree node whose
    # children are partly realized), and ``LeafNode`` means the control cannot expand
    # at all, so publishing either would tell the model something untrue about what a
    # collapse would do. A provider without the pattern answers the not-supported
    # sentinel, which ``cached_prop`` surfaces as neither state.
    if state == windows_ffi.ExpandCollapseState_Expanded:
        out.append(TRAIT_EXPANDED)
    return tuple(out)


def _secure_for_role(element: object, role: str) -> bool:
    """The secure verdict for one element, narrowed by whether it can hold text.

    ``windows_ffi.is_secure_element`` fails CLOSED, which is right for anything
    that could hold a secret and wrong for a tree item: a provider that does not
    implement ``IsPassword`` makes every one of its nodes inconclusive, and
    treating those as secure suppressed the screenshot of an ordinary File Explorer
    window (13 of 87 nodes returned the not-supported sentinel).

    So the strict rule applies to text-bearing roles and the rest resolve to plain
    unless the provider says otherwise. A definite True is still honoured for ANY
    role — a framework that reports a masked non-text control is believed — so this
    narrows only the inconclusive case, and only where there is no value to leak.

    **The narrowing covers "the provider does not implement this", NOT "the read
    failed".** Those are different answers and only the first is evidence of
    anything. A provider that answers with the reserved not-supported sentinel has
    told us it has no opinion, and on a display role whose value is its visible
    label that resolves plain. A read that RAISES, or returns a failure
    ``HRESULT``, or returns an unexpected type has told us nothing at all — a dead
    provider, a UIPI-blocked elevated window, an element torn down mid-relayout —
    so it falls back to the same fail-CLOSED posture
    :func:`windows_ffi.is_secure_element` takes. Recording a hard failure as
    "definitely not secure" would photograph a masked field's rendered glyphs with
    a debug line as the only trace.
    """
    if role in _SECURE_QUESTION_APPLIES:
        return windows_ffi.is_secure_element(element)
    try:
        hr, vt, value = windows_ffi.prop_value_ex(element, windows_ffi.UIA_IsPasswordPropertyId)
    except Exception:
        logger.debug("secure-flag read raised on role %s; treating as secure", role, exc_info=True)
        return True
    if hr != windows_ffi.S_OK:
        # Not the unsupported answer — that arrives as S_OK plus the sentinel.
        # This is the provider failing, which is not a safety guarantee.
        return True
    if vt == windows_ffi.VT_UNKNOWN:
        # The reserved not-supported sentinel: the provider has no opinion, and on
        # a display role there is no masked value to protect. THIS is the case the
        # role narrowing exists for.
        return False
    if vt != windows_ffi.VT_BOOL:
        # A type we cannot interpret is not a False.
        return True
    return bool(value)


def _is_elidable(role: str, title: str, value: str, *, secure: bool = False) -> bool:
    """Whether a node is a structural wrapper worth dropping from the numbering.

    **A SECURE node is never elided**, even when it is otherwise an empty wrapper, and
    that is the difference between a stated refusal and a silent dead end. ``secure``
    flips ``has_secure``, which suppresses the whole-window screenshot; eliding the
    node as well left a window with NO tree and NO pixels, explained only as "this
    window contains a secure (password) field".

    That combination is reachable without any password at all. ``Custom`` is both an
    elidable role and one where an INCONCLUSIVE secure read fails closed, so a
    provider that does not implement ``IsPassword`` — a bridge-less Java or Qt app, a
    custom-drawn game canvas — produces exactly it: reproduced with a single unnamed
    ``Custom`` node over the not-supported sentinel, giving ``elements=0`` and
    ``has_secure=True``. The model is then told a game canvas holds a password, and
    the skill's documented response to that message is to ask the user to type it
    themselves, so it cannot recover.

    Keeping the node visible fixes the honesty without weakening the floor: the
    fail-closed read is unchanged and the screenshot is still suppressed (an unnamed
    ``Custom`` with no ``IsPassword`` genuinely is indistinguishable from a masked
    field), but the tree now shows a ``<secure>`` node, so the suppression has a
    visible cause and the model can address a parent or child instead of concluding
    the window is empty.
    """
    if secure:
        return False
    return role in ELIDABLE_WINDOWS_ROLES and not title and not value


class SecureTargetError(ComputerUseError):
    """A write was aimed at a field that masks its value.

    Its own type rather than a bare ``ComputerUseError`` so the driver can let it
    reach the model verbatim while still distinguishing it from a provider failure —
    the two need different next moves ("this field is off limits" versus "retry").
    """


@contextmanager
def addressed(
    app: AppRef, rec: ElementRec, req: SnapshotRequest, *, for_write: bool = False
) -> "Iterator[Any | None]":
    """Yield the live UIA element at ``rec.index``, or ``None``, then release it.

    ``for_write`` re-reads the SECURE flag off the resolved element and raises
    :class:`SecureTargetError` when it is masked. That check exists at the dispatch
    chokepoint too, and duplicating it here is deliberate on both counts:

    * the chokepoint judges the ``ElementRec`` from the walk the MODEL was shown, and
      a field can flip plain to password on the application's own timer in between —
      so that record can honestly report ``secure=False`` over a control that now
      masks its value. This read is live, against the element about to be written;
    * this module is reachable without the chokepoint (``kirocrew computer call`` is
      a debug harness, and a future caller need not route through ``tools._perform``),
      and a floor that exists at exactly one call site is not a floor.

    Fails CLOSED, because :func:`_secure_for_role` does: a read that fails or a
    provider that will not answer is treated as secure, so an unreadable field cannot
    be used to get a write through.

    An action cannot reuse the walk that produced the model's tree — that walk
    released every reference it returned — so reaching element 7 means REPRODUCING
    the numbering that produced index 7. This walks with the caller's own budget for
    exactly that reason: a different budget yields a different numbering, so element
    7 would be a different control.

    The index is matched by **re-deriving the same elision and numbering rules**
    :func:`build_snapshot` applies, rather than by list position. A structural
    wrapper is dropped WITHOUT consuming an index there, so position and index are
    not interchangeable and indexing the raw walk would address the wrong node.

    Yields ``None`` when the tree no longer has that index — the drift check upstream
    normally catches this first, but a UI can change between the check and the
    action, and yielding ``None`` makes that a clean refusal rather than an
    IndexError inside a ctypes call.

    The role AND the name are re-derived and compared. Both are needed, and the
    reason is data loss rather than tidiness: a toolbar that swapped Save for Delete
    leaves both as ``Button`` at the same index, so a role-only check accepts Delete
    and the action presses it. The name is what separates them, and it is already
    computed here for the elision test.
    """
    with windows_ffi.dpi_awareness_scope():
        if not windows_ffi.window_is_live(app.window_id):
            raise ComputerUseError(_ERR_WINDOW_GONE.format(app=app.bundle_id or app.name))
        root = windows_ffi.element_from_hwnd(app.window_id)
        with windows_ffi.owned(root):
            cache = windows_ffi.create_cache_request(scope=windows_ffi.TreeScope_Children)
            with windows_ffi.owned(cache):
                walk = windows_ffi.walk_bounded(
                    root,
                    cache,
                    max_nodes=req.max_nodes,
                    max_depth=req.max_depth,
                    max_children_per_node=MAX_CHILDREN_PER_NODE,
                )
                found: Any = None
                try:
                    index = 0
                    for element in walk.elements:
                        role = _role_for(
                            windows_ffi.cached_prop(element, windows_ffi.UIA_ControlTypePropertyId)
                        )
                        secure = _secure_for_role(element, role)
                        name = _text(
                            windows_ffi.cached_prop(element, windows_ffi.UIA_NamePropertyId),
                            req.text_limit,
                        )
                        value = (
                            ""
                            if secure
                            else _text(
                                windows_ffi.cached_prop(
                                    element, windows_ffi.UIA_ValueValuePropertyId
                                ),
                                req.text_limit,
                            )
                        )
                        # ``secure=`` is REQUIRED here, not optional: this walk has to
                        # reproduce the numbering ``build_snapshot`` produced, and a
                        # secure node is kept there. Omitting it drops a node this side
                        # only, shifting every later index by one — so ``rec.index``
                        # would resolve to a DIFFERENT control, and the role+name check
                        # below is the only thing that would notice.
                        if _is_elidable(role, name, value, secure=secure):
                            continue
                        if index == rec.index:
                            if role != rec.role or name != rec.title:
                                # Role AND name: a toolbar that swapped Save for
                                # Delete leaves both as ``Button`` at this index, so a
                                # role-only check would accept Delete and press it.
                                break
                            if for_write and secure:
                                # Read LIVE off this element, not from ``rec``: the
                                # field may have started masking since the model saw
                                # it. Raised rather than yielding None so it cannot
                                # be confused with "the element is gone".
                                raise SecureTargetError(
                                    _ERR_SECURE_TARGET.format(
                                        index=rec.index,
                                        app=app.bundle_id or app.name,
                                        role=role,
                                    )
                                )
                            # AddRef so the yielded reference outlives the walk's
                            # own release in the ``finally`` below. ``add_ref``
                            # returns None, so the pointer itself is what we keep.
                            windows_ffi.add_ref(element)
                            found = element
                            break
                        index += 1
                    try:
                        yield found
                    finally:
                        windows_ffi.release(found)
                finally:
                    windows_ffi.release_all(walk.elements)


def build_snapshot(app: AppRef, req: SnapshotRequest) -> Snapshot:
    """Walk *app*'s window into a :class:`Snapshot`, honouring every budget.

    One bounded cached walk (see :func:`windows_ffi.walk_bounded` for why the
    unbounded subtree build is unusable here), then one pass turning elements into
    records. The secure flag is read LIVE per element rather than from the cache,
    because a field can flip plain to password on the application's own timer and
    a cached verdict would report False over the cleartext.

    Raises :class:`ComputerUseError` when the window is gone, which the driver
    converts into a refusal; every other failure degrades to the tree it managed
    to read.
    """
    with windows_ffi.dpi_awareness_scope():
        if not windows_ffi.window_is_live(app.window_id):
            raise ComputerUseError(_ERR_WINDOW_GONE.format(app=app.bundle_id or app.name))
        origin = windows_ffi.window_bounds(app.window_id)
        title = windows_ffi.window_text(app.window_id) or app.window_title

        root = windows_ffi.element_from_hwnd(app.window_id)
        records: list[ElementRec] = []
        saw_secure = False
        with windows_ffi.owned(root):
            cache = windows_ffi.create_cache_request(scope=windows_ffi.TreeScope_Children)
            with windows_ffi.owned(cache):
                walk = windows_ffi.walk_bounded(
                    root,
                    cache,
                    max_nodes=req.max_nodes,
                    max_depth=req.max_depth,
                    max_children_per_node=MAX_CHILDREN_PER_NODE,
                )
                try:
                    index = 0
                    for element, depth in zip(walk.elements, walk.depths):
                        role = _role_for(
                            windows_ffi.cached_prop(element, windows_ffi.UIA_ControlTypePropertyId)
                        )
                        # LIVE, never cached: see the module docstring and
                        # ``windows_ffi.is_secure_element``.
                        secure = _secure_for_role(element, role)
                        if secure:
                            saw_secure = True
                        name = _text(
                            windows_ffi.cached_prop(element, windows_ffi.UIA_NamePropertyId),
                            req.text_limit,
                        )
                        value = (
                            ""
                            if secure
                            else _text(
                                windows_ffi.cached_prop(
                                    element, windows_ffi.UIA_ValueValuePropertyId
                                ),
                                req.text_limit,
                            )
                        )
                        if _is_elidable(role, name, value, secure=secure):
                            # Dropped WITHOUT consuming an index, so the numbering
                            # the model addresses stays dense. A secure node is never
                            # dropped — see :func:`_is_elidable`.
                            continue
                        enabled = windows_ffi.cached_prop(
                            element, windows_ffi.UIA_IsEnabledPropertyId
                        )
                        records.append(
                            ElementRec(
                                index=index,
                                role=role,
                                # UIA has no subrole. Left empty rather than
                                # synthesized: ``render.fingerprint`` includes it, so
                                # a fabricated value would make drift detection
                                # depend on our own invention.
                                subrole="",
                                title=name,
                                value=value,
                                actions=_actions(element, secure=secure),
                                depth=depth,
                                secure=secure,
                                enabled=enabled is not False,
                                frame=(
                                    None
                                    if secure
                                    else _local_frame(
                                        windows_ffi.cached_prop(
                                            element,
                                            windows_ffi.UIA_BoundingRectanglePropertyId,
                                        ),
                                        origin,
                                    )
                                ),
                                traits=_traits(element, role, secure=secure),
                                focused=windows_ffi.cached_prop(
                                    element, windows_ffi.UIA_HasKeyboardFocusPropertyId
                                )
                                is True,
                            )
                        )
                        index += 1
                finally:
                    windows_ffi.release_all(walk.elements)

    return Snapshot(
        app=app,
        elements=tuple(records),
        window_title=title,
        # ``time.monotonic``, never wall clock: a clock adjustment must not make a
        # stale snapshot look fresh.
        captured_at=time.monotonic(),
        truncated=walk.truncated,
        depth_truncated=walk.depth_truncated,
        has_secure=saw_secure,
        window_bounds=origin,
        walk_budget=req,
    )


def refresh_fingerprints(app: AppRef, req: SnapshotRequest) -> Snapshot:
    """Re-walk *app* for the pre-action drift check.

    A full walk rather than a targeted read: an index only means anything relative
    to a complete walk, so verifying "index 7 is still the Save button" requires
    reproducing the numbering that produced index 7.

    This is the expensive half of a mutating action on Windows — the tree is
    walked twice per action, and a cached walk of a large Chromium window is
    hundreds of milliseconds even at the node budget. ``want_image`` is ignored;
    the verification walk never captures pixels.
    """
    return build_snapshot(app, req)


__all__ = [
    "CONTROL_TYPE_ROLES",
    "SecureTargetError",
    "MAX_CHILDREN_PER_NODE",
    "ELIDABLE_WINDOWS_ROLES",
    "addressed",
    "build_snapshot",
    "refresh_fingerprints",
]
