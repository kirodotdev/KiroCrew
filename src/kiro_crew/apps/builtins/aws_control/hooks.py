"""Lifecycle hooks — the nightly backup loop.

One background task, started on enable, that wakes every half hour and runs
the snapshot backup when it is due (nightly toggle on AND >23 h since the
last run — see ``backup.due_for_nightly``). Every AWS-reaching step keeps
the same guards the HTTP path has: consent fails closed (a silent skip plus
a log line, never an unconfirmed charge), and the drive is tag-discovered
per run rather than trusted from memory.

The loop runs against the REGISTRY DEFAULT account only — the same account
the consent card confirms. Multi-account nightly schedules arrive with the
per-account grant store (spec §9).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod
from kiro_crew.apps.builtins.aws_control.backend import storage as storage_mod
from kiro_crew.deploy import profiles as deploy_profiles
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


async def _run_once() -> None:
    """One due-check + backup attempt. Every failure is a log line, not a crash."""
    resolved = await asyncio.to_thread(deploy_profiles.resolve_profile, "")
    if resolved is None:
        logger.info("aws-control nightly: no registered profile; skipping")
        return
    profile, region = resolved
    # Backup state is keyed per account, so the loop resolves which
    # account the default profile is actually pointing at right now.
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
        # The nightly path never touches an HTTP handler, so the audit the
        # dashboard layer adds to every owner-driven mutation is simply absent
        # here -- an unattended export would leave no SEL trace of having run,
        # succeeded or failed. Emit the same three-part record the handlers do,
        # around the call, so the trail does not depend on who triggered it.
        _audit("backup_nightly", "backup/snapshots", "invoked")
        record = await asyncio.to_thread(
            backup_mod.run_snapshot_backup, account, profile, region, bucket
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


async def on_startup(ctx: Any) -> None:  # noqa: ARG001 — kept for the hook ABI
    """Start the nightly loop. Idempotent across enable/disable cycles."""
    global _task
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
    which is the same containment work tracked in #5430.
    """
    global _task
    backup_mod.signal_stop()
    if _task is not None:
        _task.cancel()
        _task = None
