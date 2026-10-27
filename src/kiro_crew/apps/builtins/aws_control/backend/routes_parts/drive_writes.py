"""Drive routes that write -- upload, delete, move, folder create and delete -- and the
share links: minting one, listing the share ledger, and forgetting an entry."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from aiohttp import web

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import shares as shares_mod
from kiro_crew.apps.builtins.aws_control.backend import storage as storage_mod
from kiro_crew.apps.builtins.aws_control.backend.routes_parts.common import (
    _MAX_UPLOAD_BYTES,
    _audit,
    _aws_failed,
    _bad_request,
    _body,
    _conflict,
    _consent,
    _drive_bucket,
    _forbidden,
    _ledger_corrupt,
    _locked_drive_sweep,
    _locked_drive_write,
    _not_found,
    _publish_gate,
    _reauthorize_in_lock,
    _resolve_target,
    _safe_error,
    _valid_section,
    logger,
)
from kiro_crew.apps.builtins.aws_control.backend.routes_parts.drive import _require_drive
from kiro_crew.dashboard.body_numbers import body_int
from kiro_crew.deploy.engine import AWSError


async def _handle_drive_upload(request: web.Request) -> web.Response:
    ctx = await _require_drive(request)
    if isinstance(ctx, web.Response):
        return ctx
    account, profile, region, bucket = ctx
    section = _valid_section(request)
    if isinstance(section, web.Response):
        return section
    key = request.rel_url.query.get("key", "")
    err = storage_mod.validate_key(key)
    if err:
        return _bad_request(err, "invalid_key")
    if request.content_length and request.content_length > _MAX_UPLOAD_BYTES:
        return _bad_request("file too large (512 MB cap)", "upload_too_large")

    # NOT a `with TemporaryDirectory()`: its __exit__ runs shutil.rmtree
    # SYNCHRONOUSLY on the event loop, and deleting a 512 MB spool is exactly
    # the stall every other touch of this file is offloaded to avoid.
    tmp = await asyncio.to_thread(tempfile.mkdtemp, prefix="kc-upload-")
    try:
        spool = Path(tmp) / "upload.bin"
        received = 0
        # Every touch of the spool file is offloaded: a 512 MB upload writing
        # synchronously from the handler would stall the gateway event loop
        # for the whole transfer (open/close included — close flushes).
        sink = await asyncio.to_thread(open, spool, "wb")
        try:
            async for chunk in request.content.iter_chunked(1 << 20):
                received += len(chunk)
                if received > _MAX_UPLOAD_BYTES:
                    return _bad_request("file too large (512 MB cap)", "upload_too_large")
                await asyncio.to_thread(sink.write, chunk)
        finally:
            await asyncio.to_thread(sink.close)
        if received == 0:
            return _bad_request("empty upload", "empty_upload")
        # A 512 MB stream can take minutes, and the per-key lock below can
        # queue this request behind another minutes-long put: both waits sit
        # between the checks _require_drive ran and the AWS call they
        # authorized. The spool and the lock are the same post-wait gap the
        # Library operations cross under their lock, so the SAME helper
        # re-runs the full re-authorization INSIDE the lock: app gate, live
        # identity re-probe, consent, and the drive bucket itself -- the piece
        # an identity check cannot cover, because tag discovery can move the
        # drive to a different bucket while the identity is unchanged, and a
        # name held across the wait is exactly the staleness the routes'
        # no-cache rule forbids. A pass means the pre-wait ``bucket`` still
        # names the account's current drive; anything else is refused with
        # nothing written. No publish gate: an upload does not consult it on
        # the way in, so the re-check does not add it.
        try:
            # Same per-key lock the move handler holds across its probe+copy:
            # an upload put inside the lock either finishes before a move's
            # destination probe (the probe then answers 409) or starts after
            # the move released — it cannot land inside the move's
            # probe-to-copy window and be silently overwritten. Only the
            # re-authorization and the put are inside the lock; the spool
            # transfer above must not hold it.
            async with _locked_drive_write(bucket, section, key):
                denied = await _reauthorize_in_lock(
                    request, "drive_upload", account, profile, region, bucket, publish=False
                )
                if denied:
                    return denied
                await asyncio.to_thread(
                    storage_mod.put_file,
                    profile,
                    region,
                    bucket,
                    section,
                    key,
                    str(spool),
                    account=account,
                )
        except AWSError as exc:
            return _aws_failed(exc)
    finally:
        await asyncio.to_thread(shutil.rmtree, tmp, True)
    return web.json_response({"uploaded": True, "key": key, "bytes": received})


async def _handle_drive_delete(request: web.Request) -> web.Response:
    ctx = await _require_drive(request)
    if isinstance(ctx, web.Response):
        return ctx
    account, profile, region, bucket = ctx
    body = await _body(request)
    section = str(body.get("section", "drive"))
    if section not in storage_mod.SECTION_PREFIXES:
        return _bad_request("unknown section", "invalid_section")
    key = str(body.get("key", ""))
    err = storage_mod.validate_key(key)
    if err:
        return _bad_request(err, "invalid_key")
    try:
        # Same coordination net as move/upload: the key lock serializes this
        # delete against a same-key writer, the shared section hold excludes
        # a concurrent folder sweep, and — because the wait can be minutes
        # behind a large upload — the authorization is re-run inside.
        async with _locked_drive_write(bucket, section, key):
            denied = await _reauthorize_in_lock(
                request, "drive_delete", account, profile, region, bucket, publish=False
            )
            if denied:
                return denied
            await asyncio.to_thread(
                storage_mod.delete_key, profile, region, bucket, section, key, account=account
            )
    except AWSError as exc:
        return _aws_failed(exc)
    return web.json_response({"deleted": True, "key": key})


async def _handle_drive_move(request: web.Request) -> web.Response:
    """Move one object inside the ``drive`` section — server-side copy, then delete.

    The section is restricted to ``drive`` by design: ``library`` and
    ``backup`` are managed surfaces whose objects carry ledger state (the
    library ledger, backup sidecars), and moving one from here would orphan
    that state. A known-but-refused section answers the same 400 an unknown
    one does.

    Both keys pass the shared :func:`storage_mod.validate_key` BEFORE any AWS
    call, the source must exist (404), and the destination must NOT (409) —
    a move never silently overwrites. A source with a LIVE share link is
    refused (409 ``share_active``): the presigned URL is bound to the old key
    and would 404 while the Access ledger still reports it live.

    Everything from the re-authorization through the source delete runs under
    :func:`_locked_drive_write` for both keys — the coordination net every
    key-scoped drive mutation shares (upload, delete, folder create, the
    share mint) plus exclusion against folder sweeps — so within this gateway
    (the drive's only product-plane writer) no sibling mutation can land
    inside the probe-to-delete window. The :func:`_reauthorize_in_lock`
    re-check runs first because the lock wait itself is a post-authorization
    gap. Writers outside the gateway are outside the promise. The source is
    deleted ONLY after the copy returned success, so a failed copy leaves the
    drive unchanged and a failed delete leaves a duplicate rather than a
    loss.
    """
    ctx = await _require_drive(request)
    if isinstance(ctx, web.Response):
        return ctx
    account, profile, region, bucket = ctx
    body = await _body(request)
    section = str(body.get("section", "drive"))
    if section != "drive":
        return _bad_request("move is limited to the drive section", "invalid_section")
    from_key = str(body.get("fromKey", ""))
    to_key = str(body.get("toKey", ""))
    for key in (from_key, to_key):
        err = storage_mod.validate_key(key)
        if err:
            return _bad_request(err, "invalid_key")
    if from_key == to_key:
        return _bad_request("source and destination are the same key", "same_key")
    try:
        async with _locked_drive_write(bucket, section, from_key, to_key):
            # The lock wait can be long (a 512 MB upload legally holds a key
            # for minutes), and it sits between the checks _require_drive ran
            # and the AWS calls below — the same post-wait gap the upload
            # spool crosses, so the SAME re-authorization runs here: app gate,
            # identity re-probe, consent, and the drive bucket itself.
            denied = await _reauthorize_in_lock(
                request, "drive_move", account, profile, region, bucket, publish=False
            )
            if denied:
                return denied
            # A live share is a bearer URL SIGNED FOR THE SOURCE KEY. The
            # copy+delete below would leave that URL answering 404 while the
            # Access ledger keeps reporting the link live until expiry — the
            # ledger cannot be "fixed up" because a presigned URL is bound to
            # its key and cannot be re-pointed. Refuse instead of silently
            # breaking a grant the owner handed out: the ledger is local, so
            # the check costs no AWS call.
            shared = await asyncio.to_thread(shares_mod.list_shares, account)
            if any(
                entry.get("section") == section and entry.get("key") == from_key for entry in shared
            ):
                return _conflict(
                    "this file has a live share link — moving it would break the link",
                    "share_active",
                )
            exists = await asyncio.to_thread(
                storage_mod.object_exists,
                profile,
                region,
                bucket,
                section,
                from_key,
                account=account,
            )
            if not exists:
                return _not_found("no such file in this drive", "object_missing")
            taken = await asyncio.to_thread(
                storage_mod.object_exists,
                profile,
                region,
                bucket,
                section,
                to_key,
                account=account,
            )
            if taken:
                return _conflict(
                    "an object already exists at the destination", "destination_exists"
                )
            await asyncio.to_thread(
                storage_mod.copy_object,
                profile,
                region,
                bucket,
                section,
                from_key,
                to_key,
                account=account,
            )
            await asyncio.to_thread(
                storage_mod.delete_key, profile, region, bucket, section, from_key, account=account
            )
    except AWSError as exc:
        return _aws_failed(exc)
    return web.json_response({"moved": True})


async def _handle_drive_folder_create(request: web.Request) -> web.Response:
    """Create an empty folder — a zero-byte, ``/``-terminated placeholder object.

    ``path`` is validated with the SAME :func:`storage_mod.validate_key` every
    object key goes through, which is what stops a folder name from escaping the
    section: it rejects ``../``, a leading slash (absolute key), control
    characters, and an empty or ``/``-only value. The trailing ``/`` that turns
    the validated key into a folder marker is appended inside storage
    (:func:`storage_mod.create_folder`), never taken from the request, so the key
    shape the listing filters on cannot be spoofed.
    """
    ctx = await _require_drive(request)
    if isinstance(ctx, web.Response):
        return ctx
    account, profile, region, bucket = ctx
    body = await _body(request)
    section = str(body.get("section", "drive"))
    if section not in storage_mod.SECTION_PREFIXES:
        return _bad_request("unknown section", "invalid_section")
    path = str(body.get("path", ""))
    err = storage_mod.validate_key(path)
    if err:
        return _bad_request(err, "invalid_key")
    try:
        # The placeholder is one object write, so it joins the key-scoped
        # protocol like an upload: shared section hold + the placeholder's
        # own key lock, with the re-authorization inside the wait.
        async with _locked_drive_write(bucket, section, path):
            denied = await _reauthorize_in_lock(
                request, "drive_folder_create", account, profile, region, bucket, publish=False
            )
            if denied:
                return denied
            await asyncio.to_thread(
                storage_mod.create_folder, profile, region, bucket, section, path, account=account
            )
    except AWSError as exc:
        return _aws_failed(exc)
    return web.json_response({"created": True, "path": path})


async def _handle_drive_folder_delete(request: web.Request) -> web.Response:
    """Delete a folder and everything under it.

    A recursive delete is a blast-radius decision, so ``path`` is validated with
    the shared :func:`storage_mod.validate_key` BEFORE it is used. That rejection
    is what makes "delete the whole section" or "delete the whole bucket"
    unreachable from here: an empty ``path`` and a bare ``/`` both fail the
    validator (empty value, leading/trailing slash), so the prefix
    :func:`storage_mod.delete_prefix` builds is always ``section/<path>/`` with a
    concrete folder name -- never the bare ``section/`` prefix and never the
    bucket root. The storage layer anchors on that trailing slash so a sibling
    folder sharing a name-prefix is not swept in, and pages the batch-delete API
    rather than assuming one request clears the folder.
    """
    ctx = await _require_drive(request)
    if isinstance(ctx, web.Response):
        return ctx
    account, profile, region, bucket = ctx
    body = await _body(request)
    section = str(body.get("section", "drive"))
    if section not in storage_mod.SECTION_PREFIXES:
        return _bad_request("unknown section", "invalid_section")
    path = str(body.get("path", ""))
    # validate_key refuses empty, '/'-only, absolute, and '..' paths — the guard
    # that keeps a folder delete from becoming a section- or bucket-wide wipe.
    err = storage_mod.validate_key(path)
    if err:
        return _bad_request(err, "invalid_key")
    try:
        # A sweep cannot enumerate the keys it will remove, so it cannot join
        # the per-key protocol — it holds the section EXCLUSIVE instead,
        # excluding every key-scoped mutation (the race this guards: sweeping
        # away a move's freshly-copied destination between the move's copy
        # and its source delete, losing the file at both ends). The wait for
        # in-flight writers to drain is a post-authorization gap like any
        # other, so the full re-check runs inside.
        async with _locked_drive_sweep(bucket, section):
            denied = await _reauthorize_in_lock(
                request, "drive_folder_delete", account, profile, region, bucket, publish=False
            )
            if denied:
                return denied
            removed = await asyncio.to_thread(
                storage_mod.delete_prefix, profile, region, bucket, section, path, account=account
            )
    except AWSError as exc:
        return _aws_failed(exc)
    return web.json_response({"deleted": True, "path": path, "objects": removed})


async def _handle_drive_share(request: web.Request) -> web.Response:
    """Mint a presigned link + ledger entry. The URL is returned ONCE and
    never persisted — the ledger keeps metadata only (see shares.py)."""
    ctx = await _require_drive(request)
    if isinstance(ctx, web.Response):
        return ctx
    account, profile, region, bucket = ctx
    # A presigned link makes the object reachable by anyone holding the URL
    # — the same bytes-leave-the-box decision class as a publish.
    denied = await _publish_gate(request, "drive_share")
    if denied:
        return denied
    body = await _body(request)
    section = str(body.get("section", "drive"))
    if section not in storage_mod.SECTION_PREFIXES:
        return _bad_request("unknown section", "invalid_section")
    if section == "backup":
        # Backup archives hold raw gateway state (sessions, memory, workspace)
        # and stay owner-only by construction: no share, no presign, ever.
        return _forbidden("backups cannot be shared", "backup_not_shareable")
    key = str(body.get("key", ""))
    err = storage_mod.validate_key(key)
    if err:
        return _bad_request(err, "invalid_key")
    try:
        expires = body_int(body.get("expiresSecs", 24 * 3600))
    except (TypeError, ValueError):
        return _bad_request("expiresSecs must be a number", "invalid_expiry")
    expires = max(60, min(expires, storage_mod.PRESIGN_MAX_SECS))
    note = str(body.get("note", ""))
    try:
        # The mint joins the coordination net: holding the KEY for the whole
        # exists-check -> presign -> ledger-record sequence means a share cannot
        # land on a file mid-move (the race: move checks the share
        # ledger, THEN this mints a share for the source, THEN the move
        # deletes it — a broken URL the ledger reports live until expiry).
        # With the lock, the mint either completes before the move's ledger
        # check (the move then refuses with share_active) or starts after the
        # move finished (the exists check then answers 404, no phantom
        # entry). The wait can queue behind a large same-key upload, so the
        # authorization is re-run inside.
        async with _locked_drive_write(bucket, section, key):
            # publish=True: the entry gate above ran _publish_gate — a share
            # is a bytes-leave-the-box decision — so the in-lock re-check must
            # re-run the SAME set. Policy revoked during the lock wait would
            # otherwise still mint a bearer URL despite the new denial.
            denied = await _reauthorize_in_lock(
                request, "drive_share", account, profile, region, bucket, publish=True
            )
            if denied:
                return denied
            exists = await asyncio.to_thread(
                storage_mod.object_exists,
                profile,
                region,
                bucket,
                section,
                key,
                account=account,
            )
            if not exists:
                # Presigning is local — without this check a typo'd key mints
                # a URL that 404s for the recipient AND a phantom ledger entry.
                return _not_found("no such file to share", "unknown_object")
            url = await asyncio.to_thread(
                storage_mod.presign, profile, region, bucket, section, key, expires
            )
            record = await asyncio.to_thread(
                shares_mod.record_share,
                account=account,
                section=section,
                key=key,
                expires_secs=expires,
                note=note,
            )
    except AWSError as exc:
        return _aws_failed(exc)
    except json.JSONDecodeError:
        # record_share is the ledger write inside the lock. The URL was minted
        # (presigning is local math, nothing durable exists in AWS) but the
        # ledger refused to record it, so it must NOT be handed out: returning
        # it would create a live unrevokable bearer grant with no local record
        # -- the exact under-reporting the strict reader exists to prevent.
        return _ledger_corrupt("share_ledger_corrupt")
    return web.json_response({"url": url, "share": record})


async def _drive_object_keys(request: web.Request, account: str) -> tuple[set[str] | None, str]:
    """Every key in one account's drive, or ``(None, reason)`` when unreadable.

    The remote half of the shares render, and NON-FATAL by contract: the Access
    section must keep listing the ledger for an account that is disconnected,
    has not confirmed S3, or has no drive yet. Every failure comes back as a
    reason the caller reports, never as a response and never as an empty set —
    "the drive holds nothing" would mark every share broken, so a listing that
    cannot be read must not be able to say it. The same shape
    :func:`_reconciled_remote_slugs` uses for the Library.

    No lock and no re-authorization, unlike that function, because this one
    WRITES NOTHING. It has no ledger critical section to close and no post-wait
    gap to re-check: the consent and identity resolved here are the ones the
    listing immediately runs under, exactly as ``_handle_drive_list`` lists
    under ``_require_drive``.

    No publish gate either. That gate governs bytes LEAVING the box; a LIST of
    key names into the account is the same read class as the Library render,
    which also passes ``publish=False``.
    """
    target = await _resolve_target(account)
    if isinstance(target, web.Response):
        # A permission DECISION, even on a route that degrades instead of
        # failing: the profile became unavailable or now names another account.
        # `_guarded`'s rule is that such a decision reaches SEL, and degrading
        # quietly would drop the one event an incident review asks about.
        # `_audit` routes the SEL write off the loop itself.
        await _audit("shares_list", request.path, "denied", error="account_unavailable")
        return None, "no working connection for this account"
    _account, profile, region = target
    if await _consent(aws_consent.SERVICE_S3, profile, region) is not None:
        return None, "S3 is not confirmed for the account in use"
    try:
        bucket = await _drive_bucket(account, profile, region)
    except AWSError as exc:
        return None, _safe_error(exc)
    if not bucket:
        return None, storage_mod.no_drive_message(region)
    try:
        keys = await asyncio.to_thread(
            storage_mod.list_object_keys, profile, region, bucket, account=account
        )
    except AWSError as exc:
        return None, _safe_error(exc)
    return keys, ""


async def _handle_shares_list(request: web.Request) -> web.Response:
    """The share ledger, with each row checked against the object it names.

    A row survives the object it points at: nothing prunes the ledger on a
    delete, and until this check existed ``GET /shares`` answered "what have I
    made reachable from outside this box" with links that resolve to nothing.
    The row is MARKED (``objectMissing``) rather than dropped —
    :func:`shares.mark_missing_objects` carries the reasoning, and the short
    version is that a deleted object does not un-mint an unexpired URL.

    ``checked`` says whether the rows in this payload were actually compared
    against the drive; a client that cannot tell "the object is there" from "the
    drive was not read" would render the second as the first. It is vacuously
    true for an empty ledger: there was no claim to check, and no listing is
    taken for one.

    WHY the check did not run is LOGGED, not sent. It is a backend-authored
    English sentence and this surface is rendered in thirteen locales, so the
    console shows a translated "not checked" line gated on ``checked`` -- the
    same resolution the Library's ``remoteError`` reaches. Putting the reason in
    the payload as well only added a field nothing reads.

    ORDER IS LOAD-BEARING: the ledger is read BEFORE the listing. Every row in
    hand therefore predates the listing, so none of them can be a share minted
    while it was in flight and wrongly marked for being absent from it. That is
    the race ``library.reconcile`` needs an ``observed_at`` cutoff for; reading
    in this order removes it instead of guarding it. Do not reorder these.
    """
    account = request.rel_url.query.get("account", "")
    entries = await asyncio.to_thread(shares_mod.list_shares, account)
    if not account:
        # Unscoped, so the rows can span accounts and there is no single drive
        # to read. The dashboard always scopes; this stays answerable anyway.
        return web.json_response({"shares": entries, "checked": False})
    if not entries:
        return web.json_response({"shares": entries, "checked": True})
    keys, reason = await _drive_object_keys(request, account)
    if keys is None:
        logger.info("aws-control shares: the objects were not checked (%s)", reason)
        return web.json_response({"shares": entries, "checked": False})
    marked = await asyncio.to_thread(shares_mod.mark_missing_objects, entries, keys)
    return web.json_response({"shares": marked, "checked": True})


async def _handle_share_forget(request: web.Request) -> web.Response:
    share_id = request.match_info.get("id", "")
    try:
        removed = await asyncio.to_thread(shares_mod.forget_share, share_id)
    except json.JSONDecodeError:
        return _ledger_corrupt("share_ledger_corrupt")
    if removed is None:
        return _not_found("unknown share", "unknown_share")
    return web.json_response({"forgotten": True})
