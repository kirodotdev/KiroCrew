"""The autouse fixtures ``test_cron_script.py`` and its two split parts share.

``_cron_caller_is_named`` names the cron caller ``mcp_cron`` requires, and
``_crons_dir_tracks_patched_home`` points ``cron_script.config_dir()`` at
``<patched home>/.kirocrew``.
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
def _crons_dir_tracks_patched_home(_floor_monkeypatch, tmp_path):
    """Keep ``cron_script.config_dir()`` pointed at ``<patched home>/.kirocrew``.

    Tests write scripts under ``<home>/.kirocrew/crons``, but ``config_dir()`` reads
    ``KIROCREW_HOME``, which the conftest pins to a different tmp dir. The lookup is
    lazy, so it tracks whatever ``Path.home()`` each test patches.

    The stub creates the directory because the real ``config_dir`` does. A test that
    leaves ``Path.home`` unpatched gets a per-test tmp dir, never the operator's home.
    """
    real_home = Path.home()
    fallback = tmp_path / "kirocrew-home-fallback"

    def _home_dir() -> Path:
        home = Path.home()
        d = fallback if home == real_home else home / ".kirocrew"
        d.mkdir(parents=True, exist_ok=True)
        return d

    _floor_monkeypatch.setattr("kiro_crew.cron_script.config_dir", _home_dir)
