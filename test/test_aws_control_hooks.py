"""Coverage for the aws_control nightly backup lifecycle hooks.

``test_aws_control_app.py::TestRound22Hardening`` already pins the two
end-of-loop SEL cases (full success emits ``invoked``+``succeeded``; a raising
backup is audited ``failed``) by patching ``hooks._audit`` wholesale. Those
leave the interesting parts of the module untested: every EARLY-RETURN guard in
``_run_once`` that makes the loop fail closed, the ``_audit`` body itself
(including its best-effort swallow), the ``_loop`` supervisor's error
swallowing, and ``on_startup`` / ``on_shutdown`` idempotence. This file covers
exactly those, and never leaves a real background task running past a test.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest import mock
from unittest.mock import AsyncMock

import pytest

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control import hooks

ACCOUNT = "111122223333"


async def _never() -> None:
    """A stand-in for ``_loop`` that blocks until cancelled.

    ``on_startup`` schedules this as a real task; using a coroutine that never
    returns (rather than one that resolves instantly) means the task is genuinely
    pending when ``on_shutdown`` cancels it, so the cancel is meaningful and no
    "task was destroyed but pending" warning leaks from a test.
    """
    await asyncio.Event().wait()


class TestAuditBody:
    """The SEL helper the two Round-22 tests patch OUT -- exercised here for real
    so its success path and its must-never-raise contract are both pinned."""

    def test_audit_forwards_a_three_part_record_to_sel(self):
        # The unattended loop has no request, so this call is the ONLY audit
        # trail for a nightly S3 mutation. Confirm every field the handlers rely
        # on is forwarded, and that resources/error are truncated to 200 chars
        # so an oversized value cannot blow past the SEL column bound.
        with mock.patch.object(hooks, "sel") as sel_factory:
            hooks._audit("backup_nightly", "r" * 300, "invoked", error="e" * 300)
        call = sel_factory.return_value.log_api_access.call_args
        assert call.kwargs["operation"] == "aws_control.backup_nightly"
        assert call.kwargs["outcome"] == "invoked"
        assert call.kwargs["caller"] == "aws-control-nightly"
        assert len(call.kwargs["resources"]) == 200
        assert len(call.kwargs["error"]) == 200

    def test_audit_swallows_a_failing_sel(self):
        # A broken audit backend must never abort a backup -- the audit is
        # best-effort by the same rule the HTTP handlers use. A raise here would
        # crash the loop and silently stop nightly backups.
        with mock.patch.object(hooks, "sel", side_effect=RuntimeError("sel down")):
            hooks._audit("backup_nightly", "backup/snapshots", "invoked")  # no raise


def _run(coro):
    """Drive one coroutine to completion on a throwaway loop."""
    return asyncio.run(coro)


class TestRunOnceEarlyReturns:
    """Each guard makes the loop fail CLOSED: a missing precondition is a log
    line and a return, never an unconfirmed AWS call. One test per guard."""

    def test_no_healthy_registered_key_skips(self):
        # With no working key resolved there is nothing the loop may run under,
        # so it must return before probing identity. The resolution is the one
        # the grant was recorded against, so a key it rejects is a key the
        # consent gate would refuse anyway.
        with (
            mock.patch.object(
                hooks.accounts_mod, "resolve_default_account_profile", AsyncMock(return_value=None)
            ),
            mock.patch.object(hooks.aws_consent, "probe_identity") as probe,
        ):
            _run(hooks._run_once())
        probe.assert_not_called()

    def test_the_nightly_identity_probe_bypasses_the_cache(self):
        """The account this loop keys backups by must not come from memory.

        ``resolve_default_account_profile`` reaches ``_fold_profile``, which probes
        every registry entry with the cache ON, so a cached probe here answers from
        an entry primed moments earlier -- the comment's "live probe" would never
        run live. A repoint inside that window keys ``due_for_nightly``,
        ``find_drive`` and the snapshot record to the wrong account, unattended.

        Asserted on the KEYWORD rather than on a real cache because the loop's own
        guards stub the probe out; the cache behaviour itself is pinned where the
        cache lives.
        """
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
            _run(hooks._run_once())
        assert probe.await_args.kwargs.get("use_cache") is False

    def test_unresolved_identity_skips(self):
        # A profile NAME is not an account; if the live probe cannot resolve one
        # (ok False or empty account) the loop cannot key backup state, so it
        # returns before the due-check.
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
            _run(hooks._run_once())
        due.assert_not_called()

    def test_identity_ok_but_no_account_skips(self):
        # ok True with an empty account is still unresolved -- the guard checks
        # both, so this distinct branch must also return before due-check.
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
            _run(hooks._run_once())
        due.assert_not_called()

    def test_not_due_skips_before_consent(self):
        # The half-hourly wake is cheap; most wakes are not due. A not-due run
        # must return before touching consent so a nightly-disabled account is
        # never even asked to spend money.
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
            mock.patch.object(hooks.aws_consent, "refuse_and_log") as refuse,
        ):
            _run(hooks._run_once())
        refuse.assert_not_called()

    def test_consent_refused_skips_before_drive_lookup(self):
        # Consent fails closed: if refuse_and_log returns False the run stops
        # (it already logged + audited), before find_drive is called, so a
        # revoked grant produces no S3 read and no upload.
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
            mock.patch.object(hooks.aws_consent, "refuse_and_log", AsyncMock(return_value=False)),
            mock.patch.object(hooks.storage_mod, "find_drive") as find,
        ):
            _run(hooks._run_once())
        find.assert_not_called()

    def test_no_drive_bucket_skips_before_backup(self):
        # The drive is tag-discovered per run, not trusted from memory. Until one
        # exists there is nowhere to push, so the run returns before invoking the
        # snapshot backup (and before the "invoked" audit).
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
            mock.patch.object(hooks.aws_consent, "refuse_and_log", AsyncMock(return_value=True)),
            mock.patch.object(hooks.storage_mod, "find_drive", return_value=""),
            mock.patch.object(hooks.backup_mod, "run_snapshot_backup") as backup,
            mock.patch.object(hooks, "_audit") as audit,
        ):
            _run(hooks._run_once())
        backup.assert_not_called()
        audit.assert_not_called()


class TestSharedDriveNotice:
    """A shared drive is a NOTICE, never a gate. Two installs pointed at one
    account each write under their own prefix, so the nightly loop records the
    fact and PROCEEDS -- refusing would silently stop backing one machine up."""

    def _proceeding_run(self, *, others):
        """Run ``_run_once`` past every guard to the backup, with the
        shared-drive check returning (or raising) ``others``."""
        others_patch = (
            mock.patch.object(hooks.backup_mod, "other_install_ids", side_effect=others)
            if isinstance(others, Exception)
            else mock.patch.object(hooks.backup_mod, "other_install_ids", return_value=others)
        )
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
            mock.patch.object(hooks.aws_consent, "refuse_and_log", AsyncMock(return_value=True)),
            mock.patch.object(hooks.storage_mod, "find_drive", return_value="kirocrew-drive-abc"),
            others_patch,
            mock.patch.object(
                hooks.backup_mod, "run_snapshot_backup", return_value={"key": "snapshots/x"}
            ) as backup,
            mock.patch.object(hooks, "_audit") as audit,
        ):
            _run(hooks._run_once())
        return backup, audit

    def test_another_install_present_still_backs_up_and_audits_the_shared_drive(self):
        # The decisive pin: the run does NOT refuse. The backup still runs, and a
        # `backup_shared_drive` record is emitted so the shared state is visible
        # in the same SEL trail as the upload it precedes.
        backup, audit = self._proceeding_run(others=["b" * 32])
        backup.assert_called_once()
        shared = [c for c in audit.call_args_list if c.args[0] == "backup_shared_drive"]
        assert shared, "the shared-drive notice was not audited"
        assert shared[0].args[2] == "invoked"

    def test_a_failing_shared_drive_check_never_blocks_the_backup(self):
        # The notice is best-effort: if other_install_ids raises, the check is
        # one less log line, never a missed nightly. The backup still runs and no
        # shared-drive record is emitted.
        backup, audit = self._proceeding_run(others=RuntimeError("list denied"))
        backup.assert_called_once()
        assert not [c for c in audit.call_args_list if c.args[0] == "backup_shared_drive"]

    def test_no_other_install_emits_no_shared_drive_record(self):
        # A drive only this install writes to is not shared, so there is nothing
        # to record. The backup runs; the shared-drive audit does not fire.
        backup, audit = self._proceeding_run(others=[])
        backup.assert_called_once()
        assert not [c for c in audit.call_args_list if c.args[0] == "backup_shared_drive"]

    def test_the_notice_costs_exactly_one_paid_list_call(self):
        # One prefix is enough to ANSWER the question the notice asks -- "does
        # another install write to this drive" -- so the sweep stays at one paid
        # LIST however many kinds the nightly pushes. Sweeping every prefix spent
        # an extra paid LIST per scheduled run to re-derive an answer already in
        # hand, a real cost on the one path that spends without a human present,
        # which is exactly where an unread call is least defensible.
        with mock.patch.object(hooks.backup_mod, "_install_folders", return_value=set()) as folders:
            hooks.backup_mod.other_install_ids("p", "us-west-2", "bkt", account="111122223333")
        assert folders.call_count == 1
        assert folders.call_args.args[3] == hooks.backup_mod.KIND_SNAPSHOT


class TestRunOnceCancellation:
    """A cancel during the backup is audited as ``cancelled`` and re-raised, so
    teardown stops the loop cleanly AND leaves a trail that it was interrupted."""

    def test_cancelled_backup_is_audited_and_reraised(self):
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
            mock.patch.object(hooks.aws_consent, "refuse_and_log", AsyncMock(return_value=True)),
            mock.patch.object(hooks.storage_mod, "find_drive", return_value="kirocrew-drive-abc"),
            mock.patch.object(
                hooks.backup_mod,
                "run_snapshot_backup",
                side_effect=asyncio.CancelledError(),
            ),
            mock.patch.object(hooks, "_audit") as audit,
        ):
            with pytest.raises(asyncio.CancelledError):
                _run(hooks._run_once())
        outcomes = [c.args[2] for c in audit.call_args_list]
        # "invoked" fired before the cancel, then "cancelled" -- never "failed",
        # because a cancel is not an error to swallow.
        assert outcomes == ["invoked", "cancelled"]


class TestLoopSupervisor:
    """The while-True supervisor: it swallows a raising ``_run_once`` (a bad
    night must not kill the worker) but propagates a cancel to exit teardown."""

    def test_loop_swallows_run_once_error_then_sleeps(self):
        # First pass raises (swallowed), so control reaches the sleep; we cancel
        # AT the sleep to break the otherwise-infinite loop deterministically.
        # Reaching the sleep is the proof the error was swallowed, not raised.
        calls = {"n": 0}

        async def _boom() -> None:
            calls["n"] += 1
            raise RuntimeError("bad night")

        async def _drive() -> None:
            with (
                mock.patch.object(hooks, "_run_once", side_effect=_boom),
                mock.patch.object(
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
            with mock.patch.object(hooks, "_run_once", side_effect=asyncio.CancelledError()):
                with pytest.raises(asyncio.CancelledError):
                    await hooks._loop()

        _run(_drive())


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
                mock.patch.object(hooks.backup_mod, "clear_stop") as clear,
            ):
                await hooks.on_startup(None)
                assert hooks._task is not None
                clear.assert_called_once()
                # Do not let the created task outlive the test.
                await hooks.on_shutdown(None)

        with mock.patch.object(hooks.backup_mod, "signal_stop"):
            _run(_drive())
        assert hooks._task is None

    def test_on_startup_is_idempotent_while_running(self):
        # A second enable while the worker is live must be a no-op: it must NOT
        # spawn a second loop or re-clear the stop, or an enable/disable/enable
        # cycle could leave two workers or lose a teardown signal.
        async def _drive() -> None:
            hooks._task = None
            with mock.patch.object(hooks, "_loop", _never):
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
            with mock.patch.object(hooks, "_loop", _never):
                with mock.patch.object(hooks.backup_mod, "clear_stop"):
                    await hooks.on_startup(None)
            task = hooks._task
            with mock.patch.object(hooks.backup_mod, "signal_stop") as signal:
                await hooks.on_shutdown(None)
            signal.assert_called_once()
            # Let the just-cancelled task settle: the cancel is a request until
            # the loop runs it, so awaiting is what proves it actually stops.
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


class TestNightlyOutcomeIsToldApart:
    """A nightly that sent nothing must not be recorded as a push.

    ``run_snapshot_backup`` now returns a record whose ``uploaded`` is False when the
    source tree has not changed since the archive already in the drive. The audit trail
    and the log line are where an operator actually reads what happened, so recording
    that run as ``succeeded`` with "backup pushed: <key>" would make a night on which no
    bytes left indistinguishable from an ordinary upload -- against a key that really is
    in the drive, so nothing downstream looks wrong either.

    Both directions, because an outcome field that always reports one value is not
    telling anything apart.
    """

    @staticmethod
    def _drive(record: dict):
        """Run one nightly with every precondition satisfied, returning the audit mock."""
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
            mock.patch.object(hooks.aws_consent, "refuse_and_log", AsyncMock(return_value=True)),
            mock.patch.object(
                hooks.storage_mod, "find_drive", return_value="kirocrew-drive-abc123def456"
            ),
            mock.patch.object(hooks.backup_mod, "run_snapshot_backup", return_value=record),
            mock.patch.object(hooks, "_audit") as audit,
        ):
            _run(hooks._run_once())
        return audit

    def test_an_unchanged_night_is_audited_as_unchanged_not_succeeded(self):
        audit = self._drive(
            {"key": "snapshots/x.tar.gz", "uploaded": False, "bytes": 10, "tree": "t1"}
        )
        outcomes = [c.args[2] for c in audit.call_args_list]
        assert "unchanged" in outcomes
        assert "succeeded" not in outcomes
        # The key is still named: it is the archive the drive holds for this kind, which
        # is what makes the record useful rather than merely honest.
        keys = [c.args[1] for c in audit.call_args_list]
        assert "snapshots/x.tar.gz" in keys

    def test_a_real_upload_is_still_audited_as_succeeded(self):
        audit = self._drive({"key": "snapshots/x.tar.gz", "uploaded": True, "bytes": 10})
        outcomes = [c.args[2] for c in audit.call_args_list]
        assert "succeeded" in outcomes
        assert "unchanged" not in outcomes

    def test_a_record_without_the_field_is_treated_as_a_push(self):
        # A record from a path that does not set `uploaded` at all must keep the old
        # meaning rather than silently becoming "unchanged" -- the branch tests
        # `is False`, so only an explicit skip takes the new wording.
        audit = self._drive({"key": "snapshots/x.tar.gz"})
        outcomes = [c.args[2] for c in audit.call_args_list]
        assert "succeeded" in outcomes
        assert "unchanged" not in outcomes


class TestFailedAttemptIsRecorded:
    """The loop's failure handlers now WRITE, not only audit.

    Before this the two halves had different fates: the audit went to SEL, where a
    human reads it after the event, and nothing at all went to state, where the
    due-check reads it on the next wake. So a nightly failing deterministically
    produced a growing pile of ``failed`` audit records and a due-check that could
    not see any of them, and re-attempted every half hour indefinitely.

    State is isolated per test, and the recorder is the REAL one: patching it out
    would leave these tests asserting that a mock was called, which cannot show that
    the due-check the loop actually reads has anything to read.
    """

    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hooks.backup_mod, "_state_path", lambda: tmp_path / "backup.json")
        yield

    @contextlib.contextmanager
    def _resolved(self):
        """The guards up to the due-check, all passed.

        A context manager rather than a tuple of patches: a parenthesized ``with``
        cannot star-unpack, and the alternative -- returning the tuple and entering
        it by hand -- would put an ``ExitStack`` in every test for no gain.
        """
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
            mock.patch.object(hooks.aws_consent, "refuse_and_log", AsyncMock(return_value=True)),
        ):
            yield

    def test_a_failing_push_records_the_attempt_and_withholds_the_next_wake(self):
        # THE regression, driven through the loop rather than through the recorder,
        # so it covers the wiring as well as the predicate: a deterministic fault
        # must leave the account NOT due on the wake that follows. With nothing
        # recorded this assertion reads True, which is the loop re-attempting every
        # half hour for as long as the fault lasts.
        hooks.backup_mod.set_nightly(ACCOUNT, True)
        for _ in range(2):
            with (
                self._resolved(),
                mock.patch.object(hooks.storage_mod, "find_drive", return_value="drive-abc"),
                mock.patch.object(hooks.backup_mod, "other_install_ids", return_value=[]),
                mock.patch.object(
                    hooks.backup_mod,
                    "run_snapshot_backup",
                    side_effect=RuntimeError("snapshot build failed (rc=2)"),
                ),
            ):
                _run(hooks._run_once())
        recorded = hooks.backup_mod.nightly_failures(ACCOUNT)
        assert recorded[hooks.backup_mod.KIND_SNAPSHOT]["consecutive"] == 2
        # The error text is carried, so an operator reading the record learns WHAT
        # keeps failing rather than only that something does.
        assert "rc=2" in recorded[hooks.backup_mod.KIND_SNAPSHOT]["error"]
        assert hooks.backup_mod.due_for_nightly(ACCOUNT) is False

    def test_the_failure_is_audited_and_recorded_together(self):
        # One helper writes both, so the backoff cannot depend on which way the run
        # broke. Asserting the pair -- not just the write -- is what pins them as
        # one fact rather than two that happen to fire today.
        hooks.backup_mod.set_nightly(ACCOUNT, True)
        with (
            self._resolved(),
            mock.patch.object(hooks.storage_mod, "find_drive", return_value="drive-abc"),
            mock.patch.object(hooks.backup_mod, "other_install_ids", return_value=[]),
            mock.patch.object(
                hooks.backup_mod, "run_snapshot_backup", side_effect=RuntimeError("eio")
            ),
            mock.patch.object(hooks, "_audit") as audit,
        ):
            _run(hooks._run_once())
        failed = [c for c in audit.call_args_list if c.args[2] == "failed"]
        assert len(failed) == 1
        assert failed[0].args[1] == "backup/snapshots"
        recorded = hooks.backup_mod.nightly_failures(ACCOUNT)
        assert recorded[hooks.backup_mod.KIND_SNAPSHOT]["consecutive"] == 1

    def test_a_cancel_records_nothing(self):
        # A cancelled attempt is teardown -- the owner disabled the app, or the
        # gateway is stopping -- not a fault. Counting it would let a clean shutdown
        # push the next night out, and a gateway restarted often enough would keep
        # the nightly permanently backed off without anything ever having failed.
        hooks.backup_mod.set_nightly(ACCOUNT, True)
        with (
            self._resolved(),
            mock.patch.object(hooks.storage_mod, "find_drive", return_value="drive-abc"),
            mock.patch.object(hooks.backup_mod, "other_install_ids", return_value=[]),
            mock.patch.object(
                hooks.backup_mod, "run_snapshot_backup", side_effect=asyncio.CancelledError()
            ),
        ):
            with pytest.raises(asyncio.CancelledError):
                _run(hooks._run_once())
        assert hooks.backup_mod.nightly_failures(ACCOUNT) == {}
        assert hooks.backup_mod.due_for_nightly(ACCOUNT) is True

    def test_a_failing_push_on_an_account_with_run_history_still_records(self):
        # Every other case here starts with an EMPTY run slot, so a `_push_nightly` that passed a constant `None` witness
        # instead of reading one would still match (absent vs absent) and every test
        # would pass. With a prior run present, a constant `None` mismatches the real
        # slot and the recorder refuses -- so the backoff would silently never engage
        # for any account that has ever backed up successfully, which is most of them.
        hooks.backup_mod.set_nightly(ACCOUNT, True)
        hooks.backup_mod._record_run(
            ACCOUNT, hooks.backup_mod.KIND_SNAPSHOT, "snapshots/i/old.tar.gz", 5, "fp", "v0"
        )
        assert hooks.backup_mod.nightly_run_witness(ACCOUNT, hooks.backup_mod.KIND_SNAPSHOT)
        with (
            self._resolved(),
            mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=True),
            mock.patch.object(hooks.storage_mod, "find_drive", return_value="drive-abc"),
            mock.patch.object(hooks.backup_mod, "other_install_ids", return_value=[]),
            mock.patch.object(
                hooks.backup_mod, "run_snapshot_backup", side_effect=RuntimeError("eio")
            ),
        ):
            _run(hooks._run_once())
        recorded = hooks.backup_mod.nightly_failures(ACCOUNT)
        assert recorded[hooks.backup_mod.KIND_SNAPSHOT]["consecutive"] == 1

    def test_a_shared_setup_failure_records_every_due_kind(self):
        # The loop has TWO places an attempt can fail, and this is the other one:
        # the setup shared by both kinds, before either push. It already audited per
        # due kind; it must record per due kind too, or a drive lookup that keeps
        # failing is the one failure shape that never backs off.
        with (
            self._resolved(),
            mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=True),
            mock.patch.object(hooks.backup_mod, "due_for_sessions_nightly", return_value=True),
            mock.patch.object(
                hooks.storage_mod, "find_drive", side_effect=RuntimeError("tag lookup denied")
            ),
        ):
            _run(hooks._run_once())
        recorded = hooks.backup_mod.nightly_failures(ACCOUNT)
        assert set(recorded) == {
            hooks.backup_mod.KIND_SNAPSHOT,
            hooks.backup_mod.KIND_SESSIONS,
        }
        assert all(row["consecutive"] == 1 for row in recorded.values())

    def test_a_cancelled_shared_setup_records_nothing(self):
        # The cancel branch of the same handler. It is a separate `except` clause, so
        # it is separately capable of regressing into recording a teardown.
        with (
            self._resolved(),
            mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=True),
            mock.patch.object(
                hooks.storage_mod, "find_drive", side_effect=asyncio.CancelledError()
            ),
        ):
            with pytest.raises(asyncio.CancelledError):
                _run(hooks._run_once())
        assert hooks.backup_mod.nightly_failures(ACCOUNT) == {}

    def test_a_successful_run_after_failures_clears_the_backoff(self):
        # End to end through the loop: the fault clears, the next run completes, and
        # the account is immediately eligible again rather than serving out a wait
        # measured for a fault that is over. The runner calls the REAL `_record_run`,
        # because the clear lives inside it -- a stubbed runner could not show the
        # loop's own success clearing anything.
        hooks.backup_mod.set_nightly(ACCOUNT, True)
        for _ in range(3):
            hooks.backup_mod.record_nightly_failure(
                ACCOUNT,
                hooks.backup_mod.KIND_SNAPSHOT,
                "eio",
                run_witness=hooks.backup_mod.nightly_run_witness(
                    ACCOUNT, hooks.backup_mod.KIND_SNAPSHOT
                ),
            )
        assert hooks.backup_mod.due_for_nightly(ACCOUNT) is False

        def _completing(account, profile, region, bucket, *, caller):
            return hooks.backup_mod._record_run(
                account,
                hooks.backup_mod.KIND_SNAPSHOT,
                "snapshots/i/a.tar.gz",
                9,
                "fp",
                "v1",
            )

        with (
            self._resolved(),
            mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=True),
            mock.patch.object(hooks.storage_mod, "find_drive", return_value="drive-abc"),
            mock.patch.object(hooks.backup_mod, "other_install_ids", return_value=[]),
            mock.patch.object(hooks.backup_mod, "run_snapshot_backup", side_effect=_completing),
        ):
            _run(hooks._run_once())
        assert hooks.backup_mod.nightly_failures(ACCOUNT) == {}
