"""Tests for ``kiro_crew.workflows.preview`` — the static plan behind the graph view.

Every assertion here is about a DECISION the previewer makes, because the whole value
of the module is what it refuses to predict. The sharp cases:

* an agent inside a branch or loop is drawn, but never as certain;
* ``ctx.parallel`` expands a literal task list and refuses a computed one;
* a modelled call's arguments are not counted twice;
* a phase whose title is not a literal is never paired with a phase name.
"""

from __future__ import annotations

import pytest

from kiro_crew.workflows.preview import (
    MAX_LABEL_CHARS,
    MAX_PHASE_TITLE_CHARS,
    MAX_PLAN_NODES,
    MAX_PLAN_PHASES,
    MODELLED_CTX_METHODS,
    NARRATION_CTX_METHODS,
    RUNNER_PROMPT_LABEL_CHARS,
    plan_from_source,
)


def _plan(body: str) -> dict:
    """Plan for a script whose ``workflow`` body is *body* (indented one level)."""
    indented = "\n".join("    " + line if line.strip() else line for line in body.splitlines())
    plan = plan_from_source(f"META = {{}}\n\nasync def workflow(ctx):\n{indented}\n")
    assert plan is not None, "a well-formed script must yield a plan"
    return plan


def _titles(plan: dict) -> list[str]:
    return [p["title"] for p in plan["phases"]]


def _nodes(plan: dict, title: str) -> list[dict]:
    for phase in plan["phases"]:
        if phase["title"] == title:
            return phase["nodes"]
    raise AssertionError(f"no phase titled {title!r} in {_titles(plan)}")


class TestNoPlanAtAll:
    """``None`` and an empty plan must not look the same to the UI."""

    def test_a_source_that_is_not_python_has_no_plan(self):
        assert plan_from_source('{"tasks": [{"id": 1,}]}') is None

    def test_a_script_without_the_entrypoint_has_no_plan(self):
        assert plan_from_source("META = {}\n\nasync def other(ctx):\n    pass\n") is None

    def test_an_entrypoint_taking_no_context_has_no_plan(self):
        assert plan_from_source("async def workflow():\n    pass\n") is None

    def test_an_empty_source_has_no_plan(self):
        assert plan_from_source("   \n") is None
        assert plan_from_source("") is None

    def test_a_non_string_source_has_no_plan(self):
        assert plan_from_source(None) is None  # type: ignore[arg-type]

    def test_a_source_carrying_a_nul_byte_has_no_plan(self):
        # ast.parse raises ValueError, not SyntaxError, for an embedded NUL.
        assert plan_from_source("async def workflow(ctx):\n    pass\n\x00") is None

    def test_an_entrypoint_with_an_empty_body_yields_an_empty_plan_not_none(self):
        plan = plan_from_source("async def workflow(ctx):\n    pass\n")
        assert plan == {"phases": [], "truncated": False, "titleLimit": MAX_PHASE_TITLE_CHARS}


class TestPhases:
    def test_a_literal_phase_title_becomes_a_certain_phase(self):
        plan = _plan('with ctx.phase("Read"):\n    await ctx.agent("look")\n')
        assert _titles(plan) == ["Read"]
        assert plan["phases"][0]["certain"] is True

    def test_a_bare_phase_call_opens_a_phase_for_the_statements_after_it(self):
        # The runner's phase persists until the next call; __exit__ restores nothing.
        plan = _plan('ctx.phase("Read")\nawait ctx.agent("look")\n')
        assert _titles(plan) == ["Read"]
        assert [n["label"] for n in _nodes(plan, "Read")] == ["look"]

    def test_a_phase_opened_inside_a_with_block_stays_current_after_it(self):
        plan = _plan(
            'with ctx.phase("Read"):\n' '    await ctx.agent("look")\n' 'await ctx.agent("after")\n'
        )
        assert _titles(plan) == ["Read"]
        assert [n["label"] for n in _nodes(plan, "Read")] == ["look", "after"]

    def test_work_before_the_first_phase_lands_in_the_unnamed_leading_phase(self):
        plan = _plan('await ctx.agent("first")\nwith ctx.phase("Read"):\n    pass\n')
        assert _titles(plan) == ["", "Read"]
        assert [n["label"] for n in _nodes(plan, "")] == ["first"]

    def test_re_entering_a_phase_title_reuses_the_same_row(self):
        plan = _plan(
            'with ctx.phase("Read"):\n'
            '    await ctx.agent("a")\n'
            'with ctx.phase("Read"):\n'
            '    await ctx.agent("b")\n'
        )
        assert _titles(plan) == ["Read"]
        assert [n["label"] for n in _nodes(plan, "Read")] == ["a", "b"]

    def test_a_phase_entered_conditionally_is_not_certain(self):
        plan = _plan('if ctx.agent_results:\n    with ctx.phase("Maybe"):\n        pass\n')
        assert _titles(plan) == ["", "Maybe"]
        assert _nodes(plan, "")[0] == {"kind": "unknown", "label": "if", "certain": False}
        maybe = next(p for p in plan["phases"] if p["title"] == "Maybe")
        assert maybe["certain"] is False

    def test_a_phase_certain_once_and_conditional_once_is_not_certain(self):
        plan = _plan(
            'with ctx.phase("Read"):\n'
            "    pass\n"
            "if ctx.agent_results:\n"
            '    with ctx.phase("Read"):\n'
            "        pass\n"
        )
        read = next(p for p in plan["phases"] if p["title"] == "Read")
        assert read["certain"] is False

    def test_a_computed_phase_title_is_marked_rather_than_named(self):
        plan = _plan('with ctx.phase(name):\n    await ctx.agent("x")\n')
        # No phase row invents a title an event could never match, and the body's own
        # node is uncertain because the region it sits in is unpredictable.
        assert _titles(plan) == [""]
        assert _nodes(plan, "") == [
            {"kind": "unknown", "label": "phase", "certain": False},
            {"kind": "agent", "label": "x", "certain": False},
        ]

    def test_a_plain_with_block_is_not_a_phase_and_keeps_its_body_certain(self):
        plan = _plan('with ctx.nudge(idle_secs=1, message="m"):\n    await ctx.agent("x")\n')
        assert _titles(plan) == [""]
        assert _nodes(plan, "") == [{"kind": "agent", "label": "x", "certain": True}]


class TestAgentNodes:
    def test_a_literal_label_wins_over_the_prompt(self):
        plan = _plan('await ctx.agent("a very long prompt", label="short")\n')
        assert _nodes(plan, "")[0]["label"] == "short"

    def test_the_prompt_is_the_label_when_no_label_is_given(self):
        plan = _plan('await ctx.agent("read the diff")\n')
        assert _nodes(plan, "")[0]["label"] == "read the diff"

    def test_a_computed_prompt_still_produces_a_node_with_no_name(self):
        plan = _plan("await ctx.agent(prompt)\n")
        assert _nodes(plan, "") == [{"kind": "agent", "label": "", "certain": True}]

    def test_an_empty_label_loses_to_the_prompt_as_it_does_in_the_run(self):
        # The runner emits ``label or prompt[:40]``, so a falsy label is no label at all.
        # Keeping "" draws a nameless box that acquires a name the moment it runs.
        plan = _plan('await ctx.agent("read the diff", label="")\n')
        assert _nodes(plan, "")[0]["label"] == "read the diff"

    def test_the_prompt_is_read_as_a_keyword_too(self):
        # ``prompt`` is positional-or-keyword in the DSL, so both spellings are labels.
        plan = _plan('await ctx.agent(prompt="read the diff")\n')
        assert _nodes(plan, "")[0]["label"] == "read the diff"

    def test_each_label_source_is_cut_where_the_run_cuts_it(self):
        # A long ``label=`` is the runner's own label, which it never cuts, so the only
        # bound is this module's. A long prompt is cut by the runner at 40, and drawing
        # 60 would rename the box when it lit up.
        from_label = _plan('await ctx.agent("p", label="%s")\n' % ("x" * 500))
        assert len(_nodes(from_label, "")[0]["label"]) == MAX_LABEL_CHARS
        from_prompt = _plan('await ctx.agent("%s")\n' % ("x" * 500))
        assert len(_nodes(from_prompt, "")[0]["label"]) == RUNNER_PROMPT_LABEL_CHARS
        assert RUNNER_PROMPT_LABEL_CHARS < MAX_LABEL_CHARS, "the two caps must differ"

    def test_a_phase_keyword_routes_one_call_without_moving_the_walker(self):
        plan = _plan(
            'with ctx.phase("Main"):\n'
            '    await ctx.agent("side", phase="Other")\n'
            '    await ctx.agent("back")\n'
        )
        assert _titles(plan) == ["Main", "Other"]
        assert [n["label"] for n in _nodes(plan, "Other")] == ["side"]
        assert [n["label"] for n in _nodes(plan, "Main")] == ["back"]

    def test_an_agent_call_not_awaited_is_still_a_node(self):
        plan = _plan('tasks = [ctx.agent("a")]\nawait ctx.parallel(tasks)\n')
        # The list literal's agent call is real work; the parallel over a NAME is the
        # part whose width is unknown.
        assert [n["kind"] for n in _nodes(plan, "")] == ["agent", "unknown"]


class TestUnpredictableRegions:
    def test_a_loop_is_marked_and_its_body_is_never_certain(self):
        plan = _plan('for item in items:\n    await ctx.agent("per item")\n')
        assert _nodes(plan, "") == [
            {"kind": "unknown", "label": "for", "certain": False},
            {"kind": "agent", "label": "per item", "certain": False},
        ]

    def test_both_branches_of_an_if_are_drawn_and_neither_is_certain(self):
        plan = _plan('if flag:\n    await ctx.agent("yes")\nelse:\n    await ctx.agent("no")\n')
        assert _nodes(plan, "") == [
            {"kind": "unknown", "label": "if", "certain": False},
            {"kind": "agent", "label": "yes", "certain": False},
            {"kind": "agent", "label": "no", "certain": False},
        ]

    def test_a_try_marks_its_handler_and_finally_bodies(self):
        plan = _plan(
            "try:\n"
            '    await ctx.agent("try")\n'
            "except Exception:\n"
            '    await ctx.agent("except")\n'
            "finally:\n"
            '    await ctx.agent("finally")\n'
        )
        assert [n["label"] for n in _nodes(plan, "")] == ["try", "try", "finally", "except"]
        assert all(n["certain"] is False for n in _nodes(plan, ""))

    def test_a_while_loop_is_marked(self):
        plan = _plan('while go:\n    await ctx.agent("again")\n')
        assert _nodes(plan, "")[0]["label"] == "while"

    def test_an_async_for_is_marked_as_a_loop(self):
        plan = _plan('async for item in stream:\n    await ctx.agent("x")\n')
        assert _nodes(plan, "")[0]["label"] == "for"

    def test_a_match_statement_is_marked(self):
        plan = _plan(
            "match kind:\n"
            '    case "a":\n'
            '        await ctx.agent("a")\n'
            "    case _:\n"
            '        await ctx.agent("b")\n'
        )
        assert _nodes(plan, "")[0] == {"kind": "unknown", "label": "match", "certain": False}
        assert [n["label"] for n in _nodes(plan, "")[1:]] == ["a", "b"]

    def test_a_conditional_expression_is_marked(self):
        plan = _plan('r = await (ctx.agent("a") if flag else ctx.agent("b"))\n')
        assert _nodes(plan, "")[0] == {"kind": "unknown", "label": "if", "certain": False}
        assert all(n["certain"] is False for n in _nodes(plan, ""))

    def test_a_comprehension_is_marked_as_a_loop(self):
        plan = _plan("rows = [ctx.agent(p) for p in prompts]\n")
        assert _nodes(plan, "") == [
            {"kind": "unknown", "label": "for", "certain": False},
            {"kind": "agent", "label": "", "certain": False},
        ]

    def test_a_nested_workflow_is_marked(self):
        plan = _plan('await ctx.workflow("other")\n')
        assert _nodes(plan, "") == [{"kind": "unknown", "label": "nested", "certain": False}]

    def test_a_pipeline_is_marked_even_with_literal_items(self):
        plan = _plan('await ctx.pipeline(["a", "b"], lambda p: ctx.agent(p))\n')
        assert _nodes(plan, "") == [{"kind": "unknown", "label": "pipeline", "certain": False}]

    def test_calling_a_helper_defined_in_the_script_is_marked(self):
        plan = _plan("async def helper(c):\n" '    await c.agent("hidden")\n' "await helper(ctx)\n")
        # The helper's own body is never read, so the region is reported as
        # unpredictable instead of contributing nothing at all.
        assert _nodes(plan, "") == [{"kind": "unknown", "label": "helper", "certain": False}]

    def test_narration_calls_draw_nothing(self):
        plan = _plan('ctx.log("hello")\nawait ctx.approve("ok?")\n')
        assert plan == {"phases": [], "truncated": False, "titleLimit": MAX_PHASE_TITLE_CHARS}

    def test_a_lambda_is_not_counted_where_it_is_defined(self):
        # A lambda runs where it is INVOKED. ``ctx.parallel`` invokes its own thunks and
        # reads them itself; a lambda stored in a variable has not run at all, so
        # counting its body here would draw work that may never happen.
        plan = _plan("stages = [lambda p: ctx.agent(p)]\nawait ctx.pipeline(items, *stages)\n")
        assert _nodes(plan, "") == [{"kind": "unknown", "label": "pipeline", "certain": False}]


class TestParallel:
    def test_a_literal_list_of_agent_calls_expands_one_node_each(self):
        plan = _plan('await ctx.parallel([ctx.agent("a"), ctx.agent("b")])\n')
        assert _nodes(plan, "") == [
            {"kind": "agent", "label": "a", "certain": True},
            {"kind": "agent", "label": "b", "certain": True},
        ]

    def test_a_literal_list_of_thunks_expands_one_node_each(self):
        plan = _plan('await ctx.parallel([lambda: ctx.agent("a"), lambda: ctx.agent("b")])\n')
        assert [n["label"] for n in _nodes(plan, "")] == ["a", "b"]

    def test_a_tuple_of_agent_calls_expands_too(self):
        plan = _plan('await ctx.parallel((ctx.agent("a"),))\n')
        assert [n["kind"] for n in _nodes(plan, "")] == ["agent"]

    def test_a_computed_task_list_is_one_unknown_node_not_a_guessed_width(self):
        plan = _plan("await ctx.parallel([ctx.agent(p) for p in prompts])\n")
        assert _nodes(plan, "") == [{"kind": "unknown", "label": "parallel", "certain": False}]

    def test_a_task_that_is_not_an_agent_call_is_one_unknown_node(self):
        plan = _plan('await ctx.parallel([helper(ctx), ctx.agent("b")])\n')
        assert _nodes(plan, "") == [
            {"kind": "unknown", "label": "task", "certain": False},
            {"kind": "agent", "label": "b", "certain": True},
        ]

    def test_an_expanded_task_is_counted_once_not_twice(self):
        # ``ctx.parallel`` reads its own thunks, so the generic walker must not reach
        # them again — an ast.walk would have produced four nodes here, not two.
        plan = _plan('await ctx.parallel([ctx.agent("a"), ctx.agent("b")])\n')
        assert len(_nodes(plan, "")) == 2

    def test_parallel_inside_a_loop_is_uncertain(self):
        plan = _plan('for x in xs:\n    await ctx.parallel([ctx.agent("a")])\n')
        assert [n["certain"] for n in _nodes(plan, "")] == [False, False]

    def test_parallel_with_no_arguments_is_one_unknown_node(self):
        plan = _plan("await ctx.parallel()\n")
        assert _nodes(plan, "") == [{"kind": "unknown", "label": "parallel", "certain": False}]


class TestContextNameIsTakenFromTheSignature:
    def test_a_renamed_context_parameter_is_followed(self):
        plan = plan_from_source(
            'async def workflow(c):\n    with c.phase("Read"):\n        await c.agent("x")\n'
        )
        assert plan is not None
        assert plan["phases"] == [
            {
                "title": "Read",
                "certain": True,
                "nodes": [{"kind": "agent", "label": "x", "certain": True}],
            }
        ]

    def test_a_call_on_some_other_object_is_not_a_node(self):
        plan = _plan('await other.agent("x")\n')
        assert plan == {"phases": [], "truncated": False, "titleLimit": MAX_PHASE_TITLE_CHARS}


class TestCeilings:
    """Expected values are LITERAL, not read from the module.

    A ceiling test whose input is computed from the ceiling agrees with any value the
    constant takes, so it cannot notice the cap being raised — the exact regression it
    exists to catch. Literals mean moving a cap must be acknowledged in the diff.
    """

    def test_too_many_nodes_truncates_rather_than_growing_without_bound(self):
        assert MAX_PLAN_NODES == 200
        body = "".join(f'await ctx.agent("a{i}")\n' for i in range(225))
        plan = _plan(body)
        assert plan["truncated"] is True
        assert len(_nodes(plan, "")) == 200

    def test_too_many_phases_truncates(self):
        assert MAX_PLAN_PHASES == 50
        body = "".join(f'ctx.phase("p{i}")\n' for i in range(55))
        plan = _plan(body)
        assert plan["truncated"] is True
        assert len(plan["phases"]) == 50

    def test_an_untruncated_plan_says_so(self):
        plan = _plan('await ctx.agent("a")\n')
        assert plan["truncated"] is False

    def test_a_long_phase_title_is_cut_at_the_bound(self):
        assert MAX_PHASE_TITLE_CHARS == 120
        plan = _plan(f'ctx.phase("{"T" * 400}")\n')
        assert len(plan["phases"]) == 1
        assert plan["phases"][0]["title"] == "T" * 120

    def test_the_title_bound_travels_with_the_plan(self):
        # The consumer pairs a plan row with a ``phase_started`` event by title, and the
        # runner emits that title whole, so it can only cut its side to the same length
        # if the plan says what the length is.
        plan = _plan('await ctx.agent("a")\n')
        assert plan["titleLimit"] == MAX_PHASE_TITLE_CHARS

    def test_two_titles_alike_past_the_bound_fold_into_one_row(self):
        # Reuse is by the BOUNDED title, so the cut cannot split one phase into two.
        long_a = "T" * 130 + "-a"
        long_b = "T" * 130 + "-b"
        plan = _plan(f'ctx.phase("{long_a}")\nctx.phase("{long_b}")\n')
        assert [p["title"] for p in plan["phases"]] == ["T" * 120]


class TestACutNeverLeavesACredentialFragment:
    """Redaction runs BEFORE the cut, on every string this module retains.

    A script's own source may legitimately carry a token, and a plan node is where it
    would surface. The response-level redactors match a credential by its full shape, so
    a cut that lands inside one leaves a fragment their patterns do not match -- the cut has
    to happen on already-redacted text, not before it.

    Measured against the real redactor: of a 40-character ``ghp_`` token, a 33-character
    prefix survives unredacted, and of a 20-character AWS key id a 19-character prefix
    does. Each case below places the token so the cut leaves exactly that prefix, which
    is the state a cut-then-redact order ships.
    """

    # (token, the longest prefix the redactor does NOT recognize)
    TOKENS = (
        ("ghp_" + "a" * 36, 33),
        ("AKIAIOSFODNN7EXAMPLE", 19),
    )

    def _pad(self, token: str, survives: int, limit: int) -> str:
        return "x" * (limit - survives)

    @pytest.mark.parametrize("token,survives", TOKENS)
    def test_an_agent_label_leaks_no_fragment(self, token, survives):
        pad = self._pad(token, survives, MAX_LABEL_CHARS)
        plan = _plan(f'await ctx.agent("read it", label="{pad}{token}")\n')
        label = plan["phases"][0]["nodes"][0]["label"]
        assert len(label) <= MAX_LABEL_CHARS
        assert token[:survives] not in label

    @pytest.mark.parametrize("token,survives", TOKENS)
    def test_a_prompt_fallback_label_leaks_no_fragment(self, token, survives):
        pad = self._pad(token, survives, MAX_LABEL_CHARS)
        plan = _plan(f'await ctx.agent("{pad}{token}")\n')
        assert token[:survives] not in plan["phases"][0]["nodes"][0]["label"]

    @pytest.mark.parametrize("token,survives", TOKENS)
    def test_a_phase_title_leaks_no_fragment(self, token, survives):
        pad = self._pad(token, survives, MAX_PHASE_TITLE_CHARS)
        plan = _plan(f'ctx.phase("{pad}{token}")\nawait ctx.agent("a")\n')
        title = plan["phases"][0]["title"]
        assert len(title) <= MAX_PHASE_TITLE_CHARS
        assert token[:survives] not in title


class TestARealisticScript:
    """One script exercising the whole surface, as the graph view will meet it."""

    def test_plan_shape(self):
        plan = _plan(
            'with ctx.phase("Research"):\n'
            "    findings = await ctx.parallel([\n"
            '        lambda: ctx.agent("read the spec", label="spec"),\n'
            '        lambda: ctx.agent("read the code", label="code"),\n'
            "    ])\n"
            'with ctx.phase("Write"):\n'
            '    draft = await ctx.agent("draft it", label="draft")\n'
            "    if draft:\n"
            '        await ctx.agent("polish it", label="polish")\n'
            'with ctx.phase("Ship"):\n'
            "    for f in findings:\n"
            '        await ctx.agent("file it", label="file")\n'
        )
        assert _titles(plan) == ["Research", "Write", "Ship"]
        assert all(p["certain"] for p in plan["phases"])
        assert _nodes(plan, "Research") == [
            {"kind": "agent", "label": "spec", "certain": True},
            {"kind": "agent", "label": "code", "certain": True},
        ]
        assert _nodes(plan, "Write") == [
            {"kind": "agent", "label": "draft", "certain": True},
            {"kind": "unknown", "label": "if", "certain": False},
            {"kind": "agent", "label": "polish", "certain": False},
        ]
        assert _nodes(plan, "Ship") == [
            {"kind": "unknown", "label": "for", "certain": False},
            {"kind": "agent", "label": "file", "certain": False},
        ]


class TestThePreviewerTracksTheDSL:
    """The previewer parses scripts written against the ``WorkflowContext`` Protocol.

    ``test_workflows_conformance.py`` already freezes that Protocol, so a method cannot
    be added to it by accident. What nothing checked is whether THIS module classified
    the new method: a deliberate re-freeze could add a work-spawning verb, and
    ``_visit_call`` would send it to the narration branch and draw nothing, so a phase
    full of real work would render as an empty one. These bind the previewer to the
    contract the conformance test froze.
    """

    def _ctx_surface(self) -> set[str]:
        """Public callable names on the contract a workflow script is written against.

        Derived from the Protocol, never listed here -- a hand-written copy would be
        exactly the enumeration that goes stale, which is the reason this test exists.
        """
        from kiro_crew.workflows import WorkflowContext

        return {
            name
            for name, value in vars(WorkflowContext).items()
            if not name.startswith("_") and (callable(value) or isinstance(value, property))
        }

    def test_the_surface_is_not_empty(self):
        # Control. Every assertion below passes vacuously if the introspection returns
        # nothing, so a renamed or emptied Protocol must fail loudly, not read as clean.
        assert len(self._ctx_surface()) >= 5

    def test_every_ctx_method_is_either_modelled_or_deliberately_silent(self):
        unclassified = self._ctx_surface() - MODELLED_CTX_METHODS - NARRATION_CTX_METHODS
        assert not unclassified, (
            f"ctx method(s) {sorted(unclassified)} are new to WorkflowContext and "
            "unclassified by the previewer. Add each to MODELLED_CTX_METHODS and draw it, "
            "or to NARRATION_CTX_METHODS if it spawns no work."
        )

    def test_the_two_sets_claim_nothing_the_contract_does_not_have(self):
        # The other direction: a name kept after the contract drops it claims coverage the
        # previewer does not have.
        stale = (MODELLED_CTX_METHODS | NARRATION_CTX_METHODS) - self._ctx_surface()
        assert not stale, f"{sorted(stale)} are not WorkflowContext methods; drop them"

    def test_the_two_sets_are_disjoint(self):
        assert not (MODELLED_CTX_METHODS & NARRATION_CTX_METHODS)

    def test_an_unclassified_ctx_call_draws_a_marker_rather_than_nothing(self):
        # The runtime half. The sets above should keep this branch unreachable in
        # practice, but if the pin is ever relaxed the plan must still be honest: an
        # unclassified call says "something happens here" instead of vanishing.
        plan = _plan('await ctx.some_future_verb("do work")\n')
        assert _nodes(plan, "") == [{"kind": "unknown", "label": "call", "certain": False}]

    def test_runner_prompt_label_cut_has_not_moved(self):
        """``RUNNER_PROMPT_LABEL_CHARS`` mirrors a literal in code this module does not own.

        Read the runner's source for the expression itself, so changing the runner's cut
        reddens here instead of silently making every prompt-labelled box disagree with
        the run it previews.
        """
        import inspect

        from kiro_crew.workflows import runner

        expected = f"label or prompt[:{RUNNER_PROMPT_LABEL_CHARS}]"
        assert expected in inspect.getsource(runner), (
            f"the runner does not spell {expected!r}; find its current agent label "
            "expression and move RUNNER_PROMPT_LABEL_CHARS to match"
        )
