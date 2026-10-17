"""An app cron's ``timeout``/``timeout_secs`` in ``app.json`` reach the registered job.

The pair is carried as one field path: ``CronEntry`` parses it, the bridge writes
it to ``app-crons.json``, and registration passes it to the cron SDK, whose
``CronService.add_job`` validates and persists it. Manifest validation applies the
same create-time rule (``validate_create_timeouts``) the SDK applies, so a pair
the scheduler would refuse is reported at install time. ``0`` or absent keeps
the scheduler default.

Each layer has its own assertion so a mutation that drops the field at that
layer names the layer that lost it; the end-to-end test fails for any of them.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.apps.execution as execution_mod
from kiro_crew import cron as seams
from kiro_crew.apps.bridges import (
    _app_crons_path,
    _register_crons,
    load_app_cron_defs,
    register_app_crons_with_service,
)
from kiro_crew.apps.manifest import AppManifest, CronEntry
from kiro_crew.cron import CronService
from kiro_crew.cron_service.fields import build_job

APP = "test-app"
SHORT = "refresh"
NAME = f"{APP}/{SHORT}"


def _manifest(cron: dict) -> AppManifest:
    """A manifest declaring one cron, with the ``permissions.cron`` grant that requires.

    Validation refuses ``crons`` without the grant, so every manifest here carries
    it: the subject under test is the ``timeout``/``timeout_secs`` pair, and an
    unrelated missing-grant error would mask it.
    """
    return AppManifest.from_dict(
        {
            "name": APP,
            "version": "1.0.0",
            "displayName": "Test App",
            "description": "x",
            "author": "t",
            "permissions": {"cron": True},
            "crons": [cron],
        }
    )


def _script_cron(**extra) -> dict:
    return {"name": SHORT, "every": 600, "script": "job.py:run", **extra}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """An isolated crew home with one installed app bundle that ships a script."""
    root = tmp_path / "kirocrew-home"
    bundle = root / "apps" / APP
    bundle.mkdir(parents=True)
    (root / "crons").mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    (bundle / "job.py").write_text("def run(ctx):\n    return 'ok'\n", encoding="utf-8")
    (bundle / "app.json").write_text(
        json.dumps(
            {
                "name": APP,
                "version": "1.0.0",
                "displayName": "Test App",
                "description": "x",
                "author": "t",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.apps.bridges._registration_denied", lambda *a, **k: False)
    monkeypatch.setattr(execution_mod, "third_party_execution_allowed", lambda: True)
    return root


def _install_and_register(home: Path, manifest: AppManifest):
    """Write the app's cron defs from ``manifest`` and register them with a real service."""
    _register_crons(APP, manifest)

    async def _go():
        svc = CronService(base_dir=home / "crons")
        await svc.start()
        try:
            await register_app_crons_with_service(APP, svc)
            return [j for j in svc.list_jobs(include_disabled=True) if j.name == NAME]
        finally:
            await svc.stop()

    return asyncio.run(_go())


class TestDeclaredTimeoutsReachTheJob:
    def test_app_json_pair_registers_with_exact_values(self, home):
        """The reported bug end to end: a 40s script needs both fields to survive install."""
        manifest = _manifest(_script_cron(timeout=60, timeout_secs=65))
        assert manifest.validate() == []

        jobs = _install_and_register(home, manifest)

        assert len(jobs) == 1
        assert jobs[0].timeout == 60
        assert jobs[0].timeout_secs == 65

    def test_absent_fields_keep_the_scheduler_default(self, home):
        """Omitting both registers exactly what an SDK create without them persists."""
        manifest = _manifest(_script_cron())
        assert manifest.validate() == []

        jobs = _install_and_register(home, manifest)

        assert len(jobs) == 1
        assert jobs[0].timeout == 0  # 0 = 30s for a script at fire time
        assert jobs[0].timeout_secs == seams._JOB_TIMEOUT_SECS


class TestEachLayerCarriesTheField:
    """A mutation that drops either field at one layer turns that layer's test red."""

    def test_manifest_parses_both(self):
        entry = CronEntry.from_dict(_script_cron(timeout=60, timeout_secs=65))
        assert (entry.timeout, entry.timeout_secs) == (60, 65)
        assert entry.type_invalid_fields == []

    def test_manifest_serializes_both_when_set_and_omits_them_when_unset(self):
        """``to_dict`` feeds the signed payload: unset values must not change it."""
        d = CronEntry.from_dict(_script_cron(timeout=60, timeout_secs=65)).to_dict()
        assert (d["timeout"], d["timeout_secs"]) == (60, 65)
        assert CronEntry.from_dict(d).timeout_secs == 65

        bare = CronEntry.from_dict(_script_cron()).to_dict()
        assert "timeout" not in bare
        assert "timeout_secs" not in bare

    def test_bridge_writes_both_to_app_crons_json(self, home):
        _register_crons(APP, _manifest(_script_cron(timeout=60, timeout_secs=65)))
        (d,) = load_app_cron_defs(APP)
        assert (d["timeout"], d["timeout_secs"]) == (60, 65)

    def test_registration_passes_both_to_the_sdk(self, home):
        _register_crons(APP, _manifest(_script_cron(timeout=60, timeout_secs=65)))
        sdk = MagicMock()
        sdk.list_jobs.return_value = []
        sdk.add_job_if_absent_async = AsyncMock(return_value=MagicMock(id="job-1"))
        with patch("kiro_crew.apps.bridges.CronSDK", return_value=sdk):
            asyncio.run(register_app_crons_with_service(APP, MagicMock()))
        kwargs = sdk.add_job_if_absent_async.call_args.kwargs
        assert (kwargs["timeout"], kwargs["timeout_secs"]) == (60, 65)

    def test_a_def_written_before_the_keys_existed_registers_the_default(self, home):
        """An ``app-crons.json`` from an older install has neither key."""
        _app_crons_path(APP).write_text(
            json.dumps([{"name": NAME, "every": 600, "message": "go", "app": APP}]),
            encoding="utf-8",
        )
        sdk = MagicMock()
        sdk.list_jobs.return_value = []
        sdk.add_job_if_absent_async = AsyncMock(return_value=MagicMock(id="job-1"))
        with patch("kiro_crew.apps.bridges.CronSDK", return_value=sdk):
            asyncio.run(register_app_crons_with_service(APP, MagicMock()))
        kwargs = sdk.add_job_if_absent_async.call_args.kwargs
        assert (kwargs["timeout"], kwargs["timeout_secs"]) == (0, 0)


# (cron fields, why the SDK refuses it)
_REFUSED = [
    pytest.param({"timeout_secs": 90000}, id="wake-budget-above-86400"),
    pytest.param({"timeout": 60, "timeout_secs": 30}, id="budget-below-subprocess"),
    pytest.param({"timeout_secs": 20}, id="budget-below-script-default"),
    pytest.param({"timeout": -5}, id="negative-subprocess-timeout"),
    pytest.param({"timeout": 90000}, id="subprocess-timeout-above-86400"),
]


class TestInvalidValuesAreRefusedLikeTheSdk:
    @pytest.mark.parametrize("fields", _REFUSED)
    def test_manifest_reports_the_sdk_error_verbatim(self, fields):
        """Manifest validation and ``build_job`` share one rule, so the message is identical."""
        with pytest.raises(ValueError) as sdk_err:
            build_job(name=NAME, message="", every_secs=600, script="job.py:run", **fields)

        errors = _manifest(_script_cron(**fields)).validate()

        assert f"cron entry {SHORT!r}: {sdk_err.value}" in errors

    @pytest.mark.parametrize("fields", _REFUSED)
    def test_registration_still_refuses_a_pair_that_skipped_validation(self, home, fields):
        """``CronService.add_job`` stays the authority: no job is created."""
        jobs = _install_and_register(home, _manifest(_script_cron(**fields)))
        assert jobs == []

    def test_a_valid_pair_passes_both(self):
        job = build_job(
            name=NAME, message="", every_secs=600, script="job.py:run", timeout=60, timeout_secs=65
        )
        assert (job.timeout, job.timeout_secs) == (60, 65)
        assert _manifest(_script_cron(timeout=60, timeout_secs=65)).validate() == []

    @pytest.mark.parametrize("key", ["timeout", "timeout_secs"])
    @pytest.mark.parametrize("bad", ["sixty", True, [60], {"s": 60}])
    def test_wrong_json_type_is_reported_not_silently_dropped(self, key, bad):
        entry = CronEntry.from_dict(_script_cron(**{key: bad}))
        assert getattr(entry, key) == 0
        errors = _manifest(_script_cron(**{key: bad})).validate()
        assert any(f"{key!r} has the wrong JSON type" in e for e in errors)

    @pytest.mark.parametrize("key", ["timeout", "timeout_secs"])
    def test_null_means_unset(self, key):
        entry = CronEntry.from_dict(_script_cron(**{key: None}))
        assert getattr(entry, key) == 0
        assert entry.type_invalid_fields == []
