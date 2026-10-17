"""What kirocrew-prepare-pr's SKILL.md may carry, so it stops regrowing.

An agent reads SKILL.md whole on every PR turn, and reads a file under
``references/`` only when a step points it there. Most of the text that kept
pushing SKILL.md past its byte ceiling was script detail that the script's own
``--help`` already states, or CI lane rules copied from the workflows. These
tests send both somewhere else: a flag SKILL.md names on a script call must be
one that script accepts, the number of such flags is capped, the CI section is a
short pointer to ``references/ci.md``, and every reference SKILL.md points to
exists. The overall byte ceiling lives in test_review_repair_routing_skill.py.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILL_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
)
SKILL = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
SCRIPTS = {p.stem: p.read_text(encoding="utf-8") for p in (SKILL_DIR / "scripts").glob("*.py")}

# A bundled script's name, then the rest of its code span or line.
_CALL = re.compile(r"(?<![\w-])(\w+)\.py\b([^`\n]*)")
_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")

# Flags SKILL.md shows on a script call. A new flag belongs in the script's
# --help; show it here only when a step of the loop runs it, and raise this
# number in the same change.
FLAG_CEILING = 15
CI_SECTION_CEILING = 1800


def _named_flags() -> set[tuple[str, str]]:
    pairs = set()
    for match in _CALL.finditer(SKILL):
        script = match.group(1)
        if script not in SCRIPTS:
            continue
        for flag in _FLAG.findall(match.group(2)):
            if flag != "--help":
                pairs.add((script, flag))
    return pairs


def test_every_flag_the_skill_names_is_one_its_script_accepts():
    pairs = _named_flags()
    assert pairs, "the call pattern matched nothing; the scan is broken"
    missing = sorted(
        f"{script}.py {flag}"
        for script, flag in pairs
        if f'"{flag}"' not in SCRIPTS[script] and f"'{flag}'" not in SCRIPTS[script]
    )
    assert not missing, f"SKILL.md names flags its scripts do not accept: {missing}"


def test_script_detail_stays_in_the_scripts_help():
    pairs = _named_flags()
    assert len(pairs) <= FLAG_CEILING, (
        f"SKILL.md shows {len(pairs)} script flags, over {FLAG_CEILING}. Put a new "
        "flag in the script's --help; show it in SKILL.md only at the step that runs it."
    )


def test_ci_lane_rules_live_in_the_ci_reference():
    start = SKILL.index("## Kiro Crew CI at a glance\n")
    end = SKILL.index("\n## ", start + 1)
    section = SKILL[start:end]
    assert "`references/ci.md`" in section
    assert "| Lane |" not in section
    assert (
        len(section.encode("utf-8")) <= CI_SECTION_CEILING
    ), "the CI section is over its ceiling: put a lane's rules in references/ci.md"
    assert "| Lane | Blocks? | When it is red |" in (SKILL_DIR / "references" / "ci.md").read_text(
        encoding="utf-8"
    )


def test_every_reference_the_skill_points_to_exists():
    named = set(re.findall(r"references/[\w.-]+\.md", SKILL))
    assert named
    assert not sorted(n for n in named if not (SKILL_DIR / n).is_file())
