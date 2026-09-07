"""Pin the server-side facts prepare-pr's disposition guidance relies on.

SKILL.md tells the agent to write a disposition's rationale in ``> `` lines,
at class level, and to argue not-a-defect (never merely disproportional) for a
security-class rebuttal. Each of those instructions is only correct because of
a specific property of the GPT lane's convergence machinery:

- the ADJUDICATION LEDGER keeps only the marker line, ``> `` lines and
  ``- **`` title bullets from each disposition comment (so prose outside them
  never reaches the reviewer), capped per record;
- convergence rule 1 lets a recorded rationale cover every instance of the
  tradeoff it names (so a class-level rebuttal converges the class);
- convergence rule 5 refuses a deferral as a ruling on a security / data-loss /
  corruption finding (so the skill must not teach deferral for that class).

The skill relaxed three local hard limits on the strength of those properties.
If any of them drifts, the skill's advice becomes silently wrong, so pin them
here the way ``test_prepare_pr_profiles.py`` pins the reviewer budgets.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL = (
    REPO_ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "prepare-pr" / "SKILL.md"
)
CODEX = REPO_ROOT / ".github" / "workflows" / "codex-review.yml"
CONVERGENCE = REPO_ROOT / ".github" / "review-prompts" / "gpt-round-convergence.md"


def test_ledger_keeps_only_marker_quote_and_title_lines():
    """The jq filter that builds the ledger selects exactly the three line shapes
    SKILL.md names, and nothing else."""
    text = CODEX.read_text(encoding="utf-8")
    m = re.search(
        r'select\(startswith\("<!--"\) or startswith\("> "\) or \(test\("([^"]+)"\)\)\)',
        text,
    )
    assert m, "the ledger's per-line filter has changed shape; re-read SKILL.md's ledger section"
    assert m.group(1).startswith("^\\\\s*[-*]\\\\s*\\\\*\\\\*"), m.group(1)

    cap = re.search(r"\| \.\[0:(\d+)\] \| join", text)
    assert cap, "the ledger's per-record line cap is gone"
    skill = SKILL.read_text(encoding="utf-8")
    words = {"12": "twelve"}
    assert (
        words.get(cap.group(1), cap.group(1)) in skill
    ), f"SKILL.md no longer states the ledger cap ({cap.group(1)} lines per record)"


def test_convergence_rules_the_skill_leans_on_are_present():
    text = CONVERGENCE.read_text(encoding="utf-8")
    assert "wherever it moves" in text, "rule 1's class-level coverage wording changed"
    assert (
        "A DEFERRAL is not an adjudication for security" in text
    ), "rule 5 (no deferral for security/data-loss/corruption) changed or moved"
    assert "Deferral covers advisory-class findings" in text


def test_skill_states_the_dependency_and_the_three_line_shapes():
    skill = SKILL.read_text(encoding="utf-8")
    assert "ADJUDICATION LEDGER" in skill
    assert "lines beginning `> `" in skill
    assert "- **title**" in skill
    assert "do not accept a deferral as a ruling on a security" in skill
    assert "argue *not a defect*, not *disproportional*" in skill
