"""What every AWS Control route reads, below the handlers.

The surface's limits and the in-process locks that serialize drive and Library writes;
the error responses; the SEL audit and the two handler wrappers (``_guarded`` on every
route, ``_mutating`` on every write); the request plumbing -- the JSON body, the account
target, consent, the drive bucket, the section; and ``_reauthorize_in_lock``, the check
a write that waited runs again before it reaches AWS. The logger keeps the facade's
name, so a log filter or a ``caplog`` capture keyed on ``...backend.routes`` sees every
line the routes write.
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any, AsyncIterator, Awaitable, Callable

from aiohttp import web

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod
from kiro_crew.apps.builtins.aws_control.backend import storage as storage_mod
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import (
    is_owner_dashboard_request,
)
from kiro_crew.deploy.engine import AWSError
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.publish_governance import publish_denied_reason
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.builtins.aws_control.backend.routes")

APP_NAME = "aws-control"
_BASE = f"/api/apps/{APP_NAME}"

#: Upload ceiling. Multipart s3 cp handles far more, but a dashboard upload
#: buffered through a temp file wants a bound; big data belongs to backups.
_MAX_UPLOAD_BYTES = 512 * 1024 * 1024

#: Download links are redirects in spirit: minted per click, short-lived.
_DOWNLOAD_URL_SECS = 60

#: The preview window. A preview is not a download: it answers "what is in
#: this file" for text-shaped objects, so 256 KB of head bytes is plenty and
#: bounds what one click moves through the gateway. Past it the response says
#: ``truncated`` and the frontend tells the user only the head is shown.
_PREVIEW_MAX_BYTES = 256 * 1024

#: Bytes fetched PAST the window so the egress redactor sees a secret that
#: straddles the window's end whole. Sized well above the longest credential
#: the patterns match (a session token runs to a couple of KB); anything
#: longer and whitespace-free is dropped by the boundary back-off instead.
_PREVIEW_REDACT_LOOKAHEAD = 8 * 1024

#: How far back from the cut the trim looks for whitespace before giving up
#: and cutting hard. Far enough that any line of ordinary text has one;
#: bounded so a minified blob still previews rather than trimming to nothing.
_PREVIEW_TRIM_SEARCH = 4 * 1024

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

#: account -> (monotonic stamp, usage payload). A full-bucket LIST per
#: console render is the kind of quiet quadratic the consent audit log would
#: never show; five minutes of staleness on a storage total is invisible.
#: Deliberately NO bucket-name cache: tag discovery is a TRUST decision
#: (uploads/deletes operate on what it returns), and a cached name outlives
#: an out-of-band delete + hostile re-creation of the same name. Numbers can
#: be stale; the identity of the bucket we write to cannot.
#: Serializes drive creation (see _handle_drive_bootstrap). LoopBoundLock so
#: the module global never binds an import-time loop.
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
#: import-time loop.
_library_lock = LoopBoundLock()

#: Per-object-key write locks for the drive surface. A move promises "never
#: silently overwrites", but S3's ``CopyObject`` carries no destination
#: precondition (``If-None-Match`` covers ``PutObject``, not the copy), so the
#: destination probe and the copy are separate requests — an upload landing
#: between them would be overwritten and the source then deleted. This gateway
#: is the drive's only product-plane writer, so serializing its own writers
#: per key closes that window for every write the product can make; writers
#: outside the gateway (the CLI drawer deliberately hands out the bucket name)
#: were never inside the promise. Per KEY, not one coarse lock like
#: ``_library_lock``: an upload legally holds its lock for the length of a
#: 512 MB put (up to 600 s), and a coarse lock would stall every unrelated
#: move behind it. Boundedness comes from refcounting instead of eviction —
#: an entry exists only while some request holds or awaits it, so the maps'
#: size is capped by in-flight requests, never by history.
_drive_key_locks: dict[str, LoopBoundLock] = {}
_drive_key_lock_refs: dict[str, int] = {}


def _drive_key_lock_unref(name: str) -> None:
    """Drop one reference; delete the registry entry with the last one."""
    refs = _drive_key_lock_refs.get(name, 1) - 1
    if refs <= 0:
        _drive_key_lock_refs.pop(name, None)
        _drive_key_locks.pop(name, None)
    else:
        _drive_key_lock_refs[name] = refs


@asynccontextmanager
async def _locked_drive_keys(bucket: str, section: str, *keys: str) -> AsyncIterator[None]:
    """Hold this gateway's write lock for each named object key.

    Locks are taken in sorted order so two requests naming the same keys in
    opposite order (a move A->B racing a move B->A) cannot deadlock. The
    refcount is incremented BEFORE awaiting the acquire so a waiter keeps the
    entry alive, and dropped again on the acquire failing, so a cancelled
    waiter does not strand a registry entry. ``\\n`` joins the name parts —
    it cannot appear in a bucket name or a validated key, so distinct
    (bucket, section, key) triples can never collide into one lock name.
    """
    names = sorted({f"{bucket}\n{section}\n{key}" for key in keys})
    held: list[str] = []
    try:
        for name in names:
            lock = _drive_key_locks.setdefault(name, LoopBoundLock())
            _drive_key_lock_refs[name] = _drive_key_lock_refs.get(name, 0) + 1
            try:
                await lock.acquire()
            except BaseException:
                _drive_key_lock_unref(name)
                raise
            held.append(name)
        yield
    finally:
        for name in reversed(held):
            _drive_key_locks[name].release()
            _drive_key_lock_unref(name)


class _SectionRWLock:
    """Per-event-loop reader/writer lock for one drive section.

    Per-key locks cannot coordinate a PREFIX SWEEP: a folder delete removes
    every object under a prefix without knowing their keys up front, so it
    can never enumerate which key locks to take — a sweep landing between a
    move's copy and its source delete would remove the freshly-copied
    destination and the move would then delete the source, losing the file
    at both ends. So key-scoped mutations hold the section SHARED (they
    coordinate among themselves via the per-key locks) and a sweep holds it
    EXCLUSIVE.

    Writer-preferent: a waiting sweep blocks NEW shared holders, so a steady
    stream of uploads cannot starve a folder delete forever. State lives in
    a per-loop map for the same reason ``LoopBoundLock`` exists: a
    module-global asyncio primitive must not bind an import-time loop.
    """

    def __init__(self) -> None:
        self._by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, Any]]" = (
            weakref.WeakKeyDictionary()
        )

    def _state(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        state = self._by_loop.get(loop)
        if state is None:
            state = {"readers": 0, "writer": False, "writers_waiting": 0}
            state["cond"] = asyncio.Condition()
            self._by_loop[loop] = state
        return state

    @asynccontextmanager
    async def shared(self) -> AsyncIterator[None]:
        state = self._state()
        cond: asyncio.Condition = state["cond"]
        async with cond:
            while state["writer"] or state["writers_waiting"]:
                await cond.wait()
            state["readers"] += 1
        try:
            yield
        finally:
            async with cond:
                state["readers"] -= 1
                cond.notify_all()

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        state = self._state()
        cond: asyncio.Condition = state["cond"]
        async with cond:
            state["writers_waiting"] += 1
            try:
                while state["writer"] or state["readers"]:
                    await cond.wait()
            finally:
                state["writers_waiting"] -= 1
            state["writer"] = True
        try:
            yield
        finally:
            async with cond:
                state["writer"] = False
                cond.notify_all()


#: (bucket, section) -> RW lock. NOT refcounted like the key locks: the domain
#: is bounded by reality (three fixed sections × the accounts the owner has
#: connected), so the registry cannot grow with drive history.
_drive_section_locks: dict[str, _SectionRWLock] = {}


def _drive_section_lock(bucket: str, section: str) -> _SectionRWLock:
    name = f"{bucket}\n{section}"
    lock = _drive_section_locks.get(name)
    if lock is None:
        lock = _drive_section_locks[name] = _SectionRWLock()
    return lock


@asynccontextmanager
async def _locked_drive_write(bucket: str, section: str, *keys: str) -> AsyncIterator[None]:
    """The guard EVERY key-scoped drive mutation runs under.

    Section held shared (so a prefix sweep excludes us), then the per-key
    locks (so same-key writers serialize among themselves). The order —
    section BEFORE keys, always — is the deadlock discipline; a sweep takes
    only the section, so no lock is ever taken in the reverse order.
    """
    async with _drive_section_lock(bucket, section).shared():
        async with _locked_drive_keys(bucket, section, *keys):
            yield


@asynccontextmanager
async def _locked_drive_sweep(bucket: str, section: str) -> AsyncIterator[None]:
    """The guard a PREFIX SWEEP (folder delete) runs under: section exclusive.

    Exclusive against every key-scoped mutation, because a sweep cannot name
    the keys it will remove and therefore cannot join the per-key protocol.
    """
    async with _drive_section_lock(bucket, section).exclusive():
        yield


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
        await _audit(operation, request.path, "denied", error=reason)
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


def _unavailable(message: str, code: str) -> web.Response:
    """A probe this handler depends on could not run. Distinct from an empty result.

    503 rather than 200-with-nothing: the client has to be able to tell "this
    machine has none" from "we could not look", and rather than 500 because
    nothing here is broken -- the host is simply not answering the question
    today, so the honest instruction is to retry.
    """
    return web.json_response({"error": message, "code": code}, status=503)


def _unsupported(message: str, code: str) -> web.Response:
    """This platform cannot serve the request at all, so retrying never helps.

    The pair with ``_unavailable`` is the point: 503 invites a retry, which is
    right for a scan that failed and wrong for one that cannot run here. 501
    with an ``unsupported_*`` code is the shape spec_builder and meetings
    already answer with, so a client that learned it there reads this too.
    """
    return web.json_response({"error": message, "code": code}, status=501)


def _ledger_corrupt(code: str) -> web.Response:
    """Map a ledger reader's corruption refusal to a coded response.

    The share and library ledger update readers refuse a corrupt document
    rather than replacing it, so mutation handlers can see a
    ``json.JSONDecodeError`` at all. Letting it
    escape gives aiohttp's bare 500 -- no ``code`` for the UI to branch on --
    and letting a handler's ``except ValueError`` arm claim it (it IS a
    ``ValueError``) reports corruption as a client mistake. 500 rather than
    503: corruption does not clear on retry, the file needs a person to repair
    it, and the bytes it still holds are exactly why the mutation refused.

    The exception's own text is deliberately NOT echoed: a decode error's
    payload can carry document content, and the fixed message says everything
    the caller can act on.
    """
    return web.json_response(
        {"error": "the local ledger is unreadable and must be repaired", "code": code},
        status=500,
    )


def _safe_error(exc: BaseException) -> str:
    """Error text fit for a response body.

    AWS CLI stderr is engine-trimmed but still echoes what the CLI printed —
    a failed call can quote back a ``credential_process`` command line, an
    SSO URL, or an endpoint override carrying inline credentials — so every
    outbound error runs BOTH passes: credentials, then exfiltration URLs.
    Same chain the deploy handlers apply at their own boundary.
    """
    from kiro_crew.security import redact

    text = redact(str(exc))
    return text


def _aws_failed(exc: AWSError) -> web.Response:
    return web.json_response({"error": _safe_error(exc), "code": "aws_call_failed"}, status=502)


def _response_code(response: web.Response) -> str:
    """The machine-readable ``code`` a denial response carries, for the audit trail.

    Never hard-code the reason next to a helper that can answer with more than one.
    ``_resolve_target`` returns THREE - ``invalid_account``, ``account_unavailable``
    and ``account_mismatch`` - so auditing a fixed string makes an incident review
    read "no working connection" for what was in fact a live-identity mismatch, which
    is the case an operator most needs to see. Falls back to ``unknown`` rather than
    raising: an audit line is best-effort and must not be able to fail a response.
    """
    try:
        return str(json.loads(response.text or "{}").get("code", "")) or "unknown"
    except Exception:
        return "unknown"


def _audit_sync(operation: str, resources: str, outcome: str, *, error: str = "") -> None:
    """Best-effort SEL audit for mutations; never blocks the response.

    Blocking body of :func:`_audit` — ``log_api_access`` only enqueues onto the
    writer thread, but the first ``sel()`` of a process CONSTRUCTS the log
    (trust-dir creation, key load/validation, on Windows the owner-only DACL),
    so handlers must reach it through the async wrapper, which routes it off
    the event loop. Only tests and the wrapper call this directly.
    """
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


async def _audit(operation: str, resources: str, outcome: str, *, error: str = "") -> None:
    """Record a SEL audit event without stalling the event loop.

    ``log_api_access`` itself only enqueues, but first touch pays SEL
    construction (see :func:`_audit_sync`), so the call is offloaded with
    ``asyncio.to_thread`` — the same pattern this module already uses for its
    other blocking calls and the shape ``dashboard.server._audit_denied``
    settled for SEL specifically. Awaiting keeps the audit-before-response
    ordering every call site relies on, and the wrapper preserves the
    never-raises contract of the sync body even if thread dispatch itself
    fails. Call sites that audit AFTER an external side effect (a mutation
    outcome, a minted presign URL) wrap this in ``asyncio.shield`` so a client
    disconnect cannot cancel the record between the side effect and its audit;
    denial paths have no side effect to orphan and stay unshielded.
    """
    try:
        await asyncio.to_thread(_audit_sync, operation, resources, outcome, error=error)
    except Exception:
        logger.debug("aws-control SEL audit dispatch failed", exc_info=True)


def _guarded(handler: Handler) -> Handler:
    """Enabled check + owner check — the wrapper every route goes through."""

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            # Same rule as the owner denial below: a permission DECISION must
            # reach SEL — a request arriving while the app is disabled is a
            # denial an incident review asks about.
            await _audit("access", request.path, "denied", error="app_disabled")
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
            await _audit(
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
                await _audit(operation, request.path, "denied", error="restricted session")
                return _forbidden(
                    "this session is restricted from AWS mutations",
                    "restricted_session",
                )
            try:
                response = await handler(request)
            except AWSError as exc:
                # The handler already ran against AWS, so this audit and the
                # success/refused one below must survive a client disconnect:
                # shield keeps the record being written even when the await
                # itself is cancelled. Before the await this point had no
                # suspension, so cancellation could never orphan the outcome.
                await asyncio.shield(_audit(operation, request.path, "error", error=str(exc)))
                return _aws_failed(exc)
            await asyncio.shield(
                _audit(
                    operation,
                    request.path,
                    "success" if response.status < 400 else "refused",
                )
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
    """Resolve the path's ``{account}``. See :func:`_resolve_target`."""
    return await _resolve_target(request.match_info.get("account", ""))


async def _resolve_target(account: str) -> tuple[str, str, str] | web.Response:
    """Resolve an account id to (account, profile, region), or an error response.

    The snapshot maps profiles to accounts with a five-minute TTL, and a
    profile can be repointed at a different account inside that window — so
    the mapping alone must never pick which account an operation runs
    against. A LIVE identity probe re-verifies that the chosen profile still
    resolves to the REQUESTED account, and a mismatch refuses rather than
    executing against whatever the profile now points at.

    ``use_cache=False`` is the whole point of the probe here and is NOT a cost
    oversight. :func:`aws_consent.probe_identity` memoises per
    ``(profile, region)`` for 30 seconds, which is right for the consent and
    voice paths that ask "who would bill this" repeatedly. It is wrong for an
    authorization decision: a cached answer of account A, honoured for a full
    30 seconds after the profile was repointed to B, verifies A while the read
    that follows runs against B's live credentials. Switching a profile between
    accounts is an ordinary operator action, not an extreme one, so that window
    is reachable in normal use. An authorization check that may answer from
    memory is not a check.

    What this buys and what it does not: the probe and the read are still two
    operations, so a repoint landing between them is not caught by anything
    here. That window is one call wide instead of thirty seconds, which is the
    difference between a race and a TTL, and closing it entirely would need an
    atomicity the AWS CLI does not offer.

    Takes the id rather than the request because ``GET /shares`` scopes by
    QUERY parameter, not by path segment, and a second copy of this resolution
    is how one route ends up without the live re-probe.
    """
    if not (account.isdigit() and len(account) == 12):
        return _bad_request("account must be a 12-digit id", "invalid_account")
    resolved = await accounts_mod.resolve_account_profile(account)
    if resolved is None:
        return _conflict(
            "no working connection for this account — reconnect first",
            "account_unavailable",
        )
    profile, region = resolved
    identity = await aws_consent.probe_identity(profile, region, use_cache=False)
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
    authorization that does not hold.

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
        await _audit(operation, request.path, "denied", error="app_disabled")
        return _forbidden("aws-control is disabled", "app_disabled")
    recheck = await _account_target(request)
    if isinstance(recheck, web.Response):
        # A permission DECISION, and it must reach SEL from HERE rather than
        # relying on the caller. The two mutations return this response and
        # `_mutating` records their outcome, but the reconcile READ converts it
        # into a degraded 200 -- so without an audit at the point of decision the
        # denial disappears entirely on that path.
        await _audit(operation, request.path, "denied", error="account_unavailable")
        return recheck
    if recheck != (account, profile, region):
        await _audit(operation, request.path, "denied", error="account_mismatch")
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
        await _audit(operation, request.path, "denied", error="drive_changed")
        return _conflict(
            "this account's drive changed while the request was queued; nothing was written",
            "drive_changed",
        )
    if publish:
        return await _publish_gate(request, operation)
    return None
