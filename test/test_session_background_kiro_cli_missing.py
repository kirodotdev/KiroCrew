"""The background session's start failure when kiro-cli is not installed.

No kiro-cli is the expected state right after a fresh install, and the dashboard
answers it with its Set up Kiro page. The background warm therefore reports it
as one line carrying the refusal's own message, which already names the
directories searched and the fix. Any other start failure keeps its traceback.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.kiro_cli_missing import AcpKiroCliMissing
from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import BACKGROUND_KEY, SessionManager

_MISSING = "kiro-cli not found (searched 14 directories; set KIROCREW_KIRO_BIN or install it)"


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    return c


def _failing_factory(exc: BaseException):
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock(side_effect=exc)
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: False
        m.stream_command = MagicMock()
        return m

    return factory


def _start_failures(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and ("background session" in r.getMessage().lower())
    ]


@pytest.mark.asyncio
async def test_a_missing_kiro_cli_is_one_line_without_a_traceback(cfg, caplog):
    mgr = SessionManager(cfg, provider_factory=_failing_factory(AcpKiroCliMissing(_MISSING)))
    await mgr.start_pool()
    with caplog.at_level(logging.WARNING):
        await mgr._ensure_background()

    (record,) = _start_failures(caplog)
    assert record.exc_info is None
    assert _MISSING in record.getMessage()
    assert "\n" not in record.getMessage()
    assert BACKGROUND_KEY not in mgr._sessions
    await mgr.close_all()


@pytest.mark.asyncio
async def test_any_other_start_failure_keeps_its_traceback(cfg, caplog):
    mgr = SessionManager(cfg, provider_factory=_failing_factory(RuntimeError("spawn exploded")))
    await mgr.start_pool()
    with caplog.at_level(logging.WARNING):
        await mgr._ensure_background()

    (record,) = _start_failures(caplog)
    assert record.getMessage() == "Failed to create background session"
    assert record.exc_info is not None
    assert BACKGROUND_KEY not in mgr._sessions
    await mgr.close_all()
