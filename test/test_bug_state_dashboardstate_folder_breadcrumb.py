"""RED test: DashboardState.folder_breadcrumb must tolerate folders lacking 'id'.

load_folders() (unlike load_tags) does no id-filtering, so a legacy/corrupt
folders.json can yield folder dicts with no 'id' key. The breadcrumb walk builds
``by_id = {f["id"]: f for f in self._folders}`` with a hard index, which raises
KeyError('id') on such an entry — violating the docstring's "tolerant of dangling
references" / "Returns '' for an empty or unknown folder id" contract.
"""

from __future__ import annotations

from kiro_crew.dashboard.state import DashboardState


def test_agent_defect() -> None:
    # Bypass the heavy constructor — folder_breadcrumb only reads self._folders.
    state = object.__new__(DashboardState)
    state._folders = [{"name": "x"}]  # legacy/corrupt folder dict: no 'id' key

    # Per docstring, an unknown folder id must return "" rather than raising.
    assert state.folder_breadcrumb("whatever") == ""
