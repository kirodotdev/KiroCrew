"""The member-turn chokepoint: one decision point for the rules-currency invariant.

``kiro_crew.members.member_turn_context`` owns the "every member turn runs
under current rules" invariant, replacing the hand-coordinated branch table it
was previously stitched across. Two families of tests pin it:

1. **Behavior** — the lifecycle mapping and the per-lifecycle verdict, so the
   invariant's shape cannot drift silently.
2. **Wiring (AST guards)** — every delivery branch structurally routes through
   the chokepoint, so a future session-lifecycle branch cannot bypass both the
   member section and the fail-closed rules gate. Same guard style as
   ``test_no_members_sel_audit_is_offloaded``.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from itertools import product

import pytest

from kiro_crew.members import (
    MemberLifecycle,
    MemberSlugError,
    is_member_session_key,
    member_lifecycle,
    member_slot_key,
    member_thread_session_alias,
    member_turn_context,
)

MEMBER = "Code Reviewer"

#: Lifecycle states whose turn injects the CURRENT member section (delivery
#: itself enforces the rules gate: the section builder reads the rules fresh
#: and fails closed on an unreadable file).
_DELIVERING = (
    MemberLifecycle.FRESH,
    MemberLifecycle.SLIM_RESUME,
    MemberLifecycle.WARM_REINJECTION,
)


class TestMemberTurnContext:
    """The chokepoint's verdict, per lifecycle state."""

    @pytest.mark.parametrize(
        "lifecycle,deliver,gate",
        [
            (MemberLifecycle.FRESH, True, False),
            (MemberLifecycle.SLIM_RESUME, True, False),
            (MemberLifecycle.WARM_REINJECTION, True, False),
            (MemberLifecycle.WARM, False, True),
            (MemberLifecycle.MINIMAL, False, False),
        ],
    )
    def test_verdict_truth_table(self, lifecycle, deliver, gate):
        ctx = member_turn_context(MEMBER, lifecycle)
        assert ctx.deliver_section is deliver
        assert ctx.enforce_rules_gate is gate
        assert ctx.member == MEMBER
        assert ctx.lifecycle is lifecycle

    @pytest.mark.parametrize("lifecycle", list(MemberLifecycle))
    def test_empty_member_never_delivers_or_gates(self, lifecycle):
        """No member on the turn means no member layer at all."""
        ctx = member_turn_context("", lifecycle)
        assert ctx.deliver_section is False
        assert ctx.enforce_rules_gate is False
        assert ctx.member == ""

    @pytest.mark.parametrize(
        "lifecycle", [lc for lc in MemberLifecycle if lc is not MemberLifecycle.MINIMAL]
    )
    def test_rules_currency_invariant(self, lifecycle):
        """THE invariant: every member turn passes the fail-closed rules gate.

        Exactly one of the two mechanisms per turn — deliver the current
        section (rules read inside) or run the standalone rules gate. Never
        neither (an unbounded member turn), never both (a double read).
        """
        ctx = member_turn_context(MEMBER, lifecycle)
        assert ctx.deliver_section != ctx.enforce_rules_gate, (
            f"{lifecycle}: a member turn must either deliver the current section "
            "or enforce the standalone rules gate, exactly one of the two"
        )

    def test_minimal_is_the_deliberate_exception(self):
        """MINIMAL turns are never member threads by contract (member is "").

        The verdict for a member name arriving anyway mirrors the branch
        structure: the minimal build early-returns before any member handling,
        so neither delivery nor gate runs. Pinned so a change here is a
        deliberate decision, not drift.
        """
        ctx = member_turn_context(MEMBER, MemberLifecycle.MINIMAL)
        assert ctx.deliver_section is False
        assert ctx.enforce_rules_gate is False

    @pytest.mark.parametrize("lifecycle", list(MemberLifecycle))
    def test_delivers_section_property_agrees_with_the_verdict(self, lifecycle):
        """MemberLifecycle.delivers_section IS the delivery half of the
        verdict — one source of truth for both the named-member chokepoint
        call and the mode-keyed chat-runner re-arm."""
        assert member_turn_context(MEMBER, lifecycle).deliver_section is lifecycle.delivers_section

    def test_context_is_immutable(self):
        """The verdict is a value, not a mutable carrier a branch could edit."""
        ctx = member_turn_context(MEMBER, MemberLifecycle.WARM)
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.enforce_rules_gate = False  # type: ignore[misc]

    @pytest.mark.parametrize("bogus", ["warm", "fresh", None, 0, object()])
    def test_unrecognized_lifecycle_is_refused_not_failed_open(self, bogus):
        """Deny by default: a non-enum lifecycle raises instead of yielding the
        one combination the invariant forbids (no delivery, no gate).

        The str mixin is the hazard pinned here: a bare "warm" compares EQUAL
        to MemberLifecycle.WARM while failing the identity/membership tests
        both verdict predicates use, so without the isinstance refusal it
        would silently produce an unbounded member turn.
        """
        with pytest.raises(TypeError):
            member_turn_context(MEMBER, bogus)  # type: ignore[arg-type]


class TestMemberLifecycleMapping:
    """member_lifecycle mirrors build_message's branch structure exactly."""

    @pytest.mark.parametrize(
        "is_new_session,resumed,minimal_context,needs_reinjection",
        list(product([False, True], repeat=4)),
    )
    def test_exhaustive_mapping(self, is_new_session, resumed, minimal_context, needs_reinjection):
        expected: MemberLifecycle
        if is_new_session:
            # minimal_context beats resumed: a minimal resumed build
            # early-returns before any member handling.
            if minimal_context:
                expected = MemberLifecycle.MINIMAL
            elif resumed:
                expected = MemberLifecycle.SLIM_RESUME
            else:
                expected = MemberLifecycle.FRESH
        elif needs_reinjection:
            # On warm turns minimal_context plays no role — the reinjection
            # and gate branches never test it.
            expected = MemberLifecycle.WARM_REINJECTION
        else:
            expected = MemberLifecycle.WARM
        got = member_lifecycle(
            is_new_session=is_new_session,
            resumed=resumed,
            minimal_context=minimal_context,
            needs_reinjection=needs_reinjection,
        )
        assert got is expected

    def test_every_lifecycle_state_is_reachable(self):
        """The mapping covers the whole enum — no orphaned state."""
        seen = {
            member_lifecycle(is_new_session=n, resumed=r, minimal_context=m, needs_reinjection=j)
            for n, r, m, j in product([False, True], repeat=4)
        }
        assert seen == set(MemberLifecycle)


class TestMemberThreadSessionAlias:
    """The canonical dashboard session alias derivation."""

    def test_alias_shape(self):
        assert member_thread_session_alias("code-reviewer") == (
            "dashboard:" + member_slot_key("code-reviewer")
        )

    def test_alias_is_a_member_session_key(self):
        """The alias round-trips through the member-key predicate."""
        assert is_member_session_key(member_thread_session_alias("code-reviewer"))

    def test_bad_slug_is_refused(self):
        with pytest.raises(MemberSlugError):
            member_thread_session_alias("../escape")


def _find_function(tree: ast.AST, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _guarded_node_ids(func: ast.AST, attr: str) -> set[int]:
    """ids of all nodes inside an ``if`` whose test reads ``.<attr>``."""
    guarded: set[int] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.If) and any(
            isinstance(t, ast.Attribute) and t.attr == attr for t in ast.walk(node.test)
        ):
            for stmt in node.body:
                for inner in ast.walk(stmt):
                    guarded.add(id(inner))
    return guarded


def _calls_named(func: ast.AST, name: str) -> list[ast.Call]:
    out = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id == name) or (
                isinstance(f, ast.Attribute) and f.attr == name
            ):
                out.append(node)
    return out


class TestChokepointWiring:
    """AST guards: every delivery branch routes through the chokepoint.

    These are structural, not behavioral: they fail when a future edit
    reintroduces a hand-coordinated member branch that bypasses
    ``member_turn_context``, which is exactly the silent-skip failure mode
    the chokepoint exists to prevent.
    """

    def test_build_message_delivery_routes_through_chokepoint(self):
        """Every member-section injection in build_message is guarded by the
        chokepoint's ``deliver_section`` verdict."""
        import kiro_crew.context as context_mod

        tree = ast.parse(inspect.getsource(context_mod))
        build_message = _find_function(tree, "build_message")
        assert _calls_named(
            build_message, "member_turn_context"
        ), "build_message must consult member_turn_context"
        section_calls = _calls_named(build_message, "_build_member_section")
        assert section_calls, "expected member-section injection sites in build_message"
        guarded = _guarded_node_ids(build_message, "deliver_section")
        unguarded = [c.lineno for c in section_calls if id(c) not in guarded]
        assert not unguarded, (
            f"_build_member_section called outside a deliver_section guard at "
            f"lines {unguarded}; route the branch through member_turn_context"
        )

    def test_build_message_rules_gate_routes_through_chokepoint(self):
        """The standalone fail-closed rules read runs iff the chokepoint says
        ``enforce_rules_gate`` — not under a hand-written lifecycle test."""
        import kiro_crew.context as context_mod

        tree = ast.parse(inspect.getsource(context_mod))
        build_message = _find_function(tree, "build_message")
        gate_calls = _calls_named(build_message, "read_member_rules")
        assert gate_calls, "expected the standalone rules-gate read in build_message"
        guarded = _guarded_node_ids(build_message, "enforce_rules_gate")
        unguarded = [c.lineno for c in gate_calls if id(c) not in guarded]
        assert not unguarded, (
            f"read_member_rules called outside an enforce_rules_gate guard at "
            f"lines {unguarded}; route the branch through member_turn_context"
        )

    def test_build_message_derives_lifecycle_from_its_own_inputs(self):
        """The lifecycle handed to the chokepoint comes from member_lifecycle
        with build_message's real branch inputs, so the chokepoint and the
        surrounding branch structure cannot disagree on the turn's state."""
        import kiro_crew.context as context_mod

        tree = ast.parse(inspect.getsource(context_mod))
        build_message = _find_function(tree, "build_message")
        lifecycle_calls = _calls_named(build_message, "member_lifecycle")
        assert lifecycle_calls, "build_message must derive the lifecycle via member_lifecycle"
        kw = {k.arg for call in lifecycle_calls for k in call.keywords}
        assert {"is_new_session", "resumed", "minimal_context", "needs_reinjection"} <= kw

    def test_build_session_context_delivery_routes_through_chokepoint(self):
        """The fresh-session injection (FRESH leg) consults the chokepoint."""
        import kiro_crew.context as context_mod

        tree = ast.parse(inspect.getsource(context_mod))
        build_session_context = _find_function(tree, "build_session_context")
        section_calls = _calls_named(build_session_context, "_build_member_section")
        assert section_calls, "expected the FRESH-leg injection in build_session_context"
        guarded = _guarded_node_ids(build_session_context, "deliver_section")
        unguarded = [c.lineno for c in section_calls if id(c) not in guarded]
        assert not unguarded, (
            f"_build_member_section called outside a deliver_section guard at "
            f"lines {unguarded} in build_session_context"
        )

    def test_chat_runner_pending_flag_routes_through_chokepoint(self):
        """The finally-block re-arm's member flag reads the chokepoint's own
        delivery verdict (MemberLifecycle.delivers_section), not a hand-written
        is_new test. Mode-keyed rather than name-keyed: the member NAME is
        resolved later, at the context build, and delivery is at stake from
        the moment the session client exists."""
        import kiro_crew.dashboard.chat_runner as chat_runner_mod

        tree = ast.parse(inspect.getsource(chat_runner_mod))
        assigns = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "_member_session_start_pending"
                for t in node.targets
            )
        ]
        assert assigns, "expected _member_session_start_pending assignments in chat_runner"
        derived = [
            node
            for node in assigns
            # The turn-scope init (`= False`) stays a literal; every DERIVED
            # assignment must come from the chokepoint.
            if not (isinstance(node.value, ast.Constant) and node.value.value is False)
        ]
        assert derived, "expected a derived _member_session_start_pending assignment"
        for node in derived:
            dump = ast.dump(node.value)
            assert "member_lifecycle" in dump and "delivers_section" in dump, (
                f"_member_session_start_pending at line {node.lineno} does not "
                "route through member_lifecycle(...).delivers_section"
            )

    def test_rules_handler_uses_canonical_alias(self):
        """The members handler never hand-builds the ``dashboard:<slot key>``
        alias — the derivation lives in member_thread_session_alias, so the
        reinjection flag, the thread's history key, and the roster's
        transcript-tail probe cannot drift apart. Any f-string carrying a
        ``dashboard:`` constant is flagged, whatever it interpolates."""
        from kiro_crew.dashboard.handlers import members as members_handler_mod

        tree = ast.parse(inspect.getsource(members_handler_mod))
        hand_built = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.JoinedStr)
            and any(
                isinstance(part, ast.Constant)
                and isinstance(part.value, str)
                and "dashboard:" in part.value
                for part in node.values
            )
        ]
        assert not hand_built, (
            f"hand-built member session alias (f-string over a 'dashboard:' "
            f"constant) at lines {hand_built}; use member_thread_session_alias"
        )
        marks = _calls_named(tree, "mark_needs_reinjection")
        assert marks, "expected the rules-write reinjection flag in the members handler"
        for call in marks:
            assert "member_thread_session_alias" in ast.dump(call), (
                f"mark_needs_reinjection at line {call.lineno} does not derive its "
                "key from member_thread_session_alias"
            )

    def test_slim_resume_is_derived_from_the_chokepoint_lifecycle(self):
        """``slim_resume`` reads the chokepoint's lifecycle instead of
        re-encoding ``resumed and not minimal_context``: two independent
        spellings of one predicate can drift, and the divergence that matters
        (branch true, lifecycle no longer SLIM_RESUME) would leave a resumed
        member session on a stale [PERMANENT RULES] snapshot silently."""
        import kiro_crew.context as context_mod

        tree = ast.parse(inspect.getsource(context_mod))
        build_message = _find_function(tree, "build_message")
        assigns = [
            node
            for node in ast.walk(build_message)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "slim_resume" for t in node.targets)
        ]
        assert assigns, "expected the slim_resume derivation in build_message"
        for node in assigns:
            dump = ast.dump(node.value)
            assert "lifecycle" in dump and "SLIM_RESUME" in dump, (
                f"slim_resume at line {node.lineno} re-encodes the resume predicate "
                "instead of reading the chokepoint's lifecycle"
            )
