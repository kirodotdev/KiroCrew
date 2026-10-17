"""Coverage for the aws_control nightly backup lifecycle hooks.

The loop CLAIMS each due backup through the Job SDK
(``start_async(kind, dedupe_key=account, params={caller: scheduled})``) rather
than running it on a bare thread. This file covers the due-check's early-return
guards, the claim itself, the ``_loop`` supervisor's error swallowing, and
``on_startup`` / ``on_shutdown`` idempotence. The runner's own behaviour (reading
the caller from params, the scheduled failure-backoff) is pinned in
``test_aws_control_backup_job.py``, where the runner lives.
"""

from __future__ import annotations

import asyncio
from unittest import mock
from unittest.mock import AsyncMock

import pytest

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control import hooks
from kiro_crew.apps.job_sdk import JobError, UnknownJobKind

ACCOUNT = "111122223333"


async def _never() -> None:
    """A stand-in for ``_loop`` that blocks until cancelled.

    ``on_startup`` schedules this as a real task; using a coroutine that never
    returns (rather than one that resolves instantly) means the task is genuinely
    pending when ``on_shutdown`` cancels it, so the cancel is meaningful and no
    "task was destroyed but pending" warning leaks from a test.
    """
    await asyncio.Event().wait()


def _run(coro):
    """Drive one coroutine to completion on a throwaway loop."""
    return asyncio.run(coro)


def _fake_sdk():
    """A JobSDK stand-in whose ``start_async`` records its calls."""
    sdk = mock.Mock()
    sdk.start_async = AsyncMock(return_value="run-123")
    return sdk


class TestRunOnceEarlyReturns:
    """Each guard makes the loop fail CLOSED: a missing precondition is a log
    line and a return, never a claimed run. One test per guard. ``start_async``
    must stay untouched in every one."""

    def test_no_healthy_registered_key_skips(self):
        # With no working key resolved there is nothing the loop may run under,
        # so it must return before probing identity. The resolution is the one
        # the grant was recorded against, so a key it rejects is a key the
        # consent gate would refuse anyway.
        sdk = _fake_sdk()
        with (
            mock.patch.object(
                hooks.accounts_mod, "resolve_default_account_profile", AsyncMock(return_value=None)
            ),
            mock.patch.object(hooks.aws_consent, "probe_identity") as probe,
        ):
            _run(hooks._run_once(sdk))
        probe.assert_not_called()
        sdk.start_async.assert_not_called()

    def test_the_nightly_identity_probe_bypasses_the_cache(self):
        """The account this loop keys backups by must not come from memory.

        A repoint inside a cached-probe window keys ``due_for_nightly`` and the
        claim to the wrong account, unattended. Asserted on the KEYWORD because
        the loop's own guards stub the probe out.
        """
        sdk = _fake_sdk()
        with (
            mock.patch.object(
                hooks.accounts_mod,
                "resolve_default_account_profile",
                AsyncMock(return_value=("p", "us-west-2")),
            ),
            mock.patch.object(
                hooks.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=False, account="")),
            ) as probe,
        ):
            _run(hooks._run_once(sdk))
        assert probe.await_args.kwargs.get("use_cache") is False

    def test_unresolved_identity_skips(self):
        # A profile NAME is not an account; if the live probe cannot resolve one
        # the loop cannot key backup state, so it returns before the due-check.
        sdk = _fake_sdk()
        with (
            mock.patch.object(
                hooks.accounts_mod,
                "resolve_default_account_profile",
                AsyncMock(return_value=("p", "us-west-2")),
            ),
            mock.patch.object(
                hooks.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=False, account="")),
            ),
            mock.patch.object(hooks.backup_mod, "due_for_nightly") as due,
        ):
            _run(hooks._run_once(sdk))
        due.assert_not_called()
        sdk.start_async.assert_not_called()

    def test_identity_ok_but_no_account_skips(self):
        # ok True with an empty account is still unresolved -- the guard checks
        # both, so this distinct branch must also return before due-check.
        sdk = _fake_sdk()
        with (
            mock.patch.object(
                hooks.accounts_mod,
                "resolve_default_account_profile",
                AsyncMock(return_value=("p", "us-west-2")),
            ),
            mock.patch.object(
                hooks.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account="")),
            ),
            mock.patch.object(hooks.backup_mod, "due_for_nightly") as due,
        ):
            _run(hooks._run_once(sdk))
        due.assert_not_called()

    def test_not_due_skips_before_consent(self):
        # Most wakes are not due. A not-due run must return before touching
        # consent so a nightly-disabled account is never asked to spend money.
        sdk = _fake_sdk()
        with (
            mock.patch.object(
                hooks.accounts_mod,
                "resolve_default_account_profile",
                AsyncMock(return_value=("p", "us-west-2")),
            ),
            mock.patch.object(
                hooks.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account=ACCOUNT)),
            ),
            mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=False),
            mock.patch.object(hooks.backup_mod, "due_for_sessions_nightly", return_value=False),
            mock.patch.object(hooks.backup_mod, "nightly_sessions_enabled", return_value=False),
            mock.patch.object(hooks.aws_consent, "refuse_and_log") as refuse,
        ):
            _run(hooks._run_once(sdk))
        refuse.assert_not_called()
        sdk.start_async.assert_not_called()

    def test_consent_refused_skips_before_claiming(self):
        # Consent fails closed: if refuse_and_log returns False the run stops
        # (it already logged + audited), before any run is claimed, so a revoked
        # grant produces no SDK run and no upload.
        sdk = _fake_sdk()
        with (
            mock.patch.object(
                hooks.accounts_mod,
                "resolve_default_account_profile",
                AsyncMock(return_value=("p", "us-west-2")),
            ),
            mock.patch.object(
                hooks.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account=ACCOUNT)),
            ),
            mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=True),
            mock.patch.object(hooks.backup_mod, "due_for_sessions_nightly", return_value=False),
            mock.patch.object(hooks.aws_consent, "refuse_and_log", AsyncMock(return_value=False)),
        ):
            _run(hooks._run_once(sdk))
        sdk.start_async.assert_not_called()


class TestClaimsTheRunThroughTheSdk:
    """Past every guard, each due kind is CLAIMED through the SDK. The account is
    the dedupe key and the scheduled caller rides in params, so a nightly and a
    manual click adopt one another and the Backup row sees the run."""

    def _drive(self, sdk, *, snapshot_due, sessions_due, bucket="bkt"):
        with (
            mock.patch.object(
                hooks.accounts_mod,
                "resolve_default_account_profile",
                AsyncMock(return_value=("p", "us-west-2")),
            ),
            mock.patch.object(
                hooks.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account=ACCOUNT)),
            ),
            mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=snapshot_due),
            mock.patch.object(
                hooks.backup_mod, "due_for_sessions_nightly", return_value=sessions_due
            ),
            mock.patch.object(hooks.aws_consent, "refuse_and_log", AsyncMock(return_value=True)),
            mock.patch.object(hooks.backup_mod.storage, "find_drive", return_value=bucket),
        ):
            _run(hooks._run_once(sdk))

    def test_a_due_snapshot_is_claimed_with_the_account_and_scheduled_caller(self):
        sdk = _fake_sdk()
        self._drive(sdk, snapshot_due=True, sessions_due=False)
        sdk.start_async.assert_awaited_once()
        call = sdk.start_async.await_args
        assert call.args[0] == hooks.backup_mod.KIND_SNAPSHOT
        # The account is the dedupe key: this is what makes a nightly run and a
        # manual click for one account adopt one another instead of both paying.
        assert call.kwargs["dedupe_key"] == ACCOUNT
        # The scheduled-caller param is what tells the shared runner to apply the
        # unattended-only gates and to feed the failure backoff. The verified
        # profile/region the loop just probed ride alongside it, so a scheduled run
        # can fall back to them if the runner's sync cache has expired past the TTL
        # by the time the fire-and-forget start reaches it.
        assert call.kwargs["params"] == {
            hooks.backup_mod.JOB_PARAM_CALLER: hooks.backup_mod.CALLER_SCHEDULED,
            hooks.backup_mod.JOB_PARAM_PROFILE: "p",
            hooks.backup_mod.JOB_PARAM_REGION: "us-west-2",
        }

    def test_both_due_kinds_are_each_claimed_once(self):
        # Snapshot and sessions dedupe on (kind, account) independently, so a wake
        # due for both claims two runs -- one per kind -- not one.
        sdk = _fake_sdk()
        self._drive(sdk, snapshot_due=True, sessions_due=True)
        kinds = [c.args[0] for c in sdk.start_async.await_args_list]
        assert kinds == [hooks.backup_mod.KIND_SNAPSHOT, hooks.backup_mod.KIND_SESSIONS]
        assert all(c.kwargs["dedupe_key"] == ACCOUNT for c in sdk.start_async.await_args_list)

    def test_no_drive_yet_claims_no_run(self):
        # The drive is tag-discovered per wake. Until one exists there is nowhere to
        # push, so the loop claims NOTHING -- as it did before this change. Claiming
        # a run anyway would leave a `done` SDK run on the Backup row every wake for
        # a backup that uploaded nothing, and that newest `done` run would clear an
        # owner's earlier `lastFailed`. A missing drive is "nothing to attempt yet".
        sdk = _fake_sdk()
        self._drive(sdk, snapshot_due=True, sessions_due=True, bucket="")
        sdk.start_async.assert_not_awaited()

    def test_a_refused_start_is_a_log_line_not_a_crash(self):
        # The runtime can refuse a start (mid-teardown, or no runner registered).
        # Nothing reached AWS, so it is a logged skip and the next wake tries
        # again -- it must not propagate out of the loop body.
        for exc in (JobError("shutting down"), UnknownJobKind("no runner")):
            sdk = _fake_sdk()
            sdk.start_async = AsyncMock(side_effect=exc)
            self._drive(sdk, snapshot_due=True, sessions_due=False)  # no raise
            sdk.start_async.assert_awaited_once()


class TestADriveLookupThatRaisesIsRecorded:
    """A drive lookup that RAISES (not returns no drive) is a fault, and it
    happens in the loop BEFORE any run is claimed, so the runner's own failure
    handler never sees it. It must still back off and audit per due kind -- or a
    deterministically failing lookup re-attempts every wake forever. A setup
    fault and a push fault for one kind are the same invariant: both back off and
    both audit `failed`. A cancel is teardown, not a fault: it does NOT back off,
    but it still audits `cancelled` per due kind so the interrupt stays visible."""

    def _run_once_with_raising_find_drive(self, sdk, *, snapshot_due, sessions_due):
        with (
            mock.patch.object(
                hooks.accounts_mod,
                "resolve_default_account_profile",
                AsyncMock(return_value=("p", "us-west-2")),
            ),
            mock.patch.object(
                hooks.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account=ACCOUNT)),
            ),
            mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=snapshot_due),
            mock.patch.object(
                hooks.backup_mod, "due_for_sessions_nightly", return_value=sessions_due
            ),
            mock.patch.object(hooks.aws_consent, "refuse_and_log", AsyncMock(return_value=True)),
            mock.patch.object(
                hooks.backup_mod.storage,
                "find_drive",
                side_effect=RuntimeError("tagging API error"),
            ),
            mock.patch.object(hooks.backup_mod, "nightly_run_witness", return_value=("proc", 3)),
            mock.patch.object(hooks.backup_mod, "record_nightly_failure") as rec,
            mock.patch.object(hooks.backup_mod, "sel") as sel,
        ):
            _run(hooks._run_once(sdk))
        return rec, sel

    def test_both_due_kinds_each_record_and_audit_the_failure(self):
        # The lookup raises for a wake due for BOTH kinds. Each due kind must get
        # its own backoff record AND its own `failed` audit -- a lookup that keeps
        # failing is otherwise the one failure shape that never backs off.
        sdk = _fake_sdk()
        rec, sel = self._run_once_with_raising_find_drive(sdk, snapshot_due=True, sessions_due=True)
        # No run is claimed -- there is no drive to push to.
        sdk.start_async.assert_not_awaited()
        # One backoff record per due kind, each witnessed and carrying the error.
        kinds_recorded = {c.args[1] for c in rec.call_args_list}
        assert kinds_recorded == {
            hooks.backup_mod.KIND_SNAPSHOT,
            hooks.backup_mod.KIND_SESSIONS,
        }
        assert all(c.kwargs["run_witness"] == ("proc", 3) for c in rec.call_args_list)
        # One `failed` SEL audit per due kind.
        audits = sel.return_value.log_api_access.call_args_list
        assert len(audits) == 2
        assert all(c.kwargs["outcome"] == "failed" for c in audits)
        assert all(c.kwargs["operation"] == "aws_control.backup_nightly" for c in audits)

    def test_a_cancel_during_the_lookup_audits_cancelled_but_does_not_back_off(self):
        # Teardown is not a fault: a cancel while resolving the drive is the app
        # being disabled / the gateway stopping, and counting it as a failed
        # attempt would push the next night's backup out -- so it does NOT back
        # off. But teardown must still leave a trail that it was interrupted, so
        # it audits `cancelled` per due kind before propagating the cancel.
        sdk = _fake_sdk()
        with (
            mock.patch.object(
                hooks.accounts_mod,
                "resolve_default_account_profile",
                AsyncMock(return_value=("p", "us-west-2")),
            ),
            mock.patch.object(
                hooks.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account=ACCOUNT)),
            ),
            mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=True),
            mock.patch.object(hooks.backup_mod, "due_for_sessions_nightly", return_value=True),
            mock.patch.object(hooks.aws_consent, "refuse_and_log", AsyncMock(return_value=True)),
            mock.patch.object(
                hooks.backup_mod.storage,
                "find_drive",
                side_effect=asyncio.CancelledError(),
            ),
            mock.patch.object(hooks.backup_mod, "nightly_run_witness", return_value=("proc", 3)),
            mock.patch.object(hooks.backup_mod, "record_nightly_failure") as rec,
            mock.patch.object(hooks.backup_mod, "sel") as sel,
        ):
            with pytest.raises(asyncio.CancelledError):
                _run(hooks._run_once(sdk))
        # A cancel is teardown, not a fault: no backoff, no run claimed.
        rec.assert_not_called()
        sdk.start_async.assert_not_awaited()
        # But the interrupt stays visible: one `cancelled` SEL audit per due kind,
        # never `failed`.
        audits = sel.return_value.log_api_access.call_args_list
        assert len(audits) == 2
        assert all(c.kwargs["outcome"] == "cancelled" for c in audits)
        assert all(c.kwargs["operation"] == "aws_control.backup_nightly" for c in audits)


class TestLoopSupervisor:
    """The while-True supervisor: it swallows a raising ``_run_once`` (a bad
    night must not kill the worker) but propagates a cancel to exit teardown."""

    def test_loop_swallows_run_once_error_then_sleeps(self):
        # First pass raises (swallowed), so control reaches the sleep; we cancel
        # AT the sleep to break the otherwise-infinite loop deterministically.
        calls = {"n": 0}

        async def _boom(_sdk) -> None:
            calls["n"] += 1
            raise RuntimeError("bad night")

        async def _drive() -> None:
            with (
                mock.patch.object(hooks, "get_sdk", return_value=_fake_sdk()),
                mock.patch.object(hooks, "_run_once", side_effect=_boom),
                mock.patch.object(  # flake-ok: patching asyncio.sleep is the deterministic way to break _loop's infinite wait
                    hooks.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError())
                ),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await hooks._loop()

        _run(_drive())
        assert calls["n"] == 1

    def test_loop_propagates_cancel_from_run_once(self):
        # A cancel raised by _run_once itself must exit the loop, not be caught
        # by the broad Exception handler (CancelledError is re-raised first).
        async def _drive() -> None:
            with (
                mock.patch.object(hooks, "get_sdk", return_value=_fake_sdk()),
                mock.patch.object(hooks, "_run_once", side_effect=asyncio.CancelledError()),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await hooks._loop()

        _run(_drive())

    def test_loop_skips_run_once_when_no_sdk_is_registered(self):
        # Without the `jobs` permission the SDK is absent; the loop must not call
        # _run_once with None -- it waits for the next wake.
        ran = {"n": 0}

        async def _count(_sdk) -> None:
            ran["n"] += 1

        async def _drive() -> None:
            with (
                mock.patch.object(hooks, "get_sdk", return_value=None),
                mock.patch.object(hooks, "_run_once", side_effect=_count),
                mock.patch.object(  # flake-ok: patching asyncio.sleep is the deterministic way to break _loop's infinite wait
                    hooks.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError())
                ),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await hooks._loop()

        _run(_drive())
        assert ran["n"] == 0


class TestStartupShutdown:
    """Enable/disable idempotence. Every test patches out the real loop body so
    no background task ever survives the test, and asserts on the task object."""

    def teardown_method(self):
        # Guard against a leaked module-global task between tests.
        hooks._task = None

    def test_on_startup_starts_a_task_and_clears_stop(self):
        async def _drive() -> None:
            hooks._task = None
            with (
                mock.patch.object(hooks, "_loop", _never),
                mock.patch.object(hooks, "_register_job_runners", AsyncMock()),
                mock.patch.object(hooks.backup_mod, "clear_stop") as clear,
            ):
                await hooks.on_startup(None)
                assert hooks._task is not None
                clear.assert_called_once()
                await hooks.on_shutdown(None)

        with mock.patch.object(hooks.backup_mod, "signal_stop"):
            _run(_drive())
        assert hooks._task is None

    def test_on_startup_is_idempotent_while_running(self):
        # A second enable while the worker is live must be a no-op: it must NOT
        # spawn a second loop or re-clear the stop.
        async def _drive() -> None:
            hooks._task = None
            with (
                mock.patch.object(hooks, "_loop", _never),
                mock.patch.object(hooks, "_register_job_runners", AsyncMock()),
            ):
                with mock.patch.object(hooks.backup_mod, "clear_stop"):
                    await hooks.on_startup(None)
                first = hooks._task
                with mock.patch.object(hooks.backup_mod, "clear_stop") as clear2:
                    await hooks.on_startup(None)
                    clear2.assert_not_called()
                assert hooks._task is first
                with mock.patch.object(hooks.backup_mod, "signal_stop"):
                    await hooks.on_shutdown(None)

        _run(_drive())

    def test_on_startup_restarts_when_prior_task_is_done(self):
        # If the previous task already finished, the "running" guard must fall
        # through and a fresh loop starts -- otherwise a crashed worker would
        # never be replaced.
        async def _drive() -> None:
            done = asyncio.get_running_loop().create_future()
            done.set_result(None)
            hooks._task = done  # a completed task/future: .done() is True
            with (
                mock.patch.object(hooks, "_loop", _never),
                mock.patch.object(hooks, "_register_job_runners", AsyncMock()),
                mock.patch.object(hooks.backup_mod, "clear_stop") as clear,
            ):
                await hooks.on_startup(None)
                assert hooks._task is not done
                clear.assert_called_once()
                with mock.patch.object(hooks.backup_mod, "signal_stop"):
                    await hooks.on_shutdown(None)

        _run(_drive())

    def test_on_shutdown_signals_stop_and_cancels(self):
        # Teardown must set the stop EVENT (so a worker mid-build refuses its
        # upload) AND cancel the task (so the awaiting loop unblocks). Both, and
        # then _task must be cleared so a later enable can start fresh.
        async def _drive() -> None:
            with (
                mock.patch.object(hooks, "_loop", _never),
                mock.patch.object(hooks, "_register_job_runners", AsyncMock()),
            ):
                with mock.patch.object(hooks.backup_mod, "clear_stop"):
                    await hooks.on_startup(None)
            task = hooks._task
            with mock.patch.object(hooks.backup_mod, "signal_stop") as signal:
                await hooks.on_shutdown(None)
            signal.assert_called_once()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
            assert hooks._task is None

        _run(_drive())

    def test_on_shutdown_with_no_task_still_signals_stop(self):
        # Disabling an app that never started (or was already torn down) must
        # still signal stop without dereferencing a None task.
        async def _drive() -> None:
            hooks._task = None
            with mock.patch.object(hooks.backup_mod, "signal_stop") as signal:
                await hooks.on_shutdown(None)
            signal.assert_called_once()
            assert hooks._task is None

        _run(_drive())
