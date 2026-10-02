"""A slot's source links: the list a session card reads, and the field-scoped unlink
that dismisses one.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        _MAX_DISMISSED_SOURCE_LINKS,
        _UNPINNED,
        DashboardState,
        _ChatSlot,
        _reauthorize_after_await,
        _source_link_txn_locks,
        _source_link_unlink_tasks,
        deny_app_slot_access,
        deny_app_slot_session_access,
        logger,
        resolved_row_identity,
        sel,
        slot_history_key,
    )


async def api_chat_slot_source_links(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/source-links — every PR/issue link, unbudgeted.

    The slots payload caps chips per kind, so the sidebar's "+N" overflow chip
    has nothing on the client to expand into. This is the lazy read behind that
    expand, kept off the slots broadcast on purpose: widening the budget would
    put up to ``_MAX_SOURCE_LINKS_PER_SLOT`` links per slot on the wire for every
    row nobody expanded, on every push.
    """
    # circular import: source_providers imports chat state helpers, so a
    # top-level import would close a cycle (same pattern as the owner-only
    # check-status gate in api_chat_slots).
    from kiro_crew.dashboard.handlers.source_providers import (
        ensure_gitlab_hosts_loaded,
        is_owner_dashboard_request,
    )

    state: DashboardState = request.app["state"]
    slot = state._slots.get(request.match_info["slot"])
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # App ownership check (App Kit §5.2): deny-by-default for app tokens. An app
    # token scoped to /api/chat/slots/* would otherwise name any slot the list
    # endpoint reveals and read every pull request and issue URL a dashboard or
    # foreign-app session ever mentioned. Same indistinguishable 404 as the send
    # path -- SAME error code too, so the response cannot serve to probe which
    # foreign slots exist.
    request_app = request.get("app", "")
    denied = deny_app_slot_access(request_app, slot, slot.key, "chat_source_links")
    if denied is not None:
        return denied
    if request_app:
        # The ALLOW is a permission decision too, and an audit trail that records
        # only refusals cannot answer which app actually read a slot's links.
        # Dashboard callers are deliberately not logged here: they are the owner,
        # and every sidebar expand would otherwise write an event.
        sel().log_api_access(
            caller=request_app,
            operation="chat_source_links",
            outcome="allowed",
            source="app_isolation",
            resources=f"slot={slot.key}",
        )

    # Same warm-up as GET /api/chat/slots: link extraction is synchronous and
    # cannot load the self-managed GitLab allowlist itself, so a cold expand
    # would drop every self-hosted MR link from the revealed set.
    try:
        await ensure_gitlab_hosts_loaded()
    except Exception:
        logger.debug(
            "GitLab allowlist warm-up failed; expanded chips may lag one round", exc_info=True
        )

    # Cached status only, gated exactly like the list endpoint: owner sees all,
    # a dashboard-user sees public-repo status, app tokens see none. No
    # schedule_check_refresh here: that pushes a `slots` update, which by
    # definition cannot carry links outside the budget, so the provider work
    # would produce a result this response can never show.
    #
    # Deliberately NOT gated on `dashboard.session_card_source_links`: the only
    # caller is the sidebar's "+N" pill, which exists only while the strip
    # renders, and the config write pushes fresh slots so the pill goes at once.
    # An app token that owns the slot could ask directly, but it can already read
    # the slot's messages -- these URLs are extracted FROM those messages, so
    # gating here would withhold nothing it does not already have.
    return web.json_response(
        slot.source_links_payload(
            include_check_status=is_owner_dashboard_request(request),
            dashboard_user=bool(request.get("is_dashboard_user")),
        )
    )


def _audit_source_link_unlink(name: str, outcome: str, **fields: Any) -> None:
    """Best-effort terminal audit must never change a settled write's outcome."""
    try:
        sel().log_tool_invocation(
            session_key=f"dashboard:{name}",
            agent="kirocrew",
            source="dashboard",
            tool_name="source_link_unlink",
            tool_kind="permission",
            outcome=outcome,
            **fields,
        )
    except Exception:
        logger.warning("Source-link unlink audit unavailable")


async def api_chat_slot_source_link_unlink(request: web.Request) -> web.Response:
    """Keep accepted unlink settlement alive across HTTP-request cancellation.

    Cancelling to_thread cannot stop its writer. Keep the transaction lock,
    reconciliation, final publication and depth cleanup together until settled.
    A repeated caller cancellation may stop waiting, never the owned task; the
    strong reference and completion callback retain and observe it in that case.
    """
    task = asyncio.create_task(_apply_source_link_unlink(request))
    _source_link_unlink_tasks.add(task)

    def completed(done: asyncio.Task) -> None:
        _source_link_unlink_tasks.discard(done)
        if not done.cancelled():
            done.exception()  # retrieve even if a repeatedly-cancelled caller left

    task.add_done_callback(completed)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        _audit_source_link_unlink(
            request.match_info["slot"],
            "failed",
            error="request_cancelled",
            metadata={"phase": "request", "settlement": "shielded"},
        )
        try:
            await asyncio.shield(task)
        except Exception:
            pass  # the operation emitted its own terminal failure
        raise


async def _apply_source_link_unlink(request: web.Request) -> web.Response:
    """DELETE /api/chat/slots/{slot}/source-links/{identity} — unlink one chip.

    The PR/issue/Jira chips are DERIVED by re-scanning the transcript, so there
    is nothing to delete: the next re-scan would re-add a removed link. Unlinking
    instead records the link's serialized identity into the slot's dismissed set,
    which the derivation filters against, and persists it so the chip stays gone
    across a gateway restart. Purely local -- no remote provider is touched, so a
    pull request is not closed and an issue is not deleted.

    The ``{identity}`` segment is the serialized ``SourceRef.identity`` key that
    the slots payload now carries on each source link (the ``identity`` field);
    the frontend echoes that opaque value straight back here rather than
    re-deriving it. It is validated against the canonical grammar before use --
    a malformed key is rejected with 400 and a machine-readable ``code`` rather
    than stored as junk that can never match a real identity.
    """
    # Function-local to avoid a circular import: source_providers imports from
    # the dashboard state/handler layer. Same lazy form the sibling source-link
    # handlers use.
    from kiro_crew.dashboard.source_providers.contract import (
        bounded_valid_identities,
        is_valid_source_identity_key,
    )

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]

    def _reject(response: web.Response, error: str, phase: str) -> web.Response:
        # Every rejection is a failed invocation of a permission-class tool, so
        # it must leave a SEL trail like the persist-failure and lock-rebind
        # paths do -- otherwise a malformed/stale/absent-identity attempt (or an
        # ownership denial) unlinks nothing yet is invisible to the audit log.
        _audit_source_link_unlink(
            name, "failed", error=error, metadata={"slot": name, "phase": phase}
        )
        return response

    publication_needed = False

    def _publish(phase: str) -> None:
        nonlocal publication_needed
        # Delivery is not the commit point. A publication here fires only AFTER
        # the guarded durable write has committed (persist-before-publish) OR
        # after a refused write has rolled the tentative dismissal back (the
        # ``rollback`` phase re-converges the client with the unchanged disk
        # state) — never on a tentative, not-yet-durable dismissal. Either way a
        # broadcast failure never interrupts persist/compensate/reconcile or
        # turns a committed 200 into a 500 the client rolls back — it only flags
        # ``publication_needed`` so a later push re-converges the client with disk.
        try:
            state.push_slots_update()
            publication_needed = False
        except Exception:
            publication_needed = True
            _audit_source_link_unlink(
                name,
                "failed",
                error="broadcast_failed",
                metadata={"slot": name, "phase": phase},
            )

    txn_slots: list[_ChatSlot] = []

    @contextlib.asynccontextmanager
    async def _transaction(history_key: str):
        async with _source_link_txn_lock(history_key):
            try:
                yield
            finally:
                # Every increment is recorded, including multiple increments
                # on an alias that departs and returns. Release exactly ours.
                for touched in txn_slots:
                    touched._dismissed_txn_depth -= 1
                    # At transaction exit the key is settled: a committed write
                    # leaves it durably dismissed (so it projects as dismissed),
                    # a rolled-back write leaves it absent from the set. Either
                    # way it is not tentative, so drop it from the pending fence
                    # on every touched slot and invalidate, letting the settled
                    # state — chip gone on commit, chip present on rollback — be
                    # what the next projection publishes.
                    if identity_key in touched._dismissed_txn_pending:
                        touched._dismissed_txn_pending.discard(identity_key)
                        touched.invalidate_source_links()

    slot = state._slots.get(name)
    if not slot:
        # Same indistinguishable 404 + code as the GET path, so the response
        # cannot serve as a probe for which slots exist.
        return _reject(
            web.json_response({"error": "not found", "code": "slot_not_found"}, status=404),
            error="slot_not_found",
            phase="lookup",
        )
    # Session-aware ownership gate, NOT the slot-only check: the dismissal is
    # persisted (forced save) into the transcript this slot routes to, so a
    # linked app-owned slot (a channel stem) would otherwise let an app write
    # metadata into a foreign human conversation. deny_app_slot_session_access
    # authorizes the transcript key the write actually lands on -- same as
    # /autocompact, /context and /note, all of which persist slot metadata.
    request_app = request.get("app", "")
    denied = deny_app_slot_session_access(request_app, slot, name, "slot_source_link_unlink")
    if denied is not None:
        return _reject(denied, error="app_isolation", phase="ownership")

    # Expected-session-identity gate, checked at the ENTRY lookup. The client
    # sends the identity of the session it MEANT to unlink from -- the same
    # ``<row_identity>|<created_at>|<linked_session_key>`` string the sidebar
    # rendered the chip under (see the frontend ``slotGeneration``/``sessionId``);
    # the transcript binding is the part a rebind changes -- as the ``expect``
    # query param. If the slot living behind ``name`` now carries a DIFFERENT
    # identity, a permanent delete + same-key recreation replaced the targeted
    # session with another BEFORE this request ran, so proceeding would dismiss a
    # link on a session the user never chose. This gate rejects the wrong session
    # at the point the slot is captured and is what makes the request
    # self-describing about which session it targeted.
    #
    # ``expect`` is REQUIRED. The downstream ``created_at`` pin alone cannot close
    # the hole: a same-key recreation that FINISHES before the entry lookup makes
    # the pin read the REPLACEMENT's own identity (so it matches itself and
    # passes), and ``mentions_source_identity`` passes precisely in the wrong-
    # session case -- when the replacement transcript genuinely mentions the same
    # link. With no un-dismiss route, that wrong tombstone would be permanent. So
    # an ABSENT ``expect`` is refused here rather than falling through to the pin.
    # This endpoint is new and its only caller (``SourceLinksSubmenu``) always
    # sends ``expect``, so there is no client to preserve.
    expected_identity = request.rel_url.query.get("expect")
    if not expected_identity:
        return _reject(
            web.json_response(
                {
                    "error": "expected session identity is required",
                    "code": "expected_identity_required",
                },
                status=400,
            ),
            error="expected_identity_required",
            phase="expected_identity",
        )
    # Mirror the client's generation TRIPLE: row identity, birth stamp, AND
    # the transcript binding (``linked_session_key``). The binding is what a
    # REBIND changes when neither of the other two moves — a live slot pointed
    # at a different transcript (a cron/workflow injector assigning
    # ``linked_session_key`` on an already-existing slot) — so without it the
    # gate would pass and the dismissal would land on the replacement
    # transcript the user never chose. Including it fails the check on rebind.
    current_identity = (
        f"{resolved_row_identity(slot)}"
        f"|{getattr(slot, 'created_at', '') or ''}"
        f"|{getattr(slot, 'linked_session_key', '') or ''}"
    )
    if expected_identity != current_identity:
        return _reject(
            web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"},
                status=409,
            ),
            error="session_gone",
            phase="expected_identity",
        )

    # aiohttp has ALREADY percent-decoded the dynamic route segment into
    # ``match_info``. The frontend single-encodes the identity
    # (``encodeURIComponent``), so this one decode round-trips it exactly. Do NOT
    # ``unquote`` again: a second decode collapses two identities that differ
    # only by encoding (``/acme/a/pull/1`` vs ``/acme/%61/pull/1``) onto the same
    # key, so an unlink would permanently dismiss the WRONG source link.
    identity_key = request.match_info["identity"]
    if not is_valid_source_identity_key(identity_key):
        return _reject(
            web.json_response(
                {"error": "invalid source-link identity", "code": "invalid_source_identity"},
                status=400,
            ),
            error="invalid_source_identity",
            phase="validate",
        )

    # Only an identity that is ACTUALLY one of this slot's currently-derived
    # chips (or one already dismissed) may be dismissed. Without this bound a
    # caller could submit unlimited distinct format-valid-but-absent identities,
    # each growing the persisted dismissed set and forcing a disk write --
    # unbounded durable-state growth. Gating on the derived set means an identity
    # can only be dismissed if the transcript actually mentions it, so the
    # dismissed set is bounded by the count of DISTINCT real source links the
    # transcript carries -- not by any single snapshot's budgeted slice (dismissing
    # one budgeted chip can reveal the next-ranked real link), but still finite and
    # tied to genuine transcript content rather than attacker-chosen junk.
    # Already-dismissed is allowed through so a double-click / retry stays an
    # idempotent 200 no-op rather than a confusing 404 (the chip is gone from the
    # derived set precisely because it worked). This pre-lock gate is an
    # anti-abuse bound only — it keeps a caller from growing the dismissed set
    # with unlimited format-valid-but-absent junk. It is NOT the authorization to
    # WRITE: the slot's in-memory ``_dismissed_source_links`` can hold a foreign
    # identity a concurrent unlink on a since-rebound slot left TENTATIVELY (not
    # yet committed, about to roll back), so admitting it here must not let a
    # DELETE without ``expect`` durably tombstone that key on the PINNED
    # transcript. The write authorization for a non-derived identity is therefore
    # re-checked inside the lock against the pinned transcript's DURABLE line and
    # its RAW mentions (see ``non_derived_identity`` below).
    derived_identities = {link.get("identity") for link in slot._pr_source_links()}
    non_derived_identity = identity_key not in derived_identities
    known = derived_identities | slot._dismissed_source_links
    if identity_key not in known:
        return _reject(
            web.json_response({"error": "not found", "code": "source_link_not_found"}, status=404),
            error="source_link_not_found",
            phase="derive",
        )

    # Serialize the ENTIRE dismiss decision — the newly-dismissed check, the
    # in-memory mutation, the mirror, and the save/rollback — under a
    # per-transcript transaction lock (mirrors api_chat_slot_autocompact). The
    # lock is acquired BEFORE any mutation on purpose: if the mutation ran first
    # (outside the lock), two concurrent DELETEs would race — the second would
    # see the identity already in the set, take the "already dismissed" no-op
    # path, and return 200, while the first's save could then fail and roll the
    # dismissal back, so the second would acknowledge state absent from disk.
    # Doing the check-and-mutate inside the lock means the second DELETE only
    # runs after the first has fully committed or rolled back, and re-derives
    # its own newly-dismissed decision from the settled state. Keyed by the
    # TRANSCRIPT so alias slots serialize together; a mid-request rebind is
    # handled by the reauth + expected_history_key pin below, not the lock key.
    locked_history_key = slot_history_key(slot)
    async with _transaction(locked_history_key):
        stale = _reauthorize_after_await(state, slot, name, request_app, "slot_source_link_unlink")
        if stale is not None:
            # A DELETE that waited on the transaction lock can find its slot
            # replaced/rebound by the time it acquires it; the reauth rejects,
            # and that rejection is a failed permission-tool invocation like the
            # input-validation ones, so it must leave a SEL trail too.
            return _reject(stale, error="session_gone", phase="reauth")
        authorized_history_key = slot_history_key(slot)
        if authorized_history_key != locked_history_key:
            # Rebound between the lock-key read and acquisition: this request
            # holds the OLD transcript's lock while the write would target the
            # new one, so the serialization guarantee does not cover it.
            _audit_source_link_unlink(
                name,
                "failed",
                error="session_gone",
                metadata={"slot": name, "phase": "lock_rebind"},
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"},
                status=409,
            )
        # The check-and-mutate now happens under the lock, so it sees state
        # settled by a concurrent request (committed OR rolled back).
        #
        # Pin the authorized transcript's IDENTITY (its ``created_at``, unique per
        # transcript creation) BEFORE any mutation — right after the reauth +
        # ``expected_history_key`` check that already established this is the
        # transcript we are authorized to write. EVERY metadata write below
        # (including the first) requires the on-disk ``created_at`` to still
        # equal this pin, so a permanent delete + same-path recreation at ANY
        # point after authorization yields a fresh ``created_at`` that fails the
        # guard: the write is declined and rolled back, never landing a stale
        # dismissal in a replacement session. The pin is read once, off-loop,
        # before ``dismiss_source_link`` touches memory; if the transcript is not
        # readable we cannot authorize a write to it, so we 409 without mutating.
        pinned_created_at: object = _UNPINNED
        if state.conversation_log is not None:
            try:
                _pin_meta, _pin_readable = await asyncio.to_thread(
                    state.conversation_log.get_metadata_status, authorized_history_key
                )
                if _pin_readable and _pin_meta.get("_type") == "metadata":
                    pinned_created_at = _pin_meta.get("created_at")
            except Exception:
                pinned_created_at = _UNPINNED
        if pinned_created_at is _UNPINNED:
            # Could not establish the authorized transcript's identity, so a
            # write cannot be proven to target it — decline without mutating.
            return _reject(
                web.json_response(
                    {"error": "session was deleted or rebound", "code": "session_gone"},
                    status=409,
                ),
                error="session_gone",
                phase="identity_pin",
            )

        # The metadata pin is an await too: the same slot object may now route
        # elsewhere, or its app owner may have changed. Reject before mutation.
        stale = _reauthorize_after_await(state, slot, name, request_app, "slot_source_link_unlink")
        if stale is not None:
            return _reject(stale, error="session_gone", phase="identity_pin_reauth")
        if slot_history_key(slot) != authorized_history_key:
            return _reject(
                web.json_response(
                    {"error": "session was deleted or rebound", "code": "session_gone"},
                    status=409,
                ),
                error="session_gone",
                phase="identity_pin_rebind",
            )

        def _guard(meta: dict) -> bool:
            # Every write — first, confirm, and compensate — must observe the
            # SAME transcript identity captured before mutation. A recreated
            # transcript (fresh ``created_at``) or a non-metadata line is rejected.
            return meta.get("_type") == "metadata" and meta.get("created_at") == pinned_created_at

        def _locked_dismissed_set(meta: dict) -> set[str]:
            # The dismissed set on the LOCKED on-disk metadata line. ``meta`` is
            # read INSIDE ``update_metadata_if``'s cross-process lock, so it is
            # the authoritative durable set at write time — not the pre-lock
            # snapshot the request computed its ``union``/``compensate`` from.
            raw = meta.get("dismissed_source_links")
            if not isinstance(raw, list):
                return set()
            # Bound retention DURING iteration (never materialize the whole
            # untrusted on-disk list before the cap): the on-disk line is
            # externally controllable, so collect at most the ceiling.
            return bounded_valid_identities(raw, _MAX_DISMISSED_SOURCE_LINKS)

        def _merge_guard(payload: dict[str, object]) -> Callable[[dict], bool]:
            # A guard that ALSO recomputes ``payload["dismissed_source_links"]``
            # against the locked on-disk line, closing the read-before-write
            # union race: the request's ``union``/``compensate`` was fixed from a
            # disk read taken BEFORE ``update_metadata_if`` acquired the
            # cross-process lock, so a concurrent gateway sharing this data home
            # could commit a different dismissal in that window and this write's
            # precomputed set would erase it (last-writer-wins lost update).
            #
            # ``update_metadata_if`` calls this guard while holding the lock, with
            # the freshly-read ``meta``, and only THEN applies ``payload`` via
            # ``_update_metadata_locked``. Mutating ``payload`` here therefore
            # rewrites what actually lands. ``payload`` already carries this
            # request's own contribution (``{identity_key}`` for an add, or the
            # empty/pre-existing base for a compensation); we UNION the locked
            # disk set in so every concurrently-committed sibling dismissal
            # survives. Dismissals only grow, so a union can only ADD — it never
            # drops the on-disk tombstones the request had not yet observed.
            #
            # The cap bounds the field it retains: if folding the locked disk set
            # in would exceed ``_MAX_DISMISSED_SOURCE_LINKS``, the durable set is
            # already at the ceiling, so REJECT (guard returns False -> the write
            # declines and the caller rolls back + 409) rather than commit an
            # oversized line restore would tail-truncate.
            def _merge(meta: dict) -> bool:
                if not _guard(meta):
                    return False
                base = payload.get("dismissed_source_links")
                base_set = set(base) if isinstance(base, (list, set)) else set()
                merged = base_set | _locked_dismissed_set(meta)
                if len(merged) > _MAX_DISMISSED_SOURCE_LINKS:
                    return False
                payload["dismissed_source_links"] = sorted(merged)
                return True

            return _merge

        # Is this identity DURABLY dismissed on the authorized transcript's disk
        # line (read once, above, into ``_pin_meta``)? A plain
        # ``dismiss_source_link`` returning False means only that the key is in
        # the slot's IN-MEMORY set — which a CONCURRENT unlink on a since-rebound
        # slot may have put there TENTATIVELY (its guarded write not yet
        # committed, and about to roll back). Fast-returning 200 off that
        # in-memory presence would acknowledge a dismissal disk never recorded
        # and that the other request is about to retract, leaving this
        # transcript's chip linked after a reported success. So the "already
        # dismissed" fast path is valid ONLY when the key is on DISK; otherwise
        # we fall through and persist it authoritatively under our own guard.
        _disk_dismissed = (
            _pin_meta.get("dismissed_source_links") if isinstance(_pin_meta, dict) else None
        )
        durably_dismissed = isinstance(_disk_dismissed, list) and identity_key in _disk_dismissed

        # Authorize a NON-DERIVED identity to enter the persist path ONLY when the
        # PINNED transcript legitimately carries it: either it is DURABLY
        # dismissed on disk (an idempotent retry whose chip is gone from the
        # derived set because a prior unlink committed), or the transcript RAW-
        # mentions it (a real chip currently suppressed only by THIS slot's own
        # in-memory dismissal). Both are independent of the tentative in-memory
        # set a concurrent unlink on a since-rebound slot may have populated with
        # a FOREIGN key, so this closes the path where such a key would authorize
        # a durable tombstone on a transcript that never mentioned the link
        # (silent, grow-only, non-self-correcting). A derived identity skips this
        # — it is trivially legitimate.
        if (
            non_derived_identity
            and not durably_dismissed
            and not slot.mentions_source_identity(identity_key)
        ):
            _audit_source_link_unlink(
                name,
                "failed",
                error="source_link_not_found",
                metadata={"slot": name, "phase": "derive_pinned"},
            )
            return web.json_response(
                {"error": "not found", "code": "source_link_not_found"}, status=404
            )

        newly_dismissed = slot.dismiss_source_link(identity_key)
        # ``dismiss_source_link`` returns False for TWO reasons: the key is
        # already dismissed (idempotent repeat), or the per-slot ceiling
        # ``_MAX_DISMISSED_SOURCE_LINKS`` was hit and the add was REFUSED. Only
        # the idempotent case may proceed. A cap-refused add must NOT fall into
        # the persist path below: that path writes ``union = on-disk ∪
        # {identity_key}``, which for a full on-disk set is ceiling+1 entries —
        # past the very bound the add-site refuses, and the oversized line is
        # then tail-truncated on restore, resurrecting whichever chip fell off.
        # Enforce the cap HERE, before any union write, by rejecting the request
        # when the key is neither already in memory nor durably on disk yet the
        # slot is at the ceiling.
        cap_refused = (
            not newly_dismissed
            and identity_key not in slot._dismissed_source_links
            and len(slot._dismissed_source_links) >= _MAX_DISMISSED_SOURCE_LINKS
        )
        if cap_refused:
            return _reject(
                web.json_response(
                    {
                        "error": "too many dismissed source links for this session",
                        "code": "dismissed_source_links_full",
                    },
                    status=409,
                ),
                error="dismissed_source_links_full",
                phase="cap",
            )
        # Enter the persist path when THIS request first dismissed the key, OR
        # when the key sits in memory only tentatively (not yet on disk) — the
        # latter is the concurrent-rebind case that must not be acknowledged off
        # an uncommitted dismissal.
        if newly_dismissed or not durably_dismissed:
            # Persist-before-publish: the dismissal is applied in memory and
            # tracked as txn-in-flight here, but the client broadcast that makes
            # the chip disappear is deferred until AFTER the guarded durable write
            # lands (see the ``persisted`` success path below). Announcing removal
            # only once disk has recorded it means a persist failure rolls the
            # tentative in-memory dismissal back and returns 409 with NO broadcast
            # ever having gone out, so a client never observes a chip removed that
            # disk rejected. The durable write is a fast local metadata write, so
            # the removal still lands within the same request without a refetch.
            aliases = [
                s
                for s in list(state._slots.values())
                if slot_history_key(s) == authorized_history_key
            ]
            # Track which slots THIS request newly added the identity to (the
            # requesting slot plus any alias that was not already showing it
            # dismissed). Only these may be rolled back on a persist failure —
            # an alias that had already committed this dismissal keeps it, or the
            # rollback would resurrect a chip it legitimately removed earlier.
            # Include the requesting slot ONLY when THIS request actually added
            # the key to it (``newly_dismissed``). When we entered the persist
            # path over a key the slot ALREADY held (a pre-existing durable
            # tombstone reached via a non-durable/stale read), rolling it back
            # would erase a dismissal this request did not create.
            newly_added = [slot] if newly_dismissed else []
            for other in aliases:
                if other is not slot and other.dismiss_source_link(identity_key):
                    newly_added.append(other)
                    publication_needed = True
            # Mark every slot carrying THIS request's tentative dismissal as
            # txn-in-flight by INCREMENTING its depth counter, so a periodic
            # full-save flush that fires before the guarded write commits carries
            # the on-disk dismissed line forward instead of persisting the
            # tentative set. Keep every touched slot tracked through ALL awaits,
            # including a slot that leaves and later returns to this transcript.
            # A concurrent unlink on a different transcript keeps its own depth.
            txn_slots.extend(newly_added)
            for s in txn_slots:
                s._dismissed_txn_depth += 1
                # Mark THIS request's key tentative on every touched slot so the
                # source-link projection keeps the chip visible until the guarded
                # write commits. Without this, a concurrent ``push_slots_update``
                # during the write below would publish the removal off the
                # in-memory set alone, and a failed write rolling it back would
                # leave a client showing a chip disk still records. Invalidate the
                # cached projection so that concurrent broadcast recomputes with
                # the key still present.
                s._dismissed_txn_pending.add(identity_key)
                s.invalidate_source_links()
            # The write set is deliberately NOT the raw union of every live
            # alias's in-memory dismissed set. A slot that rebound B→A can arrive
            # in ``aliases`` still carrying a CONCURRENT unlink's TENTATIVE (not
            # yet committed) dismissal for B's identity; unioning that in would
            # persist B's tombstone onto A and hide a link A never dismissed. So
            # this request contributes ONLY the one identity it is authorized to
            # dismiss (``identity_key``); every OTHER dismissal A legitimately
            # holds is COMMITTED, hence on A's on-disk line, and is folded in
            # below. A committed sibling dismissal is therefore retained; an
            # in-memory-only (tentative, possibly foreign) key is correctly
            # excluded. Dismissals still only grow: on-disk ∪ {identity_key}.
            union: set[str] = {identity_key}
            conv_log = state.conversation_log
            # ALWAYS fold the on-disk dismissed set into ``union`` before writing
            # — not only when a live alias is dismissed-unhydrated. Two distinct
            # ways ``union`` (rebuilt from the LIVE aliases' in-memory sets) can
            # under-represent the durable set:
            #   1. a live alias is dismissed-unhydrated (its in-memory set is an
            #      incomplete EMPTY stand-in), or
            #   2. an alias that held a UNIQUE, already-committed tombstone has
            #      DEPARTED this transcript (rebound away), so its tombstone is on
            #      disk but in NO live alias's memory.
            # In (2) every live alias can be fully hydrated yet ``union`` still
            # omits the departed alias's tombstone, so a fold gated on
            # "any unhydrated alias" would skip it and this write would SHRINK the
            # on-disk line — the departed alias's dismissed chip reappears after
            # restart. Dismissals only ever grow, so folding the on-disk line in
            # unconditionally can only ADD, never remove, which also keeps the
            # tiny read-before-write window safe. Publish the full set and mark
            # current aliases hydrated only after the transaction's final await.
            #
            # If that fold-in read is ITSELF unreadable we do not know the durable
            # set, so writing ``union`` could overwrite real tombstones (a
            # departed alias's, or an unhydrated alias's). Mark the fold failed
            # and DECLINE to persist (fall to rollback + 409) rather than persist
            # an under-approximation that erases a committed dismissal.
            fold_failed = False
            if conv_log is not None:
                try:
                    _disk_meta, _disk_readable = await asyncio.to_thread(
                        conv_log.get_metadata_status, authorized_history_key
                    )
                except Exception:
                    _disk_meta, _disk_readable = {}, False
                # Do not install the folded set into live aliases here. They
                # can depart during this read OR a subsequent write. Publish it
                # only after the last await, onto freshly selected live aliases.
                if _disk_readable:
                    _disk = _disk_meta.get("dismissed_source_links")
                    if isinstance(_disk, list):
                        # Bound the disk-fold DURING collection so an oversized
                        # on-disk line never materializes in full before the
                        # ceiling below applies.
                        union |= bounded_valid_identities(_disk, _MAX_DISMISSED_SOURCE_LINKS)
                        # The add-site cap guards the per-slot in-memory set, but
                        # folding the authoritative on-disk line in can still push
                        # the write past the ceiling when disk already holds the
                        # full set and this request contributes a new key. A bound
                        # must bound every field it RETAINS, so never write more
                        # than ``_MAX_DISMISSED_SOURCE_LINKS`` entries: if the fold
                        # exceeds it, the durable set is already at the limit, so
                        # deny this request's growth (fall to rollback + 409)
                        # rather than commit an oversized line that restore would
                        # tail-truncate — resurrecting whichever chip fell off.
                        if len(union) > _MAX_DISMISSED_SOURCE_LINKS:
                            fold_failed = True
                else:
                    fold_failed = True
            stale = _reauthorize_after_await(
                state, slot, name, request_app, "slot_source_link_unlink"
            )
            if stale is not None or slot_history_key(slot) != authorized_history_key:
                fold_failed = True
            # update_metadata_if reports whether the merge actually landed: it
            # returns False when the transcript's metadata line is unreadable or
            # the guard rejects it (``_update_metadata_locked`` silently no-ops on
            # a malformed/absent line, so a plain update_metadata could write
            # NOTHING yet raise nothing — acknowledging a dismissal disk never
            # recorded). The guard requires an EXISTING metadata line
            # (``_type == "metadata"``): a concurrently-deleted transcript reads
            # back as ``({}, True)`` (readable, empty), and a guard that merely
            # accepted any dict would let the write RECREATE the deleted line and
            # resurrect the session. Requiring ``_type`` rejects the empty case,
            # so a lost race declines to persist (rollback + 409) instead.
            try:
                if conv_log is None:
                    raise RuntimeError("no conversation log")
                if fold_failed:
                    # An unhydrated alias whose durable set we could not read:
                    # persisting the incomplete union would drop real tombstones,
                    # so decline (rollback + 409) rather than under-approximate.
                    raise RuntimeError("dismissed fold-in read unreadable")
                # update_metadata_if enters ``_locked`` (flock + os.close), which
                # is blocking-on-loop-prohibited, so it goes to a worker thread.
                # The payload carries the pre-lock ``union``; ``_merge_guard``
                # re-folds the locked on-disk set into it at write time so a
                # concurrent gateway's dismissal in the read-to-lock window is
                # not erased.
                _add_payload: dict[str, object] = {"dismissed_source_links": sorted(union)}
                persisted = await asyncio.to_thread(
                    conv_log.update_metadata_if,
                    authorized_history_key,
                    _add_payload,
                    _merge_guard(_add_payload),
                )
                if persisted:
                    # ``_merge_guard`` rewrote ``_add_payload`` under the lock to
                    # the set that ACTUALLY landed on disk — this request's
                    # contribution UNIONED with the locked on-disk line, which a
                    # concurrent gateway sharing this data home may have grown
                    # with its own committed dismissal in the read-to-lock
                    # window. Fold that persisted set back into ``union`` so the
                    # values installed onto live aliases and published below
                    # reflect what disk holds, not the pre-lock ``union`` that
                    # omits the sibling. Without this, aliases are marked
                    # hydrated on an incomplete set and the sibling's chip
                    # resurfaces until a later disk re-read. Dismissals only
                    # grow, so this can only ADD.
                    _persisted_set = _add_payload.get("dismissed_source_links")
                    if isinstance(_persisted_set, list):
                        union |= {k for k in _persisted_set if is_valid_source_identity_key(k)}
            except Exception:
                persisted = False
                logger.exception("Slot %s source-link dismissal persist failed", name)
            # Reauthorize across the persist await. ``linked_session_key`` is
            # rebound on already-live slots with no ``running`` gate (a cron
            # completion, a workflow injection), so during the ``to_thread``
            # window a slot this request just dismissed on can be rebound to a
            # DIFFERENT transcript. The in-memory dismissal would then ride into
            # the new conversation and suppress an unrelated matching link on its
            # next save. Drop the dismissal from any slot whose current history
            # key differs from the authorized transcript (that transcript was
            # never its to dismiss), on BOTH the success and failure paths.
            # Rollback separately checks the current key so it never removes a
            # foreign conversation's independently committed dismissal.
            #
            # BUT only KEEP the dismissal on a rebound slot when ``identity_key``
            # IS already dismissed on the slot's NEW transcript: a concurrent
            # unlink may have committed exactly this key on the rebind target, and
            # a blind discard would erase that freshly-committed dismissal in
            # memory and make the chip the other request just removed reappear.
            # Read the new transcript's dismissed set off-loop; keep the key when
            # it is durably dismissed there (legitimately the target's), discard
            # it otherwise. On an UNREADABLE read we DISCARD (the safe default):
            # keeping a key that turns out foreign would let the union-on-save
            # guard persist it into the target and hide the target's own chip,
            # while discarding a key that turns out to be the target's own is
            # re-added from the target's on-disk line on its next save — a wrong
            # discard self-heals, a wrong keep contaminates.
            # This reconciliation must run after EVERY await that can rebind a
            # slot in ``newly_added`` — the first persist await above and the
            # confirm await further below (late-alias joiners are appended to
            # ``newly_added`` just before that second await, so a joiner that
            # rebinds during the confirm would otherwise carry a foreign dismissal
            # into its replacement transcript's next save). Hence the helper.

            async def _reconcile_rebound() -> None:
                # One bounded read batch, followed by one non-awaiting apply.
                # Keep tracking ALL transaction slots even after a prior pass:
                # a slot kept on B can move to C during a later confirm/read.
                rebound = [
                    (s, slot_history_key(s))
                    for s in txn_slots
                    if slot_history_key(s) != authorized_history_key
                ]
                readings: dict[str, tuple[dict, bool]] = {}
                for _, new_key in rebound:
                    if new_key not in readings and conv_log is not None:
                        try:
                            readings[new_key] = await asyncio.to_thread(
                                conv_log.get_metadata_status, new_key
                            )
                        except Exception:
                            readings[new_key] = ({}, False)
                # No awaits below: even a second rebind during an earlier
                # target's read cannot install that target's value elsewhere.
                # Include slots that departed DURING the batch, not just those
                # in the initial snapshot. Unknown targets discard and retry
                # hydration later; never chase a moving slot with a read loop.
                changed = False
                for s in txn_slots:
                    current_key = slot_history_key(s)
                    if current_key == authorized_history_key:
                        continue
                    meta, readable = readings.get(current_key, ({}, False))
                    disk = meta.get("dismissed_source_links")
                    keep = readable and isinstance(disk, list) and identity_key in disk
                    if not keep:
                        changed |= identity_key in s._dismissed_source_links
                        s._dismissed_source_links.discard(identity_key)
                        s.invalidate_source_links()
                        if not readable:
                            s._dismissed_hydrated = False
                if changed:
                    _publish("reconcile")

            await _reconcile_rebound()
            first_committed = False  # set True only if a confirm follows a landed first write
            if persisted:
                # The guarded durable write has landed, so the dismissal is
                # committed: drop the committed identity from every tracked
                # slot's pending fence BEFORE publishing. The fence exists only
                # to keep the chip visible while the write is in flight; once
                # disk records the removal, a post-commit broadcast must project
                # the chip as GONE. Clearing here (not only in the transaction's
                # ``finally``, which runs after this publish) ensures the
                # ``broadcast`` / late-alias / confirm publishes below all
                # project the committed removal rather than subtracting a
                # now-stale pending key and leaving other clients showing the
                # chip. The ``finally`` still clears the fence on the rollback
                # path where no commit reached this point.
                for s in txn_slots:
                    if identity_key in s._dismissed_txn_pending:
                        s._dismissed_txn_pending.discard(identity_key)
                        s.invalidate_source_links()
                # Persist-before-publish: the guarded durable write has now
                # landed, so announce the chip removal. Deferring the broadcast
                # to here (instead of an optimistic pre-persist publish) means a
                # persist failure returns 409 with no removal ever broadcast, so
                # a client never observes a chip that disk rejected. This publish
                # runs with the default non-aborting mode: a delivery failure only
                # flags ``publication_needed`` for a later retry and never
                # unwinds the committed durable write, so disk and the client
                # re-converge on the next push without leaking an unacknowledged
                # chip.
                _publish("broadcast")
                # Mirror onto any alias that bound INTO the authorized transcript
                # DURING the await. The ``aliases`` snapshot was taken before the
                # persist, so a slot rebound onto this transcript mid-write missed
                # the in-memory mirror; the persisted line already carries the
                # dismissal, but the joined alias's own set is stale until it next
                # hydrates, and its full save meanwhile would serialize a set
                # WITHOUT this identity and overwrite the acknowledged tombstone.
                # Re-scan and add it so every live alias's in-memory set matches
                # what disk now records.
                joined = [
                    s
                    for s in list(state._slots.values())
                    if slot_history_key(s) == authorized_history_key
                    and identity_key not in s._dismissed_source_links
                ]
                if joined:
                    for s in joined:
                        s.dismiss_source_link(identity_key)
                        s._dismissed_txn_depth += 1
                        txn_slots.append(s)
                        # Track them for the failure rollback below: if the
                        # confirm write fails, their just-mirrored dismissal must
                        # be reverted too so acknowledged state matches disk.
                        newly_added.append(s)
                    _publish("late_alias")
                    # Confirm the field-scoped write AFTER mirroring the late
                    # joiners. The first write's union predates them, and a
                    # joiner can carry a stale full-slot flush (queued with its
                    # OLD dismissed set, before it joined) that lands AFTER this
                    # request and overwrites the acknowledged tombstone. Re-assert
                    # the recomputed union so disk reflects every live alias; if
                    # this confirm cannot land, the acknowledgement is not durable
                    # — fall through to the rollback + 409 below.
                    # Re-assert EXACTLY the first write's set (A's on-disk
                    # committed line ∪ this request's ``identity_key``), never a
                    # fresh union of the live aliases' raw in-memory sets: those
                    # can carry a concurrent unlink's TENTATIVE foreign key on a
                    # rebound slot, which the confirm would then persist onto A
                    # (the same cross-transcript leak the first ``union`` avoids).
                    # ``union`` already merged the on-disk line, so this is the
                    # complete, leak-free set; dismissals only grow, so it can
                    # never drop a departed alias's committed tombstone either.
                    confirm_union: set[str] = set(union)
                    first_committed = persisted  # the pre-confirm write reached disk
                    try:
                        if conv_log is None:
                            raise RuntimeError("no conversation log")
                        _confirm_payload: dict[str, object] = {
                            "dismissed_source_links": sorted(confirm_union)
                        }
                        persisted = await asyncio.to_thread(
                            conv_log.update_metadata_if,
                            authorized_history_key,
                            _confirm_payload,
                            _merge_guard(_confirm_payload),
                        )
                    except Exception:
                        persisted = False
                        logger.exception("Slot %s source-link dismissal confirm failed", name)
                    if persisted:
                        # As with the first write, ``_merge_guard`` rewrote
                        # ``_confirm_payload`` under the lock to the set that
                        # actually landed. Fold it back into ``union`` so the
                        # install/publish below reflects a concurrent gateway's
                        # committed sibling rather than the pre-lock set.
                        _confirmed_set = _confirm_payload.get("dismissed_source_links")
                        if isinstance(_confirmed_set, list):
                            union |= {k for k in _confirmed_set if is_valid_source_identity_key(k)}
                    # A late-alias joiner (appended to ``newly_added`` above) can
                    # rebind AWAY during the confirm await just as an original slot
                    # can rebind during the first persist await. Re-run the same
                    # reconciliation so a joiner that left carries no foreign
                    # dismissal into its replacement transcript's next save.
                    await _reconcile_rebound()
            if not persisted:
                # The merge did not reach disk (raised, or refused by the guard /
                # unreadable line), so acknowledging 200 would show a chip gone
                # that reappears on restart. Roll back ONLY the slots this request
                # newly dismissed AND still owns so acknowledged state matches
                # disk, and 409.
                #
                # BUT only when the FIRST write did not commit. If it did
                # (``first_committed``), disk already durably holds this request's
                # dismissal (a grow-only tombstone) and the accept-committed path
                # below keeps it — stripping it here and publishing would emit a
                # chip-RESTORED frame that the accept-committed publish then undoes,
                # a visible flicker of a chip disk never un-dismissed. So when the
                # first write committed, leave the in-memory dismissal in place and
                # publish nothing here; the accept-committed branch (or the
                # else-branch rollback, if the transcript was since recreated)
                # emits the single correct final frame.
                if not first_committed:
                    for s in newly_added:
                        if slot_history_key(s) == authorized_history_key:
                            s._dismissed_source_links.discard(identity_key)
                            s.invalidate_source_links()
                    # Keep the flush guards through compensation and its reads:
                    # rebound slots still carry a tentative key until the LAST
                    # reconciliation, even if they survived an earlier pass.
                    _publish("rollback")
                # Treat a committed FIRST write as TERMINAL — never compensate it
                # with a strip. When the pre-confirm write reached disk but the
                # confirm did not, disk already carries this request's dismissal.
                # A dismissal is a GROW-ONLY tombstone, so the committed set is a
                # valid durable state on its own (the confirm only re-asserted the
                # same set after mirroring late-alias joiners; its failure loses
                # nothing durable). Rolling the committed write back would require
                # reading the on-disk line and rewriting it MINUS ``identity_key``
                # — but that on-disk line can, on a shared-data-home multi-gateway
                # deployment, already carry ANOTHER gateway's independently-
                # committed dismissal of the SAME identity landed in the window
                # between our first write and the compensation read; subtracting
                # the key would then erase that gateway's acknowledged unlink.
                # There is no cross-process lock spanning the first write and the
                # compensation, so the only safe move is to NOT compensate:
                # keep the committed grow-only tombstone and accept it below, once
                # the transcript identity is re-verified. ``compensated`` stays
                # False so the identity re-check + accept-committed path runs.
                compensated = False
                # A committed first write was NOT undone (we deliberately do not
                # compensate it). Before accepting that write as durable we MUST
                # confirm the transcript is still the one we committed to:
                #   (a) transient confirm failure — the committed dismissal is
                #       still on the SAME transcript, so accepting it matches disk.
                #   (b) the transcript was deleted and recreated (fresh
                #       created_at) between the first write and now — the committed
                #       write went with the OLD transcript and is GONE; mirroring
                #       the dismissal onto the replacement would contaminate a
                #       session that never dismissed anything.
                # Re-read the identity off-loop and accept-committed ONLY when it
                # still matches the pin; a changed/unreadable identity falls to
                # the 409 rollback, leaving the replacement transcript untouched.
                still_authorized = False
                if first_committed and not compensated and conv_log is not None:
                    try:
                        _rc_meta, _rc_readable = await asyncio.to_thread(
                            conv_log.get_metadata_status, authorized_history_key
                        )
                        still_authorized = (
                            _rc_readable
                            and _rc_meta.get("_type") == "metadata"
                            and _rc_meta.get("created_at") == pinned_created_at
                        )
                    except Exception:
                        still_authorized = False
                # Compensation/read awaits can rebind any tracked slot again.
                # Nothing below this last pass may await or publish
                # a pre-await slot snapshot.
                await _reconcile_rebound()
                if first_committed and not compensated and still_authorized:
                    # The committed write IS durable on the SAME transcript, so
                    # reporting 409 while disk keeps the dismissal would desync
                    # (restart hides the chip for a "failed" request). Accept the
                    # committed state: re-mirror the dismissal onto every live
                    # alias and fall through to the success path, so acknowledged
                    # state matches what disk actually holds.
                    for s in list(state._slots.values()):
                        if slot_history_key(s) == authorized_history_key:
                            s.dismiss_source_link(identity_key)
                            s._dismissed_hydrated = True
                    _publish("accept_committed")
                else:
                    # A slot may have returned to the authorized transcript
                    # during compensation. Roll it back too, without touching
                    # a foreign target's independently committed dismissal.
                    for s in newly_added:
                        if slot_history_key(s) == authorized_history_key:
                            publication_needed |= identity_key in s._dismissed_source_links
                            s._dismissed_source_links.discard(identity_key)
                            s.invalidate_source_links()
                    if publication_needed:
                        _publish("final")
                    _audit_source_link_unlink(
                        name,
                        "failed",
                        error="session_gone",
                        metadata={"slot": name, "phase": "metadata_persist"},
                    )
                    return web.json_response(
                        {"error": "session was deleted or rebound", "code": "session_gone"},
                        status=409,
                    )
            # Install the folded values only on CURRENT aliases after the last
            # await. Until here only identity_key was tentative and tracked, so
            # a departed alias never inherits the rest of this transcript's set.
            for s in list(state._slots.values()):
                if slot_history_key(s) == authorized_history_key:
                    if not union <= s._dismissed_source_links:
                        # ASSIGN the authoritative bounded ``union`` — never
                        # ``|=`` it. ``union`` is on-disk ∪ {identity_key} and was
                        # already guarded to ``_MAX_DISMISSED_SOURCE_LINKS`` above
                        # (an over-cap fold set ``fold_failed`` and never reached
                        # here), so it holds every committed tombstone this slot
                        # should carry. Unioning it INTO the slot's existing
                        # in-memory set could push that set past the ceiling
                        # (existing keys not in ``union`` + ``union``'s keys),
                        # and the next capped save would then drop durable
                        # tombstones and resurrect hidden chips. Replacing with the
                        # bounded authoritative set keeps the field within its
                        # bound — ``a-bound-bounds-every-field-it-retains``.
                        s._dismissed_source_links = set(union)
                        s.invalidate_source_links()
                        publication_needed = True
                    s._dismissed_hydrated = True
            if publication_needed:
                _publish("final")
    _audit_source_link_unlink(
        name, "allowed", metadata={"slot": name, "already_dismissed": not newly_dismissed}
    )
    # Return the AUTHORITATIVE post-unlink total (the count the next slots push
    # will also carry) so the client assigns it rather than decrementing its own
    # ``source_links_total`` locally. A local ``- 1`` double-counts on an
    # idempotent retry: a peer tab's unlink of the same identity can already have
    # reduced the total while this client's placeholder still lists the chip, so
    # a second local decrement would understate the "+N" overflow count until the
    # next authoritative frame. ``_summary_source_links`` is the same pre-budget
    # derived list ``source_links_total`` is ``len()``'d from in the projection,
    # and the dismissal is already applied in memory here, so this is exactly the
    # value the projection would compute.
    source_links_total = len(slot._summary_source_links())
    return web.json_response(
        {"ok": True, "dismissed": True, "source_links_total": source_links_total}
    )


def _source_link_txn_lock(history_key: str) -> asyncio.Lock:
    lock = _source_link_txn_locks.get(history_key)
    if lock is None:
        lock = asyncio.Lock()
        _source_link_txn_locks[history_key] = lock
    return lock
