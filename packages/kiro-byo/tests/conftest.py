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


# Browser-dependent UI integration checks run in an isolated CI job.
# Mark by explicit filenames, not by substring: tests about browser tool
# permissions/ACP behavior are ordinary backend tests and stay in core.
_BROWSER_TEST_MODULES = frozenset({
    "test_crew11_name_scope_browser.py",
    "test_crew11_picker_scroll_browser.py",
    "test_overview_setup_layout.py",
    "test_overview_two_column_layout.py",
    "test_schedule_picker_browser.py",
    "test_settings_layout.py",
})


def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.name in _BROWSER_TEST_MODULES:
            # The same file can contain non-browser contract checks. Keeping
            # them in the browser job is intentional: no duplicate collection
            # and CI summaries account for every test exactly once.
            item.add_marker(pytest.mark.browser)
