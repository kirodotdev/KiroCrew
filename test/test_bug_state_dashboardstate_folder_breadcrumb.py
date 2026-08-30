"""DashboardState.folder_breadcrumb tolerates a manually built malformed state."""

from __future__ import annotations

from kiro_crew.dashboard.state import DashboardState


def test_agent_defect() -> None:
    # Bypass the heavy constructor — folder_breadcrumb only reads self._folders.
    state = object.__new__(DashboardState)
    state._folders = [{"name": "x"}]  # legacy/corrupt folder dict: no 'id' key

    # Per docstring, an unknown folder id must return "" rather than raising.
    assert state.folder_breadcrumb("whatever") == ""
