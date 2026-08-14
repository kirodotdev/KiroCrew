"""The Windows-gap collection hook that gates this suite.

On any POSIX host the hook returns immediately, so the part that actually decides
anything — the skip marker and the two name-matching legs that pick which tracked
tests it lands on — never runs where the suite is normally developed. The list it
consults is documented as a burn-down that "cannot silently absorb a real
regression"; that only holds if the matching is exercised, so force the platform
flag and drive the hook directly.

The conftest module is imported by its canonical package path, which is the same
module object pytest already loaded as this directory's conftest — so this reads the
one live copy of the list rather than a second import of it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.spec_builder.tests import conftest as spec_conftest


class _Item:
    """The two attributes the hook reads off a collected pytest item."""

    def __init__(self, name: str, originalname: str | None = None) -> None:
        self.name = name
        self.originalname = originalname if originalname is not None else name
        self.markers: list[Any] = []

    def add_marker(self, marker: Any) -> None:
        self.markers.append(marker)


_TRACKED = "test_stop_write_is_refused_for_a_replaced_spec"


class TestPosixHostsAreUntouched:
    def test_hook_marks_nothing_off_windows(self, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        items = [_Item(_TRACKED), _Item("test_something_untracked")]

        spec_conftest.pytest_collection_modifyitems(None, items)

        assert [it.markers for it in items] == [[], []]


class TestWindowsGapsAreSkipped:
    def test_tracked_test_is_skipped_with_a_reason(self, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        item = _Item(_TRACKED)

        spec_conftest.pytest_collection_modifyitems(None, [item])

        assert len(item.markers) == 1
        marker = item.markers[0]
        assert marker.name == "skip"
        assert "known Windows gap" in marker.kwargs["reason"]

    def test_every_parametrization_is_skipped_via_the_bracket_stripped_name(self, monkeypatch):
        """A parametrized id must match too, even when ``originalname`` disagrees."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        item = _Item(f"{_TRACKED}[case-2]", originalname="not_a_tracked_name")

        spec_conftest.pytest_collection_modifyitems(None, [item])

        assert len(item.markers) == 1

    def test_untracked_test_still_runs_on_windows(self, monkeypatch):
        """Anything absent from the list must keep failing the Windows job."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        item = _Item("test_a_new_posix_only_assertion")

        spec_conftest.pytest_collection_modifyitems(None, [item])

        assert item.markers == []


class TestTheListItself:
    def test_the_two_reason_buckets_make_up_the_whole_list(self):
        assert spec_conftest._WINDOWS_GAPS == (
            spec_conftest._POSIX_SENTINEL_PINNING | spec_conftest._POSIX_PATH_SHAPE
        )
        assert spec_conftest._POSIX_SENTINEL_PINNING
        assert spec_conftest._POSIX_PATH_SHAPE

    def test_every_tracked_name_is_a_test_that_exists(self):
        """A stale entry silently absorbs a regression, which is what the list forbids."""
        source = (Path(spec_conftest.__file__).parent / "test_routes.py").read_text()
        missing = sorted(n for n in spec_conftest._WINDOWS_GAPS if f"def {n}(" not in source)
        assert missing == []
