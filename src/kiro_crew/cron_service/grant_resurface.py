"""Resurface a pending re-approval request when a script job's grant goes stale.

An approved grant is pinned to the code it was approved over, so a later edit to the
script body, its message or its delivery controls makes every run refuse the grant
(fail closed) until the operator approves it again. Approval only ever promotes a
PENDING request (``cron_secret_request`` -> Schedule > job > Secrets), and the
refusal by itself files none: the job was left holding a dead active grant with
nothing on the page to approve, so the only recovery was revoke-and-re-request.

:func:`resurface_stale_grant` closes that gap. After a run is refused for a stale
pin, the scheduler files a pending request for the SAME mapping the operator already
approved, pinned to the job's CURRENT code. It grants nothing: the request goes
through the one verified promotion path, which re-checks the pending pin, the source
digest the operator reviewed and the vault names before any value can flow.

It never displaces a request it did not raise for this grant. A pending request
whose mapping differs from the active one is the agent's own ask and stands; one
whose mapping matches is replaced only when its pin does not cover the current
code, since approving it would be refused as ``code_changed`` anyway. Each code
revision is asked about once (``secret_env_resurfaced_pin``), so an operator who
denies the request is not asked again on every refused run; the marker applies
only once no request is pending, so a live same-mapping request pinned to other
code is still repinned. Approving a grant
clears that marker, so a later revision is asked about afresh even when its
code matches one asked about before.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from kiro_crew import cron_script
from kiro_crew.cron_service.store import (
    CronPendingMismatch,
    CronStoreBusy,
    CronStoreUnreadable,
)

if TYPE_CHECKING:
    from kiro_crew.cron import CronService
    from kiro_crew.cron_service.model import CronJob

logger = logging.getLogger("kiro_crew.cron")


def _is_str_mapping(value: object) -> bool:
    """True when ``value`` is a dict of ``str`` -> ``str`` (the store is agent-writable)."""
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    )


def _current_pending_pin(job: CronJob) -> str:
    """The ``pending``-domain pin of the job's active mapping over its current code."""
    return cron_script.compute_secret_env_pin(
        job.script,
        job.command,
        job.message,
        job_id=job.id,
        grant=dict(job.secret_env),
        domain="pending",
        delivery=cron_script.delivery_fingerprint(
            job.session_key, job.silent, job.channel or "", job.thread_ts or ""
        ),
    )


async def resurface_stale_grant(svc: CronService, job_id: str, refused_pin: str) -> bool:
    """File a pending re-approval request for a job whose grant was refused as stale.

    ``refused_pin`` is the active pin the refused run verified against. The write is
    a compare-and-swap on it, so an approval or revocation that landed after the
    refusal wins and nothing is filed. Returns whether a request was filed. Never
    raises: the run has already failed closed, and a request that could not be filed
    leaves the job exactly as it was before this ran.
    """
    try:
        job = await svc.get_job_async(job_id)
        # An unreadable store loads as an empty job list without raising, so a
        # miss alone is not proof the job is gone; the probe reads the latch the
        # read just refreshed and lands in the store-fault arm below.
        svc.raise_if_store_unreadable()
        if job is None or not job.script:
            return False
        active = job.secret_env
        pending = job.secret_env_pending
        if not active or not _is_str_mapping(active) or not _is_str_mapping(pending):
            return False
        if job.secret_env_pin != refused_pin:
            return False
        if pending and pending != active:
            return False
        pin = await asyncio.to_thread(_current_pending_pin, job)
        if pending and job.secret_env_pending_pin == pin:
            return False
        # The once-per-revision marker only spares a revision whose request was
        # decided (a denial clears the pending record). A live same-mapping request
        # pinned to other code is repinned even on an already-asked revision:
        # approving it as it stands would be refused as ``code_changed``.
        if not pending and pin == job.secret_env_resurfaced_pin:
            return False
        updated = await svc.update_job_async(
            job_id,
            secret_env_pending=dict(active),
            secret_env_pending_pin=pin,
            secret_env_pending_ts=time.time(),
            secret_env_resurfaced_pin=pin,
            expect_secret_env_pending=dict(pending),
            expect_secret_env_pending_ts=job.secret_env_pending_ts,
            expect_secret_env=dict(active),
            expect_secret_env_pin=refused_pin,
        )
        if updated is None:
            # The job was removed while the pin was computed: nothing landed.
            return False
    except CronPendingMismatch:
        # A decision or a newer request landed between the read and the write.
        return False
    except (
        CronStoreBusy,
        CronStoreUnreadable,
        ValueError,
        OSError,
        RuntimeError,
    ):
        logger.warning("Could not file the re-approval request for job %s", job_id, exc_info=True)
        return False
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    try:
        seams.sel.sel().log_api_access(
            caller="cron",
            operation="cron.secret_request",
            outcome="allowed",
            source="cron",
            resources=f"job_id={job_id}:{','.join(sorted(active))}:resurfaced",
        )
    except Exception:
        logger.debug("SEL logging failed for resurfaced secret request", exc_info=True)
    logger.info("Cron %s: grant stale; filed a pending re-approval request", job_id)
    return True
