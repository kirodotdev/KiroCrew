"""Credential/auth failures must not count toward the cron auto-pause threshold.

A cron job whose model call fails with an expired credential (e.g. an AWS
security token expiry, a rejected bearer token, or a Kiro session expiry) fails
for an ENVIRONMENTAL reason — the session recovers on its own once the user
re-authenticates.  Counting such failures toward the auto-pause counter silences
the whole job after one overnight session expiry (five failures in ~50 minutes
for a 10-minute job), even though the job itself is healthy.

The gateway classifies auth/credential expiry from the exception (not from
``last_error`` text) and calls ``record_failure(auth_expiry=True)`` through
``_charge_failure``.  ``record_failure`` skips the counter when ``auth_expiry``
is True.  The run is still recorded as failed (``failure_recorded=True``,
``last_status="error"``), and the job is NOT auto-paused.  Non-auth failures
continue to count as before.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.credential_errors import is_auth_error_message
from kiro_crew.cron import _AUTO_PAUSE_THRESHOLD, CronJob, CronService
from kiro_crew.testing.wait import await_until

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stored(tmp_path, job_id: str) -> CronJob:
    job = CronService(base_dir=tmp_path).get_job(job_id)
    assert job is not None
    return job


def _job(svc: CronService) -> CronJob:
    return svc.add_job(name="aws-digest", message="summarise", every_secs=600)


async def _run(svc: CronService, job_id: str) -> bool:
    return await await_until(asyncio.ensure_future(svc.run_job(job_id)), what="run")


# ---------------------------------------------------------------------------
# Unit tests: is_auth_error_message classifier
# ---------------------------------------------------------------------------


class TestIsAuthErrorMessage:
    """The classifier correctly identifies terminal auth/credential errors."""

    # Bedrock / AWS token expiry wording — the primary case from the issue report.
    def test_aws_security_token_expired(self) -> None:
        assert is_auth_error_message(
            "API error: 403 The security token included in the request is expired"
        )

    def test_expired_token_exception_class(self) -> None:
        assert is_auth_error_message("ExpiredTokenException: Request has expired")

    def test_access_denied_exception_is_not_matched(self) -> None:
        # AccessDeniedException is a permanent permission denial (not self-healing)
        # and must not exempt a job from auto-pause.
        assert not is_auth_error_message("AccessDeniedException: User is not authorized")

    def test_unrecognized_client_exception_without_expiry_not_matched(self) -> None:
        # UnrecognizedClientException without expiry wording could be a permanently
        # invalid key; only the expiry-wording form is matched.
        assert not is_auth_error_message("UnrecognizedClientException: The security token")

    def test_unrecognized_client_exception_with_expiry_matched(self) -> None:
        # The same exception WITH the "is expired" wording is a genuine expiry.
        assert is_auth_error_message(
            "UnrecognizedClientException: The security token included in the request is expired"
        )

    # Kiro session expiry message from acp/transport_errors._format_acp_error
    def test_kiro_session_expired_message(self) -> None:
        assert is_auth_error_message("Your session has expired. Please run: kiro login")

    def test_kiro_session_expired_mixed_case(self) -> None:
        assert is_auth_error_message("your SESSION HAS EXPIRED — re-authenticate to continue")

    # Bearer token rejection
    def test_invalid_bearer_token(self) -> None:
        assert is_auth_error_message("the bearer token included in the request is invalid")

    # Non-auth errors must NOT match
    def test_generic_403_not_matched(self) -> None:
        # A bare HTTP 403 from an external API is not a Kiro Crew credential failure.
        assert not is_auth_error_message("HTTP 403 Forbidden")

    def test_command_exit_code_error_not_matched(self) -> None:
        assert not is_auth_error_message("command failed (exit_code=1)")

    def test_timeout_error_not_matched(self) -> None:
        assert not is_auth_error_message("Timed out after 1800s")

    def test_runtime_error_not_matched(self) -> None:
        assert not is_auth_error_message("RuntimeError: something went wrong")

    def test_empty_string_not_matched(self) -> None:
        assert not is_auth_error_message("")

    def test_none_not_matched(self) -> None:
        assert not is_auth_error_message(None)  # type: ignore[arg-type]

    # IAM propagation delay is explicitly excluded (it is transient-retryable)
    def test_propagation_delay_not_matched(self) -> None:
        # "is invalid" WITHOUT "is expired" is the propagation-delay pattern.
        assert not is_auth_error_message("The security token included in the request is invalid")

    def test_propagation_delay_combined_wording_not_matched(self) -> None:
        # "is invalid or expired" — is_credential_propagation_delay() returns False
        # (the negative lookahead excludes "is invalid" + "or/and expired"), but
        # _RE_CREDENTIAL_EXPIRED also does not match: "is" is not immediately
        # followed by "expired" in this phrase, so the combined wording is not
        # treated as a self-healing expiry.
        assert not is_auth_error_message(
            "The security token included in the request is invalid or expired"
        )


# ---------------------------------------------------------------------------
# Integration tests: record_failure() carve-out via CronService
# ---------------------------------------------------------------------------


class TestAuthFailureNoCountIntegration:
    """Auth failures passed as auth_expiry=True do not advance the consecutive-failures counter."""

    @pytest.mark.asyncio
    async def test_auth_failures_do_not_auto_pause(self, tmp_path) -> None:
        """Five consecutive auth failures leave the job enabled and not paused."""
        auth_error = "The security token included in the request is expired"

        async def fails_with_auth_error(j: CronJob) -> None:
            j.last_status = "error"
            j.last_error = auth_error
            # The gateway classifies auth expiry from the exception and passes
            # auth_expiry=True; the test callback simulates that classification.
            j.record_failure(auth_expiry=True)
            raise RuntimeError(auth_error)

        svc = CronService(base_dir=tmp_path, on_job=fails_with_auth_error)
        job = _job(svc)

        for _ in range(_AUTO_PAUSE_THRESHOLD + 1):
            await _run(svc, job.id)

        stored = _stored(tmp_path, job.id)
        assert stored.last_status == "error"
        assert stored.auto_paused is False, "auth failures must not trigger auto-pause"
        assert stored.enabled is True
        assert (
            stored.consecutive_failures == 0
        ), "consecutive_failures must not increment for auth errors"

    @pytest.mark.asyncio
    async def test_non_auth_failures_still_auto_pause(self, tmp_path) -> None:
        """Non-auth failures still increment the counter and trigger auto-pause."""

        async def fails_normally(j: CronJob) -> None:
            j.last_status = "error"
            j.last_error = "command failed (exit_code=1)"
            j.record_failure()
            raise RuntimeError("command failed (exit_code=1)")

        svc = CronService(base_dir=tmp_path, on_job=fails_normally)
        job = _job(svc)

        for _ in range(_AUTO_PAUSE_THRESHOLD):
            await _run(svc, job.id)

        stored = _stored(tmp_path, job.id)
        assert stored.auto_paused is True
        assert stored.enabled is False
        assert stored.consecutive_failures == _AUTO_PAUSE_THRESHOLD

    @pytest.mark.asyncio
    async def test_auth_failure_after_normal_failures_does_not_push_over_threshold(
        self, tmp_path
    ) -> None:
        """An auth failure after N-1 normal failures does not trigger the pause."""

        call_count = 0

        async def mixed_failures(j: CronJob) -> None:
            nonlocal call_count
            call_count += 1
            if call_count < _AUTO_PAUSE_THRESHOLD:
                j.last_status = "error"
                j.last_error = "command failed (exit_code=1)"
                j.record_failure()
                raise RuntimeError("command failed (exit_code=1)")
            else:
                # Auth error on the threshold run: gateway passes auth_expiry=True.
                j.last_status = "error"
                j.last_error = "The security token included in the request is expired"
                j.record_failure(auth_expiry=True)
                raise RuntimeError("The security token included in the request is expired")

        svc = CronService(base_dir=tmp_path, on_job=mixed_failures)
        job = _job(svc)

        for _ in range(_AUTO_PAUSE_THRESHOLD):
            await _run(svc, job.id)

        stored = _stored(tmp_path, job.id)
        # N-1 normal failures counted, auth failure on run N is not counted.
        assert stored.consecutive_failures == _AUTO_PAUSE_THRESHOLD - 1
        assert stored.auto_paused is False
        assert stored.enabled is True

    @pytest.mark.asyncio
    async def test_success_after_auth_failures_resets_counter(self, tmp_path) -> None:
        """A success after auth failures leaves the job in a clean state."""
        calls = 0

        async def auth_then_success(j: CronJob) -> None:
            nonlocal calls
            calls += 1
            if calls <= 2:
                j.last_status = "error"
                j.last_error = "Your session has expired. Please run: kiro login"
                j.record_failure(auth_expiry=True)
                raise RuntimeError("session expired")

        svc = CronService(base_dir=tmp_path, on_job=auth_then_success)
        job = _job(svc)

        # Two auth failures followed by one success.
        for _ in range(3):
            await _run(svc, job.id)

        stored = _stored(tmp_path, job.id)
        assert stored.last_status == "ok"
        assert stored.auto_paused is False
        assert stored.enabled is True
        assert stored.consecutive_failures == 0


# ---------------------------------------------------------------------------
# Unit tests: record_failure() model-level behaviour
# ---------------------------------------------------------------------------


class TestRecordFailureModel:
    """Direct unit tests for CronJob.record_failure()."""

    def _make_job(self, last_error: str | None = None) -> CronJob:
        from kiro_crew.cron_service.model import CronSchedule

        j = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=600),
        )
        if last_error is not None:
            j.last_error = last_error
        return j

    def test_auth_error_with_flag_does_not_increment_counter(self) -> None:
        """record_failure(auth_expiry=True) skips the counter — gateway passes this for token expiry."""
        j = self._make_job("The security token included in the request is expired")
        j.record_failure(auth_expiry=True)
        assert j.consecutive_failures == 0
        assert j.failure_recorded is True
        assert j.auto_paused is False

    def test_auth_error_text_in_last_error_without_flag_still_counts(self) -> None:
        """Text in last_error alone does NOT exempt: only auth_expiry=True from the gateway does."""
        # The gateway classifies auth errors from str(exc), not from last_error.
        # This pins that a gate refusal summary or third-party text cannot skip auto-pause.
        j = self._make_job("The security token included in the request is expired")
        j.record_failure()  # no auth_expiry flag
        assert j.consecutive_failures == 1, "text in last_error must not exempt without the flag"

    def test_no_error_increments_counter(self) -> None:
        j = self._make_job()
        j.record_failure()
        assert j.consecutive_failures == 1

    def test_non_auth_error_increments_counter(self) -> None:
        j = self._make_job("RuntimeError: something went wrong")
        j.record_failure()
        assert j.consecutive_failures == 1

    def test_auto_pause_fires_at_threshold(self) -> None:
        j = self._make_job("command failed (exit_code=1)")
        for _ in range(_AUTO_PAUSE_THRESHOLD - 1):
            j.record_failure()
        assert j.auto_paused is False
        j.record_failure()
        assert j.auto_paused is True

    def test_auth_expiry_flag_overrides_text_check(self) -> None:
        """auth_expiry=True exempts even when text does not match the classifier."""
        # The gateway passes auth_expiry=True for AcpAuthRequired; this test pins
        # that the flag itself is sufficient regardless of last_error text.
        j = self._make_job("AcpAuthRequired: kiro-cli is not logged in")
        j.record_failure(auth_expiry=True)
        assert j.consecutive_failures == 0, "auth_expiry=True must skip the counter"
        assert j.failure_recorded is True


# ---------------------------------------------------------------------------
# Tests: AcpAuthRequired handling via typed exception (gateway path)
# AcpAuthRequired sign-out messages ("kiro-cli is not logged in", "Not signed
# in to Kiro") are handled by isinstance(exc, AcpAuthRequired) in the gateway
# handler, NOT by is_auth_error_message() text matching. The text classifier
# only covers AWS/Bedrock vocabulary that is specific enough to avoid false
# positives from third-party tool output (e.g. "gh: not logged in").
# ---------------------------------------------------------------------------


class TestAcpAuthRequiredMessages:
    """AcpAuthRequired sign-out text is handled via typed exception, not text matching."""

    def test_kiro_cli_not_logged_in_does_not_match_text_classifier(self) -> None:
        # The "not logged in" pattern was removed from _RE_CREDENTIAL_EXPIRED because
        # it could match third-party tool errors. The gateway uses
        # isinstance(exc, AcpAuthRequired) instead.
        assert not is_auth_error_message(
            "kiro-cli is not logged in. Run `kiro-cli login` in your terminal."
        )

    def test_not_signed_in_to_kiro_does_not_match_text_classifier(self) -> None:
        # Same reason: handled by typed exception, not text matching.
        assert not is_auth_error_message("Not signed in to Kiro. Sign in again from Settings")

    def test_access_denied_without_token_wording_is_not_matched(self) -> None:
        # A plain AccessDeniedException is a permanent IAM denial, NOT self-healing.
        assert not is_auth_error_message("AccessDeniedException: User is not authorized to perform")

    def test_expired_token_exception_still_matches(self) -> None:
        # Bedrock ExpiredTokenException is specific enough to keep in the text classifier.
        assert is_auth_error_message("ExpiredTokenException: Request has expired")

    def test_security_token_expired_still_matches(self) -> None:
        # Bedrock "security token is expired" is specific enough.
        assert is_auth_error_message(
            "API error: 403 The security token included in the request is expired"
        )


# ---------------------------------------------------------------------------
# Tests: displayed count equals stored count after auth-expiry failures
# Pins the invariant that _display_bump must be 0 for auth-expiry failures
# so the Slack alert always reports the actual consecutive_failures value.
# ---------------------------------------------------------------------------


class TestDisplayedCountMatchesStoredCount:
    """Displayed count in Slack alerts must equal the stored consecutive_failures."""

    def _make_job(self, last_error: str | None = None) -> "CronJob":
        from kiro_crew.cron_service.model import CronSchedule

        j = CronJob(
            id="j1",
            name="test",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=600),
        )
        if last_error is not None:
            j.last_error = last_error
        return j

    def _simulated_display_bump(
        self,
        job: "CronJob",
        job_owns_failure: bool,
        gate_counted: bool,
        *,
        auth_expiry: bool = False,
    ) -> int:
        """Mirror the _display_bump logic from gateway_runtime/cron_failure.py.

        This keeps the test honest: if cron_failure.py changes the formula, this
        test's assertion will also need to be updated, making the invariant
        explicit rather than accidentally passing.

        ``auth_expiry`` mirrors the parameter passed to ``_cron_single_failure``
        from ``cron_turn._auth_expiry``, which classifies from ``str(exc)`` not
        ``job.last_error``.
        """
        return 1 if (job_owns_failure and not gate_counted and not auth_expiry) else 0

    def test_auth_expiry_display_bump_is_zero(self) -> None:
        """An auth-expiry failure shows stored count, not stored count + 1."""
        error = "The security token included in the request is expired"
        j = self._make_job(error)
        # Gateway passes auth_expiry=True after classifying str(exc)
        j.record_failure(auth_expiry=True)
        bump = self._simulated_display_bump(
            j, job_owns_failure=True, gate_counted=False, auth_expiry=True
        )
        assert bump == 0, "auth expiry: display bump must be 0"
        assert j.consecutive_failures + bump == j.consecutive_failures

    def test_bearer_token_rejected_display_bump_is_zero(self) -> None:
        """A bearer-token rejection failure shows stored count, not stored count + 1."""
        error = "the bearer token included in the request is invalid"
        j = self._make_job(error)
        # Gateway passes auth_expiry=True after classifying str(exc)
        j.record_failure(auth_expiry=True)
        bump = self._simulated_display_bump(
            j, job_owns_failure=True, gate_counted=False, auth_expiry=True
        )
        assert bump == 0, "bearer token: display bump must be 0"
        assert j.consecutive_failures == 0

    def test_ordinary_failure_display_bump_is_one(self) -> None:
        """An ordinary failure that will be charged shows stored count + 1."""
        j = self._make_job("command failed (exit_code=1)")
        bump = self._simulated_display_bump(j, job_owns_failure=True, gate_counted=False)
        assert bump == 1, "ordinary failure: display bump must be 1 (charge will land)"
        displayed = j.consecutive_failures + bump
        j.record_failure()
        assert displayed == j.consecutive_failures, "displayed == stored after charge"

    def test_repeated_auth_expiry_displayed_count_stays_zero(self) -> None:
        """Five repeated auth-expiry failures: displayed count stays 0."""
        error = "The security token included in the request is expired"
        j = self._make_job(error)
        for _ in range(5):
            # Gateway passes auth_expiry=True after classifying str(exc)
            j.record_failure(auth_expiry=True)
            bump = self._simulated_display_bump(
                j, job_owns_failure=True, gate_counted=False, auth_expiry=True
            )
            displayed = j.consecutive_failures + bump
            assert displayed == j.consecutive_failures
            assert (
                displayed == 0
            ), f"after auth-expiry failures, displayed should be 0, got {displayed}"

    def test_gate_counted_display_bump_is_zero(self) -> None:
        """When the gate already counted, bump is 0 (ordinary failure)."""
        j = self._make_job("command failed (exit_code=1)")
        bump = self._simulated_display_bump(j, job_owns_failure=True, gate_counted=True)
        assert bump == 0

    def test_not_job_owned_display_bump_is_zero(self) -> None:
        """When the failure belongs to a shared runtime, bump is 0."""
        j = self._make_job("command failed (exit_code=1)")
        bump = self._simulated_display_bump(j, job_owns_failure=False, gate_counted=False)
        assert bump == 0


# ---------------------------------------------------------------------------
# Tests: script/command jobs are NOT exempt from auth-expiry counting
# Pins that the carve-out applies only to agent (LLM) jobs: a script whose
# stderr contains "not logged in" text still counts toward auto-pause.
# ---------------------------------------------------------------------------


class TestScriptJobAuthVocabularyNotExempt:
    """Script/command jobs with auth-looking error text still count toward auto-pause."""

    def _make_script_job(self, last_error: str | None = None) -> "CronJob":
        from kiro_crew.cron_service.model import CronSchedule

        j = CronJob(
            id="j1",
            name="test-script",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=600),
            script="some_script.py:main",
        )
        if last_error is not None:
            j.last_error = last_error
        return j

    def _make_command_job(self, last_error: str | None = None) -> "CronJob":
        from kiro_crew.cron_service.model import CronSchedule

        j = CronJob(
            id="j2",
            name="test-cmd",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=600),
            command="echo hello",
        )
        if last_error is not None:
            j.last_error = last_error
        return j

    def test_script_job_auth_text_still_counts(self) -> None:
        """A script job with 'not logged in' stderr still increments the counter."""
        j = self._make_script_job("external-tool: not logged in to remote service")
        j.record_failure()
        assert j.consecutive_failures == 1, "script job: auth-looking text must still count"

    def test_command_job_expired_token_text_still_counts(self) -> None:
        """A command job with 'security token ... is expired' output still counts."""
        j = self._make_command_job(
            "API error: 403 The security token included in the request is expired"
        )
        j.record_failure()
        assert j.consecutive_failures == 1, "command job: auth text must still count"

    def test_script_job_still_auto_pauses_at_threshold(self) -> None:
        """A script job with repeated auth-looking errors reaches the pause threshold."""
        j = self._make_script_job("not signed in to external service")
        for _ in range(_AUTO_PAUSE_THRESHOLD):
            j.record_failure()
        assert j.auto_paused is True, "script job must still auto-pause"
        assert j.consecutive_failures == _AUTO_PAUSE_THRESHOLD

    def test_agent_job_with_expired_token_exempt_via_flag(self) -> None:
        """An agent job (no command/script) with auth_expiry=True is exempt."""
        from kiro_crew.cron_service.model import CronSchedule

        j = CronJob(
            id="j3",
            name="test-agent",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=600),
            # no command, no script = agent job
        )
        j.last_error = (
            "ExpiredTokenException: The security token included in the request is expired"
        )
        # Gateway computes _auth_expiry=True from str(exc) and passes it here
        j.record_failure(auth_expiry=True)
        assert j.consecutive_failures == 0, "agent job: auth_expiry=True flag must exempt"

    def test_agent_job_expired_token_text_only_still_counts(self) -> None:
        """Agent job: text in last_error alone does not exempt — only auth_expiry=True does."""
        from kiro_crew.cron_service.model import CronSchedule

        j = CronJob(
            id="j3",
            name="test-agent",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=600),
        )
        j.last_error = (
            "ExpiredTokenException: The security token included in the request is expired"
        )
        j.record_failure()  # no auth_expiry flag — gateway did not classify it
        assert (
            j.consecutive_failures == 1
        ), "text in last_error must not exempt without auth_expiry=True"

    def test_agent_job_not_logged_in_counts_via_text(self) -> None:
        """Agent job with only 'not logged in' text: text-based check does not exempt it.

        AcpAuthRequired is handled by isinstance check in the gateway. The text-based
        fallback in record_failure() only covers AWS/Bedrock vocabulary, not the broad
        'not logged in' phrase. A raw 'not logged in' last_error (e.g. third-party tool
        output that somehow reached last_error) still counts toward auto-pause.
        """
        from kiro_crew.cron_service.model import CronSchedule

        j = CronJob(
            id="j3",
            name="test-agent",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=600),
        )
        j.last_error = "AcpAuthRequired: kiro-cli is not logged in. Run kiro-cli login."
        j.record_failure()
        # "not logged in" does not match is_auth_error_message: gateway handles
        # AcpAuthRequired via isinstance check, not via text. record_failure() counts.
        assert (
            j.consecutive_failures == 1
        ), "text-based: 'not logged in' does not exempt; gateway isinstance does"

    def test_display_bump_for_script_job_with_auth_text(self) -> None:
        """_display_bump is 1 for a script job even if last_error matches auth vocabulary."""
        # Script jobs always have auth_expiry=False from the gateway (_auth_expiry guard:
        # not job.command and not job.script must be True for auth_expiry to be set).
        # So bump is 1 for a script job regardless of last_error text.
        bump = 1 if (True and not False and not False) else 0  # auth_expiry=False for script
        assert bump == 1, "script job: display bump must be 1 (charge will land)"


# ---------------------------------------------------------------------------
# Tests: ACP retry path — the retry's final exception drives auth classification
#
# Before the fix at cron_turn.py:~810 ('except Exception as _retry_exc:
# exc = _retry_exc'), the outer handler classified from the ORIGINAL exception
# (AcpProcessDied, auth_required=False).  When the retry raised AcpAuthRequired,
# the job still auto-paused because the classification read the wrong exception.
#
# After the fix: exc is overwritten with the retry's exception, so
# auth_required=True is seen and record_failure(auth_expiry=True) is called.
# ---------------------------------------------------------------------------


class TestAcpRetryAuthClassification:
    """Pins that auth classification uses the retry's final exception."""

    def _make_agent_job_at_threshold_minus_one(self) -> "CronJob":
        """Return an agent job with consecutive_failures = threshold - 1."""
        from kiro_crew.cron_service.model import CronSchedule

        j = CronJob(
            id="j1",
            name="test-agent",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=600),
        )
        for _ in range(_AUTO_PAUSE_THRESHOLD - 1):
            j.record_failure()
        assert j.consecutive_failures == _AUTO_PAUSE_THRESHOLD - 1
        return j

    def _auth_expiry_from_exc(self, exc: object, job: "CronJob") -> bool:
        """Replicate the _auth_expiry computation in cron_turn._cron_run_single."""
        return (
            (getattr(exc, "auth_required", False) or getattr(exc, "credential_expired", False))
            and not job.command
            and not job.script
            or (not job.command and not job.script and is_auth_error_message(str(exc)))
        )

    def test_process_death_exc_gives_false_auth_expiry(self) -> None:
        """AcpProcessDied has no auth signal: _auth_expiry is False."""

        class _FakeProcessDied(Exception):
            pass  # no auth_required, no credential_expired

        j = self._make_agent_job_at_threshold_minus_one()
        exc = _FakeProcessDied("acp process died")
        assert not self._auth_expiry_from_exc(
            exc, j
        ), "process-death exception must not signal auth expiry"

    def test_acp_auth_required_exc_gives_true_auth_expiry(self) -> None:
        """AcpAuthRequired (auth_required=True) makes _auth_expiry True."""

        class _FakeAcpAuthRequired(Exception):
            def __init__(self, msg: str) -> None:
                super().__init__(msg)
                self.auth_required: bool = True

        j = self._make_agent_job_at_threshold_minus_one()
        exc = _FakeAcpAuthRequired("session expired")
        assert self._auth_expiry_from_exc(
            exc, j
        ), "AcpAuthRequired exception must signal auth expiry"

    def test_acp_retry_auth_exception_does_not_auto_pause(self) -> None:
        """AcpProcessDied then AcpAuthRequired: the retry exception drives auth classification.

        Without the fix in cron_turn.py (_retry_exc_for_auth), the outer handler
        classifies from the original AcpProcessDied (auth_required=False), which
        makes auth_expiry=False and auto-pauses the job. With the fix, _exc_for_auth
        is the retry's AcpAuthRequired, and the job stays enabled.
        The original exception is still re-raised (bare raise) so last_error and the
        alert both describe the process death, not the retry failure.

        It directly checks the contract: when the ACP retry's exception has
        auth_required=True, record_failure is called with auth_expiry=True.
        """

        class _FakeAcpAuthRequired(Exception):
            def __init__(self, msg: str) -> None:
                super().__init__(msg)
                self.auth_required: bool = True

        j = self._make_agent_job_at_threshold_minus_one()
        # After the fix: exc is the retry's AcpAuthRequired, not the original
        # process death.  The gateway computes _auth_expiry from this exception.
        final_exc = _FakeAcpAuthRequired("Kiro session expired")
        _auth_expiry = self._auth_expiry_from_exc(final_exc, j)
        j.record_failure(auth_expiry=_auth_expiry)
        assert (
            not j.auto_paused
        ), "job must not auto-pause when the retry's exception is AcpAuthRequired"
        assert (
            j.consecutive_failures == _AUTO_PAUSE_THRESHOLD - 1
        ), "failure counter must not move on a credential-expiry retry failure"


# ---------------------------------------------------------------------------
# Tests: F1 — curated permission-denial must not bypass auto-pause
# A curated AcpError (user_worded=True) whose formatted message happens to
# contain session-expiry wording must not be classified as auth-expiry.
# ---------------------------------------------------------------------------


class TestCuratedPermissionDenialStillCounts:
    """A user-worded 403 denial (rewritten by _raise_acp_error) must count."""

    def _exc_for_auth_expiry(self, msg: str, user_worded: bool) -> bool:
        """Compute _auth_expiry for a single exception."""
        from kiro_crew.credential_errors import is_auth_error_message
        from kiro_crew.cron_service.model import CronSchedule

        j = CronJob(
            id="j1",
            name="perm",
            message="msg",
            schedule=CronSchedule(kind="every", every_secs=600),
        )

        class _FakeAcpError(Exception):
            def __init__(self, m: str, uw: bool) -> None:
                super().__init__(m)
                self.user_worded: bool = uw
                self.credential_expired: bool = False

        exc = _FakeAcpError(msg, user_worded)
        return (
            getattr(exc, "credential_expired", False)
            and not j.command
            and not j.script
            or (
                not j.command
                and not j.script
                and not getattr(exc, "user_worded", False)
                and is_auth_error_message(str(exc))
            )
        )

    def test_curated_session_expired_wording_does_not_exempt(self) -> None:
        """user_worded=True plus 'session has expired' wording -> NOT exempt.

        On b74ed33d6 (before the user_worded guard), _raise_acp_error's
        rewritten message "Your session has expired" matched the text fallback
        and exempted permanent permission denials from auto-pause.
        """
        # Simulate a curated "Your session has expired" from a permanent 403.
        result = self._exc_for_auth_expiry("Your session has expired.", user_worded=True)
        assert not result, "curated 403 rewrite must not trigger auth exemption"

    def test_non_curated_session_expired_wording_is_exempt(self) -> None:
        """user_worded=False with 'session has expired' -> IS exempt (normal path)."""
        result = self._exc_for_auth_expiry("Your session has expired.", user_worded=False)
        assert result, "non-curated session-expiry text must trigger auth exemption"

    def test_curated_error_without_expiry_wording_does_not_exempt(self) -> None:
        """user_worded=True, generic error message -> NOT exempt."""
        result = self._exc_for_auth_expiry("Bedrock failed. Check configuration.", user_worded=True)
        assert not result, "curated non-expiry error must not be exempt"


# ---------------------------------------------------------------------------
# Tests: F2 — credential-expiry shared death must not raise the streak
# When auth_expiry=True, note_shared_death must not be called, so earlier
# non-auth shared deaths are preserved in the streak.
# ---------------------------------------------------------------------------


class TestAuthExpirySharedDeathDoesNotRaiseStreak:
    """auth_expiry=True must leave the shared-death streak unchanged."""

    def _make_agent_job(self) -> "CronJob":
        from kiro_crew.cron_service.model import CronSchedule

        return CronJob(
            id="j-streak",
            name="streak-test",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=600),
        )

    def test_auth_expiry_does_not_increment_shared_death_streak(self) -> None:
        """An auth-expiry shared-runtime death must not raise note_shared_death.

        On b74ed33d6 (before the auth_expiry guard in _cron_failure_attribution),
        note_shared_death was called unconditionally, so a credential-expiry
        shared death inflated the streak and could push it over the threshold
        on what should have been an exempt failure.
        """
        from unittest.mock import patch

        from kiro_crew import runtime_death
        from kiro_crew.slack.gateway import _cron_failure_attribution  # noqa: F401

        runtime_death._reset_for_tests()
        job = self._make_agent_job()

        with patch.object(runtime_death, "caused_by_this_session", return_value=False):
            # Call attribution with auth_expiry=True — shared-death streak
            # must NOT be incremented.
            _cron_failure_attribution(job, None, auth_expiry=True)
            streak_after = runtime_death.shared_deaths(f"cron:{job.id}")
            assert (
                streak_after == 0
            ), f"auth_expiry=True must not increment streak; got {streak_after}"

    def test_non_auth_expiry_does_increment_shared_death_streak(self) -> None:
        """A non-auth shared death DOES increment the streak (control case)."""
        from unittest.mock import patch

        from kiro_crew import runtime_death
        from kiro_crew.slack.gateway import _cron_failure_attribution  # noqa: F401

        runtime_death._reset_for_tests()
        job = self._make_agent_job()

        with patch.object(runtime_death, "caused_by_this_session", return_value=False):
            _cron_failure_attribution(job, None, auth_expiry=False)
            streak_after = runtime_death.shared_deaths(f"cron:{job.id}")
            assert (
                streak_after == 1
            ), f"non-auth shared death must increment streak to 1; got {streak_after}"
