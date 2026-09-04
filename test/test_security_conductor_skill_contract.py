"""The security-conductor skill's contract, pinned against its own text.

The skill body IS the operating procedure -- it is what a conductor session is
seeded with, and the sibling scripts are written against clauses it states. A
well-meaning rewrite of a markdown file can drop the clause a script's exit codes
mean something under, or the clause that bounds a worker's aggression, and nothing
anywhere goes red. This file closes that.

The assertions are about SUBSTANCE, not sentences, and the dividing line is stated
so a later edit has a rule to follow: assert a phrase when it states a RULE -- an
obligation, a prohibition, a defined behaviour, a name a script shares -- and do
NOT assert one that only explains WHY the rule exists. Rationale is what a rewrite
is entitled to reword; pinning it produces a typo detector that every legitimate
edit has to fight, which is the failure mode that makes text tests get deleted.

The rules-of-engagement export gets its own class. It is the M0 review surface, so
its SHAPE (schema tag, the six rule fields, a reason on every rule) is a contract a
human reviewer and ``scope_check.py`` both read -- and an export missing a field
reads as "nothing to say about that" rather than as an omission.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "src" / "kiro_crew" / "builtin_skills" / "security-conductor"
SKILL_MD = SKILL_DIR / "SKILL.md"
ROE_JSON = SKILL_DIR / "rules-of-engagement.json"

#: The four scripts the procedure delegates its deterministic half to. Named here
#: rather than globbed from the directory on purpose: the point is that the PROSE
#: cites each one, and a glob would pass on a skill body that mentions none of
#: them -- which is exactly the state while sibling changes are still building
#: them.
BUNDLED_SCRIPTS = (
    "scope_check.py",
    "finding_entry.py",
    "verify_finding.py",
    "ledger.py",
)

#: Every field ``scope_check.py`` and the human reviewer read. Pinned as a set so
#: an export that silently drops one fails, instead of reading as a target with
#: nothing to say about (say) what is forbidden.
ROE_FIELDS = (
    "scope",
    "allowed_techniques",
    "forbidden",
    "severity_scale",
    "human_approval",
    "report_schema",
)


def _flat(text: str) -> str:
    """Collapse whitespace and markdown decoration, lowercased.

    Markdown re-flows every time a sentence is edited, and emphasis moves with
    it, so a check that breaks on a line break or on a pair of backticks is
    testing the formatting rather than the contract. Three normalizations, each
    for a shape this skill's body actually uses: leading blockquote markers (the
    seed templates are quote blocks, so a wrapped sentence inside one carries a
    ``>`` mid-phrase), backticks (a script name or a verdict word is inline
    code), and asterisks (a rule's operative clause is often bolded).
    """
    joined = " ".join(line.lstrip().lstrip(">") for line in text.splitlines())
    return " ".join(joined.replace("`", "").replace("*", "").split()).lower()


def _section(text: str, heading: str) -> str:
    """One markdown section's body, subsections included.

    Scoping an assertion to its own section is what keeps a check honest: the
    verifier's independence clause must not be satisfiable by a sentence that
    happens to live in the auditor's brief.
    """
    lines = text.splitlines()
    depth = len(heading) - len(heading.lstrip("#"))
    start = next((i for i, line in enumerate(lines) if line.strip() == heading.strip()), None)
    assert start is not None, f"section missing from the skill: {heading}"
    body: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.lstrip("#")
        level = len(line) - len(stripped)
        if line.startswith("#") and level <= depth:
            break
        body.append(line)
    return "\n".join(body)


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def roe() -> dict:
    return json.loads(ROE_JSON.read_text(encoding="utf-8"))


class TestSkillIsInstallable:
    """A builtin skill is reachable only through its front matter."""

    def test_skill_file_ships(self) -> None:
        assert SKILL_MD.is_file(), f"missing skill body: {SKILL_MD}"

    def test_front_matter_declares_the_loader_fields(self, skill_text: str) -> None:
        assert skill_text.startswith("---\n"), "front matter must open the file"
        _, raw, _ = skill_text.split("---", 2)
        meta = yaml.safe_load(raw)
        assert meta["name"] == "security-conductor"
        # The description is the only thing a triggering reader sees before
        # loading the body, so it has to name the agent it is the procedure for.
        assert "kirocrew-security-conductor" in meta["description"]

    def test_no_stub_scripts_are_shipped(self) -> None:
        """The scripts land in sibling changes, one at a time. Whatever has
        landed must be a real script, never a placeholder: a present-but-empty
        script is worse than an absent one, because the absent-script rule below
        stops reading as the live path. An absent script is fine here (the
        sibling has not landed yet); an empty one is the defect."""
        scripts_dir = SKILL_DIR / "scripts"
        if not scripts_dir.exists():
            return
        for path in scripts_dir.iterdir():
            if path.suffix != ".py":
                continue
            assert path.name in BUNDLED_SCRIPTS, f"unlisted script shipped: {path.name}"
            assert path.stat().st_size > 0, f"stub script shipped: {path.name}"


class TestTheProcedureDelegatesToItsScripts:
    def test_every_script_is_cited_by_name(self, skill_text: str) -> None:
        flat = _flat(skill_text)
        for script in BUNDLED_SCRIPTS:
            assert script in flat, script

    def test_an_absent_script_is_unknown_and_not_permission(self, skill_text: str) -> None:
        """The scripts land in sibling changes, so "not installed yet" is the
        live path on day one. Undefined, it is a file-not-found with no way
        forward; answered, it is another UNKNOWN, which is never permission."""
        flat = _flat(skill_text)
        assert "presence is not assumed" in flat
        assert "rather than permission" in flat

    def test_scope_is_decided_by_the_script_not_by_judgment(self, skill_text: str) -> None:
        flat = _flat(skill_text)
        assert "never by your judgment" in flat
        assert "unknown is never permission" in flat


class TestPolicyRefusalIsTheBoundary:
    """The clause that matters most in practice: an auditor hunting fence
    weaknesses will meet the fence, and the correct move is to stop."""

    def test_the_rule_is_stated_as_a_prohibition(self, skill_text: str) -> None:
        flat = _flat(skill_text)
        assert "a policy refusal is the boundary" in flat
        assert "never to rephrase" in flat

    def test_a_refusal_is_recorded_as_an_event(self, skill_text: str) -> None:
        assert "record it as an event" in _flat(skill_text)

    def test_the_auditor_brief_carries_the_rule_itself(self, skill_text: str) -> None:
        """The conductor knowing it is not enough -- the worker is the one holding
        the blocked tool call."""
        brief = _flat(_section(skill_text, "## Auditor seed template"))
        assert "a policy refusal is the boundary" in brief
        assert "do not rephrase" in brief

    def test_a_reported_circumvention_stops_the_fleet(self, skill_text: str) -> None:
        stops = _flat(_section(skill_text, "## Stop conditions"))
        assert "circumvented" in stops


class TestWorkItemQualification:
    """Three properties, all required. A candidate with no expressible proof
    shape produces prose, and prose is where hallucinated findings come from."""

    HEADING = "## What qualifies as a work item"

    def test_one_surface_per_item(self, skill_text: str) -> None:
        assert "one work item is one attack surface" in _flat(_section(skill_text, self.HEADING))

    def test_independently_auditable(self, skill_text: str) -> None:
        assert "independently auditable" in _flat(_section(skill_text, self.HEADING))

    def test_a_named_poc_shape_is_required(self, skill_text: str) -> None:
        body = _flat(_section(skill_text, self.HEADING))
        assert "proof-of-concept shape" in body
        assert "is not a work item" in body


class TestAuditorBrief:
    HEADING = "## Auditor seed template"

    def test_governance_is_the_first_step(self, skill_text: str) -> None:
        """ARCC search happens before any probe, so it is the first clause of the
        brief and not an item in a list the reader may reorder."""
        body = _section(skill_text, self.HEADING)
        first = _flat(body).split("read the rules of engagement")[0]
        assert "security-assistance" in first
        assert "if it is installed" in first

    def test_an_unavailable_arcc_skill_has_a_defined_behavior(self, skill_text: str) -> None:
        """The skill is an installed one, not a builtin, so absent is the common
        case. Unanswered, an auditor either stalls or substitutes its own
        governance judgment -- and the finding then does not record which."""
        body = _flat(_section(skill_text, self.HEADING))
        assert "arcc: unavailable" in body
        assert "do not treat its absence as permission" in body

    def test_the_poc_ceiling_is_stated(self, skill_text: str) -> None:
        body = _flat(_section(skill_text, self.HEADING))
        assert "unit-level test" in body
        assert "no network egress" in body

    def test_a_clean_surface_is_a_valid_outcome(self, skill_text: str) -> None:
        """Without this the brief only rewards findings, which is the incentive
        that manufactures them."""
        body = _flat(_section(skill_text, self.HEADING))
        assert "prefer reporting a surface as clean" in body


class TestVerifierBrief:
    """The verifier exists to REJECT. A brief that reads as "check this finding"
    gets a second opinion; one that reads as "reject it if it does not hold" gets
    a rejection pass."""

    HEADING = "## Verifier seed template"

    def test_the_purpose_is_rejection(self, skill_text: str) -> None:
        assert "reject it if it does not hold" in _flat(_section(skill_text, self.HEADING))

    def test_the_poc_is_re_run_independently(self, skill_text: str) -> None:
        body = _flat(_section(skill_text, self.HEADING))
        assert "re-run the proof of concept independently" in body
        # Independence is procedural: it comes from withholding the auditor's
        # reasoning, so that instruction is the mechanism, not colour.
        assert "do not read the auditor's transcript" in body

    def test_the_three_verdicts_are_the_only_verdicts(self, skill_text: str) -> None:
        body = _flat(_section(skill_text, self.HEADING))
        for verdict in ("confirmed", "rejected", "needs-human"):
            assert verdict in body, verdict

    def test_an_out_of_scope_proof_is_needs_human_not_a_widening(self, skill_text: str) -> None:
        body = _flat(_section(skill_text, self.HEADING))
        assert "not a reason to widen the scope" in body


class TestRetrospectiveBrief:
    HEADING = "## Retrospective seed template"

    def test_it_compares_verdicts_across_roles(self, skill_text: str) -> None:
        body = _flat(_section(skill_text, self.HEADING))
        assert "auditor verdicts against the verifier and human verdicts" in body

    def test_lessons_are_proposed_through_the_ledger_cli(self, skill_text: str) -> None:
        assert "ledger.py propose-lesson" in _flat(_section(skill_text, self.HEADING))

    def test_an_unapproved_lesson_never_reaches_a_seed(self, skill_text: str) -> None:
        body = _flat(_section(skill_text, self.HEADING))
        assert "inactive until a human approves" in body
        assert "never inject an unapproved lesson" in body


class TestSeverityAdjudication:
    HEADING = "## Severity adjudication"

    def test_severity_comes_only_from_the_scale(self, skill_text: str) -> None:
        body = _flat(_section(skill_text, self.HEADING))
        assert "severity_scale" in body
        assert "from nowhere else" in body

    def test_only_confirmed_findings_are_graded(self, skill_text: str) -> None:
        body = _flat(_section(skill_text, self.HEADING))
        assert "only findings a verifier confirmed" in body
        assert "never silently promoted" in body


class TestHumanGates:
    HEADING = "## The two human gates"

    def test_both_gates_are_named(self, skill_text: str) -> None:
        body = _flat(_section(skill_text, self.HEADING))
        assert "active testing beyond static review" in body
        assert "fixer dispatch" in body

    def test_silence_is_not_approval(self, skill_text: str) -> None:
        body = _flat(_section(skill_text, self.HEADING))
        assert "never treat silence as approval" in body

    def test_a_pending_gate_is_a_standing_obligation(self, skill_text: str) -> None:
        """A gate nobody re-reads is a gate that quietly stops existing, because
        nothing fires to remind the conductor of it."""
        patrol = _flat(_section(skill_text, "## The patrol cycle"))
        assert "every cycle regardless of what fired" in patrol
        assert "pending human gate" in patrol


class TestStopConditions:
    HEADING = "## Stop conditions"

    def test_nothing_runs_before_the_human_reviews_the_rules(self, skill_text: str) -> None:
        """The M0 exit criterion, stated where it stops work rather than only in
        the design document."""
        body = _flat(_section(skill_text, self.HEADING))
        assert "have not been reviewed by a human" in body
        assert "nothing is dispatched before that" in body

    def test_an_absent_scope_script_stops_the_run(self, skill_text: str) -> None:
        body = _flat(_section(skill_text, self.HEADING))
        assert "scope_check.py is absent" in body

    def test_the_normal_exit_stops_the_loop_deliberately(self, skill_text: str) -> None:
        body = _flat(_section(skill_text, self.HEADING))
        assert "autonudge_stop" in body


class TestRulesOfEngagementExport:
    """The M0 review surface. Shape is a contract; the values are the human's."""

    def test_it_is_a_versioned_export(self, roe: dict) -> None:
        assert roe["schema"] == "roe/v1"
        assert "exported_at" in roe

    def test_it_declares_itself_an_export_not_the_source_of_truth(self) -> None:
        """A reader who edits this file to widen a scope has defeated the
        attribution the rows exist to provide, so the file has to say so."""
        flat = _flat(ROE_JSON.read_text(encoding="utf-8"))
        assert "not the source of truth" in flat

    def test_every_field_is_present(self, roe: dict) -> None:
        assert set(ROE_FIELDS) <= set(roe["rules"])

    def test_every_rule_carries_a_value_and_a_one_line_reason(self, roe: dict) -> None:
        for field, rules in roe["rules"].items():
            assert rules, f"{field} is empty; an empty field reads as no constraint"
            for rule in rules:
                assert rule.get("value"), f"{field}: rule with no value"
                reason = rule.get("reason") or ""
                assert reason, f"{field}: {rule.get('value')!r} has no reason"
                assert "\n" not in reason, f"{field}: {rule.get('value')!r} reason is not one line"

    def test_it_is_unapproved_until_a_human_reviews_it(self, roe: dict) -> None:
        """M0's exit criterion, as data. A default of "approved" would let the
        first round run on rules nobody read."""
        assert roe["approved_by"] is None

    def test_the_severity_scale_carries_all_four_levels(self, roe: dict) -> None:
        values = " ".join(r["value"] for r in roe["rules"]["severity_scale"])
        for level in ("Critical:", "High:", "Medium:", "Low:"):
            assert level in values, level

    def test_the_forbidden_list_covers_the_absolutes(self, roe: dict) -> None:
        """Seven classes the design fixes. Pinned by substance rather than by
        count so adding an eighth is not a test change, but dropping one is."""
        values = " ".join(r["value"] for r in roe["rules"]["forbidden"])
        for token in (
            "production",
            "credential",
            "dos",
            "safety-protections",
            "policy-block",
            "network-egress",
            "scratch-worktree",
        ):
            assert token in values, token

    def test_both_human_gates_are_rows(self, roe: dict) -> None:
        values = " ".join(r["value"] for r in roe["rules"]["human_approval"])
        assert "active-testing" in values
        assert "fixer-dispatch" in values

    def test_the_report_schema_matches_the_finding_record(self, roe: dict) -> None:
        fields = {r["value"] for r in roe["rules"]["report_schema"]}
        assert {
            "id",
            "surface",
            "severity",
            "title",
            "paths",
            "poc",
            "verifier_verdict",
            "status",
        } <= fields

    def test_allowed_techniques_stop_at_the_local_poc_ceiling(self, roe: dict) -> None:
        """The ceiling is what makes the active-testing gate meaningful: a
        technique row admitting a live target would make the gate unreachable."""
        values = {r["value"] for r in roe["rules"]["allowed_techniques"]}
        assert values == {"static-code-review", "dependency-audit", "local-unit-poc"}
