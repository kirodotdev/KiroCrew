"""Credential/auth failures must not auto-pause a cron agent job.

A cron agent job whose turn fails because the harness needs a new sign-in fails
for an ENVIRONMENTAL reason: it recovers on its own once the user signs in again.
Auto-pausing on such failures silences the whole job after one overnight session
expiry (five failures in ~50 minutes for a 10-minute job), even though the job
itself is healthy.

The gateway decides from typed data on the exception (``_cron_auth_expiry`` via
``acp_error_needs_sign_in``: an ``AcpAuthRequired``, or an ``AcpError`` whose
``auth_required`` tag is ``True`` or whose ``auth_error_code`` is an expired AWS
token; never any error text) and
calls ``record_failure(auth_expiry=True)`` through ``_charge_failure``. The
failure still counts in ``consecutive_failures`` (so the alert, the reminder and
the backoff read the real streak), and only the auto-pause is skipped. Past the
default threshold the job backs off like a never-pause job, so a job that stays
signed out keeps a bounded retry rate.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.acp.client import AcpAuthRequired, AcpError, _raise_acp_error
from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.cron import _AUTO_PAUSE_THRESHOLD, CronJob, CronService
from kiro_crew.cron_service.model import _NEVER_PAUSE_BACKOFF_BASE_SECS, CronSchedule
from kiro_crew.cron_service.schedule import is_due
from kiro_crew.slack.gateway import _charge_failure, _cron_auth_expiry, _failure_alert_due
from kiro_crew.testing.wait import await_until


def _stored(tmp_path, job_id: str) -> CronJob:
    job = CronService(base_dir=tmp_path).get_job(job_id)
    assert job is not None
    return job


def _service_job(svc: CronService) -> CronJob:
    return svc.add_job(name="aws-digest", message="summarise", every_secs=600)


async def _run(svc: CronService, job_id: str) -> bool:
    return await await_until(asyncio.ensure_future(svc.run_job(job_id)), what="run")


def _agent_job(**kw: object) -> CronJob:
    return CronJob(
        id="j1",
        name="agent",
        message="msg",
        schedule=CronSchedule(kind="every", every_secs=60),
        **kw,  # type: ignore[arg-type]
    )


def _frame(data: str) -> dict:
    return {"code": -32603, "message": "Internal error", "data": data}


#: The reported failure as a JSON-RPC error frame (as pinned in
#: ``test_acp_credential_propagation_retry.py``).
_EXPIRED_FRAME = _frame(
    "ExpiredTokenException: The security token included in the request is expired (HTTP 403)"
)


def _raised(error: dict) -> AcpError:
    with pytest.raises(AcpError) as excinfo:
        _raise_acp_error(error)
    return excinfo.value


def _tagged(message: str) -> AcpError:
    err = AcpError(message)
    err.auth_required = True
    return err


# ---------------------------------------------------------------------------
# _cron_auth_expiry decides from typed data only
# ---------------------------------------------------------------------------


class TestCronAuthExpiryClassifier:
    """The exception type and the ``auth_required`` tag decide; error text never does."""

    def test_signed_out_kiro_cli_is_auth_expiry(self) -> None:
        # kiro-cli signs in on its own, so its sign-out carries no sign-in-card
        # tag: the exception type is the verdict.
        exc = AcpAuthRequired("kiro-cli is not logged in.", backend=ACP_BACKEND_KIRO)
        assert exc.auth_required is False
        assert _cron_auth_expiry(_agent_job(), exc)

    def test_signed_out_harness_of_any_backend_is_auth_expiry(self) -> None:
        exc = AcpAuthRequired("signed out", backend="some-other-harness")
        assert _cron_auth_expiry(_agent_job(), exc)

    def test_session_expiry_tag_is_auth_expiry(self) -> None:
        assert _cron_auth_expiry(_agent_job(), _tagged("Your session has expired."))

    @pytest.mark.parametrize("backend", ["", "claude", "byo-harness"])
    def test_a_session_expiry_frame_on_a_non_kas_harness_is_auth_expiry(self, backend: str) -> None:
        # Only KAS gets ``auth_required`` (the sign-in card); every harness gets
        # ``sign_in_needed``, and that is the tag the cron exemption reads.
        with pytest.raises(AcpError) as excinfo:
            _raise_acp_error(_frame("Your session has expired (HTTP 401)"), backend=backend)
        exc = excinfo.value
        assert exc.sign_in_needed is True and exc.auth_required is False
        job = _agent_job()
        for _ in range(_AUTO_PAUSE_THRESHOLD + 1):
            job.record_failure(auth_expiry=_cron_auth_expiry(job, exc))
        assert job.auto_paused is False

    def test_truthy_non_bool_sign_in_tag_is_not_auth_expiry(self) -> None:
        exc = AcpError("boom")
        exc.sign_in_needed = "yes"  # type: ignore[assignment]
        assert not _cron_auth_expiry(_agent_job(), exc)

    def test_expiry_text_without_the_tag_is_not_auth_expiry(self) -> None:
        # The issue's own wording, on an untagged error: text alone never decides.
        exc = AcpError("API error: 403 The security token included in the request is expired")
        assert not _cron_auth_expiry(_agent_job(), exc)

    def test_expired_token_name_in_the_message_only_is_not_auth_expiry(self) -> None:
        # Built by hand, so no raise site read a code: the message never decides.
        exc = AcpError("ExpiredTokenException: Request has expired")
        assert exc.auth_error_code == ""
        assert not _cron_auth_expiry(_agent_job(), exc)

    def test_the_reported_expired_token_frame_is_auth_expiry(self) -> None:
        # The issue's failure as it arrives: the raise site reads the AWS code.
        exc = _raised(_EXPIRED_FRAME)
        assert exc.auth_error_code == "ExpiredTokenException"
        assert exc.auth_required is False
        job = _agent_job()
        assert _cron_auth_expiry(job, exc)
        for _ in range(_AUTO_PAUSE_THRESHOLD + 1):
            job.record_failure(auth_expiry=_cron_auth_expiry(job, exc))
        assert job.auto_paused is False

    def test_a_permanent_denial_frame_is_not_auth_expiry(self) -> None:
        exc = _raised(_frame("AccessDeniedException: User is not authorized (HTTP 403)"))
        assert exc.auth_error_code == "AccessDeniedException"
        assert not _cron_auth_expiry(_agent_job(), exc)

    def test_the_propagation_race_frame_is_not_auth_expiry(self) -> None:
        exc = _raised(
            _frame(
                "UnrecognizedClientException: The security token included in the request "
                "is invalid (HTTP 403)"
            )
        )
        assert not _cron_auth_expiry(_agent_job(), exc)

    def test_a_non_str_code_is_not_auth_expiry(self) -> None:
        exc = AcpError("boom")
        exc.auth_error_code = 1  # type: ignore[assignment]
        assert not _cron_auth_expiry(_agent_job(), exc)

    def test_curated_expiry_wording_without_the_tag_is_not_auth_expiry(self) -> None:
        exc = AcpError("Your session has expired.", user_worded=True)
        assert not _cron_auth_expiry(_agent_job(), exc)

    def test_third_party_expiry_prose_is_not_auth_expiry(self) -> None:
        assert not _cron_auth_expiry(_agent_job(), RuntimeError("gh: session has expired"))

    def test_tag_on_a_non_acp_error_is_not_auth_expiry(self) -> None:
        exc = RuntimeError("boom")
        exc.auth_required = True  # type: ignore[attr-defined]
        assert not _cron_auth_expiry(_agent_job(), exc)

    def test_truthy_non_bool_tag_is_not_auth_expiry(self) -> None:
        exc = AcpError("boom")
        exc.auth_required = "yes"  # type: ignore[assignment]
        assert not _cron_auth_expiry(_agent_job(), exc)

    def test_untagged_acp_error_is_not_auth_expiry(self) -> None:
        assert not _cron_auth_expiry(_agent_job(), AcpError("acp process died"))

    def test_command_job_is_never_auth_expiry(self) -> None:
        assert not _cron_auth_expiry(_agent_job(command="echo hi"), _tagged("expired"))

    def test_script_job_is_never_auth_expiry(self) -> None:
        job = _agent_job(script="some_script.py:main")
        assert not _cron_auth_expiry(job, _tagged("Your session has expired."))


# ---------------------------------------------------------------------------
# record_failure(auth_expiry=True): counted, never pauses, backs off
# ---------------------------------------------------------------------------


class TestAuthExpiryFailureModel:
    def test_auth_expiry_counts_but_does_not_pause_at_the_limit(self) -> None:
        job = _agent_job()
        for _ in range(_AUTO_PAUSE_THRESHOLD + 2):
            job.record_failure(auth_expiry=True)
        assert job.consecutive_failures == _AUTO_PAUSE_THRESHOLD + 2
        assert job.failure_recorded is True
        assert job.auto_paused is False
        assert job.enabled is True

    def test_ordinary_failures_still_pause_at_the_limit(self) -> None:
        job = _agent_job()
        for _ in range(_AUTO_PAUSE_THRESHOLD):
            job.record_failure()
        assert job.auto_paused is True
        assert job.enabled is False

    def test_expiry_text_in_last_error_without_the_flag_still_pauses(self) -> None:
        job = _agent_job()
        job.last_error = "The security token included in the request is expired"
        for _ in range(_AUTO_PAUSE_THRESHOLD):
            job.record_failure()
        assert job.auto_paused is True

    def test_an_ordinary_failure_after_an_auth_streak_pauses(self) -> None:
        job = _agent_job()
        for _ in range(_AUTO_PAUSE_THRESHOLD):
            job.record_failure(auth_expiry=True)
        assert job.auto_paused is False
        job.record_failure()
        assert job.auto_paused is True

    def test_a_signed_out_job_backs_off_past_the_default_threshold(self) -> None:
        # The bounded retry rate: without it a signed-out every=60 job would
        # start a harness on every tick until someone signed in again.
        job = _agent_job()
        for _ in range(_AUTO_PAUSE_THRESHOLD - 1):
            job.record_failure(auth_expiry=True)
        job.last_run_ts = 1_000_000.0
        assert job.failure_backoff_until() is None
        job.record_failure(auth_expiry=True)
        until = job.last_run_ts + _NEVER_PAUSE_BACKOFF_BASE_SECS
        assert job.failure_backoff_until() == until
        assert not is_due(job, job.last_run_ts + 60)
        assert is_due(job, until)

    def test_a_low_limit_signed_out_job_backs_off_too(self) -> None:
        job = _agent_job(auto_pause_after_failures=1)
        for _ in range(_AUTO_PAUSE_THRESHOLD):
            job.record_failure(auth_expiry=True)
        job.last_run_ts = 1_000_000.0
        assert job.enabled is True
        assert job.failure_backoff_until() == job.last_run_ts + _NEVER_PAUSE_BACKOFF_BASE_SECS

    def test_a_paused_job_reports_no_backoff(self) -> None:
        job = _agent_job()
        for _ in range(_AUTO_PAUSE_THRESHOLD + 3):
            job.record_failure()
        job.last_run_ts = 1_000_000.0
        assert job.auto_paused is True
        assert job.failure_backoff_until() is None

    def test_success_after_an_auth_streak_resets_everything(self) -> None:
        job = _agent_job()
        for _ in range(_AUTO_PAUSE_THRESHOLD + 1):
            job.record_failure(auth_expiry=True)
        job.last_run_ts = 1_000_000.0
        job.record_success()
        assert job.consecutive_failures == 0
        assert job.failure_backoff_until() is None

    def test_charge_failure_forwards_the_flag(self) -> None:
        job = _agent_job()
        for _ in range(_AUTO_PAUSE_THRESHOLD):
            _charge_failure(job, False, auth_expiry=True)
        assert job.consecutive_failures == _AUTO_PAUSE_THRESHOLD
        assert job.auto_paused is False

    def test_a_hand_over_on_an_auth_expiry_does_not_pause(self) -> None:
        job = _agent_job()
        _charge_failure(job, True, auth_expiry=True)
        assert job.consecutive_failures == _AUTO_PAUSE_THRESHOLD
        assert job.auto_paused is False


class TestAuthExpiryAlertThreshold:
    """The "run at the pause limit is the last one" alert shortcut needs a pause.

    An auth-expiry failure never pauses the job, so from the pause limit on only
    ``alert_after_failures`` decides. A job that does pause keeps main's rule.
    """

    @pytest.mark.parametrize(
        ("alert_after", "streak", "auth_expiry", "due"),
        [
            # alert_after_failures above the pause limit (default 5)
            (20, _AUTO_PAUSE_THRESHOLD - 1, True, False),
            (20, _AUTO_PAUSE_THRESHOLD, True, False),
            (20, 19, True, False),
            (20, 20, True, True),
            (20, 21, True, True),
            # an ordinary failure at the limit pauses the job: alert as on main
            (20, _AUTO_PAUSE_THRESHOLD - 1, False, False),
            (20, _AUTO_PAUSE_THRESHOLD, False, True),
            # alert_after_failures at or below the limit: unchanged either way
            (1, 1, True, True),
            (3, 2, True, False),
            (3, 3, True, True),
            (3, 3, False, True),
        ],
    )
    def test_alert_due(self, alert_after: int, streak: int, auth_expiry: bool, due: bool) -> None:
        job = _agent_job(alert_after_failures=alert_after)
        assert job.auto_pause_limit() == _AUTO_PAUSE_THRESHOLD
        assert _failure_alert_due(job, streak, auth_expiry=auth_expiry) is due


# ---------------------------------------------------------------------------
# Through CronService: a signed-out job is never auto-paused
# ---------------------------------------------------------------------------


class TestAuthFailureNoPauseIntegration:
    @pytest.mark.asyncio
    async def test_auth_failures_do_not_auto_pause(self, tmp_path) -> None:
        async def fails_signed_out(j: CronJob) -> None:
            j.last_status = "error"
            j.last_error = "Your session has expired."
            j.record_failure(auth_expiry=True)
            raise RuntimeError("Your session has expired.")

        svc = CronService(base_dir=tmp_path, on_job=fails_signed_out)
        job = _service_job(svc)
        for _ in range(_AUTO_PAUSE_THRESHOLD + 1):
            await _run(svc, job.id)

        stored = _stored(tmp_path, job.id)
        assert stored.last_status == "error"
        assert stored.auto_paused is False
        assert stored.enabled is True
        assert stored.consecutive_failures == _AUTO_PAUSE_THRESHOLD + 1

    @pytest.mark.asyncio
    async def test_ordinary_failures_still_auto_pause(self, tmp_path) -> None:
        async def fails_normally(j: CronJob) -> None:
            j.last_status = "error"
            j.last_error = "command failed (exit_code=1)"
            j.record_failure()
            raise RuntimeError("command failed (exit_code=1)")

        svc = CronService(base_dir=tmp_path, on_job=fails_normally)
        job = _service_job(svc)
        for _ in range(_AUTO_PAUSE_THRESHOLD):
            await _run(svc, job.id)

        stored = _stored(tmp_path, job.id)
        assert stored.auto_paused is True
        assert stored.enabled is False

    @pytest.mark.asyncio
    async def test_success_after_auth_failures_resets_the_counter(self, tmp_path) -> None:
        calls = 0

        async def signed_out_then_ok(j: CronJob) -> None:
            nonlocal calls
            calls += 1
            if calls <= 2:
                j.last_status = "error"
                j.last_error = "Your session has expired."
                j.record_failure(auth_expiry=True)
                raise RuntimeError("session expired")

        svc = CronService(base_dir=tmp_path, on_job=signed_out_then_ok)
        job = _service_job(svc)
        for _ in range(3):
            await _run(svc, job.id)

        stored = _stored(tmp_path, job.id)
        assert stored.last_status == "ok"
        assert stored.auto_paused is False
        assert stored.consecutive_failures == 0
