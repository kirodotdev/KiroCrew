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


class TestCronAddModel:
    """Test per-job model override on cron_add and cron_update."""

    def test_cron_add_with_valid_model(self, monkeypatch, tmp_path):
        """A recognized model is stored on the job."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"model-valid-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "go", "every": 120, "model": "sonnet"},
        )
        assert "Added job" in result

        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        matching = [j for j in svc.list_jobs() if j.name == job_name]
        assert len(matching) == 1
        assert matching[0].model != ""

    def test_cron_add_with_empty_model(self, monkeypatch, tmp_path):
        """Empty model string means inherit (no override stored)."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"model-empty-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "go", "every": 120, "model": ""},
        )
        assert "Added job" in result

        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        matching = [j for j in svc.list_jobs() if j.name == job_name]
        assert len(matching) == 1
        assert matching[0].model == ""

    def test_cron_add_with_unknown_model_rejected(self, monkeypatch, tmp_path):
        """An unrecognized model is rejected with an error message."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"model-bad-{uuid.uuid4().hex[:8]}"
        result = _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "go", "every": 120, "model": "nonexistent-xyz"},
        )
        assert "Error" in result or "unknown model" in result

        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        matching = [j for j in svc.list_jobs() if j.name == job_name]
        assert len(matching) == 0

    def test_cron_update_model(self, monkeypatch, tmp_path):
        """cron_update with a valid model stores it on the job."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"model-upd-{uuid.uuid4().hex[:8]}"
        _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "go", "every": 120},
        )
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        job = next(j for j in svc.list_jobs() if j.name == job_name)

        result = _call_tool_inner(
            "cron_update",
            {"job_id": job.id, "model": "sonnet"},
        )
        assert "Updated" in result or "updated" in result.lower()

        svc2 = CronService(base_dir=tmp_path)
        updated = next(j for j in svc2.list_jobs() if j.id == job.id)
        assert updated.model != ""

    def test_cron_update_model_clear(self, monkeypatch, tmp_path):
        """cron_update with model='' clears the override."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"model-clr-{uuid.uuid4().hex[:8]}"
        _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "go", "every": 120, "model": "sonnet"},
        )
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        job = next(j for j in svc.list_jobs() if j.name == job_name)

        result = _call_tool_inner(
            "cron_update",
            {"job_id": job.id, "model": ""},
        )
        assert "Updated" in result or "updated" in result.lower()

        svc2 = CronService(base_dir=tmp_path)
        updated = next(j for j in svc2.list_jobs() if j.id == job.id)
        assert updated.model == ""

    def test_cron_update_unknown_model_rejected(self, monkeypatch, tmp_path):
        """cron_update with an unrecognized model is rejected."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

        job_name = f"model-upd-bad-{uuid.uuid4().hex[:8]}"
        _call_tool_inner(
            "cron_add",
            {"name": job_name, "message": "go", "every": 120},
        )
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        job = next(j for j in svc.list_jobs() if j.name == job_name)

        result = _call_tool_inner(
            "cron_update",
            {"job_id": job.id, "model": "nonexistent-xyz"},
        )
        assert "Error" in result or "unknown model" in result

        svc2 = CronService(base_dir=tmp_path)
        unchanged = next(j for j in svc2.list_jobs() if j.id == job.id)
        assert unchanged.model == ""
