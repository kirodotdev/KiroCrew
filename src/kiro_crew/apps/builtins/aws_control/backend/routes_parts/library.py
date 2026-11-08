"""Library routes: the cloud library reconciled against the ledger, and push and
remove of one artifact."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

from aiohttp import web

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import library as library_mod
from kiro_crew.apps.builtins.aws_control.backend import storage as storage_mod
from kiro_crew.apps.builtins.aws_control.backend.routes_parts.common import (
    _LIBRARY_RECONCILE_LOCK_WAIT_SECS,
    _account_target,
    _audit,
    _aws_failed,
    _bad_request,
    _body,
    _consent,
    _drive_bucket,
    _ledger_corrupt,
    _library_lock,
    _not_found,
    _publish_gate,
    _reauthorize_in_lock,
    _safe_error,
    logger,
)
from kiro_crew.apps.builtins.aws_control.backend.routes_parts.drive import _require_drive
from kiro_crew.deploy.engine import AWSError

# --------------------------------------------------------------------------
# Library
# --------------------------------------------------------------------------


async def _reconciled_remote_slugs(request: web.Request) -> tuple[set[str] | None, str]:
    """Read the account's cloud library and reconcile the ledger against it.

    Returns the slugs the bucket holds, or ``(None, reason)`` when it could not
    be read. A NON-FATAL variant of the drive preamble: the Library list must
    keep rendering local artifacts for an account that is disconnected, has not
    confirmed S3, or has no drive yet -- the same shape
    ``_handle_backup_status`` uses for its remote half. Every failure comes back
    as a reason rather than as a response, and the caller reports it instead of
    implying an answer.

    The listing and the prune run under ``_library_lock`` as ONE step. They are
    a read of remote state followed by a decision about local state, and a push
    completing between them would have its fresh record pruned on a snapshot
    taken before it existed -- the bucket backs that record, so deleting it
    breaks reconcile's own rule that it only drops what the bucket has
    disproven. ``observed_at`` is passed through as a second, cross-process
    guard; see :func:`library.reconcile`.
    """
    target = await _account_target(request)
    if isinstance(target, web.Response):
        # A permission DECISION, even though this route degrades instead of
        # failing: the profile became unavailable or now names another account,
        # and _guarded's own rule is that such a decision reaches SEL. Degrading
        # quietly would drop the one event an incident review asks about.
        await _audit("library_list", request.path, "denied", error="account_unavailable")
        return None, "no working connection for this account"
    account, profile, region = target
    if await _consent(aws_consent.SERVICE_S3, profile, region) is not None:
        return None, "S3 is not confirmed for the account in use"
    try:
        bucket = await _drive_bucket(account, profile, region)
    except AWSError as exc:
        return None, _safe_error(exc)
    if not bucket:
        return None, storage_mod.no_drive_message(region)
    # Taken BEFORE the listing, never after: the cutoff must not postdate the
    # snapshot it describes. Reading it early only widens the set of records the
    # prune leaves alone, which is the safe direction.
    observed_at = dt.datetime.now(dt.timezone.utc)
    try:
        # BOUNDED wait, unlike the two mutations, which are user-initiated actions
        # that may legitimately queue. This is a page render: a push holding the
        # lock through a 600s upload must not hang it. Giving up here loses
        # nothing durable -- the reconcile is self-correcting, so the next render
        # performs it -- and it is reported rather than silently skipped.
        await asyncio.wait_for(_library_lock.acquire(), timeout=_LIBRARY_RECONCILE_LOCK_WAIT_SECS)
    except asyncio.TimeoutError:
        return None, "another library operation is in progress; the bucket was not re-read"
    try:
        # The lock is a WAIT, so the consent and identity resolved above may have
        # expired while queued -- and a listing is still a call into a paid
        # service, which this app never makes on a withdrawn grant. Same re-check
        # the two mutations run; failure degrades to "not reconciled" rather than
        # to an error, because this route's local half must still render.
        if (
            await _reauthorize_in_lock(
                request, "library_list", account, profile, region, bucket, publish=False
            )
            is not None
        ):
            return None, "authorization changed while this request was queued"
        try:
            # list_library_folders, NOT the paged display listing: this answer is
            # compared against ledger KEYS and reasoned about as absence, so it
            # must be unredacted and complete. See its docstring.
            folders = await asyncio.to_thread(
                storage_mod.list_library_folders,
                profile,
                region,
                bucket,
                account=account,
            )
        except AWSError as exc:
            return None, _safe_error(exc)
        slugs = {f for f in folders if library_mod.valid_slug(f)}
        try:
            await asyncio.to_thread(library_mod.reconcile, account, slugs, observed_at=observed_at)
        except json.JSONDecodeError:
            # The strict update reader refuses a corrupt ledger. Same
            # degradation as the OSError arm below -- this route is best-effort by
            # contract and the LIST must keep rendering (its rows come from the
            # lenient display read) -- but the reason differs on the axis the
            # operator acts on: this does not clear on retry, the file needs repair.
            logger.warning("aws-control library reconcile refused: ledger corrupt")
            return None, "the local sync ledger is corrupt and must be repaired"
        except OSError as exc:
            # The reconcile WRITES, and this route is best-effort by contract. An
            # unwritable ledger directory, or a lock this platform refuses to
            # take, must not turn a page render into a 500: the rows are still
            # renderable, they are just unverified. Reported as a reason rather
            # than swallowed, so the payload does not claim a reconcile happened.
            logger.warning("aws-control library reconcile could not write: %s", exc)
            return None, "the local sync ledger could not be updated"
    finally:
        _library_lock.release()
    return slugs, ""


async def _handle_library_list(request: web.Request) -> web.Response:
    """Local artifacts + push state, with the ledger reconciled first.

    The reconcile is BEST-EFFORT and its outcome is REPORTED, never implied.
    ``reconciled`` says whether the bucket was actually read; when it is false
    the rows are the ledger's unverified claim and ``remoteError`` says why. A
    caller that cannot tell "no cloud copy" from "cloud state unknown" is how a
    destructive control gets offered for an item nothing is known about, so the
    distinction is in the payload rather than left to be inferred from an empty
    list.

    This GET writes local state, which is deliberate and narrow: reconcile only
    ever DELETES a claim the bucket has already disproven. It touches no object,
    removes no data, and takes nothing from the request -- the bucket listing is
    its only input, so a caller cannot steer it. Doing it here is the point: the
    stale record's whole symptom (a push refused because the ledger insists the
    copy is already there) is only observable on the render that reads it.
    """
    account = request.match_info.get("account", "")
    remote, reason = await _reconciled_remote_slugs(request)
    payload: dict[str, Any] = {"reconciled": remote is not None}
    if remote is None:
        payload["remoteError"] = reason
    rows = await asyncio.to_thread(library_mod.list_pushable, account)
    if remote is not None:
        # Cloud copies with no local artifact row: pushed from another machine,
        # or the local artifact has since been deleted. list_pushable walks the
        # LOCAL store, so these have no row to carry them and would otherwise be
        # unreachable from the console -- the exact "filled it, cannot empty it"
        # gap. Slugs only; a version would cost one GET per copy on a render.
        local = {str(row.get("slug", "")) for row in rows}
        payload["remoteOnly"] = sorted(remote - local)
    return web.json_response({"artifacts": rows, **payload})


async def _handle_library_remove(request: web.Request) -> web.Response:
    """Delete one artifact's cloud copy and forget its ledger record.

    No publish gate: that gate governs bytes LEAVING the box (a push, a
    presigned link), and this route sends nothing out. It is a mutation, so it
    carries the restricted-session refusal and the SEL audit every mutation
    does. Single-call like the Drive's own file and folder deletes -- the
    two-call confirm is for creating a BILLABLE resource, and the dashboard
    confirms deletions itself.
    """
    ctx = await _require_drive(request)
    if isinstance(ctx, web.Response):
        return ctx
    account, profile, region, bucket = ctx
    body = await _body(request)
    slug = str(body.get("slug", ""))
    # An artifact slug, not a free-form key: the slug becomes an object prefix,
    # and the validator refuses the empty and '/'-bearing values that would
    # widen that prefix beyond one artifact. library_remove re-checks it, so the
    # blast radius does not depend on this route being the only caller.
    if not library_mod.valid_slug(slug):
        return _bad_request("slug must be an artifact slug", "invalid_slug")
    try:
        # Under _library_lock, for the mirror of the push case: the delete sweep
        # and the ledger write are two steps, and a push of the same slug
        # interleaving can land an object after the sweep has finished looking.
        async with _library_lock:
            # A queued delete can outlive its own authorization the same way a
            # queued push can; a delete under withdrawn consent is still an
            # unauthorized call into the account.
            denied = await _reauthorize_in_lock(
                request, "library_remove", account, profile, region, bucket, publish=False
            )
            if denied:
                return denied
            result = await asyncio.to_thread(
                library_mod.library_remove, profile, region, bucket, account, slug
            )
    except json.JSONDecodeError:
        # Before the ``ValueError`` arm for the same subclass reason as the push
        # handler: without it a corrupt ledger reads as ``invalid_slug``, blaming
        # the request for a store that needs repair.
        return _ledger_corrupt("library_ledger_corrupt")
    except ValueError as exc:
        return _bad_request(_safe_error(exc), "invalid_slug")
    except AWSError as exc:
        return _aws_failed(exc)
    return web.json_response({"removed": True, **result})


async def _handle_library_push(request: web.Request) -> web.Response:
    ctx = await _require_drive(request)
    if isinstance(ctx, web.Response):
        return ctx
    account, profile, region, bucket = ctx
    # Artifact bytes leaving the box is a publish decision — same shared
    # fail-closed gate deploy-web consults before its own uploads.
    denied = await _publish_gate(request, "library_push")
    if denied:
        return denied
    body = await _body(request)
    slug = str(body.get("slug", ""))
    if not slug:
        return _bad_request("slug required", "invalid_slug")
    from kiro_crew.artifacts import ArtifactNotFoundError

    try:
        # Under _library_lock: the upload and the ledger write are two steps, and
        # a removal of the same slug interleaving between them can sweep one of
        # the two uploaded objects and then forget a record this push is about to
        # rewrite. See the lock's own comment.
        async with _library_lock:
            # The lock is a WAIT, so the authorization above may have expired
            # while queued. Re-run it before a byte moves.
            denied = await _reauthorize_in_lock(
                request, "library_push", account, profile, region, bucket, publish=True
            )
            if denied:
                return denied
            record = await asyncio.to_thread(
                library_mod.push_artifact, profile, region, bucket, account, slug
            )
    except ArtifactNotFoundError:
        return _not_found("unknown artifact", "unknown_artifact")
    except json.JSONDecodeError:
        # MUST precede the ``ValueError`` arm below: ``JSONDecodeError`` subclasses
        # ``ValueError``, so without this the ledger's corruption refusal
        # would be reported as ``not_pushable`` -- a 400 blaming the artifact for a
        # store the operator has to repair. The objects may already be in the
        # bucket (upload precedes the ledger write); the honest answer is that the
        # push is NOT recorded, which the next reconcile will surface as
        # ``remoteOnly`` once the ledger is repaired.
        return _ledger_corrupt("library_ledger_corrupt")
    except ValueError as exc:
        return _bad_request(_safe_error(exc), "not_pushable")
    except AWSError as exc:
        return _aws_failed(exc)
    return web.json_response({"pushed": True, **record})
