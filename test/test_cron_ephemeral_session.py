"""Tests for persistent_session flag on CronJob.

Bug: Mesh-1026 — stateful cron sessions accumulate context indefinitely for
polling-style jobs, causing LLM turns to drift from seconds to tens of minutes
over days. Adding ``persistent_session=False`` gives each run a fresh session
key and skips the ``last_result`` prefix.

This file covers:
- Dataclass field round-trip (save → load)
- Default is True (backward-compat for all existing jobs)
- Session key + prompt builder helper:
    - persistent_session=True  → key = f"cron:{job.id}"           (stable)
                              → prompt contains last_result prefix
    - persistent_session=False → key = f"cron:{job.id}:<run_id>"  (fresh)
                              → prompt does NOT contain last_result prefix
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.cron import CronJob, CronService


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


class TestCronJobPersistentSessionField:
    """Dataclass field + serialization round-trip."""

    def test_default_is_true(self):
        """New CronJob defaults to persistent_session=True (backward-compat)."""
        job = CronJob(id="j1", name="x", message="y")
        assert job.persistent_session is True

    def test_field_roundtrips_through_save_load(self, tmp_path):
        """Save a job with persistent_session=False → reload → still False."""
        svc = CronService()
        job = svc.add_job(name="poll", message="check", every_secs=120)
        job.persistent_session = False
        svc._save()

        # Force reload from disk
        svc2 = CronService()
        loaded = [j for j in svc2.list_jobs() if j.id == job.id][0]
        assert loaded.persistent_session is False

    def test_legacy_job_without_field_defaults_to_true(self, tmp_path):
        """A crons.json written before this feature must load as persistent=True."""
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
                    # Note: no "persistent_session" key
                }
            ],
        }
        path.write_text(json.dumps(legacy))

        svc = CronService()
        loaded = [j for j in svc.list_jobs() if j.id == "legacy1"][0]
        assert loaded.persistent_session is True


class TestBuildCronSessionContext:
    """Helper that computes session_key + prompt for a given run.

    This helper is what ``_cron_callback`` in slack/gateway.py will call.
    Extracting it lets us unit-test the decision without spinning up the gateway.
    """

    def test_persistent_uses_stable_key(self):
        """persistent_session=True → key == f'cron:{job.id}' (unchanged)."""
        from kiro_crew.cron import build_cron_session_context

        job = CronJob(id="abc123", name="x", message="hi", persistent_session=True)
        key, _prompt = build_cron_session_context(job)
        assert key == "cron:abc123"

    def test_persistent_prompt_prepends_last_result(self):
        """persistent_session=True → prompt contains the previous run's result."""
        from kiro_crew.cron import build_cron_session_context

        job = CronJob(
            id="abc123",
            name="x",
            message="do work",
            persistent_session=True,
            last_result="PREVIOUS OUTPUT HERE",
        )
        _key, prompt = build_cron_session_context(job)
        assert "PREVIOUS OUTPUT HERE" in prompt
        assert "do work" in prompt

    def test_stateless_uses_fresh_key_per_call(self):
        """persistent_session=False → two calls return two different keys."""
        from kiro_crew.cron import build_cron_session_context

        job = CronJob(id="abc123", name="x", message="hi", persistent_session=False)
        key1, _ = build_cron_session_context(job)
        key2, _ = build_cron_session_context(job)
        assert key1 != key2

    def test_stateless_key_prefix_is_job_id(self):
        """Ephemeral key must still start with 'cron:{job.id}:' for reaper matching."""
        from kiro_crew.cron import build_cron_session_context

        job = CronJob(id="abc123", name="x", message="hi", persistent_session=False)
        key, _ = build_cron_session_context(job)
        assert key.startswith("cron:abc123:")
        # Suffix is non-empty
        assert len(key) > len("cron:abc123:")

    def test_stateless_prompt_skips_last_result(self):
        """persistent_session=False → prompt does NOT contain last_result prefix.

        This is the second half of the fix: even if a stateless job
        accidentally had last_result from a previous version's persisted state,
        the new code must never inject it.
        """
        from kiro_crew.cron import build_cron_session_context

        job = CronJob(
            id="abc123",
            name="x",
            message="do work",
            persistent_session=False,
            last_result="STALE OUTPUT MUST NOT APPEAR",
        )
        _key, prompt = build_cron_session_context(job)
        assert "STALE OUTPUT MUST NOT APPEAR" not in prompt
        assert "do work" in prompt

    def test_stateless_prompt_equals_bare_message(self):
        """When persistent_session=False and no acks, prompt is exactly job.message."""
        from kiro_crew.cron import build_cron_session_context

        job = CronJob(
            id="abc123",
            name="x",
            message="do work",
            persistent_session=False,
            last_result=None,
        )
        _key, prompt = build_cron_session_context(job)
        assert prompt == "do work"
