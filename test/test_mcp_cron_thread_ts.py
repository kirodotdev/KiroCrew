"""Tests for mcp_cron thread_ts parameter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.cron import CronSchedule
from kiro_crew.mcp_cron import _call_tool
from kiro_crew.validation import (
    CRON_ADD_SCHEMA,
    MCP_CRON_SCHEMAS,
    ValidationError,
    validate_tool_args,
)


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """Every test in this module exercises cron field handling, not authorization.

    ``mcp_cron`` refuses a write from a caller it cannot name, so this states the
    precondition these tests always assumed. See the ``named_cron_caller``
    fixture in ``test/conftest.py``.
    """


class TestCronAddThreadTs:
    def test_add_with_thread_ts(self, tmp_path: Path) -> None:
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
                {
                    "name": "ops",
                    "message": "check",
                    "every": 300,
                    "channel": "C0AP77JJSN6",
                    "thread_ts": "1776298241.408339",
                },
            )
            call_kwargs = mock_svc.add_job.call_args
            assert (
                call_kwargs.kwargs.get("thread_ts") == "1776298241.408339"
                or call_kwargs[1].get("thread_ts") == "1776298241.408339"
            )
            assert "abc" in result

    def test_add_without_thread_ts(self, tmp_path: Path) -> None:
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
                },
            )()
            mock_svc.add_job.return_value = mock_job
            result = _call_tool(
                "cron_add",
                {"name": "ops", "message": "check", "every": 300},
            )
            call_kwargs = mock_svc.add_job.call_args
            assert (
                call_kwargs.kwargs.get("thread_ts") is None
                or call_kwargs[1].get("thread_ts") is None
            )
            assert "def" in result


class TestCronUpdateThreadTs:
    def test_update_sets_thread_ts(self, tmp_path: Path, named_cron_caller: str) -> None:
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc_cls:
            mock_svc = mock_svc_cls.return_value
            fake_job = MagicMock()
            fake_job.id = "abc"
            fake_job.name = "test-job"
            fake_job.schedule = CronSchedule(kind="every", every_secs=300)
            # The ownership gate now reaches the stored row, so the mock has to
            # model an owner. A bare MagicMock attribute compares unequal to the
            # caller's key and the update would be refused.
            fake_job.session_key = named_cron_caller
            mock_svc.get_job.return_value = fake_job
            mock_svc.update_job.return_value = fake_job
            result = _call_tool(
                "cron_update",
                {"job_id": "abc", "thread_ts": "1776298241.408339"},
            )
            call_kwargs = mock_svc.update_job.call_args
            assert (
                call_kwargs.kwargs.get("thread_ts") == "1776298241.408339"
                or call_kwargs[1].get("thread_ts") == "1776298241.408339"
            )
            assert "Updated" in result


class TestThreadTsValidation:
    """Schema rejects invalid thread_ts formats."""

    def test_valid_thread_ts_accepted(self) -> None:
        args = {"name": "j", "message": "go", "every": 300, "thread_ts": "1776298241.408339"}
        result = validate_tool_args(args, CRON_ADD_SCHEMA)
        assert result["thread_ts"] == "1776298241.408339"

    def test_invalid_thread_ts_rejected(self) -> None:
        args = {"name": "j", "message": "go", "every": 300, "thread_ts": "not-a-timestamp"}
        with pytest.raises(ValidationError, match="thread_ts"):
            validate_tool_args(args, CRON_ADD_SCHEMA)

    def test_empty_thread_ts_passes(self) -> None:
        args = {"name": "j", "message": "go", "every": 300}
        result = validate_tool_args(args, CRON_ADD_SCHEMA)
        assert "thread_ts" not in result

    def test_cron_update_thread_ts_validated(self) -> None:
        schema = MCP_CRON_SCHEMAS["cron_update"]
        args = {"job_id": "abc123", "thread_ts": "bad"}
        with pytest.raises(ValidationError, match="thread_ts"):
            validate_tool_args(args, schema)

    def test_cron_update_valid_thread_ts(self) -> None:
        schema = MCP_CRON_SCHEMAS["cron_update"]
        args = {"job_id": "abc123", "thread_ts": "1776298241.408339"}
        result = validate_tool_args(args, schema)
        assert result["thread_ts"] == "1776298241.408339"
