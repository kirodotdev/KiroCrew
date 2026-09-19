"""A cleared project resolves, unmocked, to the per-session workspace directory."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.config.loader import workspace_root
from kiro_crew.config.paths import resolved_cwd
from kiro_crew.session import SessionManager


def test_an_empty_cwd_resolves_to_the_sessions_own_workspace_directory():
    resolved = Path(resolved_cwd("", "dashboard:x"))

    assert resolved.parent == workspace_root()
    assert resolved.name not in ("", "_default")
    assert resolved != Path(resolved_cwd("", "dashboard:y"))


@pytest.mark.asyncio
async def test_the_real_session_manager_resolves_a_cleared_arm_without_raising():
    cfg = MagicMock()
    cfg.default_agent = ""
    cfg.model = "auto"
    cfg.session.pool_size = 0
    cfg.session.pool_agent = ""
    cfg.session.pool_ttl_secs = 0

    resolved = await SessionManager(cfg).resolve_arm_cwd("dashboard:x", "")

    assert Path(resolved).parent == workspace_root()
