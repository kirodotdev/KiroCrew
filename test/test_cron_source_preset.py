"""Template-provenance fields on CronJob.

Two create-only fields record where a job was seeded from:
  source_preset          -- the Schedule-page template id (e.g. "error-digest").
  source_template_prompt -- the template's prompt text as it was at save time.

The snapshot makes the "template updated" hint attributable: the Schedule page
compares the SNAPSHOT against the template's current prompt (did the template
move?), not the job's live message (which the user may have edited). Both are
dashboard-only (only that create path stamps them), create-only (provenance is
fixed at creation), and never gate execution.

The "" default is load-bearing: a crons.json written without these fields must
still deserialize and carry "" (no hint). That is pinned here.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.cron import CronJob, CronService

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


class TestFields:
    """Dataclass fields and serialization round-trip.

    Jobs are built directly here (via the constructor + _save/_load) because
    template provenance travels the async dashboard create path, not the sync
    add_job."""

    def test_defaults_are_empty(self):
        job = CronJob(id="j1", name="x", message="y")
        assert job.source_preset == ""
        assert job.source_template_prompt == ""

    def test_fields_roundtrip_through_save_load(self, tmp_path):
        svc = CronService()
        job = svc.add_job(name="Error Digest", message="edited by me", every_secs=21600)
        job.source_preset = "error-digest"
        job.source_template_prompt = "Original template prompt."
        svc._save()

        svc2 = CronService()
        loaded = [j for j in svc2.list_jobs() if j.id == job.id][0]
        assert loaded.source_preset == "error-digest"
        assert loaded.source_template_prompt == "Original template prompt."

    def test_serialized_dict_includes_both_fields(self, tmp_path):
        svc = CronService()
        job = svc.add_job(name="d", message="m", every_secs=600)
        job.source_preset = "standup-brief"
        job.source_template_prompt = "Give me a standup brief."
        svc._save()

        raw = json.loads((tmp_path / "crons.json").read_text(encoding="utf-8"))
        entry = [j for j in raw["jobs"] if j["id"] == job.id][0]
        assert entry["source_preset"] == "standup-brief"
        assert entry["source_template_prompt"] == "Give me a standup brief."

    def test_legacy_job_without_fields_deserializes_and_defaults_empty(self, tmp_path):
        """A crons.json record with neither key loads (the record simply omits
        them) and reads "". This pins the load-bearing "" default: such a job
        stays valid and never shows the hint."""
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
        assert loaded.source_preset == ""
        assert loaded.source_template_prompt == ""

    @pytest.mark.parametrize("bad_value", [123, None, {"nested": "obj"}, ["a", "list"]])
    @pytest.mark.parametrize("field", ["source_preset", "source_template_prompt"])
    def test_non_string_provenance_degrades_to_empty(self, tmp_path, field, bad_value):
        """crons.json is hand-editable; a non-string provenance value must
        degrade to "" on load rather than flow into the redacting serializer,
        which calls string methods on it and would 500 the WHOLE listing (the
        anchor is crash/availability, not one cosmetic row). A default handles a
        MISSING key; this pins the neighbouring case -- key present, wrong type
        -- for each bad shape and each field independently."""
        path = tmp_path / "crons.json"
        record = {
            "id": "bad1",
            "name": "x",
            "message": "y",
            "schedule": {"kind": "every", "every_secs": 300},
            field: bad_value,
        }
        path.write_text(json.dumps({"version": 2, "jobs": [record]}))

        svc = CronService()
        loaded = [j for j in svc.list_jobs() if j.id == "bad1"][0]
        # Both fields end up "" regardless: the wrong-typed one is normalized,
        # the other was never set.
        assert loaded.source_preset == ""
        assert loaded.source_template_prompt == ""


class TestAddJobAsync:
    """The dashboard create path (add_job_async) stamps both fields, fully-formed
    on the first save."""

    async def test_async_create_stamps_both_and_persists(self, tmp_path):
        svc = CronService()
        job = await svc.add_job_async(
            "Error Digest",
            "Summarize errors.",
            every_secs=21600,
            source_preset="error-digest",
            source_template_prompt="Summarize errors.",
        )
        assert job.source_preset == "error-digest"
        assert job.source_template_prompt == "Summarize errors."

        svc2 = CronService()
        loaded = [j for j in svc2.list_jobs() if j.id == job.id][0]
        assert loaded.source_preset == "error-digest"
        assert loaded.source_template_prompt == "Summarize errors."

    async def test_async_create_without_preset_leaves_both_empty(self, tmp_path):
        svc = CronService()
        job = await svc.add_job_async("hand made", "do a thing", every_secs=3600)
        assert job.source_preset == ""
        assert job.source_template_prompt == ""


class TestNotUpdatable:
    def test_provenance_is_not_updatable(self, tmp_path):
        """Fixed at creation: update_job validates the fields (they are in the
        caps table) but never ASSIGNS them, mirroring created_by."""
        svc = CronService()
        job = svc.add_job(name="d", message="m", every_secs=600)
        job.source_preset = "error-digest"
        job.source_template_prompt = "Original."
        svc._save()

        svc.update_job(job.id, source_preset="standup-brief", source_template_prompt="Changed.")

        svc2 = CronService()
        loaded = [j for j in svc2.list_jobs() if j.id == job.id][0]
        assert loaded.source_preset == "error-digest"
        assert loaded.source_template_prompt == "Original."
