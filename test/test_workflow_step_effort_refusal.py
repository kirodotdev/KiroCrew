"""A step's effort the session's model refuses leaves the session running, not failed."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.command_refused import AcpCommandRefused
from kiro_crew.workflows.agent_exec import apply_step_effort


@pytest.mark.asyncio
async def test_a_refused_step_effort_keeps_the_session() -> None:
    provider = MagicMock()
    provider.accepts_effort_level.return_value = True
    provider.change_effort = AsyncMock(side_effect=AcpCommandRefused("no effort", code=-32603))

    await apply_step_effort(provider, "high")

    provider.change_effort.assert_awaited_once_with("high")


@pytest.mark.asyncio
async def test_another_step_effort_failure_still_raises() -> None:
    provider = MagicMock()
    provider.accepts_effort_level.return_value = True
    provider.change_effort = AsyncMock(side_effect=RuntimeError("overlay locked"))

    with pytest.raises(RuntimeError):
        await apply_step_effort(provider, "high")
