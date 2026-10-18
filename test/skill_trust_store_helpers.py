"""Fixtures shared by the three ``test_skill_trust_store*.py`` suites.

``_isolated_home`` (autouse) points the data home under ``tmp_path`` and drops the
memoized enforcement read around every test; ``project`` is a directory holding an
empty ``.kiro/skills`` tree. ``test_skill_trust_store.py``,
``test_skill_trust_store_project_skill_loading.py`` and
``test_skill_trust_store_operator_consent.py`` import both.
"""

from __future__ import annotations

import pytest

from kiro_crew import skill_trust


@pytest.fixture(autouse=True)
# flake-ok: pre-existing autouse fixture moved verbatim; no importing module calls monkeypatch.undo()
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no importing module calls monkeypatch.undo()
def _isolated_home(tmp_path, monkeypatch):
    """Point the data home at tmp_path and drop the memoized enforcement read."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    skill_trust.reset_cache_for_tests()
    yield
    skill_trust.reset_cache_for_tests()


@pytest.fixture(name="project")
def project_fixture(tmp_path):
    d = tmp_path / "proj"
    (d / ".kiro" / "skills").mkdir(parents=True)
    return d
