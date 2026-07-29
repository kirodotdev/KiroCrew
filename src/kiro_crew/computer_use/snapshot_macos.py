"""The accessibility walk: one application window into an indexed element list.

Two things here carry real weight.

**The walk is ITERATIVE, with an explicit stack.** Not a style choice. A recursive
walk over a pathological tree raises ``RecursionError``, and Python's recursion
guard is not reliable when the frames in question are interleaved with ctypes
calls into another process's accessibility server — the failure mode there is a C
stack overflow, which is a hard crash rather than an exception. An explicit stack
makes depth a data bound instead of a stack bound.

**``secure`` is derived from BOTH ``AXRole`` and ``AXSubrole``.** A real macOS
password box reports ``AXRole='AXTextField'`` — completely innocuous — with
``AXSubrole='AXSecureTextField'`` and a **readable** ``AXValue``. A role-only
check therefore misses EVERY password field, and three separate protections key
off this one flag: value redaction in the renderer, input refusal in
:mod:`policy`, and whole-window screenshot suppression. Getting it wrong defeats
all three at once, which is why the derivation lives in one function
(:func:`_is_secure`) that a test pins directly.

Electron/Chromium apps (Slack, VS Code, Obsidian, and KiroCrew's own desktop app)
need ``AXManualAccessibility`` before they expose anything useful, and the symptom
has two shapes — both reproduced live:

* a hard ``kAXErrorCannotComplete`` (-25204) for every attribute read (Chrome);
* a *successful* read that returns a 13-node stub (Obsidian, before the opt-in;
  574 nodes after it).

So the retry triggers on either an error OR an implausibly small tree, which is
the only way to catch the second shape.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from kiro_crew.computer_use import macos_ffi
from kiro_crew.computer_use.types import (
    AX_CHILD_ATTRIBUTES,
    AX_CHILDREN,
    AX_DESCRIPTION,
    AX_ENABLED,
    AX_EXPANDED,
    AX_FOCUSED_UI_ELEMENT,
    AX_ROLE,
    AX_ROWS,
    AX_SELECTED,
    AX_SELECTED_TEXT,
    AX_SUBROLE,
    AX_TITLE,
    AX_VALUE,
    AX_VISIBLE_CHILDREN,
    AX_WINDOWS,
    ELIDABLE_ROLES,
    MAX_TREE_DEPTH_LIMIT,
    MAX_TREE_NODES_LIMIT,
    ROW_BEARING_ROLES,
    SECURE_SUBROLE,
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

# The window attribute a walk starts from. ``AXWindows`` (all windows) rather than
# ``AXFocusedWindow``: the focused window is often NULL for a background app —
# exactly the case computer use exists for.
WINDOW_ATTRIBUTE = AX_WINDOWS
# ``AXMain``: the app's own notion of its primary window. The tie-breaker when the
# CG window id cannot be correlated.
AX_MAIN = "AXMain"
# How many AX windows are examined while looking for the one the window list
# picked. Bounded so an app with a hundred open windows cannot make the
# correlation scan expensive; the target is almost always in the first few.
MAX_WINDOWS_TO_INSPECT = 16
# U+2026, the character CoreGraphics uses when it elides a long window title.
TITLE_ELLIPSIS = "…"
# Shortest title prefix trusted to identify a window. Below this a match is more
# likely to be coincidence than identity, and the ``AXMain`` fallback is better
# than a confident mismatch.
MIN_TITLE_STEM_LEN = 8

# A tree this small from an app with a visible window is the Electron
# "accessibility not yet enabled" signature, not a genuinely simple app: Obsidian
# reported 13 nodes before the opt-in and 574 after. Native apps that really are
# this small (a bare alert) simply pay one bounded retry.
ELECTRON_STUB_NODE_THRESHOLD = 24

# Per-node child cap. Bounds one pathological container (a table with 100k rows)
# before the global node budget notices, so a single element cannot make one walk
# materialise a huge Python list of CF pointers.
MAX_CHILDREN_PER_NODE = 512
# Cap on advertised action names read per element. Advisory metadata; a handful is
# the norm and the cap keeps a hostile element from making the walk quadratic.
MAX_ACTIONS_PER_NODE = 16
# AGGREGATE wall-clock budget for one walk, and the reason the node/depth budgets
# are not sufficient on their own: ``AX_MESSAGING_TIMEOUT_SECS`` is a PER-CALL
# bound, and a walk makes ~5 AX calls per node across up to
# ``MAX_TREE_NODES_LIMIT`` nodes — so a partially-wedged app whose every read
# takes the full 2s timeout could park a worker thread for tens of minutes while
# never violating a single per-call timeout. This deadline is the only thing that
# bounds the total. Generous relative to reality (measured: Slack 506 nodes in
# 0.025s, Chrome 1475 in 0.07s — three orders of magnitude of headroom), so a
# healthy app can never hit it; it exists purely to cut a pathological one loose.
# Kept here beside the other walk budgets rather than in ``types.py`` because it
# bounds this walk implementation, not the request schema the MCP layer validates.
MAX_WALK_SECS = 10.0

# Attributes read for every node, in the order the record needs them.
_TEXT_ATTRIBUTES: tuple[str, ...] = (AX_TITLE, AX_VALUE, AX_DESCRIPTION)


@dataclass(frozen=True)
class WalkStats:
    """Diagnostics for one walk — visited count and which budget stopped it.

    ``saw_secure`` is deliberately SEPARATE from "a secure record is present in the
    returned elements". Screenshot suppression must not depend on the REPORTING
    budget: a password field sitting past ``max_nodes`` (a login form whose secure
    input is the last child, or any Chromium window — measured at 1431-1475 nodes
    against a 1200 default) would otherwise leave ``has_secure`` False and the
    whole window's pixels would be captured and relayed. Worse, the MCP schema lets
    the model CHOOSE ``max_tree_nodes``, so it could pick a budget that hides the
    field. This flag is set for every node the walk classified, whether or not it
    made the cut.
    """

    visited: int = 0
    truncated: bool = False
    depth_truncated: bool = False
    saw_secure: bool = False
    # The aggregate :data:`MAX_WALK_SECS` deadline stopped this walk. Reported
    # separately from ``truncated`` (which it also sets, because the tree really is
    # incomplete) so a diagnostic can tell "this app is wedged" apart from "the
    # model asked for a small budget".
    time_truncated: bool = False


def build_snapshot(app: AppRef, req: SnapshotRequest) -> Snapshot:
    """Walk *app*'s frontmost window into a :class:`Snapshot`.

    Order of operations, and every step is load-bearing:

    1. create the application element — which applies the PER-CALL messaging
       timeout inside :func:`macos_ffi.ax_app_element`, immediately and
       unconditionally. Per-call is the important word: it bounds one accessibility
       round-trip, NOT this function, which makes thousands of them. The aggregate
       bound on a walk is :data:`MAX_WALK_SECS`, enforced inside :func:`_walk`;
    2. read ``AXWindows``;
    3. on ``-25204`` **or** an implausibly small tree, set
       ``AXManualAccessibility``, poll for up to
       :data:`macos_ffi.ELECTRON_OPT_IN_WAIT_SECS` in
       :data:`macos_ffi.ELECTRON_OPT_IN_POLL_SECS` steps, and retry **once**;
    4. still nothing usable -> raise :class:`ComputerUseError` naming the RAW AX
       code, because that number is the only thing that makes a support thread
       diagnosable.

    The image is NOT captured here — :mod:`capture_macos` owns that, and the
    caller suppresses it when the tree contains a secure field. Separating them is
    what makes "any secure node -> no pixels at all" enforceable in one place
    rather than as an argument threaded into the walk.

    Every CoreFoundation reference this function creates is released before it
    returns — the application element, the window element, and every element the
    walk touched. ``AXUIElementCreateApplication`` and
    ``AXUIElementCopyAttributeValue`` are both Create-Rule calls, and skipping the
    releases measured at ~400KB of permanent growth per walk.
    """
    with macos_ffi.ax_application(app.pid) as app_elem:
        if not app_elem:
            raise ComputerUseError(
                f"could not open an accessibility connection to '{app.name}' (pid {app.pid}); "
                "the process may have exited"
            )

        window, err = _frontmost_window(
            app_elem, window_id=app.window_id, window_title=app.window_title
        )
        elements: tuple[ElementRec, ...] = ()
        stats = WalkStats()
        selected_text = ""
        # Read ONCE per snapshot, then passed DOWN into the walk. Both answer a
        # question that has one answer for the whole tree, so reading either
        # per-node would add an AX round-trip to every one of up to ~1,400 nodes.
        # The focused element is a +1 reference, hence the context manager.
        with _focused_element(app_elem) as focused:
            bounds = _window_bounds(window)
            try:
                if window is not None:
                    elements, stats, _ = _walk(window, req, window_bounds=bounds, focused=focused)

                if _needs_manual_accessibility(window, err, stats):
                    retried, err, elements, stats = _retry_with_manual_accessibility(
                        app_elem, app, req, err, focused=focused
                    )
                    if retried is not window:
                        # The retry selected a different window element; release the
                        # one we are dropping so the +1 from _frontmost_window is not
                        # leaked.
                        macos_ffi.release_all([window] if window is not None else [])
                        window = retried
                    # Re-read even when the SAME element came back: the retry's walk
                    # framed against the bounds IT read, and the reported
                    # ``window_bounds`` has to be the origin those frames are
                    # actually relative to. Electron windows resize as they finish
                    # loading, so this is not hypothetical.
                    bounds = _window_bounds(window)

                if window is None:
                    raise ComputerUseError(
                        f"'{app.name}' (pid {app.pid}) exposed no accessible window "
                        f"(AX error {err}). Electron apps may need a moment after launch."
                    )

                selected_text = _selected_text(focused, text_limit=req.text_limit)

                return Snapshot(
                    app=app,
                    elements=elements,
                    window_title=macos_ffi.ax_str(window, AX_TITLE),
                    # monotonic, never wall clock: a clock adjustment must not be able
                    # to make a stale snapshot look fresh (or a fresh one look expired).
                    captured_at=time.monotonic(),
                    truncated=stats.truncated,
                    depth_truncated=stats.depth_truncated,
                    # Independent of the REPORTING budget: ``stats.saw_secure`` covers
                    # every node the walk classified, including those the node budget
                    # dropped and those below ``max_depth``. Without it a password field
                    # past ``max_nodes`` left ``has_secure`` False and the whole
                    # window's pixels were captured — and the model chooses
                    # ``max_tree_nodes`` itself, so it could pick a budget that hid the
                    # field. The ``elements`` term is redundant but kept as a cheap,
                    # obvious local check.
                    has_secure=any(rec.secure for rec in elements) or stats.saw_secure,
                    window_bounds=bounds,
                    selected_text=selected_text,
                )
            finally:
                macos_ffi.release_all([window] if window is not None else [])


def _frontmost_window(
    app_elem: object, *, window_id: int = 0, window_title: str = ""
) -> "tuple[object | None, int]":
    """``(window_element_or_None, ax_error)`` for the window we mean to observe.

    The returned window is a **+1 reference the caller must release**
    (:func:`macos_ffi.ax_retained_elements` retains, because the AX children of a
    ``Copy``-returned array die with the array — using one afterwards would be a
    use-after-free in another process's accessibility client, i.e. a crash).

    **Not simply ``AXWindows[0]``.** ``AXWindows`` and the CoreGraphics window list
    are INDEPENDENTLY ordered, and mixing them up is a silent wrong-observation
    bug: verified live on Notes, ``AXWindows[0]`` was an ``AXDialog`` with an empty
    title exposing 3 nodes while the frontmost CG window — the one
    :mod:`capture_macos` would have photographed — was the real 103-node document
    window at ``AXWindows[1]``. The model would have received a 3-node tree
    alongside a screenshot of a completely different window and had no way to tell.

    Selection order:

    1. the AX window whose ``_AXUIElementGetWindow`` id equals *window_id* — the
       authoritative correlation, since the window id is what the capture uses;
    2. a PREFIX match against *window_title* (the private symbol may be absent on
       a future macOS). Prefix, not equality: the two namespaces report different
       strings for the same window — verified, CoreGraphics truncates with an
       ellipsis (``'fix(ci): keep GHCR package priv…est #621'``) while AX returns
       the full title plus an app-name suffix (``'… - Google Chrome'``), and
       CoreGraphics said ``'All on My Mac'`` where AX said
       ``'All on My Mac – 6 notes'``. An equality test would never match either;
    3. the window the app itself marks ``AXMain``;
    4. ``AXWindows[0]``, as a last resort.
    """
    windows = macos_ffi.ax_retained_elements(
        app_elem, WINDOW_ATTRIBUTE, limit=MAX_WINDOWS_TO_INSPECT
    )
    if not windows:
        # ``ax_retained_elements`` returns [] both for an AX error and for an app
        # with no windows; re-read the error only in that case so the caller can
        # distinguish "-25204, needs the Electron opt-in" from "no windows open".
        _, err = macos_ffi.ax_attr_error(app_elem, WINDOW_ATTRIBUTE)
        return None, err

    chosen = _select_window(windows, window_id, window_title)
    # Release every window we did NOT choose: each came back +1.
    macos_ffi.release_all([w for w in windows if w is not chosen])
    return chosen, macos_ffi.AX_ERR_SUCCESS


def _select_window(windows: list, window_id: int, window_title: str) -> object:
    """Pick the window matching *window_id* / *window_title*; see the caller."""
    if window_id > 0:
        for window in windows:
            if macos_ffi.ax_window_id(window) == window_id:
                return window
    stem = _title_stem(window_title)
    if stem:
        for window in windows:
            if macos_ffi.ax_str(window, AX_TITLE).startswith(stem):
                return window
    for window in windows:
        if macos_ffi.ax_bool(window, AX_MAIN, default=False):
            return window
    return windows[0]


def _title_stem(window_title: str) -> str:
    """The leading, un-truncated part of a CoreGraphics window title.

    CoreGraphics elides the middle of a long title with U+2026 (verified:
    ``'fix(ci): keep GHCR package priv…est #621 · kirodotdev/KiroCrew'``), so only
    the text BEFORE the ellipsis is a reliable prefix of the AX title. Returns
    ``""`` for a stem too short to identify a window — a 3-character prefix would
    match the wrong window more often than the right one, and the ``AXMain``
    fallback is better than a confident mismatch.
    """
    stem = (window_title or "").split(TITLE_ELLIPSIS, 1)[0].strip()
    return stem if len(stem) >= MIN_TITLE_STEM_LEN else ""


def _child_attribute_order(role: str) -> tuple[str, ...]:
    """The child collections to read for *role*, in merge order.

    For a :data:`ROW_BEARING_ROLES` container (a table, outline, list or browser)
    the row collections come FIRST. Order decides which children get the low
    element indices, and a model addresses the early ones: putting a spreadsheet's
    rows behind its own header/scrollbar scaffolding buries the content the model
    is actually looking for, and on a truncated walk can drop it entirely.

    Every other role keeps ``AXChildren`` first — that is where its content really
    is, and the alternates are then pure additive coverage.
    """
    if role in ROW_BEARING_ROLES:
        return (AX_ROWS, AX_VISIBLE_CHILDREN, AX_CHILDREN)
    return AX_CHILD_ATTRIBUTES


def _dedup_against(seen: list, found: list) -> list:
    """Entries of *found* not already in *seen*, capped, releasing the rejects.

    Identity comparison via ``CFEqual`` — see ``read_children``. Rejected entries
    are released here rather than returned, because they are +1 references the walk
    owns and nothing downstream will ever see them again.

    Only reached when a node genuinely exposes children in more than one
    collection, which is the uncommon case; the single-collection path skips this
    scan entirely.
    """
    kept: list = []
    for child in found:
        if len(seen) + len(kept) >= MAX_CHILDREN_PER_NODE or any(
            macos_ffi.same_element(child, other) for other in (*seen, *kept)
        ):
            macos_ffi.release_all([child])
            continue
        kept.append(child)
    return kept


def _window_bounds(
    window: "object | None",
) -> "tuple[float, float, float, float] | None":
    """The window element's own screen rect, or ``None``.

    Read from the AX element rather than correlated back through the CoreGraphics
    window list, for two reasons: it is the SAME source the element frames come
    from (so a subtraction between them cannot mix two coordinate systems that
    disagree by a title-bar height), and it needs no window id — which the
    Electron retry path may not have yet.

    ``None`` is an ordinary answer: some window elements answer ``-25205`` for
    ``AXPosition``. Every frame then reports ``None`` too, which is the correct
    degradation — see :func:`_local_frame`.
    """
    if window is None:
        return None
    return macos_ffi.ax_frame(window)


@contextmanager
def _focused_element(app_elem: object) -> Iterator[object]:
    """Yield the app's ``AXFocusedUIElement`` (+1, released on exit), or ``None``.

    Read off the APPLICATION element, not the system-wide one: the system-wide
    focus follows whatever the operator is actually working in, so using it would
    mark an element of a background app as focused whenever the target app happens
    to be frontmost — and report nothing at all the rest of the time. The
    app-scoped attribute answers "where is this app's caret", which is the question
    the model is asking, and it answers it for a background app too.

    ``None`` when nothing is focused (common — a background app with no key window)
    or when the attribute is unsupported. Callers treat it as "no focus marker".
    """
    ref: object = None
    try:
        with macos_ffi.ax_owned_attr(app_elem, AX_FOCUSED_UI_ELEMENT) as (value, err):
            if err == macos_ffi.AX_ERR_SUCCESS and macos_ffi.cf_is(
                value, macos_ffi.type_ids().ax_element
            ):
                # Retained because ``ax_owned_attr`` releases the value on exit and
                # the walk uses this reference afterwards — without the retain that
                # is a use-after-free in another process's accessibility client.
                ref = macos_ffi.retain(value)
        yield ref
    finally:
        macos_ffi.release_all([ref] if ref is not None else [])


def _selected_text(focused: object, *, text_limit: int) -> str:
    """The operator's current text selection inside *focused*, or ``""``.

    Worth one AX read per snapshot because it is the difference between "there is a
    text area" and "the user has highlighted THIS passage" — the thing a request
    like "rewrite what I selected" refers to, and something no amount of tree
    walking recovers.

    **Never read for a secure element.** A selected password is still a password,
    and the established rule is that a secure node discloses only its existence.
    The check is on the FOCUSED element specifically (not the tree's
    ``has_secure``), because that is the element whose selection this reads: a
    window may legitimately contain a password field while the caret sits in an
    ordinary search box, and refusing the whole window's selection for that would
    withhold a value that is not sensitive.

    Clipped through the same ``text_limit`` as every other string, and flattened,
    for the tree-forging reason documented on ``render._clip``: a selection is
    arbitrary user content and may contain newlines.
    """
    if not focused:
        return ""
    role = macos_ffi.ax_str(focused, AX_ROLE)
    subrole = macos_ffi.ax_str(focused, AX_SUBROLE)
    if _is_secure(role, subrole):
        return ""
    return macos_ffi.ax_str(focused, AX_SELECTED_TEXT)


def _needs_manual_accessibility(window: "object | None", err: int, stats: WalkStats) -> bool:
    """True when the Electron opt-in should be attempted.

    Two triggers, because the symptom has two shapes:

    * ``window is None`` with ``-25204`` — Chrome's behaviour, a hard error on
      every attribute read;
    * a window that walked to fewer than :data:`ELECTRON_STUB_NODE_THRESHOLD`
      nodes — Obsidian's behaviour, a *successful* read of a 13-node stub. An
      error-only check misses this one entirely, which is how an Electron app ends
      up looking like a working application with no controls.
    """
    if window is None:
        return err == macos_ffi.AX_ERR_CANNOT_COMPLETE or err != macos_ffi.AX_ERR_SUCCESS
    return stats.visited < ELECTRON_STUB_NODE_THRESHOLD


def _retry_with_manual_accessibility(
    app_elem: object,
    app: AppRef,
    req: SnapshotRequest,
    previous_err: int,
    *,
    focused: object = None,
) -> "tuple[object | None, int, tuple[ElementRec, ...], WalkStats]":
    """Set ``AXManualAccessibility``, poll, and re-walk ONCE.

    Bounded on both axes: at most
    ``ELECTRON_OPT_IN_WAIT_SECS / ELECTRON_OPT_IN_POLL_SECS`` polls, and exactly
    one retry — never a loop that could hold a worker thread on an app that will
    never answer. The best result seen across the polls is kept, so a partially
    populated tree is better than nothing if the app is still warming up.

    Failure to SET the attribute is not fatal and not even a warning: a native app
    that has no such attribute answers with an error, which simply means "this was
    not an Electron app" and the original result stands.

    The returned window is a +1 reference the caller releases; every window this
    function looked at and did not return is released here, so a slow-warming app
    that takes all eight polls does not leak seven window references.

    Each candidate window's bounds are re-read for ITS walk rather than inherited
    from the caller: a poll can select a different window (the app finished opening
    its real one), and frames measured against the previous window's origin would be
    offset by the distance between the two — plausible-looking numbers pointing at
    the wrong place.
    """
    set_err = macos_ffi.ax_set_true(app_elem, macos_ffi.AX_MANUAL_ACCESSIBILITY)
    if set_err != macos_ffi.AX_ERR_SUCCESS:
        logger.debug("AXManualAccessibility not settable (AX error %s)", set_err)

    deadline = time.monotonic() + macos_ffi.ELECTRON_OPT_IN_WAIT_SECS
    best_window: "object | None" = None
    best_elements: tuple[ElementRec, ...] = ()
    best_stats = WalkStats()
    err = previous_err
    while True:
        time.sleep(macos_ffi.ELECTRON_OPT_IN_POLL_SECS)
        window, err = _frontmost_window(
            app_elem, window_id=app.window_id, window_title=app.window_title
        )
        if window is not None:
            elements, stats, _ = _walk(
                window, req, window_bounds=_window_bounds(window), focused=focused
            )
            if stats.visited >= ELECTRON_STUB_NODE_THRESHOLD:
                macos_ffi.release_all([best_window] if best_window is not None else [])
                return window, macos_ffi.AX_ERR_SUCCESS, elements, stats
            if stats.visited > best_stats.visited:
                macos_ffi.release_all([best_window] if best_window is not None else [])
                best_window, best_elements, best_stats = window, elements, stats
            else:
                macos_ffi.release_all([window])
        if time.monotonic() >= deadline:
            break
    return best_window, err, best_elements, best_stats


def _walk(
    window: object,
    req: SnapshotRequest,
    *,
    keep_index: "int | None" = None,
    window_bounds: "tuple[float, float, float, float] | None" = None,
    focused: object = None,
) -> "tuple[tuple[ElementRec, ...], WalkStats, object | None]":
    """Flatten a window's accessibility subtree into indexed records.

    **The single numbering authority.** Both the snapshot path and the driver's
    element-addressing re-walk call this one function, because two
    implementations of "which node is index 7" that drifted apart would be a
    silent wrong-target bug — the worst class this feature can produce.

    **Iterative** — an explicit ``(element, depth)`` stack, never recursion. A
    recursive walk of a deep tree overflows the C stack from inside a ctypes call
    chain, and that is a process abort rather than a ``RecursionError`` something
    could catch.

    Pre-order with children pushed in reverse, so pop order is left-to-right and
    the indices the model sees match the order the renderer prints them.

    Three budgets, reported separately because they mean different things to a
    model: ``truncated`` says "there is more of this tree", ``depth_truncated``
    says "there is more BELOW what you can see", and ``time_truncated`` says "this
    app was too slow to finish".

    **The time budget is not redundant with the node budget.**
    ``AX_MESSAGING_TIMEOUT_SECS`` bounds ONE accessibility call, not a walk: this
    loop makes ~5 AX calls per node over up to :data:`MAX_TREE_NODES_LIMIT` nodes,
    so an app whose reads each take the full per-call timeout would park a worker
    thread for tens of minutes without ever exceeding a single timeout. The node
    and depth budgets do not help — they bound work, not time. Only the aggregate
    :data:`MAX_WALK_SECS` deadline checked at the top of the loop does.

    That deadline is checked BEFORE the pop, so the ``finally`` drain still owns
    every reference on the stack. Like the ``MAX_TREE_NODES_LIMIT`` cutoff above
    it, stopping early means the unvisited remainder was never classified, so
    ``saw_secure`` covers only what was reached — the same accepted bound as the
    node cutoff, and the reason the deadline is set three orders of magnitude
    above a measured healthy walk.

    Structural containers with no text of their own are ELIDED — they consume
    neither an index nor a rendered line — but their children are still visited at
    the parent's depth. A real Finder tree is mostly ``AXGroup`` scaffolding, and
    spending indices on it both wastes the node budget and makes the numbering the
    model reasons about sparse and confusing.

    *keep_index* additionally returns the live ``AXUIElement`` for that ONE index
    (the addressing path needs it) and ends the walk there, so addressing element 3
    does not walk 1,400 nodes. That element is returned as a **+1 reference the
    caller must release**; every other element the walk touched is released here.

    *window_bounds* is the target window's screen rect. When present, each record's
    geometry is stored WINDOW-RELATIVE; when absent, no frame is stored at all
    rather than a screen-absolute one, because a consumer cannot tell the two apart
    and the two mean different things.

    *focused* is the app's ``AXFocusedUIElement``, read ONCE by the caller — the
    walk only compares pointers against it. Reading it per node would be one extra
    AX round-trip on every node of every walk (~1,400 on Chrome) to answer a
    question with a single answer.

    **Reference ownership is tracked per stack entry.** ``ax_children`` returns +1
    references (they must be retained, because the AX children of a
    ``Copy``-returned array die with the array), so each one is released once the
    walk is done with it — including the entries still on the stack when a budget
    cuts the walk short, which the ``finally`` drains. The root *window* is NOT
    owned here; the caller that obtained it releases it.

    The one path the ``finally`` drain cannot cover is *keep_index*: the addressed
    node's children have already been read (and retained) by the time the index
    matches, and they are never pushed, so they are released explicitly there. That
    is not a cosmetic leak — every mutating action runs an addressing re-walk, so it
    would be a per-action leak of one node's whole child list.
    """
    records: list[ElementRec] = []
    kept: "object | None" = None
    truncated = False
    depth_truncated = False
    time_truncated = False
    saw_secure = False
    visited = 0

    def read_children(element: object, role: str = "") -> list:
        """Children of *element* across every child collection, deduplicated.

        **``AXChildren`` alone is not the whole tree.** A table, outline or list
        routinely exposes its rows ONLY through ``AXRows`` (and a scrolled list only
        the on-screen ones through ``AXVisibleChildren``), reporting an empty or
        scaffolding-only ``AXChildren``. A children-only walk therefore renders a
        spreadsheet, a Finder list or a mail inbox as an empty container — which the
        model reads as "this app has no content" and answers by guessing
        coordinates. Reading all three and merging is what makes those apps
        addressable at all.

        For a :data:`ROW_BEARING_ROLES` container the alternate collections are read
        FIRST, so the rows take the low indices rather than landing behind the
        container's own scaffolding — the indices a model addresses are the early
        ones.

        **Deduplicated by element IDENTITY** (``CFEqual``, not handle equality): a
        row is commonly present in both ``AXRows`` and ``AXVisibleChildren``, and
        each read mints a distinct reference for it. Comparing addresses would let
        the same row through twice, and a duplicated row is worse than a missing
        one — the model would address index 7 and index 9 believing they are two
        different rows. Duplicates are released immediately so the dedup is not a
        leak.

        ``MAX_CHILDREN_PER_NODE`` silently dropped the tail (reviewer finding): a
        container with more children than the cap left ``saw_secure`` reflecting only
        the first 512 while ``truncated`` stayed False — so the walk reported itself
        COMPLETE and non-secure, and ``capture_snapshot_image``'s fail-closed check
        (which refuses to capture a truncated walk) had nothing to refuse on. A
        password field as child 513 therefore left the window's pixels capturable.

        ``truncated`` is exactly the right signal: it already means "there is more of
        this tree than you were shown", which is precisely true here, and the capture
        gate already reads it as "unknown ⇒ present".

        The cap now bounds the MERGED list, so three collections cannot together
        exceed what one was allowed to — otherwise the memory bound the cap exists
        for would silently have tripled.

        Detected as ``len(children) >= limit`` rather than by asking for the real
        count: the cap exists so one pathological container cannot materialise 100k CF
        pointers, and re-reading the array to count it would reintroduce that cost. A
        node with EXACTLY the cap many children is therefore treated as truncated too
        — a false positive costs one suppressed screenshot, a false negative is the
        disclosure.
        """
        nonlocal truncated
        merged: list = []
        for name in _child_attribute_order(role):
            if len(merged) >= MAX_CHILDREN_PER_NODE:
                # The merged cap is already reached; do not even read the remaining
                # collections. ``truncated`` is set below.
                break
            found = macos_ffi.ax_retained_elements(element, name, limit=MAX_CHILDREN_PER_NODE)
            if not merged:
                # FIRST non-empty collection: nothing to compare against, so the
                # dedup scan is skipped entirely. This is the overwhelmingly common
                # shape — almost every node has children in exactly one collection —
                # and it keeps the pairwise comparison below off the hot path.
                merged = found[:MAX_CHILDREN_PER_NODE]
                macos_ffi.release_all(found[MAX_CHILDREN_PER_NODE:])
                continue
            merged.extend(_dedup_against(merged, found))
        if len(merged) >= MAX_CHILDREN_PER_NODE:
            truncated = True
        return merged

    # (element, depth, owned) — ``owned`` is False only for the caller's root.
    stack: list[tuple[object, int, bool]] = [(window, 0, False)]
    # monotonic, never wall clock: a clock adjustment mid-walk must not be able to
    # expire the budget instantly (or postpone it forever).
    deadline = time.monotonic() + MAX_WALK_SECS

    try:
        while stack:
            if time.monotonic() >= deadline:
                # Checked before the pop so the ``finally`` drain still owns every
                # queued reference. ``truncated`` is set too: whatever the reason,
                # the tree the model is shown is incomplete and must say so.
                time_truncated = True
                truncated = True
                break
            element, depth, owned = stack.pop()
            release_me = owned
            try:
                # Classify BEFORE the budget checks. ``saw_secure`` must reflect the
                # whole window, not just the part that fit in the report — see
                # WalkStats. Reading role+subrole is two cheap AX reads; the
                # expensive parts (value, actions, children) still happen only for
                # nodes that make the cut.
                role = macos_ffi.ax_str(element, AX_ROLE)
                subrole = macos_ffi.ax_str(element, AX_SUBROLE)
                secure = _is_secure(role, subrole)
                if secure:
                    saw_secure = True

                if depth > req.max_depth:
                    # Same reasoning as the node-budget branch below, and the same
                    # bug if it is not applied here: a bare ``continue`` stops
                    # DESCENDING, so a secure field below ``max_depth`` is never
                    # classified and ``saw_secure`` stays False — and the model
                    # chooses ``max_tree_depth``, so ``max_tree_depth=1`` would hide
                    # a password field and leave the window's pixels capturable.
                    # Keep walking cheaply (role+subrole only, no record) so the
                    # secure scan still covers the whole window.
                    depth_truncated = True
                    if visited >= MAX_TREE_NODES_LIMIT:
                        continue
                    visited += 1
                    for child in reversed(read_children(element, role)):
                        stack.append((child, depth + 1, True))
                    continue
                if len(records) >= req.max_nodes:
                    # The REPORT is full, but do not stop walking: keep descending
                    # (cheaply — role+subrole only, no value/actions read and no
                    # record built) so ``saw_secure`` still covers the part of the
                    # window that did not fit. Stopping here is what let a password
                    # field past ``max_nodes`` leave the window's pixels capturable.
                    # Bounded by ``MAX_TREE_NODES_LIMIT`` so a hostile tree cannot
                    # make this scan unbounded.
                    truncated = True
                    if visited >= MAX_TREE_NODES_LIMIT:
                        break
                    visited += 1
                    for child in reversed(read_children(element, role)):
                        stack.append((child, depth + 1, True))
                    continue
                visited += 1
                title = macos_ffi.ax_str(element, AX_TITLE)
                # A secure field's value is not read AT ALL. Not
                # read-then-redacted: the bytes never enter the process, so no
                # later bug — a debug log, an exception message, a serialization —
                # can leak what was never fetched.
                value = "" if secure else _first_text(element, skip_title=bool(title))
                actions = (
                    () if secure else macos_ffi.ax_actions(element, limit=MAX_ACTIONS_PER_NODE)
                )

                children = read_children(element, role)
                if _is_elidable(role, title, value, actions, children):
                    # Elided: children inherit the parent's depth so the visible
                    # tree does not gain a level of indentation for a node nobody
                    # can see.
                    for child in reversed(children):
                        stack.append((child, depth, True))
                    continue

                index = len(records)
                records.append(
                    ElementRec(
                        index=index,
                        role=role,
                        subrole=subrole,
                        title=title,
                        value=value,
                        actions=actions,
                        depth=depth,
                        secure=secure,
                        # AXEnabled is unsupported on many elements (a window
                        # answers -25205), so absent must read as enabled —
                        # rendering every such node "(disabled)" would be both
                        # wrong and noisy.
                        enabled=macos_ffi.ax_bool(element, AX_ENABLED, default=True),
                        frame=_local_frame(element, window_bounds),
                        traits=_traits(element, secure=secure),
                        # Pointer identity against the ONE focused element the
                        # caller read. ``macos_ffi.same_element`` compares the
                        # underlying CF pointers rather than the Python wrappers,
                        # which are distinct objects for the same element.
                        focused=focused is not None and macos_ffi.same_element(element, focused),
                    )
                )
                if keep_index is not None and index == keep_index:
                    # Ownership transfers to the caller; do not release it here.
                    kept = macos_ffi.retain(element) if not owned else element
                    release_me = False
                    # ``children`` was read (and therefore retained, +1 each) BEFORE
                    # the elidable check, so this node's children exist even though
                    # they were never pushed. Draining the stack alone would leak
                    # every one of them — on EVERY mutating action, since each one
                    # runs an addressing re-walk. Release them explicitly.
                    macos_ffi.release_all(children)
                    macos_ffi.release_all(_drain(stack))
                    break
                for child in reversed(children):
                    stack.append((child, depth + 1, True))
            finally:
                if release_me:
                    macos_ffi.release_all([element])
    finally:
        # Whatever a break or an exception left behind.
        macos_ffi.release_all(_drain(stack))

    return (
        tuple(records),
        WalkStats(
            visited=visited,
            truncated=truncated,
            depth_truncated=depth_truncated,
            saw_secure=saw_secure,
            time_truncated=time_truncated,
        ),
        kept,
    )


def _drain(stack: list) -> list:
    """Pop every OWNED element off *stack* and return it, emptying the stack."""
    owned = [entry[0] for entry in stack if entry[2]]
    stack.clear()
    return owned


@contextmanager
def resolve_element(
    app: AppRef, rec: ElementRec
) -> Iterator["tuple[object | None, ElementRec | None]"]:
    """Yield ``(live_element, its_record)`` for ``rec.index`` in *app*, or ``(None, None)``.

    The driver's addressing primitive, and a context manager so that **no CF
    reference ever escapes this module**. Every reference the re-walk creates — the
    application element, the window, every element visited, and the one addressed —
    is released when the block exits, whatever happens inside it.

    Numbering comes from the SAME :func:`_walk` that produced the snapshot the model
    was shown, not a private copy: two implementations of "which node is index 7"
    that drifted apart would be a silent wrong-target bug.

    The record is yielded alongside the element so the caller can confirm the
    control at that index is still the one the model addressed. That check is what
    makes the addressing safe rather than merely plausible, because the addressing
    walk uses the widest budgets and its numbering is therefore not guaranteed
    identical to a truncated original.
    """
    with macos_ffi.ax_application(app.pid) as app_elem:
        if not app_elem:
            yield None, None
            return
        window, _ = _frontmost_window(
            app_elem, window_id=app.window_id, window_title=app.window_title
        )
        if window is None:
            yield None, None
            return
        element: "object | None" = None
        try:
            records, _, element = _walk(window, ADDRESSING_REQUEST, keep_index=rec.index)
            found = next((r for r in records if r.index == rec.index), None)
            yield element, found
        finally:
            macos_ffi.release_all([element] if element is not None else [])
            macos_ffi.release_all([window])


# Budgets for an addressing re-walk: the widest node/depth caps the MCP schemas
# allow, so an index from any legitimate snapshot is reachable. ``_walk``'s
# ``keep_index`` ends the walk as soon as that index is recorded, and no image is
# captured — this walk exists to locate one element.
ADDRESSING_REQUEST = SnapshotRequest(
    max_nodes=MAX_TREE_NODES_LIMIT,
    max_depth=MAX_TREE_DEPTH_LIMIT,
    want_image=False,
)


def _is_secure(role: str, subrole: str) -> bool:
    """THE security-critical predicate: is this element a secure text field?

    Checks **both** attributes against :data:`SECURE_SUBROLE`. A live probe of a
    real macOS password box reported::

        AXRole    = 'AXTextField'          <- innocuous
        AXSubrole = 'AXSecureTextField'    <- the only reliable signal
        AXValue   = readable

    so the intuitive ``role == "AXSecureTextField"`` check misses every password
    field on the platform. The role side is still checked because some toolkits do
    report it there, and either one being enough is strictly safer than requiring
    a specific placement.

    Deliberately its own function despite being one expression: value redaction,
    input refusal and screenshot suppression all key off this result, so it gets a
    name a test can pin and a comment explaining the non-obvious asymmetry.
    """
    return SECURE_SUBROLE in (role, subrole)


def _local_frame(
    element: object,
    window_bounds: "tuple[float, float, float, float] | None",
) -> "tuple[float, float, float, float] | None":
    """*element*'s rect expressed relative to *window_bounds*, or ``None``.

    Two independent reasons this returns ``None`` rather than a screen rect when
    *window_bounds* is unavailable:

    * an unlabelled coordinate is worse than no coordinate — a consumer cannot tell
      a window-local ``(12, 40)`` from a screen-absolute one, and the difference is
      the whole position of the window;
    * the screenshot the model may also be reading is a CROP of this window, so a
      screen-absolute number could not be related to any pixel it can see.

    Window-relative also makes the value stable across a window drag, which matters
    because the model may reason about a frame it was shown one turn earlier.
    """
    if window_bounds is None:
        return None
    frame = macos_ffi.ax_frame(element)
    if frame is None:
        return None
    x, y, width, height = frame
    return (x - window_bounds[0], y - window_bounds[1], width, height)


def _traits(element: object, *, secure: bool) -> tuple[str, ...]:
    """Observed trait words for *element* (``selected``, ``expanded``, ``editable``).

    Each source attribute is read as a TRI-STATE and only a definite ``True``
    produces a word: ``AXSelected`` is absent on the great majority of elements,
    and turning absent into ``not selected`` would attach a trait to every node in
    the tree.

    ``editable`` comes from ``AXUIElementIsAttributeSettable(AXValue)`` rather than
    from ``AXEnabled``, because those answer different questions: a read-only text
    field (a disabled form input, a log pane) reports ``AXEnabled=true`` with a
    readable value, and typing into it silently does nothing. This is the trait that
    turns that dead end into something the model can see before it acts.

    A *secure* element gets NO traits. Not for tidiness: ``editable`` would confirm
    the password box accepts input, and the established rule for a secure node is
    that only its existence is disclosed — same reason its title and value are
    withheld.
    """
    if secure:
        return ()
    words: list[str] = []
    if macos_ffi.ax_bool_opt(element, AX_SELECTED) is True:
        words.append(TRAIT_SELECTED)
    if macos_ffi.ax_bool_opt(element, AX_EXPANDED) is True:
        words.append(TRAIT_EXPANDED)
    if macos_ffi.ax_is_settable(element, AX_VALUE):
        words.append(TRAIT_EDITABLE)
    return tuple(words)


def _first_text(element: object, *, skip_title: bool) -> str:
    """First non-empty text attribute of *element* (value, then description).

    ``AXTitle`` is skipped when the caller already has it, so a node does not
    report the same string twice. ``AXDescription`` is the last resort: icon-only
    buttons carry their meaning ONLY there, and without it the model sees an
    unlabelled button it cannot reason about.
    """
    for name in _TEXT_ATTRIBUTES:
        if skip_title and name == AX_TITLE:
            continue
        text = macos_ffi.ax_str(element, name)
        if text:
            return text
    return ""


def _is_elidable(
    role: str,
    title: str,
    value: str,
    actions: tuple[str, ...],
    children: list,
) -> bool:
    """True when this node is pure structure and should not consume an index.

    Elided only when ALL of these hold: the role is one of
    :data:`ELIDABLE_ROLES`, there is no text, no advertised action, and at least
    one child. The "has children" condition matters — an empty group is a genuine
    leaf and dropping it silently would make the tree claim a container had
    nothing in it when in fact nothing was reported. The "no actions" condition
    matters too: a clickable ``AXGroup`` is a real target, however structural its
    role looks.
    """
    if role not in ELIDABLE_ROLES:
        return False
    if title or value or actions:
        return False
    return bool(children)


def refresh_fingerprints(app: AppRef, req: SnapshotRequest) -> Snapshot:
    """Re-walk *app* for the pre-action drift check.

    A full walk rather than a targeted single-element read: an index only means
    anything relative to a complete walk, so verifying "index 7 is still the Save
    button" requires reproducing the numbering that produced index 7. Measured at
    25-70ms (Slack 506 nodes / 0.025s, Chrome 1475 / 0.07s), which is an
    acceptable cost per mutating action.

    ``want_image`` is ignored: the verification walk never captures pixels.
    """
    return build_snapshot(app, req)


__all__ = [
    "ADDRESSING_REQUEST",
    "ELECTRON_STUB_NODE_THRESHOLD",
    "MAX_ACTIONS_PER_NODE",
    "MAX_CHILDREN_PER_NODE",
    "MAX_WALK_SECS",
    "WalkStats",
    "build_snapshot",
    "refresh_fingerprints",
    "resolve_element",
]
