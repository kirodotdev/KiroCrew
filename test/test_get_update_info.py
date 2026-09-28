"""Tests for get_update_info accessor in dashboard/handlers.py."""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.handlers import get_update_info, updates


@pytest.fixture(autouse=True)
def _isolated_cache():
    """Reset the module cache around each test.

    The cache is module-level state that any earlier test's check can leave
    populated, so asserting the no-verdict shape has to establish it rather than
    inherit whatever ran first in this worker.
    """
    original = dict(updates._update_info)
    updates._set_update_info()
    yield
    updates._update_info.clear()
    updates._update_info.update(original)


class TestGetUpdateInfo:
    """Tests for the public update info accessor."""

    def test_returns_dict_with_expected_keys(self) -> None:
        info = get_update_info()
        assert isinstance(info, dict)
        assert "update_available" in info
        assert "check_status" in info

    def test_returns_copy_not_reference(self) -> None:
        info = get_update_info()
        info["update_available"] = "MUTATED"
        assert get_update_info()["update_available"] != "MUTATED"

    def test_defaults_to_no_verdict_not_a_negative_one(self) -> None:
        # Before any check has reached a verdict there is no verdict at all.
        # Defaulting to False is what let the dashboard report an out-of-date
        # install as current.
        info = get_update_info()
        assert info["update_available"] is None
        assert info["check_status"] == "unchecked"
