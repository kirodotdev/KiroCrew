"""Guard: a skill another packaged skill points at must itself be packaged.

`_ensure_builtin_skills` copies `src/kiro_crew/builtin_skills/` into a user's
`~/.kiro/crew/skills/`, on every distribution. Top-level `skills/` is reachable
only through `_project_skills_dir()`, which reads `KIROCREW_PROJECT_DIR` — a
repo checkout and the desktop bundle set it, a `pip install` from the wheel or
sdist does not. A skill that lives only there is therefore invisible to a pip
user, so a packaged skill telling the agent to "see the X skill" resolves to
nothing on disk, silently.

These tests fail if that split reappears.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew.skills import _RELOCATED_SKILLS

PKG = Path(kiro_crew.__file__).resolve().parent
BUILTIN = PKG / "builtin_skills"


def _packaged_skill_names() -> set[str]:
    """Every skill directory under builtin_skills, as loader-visible names."""
    return {
        str(p.parent.relative_to(BUILTIN)).replace("\\", "/") for p in BUILTIN.rglob("SKILL.md")
    }


class TestRelocationTargetsArePackaged:
    """Each destination in the relocation map must exist in the package.

    `_ensure_builtin_skills` quarantines a user's old flat copy only when the
    nested replacement is present. A destination the package never ships makes
    that migration a permanent no-op, and leaves the flat copy — which may be
    stale — as the only copy the loader can find.
    """

    def test_every_relocation_destination_ships(self) -> None:
        packaged = _packaged_skill_names()
        missing = sorted(dest for dest in _RELOCATED_SKILLS.values() if dest not in packaged)
        assert not missing, (
            f"relocation destinations absent from builtin_skills/: {missing}. "
            "Either package the skill or drop its entry from the map."
        )


class TestPackagedSkillsReferenceOnlyPackagedSkills:
    """A shipped skill must not send the agent to a skill that is not shipped."""

    # Referenced as "the `<name>` skill" / "`<name>` skill" in SKILL.md prose.
    _REF = re.compile(r"`([a-z][a-z0-9-]{2,})`(?=[^`\n]{0,24}\bskill\b)")

    @pytest.mark.parametrize(
        "referenced",
        sorted({"kirocrew-worktree-dev", "babysit"}),
    )
    def test_known_cross_references_resolve(self, referenced: str) -> None:
        """The two names packaged skills point at today.

        Pinned by name rather than only by scan so the guard keeps working if
        the prose around them is reworded.
        """
        names = _packaged_skill_names()
        assert any(
            n == referenced or n.endswith("/" + referenced) for n in names
        ), f"{referenced!r} is referenced by a packaged skill but is not packaged"

    def test_no_packaged_skill_points_at_an_unpackaged_skill(self) -> None:
        # Needs the repo-checkout tree to know which names are checkout-only.
        # Against an installed package that tree is absent, and without this
        # skip the scan below would find nothing and pass while guarding
        # nothing — the same silent-vacuity failure mode this file exists to
        # catch. Skip loudly instead.
        repo_skills = PKG.parents[1] / "skills"
        if not repo_skills.is_dir():
            pytest.skip("needs the repo checkout: top-level skills/ is not in the package")

        names = _packaged_skill_names()
        leaves = {n.rsplit("/", 1)[-1] for n in names}
        offenders: list[str] = []
        for skill_md in BUILTIN.rglob("SKILL.md"):
            owner = str(skill_md.parent.relative_to(BUILTIN)).replace("\\", "/")
            text = skill_md.read_text(encoding="utf-8", errors="replace")
            for ref in set(self._REF.findall(text)):
                if ref in leaves or ref in names:
                    continue
                # Only flag names that exist somewhere in the repo's skill trees,
                # so ordinary prose in backticks is not mistaken for a skill.
                if (repo_skills / ref).is_dir() or any(
                    p.parent.name == ref for p in repo_skills.rglob("SKILL.md")
                ):
                    offenders.append(f"{owner} -> {ref}")
        assert not offenders, (
            "packaged skills reference skills that exist only in the repo-checkout "
            f"tree: {sorted(offenders)}"
        )
