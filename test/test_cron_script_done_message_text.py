"""A script's ``Done``/``Report`` message is text, whatever the script passed.

``raise Done(42)`` or ``raise Report({"n": 3})`` is a clean finish. The message
reaches the gateway as text, because ``redact()`` there only takes text; a raise
in that handling would record the clean finish as a failure (a strike toward
auto-pause and a failure alert), keep a ``Done`` job so it fires again, and drop
the ``Report``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.slack.gateway as gw_mod
from kiro_crew.cron_script_context import DoneError, ReportError


@pytest.mark.parametrize(
    "given,text",
    [
        ("", ""),
        (None, ""),
        (0, ""),
        ([], ""),
        ("all done", "all done"),
        (42, "42"),
        ({"n": 3}, "{'n': 3}"),
    ],
)
@pytest.mark.parametrize("error", [DoneError, ReportError])
def test_the_message_is_kept_as_text(error, given, text):
    assert error(given).message == text


def test_the_default_message_is_empty():
    assert DoneError().message == ReportError().message == ""


@pytest.mark.parametrize(
    "status,removes",
    [("done", True), ("report", False)],
)
@pytest.mark.asyncio
async def test_a_non_text_envelope_message_is_a_clean_finish(monkeypatch, status, removes):
    from cron_gateway_integration_helpers import _make_gw, _make_script_job

    gw = _make_gw()
    gw._running_script_ids = set()
    gw.cron_svc = MagicMock(name="cron_svc")
    gw._alert_cron_failure = AsyncMock()
    gw._deliver_script_result = AsyncMock()
    monkeypatch.setattr(gw_mod, "sel", MagicMock(name="sel"))
    monkeypatch.setattr(gw_mod, "_await_cron_fire_time_gate", AsyncMock(return_value=(None, False)))
    monkeypatch.setattr(
        gw_mod, "run_in_cron_pool", AsyncMock(return_value={"status": status, "message": 42})
    )
    job = _make_script_job()

    result = await gw._cron_run_script(job)

    assert result == "42"
    assert job.last_status == "ok"
    assert job.consecutive_failures == 0
    gw._alert_cron_failure.assert_not_awaited()
    args, kwargs = gw._deliver_script_result.await_args
    assert args[1] == "42"
    assert kwargs.get("remove", False) is removes
