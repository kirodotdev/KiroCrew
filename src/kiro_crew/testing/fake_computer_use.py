"""Fake computer-use backend for offline tests, evals and demos.

Shipped in the runtime wheel (not dev-only), alongside
:mod:`kiro_crew.testing.fake_acp_backend`, so a downstream suite can exercise
the whole computer-use stack — MCP dispatch, governance, policy, the snapshot
index, rendering — with **no native framework, no real window, no real
application and no permission grant**.

The pytest suite registers this backend process-wide
(``register_computer_use_backend(FakeComputerUseBackend)``), which is one of the
two mechanisms that keep CI off the native path; the other is structural (no
module-scope ``CDLL`` anywhere in the package).

Its fixtures are chosen to make the security-critical branches reachable:

* **A secure field that looks innocuous.** ``role="AXTextField"`` with
  ``subrole="AXSecureTextField"`` and a POPULATED value — exactly what macOS
  reports for a real password box. A role-only check would treat it as an
  ordinary text field, so this fixture is what proves the both-attribute check
  and, through it, value redaction, input refusal and whole-window screenshot
  suppression.
* **A node whose title carries a credential-shaped literal and an
  exfiltration-shaped URL**, so a test can assert the rendered tree comes back
  masked rather than trusting that the redaction pass is wired up. The
  credential is the well-known public documentation example key — never a real
  secret.
* **A blocked app in the catalog** (a terminal), so the denylist refusal is
  reachable without inventing an app.
* **A real, decodable 1x1 JPEG** for image bytes, so the persistence and
  size-reporting paths run against genuine bytes rather than a placeholder.

Deterministic, offline, stdlib-only.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field, replace
from typing import Any

from kiro_crew.computer_use.backend import ComputerUseBackend
from kiro_crew.computer_use.types import (
    AX_PRESS_ACTION,
    CLICK_METHOD_ACCESSIBILITY,
    CLICK_METHOD_APP_POST,
    CLICK_METHOD_GLOBAL,
    ERR_ACTION_FAILED,
    ERR_APP_NOT_FOUND,
    ERR_POINT_REQUIRED,
    ERR_UNKNOWN_CLICK_METHOD,
    PERMISSION_GRANTED,
    PLATFORM_FAKE,
    REFUSAL_ACCESSIBILITY_NEEDS_INDEX,
    SECURE_SUBROLE,
    TOOL_LIST_APPS,
    TRAIT_EDITABLE,
    AppRef,
    BackendStatus,
    ClickRequest,
    DragRequest,
    DriverResult,
    ElementRec,
    PermissionProbe,
    Snapshot,
    SnapshotRequest,
)

# ── Image fixture ──
# A real baseline JPEG: 1x1 grayscale, quality 55, 159 bytes. Verified decodable
# (SOI .. EOI, valid SOF0/SOS segments) so a test that inspects or re-encodes it
# is exercising a genuine image, not a sentinel byte string.
FAKE_JPEG_BASE64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA4KCw0LCQ4NDA0QDw4RFiQXFhQUFiwgIRokNC43"
    "NjMuMjI6QVNGOj1OPjIySGJJTlZYXV5dOEVmbWVabFNbXVn/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAAAP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AP//Z"
)
FAKE_JPEG_BYTES = base64.b64decode(FAKE_JPEG_BASE64)
FAKE_IMAGE_WIDTH = 1
FAKE_IMAGE_HEIGHT = 1

# ── Redaction fixtures ──
# The canonical PUBLIC documentation access-key id (it appears verbatim in AWS's
# own docs and in this repo's redaction tests) plus two exfiltration-shaped URLs
# using IANA-reserved documentation ranges. None of these is a real credential or
# a reachable host; they exist so a test can assert that the rendered tree comes
# back MASKED.
FAKE_CREDENTIAL_FIXTURE = "AKIA" "IOSFODNN7EXAMPLE"
FAKE_EXFIL_URL = (
    "https://evil.example.com/collect?data="
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)
FAKE_LEAKY_TITLE = f"deploy notes key={FAKE_CREDENTIAL_FIXTURE} upload={FAKE_EXFIL_URL}"
# The value a secure field holds. A test asserts this string is NEVER present in
# any rendered output.
FAKE_SECRET_VALUE = "correct-horse-battery-staple"

# The Files window's screen rect, and the selection its focused field reports.
# Deliberately a NON-ZERO origin: element frames are window-local, so an origin of
# (0, 0) would make a correct implementation and one that leaked screen coordinates
# produce identical output — a downstream suite could not tell them apart.
FAKE_WINDOW_BOUNDS = (220.0, 118.0, 900.0, 600.0)
FAKE_SELECTED_TEXT = "quarterly"

# ── App catalog (deterministic) ──
FAKE_FILES_APP = AppRef(
    name="Fake Files",
    pid=4101,
    bundle_id="dev.kirocrew.fake.files",
    window_id=8801,
    window_title="Documents",
)
# Holds the secure field, so this app is the screenshot-suppression fixture.
FAKE_LOGIN_APP = AppRef(
    name="Fake Login",
    pid=4102,
    bundle_id="dev.kirocrew.fake.login",
    window_id=8802,
    window_title="Sign in",
)
# Matches the built-in terminal denylist by bundle prefix — the policy-refusal
# fixture. Present in the catalog on purpose: ``computer_list_apps`` reports
# what is on screen, and the refusal happens when it is TARGETED.
FAKE_TERMINAL_APP = AppRef(
    name="Terminal",
    pid=4103,
    bundle_id="com.apple.Terminal",
    window_id=8803,
    window_title="bash — 80x24",
)
FAKE_APPS: tuple[AppRef, ...] = (FAKE_FILES_APP, FAKE_LOGIN_APP, FAKE_TERMINAL_APP)


@dataclass(frozen=True)
class FakeNode:
    """One node of a fixture accessibility tree.

    ``secure`` is DERIVED from role and subrole exactly the way a real driver
    derives it (either attribute matching :data:`SECURE_SUBROLE` is enough), so
    the fixture cannot drift from the production rule it exists to test.
    """

    role: str
    subrole: str = ""
    title: str = ""
    value: str = ""
    actions: tuple[str, ...] = ()
    enabled: bool = True
    children: tuple["FakeNode", ...] = field(default_factory=tuple)
    #: ``(x, y, width, height)`` in WINDOW-LOCAL coordinates, matching
    #: ``ElementRec.frame``. ``None`` models the ordinary case of an element with no
    #: exposed geometry, which a downstream suite needs to be able to reach.
    frame: "tuple[float, float, float, float] | None" = None
    traits: tuple[str, ...] = ()
    focused: bool = False

    @property
    def secure(self) -> bool:
        return SECURE_SUBROLE in (self.role, self.subrole)

    @property
    def visible_traits(self) -> tuple[str, ...]:
        """Traits as a real driver would report them: NONE for a secure node.

        Derived rather than stored for the same reason ``secure`` is — a fixture
        that could set ``traits`` on a password field would drift from the
        production rule that a secure element discloses only its existence.
        """
        return () if self.secure else self.traits


# Files-app tree: no secure node, so a screenshot IS attached. Carries the
# credential/exfil title, a deep-ish nesting for depth truncation, and an empty
# AXGroup that a renderer may elide.
_FILES_TREE = FakeNode(
    role="AXWindow",
    title="Documents",
    # The window's own rect. Element frames are expressed RELATIVE to this, so a
    # root without one would make every child's frame unpublishable — see
    # ``FAKE_WINDOW_BOUNDS``.
    frame=(0.0, 0.0, 900.0, 600.0),
    children=(
        FakeNode(
            role="AXSplitGroup",
            children=(
                FakeNode(
                    role="AXScrollArea",
                    children=(
                        FakeNode(
                            role="AXButton",
                            title="Back",
                            actions=(AX_PRESS_ACTION,),
                            frame=(18.0, 12.0, 28.0, 24.0),
                        ),
                        FakeNode(
                            role="AXButton",
                            title="Save",
                            actions=(AX_PRESS_ACTION, "AXShowMenu"),
                            frame=(54.0, 12.0, 44.0, 24.0),
                        ),
                        FakeNode(role="AXStaticText", title=FAKE_LEAKY_TITLE),
                        # The editable + focused fixture: a downstream suite needs one
                        # element carrying every new field at once, and a search box
                        # is where focus and a selection realistically live.
                        FakeNode(
                            role="AXTextField",
                            title="Search",
                            value="quarterly",
                            frame=(120.0, 52.0, 300.0, 24.0),
                            traits=(TRAIT_EDITABLE,),
                            focused=True,
                        ),
                        FakeNode(role="AXButton", title="Disabled", enabled=False),
                    ),
                ),
                FakeNode(role="AXGroup"),
            ),
        ),
    ),
)

# Login-app tree: the secure-field fixture. ``role`` is the INNOCUOUS
# ``AXTextField``; only ``subrole`` reveals it, and the value is populated.
_LOGIN_TREE = FakeNode(
    role="AXWindow",
    title="Sign in",
    children=(
        FakeNode(role="AXStaticText", title="Password"),
        FakeNode(
            role="AXTextField",
            subrole=SECURE_SUBROLE,
            title="Password",
            value=FAKE_SECRET_VALUE,
            actions=(AX_PRESS_ACTION,),
        ),
        FakeNode(role="AXButton", title="Sign In", actions=(AX_PRESS_ACTION,)),
    ),
)

_TERMINAL_TREE = FakeNode(
    role="AXWindow",
    title="bash — 80x24",
    children=(FakeNode(role="AXTextArea", title="shell", value="$ cat ~/.aws/credentials"),),
)

FAKE_TREES: dict[str, FakeNode] = {
    FAKE_FILES_APP.key: _FILES_TREE,
    FAKE_LOGIN_APP.key: _LOGIN_TREE,
    FAKE_TERMINAL_APP.key: _TERMINAL_TREE,
}


class FakeComputerUseBackend(ComputerUseBackend):
    """Deterministic in-memory backend. ``platform_id`` is ``"fake"``.

    Every method appends ``(method_name, kwargs)`` to the public :attr:`calls`
    journal BEFORE doing anything else, so a test can assert both what was
    called and — more importantly — that a refusal happened *upstream* and the
    driver was never reached at all (the journal stays empty).

    :attr:`force_error` makes the next driver call fail without needing a
    special app or element, which is how the "driver failure" branch (and its
    SEL ``failed`` outcome) is exercised.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.force_error: str = ""
        self.closed: bool = False
        # Mutable copy so a test can stage its own tree (e.g. to simulate the
        # UI changing between two walks and trip fingerprint drift).
        self.trees: dict[str, FakeNode] = dict(FAKE_TREES)
        self.apps: tuple[AppRef, ...] = FAKE_APPS

    # ── bookkeeping ──

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    def _forced(self) -> "DriverResult | None":
        """Consume :attr:`force_error` and return the failure, or ``None``."""
        if not self.force_error:
            return None
        reason, self.force_error = self.force_error, ""
        return DriverResult(ok=False, text=reason)

    def reset(self) -> None:
        """Clear the journal and restage the default fixtures."""
        self.calls.clear()
        self.force_error = ""
        self.trees = dict(FAKE_TREES)
        self.apps = FAKE_APPS

    # ── identity / status ──

    @property
    def platform_id(self) -> str:
        return PLATFORM_FAKE

    def status(self) -> BackendStatus:
        return BackendStatus(supported=True, platform_id=PLATFORM_FAKE, reason="")

    def probe_permissions(self) -> PermissionProbe:
        return PermissionProbe(
            accessibility=PERMISSION_GRANTED,
            screen_recording=PERMISSION_GRANTED,
            responsible_hint="fake backend",
        )

    # ── observation ──

    def list_apps(self) -> DriverResult:
        self._record("list_apps")
        forced = self._forced()
        return forced if forced is not None else DriverResult(ok=True, apps=self.apps)

    def resolve_app(self, query: str) -> DriverResult:
        self._record("resolve_app", query=query)
        forced = self._forced()
        if forced is not None:
            return forced
        needle = (query or "").strip().lower()
        for app in self.apps:
            if needle and (needle in app.name.lower() or needle in app.bundle_id.lower()):
                return DriverResult(ok=True, app=app)
        return DriverResult(
            ok=False, text=ERR_APP_NOT_FOUND.format(query=query, tool=TOOL_LIST_APPS)
        )

    def snapshot(self, app: AppRef, req: SnapshotRequest) -> DriverResult:
        self._record(
            "snapshot",
            app=app.key,
            max_nodes=req.max_nodes,
            max_depth=req.max_depth,
            want_image=req.want_image,
        )
        forced = self._forced()
        if forced is not None:
            return forced
        root = self.trees.get(app.key)
        if root is None:
            return DriverResult(
                ok=False, text=ERR_APP_NOT_FOUND.format(query=app.name, tool=TOOL_LIST_APPS)
            )
        elements, truncated, depth_truncated = _walk(root, req.max_nodes, req.max_depth)
        has_secure = any(rec.secure for rec in elements)
        # Mirror BOTH production capture refusals (``capture_macos``), not just the
        # obvious one:
        #  * a window holding ANY secure element gets no pixels at all, because a
        #    password field's rendered glyphs are a credential even after the tree
        #    redacted its value;
        #  * a TRUNCATED walk also gets none, because it cannot prove the window is
        #    free of a secure field — "unknown" has to behave like "present" at the
        #    one gate that decides whether pixels leave the process.
        want_image = req.want_image and not has_secure and not (truncated or depth_truncated)
        snap = Snapshot(
            app=app,
            elements=elements,
            window_title=root.title,
            captured_at=time.monotonic(),
            truncated=truncated,
            depth_truncated=depth_truncated,
            has_secure=has_secure,
            image_jpeg=FAKE_JPEG_BYTES if want_image else b"",
            image_width=FAKE_IMAGE_WIDTH if want_image else 0,
            image_height=FAKE_IMAGE_HEIGHT if want_image else 0,
            # Only published when the root actually carries a rect, mirroring the
            # production degradation: no window origin means no element frames
            # either, and a fixture that always supplied one would hide that branch.
            window_bounds=FAKE_WINDOW_BOUNDS if root.frame is not None else None,
            # A secure window discloses no selection — the focused element there is
            # the password field, and a selected password is still a password.
            selected_text="" if has_secure else _selection_of(root),
        )
        return DriverResult(ok=True, app=app, snapshot=snap)

    # ── input ──

    def click(
        self,
        app: AppRef,
        rec: "ElementRec | None",
        req: ClickRequest,
    ) -> DriverResult:
        """Journal the click, then mirror the real driver's per-method branching.

        ``method`` and ``moves_pointer`` are journalled so a test can assert the
        exact path taken — in particular that an ``auto`` request NEVER arrives here
        as ``global``, and that a refused pointer permit means this method was not
        reached at all (the journal stays empty).
        """
        self._record(
            "click",
            app=app.key,
            index=None if rec is None else rec.index,
            method=req.method,
            point=req.point,
            button=req.button,
            count=req.count,
            moves_pointer=req.moves_pointer,
        )
        forced = self._forced()
        if forced is not None:
            return forced
        if req.method == CLICK_METHOD_ACCESSIBILITY:
            if rec is None:
                return DriverResult(ok=False, text=REFUSAL_ACCESSIBILITY_NEEDS_INDEX)
            if AX_PRESS_ACTION not in rec.actions:
                return DriverResult(
                    ok=False,
                    text=ERR_ACTION_FAILED.format(
                        action=AX_PRESS_ACTION,
                        index=rec.index,
                        app=app.bundle_id or app.name,
                        detail="element advertises no press action",
                    ),
                )
            return DriverResult(ok=True, text=f"pressed element {rec.index}", app=app)
        if req.method not in (CLICK_METHOD_APP_POST, CLICK_METHOD_GLOBAL):
            return DriverResult(ok=False, text=ERR_UNKNOWN_CLICK_METHOD.format(method=req.method))
        if req.point is None:
            return DriverResult(ok=False, text=ERR_POINT_REQUIRED.format(method=req.method))
        times = "" if req.count == 1 else f" x{req.count}"
        return DriverResult(
            ok=True,
            text=(
                f"{req.button} click{times} at "
                f"({req.point[0]:.0f}, {req.point[1]:.0f}) in '{app.name}' ({req.method})"
            ),
            app=app,
        )

    def drag(self, app: AppRef, req: DragRequest) -> DriverResult:
        self._record(
            "drag",
            app=app.key,
            start=req.start,
            end=req.end,
            method=req.method,
            button=req.button,
            moves_pointer=req.moves_pointer,
        )
        forced = self._forced()
        if forced is not None:
            return forced
        if req.method not in (CLICK_METHOD_APP_POST, CLICK_METHOD_GLOBAL):
            # Mirrors the real driver: there is no accessibility action that
            # expresses a sweep between two points.
            return DriverResult(ok=False, text=ERR_UNKNOWN_CLICK_METHOD.format(method=req.method))
        return DriverResult(
            ok=True,
            text=(
                f"dragged with the {req.button} button from "
                f"({req.start[0]:.0f}, {req.start[1]:.0f}) to "
                f"({req.end[0]:.0f}, {req.end[1]:.0f}) in '{app.name}' ({req.method})"
            ),
            app=app,
        )

    def type_text(self, app: AppRef, rec: "ElementRec | None", text: str) -> DriverResult:
        self._record(
            "type_text",
            app=app.key,
            index=None if rec is None else rec.index,
            text=text,
        )
        forced = self._forced()
        if forced is not None:
            return forced
        target = "the focused element" if rec is None else f"element {rec.index}"
        return DriverResult(ok=True, text=f"typed {len(text)} character(s) into {target}", app=app)

    def press_key(self, app: AppRef, rec: "ElementRec | None", key: str) -> DriverResult:
        self._record("press_key", app=app.key, key=key)
        forced = self._forced()
        if forced is not None:
            return forced
        return DriverResult(ok=True, text=f"sent {key}", app=app)

    def set_value(self, app: AppRef, rec: ElementRec, value: str) -> DriverResult:
        self._record("set_value", app=app.key, index=rec.index, value=value)
        forced = self._forced()
        if forced is not None:
            return forced
        # Reflect the write into the staged tree so a follow-up snapshot shows
        # the new value — the behavior a real AXValue write has, and what makes
        # a read-back assertion meaningful.
        self._restage_value(app, rec, value)
        return DriverResult(ok=True, text=f"set element {rec.index}", app=app)

    def scroll(self, app: AppRef, rec: ElementRec, direction: str, pages: float) -> DriverResult:
        self._record("scroll", app=app.key, index=rec.index, direction=direction, pages=pages)
        forced = self._forced()
        if forced is not None:
            return forced
        return DriverResult(
            ok=True, text=f"scrolled element {rec.index} {direction} {pages} page(s)", app=app
        )

    def perform_action(self, app: AppRef, rec: ElementRec, action: str) -> DriverResult:
        self._record("perform_action", app=app.key, index=rec.index, action=action)
        forced = self._forced()
        if forced is not None:
            return forced
        if action not in rec.actions:
            return DriverResult(
                ok=False,
                text=ERR_ACTION_FAILED.format(
                    action=action,
                    index=rec.index,
                    app=app.bundle_id or app.name,
                    detail="action is not advertised by this element",
                ),
            )
        return DriverResult(ok=True, text=f"performed {action} on element {rec.index}", app=app)

    def close(self) -> None:
        self._record("close")
        self.closed = True

    # ── fixture mutation helpers (for drift / read-back tests) ──

    def _restage_value(self, app: AppRef, rec: ElementRec, value: str) -> None:
        """Rewrite the fixture node addressed by *rec* to hold *value*."""
        root = self.trees.get(app.key)
        if root is None:
            return
        self.trees[app.key] = _rewrite_nth(root, rec.index, lambda n: replace(n, value=value))

    def restage_title(self, app_key: str, element_index: int, title: str) -> None:
        """Retitle the node at *element_index* — the fingerprint-drift fixture.

        A test snapshots, calls this, then acts: the chokepoint's re-walk sees a
        different fingerprint at that index and must refuse.
        """
        root = self.trees.get(app_key)
        if root is None:
            return
        self.trees[app_key] = _rewrite_nth(root, element_index, lambda n: replace(n, title=title))

    def stage_tree(self, app_key: str, root: FakeNode) -> None:
        """Replace an app's whole fixture tree (deep trees, empty trees, …)."""
        self.trees[app_key] = root


def deep_tree(depth: int) -> FakeNode:
    """Build a linear tree *depth* levels deep.

    The iterative-walk regression fixture: a 5,000-deep tree must truncate at
    ``max_depth`` rather than raise ``RecursionError``. A recursive walk would
    blow the stack — and inside a ctypes callback that is a hard crash, not an
    exception, which is why the production walk is explicitly iterative.
    """
    node = FakeNode(role="AXGroup", title=f"leaf-{depth}")
    for level in range(depth - 1, 0, -1):
        node = FakeNode(role="AXGroup", title=f"level-{level}", children=(node,))
    return node


def _selection_of(root: FakeNode) -> str:
    """The selection the focused node reports, or ``""``.

    Derived from the tree rather than stored on the backend so a fixture cannot end
    up claiming a selection with nothing focused — the production invariant is that
    a selection belongs to the focused element, and there is at most one.
    """
    stack = [root]
    while stack:
        node = stack.pop()
        if node.focused:
            return "" if node.secure else FAKE_SELECTED_TEXT
        stack.extend(node.children)
    return ""


def _walk(
    root: FakeNode, max_nodes: int, max_depth: int
) -> tuple[tuple[ElementRec, ...], bool, bool]:
    """Iteratively flatten a fixture tree into indexed records.

    Explicit stack, matching the production walk: depth is bounded by
    *max_depth* and node count by *max_nodes*, and neither limit relies on the
    Python recursion limit. Pre-order, so indices are assigned in the same order
    the renderer prints them.
    """
    records: list[ElementRec] = []
    truncated = False
    depth_truncated = False
    # Stack of (node, depth); reversed pushes keep left-to-right pre-order.
    stack: list[tuple[FakeNode, int]] = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            depth_truncated = True
            continue
        if len(records) >= max_nodes:
            truncated = True
            break
        records.append(
            ElementRec(
                index=len(records),
                role=node.role,
                subrole=node.subrole,
                title=node.title,
                value="" if node.secure else node.value,
                actions=node.actions,
                depth=depth,
                secure=node.secure,
                enabled=node.enabled,
                # A secure node discloses only its existence — no frame and no
                # traits, mirroring the production driver.
                frame=None if node.secure else node.frame,
                traits=node.visible_traits,
                focused=node.focused,
            )
        )
        for child in reversed(node.children):
            stack.append((child, depth + 1))
    return tuple(records), truncated, depth_truncated


def _rewrite_nth(root: FakeNode, target: int, fn: Any) -> FakeNode:
    """Return a copy of *root* with *fn* applied to the *target*-th node.

    Numbering matches :func:`_walk` (unbounded pre-order), so an ``ElementRec``
    index addresses the same node here as in a snapshot.
    """
    counter = [0]

    def visit(node: FakeNode) -> FakeNode:
        mine = counter[0]
        counter[0] += 1
        kids = tuple(visit(child) for child in node.children)
        out = node if kids == node.children else replace(node, children=kids)
        return fn(out) if mine == target else out

    return visit(root)
