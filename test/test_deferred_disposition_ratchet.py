"""A concern that needs a maintainer ruling must not be dispositioned as deferred work.

``accepted-and-deferred`` and ``needs-a-decision`` look interchangeable in a review
reply and are not. The first says the work is settled and merely out of scope, so a
filed issue names something a contributor can pick up. The second says nobody knows
yet what the right change is, so an issue filed for it carries a question instead of
a task: it cannot be actioned by anyone but the maintainer, it is not read as a
question because it is shaped like a backlog item, and it accumulates one review
round at a time.

Collapsing the two is invisible in every other check. The prose still reads as
diligence, the PR still goes green, and the cost lands weeks later in a tracker full
of items whose bodies ask which of three designs to take.

So this file pins the distinction wherever an agent reads it: any line that
dispositions a concern as ``accepted-and-deferred`` must also offer
``needs-a-decision``, and the prepare-pr skill must keep telling the agent not to
file an issue for one. It is a ratchet, not a description -- it does not care how the
guidance is worded, only that both halves are still there.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "src" / "kiro_crew" / "builtin_skills"
PREPARE_PR = SKILLS / "kirocrew-dev" / "prepare-pr" / "SKILL.md"

DEFERRED = "accepted-and-deferred"
DECISION = "needs-a-decision"


def _skill_files() -> list[Path]:
    return sorted(SKILLS.rglob("SKILL.md"))


def test_prepare_pr_skill_defines_the_decision_disposition() -> None:
    """The skill that owns the disposition vocabulary must carry both names."""
    text = PREPARE_PR.read_text(encoding="utf-8")
    assert DEFERRED in text, f"{PREPARE_PR.name} lost the deferred disposition entirely"
    assert DECISION in text, (
        f"{PREPARE_PR.name} no longer offers `{DECISION}`. Without it every advisory "
        "concern the maintainer has to rule on is dispositioned as deferred work and "
        "mints an unactionable issue."
    )


def test_prepare_pr_skill_forbids_filing_an_issue_for_a_decision() -> None:
    """The load-bearing half is the prohibition, not the label."""
    text = PREPARE_PR.read_text(encoding="utf-8")
    assert "not** file an issue" in text or "not file an issue" in text, (
        f"{PREPARE_PR.name} no longer tells the agent to skip filing an issue for a "
        f"`{DECISION}` concern. The label alone does not stop the issue being filed."
    )


def test_every_disposition_enumeration_offers_the_decision_branch() -> None:
    """A surface that names one disposition set must name the whole current set.

    Line-scoped on purpose: these enumerations are written inline, so a stale copy
    that offers only fixed/rebutted/accepted-and-deferred is exactly the regression
    to catch -- an agent reading that line has no fourth option to choose.
    """
    stale: list[str] = []
    for path in _skill_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if DEFERRED in line and DECISION not in line:
                stale.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not stale, (
        "These lines disposition a concern as deferred work without offering "
        f"`{DECISION}` alongside it: " + ", ".join(stale)
    )


# --- Disposition scope --------------------------------------------------------
#
# The vocabulary above says WHICH disposition a concern gets. These pin WHAT ONE
# disposition may cover, which is the other half and had no test at all.
#
# `codex-review.yml` scopes a ruling's coverage by its recorded rationale and
# never parses `target=`, so one comment carrying one rationale and several
# finding bullets claims every one of them -- across lanes, since the lane in
# `target=` is text nobody reads. The skill has forbidden the blanket line since
# it was written ("never one blanket 'addressed feedback' line for a batch") and
# nothing enforced it, so a single "out of scope for this fix" answered four
# findings from three lanes and the PR went green. Prose that only a model or an
# agent reads is the failure mode; these assertions are the ratchet.

ONE_LANE = "One comment covers exactly one lane"
ONE_RATIONALE = "one rationale covers exactly one finding"


def test_disposition_step_scopes_a_comment_to_one_lane() -> None:
    """`target=` names a lane, so a second lane's concern needs its own comment."""
    text = PREPARE_PR.read_text(encoding="utf-8")
    assert ONE_LANE in text, (
        f"{PREPARE_PR.name} no longer says a disposition comment covers one lane. "
        "Nothing parses `target=`, so without this rule one GPT-targeted comment "
        "silently answers the Design, UX and First Principles concerns too."
    )


def test_disposition_step_scopes_a_rationale_to_one_finding() -> None:
    """Coverage is scoped by rationale, so a shared rationale claims every finding."""
    text = PREPARE_PR.read_text(encoding="utf-8")
    assert ONE_RATIONALE in text, (
        f"{PREPARE_PR.name} no longer says one rationale covers one finding. The "
        "adjudication ledger widens a ruling to everything its rationale fits, so "
        "one reused reason downgrades findings it was never checked against."
    )


def test_the_disposition_step_offers_the_whole_vocabulary() -> None:
    """The step that writes the comment must not offer a stale shorter set.

    The enumeration in Core Concepts and the one in the Phase 3 step are read by
    the same agent at different moments; a three-name copy in the step is what it
    follows while writing, so `accepted-and-deferred` and `needs-a-decision`
    silently collapse into a bare `accepted`.
    """
    text = PREPARE_PR.read_text(encoding="utf-8")
    assert "`fixed`/`rebutted`/`accepted` " not in text, (
        f"{PREPARE_PR.name} carries a stale three-disposition enumeration. Every "
        "place that lists the vocabulary must list all four."
    )


# --- Mechanical enforcement ----------------------------------------------------
#
# The scope rules above were prose-only: `target=` was written into the marker
# and parsed nowhere, so one comment with one rationale silently covered N
# findings across lanes (observed: one "out of scope" rationale answered four
# findings from three lanes and the PR merged green). The enforcement half is
# a disposition-record contract shared by the two prepare-pr scripts: a
# writer-authored record claims exactly one span= finding identity from its
# own target= lane, pr_findings.py computes the violations (non-gating) and
# pr_status.py gates on them. These assertions pin that the rule STAYS
# mechanical -- deleting the parser or the gate must fail here, not silently
# demote the rule back to prose.

SCRIPTS = SKILLS / "kirocrew-dev" / "prepare-pr" / "scripts"


def test_the_disposition_rule_is_mechanically_enforced() -> None:
    contract = (SCRIPTS / "_review_contract.py").read_text(encoding="utf-8")
    status = (SCRIPTS / "pr_status.py").read_text(encoding="utf-8")
    findings = (SCRIPTS / "pr_findings.py").read_text(encoding="utf-8")

    assert (
        "ai-review-disposition" in contract
    ), "_review_contract.py no longer knows the disposition marker; the rule is prose again"
    assert (
        "def disposition_violations" in contract
    ), "_review_contract.py lost the violation computation; `target=` is decorative again"
    assert (
        "target=([A-Za-z0-9_-]+)" in contract
    ), "_review_contract.py no longer parses `target=` out of the disposition marker"
    for name, source in (("pr_status.py", status), ("pr_findings.py", findings)):
        assert (
            "disposition_violations" in source
        ), f"{name} no longer consumes the shared disposition computation"
        assert "disposition_violations = _review_contract.disposition_violations" in source, (
            f"{name} no longer directly exports disposition enforcement from " "_review_contract.py"
        )
    assert "disposition_eval" in status, (
        "pr_status.py's decide() no longer consumes the disposition evaluation, "
        "so a violating record cannot block readiness"
    )


def test_the_skill_tells_the_writer_to_claim_the_span() -> None:
    """The gate demands a span= claim exactly where finding identity exists;
    the skill step that writes the comment must say so, or every agent's
    first disposition after a finding blocks readiness with no instruction
    for avoiding it."""
    text = PREPARE_PR.read_text(encoding="utf-8")
    assert "span=" in text, (
        f"{PREPARE_PR.name} no longer tells the writer to name the finding's "
        "span= identity in the disposition comment"
    )
