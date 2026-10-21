"""Approving, denying and revoking a script job's secret grant.

``PUT /api/crons/{job_id}/secrets`` and the one promotion path an approval of a
pending secret request runs. Every decision is audit-or-deny: its SEL record is
written before the grant changes, and a decision whose record cannot be written
is refused.

Served through :mod:`kiro_crew.dashboard.handlers.cron`, which callers and tests import and patch; this module
never imports it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any

from aiohttp import web

from kiro_crew.config.loader import config_dir
from kiro_crew.cron import CronPendingMismatch, CronStoreBusy, CronStoreUnreadable
from kiro_crew.cron_script import (
    _read_script_body,
    bump_grant_epoch,
    commit_grant_epoch,
    compute_secret_env_pin,
    delivery_fingerprint,
    peek_grant_epoch,
    resolve_script_path,
    validate_secret_env_grant,
)
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.cron_script_source import _display_source
from kiro_crew.dashboard.handlers.cron_shared import (
    _audit_unavailable_response,
    _cron_unreadable_response,
    _invalid_path_id_response,
    _redacted_grant_map,
    _sel,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.secrets import SecretVault
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

from ._shared import guard_owner_surface_routes

logger = logging.getLogger("kiro_crew.dashboard.handlers.cron")


# Shape of the ``sha256`` GET /api/crons/{id}/script returns and the approval
# echoes back as ``expected_source_sha256``: 64 lowercase hex digits, nothing
# else. Anything not in this shape is a malformed approval, refused before it
# reaches the promotion path.
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


def _is_str_mapping(value: object) -> bool:
    """True when ``value`` is a dict whose keys AND values are all ``str``.

    The grant fields on a stored job are loaded verbatim from the
    agent-writable cron store, so the dataclass annotation is a promise the
    persisted bytes need not keep; every consumer that copies or iterates one
    checks the shape first.
    """
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    )


async def _promote_pending_grant(
    state: DashboardState,
    job_id: str,
    expected_source_sha256: str,
    expected_env: dict[str, str],
    expected_ts: float,
) -> web.Response:
    """Promote a job's PENDING secret request to the active grant.

    The single verified promotion path, reached only through the PUT
    ``approve_pending`` branch (approval lives exclusively on the dashboard
    Schedule page): re-validates the mapping, re-verifies
    the pending pin against the job's CURRENT code (409 ``code_changed`` on
    drift — an approval never blesses code that changed after the request),
    re-checks vault-name existence, then swaps pending -> active in one store
    update.

    ``expected_env``/``expected_ts`` bind the approval to the request the
    approver actually SAW: the agent can replace a pending request at any
    moment (cron_secret_request overwrites), so an approval that does not
    restate the displayed mapping could promote a different request than the
    one reviewed. Both are required and compared unconditionally — an
    optional check is a check the agent can arrange to skip — and a mismatch
    refuses with 409 ``stale_request`` instead of promoting.

    ``expected_source_sha256`` binds the approval to the script BODY the
    approver saw (the ``sha256`` GET ``/api/crons/{id}/script`` returned with
    the source it rendered). The request-level binding above is not enough on
    its own: an agent can rewrite the script and re-issue the request, and a
    dashboard whose request banner refreshed while its source view did not
    would then restate the NEW request's mapping and timestamp — passing both
    checks — while the operator is still looking at the OLD code. The pin only
    proves the code matches the request, never that the approver read it, so
    the approval must present the digest of what it displayed, and a digest
    that does not match the body this promotion pins is a 409 ``stale_source``.
    The field is required, not optional: an approval that omits it is an
    approval of code nobody attested to having seen. And a digest can only
    attest to what was SHOWN, so a body the dashboard cannot render verbatim
    (see ``_display_source``) is refused outright with 409
    ``source_not_reviewable`` — otherwise an agent could hide executable
    content behind a span the redactor masks and the operator would approve a
    raw digest over code the display never let them read.
    """
    job = await state.crons.get_job_async(job_id)
    # An unreadable store loads as an EMPTY job list without raising, so a miss
    # here alone would answer "job not found" over a corrupt file. The probe
    # reads the latch this read just refreshed and does no I/O of its own.
    try:
        state.crons.raise_if_store_unreadable()
    except CronStoreUnreadable as exc:
        return _cron_unreadable_response(exc)
    if not job:
        return web.json_response({"error": "job not found", "code": "job_not_found"}, status=404)
    # The store is agent-writable and ``_job_from_record`` loads both grant
    # fields verbatim, so either may be a list, a string or a dict with
    # non-string members. Refuse that shape here rather than letting the
    # ``dict(...)`` snapshots below raise into a 500: the owner sees a
    # structured refusal and the planted value never gets promoted.
    if not _is_str_mapping(job.secret_env) or not _is_str_mapping(job.secret_env_pending):
        return web.json_response(
            {
                "error": "the job's stored secret grant is malformed — revoke it and "
                "have the agent re-request",
                "code": "malformed_grant_state",
            },
            status=400,
        )
    if not job.secret_env_pending:
        return web.json_response(
            {"error": "no pending request", "code": "no_pending_request"}, status=404
        )
    if job.secret_env_pending != expected_env:
        return web.json_response(
            {
                "error": "the pending request changed after it was displayed — "
                "review the current request and approve again",
                "code": "stale_request",
            },
            status=409,
        )
    if job.secret_env_pending_ts != expected_ts:
        return web.json_response(
            {
                "error": "the pending request was re-issued after it was displayed — "
                "review the current request and approve again",
                "code": "stale_request",
            },
            status=409,
        )
    pending_snapshot = dict(job.secret_env_pending)
    pending_ts_snapshot = job.secret_env_pending_ts
    # Snapshot the PRIOR active grant too: the promoting write below replaces
    # it, and an epoch-commit failure must compensate by restoring it — its
    # pin was minted under the still-current epoch (the failed commit never
    # advanced it), so grant A stays exactly as valid as before this attempt.
    prior_secret_env = dict(job.secret_env)
    prior_secret_env_pin = job.secret_env_pin
    try:
        validate_secret_env_grant(pending_snapshot)

        # Verify the PENDING pin (what the request minted), then mint the
        # ACTIVE pin the runners honour — separate HMAC domains, so a pending
        # pin copied verbatim into the active fields never verifies. BOTH pins
        # derive from ONE script-body snapshot: a second read would let an
        # agent swap the file between them and get unseen code blessed. The
        # approver's source digest is checked against that SAME snapshot for
        # the same reason.
        def _both_pins() -> tuple[str, str, int, str, bool]:
            body_snapshot: bytes | None = None
            if job.script:
                file_path, _func = resolve_script_path(job.script)
                body_snapshot = _read_script_body(file_path)
            body_sha256 = hashlib.sha256(body_snapshot or b"").hexdigest()
            # Same verdict the source endpoint renders, re-derived from THIS
            # snapshot: the client's copy of the flag is not trusted.
            _shown, reviewable = _display_source(body_snapshot or b"")
            delivery = delivery_fingerprint(
                job.session_key, job.silent, job.channel or "", job.thread_ts or ""
            )
            pending_now = compute_secret_env_pin(
                job.script,
                job.command,
                job.message,
                job_id=job.id,
                grant=pending_snapshot,
                domain="pending",
                body=body_snapshot,
                delivery=delivery,
            )
            # Mint under the NEXT epoch without writing it: the epoch commits
            # only after the store swap succeeds, so a refused approval (code
            # drift, stale request, store busy) never invalidates an existing
            # active grant this operation did not replace.
            next_epoch = peek_grant_epoch(job.id)
            active = compute_secret_env_pin(
                job.script,
                job.command,
                job.message,
                job_id=job.id,
                grant=pending_snapshot,
                domain="active",
                body=body_snapshot,
                epoch=next_epoch,
                delivery=delivery,
            )
            return pending_now, active, next_epoch, body_sha256, reviewable

        pending_pin_now, active_pin, next_epoch, body_sha256, reviewable = await asyncio.to_thread(
            _both_pins
        )
    except (ValueError, FileNotFoundError, PermissionError, RuntimeError) as exc:
        return web.json_response({"error": str(exc), "code": "invalid_secret_env"}, status=400)
    if pending_pin_now != job.secret_env_pending_pin:
        return web.json_response(
            {
                "error": "the job's code changed after this request was made — "
                "review the current script/command, then ask the agent to "
                "re-request (or grant directly)",
                "code": "code_changed",
            },
            status=409,
        )
    if not reviewable:
        # The dashboard cannot show this body faithfully (redaction masked a
        # span, or it is not valid UTF-8), so no digest the operator echoes
        # can attest to having read the code that would run. Not approvable.
        return web.json_response(
            {
                "error": "parts of this script cannot be shown as written (masked "
                "credential-like text or undecodable bytes), so what the reviewer "
                "sees is not exactly what would run — rewrite the script so its "
                "source displays verbatim, then ask the agent to re-request",
                "code": "source_not_reviewable",
            },
            status=409,
        )
    if body_sha256 != expected_source_sha256:
        return web.json_response(
            {
                "error": "the script shown is not the script this request would run — "
                "reload the job, review the current source, and approve again",
                "code": "stale_source",
            },
            status=409,
        )
    known = set(await asyncio.to_thread(SecretVault(config_dir()).list_names))
    missing = sorted(set(pending_snapshot.values()) - known)
    if missing:
        # The names are agent-authored (they came in via the pending request),
        # so they take the same credential/exfiltration scrub as every other
        # agent-written string the dashboard echoes.
        shown = [redact_credentials(redact_exfiltration_urls(str(n))[0])[0] for n in missing]
        return web.json_response(
            {
                "error": "unknown vault secret name(s): " + ", ".join(shown),
                "code": "unknown_secret",
            },
            status=400,
        )
    # AUDIT-OR-DENY: a secret grant must never exist unaudited. The intent
    # record is written (and awaited, off-loop) BEFORE the promoting write;
    # an unwritable SEL store refuses the approval with nothing mutated —
    # the same fail-closed posture the repo applies to other privileged
    # mutations. ``critical=True`` is what makes that true: the default SEL
    # path enqueues for a background writer and swallows a filesystem
    # failure, so only a critical write surfaces it here. The terminal
    # success event below stays best-effort: an applied grant is already
    # covered by this record. Names only — env keys and vault names, never
    # values. _sel() is resolved INSIDE the worker lambda: a fresh gateway's
    # first call initializes the SEL store (trust key + log files), which
    # must never run on the event loop.
    try:
        await asyncio.to_thread(
            lambda: _sel().log_api_access(
                caller="dashboard",
                operation="cron.secret_request_approved",
                outcome="invoked",
                source="dashboard",
                resources=f"{job_id}:{','.join(sorted(pending_snapshot))}",
                critical=True,
            )
        )
    except Exception:
        logger.warning("SEL unavailable; refusing grant approval for %s", job_id, exc_info=True)
        return _audit_unavailable_response("approval")
    try:
        updated = await state.crons.update_job_async(
            job_id,
            secret_env=pending_snapshot,
            secret_env_pin=active_pin,
            # The pending request is CONSUMED in the same atomic write that
            # promotes it. Leaving it behind would let a concurrent decision
            # pass its own compare-and-swap against the same snapshot: a deny
            # would report success while this grant stays active, and a second
            # approval would re-mint over it. A commit failure below RESTORES
            # the request (compensating write), so the re-approve path
            # survives without that window.
            secret_env_pending={},
            # Locked compare-and-swap: everything above ran against a snapshot
            # the agent could have replaced in the meantime; the store refuses
            # the swap unless the record STILL carries exactly that snapshot.
            expect_secret_env_pending=pending_snapshot,
            expect_secret_env_pending_ts=pending_ts_snapshot,
        )
    except CronPendingMismatch:
        return web.json_response(
            {
                "error": "the pending request changed after it was displayed — "
                "review the current request and approve again",
                "code": "stale_request",
            },
            status=409,
        )
    except CronStoreBusy:
        return web.json_response(
            {
                "error": "cron store busy, please retry",
                "retryable": True,
                "code": "cron_store_busy",
            },
            status=409,
        )
    except CronStoreUnreadable as exc:
        return _cron_unreadable_response(exc)
    except ValueError as e:
        return web.json_response({"error": str(e), "code": "invalid_secret_env"}, status=400)
    if not updated:
        return web.json_response({"error": "job not found", "code": "job_not_found"}, status=404)
    # The swap landed: commit the epoch the pin was minted under. A crash in
    # this gap leaves the NEW grant failing closed (re-approve heals), never
    # a dead pin on a grant this operation did not replace. The commit is
    # compare-and-swap on the epoch this mint peeked: a concurrent revoke or
    # job removal that bumped meanwhile REFUSES the commit — re-committing
    # the bumped value would re-validate the very pin that bump meant to
    # kill. On refusal, bump once more so the just-swapped pin is dead too
    # (fail closed), then surface the conflict for a fresh approval.
    try:
        committed = await asyncio.to_thread(
            commit_grant_epoch, job.id, next_epoch, expected_current=next_epoch - 1
        )
    except (OSError, ValueError):
        # The swap landed but the epoch could not be committed: the stored pin
        # was minted under an uncommitted epoch, so every run refuses it (fail
        # closed). The request was consumed by the promoting write above, so
        # RESTORE it here — unless the agent already posted a NEWER request
        # into the gap, which stays (a fresh ask awaiting its own approval,
        # never silently overwritten by the old one's restoration).
        logger.warning("Grant-epoch commit failed for job %s", job.id, exc_info=True)

        async def _compensate(**kwargs: Any) -> bool:
            # CronStoreBusy is transient lock contention — the promoting
            # write succeeded moments ago, so a short bounded retry recovers
            # almost every real case instead of abandoning grant A to the
            # dead just-swapped pin. Unreadable state and invalid input
            # cannot heal on retry and fall through to the loud path below.
            for attempt in range(3):
                try:
                    await state.crons.update_job_async(job_id, **kwargs)
                    return True
                except CronStoreBusy:
                    await asyncio.sleep(0.2 * (attempt + 1))
                except (CronStoreUnreadable, ValueError):
                    break
            return False

        restored = False
        try:
            restored = await _compensate(
                # FULL compensation: the promoting write replaced active grant
                # A with a pin minted under the uncommitted epoch (dead), so
                # restore A alongside the consumed request — A's pin is still
                # valid, the failed commit never advanced the epoch.
                secret_env=prior_secret_env,
                secret_env_pin=prior_secret_env_pin,
                secret_env_pending=pending_snapshot,
                secret_env_pending_pin=pending_pin_now,
                secret_env_pending_ts=pending_ts_snapshot,
                expect_secret_env_pending={},
                expect_secret_env_pending_ts=0.0,
                # The ACTIVE fields must still hold the just-promoted (dead)
                # grant: a concurrent revoke that already cleared them owns
                # the state now, and restoring A over the operator's
                # revocation would resurrect what they withdrew.
                expect_secret_env=pending_snapshot,
                expect_secret_env_pin=active_pin,
            )
        except CronPendingMismatch as mismatch:
            if "active grant" in str(mismatch):
                # A concurrent writer (revoke) replaced the active fields in
                # the gap: their decision stands. Nothing to restore — the
                # dead just-swapped pin is already gone.
                logger.info("Compensation on %s skipped (grant changed concurrently)", job_id)
                restored = True
            else:
                # A NEWER request landed in the gap: leave it (never
                # overwritten by the old one's restoration), but STILL
                # restore the prior active grant in its own write — the dead
                # just-swapped pin must not stand in for grant A regardless
                # of the pending slot. Same active-field guard applies.
                logger.info("Pending restore on %s skipped (newer request)", job_id)
                try:
                    restored = await _compensate(
                        secret_env=prior_secret_env,
                        secret_env_pin=prior_secret_env_pin,
                        expect_secret_env=pending_snapshot,
                        expect_secret_env_pin=active_pin,
                    )
                except CronPendingMismatch:
                    logger.info("Compensation on %s skipped (grant changed concurrently)", job_id)
                    restored = True
        if not restored:
            # Double fault: the store accepted the promoting write but then
            # refused every compensation attempt. The dead just-swapped pin
            # stays persisted (runs refuse it — still fail-closed), but the
            # prior grant is NOT restored; say so loudly instead of
            # pretending the compensation landed.
            logger.critical(
                "Compensation failed for %s: prior grant not restored after "
                "epoch-commit failure",
                job_id,
            )
            return web.json_response(
                {
                    "error": "the grant's revocation epoch could not be committed "
                    "AND the compensating restore failed; the stored grant is "
                    "inactive (runs refuse its pin) — fix the cron/epoch storage, "
                    "then re-approve",
                    "code": "epoch_commit_failed",
                },
                status=503,
            )
        return web.json_response(
            {
                "error": "the grant's revocation epoch could not be committed; "
                "the previous grant was restored and the request is still "
                "pending — fix the epoch storage and approve again",
                "code": "epoch_commit_failed",
            },
            status=503,
        )
    if not committed:
        try:
            await asyncio.to_thread(bump_grant_epoch, job.id)
        except (OSError, ValueError):
            # Bump refused (corrupt state): every granted run already
            # refuses under it, so the direction is still closed.
            logger.warning("Conflict-bump failed for job %s", job.id, exc_info=True)
        return web.json_response(
            {
                "error": "the grant changed concurrently (a revoke or job removal "
                "raced this approval); ask the agent to re-request",
                "code": "grant_conflict",
            },
            status=409,
        )
    # The commit landed and the promoting write already consumed the pending
    # request, so the grant is fully active with nothing left to clear.
    # A bare enqueue: SEL is warmed at gateway startup (sel.warm_sel_singleton);
    # guarded because a FAILED warm leaves construction to retry here.
    try:
        _sel().log_api_access(
            caller="dashboard",
            operation="cron.secret_request_approved",
            outcome="allowed",
            source="dashboard",
            resources=f"{job_id}:{','.join(sorted(updated.secret_env))}",
        )
    except Exception:
        logger.debug("SEL logging failed for cron secret approve", exc_info=True)
    state.push_refresh("crons")
    return web.json_response(
        {"ok": True, "id": updated.id, "secret_env": _redacted_grant_map(updated.secret_env)}
    )


async def api_cron_secret_grant(request: web.Request) -> web.Response:
    """PUT /api/crons/{id}/secrets — revoke a grant or decide a pending request.

    Operator surface ONLY, enforced IN the handler: ``/api/crons`` is a PREFIX
    entry in the mixed internal paths (the CLI cron trigger needs it), so this
    route IS reachable with ``X-Internal-Secret`` — the credential every cron
    script subprocess and MCP process holds. Granting is the one cron mutation
    that must be human-only, so a proven internal-secret caller
    (``request["internal_auth"] is True``) is refused outright; only a
    cookie/token-authenticated browser caller proceeds. Body:
    ``{"secret_env": {}}`` (an EMPTY map) revokes — a non-empty map is
    refused, since request->approve is the only mint path —
    ``{"approve_pending": true}`` / ``{"deny_pending": true}`` act on an
    agent-requested pending grant. The code pin is computed HERE from the
    job's current script body — a client-supplied pin is
    ignored, so a grant always binds to the code the operator could inspect
    at grant time.
    """
    state: DashboardState = request.app["state"]
    # internal_auth is set solely after a constant-time X-Internal-Secret
    # match in token_auth_middleware — the machine credential. Machines
    # request (cron_secret_request); only humans grant. The denial is
    # SEL-audited like every other refused privileged operation: a machine
    # probing the grant endpoint is exactly the signal the audit log exists
    # to record.
    if request.get("internal_auth") is True:
        try:
            _sel().log_api_access(
                caller=str(request.get("user") or "internal"),
                operation="cron.secret_grant",
                outcome="denied",
                source="dashboard",
                resources=request.match_info.get("job_id", ""),
                error="operator_only",
            )
        except Exception:  # pragma: no cover - audit must never change the outcome
            logger.debug("SEL audit for machine secret-grant denial failed", exc_info=True)
        return web.json_response(
            {
                "error": "secret grants require the dashboard (operator) credential",
                "code": "operator_only",
            },
            status=403,
        )
    # And not just any human: a dashboard token is also minted for every
    # allowed Slack user (!dashboard), who is not the vault's owner. Granting
    # hands agent-authored code a vault value, so it is owner-only — the same
    # boundary ask_question's card resolution draws. The shared gate audits
    # the denial to SEL and reuses the one definition of "owner" (exact
    # owner_id match, or the signed local bootstrap subject when no owner is
    # configured).
    denied = await require_owner_dashboard_request(request, "cron.secret_grant")
    if denied is not None:
        return denied
    job_id = request.match_info["job_id"]
    if (_e := _invalid_path_id_response(job_id, "job_id")) is not None:
        return _e
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_json"}, status=400
        )
    job = await state.crons.get_job_async(job_id)
    # An unreadable store loads as an EMPTY job list without raising, so a miss
    # here alone would answer "job not found" over a corrupt file. The probe
    # reads the latch this read just refreshed and does no I/O of its own.
    try:
        state.crons.raise_if_store_unreadable()
    except CronStoreUnreadable as exc:
        return _cron_unreadable_response(exc)
    if not job:
        return web.json_response({"error": "job not found", "code": "job_not_found"}, status=404)
    # ── Agent-requested pending grants: approve / deny ──
    # The MCP cron_secret_request tool records a pending mapping + a pin of the
    # code at request time. Approval re-verifies that pin against the job's
    # CURRENT code so the operator only ever blesses what they could inspect —
    # a body changed after the request refuses with 409 rather than promoting.
    if body.get("deny_pending") is True:
        if not job.secret_env_pending:
            return web.json_response(
                {"error": "no pending request", "code": "no_pending_request"}, status=404
            )
        deny_expected = body.get("expected_secret_env")
        deny_expected_ts = body.get("expected_ts")
        # AUDIT-OR-DENY, same as approval: the intent record lands before the
        # request is discarded, synchronously, or the denial is refused.
        try:
            await asyncio.to_thread(
                lambda: _sel().log_api_access(
                    caller="dashboard",
                    operation="cron.secret_request_denied",
                    outcome="invoked",
                    source="dashboard",
                    resources=job_id,
                    critical=True,
                )
            )
        except Exception:
            logger.warning("SEL unavailable; refusing grant denial for %s", job_id, exc_info=True)
            return _audit_unavailable_response("denial")
        try:
            updated = await state.crons.update_job_async(
                job_id,
                secret_env_pending={},
                # A denial is a decision about the DISPLAYED request too: an
                # agent replacing it in the meantime must not have its unseen
                # request silently discarded by a stale click. The timestamp
                # additionally distinguishes a REISSUED request with an
                # identical mapping from the displayed one.
                expect_secret_env_pending=(
                    deny_expected if isinstance(deny_expected, dict) else None
                ),
                expect_secret_env_pending_ts=(
                    float(deny_expected_ts) if isinstance(deny_expected_ts, (int, float)) else None
                ),
            )
        except CronPendingMismatch:
            return web.json_response(
                {
                    "error": "the pending request changed after it was displayed — "
                    "review the current request and decide again",
                    "code": "stale_request",
                },
                status=409,
            )
        except CronStoreBusy:
            return web.json_response(
                {
                    "error": "cron store busy, please retry",
                    "retryable": True,
                    "code": "cron_store_busy",
                },
                status=409,
            )
        except CronStoreUnreadable as exc:
            return _cron_unreadable_response(exc)
        state.push_refresh("crons")
        return web.json_response({"ok": True, "id": job_id})
    if body.get("approve_pending") is True:
        # The approval must restate the request the approver saw (409
        # stale_request on drift) — see _promote_pending_grant. All three
        # snapshot fields are REQUIRED, not optional: an approval that omits
        # one attests to nothing about that dimension, so an agent could
        # re-issue the request between display and click and have the unseen
        # version promoted. The UI always sends the mapping it rendered, the
        # request timestamp and the source digest.
        expected_env = body.get("expected_secret_env")
        if not (
            isinstance(expected_env, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in expected_env.items())
        ):
            return web.json_response(
                {
                    "error": "expected_secret_env is required: the object mapping "
                    "env-var names to vault secret names exactly as displayed "
                    "for this approval",
                    "code": "invalid_secret_env",
                },
                status=400,
            )
        expected_ts = body.get("expected_ts")
        if isinstance(expected_ts, bool) or not isinstance(expected_ts, (int, float)):
            return web.json_response(
                {
                    "error": "expected_ts is required: the pending request timestamp "
                    "as displayed for this approval",
                    "code": "invalid_secret_env",
                },
                status=400,
            )
        # REQUIRED: the digest of the script source the approver reviewed
        # (``sha256`` from GET /api/crons/{id}/script). Without it the approval
        # attests to nothing about the code — see _promote_pending_grant.
        expected_source = body.get("expected_source_sha256")
        if not isinstance(expected_source, str) or not _SHA256_HEX_RE.fullmatch(expected_source):
            return web.json_response(
                {
                    "error": "expected_source_sha256 is required: the sha256 of the "
                    "script source reviewed for this approval, as returned by "
                    "GET /api/crons/{id}/script",
                    "code": "invalid_secret_env",
                },
                status=400,
            )
        return await _promote_pending_grant(
            state,
            job_id,
            expected_source_sha256=expected_source,
            expected_env=expected_env,
            expected_ts=float(expected_ts),
        )
    secret_env = body.get("secret_env")
    if not isinstance(secret_env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in secret_env.items()
    ):
        return web.json_response(
            {
                "error": "secret_env must be an object mapping env-var names "
                "to vault secret names",
                "code": "invalid_secret_env",
            },
            status=400,
        )
    pin = ""
    if secret_env:
        # Direct grants are removed BY DESIGN: nothing binds what the owner
        # saw to what gets pinned — the agent can rewrite the script between
        # the page render and the grant click, and the pin would bless the
        # rewrite. The request->approve flow is the only mint path: the
        # request pins the code at request time and approval refuses on
        # drift, so what the operator blesses is what the agent showed them.
        return web.json_response(
            {
                "error": "direct grants are not supported — have the agent "
                "call cron_secret_request, then approve the pending request "
                "here (the approval verifies the code has not changed since "
                "the request)",
                "code": "direct_grant_removed",
            },
            status=400,
        )
    # AUDIT-OR-DENY before anything moves — the epoch bump below is itself a
    # mutation (it kills the current pin), so the record must precede it.
    # Names only — env keys and vault names, never values.
    try:
        await asyncio.to_thread(
            lambda: _sel().log_api_access(
                caller="dashboard",
                operation="cron.secret_grant" if secret_env else "cron.secret_revoke",
                outcome="invoked",
                source="dashboard",
                resources=f"{job_id}:{','.join(sorted(secret_env)) or '-'}",
                critical=True,
            )
        )
    except Exception:
        logger.warning("SEL unavailable; refusing grant revocation for %s", job_id, exc_info=True)
        return _audit_unavailable_response("revocation")
    # Revoke (empty mapping): bump the epoch FIRST so a saved copy of the old
    # mapping+pin written back into the agent-writable store never verifies
    # again, even if the clear below fails.
    try:
        await asyncio.to_thread(bump_grant_epoch, job_id)
    except (OSError, ValueError):
        # Corrupt or unwritable epoch state: the grant is left in place —
        # every granted run already refuses under unreadable epoch state
        # (fail closed), and clearing the store WITHOUT the bump would let a
        # saved copy of the mapping+pin verify again once the state heals.
        logger.warning("Grant-epoch bump failed for revoke of %s", job_id, exc_info=True)
        return web.json_response(
            {
                "error": "the grant's revocation epoch could not be advanced; "
                "the grant was NOT cleared (runs refuse its pin while the "
                "epoch state is unhealthy) — fix the epoch storage and "
                "revoke again",
                "code": "epoch_bump_failed",
            },
            status=503,
        )
    try:
        updated = await state.crons.update_job_async(
            job_id, secret_env=secret_env, secret_env_pin=pin
        )
    except CronStoreBusy:
        return web.json_response(
            {
                "error": "cron store busy, please retry",
                "retryable": True,
                "code": "cron_store_busy",
            },
            status=409,
        )
    except CronStoreUnreadable as exc:
        return _cron_unreadable_response(exc)
    except ValueError as e:
        return web.json_response({"error": str(e), "code": "invalid_secret_env"}, status=400)
    if not updated:
        return web.json_response({"error": "job not found", "code": "job_not_found"}, status=404)
    state.push_refresh("crons")
    return web.json_response(
        {"ok": True, "id": updated.id, "secret_env": _redacted_grant_map(updated.secret_env)}
    )


# Every api_* handler in this module is an owner surface, so a private member's
# internal call is refused before it runs.
guard_owner_surface_routes(globals(), member_scoped=frozenset())
