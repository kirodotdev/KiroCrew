"""Fire-time gate: a cron owned by a DISABLED app must not execute.

An app's crons are copied into the global store at install time and then live
on their own, so a disable that could not reach the store left them enabled and
firing. These tests drive the real ``vet_job_at_fire_time`` through the
gateway's own cron callback, so they exercise the path a live fire takes.

The observed incident state is the first test: the app's metadata says
``enabled: false`` while the store copy still says ``enabled: true``.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from test_cron_gateway_integration import (
    _make_command_job,
    _make_gw,
    _run_command_callback,
)

_APP = "ops-mission-control"
_OK = {"status": "ok", "output": "hello\n", "exit_code": 0}


def _app_owned_job(**overrides):
    """A store copy exactly as the installer wrote it: app-owned and ENABLED."""
    fields = {
        "name": f"{_APP}/dispatch",
        "created_by": f"app:{_APP}",
        "enabled": True,
    }
    fields.update(overrides)
    return _make_command_job(**fields)


def _allow_other_gates():
    """Neutralize the sibling fire-time gates so only the app gate decides."""
    return (
        patch("kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None),
        patch("kiro_crew.mcp_cron._vet_command_governance", return_value=None),
    )


class TestDisabledAppCronIsSkipped:
    @pytest.mark.asyncio
    async def test_disabled_app_cron_is_skipped_logged_and_kept(self, caplog):
        gw = _make_gw()
        job = _app_owned_job()
        cap, cmd = _allow_other_gates()
        with (
            caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_cron"),
            cap,
            cmd,
            patch("kiro_crew.apps.manager.app_enabled_state", return_value=False) as state,
        ):
            result, mock_run = await _run_command_callback(gw, job, _OK)

        state.assert_called_once_with(_APP)
        # Skipped: nothing executed.
        assert result is None
        mock_run.assert_not_called()
        # With a reason an operator can read, naming the app that stopped it.
        assert _APP in caplog.text
        assert "disabled" in caplog.text
        assert _APP in (job.last_error or "")
        assert job.last_status == "error"
        # Kept, not deleted and not durably paused: the gate persists nothing,
        # so re-enabling the app resumes the job with no repair step.
        gw.cron_svc.remove_job_async.assert_not_called()
        assert job.enabled is True
        assert job.user_paused is False
        assert job.auto_paused is False
        # The retention marker the deny path owns for one-shot jobs.
        assert job.fire_time_denied is True

    @pytest.mark.asyncio
    async def test_enabled_app_cron_still_fires(self):
        gw = _make_gw()
        job = _app_owned_job()
        cap, cmd = _allow_other_gates()
        with (
            cap,
            cmd,
            patch("kiro_crew.apps.manager.app_enabled_state", return_value=True),
        ):
            result, mock_run = await _run_command_callback(gw, job, _OK)

        assert job.last_status == "ok"
        assert result is not None
        mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_unreadable_app_state_is_skipped_too(self):
        # ``app_enabled_state`` answers None when it cannot READ the metadata.
        # That is not a licence to run the app's code, so the fire is skipped --
        # and kept, so the next fire runs once the state can be read again.
        gw = _make_gw()
        job = _app_owned_job()
        cap, cmd = _allow_other_gates()
        with (
            cap,
            cmd,
            patch("kiro_crew.apps.manager.app_enabled_state", return_value=None),
        ):
            result, mock_run = await _run_command_callback(gw, job, _OK)

        assert result is None
        mock_run.assert_not_called()
        assert "not readable" in (job.last_error or "")
        gw.cron_svc.remove_job_async.assert_not_called()
        assert job.enabled is True
        assert job.user_paused is False

    @pytest.mark.asyncio
    async def test_repeated_skips_never_auto_pause_the_job(self):
        # A skip is a policy state, not a job defect. Counting it would let the
        # consecutive-failure auto-pause park the job for good after a few fires,
        # leaving a re-enable unable to resume it.
        gw = _make_gw()
        job = _app_owned_job()
        cap, cmd = _allow_other_gates()
        with (
            cap,
            cmd,
            patch("kiro_crew.apps.manager.app_enabled_state", return_value=False),
        ):
            for _ in range(6):
                await _run_command_callback(gw, job, _OK)

        assert job.consecutive_failures == 0
        assert job.auto_paused is False
        assert job.user_paused is False
        assert job.enabled is True

    @pytest.mark.asyncio
    async def test_a_traversal_stamp_is_refused_without_touching_the_disk(self):
        # The cron store is writable from inside the sandbox and the stamp is only
        # type-checked when the store loads, so the owner name is untrusted input.
        # It must be refused before it can become a filesystem lookup key.
        gw = _make_gw()
        job = _app_owned_job(created_by="app:../../../../etc")
        cap, cmd = _allow_other_gates()
        with (
            cap,
            cmd,
            patch("kiro_crew.apps.manager.app_enabled_state") as state,
        ):
            result, mock_run = await _run_command_callback(gw, job, _OK)

        state.assert_not_called()
        assert result is None
        mock_run.assert_not_called()
        assert "not readable" in (job.last_error or "")

    @pytest.mark.asyncio
    async def test_person_owned_job_is_never_charged_to_an_app(self):
        # ``created_by`` carries a human creator's id too. A job whose name
        # merely LOOKS app-shaped must not be gated on any app's state.
        gw = _make_gw()
        job = _make_command_job(name=f"{_APP}/dispatch", created_by="U0123ABC456")
        cap, cmd = _allow_other_gates()
        with (
            cap,
            cmd,
            patch("kiro_crew.apps.manager.app_enabled_state", return_value=False) as state,
        ):
            result, mock_run = await _run_command_callback(gw, job, _OK)

        state.assert_not_called()
        assert job.last_status == "ok"
        assert result is not None
        mock_run.assert_called_once()
