"""The PR body has to read as plain language, and the rule has to stay anchored.

`prepare-pr` writes the PR description, and its "What changed" section is what a
reviewer reads first. Left unconstrained it grows into layered clauses and
decorative jargon that hide the actual change. The skill now pins that prose to
the Age 5 row of the `explain-for` skill (Age 10 was tried first and still let
dense prose through).

Two joints can break silently. The register rule can be dropped from
`prepare-pr` (bodies drift back to dense prose with no test failing), or the
`explain-for` row it points at can be renamed away (the reference still reads
fine but resolves to nothing). One assertion each.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "src" / "kiro_crew" / "builtin_skills"
PREPARE_PR = SKILLS / "kirocrew-dev" / "prepare-pr" / "SKILL.md"
EXPLAIN_FOR = SKILLS / "explain-for" / "SKILL.md"


def _flat(path: Path) -> str:
    """Skill body with runs of whitespace collapsed to single spaces.

    These files are hard-wrapped prose, so an asserted phrase can legitimately
    straddle a newline. Normalizing first keeps the assertions about content
    rather than about where the wrap landed.
    """
    return " ".join(path.read_text(encoding="utf-8").split())


def test_prepare_pr_pins_the_pr_body_to_the_age_5_register() -> None:
    flat = _flat(PREPARE_PR)
    # The rule itself, and the skill it borrows the calibration from.
    assert "Age 5 row of the `explain-for`" in flat
    # The old pin must be gone everywhere, not just in the heading.
    assert "Age 10" not in flat
    assert "ten-year-old" not in flat
    # Register, not depth or reader: a plain-language rule that also cut facts
    # or talked down to the reviewer would be worse.
    assert "Age 5 is the *register*, never the depth or the reader" in flat
    assert "the facts stay complete and technically exact" in flat
    # The concrete bound on the section the user could not read.
    assert "Three short paragraphs at most" in flat


def test_explain_for_still_carries_the_age_5_row() -> None:
    """The row `prepare-pr` points at must exist, or the reference is dead."""
    assert "| Age 5 |" in _flat(EXPLAIN_FOR)


def test_prepare_pr_body_is_a_snapshot_of_the_whole_diff_not_a_round_changelog() -> None:
    """Each round must REWRITE the body from the whole diff, never append to it.

    Observed failure: `What changed` grew an "also, after review, ..." paragraph
    per round until no paragraph matched the diff. Three joints keep that from
    coming back: the contract section, the Phase 2 amend step, and the Phase 3
    existing-PR path (which is where `gh pr view --json body` + edit sneaks in).
    """
    flat = _flat(PREPARE_PR)
    assert "### Snapshot, not changelog" in flat
    assert "never one round's fix" in flat
    # Phase 2: an amend rewrites, it does not append.
    assert "rewrite the PR body from the whole diff" in flat
    assert "Rewrite, do not append" in flat
    # Phase 3: an existing PR gets a regenerated body, not a patched one.
    assert "regenerate the whole body from the current diff" in flat
    # The history words that betray a per-round delta are named, so the rule is
    # checkable rather than a vibe.
    assert "`after review`, `round N`" in flat


def test_prepare_pr_body_leads_with_the_punch_line() -> None:
    """First sentence of each section is the fact; the diff is not recited."""
    flat = _flat(PREPARE_PR)
    assert "Punch line first, in every section" in flat
    assert "Do not recite the diff" in flat


def test_a_tightened_contract_forces_a_breaking_line_with_a_fresh_sweep() -> None:
    """The section is only worth having if a tightening diff cannot claim Compatible.

    Three joints, because any one of them alone rots: the template must OFFER the
    section (an author fills in nothing else), the contract must say which of the
    two lines a tightening diff forces, and Phase 1.5 -- where the body is
    actually written -- must point at that rule. The sweep's freshness is the
    fourth: the writers a sweep misses are the ones another open PR is adding
    while this one waits.
    """
    template = (
        Path(__file__).resolve().parents[1] / ".github" / "PULL_REQUEST_TEMPLATE.md"
    ).read_text(encoding="utf-8")
    assert "## Backwards compatibility" in template
    assert "Compatible:" in template, "the compatible branch vanished from the template"
    assert "Breaking:" in template, "the breaking branch vanished from the template"
    assert "re-run the sweep on fresh main before the last push" in " ".join(template.split())

    flat = _flat(PREPARE_PR)
    # Which diffs cannot claim Compatible, named so the rule is checkable.
    assert "new required field, a validator that raises on input the base accepts" in flat
    assert "cannot be `Compatible:`" in flat
    # A sweep is a statement about a commit, so a fresh commit and its sha are owed.
    assert "writer sweep re-run on FRESH `origin/<base>` before the final push" in flat
    # Phase 1.5 is where the body is written, so the rule has to be reachable there.
    assert "A tightening diff makes `## Backwards compatibility` a `Breaking:` line" in flat


def test_the_new_section_is_not_added_to_the_fork_description_gate() -> None:
    """Requiring it would red every open PR's existing body.

    The gate's own list is asserted elsewhere to be a subset of the template; this
    is the other direction -- the template may grow a section the gate does not
    require, and that is deliberate rather than an oversight, so the reason is
    recorded where a later author will look for it.
    """
    root = Path(__file__).resolve().parents[1]
    workflow = " ".join(
        (root / ".github" / "workflows" / "fork-pr-description.yml")
        .read_text(encoding="utf-8")
        .split()
    )
    assert "## Backwards compatibility" not in workflow
    rationale = " ".join(
        (
            root
            / "src"
            / "kiro_crew"
            / "builtin_skills"
            / "kirocrew-dev"
            / "prepare-pr"
            / "references"
            / "rationale.md"
        )
        .read_text(encoding="utf-8")
        .split()
    )
    assert "## Why the Backwards compatibility section is not a required one" in rationale
    assert "would fail every body written before this change" in rationale
