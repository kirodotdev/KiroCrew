"""A background helper's expected backend failure logs one line, not a traceback."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from kiro_crew import suggestions
from kiro_crew.acp.client import AcpError
from kiro_crew.background_failure_log import log_background_failure

_LOGGER = "kiro_crew.test_background_failure_log"
_SIGN_IN = "Sign in to Kiro to send a prompt."


def _raised(exc: BaseException) -> BaseException:
    try:
        raise exc
    except BaseException as caught:
        return caught


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING]


def test_an_acp_error_is_one_warning_line_without_a_traceback(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    exc = _raised(AcpError(f"{_SIGN_IN}\n  Then try again."))

    log_background_failure(logging.getLogger(_LOGGER), "Suggestions generation failed", exc)

    [warning] = _warnings(caplog)
    assert warning.getMessage() == f"Suggestions generation failed: {_SIGN_IN} Then try again."
    assert warning.exc_info is None
    debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert [r.exc_info[1] for r in debug if r.exc_info] == [exc]


def test_a_long_acp_error_message_is_capped(caplog) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    log_background_failure(logging.getLogger(_LOGGER), "Tips", _raised(AcpError("x" * 2000)))

    [warning] = _warnings(caplog)
    assert len(warning.getMessage()) < 600
    assert warning.getMessage().endswith("…")


def test_an_unexpected_exception_keeps_its_traceback(caplog) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    exc = _raised(KeyError("context"))

    log_background_failure(logging.getLogger(_LOGGER), "Tips generation failed", exc)

    [warning] = _warnings(caplog)
    assert warning.getMessage() == "Tips generation failed"
    assert warning.exc_info is not None and warning.exc_info[1] is exc


@pytest.mark.asyncio
async def test_a_signed_out_suggestions_refresh_logs_no_traceback(monkeypatch, caplog) -> None:
    async def _signed_out(_sessions, _prompt, **_kw):
        raise AcpError(_SIGN_IN)

    monkeypatch.setattr(suggestions, "_build_context", lambda _state: "context " * 20)
    monkeypatch.setattr(suggestions, "run_bg_oneliner", _signed_out)
    caplog.set_level(logging.WARNING, logger="kiro_crew.suggestions")

    state = SimpleNamespace(sessions=None)
    assert await suggestions.generate_suggestions(state, ui_language="en") == []  # type: ignore[arg-type]

    [warning] = _warnings(caplog)
    assert warning.getMessage() == f"Suggestions generation failed: {_SIGN_IN}"
    assert warning.exc_info is None
