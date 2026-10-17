"""Shared fixtures of the ``test_dogfood_learnings*.py`` family.

A fixture is active in a module only because the module imports it into its own
namespace: every module of the family imports the autouse ``_cwd_is_the_scratch_dir``
pin, and the modules whose classes request ``synthetic_clone_is_safe`` with
``usefixtures`` import that one too. A helper only one module uses stays in that module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse cwd pin of test_dogfood_learnings.py and its parts, moved verbatim; no dogfood-learnings module calls monkeypatch.undo()
def _cwd_is_the_scratch_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test here with the process cwd inside its own ``tmp_path``.

    The clones these tests drive are scratch repositories under ``tmp_path``, and the
    product's git helpers (``backend/commit._git``, ``spine/driver_git._git``,
    ``spine/proposer._git``, ``spine/gate._changed_paths``, ``spine/scope.scoped_relpaths``
    and the ``git apply`` calls beside them) address that clone with ``-C`` and pass no
    ``cwd``: each of those ~100 spawns per run inherits the process cwd. By default that
    is the pytest worker's cwd -- THIS checkout -- so a helper handed the wrong path, or a
    git that falls back to the cwd, would read or write the developer's own repository.
    The tests' own ``subprocess.run`` calls already pass ``cwd=``; this pins the one
    default they cannot reach. ``monkeypatch.chdir`` restores the cwd at teardown.
    """
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def synthetic_clone_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda _clone: True)
    monkeypatch.setattr(clone_setup, "_push_disabled", lambda _clone: True)
