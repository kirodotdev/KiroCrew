"""The cause of a decision's ``provider`` error reaches the shipped WARNING log."""

import asyncio
import logging

from test_decisions_gate import consent  # noqa: F401 - fixture
from test_decisions_gate import install_impl  # noqa: F401 - fixture
from test_decisions_gate import log_home  # noqa: F401 - fixture
from test_decisions_gate import POINT, QUESTIONS, _config, _RaisingOracle

from kiro_crew.decisions import gate as gate_mod
from kiro_crew.decisions.gate import decide


def _failure_lines(caplog):
    return [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if "provider call failed" in r.getMessage()
    ]


def test_the_first_failure_per_point_and_class_is_a_warning_then_info(
    install_impl, log_home, caplog  # noqa: F811
):
    """``agent.log_level`` ships as WARNING, so the class must reach it once, not every time."""
    install_impl(_RaisingOracle(ConnectionRefusedError("rejected body: my-secret-payload")))
    with caplog.at_level(logging.INFO, logger="kiro_crew.decisions.gate"):
        for _ in range(3):
            asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config()))
        install_impl(_RaisingOracle(ValueError("response is not JSON")))
        asyncio.run(decide(POINT, "hi", QUESTIONS, config=_config()))
    refused = f"decisions: {POINT} provider call failed (ConnectionRefusedError)"
    assert _failure_lines(caplog) == [
        (logging.WARNING, refused),
        (logging.INFO, refused),
        (logging.INFO, refused),
        (logging.WARNING, f"decisions: {POINT} provider call failed (ValueError)"),
    ]
    assert "my-secret-payload" not in caplog.text, "the class only, never the provider's message"
    assert "response is not JSON" not in caplog.text
    rows = log_home()
    assert [r["error"] for r in rows] == [gate_mod.ERROR_PROVIDER] * 4
