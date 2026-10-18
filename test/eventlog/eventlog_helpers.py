"""Shared autouse fixture for the member event log suites.

``test_eventlog.py`` and ``test_eventlog_legacy_fold.py`` each import ``_isolated_home`` from here
by name, so every test in both runs against its own data home. This is a plain module, not a
conftest.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
# flake-ok: pre-existing per-test data home pin on the shared monkeypatch, moved verbatim; the one monkeypatch.undo(), in test_eventlog_legacy_fold.py, re-pins KIROCREW_HOME after it
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the per-test data home pin of the member event log suites, moved verbatim; the one test that calls monkeypatch.undo() re-pins KIROCREW_HOME itself
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one.

    A member log is a ``member``-kind crew log now, so the store derives its path
    from the data home rather than taking one. Repointing the home is therefore
    what isolates a test, and it is the same fixture the crew log's own suites use.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield
