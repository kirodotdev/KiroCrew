"""Lifecycle hooks — the nightly backup loop.

One background task, started on enable, that wakes every half hour and runs
the snapshot backup when it is due (nightly toggle on AND >23 h since the
last run AND not inside the retry backoff a run of failed attempts earns --
see ``backup.due_for_nightly``). Every AWS-reaching step keeps
the same guards the HTTP path has: consent fails closed (a silent skip plus
a log line, never an unconfirmed charge), and the drive is tag-discovered
per run rather than trusted from memory.

The wake interval is a due-CHECK interval and never a retry interval. A failed
attempt is recorded, by ``_failed_attempt``, so the due-check can tell a fault it
has already met from a first one; without that record the loop re-attempted a
deterministic failure on every wake forever, because recording only successes left
the state file with nothing to distinguish "never ran" from "keeps breaking".

The loop runs against the REGISTRY DEFAULT account only — the same account
the consent card confirms, resolved through the same healthy-first policy, so
the grant it checks names the key it runs under. Multi-account nightly
schedules arrive with the per-account grant store (spec §9).

Several INSTALLS pointed at one account is a different axis and is supported
rather than refused: each writes under its own ``<install>`` prefix, so the loop
records that the drive is shared and proceeds. Making the schedule single-owner
would leave one machine silently un-backed-up, which is the worse failure.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod
from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod
from kiro_crew.apps.builtins.aws_control.backend import storage as storage_mod
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SECS = 30 * 60

_task: asyncio.Task[None] | None = None


def _audit(operation: str, resources: str, outcome: str, *, error: str = "") -> None:
    """SEL record for an UNATTENDED backup step.

    The HTTP handlers get their audit from the dashboard layer; this loop has no
    request, so without this the only unattended S3 mutation in the app would be
    the one operation with no trail. Best-effort by the same rule the handlers
    use: an audit failure must never abort the backup.
    """
    try:
        sel().log_api_access(
            caller="aws-control-nightly",
            operation=f"aws_control.{operation}",
            outcome=outcome,
            source=backup_mod.APP_NAME,
            resources=resources[:200],
            error=error[:200],
        )
    except Exception:
        logger.debug("aws-control nightly SEL audit failed", exc_info=True)


async def _note_shared_drive(profile: str, region: str, bucket: str, account: str) -> None:
    """Record that another install also backs up here. Then carry on.

    A NOTICE, not a gate, and that is a deliberate departure from the reported
    issue's own suggestion that the loop refuse. Refusing would make the drive
    single-owner, and single-owner scheduling means exactly one machine keeps
    getting backed up while the other silently stops -- discovering that at
    restore time is worse than the unattributed pile this change replaces. Once
    the keys carry an install id there is nothing left to collide: two installs
    write to two prefixes, and two machines each keeping their own memory backed
    up is what the owner asked for by pointing both at one account.

    So the value here is EVIDENCE, and specifically evidence a later panel view
    cannot reconstruct. The console's ``others`` count is LIVE: it says what the
    drive looks like when a human opens the page. This record says what the
    UNATTENDED run observed at the moment it spent the owner's money -- and the
    nightly is the one path in this app that spends it with nobody present, so "at
    this run, the drive already held another install's archives" is an audit fact
    about that spend rather than a line for someone to read. The consumer is a human
    after the fact, which is what the whole SEL trail is for.

    ONE list call, against the snapshot prefix only. This loop uploads snapshots
    and nothing else, so sweeping the sessions prefix too would have cost a second
    paid call per scheduled run to answer a question about a run that is not
    happening.

    Never raises. A listing failure must not stop the backup it was only
    annotating; the outcome is one less log line, not a missed nightly.
    """
    try:
        others = await asyncio.to_thread(
            backup_mod.other_install_ids, profile, region, bucket, account=account
        )
    except Exception:
        logger.debug("aws-control nightly: shared-drive check failed", exc_info=True)
        return
    if not others:
        return
    logger.warning(
        "aws-control nightly: %d other install(s) also back up to this drive (%s); "
        "each writes under its own prefix, so this run proceeds -- restore attributes "
        "archives by that prefix",
        len(others),
        ", ".join(others[:5]),
    )
    _audit("backup_shared_drive", f"installs={len(others)}", "invoked")


async def _run_once() -> None:
    """One due-check + backup attempt. Every failure is a log line, not a crash."""
    # Same resolution the consent card and the HTTP handlers use, so the key this
    # unattended loop runs under is the key the grant was recorded for. A raw
    # registry-default read would pick an unhealthy default over the account's
    # working sibling, and then skip on every wake -- silently, since nobody is
    # watching a 03:00 loop. The STRICT variant: with no working key there is
    # nothing to back up, so the loop stops rather than naming one it cannot use.
    # Costs one probe sweep per wake (free STS calls, concurrency-bounded,
    # snapshot-cached); the correct key is worth it.
    resolved = await accounts_mod.resolve_default_account_profile()
    if resolved is None:
        # Covers both "nothing registered" and "the default account has no
        # working key"; the accounts pane is where the difference is visible.
        logger.info("aws-control nightly: no healthy registered key; skipping")
        return
    profile, region = resolved
    # Backup state is keyed per account, so the loop resolves which
    # account the default profile is actually pointing at right now. The snapshot
    # above has a TTL, so this live probe is what the account id may be trusted
    # from -- the same rule the HTTP path's ``_resolve_target`` follows.
    #
    # ``use_cache=False`` is what makes that sentence true, and it is doing more
    # work here than at the HTTP resolver. ``resolve_default_account_profile``
    # reaches ``list_accounts`` -> ``_fold_profile``, which probes every registry
    # entry with the cache ON, so a cached probe here answers from the entry the
    # line above just primed: not a 30-second window but a probe that never runs
    # live at all. A repoint inside it keys ``due_for_nightly``, ``find_drive``
    # and the snapshot record to the wrong account, unattended and with nobody
    # reading a log.
    identity = await aws_consent.probe_identity(profile, region, use_cache=False)
    if not identity.ok or not identity.account:
        logger.info("aws-control nightly: account unresolved; skipping")
        return
    account = identity.account
    # Two independent grants, each read on its own. A wake proceeds when EITHER
    # is due, so a snapshot that already ran today cannot swallow the window the
    # transcripts were authorized for -- and neither bit is ever inferred from
    # the other.
    snapshot_due = await asyncio.to_thread(backup_mod.due_for_nightly, account)
    sessions_due = await asyncio.to_thread(backup_mod.due_for_sessions_nightly, account)
    if not snapshot_due and not sessions_due:
        # Say why when the grant is on and something else is withholding the run.
        # Without this the operator who turned transcripts on, and then turned
        # outbound redaction on, sees a nightly that silently never runs and no
        # statement anywhere of which of their two settings withheld it.
        if await asyncio.to_thread(backup_mod.nightly_sessions_enabled, account):
            gap = await asyncio.to_thread(backup_mod._unattended_sessions_redaction_gap)
            if gap:
                logger.info("aws-control nightly: transcripts withheld -- %s", gap)
        return
    allowed = await aws_consent.refuse_and_log(
        aws_consent.SERVICE_S3, profile=profile, region=region
    )
    if not allowed:
        return  # refuse_and_log already logged + audited
    # The kinds this wake is actually for, derived once. The shared setup below
    # can fail before any push, and a failure there must name the kinds that were
    # due rather than one fixed kind: a transcripts-only wake reaches the same
    # block, so a hardcoded `backup/snapshots` would record a snapshot that was
    # never due, and the SEL trail is append-only. Deriving the list here also
    # means the subjects a failure is audited against and the kinds actually
    # pushed below cannot drift apart -- they are the same list.
    due_kinds = [
        kind
        for kind, is_due in (
            (backup_mod.KIND_SNAPSHOT, snapshot_due),
            (backup_mod.KIND_SESSIONS, sessions_due),
        )
        if is_due
    ]
    # The run slot's identity per due kind, read BEFORE anything is attempted. This is
    # the window a failure write has to be judged against: a run recorded inside it is
    # positive evidence that backups are reaching the drive, and the recorder refuses to
    # write a failure over it. Captured here rather than inside the handler because by
    # then the window has closed.
    witnesses = {
        kind: await asyncio.to_thread(backup_mod.nightly_run_witness, account, kind)
        for kind in due_kinds
    }
    try:
        bucket = await asyncio.to_thread(storage_mod.find_drive, profile, region, account=account)
        if not bucket:
            logger.info("aws-control nightly: no drive bucket yet; skipping")
            return
        await _note_shared_drive(profile, region, bucket, account)
    except asyncio.CancelledError:
        for kind in due_kinds:
            _audit("backup_nightly", _audit_subject(kind), "cancelled")
        raise
    except Exception as exc:
        for kind in due_kinds:
            await _failed_attempt(kind, account, exc, witnesses[kind])
        logger.warning("aws-control nightly backup failed", exc_info=True)
        return
    for kind in due_kinds:
        await _push_nightly(kind, account, profile, region, bucket)


def _audit_subject(kind: str) -> str:
    """The SEL subject for one backup kind.

    One spelling, used by the shared setup's failure handlers and by
    :func:`_push_nightly` alike. Two copies of this expression is how a run's
    setup failure and its push end up filed under different subjects for the
    same kind, which is exactly the misattribution this exists to prevent.
    """
    return f"backup/{backup_mod.KIND_SUBPATHS[kind]}"


async def _failed_attempt(
    kind: str,
    account: str,
    exc: BaseException,
    run_witness: Any,
) -> None:
    """Audit one kind's failed unattended attempt AND record it. One spelling.

    The two live together because they are two halves of the same fact, and they had
    different fates before this existed: the audit went to SEL, where a human reads
    it after the event, and nothing at all went to state, where the loop reads it on
    the next wake. So a nightly failing deterministically produced a growing pile of
    ``failed`` audit records and a due-check that could not see any of them, and
    re-attempted every half hour indefinitely -- staging the whole data home into a
    fresh temporary directory each time, and burying every other warning in the
    gateway log at that cadence.

    ONE helper rather than the same pair written at each site, for the reason
    :func:`_audit_subject` itself gives: this module has two places a nightly attempt
    can fail -- the shared setup, and each kind's own push -- and two copies is how
    one of them ends up auditing a failure it never counted, or counting one it never
    audited. The backoff would then depend on WHICH way the run broke.

    ``run_witness`` is ``backup.nightly_run_witness`` read BEFORE the attempt began, and
    the recorder refuses to write when the run slot has moved since. Every caller reads
    it at the top of its own attempt rather than here, because by the time this helper
    runs the attempt is already over and the window it has to witness has closed.

    Cancellation deliberately does NOT come here. A cancelled attempt is teardown,
    not a fault: the owner disabled the app or the gateway is stopping, and counting
    that as a failed attempt would have a clean shutdown push the next night out.
    Those branches keep their own audit call and record nothing.
    """
    _audit("backup_nightly", _audit_subject(kind), "failed", error=str(exc))
    # Off the loop, like every other state read in this module: the recorder takes
    # the sidecar lock and rewrites the document, which is real blocking file I/O.
    await asyncio.to_thread(
        functools.partial(
            backup_mod.record_nightly_failure,
            account,
            kind,
            str(exc),
            run_witness=run_witness,
        )
    )


async def _push_nightly(kind: str, account: str, profile: str, region: str, bucket: str) -> None:
    """Push one due nightly kind, audited around the call.

    Each kind gets its OWN try/except rather than sharing one. The two payloads
    have nothing in common but the drive they land in, so a snapshot that fails
    must not cost the transcripts their window, and the reverse. A shared handler
    would turn one failure into two skipped nights.
    """
    subject = _audit_subject(kind)
    # Read before the attempt, for the reason `_run_once` gives at its own capture:
    # the window this witnesses is the attempt's whole duration, so it cannot be
    # read from the handler after the attempt has ended.
    run_witness = await asyncio.to_thread(backup_mod.nightly_run_witness, account, kind)
    runner = (
        backup_mod.run_snapshot_backup
        if kind == backup_mod.KIND_SNAPSHOT
        else backup_mod.run_sessions_backup
    )
    try:
        # The nightly path never touches an HTTP handler, so the audit the
        # dashboard layer adds to every owner-driven mutation is simply absent
        # here -- an unattended export would leave no SEL trace of having run,
        # succeeded or failed. Emit the same three-part record the handlers do,
        # around the call, so the trail does not depend on who triggered it.
        _audit("backup_nightly", subject, "invoked")
        record = await asyncio.to_thread(
            functools.partial(
                runner,
                account,
                profile,
                region,
                bucket,
                # Nobody is at the keyboard at 03:00. An upload refused here must
                # not be recorded against the dashboard owner, which is what a
                # hardcoded interactive caller inside the gate would have done.
                caller=backup_mod.CALLER_SCHEDULED,
            )
        )
        if record.get("uploaded") is False:
            # A run that found the tree unchanged sent nothing, so recording it as a
            # push would put a SEL entry and a log line against a key no bytes reached
            # tonight -- indistinguishable, to whoever reads the audit trail, from an
            # ordinary upload. The key is still named because it is the archive this
            # run stood on: it is what the drive still holds for this kind.
            _audit("backup_nightly", str(record.get("key", "")), "unchanged")
            logger.info(
                "aws-control nightly backup: tree unchanged since %s, nothing uploaded",
                record.get("key", ""),
            )
        else:
            _audit("backup_nightly", str(record.get("key", "")), "succeeded")
            logger.info("aws-control nightly backup pushed: %s", record.get("key", ""))
    except asyncio.CancelledError:
        _audit("backup_nightly", subject, "cancelled")
        raise
    except Exception as exc:
        await _failed_attempt(kind, account, exc, run_witness)
        logger.warning("aws-control nightly backup failed: %s", kind, exc_info=True)


async def _loop() -> None:
    while True:
        try:
            await _run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("aws-control nightly loop error", exc_info=True)
        await asyncio.sleep(_CHECK_INTERVAL_SECS)


async def _register_job_runners(ctx: Any) -> None:
    """Bind the backup kinds to their runners, then resolve any dead run.

    Registration is the SDK's contract: a kind is bound to its callable ONCE, at
    app init, and ``start`` then names only the kind. That is what lets the
    browser and the reconciliation pass address a run without holding a Python
    callable. It happens before the nightly-task guard below because a re-enable
    builds a FRESH ``AppContext`` -- and therefore a fresh ``JobSDK`` with an
    empty runner table -- so skipping it on the "already running" path would
    leave an app whose kinds have no runners.

    ``cancellable`` is left at its default of False. Neither backup runner polls
    ``handle.cancelled``: the only stop signal they honour is the teardown event
    ``_STOP``, checked in ``_authorize_upload``, which is not a cancel checkpoint.
    The SDK cannot verify the assertion, so claiming True here would put a Cancel
    button in front of the owner that does nothing. The UI hides it instead.

    The reconcile call is deliberate and is NOT redundant with the gateway's.
    ``reconcile_all()`` runs once after the WHOLE enable loop, so on the startup
    path there is a window -- every app enabled after this one -- in which
    ``_jobs/active`` would serve a run left behind by a process that is gone. The
    backup UI adopts an in-flight record on mount, so that window is precisely
    when it would show a phantom "running" for work nothing can finish. Calling
    it here shortens the window to this app's own startup, and it is safe to run
    twice: a terminal record is skipped (``job_sdk.py:786``), so the later pass
    finds nothing left to do.
    """
    sdk = getattr(ctx, "job", None)
    if sdk is None:
        # Granted-but-absent is the app's to report, not to assume away: without
        # the `jobs` permission the context carries no SDK, and a backup start
        # would fail at the route with no explanation of why.
        logger.warning(
            "aws-control: no job runtime on the app context; "
            "backups cannot run (is the 'jobs' permission declared?)"
        )
        return
    for kind in backup_mod.JOB_KINDS:
        sdk.register(kind, backup_mod.make_job_runner(sdk, kind))
    try:
        interrupted = await asyncio.to_thread(sdk.reconcile)
    except Exception:  # noqa: BLE001 — a bad run store must not block enable
        logger.warning("aws-control: job reconciliation failed", exc_info=True)
        return
    if interrupted:
        logger.info("aws-control: resolved %d interrupted backup run(s)", interrupted)


async def on_startup(ctx: Any) -> None:
    """Register the backup runners, then start the nightly loop.

    Idempotent across enable/disable cycles.
    """
    global _task
    # Before the guard below: a re-enable brings a new JobSDK that has no runners.
    await _register_job_runners(ctx)
    if _task is not None and not _task.done():
        return
    # Re-enabling clears a stop left by a previous teardown, so an enable/disable
    # /enable cycle does not leave the worker permanently refusing to upload.
    backup_mod.clear_stop()
    _task = asyncio.get_running_loop().create_task(_loop())


async def on_shutdown(ctx: Any) -> None:  # noqa: ARG001 — kept for the hook ABI
    """Stop the loop, and stop a worker that has not begun uploading yet.

    ``_task.cancel()`` alone only unblocks the ``await``: a
    ``asyncio.to_thread`` worker is a real thread and Python cannot kill one, so
    a snapshot already streaming to S3 runs to completion regardless of what the
    hook does. The stop EVENT closes the part that is closeable -- the worker
    re-checks authorization immediately before ``put_file``, and that check now
    also refuses once teardown has been signalled, so a backup still building its
    archive when the owner disables the app never starts its upload.

    The residual is one in-flight object: an ``aws s3 cp`` already mid-stream
    finishes, into the owner's own bucket, and the SEL record above says it did.
    Revoking that would mean tracking and terminating the CLI subprocess itself,
    which this hook does not do.
    """
    global _task
    backup_mod.signal_stop()
    if _task is not None:
        _task.cancel()
        _task = None
