"""Shared fixture for the auto_research handler suite that ``test_auto_research.py`` heads
(that file and the parts its docstring lists): every test runs against its own campaign
database and research directory under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True, name="_isolate")
def isolate_fixture(tmp_path: Path):
    """Isolate DB and research dir per test."""
    with (
        patch(
            "kiro_crew.apps.builtins.auto_research.handlers.DB_PATH",
            tmp_path / "test.db",
        ),
        patch(
            "kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR",
            tmp_path / "research",
        ),
    ):
        yield tmp_path
