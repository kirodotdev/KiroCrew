"""Tests for mcp_cron thread_ts parameter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.cron import CronSchedule, CronService
from kiro_crew.mcp_cron import _call_tool_inner, _call_tool_locally
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
                    "timezone": "",
                    "schedule": type(
                        "S",
                        (),
                        {"kind": "every", "every_secs": 300, "cron_expr": None, "at_ts": None},
                    )(),
                },
            )()
            mock_svc.add_job.return_value = mock_job
            result = _call_tool_locally(
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
                    "timezone": "",
                    "schedule": type(
                        "S",
                        (),
                        {"kind": "every", "every_secs": 300, "cron_expr": None, "at_ts": None},
                    )(),
                },
            )()
            mock_svc.add_job.return_value = mock_job
            result = _call_tool_locally(
                "cron_add",
                {"name": "ops", "message": "check", "every": 300},
            )
            call_kwargs = mock_svc.add_job.call_args
            assert (
                call_kwargs.kwargs.get("thread_ts") is None
                or call_kwargs[1].get("thread_ts") is None
            )
            assert "def" in result


class TestCronAddInheritsCallerThread:
    """A cron scheduled by an agent running IN a Slack thread inherits that
    thread automatically, with no explicit thread_ts.

    The caller's strict session key is ``slack:<thread_ts>`` (canonical_key of
    the bare reply_ts), so cron_add can recover the thread from the caller's own
    identity — no gateway/caller-schema change. Gated on a channel being present,
    since a thread_ts is meaningless without its channel.
    """

    @staticmethod
    def _mock_job():
        return type(
            "Job",
            (),
            {
                "id": "thr",
                "name": "test",
                "timezone": "",
                "schedule": type(
                    "S",
                    (),
                    {"kind": "every", "every_secs": 300, "cron_expr": None, "at_ts": None},
                )(),
                "agent_id": "",
            },
        )()

    def test_slack_caller_thread_is_inherited(self, tmp_path: Path, monkeypatch) -> None:
        # Caller runs in a Slack thread: session key is slack:<thread_ts>.
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "slack:1789696119.009399")
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc_cls:
            mock_svc = mock_svc_cls.return_value
            mock_svc.add_job.return_value = self._mock_job()
            _call_tool_locally(
                "cron_add",
                {
                    "name": "ops",
                    "message": "check",
                    "every": 300,
                    "channel": "C0AP77JJSN6",
                    # no explicit thread_ts
                },
            )
            kw = mock_svc.add_job.call_args.kwargs
            assert kw.get("thread_ts") == "1789696119.009399", "caller thread not inherited"

    def test_explicit_thread_ts_wins_over_caller(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "slack:1789696119.009399")
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc_cls:
            mock_svc = mock_svc_cls.return_value
            mock_svc.add_job.return_value = self._mock_job()
            _call_tool_locally(
                "cron_add",
                {
                    "name": "ops",
                    "message": "check",
                    "every": 300,
                    "channel": "C0AP77JJSN6",
                    "thread_ts": "1776298241.408339",
                },
            )
            kw = mock_svc.add_job.call_args.kwargs
            assert kw.get("thread_ts") == "1776298241.408339", "explicit thread_ts overridden"

    def test_non_slack_caller_gets_no_thread(self, tmp_path: Path, monkeypatch) -> None:
        # Dashboard caller: no thread to inherit.
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:conftest-slot")
        with patch("kiro_crew.mcp_cron.CronService") as mock_svc_cls:
            mock_svc = mock_svc_cls.return_value
            mock_svc.add_job.return_value = self._mock_job()
            _call_tool_locally(
                "cron_add",
                {"name": "ops", "message": "check", "every": 300, "channel": "C0AP77JJSN6"},
            )
            kw = mock_svc.add_job.call_args.kwargs
            assert kw.get("thread_ts") is None, "non-Slack caller must not inherit a thread"

    def test_no_channel_no_thread_inherit(self, tmp_path: Path, monkeypatch) -> None:
        # Even a Slack caller: without a channel a thread_ts is meaningless.
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "slack:1789696119.009399")
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        with patch("kiro_crew.mcp_cron._caller_channel_id", return_value=""):
            with patch("kiro_crew.mcp_cron.CronService") as mock_svc_cls:
                mock_svc = mock_svc_cls.return_value
                mock_svc.add_job.return_value = self._mock_job()
                _call_tool_locally("cron_add", {"name": "ops", "message": "check", "every": 300})
                kw = mock_svc.add_job.call_args.kwargs
                assert kw.get("thread_ts") is None
                assert kw.get("channel") is None


class TestCronUpdateThreadTs:
    """Forwarding-only coverage: ``CronService`` is a ``MagicMock`` here.

    These tests pin that ``cron_update`` builds the ``thread_ts`` kwarg, and
    nothing more — the real ``update_job`` never runs, so they cannot observe
    whether the value is applied or silently dropped on the next line. The
    persistence half is pinned by
    :class:`TestCronUpdateThreadTsPersistence` and
    :class:`TestCronThreadTsEndToEnd`, which drive a real service and read the
    value back off disk.
    """

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
            result = _call_tool_locally(
                "cron_update",
                {"job_id": "abc", "thread_ts": "1776298241.408339"},
            )
            call_kwargs = mock_svc.update_job.call_args
            assert (
                call_kwargs.kwargs.get("thread_ts") == "1776298241.408339"
                or call_kwargs[1].get("thread_ts") == "1776298241.408339"
            )
            assert "Updated" in result


class TestCronUpdateThreadTsPersistence:
    """``update_job`` must APPLY thread_ts, not just validate it.

    ``add_job`` accepted the field from the start, so a job's reply thread was
    settable at creation and then frozen: ``_update_job_locked`` length-checked
    the value and fell through to ``_save()`` without ever assigning it, so the
    caller was told "Updated" while the cron kept posting to the old thread.
    The in-memory object is not the contract — every assertion here reloads the
    store.
    """

    def test_update_job_applies_thread_ts(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        assert job.thread_ts is None

        updated = svc.update_job(job.id, thread_ts="1776298241.408339")
        assert updated is not None
        assert updated.thread_ts == "1776298241.408339"
        # Reload: the value has to be on disk, not only on the live object.
        assert CronService(base_dir=tmp_path).list_jobs()[0].thread_ts == "1776298241.408339"

    def test_update_job_clears_thread_ts_with_empty_string(self, tmp_path: Path) -> None:
        """Blank clears the thread, matching how ``mcp_cron`` normalizes it."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600, thread_ts="1776298241.408339")

        updated = svc.update_job(job.id, thread_ts="")
        assert updated is not None
        assert updated.thread_ts is None
        assert CronService(base_dir=tmp_path).list_jobs()[0].thread_ts is None

    def test_update_job_without_thread_ts_leaves_it_alone(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600, thread_ts="1776298241.408339")

        updated = svc.update_job(job.id, name="renamed")
        assert updated is not None
        assert updated.thread_ts == "1776298241.408339"
        assert CronService(base_dir=tmp_path).list_jobs()[0].thread_ts == "1776298241.408339"


class TestCronThreadTsEndToEnd:
    """The same guarantee through the real MCP tool, deliberately unmocked."""

    def test_cron_update_persists_thread_ts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(
            name="e2e", message="m", every_secs=3600, session_key="dashboard:conftest-slot"
        )

        result = _call_tool_inner(
            "cron_update", {"job_id": job.id, "thread_ts": "1776298241.408339"}
        )
        assert "Updated" in result, result
        assert CronService(base_dir=tmp_path).list_jobs()[0].thread_ts == "1776298241.408339"

    def test_cron_update_clears_thread_ts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(
            name="e2e-clear",
            message="m",
            every_secs=3600,
            thread_ts="1776298241.408339",
            session_key="dashboard:conftest-slot",
        )

        result = _call_tool_inner("cron_update", {"job_id": job.id, "thread_ts": ""})
        assert "Updated" in result, result
        assert CronService(base_dir=tmp_path).list_jobs()[0].thread_ts is None


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
