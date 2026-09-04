"""Campaign -> workflow ``args`` mapping for Research Lab v2.

The interesting behaviour is decoding: ``sub_questions`` / ``sources`` arrive
JSON-encoded from the DB row on one path and as real lists on another, and a row
written by an older schema (or a truncated value) must degrade to an empty list
rather than blow up the run that is about to be started.
"""

from __future__ import annotations

from kiro_crew.apps.builtins.auto_research.workflow_template import (
    RESEARCH_WORKFLOW_SOURCE,
    build_workflow_args,
)


class TestDecoding:
    def test_json_encoded_columns_are_decoded(self):
        args = build_workflow_args(
            {
                "question": "why does it fail",
                "sub_questions": '[{"text": "a"}, {"text": "b"}]',
                "sources": '["web", "wiki"]',
            }
        )
        assert args["sub_questions"] == [{"text": "a"}, {"text": "b"}]
        assert args["sources"] == ["web", "wiki"]
        assert args["question"] == "why does it fail"

    def test_lists_pass_through_untouched(self):
        rows = [{"text": "a"}]
        args = build_workflow_args({"sub_questions": rows, "sources": ["web"]})
        assert args["sub_questions"] is rows
        assert args["sources"] == ["web"]

    def test_malformed_json_degrades_to_empty(self):
        args = build_workflow_args({"sub_questions": "{not json", "sources": "["})
        assert args["sub_questions"] == []
        assert args["sources"] == []

    def test_json_that_is_not_a_list_degrades_to_empty(self):
        """A JSON object decodes fine but is not a frontier; the workflow wants a list."""
        args = build_workflow_args({"sub_questions": '{"text": "a"}', "sources": "7"})
        assert args["sub_questions"] == []
        assert args["sources"] == []

    def test_missing_and_none_columns_degrade_to_empty(self):
        args = build_workflow_args({"sub_questions": None})
        assert args["sub_questions"] == []
        assert args["sources"] == []


class TestDefaults:
    def test_empty_campaign_gets_workable_defaults(self):
        args = build_workflow_args({})
        assert args["question"] == ""
        assert args["max_rounds"] == 10
        assert args["max_subquestions_per_round"] == 3
        assert args["parallel_workers"] == 3
        assert args["reserve_fraction"] == 0.15
        assert args["success_criteria"] == ""

    def test_zero_and_none_knobs_fall_back_rather_than_disabling_the_run(self):
        """A 0 in the row would otherwise mean 'never explore' / 'no reserve'."""
        args = build_workflow_args(
            {
                "max_cycles": 0,
                "max_subquestions_per_round": None,
                "parallel_workers": 0,
                "reserve_fraction": 0,
            }
        )
        assert args["max_rounds"] == 10
        assert args["max_subquestions_per_round"] == 3
        assert args["parallel_workers"] == 3
        assert args["reserve_fraction"] == 0.15

    def test_configured_knobs_are_carried_and_coerced(self):
        args = build_workflow_args(
            {
                "max_cycles": "6",
                "max_subquestions_per_round": "4",
                "parallel_workers": "2",
                "reserve_fraction": "0.5",
                "success_criteria": "cite three sources",
            }
        )
        assert args["max_rounds"] == 6
        assert args["max_subquestions_per_round"] == 4
        assert args["parallel_workers"] == 2
        assert args["reserve_fraction"] == 0.5
        assert args["success_criteria"] == "cite three sources"


class TestSource:
    def test_source_is_a_sandbox_shaped_workflow_script(self):
        """The engine requires literal ``META`` plus ``async def workflow(ctx)``."""
        assert "META = {" in RESEARCH_WORKFLOW_SOURCE
        assert "async def workflow(ctx):" in RESEARCH_WORKFLOW_SOURCE
        assert "import " not in RESEARCH_WORKFLOW_SOURCE
