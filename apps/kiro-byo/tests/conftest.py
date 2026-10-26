from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_kiro_byo_home(tmp_path, monkeypatch):
    """Keep every test's mutable Kiro BYO state out of the source/release tree.

    Individual tests may still override KIRO_BYO_HOME for a more specific
    scenario.  The autouse default prevents runtime trace/session/run files from
    leaking into the checkout merely because a test forgot to provide a home.
    """
    home = tmp_path / "kiro-byo-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIRO_BYO_HOME", str(home))
