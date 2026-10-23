"""A crewmate's user-owned permanent rules: ``GET`` and ``PUT /api/members/{slug}/rules``.

The owner-gated read, and the one write path for the rules layer: the exact-name and
slug-collision checks, the size and encoding refusals, the event the member's log
records, the audit rows, and the reinjection flag that hands a warm session the new
rules.

Served through :mod:`kiro_crew.dashboard.handlers.members`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.members_access import (
    _deny_app_caller,
    _member_names_for_slug,
    _sel,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.members import MemberSlugError

logger = logging.getLogger("kiro_crew.dashboard.handlers.members")


async def api_member_rules_get(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/rules?member=<name> — user-owned permanent rules.

    The read half of the rules API (a Members-page rules editor is a
    follow-up; nothing in the frontend consumes this yet). ``member``
    (query, REQUIRED) is the
    exact crew name, same posture as the activity endpoint: slugification is
    lossy, and the name-scoped read is what keeps a colliding slug's editor
    from showing another member's safety rules. Absent rules read as ``""`` (a
    legal state), never 404: the editor's empty state IS "no rules yet". An
    EXISTING file that cannot be read answers 500 ``rules_unreadable`` rather
    than an empty editor a save would then silently overwrite.
    """
    denied = await _deny_app_caller(request, "members.rules")
    if denied is not None:
        return denied
    # Owner gate, same boundary as the PUT: the rules are the OWNER's private
    # safety instructions for this member. A Telegram, Teams or Webex allowlist
    # user can mint a dashboard session, so without this gate a non-owner
    # colleague could read boundaries the owner never shared — disclosure is
    # one-way, so the read is gated exactly like the write.
    owner_denied = await require_owner_dashboard_request(request, "members.rules.read")
    if owner_denied is not None:
        return owner_denied
    slug = request.match_info["slug"]
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return web.json_response(
            {"error": "invalid member slug", "code": "invalid_member_slug"}, status=400
        )
    member = request.query.get("member", "")
    if not members_mod.is_dispatchable_member_name(member):
        return web.json_response(
            {"error": "member query parameter required", "code": "missing_member"}, status=400
        )
    try:
        rules = await asyncio.to_thread(members_mod.read_member_rules, slug, member)
    except members_mod.MemberRulesUnreadable:
        logger.warning("member rules unreadable for %r", slug, exc_info=True)
        return web.json_response(
            {
                "error": (
                    "rules file exists but cannot be read; rewrite or clear "
                    "the rules via PUT /api/members/{slug}/rules to repair it"
                ),
                "code": "rules_unreadable",
            },
            status=500,
        )

    # Successful reads leave an audit trace too: the rules are the owner's
    # private safety boundary, so WHO read them matters as much as who was
    # refused — a denied-only trail cannot answer "was this boundary
    # disclosed". A direct enqueue, not a to_thread hop: the SEL singleton is
    # warmed at startup (sel.warm_sel_singleton), so the first-touch
    # initialization never runs on this call site. Guarded because a
    # FAILED warm leaves construction to retry on this thread and possibly
    # raise, and an audit must never change the outcome.
    try:
        _sel().log_api_access(
            caller=request.remote or "",
            operation="members.rules.read",
            outcome="allowed",
            source="dashboard",
            resources=f"slug={slug}",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for members.rules.read failed", exc_info=True)
    return web.json_response(
        {"slug": slug, "rules": rules, "max_chars": members_mod.MEMBER_RULES_MAX_CHARS}
    )


async def api_member_rules_put(request: web.Request) -> web.Response:
    """PUT /api/members/{slug}/rules — write a member's permanent rules.

    This is the ONLY write path for the rules layer, and it is a HUMAN
    dashboard action by construction: app tokens are denied like every member
    surface, and the file itself lives under the keystone-gated ``trust/``
    subtree the agent's tools cannot write. ``member`` in the body must name
    the exact registered crew the slug belongs to, and when TWO registered
    crews collide onto one slug the write is refused outright (409
    ``rules_slug_ambiguous``): the rules file is one-per-slug, so either
    colliding member's save would overwrite the other's safety boundary —
    ambiguous ownership is refused, never resolved silently.

    An empty ``rules`` string clears the rules (documented absent state).
    Over-cap payloads are refused with 400, never truncated.
    """
    denied = await _deny_app_caller(request, "members.rules")
    if denied is not None:
        return denied
    # Owner gate BEFORE any input validation: the rules layer is the USER's
    # safety boundary for this member, so writing it is owner-only — the same
    # server-side boundary the agent-config mutations enforce. Gating first
    # also keeps the route's non-owner answer a uniform 401/403 (the owner-gate
    # invariant test walks every mutating route), never a 400 that leaks
    # which slugs validate.
    owner_denied = await require_owner_dashboard_request(request, "members.rules.write")
    if owner_denied is not None:
        return owner_denied
    slug = request.match_info["slug"]
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return web.json_response(
            {"error": "invalid member slug", "code": "invalid_member_slug"}, status=400
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        # Valid JSON that is not an object (an array, a string) would raise on
        # .get() below — a coded 400, never a 500, for a malformed request.
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    member = body.get("member", "")
    if "rules" not in body:
        # Absent is NOT empty: an explicit "" clears the rules (documented),
        # but a payload that simply omitted the key must not silently delete
        # the user's safety boundary.
        return web.json_response(
            {"error": "rules field required", "code": "missing_rules"}, status=400
        )
    rules = body.get("rules", "")
    if not members_mod.is_dispatchable_member_name(member):
        return web.json_response(
            {"error": "member field required", "code": "missing_member"}, status=400
        )
    if not isinstance(rules, str):
        return web.json_response(
            {"error": "rules must be a string", "code": "invalid_rules"}, status=400
        )
    try:
        # JSON allows escaped lone surrogates; UTF-8 does not. Refuse them with
        # a coded 400 here — write_member_rules re-checks and raises ValueError
        # as the storage-layer backstop, but that branch answers "too long".
        rules.encode("utf-8")
    except UnicodeEncodeError:
        return web.json_response(
            {
                "error": "rules contain characters that cannot be encoded",
                "code": "rules_not_encodable",
            },
            status=400,
        )
    # Reuse this off-loop snapshot for identity validation and collision checks.
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    try:
        if members_mod.member_slug(member, cfg) != slug:
            return web.json_response(
                {"error": "member does not match slug", "code": "member_slug_mismatch"}, status=400
            )
    except MemberSlugError:
        return web.json_response(
            {"error": "member does not match slug", "code": "member_slug_mismatch"}, status=400
        )
    if member not in cfg.agents:
        return web.json_response(
            {"error": "no crew member for this slug", "code": "member_not_found"}, status=404
        )
    colliding = _member_names_for_slug(cfg, slug)
    if colliding != [member]:
        return web.json_response(
            {
                "error": "multiple crews share this slug; rules would be ambiguous",
                "code": "rules_slug_ambiguous",
            },
            status=409,
        )
    from kiro_crew.dashboard.handlers.change_cards import REQ_CARD_UNDO_RULES_EXPECT

    # A card Undo clearing the rules it wrote: only while they still read as it
    # left them, compared under the rules write lock (``write_member_rules``).
    expected = request.get(REQ_CARD_UNDO_RULES_EXPECT)
    guard = {"expected_sha256": expected} if isinstance(expected, str) else {}
    try:
        await asyncio.to_thread(
            members_mod.write_member_rules, slug, member=member, text=rules, **guard
        )
    except (members_mod.MemberRulesChanged, members_mod.MemberRulesUnreadable):
        if not guard:
            raise
        return web.json_response(
            {"error": "the rules changed after the import", "code": "changed_since_apply"},
            status=409,
        )
    except ValueError:
        return web.json_response(
            {
                "error": f"rules exceed {members_mod.MEMBER_RULES_MAX_CHARS} characters",
                "code": "rules_too_long",
            },
            status=400,
        )
    except OSError:
        logger.warning("member rules write failed for %r", slug, exc_info=True)
        return web.json_response(
            {"error": "could not persist rules", "code": "rules_write_failed"}, status=500
        )

    # Record the rules change in the member's append-only log. The trust-file
    # write above is the security fence and stays authoritative; this is the
    # durable event so the log reflects the current rules text. Best-effort and
    # off-loop; a logging fault never fails a save the fence already persisted.
    def _emit_rules() -> None:
        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog.types import MEMBER_RULES

        eventlog_hooks.emit(slug, member, MEMBER_RULES, {"text": rules})

    await asyncio.to_thread(_emit_rules)

    # Same audit posture as the GET: a successful boundary WRITE is the event
    # an owner most needs a trace of — it is the moment the member's safety
    # rules changed. Direct enqueue for the same reason (SEL warmed at startup).
    try:
        _sel().log_api_access(
            caller=request.remote or "",
            operation="members.rules.write",
            outcome="allowed",
            source="dashboard",
            resources=f"slug={slug}",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for members.rules.write failed", exc_info=True)
    # A warm member session injected its rules at session start; without this,
    # the member keeps running under the OLD boundary until a compaction or a
    # cold start happens to refresh it. Flag the thread's session for
    # reinjection: the next turn re-injects the whole member section (fresh
    # rules included) through the same post-compaction branch, no session
    # teardown needed — and the flag is a no-op when no session is warm.
    try:
        state: DashboardState = request.app["state"]
        binding = await asyncio.to_thread(members_mod.read_dm_binding, slug)
        if binding is not None:
            state.sessions.mark_needs_reinjection(
                members_mod.member_thread_session_alias(slug, binding.get("memory_store", ""))
            )
    except Exception:
        # Best-effort: the write LANDED (the durable state is correct), and a
        # cold session picks the new rules up at its next start regardless.
        logger.debug("could not flag member session for reinjection", exc_info=True)
    return web.json_response({"slug": slug, "ok": True})
