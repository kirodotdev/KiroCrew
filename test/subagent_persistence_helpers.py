"""The ``agent_root`` fixture the subagent persistence tests share.

``test_subagent_persistence.py`` and its ``_spawn_lifecycle`` and ``_orphans`` siblings
request ``agent_root``: a subagent registry below the test's ``tmp_path``, patched in as
``kiro_crew.subagent_persistence._SUBAGENTS_DIR`` for the test's duration. It is declared
``name="agent_root"`` on ``agent_root_fixture``, so no test parameter shadows the import.
"""

from __future__ import annotations

import pytest


@pytest.fixture(name="agent_root")
def agent_root_fixture(tmp_path, monkeypatch):
    """Point persistence at a registry below this test's temp directory."""
    root = tmp_path / "subagents"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", root)
    return root
