"""The ``kirocrew-computer`` action verbs, driven through the gateway dispatcher.

Coordinate clicking and drag: a click names an element or a point but not both, the
click method, button and count are closed enums, ``auto`` never resolves to ``global``,
cursor motion is wired to the pointer path, and drag is a tool of its own. Also the
action header's redaction, the skill and spec contract that keyboard verbs need an
element index, and the snapshot budget the drift and refresh walks reuse.

One topic of the ``test_mcp_computer*.py`` family; the fixtures and dispatch helpers
the family shares come from ``mcp_computer_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp_computer_helpers import fake_backend_fixture  # noqa: F401
from mcp_computer_helpers import home_fixture  # noqa: F401
from mcp_computer_helpers import keystone_fixture  # noqa: F401
from mcp_computer_helpers import _SESSION, _dispatch, _enable, _get_state

from kiro_crew import mcp_computer
from kiro_crew.computer_use import index as cu_index
from kiro_crew.computer_use import policy as cu_policy
from kiro_crew.computer_use import service as cu_service
from kiro_crew.computer_use import tools as cu_tools
from kiro_crew.computer_use.types import (
    CLICK_METHOD_GLOBAL,
    CLICK_METHOD_SKY_CLICK,
    CLICK_METHODS,
    ERROR_PREFIX,
    POINTER_MOVING_METHODS,
    TOOL_CLICK,
    TOOL_DRAG,
    TOOL_GET_STATE,
    TOOL_PRESS_KEY,
    TOOL_SCROLL,
    TOOL_SET_VALUE,
    TOOL_TYPE_TEXT,
    AppRef,
)
from kiro_crew.testing.fake_computer_use import (
    FAKE_CREDENTIAL_FIXTURE,
    FAKE_FILES_APP,
    FakeComputerUseBackend,
)
from kiro_crew.validation import ValidationError

# ──────────────────────────────────────────────────────────────────────────
# Coordinate clicking + drag: the one-of rule, the enums, and `auto`
# ──────────────────────────────────────────────────────────────────────────


def _pressable_index() -> int:
    """The fake tree's index for a node that ADVERTISES ``AXPress``.

    Element 0 is a window, which legitimately has no press action — using it would
    make an accessibility-click assertion fail for a reason that has nothing to do
    with what is being tested.
    """
    return next(
        rec.index
        for rec in cu_index.get_shared_index()
        .get(FAKE_FILES_APP.window_key, session_key=_SESSION)
        .elements
        if rec.title == "Save"
    )


class TestClickTargeting:
    """Exactly one of (``element_index`` | ``x`` + ``y``).

    Both failure modes are REFUSED rather than resolved by a precedence rule, and
    both directions matter: silently preferring the index would make a model that
    meant the coordinates act somewhere else entirely, in a live application, with no
    signal that it happened; accepting neither has no meaning at all.
    """

    def test_element_index_alone_uses_the_accessibility_press(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        _enable(keystone)
        _get_state()
        result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=_pressable_index())
        assert not result.startswith(ERROR_PREFIX), result
        clicks = [args for name, args in fake_backend.calls if name == "click"]
        assert clicks[0]["method"] == "accessibility"
        assert clicks[0]["point"] is None
        assert clicks[0]["moves_pointer"] is False

    def test_coordinates_alone_use_the_app_scoped_mouse_path(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        _enable(keystone)
        _get_state()
        result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, x=120, y=340)
        assert not result.startswith(ERROR_PREFIX), result
        clicks = [args for name, args in fake_backend.calls if name == "click"]
        assert clicks[0]["method"] == "app_post"
        assert clicks[0]["point"] == (120.0, 340.0)
        assert clicks[0]["index"] is None
        assert clicks[0]["moves_pointer"] is False

    def test_both_target_forms_are_refused(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        _enable(keystone)
        _get_state()
        result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=0, x=1, y=2)
        assert result.startswith(ERROR_PREFIX)
        assert "not both" in result
        assert not [args for name, args in fake_backend.calls if name == "click"]

    def test_neither_target_form_is_refused(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        _enable(keystone)
        _get_state()
        result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name)
        assert result.startswith(ERROR_PREFIX)
        assert "element_index" in result
        assert not [args for name, args in fake_backend.calls if name == "click"]

    @pytest.mark.parametrize("half", ["x", "y"])
    def test_half_a_coordinate_pair_is_refused(
        self, half: str, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        """A lone ``x`` is not a target, and ``(x, 0)`` would click the screen edge."""
        _enable(keystone)
        _get_state()
        result = _dispatch(TOOL_CLICK, **{"app": FAKE_FILES_APP.name, half: 42})
        assert result.startswith(ERROR_PREFIX)
        assert not [args for name, args in fake_backend.calls if name == "click"]

    def test_a_boolean_coordinate_is_rejected_by_the_schema(self):
        """``bool`` is an ``int`` subclass, so ``x: true`` would become the number 1."""
        with pytest.raises(ValidationError):
            mcp_computer._validate_args(TOOL_CLICK, {"app": "Finder", "x": True, "y": 2})

    def test_an_out_of_range_coordinate_is_rejected(self):
        with pytest.raises(ValidationError):
            mcp_computer._validate_args(TOOL_CLICK, {"app": "Finder", "x": 10**9, "y": 2})

    def test_a_fractional_coordinate_is_accepted(self):
        """An element's frame centre is legitimately fractional."""
        clean = mcp_computer._validate_args(TOOL_CLICK, {"app": "Finder", "x": 10.5, "y": 20.25})
        assert clean["x"] == pytest.approx(10.5)


class TestClickEnums:
    """``click_method`` and ``mouse_button`` are CLOSED enums.

    An unknown value must be refused before any event is synthesized: a substituted
    button or method performs a DIFFERENT gesture in a live application, which is the
    same class of defect as ``keymap.parse_key`` swallowing an unrecognised modifier.
    """

    @pytest.mark.parametrize("method", ["auto", "accessibility", "app_post", "global"])
    def test_every_shipped_method_is_accepted_by_the_schema(self, method: str):
        clean = mcp_computer._validate_args(
            TOOL_CLICK, {"app": "Finder", "element_index": 0, "click_method": method}
        )
        assert clean["click_method"] == method

    def test_sky_click_IS_a_shipped_method_but_must_be_named(self):
        """Reverses an earlier decision, deliberately.

        ``sky_click`` needs the PRIVATE SkyLight framework, and this package shipped
        without it on the grounds that a product should not depend on ABI Apple can
        remove. What changed the call is that the gap is real and reachable: a canvas
        window behind another app's overlay cannot be clicked by ANY public method —
        ``accessibility`` needs an addressable element and ``app_post`` is ignored by
        renderers that hit-test against the window server. Hit in practice.

        The trade is contained rather than accepted wholesale: the private ABI is
        quarantined in ``macos_skylight``, it degrades to a refusal naming
        ``app_post`` when a symbol is absent, and — asserted here — it is reachable
        only by NAME. ``auto`` never resolves onto it (see
        ``TestAutoNeverResolvesToAPrivateOrPointerPath``), so a model that did not ask
        for a private-API path never gets one.
        """
        assert CLICK_METHOD_SKY_CLICK in CLICK_METHODS
        clean = mcp_computer._validate_args(
            TOOL_CLICK,
            {"app": "Finder", "x": 10, "y": 20, "click_method": CLICK_METHOD_SKY_CLICK},
        )
        assert clean["click_method"] == CLICK_METHOD_SKY_CLICK

    def test_an_unknown_method_is_rejected(self):
        with pytest.raises(ValidationError):
            mcp_computer._validate_args(
                TOOL_CLICK, {"app": "Finder", "element_index": 0, "click_method": "teleport"}
            )

    @pytest.mark.parametrize("button", ["left", "right", "middle"])
    def test_every_shipped_button_is_accepted(self, button: str):
        clean = mcp_computer._validate_args(
            TOOL_CLICK, {"app": "Finder", "x": 1, "y": 2, "mouse_button": button}
        )
        assert clean["mouse_button"] == button

    def test_an_unknown_button_is_rejected(self):
        with pytest.raises(ValidationError):
            mcp_computer._validate_args(
                TOOL_CLICK, {"app": "Finder", "x": 1, "y": 2, "mouse_button": "pinky"}
            )

    @pytest.mark.parametrize("count", [0, 4])
    def test_click_count_is_bounded_to_one_through_three(self, count: int):
        """macOS itself only reports up to a triple click as a distinct gesture."""
        with pytest.raises(ValidationError):
            mcp_computer._validate_args(
                TOOL_CLICK, {"app": "Finder", "x": 1, "y": 2, "click_count": count}
            )

    def test_a_button_and_count_reach_the_driver(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        _enable(keystone)
        _get_state()
        result = _dispatch(
            TOOL_CLICK,
            app=FAKE_FILES_APP.name,
            x=5,
            y=6,
            mouse_button="right",
            click_count=2,
        )
        assert not result.startswith(ERROR_PREFIX), result
        clicks = [args for name, args in fake_backend.calls if name == "click"]
        assert clicks[0]["button"] == "right"
        assert clicks[0]["count"] == 2

    def test_accessibility_without_an_element_index_is_refused(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        """AXPress addresses an element; there is no coordinate form of it."""
        _enable(keystone)
        _get_state()
        result = _dispatch(
            TOOL_CLICK, app=FAKE_FILES_APP.name, x=1, y=2, click_method="accessibility"
        )
        assert result.startswith(ERROR_PREFIX)
        assert "element_index" in result
        assert not [args for name, args in fake_backend.calls if name == "click"]

    def test_app_post_without_coordinates_is_refused(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        _enable(keystone)
        _get_state()
        result = _dispatch(
            TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=0, click_method="app_post"
        )
        assert result.startswith(ERROR_PREFIX)
        assert not [args for name, args in fake_backend.calls if name == "click"]


class TestAutoNeverResolvesToGlobal:
    """**The invariant.**

    ``auto`` resolving onto ``global`` would let a model take over the operator's
    physical mouse without ever naming the method. Since the separate pointer opt-in
    was removed — there is no keystone ``allow_pointer_move`` and no
    ``capabilities.computer_use_pointer`` row — this resolver is the ONLY thing
    standing between an ordinary click and the operator's cursor, so it matters more
    now, not less. Asserted three ways, because a single behavioural case would not
    survive a refactor of the resolver.
    """

    @pytest.mark.parametrize(
        "element_index,point",
        [(0, None), (None, (10.0, 20.0)), (None, None)],
    )
    def test_resolver_never_returns_global(self, element_index, point):
        resolved = cu_policy.resolve_click_method("auto", element_index=element_index, point=point)
        assert resolved != CLICK_METHOD_GLOBAL

    def test_global_is_not_in_the_resolvers_reachable_set(self):
        """Structural: the pointer-moving set and what ``auto`` can produce are
        DISJOINT, so a future branch added to the resolver is still bounded."""
        reachable = {
            cu_policy.resolve_click_method("auto", element_index=idx, point=pt)
            for idx, pt in ((0, None), (None, (1.0, 2.0)), (None, None))
        }
        assert reachable & POINTER_MOVING_METHODS == set()

    def test_auto_click_with_coordinates_reaches_the_driver_as_app_post(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        """End-to-end: even with the opt-in ON and governance permitting, an ``auto``
        request must not arrive at the driver as the pointer-moving method."""
        _enable(keystone)
        _get_state()
        result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, x=7, y=8)
        assert not result.startswith(ERROR_PREFIX), result
        clicks = [args for name, args in fake_backend.calls if name == "click"]
        assert clicks[0]["method"] == "app_post"
        assert clicks[0]["moves_pointer"] is False

    def test_auto_drag_reaches_the_driver_as_app_post(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        _enable(keystone)
        _get_state()
        result = _dispatch(TOOL_DRAG, app=FAKE_FILES_APP.name, from_x=1, from_y=2, to_x=3, to_y=4)
        assert not result.startswith(ERROR_PREFIX), result
        drags = [args for name, args in fake_backend.calls if name == "drag"]
        assert drags[0]["method"] == "app_post"
        assert drags[0]["moves_pointer"] is False


class TestCursorMotionIsWiredToThePointerPath:
    """Cursor Motion must actually RUN, not merely exist.

    The overlay supervisor, the motion planner and the AppKit child are each
    unit-tested, but unless a production dispatch path calls them the feature
    never appears on a user's screen. These pin the
    call at the one place it belongs: immediately before a REAL-POINTER gesture.
    """

    @pytest.fixture
    def motions(self, monkeypatch) -> list:
        """Record ``show_pointer_motion`` calls instead of drawing anything."""
        from kiro_crew.computer_use import overlay as overlay_mod

        seen: list = []
        monkeypatch.setattr(
            overlay_mod,
            "show_pointer_motion",
            lambda x, y, count=1: seen.append((x, y, count)),
        )
        return seen

    def test_a_global_click_animates_the_cursor_to_the_target(
        self, keystone: Path, fake_backend: FakeComputerUseBackend, monkeypatch, motions
    ):
        _enable(keystone)
        _get_state()
        result = _dispatch(
            TOOL_CLICK, app=FAKE_FILES_APP.name, x=42, y=99, click_method="global", click_count=2
        )
        assert not result.startswith(ERROR_PREFIX), result
        assert motions == [(42, 99, 2)]

    def test_an_app_scoped_click_animates_NOTHING(
        self, keystone: Path, fake_backend: FakeComputerUseBackend, motions
    ):
        """The physical cursor does not move on this path, so drawing one would lie."""
        _enable(keystone)
        _get_state()
        result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, x=42, y=99, click_method="app_post")
        assert not result.startswith(ERROR_PREFIX), result
        assert motions == []

    def test_an_accessibility_press_animates_NOTHING(
        self, keystone: Path, fake_backend: FakeComputerUseBackend, motions
    ):
        _enable(keystone)
        _get_state()
        save = next(
            rec.index
            for rec in cu_index.get_shared_index()
            .get(FAKE_FILES_APP.window_key, session_key=_SESSION)
            .elements
            if rec.title == "Save"
        )
        result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=save)
        assert not result.startswith(ERROR_PREFIX), result
        assert motions == []

    def test_a_global_drag_animates_to_the_START_point_only(
        self, keystone: Path, fake_backend: FakeComputerUseBackend, monkeypatch, motions
    ):
        """The sweep itself is drawn by the real cursor the driver moves."""
        _enable(keystone)
        _get_state()
        result = _dispatch(
            TOOL_DRAG,
            app=FAKE_FILES_APP.name,
            from_x=10,
            from_y=20,
            to_x=30,
            to_y=40,
            click_method="global",
        )
        assert not result.startswith(ERROR_PREFIX), result
        assert motions == [(10, 20, 1)]

    def test_an_unrecognized_keystone_key_does_not_imply_the_primary_enable(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        """Only ``enabled`` enables. Anything else on the keystone is not a grant.

        ``allow_pointer_move`` is used as the payload precisely because an earlier
        revision documented it as a second consent switch for the real-pointer path.
        It was removed by product decision and ``PolicyConfig.from_state`` never reads
        it, so a keystone carrying it and nothing else must still resolve to DISABLED
        rather than to "the operator configured something, so presumably yes".
        """
        keystone.write_text(json.dumps({"allow_pointer_move": True}), encoding="utf-8")
        result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, x=1, y=2, click_method="global")
        assert result.startswith(ERROR_PREFIX)
        assert "disabled" in result
        assert fake_backend.calls == []

    def test_an_app_post_click_never_consults_the_pointer_permit(
        self, keystone: Path, fake_backend: FakeComputerUseBackend, monkeypatch
    ):
        """A pointer-free click must not travel the real-pointer code path at all.

        ``require_pointer_move`` is now a no-op that only exists so an edition can
        reintroduce a decision, but the CALL still marks which gestures the code
        treats as pointer-moving — so an ``AXPress`` or an ``app_post`` click asking
        for the pointer permit would mean the chokepoint had lost track of which
        methods warp the cursor, which is the invariant that keeps ``auto`` off the
        real-pointer path.
        """
        from kiro_crew.computer_use import gate as cu_gate

        asked: list[str] = []
        monkeypatch.setattr(
            cu_gate,
            "require_pointer_move",
            lambda *a, **k: asked.append("asked") or None,
        )
        _enable(keystone)
        _get_state()
        for args in (
            {"element_index": _pressable_index()},
            {"x": 1, "y": 2},
            {"x": 1, "y": 2, "click_method": "app_post"},
        ):
            assert not _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, **args).startswith(
                ERROR_PREFIX
            )
        assert asked == [], "a pointer-free click must not consult the pointer permit"

    def test_a_permitted_pointer_move_is_audited_by_method(
        self, keystone: Path, fake_backend: FakeComputerUseBackend, monkeypatch
    ):
        """SEL must record the METHOD on the ALLOW path, so "did the agent ever take
        control of my mouse?" is answerable from the trail."""
        recorded: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kwargs):
                recorded.append(kwargs)

            def log_governance_decision(self, **kwargs):
                pass

        monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
        _enable(keystone)
        _get_state()
        _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, x=1, y=2, click_method="global")
        pointer_records = [r for r in recorded if r.get("tool_kind") == "computer_use_pointer"]
        assert pointer_records
        assert CLICK_METHOD_GLOBAL in pointer_records[-1]["resources"]

    def test_a_pointer_free_click_emits_no_pointer_audit_record(
        self, keystone: Path, fake_backend: FakeComputerUseBackend, monkeypatch
    ):
        recorded: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kwargs):
                recorded.append(kwargs)

            def log_governance_decision(self, **kwargs):
                pass

        monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
        _enable(keystone)
        _get_state()
        _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=_pressable_index())
        assert not [r for r in recorded if r.get("tool_kind") == "computer_use_pointer"]


class TestDragTool:
    def test_all_four_coordinates_reach_the_driver(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        _enable(keystone)
        _get_state()
        result = _dispatch(
            TOOL_DRAG, app=FAKE_FILES_APP.name, from_x=10, from_y=20, to_x=110, to_y=220
        )
        assert not result.startswith(ERROR_PREFIX), result
        drags = [args for name, args in fake_backend.calls if name == "drag"]
        assert drags[0]["start"] == (10.0, 20.0)
        assert drags[0]["end"] == (110.0, 220.0)

    def test_drag_has_no_element_form(self, keystone: Path):
        """A drag's meaning IS the path between two points, and no accessibility
        action expresses it — so ``element_index`` is not even a field."""
        with pytest.raises(ValidationError):
            mcp_computer._validate_args(
                TOOL_DRAG,
                {
                    "app": "Finder",
                    "element_index": 0,
                    "from_x": 1,
                    "from_y": 2,
                    "to_x": 3,
                    "to_y": 4,
                },
            )

    def test_accessibility_method_is_refused_for_a_drag(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        _enable(keystone)
        _get_state()
        result = _dispatch(
            TOOL_DRAG,
            app=FAKE_FILES_APP.name,
            from_x=1,
            from_y=2,
            to_x=3,
            to_y=4,
            click_method="accessibility",
        )
        assert result.startswith(ERROR_PREFIX)
        assert not [args for name, args in fake_backend.calls if name == "drag"]

    def test_drag_is_refused_while_the_feature_is_disabled(
        self, home: Path, fake_backend: FakeComputerUseBackend
    ):
        result = _dispatch(TOOL_DRAG, app=FAKE_FILES_APP.name, from_x=1, from_y=2, to_x=3, to_y=4)
        assert result.startswith(ERROR_PREFIX)
        assert fake_backend.calls == []

    def test_drag_is_refused_for_a_blocked_app(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        """The one retained refusal applies to the new verb like every other one.

        Retargeted from a terminal (not refused) onto Kiro Crew's own window,
        which stays refused because driving our own Settings UI would route around
        the keystone that holds the primary enable.
        """
        _enable(keystone)
        result = _dispatch(TOOL_DRAG, app="Kiro Crew", from_x=1, from_y=2, to_x=3, to_y=4)
        assert result.startswith(ERROR_PREFIX)
        assert not [args for name, args in fake_backend.calls if name == "drag"]

    def test_drag_result_carries_the_refreshed_tree(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        """Every mutator ends with a structural re-walk, so the model can see what it
        did without a second call."""
        _enable(keystone)
        _get_state()
        result = _dispatch(TOOL_DRAG, app=FAKE_FILES_APP.name, from_x=1, from_y=2, to_x=3, to_y=4)
        assert "Refreshed state:" in result


class TestTheActionHeaderIsRedacted:
    """GPT 5.6 BLOCKING, confirmed: the action confirmation bypassed redaction.

    Every mutating tool returns ``"<detail>\\n\\nRefreshed state:\\n<tree>"``. The tree
    half is redacted inside ``render_tree`` — that is the package's primary egress
    control — but the header was concatenated AFTER that pass, and ``detail`` is not
    our prose: every driver confirmation interpolates app-supplied text. ``_click_text``
    embeds ``app.name``, which is the process name macOS reports and is therefore
    attacker-controlled: a process named ``Notes key=AKIA…`` put a raw credential
    directly in front of a fully redacted tree.

    A single unredacted egress path is the whole failure — the redaction elsewhere
    does not help if one line skips it.
    """

    @staticmethod
    def _credential_named_app() -> AppRef:
        """The Files fixture, renamed to carry a credential-shaped literal.

        Renamed rather than added so its staged tree still resolves: the index is
        keyed by ``window_key`` (pid + window id), which is unchanged.
        """
        return AppRef(
            name=f"Notes key={FAKE_CREDENTIAL_FIXTURE}",
            pid=FAKE_FILES_APP.pid,
            bundle_id=FAKE_FILES_APP.bundle_id,
            window_id=FAKE_FILES_APP.window_id,
            window_title=FAKE_FILES_APP.window_title,
        )

    def _dispatch_click(self, keystone: Path, fake_backend) -> str:
        _enable(keystone)
        app = self._credential_named_app()
        fake_backend.apps = (app,) + tuple(
            a for a in fake_backend.apps if a.key != FAKE_FILES_APP.key
        )
        _dispatch(TOOL_GET_STATE, app=app.name)
        return _dispatch(TOOL_CLICK, app=app.name, x=10, y=20)

    def test_a_credential_in_the_app_NAME_never_reaches_the_model(
        self, keystone: Path, fake_backend
    ):
        out = self._dispatch_click(keystone, fake_backend)
        # The action succeeded, so the header really was rendered (this would pass
        # trivially against a refusal).
        assert "Refreshed state:" in out
        assert FAKE_CREDENTIAL_FIXTURE not in out
        assert "REDACTED" in out

    def test_the_TREE_half_is_not_redacted_a_SECOND_time(self, keystone: Path, fake_backend):
        """Why the two halves are redacted SEPARATELY rather than as one joined string.

        ``render_tree`` appends its screenshot note AFTER its own redaction pass, on
        purpose: the spool path must reach the model byte-exact, and
        ``redact_credentials``' bare-secret-key heuristic reads a path as one
        base64-alphabet run, so a pass over the joined text would expose every
        screenshot path to it (see ``render._render_image_note``).

        So the fix redacts the HEADER only and leaves the already-redacted body
        untouched. Asserted structurally, because the behavioural shape is not
        reachable here — a mutating tool's refresh walk is deliberately
        ``want_image=False``, so no screenshot note appears in an action result at
        all. A future "just redact the whole response" simplification would pass every
        behavioural test in this file and break screenshots on the read path.
        """
        import inspect

        source = inspect.getsource(cu_tools._run)
        # The header is redacted at the interpolation...
        assert "redact_result(detail)" in source
        # ...and the body is passed through as-is, never re-redacted.
        assert "redact_result(body)" not in source


class TestTheSkillContractMatchesTheRuntime:
    """GPT 5.6 FINDING, confirmed as a DOC bug: the skill advertised an optional index.

    ``SKILL.md`` described ``computer_type_text(app, text, element_index?)`` with an
    "else the focused control" fallback, which the runtime has never allowed —
    ``_ELEMENT_REQUIRED_TOOLS`` refuses an indexless keyboard call because an unnamed
    target has no role or subrole for the secure-field check to inspect.

    GPT's prescribed fix was to change the SCHEMA (and the contract) to match. Taken
    the other way round: the runtime behaviour is the security control and is correct,
    so the DOC was wrong. This test pins the two together, because a stale contract
    makes the model discover the refusal by hitting it — which is precisely the
    trial-and-error the skill exists to remove.
    """

    _SKILL = (
        Path(__file__).resolve().parents[1] / "src/kiro_crew/builtin_skills/computer-use/SKILL.md"
    )

    _SPEC = Path(__file__).resolve().parents[1] / "docs/system-specs/modules/computer-use.md"

    def test_the_spec_names_every_screenshot_spool_writer(self):
        """The security contract must cover every place that persists pixels."""
        text = self._SPEC.read_text(encoding="utf-8")
        writers = {
            "service._persist_image",
            "capture_macos.persist_jpeg",
            "capture_windows.persist_jpeg",
        }
        missing = {writer for writer in writers if writer not in text}
        assert not missing, missing
        assert "invocation-owned partial frame" in text

    def test_the_skill_does_not_advertise_an_optional_element_index(self):
        text = self._SKILL.read_text(encoding="utf-8")
        assert "element_index?" not in text
        # And it says WHY, so a reader does not "helpfully" make it optional again.
        assert "required" in text.lower()

    def test_the_SPEC_does_not_claim_indexless_input_works(self):
        """The stale claim lived in TWO documents, and the second was easy to miss.

        `docs/.../computer-use.md` listed "indexless keyboard input — typing into
        whatever the app has focused works again" under the removed-refusals heading —
        written during the scope change and never implemented. A reader auditing the
        security posture from that document would have concluded a control was gone
        that is in fact still enforced, which is the more dangerous direction for a
        spec to be wrong in. It is now a row in **What still refuses** instead.
        """
        text = self._SPEC.read_text(encoding="utf-8")
        assert "**indexless keyboard input**" not in text
        assert "Indexless keyboard input" in text

    @pytest.mark.parametrize("tool", [TOOL_TYPE_TEXT, TOOL_PRESS_KEY])
    def test_the_runtime_really_does_refuse_an_indexless_keyboard_call(
        self, tool, keystone: Path, fake_backend
    ):
        """The behaviour the doc now matches — asserted so "the doc is right" is a
        verified claim rather than two files that happen to agree today."""
        _enable(keystone)
        _get_state()
        args = {"app": FAKE_FILES_APP.name}
        args["text" if tool == TOOL_TYPE_TEXT else "key"] = (
            "hi" if tool == TOOL_TYPE_TEXT else "return"
        )
        out = _dispatch(tool, **args)
        assert out.startswith(ERROR_PREFIX)
        assert "element_index" in out

    def test_click_is_the_ONLY_element_tool_with_an_alternative(self):
        """The exception, pinned. ``computer_click`` may omit the index because
        coordinates are a target the chokepoint can still check; nothing else has one,
        so a future tool cannot quietly join that list."""
        assert TOOL_CLICK not in cu_tools._ELEMENT_REQUIRED_TOOLS
        for tool in (TOOL_TYPE_TEXT, TOOL_PRESS_KEY, TOOL_SET_VALUE, TOOL_SCROLL):
            assert tool in cu_tools._ELEMENT_REQUIRED_TOOLS, tool

    def test_no_worked_example_in_the_skill_omits_a_required_element_index(self):
        """The shipped example must be RUNNABLE, not just the prose correct.

        ``SKILL.md``'s only end-to-end example called
        ``computer_press_key(app="Finder", key="return")`` twice — both refused by
        ``_CU_ELEMENT_FIELD`` and again by ``_ELEMENT_REQUIRED_TOOLS`` — while the same
        file's prose said an index was required. A model following the example hits a
        refusal on its second call, which is exactly the trial-and-error the skill
        exists to remove, and prose alone could not catch it.
        """
        text = self._SKILL.read_text(encoding="utf-8")
        offenders: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            for tool in sorted(cu_tools._ELEMENT_REQUIRED_TOOLS):
                # Only literal invocations, not the signature rows in the tool table
                # (which are rendered as `| \`tool(app, element_index, …)\` |`).
                if stripped.startswith(f"{tool}(") and "element_index" not in stripped:
                    offenders.append(stripped)
        assert not offenders, (
            "SKILL.md shows call(s) that every layer refuses — a model copying them "
            f"gets an error, not a result: {offenders}"
        )

    def test_the_skill_documents_the_truncated_screenshot_suppression(self):
        """Every non-failure message the model can see belongs in the skill's table.

        A suppressed capture on a truncated walk is ROUTINE (a browser or Electron app
        exceeds the default node budget), and without the note it is emitted with no text at
        all — the model asked for a screenshot, got none, and had no reason to stop
        retrying. The note is now in ``types``; asserting the skill quotes it keeps the
        two from drifting, since ``SKILL.md`` ships to every pip/DMG install and is
        what tells the model this is an answer rather than a fault.
        """
        from kiro_crew.computer_use.types import TRUNCATED_WINDOW_NOTE

        text = self._SKILL.read_text(encoding="utf-8")
        # The distinctive opening clause, not the whole sentence: the table wraps it.
        head = TRUNCATED_WINDOW_NOTE.split(",")[0]
        assert head in text, f"SKILL.md does not document the {head!r} response"
        # And the remedy the model can actually act on.
        assert "max_tree_nodes" in TRUNCATED_WINDOW_NOTE


class TestTheDriftWalkHonoursTheSnapshotBudget:
    """A raised ``max_tree_nodes`` must not make its own elements un-actionable.

    Exercised end-to-end. A mutating tool takes no tree-budget arguments, so a
    drift-verification request built from ``service.snapshot_request()`` — the
    *config default* (1200) — would discard the budget the cached snapshot was
    walked at. Then ``computer_get_state(app, max_tree_nodes=2001)`` renders element
    1400 with no truncation note, and the follow-up
    ``computer_click(element_index=1400)`` is refused with
    ``"… now no element at that index"`` because ``verify_fingerprint`` re-walks at
    1200. Re-snapshotting reproduces the same tree and the same refusal, so the model
    has no way out of the loop — on a documented happy path: the MCP schema advertises
    ``max_tree_nodes`` up to 5000 and the Settings copy says to raise it for dense
    apps.

    ``service.snapshot`` stamps ``Snapshot.walk_budget`` and the drift walk re-walks
    at it.
    """

    @staticmethod
    def _wide_tree(count: int = 1500):
        from kiro_crew.testing.fake_computer_use import FakeNode

        return FakeNode(
            role="AXWindow",
            title="Wide",
            children=tuple(
                FakeNode(role="AXButton", title=f"b{i}", actions=("AXPress",)) for i in range(count)
            ),
        )

    def test_an_element_above_the_config_default_stays_actionable(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        _enable(keystone)
        fake_backend.trees[FAKE_FILES_APP.key] = self._wide_tree()
        shown = _dispatch(
            TOOL_GET_STATE, app=FAKE_FILES_APP.name, max_tree_nodes=2001, screenshot=False
        )
        assert '1400 button "b1399"' in shown, "the raised budget did not render element 1400"

        result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=1400)
        assert not result.startswith(ERROR_PREFIX), result
        assert "no element at that index" not in result

    def test_the_snapshot_records_the_budget_it_was_walked_at(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        """The mechanism, asserted directly: a stamp of ``None`` would silently
        restore the old behaviour via the ``or req`` fallback."""
        _enable(keystone)
        fake_backend.trees[FAKE_FILES_APP.key] = self._wide_tree(20)
        _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name, max_tree_nodes=1777, screenshot=False)
        svc = cu_service.get_shared_service()
        cached = svc.index.get(FAKE_FILES_APP.window_key, session_key=_SESSION)
        assert cached is not None
        assert cached.walk_budget is not None, "service.snapshot did not stamp walk_budget"
        assert cached.walk_budget.max_nodes == 1777

    def test_the_drift_walk_reuses_that_budget_not_the_config_default(
        self, keystone: Path, fake_backend: FakeComputerUseBackend
    ):
        """Read off the fake's own journal, so this pins the request the driver saw."""
        _enable(keystone)
        fake_backend.trees[FAKE_FILES_APP.key] = self._wide_tree(20)
        _dispatch(TOOL_GET_STATE, app=FAKE_FILES_APP.name, max_tree_nodes=1777, screenshot=False)
        fake_backend.calls.clear()
        _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=3)
        walks = [kw["max_nodes"] for name, kw in fake_backend.calls if name == "snapshot"]
        assert walks, "the mutating action performed no verification walk"
        assert all(n == 1777 for n in walks), (
            f"the drift/refresh walks used {walks} instead of the snapshot's own 1777 — "
            "an element the model was legitimately shown would be refused"
        )
