"""The autouse fixtures ``test_cron_script.py`` and its two split parts share.

``_cron_caller_is_named`` names the cron caller ``mcp_cron`` requires, and
``_crons_dir_tracks_patched_home`` points ``cron_script.config_dir()`` at
``<patched home>/.kirocrew`` (a per-test tmp dir when a test leaves ``Path.home``
unpatched). ``test_cron_script.py``, ``test_cron_script_execution.py`` and
``test_cron_script_hardening.py`` import both, so "this module" in their docstrings
means whichever of those files imports them.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """Every test in this module exercises cron field handling, not authorization.

    ``mcp_cron`` refuses a write from a caller it cannot name, so this states the
    precondition these tests always assumed. See the ``named_cron_caller``
    fixture in ``test/conftest.py``.
    """


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim and no test in the cron script files calls monkeypatch.undo()
def _crons_dir_tracks_patched_home(monkeypatch, tmp_path):
    """Keep ``cron_script.config_dir()`` pointed at ``<patched home>/.kirocrew``.

    The data home moved from the top-level ``~/.kirocrew`` to ``~/.kiro/crew``
    (``config_dir()``), and ``resolve_script_path`` now derives its allowed
    ``crons/`` dir from ``config_dir()`` rather than ``Path.home()/".kirocrew"``.
    These tests patch ``Path.home()`` per-test and write scripts under
    ``<home>/.kirocrew/crons`` — but ``config_dir()`` reads ``KIROCREW_HOME``
    (pinned to a *different* tmp dir by the conftest ``_isolate_kirocrew_home``
    fixture), so without this redirect the allowed dir would never match.
    Redirect ``config_dir`` to ``Path.home()/".kirocrew"`` (evaluated lazily, so
    it tracks whatever ``Path.home()`` each test patches) — preserving the
    existing ``.kirocrew/crons`` layout the tests build. Tests that patch
    ``cron_script.config_dir`` themselves still win (applied later).

    The stub CREATES the directory, because the real resolver does: ``config_dir``
    runs ``mkdir(parents=True, exist_ok=True)`` on every call, so a path it hands
    back always exists on disk. A stub that only computes the path models a home
    that the production code cannot be handed, and a caller that legitimately
    creates something beside the tree it returns then fails on a missing parent
    that no real run has.

    Creating means the fallback matters: a test in this module that does NOT patch
    ``Path.home`` would otherwise have this stub create directories in the
    OPERATOR's real home, which outlive the run and which the conftest's
    real-data-home guards cannot see (they inspect ``KIROCREW_HOME`` only). So an
    unpatched home resolves to a per-test tmp dir instead, the same shape
    ``cron_secret_env_helpers.py`` uses.
    """
    real_home = Path.home()
    fallback = tmp_path / "kirocrew-home-fallback"

    def _home_dir() -> Path:
        home = Path.home()
        d = fallback if home == real_home else home / ".kirocrew"
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr("kiro_crew.cron_script.config_dir", _home_dir)
