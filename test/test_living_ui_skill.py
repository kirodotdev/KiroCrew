"""Pin the living-ui skill to the facts it rests on.

The skill is reached by its name, and it leans on things other files own: the
freeze-one-frame test it runs as its first gate and the ``widgets`` skill it sends
an author to (both in the widgets skill), the theme variables it may name (in the
widget frame's source), and the licence notice its credits point at. Each check
reads both sides, so a change to either one that makes the skill wrong fails here.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.skills import SkillsLoader

ROOT = Path(__file__).resolve().parent.parent
BUILTIN = ROOT / "src" / "kiro_crew" / "builtin_skills"
SKILL_DIR = BUILTIN / "living-ui"
SKILL_FILE = SKILL_DIR / "SKILL.md"
LICENSE_FILE = SKILL_DIR / "LICENSE-anatomy.md"
WIDGETS_SKILL = BUILTIN / "widgets" / "SKILL.md"
WIDGET_SRCDOC = ROOT / "website" / "src" / "lib" / "widgetSrcdoc.ts"

# The widgets skill wraps the phrase across a line break, so any whitespace counts.
_FREEZE_ONE_FRAME = re.compile(r"freeze[\s-]+one[\s-]+frame", re.I)
_WIDGETS_LINK = "](../widgets/SKILL.md)"
_LICENSE_LINK = "](LICENSE-anatomy.md)"
# `#rgb` through `#rrggbbaa`, and the functional colour notations.
_COLOUR_LITERAL = re.compile(r"#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?)\(", re.I)


def _body() -> str:
    return SkillsLoader.strip_frontmatter(SKILL_FILE.read_text(encoding="utf-8"))


def _theme_var_names() -> set[str]:
    """``THEME_VAR_NAMES`` as the widget frame defines it, read from its source."""
    src = WIDGET_SRCDOC.read_text(encoding="utf-8")
    block = re.search(r"export const THEME_VAR_NAMES = \[(.*?)\] as const", src, re.S)
    assert block, "THEME_VAR_NAMES not found in widgetSrcdoc.ts; update this test"
    return set(re.findall(r"'(--[a-z-]+)'", block.group(1)))


def test_the_skill_loads_by_its_name(tmp_path: Path, opened) -> None:
    root = tmp_path / "skills"
    shutil.copytree(SKILL_DIR, root / "living-ui")
    loader = opened(SkillsLoader(skills_path=root, install_builtins=False, config=KiroCrewConfig()))
    content = loader.load_skill("living-ui")
    assert content is not None, "load_skill('living-ui') found nothing"
    assert SkillsLoader._parse_frontmatter_text(content).get("name") == "living-ui"


def test_the_gate_is_the_widgets_freeze_one_frame_test_and_comes_first() -> None:
    assert _FREEZE_ONE_FRAME.search(
        WIDGETS_SKILL.read_text(encoding="utf-8")
    ), "the widgets skill no longer states the freeze-one-frame test this skill gates on"
    body = _body()
    gate = re.search(r"^## .*freeze one frame", body, re.I | re.M)
    motion = re.search(r"^## M\b", body, re.M)
    assert gate, "SKILL.md has no freeze-one-frame gate heading"
    assert motion, "SKILL.md has no rule M heading"
    assert gate.start() < motion.start(), "the gate must come before the motion rules"


def test_links_the_widgets_skill() -> None:
    assert _WIDGETS_LINK in _body(), "SKILL.md does not link the widgets skill"
    target = (SKILL_DIR / "../widgets/SKILL.md").resolve()
    assert target.is_file(), f"the widgets link points at nothing: {target}"
    assert SkillsLoader._parse_frontmatter(target).get("name") == "widgets"


def test_names_no_colour_and_only_theme_variables() -> None:
    body = _body()
    assert not _COLOUR_LITERAL.findall(body), "SKILL.md names a literal colour"
    allowed = _theme_var_names()
    used = set(re.findall(r"var\((--[A-Za-z0-9-]+)", body))
    assert used, "SKILL.md names no theme variable; update this test"
    assert used <= allowed, f"not theme variables: {sorted(used - allowed)}"


def test_carries_the_anatomy_mit_notice_and_points_to_it() -> None:
    notice = " ".join(LICENSE_FILE.read_text(encoding="utf-8").split())
    assert "MIT License" in notice
    assert "Copyright (c) 2026 Ryan" in notice
    assert (
        "The above copyright notice and this permission notice shall be included in all "
        "copies or substantial portions of the Software." in notice
    )
    assert _LICENSE_LINK in _body(), "SKILL.md does not point at its licence notice"
