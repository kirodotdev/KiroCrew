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

from aiohttp import web

import kiro_crew.dashboard.handlers as _h
from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async
from kiro_crew.dashboard.state import DashboardState, request_slot_origin
from kiro_crew.members import MemberSlugError
from kiro_crew.validation import _AGENT_NAME_RE

logger = logging.getLogger(__name__)

#: Activity entries returned to the drawer. Bounds the payload and the JSONL
#: scan alike; the log itself rotates at ~256KiB so this is a display cap,
#: not a durability boundary.
_ACTIVITY_LIMIT = 50


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


def _deny_app_caller(request: web.Request, operation: str) -> web.Response | None:
    """404 for app-token callers; ``None`` for the dashboard user.

    404 rather than 403, matching the slot-access denials: a distinct status
    would confirm the surface exists to a caller that may not know about it.
    """
    request_app = request.get("app", "")
    if not request_app:
        return None
    _sel().log_api_access(
        caller=request_app,
        operation=operation,
        outcome="denied",
        source="app_isolation",
        error="apps cannot access member threads",
    )
    return web.json_response({"error": "not found", "code": "not_found"}, status=404)


def _member_names_for_slug(cfg: KiroCrewConfig, slug: str) -> list[str]:
    """Crew names whose derived slug equals *slug*, in config order.

    Config order is insertion order, so "first name wins" is deterministic for
    a colliding slug. Names failing the agent-name grammar are skipped rather
    than matched: they cannot have been created through the validated CRUD
    surface, so a hand-edited config row never becomes addressable here.
    """
    out: list[str] = []
    for name in cfg.agents:
        if not _AGENT_NAME_RE.match(name):
            continue
        try:
            if members_mod.slug_for_name(name) == slug:
                out.append(name)
        except MemberSlugError:
            continue
    return out


async def api_members(request: web.Request) -> web.Response:
    """GET /api/members — crew roster with DM binding and cheap live status.

    One row per GLOBAL crew (project-scoped crews are out of V1's scope: the
    per-member space is keyed off the global registry). Status fields are
    limited to what costs no IO and no redaction pass — ``running`` is an O(1)
    property read; everything richer (last message, waiting states) rides the
    already-subscribed WS ``slots`` frames on the frontend, so this endpoint
    only fills the cold-start gap.
    """
    denied = _deny_app_caller(request, "members.list")
    if denied is not None:
        return denied
    state: DashboardState | None = request.app.get("state")
    cfg = await asyncio.to_thread(KiroCrewConfig.load)

    rows: list[dict] = []
    for name, agent_cfg in cfg.agents.items():
        if not _AGENT_NAME_RE.match(name):
            continue
        try:
            slug = members_mod.slug_for_name(name)
        except MemberSlugError:
            continue
        rows.append(
            {
                # Explicit allowlist — never a dataclass spread. The response
                # is a network-boundary contract: spreading `AgentConfig`
                # would ship every future field (including a credential-shaped
                # one) to the roster endpoint automatically. These are
                # exactly what the detail drawer renders.
                "name": name,
                "slug": slug,
                "kiro_agent": agent_cfg.kiro_agent,
                "workspace": agent_cfg.workspace,
                "memory_store": agent_cfg.memory_store,
                "model": agent_cfg.model,
                # Presentation-only and validated by _safe_avatar at load, so
                # it cannot carry a credential-shaped value. Without it every
                # Members surface silently falls back to the name-derived face.
                "avatar": agent_cfg.avatar,
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
    def _read_transcript_tails() -> dict[str, tuple[float, str]]:
        if state is None or state.conversation_log is None:
            return {}

        def _sanitize(text: str) -> str:
            # Same redaction chain the sessions list uses, injected so it
            # runs BEFORE the preview's length cap — a credential split by
            # truncation leaves a partial token the patterns cannot match.
            text, _ = _h.redact_exfiltration_urls(text)
            text, _ = _h.redact_credentials(text)
            return text

        out: dict[str, tuple[float, str]] = {}
        for row in rows:
            if not row["slot_key"]:
                continue
            log_key = f"dashboard:{row['slot_key']}"
            mt = state.conversation_log.session_mtime(log_key)
            if not mt:
                continue
            preview, msg_ts = state.conversation_log.last_message_info(log_key, sanitize=_sanitize)
            # Order by the newest MESSAGE, not the file: metadata writes and
            # rehydration bump the mtime without any new message, which made
            # rows reorder with no visible cause. mtime remains only as the
            # fallback for pre-timestamp transcript rows.
            out[row["slot_key"]] = (msg_ts or mt, preview)
        return out

    tails = await asyncio.to_thread(_read_transcript_tails)
    for row in rows:
        mt, preview = tails.get(row["slot_key"], (0.0, ""))
        row["last_active_ts"] = mt
        row["last_message"] = preview

    return web.json_response({"members": rows})


async def api_member_thread(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/thread — idempotent get-or-create of a DM thread.

    Returns the thread's slot key. Safe to call every time the page opens a
    member: an existing binding and slot are returned as-is; a missing half is
    re-created (the slot key is a pure derivation of the slug, so re-creation
    always converges on the same thread).
    """
    denied = _deny_app_caller(request, "members.thread")
    if denied is not None:
        return denied
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
    # exists and folds to the slug being opened). Otherwise fall back to the
    # first crew (config order) whose name derives this slug. This keeps a
    # colliding slug's thread stably attributed to whoever bound it first.
    slug_owners = _member_names_for_slug(cfg, slug)
    member_name = ""
    if binding is not None:
        if binding.get("member") in slug_owners:
            member_name = binding["member"]
        else:
            # The binding names a crew that no longer derives this slug
            # (renamed, or deleted with a same-slug successor). Falling
            # through to the successor here would hand it the SAME derived
            # key — and with it the previous crew's entire transcript,
            # rendered under the successor's name with the pin chip vouching
            # for it. The live-slot mismatch check below cannot catch this
            # (after a restart no live slot exists), so the refusal must
            # key off the BINDING itself. Fail closed, leave dm.json
            # untouched (re-entrant), and let the user resolve it in the
            # crew manager.
            _sel().log_api_access(
                caller=request.remote or "",
                operation="member_thread_open",
                outcome="denied",
                source="member_pin",
                resources=f"slug={slug}",
                error="binding names a crew outside the slug's owners",
            )
            return web.json_response(
                {
                    "error": "the thread is bound to a crew the registry no longer names",
                    "code": "member_pin_mismatch",
                },
                status=409,
            )
    else:
        member_name = slug_owners[0] if slug_owners else ""
        # No binding, but the canonical history key already holds a
        # transcript: rebinding here would hand whoever currently derives the
        # slug the PREVIOUS occupant's entire conversation (ChatPane hydrates
        # from disk history by key). Attribution is lost with the binding —
        # it is not re-derivable when names collide — so fail closed and let
        # the user resolve it (delete the old thread from History, or restore
        # the crew). A member key with NO history binds fresh as usual.
        if member_name and state.conversation_log is not None:
            _log = state.conversation_log
            _history_key = f"dashboard:{members_mod.member_slot_key(slug)}"
            # STRUCTURAL existence, not metadata truthiness: get_metadata
            # answers {} for both "never persisted" and "present but
            # malformed/unreadable", and treating the second as the first
            # would rebind the slug and hand the on-disk transcript to the
            # successor the moment its metadata line is corrupt.
            _history_exists = await asyncio.to_thread(_log.has_log, _history_key)
            if _history_exists:
                _sel().log_api_access(
                    caller=request.remote or "",
                    operation="member_thread_open",
                    outcome="denied",
                    source="member_pin",
                    resources=f"slug={slug}",
                    error="orphan history: binding gone, transcript survives",
                )
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

    slot_key = members_mod.member_slot_key(slug)
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
        # No usable history — a genuinely fresh thread.
        slot = state.get_or_create_slot(
            name=slot_key,
            agent=member_name,
            mode=members_mod.DM_SLOT_MODE,
            origin=request_slot_origin(request.get("app", "")),
        )
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
        _sel().log_api_access(
            caller=request.remote or "",
            operation="member_thread_open",
            outcome="denied",
            source="member_pin",
            resources=f"slug={slug}",
            error="live slot pinned to a crew the registry no longer names",
        )
        return web.json_response(
            {
                "error": "the thread is pinned to a crew the registry no longer names",
                "code": "member_pin_mismatch",
            },
            status=409,
        )

    created = (
        binding is None
        or binding.get("slot_key") != slot.key
        or binding.get("member") != member_name
    )
    if created:
        try:
            await asyncio.to_thread(
                lambda: members_mod.write_dm_binding(slug, member=member_name, slot_key=slot.key)
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
    denied = _deny_app_caller(request, "members.activity")
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

    entries = await asyncio.to_thread(members_mod.read_activity, slug)

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
    for idx, entry in enumerate(entries):
        if entry.get("member") != member:
            # A colliding slug's log holds records for another exact name;
            # they belong to that member's drawer, not this one's.
            continue
        ts = _parse_activity_ts(entry.get("ts", ""))
        if ts <= 0:
            # A record without a readable timestamp cannot be placed on a
            # timeline; skip it rather than sorting garbage to the top.
            continue
        rows.append(
            (
                ts,
                idx,
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
