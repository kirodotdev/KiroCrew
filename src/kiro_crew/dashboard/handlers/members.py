"""Crew Members HTTP handlers — roster and per-member DM thread binding.

The Crew Members page talks to each crew member in one durable, pinned DM
thread. The thread's slot key is DERIVED (``member-<slug>``) and its binding
lives in the member's own space (``members/<slug>/dm.json``), so the mapping
survives restarts independently of the slot layer's own persistence.

Member slots are born ONLY here, with ``mode="member"``: the generic slot
create endpoint's ``_CREATABLE_MODES`` deliberately excludes it, and the
frontend's chat-ownership predicate (``isChatPageSurface``) does not admit it,
which is what keeps member threads out of the ordinary Sessions list with no
filtering code anywhere.

Dashboard-only surface: app tokens are denied outright (deny-by-default, same
posture as slot access — an app has no business enumerating the user's crews
or opening threads that speak as them).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter

from aiohttp import web

import kiro_crew.dashboard.handlers as _h
from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig, default_project_dir
from kiro_crew.dashboard.chat_persistence import (
    pin_private_agent_store,
    rehydrate_slot_from_history_async,
)
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.state import DashboardState, request_slot_origin
from kiro_crew.members import MemberSlugError
from kiro_crew.validation import _AGENT_NAME_RE

logger = logging.getLogger(__name__)

#: Activity entries returned to the drawer. Bounds the payload and the JSONL
#: scan alike; the log itself rotates at ~256KiB so this is a display cap,
#: not a durability boundary.
_ACTIVITY_LIMIT = 50
# How many log envelopes one backwards page reads. Larger than the display limit
# because the log is shared: config, binding, rules, message, slot and patrol
# events all sit between one member's activity records, so a page the size of the
# display cap would usually need several round trips to fill it. This bounds the
# allocation per page, which is the point -- it is not a cap on the answer.
_ACTIVITY_PAGE = 500


def _parse_activity_ts(raw: str) -> float:
    """Epoch seconds from an activity record's ISO-8601 ``ts``, or 0.0.

    ``record_activity`` writes ``%Y-%m-%dT%H:%M:%SZ`` (UTC, second
    precision); tolerate a ``+00:00`` suffix too since ``fromisoformat``
    accepts it and hand-edited logs exist. Anything that is not a string in
    that shape — including a numeric epoch from a foreign writer — reads as
    unplaceable (0.0) rather than crashing the endpoint: the log is
    append-only from multiple processes and tolerant reads are its contract.
    """
    if not isinstance(raw, str) or not raw:
        return 0.0
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


async def _deny_app_caller(request: web.Request, operation: str) -> web.Response | None:
    """404 for app-token callers; ``None`` for the dashboard user.

    404 rather than 403, matching the slot-access denials: a distinct status
    would confirm the surface exists to a caller that may not know about it.

    The audit is a bare enqueue: SEL is warmed at gateway startup
    (``sel.warm_sel_singleton``), so the first-touch filesystem initialization
    never runs on this call site. Guarded because a FAILED warm leaves
    construction to retry on this thread and possibly raise.
    """
    request_app = request.get("app", "")
    if not request_app:
        return None
    try:
        _sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            error="apps cannot access member threads",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for %s app denial failed", operation, exc_info=True)
    return web.json_response({"error": "not found", "code": "not_found"}, status=404)


def _member_names_for_slug(cfg: KiroCrewConfig, slug: str) -> list[str]:
    """Crew names whose derived slug equals *slug*, in config order.

    Config order is insertion order, so "first name wins" is deterministic for
    a colliding slug. Names failing the agent-name grammar are skipped rather
    than matched: they cannot have been created through the validated CRUD
    surface, so a hand-edited config row never becomes addressable here.

    ADDRESSABILITY only. A caller asking whether a crew still EXISTS must use
    :func:`_slug_is_claimed_by_any_member` instead -- see the contrast there.
    """
    out: list[str] = []
    for name in cfg.agents:
        if not _AGENT_NAME_RE.match(name):
            continue
        try:
            if members_mod.member_slug(name, cfg) == slug:
                out.append(name)
        except MemberSlugError:
            continue
    return out


def _slug_is_claimed_by_any_member(cfg: KiroCrewConfig, slug: str, owner_key: str) -> bool:
    """Whether a crew named in the roster still derives *slug* and *owner_key*.

    The same enumeration as :func:`_member_names_for_slug` WITHOUT the grammar
    filter, and the difference is the point. That filter is right for deciding
    what a route may address -- an ungrammatical row stays unreachable -- and
    wrong for deciding whether a crew is still there, because the create route
    validates a crew name only against the credential-shape check, so a name the
    grammar rejects can be a real, live crew. Filtering it out here would report
    a live owner as gone, and the caller reads "gone" as permission to take its
    record over.

    Compared on the ownership DIGEST, like every other check on this path, so no
    crew name has to be carried around to make the comparison.

    Resolved through ``member_slug``, the same function the publish path uses to
    choose which record to write, and NOT through ``slug_for_name``. The two
    disagree for a crew whose persisted ``member_id`` is not what its name
    derives -- which provisioning produces deliberately, to keep a recreated
    crew off a deleted namesake's records. Resolving one side by persisted
    identity and the other by name would skip exactly that crew here, report a
    live owner as gone, and hand its record to the next writer.

    ``agent_panel`` is imported HERE rather than at module scope because this
    module is pulled in while the gateway boots, and the panel subsystem is
    optional: loading it before the socket is bound delays readiness for every
    installation, including the ones that never assign a panel.
    """
    from kiro_crew import agent_panel as agent_panel_mod

    for name in cfg.agents:
        try:
            if members_mod.member_slug(name, cfg) != slug:
                continue
        except MemberSlugError:
            continue
        if agent_panel_mod.crew_key(name) == owner_key:
            return True
    return False


#: The roster's origin vocabulary. ``source`` on the record is free text in a
#: hand-editable, agent-writable config, so it never reaches the response raw:
#: the two known non-package origins pass through and everything else -- the
#: legacy ``aim`` spelling, a typo, a credential-shaped string -- collapses to
#: ``package``, which is also what the sync's prune step treats as package.
_SOURCE_KIROCREW = "kirocrew"
_SOURCE_BUILTIN = "builtin"
_SOURCE_PACKAGE = "package"


def normalize_member_source(raw: object) -> str:
    """Bound a record's ``source`` to the three values the roster renders."""
    if raw == _SOURCE_KIROCREW:
        return _SOURCE_KIROCREW
    if raw == _SOURCE_BUILTIN:
        return _SOURCE_BUILTIN
    return _SOURCE_PACKAGE


async def api_members(request: web.Request) -> web.Response:
    """GET /api/members — crew roster with DM binding and cheap live status.

    One row per GLOBAL crew (project-scoped crews are out of V1's scope: the
    per-member space is keyed off the global registry). Status fields are
    limited to what costs no IO and no redaction pass — ``running`` is an O(1)
    property read; everything richer (last message, waiting states) rides the
    already-subscribed WS ``slots`` frames on the frontend, so this endpoint
    only fills the cold-start gap.
    """
    denied = await _deny_app_caller(request, "members.list")
    if denied is not None:
        return denied
    state: DashboardState | None = request.app.get("state")
    cfg = await asyncio.to_thread(KiroCrewConfig.load)

    # The roster's redaction chokepoint, shared with ``GET /api/agents`` so the
    # two endpoints cannot drift apart. Function-local for the same reason
    # ``agent_panel`` is below: ``handlers.agents`` reaches back into this
    # package at import time, so a module-level import would close the cycle.
    from kiro_crew.dashboard.handlers.agents import _roster_avatar, _roster_mask

    rows: list[dict] = []
    for name, agent_cfg in cfg.agents.items():
        if not _AGENT_NAME_RE.match(name):
            continue
        try:
            slug = members_mod.member_slug(name, cfg)
        except MemberSlugError:
            continue
        store = agent_cfg.memory_store
        record = getattr(cfg, "memory_stores", {}).get(store)
        version = getattr(record, "memory_version", 1 if store == "default" else None)
        owner = getattr(record, "owner_member", "")
        if name != "default" and version == 1 and not owner:
            if any(item.owner_member == name for item in cfg.memory_stores.values()):
                version = None
        rows.append(
            {
                # Explicit allowlist — never a dataclass spread. The response
                # is a network-boundary contract: spreading `AgentConfig`
                # would ship every future field (including a credential-shaped
                # one) to the roster endpoint automatically. Each field below is
                # here because a caller renders or routes on it.
                # `name` and `slug` stay verbatim: they are the row's
                # IDENTITY, which every per-member route is keyed on, and a
                # credential-shaped name is refused at creation
                # (`_name_would_be_masked`). Every other
                # record value is agent-writable free text, so it goes through
                # `_roster_mask` and is replaced WHOLESALE when the redactors
                # would alter it.
                "name": name,
                "slug": slug,
                "kiro_agent": _roster_mask(agent_cfg.kiro_agent),
                "workspace": _roster_mask(agent_cfg.workspace),
                "memory_store": _roster_mask(agent_cfg.memory_store),
                "memory_version": version,
                "memory_owner": _roster_mask(owner),
                "model": _roster_mask(agent_cfg.model),
                # Presentation-only, but `_safe_avatar` pins only the SHAPE:
                # its `traits` and `expressions` values are free text, so the
                # avatar is masked leaf-by-leaf (`_roster_avatar`) rather than
                # shipped raw. Without it every Members surface silently falls
                # back to the name-derived face.
                "avatar": _roster_avatar(getattr(agent_cfg, "avatar", {})),
                # Roster-filter inputs. `source` lets the page collapse the
                # package-installed majority the agent sync writes; it is
                # NORMALIZED, never the raw config string (see
                # normalize_member_source). `starred` is a load-time-coerced
                # bool (the user's own favourite mark, PUT /api/agents/{name}).
                "source": normalize_member_source(agent_cfg.source),
                "starred": bool(agent_cfg.starred),
                # A crew's IDENTITY: who it is, and the phrasings that should
                # reach it. Both are operator-authored prose already stored on the
                # crew, and both are needed off-config — a roster that shows a
                # crew's memory store but not what it is for cannot answer "which
                # of these should handle a ticket", by the reader or by a router.
                # An empty `triggers` is meaningful rather than missing: it is the
                # operator's opt-out from being routed to at all.
                "description": _roster_mask(agent_cfg.description),
                "triggers": _roster_mask(agent_cfg.triggers),
            }
        )

    # Binding reads are file IO — one thread hop for the whole roster, not one
    # per row. Colliding slugs read the same file twice at most.
    def _read_bindings() -> dict[str, dict | None]:
        return {row["slug"]: members_mod.read_dm_binding(row["slug"]) for row in rows}

    bindings = await asyncio.to_thread(_read_bindings)

    for row in rows:
        binding = bindings.get(row["slug"])
        # The binding's own `member` field is authoritative: a colliding slug's
        # dm.json belongs to exactly one crew name, so only the exact-name
        # match reads as bound. `bound` itself is not exposed: the page never
        # trusts it (every open POSTs the thread endpoint regardless).
        bound = binding is not None and binding.get("member") == row["name"]
        slot_key = binding["slot_key"] if bound and binding else ""
        row["slot_key"] = slot_key
        slot = state._slots.get(slot_key) if (state and slot_key) else None
        row["running"] = bool(slot.running) if slot is not None else False

    # Last activity, for the roster's most-recent-first ordering. The DM
    # transcript's mtime is the one durable signal that survives restarts and
    # covers live and dormant threads alike. File stats are IO — one thread
    # hop for the whole roster, mirroring the binding reads above.
    def _read_transcript_tails() -> dict[str, tuple[float, str, bool]]:
        if state is None or state.conversation_log is None:
            return {}

        def _sanitize(text: str) -> str:
            # Same redaction chain the sessions list uses, injected so it
            # runs BEFORE the preview's length cap — a credential split by
            # truncation leaves a partial token the patterns cannot match.
            text, _ = _h.redact_exfiltration_urls(text)
            text, _ = _h.redact_credentials(text)
            return text

        out: dict[str, tuple[float, str, bool]] = {}
        for row in rows:
            if not row["slot_key"]:
                continue
            # A non-empty slot_key came from read_dm_binding, which refuses
            # any binding whose slot_key is not the slug's own derivation —
            # so the canonical alias helper reads the same key the binding
            # names, and the alias format stays owned by ONE function.
            binding = bindings.get(row["slug"])
            generation = binding.get("memory_store", "") if binding is not None else ""
            log_key = members_mod.member_thread_session_alias(row["slug"], generation)
            mt = state.conversation_log.session_mtime(log_key)
            if not mt:
                continue
            preview, msg_ts, stopped = state.conversation_log.last_message_info(
                log_key, sanitize=_sanitize
            )
            # Order by the newest MESSAGE, not the file: metadata writes and
            # rehydration bump the mtime without any new message, which made
            # rows reorder with no visible cause. mtime remains only as the
            # fallback for pre-timestamp transcript rows.
            out[row["slot_key"]] = (msg_ts or mt, preview, stopped)
        return out

    tails = await asyncio.to_thread(_read_transcript_tails)
    for row in rows:
        mt, preview, stopped = tails.get(row["slot_key"], (0.0, "", False))
        row["last_active_ts"] = mt
        row["last_message"] = preview
        # A locale-independent boolean, NEVER the word "Stopped": the preview
        # is computed here where the client's locale is unknown, which is why
        # the trailing stop is SKIPPED from `last_message` rather than rendered
        # as a sentence. This flag lets the locale-aware client render its own
        # "Stopped" chip beside the preview, so a thread the user has stopped
        # does not read as ongoing work. Omitted when false so the common row
        # stays byte-for-byte what it is without it.
        if stopped:
            row["last_message_stopped"] = True

    # Per-member event-log projections + lazy config reconcile. Off-loop
    # because ensure/append/snapshot are synchronous file IO (one fsync per
    # append). Best-effort: a logging fault never breaks the roster, so a
    # member whose log cannot be reconciled falls back to an empty projection
    # rather than failing the endpoint. The config-derived row fields are
    # sourced from the reconciled roster view — after reconcile they equal the
    # live config, so a hand-edited config is corrected in the log AND the row
    # stays byte-identical to what it would have carried straight from cfg.
    agent_cfgs = {row["name"]: cfg.agents.get(row["name"]) for row in rows}

    def _project_rows() -> dict[str, dict]:
        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog.service import get_service

        svc = get_service()
        out: dict[str, dict] = {}
        # This map is keyed by SLUG while the roster is keyed by row, and a slug is
        # a lossy fold, so two rows can land on one key. Whichever row is projected
        # last would win it: in one order the log's own member loses its state to a
        # stranger's blank, and in the other the stranger's row renders the owner's
        # roster, activity, wake and driving state as its own. Counting the rows per
        # slug FIRST makes the answer independent of iteration order -- a collided
        # slug is blank for everyone, which is the same visibly-empty row the
        # header-name guard below already serves, and never somebody else's data.
        slug_rows = Counter(row["slug"] for row in rows)
        for row in rows:
            slug = row["slug"]
            try:
                if slug_rows[slug] > 1:
                    logger.warning(
                        "member slug %r is shared by %d members, so none of them "
                        "gets a projection; rename one member so their slugs differ",
                        slug,
                        slug_rows[slug],
                    )
                    out[slug] = {"asOfSeq": -1, "values": {}}
                    continue
                # Hand over the config this read already loaded: ensure resolves a
                # placeholder header name, and `name == slug` is true for any member
                # whose name IS its own fold, so without this the roster would load
                # the config once per row off the loop.
                svc.ensure(slug, row["name"], cfg)
                # A slug is LOSSY, and colliding names are supported: `Review_Agent`
                # and `review-agent` both fold to `review-agent`, and each activity
                # entry keeps the exact name so attribution survives. What does NOT
                # survive is a whole-member PROJECTION: one log holds one member's
                # folded roster, activity, wake and driving state, so serving it on a
                # second member's row renders the first member's work as the second's.
                # The header names the member the log belongs to, so a row that is
                # not that member is served an empty projection instead of a wrong
                # one. Logged at warning level because a blank row needs its reason.
                # A header holding the SLUG is exempt: `ensure` writes the header only
                # while the log is fresh, so a writer with no name in hand (the
                # message path passes None) locks the slug in as the name for good.
                # That placeholder names nobody, and a slug is a lossy fold, so it
                # differs from almost every real name -- reading it as a second member
                # would blank a member's own state over a value that never was a name.
                logged = svc.logged_name(slug)
                if logged is not None and logged != row["name"] and logged != slug:
                    logger.warning(
                        "member slug %r logs %r, so %r gets no projection; "
                        "rename one member so their slugs differ",
                        slug,
                        logged,
                        row["name"],
                    )
                    out[slug] = {"asOfSeq": -1, "values": {}}
                    continue
                snap = svc.snapshot(slug)
                values = snap.get("values", {}) if isinstance(snap, dict) else {}
                agent_cfg = agent_cfgs.get(row["name"])
                if agent_cfg is not None:
                    eventlog_hooks.reconcile_member_config(
                        slug, row["name"], agent_cfg, values.get("roster", {})
                    )
                    # Re-snapshot only when the reconcile appended (the roster
                    # config fields would otherwise be stale for this response).
                    snap = svc.snapshot(slug)
                out[slug] = snap if isinstance(snap, dict) else {"asOfSeq": -1, "values": {}}
            except Exception:
                logger.debug("member projections failed for %r", slug, exc_info=True)
                out[slug] = {"asOfSeq": -1, "values": {}}
        return out

    projections = await asyncio.to_thread(_project_rows)
    # Same network-boundary redaction as the /history read and the projection
    # WS push: a projection block carries agent-authored free-text (an activity
    # record's `project`, message previews) and `svc.snapshot()` returns it raw,
    # so the credential + exfiltration-URL chain has to run before it crosses to
    # the browser or the roster list leaks what the sibling reads scrub.
    from kiro_crew.eventlog.service import _redact_projection_value

    for row in rows:
        block = projections.get(row["slug"], {"asOfSeq": -1, "values": {}})
        row["projections"] = _redact_projection_value(block)

    return web.json_response({"members": rows})


def _member_thread_slot(cfg, member: str, slug: str) -> tuple[str, str]:
    """Choose the member's deterministic DM key without opening learned memory."""
    from kiro_crew.memory_stores import require_member_memory_store

    store = require_member_memory_store(cfg, member, require_directory=False)
    record = cfg.memory_stores.get(store)
    if record is None or record.memory_version != 2:
        return members_mod.member_slot_key(slug), ""
    return members_mod.member_slot_key(slug, store), store


async def api_member_thread(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/thread — idempotent get-or-create of a DM thread.

    Returns the thread's slot key. Safe to call every time the page opens a
    member: an existing binding and slot are returned as-is; a missing half is
    re-created (the slot key is a pure derivation of the slug, so re-creation
    always converges on the same thread).
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await _deny_app_caller(request, "members.thread")
    if denied is not None:
        return denied
    # An app token is already refused above with the module's existence-hiding
    # 404. This gate covers the other half: a dashboard token with an empty app
    # identity but a non-owner subject (the `!dashboard` Slack case), which
    # would otherwise bind a session slot to a crew member. Kept below the app
    # denial so app callers keep the 404 they get on every other member route.
    owner_denied = await require_owner_dashboard_request(request, "members.thread")
    if owner_denied is not None:
        return owner_denied
    state: DashboardState | None = request.app.get("state")
    if state is None:
        return web.json_response(
            {"error": "dashboard state unavailable", "code": "state_unavailable"}, status=503
        )
    slug = request.match_info["slug"]
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return web.json_response(
            {"error": "invalid member slug", "code": "invalid_member_slug"}, status=400
        )

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    binding = await asyncio.to_thread(members_mod.read_dm_binding, slug)

    # The bound member wins as long as it still exists AND still derives this
    # slug — dm.json's `member` field is operator-editable state, so it is
    # honored only when the registry independently corroborates it (the name
    # exists and folds to the slug being opened). This keeps a colliding
    # slug's thread stably attributed to whoever bound it first. With no
    # binding at all, the first crew in config order whose name derives this
    # slug takes the thread. An uncorroborated binding resolves to that same
    # crew here — but only far enough to look up its slot; the branches below
    # refuse the open rather than rebinding the slug to it.
    slug_owners = _member_names_for_slug(cfg, slug)
    if binding is not None and binding.get("member") in slug_owners:
        member_name = binding["member"]
    elif slug_owners:
        member_name = slug_owners[0]
    else:
        member_name = ""
    slot_key, generation = "", ""
    if member_name:
        try:
            slot_key, generation = await asyncio.to_thread(
                _member_thread_slot, cfg, member_name, slug
            )
        except Exception as exc:
            from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

            return _store_unavailable_response(cfg.agents[member_name].memory_store, exc)
    if binding is not None:
        if binding.get("member") not in slug_owners:
            # The binding names a crew absent from the registry (renamed, or
            # deleted). It still derives this slug — `read_dm_binding`
            # refuses any binding that does not — so falling through to a
            # same-slug successor here would hand it the SAME derived key,
            # and with it the previous crew's entire transcript, rendered
            # under the successor's name with the pin chip vouching for it.
            # The live-slot mismatch check below cannot catch this (after a
            # restart no live slot exists), so the refusal must key off the
            # BINDING itself. Fail closed, leave dm.json untouched
            # (re-entrant), and let the user resolve it in the crew manager.
            # A binding whose slug has no owner left lands here too, and is
            # refused the same way rather than reaching the 404 below.
            try:
                _sel().log_api_access(
                    caller=request.remote or "",
                    operation="member_thread_open",
                    outcome="denied",
                    source="member_pin",
                    resources=f"slug={slug}",
                    error="binding names a crew outside the slug's owners",
                )
            except Exception:  # pragma: no cover - audit must never change the outcome
                logger.debug("SEL audit for member_pin denial failed", exc_info=True)
            return web.json_response(
                {
                    "error": "the thread is bound to a crew the registry no longer names",
                    "code": "member_pin_mismatch",
                },
                status=409,
            )
    else:
        # No binding, but the canonical history key already holds a
        # transcript: rebinding here would hand whoever currently derives the
        # slug the PREVIOUS occupant's entire conversation (ChatPane hydrates
        # from disk history by key). Attribution is lost with the binding —
        # it is not re-derivable when names collide — so fail closed and let
        # the user resolve it (delete the old thread from History, or restore
        # the crew). A member key with NO history binds fresh as usual.
        if member_name and state.conversation_log is not None:
            _log = state.conversation_log
            _history_key = members_mod.member_thread_session_alias(slug, generation)
            # STRUCTURAL existence, not metadata truthiness: get_metadata
            # answers {} for both "never persisted" and "present but
            # malformed/unreadable", and treating the second as the first
            # would rebind the slug and hand the on-disk transcript to the
            # successor the moment its metadata line is corrupt.
            _history_exists = await asyncio.to_thread(_log.has_log, _history_key)
            if _history_exists:
                try:
                    _sel().log_api_access(
                        caller=request.remote or "",
                        operation="member_thread_open",
                        outcome="denied",
                        source="member_pin",
                        resources=f"slug={slug}",
                        error="orphan history: binding gone, transcript survives",
                    )
                except Exception:  # pragma: no cover - audit must never change the outcome
                    logger.debug("SEL audit for member_pin denial failed", exc_info=True)
                return web.json_response(
                    {
                        "error": "this thread's history exists but its binding is gone",
                        "code": "member_binding_missing",
                    },
                    status=409,
                )
    if not member_name:
        return web.json_response(
            {"error": "no crew member for this slug", "code": "member_not_found"}, status=404
        )

    slot = state._slots.get(slot_key)
    if slot is None:
        # A dormant thread (gateway restart outside the restore window, or a
        # thread the user closed) still has its canonical transcript on disk.
        # Minting a bare slot here would reopen the DM with EMPTY in-memory
        # context — the next reply would run without any prior conversation.
        # Rehydrate first: the restore path resolves identity from dm.json
        # (never transcript metadata) and reads off the event loop.
        # adopt_closed: this endpoint IS the deliberate reopen path for a
        # member thread, so a ✕-closed transcript reopens with its history.
        slot = await rehydrate_slot_from_history_async(state, slot_key, adopt_closed=True)
    if slot is None:
        member_workspace = cfg.agents[member_name].workspace
        if member_workspace not in cfg.workspaces:
            member_workspace = cfg.default_workspace
        project = await asyncio.to_thread(default_project_dir, member_workspace)
        # Resolve before publication, then re-check: another opener can create
        # the slot while path validation waits. Its project remains its choice.
        slot = state._slots.get(slot_key)
        if slot is None:
            with state.suspend_slots_push():
                slot = state.get_or_create_slot(
                    name=slot_key,
                    agent=member_name,
                    workspace=member_workspace,
                    mode=members_mod.DM_SLOT_MODE,
                    origin=request_slot_origin(request.get("app", "")),
                )
                slot.project = project
    if slot.mode != members_mod.DM_SLOT_MODE:
        # The derived key is already occupied by a foreign slot (mode is set at
        # creation only, so a pre-existing non-member slot keeps its own). Never
        # adopt it: speaking into it would not be the member's pinned thread.
        return web.json_response(
            {"error": "slot key occupied by a non-member session", "code": "member_slot_conflict"},
            status=409,
        )
    if not slot.agent:
        # A member slot is only ever born with its crew pinned; an empty agent
        # here means the slot predates the binding (e.g. restored from history
        # metadata that lost it). Nothing has run as anyone on it, so adopting
        # the resolved member is a pure repair with no session semantics.
        slot.agent = member_name
    elif slot.agent != member_name:
        # The registry moved under the binding (crew renamed/deleted with a
        # same-slug successor). Re-pinning here would be an agent switch that
        # skips every invariant the real switch endpoint holds (slot lock,
        # workspace/project re-resolution, pending-wait unblocking, metadata
        # persistence, client broadcast) — so FAIL CLOSED instead and leave
        # the binding untouched, keeping this branch re-entrant: the user
        # resolves it in the crew manager (restore the name, or delete the
        # thread), and until then the thread refuses to speak as anyone else.
        try:
            _sel().log_api_access(
                caller=request.remote or "",
                operation="member_thread_open",
                outcome="denied",
                source="member_pin",
                resources=f"slug={slug}",
                error="live slot pinned to a crew the registry no longer names",
            )
        except Exception:  # pragma: no cover - audit must never change the outcome
            logger.debug("SEL audit for member_pin denial failed", exc_info=True)
        return web.json_response(
            {
                "error": "the thread is pinned to a crew the registry no longer names",
                "code": "member_pin_mismatch",
            },
            status=409,
        )

    member_store = getattr(cfg.agents[member_name], "memory_store", "")
    store_record = cfg.memory_stores.get(member_store) if member_store else None
    if store_record is not None and store_record.memory_version == 2:
        from kiro_crew.member_memory_auth import read_private_session_store

        canonical_key = members_mod.member_thread_session_alias(slug, generation)

        async def reopen_bound_thread() -> web.Response:
            """Return a running member thread validated against current ownership.

            Read-only: it writes no assignment and does not touch the turn. Both
            reachable running paths share it, so the opener that finds a turn
            already in flight and the one whose turn starts during its slot-lock
            wait are answered by the same validation rather than by two.
            """
            from kiro_crew.dashboard.chat_persistence import member_store_ownership_holds
            from kiro_crew.dashboard.handlers.agents import _get_config_lock
            from kiro_crew.memory_stores import memory_store_namespace_lock

            # Do not hold the slot lock while waiting for config: member
            # updates already take config before slot.
            try:
                assigned_store = await asyncio.to_thread(read_private_session_store, canonical_key)
            except Exception as exc:
                from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                return _store_unavailable_response(member_store, exc)
            async with _get_config_lock(), slot._lock:

                @memory_store_namespace_lock()
                def current_binding():
                    current = KiroCrewConfig.load()
                    if not member_store_ownership_holds(current, member_name, member_store):
                        return None
                    return members_mod.read_dm_binding(slug)

                try:
                    binding = await asyncio.to_thread(current_binding)
                except Exception as exc:
                    from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                    return _store_unavailable_response(member_store, exc)
                if (
                    state._slots.get(slot_key) is not slot
                    or effective_session_key(slot) != canonical_key
                    or slot.agent != member_name
                    or slot.mode != members_mod.DM_SLOT_MODE
                    or slot.memory_store != member_store
                    or assigned_store != member_store
                    or binding is None
                    or binding.get("slot_key") != slot.key
                    or binding.get("member") != member_name
                ):
                    error = "member thread changed or its private binding is inconsistent"
                    if effective_session_key(slot) != canonical_key:
                        error = "the member thread is linked to another session"
                    elif slot.memory_store != member_store or assigned_store != member_store:
                        error = "the member thread has a different member memory assignment"
                    return web.json_response(
                        {"error": error, "code": "member_slot_conflict"},
                        status=409,
                    )
                return web.json_response(
                    {"slot_key": slot.key, "slug": slug, "member": member_name}
                )

        if slot.running:
            return await reopen_bound_thread()
        running_after_wait = False
        async with slot._lock:
            if effective_session_key(slot) != canonical_key:
                return web.json_response(
                    {
                        "error": "the member thread is linked to another session",
                        "code": "member_slot_conflict",
                    },
                    status=409,
                )
            try:
                if slot.running:
                    # The turn started while this opener waited for the slot.
                    # Its entry snapshot cannot authorize reuse, and taking the
                    # config lock here would invert the config -> slot order,
                    # so re-enter the running path once after this lock is
                    # released: it validates current ownership under config
                    # then slot, which is the answer this window deserves
                    # rather than a conflict the caller has to retry through.
                    running_after_wait = True
                else:
                    # The owner selected this slug, not an editable transcript
                    # or DM binding. A collision needs a protected assignment.
                    slot._memory_assignment_from_history = True
                    if (
                        len(slug_owners) != 1
                        and (await asyncio.to_thread(read_private_session_store, canonical_key))
                        != member_store
                    ):
                        return web.json_response(
                            {
                                "error": "choose distinct member names before opening this private thread",
                                "code": "member_pin_mismatch",
                            },
                            status=409,
                        )
                    assigned_store = await pin_private_agent_store(
                        state, canonical_key, member_name, cfg
                    )
            except Exception as exc:
                from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                return _store_unavailable_response(member_store, exc)
            if not running_after_wait:
                if slot.running:
                    # A turn started while this opener awaited the namespaced
                    # assignment above. ``slot._lock`` does not exclude it:
                    # ``api_chat`` reads ``slot.running`` and publishes
                    # ``slot.task`` without taking the slot lock, so the window
                    # is the await, not the lock. Publishing an assignment onto
                    # a thread whose turn is already in flight is the write this
                    # route exists to avoid, so drop it -- the same treatment
                    # the pre-assignment window above gets -- and answer through
                    # the read-only reopen path instead. The assignment
                    # ``pin_private_agent_store`` already published cannot
                    # repoint that turn: ``bind_session_execution`` compares the
                    # record it read and refuses a differing member or store.
                    running_after_wait = True
                elif (
                    state._slots.get(slot_key) is not slot
                    or effective_session_key(slot) != canonical_key
                    or slot.agent != member_name
                ):
                    return web.json_response(
                        {
                            "error": "member thread changed during assignment",
                            "code": "member_slot_conflict",
                        },
                        status=409,
                    )
                else:
                    slot.memory_store = assigned_store
        if running_after_wait:
            return await reopen_bound_thread()

    created = (
        binding is None
        or binding.get("slot_key") != slot.key
        or binding.get("member") != member_name
    )
    if created:
        try:
            await asyncio.to_thread(
                lambda: members_mod.write_dm_binding(
                    slug, member=member_name, slot_key=slot.key, memory_store=generation
                )
            )
        except OSError:
            logger.warning("failed to persist dm binding for %r", slug, exc_info=True)
            return web.json_response(
                {
                    "error": "could not persist thread binding",
                    "code": "member_binding_write_failed",
                },
                status=500,
            )
        # Record the binding in the member's append-only log. The trust-file
        # write above is the security fence and stays authoritative; this is
        # the durable projection input for the roster's slot_key. Best-effort
        # and off-loop (ensure/append are synchronous file IO); a logging fault
        # never fails a binding the fence already persisted.

        def _emit_binding() -> None:
            from kiro_crew import eventlog_hooks
            from kiro_crew.eventlog.types import MEMBER_BINDING

            eventlog_hooks.emit(slug, member_name, MEMBER_BINDING, {"slot_key": slot.key})

        await asyncio.to_thread(_emit_binding)

    return web.json_response({"slot_key": slot.key, "slug": slug, "member": member_name})


async def api_member_activity(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/activity — a member's recent activity pointers.

    Feeds the detail drawer's "recent activity" timeline and its derived
    counts. Entries come from the member's own append-only pointer log
    (``members.record_activity``), so everything here is REAL recorded
    signal — the drawer omits a stat rather than fabricating one.

    Response entries carry an allowlist of fields only: ``ts`` (epoch
    seconds), ``via`` (how the member was engaged — ``chat`` is a session
    the user opened with it, ``select_crew`` is a routing decision), and
    ``project``. Session keys stay out of the payload: the drawer renders
    what happened, not handles into other sessions.

    ``member`` (query, REQUIRED) is the exact crew name. Slugification is
    lossy — two distinct names can share one slug and therefore one log
    file — and each record carries the exact name precisely so attribution
    stays recoverable. Filtering here (BEFORE the display limit) is what
    keeps a colliding slug's drawer from rendering the other member's
    events; making the parameter required makes the mixed read impossible
    by construction rather than a caller obligation.
    """
    denied = await _deny_app_caller(request, "members.activity")
    if denied is not None:
        return denied
    slug = request.match_info["slug"]
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return web.json_response(
            {"error": "invalid member slug", "code": "invalid_member_slug"}, status=400
        )
    member = request.query.get("member", "")
    if not member or not _AGENT_NAME_RE.match(member):
        return web.json_response(
            {"error": "member query parameter required", "code": "missing_member"}, status=400
        )

    # Source the records from the member's append-only log: ACTIVITY_RECORD
    # events for THIS exact member, unwrapped to the record dict each carries.
    #
    # The member filter and the timestamp parse run INSIDE this read, before any
    # cap, because a log's newest N envelopes are not N of one member's activity
    # records. The same log also carries config, binding, rules, message, slot and
    # patrol events, and a colliding slug's log carries another exact name's
    # records as well -- so capping the envelope read first and filtering second
    # drops activity that is well inside the window the drawer promises.
    #
    # But the read is PAGED rather than asked for the whole log. `history` with
    # `limit=None` materialises every event in the lifetime file into a list and
    # reverses it, which is bounded only while the log still fits the retained tail
    # (`MAX_RETAINED_EVENTS`); past that the allocation is the file's whole length,
    # on a request path, for a response that shows `_ACTIVITY_LIMIT` rows. The log
    # has no rotation, so outgrowing the tail is ordinary ageing rather than an
    # extreme input. Paging keeps the filter where it has to be AND keeps the
    # allocation bounded: walk backwards a page at a time and stop as soon as one
    # more than the display cap has matched, which is all `capped` needs to know.
    def _read_activity_records() -> list[tuple[float, int, dict]]:
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.eventlog.types import ACTIVITY_RECORD

        svc = get_service()
        out: list[tuple[float, int, dict]] = []
        before: int | None = None
        while True:
            page = svc.history(slug, before=before, limit=_ACTIVITY_PAGE)
            if not page:
                break
            for event in page:
                if event.get("type") != ACTIVITY_RECORD:
                    continue
                record = event.get("data")
                if not isinstance(record, dict):
                    continue
                if record.get("member") != member:
                    # A colliding slug's log holds records for another exact name;
                    # they belong to that member's drawer, not this one's.
                    continue
                ts = _parse_activity_ts(record.get("ts", ""))
                if ts <= 0:
                    # A record without a readable timestamp cannot be placed on a
                    # timeline; skip it rather than sorting garbage to the top.
                    continue
                # ``seq`` is the log's own append order, which is what the former
                # read index stood in for -- and it stays the same number whatever
                # slice this read returns, so same-second ties break identically.
                out.append((ts, int(event.get("seq", 0)), record))
            if len(out) > _ACTIVITY_LIMIT:
                # One past the display cap is enough: the sort below can only trim
                # the oldest tail, and `capped` is a boolean, not a total.
                break
            if len(page) < _ACTIVITY_PAGE:
                break  # the log is exhausted
            oldest = page[-1].get("seq")  # pages come newest-first
            if not isinstance(oldest, int) or (before is not None and oldest >= before):
                # No usable cursor, or one that did not move: stop rather than
                # re-read the same page for ever.
                break
            before = oldest
        return out

    entries = await asyncio.to_thread(_read_activity_records)

    def _sanitize(text: str) -> str:
        # Same redaction chain the roster's message preview uses: a project
        # value is an operator-supplied path that can embed a credential or
        # presigned URL, and this response is a network boundary. Run it on
        # the FULL value (nothing here truncates, so order is trivial today,
        # but keeping the shared chain means a future cap cannot split a
        # token past the patterns).
        text, _ = _h.redact_exfiltration_urls(text)
        text, _ = _h.redact_credentials(text)
        return text

    rows: list[tuple[float, int, dict]] = []
    for ts, seq, entry in entries:
        rows.append(
            (
                ts,
                seq,
                {
                    "ts": ts,
                    "via": entry.get("via", "") or "chat",
                    "project": _sanitize(str(entry.get("project", "") or "")),
                },
            )
        )
    # Newest first — the drawer renders top-down and the newest event is the
    # one the user opened the drawer to see. The log's ts is second-precision,
    # so append order (the read index) breaks same-second ties: without it two
    # events in one second would render oldest-first at the top. The display
    # cap applies AFTER the member filter and the sort, so it can only ever
    # trim the oldest tail — never another member's share of a shared log.
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    capped = len(rows) > _ACTIVITY_LIMIT
    return web.json_response(
        {
            "slug": slug,
            "member": member,
            # `capped` tells the drawer its derived counters are floors, not
            # totals, once the window is saturated — it renders "N+" instead
            # of asserting an exact count it cannot know.
            "capped": capped,
            "entries": [r[2] for r in rows[:_ACTIVITY_LIMIT]],
        }
    )


async def api_member_briefing(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/briefing?member=<name> — a crewmate's own notes, read-only.

    Feeds the Crewmates page panel's Notes tab. The briefing
    (``members/<slug>/briefing.md``) is the crewmate's self-maintained standing
    notes — an AGENT-written file, curated by the crewmate for its future self.
    This endpoint reads and never writes, and the panel offers no editor for
    the file: the dashboard's file viewer reads through a redacting path and
    its Save writes the buffer back, so any in-dashboard edit of an
    agent-written file could replace a secret the crewmate wrote in the
    meantime with its placeholder. The notes are edited where the crewmate
    writes them, outside the dashboard.

    The text comes from :func:`members.read_member_briefing_bounded` and
    inherits its total contract: a missing or unreadable file reads as ``""``
    (the normal state of a fresh crewmate — never a 404), and content past
    ``MEMBER_BRIEFING_MAX_CHARS`` is cut at the cap with a visible marker
    (:func:`members.cap_member_briefing`, applied AFTER redaction so the cap
    cannot split a token past the patterns), which the panel renders as-is so
    the human sees the same overflow the crewmate is shown. ``supported`` is :func:`members.member_briefing_supported`:
    on platforms without ``O_NOFOLLOW`` and the pinned ancestor walk the read
    fails closed to ``""`` and the panel explains that from the flag rather
    than presenting an empty briefing as "no notes yet". ``updated_ts`` is the
    file's own mtime (epoch seconds) or ``null`` when there is no file.
    ``redacted`` and ``truncated`` each say the wire text shows less than the
    file holds (a secret replaced by its placeholder; a tail past the cap not
    shown), so the panel can say so above the notes instead of leaving
    placeholders and a marker unexplained. A successful read leaves a SEL row
    (``members.briefing.read`` / ``allowed``), as the rules read does.

    ``member`` (query, REQUIRED) is the exact crew name, same posture as the
    activity endpoint: slugification is lossy, and the exact name is echoed
    back so the frontend keys its cache by name rather than by a slug two
    crewmates can share -- and, as on the rules endpoint, the exact name must
    derive this slug, exist, and be the ONLY crew that derives it: the briefing
    is one file per slug, so for a colliding slug the notes belong to neither
    crewmate and the read is refused (409 ``briefing_slug_ambiguous``) rather
    than shown -- with an Edit -- as one of theirs.
    """
    denied = await _deny_app_caller(request, "members.briefing")
    if denied is not None:
        return denied
    # Owner gate, the rules endpoint's boundary: the briefing is the crewmate's
    # private working memory, written for its owner. Any allowed Slack user can
    # mint a dashboard session (`!dashboard`), so the app-caller guard alone
    # would let a non-owner colleague read notes the owner never shared. Gated
    # before any validation or file IO, so a denial costs no read.
    owner_denied = await require_owner_dashboard_request(request, "members.briefing.read")
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
    if not member or not _AGENT_NAME_RE.match(member):
        return web.json_response(
            {"error": "member query parameter required", "code": "missing_member"}, status=400
        )
    # The briefing is a PER-SLUG file and the slug is lossy (`Code_Reviewer` and
    # `code-reviewer` share one), so for a colliding slug the file belongs to
    # neither crewmate cleanly: showing it as one member's notes -- with an Edit
    # that saves over it -- would let the two overwrite each other. Same posture
    # as the rules endpoint: verify the exact member derives this slug, exists,
    # and is the ONLY one that does; otherwise refuse with a coded answer the
    # panel turns into a plain sentence. Config read off-loop (file IO).
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
    if _member_names_for_slug(cfg, slug) != [member]:
        return web.json_response(
            {
                "error": "multiple crews share this slug; their notes would be ambiguous",
                "code": "briefing_slug_ambiguous",
            },
            status=409,
        )

    supported = members_mod.member_briefing_supported()

    # The pinned, bounded open (text + mtime from one descriptor) is blocking
    # file IO: one hop off the loop.
    text, updated_ts, read_bounded = await asyncio.to_thread(
        members_mod.read_member_briefing_bounded, slug
    )

    # Same redaction chain as the activity endpoint: the briefing is an
    # AGENT-written file, so a token the crewmate pasted into its own notes
    # would otherwise cross this network boundary into the browser verbatim.
    # Run on the whole BOUNDED buffer, BEFORE the character cap: a redaction
    # over already-capped text cannot match a token the cap split in two, and
    # the plaintext half would cross the boundary unmatched. The cap comes
    # after -- judged on the REDACTED length, so a briefing that only
    # overflowed before its placeholders shrank it is shown whole -- and drops
    # a trailing split word for the same reason (the bounded read has an edge
    # of its own).
    text, url_hits = _h.redact_exfiltration_urls(text)
    text, cred_hits = _h.redact_credentials(text)
    text, truncated = members_mod.cap_member_briefing(text, read_bounded, drop_split_tail=True)
    # Whether the text on the wire shows less than the file holds, so the panel
    # can say so above the notes: ``redacted`` when a placeholder replaced a
    # secret, ``truncated`` when the marker stands in for the tail.
    redacted = bool(url_hits or cred_hits)

    # Audit the disclosure, as the rules read does: WHO read a crewmate's
    # private notes matters as much as who was refused, and a denied-only
    # trail cannot answer "was this boundary disclosed". Best effort -- an
    # audit must never change the outcome.
    try:
        _sel().log_api_access(
            caller=request.remote or "",
            operation="members.briefing.read",
            outcome="allowed",
            source="dashboard",
            resources=f"slug={slug}",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for members.briefing.read failed", exc_info=True)

    return web.json_response(
        {
            "slug": slug,
            "member": member,
            "supported": supported,
            "text": text,
            "updated_ts": updated_ts,
            "redacted": redacted,
            "truncated": truncated,
        }
    )


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
    # safety instructions for this member. Any allowed Slack user can mint a
    # dashboard session (`!dashboard`), so without this gate a non-owner
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
    if not member or not _AGENT_NAME_RE.match(member):
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
    if not isinstance(member, str) or not member or not _AGENT_NAME_RE.match(member):
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
    # Same collision scan the roster/thread paths use — the central helper
    # applies the agent-name grammar filter and tolerates MemberSlugError, so
    # a hand-edited config key that is not a valid agent name can neither
    # crash this scan nor manufacture a phantom collision.
    colliding = _member_names_for_slug(cfg, slug)
    if colliding != [member]:
        return web.json_response(
            {
                "error": "multiple crews share this slug; rules would be ambiguous",
                "code": "rules_slug_ambiguous",
            },
            status=409,
        )
    try:
        await asyncio.to_thread(members_mod.write_member_rules, slug, member=member, text=rules)
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
