"""Lifecycle hooks — the nightly backup loop.

One background task, started on enable, that wakes every half hour and CLAIMS
the snapshot backup through the Job SDK when it is due (nightly toggle on AND
>23 h since the last run AND not inside the retry backoff -- see
``backup.due_for_nightly``). The due-check keeps the same guards the HTTP path
has: consent fails closed (a silent skip plus a log line, never an unconfirmed
charge), and the account is resolved through the same healthy-first policy.

Claiming the run through ``sdk.start_async`` rather than running the backup on
a bare thread is what makes a nightly a RECORDED run: it dedupes against a
manual click for the same account (both share the SDK's ``(kind, dedupe_key)``
index, so they do not both do the paid upload), and it is visible to the Backup
row, which derives its busy state from SDK records. The shared runner re-resolves
the drive per run and applies the unattended gates, because the loop names
``CALLER_SCHEDULED`` in the start's ``params``.

The wake interval is a due-CHECK interval and never a retry interval.

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
import logging
from typing import Any

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod
from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod
from kiro_crew.apps.job_sdk import JobError, UnknownJobKind, get_sdk

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SECS = 30 * 60


def _nightly_setup_failure(account: str, kind: str, exc: BaseException, witness: Any) -> None:
    """Record and audit one scheduled wake that failed BEFORE a run was claimed.

    The loop resolves the drive itself, before it claims any run (see
    :func:`_run_once`), so a lookup that *raises* there never reaches the runner's
    own ``failed`` handler -- it would escape the wake into :func:`_loop`'s
    catch-all warning with no backoff and no audit, the one failure shape that
    re-attempts every wake forever. This is the loop-side equivalent of the
    runner's post-claim ``except`` branch, and it is deliberately the SAME two
    writes, through the SAME helpers the runner uses: the ``failed`` SEL audit via
    :func:`backup._nightly_audit` under :func:`backup._nightly_subject` -- so a
    setup failure and a push failure for one kind never land under different
    subjects or through a second copy of the audit call -- and
    :func:`backup.record_nightly_failure`, so ``due_for_nightly`` can tell a fault
    it has already met from a new one.

    ``witness`` is :func:`backup.nightly_run_witness` read BEFORE the lookup, so the
    recorder refuses to write over a run slot that moved since. Never raises: it is
    already on a failed-wake path, and the audit is best-effort.
    """
    backup_mod._nightly_audit(
        "backup_nightly", backup_mod._nightly_subject(kind), "failed", error=str(exc)
    )
    backup_mod.record_nightly_failure(account, kind, str(exc), run_witness=witness)


def _nightly_setup_cancelled(kind: str) -> None:
    """Audit one scheduled wake that was CANCELLED before a run was claimed.

    A cancel is teardown, not a fault: unlike :func:`_nightly_setup_failure` this
    writes only the ``cancelled`` SEL record and does NOT back off, so an
    interrupted wake leaves a trail that it was stopped without poisoning the next
    one. It mirrors the per-kind ``cancelled`` audit the loop-side setup emitted
    before the backup moved to the Job SDK, kept so teardown during the drive
    lookup stays visible in the SEL trail -- through the same
    :func:`backup._nightly_audit` the fault path uses, so there is one audit call,
    not a second copy. Never raises: the audit is best-effort and the
    ``CancelledError`` must propagate regardless.
    """
    backup_mod._nightly_audit("backup_nightly", backup_mod._nightly_subject(kind), "cancelled")


_task: asyncio.Task[None] | None = None


async def _run_once(sdk: Any) -> None:
    """One due-check + claim of each due backup through the Job SDK.

    Takes the SDK rather than reaching for it, so the due-check and claim stay
    testable without a live app context. The loop fetches the live SDK each wake
    (see :func:`_loop`); a direct caller passes one in.
    """
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
    # The kinds this wake is actually for. A transcripts-only wake must not claim
    # a snapshot run, so each due bit gates its own kind independently.
    due_kinds = [
        kind
        for kind, is_due in (
            (backup_mod.KIND_SNAPSHOT, snapshot_due),
            (backup_mod.KIND_SESSIONS, sessions_due),
        )
        if is_due
    ]
    # Check the drive BEFORE claiming any run, as the loop did before this change.
    # The drive is tag-discovered per wake, not trusted from memory; until one
    # exists there is nowhere to push, so no run is claimed. Claiming one anyway
    # would leave a `done` SDK run on the Backup row every 30 minutes for a backup
    # that uploaded nothing -- and that newest `done` run would clear an owner's
    # earlier `lastFailed` (e.g. their own "no drive yet" click). A missing drive
    # is "nothing to attempt yet", so the wake simply returns, recording nothing.
    #
    # A drive lookup that RAISES is a different fact from one that returns no drive.
    # It is a fault, and because it happens here -- before any run is claimed -- the
    # runner's own failure handler never sees it. Left unhandled it escapes into
    # `_loop`'s catch-all warning with no backoff and no audit, so a deterministically
    # failing lookup re-attempts every wake forever (the exact shape
    # `record_nightly_failure` exists to close). So each due kind's run slot is
    # witnessed BEFORE the lookup -- the window a failure write is judged against,
    # which has closed by the time the handler runs -- and a raise records+audits a
    # `failed` attempt per due kind, matching the runner's post-claim branch. A
    # CancelledError is teardown, not a fault, so it audits a `cancelled` trail per
    # due kind -- so the interrupt stays visible in the SEL record -- but does NOT
    # back off.
    witnesses = {
        kind: await asyncio.to_thread(backup_mod.nightly_run_witness, account, kind)
        for kind in due_kinds
    }
    try:
        bucket = await asyncio.to_thread(
            backup_mod.storage.find_drive, profile, region, account=account
        )
    except asyncio.CancelledError:
        for kind in due_kinds:
            _nightly_setup_cancelled(kind)
        raise
    except Exception as exc:
        for kind in due_kinds:
            await asyncio.to_thread(_nightly_setup_failure, account, kind, exc, witnesses[kind])
        logger.warning("aws-control nightly: drive lookup failed", exc_info=True)
        return
    if not bucket:
        logger.info("aws-control nightly: no drive yet; skipping")
        return
    # Claim each due kind through the Job SDK, the SAME call the owner click makes,
    # rather than running the backup on a bare thread. Claiming through the SDK
    # is what makes the run recorded and dedupable:
    #
    #   * dedupe_key=account makes a nightly run and a manual click for one account
    #     adopt one another through the SDK's (kind, dedupe_key) index, so they do
    #     not both do the paid upload.
    #   * a claimed run is what the Backup row reads, so a nightly is visible like
    #     any other run instead of leaving the row idle while it uploads.
    #   * params names CALLER_SCHEDULED so the shared runner applies the
    #     unattended-only gates and attributes the spend to the schedule, not to a
    #     dashboard owner who is not present.
    #
    # Fire-and-forget: start_async returns once the run is claimed, and the SDK
    # record carries the outcome (done / failed) that the row and the audit trail
    # read. A refused start (mid-teardown, or no runner registered) is a logged
    # skip, not a crash -- the next wake tries again.
    for kind in due_kinds:
        try:
            await sdk.start_async(
                kind,
                dedupe_key=account,
                params={
                    backup_mod.JOB_PARAM_CALLER: backup_mod.CALLER_SCHEDULED,
                    # The profile/region this loop JUST verified live
                    # (`probe_identity(use_cache=False)` above). The runner
                    # re-resolves per run, but its sync cache returns None once the
                    # snapshot is past the TTL -- and `start_async` is
                    # fire-and-forget, so the runner resolves later than this check.
                    # Carrying the verified pair lets the scheduled path fall back to
                    # it rather than audit a just-verified nightly as failed. The
                    # live account re-check in `_authorize_upload` still guards wrong
                    # accounts.
                    backup_mod.JOB_PARAM_PROFILE: profile,
                    backup_mod.JOB_PARAM_REGION: region,
                },
            )
        except (JobError, UnknownJobKind):
            logger.warning("aws-control nightly: could not claim %s run", kind, exc_info=True)


async def _loop() -> None:
    while True:
        try:
            # Fetch the LIVE SDK each wake rather than capturing one: a re-enable
            # builds a fresh AppContext and therefore a fresh JobSDK, and the loop
            # outlives that cycle. `get_sdk` returns whichever SDK is registered
            # under this app right now (the same one the route reads), so the loop
            # never holds a stale handle. Absent means the `jobs` permission is not
            # granted; `_register_job_runners` already logged that, so here it is a
            # quiet skip until the next wake.
            sdk = get_sdk(backup_mod.APP_NAME)
            if sdk is not None:
                await _run_once(sdk)
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
