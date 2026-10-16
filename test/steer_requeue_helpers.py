"""The SEL patch and the mid-turn slot shared by the ``test_steer_requeue*.py`` suites.

``_patch_sel`` swaps ``kiro_crew.dashboard.chat_handlers.sel`` for a mock for the
test's duration and yields the mock its calls return; each suite binds it by name so
pytest registers it for that module. ``_running_slot`` makes a slot that reads as
mid-turn.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def _patch_sel():
    mock_sel = MagicMock()
    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel):
        yield mock_sel


def _running_slot(state, key="test"):
    slot = state.get_or_create_slot(key)
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot
