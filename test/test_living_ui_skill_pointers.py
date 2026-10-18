"""Pin where other skills send an author to the living-ui skill.

``living-ui`` says how a live figure moves. Agents reach it through the skills
that raise motion in the first place, so each pointer has to sit in the section
that raises it; ``test_living_ui_skill.py`` pins the name the pointers use.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILTIN = ROOT / "src" / "kiro_crew" / "builtin_skills"

# Each skill that points at living-ui, and the heading of the section that must
# carry the pointer: the one an author is reading when motion comes up.
POINTERS = {
    "widgets": (BUILTIN / "widgets" / "SKILL.md", "## Animation"),
    "dashboard": (BUILTIN / "dashboard" / "SKILL.md", "## 6. Theme and reach"),
    "explain-for": (
        BUILTIN / "explain-for" / "SKILL.md",
        "## Draw it when the thing has a shape",
    ),
    "frontend-design-workflow": (
        BUILTIN / "frontend-design-workflow" / "SKILL.md",
        "## Phase 2 — Implementation",
    ),
    "kirocrew-app-dev": (ROOT / "skills" / "kirocrew-app-dev" / "SKILL.md", "### UI Rules"),
}


def _level(line: str) -> int:
    """The markdown heading level of *line*, or 0 when it is not a heading."""
    hashes = len(line) - len(line.lstrip("#"))
    return hashes if 0 < hashes <= 6 and line[hashes : hashes + 1] == " " else 0


def _fence(line: str) -> str:
    """The run of backticks or tildes that starts *line*, or "" when it is no code fence."""
    stripped = line.lstrip()
    for char in "`~":
        run = len(stripped) - len(stripped.lstrip(char))
        if run >= 3:
            return stripped[:run]
    return ""


def _section(path: Path, heading: str) -> str:
    """The text under *heading*, up to the next heading of the same or a higher level.

    A line inside a fenced code block is never a heading, so a ``# comment`` in a
    sample cannot end the section early. A block opens on three or more backticks
    or tildes and closes only on a run of the same character at least as long, so
    a shorter or different fence inside a sample stays sample text.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    assert heading in lines, f"{path.relative_to(ROOT)} has no {heading!r} heading"
    start = lines.index(heading)
    level = _level(heading)
    fence = ""  # the marker that opened the code block the scan is in
    for end in range(start + 1, len(lines)):
        line = lines[end]
        marker = _fence(line)
        if fence:
            if marker.startswith(fence):
                fence = ""
        elif marker:
            fence = marker
        elif 0 < _level(line) <= level:
            break
    else:
        end = len(lines)
    return "\n".join(lines[start + 1 : end]) + "\n"


def test_a_heading_inside_any_code_block_does_not_end_the_section(tmp_path: Path) -> None:
    # Each "# ..." line sits inside a code block: a tilde fence, closed by a longer
    # tilde run, that holds a backtick fence; then a four-backtick fence that holds
    # a three-backtick one. So the section still reaches the pointer, and the real
    # heading after it ends the section.
    doc = tmp_path / "SKILL.md"
    doc.write_text(
        "## Motion\n"
        "~~~sh\n# a comment\n```\n# still inside\n~~~~\n"
        "````md\n```\n# a nested sample\n```\n# still inside\n````\n"
        "Load `living-ui`.\n"
        "## Next\n",
        encoding="utf-8",
    )
    assert _section(doc, "## Motion").endswith("\nLoad `living-ui`.\n")


@pytest.mark.parametrize("skill", sorted(POINTERS))
def test_the_section_that_raises_motion_names_living_ui(skill: str) -> None:
    path, heading = POINTERS[skill]
    section = _section(path, heading)
    assert "`living-ui`" in section, f"{skill}: {heading!r} does not point at living-ui"
