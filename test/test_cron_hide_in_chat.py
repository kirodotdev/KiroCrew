"""Tests for the hide_in_chat flag on CronJob.

When hide_in_chat=True, an agent cron's runs are NOT injected into a dashboard
chat slot, so they stay out of the active session list. The result still reaches
Slack/dashboard notifications, and the run remains visible in the History tab via
the cron execution-history store (CronHistoryStore / get_history(), surfaced at
GET /api/crons/{id}/history) which the executor writes unconditionally. (The
cron:{id} conversation_log is written only by inject_cron_result_to_dashboard,
gated off here, so it is intentionally empty for a hidden cron.)

Default is False (preserves legacy behaviour; absent field reads as False, i.e.
the cron shows in chat as before). Only agent crons (LLM jobs with a message)
ever create a slot — script/command crons never do, so the flag is a no-op for
them.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.cron import CronJob, CronService


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


class TestCronJobHideInChatField:
    """Dataclass field + serialization round-trip."""

    def test_default_is_false(self):
        job = CronJob(id="j1", name="x", message="y")
        assert job.hide_in_chat is False

    def test_field_roundtrips_through_save_load(self, tmp_path):
        svc = CronService()
        job = svc.add_job(name="digest", message="summarize", every_secs=86400)
        job.hide_in_chat = True
        svc._save()

        svc2 = CronService()
        loaded = [j for j in svc2.list_jobs() if j.id == job.id][0]
        assert loaded.hide_in_chat is True

    def test_false_roundtrips_through_save_load(self, tmp_path):
        svc = CronService()
        job = svc.add_job(name="chatty", message="talk", every_secs=3600)
        job.hide_in_chat = False
        svc._save()

        svc2 = CronService()
        loaded = [j for j in svc2.list_jobs() if j.id == job.id][0]
        assert loaded.hide_in_chat is False

    def test_legacy_job_without_field_defaults_to_false(self, tmp_path):
        """A crons.json predating the flag must read as hide_in_chat=False
        (no behaviour change for existing jobs — they still show in chat)."""
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
        assert loaded.hide_in_chat is False

    def test_serialized_dict_includes_field(self, tmp_path):
        svc = CronService()
        job = svc.add_job(name="d", message="m", every_secs=600)
        job.hide_in_chat = True
        svc._save()

        raw = json.loads((tmp_path / "crons.json").read_text(encoding="utf-8"))
        entry = [j for j in raw["jobs"] if j["id"] == job.id][0]
        assert entry["hide_in_chat"] is True


class TestCronServiceUpdateHideInChat:
    """update_job applies hide_in_chat."""

    def test_update_sets_true(self, tmp_path):
        svc = CronService()
        job = svc.add_job(name="d", message="m", every_secs=600)
        assert job.hide_in_chat is False

        updated = svc.update_job(job.id, hide_in_chat=True)
        assert updated is not None
        assert updated.hide_in_chat is True

        # Persisted
        svc2 = CronService()
        loaded = [j for j in svc2.list_jobs() if j.id == job.id][0]
        assert loaded.hide_in_chat is True

    def test_update_back_to_false(self, tmp_path):
        svc = CronService()
        job = svc.add_job(name="d", message="m", every_secs=600)
        svc.update_job(job.id, hide_in_chat=True)
        updated = svc.update_job(job.id, hide_in_chat=False)
        assert updated is not None
        assert updated.hide_in_chat is False
