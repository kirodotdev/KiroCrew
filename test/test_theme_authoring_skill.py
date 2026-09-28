"""The theme-pack-authoring skill's restated contract facts must track the code.

The skill is a distributed digest of the theming contract: it is synced into
every user's skills directory, so any enumeration it restates (variable count,
runtime surface allowlist, font caps) becomes what agents worldwide believe.
Restated counts go stale silently — the same failure mode AGENTS.md pins its
denied-rule count for — so every number and list the skill hardcodes is
asserted here against the owning constants in ``theme_validate.py`` and
``themeCss.ts``. When an allowlist changes, this test fails and points at the
skill line to update, instead of every theme author hitting the silent
drop this skill exists to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.dashboard.theme_validate import (
    _THEME_CSS_VARS,
    _THEME_MAX_FONTS,
)

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "src" / "kiro_crew" / "builtin_skills" / "theme-pack-authoring" / "SKILL.md"
THEME_CSS_TS = ROOT / "website" / "src" / "hooks" / "themeCss.ts"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _frontend_allowed_classes() -> set[str]:
    """The runtime scoper's surface allowlist, parsed from its owning constant."""
    src = THEME_CSS_TS.read_text(encoding="utf-8")
    m = re.search(r"_ALLOWED_CLASSES = new Set\(\[(.*?)\]\)", src, re.S)
    assert m, "themeCss.ts no longer defines _ALLOWED_CLASSES — update this test AND the skill"
    return set(re.findall(r"'([^']+)'", m.group(1)))


def test_variable_count_matches_validator() -> None:
    """The '43-variable palette' claim tracks _THEME_CSS_VARS."""
    n = len(_THEME_CSS_VARS)
    text = _skill_text()
    assert f"{n} allowlisted variables" in text and f"{n}-variable palette" in text, (
        f"theme_validate._THEME_CSS_VARS now has {n} entries; update the counts in "
        f"{SKILL.relative_to(ROOT)}"
    )


def test_font_cap_matches_validator() -> None:
    """'max 6 faces' tracks _THEME_MAX_FONTS."""
    assert f"max {_THEME_MAX_FONTS} faces" in _skill_text().replace("**", ""), (
        f"theme_validate._THEME_MAX_FONTS is now {_THEME_MAX_FONTS}; update the cap in "
        f"{SKILL.relative_to(ROOT)}"
    )


def test_surface_allowlist_matches_runtime_scoper() -> None:
    """Every class surface the scoper allows is named in the skill, and the skill
    names no class surface the scoper does not allow.

    ``body`` / ``button.primary`` are element-level allowances handled separately
    in themeCss.ts; the class list is the part that drifts.
    """
    text = _skill_text()
    allowed = _frontend_allowed_classes()
    for cls in allowed:
        assert f"`.{cls}`" in text, (
            f"scoper allows .{cls} but the skill does not list it; update "
            f"{SKILL.relative_to(ROOT)}"
        )
    # The skill's overrides.css section must not name class surfaces beyond the
    # scoper's set (plus the element-level body/button.primary allowances and
    # the classes it cites as explicitly forbidden, e.g. `.token`).
    src = THEME_CSS_TS.read_text(encoding="utf-8")
    fm = re.search(r"_FORBIDDEN_CLASSES = new Set\(\[(.*?)\]\)", src, re.S)
    forbidden = set(re.findall(r"'([^']+)'", fm.group(1))) if fm else set()
    section = text.split("## overrides.css", 1)[1].split("## ", 1)[0]
    named = set(re.findall(r"`\.([a-z-]+)`", section))
    assert named <= allowed | forbidden, (
        f"skill names surfaces the scoper does not allow: {sorted(named - allowed - forbidden)}"
    )
