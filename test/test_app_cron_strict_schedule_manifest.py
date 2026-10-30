"""An app cron's ``strict_schedule`` in ``app.json`` reaches the registered job.

``CronJob.strict_schedule`` skips the scheduler's start jitter, which on a daily
job can reach 59 minutes. An app asks for it on a cron entry, and the field
travels manifest -> ``app-crons.json`` -> the cron SDK, and is re-applied like
the other manifest-owned fields when it changes.

Each layer has its own assertion so a mutation that drops the field at one layer
names that layer; the end-to-end tests fail for any of them.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.apps.execution as execution_mod
from kiro_crew.apps.bridges import (
    _app_crons_path,
    _register_crons,
    load_app_cron_defs,
    register_app_crons_with_service,
)
from kiro_crew.apps.manifest import AppManifest, CronEntry
from kiro_crew.cron import CronService
from kiro_crew.cron_service.schedule import compute_jitter

APP = "strict-app"
SHORT = "morning-report"
NAME = f"{APP}/{SHORT}"


def _manifest(cron: dict) -> AppManifest:
    """A manifest declaring one cron, with the ``permissions.cron`` grant that requires."""
    return AppManifest.from_dict(
        {
            "name": APP,
            "version": "1.0.0",
            "displayName": "Strict App",
            "description": "x",
            "author": "t",
            "permissions": {"cron": True},
            "crons": [cron],
        }
    )


def _daily_cron(**extra) -> dict:
    return {"name": SHORT, "cron_expr": "50 6 * * *", "message": "gather the report", **extra}


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
                "displayName": "Strict App",
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
    jobs = [j for j in svc.list_jobs(include_disabled=True) if j.name == NAME]
    assert len(jobs) == 1, [j.name for j in svc.list_jobs(include_disabled=True)]
    return jobs[0]


def _run_scenario(home: Path, scenario):
    async def _go():
        svc = CronService(base_dir=home / "crons")
        await svc.start()
        try:
            return await scenario(svc)
        finally:
            await svc.stop()

    return asyncio.run(_go())


def _install_and_register(home: Path, manifest: AppManifest):
    async def scenario(svc):
        _register_crons(APP, manifest)
        await register_app_crons_with_service(APP, svc)
        return _job(svc)

    return _run_scenario(home, scenario)


class TestDeclaredStrictScheduleReachesTheJob:
    def test_a_strict_daily_cron_registers_with_no_start_jitter(self, home):
        """The reported bug end to end: the job fires at 06:50, not up to 59 minutes later."""
        manifest = _manifest(_daily_cron(strict_schedule=True))
        assert manifest.validate() == []

        job = _install_and_register(home, manifest)

        assert job.strict_schedule is True
        assert compute_jitter(job) == 0

    def test_absent_keeps_the_jittered_default(self, home):
        job = _install_and_register(home, _manifest(_daily_cron()))

        assert job.strict_schedule is False


class TestEachLayerCarriesTheField:
    def test_manifest_parses_it(self):
        entry = CronEntry.from_dict(_daily_cron(strict_schedule=True))
        assert entry.strict_schedule is True
        assert entry.type_invalid_fields == []

    def test_manifest_serializes_it_when_set_and_omits_it_when_unset(self):
        """``to_dict`` feeds the signed payload: an unset value must not change it."""
        d = CronEntry.from_dict(_daily_cron(strict_schedule=True)).to_dict()
        assert d["strict_schedule"] is True
        assert CronEntry.from_dict(d).strict_schedule is True

        assert "strict_schedule" not in CronEntry.from_dict(_daily_cron()).to_dict()
        assert (
            "strict_schedule"
            not in CronEntry.from_dict(_daily_cron(strict_schedule=False)).to_dict()
        )

    def test_bridge_writes_it_to_app_crons_json(self, home):
        _register_crons(APP, _manifest(_daily_cron(strict_schedule=True)))
        (d,) = load_app_cron_defs(APP)
        assert d["strict_schedule"] is True

    def test_registration_passes_it_to_the_sdk(self, home):
        _register_crons(APP, _manifest(_daily_cron(strict_schedule=True)))
        sdk = MagicMock()
        sdk.list_jobs.return_value = []
        sdk.add_job_if_absent_async = AsyncMock(return_value=MagicMock(id="job-1"))
        with patch("kiro_crew.apps.bridges.CronSDK", return_value=sdk):
            asyncio.run(register_app_crons_with_service(APP, MagicMock()))
        assert sdk.add_job_if_absent_async.call_args.kwargs["strict_schedule"] is True

    def test_a_def_written_before_the_key_existed_registers_the_default(self, home):
        """An ``app-crons.json`` from an older install has no such key."""
        _app_crons_path(APP).write_text(
            json.dumps([{"name": NAME, "every": 600, "message": "go", "app": APP}]),
            encoding="utf-8",
        )
        sdk = MagicMock()
        sdk.list_jobs.return_value = []
        sdk.add_job_if_absent_async = AsyncMock(return_value=MagicMock(id="job-1"))
        with patch("kiro_crew.apps.bridges.CronSDK", return_value=sdk):
            asyncio.run(register_app_crons_with_service(APP, MagicMock()))
        assert sdk.add_job_if_absent_async.call_args.kwargs["strict_schedule"] is False


class TestWrongTypeIsReported:
    @pytest.mark.parametrize("bad", ["true", "false", 1, 0, [True], {"on": True}])
    def test_a_non_boolean_is_ignored_and_reported(self, bad):
        """The string "false" is truthy; reading it as True would drop the delay unasked."""
        entry = CronEntry.from_dict(_daily_cron(strict_schedule=bad))
        assert entry.strict_schedule is False
        errors = _manifest(_daily_cron(strict_schedule=bad)).validate()
        assert any("'strict_schedule' has the wrong JSON type" in e for e in errors)

    def test_null_means_unset(self):
        entry = CronEntry.from_dict(_daily_cron(strict_schedule=None))
        assert entry.strict_schedule is False
        assert entry.type_invalid_fields == []


class TestReRegistration:
    def test_an_author_turning_it_on_reaches_the_installed_job(self, home):
        async def scenario(svc):
            _register_crons(APP, _manifest(_daily_cron()))
            await register_app_crons_with_service(APP, svc)
            assert _job(svc).strict_schedule is False
            _register_crons(APP, _manifest(_daily_cron(strict_schedule=True)))
            await register_app_crons_with_service(APP, svc)
            return _job(svc)

        assert _run_scenario(home, scenario).strict_schedule is True

    def test_an_author_turning_it_off_reaches_the_installed_job(self, home):
        async def scenario(svc):
            _register_crons(APP, _manifest(_daily_cron(strict_schedule=True)))
            await register_app_crons_with_service(APP, svc)
            _register_crons(APP, _manifest(_daily_cron()))
            await register_app_crons_with_service(APP, svc)
            return _job(svc)

        assert _run_scenario(home, scenario).strict_schedule is False

    def test_a_persons_setting_survives_when_the_author_did_not_touch_it(self, home):
        async def scenario(svc):
            _register_crons(APP, _manifest(_daily_cron()))
            await register_app_crons_with_service(APP, svc)
            assert await svc.update_job_async(_job(svc).id, strict_schedule=True)
            _register_crons(APP, _manifest(_daily_cron(message="a new prompt")))
            await register_app_crons_with_service(APP, svc)
            return _job(svc)

        job = _run_scenario(home, scenario)
        assert job.message == "a new prompt"
        assert job.strict_schedule is True


class TestUpgradeFromAStampWithoutTheKey:
    """The first pass after an upgrade meets stamps written before the key existed."""

    @staticmethod
    def _older_stamp(stamp: str) -> str:
        return ",".join(p for p in stamp.split(",") if not p.startswith("strict_schedule="))

    def test_a_hand_set_value_is_not_reset_to_the_default(self, home):
        async def scenario(svc):
            _register_crons(APP, _manifest(_daily_cron()))
            await register_app_crons_with_service(APP, svc)
            job = _job(svc)
            assert await svc.update_job_async(
                job.id,
                app_manifest_digest=self._older_stamp(job.app_manifest_digest),
                strict_schedule=True,
            )
            await register_app_crons_with_service(APP, svc)
            return _job(svc)

        assert _run_scenario(home, scenario).strict_schedule is True

    def test_an_author_who_asks_for_it_still_gets_it(self, home):
        async def scenario(svc):
            _register_crons(APP, _manifest(_daily_cron()))
            await register_app_crons_with_service(APP, svc)
            job = _job(svc)
            assert await svc.update_job_async(
                job.id, app_manifest_digest=self._older_stamp(job.app_manifest_digest)
            )
            _register_crons(APP, _manifest(_daily_cron(strict_schedule=True)))
            await register_app_crons_with_service(APP, svc)
            return _job(svc)

        assert _run_scenario(home, scenario).strict_schedule is True
