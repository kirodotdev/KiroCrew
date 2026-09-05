"""The PR body has to read as plain language, and the rule has to stay anchored.

`prepare-pr` writes the PR description, and its "What changed" section is what a
reviewer reads first. Left unconstrained it grows into layered clauses and
decorative jargon that hide the actual change. The skill now pins that prose to
the Age 10 row of the `explain-for` skill.

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


def test_prepare_pr_pins_the_pr_body_to_the_age_10_register() -> None:
    flat = _flat(PREPARE_PR)
    # The rule itself, and the skill it borrows the calibration from.
    assert "Age 10 row of the `explain-for`" in flat
    # Register, not depth: a plain-language rule that also cut facts would be worse.
    assert "This sets the *register*, never the depth" in flat
    # The concrete bound on the section the user could not read.
    assert "Three short paragraphs at most" in flat


def test_explain_for_still_carries_the_age_10_row() -> None:
    """The row `prepare-pr` points at must exist, or the reference is dead."""
    assert "| Age 10 |" in _flat(EXPLAIN_FOR)
