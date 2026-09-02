"""HTTP routes for the AWS Control builtin, served in-process by the gateway.

Mounted at ``/api/apps/aws-control/`` — the same single-argument
``register_routes(app)`` convention every builtin uses (registered at startup
by the ``BUILTIN_NAMES`` loop; every handler carries its own enabled check).

Surface (owner-only throughout, see below):

READS
``GET /accounts``                              aggregated account list (?refresh=1)
``GET /profiles/{name}/reconnect-plan``        what Reconnect can offer, no action
``GET /drive/{account}``                       drive presence + cached usage
``GET /drive/{account}/list``                  one listing page (?section&path&token)
``GET /drive/{account}/download``              short-lived download URL (?section&key)
``GET /costs/{account}``                       cached bill (?refresh=1 re-queries CE)
``GET /library/{account}``                     local artifacts + reconciled push state
``GET /backup/{account}``                      backup state + remote archive listing
``GET /shares``                                live share ledger (?account=)
``GET /iam-policy``                            drive-tier policy JSON (local render)

MUTATIONS (also restricted-session refused + SEL-audited)
``POST /drive/{account}/bootstrap``            create the bucket (two-call confirm)
``POST /drive/{account}/upload``               upload one file (?section&key, raw body)
``POST /drive/{account}/delete``               delete one object
``POST /drive/{account}/folder``               create an empty folder (placeholder)
``POST /drive/{account}/folder/delete``        delete a folder and all its objects
``POST /drive/{account}/share``                mint a presigned share + ledger entry
``POST /shares/{id}/forget``                   drop a ledger entry (link lives to expiry)
``POST /library/{account}/push``               push one artifact to the cloud library
``POST /library/{account}/remove``             delete a cloud copy + forget its record
``POST /backup/{account}/run``                 run a backup (snapshot | sessions)
``POST /backup/{account}/nightly``             toggle the nightly snapshot
``POST /backup/{account}/restore``             download an archive to the staging dir

GUARDS, in order, and why each exists:

1. **Enabled check** — a disabled app answers 403 ``app_disabled`` to
   everyone, so a probe cannot tell "app off" from "not for you".
2. **Owner-only, INCLUDING reads.** Account ids and caller ARNs are what the
   keystone-fenced consent leaf is fenced FROM; a surface printing them for
   any authenticated app token would hand out through one door what is
   locked behind another. ``is_owner_dashboard_request`` encodes the rule
   (same as ``aws_consent`` and ``mcp_apps``).
3. **Consent** — every handler that reaches AWS calls
   ``aws_consent.refuse_and_log`` (``s3`` or ``ce``) with the profile+region
   it is about to use. Fails closed; the page renders the consent card.
4. **Restricted-session refusal + SEL audit** on every mutation, matching
   the deploy handlers' discipline.
5. **Two-call confirm on billable resource creation** (bucket bootstrap):
   the first call returns a preview, only ``{"confirm": true}`` executes.
   There is no agent path to these endpoints at all (guard 2), so the
   dashboard click IS the human confirmation the spec's G1 requires.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import shutil
import tempfile
import time
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod
from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod
from kiro_crew.apps.builtins.aws_control.backend import costs as costs_mod
from kiro_crew.apps.builtins.aws_control.backend import library as library_mod
from kiro_crew.apps.builtins.aws_control.backend import shares as shares_mod
from kiro_crew.apps.builtins.aws_control.backend import storage as storage_mod
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import (
    is_owner_dashboard_request,
)
from kiro_crew.deploy import profiles as deploy_profiles
from kiro_crew.deploy.engine import AWSError
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.publish_governance import publish_denied_reason
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

APP_NAME = "aws-control"
_BASE = f"/api/apps/{APP_NAME}"

#: Upload ceiling. Multipart s3 cp handles far more, but a dashboard upload
#: buffered through a temp file wants a bound; big data belongs to backups.
_MAX_UPLOAD_BYTES = 512 * 1024 * 1024

#: Download links are redirects in spirit: minted per click, short-lived.
_DOWNLOAD_URL_SECS = 60

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

#: account -> (monotonic stamp, usage payload). A full-bucket LIST per
#: console render is the kind of quiet quadratic the consent audit log would
#: never show; five minutes of staleness on a storage total is invisible.
#: Deliberately NO bucket-name cache: tag discovery is a TRUST decision
#: (uploads/deletes operate on what it returns), and a cached name outlives
#: an out-of-band delete + hostile re-creation of the same name. Numbers can
#: be stale; the identity of the bucket we write to cannot.
#: Serializes drive creation (see _handle_drive_bootstrap). LoopBoundLock so
#: the module global never binds an import-time loop (#4800).
#: How long the RENDER path waits for the Library lock before giving up on the
#: reconcile. The lock is also held across a push, whose upload allows up to 600s,
#: so waiting on it unbounded would let one large push hang every Library page
#: render for the length of that upload. Errors on this path already degrade to
#: `reconciled: false`; slowness has to degrade the same way or the degradation is
#: only half real. Skipping costs nothing durable -- the reconcile is
#: self-correcting, so the next render does it.
_LIBRARY_RECONCILE_LOCK_WAIT_SECS = 5.0

_bootstrap_lock = LoopBoundLock()

#: Serializes the Library's own operations on one drive: a push, a removal, and
#: the reconcile that repairs the ledger. Each is a network round trip followed
#: by a ledger write, and interleaving two of them corrupts state that neither
#: half can detect on its own -- a push completing between the reconcile's
#: listing and its prune has its fresh record deleted, and a push racing a
#: removal of the same slug can leave one uploaded object behind the delete
#: sweep. The ledger's own file lock cannot serialize these: it deliberately
#: covers a sub-second read plus rename, and stretching it across S3 would make
#: a concurrent caller fail closed on Windows.
#:
#: Coarse on purpose -- one lock, not one per slug. This is an owner-only
#: surface where a human clicks buttons, so the contention is theoretical, and a
#: per-slug map would need eviction to stay bounded. LoopBoundLock for the same
#: reason ``_bootstrap_lock`` uses it: a module global must not bind an
#: import-time loop (#4800).
_library_lock = LoopBoundLock()

#: The provider name this app's egress paths answer to under the shared
#: publish-governance gate (``capabilities.publish`` ∩ ``destinations:<id>``).
#: Ungoverned standalone installs permit it; a governance profile that denies
#: publishing denies these routes the same way it denies deploy-web.
_PUBLISH_PROVIDER_ID = "aws-control-drive"


async def _publish_gate(request: web.Request, operation: str) -> web.Response | None:
    """The shared fail-closed publish-governance decision, for the two routes
    where artifact/file bytes become reachable outside the box (library push
    to S3, presigned share links). Off the loop: it reads the trust-root
    policy, every governance profile, and config.json from disk."""
    reason = await asyncio.to_thread(publish_denied_reason, request, _PUBLISH_PROVIDER_ID)
    if reason:
        _audit(operation, request.path, "denied", error=reason)
        return _forbidden(f"publishing is disabled by policy: {reason}", "publish_denied")
    return None


_usage_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_USAGE_TTL_SECS = 300.0


def _bad_request(message: str, code: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=400)


def _forbidden(message: str, code: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=403)


def _not_found(message: str, code: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=404)


def _conflict(message: str, code: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=409)


def _safe_error(exc: BaseException) -> str:
    """Error text fit for a response body.

    AWS CLI stderr is engine-trimmed but still echoes what the CLI printed —
    a failed call can quote back a ``credential_process`` command line, an
    SSO URL, or an endpoint override carrying inline credentials — so every
    outbound error runs BOTH passes: credentials, then exfiltration URLs.
    Same chain the deploy handlers apply at their own boundary.
    """
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    text, _ = redact_credentials(str(exc))
    text, _ = redact_exfiltration_urls(text)
    return text


def _aws_failed(exc: AWSError) -> web.Response:
    return web.json_response({"error": _safe_error(exc), "code": "aws_call_failed"}, status=502)


def _audit(operation: str, resources: str, outcome: str, *, error: str = "") -> None:
    """Best-effort SEL audit for mutations; never blocks the response."""
    try:
        sel().log_api_access(
            caller="dashboard-owner",
            operation=f"aws_control.{operation}",
            outcome=outcome,
            source=APP_NAME,
            resources=resources[:200],
            error=error[:200],
        )
    except Exception:
        logger.debug("aws-control SEL audit failed", exc_info=True)


def _guarded(handler: Handler) -> Handler:
    """Enabled check + owner check — the wrapper every route goes through."""

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            # Same rule as the owner denial below: a permission DECISION must
            # reach SEL — a request arriving while the app is disabled is a
            # denial an incident review asks about.
            _audit("access", request.path, "denied", error="app_disabled")
            return _forbidden("aws-control is disabled", "app_disabled")
        if not is_owner_dashboard_request(request):
            logger.warning(
                "refused aws-control access: account portal is a dashboard "
                "owner surface (app=%s)",
                request.get("app"),
            )
            # The permission DECISION must reach SEL, not just the logger —
            # an app token probing the account portal is exactly the event
            # an incident review asks about. Before either response shape.
            _audit(
                "access",
                request.path,
                "denied",
                error=f"non-owner caller (app={request.get('app') or ''})",
            )
            # Stale-session relabel + 403 via the shared helper's tail pattern.
            return _owner_denial_response(
                request, "dashboard owner required", "dashboard_owner_required"
            )
        return await handler(request)

    return _wrapped


def _mutating(operation: str) -> Callable[[Handler], Handler]:
    """Restricted-session refusal + SEL audit around a mutation handler."""

    def _decorate(handler: Handler) -> Handler:
        @wraps(handler)
        async def _wrapped(request: web.Request) -> web.StreamResponse:
            from kiro_crew.dashboard.handlers._shared import _is_restricted_session

            state = request.app.get("state")
            if state is not None and _is_restricted_session(state, request):
                _audit(operation, request.path, "denied", error="restricted session")
                return _forbidden(
                    "this session is restricted from AWS mutations",
                    "restricted_session",
                )
            try:
                response = await handler(request)
            except AWSError as exc:
                _audit(operation, request.path, "error", error=str(exc))
                return _aws_failed(exc)
            _audit(
                operation,
                request.path,
                "success" if response.status < 400 else "refused",
            )
            return response

        return _wrapped

    return _decorate


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def _account_target(request: web.Request) -> tuple[str, str, str] | web.Response:
    """Resolve ``{account}`` to (account, profile, region), or an error response.

    The snapshot maps profiles to accounts with a five-minute TTL, and a
    profile can be repointed at a different account inside that window — so
    the mapping alone must never pick which account an operation runs
    against. A LIVE identity probe re-verifies that the chosen profile still
    resolves to the REQUESTED account, and a mismatch refuses rather than
    executing against whatever the profile now points at. The probe's short
    cache (~30s) bounds the cost without reopening the five-minute window.
    """
    account = request.match_info.get("account", "")
    if not (account.isdigit() and len(account) == 12):
        return _bad_request("account must be a 12-digit id", "invalid_account")
    resolved = await accounts_mod.resolve_account_profile(account)
    if resolved is None:
        return _conflict(
            "no working connection for this account — reconnect first",
            "account_unavailable",
        )
    profile, region = resolved
    identity = await aws_consent.probe_identity(profile, region)
    if not identity.ok or identity.account != account:
        return _conflict(
            "this connection no longer points at the requested account — "
            "refresh the accounts page",
            "account_mismatch",
        )
    return account, profile, region


async def _consent(service: str, profile: str, region: str) -> web.Response | None:
    """The consent gate every AWS-reaching handler runs first."""
    allowed = await aws_consent.refuse_and_log(service, profile=profile, region=region)
    if not allowed:
        return web.json_response(
            {
                "error": "this paid service is not confirmed for the account in use",
                "code": "aws_consent_required",
                "service": service,
            },
            status=409,
        )
    return None


async def _drive_bucket(account: str, profile: str, region: str) -> str | None:
    """The account's drive bucket, tag-discovered on EVERY call.

    No cache on purpose (see the module-level note): the name this returns
    is what uploads and deletes trust, so it must reflect the tags as they
    are now, not as they were when some earlier request looked.
    """
    return await asyncio.to_thread(storage_mod.find_drive, profile, region, account=account)


def _valid_section(request: web.Request) -> str | web.Response:
    section = request.rel_url.query.get("section", "drive")
    if section not in storage_mod.SECTION_PREFIXES:
        return _bad_request("unknown section", "invalid_section")
    return section


# --------------------------------------------------------------------------
# Accounts + reconnect (foundations)
# --------------------------------------------------------------------------


async def _handle_accounts(request: web.Request) -> web.Response:
    refresh = request.rel_url.query.get("refresh") == "1"
    snapshot = await accounts_mod.list_accounts(refresh=refresh)
    return web.json_response(snapshot)


async def _handle_reconnect_plan(request: web.Request) -> web.Response:
    """Classification + guidance, no action. Registered profiles only, so
    attacker-shaped names are never echoed into a guidance card."""
    name = request.match_info.get("name", "")
    reg = await asyncio.to_thread(deploy_profiles.load_registry)
    entry = deploy_profiles.get_entry(reg, name)
    if entry is None:
        return _not_found("unknown profile", "unknown_profile")
    kind = await accounts_mod.classify_profile(name)
    plan = accounts_mod.reconnect_plan(kind, name)
    return web.json_response(plan)


#: Ceiling on registered profiles, matching the deploy registry's own cap. The
#: portal must not be the surface that grows the registry past what deploy will
#: accept, or a profile registered here becomes unusable there.
_MAX_REGISTERED = 50


async def _handle_profiles_available(request: web.Request) -> web.Response:  # noqa: ARG001
    """Profile NAMES the AWS CLI knows, annotated with whether we registered them.

    This is the answer to "why can I not see my accounts": the portal only ever
    showed the REGISTERED set, and nothing on the page listed what else exists,
    so an operator with many local profiles had no way in.

    Names only, and not read from disk -- ``discover_aws_profiles`` shells
    ``aws configure list-profiles``, which enumerates the sections itself, so no
    credential file is ever opened here. The call is local and free, so it needs
    no consent gate. Names still go through the redactor: a profile name is
    operator-authored text on a display path, the same reason the account
    snapshot redacts its own.
    """
    available = await asyncio.to_thread(deploy_profiles.discover_aws_profiles)
    reg = await asyncio.to_thread(deploy_profiles.load_registry)
    registered = {str(p.get("name", "")) for p in reg.get("profiles", [])}
    rows = [
        {"name": accounts_mod._safe_field(name), "registered": name in registered}
        for name in available
        if aws_consent._PROFILE_RE.match(name)
    ]
    return web.json_response(
        {
            "profiles": rows,
            "registeredCount": len(registered),
            "max": _MAX_REGISTERED,
            # An empty list on a host that HAS profiles is the Windows case
            # (discovery is POSIX-only), and the UI must say so rather than
            # implying the operator has none.
            "supported": os.name != "nt",
        }
    )


async def _handle_profiles_register(request: web.Request) -> web.Response:
    """Register selected LOCAL profiles into the registry the portal reads.

    Three refusals matter more than the happy path:

    * A name must be one ``aws configure list-profiles`` actually reports. The
      registry is agent-writable and its names reach an argv, so accepting an
      arbitrary string here would let a caller plant one -- the same class the
      read-side validation in ``accounts.py`` closes from the other end.
    * The name must match the shared profile pattern, checked before it is
      compared against anything.
    * The registry cap is enforced across the whole batch, so a partial batch
      registers the prefix that fits rather than silently overflowing.

    Registration records NO account id: the account is whatever a live identity
    probe resolves, and writing a guessed one would seed the very stale mapping
    the drive routes re-probe to avoid. The REGION is the opposite case and is
    recorded: it is a value the profile states about itself, and leaving it empty
    makes ``make_entry`` substitute ``DEFAULT_REGION``, which would create the
    drive bucket in the wrong region for any profile configured elsewhere.
    """
    body = await _body(request)
    raw = body.get("names")
    if not isinstance(raw, list) or not raw:
        return _bad_request("names must be a non-empty list", "invalid_names")
    requested = [str(n) for n in raw][:_MAX_REGISTERED]

    available = set(await asyncio.to_thread(deploy_profiles.discover_aws_profiles))
    unknown = [n for n in requested if n not in available]
    if unknown:
        # Deliberately does not echo the rejected names: they are caller-supplied
        # and this response is a display surface.
        return _bad_request(
            f"{len(unknown)} name(s) are not profiles on this machine",
            "unknown_profile",
        )
    if any(not aws_consent._PROFILE_RE.match(n) for n in requested):
        return _bad_request("a profile name is not in the accepted form", "invalid_names")

    # Read each profile's own declared region BEFORE taking the registry lock:
    # these are subprocess round trips and must not be held under it.
    regions = {n: await accounts_mod.configured_region(n) for n in requested}

    added: list[str] = []
    skipped: list[str] = []

    def _mutate() -> None:
        with deploy_profiles.locked_registry() as reg:
            existing = {str(p.get("name", "")) for p in reg.get("profiles", [])}
            for name in requested:
                if name in existing:
                    skipped.append(name)
                    continue
                if len(reg["profiles"]) >= _MAX_REGISTERED:
                    skipped.append(name)
                    continue
                reg["profiles"].append(deploy_profiles.make_entry(name, regions.get(name, "")))
                existing.add(name)
                added.append(name)
            if not reg.get("default") and reg["profiles"]:
                reg["default"] = reg["profiles"][0]["name"]

    await asyncio.to_thread(_mutate)
    # The snapshot is TTL-cached, so without this the page the operator just
    # registered from would keep showing the old set for up to five minutes.
    accounts_mod.invalidate_cache()
    return web.json_response({"added": len(added), "skipped": len(skipped)})


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
        return web.json_response({"exists": False})
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
        recheck = await _account_target(request)
        if isinstance(recheck, web.Response):
            return recheck
        if recheck != (account, profile, region):
            return _conflict(
                "this connection changed while the drive was being created; " "nothing was created",
                "account_mismatch",
            )
        denied = await _consent(aws_consent.SERVICE_S3, profile, region)
        if denied:
            return denied
        # `create_drive` re-checks the bucket's owner against this account once
        # it exists: its own CLI child resolves the profile independently, so
        # matching triples here cannot promise where the bucket lands.
        bucket = await asyncio.to_thread(storage_mod.create_drive, profile, region, account)
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
        return _conflict("this account has no drive yet", "drive_missing")
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
        # Backups stay owner-only by construction: no share (round 8) and no
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
        # typo'd key mints a URL that looks fine and 404s when opened. Share has
        # required this head-object since round 3; download mints the same kind
        # of bearer URL and needs the same precondition.
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
    _audit("drive_download", f"{section}/{key}", "granted")
    return web.json_response({"url": url, "expiresSecs": _DOWNLOAD_URL_SECS})


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
        # A 512 MB stream can take minutes: the authorization resolved before
        # the transfer may no longer hold. The spool is the same post-wait gap
        # the Library operations cross under their lock, so the SAME helper
        # re-runs the full re-authorization: app gate, live identity re-probe,
        # consent, and the drive bucket itself -- the piece an identity check
        # cannot cover, because tag discovery can move the drive to a different
        # bucket while the identity is unchanged, and a name held across the
        # spool is exactly the staleness the module's no-cache rule forbids. A
        # pass means the pre-spool ``bucket`` still names the account's current
        # drive, so the put below writes to the post-spool resolution; anything
        # else is refused with nothing written. No publish gate: an upload does
        # not consult it on the way in, so the re-check does not add it.
        denied = await _reauthorize_in_lock(
            request, "drive_upload", account, profile, region, bucket, publish=False
        )
        if denied:
            return denied
        try:
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
        await asyncio.to_thread(
            storage_mod.delete_key, profile, region, bucket, section, key, account=account
        )
    except AWSError as exc:
        return _aws_failed(exc)
    return web.json_response({"deleted": True, "key": key})


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
        expires = int(body.get("expiresSecs", 24 * 3600))
    except (TypeError, ValueError):
        return _bad_request("expiresSecs must be a number", "invalid_expiry")
    expires = max(60, min(expires, storage_mod.PRESIGN_MAX_SECS))
    note = str(body.get("note", ""))
    try:
        exists = await asyncio.to_thread(
            storage_mod.object_exists,
            profile,
            region,
            bucket,
            section,
            key,
            account=account,
        )
    except AWSError as exc:
        return _aws_failed(exc)
    if not exists:
        # Presigning is local — without this check a typo'd key mints a URL
        # that 404s for the recipient AND a phantom ledger entry.
        return _not_found("no such file to share", "unknown_object")
    try:
        url = await asyncio.to_thread(
            storage_mod.presign, profile, region, bucket, section, key, expires
        )
    except AWSError as exc:
        return _aws_failed(exc)
    record = await asyncio.to_thread(
        shares_mod.record_share,
        account=account,
        section=section,
        key=key,
        expires_secs=expires,
        note=note,
    )
    return web.json_response({"url": url, "share": record})


async def _handle_shares_list(request: web.Request) -> web.Response:
    account = request.rel_url.query.get("account", "")
    entries = await asyncio.to_thread(shares_mod.list_shares, account)
    return web.json_response({"shares": entries})


async def _handle_share_forget(request: web.Request) -> web.Response:
    share_id = request.match_info.get("id", "")
    removed = await asyncio.to_thread(shares_mod.forget_share, share_id)
    if removed is None:
        return _not_found("unknown share", "unknown_share")
    return web.json_response({"forgotten": True})


# --------------------------------------------------------------------------
# Costs (Bill)
# --------------------------------------------------------------------------


async def _handle_costs(request: web.Request) -> web.Response:
    target = await _account_target(request)
    if isinstance(target, web.Response):
        return target
    account, profile, region = target
    cached = await asyncio.to_thread(costs_mod.read_cached, account)
    refresh = request.rel_url.query.get("refresh") == "1"
    if costs_mod.is_fresh(cached) and not refresh:
        return web.json_response({"fresh": True, **(cached or {})})
    denied = await _consent(aws_consent.SERVICE_COST_EXPLORER, profile, region)
    if denied:
        # Stale-but-present beats nothing: label the age, keep the page alive.
        if cached:
            return web.json_response({"fresh": False, "consentMissing": True, **cached})
        return denied
    try:
        result = await asyncio.to_thread(costs_mod.fetch_month_costs, profile, region, account)
    except AWSError as exc:
        if cached:
            return web.json_response({"fresh": False, "fetchError": _safe_error(exc), **cached})
        return _aws_failed(exc)
    return web.json_response({"fresh": True, **result})


# --------------------------------------------------------------------------
# Library
# --------------------------------------------------------------------------


async def _reauthorize_in_lock(
    request: web.Request,
    operation: str,
    account: str,
    profile: str,
    region: str,
    bucket: str,
    *,
    publish: bool,
) -> web.Response | None:
    """Re-run the authorization a waiting caller may have outlived.

    Something makes a handler WAIT -- ``_library_lock`` queues the Library
    operations, and the upload spool holds ``_handle_drive_upload`` for as long
    as a 512 MB transfer takes -- and the wait sits between the checks
    ``_require_drive`` ran and the AWS call they authorized. In that gap the app
    can be disabled, the profile can be repointed at another account, consent
    can be withdrawn, publish governance can start denying, or the drive tags
    can move to a different bucket -- and the call would then run on an
    authorization that no longer holds.

    The order is deliberate: app, then IDENTITY, then consent. Consent is asked
    ABOUT a profile and region, so verifying it against a stale pair proves
    nothing about where the bytes are going -- the identity has to be
    re-resolved first, and must still be the triple the request was authorized
    for.

    The BUCKET is re-resolved too, which the identity check does not cover: tag
    discovery can return a different bucket while the identity is unchanged. This
    module keeps no bucket-name cache precisely because "numbers can be stale; the
    identity of the bucket we write to cannot" (see the module's cache comment),
    and a queued caller holding a name resolved before the wait is the same
    staleness that comment forbids.

    ``publish`` adds the egress gate, for the push. Removal and the reconcile read
    send nothing out, so they do not consult that gate here any more than they do
    on the way in.
    """
    if not await asyncio.to_thread(is_app_enabled, APP_NAME):
        _audit(operation, request.path, "denied", error="app_disabled")
        return _forbidden("aws-control is disabled", "app_disabled")
    recheck = await _account_target(request)
    if isinstance(recheck, web.Response):
        # A permission DECISION, and it must reach SEL from HERE rather than
        # relying on the caller. The two mutations return this response and
        # `_mutating` records their outcome, but the reconcile READ converts it
        # into a degraded 200 -- so without an audit at the point of decision the
        # denial disappears entirely on that path.
        _audit(operation, request.path, "denied", error="account_unavailable")
        return recheck
    if recheck != (account, profile, region):
        _audit(operation, request.path, "denied", error="account_mismatch")
        return _conflict(
            "this connection changed while the request was queued; nothing was written",
            "account_mismatch",
        )
    denied = await _consent(aws_consent.SERVICE_S3, profile, region)
    if denied:
        return denied
    try:
        current = await _drive_bucket(account, profile, region)
    except AWSError as exc:
        return _aws_failed(exc)
    if current != bucket:
        _audit(operation, request.path, "denied", error="drive_changed")
        return _conflict(
            "this account's drive changed while the request was queued; nothing was written",
            "drive_changed",
        )
    if publish:
        return await _publish_gate(request, operation)
    return None


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
        _audit("library_list", request.path, "denied", error="account_unavailable")
        return None, "no working connection for this account"
    account, profile, region = target
    if await _consent(aws_consent.SERVICE_S3, profile, region) is not None:
        return None, "S3 is not confirmed for the account in use"
    try:
        bucket = await _drive_bucket(account, profile, region)
    except AWSError as exc:
        return None, _safe_error(exc)
    if not bucket:
        return None, "this account has no drive yet"
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
    except ValueError as exc:
        return _bad_request(_safe_error(exc), "not_pushable")
    except AWSError as exc:
        return _aws_failed(exc)
    return web.json_response({"pushed": True, **record})


# --------------------------------------------------------------------------
# Backup
# --------------------------------------------------------------------------


async def _handle_backup_status(request: web.Request) -> web.Response:
    target = await _account_target(request)
    if isinstance(target, web.Response):
        return target
    account, profile, region = target
    payload: dict[str, Any] = {
        "nightly": await asyncio.to_thread(backup_mod.nightly_enabled, account),
        "runs": await asyncio.to_thread(backup_mod.last_runs, account),
        "remote": None,
    }
    denied = await _consent(aws_consent.SERVICE_S3, profile, region)
    if denied is None:
        try:
            bucket = await _drive_bucket(account, profile, region)
            if bucket:
                payload["remote"] = await asyncio.to_thread(
                    backup_mod.list_remote_backups, profile, region, bucket, account=account
                )
        except AWSError as exc:
            payload["remoteError"] = _safe_error(exc)
    return web.json_response(payload)


async def _handle_backup_run(request: web.Request) -> web.Response:
    ctx = await _require_drive(request)
    if isinstance(ctx, web.Response):
        return ctx
    _account, profile, region, bucket = ctx
    body = await _body(request)
    kind = str(body.get("kind", ""))
    if kind == backup_mod.KIND_SNAPSHOT:
        runner = backup_mod.run_snapshot_backup
    elif kind == backup_mod.KIND_SESSIONS:
        runner = backup_mod.run_sessions_backup
    else:
        return _bad_request("kind must be snapshot or sessions", "invalid_kind")
    try:
        record = await asyncio.to_thread(runner, _account, profile, region, bucket)
    except AWSError as exc:
        return _aws_failed(exc)
    except RuntimeError as exc:
        return _conflict(_safe_error(exc), "backup_failed")
    return web.json_response({"ran": True, "kind": kind, "run": record})


async def _handle_backup_nightly(request: web.Request) -> web.Response:
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
    await asyncio.to_thread(backup_mod.set_nightly, account, enabled)
    return web.json_response({"nightly": enabled})


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
    try:
        result = await asyncio.to_thread(
            backup_mod.restore_download, profile, region, bucket, key, account=account
        )
    except AWSError as exc:
        return _aws_failed(exc)
    return web.json_response({"downloaded": True, **result})


# --------------------------------------------------------------------------
# IAM policy (local render — what the user pastes into their account)
# --------------------------------------------------------------------------


async def _handle_iam_policy(request: web.Request) -> web.Response:
    from kiro_crew.deploy import iam

    policy = await asyncio.to_thread(iam.policy_json, tier="drive")
    return web.json_response({"policy": policy})


def register_routes(app: web.Application) -> None:
    """Register on the gateway's aiohttp Application (single-arg convention)."""
    r = app.router
    # Reads
    r.add_get(f"{_BASE}/accounts", _guarded(_handle_accounts))
    # Registered BEFORE the {name} route: aiohttp matches in registration order
    # and a literal "available" would otherwise be captured as a profile name.
    r.add_get(f"{_BASE}/profiles/available", _guarded(_handle_profiles_available))
    r.add_get(f"{_BASE}/profiles/{{name}}/reconnect-plan", _guarded(_handle_reconnect_plan))
    r.add_get(f"{_BASE}/drive/{{account}}", _guarded(_handle_drive_status))
    r.add_get(f"{_BASE}/drive/{{account}}/list", _guarded(_handle_drive_list))
    r.add_get(f"{_BASE}/drive/{{account}}/download", _guarded(_handle_drive_download))
    r.add_get(f"{_BASE}/costs/{{account}}", _guarded(_handle_costs))
    r.add_get(f"{_BASE}/library/{{account}}", _guarded(_handle_library_list))
    r.add_get(f"{_BASE}/backup/{{account}}", _guarded(_handle_backup_status))
    r.add_get(f"{_BASE}/shares", _guarded(_handle_shares_list))
    r.add_get(f"{_BASE}/iam-policy", _guarded(_handle_iam_policy))
    # Mutations
    r.add_post(
        f"{_BASE}/profiles/register",
        _guarded(_mutating("profiles_register")(_handle_profiles_register)),
    )
    r.add_post(
        f"{_BASE}/drive/{{account}}/bootstrap",
        _guarded(_mutating("drive_bootstrap")(_handle_drive_bootstrap)),
    )
    r.add_post(
        f"{_BASE}/drive/{{account}}/upload",
        _guarded(_mutating("drive_upload")(_handle_drive_upload)),
    )
    r.add_post(
        f"{_BASE}/drive/{{account}}/delete",
        _guarded(_mutating("drive_delete")(_handle_drive_delete)),
    )
    r.add_post(
        f"{_BASE}/drive/{{account}}/folder",
        _guarded(_mutating("drive_folder_create")(_handle_drive_folder_create)),
    )
    r.add_post(
        f"{_BASE}/drive/{{account}}/folder/delete",
        _guarded(_mutating("drive_folder_delete")(_handle_drive_folder_delete)),
    )
    r.add_post(
        f"{_BASE}/drive/{{account}}/share",
        _guarded(_mutating("drive_share")(_handle_drive_share)),
    )
    r.add_post(
        f"{_BASE}/shares/{{id}}/forget",
        _guarded(_mutating("share_forget")(_handle_share_forget)),
    )
    r.add_post(
        f"{_BASE}/library/{{account}}/push",
        _guarded(_mutating("library_push")(_handle_library_push)),
    )
    r.add_post(
        f"{_BASE}/library/{{account}}/remove",
        _guarded(_mutating("library_remove")(_handle_library_remove)),
    )
    r.add_post(
        f"{_BASE}/backup/{{account}}/run",
        _guarded(_mutating("backup_run")(_handle_backup_run)),
    )
    r.add_post(
        f"{_BASE}/backup/{{account}}/nightly",
        _guarded(_mutating("backup_nightly")(_handle_backup_nightly)),
    )
    r.add_post(
        f"{_BASE}/backup/{{account}}/restore",
        _guarded(_mutating("backup_restore")(_handle_backup_restore)),
    )
