"""The dashboard-author crewmate's spec parses, and its narrowing is real.

The template-authoring charter ships as a markdown agent spec beside the skill that is
its procedure. A charter in prose can be ignored; a tool that is not mounted cannot be
called, so the spec is where the charter becomes checkable.

Parsed with the PRODUCT's own parser rather than a YAML read of my own, because the
property worth asserting is that the shipped file is a spec this runtime accepts. A local
parse would pass over a file kiro-cli refuses.

Scope, honestly. This asserts the spec is well formed and mounts what the charter says it
mounts. It does NOT assert the spec is installed: registering it means adding a filename
to ``OWNED_KIRO_AGENT_FILES`` and an installer beside its siblings, which moves nine
recorded digests in the agent-materialization characterization that landed for a refactor
still in flight. That registration is its own change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew.agent_spec_format import is_markdown_spec, parse_markdown_spec

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "dashboard-template"
SPEC_PATH = SKILL_DIR / "agent-spec.md"

#: Everything the charter needs, and nothing that lets it become something else.
EXPECTED_TOOLS = ["execute_bash", "fs_read", "fs_write", "tool_search", "@kirocrew-core"]

#: Auto-approved verbs. Every one only reads or recalls.
EXPECTED_GRANTS = [
    "fs_read",
    "tool_search",
    "@kirocrew-core/skill_search",
    "@kirocrew-core/skill_discover",
    "@kirocrew-core/memory_recall",
    "@kirocrew-core/resource_status",
]


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return parse_markdown_spec(SPEC_PATH.read_text(encoding="utf-8"))


class TestTheSpecIsOneTheRuntimeAccepts:
    def test_the_file_is_recognised_as_a_markdown_spec(self) -> None:
        assert is_markdown_spec(SPEC_PATH)

    def test_it_parses_with_the_products_own_parser(self, spec: dict[str, Any]) -> None:
        """A local YAML read would pass over a file kiro-cli refuses."""
        assert spec["name"] == "kirocrew-dashboard-author"
        assert spec["description"].strip()

    def test_the_body_is_the_prompt(self, spec: dict[str, Any]) -> None:
        """A markdown spec is one file, so the body IS the system prompt. A frontmatter
        ``prompt`` is honoured only for a body-less file, so declaring one here would be
        dead configuration that reads as live."""
        assert "prompt" not in spec or spec.get("prompt")
        body = spec.get("prompt") or ""
        assert "You are `kirocrew-dashboard-author`" in body

    def test_the_description_fits_a_roster_line(self, spec: dict[str, Any]) -> None:
        """The roster shows one line. A description that spends it on mechanism leaves an
        operator picking an agent they cannot tell apart from its siblings."""
        assert len(" ".join(spec["description"].split())) <= 300


class TestTheNarrowingIsReal:
    def test_it_mounts_exactly_the_charter_toolset(self, spec: dict[str, Any]) -> None:
        assert spec["tools"] == EXPECTED_TOOLS

    def test_it_can_read_write_and_run(self, spec: dict[str, Any]) -> None:
        """The job itself: read the skill and the code, write four files, drive the
        scaffold, the type checker, git and gh."""
        for tool in ("fs_read", "fs_write", "execute_bash"):
            assert tool in spec["tools"], tool

    @pytest.mark.parametrize(
        "forbidden, why",
        [
            ("session", "it dispatches nobody; a session verb is how this becomes a conductor"),
            ("report", "it is not a dispatched worker reporting against an item"),
            ("@kirocrew-work", "work_report writes into a PARENT's record"),
            ("@kirocrew-dashboard", "a template is not published at run time"),
            ("code", "governance classes it filesystem.write, and it can shell out"),
            ("web_search", "the inputs are on disk or behind gh"),
            ("web_fetch", "the inputs are on disk or behind gh"),
        ],
    )
    def test_it_mounts_nothing_that_lets_it_become_something_else(
        self, spec: dict[str, Any], forbidden: str, why: str
    ) -> None:
        assert forbidden not in spec["tools"], f"{forbidden} is mounted, but {why}"

    def test_every_auto_approved_entry_only_reads(self, spec: dict[str, Any]) -> None:
        assert spec["allowedTools"] == EXPECTED_GRANTS

    def test_writing_and_shell_are_mounted_but_never_auto_approved(
        self, spec: dict[str, Any]
    ) -> None:
        """``allowedTools`` has no argument matching, so a blanket write grant cannot be
        told apart from "write anywhere" and a blanket shell grant from "run anything".
        The safety story is that a human reads the diff, so a grant reaching outside the
        tree it was pointed at removes the one place that is checked."""
        for tool in ("fs_write", "execute_bash"):
            assert tool in spec["tools"], tool
            assert tool not in spec["allowedTools"], tool

    def test_no_grant_names_a_tool_the_spec_does_not_mount(self, spec: dict[str, Any]) -> None:
        """A grant for an unmounted tool is a permission with no subject: it reads as
        authority the agent has and cannot exercise, which is the kind of line that
        survives a review because it looks deliberate."""
        mounted = set(spec["tools"])
        for ref in spec["allowedTools"]:
            server = ref.split("/", 1)[0]
            assert ref in mounted or server in mounted, ref


class TestTheCharterStatesItsFiveRules:
    """The five rules are the deliverable, so their absence is a defect.

    Pinned by the fact each states rather than by its wording: a rewrite that keeps the
    rule passes, and a rewrite that drops one fails.
    """

    @pytest.mark.parametrize(
        "needle",
        [
            "pull request",  # outputs land as a PR, never at runtime
            "Pick an existing fold first",
            "Unsaid",
            "No percentages",
            "Zero controls",
        ],
    )
    def test_each_rule_is_stated(self, spec: dict[str, Any], needle: str) -> None:
        assert needle in (spec.get("prompt") or "")

    def test_it_sends_the_agent_to_the_skill_and_the_catalogue(self, spec: dict[str, Any]) -> None:
        body = spec.get("prompt") or ""
        assert "dashboard-template" in body
        assert "FOLDS.md" in body

    def test_the_skill_names_the_crewmate_it_is_the_procedure_for(self) -> None:
        body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "agent-spec.md" in body
        assert "kirocrew-dashboard-author" in body
