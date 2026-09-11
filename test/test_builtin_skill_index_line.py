"""Guard: a packaged skill's "when to load" sentence must survive the index cut.

The session-start ``## Available Skills`` block shows each on-demand skill as one
line: its name plus ``SkillsLoader._short_desc(description)``, which truncates
the description at ``_SHORT_DESC_CHARS``. Per-message trigger matching is off
by default (``skills.max_triggered = 0``), so that one line is the ONLY thing
telling an agent whether to ``cat`` the skill. A description that spends its
first 300 characters on what the skill does and puts "use when ..." / "load
this whenever ..." after the cut ships a routing rule no agent ever reads.

This happened to ``prepare-pr``: its "FULL LOOP IS THE DEFAULT: load this
whenever a task will open or update a PR" sentence started at character 339,
so a worker that opened a PR saw a feature summary and never loaded the loop
that answers reviewer CONCERNS. Five other packaged skills had the same shape.

These tests fail if a packaged skill's first routing directive lands past the
cut again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew.skills import _SHORT_DESC_CHARS, SkillsLoader

PKG = Path(kiro_crew.__file__).resolve().parent
BUILTIN = PKG / "builtin_skills"

# Phrases a description uses to say WHEN the skill applies. Case-insensitive.
# Deliberately narrow: a description with none of these has no routing
# directive to protect and is not judged by this guard.
_DIRECTIVE = re.compile(
    r"\b("
    r"use (this )?(skill )?(when|for)"
    r"|load (this|it|the skill)"
    r"|invoke (this|it|when)"
    r"|apply when"
    r"|is the default"
    r")\b",
    re.I,
)


def _packaged_descriptions() -> dict[str, str]:
    out: dict[str, str] = {}
    for skill_md in sorted(BUILTIN.rglob("SKILL.md")):
        name = str(skill_md.parent.relative_to(BUILTIN)).replace("\\", "/")
        fm = SkillsLoader._parse_frontmatter_text(skill_md.read_text(encoding="utf-8"))
        out[name] = " ".join((fm.get("description") or "").split())
    return out


class TestRoutingDirectiveSurvivesIndexCut:
    def test_first_directive_lands_inside_the_index_line(self) -> None:
        offenders: list[str] = []
        for name, desc in _packaged_descriptions().items():
            m = _DIRECTIVE.search(desc)
            if m is None:
                continue
            if m.start() >= _SHORT_DESC_CHARS:
                offenders.append(f"{name}: directive {m.group(0)!r} starts at {m.start()}")
        assert not offenders, (
            f"routing directive falls past the {_SHORT_DESC_CHARS}-char index cut, so the "
            "Available Skills line never shows it — move the 'use when / load this' "
            f"sentence to the front of the description: {offenders}"
        )

    @pytest.mark.parametrize(
        "name",
        [
            "kirocrew-dev/prepare-pr",
            "kirocrew-dev/babysit",
            "goal-ledger-conductor",
            "llm-council",
            "pipeline-conductor",
            "security-conductor",
        ],
    )
    def test_rendered_index_line_carries_the_directive(self, name: str) -> None:
        """End to end through the real renderer, for the six that were broken.

        Pinned by name so a rewording that drops the directive entirely (which
        the scan above would silently accept) still fails here.
        """
        desc = _packaged_descriptions()[name]
        line = SkillsLoader._short_desc(desc)
        assert _DIRECTIVE.search(
            line
        ), f"{name}: rendered index line has no routing directive: {line!r}"
