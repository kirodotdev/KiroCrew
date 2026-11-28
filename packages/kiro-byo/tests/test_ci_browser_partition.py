"""Keep actual Chromium tests mandatory without blocking backend CI shards."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import conftest

ROOT = Path(__file__).resolve().parents[1]


def test_browser_module_inventory_is_explicit_and_exists():
    expected = {
        "test_crew11_name_scope_browser.py",
        "test_crew11_picker_scroll_browser.py",
        "test_overview_setup_layout.py",
        "test_overview_two_column_layout.py",
        "test_schedule_picker_browser.py",
        "test_settings_layout.py",
    }
    assert conftest._BROWSER_TEST_MODULES == expected
    assert all((ROOT / "tests" / module).is_file() for module in expected)
    # Browser-tool policy tests and Node-only picker tests belong in core CI.
    assert "test_crew_browser_parity.py" not in expected
    assert "test_mode_live_browser_sync.py" not in expected


def test_collection_hook_only_marks_real_browser_modules():
    marked = []
    for name in ("test_crew11_picker_scroll_browser.py", "test_crew_browser_parity.py"):
        item = SimpleNamespace(path=Path(name), add_marker=lambda m, n=name: marked.append((n, m.name)))
        conftest.pytest_collection_modifyitems([item])
    assert marked == [("test_crew11_picker_scroll_browser.py", "browser")]


def test_ci_still_runs_browser_job_and_exports_failures():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'coverage run --branch -m pytest -q -m "not browser"' in ci
    assert "  browser-ui:" in ci
    assert 'python -m pytest -q -m browser --timeout=45' in ci
    assert "chromium --version" in ci
    assert "browser-results.xml" in ci
    assert "if: always()" in ci
    assert "actions/upload-artifact@v4" in ci
