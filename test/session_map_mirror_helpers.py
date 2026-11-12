"""The SessionMap fixture shared by the ``test_session_map_mirror*.py`` modules."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.session_map import SessionMap


@pytest.fixture(name="session_map")
def session_map_fixture(tmp_path):
    """A SessionMap backed by a temp directory."""
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        yield SessionMap()
