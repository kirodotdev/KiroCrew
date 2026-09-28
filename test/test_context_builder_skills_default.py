"""Prove the rootdir ``_no_default_builtin_skills_sync`` floor does what it claims:

a bare ``ContextBuilder()`` / ``SkillsLoader()`` never syncs the builtin-skills
tree to disk, while an explicit ``install_builtins=True`` still gets the real
sync. See ``conftest.py``'s ``_no_default_builtin_skills_sync`` docstring for why
the floor exists (a bare construction otherwise costs ~20s on a loaded host).
"""

from __future__ import annotations

import pytest

from kiro_crew.context import ContextBuilder
from kiro_crew.skills import SkillsLoader


def test_bare_context_builder_does_not_write_builtin_skills(tmp_path, monkeypatch):
    """A default ``ContextBuilder()`` must not touch the builtin-skills sync."""
    calls: list[object] = []
    monkeypatch.setattr(
        "kiro_crew.skills._ensure_builtin_skills",
        lambda skills_dir: calls.append(skills_dir),
    )

    ContextBuilder()

    assert calls == [], "the floor should have defaulted install_builtins to False"


def test_bare_skills_loader_does_not_write_builtin_skills(tmp_path, monkeypatch):
    """Same guarantee directly against ``SkillsLoader()``, no path/kwargs at all."""
    calls: list[object] = []
    monkeypatch.setattr(
        "kiro_crew.skills._ensure_builtin_skills",
        lambda skills_dir: calls.append(skills_dir),
    )

    SkillsLoader()

    assert calls == []


def test_explicit_install_builtins_true_still_syncs(tmp_path, monkeypatch):
    """An explicit opt-in must still reach the real sync through the floor."""
    calls: list[object] = []
    monkeypatch.setattr(
        "kiro_crew.skills._ensure_builtin_skills",
        lambda skills_dir: calls.append(skills_dir),
    )

    SkillsLoader(skills_path=tmp_path / "skills", install_builtins=True)

    assert len(calls) == 1
    assert calls[0] == tmp_path / "skills"


def test_explicit_install_builtins_true_positional_still_syncs(tmp_path, monkeypatch):
    """The positional form of the opt-in must not be swallowed by the floor either."""
    calls: list[object] = []
    monkeypatch.setattr(
        "kiro_crew.skills._ensure_builtin_skills",
        lambda skills_dir: calls.append(skills_dir),
    )

    SkillsLoader(tmp_path / "skills", True)

    assert len(calls) == 1


@pytest.mark.real_builtin_skills_sync
def test_marked_test_gets_the_real_default_back(tmp_path, monkeypatch):
    """The opt-out marker leaves the constructor untouched: omitted means True."""
    calls: list[object] = []
    monkeypatch.setattr(
        "kiro_crew.skills._ensure_builtin_skills",
        lambda skills_dir: calls.append(skills_dir),
    )

    SkillsLoader(skills_path=tmp_path / "skills")

    assert calls == [tmp_path / "skills"]
