"""The fake home, skill writer and app builder the skill browser tests share.

``test_skill_browser.py`` and its ``_containment`` sibling import these by name, and
``test_skills_catalog_cache.py`` imports ``_make_app`` and ``_write_skill`` through
``test_skill_browser``. ``fake_home`` is declared ``name="fake_home"`` on
``fake_home_fixture`` so no test parameter shadows the import.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web

# ── Fixtures ──


@pytest.fixture(name="fake_home")
def fake_home_fixture(tmp_path, monkeypatch):
    """Pin $HOME to tmp_path so Path.home() returns a writable sandbox.

    Also clears KIROCREW_HOME so ``skills_dir()`` resolves to
    ``<tmp>/.kiro/crew/skills`` rather than any value leaked from the
    surrounding build environment.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _write_skill(root: Path, name: str, *, description: str = "", body: str = "body") -> Path:
    """Materialize a SKILL.md under root/<name>/SKILL.md."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = ["---", f"name: {name}"]
    if description:
        fm.append(f"description: {description}")
    fm.append("---")
    skill_dir.joinpath("SKILL.md").write_text("\n".join(fm) + f"\n{body}\n")
    return skill_dir


def _make_app(state):
    from kiro_crew.dashboard.handlers import api_skill_file, api_skill_tree, api_skills

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/skills", api_skills)
    # Route order matters — specific routes must come before {name:.+}.
    # ``/-/`` separator avoids collision with skills named ``.../tree``.
    app.router.add_get("/api/skills/{name:.+}/-/tree", api_skill_tree)
    app.router.add_get("/api/skills/{name:.+}/-/file", api_skill_file)
    return app
