"""Tests for mcp_cron channel parameter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.mcp_cron import _call_tool


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """Every test in this module exercises cron field handling, not authorization.

    ``mcp_cron`` refuses a write from a caller it cannot name, so this states the
    precondition these tests always assumed. See the ``named_cron_caller``
    fixture in ``test/conftest.py``.
    """


class TestCronAddChannel:
    def test_add_with_channel(self, tmp_path: Path) -> None:
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc_cls:
            mock_svc = mock_svc_cls.return_value
            mock_job = type(
                "Job",
                (),
                {
                    "id": "abc",
                    "name": "test",
                    "schedule": type(
                        "S",
                        (),
                        {"kind": "every", "every_secs": 300, "cron_expr": None, "at_ts": None},
                    )(),
                },
            )()
            mock_svc.add_job.return_value = mock_job
            result = _call_tool(
                "cron_add",
                {"name": "ops", "message": "check", "every": 300, "channel": "C0AP77JJSN6"},
            )
            mock_svc.add_job.assert_called_once()
            call_kwargs = mock_svc.add_job.call_args
            assert (
                call_kwargs.kwargs.get("channel") == "C0AP77JJSN6"
                or call_kwargs[1].get("channel") == "C0AP77JJSN6"
            )
            assert "abc" in result

    def test_add_without_channel(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc_cls:
            mock_svc = mock_svc_cls.return_value
            mock_job = type(
                "Job",
                (),
                {
                    "id": "def",
                    "name": "test",
                    "schedule": type(
                        "S",
                        (),
                        {"kind": "every", "every_secs": 300, "cron_expr": None, "at_ts": None},
                    )(),
                    "agent_id": "",
                },
            )()
            mock_svc.add_job.return_value = mock_job
            result = _call_tool("cron_add", {"name": "ops", "message": "check", "every": 300})
            call_kwargs = mock_svc.add_job.call_args
            assert (
                call_kwargs.kwargs.get("channel") is None or call_kwargs[1].get("channel") is None
            )
            assert "def" in result
