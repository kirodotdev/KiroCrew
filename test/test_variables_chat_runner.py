"""Security tests for {{variable}} expansion in the dashboard turn pipeline.

The property under test is that expansion reaches the user's OWN text and nothing
else. Two halves are needed, because either alone is weak evidence:

* Behaviour at the seam — the resolvers, the expander and the join helpers, driven
  in the order the pipeline drives them.
* A source-order guard proving the pipeline actually composes them in that order.
  Without it, these tests would only prove that a safe ordering exists, not that
  the shipped code uses it.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import chat_runner as runner_mod
from kiro_crew.dashboard.chat_runner import _ChatSlot


def _expansion_gate_line() -> str:
    """Return the ``if`` line guarding the message-expansion call.

    Source-anchored tests on this feature kept breaking because they pinned the whole
    gate expression, which legitimately grew conjuncts twice. This locates the
    expansion site and hands back its guard so each test can assert only the conjunct
    it cares about.

    Raises rather than returning empty when the site cannot be found: a helper that
    returned "" would make every conjunct assertion below vacuously fail-open the
    moment the code moved.
    """
    source = inspect.getsource(runner_mod).splitlines()
    # Matches the bare call AND the offloaded form (asyncio.to_thread passes the
    # helper by NAME, so there is no paren after it). Anchored on the identifier
    # rather than on the call syntax, which has now changed twice.
    hits = [
        i for i, ln in enumerate(source) if "_expand_message_variables" in ln and "def " not in ln
    ]
    if len(hits) != 1:
        raise AssertionError(
            f"expected exactly one message-expansion site, found {len(hits)}; "
            "re-point these guards at its new home rather than deleting them"
        )
    for i in range(hits[0] - 1, -1, -1):
        stripped = source[i].strip()
        if stripped.startswith("if ") and stripped.endswith(":"):
            return stripped
    raise AssertionError("found the expansion site but no enclosing if-guard above it")


SENTINEL = "SENTINEL-VALUE-9d1f"


def _slot(agent: str = "") -> _ChatSlot:
    slot = _ChatSlot("chat-vars-1")
    slot._titled = True
    slot.agent = agent
    return slot


def _state() -> MagicMock:
    state = MagicMock()
    state.push_slots_update = MagicMock()
    return state


def _skills(resolved: list[tuple[str, str, str]]) -> MagicMock:
    skills = MagicMock()
    skills.resolve_dollar_skills.return_value = resolved
    skills.has_dollar_candidate.return_value = bool(resolved)
    return skills


def _expand(message: str, values: dict[str, str], slot: _ChatSlot, state: MagicMock) -> str:
    """Run the expander with a fixed variable map, as the pipeline would."""
    with patch.object(chat_runner, "resolve_variables") as rv:
        rv.return_value = MagicMock(values=values)
        with patch.object(chat_runner.KiroCrewConfig, "load", classmethod(lambda cls: MagicMock())):
            return chat_runner._expand_message_variables(message, state, slot)


class TestImportedTextIsNeverExpanded:
    def test_a_skill_body_token_is_left_literal(self):
        """A skill installed from the public registry must not be able to read a
        variable by referencing it in its own body."""
        state, slot = _state(), _slot()
        body = "Step one: call {{apiToken}} then report."
        with patch.object(
            chat_runner, "_get_skills", return_value=_skills([("$dep", "dep", body)])
        ):
            authored, blocks, count = chat_runner._resolve_dollar_skills(
                "run $dep", state, slot, "dashboard:x"
            )
        assert count == 1

        authored = _expand(authored, {"apiToken": SENTINEL}, slot, state)
        assembled = chat_runner._join_skill_parts(authored, blocks)

        assert SENTINEL not in assembled
        assert "{{apiToken}}" in assembled

    def test_a_prompt_body_token_is_left_literal(self, tmp_path: Path):
        state, slot = _state(), _slot()
        prompt = tmp_path / "sop.md"
        prompt.write_text("Follow this using {{apiToken}}.", encoding="utf-8")
        match = {"path": str(prompt), "fullName": "sop"}

        with patch.object(chat_runner, "_find_prompt", return_value=match):
            authored, blocks, status = chat_runner._resolve_prompt_mention(
                "@sop and also this", state, slot
            )
        assert status == "ok"
        assert authored == "and also this"

        authored = _expand(authored, {"apiToken": SENTINEL}, slot, state)
        assembled = chat_runner._join_prompt_parts(authored, blocks)

        assert SENTINEL not in assembled
        assert "{{apiToken}}" in assembled

    def test_the_users_own_trailing_text_still_expands(self, tmp_path: Path):
        """The mirror of the above: confinement must not disable the feature."""
        state, slot = _state(), _slot()
        prompt = tmp_path / "sop.md"
        prompt.write_text("Imported body.", encoding="utf-8")
        with patch.object(
            chat_runner, "_find_prompt", return_value={"path": str(prompt), "fullName": "sop"}
        ):
            authored, blocks, _ = chat_runner._resolve_prompt_mention(
                "@sop use {{apiToken}}", state, slot
            )
        authored = _expand(authored, {"apiToken": SENTINEL}, slot, state)
        assert SENTINEL in chat_runner._join_prompt_parts(authored, blocks)


class TestImportedPromptBodiesAreNotExpandedOnReentry:
    """`/prompts get` re-enters `_run_chat` at depth 1 with the resolved PROMPT BODY
    as the message.

    That body is Imported_Text — it can come from a packaged or registry prompt — and
    `is_slash` is False on the re-entry, so guarding expansion on `is_slash` alone let
    a `{{NAME}}` inside an imported prompt resolve and put a configured value into
    untrusted prompt content. That is the one invariant the whole feature rests on.
    """

    def test_expansion_is_gated_on_the_prompt_depth(self):
        """Asserts the depth CONJUNCT, not the whole expression.

        Pinning the exact gate line has broken three times on this feature as the
        gate legitimately gained conditions (a depth check, then an
        ``operator_authored`` check). What this test is actually for is the depth
        property, so it locates the expansion site and checks that its guard mentions
        the depth -- order-independent, and indifferent to further conjuncts.
        """
        gate = _expansion_gate_line()
        assert (
            "_prompt_depth < 1" in gate
        ), f"expansion is not gated on the recursive prompt depth; guard reads: {gate!r}"

    def test_the_recursive_call_really_passes_depth_one(self):
        """Positive control: the gate is only meaningful if the re-entry it guards
        actually arrives at depth 1. If that call ever stopped passing the depth, the
        gate above would still read as present while protecting nothing."""
        source = inspect.getsource(runner_mod)
        assert "_run_chat(state, slot, expanded, _prompt_depth=1)" in source

    def test_the_at_mention_gate_uses_the_same_depth(self):
        """The precedent this follows: the `@prompt` resolver was already gated on
        the same depth, for the same reason."""
        source = inspect.getsource(runner_mod)
        assert 'message.startswith("@") and not is_slash and _prompt_depth < 1' in source

    """A value that happens to contain `$skill` or `@prompt` must not load anything.

    Both tests here previously proved nothing: they asserted `"$" not in raw` (a
    property of a literal written two lines above), passed a literal `[]` as the
    block list so the sentinel could not appear whatever the code did, and called
    `assert_not_called()` on mocks that were never wired into the path — so
    reverting the fix left them green. They now run the resolvers the pipeline
    actually uses, in the pipeline's order.
    """

    def test_a_value_naming_a_skill_is_never_resolved(self):
        """The resolver runs BEFORE expansion, so the `$dep` a value introduces
        arrives after its only chance to be resolved has passed."""
        state, slot = _state(), _slot()
        skills = _skills([("$dep", "dep", "SHOULD NOT LOAD")])
        raw = "please do {{ref}}"

        with patch.object(chat_runner, "_get_skills", return_value=skills):
            # Pipeline order: resolve $skill on the AUTHORED text first...
            resolved, skill_blocks, _n = chat_runner._resolve_dollar_skills(
                raw, state, slot, "sess-1"
            )
            # ...then expand, which is what introduces "$dep".
            expanded = _expand(resolved, {"ref": "$dep"}, slot, state)
            assembled = chat_runner._join_skill_parts(expanded, skill_blocks)

        assert "$dep" in expanded, "the value should survive verbatim, unresolved"
        assert (
            "SHOULD NOT LOAD" not in assembled
        ), "a skill body reached the assembled message from a variable value"
        # Never consulted, because the text it saw held no '$'.
        skills.resolve_dollar_skills.assert_not_called()

    def test_the_skill_resolver_is_genuinely_wired_at_that_patch_point(self):
        """Positive control for the test above, and the reason it is not vacuous.

        "The resolver was not called" is exactly what the previous, broken version
        of that test asserted — and it passed because the mock had never been wired
        into the path at all. This proves the patch point is live: the SAME mock IS
        consulted when the authored text really does carry a `$token`. So a future
        edit that expanded before resolving would reach it, and the assertion above
        would fail rather than silently keep passing.
        """
        state, slot = _state(), _slot()
        skills = _skills([("$dep", "dep", "SHOULD NOT LOAD")])

        with patch.object(chat_runner, "_get_skills", return_value=skills):
            chat_runner._resolve_dollar_skills("please do $dep", state, slot, "sess-1")

        skills.resolve_dollar_skills.assert_called_once()

    def test_a_value_naming_a_prompt_is_never_inlined(self, tmp_path: Path):
        """Same ordering, for `@prompt`: the mention gate has already run."""
        state, slot = _state(), _slot()
        prompt = tmp_path / "sop.md"
        prompt.write_text("SHOULD NOT INLINE", encoding="utf-8")
        raw = "check {{ref}}"

        with patch.object(chat_runner, "_find_prompt", return_value=prompt) as finder:
            resolved, prompt_blocks, _status = chat_runner._resolve_prompt_mention(raw, state, slot)
            expanded = _expand(resolved, {"ref": "@sop"}, slot, state)

        assert "@sop" in expanded, "the value should survive verbatim, uninlined"
        # No block was imported, so there is nothing for assembly to prepend — the
        # body cannot reach the turn at all.
        assert prompt_blocks == []
        finder.assert_not_called()

    def test_the_prompt_finder_is_genuinely_wired_at_that_patch_point(self, tmp_path: Path):
        """Positive control, for the same reason as the skills one: without it,
        `finder.assert_not_called()` above could pass because nothing is wired.

        ``_find_prompt`` returns a prompt RECORD (a dict), not a path — mocking it
        with a Path made the resolver fail on subscripting rather than exercising it.
        """
        state, slot = _state(), _slot()
        body = tmp_path / "sop.md"
        body.write_text("REAL MENTION BODY", encoding="utf-8")
        # The resolver reads the body from ``match["path"]``, so the record has to
        # point at a real file rather than carry the text inline.
        record = {
            "name": "sop",
            "fullName": "sop",
            "package": "local",
            "path": str(body),
        }

        with patch.object(chat_runner, "_find_prompt", return_value=record) as finder:
            _resolved, blocks, status = chat_runner._resolve_prompt_mention(
                "@sop do the thing", state, slot
            )

        finder.assert_called_once()
        assert status == "ok", f"the mention did not resolve: {status}"
        assert blocks and "REAL MENTION BODY" in blocks[0]


class TestAssemblyIsUnchangedWithoutVariables:
    """The refactor split resolution from assembly; the emitted string must be
    byte-identical to what the single-string helpers produced."""

    def test_prompt_join_matches_the_previous_format(self):
        authored, blocks = "extra words", ["Execute the following instructions:\n\nBODY"]
        assert chat_runner._join_prompt_parts(authored, blocks) == (
            "Execute the following instructions:\n\nBODY"
            "\n\n---\nAdditional context from user: extra words"
        )

    def test_prompt_join_without_user_text(self):
        blocks = ["Execute the following instructions:\n\nBODY"]
        assert chat_runner._join_prompt_parts("", blocks) == blocks[0]

    def test_skill_join_matches_the_previous_format(self):
        out = chat_runner._join_skill_parts("run $a $b", ["[Skill: a]\n\nA", "[Skill: b]\n\nB"])
        assert out == "run $a $b\n\n[Skill: a]\n\nA\n\n---\n\n[Skill: b]\n\nB"

    def test_skill_join_with_no_blocks_is_the_authored_text(self):
        assert chat_runner._join_skill_parts("plain", []) == "plain"

    def test_wrapper_still_returns_one_string(self):
        state, slot = _state(), _slot()
        with patch.object(
            chat_runner, "_get_skills", return_value=_skills([("$dep", "dep", "BODY")])
        ):
            expanded, count = chat_runner._expand_dollar_skills(
                "run $dep", state, slot, "dashboard:x"
            )
        assert count == 1
        assert expanded == "run $dep\n\n[Skill: dep]\n\nBODY"


class TestExpanderBehaviour:
    def test_no_token_short_circuits_without_resolving(self):
        state, slot = _state(), _slot()
        with patch.object(chat_runner, "resolve_variables") as rv:
            out = chat_runner._expand_message_variables("no tokens here", state, slot)
        assert out == "no tokens here"
        rv.assert_not_called()

    def test_resolution_failure_leaves_the_message_intact(self):
        state, slot = _state(), _slot()
        with patch.object(chat_runner, "resolve_variables", side_effect=RuntimeError("boom")):
            with patch.object(
                chat_runner.KiroCrewConfig, "load", classmethod(lambda cls: MagicMock())
            ):
                out = chat_runner._expand_message_variables("use {{a}}", state, slot)
        assert out == "use {{a}}"

    def test_unresolved_name_is_surfaced_once_and_left_literal(self):
        state, slot = _state(), _slot()
        out = _expand("use {{missing}}", {"other": "1"}, slot, state)
        assert out == "use {{missing}}"
        surfaced = [m for m in slot.messages if "missing" in str(m)]
        assert len(surfaced) == 1


def test_pipeline_expands_before_joining_imported_bodies():
    """Guard the ORDER in the shipped pipeline, not just in these tests.

    If a future edit joins the imported bodies before expanding, every behavioural
    test above still passes while the real turn leaks values into skill bodies.
    """
    # Scope to the turn pipeline by taking the ENCLOSING FUNCTION's source rather
    # than a fixed character window. The previous form sliced
    # `source[start:start + 6000]`, which silently dropped the tail of the pipeline
    # the moment anything above it grew -- adding a comment to the expansion gate was
    # enough to push `_join_skill_parts(` outside the window and fail the guard with
    # "pipeline no longer contains", which reads like a deletion rather than a
    # measurement artefact. A function's own source cannot go out of scope.
    region = inspect.getsource(runner_mod._run_chat)
    assert "prompt_blocks: list[str] = []" in region, (
        "the turn pipeline no longer binds prompt_blocks inside _run_chat; "
        "re-point this guard at its new home rather than widening the region"
    )

    def _at(needle: str) -> int:
        idx = region.find(needle)
        assert idx >= 0, f"pipeline no longer contains {needle!r}"
        return idx

    resolve_prompt = _at("_resolve_prompt_mention(")
    resolve_skills = _at("_resolve_dollar_skills(")
    # No paren: the offloaded form passes the helper by name to asyncio.to_thread.
    expand = _at("_expand_message_variables")
    join_prompt = _at("_join_prompt_parts(")
    join_skills = _at("_join_skill_parts(")

    assert resolve_prompt < expand, "@prompt must resolve against pre-expansion text"
    assert resolve_skills < expand, "$skill must resolve against pre-expansion text"
    assert expand < join_prompt, "imported prompt body must be joined AFTER expansion"
    assert expand < join_skills, "imported skill bodies must be joined AFTER expansion"


def test_expansion_is_skipped_for_slash_commands():
    """Slash commands are runner directives, not agent prose.

    Asserts each required CONJUNCT as a substring of the guard, order-independent.
    Pinning the expression (even as a regex anchored at ``if not is_slash``) has
    broken repeatedly as the gate legitimately grew conditions -- a
    ``_prompt_depth < 1`` check, then an ``operator_authored`` check that had to come
    first. Each conjunct guards a different disclosure, so all three are named here
    and none of them cares where in the expression it sits.
    """
    gate = _expansion_gate_line()
    for conjunct, why in (
        ("not is_slash", "a slash command is a runner directive, not operator prose"),
        ("_prompt_depth < 1", "a depth-1 re-entry carries an imported prompt body"),
        ("operator_authored", "a channel participant's text must never expand"),
    ):
        assert conjunct in gate, f"guard lost {conjunct!r} ({why}); reads: {gate!r}"


@pytest.mark.parametrize("token", ["{{ }}", "{{1abc}}", "{{a-b}}"])
def test_malformed_tokens_are_left_alone(token: str):
    state, slot = _state(), _slot()
    assert _expand(f"x {token} y", {"a": "1"}, slot, state) == f"x {token} y"


class TestTheMirrorPayloadIsCapturedNotReconstructed:
    """The mirror legs echo into a LINKED channel whose participants are not the
    operator, so the payload must be what the operator TYPED.

    Captured once, above every step that injects: expansion, the imported ``@prompt``
    body, each ``$skill`` body, the cancelled-turn preamble and the subagent-failure
    notices. An earlier revision reconstructed it instead, undoing the expansion on the
    joined text -- which cannot work, because ``_join_prompt_parts`` puts the imported
    body FIRST and a bounded swap cannot tell the operator's own run from an identical
    run inside that body. These guards pin the capture, since a behavioural test would
    have to drive the whole turn engine.
    """

    def _runner_source(self) -> list[str]:
        return inspect.getsource(runner_mod).splitlines()

    def _sole_capture(self) -> int:
        source = self._runner_source()
        hits = [i for i, ln in enumerate(source) if ln.strip() == "_user_msg_for_mirror = message"]
        assert len(hits) == 1, (
            f"expected exactly ONE mirror capture, found {len(hits)}; a second "
            "assignment re-derives the payload from a mutated `message`"
        )
        return hits[0]

    def test_the_payload_is_captured_exactly_once(self):
        self._sole_capture()

    def test_nothing_re_derives_the_payload_after_the_capture(self):
        """Any later ``_user_msg_for_mirror =`` would pick up whatever had been joined
        or prepended onto ``message`` by then."""
        source = self._runner_source()
        assigns = [i for i, ln in enumerate(source) if "_user_msg_for_mirror =" in ln]
        assert len(assigns) == 1, f"mirror payload is assigned {len(assigns)} times, want 1"

    def test_the_capture_precedes_every_injecting_step(self):
        """Order is the whole property: each of these puts content the operator never
        typed into ``message``, so a capture below any of them discloses it."""
        source = self._runner_source()
        capture = self._sole_capture()

        def _first_at_or_after(needle: str, start: int) -> int:
            for i in range(start, len(source)):
                if needle in source[i] and "def " not in source[i]:
                    return i
            raise AssertionError(f"{needle!r} not found after the mirror capture")

        for needle, what in (
            ("_expand_message_variables", "a variable's VALUE"),
            ("_join_prompt_parts(", "the imported @prompt body"),
            ("_join_skill_parts(", "each $skill body"),
        ):
            assert capture < _first_at_or_after(needle, capture), (
                f"mirror capture must precede {needle} -- otherwise {what} reaches "
                "the linked channel"
            )

    def test_the_capture_precedes_the_context_builder_prepends(self):
        """The preamble and the subagent-failure notices are runner-authored, and the
        turn engine prepends both onto ``message`` further down."""
        source = self._runner_source()
        capture = self._sole_capture()
        for needle, what in (
            ("message = preamble + ", "the cancelled-turn preamble"),
            (".join(failures) + ", "the subagent-failure notices"),
        ):
            hits = [i for i, ln in enumerate(source) if needle in ln]
            assert hits, f"{needle!r} not found; re-point this guard"
            assert capture < min(hits), (
                f"mirror capture must precede {needle!r} -- otherwise {what} reaches "
                "the linked channel"
            )

    def test_the_expansion_gate_does_not_move_above_the_capture(self):
        """The capture reads ``message`` directly, so it is only the operator's own
        text while it still sits above the expansion call."""
        source = self._runner_source()
        capture = self._sole_capture()
        expand = [
            i
            for i, ln in enumerate(source)
            if "_expand_message_variables" in ln and "def " not in ln
        ]
        assert len(expand) == 1
        assert capture < expand[0]
