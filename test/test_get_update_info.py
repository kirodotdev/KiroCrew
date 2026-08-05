"""Tests for get_update_info accessor in dashboard/handlers.py."""

from __future__ import annotations

from kiro_crew.dashboard.handlers import get_update_info


class TestGetUpdateInfo:
    """Tests for the public update info accessor."""

    def test_returns_dict_with_expected_keys(self) -> None:
        info = get_update_info()
        assert isinstance(info, dict)
        assert "available" in info
        assert "checked" in info

    def test_returns_copy_not_reference(self) -> None:
        info = get_update_info()
        info["available"] = "MUTATED"
        assert get_update_info()["available"] != "MUTATED"

    def test_available_defaults_to_false(self) -> None:
        info = get_update_info()
        assert info["available"] is False
