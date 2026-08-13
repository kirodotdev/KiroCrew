"""The image-authoring skill must stay discoverable and keep its Excalidraw route.

Locks two joints:

(1) Every packaged builtin skill carries the frontmatter fields ``skills/README.md``
    documents as required — ``name`` and ``description``. Without them
    ``SkillsLoader.list_skills`` falls back to ``meta.get("description", name)``, so
    the skill appears in the lazy-load catalog as nothing but its own directory name
    and ``skill_search`` has no text to match on. image-authoring shipped that way
    and was effectively unreachable: the only route to it was typing its exact name.

    ``triggers`` is deliberately NOT asserted here. ``skills/README.md`` documents it
    as optional, and whether a skill should auto-inject is a per-skill judgement
    about trigger overlap (see the PR #353 arbiter note in ``skills.py``) rather than
    something a blanket test should force.

(2) image-authoring names the ``excalidraw`` fence. The dashboard renders it
    (``MarkdownRenderer`` -> ``ExcalidrawBlock``), but that fence is specific to this
    project, so an agent only emits one if this skill says the option exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.skills import SkillsLoader

ROOT = Path(__file__).resolve().parent.parent
BUILTIN_SKILLS_DIR = ROOT / "src" / "kiro_crew" / "builtin_skills"
IMAGE_AUTHORING = BUILTIN_SKILLS_DIR / "image-authoring" / "SKILL.md"


def _builtin_skill_files() -> list[Path]:
    """Every packaged builtin SKILL.md, including nested ones (kirocrew-dev/…)."""
    return sorted(BUILTIN_SKILLS_DIR.rglob("SKILL.md"))


def _skill_id(path: Path) -> str:
    """Skill key as the loader sees it — the dir path relative to the builtin root."""
    return str(path.parent.relative_to(BUILTIN_SKILLS_DIR))


class TestBuiltinSkillFrontmatter:
    """Required frontmatter is what makes a packaged skill findable at all."""

    def test_builtin_skills_are_discovered(self):
        assert _builtin_skill_files(), f"no SKILL.md found under {BUILTIN_SKILLS_DIR}"

    @pytest.mark.parametrize("skill_file", _builtin_skill_files(), ids=_skill_id)
    def test_name_and_description_present(self, skill_file: Path):
        # The loader's own parser, not a reimplementation of it.
        meta = SkillsLoader._parse_frontmatter(skill_file)
        skill = _skill_id(skill_file)
        assert meta, f"{skill}: no YAML frontmatter — nothing to show in the catalog"
        assert meta.get("name"), f"{skill}: missing required 'name'"
        assert meta.get("description"), f"{skill}: missing required 'description'"


class TestImageAuthoringSkill:
    """The Excalidraw route stays named in the skill that owns diagram technique."""

    def test_names_the_excalidraw_fence(self):
        content = IMAGE_AUTHORING.read_text(encoding="utf-8")
        assert "excalidraw" in content.lower(), (
            "image-authoring must name the excalidraw option; the dashboard renders "
            "the fence but no agent will emit one it was never told about"
        )
