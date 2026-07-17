"""Tests for mcp_cron channel auto-capture."""

from __future__ import annotations

import uuid

from kiro_crew.mcp_cron import _call_tool_inner


class TestCronAddChannelCapture:
    def test_cron_add_captures_channel_from_env(self, monkeypatch, tmp_path):
        """KIROCREW_CHANNEL_ID env var is used as job channel."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setenv("KIROCREW_CHANNEL_ID", "C0ABC123")

        job_name = f"test-job-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "hello", "every": 120},
        )
        assert "Added job" in result

        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        jobs = svc.list_jobs()
        matching = [j for j in jobs if j.name == job_name]
        assert len(matching) == 1
        assert matching[0].channel == "C0ABC123"

    def test_cron_add_no_env_channel_is_none(self, monkeypatch, tmp_path):
        """Without KIROCREW_CHANNEL_ID, job channel is None (DM fallback)."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"test-no-channel-{uuid.uuid4().hex[:8]}"
        _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "hello", "every": 120},
        )

        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        jobs = svc.list_jobs()
        matching = [j for j in jobs if j.name == job_name]
        assert len(matching) == 1
        assert matching[0].channel is None

    def test_cron_respects_kirocrew_home(self, monkeypatch, tmp_path):
        """CronService uses KIROCREW_HOME when set, not the default ~/.kirocrew."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"test-home-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "hello", "every": 120},
        )
        assert "Added job" in result

        # Job should be in tmp_path, not ~/.kirocrew
        crons_file = tmp_path / "crons.json"
        assert crons_file.exists(), "crons.json not written to KIROCREW_HOME directory"

        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        jobs = svc.list_jobs()
        assert any(j.name == job_name for j in jobs)
