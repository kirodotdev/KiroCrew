"""Tests for minimal_context flag on CronJob.

When minimal_context=True:
- build_session_context returns only date/time + agent identity (~200 tokens)
- build_cron_session_context caps last_result to 2000 chars
- Field round-trips through serialization

Saves ~30-50k tokens per cron run for simple polling jobs.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.cron import CronJob, CronService, build_cron_session_context


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


class TestCronJobMinimalContextField:
    """Dataclass field + serialization round-trip."""

    def test_default_is_false(self):
        job = CronJob(id="j1", name="x", message="y")
        assert job.minimal_context is False

    def test_field_roundtrips_through_save_load(self, tmp_path):
        svc = CronService()
        job = svc.add_job(name="poll", message="check", every_secs=120)
        job.minimal_context = True
        svc._save()

        svc2 = CronService()
        loaded = [j for j in svc2.list_jobs() if j.id == job.id][0]
        assert loaded.minimal_context is True

    def test_legacy_job_without_field_defaults_to_false(self, tmp_path):
        path = tmp_path / "crons.json"
        legacy = {
            "version": 2,
            "jobs": [
                {
                    "id": "legacy1",
                    "name": "old",
                    "message": "hello",
                    "schedule": {"kind": "every", "every_secs": 300},
                    "created_ts": 1_700_000_000.0,
                }
            ],
        }
        path.write_text(json.dumps(legacy))

        svc = CronService()
        loaded = [j for j in svc.list_jobs() if j.id == "legacy1"][0]
        assert loaded.minimal_context is False


class TestBuildCronSessionContextMinimal:
    """last_result capping behavior when minimal_context=True."""

    def test_short_last_result_not_truncated(self):
        job = CronJob(
            id="j1",
            name="x",
            message="do work",
            persistent_session=True,
            minimal_context=True,
            last_result="short output",
        )
        _key, prompt = build_cron_session_context(job)
        assert "short output" in prompt
        assert "[truncated]" not in prompt

    def test_long_last_result_capped_at_2000(self):
        long_result = "x" * 5000
        job = CronJob(
            id="j1",
            name="x",
            message="do work",
            persistent_session=True,
            minimal_context=True,
            last_result=long_result,
        )
        _key, prompt = build_cron_session_context(job)
        assert "x" * 2000 in prompt
        assert "x" * 2001 not in prompt
        assert "[truncated]…" + "x" * 2000 in prompt

    def test_without_minimal_context_long_result_not_capped(self):
        long_result = "x" * 5000
        job = CronJob(
            id="j1",
            name="x",
            message="do work",
            persistent_session=True,
            minimal_context=False,
            last_result=long_result,
        )
        _key, prompt = build_cron_session_context(job)
        assert long_result in prompt
        assert "[truncated]" not in prompt

    def test_no_last_result_works_fine(self):
        job = CronJob(
            id="j1",
            name="x",
            message="do work",
            persistent_session=True,
            minimal_context=True,
            last_result=None,
        )
        _key, prompt = build_cron_session_context(job)
        assert prompt == "do work"


class TestBuildSessionContextMinimal:
    """build_session_context with minimal_context=True returns minimal output."""

    def test_minimal_context_has_date(self, tmp_path):
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        ctx = builder.build_session_context(
            session_key="cron:test123",
            minimal_context=True,
        )
        assert "[CURRENT DATE]" in ctx
        assert "[CURRENT AGENT]" in ctx

    def test_minimal_context_agent_without_session_key(self, tmp_path):
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        ctx = builder.build_session_context(
            session_key=None,
            minimal_context=True,
        )
        assert "[CURRENT AGENT] kirocrew" in ctx
        assert "[RUNTIME]" not in ctx

    def test_minimal_context_skips_memory(self, tmp_path):
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        ctx = builder.build_session_context(
            session_key="cron:test123",
            minimal_context=True,
        )
        assert "[WORKSPACE IDENTITY]" not in ctx
        assert "User Preferences" not in ctx
        assert "Learned corrections" not in ctx

    def test_minimal_context_is_short(self, tmp_path):
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        ctx = builder.build_session_context(
            session_key="cron:test123",
            minimal_context=True,
        )
        assert len(ctx) < 500

    def test_full_context_is_longer_than_minimal(self, tmp_path):
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        minimal = builder.build_session_context(
            session_key="cron:test123",
            minimal_context=True,
        )
        full = builder.build_session_context(
            session_key="cron:test123",
            minimal_context=False,
        )
        assert len(full) > len(minimal)
