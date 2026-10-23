"""The fixtures and builders the ``skills`` test modules share.

The two autouse fixtures close every ``SkillsLoader`` a test builds and keep the global
config's extra skill paths and the edition's contributed skill roots out of the loader;
``_create_skill`` writes a skill directory and ``_settled`` returns a loader once its
catalog is listed. ``test_skills.py`` and its ``_loading``, ``_triggers`` and
``_authoring`` siblings import what they use of these by name, so "this module" in the
fixtures' docstrings means whichever of those files imports them, and the loader counts
``_close_skills_loaders`` quotes cover the tests of the four files together.
"""

from __future__ import annotations

import pytest

from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.skills import SkillsLoader
from kiro_crew.testing.wait import wait_until


@pytest.fixture(autouse=True)
def _close_skills_loaders(close_skills_loaders):
    """Release every loader this module builds, with its index and its worker.

    Settling a loader is what starts its ``skill-catalog-refresh`` worker and
    opens its SQLite index, and the tests that only reach a settled loader
    through a shared ``_loader`` helper never close one. Measured over this file
    at ``-n0``: 113 parked workers and 348 descriptors at the end without it, 0
    and 14 with it.
    """


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim and no test in the four modules that import it calls monkeypatch.undo()
def _isolate_extra_paths(monkeypatch, tmp_path_factory):
    """SkillsLoader.__init__ reads global config for ``skills.extra_paths`` and
    edition-contributed skill roots via the ``extra_skills()`` seam; isolate both
    so a developer's local ~/.kirocrew extra_paths / composed companion roots
    don't bleed into these hermetic loader tests. Tests that need extra_paths
    pass ``config=``; tests that need edition-root resolution monkeypatch
    ``DefaultMcpToolingProvider.extra_skills``."""
    from kiro_crew.platform.defaults import DefaultMcpToolingProvider

    monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        classmethod(lambda cls: KiroCrewConfig(skills=SkillsConfig(extra_paths=[]))),
    )
    monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: [])


def _create_skill(skills_dir, name, content):
    """Helper to create a skill directory with SKILL.md."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content)


#: Lost-run bound on one discovery walk of these few-file trees, which takes
#: milliseconds. It only ends a wedged walk, so it sits far above the loader's
#: own two-second cold-start budget and under half the suite's 120 s timeout.
_CATALOG_SETTLE_SECS = 30.0


def _settled(loader: SkillsLoader) -> SkillsLoader:
    """Return *loader* once its catalog lists the skills tree as it is now.

    A fresh skills root has no listed entry to serve, and neither has a root
    just after a mutation, so the loader's next enumeration waits at most
    ``_COLD_CATALOG_WAIT_SECS`` (2 s) for the background walk and then answers
    ``[]`` with the scope marked ``"building"``. That is by design: a live turn
    never stalls on discovery. A test is not a live turn: on a loaded Windows
    runner the walk outlasts the budget, and then a positive assertion reads an
    empty catalog and a negative one passes for the trivial reason.

    ``_iter`` arms the walk without parsing a body or logging anything a test
    counts, and the poll ends when ``catalog_status`` says the list is complete.
    """

    def _listed() -> bool:
        loader._iter()
        return loader.catalog_status() == "complete"

    wait_until(
        _listed,
        timeout=_CATALOG_SETTLE_SECS,
        describe=lambda: f"catalog_status={loader.catalog_status()!r}",
    )
    return loader
