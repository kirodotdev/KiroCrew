"""Drive routes: the drive's status and bootstrap, and listing, download, preview and
search.

``_require_drive`` is here too: the check every handler that needs an existing drive runs
first, which the Drive write, Library and backup owners import from this module.
"""

from __future__ import annotations

import asyncio
import time

from aiohttp import web

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import storage as storage_mod
from kiro_crew.apps.builtins.aws_control.backend.routes_parts.common import (
    _DOWNLOAD_URL_SECS,
    _PREVIEW_MAX_BYTES,
    _PREVIEW_REDACT_LOOKAHEAD,
    _PREVIEW_TRIM_SEARCH,
    _USAGE_TTL_SECS,
    APP_NAME,
    _account_target,
    _audit,
    _aws_failed,
    _bad_request,
    _body,
    _bootstrap_lock,
    _conflict,
    _consent,
    _drive_bucket,
    _forbidden,
    _not_found,
    _publish_gate,
    _response_code,
    _safe_error,
    _usage_cache,
    _valid_section,
    logger,
)
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.deploy.engine import AWSError
from kiro_crew.security import (  # noqa: F401 - pinned by test_routes_parts_facade.py
    redact_credentials,
    redact_exfiltration_urls,
)

# --------------------------------------------------------------------------
# Drive
# --------------------------------------------------------------------------


async def _handle_drive_status(request: web.Request) -> web.Response:
    target = await _account_target(request)
    if isinstance(target, web.Response):
        return target
    account, profile, region = target
    denied = await _consent(aws_consent.SERVICE_S3, profile, region)
    if denied:
        return denied
    try:
        bucket = await _drive_bucket(account, profile, region)
    except AWSError as exc:
        return _aws_failed(exc)
    if not bucket:
        return web.json_response(
            {"exists": False, "searched_region": storage_mod.searched_region(region)}
        )
    refresh = request.rel_url.query.get("refresh") == "1"
    cached = _usage_cache.get(account)
    if cached and not refresh and (time.monotonic() - cached[0]) < _USAGE_TTL_SECS:
        usage = cached[1]
    else:
        try:
            usage = await asyncio.to_thread(
                storage_mod.usage, profile, region, bucket, account=account
            )
        except AWSError as exc:
            return _aws_failed(exc)
        _usage_cache[account] = (time.monotonic(), usage)
    # The bucket name feeds the under-the-hood drawer — engineer-facing truth,
    # deliberately part of the payload (spec §2.4), never secret.
    return web.json_response({"exists": True, "bucket": bucket, "region": region, "usage": usage})


async def _handle_drive_bootstrap(request: web.Request) -> web.Response:
    """Two-call confirm: preview first, ``{"confirm": true}`` executes.

    Creation is serialized and existence re-checked INSIDE the lock: two
    concurrent confirms would otherwise both see no drive and create two
    tagged buckets — which discovery then refuses as ambiguous, bricking
    the drive until someone untangles the tags by hand.
    """
    target = await _account_target(request)
    if isinstance(target, web.Response):
        return target
    account, profile, region = target
    denied = await _consent(aws_consent.SERVICE_S3, profile, region)
    if denied:
        return denied
    body = await _body(request)
    if body.get("confirm") is not True:
        existing = await _drive_bucket(account, profile, region)
        if existing:
            return _conflict("this account already has a drive", "drive_exists")
        return web.json_response(
            {
                "preview": True,
                "account": account,
                "region": region,
                "resource": "one private storage bucket (encrypted, versioned)",
            }
        )
    async with _bootstrap_lock:
        existing = await _drive_bucket(account, profile, region)
        if existing:
            return _conflict("this account already has a drive", "drive_exists")
        # Re-authorize INSIDE the lock, immediately before the billable call.
        # The checks above ran before the lock, and what sits between them and
        # this line is an unbounded lock wait plus a tag-discovery round trip to
        # AWS -- a real suspension point. A profile repointed in that window
        # would have `create_drive` make and bill a bucket in an account the
        # owner never confirmed. `_account_target` re-probes the live identity,
        # so requiring it to still resolve the SAME triple is what closes it;
        # consent is re-read for the same reason it is re-read before an upload.
        #
        # Each denial audits at the POINT OF DECISION, like the helper's arms
        # do: `_mutating` records the outcome of whatever response leaves here,
        # but only a `denied` record naming the reason tells an incident review
        # why a bucket the owner confirmed was never created.
        recheck = await _account_target(request)
        if isinstance(recheck, web.Response):
            await _audit("drive_bootstrap", request.path, "denied", error=_response_code(recheck))
            return recheck
        if recheck != (account, profile, region):
            await _audit("drive_bootstrap", request.path, "denied", error="account_mismatch")
            return _conflict(
                "this connection changed while the drive was being created; " "nothing was created",
                "account_mismatch",
            )
        denied = await _consent(aws_consent.SERVICE_S3, profile, region)
        if denied:
            return denied

        # The app gate is the LAST thing before the billable call, deliberately.
        # `_guarded` already refused a disabled app before the lock, so this one
        # exists only for the app being switched off DURING the critical section --
        # and every await above it (the unbounded lock wait, the tag-discovery
        # round trip, the live identity re-probe, the consent read) is a point at
        # which that can happen. A gate placed at the top of the lock is stale by
        # the time `create_drive` runs, which is the whole failure it was added to
        # prevent: a bucket billed for a surface the owner has already switched off.
        #
        # Read and create share ONE thread hop, so there is no await BETWEEN them
        # at which the loop could run anything. `create_drive` returns the bucket
        # name, so `None` is an unambiguous "refused, nothing created".
        #
        # A re-check, NOT a lock. `app_lifecycle_lock` is an in-process asyncio
        # lock covering only the HTTP disable path, while `kirocrew app disable`
        # runs in a SEPARATE process calling `disable_app` directly
        # (`cli_commands.py` imports it), so no in-process lock serializes the
        # writer this would have to wait on. Closing the cross-process window
        # needs a cross-process lock this repo does not have; a re-check is the
        # form the repo already accepts here (see
        # `pptx_maker/backend/provision.py`, and the 9 `_reauthorize_in_lock` call
        # sites, which all keep the same window).
        #
        # `create_drive` re-checks the bucket's owner against this account once
        # it exists: its own CLI child resolves the profile independently, so
        # matching triples here cannot promise where the bucket lands.
        def _create_drive_if_still_enabled() -> str | None:
            if not is_app_enabled(APP_NAME):
                return None
            return storage_mod.create_drive(profile, region, account)

        bucket = await asyncio.to_thread(_create_drive_if_still_enabled)
        if bucket is None:
            await _audit("drive_bootstrap", request.path, "denied", error="app_disabled")
            return _forbidden("aws-control is disabled", "app_disabled")
    return web.json_response({"created": True, "bucket": bucket})


async def _require_drive(request: web.Request) -> tuple[str, str, str, str] | web.Response:
    """account/profile/region/bucket for handlers that need an existing drive."""
    target = await _account_target(request)
    if isinstance(target, web.Response):
        return target
    account, profile, region = target
    denied = await _consent(aws_consent.SERVICE_S3, profile, region)
    if denied:
        return denied
    try:
        bucket = await _drive_bucket(account, profile, region)
    except AWSError as exc:
        return _aws_failed(exc)
    if not bucket:
        return _conflict(storage_mod.no_drive_message(region), "drive_missing")
    return account, profile, region, bucket


async def _handle_drive_list(request: web.Request) -> web.Response:
    ctx = await _require_drive(request)
    if isinstance(ctx, web.Response):
        return ctx
    account, profile, region, bucket = ctx
    section = _valid_section(request)
    if isinstance(section, web.Response):
        return section
    subpath = request.rel_url.query.get("path", "")
    if subpath:
        err = storage_mod.validate_key(subpath)
        if err:
            return _bad_request(err, "invalid_key")
    token = request.rel_url.query.get("token", "")
    try:
        page = await asyncio.to_thread(
            storage_mod.list_section,
            profile,
            region,
            bucket,
            section,
            subpath,
            token,
            account=account,
        )
    except AWSError as exc:
        return _aws_failed(exc)
    return web.json_response(page)


async def _handle_drive_download(request: web.Request) -> web.Response:
    """A short-lived URL, minted per click — the JSON cousin of a redirect."""
    ctx = await _require_drive(request)
    if isinstance(ctx, web.Response):
        return ctx
    account, profile, region, bucket = ctx
    # Same decision class as a share: the presigned URL works for anyone
    # holding it, so the bytes become reachable outside the box.
    denied = await _publish_gate(request, "drive_download")
    if denied:
        return denied
    section = _valid_section(request)
    if isinstance(section, web.Response):
        return section
    if section == "backup":
        # Backups stay owner-only by construction: no share and no
        # download presign either — a bearer URL to raw gateway state is the
        # same exposure class regardless of which route mints it. Recovery
        # goes through the restore endpoint, which downloads server-side.
        return _forbidden("backups cannot be downloaded by link", "backup_not_shareable")
    key = request.rel_url.query.get("key", "")
    err = storage_mod.validate_key(key)
    if err:
        return _bad_request(err, "invalid_key")
    try:
        # Presigning is LOCAL signing - S3 is never consulted - so a stale or
        # typo'd key mints a URL that looks fine and 404s when opened. Share
        # requires this head-object; download mints the same kind of bearer URL
        # and needs the same precondition. The same HEAD carries
        # the stored Content-Type, returned so the preview can tell a real PDF
        # from a `.pdf`-named object served as octet-stream (uploaded before
        # content types were set), which a sandboxed iframe shows as blank.
        meta = await asyncio.to_thread(
            storage_mod.head_object_meta,
            profile,
            region,
            bucket,
            section,
            key,
            account=account,
        )
        if meta is None:
            return _not_found("no such file in this drive", "object_missing")
        url = await asyncio.to_thread(
            storage_mod.presign,
            profile,
            region,
            bucket,
            section,
            key,
            _DOWNLOAD_URL_SECS,
        )
    except AWSError as exc:
        return _aws_failed(exc)
    # A presign is an ACCESS GRANT (a bearer URL now exists), not a plain
    # read — record it like every other grant so the audit trail can answer
    # "what URLs were minted". The key is metadata, never the URL itself.
    # A bearer URL now exists, so the grant record must survive a client
    # disconnect: shield keeps the audit being written even when the await
    # itself is cancelled.
    await asyncio.shield(_audit("drive_download", f"{section}/{key}", "granted"))
    content_type = meta.get("ContentType")
    return web.json_response(
        {
            "url": url,
            "expiresSecs": _DOWNLOAD_URL_SECS,
            "contentType": content_type if isinstance(content_type, str) else None,
        }
    )


async def _handle_drive_preview(request: web.Request) -> web.Response:
    """Head bytes of a text object, read gateway-side and returned as JSON.

    A browser ``fetch`` against the presigned URL is blocked by CORS (the
    bucket deliberately carries none), so the gateway proxies the read. A
    plain read: no bearer URL is minted, so unlike download there is no
    publish gate and nothing to audit as a grant. Binary content is not
    detected here — the frontend decides by extension whether to call this
    endpoint at all.
    """
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
    try:
        # Same precondition as download: a typo'd key must answer 404 rather
        # than surface as an opaque transfer failure.
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
            return _not_found("no such file in this drive", "object_missing")
        data, size = await asyncio.to_thread(
            storage_mod.get_object_head_bytes,
            profile,
            region,
            bucket,
            section,
            key,
            account=account,
            # The window PLUS the redaction look-ahead: the redactor must see a
            # secret whole, and a secret that straddles the window's end would
            # otherwise reach it as a prefix it cannot recognise.
            max_bytes=_PREVIEW_MAX_BYTES + _PREVIEW_REDACT_LOOKAHEAD,
        )
    except AWSError as exc:
        return _aws_failed(exc)
    except (ValueError, OSError) as exc:
        # The staging fence refused (a link squatting the staging root, a
        # non-directory at the path) or the local filesystem failed around the
        # transfer. Neither is an AWS failure and neither is the caller's
        # doing, so it is a coded 500 the dashboard can name -- not an
        # uncoded crash. The exception text stays out of the body: an OSError
        # carries the staging path, which is gateway-internal.
        logger.warning("drive preview staging failed: %s", _safe_error(exc))
        return web.json_response(
            {"error": "the preview could not be staged locally", "code": "preview_staging_failed"},
            status=500,
        )
    # Decode, redact and trim OFF the event loop, like the sibling search's
    # redaction: the redactor's base64 passes are O(k·n) over a buffer sized
    # by the caller's window, and a base64-dense object would otherwise hold
    # every other request behind this one.
    text, truncated, redacted = await asyncio.to_thread(_redact_preview, data, size)
    return web.json_response(
        {
            "content": text,
            "truncated": truncated,
            "redacted": redacted,
        }
    )


def _redact_preview(data: bytes, size: int) -> tuple[str, bool, bool]:
    """Decode the fetched head, redact it whole, then cut it to the window.

    errors="replace" so a stray non-UTF-8 byte degrades to the replacement
    character instead of failing the whole preview. The text then passes
    through the same egress redactor as listing and search names: a file
    that happens to hold a credential renders masked in the dashboard, the
    same way it would in a chat surface. Whether a redactor FIRED is reported
    alongside, for the same reason truncation is: a reader checking a config
    file must be told the masked values are the preview's, not the file's.

    Redaction runs over the WHOLE buffer (window + look-ahead) and the cut to
    the window happens after it, so a secret crossing the window's end is
    masked before anything is trimmed. The cut then backs off to the last
    whitespace inside the tail, dropping any run the cut would split: a
    token longer than the look-ahead is the one thing the redactor could
    still have seen only partially, and a split run is never shown.
    """
    text = data.decode("utf-8", errors="replace")
    from kiro_crew.security import redact_with_findings

    text, credential_hits, url_hits = redact_with_findings(text)
    truncated = size > _PREVIEW_MAX_BYTES
    if truncated:
        text = _trim_preview(text, _PREVIEW_MAX_BYTES)
    return text, truncated, bool(credential_hits or url_hits)


def _trim_preview(text: str, limit: int) -> str:
    """Cut redacted preview text to ``limit`` UTF-8 BYTES at a whitespace boundary.

    Bytes, not characters: the window is a byte budget (it is what the fetch
    asked for and what the client is sized for), and a character count would
    let a multibyte file carry up to the whole look-ahead past it. A code point
    split by the byte cut is dropped rather than shown as a replacement glyph.

    The whitespace back-off is bounded by :data:`_PREVIEW_TRIM_SEARCH`: a
    whitespace-free blob (minified JSON) would otherwise trim to nothing, so
    past that distance the cut is a hard one -- the redactor has already seen
    everything up to the look-ahead, so a hard cut can split a placeholder but
    not a secret.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    # The text was decoded with errors="replace" and is valid UTF-8, so the only
    # thing "ignore" can drop here is the code point the byte cut split.
    shown = encoded[:limit].decode("utf-8", errors="ignore")
    boundary = max(shown.rfind(" "), shown.rfind("\n"), shown.rfind("\t"))
    if boundary >= len(shown) - _PREVIEW_TRIM_SEARCH:
        return shown[: boundary + 1]
    return shown


async def _handle_drive_search(request: web.Request) -> web.Response:
    """Case-insensitive filename search across the file-drive section.

    Only ``drive`` is searchable: the library and backups have their own
    listing surfaces, and a search reaching into backup archive keys would
    surface names the dashboard otherwise never renders. The section is still
    an explicit query parameter so the route reads like its siblings.
    """
    ctx = await _require_drive(request)
    if isinstance(ctx, web.Response):
        return ctx
    account, profile, region, bucket = ctx
    section = _valid_section(request)
    if isinstance(section, web.Response):
        return section
    if section != "drive":
        return _bad_request("only the drive section is searchable", "section_not_searchable")
    query = request.rel_url.query.get("q", "").strip()
    if not query:
        return _bad_request("q is required", "empty_query")
    try:
        results, capped = await asyncio.to_thread(
            storage_mod.search_keys,
            profile,
            region,
            bucket,
            section,
            query,
            account=account,
        )
    except AWSError as exc:
        return _aws_failed(exc)
    # ``limit`` rides along so the capped notice can say the real number: the
    # cap has one owner (storage.SEARCH_MAX_RESULTS) and the locales never
    # spell it.
    return web.json_response(
        {"results": results, "capped": capped, "limit": storage_mod.SEARCH_MAX_RESULTS}
    )
