"""Backup routes: status, runs through the Job SDK, the nightly toggles, retention,
Layer B, restore and the install label."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from aiohttp import web

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod
from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod
from kiro_crew.apps.builtins.aws_control.backend import storage as storage_mod
from kiro_crew.apps.builtins.aws_control.backend.routes_parts.common import (
    _account_target,
    _aws_failed,
    _bad_request,
    _body,
    _consent,
    _drive_bucket,
    _safe_error,
    logger,
)
from kiro_crew.apps.builtins.aws_control.backend.routes_parts.drive import _require_drive
from kiro_crew.apps.job_sdk import JobError, UnknownJobKind
from kiro_crew.apps.job_sdk import get_sdk as get_job_sdk
from kiro_crew.deploy.engine import AWSError

# --------------------------------------------------------------------------
# Backup
# --------------------------------------------------------------------------


def _account_jobs(account: str) -> dict[str, Any]:
    """Per-kind job state for THIS account, read from the SDK's own records.

    The generic ``_jobs/active`` surface is app-scoped by construction -- one app,
    one kind, every run -- and it withholds ``dedupe_key`` from its public view on
    purpose, so a browser cannot tell which account a listed run belongs to. An
    app whose work is per-account therefore needs an account-scoped read, and
    providing one is the APP's job rather than the SDK's. This endpoint is already
    account-scoped and already what the page reads for that account, so the
    in-flight run belongs in it.

    Server-side the key is available: ``list_active`` returns records, and only the
    HTTP view drops the field. Filtering happens HERE, and the key still never
    reaches the client.

    ``lastFailed`` is the other half of a durable record being useful. The app's
    own ``runs`` ledger only gains an entry when an upload SUCCEEDS, so a failed
    run leaves it untouched and the row would go quiet as though nothing had been
    asked for. The SDK holds how the run ended, so the most recent non-``done``
    terminal run for this account is served alongside it.
    """
    sdk = get_job_sdk(backup_mod.APP_NAME)
    if sdk is None:
        return {}
    out: dict[str, Any] = {}
    for kind in backup_mod.JOB_KINDS:
        active = next(
            (r for r in sdk.list_active(kind) if r.dedupe_key == account),
            None,
        )
        failed = None
        if active is None:
            # The NEWEST terminal run, then reported only if it did not succeed.
            # Picking the first non-`done` run instead would skip past a newer
            # success, so a fail-then-retry left the row saying "last run failed"
            # directly above the fresh success it had just recorded -- the row
            # contradicting itself, which is worse than saying nothing.
            # `list_recent` is already sorted newest-first.
            newest = next(
                (r for r in sdk.list_recent(kind, limit=20) if r.dedupe_key == account),
                None,
            )
            if newest is not None and newest.status != "done":
                failed = newest
        out[kind] = {
            "active": _job_view(active),
            "lastFailed": _job_view(failed),
        }
    return out


def _job_view(run: Any) -> dict[str, Any] | None:
    """The client's view of a run, for THIS endpoint's contract.

    Deliberately not ``job_routes._public_view``, and not a copy of it either.
    The two projections answer different questions, and they already differ in
    fields today: ``_public_view`` serves ``cancellable`` and ``cancelling`` and
    the full ``error``, while this one omits both cancel fields (the backup
    runners declare no cancellability, so they would be permanently false) and
    clamps ``error`` for the caption that renders it. ``_public_view`` also takes
    required ``cancelling`` and ``live`` sets read from the SDK's live table,
    which this endpoint has no reason to compute.

    Sharing one projection between two endpoints with different contracts is how
    a field leaks into one because the other needed it -- and the field at stake
    is ``dedupe_key``, whose leaking is the defect this endpoint exists to fix.
    ``test_the_account_never_reaches_the_client`` guards that here, which makes
    this a separate contract with its own proof rather than a copy nobody
    deduplicated. It is also a private helper of P1, so importing it would stop
    P1 from changing its own projection.
    """
    if run is None:
        return None
    return {
        "run_id": run.run_id,
        "kind": run.kind,
        "status": run.status,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "finished_at": run.finished_at,
        # Clamped for the surface that renders it. The SDK stores up to 2000
        # characters of whatever the runner raised, and the row shows this in a
        # 12px caption -- an expired-credential botocore message would blow the
        # line out. The marker matters as much as the limit: a sentence cut at
        # 180 with no sign of it reads as a complete thought that happens to be
        # ungrammatical. The full text stays in the run record, and the SDK has
        # already redacted it.
        "error": run.error if len(run.error) <= 180 else run.error[:177] + "...",
    }


async def _handle_backup_status(request: web.Request) -> web.Response:
    target = await _account_target(request)
    if isinstance(target, web.Response):
        return target
    account, profile, region = target
    payload: dict[str, Any] = {
        "nightly": await asyncio.to_thread(backup_mod.nightly_enabled, account),
        # The EFFECTIVE count the sweep would use, not the raw stored value, and
        # `null` when retention is off. A panel reporting a number the sweep would
        # clamp or ignore is worse than reporting none.
        "retentionKeep": await asyncio.to_thread(backup_mod.retention_keep, account),
        # Reported as its own field, never folded into `nightly`. The console
        # renders two switches because there are two grants, and a single field
        # would make the page unable to show that transcripts are still off.
        "nightlySessions": await asyncio.to_thread(backup_mod.nightly_sessions_enabled, account),
        # Why that grant cannot actually run, or None when it can. Reported
        # BESIDE the grant rather than folded into it, because the two answer
        # different questions: the grant is what the owner asked for and must
        # keep reading back as they set it, while this says whether asking for it
        # achieves anything here. Without it the console can only show the
        # switch as on, which on a host that cannot produce the archive is a
        # claim that transcripts are being backed up when none ever are -- and
        # the loss only surfaces on the host-loss event the feature exists for.
        #
        # ``scheduled_account`` is the part only this route can answer. The grant
        # is settable on any account in the registry while the nightly loop runs
        # for the one the default key belongs to, so a grant recorded on a second
        # account is authorized and unreachable at the same time. Answering it
        # here turns that into a sentence under the switch instead of a schedule
        # the operator believes in and nothing ever honours.
        "nightlySessionsBlocked": await asyncio.to_thread(
            backup_mod.scheduled_sessions_blocked_code,
            scheduled_account=account == await accounts_mod.default_account_id(),
        ),
        # Per kind, the last sweep's count of archives this install wrote that no sweep
        # can ever retire, and their bytes. The count above says what retention WILL
        # collect; this says what it never will, which is why a bill can fail to fall
        # after an operator enables it. Empty until a sweep has measured one, and
        # stamped rather than live -- refreshing it would need the bucket listing this
        # payload deliberately keeps opt-in.
        "retentionUnclaimed": await asyncio.to_thread(backup_mod.retention_unclaimed, account),
        # Beside it, never instead of it: one is a floor on the archives this install
        # remembers, the other counts what the listing held that it has no record of,
        # and the first deliberately reads 0 for the second's keys. Neither asserts
        # anything is reclaimable. See `backup.retention_unrecorded`.
        "retentionUnrecorded": await asyncio.to_thread(backup_mod.retention_unrecorded, account),
        # Per kind, how the last sweep ENDED -- successful, refused or failed, its fixed
        # skip reason, what it retired, and when. `retentionKeep` says what a sweep would
        # act on; without this nothing says whether one has, so a sweep refused every
        # night looks identical to a working one. Written on every ending, refusals
        # included, and stamped rather than live. See `backup.retention_last_sweep`.
        "retentionLastSweep": await asyncio.to_thread(backup_mod.retention_last_sweep, account),
        # Per kind, the consecutive-failure record for UNATTENDED attempts, and absent
        # for a kind whose last attempt completed. `runs` below says when the nightly
        # last SUCCEEDED, which cannot distinguish a schedule that has never run from
        # one that has been failing since a particular day -- and the operator is the
        # one who has to act on that difference. Local and free, like `install`: it is
        # a read of the same state document this payload already loads, so it rides on
        # the unpolled half rather than waiting for the opt-in remote one.
        "nightlyFailures": await asyncio.to_thread(backup_mod.nightly_failures, account),
        # Per kind, how many archives this install still holds a record of uploading,
        # which is a record count and not an inventory in either direction. `runs`
        # below keeps one record per kind, so a second nightly overwrites the first
        # while both archives stay in the drive: a row reading only that record states
        # "last backed up" and nothing else, which on a prefix holding several reads as
        # a prefix holding one. It can also read HIGH, because retention deletes an
        # object while its `uploads` key stays. Local and free, like `install` -- a read
        # of the state document this payload already loads -- so it rides on the
        # unpolled half rather than the opt-in remote listing, which is the only thing
        # that can say what the drive really holds. See `backup.remembered_archives`.
        "rememberedArchives": await asyncio.to_thread(backup_mod.remembered_archives, account),
        "runs": await asyncio.to_thread(backup_mod.last_runs, account),
        "jobs": await asyncio.to_thread(_account_jobs, account),
        # This install's own identity, so every row can be told from every other
        # install's. Local and free -- no AWS call -- so it rides on the unpolled
        # payload rather than waiting for the opt-in remote half.
        "install": await asyncio.to_thread(backup_mod.install_identity),
        "remote": None,
    }
    # The remote listing is OPT-IN, because this endpoint is now polled. Its
    # remote half tag-discovers the bucket on every call and then lists the
    # archive, so a minutes-long backup polled every 3s would fire hundreds of
    # paid AWS round trips to learn `jobs.active`, which the server already holds
    # in memory. The page asks for it only when the stored-archive disclosure is
    # open, which is the same condition it already gates the display behind.
    if request.query.get("remote") != "1":
        return web.json_response(payload)
    # A second opt-in inside the first. Enumerating the OTHER installs' prefixes
    # costs a list call each per kind plus one label read, so it is asked for
    # separately -- and it is asked for at all because a replacement machine owns
    # no archives, so without it a fresh install would see an empty list on the one
    # occasion the bucket holds the only surviving copy.
    include_others = request.query.get("others") == "1"
    denied = await _consent(aws_consent.SERVICE_S3, profile, region)
    if denied is None:
        try:
            bucket = await _drive_bucket(account, profile, region)
            if bucket:
                payload["remote"] = await asyncio.to_thread(
                    backup_mod.list_remote_backups,
                    profile,
                    region,
                    bucket,
                    account=account,
                    include_others=include_others,
                )
        except AWSError as exc:
            payload["remoteError"] = _safe_error(exc)
    return web.json_response(payload)


async def _handle_backup_run(request: web.Request) -> web.Response:
    """Start a backup and return its run id. Does NOT wait for it to finish.

    The work is a durable Job SDK run, so the fact that a backup is in flight
    lives on the server rather than in the browser tab that asked for it: a
    reload, a navigation away, or a second tab all still see it, and a run left
    behind by a gateway that died is resolved rather than advertised as running.
    The client follows the run on ``GET /backup/{account}``, whose ``jobs`` block
    is filtered to this account server-side. It does NOT read the app-scoped
    ``_jobs`` surface, which withholds ``dedupe_key`` and so cannot answer
    "is a backup running for THIS account".

    The pre-flight below is not the authorization gate --
    ``backup._authorize_upload`` is, inside the worker,
    immediately before the upload, and it holds for a run started through the
    generic ``_jobs`` surface too. What the pre-flight buys is a FAST, specific
    refusal: an unreconnected account, unconfirmed S3, or a missing drive answers
    409 with a code the UI can localise, instead of accepting the run and
    reporting the same thing thirty seconds later as a failed record.

    There is no terminal record in this response, because there is no
    terminal record yet. It reaches the client through ``GET /backup/{account}``,
    whose ``runs`` ledger the worker writes on success -- unchanged, and still
    the app's own record of what a backup PRODUCED (key, size, when). The Job SDK
    holds only that a run existed and how it ended.
    """
    ctx = await _require_drive(request)
    if isinstance(ctx, web.Response):
        return ctx
    account, _profile, _region, _bucket = ctx
    body = await _body(request)
    kind = str(body.get("kind", ""))
    if kind not in backup_mod.JOB_KINDS:
        return _bad_request("kind must be snapshot or sessions", "invalid_kind")
    # A kind this HOST cannot run is refused HERE, before a run record exists.
    # The worker would refuse too -- `run_sessions_backup` fail-closes without
    # `openat` rather than walking agent-writable directories by name -- but it
    # would do so as a `failed` record carrying a raised exception, which reads
    # like a broken backup rather than a platform that never offered the feature.
    # 501 and not 400: the request is well-formed and would be honoured on
    # another host, so it is this server that does not implement it.
    unavailable = backup_mod.kind_unavailable_reason(kind)
    if unavailable is not None:
        return web.json_response(
            {"error": unavailable, "code": "kind_unavailable_on_platform"}, status=501
        )
    sdk = get_job_sdk(backup_mod.APP_NAME)
    if sdk is None:
        # Enabled, but no SDK was published for it: the `jobs` grant is missing
        # from the manifest, or the context build failed. Not the owner's fault
        # and not a bad request -- say the runtime is absent.
        return web.json_response(
            {"error": "the backup runtime is not available", "code": "jobs_unavailable"},
            status=503,
        )
    try:
        # The account is the dedupe key: two runs of one kind for one account
        # must not both perform the paid upload, so the second ADOPTS the first
        # and returns its id. The SDK indexes on (kind, dedupe_key), so a
        # snapshot and a sessions backup of the same account stay independent.
        run_id = await sdk.start_async(kind, dedupe_key=account)
    except UnknownJobKind:
        # The kind is valid but nothing services it: startup registration did not
        # run. A 503 for the same reason as above -- the runtime, not the request.
        return web.json_response(
            {"error": "the backup runtime is not available", "code": "jobs_unavailable"},
            status=503,
        )
    except JobError as exc:
        return web.json_response(
            {"error": _safe_error(exc), "code": "backup_start_failed"}, status=503
        )
    return web.json_response({"started": True, "kind": kind, "runId": run_id})


async def _toggle_nightly(
    request: web.Request,
    *,
    setter: Any,
    field: str,
    what: str,
) -> web.Response:
    """The shared body of both nightly toggles.

    One implementation rather than two copies: both flips authorize unattended
    paid uploads, so the validation, the failure posture and the disclosure rule
    are the same decision and must not be able to drift apart. ``field`` is the
    key the console reads back and ``what`` names the setting in the error text.
    """
    target = await _account_target(request)
    if isinstance(target, web.Response):
        return target
    body = await _body(request)
    # NOT bool(): this toggle turns UNATTENDED PAID uploads on, and `bool("false")`
    # is True in Python, so a stringly-typed caller sending {"enabled": "false"}
    # would switch nightly backups ON while believing it asked for off. A flag
    # that costs money when it flips the wrong way is validated, never coerced --
    # the same posture the per-account paid-service confirmation takes.
    raw = body.get("enabled")
    if not isinstance(raw, bool):
        return _bad_request("enabled must be a boolean", "invalid_enabled")
    enabled = raw
    account, _profile, _region = target
    try:
        await asyncio.to_thread(setter, account, enabled)
    except OSError:
        # The setters propagate rather than publishing over state they could
        # not read, so this toggle can genuinely fail to persist. It must fail
        # LOUDLY -- reporting a setting the next read contradicts is worse than an
        # error -- but as a structured failure, because every non-2xx this app
        # returns carries a machine-readable `code` the console switches on.
        #
        # The message is FIXED rather than the OSError's own text: that text
        # renders the absolute path of the state file, and there is no reason to
        # disclose a local filesystem path in a response body when the log below
        # already carries it for whoever is actually debugging.
        logger.exception("aws-control: the %s toggle could not be persisted", what)
        return web.json_response(
            {"error": f"the {what} setting could not be saved", "code": "state_persist_failed"},
            status=500,
        )
    return web.json_response({field: enabled})


async def _handle_backup_nightly(request: web.Request) -> web.Response:
    return await _toggle_nightly(
        request, setter=backup_mod.set_nightly, field="nightly", what="nightly"
    )


async def _handle_backup_nightly_sessions(request: web.Request) -> web.Response:
    """Authorize unattended TRANSCRIPT uploads -- a separate grant, default off.

    Deliberately its own route rather than a second field on the snapshot
    toggle's body. A shared route would let one request carry both grants, and
    the whole point of the separate bit is that the operator says yes to
    transcripts as its own act.
    """
    return await _toggle_nightly(
        request,
        setter=backup_mod.set_nightly_sessions,
        field="nightlySessions",
        what="nightly sessions",
    )


async def _handle_backup_retention(request: web.Request) -> web.Response:
    """Set or clear this account's retention count -- the only shipped way in.

    Retention deletes object versions permanently and ships OFF, so this is the
    switch that turns it on. It takes the count rather than a flag because there is
    no separate enable bit: a count IS on, and `null` IS off.
    """
    target = await _account_target(request)
    if isinstance(target, web.Response):
        return target
    body = await _body(request)
    raw = body.get("keep", ...)
    if raw is ...:
        return _bad_request("keep is required", "invalid_keep")
    # NOT int(): this value authorizes PERMANENT DELETES of the owner's archives, so
    # it is validated and never coerced. `True` IS an `int` in Python, so a coercing
    # handler would read {"keep": true} as keep=1 -- the most destructive value
    # available -- from a caller that believed it was sending a flag. Out of range is
    # refused rather than clamped: the stored value must be the one the caller asked
    # for, so nobody configures 0 and is later told they configured 1. There is no
    # upper bound to refuse against -- a count larger than the number of archives that
    # exist keeps all of them, which is not a harm.
    if raw is not None and (isinstance(raw, bool) or not isinstance(raw, int)):
        return _bad_request("keep must be an integer or null", "invalid_keep")
    if raw is not None and raw < backup_mod.RETENTION_KEEP_MIN:
        return _bad_request(
            f"keep must be at least {backup_mod.RETENTION_KEEP_MIN}",
            "invalid_keep",
        )
    account, _profile, _region = target
    try:
        await asyncio.to_thread(backup_mod.set_retention_keep, account, raw)
    except OSError:
        # Loud, and with a FIXED message, for the reasons the nightly toggle states:
        # reporting a setting the next read contradicts is worse than an error, and
        # the OSError's own text carries the state file's absolute path.
        logger.exception("aws-control: the retention count could not be persisted")
        return web.json_response(
            {"error": "the retention setting could not be saved", "code": "state_persist_failed"},
            status=500,
        )
    return web.json_response({"retentionKeep": raw})


async def _handle_backup_layer_b(request: web.Request) -> web.Response:
    """The ONLY writer of the sessions archive's Layer B permission.

    Owner-gated by ``_guarded`` like every route here, and it reaches the state
    file directly rather than through the agent file gate -- which is the whole
    point of keeping this permission out of ``config.json``. See
    ``backup.sessions_layer_b_enabled`` for why an agent-writable home would
    let a prompt-injected shell consent to an irreversible upload of unredacted
    model context on the operator's behalf.
    """
    target = await _account_target(request)
    if isinstance(target, web.Response):
        return target
    body = await _body(request)
    # Validated, never coerced, for the same reason the nightly toggle above is:
    # `bool("false")` is True, so a stringly-typed caller asking for OFF would
    # switch unredacted context ON. Here the wrong direction is unrecallable
    # rather than merely billable, so the posture is not optional.
    raw = body.get("enabled")
    if not isinstance(raw, bool):
        return _bad_request("enabled must be a boolean", "invalid_enabled")
    enabled = raw
    # The grant's SCOPE must be NAMED by the caller, never derived from the act of
    # enabling. A bare `{"enabled": true}` carries no evidence of what the operator was
    # shown, so an idempotent retry, an automation, and a client still rendering older
    # copy all look identical to a deliberate re-consent -- and the wider scope ships
    # host-wide terminal conversations off-host, unrecallably. Absent means the narrower
    # grant, which is why this field is optional rather than required: the existing
    # request shape keeps its existing meaning.
    scope = body.get("scope")
    if scope is not None and not isinstance(scope, str):
        return _bad_request("scope must be a string", "invalid_scope")
    account, _profile, _region = target
    try:
        if scope is None:
            await asyncio.to_thread(backup_mod.set_sessions_layer_b, account, enabled)
        else:
            await asyncio.to_thread(
                partial(backup_mod.set_sessions_layer_b, account, enabled, scope=scope)
            )
    except OSError:
        # Same contract as the nightly toggle: the write can genuinely fail, and
        # a permission the console renders as stored while the next read denies it
        # is worse than an error. Fixed message, structured code, path only in
        # the log.
        logger.exception("aws-control: the Layer B permission could not be persisted")
        return web.json_response(
            {
                "error": "the Layer B setting could not be saved",
                "code": "state_persist_failed",
            },
            status=500,
        )
    if scope is None:
        return web.json_response({"sessionsIncludeLayerB": enabled})
    # Echoed only when a scope was asked for, so the existing request shape keeps its
    # existing response. A caller that named one needs to see what it actually got: an
    # unrecognised value records the narrower grant rather than failing, so silence here
    # would let it believe it had consented to the wider payload.
    granted = await asyncio.to_thread(backup_mod.layer_b_grant_covers_conversations, account)
    return web.json_response(
        {
            "sessionsIncludeLayerB": enabled,
            "sessionsLayerBScope": (
                backup_mod.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS if granted else ""
            ),
        }
    )


async def _handle_backup_restore(request: web.Request) -> web.Response:
    """Download an archive to the staging dir — never a live hot-swap."""
    ctx = await _require_drive(request)
    if isinstance(ctx, web.Response):
        return ctx
    account, profile, region, bucket = ctx
    body = await _body(request)
    key = str(body.get("key", ""))
    err = storage_mod.validate_key(key)
    if err or not (key.startswith("snapshots/") or key.startswith("sessions/")):
        return _bad_request("key must name a backup archive", "invalid_key")
    # NOT bool(): the same rule the nightly toggle states. This flag waives a
    # guard that stands between the owner and overwriting this machine's memory
    # with another machine's, and `bool("false")` is True -- so a stringly-typed
    # caller asking NOT to override would be granted the override. Absent means
    # not overridden; present means it has to be a real boolean.
    raw_foreign = body.get("foreignOk", False)
    if not isinstance(raw_foreign, bool):
        return _bad_request("foreignOk must be a boolean", "invalid_foreign_ok")
    try:
        result = await asyncio.to_thread(
            backup_mod.restore_download,
            profile,
            region,
            bucket,
            key,
            account=account,
            foreign_ok=raw_foreign,
        )
    except backup_mod.UnprovenArchive as exc:
        # 409, not 403: nothing about the caller's authority is in question -- the
        # request conflicts with the state of the thing it names, and the same
        # request with the override succeeds. The ORIGIN travels with the refusal
        # because the three cases need different words: a co-tenant's archive, one
        # under this install's own prefix with no upload record, and one predating
        # install ids are three different things to tell an operator. The owning id
        # comes too, so "another install" is never the whole answer.
        return web.json_response(
            {
                "error": str(exc),
                "code": "foreign_install_archive",
                "origin": exc.origin,
                "install": exc.install_id,
            },
            status=409,
        )
    except AWSError as exc:
        return _aws_failed(exc)
    return web.json_response({"downloaded": True, **result})


async def _handle_install_label(request: web.Request) -> web.Response:
    """Rename THIS install. Local only — no AWS call, no account.

    Not under ``/backup/{account}``: the install is the same install whichever
    account it backs up to, so scoping the rename to one account would imply a
    name per account and mint the confusion the id exists to remove. The new name
    reaches the drive on the next backup, which already holds a bucket and a live
    authorization decision; making a cosmetic rename depend on the network would
    let it fail for no benefit.

    The label changes what is DISPLAYED and nothing else. Ownership, the restore
    refusal and the nightly's shared-drive notice all read the id in the object
    key, so no name an owner (or another install) chooses can move an archive
    across that line.
    """
    body = await _body(request)
    raw = body.get("label")
    if not isinstance(raw, str):
        return _bad_request("label must be a string", "invalid_label")
    try:
        identity = await asyncio.to_thread(backup_mod.set_install_label, raw)
    except OSError:
        # Same posture as the nightly toggle: a setting that did not persist is
        # reported as a failure rather than echoed back as if it had. The message
        # is fixed because the OSError's own text carries the absolute path of the
        # state file, which has no business in a response body.
        logger.exception("aws-control: the install label could not be persisted")
        return web.json_response(
            {"error": "the install name could not be saved", "code": "state_persist_failed"},
            status=500,
        )
    return web.json_response({"install": identity})
