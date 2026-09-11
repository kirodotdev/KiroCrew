"""Lifecycle hooks — the nightly backup loop.

One background task, started on enable, that wakes every half hour and runs
the snapshot backup when it is due (nightly toggle on AND >23 h since the
last run — see ``backup.due_for_nightly``). Every AWS-reaching step keeps
the same guards the HTTP path has: consent fails closed (a silent skip plus
a log line, never an unconfirmed charge), and the drive is tag-discovered
per run rather than trusted from memory.

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
    identity = await aws_consent.probe_identity(profile, region)
    if not identity.ok or not identity.account:
        logger.info("aws-control nightly: account unresolved; skipping")
        return
    account = identity.account
    if not await asyncio.to_thread(backup_mod.due_for_nightly, account):
        return
    allowed = await aws_consent.refuse_and_log(
        aws_consent.SERVICE_S3, profile=profile, region=region
    )
    if not allowed:
        return  # refuse_and_log already logged + audited
    try:
        bucket = await asyncio.to_thread(storage_mod.find_drive, profile, region, account=account)
        if not bucket:
            logger.info("aws-control nightly: no drive bucket yet; skipping")
            return
        await _note_shared_drive(profile, region, bucket, account)
        # The nightly path never touches an HTTP handler, so the audit the
        # dashboard layer adds to every owner-driven mutation is simply absent
        # here -- an unattended export would leave no SEL trace of having run,
        # succeeded or failed. Emit the same three-part record the handlers do,
        # around the call, so the trail does not depend on who triggered it.
        _audit("backup_nightly", "backup/snapshots", "invoked")
        record = await asyncio.to_thread(
            functools.partial(
                backup_mod.run_snapshot_backup,
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
        _audit("backup_nightly", str(record.get("key", "")), "succeeded")
        logger.info("aws-control nightly backup pushed: %s", record.get("key", ""))
    except asyncio.CancelledError:
        _audit("backup_nightly", "backup/snapshots", "cancelled")
        raise
    except Exception as exc:
        _audit("backup_nightly", "backup/snapshots", "failed", error=str(exc))
        logger.warning("aws-control nightly backup failed", exc_info=True)


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
