"""Deterministic startup timing without credentials, prompt text or live compute."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from kiro_crew.coder import startup
from kiro_crew.coder.client import CoderClient


@pytest.mark.parametrize("failure", [None, ValueError("private-detail"), asyncio.CancelledError()])
def test_timing_reports_elapsed_and_preserves_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: BaseException | None,
) -> None:
    ticks = iter([10.0, 10.125])
    monkeypatch.setattr(startup, "monotonic", lambda: next(ticks))
    logger = logging.getLogger("test.startup")
    with caplog.at_level(logging.INFO, logger=logger.name):
        if failure is None:
            with startup.startup_timing(logger, "prepare", workspace="crew-fixture"):
                pass
        else:
            with pytest.raises(type(failure)) as raised:
                with startup.startup_timing(logger, "prepare", workspace="crew-fixture"):
                    raise failure
            assert raised.value is failure
    expected = (
        "ok"
        if failure is None
        else "cancelled" if isinstance(failure, asyncio.CancelledError) else "error"
    )
    assert f"outcome={expected}" in caplog.text
    assert "elapsed_ms=125" in caplog.text
    assert "workspace=crew-fixture" in caplog.text
    assert "private-detail" not in caplog.text


def test_timing_prefers_the_gateways_content_free_producer_when_installed() -> None:
    fallback = logging.getLogger("test.startup.fallback")
    timing_logger = logging.getLogger(startup.TIMING_LOGGER_NAME)
    assert startup.startup_logger(fallback) is fallback
    handler = logging.NullHandler()
    timing_logger.addHandler(handler)
    try:
        assert startup.startup_logger(fallback) is timing_logger
    finally:
        timing_logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_coder_cli_timing_never_logs_argv_environment_or_output(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "fake-private-canary-0123456789"
    runner = AsyncMock(return_value=canary.encode())
    client = CoderClient("coder", "https://coder.example", canary, tmp_path, runner=runner)
    with caplog.at_level(logging.INFO, logger="kiro_crew.coder.client"):
        assert await client._call("list", canary) == canary.encode()
    assert "step=cli_list" in caplog.text
    assert "outcome=ok" in caplog.text
    assert canary not in caplog.text
