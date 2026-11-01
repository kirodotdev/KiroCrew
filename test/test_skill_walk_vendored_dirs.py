"""The skill walk skips package-manager trees that ship their own SKILL.md.

A skill's scripts can install npm or pip packages beside it, and some of those
packages carry ``SKILL.md`` files of their own (Playwright does, deep under
``node_modules/playwright-core``). Those are vendored copies, not the user's
skills, so neither walk (the provider walk and the confined project walk) may
index them. A real skill nested in a plain folder, and one named ``build``,
still load.
"""

from __future__ import annotations

import pytest

from kiro_crew import skill_trust
from kiro_crew import skills as skills_mod


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    skill_trust.reset_cache_for_tests()
    yield
    skill_trust.reset_cache_for_tests()


def _skill(root, rel):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {rel.rsplit('/', 1)[-1]}\ndescription: d\n---\n\nBODY\n",
        encoding="utf-8",
    )


def _tree(base):
    _skill(base, "real")
    _skill(base, "real/scripts/node_modules/playwright-core/lib/skills/playwright-cli")
    _skill(base, "real/scripts/__pycache__/cached")
    _skill(base, "real/scripts/venv/lib/python3.12/site-packages/pkg/skills/bundled")
    _skill(base, "node_modules/top-level-vendored")
    _skill(base, "family/leaf")
    _skill(base, "build")


_EXPECTED = ["build", "family/leaf", "real"]


def test_provider_walk_skips_vendored_trees(tmp_path):
    base = tmp_path / "skills"
    _tree(base)

    names = [name for name, _ in skills_mod._iter_skill_files(base)]

    assert names == _EXPECTED


def test_confined_project_walk_skips_vendored_trees(tmp_path):
    if not skill_trust.project_skill_traversal_supported():
        pytest.skip("no-follow project traversal is unavailable on this platform")
    project = tmp_path / "proj"
    base = project / ".kiro" / "skills"
    _tree(base)

    names = [name for name, _ in skills_mod._iter_skill_files(base, confine_to=(str(project),))]

    assert names == _EXPECTED
