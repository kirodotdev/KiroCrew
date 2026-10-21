"""A changed ``app.json`` cron entry reaches a job that is already registered.

``register_app_crons_with_service`` was add-if-absent: a job whose name was already
in the scheduler was skipped, so an author's change to a cron field (``timeout``,
``message``, ``timezone``, ...) reached a fresh install and never a machine that
already had the app, short of a disable/enable cycle that deletes and recreates
the job.

The fix stamps each app job with the digest of the manifest entry it was built
from (``CronJob.app_manifest_digest``) and makes registration compare it:

* digest equal     -- the job is left exactly as it is, hand edits included;
* digest different -- the manifest's fields are re-applied and the stamp renewed,
  while the job's pause state is kept;
* no digest        -- a job from before the stamp existed counts as changed once.

Fields ``CronService.update_job`` cannot change (``command``, ``script``,
``agent_sequence``, ``env``) are never applied this way: the change is logged and
audited and waits for a disable/enable of the app, while the job keeps its id,
its pause state and every setting the manifest does not own. Every scenario here
runs against a real
``CronService`` on an isolated home, so the stamp is exercised through the store
it is persisted in, not through a mock of it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

import kiro_crew.apps.execution as execution_mod
from kiro_crew import cron as seams
from kiro_crew.apps.bridges import (
    _register_crons,
    app_cron_manifest_digest,
    load_app_cron_defs,
    register_app_crons_with_service,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.cron import CronService

APP = "resync-app"
SHORT = "digest"
NAME = f"{APP}/{SHORT}"


def _manifest(cron: dict) -> AppManifest:
    """A manifest declaring one cron, with the ``permissions.cron`` grant that requires."""
    return AppManifest.from_dict(
        {
            "name": APP,
            "version": "1.0.0",
            "displayName": "Resync App",
            "description": "x",
            "author": "t",
            "permissions": {"cron": True},
            "crons": [cron],
        }
    )


def _agent_cron(**extra) -> dict:
    return {"name": SHORT, "every": 600, "message": "digest the inbox", **extra}


def _command_cron(**extra) -> dict:
    return {"name": SHORT, "every": 600, "command": "echo hello", **extra}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """An isolated crew home with one installed app bundle."""
    root = tmp_path / "kirocrew-home"
    bundle = root / "apps" / APP
    bundle.mkdir(parents=True)
    (root / "crons").mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    (bundle / "app.json").write_text(
        json.dumps(
            {
                "name": APP,
                "version": "1.0.0",
                "displayName": "Resync App",
                "description": "x",
                "author": "t",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.apps.bridges._registration_denied", lambda *a, **k: False)
    monkeypatch.setattr(execution_mod, "third_party_execution_allowed", lambda: True)
    return root


def _job(svc: CronService):
    """The app's one job. The LIVE store object: an in-place update mutates it,
    so a "before" snapshot is taken with :func:`_snapshot`, never by holding it."""
    jobs = [j for j in svc.list_jobs(include_disabled=True) if j.name == NAME]
    assert len(jobs) == 1, [j.name for j in svc.list_jobs(include_disabled=True)]
    return jobs[0]


def _snapshot(svc: CronService) -> dict:
    """Scalar copy of the fields the scenarios compare across a registration pass."""
    j = _job(svc)
    return {
        "id": j.id,
        "message": j.message,
        "timeout_secs": j.timeout_secs,
        "digest": j.app_manifest_digest,
        "enabled": j.enabled,
        "user_paused": j.user_paused,
        "command": j.command,
        "created_by": j.created_by,
    }


def _run_scenario(home: Path, scenario):
    """Drive ``scenario(svc)`` against a started service, stopping it afterwards.

    ``scenario`` is an async callable; its return value is the test's evidence.
    """

    async def _go():
        svc = CronService(base_dir=home / "crons")
        await svc.start()
        try:
            return await scenario(svc)
        finally:
            await svc.stop()

    return asyncio.run(_go())


class TestChangedManifestReachesTheJob:
    def test_changed_fields_are_applied_in_place(self, home):
        """The reported bug: a bigger ``timeout_secs`` shipped in app.json lands on the job."""

        async def scenario(svc):
            _register_crons(APP, _manifest(_agent_cron(timeout_secs=600)))
            await register_app_crons_with_service(APP, svc)
            before = _snapshot(svc)

            _register_crons(APP, _manifest(_agent_cron(message="new words", timeout_secs=900)))
            created = await register_app_crons_with_service(APP, svc)
            return before, _snapshot(svc), created

        before, after, created = _run_scenario(home, scenario)

        assert before["timeout_secs"] == 600
        assert (after["message"], after["timeout_secs"]) == ("new words", 900)
        assert after["id"] == before["id"], "an in-place update keeps the job's id"
        assert created == [], "a re-applied job is not reported as newly created"
        assert after["digest"] != before["digest"]
        assert after["digest"] != ""

    def test_schedule_and_calendar_fields_follow_the_manifest(self, home):
        async def scenario(svc):
            _register_crons(APP, _manifest(_agent_cron()))
            await register_app_crons_with_service(APP, svc)
            changed = {
                "name": SHORT,
                "cron_expr": "0 9 * * 1-5",
                "message": "digest the inbox",
                "timezone": "America/New_York",
                "skip_dates": ["2026-12-25"],
            }
            _register_crons(APP, _manifest(changed))
            await register_app_crons_with_service(APP, svc)
            return _job(svc)

        job = _run_scenario(home, scenario)

        assert (job.schedule.kind, job.schedule.cron_expr) == ("cron", "0 9 * * 1-5")
        assert job.timezone == "America/New_York"
        assert job.skip_dates == ["2026-12-25"]

    def test_a_changed_agent_lands_as_agent_id(self, home):
        """``agent`` is the one key spelled differently by the update gate."""

        async def scenario(svc):
            _register_crons(APP, _manifest(_agent_cron()))
            await register_app_crons_with_service(APP, svc)
            _register_crons(APP, _manifest(_agent_cron(agent="digest-agent")))
            await register_app_crons_with_service(APP, svc)
            return _job(svc)

        job = _run_scenario(home, scenario)

        assert job.agent_id == "digest-agent"

    def test_pause_state_survives_a_manifest_change(self, home):
        """The person's pause outranks the manifest: the job gets the new fields, still paused."""

        async def scenario(svc):
            _register_crons(APP, _manifest(_agent_cron()))
            await register_app_crons_with_service(APP, svc)
            job = _job(svc)
            assert await svc.enable_job_async(job.id, False)

            _register_crons(APP, _manifest(_agent_cron(message="new words")))
            await register_app_crons_with_service(APP, svc)
            return _job(svc)

        job = _run_scenario(home, scenario)

        assert job.message == "new words"
        assert job.enabled is False
        assert job.user_paused is True

    def test_a_job_without_a_stamp_counts_as_changed_once(self, home, caplog):
        """An app job from before the stamp existed is brought up to date on the first pass only,
        and the pass names each value it replaced so the person can put an edit back."""

        async def scenario(svc):
            _register_crons(APP, _manifest(_agent_cron(timeout_secs=900)))
            await register_app_crons_with_service(APP, svc)
            job = _job(svc)
            # A job persisted by a build that had no stamp: the field reads "".
            assert await svc.update_job_async(job.id, app_manifest_digest="", timeout_secs=600)
            assert _job(svc).app_manifest_digest == ""

            with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.bridges"):
                await register_app_crons_with_service(APP, svc)
            first = _snapshot(svc)
            # A hand edit after the stamp landed must survive the next pass.
            assert await svc.update_job_async(job.id, timezone="Europe/Paris")
            await register_app_crons_with_service(APP, svc)
            second = _job(svc)
            return first, second

        first, second = _run_scenario(home, scenario)

        assert first["timeout_secs"] == 900, "the unstamped job took the manifest once"
        assert first["digest"] != ""
        overwrite_lines = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "predates the manifest stamp" in r.getMessage()
        ]
        assert len(overwrite_lines) == 1, [r.getMessage() for r in caplog.records]
        assert "timeout_secs: 600 -> 900" in overwrite_lines[0]
        assert "message" not in overwrite_lines[0], "an unchanged value is not listed as replaced"
        assert second.timezone == "Europe/Paris", "the stamped job kept its hand edit"
        assert second.app_manifest_digest == first["digest"]


class TestOnlyTheChangedFieldsAreApplied:
    def test_a_users_edit_to_a_field_the_author_did_not_touch_stays(self, home):
        """User moves the schedule; author later changes only timeout_secs: the schedule stays."""

        async def scenario(svc):
            _register_crons(APP, _manifest(_agent_cron(timeout_secs=600)))
            await register_app_crons_with_service(APP, svc)
            job = _job(svc)
            assert await svc.update_job_async(job.id, cron_expr="0 7 * * *", message="mine")

            _register_crons(APP, _manifest(_agent_cron(timeout_secs=900)))
            await register_app_crons_with_service(APP, svc)
            return _job(svc)

        job = _run_scenario(home, scenario)

        assert job.timeout_secs == 900, "the field the author changed lands"
        assert (job.schedule.kind, job.schedule.cron_expr) == ("cron", "0 7 * * *")
        assert job.message == "mine", "fields the author did not touch keep the user's value"
        assert job.app_manifest_digest == app_cron_manifest_digest(load_app_cron_defs(APP)[0])

    def test_a_cleared_message_is_reported_not_stamped_as_applied(self, home, caplog):
        """The update gate keeps the stored prompt on an empty message, so the pass must not
        claim it applied one; the drift is reported on every pass instead."""

        async def scenario(svc):
            _register_crons(APP, _manifest(_agent_cron()))
            await register_app_crons_with_service(APP, svc)
            _register_crons(APP, _manifest({"name": SHORT, "every": 600, "message": ""}))
            with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.bridges"):
                await register_app_crons_with_service(APP, svc)
                await register_app_crons_with_service(APP, svc)
            return _job(svc)

        job = _run_scenario(home, scenario)

        assert job.message == "digest the inbox"
        assert job.app_manifest_digest != app_cron_manifest_digest(
            load_app_cron_defs(APP)[0]
        ), "the stamp must not say the empty message was applied"
        reports = [
            r
            for r in caplog.records
            if "message" in r.getMessage() and "disabled" in r.getMessage()
        ]
        assert len(reports) == 2, [r.getMessage() for r in caplog.records]


class TestUnchangedManifestLeavesTheJobAlone:
    def test_hand_edits_survive_a_registration_pass(self, home):
        """Same manifest, re-registered (gateway restart): the person's edits stay."""

        async def scenario(svc):
            _register_crons(APP, _manifest(_agent_cron()))
            await register_app_crons_with_service(APP, svc)
            job = _job(svc)
            assert await svc.update_job_async(
                job.id, message="my own words", timezone="Europe/Paris", timeout_secs=777
            )

            created = await register_app_crons_with_service(APP, svc)
            return _job(svc), created

        job, created = _run_scenario(home, scenario)

        assert (job.message, job.timezone, job.timeout_secs) == (
            "my own words",
            "Europe/Paris",
            777,
        )
        assert created == []

    def test_flipping_enabled_in_the_manifest_is_not_a_change(self, home):
        """``enabled`` is the initial pause state only; the manifest never resumes a job."""

        async def scenario(svc):
            _register_crons(APP, _manifest(_agent_cron(enabled=False)))
            await register_app_crons_with_service(APP, svc)
            paused = _snapshot(svc)
            assert paused["user_paused"] is True

            _register_crons(APP, _manifest(_agent_cron(enabled=True)))
            await register_app_crons_with_service(APP, svc)
            return paused, _job(svc)

        paused, after = _run_scenario(home, scenario)

        assert after.enabled is False
        assert after.user_paused is True
        assert after.app_manifest_digest == paused["digest"]


class TestCreateOnlyFieldsWaitForAReenable:
    """``command``/``script``/``agent_sequence``/``env`` have no update path.

    The job is never removed and recreated for them: a remove-then-add would
    lose the job when the create gate refuses the replacement, and would reset
    every setting the manifest does not own (delivery channel, model, a secret
    grant). The change is logged and audited instead, the other changed fields
    land, and the job keeps its id and pause state.
    """

    def test_a_changed_command_is_not_applied_and_the_job_survives(self, home, caplog):
        async def scenario(svc):
            _register_crons(APP, _manifest(_command_cron(timeout_secs=600)))
            await register_app_crons_with_service(APP, svc)
            assert await svc.enable_job_async(_job(svc).id, False)
            before = _snapshot(svc)

            _register_crons(APP, _manifest(_command_cron(command="echo world", timeout_secs=900)))
            with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.bridges"):
                created = await register_app_crons_with_service(APP, svc)
            return before, _job(svc), created

        before, after, created = _run_scenario(home, scenario)

        assert before["command"] == "echo hello"
        assert after.command == "echo hello", "a create-only key keeps its value"
        assert after.timeout_secs == 900, "the same change's other fields still land"
        assert after.id == before["id"], "the job is never removed and recreated"
        assert after.enabled is False
        assert after.user_paused is True
        assert after.app_manifest_digest != before["digest"]
        assert created == []
        assert any(
            "command" in rec.getMessage() and "disabled and enabled" in rec.getMessage()
            for rec in caplog.records
        ), [rec.getMessage() for rec in caplog.records]

    def test_a_changed_env_keeps_operator_settings_and_later_passes_keep_reporting(
        self, home, caplog
    ):
        """The pending drift is never stamped as applied: each later pass warns again,
        and -- the drift being all that differs -- writes nothing, so a hand edit stays."""

        async def scenario(svc):
            _register_crons(APP, _manifest(_command_cron(env={"MODE": "a"})))
            await register_app_crons_with_service(APP, svc)
            job = _job(svc)
            assert await svc.update_job_async(job.id, channel="C123", model="my-model")

            _register_crons(APP, _manifest(_command_cron(env={"MODE": "b"})))
            await register_app_crons_with_service(APP, svc)
            stamped = _job(svc).app_manifest_digest
            assert await svc.update_job_async(job.id, timezone="Europe/Paris")
            # Only the two passes below are counted; the first report, emitted by
            # the pass that stamped the drift, is not under test here.
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.bridges"):
                await register_app_crons_with_service(APP, svc)
                await register_app_crons_with_service(APP, svc)
            return _job(svc), stamped

        job, stamped = _run_scenario(home, scenario)

        assert job.env == {"MODE": "a"}
        assert (job.channel, job.model) == (
            "C123",
            "my-model",
        ), "settings the manifest does not own stay"
        assert job.timezone == "Europe/Paris", "a hand edit made while the drift stands survives"
        assert job.app_manifest_digest == stamped, "the stamp is not advanced to hide the drift"
        reports = [
            r for r in caplog.records if "env" in r.getMessage() and "disabled" in r.getMessage()
        ]
        assert len(reports) == 2, [r.getMessage() for r in caplog.records]

    def test_a_reenable_clears_the_pending_drift(self, home, caplog):
        """Disable/enable rebuilds the job from the manifest; the next pass is quiet."""

        async def scenario(svc):
            _register_crons(APP, _manifest(_command_cron()))
            await register_app_crons_with_service(APP, svc)
            _register_crons(APP, _manifest(_command_cron(command="echo world")))
            await register_app_crons_with_service(APP, svc)
            # What on_app_disable does to the app's jobs, then enable again.
            assert await svc.remove_job_async(_job(svc).id, actor="test", source="test")
            await register_app_crons_with_service(APP, svc)
            # The pass before the re-enable reported the drift; only the pass
            # after it must be quiet.
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.bridges"):
                await register_app_crons_with_service(APP, svc)
            return _job(svc)

        job = _run_scenario(home, scenario)

        assert job.command == "echo world"
        assert not [r for r in caplog.records if "disabled and enabled" in r.getMessage()]


class TestConcurrentRegistrars:
    def test_a_job_restamped_during_the_pass_is_left_alone(self, home, monkeypatch):
        """Fields computed against an old stamp never overwrite a job another registrar moved on.

        The race is reproduced at its seam: the locked update is entered with a
        stamp that does not match the record, as after a CLI enable applied the
        manifest -- and a person then edited the message -- between this pass's
        snapshot and its write.
        """

        async def scenario(svc):
            _register_crons(APP, _manifest(_agent_cron(timeout_secs=600)))
            await register_app_crons_with_service(APP, svc)
            job = _job(svc)
            _register_crons(APP, _manifest(_agent_cron(timeout_secs=900)))
            current = app_cron_manifest_digest(load_app_cron_defs(APP)[0])

            real_update = svc.update_job_async
            raced = {"done": False}

            async def _other_registrar_first(job_id, **kwargs):
                if not raced["done"]:
                    raced["done"] = True
                    # The other registrar applies the new manifest, then the person
                    # edits the prompt -- both before our locked write.
                    await real_update(job_id, timeout_secs=900, app_manifest_digest=current)
                    await real_update(job_id, message="my own words")
                return await real_update(job_id, **kwargs)

            monkeypatch.setattr(svc, "update_job_async", _other_registrar_first)
            await register_app_crons_with_service(APP, svc)
            return _job(svc), job.id

        job, job_id = _run_scenario(home, scenario)

        assert job.id == job_id
        assert job.timeout_secs == 900
        assert job.message == "my own words", "the stale pass wrote nothing over the edit"


class TestTheStampIsPersisted:
    def test_a_failed_save_leaves_the_stamp_unchanged_so_the_next_pass_retries(
        self, home, monkeypatch
    ):
        """The cache must not say "applied" when crons.json still holds the old values."""

        async def scenario(svc):
            _register_crons(APP, _manifest(_agent_cron(timeout_secs=600)))
            await register_app_crons_with_service(APP, svc)
            before = _snapshot(svc)

            _register_crons(APP, _manifest(_agent_cron(timeout_secs=900)))
            real_save = svc._save

            def _disk_full(*args, **kwargs):
                raise OSError(28, "No space left on device")

            monkeypatch.setattr(svc, "_save", _disk_full)
            await register_app_crons_with_service(APP, svc)  # logs the failure, raises nothing
            after_failure = _snapshot(svc)

            monkeypatch.setattr(svc, "_save", real_save)
            await register_app_crons_with_service(APP, svc)
            return before, after_failure, _snapshot(svc)

        before, after_failure, retried = _run_scenario(home, scenario)

        assert after_failure == before, "a failed save publishes nothing, stamp included"
        assert retried["timeout_secs"] == 900, "the next pass sees the old stamp and retries"
        assert retried["digest"] != before["digest"]

    def test_app_job_record_carries_it_and_a_persons_job_does_not(self, home):
        """Written only when set, so a store with no app job keeps its pre-stamp bytes."""

        async def scenario(svc):
            _register_crons(APP, _manifest(_agent_cron()))
            await register_app_crons_with_service(APP, svc)
            await svc.add_job_async("mine", "my own job", every_secs=600)
            return json.loads((home / "crons" / "crons.json").read_text(encoding="utf-8"))

        store = _run_scenario(home, scenario)

        by_name = {j["name"]: j for j in store["jobs"]}
        assert by_name[NAME]["app_manifest_digest"] == app_cron_manifest_digest(
            {
                "name": NAME,
                "every": 600,
                "cron_expr": "",
                "agent": "",
                "message": "digest the inbox",
                "command": "",
                "script": "",
                "app": APP,
                "agent_sequence": [],
                "env": {},
                "persistent_session": True,
                "silent": False,
                "enabled": True,
                "timezone": "",
                "skip_dates": [],
                "folder": "",
                "timeout": 0,
                "timeout_secs": 0,
            }
        )
        assert "app_manifest_digest" not in by_name["mine"]

    def test_a_reloaded_store_reads_the_stamp_back(self, home):
        async def scenario(svc):
            _register_crons(APP, _manifest(_agent_cron()))
            await register_app_crons_with_service(APP, svc)
            return _job(svc).app_manifest_digest

        stamped = _run_scenario(home, scenario)

        reloaded = CronService(base_dir=home / "crons")
        reloaded._load()
        assert _job(reloaded).app_manifest_digest == stamped != ""


class TestDigest:
    def test_identity_and_enabled_do_not_take_part(self):
        base = {"name": "a/x", "app": "a", "every": 60, "message": "m", "enabled": True}
        assert app_cron_manifest_digest(base) == app_cron_manifest_digest(
            {**base, "name": "a/y", "app": "b", "enabled": False}
        )

    def test_a_stamp_from_a_different_key_list_reads_as_legacy(self, home):
        """A stamp this build cannot read (shorter, older) applies the whole entry once."""

        async def scenario(svc):
            _register_crons(APP, _manifest(_agent_cron(timeout_secs=900)))
            await register_app_crons_with_service(APP, svc)
            job = _job(svc)
            assert await svc.update_job_async(
                job.id, app_manifest_digest="abc,def", timeout_secs=600
            )
            await register_app_crons_with_service(APP, svc)
            return _job(svc)

        job = _run_scenario(home, scenario)

        assert job.timeout_secs == 900
        assert len(job.app_manifest_digest.split(",")) == 16

    def test_a_stamp_missing_one_key_counts_that_key_alone(self, home):
        """A key added to the manifest after the stamp was written is the only one re-applied."""

        async def scenario(svc):
            _register_crons(APP, _manifest(_agent_cron(timeout_secs=900)))
            await register_app_crons_with_service(APP, svc)
            job = _job(svc)
            # An older build's stamp: every key but timeout_secs, as if the key
            # did not exist then; plus a user edit to a key the author kept.
            older = ",".join(
                part
                for part in job.app_manifest_digest.split(",")
                if not part.startswith("timeout_secs=")
            )
            assert await svc.update_job_async(
                job.id, app_manifest_digest=older, timeout_secs=600, message="mine"
            )
            await register_app_crons_with_service(APP, svc)
            return _job(svc)

        job = _run_scenario(home, scenario)

        assert job.timeout_secs == 900, "the key the stamp lacked is applied"
        assert job.message == "mine", "a key the stamp had, and the author kept, is not touched"

    def test_key_order_does_not_take_part(self):
        assert app_cron_manifest_digest({"every": 60, "message": "m"}) == app_cron_manifest_digest(
            {"message": "m", "every": 60}
        )

    @pytest.mark.parametrize(
        "field, value",
        [
            ("every", 120),
            ("cron_expr", "0 9 * * *"),
            ("agent", "other"),
            ("message", "changed"),
            ("command", "echo x"),
            ("script", "job.py:run"),
            ("agent_sequence", ["a", "b"]),
            ("env", {"K": "v"}),
            ("persistent_session", False),
            ("silent", True),
            ("timezone", "Europe/Paris"),
            ("skip_dates", ["2026-01-01"]),
            ("folder", "Ops"),
            ("timeout", 60),
            ("timeout_secs", 65),
            ("strict_schedule", True),
        ],
    )
    def test_every_manifest_owned_field_takes_part(self, field, value):
        base = {"name": "a/x", "every": 60, "message": "m"}
        assert app_cron_manifest_digest(base) != app_cron_manifest_digest({**base, field: value})


def test_default_wake_budget_is_the_scheduler_default_after_a_resync(home):
    """A manifest that drops ``timeout_secs`` puts the job back on the default, not on 0."""

    async def scenario(svc):
        _register_crons(APP, _manifest(_agent_cron(timeout_secs=900)))
        await register_app_crons_with_service(APP, svc)
        _register_crons(APP, _manifest(_agent_cron()))
        await register_app_crons_with_service(APP, svc)
        return _job(svc)

    job = _run_scenario(home, scenario)

    assert job.timeout_secs == seams._JOB_TIMEOUT_SECS
