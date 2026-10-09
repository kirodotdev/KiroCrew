"""The ``## Motion`` section of the dashboard-template skill, held to the code it describes.

The section tells an author which effects a page may run, what the frame allows, and
that a reduced-motion block is mandatory. Each claim names a real part -- the goal-board
reference page, the host's open bridge -- so each is checked against that part here:
deleting the section, or its reduced-motion rule, or renaming the bridge on one side
only, fails a case below.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "src/kiro_crew/builtin_skills/kirocrew-dev/dashboard-template/SKILL.md"
REFERENCE = ROOT / "src/kiro_crew/dashboard_templates/builtin/goal-board/template.html"
BRIDGE = ROOT / "website/src/hooks/useFrameOpenLink.ts"
DASHBOARD_HOST = ROOT / "website/src/pages/members/CrewDynamicDashboard.tsx"

_REDUCED = re.compile(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\}\s*\}", re.DOTALL)


def _section() -> str:
    text = SKILL.read_text(encoding="utf-8")
    assert "\n## Motion\n" in text, "SKILL.md has no '## Motion' section"
    start = text.index("\n## Motion\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def _css_block() -> str:
    blocks = re.findall(r"```css\n(.*?)```", _section(), re.DOTALL)
    assert len(blocks) == 1, "the Motion section must show exactly one css block"
    return blocks[0]


def _rule(css: str) -> str:
    m = _REDUCED.search(css)
    assert m, "no prefers-reduced-motion block"
    return " ".join(m.group(1).split())


def test_the_reduced_motion_block_is_mandatory_and_stops_everything() -> None:
    section = _section()
    assert "MUST" in section and "prefers-reduced-motion" in section
    rule = _rule(_css_block())
    for selector in (".board *", ".board *::before", ".board *::after"):
        assert selector in rule, selector
    assert "animation: none !important" in rule
    assert "transition: none !important" in rule


def test_the_guides_block_is_the_reference_pages_own_rule() -> None:
    """The guide shows the rule goal-board ships, not a third copy that can drift."""
    style = REFERENCE.read_text(encoding="utf-8").split("<style>")[1].split("</style>")[0]
    assert _rule(_css_block()) == _rule(style)


def test_every_class_and_keyframes_the_guide_cites_exists_in_the_reference() -> None:
    section = _section()
    style = REFERENCE.read_text(encoding="utf-8").split("<style>")[1].split("</style>")[0]
    keyframes = set(re.findall(r"`(gb-[\w-]+)`", section))
    assert keyframes == {"gb-pop", "gb-grow", "gb-shimmer", "gb-blink", "gb-glow"}
    for name in keyframes:
        assert f"@keyframes {name} " in style, name
    for selector in (".item.pop", ".states.grow i", ".item.live::after", ".dot", ".band"):
        assert f"`{selector}`" in section, selector
        assert selector + " {" in style, selector


def test_only_live_or_needs_you_elements_loop_in_the_reference() -> None:
    """The guide allows a loop only on a live or needs-you element; the reference agrees."""
    style = REFERENCE.read_text(encoding="utf-8").split("<style>")[1].split("</style>")[0]
    looping = set(re.findall(r"animation:\s*(gb-[\w-]+)[^;]*\binfinite\b", style))
    assert looping == {"gb-shimmer", "gb-blink"}
    assert re.search(
        r"\.band \{[^}]*animation: gb-glow [^;]* 3;", style
    ), "the glow is not 3 pulses"
    section = _section()
    assert "Only a live or needs-you element may loop" in section
    assert "`gb-glow` (3 pulses)" in section
    assert "Every loop stops under `prefers-reduced-motion`" in section


def test_the_bridge_the_guide_names_is_the_one_the_host_honours() -> None:
    section = _section()
    bridge = BRIDGE.read_text(encoding="utf-8")
    m = re.search(r"OPEN_MESSAGE_TYPE = '([\w:-]+)'", bridge)
    assert m, "useFrameOpenLink.ts no longer exports OPEN_MESSAGE_TYPE"
    call = f"parent.postMessage({{ type: '{m.group(1)}', url: url }}, '*')"
    assert call in section
    assert call in REFERENCE.read_text(encoding="utf-8")
    assert "useFrameOpenLink" in section and "PR_URL_RE" in section
    assert "hasGesture" in bridge and "right after a click" in section
    assert "export const PR_URL_RE" in bridge


def test_the_sandbox_the_guide_states_is_the_hosts() -> None:
    host = DASHBOARD_HOST.read_text(encoding="utf-8")
    assert "export const CREW_DASHBOARD_SANDBOX = 'allow-scripts'\n" in host
    section = _section()
    assert '`sandbox="allow-scripts"` only' in section
    assert "no `allow-popups`" in section
