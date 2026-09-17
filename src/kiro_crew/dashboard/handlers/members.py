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
import functools
import logging
import uuid
from typing import Any

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

    rows: list[dict] = []
    for name, agent_cfg in cfg.agents.items():
        if not _AGENT_NAME_RE.match(name):
            continue
        try:
            slug = members_mod.slug_for_name(name)
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
                "name": name,
                "slug": slug,
                "kiro_agent": agent_cfg.kiro_agent,
                "workspace": agent_cfg.workspace,
                "memory_store": agent_cfg.memory_store,
                "memory_version": version,
                "memory_owner": owner,
                "model": agent_cfg.model,
                # Presentation-only and validated by _safe_avatar at load, so
                # it cannot carry a credential-shaped value. Without it every
                # Members surface silently falls back to the name-derived face.
                "avatar": agent_cfg.avatar,
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
                "description": agent_cfg.description,
                "triggers": agent_cfg.triggers,
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

    return web.json_response({"members": rows})


def _member_thread_slot(cfg, member: str, slug: str) -> tuple[str, str]:
    """Keep protected V2 DMs; otherwise give private memory a fresh generation."""
    from kiro_crew.member_memory_auth import read_private_session_store
    from kiro_crew.memory_stores import require_member_memory_store

    store = require_member_memory_store(cfg, member)
    record = cfg.memory_stores.get(store)
    if record is None or record.memory_version != 2:
        return members_mod.member_slot_key(slug), ""
    legacy_key = members_mod.member_thread_session_alias(slug)
    if read_private_session_store(legacy_key) == store:
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
                    # Opening a live DM must not assign or repair its identity.
                    # Reuse only the already protected store, including while
                    # the turn waits for approval. The common recheck below
                    # rejects a slot changed during this off-loop read.
                    protected_store = await asyncio.to_thread(
                        read_private_session_store, canonical_key
                    )
                    if protected_store != member_store or slot.memory_store != member_store:
                        return web.json_response(
                            {
                                "error": "the member thread has a different private-memory assignment",
                                "code": "member_slot_conflict",
                            },
                            status=409,
                        )
                    assigned_store = member_store
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
            if (
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
            slot.memory_store = assigned_store

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
    try:
        if members_mod.slug_for_name(member) != slug:
            return web.json_response(
                {"error": "member does not match slug", "code": "member_slug_mismatch"}, status=400
            )
    except MemberSlugError:
        return web.json_response(
            {"error": "member does not match slug", "code": "member_slug_mismatch"}, status=400
        )
    # Config load does filesystem reads + validation — off-loop, like every
    # other handler's config access on a request path.
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
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


# ── Perpetual mode ──────────────────────────────────────────────────────────

#: Wake interval a member starts on when the owner switches Perpetual mode on
#: with no loop of its own yet. The member adjusts it from inside its wakes
#: (``monitor_update`` interval); the owner never sets it.
PERPETUAL_DEFAULT_IDLE_SECS = 3600

#: The recurring instruction an owner-armed perpetual loop carries. The member
#: receives it on every wake; it is the standing brief, not a task.
PERPETUAL_INSTRUCTION = (
    "Perpetual mode wake. Review your standing goals, your inbox and the work "
    "you own; act on whatever is due; leave routine progress in your ledger "
    "and message the user only for a decision they alone can make. If the "
    "cadence is wrong, change the interval with monitor_update. End your turn "
    "when nothing is due. Stop this loop yourself only in the rare case the "
    "standing duty is truly over, and say why in the stop reason; otherwise "
    "the user turns Perpetual mode off from the Crew Members page."
)

PERPETUAL_BANNER = "Perpetual mode"


def _perpetual_error(message: str, code: str, status: int) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


def _restore_arm_party(loop_id: str, slot_key: str, party: str, token: str) -> bool:
    """Put a loop's trust entry back after a failed owner takeover.

    A thin, blocking wrapper over ``restore_arm_party_if_token``, which does
    the compare-and-restore as ONE locked read-modify-write in the trust
    module: the entry is rewritten to ``party`` (``"self"`` re-recorded, ``""``
    removed) only while it still carries THIS takeover's ``token`` -- "still
    says owner" is not an identity, since the next takeover writes owner too,
    and an earlier takeover's late cleanup must never undo a later one's
    entry. ``"owner"`` as the prior party is a no-op by construction. Returns
    whether it restored. Best-effort like every revoke: the loop itself was
    left unchanged, so the worst outcome of a failed restore is a stopped loop
    whose entry names the owner -- which only the owner's switch can resume
    anyway. Blocking file IO: callers offload.
    """
    from kiro_crew.autonudge_selfarm import restore_arm_party_if_token

    try:
        return restore_arm_party_if_token(loop_id, slot_key, token, party)
    except (OSError, ValueError):
        logger.warning("could not restore trust entry for loop %s", loop_id, exc_info=True)
        return False


async def api_member_perpetual_set(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/perpetual — the owner's Perpetual mode switch.

    Body: ``{"member": <exact crew name>, "enabled": true|false}``.

    The switch drives the auto-nudge loop on the member's OWN DM thread. The
    slot key is derived server-side from the slug's binding, never taken from
    the body, so the route can only ever touch that one thread. Owner-only by
    construction (app tokens 404, non-owner dashboard subjects are refused by
    ``require_owner_dashboard_request``): arming a member from outside its own
    turn is exactly what ``autonudge_authz`` refuses everyone else, and the
    ``owner_arm`` admission it grants this route is recorded under its own
    ``armed_by`` in the keystone-gated trust record.

    ON with no loop: arm a NEW loop with ``max_cycles=0`` and
    ``max_runtime_secs=0`` -- unlimited, which is the point of the mode and
    the owner's deliberate choice here. Finite loops armed anywhere else keep
    their own caps; nothing here converts them.
    ON with a stopped loop: resume THAT loop and lift its caps to unlimited,
    keeping its cycle accounting and its instruction.
    ON with an active loop: nothing to do; the current record is returned.
    OFF: deactivate the loop (``active=False``). The record stays, with its
    ``manual`` stop reason, so the drawer keeps saying why; the pending wake is
    cancelled by the service and no queued wake can revive it (the timer and
    ``notify_turn_complete`` both re-read ``active``). A turn already running
    is not interrupted. A structured monitor (``monitor_watch``) is not this
    switch's to convert: 409.
    """
    from kiro_crew.autonudge import get_instance as _autonudge_get

    denied = await _deny_app_caller(request, "members.perpetual")
    if denied is not None:
        return denied
    owner_denied = await require_owner_dashboard_request(request, "members.perpetual")
    if owner_denied is not None:
        return owner_denied
    slug = request.match_info["slug"]
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return _perpetual_error("invalid member slug", "invalid_member_slug", 400)
    try:
        body = await request.json()
    except Exception:
        return _perpetual_error("invalid JSON body", "invalid_json", 400)
    if not isinstance(body, dict):
        return _perpetual_error("invalid JSON body", "invalid_json", 400)
    member = body.get("member", "")
    enabled = body.get("enabled")
    if not isinstance(member, str) or not member or not _AGENT_NAME_RE.match(member):
        return _perpetual_error("member field required", "missing_member", 400)
    # A real boolean only: bool("false") is True, and this switch runs tools
    # unattended when it is on.
    if not isinstance(enabled, bool):
        return _perpetual_error("enabled must be a boolean", "not_a_boolean", 400)
    try:
        if members_mod.slug_for_name(member) != slug:
            return _perpetual_error("member does not match slug", "member_slug_mismatch", 400)
    except MemberSlugError:
        return _perpetual_error("member does not match slug", "member_slug_mismatch", 400)
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    if member not in cfg.agents:
        return _perpetual_error("no crew member for this slug", "member_not_found", 404)
    svc = _autonudge_get()
    if svc is None:
        return _perpetual_error(
            "auto-nudge disabled (KIROCREW_AUTONUDGE not set)", "autonudge_disabled", 503
        )
    state: DashboardState | None = request.app.get("state")
    if state is None:
        return _perpetual_error("dashboard state unavailable", "state_unavailable", 503)
    # OWNERSHIP, the thread route's own checks in the thread route's own order
    # (``api_member_thread``), resolved by ``_resolve_owned_member_slot``: the
    # binding must exist and name THIS member, the bound slot key must be the
    # CURRENT derivation for this member, and the live slot must be a
    # member-mode slot pinned to this crew. Run once here to answer a stale
    # page fast and to pick the lock, and run AGAIN under the lock, where the
    # mutation reads it.
    slot_key, denied = await _resolve_owned_member_slot(state, cfg, slug, member)
    if denied is not None:
        return denied
    caller = request.remote or ""
    # The mutation runs as ONE SUPERVISED TASK the request only waits on: a
    # cancelled request (client gone, aiohttp handler cancellation) must never
    # interrupt the takeover's steps mid-flight -- a trust entry rewritten with
    # no loop resumed, or a loop resumed under an entry already put back. The
    # task acquires and releases the slot lock itself, so the lock is held
    # until every step INCLUDING any rollback has finished. A cancelled request
    # gets no response; the task still completes and the store is consistent.
    if request.app.get(_PERPETUAL_SHUTDOWN_KEY):
        return _perpetual_error("gateway is shutting down", "shutting_down", 503)
    task = asyncio.ensure_future(
        _perpetual_mutation(
            state=state,
            cfg=cfg,
            svc=svc,
            slug=slug,
            member=member,
            slot_key=slot_key,
            enabled=enabled,
            caller=caller,
        )
    )
    tasks = _perpetual_tasks_of(request.app)
    tasks.add(task)
    task.add_done_callback(functools.partial(_perpetual_task_done, tasks=tasks, slot_key=slot_key))
    return await asyncio.shield(task)


async def _resolve_owned_member_slot(
    state: DashboardState, cfg: KiroCrewConfig, slug: str, member: str
) -> tuple[str, web.Response | None]:
    """The member's own DM slot key, or the refusal that stands in for it.

    Reuses the thread route's helpers rather than re-deriving anything: the
    binding (``read_dm_binding``) must name *member* and *member* must derive
    *slug* (``_member_names_for_slug``) -- else 409 ``member_pin_mismatch``,
    the pin refusal the thread route gives a binding naming another crew; the
    bound key must equal ``_member_thread_slot``'s CURRENT derivation and name a
    live member-mode slot -- else 409 ``member_thread_not_open``; and the live
    slot must be pinned to *member* -- else 409 ``member_slot_conflict``. The
    slot key is never taken from a request body.
    """
    binding = await asyncio.to_thread(members_mod.read_dm_binding, slug)
    if binding is None:
        return "", _perpetual_error(
            "open this member's thread first", "member_thread_not_open", 409
        )
    if binding.get("member") != member or member not in _member_names_for_slug(cfg, slug):
        return "", _perpetual_error(
            "the thread is bound to a different crew", "member_pin_mismatch", 409
        )
    try:
        expected_slot_key, _generation = await asyncio.to_thread(
            _member_thread_slot, cfg, member, slug
        )
    except Exception as exc:
        from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

        return "", _store_unavailable_response(cfg.agents[member].memory_store, exc)
    slot_key = str(binding.get("slot_key", ""))
    slot = state._slots.get(slot_key) if slot_key else None
    if slot_key != expected_slot_key or slot is None or str(getattr(slot, "mode", "")) != "member":
        return "", _perpetual_error(
            "open this member's thread first", "member_thread_not_open", 409
        )
    if str(getattr(slot, "agent", "")) != member:
        return "", _perpetual_error(
            "the thread is pinned to a different crew", "member_slot_conflict", 409
        )
    return slot_key, None


async def _perpetual_mutation(
    *,
    state: DashboardState,
    cfg: KiroCrewConfig,
    svc: Any,
    slug: str,
    member: str,
    slot_key: str,
    enabled: bool,
    caller: str,
) -> web.Response:
    """The switch's effect, under the slot lock, start to finish.

    Re-runs the ownership resolution under the lock and aborts (409
    ``member_slot_conflict``) unless it still answers the key the lock was
    taken for -- the binding or the live slot moved while this request waited.
    Then re-reads the loop under the lock: two presses on the same switch, or a
    press racing the member's own re-arm, must see each other's result.
    """
    from kiro_crew.autonudge import is_structured_monitor_loop
    from kiro_crew.autonudge_authz import authorize_and_add_nudge, authorize_and_update_nudge
    from kiro_crew.dashboard.handlers.autonudge import _serialize

    async with _perpetual_lock(slot_key):
        current_key, denied = await _resolve_owned_member_slot(state, cfg, slug, member)
        if denied is not None:
            return denied
        if current_key != slot_key:
            return _perpetual_error(
                "member thread changed during the request", "member_slot_conflict", 409
            )
        existing = svc.get_by_slot(slot_key)
        if existing is not None and is_structured_monitor_loop(existing):
            return _perpetual_error(
                "this member is running a structured monitor; stop it from its own thread",
                "structured_monitor_not_convertible",
                409,
            )
        if not enabled:
            if existing is None:
                return web.json_response({"ok": True, "loop": None})
            if not existing.active:
                return web.json_response({"ok": True, "loop": _serialize(existing)})
            loop, error, status = await authorize_and_update_nudge(
                svc=svc,
                loop_id=existing.id,
                active=False,
                source="dashboard",
                caller=caller,
            )
            if error is not None:
                return _perpetual_error(error, "perpetual_off_failed", status)
            return web.json_response({"ok": True, "loop": _serialize(loop)})
        if existing is not None:
            if existing.active:
                return web.json_response({"ok": True, "loop": _serialize(existing)})
            loop, error, status = await _takeover_stopped_loop(
                svc, existing, slot_key, caller=caller
            )
            if error is not None:
                return _perpetual_error(error, "perpetual_on_failed", status)
            return web.json_response({"ok": True, "loop": _serialize(loop)})
        loop, error, status = await authorize_and_add_nudge(
            svc=svc,
            state=state,
            slot_key=slot_key,
            message=PERPETUAL_INSTRUCTION,
            idle_secs=PERPETUAL_DEFAULT_IDLE_SECS,
            max_cycles=0,
            max_runtime_secs=0,
            banner=PERPETUAL_BANNER,
            source="dashboard",
            caller=caller,
            gate=False,
            replace_existing=False,
            owner_arm=True,
        )
        if error is not None:
            return _perpetual_error(error, "perpetual_on_failed", status)
        return web.json_response({"ok": True, "loop": _serialize(loop)})


#: PER-APP set of supervised mutation tasks in flight (``app[_PERPETUAL_TASKS_KEY]``,
#: created by ``register_perpetual_lifecycle``), held so a request that stops
#: waiting on one cannot let it be garbage-collected mid-step, and so one
#: app's cleanup drains exactly its own tasks. Every task's outcome is read by
#: ``_perpetual_task_done`` -- a request that stopped awaiting must not leave a
#: failure unlogged -- and the set is drained at gateway shutdown by
#: ``_perpetual_drain`` (registered through ``register_perpetual_lifecycle``,
#: the same ``on_shutdown`` / ``on_cleanup`` pair every other background
#: subsystem in ``server.py`` uses). An app that never registered the lifecycle
#: (a bare test router) gets a set minted on first use.
_PERPETUAL_TASKS_KEY = "members.perpetual.tasks"

#: Per-app flag set by the ``on_shutdown`` hook: once true the route admits no
#: new mutation (503 ``shutting_down``) so the drain below sees a closed set.
_PERPETUAL_SHUTDOWN_KEY = "members.perpetual.shutting_down"


def _perpetual_tasks_of(app: web.Application) -> set["asyncio.Task[Any]"]:
    tasks = app.get(_PERPETUAL_TASKS_KEY)
    if tasks is None:
        tasks = app[_PERPETUAL_TASKS_KEY] = set()
    return tasks


#: Bounded drain at cleanup: outstanding mutations get this long to finish on
#: their own (a takeover is three short steps), then are cancelled and joined
#: for at most this long again. A task cancelled here leaves its trust entry
#: as written (the takeover's own cancellation rule) and is logged at warning.
_PERPETUAL_DRAIN_GRACE_SECS = 5.0


def _perpetual_task_done(
    task: "asyncio.Task[Any]", *, tasks: set["asyncio.Task[Any]"], slot_key: str
) -> None:
    """Retrieve every supervised task's outcome so none is swallowed.

    The error line carries a fixed template -- exception TYPE and slot key --
    and never the exception's message (which can echo request text or a stop
    reason); the full chain goes to debug. A cancelled task is the shutdown
    case: its takeover may have left the trust entry saying owner, which the
    log states.
    """
    tasks.discard(task)
    if task.cancelled():
        logger.warning(
            "perpetual mutation for slot %s was cancelled; a takeover in flight may "
            "have left its trust entry saying owner (resumable by the owner's switch)",
            slot_key,
        )
        return
    exc = task.exception()
    if exc is not None:
        logger.error("perpetual mutation for slot %s failed: %s", slot_key, type(exc).__name__)
        logger.debug("perpetual mutation failure detail for slot %s", slot_key, exc_info=exc)


def register_perpetual_lifecycle(app: web.Application) -> None:
    """Hook the switch's supervised tasks into the app's shutdown sequence."""
    app[_PERPETUAL_TASKS_KEY] = set()
    app.on_shutdown.append(_perpetual_stop_admitting)
    app.on_cleanup.append(_perpetual_drain)


async def _perpetual_stop_admitting(app: web.Application) -> None:
    app[_PERPETUAL_SHUTDOWN_KEY] = True


async def _perpetual_drain(app: web.Application) -> None:
    """Join this app's supervised mutations, bounded, then the service's own in-flight writes.

    Phase one: the app's tasks get ``_PERPETUAL_DRAIN_GRACE_SECS`` to finish on
    their own, then are cancelled and joined for as long again. Every blocking
    trust write a task issues (owner record -- in the takeover and in the
    authorizer's no-loop arm -- and the restore) is awaited through
    ``autonudge_selfarm.await_thread_to_completion``, so joining the task IS
    joining its threads: a cancel arriving mid-write, or a second one during
    the rejoin, finishes the thread before the task ends. The join itself is
    bounded: a task still running when the second wait expires is given up on
    and logged, and its thread then finishes on its own. Phase two is a
    BOUNDED, BEST-EFFORT wait as well: the nudge service
    runs its own add/update persists as SHIELDED internal tasks it retains in
    ``_inflight_adds``; a takeover cancelled mid-update leaves one of those
    running, so the drain waits on that set for the same bound -- never
    cancelling them, they are the service's -- and logs at info how many were
    still writing when it gave up. Nothing takes them over after that:
    ``AutoNudgeService.stop`` cancels timers and does not join in-flight
    persists, so a write still running when the drain returns finishes on
    its own or not at all.
    """
    tasks = [t for t in _perpetual_tasks_of(app) if not t.done()]
    if tasks:
        _done, pending = await asyncio.wait(tasks, timeout=_PERPETUAL_DRAIN_GRACE_SECS)
        if pending:
            logger.warning(
                "cancelling %d perpetual mutation task(s) still running at shutdown", len(pending)
            )
            for task in pending:
                task.cancel()
            _done, still = await asyncio.wait(pending, timeout=_PERPETUAL_DRAIN_GRACE_SECS)
            if still:
                logger.warning(
                    "%d perpetual mutation task(s) did not stop within the drain", len(still)
                )
    from kiro_crew.autonudge import get_instance as _autonudge_get

    svc = _autonudge_get()
    inflight = [t for t in list(getattr(svc, "_inflight_adds", None) or ()) if not t.done()]
    if inflight:
        _done, still_writing = await asyncio.wait(inflight, timeout=_PERPETUAL_DRAIN_GRACE_SECS)
        if still_writing:
            logger.info(
                "%d nudge-service write(s) were still in flight when the perpetual drain "
                "gave up; nothing joins them after this (AutoNudgeService.stop does not)",
                len(still_writing),
            )


#: One lock per member slot for THIS ROUTE's mutations, and only this route's:
#: it serializes concurrent presses of the switch on one member (and the
#: identity re-check + loop re-read + takeover steps of each) against each
#: other. The member's own directives (``monitor_update`` / ``autonudge_stop``)
#: and the fire path go through the nudge service's own locks, not this one;
#: the takeover therefore never assumes the loop it read is the loop that is
#: still there, and its rollback targets exactly the loop id and the takeover
#: token it captured at its start. Process-local like the service. Entries are
#: dropped once no request holds or waits on them (``_perpetual_lock_users``),
#: so the map is bounded by the presses in flight, not by the members ever
#: pressed.
_PERPETUAL_LOCKS: dict[str, asyncio.Lock] = {}
_PERPETUAL_LOCK_USERS: dict[str, int] = {}


class _perpetual_lock:
    """``async with _perpetual_lock(slot_key):`` -- the per-slot lock, refcounted."""

    def __init__(self, slot_key: str) -> None:
        self._key = slot_key
        self._lock: asyncio.Lock | None = None

    async def __aenter__(self) -> None:
        lock = _PERPETUAL_LOCKS.get(self._key)
        if lock is None:
            lock = _PERPETUAL_LOCKS[self._key] = asyncio.Lock()
        _PERPETUAL_LOCK_USERS[self._key] = _PERPETUAL_LOCK_USERS.get(self._key, 0) + 1
        self._lock = lock
        try:
            await lock.acquire()
        except BaseException:
            self._release_use()
            raise

    async def __aexit__(self, *_exc: object) -> None:
        assert self._lock is not None
        self._lock.release()
        self._release_use()

    def _release_use(self) -> None:
        left = _PERPETUAL_LOCK_USERS.get(self._key, 1) - 1
        if left <= 0:
            _PERPETUAL_LOCK_USERS.pop(self._key, None)
            _PERPETUAL_LOCKS.pop(self._key, None)
        else:
            _PERPETUAL_LOCK_USERS[self._key] = left


async def _takeover_stopped_loop(
    svc: Any, existing: Any, slot_key: str, *, caller: str
) -> tuple[Any | None, str | None, int]:
    """OWNER TAKEOVER of a stopped member loop, as one transaction.

    Runs inside the supervised mutation task, under :func:`_perpetual_lock`
    for *slot_key*; the caller has established that *existing* is the slot's
    loop, inactive, and not a structured monitor. Because the task is
    shielded from the request, no step here is interrupted by a client going
    away; the sequence runs to its end and decides on what it saw.

    Steps: read the entry's current party with the STRICT reader (an
    unreadable or malformed record is indeterminate and refuses the takeover);
    rewrite the entry to ``owner`` stamped with THIS takeover's token (skipped
    when it already says owner: nothing changed, nothing to restore); resume
    the loop with its caps lifted and its cycle accounting kept; decide on the
    update's FINAL RESULT -- the loop the awaited service call returned, never
    the in-memory record, since the service rolls its fields back when the
    persist fails and only the returned value reflects what was written.

    Rollback: on a refused resume or a raised one (the service has rolled its
    own state back by then), the entry is put back to the prior party -- but
    only if it still carries this takeover's token, so a later takeover's
    entry is never overwritten by an earlier one's cleanup. A CANCELLATION of
    the task itself (gateway shutdown) is the one case where the outcome is
    unknown -- the service shields its persist and may still commit -- so the
    entry is left as written and the cancellation propagates: a stopped loop
    under an owner entry is resumable only by the owner's switch, which is the
    safe side. One entry per loop, one party per entry: see
    ``record_owner_arm``.
    """
    from kiro_crew.autonudge_authz import authorize_and_update_nudge
    from kiro_crew.autonudge_selfarm import (
        ARMED_BY_OWNER,
        await_thread_to_completion,
        read_arm_party_strict,
        record_owner_arm,
    )

    loop_id = str(existing.id)
    try:
        previous_party = await asyncio.to_thread(read_arm_party_strict, loop_id, slot_key)
    except OSError:
        logger.error("trust record unreadable; loop not resumed", exc_info=True)
        return None, "trust record unreadable — loop not resumed", 503
    token = ""
    if previous_party != ARMED_BY_OWNER:
        token = uuid.uuid4().hex
        try:
            await await_thread_to_completion(record_owner_arm, loop_id, slot_key, txn=token)
        except asyncio.CancelledError:
            logger.warning(
                "cancelled while writing the owner entry for %s on %s; the write was "
                "joined and the entry may say owner",
                loop_id,
                slot_key,
            )
            raise
        except OSError:
            logger.error("owner-arm record unavailable; loop not resumed", exc_info=True)
            return None, "owner-arm record unavailable — loop not resumed", 503

    async def _rollback() -> None:
        # Awaited INSIDE the slot lock (the caller's ``async with``), so the lock
        # is released only after the restore has run -- and a cancellation here
        # (a second one too) still JOINS the restore thread before propagating
        # (``await_thread_to_completion``): no detached worker outlives the
        # lock or the shutdown drain.
        if not token:
            return
        try:
            await await_thread_to_completion(
                _restore_arm_party, loop_id, slot_key, previous_party, token
            )
        except asyncio.CancelledError:
            logger.warning(
                "cancelled while restoring the trust entry for %s on %s; the restore was "
                "joined, the entry may still say owner",
                loop_id,
                slot_key,
            )
            raise
        except Exception:  # noqa: BLE001 - best-effort; the loop is unchanged
            logger.warning("trust entry restore did not complete for %s", loop_id, exc_info=True)

    try:
        loop, error, status = await authorize_and_update_nudge(
            svc=svc,
            loop_id=loop_id,
            active=True,
            max_cycles=0,
            max_runtime_secs=0,
            source="dashboard",
            caller=caller,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        await _rollback()
        raise
    if error is not None or loop is None or not bool(getattr(loop, "active", False)):
        await _rollback()
        return None, error or "loop did not resume", status if error is not None else 409
    return loop, None, 200
