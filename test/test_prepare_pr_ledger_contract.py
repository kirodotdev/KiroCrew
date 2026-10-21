"""Pin the server-side facts kirocrew-prepare-pr's disposition guidance relies on.

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
    REPO_ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "SKILL.md"
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
    assert "A deferral covers a defect with Anchor: defect only" in text


def test_skill_states_the_dependency_and_the_three_line_shapes():
    skill = SKILL.read_text(encoding="utf-8")
    assert "ADJUDICATION LEDGER" in skill
    assert "lines beginning `> `" in skill
    assert "- **title**" in skill
    assert "do not accept a deferral as a ruling on a security" in skill
    assert "argue *not a defect*, not *disproportional*" in skill


# ---------------------------------------------------------------------------
# The whole-design extractor (_review_contract.extract_design_items) parses the
# lanes' OUTPUT TEMPLATES: the machine-parsed `<Lane>-Verdict:` line and the
# `### <section>` headings. Those templates live in the workflows and prompt
# files, so a rename there makes the extractor silently yield nothing -- which
# looks exactly like a lane that raised no items. Pin the anchors, not the
# whole template: a lane may gain a section without breaking anything.
# ---------------------------------------------------------------------------

CONTRACT = (
    REPO_ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "scripts"
    / "_review_contract.py"
)
DESIGN_LANE_TEMPLATES = {
    "DESIGN": (REPO_ROOT / ".github" / "workflows" / "design-review.yml", "Design-Verdict:"),
    "UX": (REPO_ROOT / ".github" / "workflows" / "ux-review.yml", "UX-Verdict:"),
    "FIRST-PRINCIPLES": (
        REPO_ROOT / ".github" / "review-prompts" / "first-principles.md",
        "First-Principles-Verdict:",
    ),
}


# Every whole-design lane files its items under `### Blockers` (a BLOCK lists a
# Trigger, an Outcome and a Clears-when line for each) and may add a `### Notes`
# block that no reader treats as an item.
DESIGN_LANE_ITEM_HEADINGS = {
    "DESIGN": "### Blockers",
    "UX": "### Blockers",
    "FIRST-PRINCIPLES": "### Blockers",
}


def test_each_whole_design_lane_still_emits_the_anchors_the_extractor_reads():
    for lane, (path, verdict_key) in DESIGN_LANE_TEMPLATES.items():
        text = path.read_text(encoding="utf-8")
        assert verdict_key in text, f"{lane}: the machine-parsed verdict line moved"
        heading = DESIGN_LANE_ITEM_HEADINGS[lane]
        assert heading in text, f"{lane}: the {heading} section heading moved"
        assert "### Blockers" in text, f"{lane}: the Blockers section heading moved"
        assert f"[{lane}-REVIEWED]" in text, f"{lane}: the freshness stamp moved"
    # No lane grows the restating or soft sections back: they are what buried
    # findings under copies of the same item, and the soft tier is gone.
    for lane, (path, _key) in DESIGN_LANE_TEMPLATES.items():
        text = path.read_text(encoding="utf-8")
        assert "### Watch" not in text, lane
        assert "### Subtractions" not in text, lane
        assert "### Suggestions" not in text, lane
        assert "### Not justified as shipped" not in text, lane
        assert "### Notes" in text, lane


def test_the_extractor_allowlist_covers_the_shared_item_sections():
    contract = CONTRACT.read_text(encoding="utf-8")
    assert 'DESIGN_ITEM_SECTIONS = ("Blockers",)' in contract
    for retired in ("Watch", "Subtractions", "Suggestions", "Not justified as shipped"):
        assert f'"{retired}"' not in contract, retired
    assert '"Notes"' not in contract
    # Deliberately EXCLUDED: an inventory line and an evidence note are not
    # items an author rules on one by one.
    assert "What this change ships" in (
        DESIGN_LANE_TEMPLATES["FIRST-PRINCIPLES"][0].read_text(encoding="utf-8")
    )
    assert '"What this change ships"' not in contract
    assert '"Evidence gaps"' not in contract


def test_the_skill_says_a_block_is_answered_and_a_note_needs_no_reply():
    """A BLOCK blocks readiness and is fixed, rebutted or overridden; a PASS and a
    `Notes:` bullet need no reply. SKILL.md has to say so, or a reader will expect a
    soft tier, and there is none."""
    skill = SKILL.read_text(encoding="utf-8")
    assert "It never stays unanswered." in skill
    assert "A PASS, and every `Notes:` bullet, needs no reply." in skill
    assert "CONCERNS" not in skill
