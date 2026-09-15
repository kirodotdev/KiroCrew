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
import json
import logging
from collections.abc import Callable, Collection
from datetime import datetime, timezone

from aiohttp import web

import kiro_crew.dashboard.handlers as _h
import kiro_crew.dashboard.handlers.agents as _agents_handlers
from kiro_crew import agent_state, member_templates
from kiro_crew import members as members_mod
from kiro_crew.agent import agents_spec_lock
from kiro_crew.apps.manager import app_lifecycle_lock
from kiro_crew.config.loader import (
    KiroCrewConfig,
    default_project_dir,
    update_config_locked,
)
from kiro_crew.dashboard.chat_persistence import (
    pin_private_agent_store,
    rehydrate_slot_from_history_async,
)
from kiro_crew.dashboard.chat_utils import drained, effective_session_key
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.discover import _redact_external
from kiro_crew.dashboard.state import DashboardState, request_slot_origin
from kiro_crew.member_identity import effective_display_name
from kiro_crew.members import MemberSlugError
from kiro_crew.memory_stores import UnknownMemoryStore
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


def enrolled_member_ids(cfg: KiroCrewConfig, *, strict: bool = False) -> list[str]:
    """The ids on the roster: ``config.agents`` rows with a crewmate ENROLLMENT
    record whose generation is the row's private store, in config order.

    Membership is decided by the record alone (``agent_state.all_crewmate_records``,
    written by a confirmed hire inside its locked publication, removed by the
    fire) -- never by a row's presence in ``config.agents``, its source, its
    display name, a member directory or a session that used it. So ``default``,
    a built-in, an app's materialized agent, a file the agent sync registered
    and a plain crew-manager crew all stay session agents until the owner hires
    them by name. A record that does not cover the row's store
    (``agent_state.record_covers_store``: its generation, or the generation a
    store change through the API is moving it to) is a record of a member that
    was deleted and recreated under the same id: not this row.

    Nothing is enrolled on upgrade. No build before this one wrote a mark that
    only a hire could have written -- an earlier hire verb's private copy and
    ``member-`` store are exactly what the crew editor's fork and private-memory
    provisioning give a plain crew -- so every pre-existing row, that class
    included, stays a session agent until the owner hires it by name; nothing
    is deleted, retired or rebound.

    ``strict`` raises on an unreadable sidecar: a route about to WRITE for a
    member must not read "cannot verify" as "not a member", and the roster
    GET refuses the same way (503 ``members_unavailable``) so a corrupt record
    renders as the page's load error, never as a fresh install's empty crew.
    A lenient read logs and answers no members -- for callers that only
    DECORATE (a sidebar badge, a lineage caption) and have an honest empty.
    """
    try:
        records = agent_state.all_crewmate_records(strict=strict)
    except (OSError, ValueError):
        if strict:
            raise
        logger.warning("crewmate record unreadable; the roster lists no members", exc_info=True)
        return []
    out: list[str] = []
    for name, row in cfg.agents.items():
        if not isinstance(name, str) or not _AGENT_NAME_RE.match(name):
            continue
        if agent_state.record_covers_store(records.get(name), row.memory_store):
            out.append(name)
    return out


def _member_names_for_slug(cfg: KiroCrewConfig | Collection[str], slug: str) -> list[str]:
    """Crew names whose derived slug equals *slug*, in config order.

    Config order is insertion order, so "first name wins" is deterministic for
    a colliding slug. Names failing the agent-name grammar are skipped rather
    than matched: they cannot have been created through the validated CRUD
    surface, so a hand-edited config row never becomes addressable here.
    *cfg* is the parsed config or, inside a locked write, the raw document's
    ``agents`` map (its keys are the names).
    """
    # Duck-typed on ``agents``: the parsed config, or the lightweight stand-ins
    # the DM-thread tests hand the routes; a bare collection is the names.
    agents = getattr(cfg, "agents", None)
    names: Collection[str] = agents if agents is not None else cfg  # type: ignore[assignment]
    out: list[str] = []
    for name in names:
        if not isinstance(name, str):
            continue
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


def _identity_text(value: object) -> str:
    """Bound a wrapper identity string for the roster: text only, redacted.

    ``display_name``, ``role`` and the template link are user- or
    template-author-typed and live in a hand-editable, agent-writable config,
    so they get the same treatment as the roster's message preview: the
    credential / exfiltration-URL redactors run over the text before it ships.
    The create and rename routes already REFUSE a credential-shaped label, so
    for a value written through the API this is a no-op; it exists for the
    value that was not. Non-strings (a hand-edited object) render as "".
    """
    if not isinstance(value, str):
        return ""
    return _redact_external(value)


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

    # One sidecar read for the whole roster: which bound agent files are a
    # member's OWN copy, and of what. Lenient like the agents roster -- a
    # corrupt sidecar degrades to "no lineage shown", never to no roster.
    try:
        forks = await asyncio.to_thread(agent_state.all_fork_info)
    except (OSError, ValueError):
        logger.warning("fork sidecar unreadable; members roster shows no lineage", exc_info=True)
        forks = {}

    # Explicit enrollment decides the roster (see enrolled_member_ids): a
    # template the sync registered, a built-in or ``default`` is a session
    # agent, not a crewmate. Strict, like every member write: an unreadable
    # record must not read as "nobody is enrolled" on the one surface the
    # user looks at -- that is the fresh-install empty state wearing a
    # corruption. The page already renders ``roster_load_failed`` for a 5xx.
    try:
        roster_ids = await asyncio.to_thread(enrolled_member_ids, cfg, strict=True)
    except (OSError, ValueError):
        logger.warning("crewmate record unreadable; refusing the roster read", exc_info=True)
        return web.json_response(
            {"error": "the crewmate record could not be read", "code": "members_unavailable"},
            status=503,
        )
    rows: list[dict] = []
    for name in roster_ids:
        agent_cfg = cfg.agents[name]
        try:
            slug = members_mod.slug_for_name(name)
        except MemberSlugError:
            continue
        # The binding is free text in a hand-editable config: a non-string
        # (`kiro_agent: []`) must not turn the roster read into a 500 -- the
        # row lists with no lineage, like an unreadable sidecar does.
        bound_to = agent_cfg.kiro_agent
        fork = forks.get(bound_to) if isinstance(bound_to, str) else None
        template_origin = fork["forked_from"] if fork and fork.get("private_to") == name else ""
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
                # Wrapper identity (member_identity.py). `name` above is the
                # member's ID -- the key every other route addresses -- and
                # `display_name` is what the user reads and renames; the server
                # resolves the "" -> id fallback so no page re-implements it.
                # `role` is the job title. Where the member came from is the
                # normalized `source` above; nothing re-derives it here.
                "display_name": _identity_text(
                    effective_display_name(name, agent_cfg.display_name)
                ),
                "role": _identity_text(agent_cfg.role),
                # Lineage: when `kiro_agent` is this member's own copy (the
                # hire's copy-on-hire, or the editor's first-edit fork), the
                # template it was copied FROM -- so the drawer can say
                # "reviewer — own copy" instead of showing the copy's stem as
                # if it were a template. "" when the member is bound to a
                # shared template directly. The value is a DECLARED template
                # name -- text a package or a hand-edited spec wrote -- so it
                # takes the same redactor as the other identity fields.
                "template_origin": _identity_text(template_origin),
                # Store provenance: the template ('<app>/<agent>') and app version
                # the member was hired at; "" for a hand-made or locally adopted
                # member. Both are manifest text, so they take the same redactor.
                "template": _identity_text(agent_cfg.template),
                "template_version": _identity_text(agent_cfg.template_version),
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
    # Enrolled members only: a template or a plain crew that derives this slug
    # is not a member and cannot acquire a DM thread through this route. Strict
    # on the record -- this route creates the binding.
    def _enrolled_owners() -> list[str]:
        return _member_names_for_slug(enrolled_member_ids(cfg, strict=True), slug)

    try:
        slug_owners = await asyncio.to_thread(_enrolled_owners)
    except (OSError, ValueError):
        logger.warning("crewmate record unreadable; refusing the thread open", exc_info=True)
        return web.json_response(
            {"error": "the crewmate record could not be read", "code": "members_unavailable"},
            status=503,
        )
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
            if effective_session_key(slot) != canonical_key or slot.running:
                return web.json_response(
                    {
                        "error": "the member thread is running or linked to another session",
                        "code": "member_slot_conflict",
                    },
                    status=409,
                )
            # The owner selected this slug, not an editable transcript or DM
            # binding. A colliding slug needs an already protected assignment.
            slot._memory_assignment_from_history = True
            try:
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
    try:
        enrolled = await asyncio.to_thread(enrolled_member_ids, cfg, strict=True)
    except (OSError, ValueError):
        logger.warning("crewmate record unreadable; refusing the rules write", exc_info=True)
        return web.json_response(
            {"error": "the crewmate record could not be read", "code": "members_unavailable"},
            status=503,
        )
    if member not in enrolled:
        return web.json_response(
            {"error": "no crew member for this slug", "code": "member_not_found"}, status=404
        )
    # Same collision scan the roster/thread paths use — the central helper
    # applies the agent-name grammar filter and tolerates MemberSlugError, so
    # a hand-edited config key that is not a valid agent name can neither
    # crash this scan nor manufacture a phantom collision.
    colliding = _member_names_for_slug(enrolled, slug)
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


#: Sources a hire may name. ``local`` copies an installed Kiro custom-agent
#: The sources a member can be hired from. ``local`` adopts an installed agent
#: file (``~/.kiro/agents/<agent>.json``); ``store`` hires from a template an
#: installed app offers in its manifest's ``crew`` section (``{app, agent}``).
_HIRE_SOURCE_KINDS = frozenset({"local", "store"})


async def api_member_hire(request: web.Request) -> web.Response:
    """POST /api/members — hire a crew member from a local Custom Agent file.

    The one hire verb of the wrapper design, source ``local`` (the "adopt"
    path). Body::

        {"source": {"kind": "local", "agent": "reviewer"},  # installed agent file
         "display_name": "Checkout triage",          # optional: what you call them
         "role": "Oncall Triage Engineer",           # optional job title
         "workspace": "default", "triggers": "", "session_color": ""}  # optional

    **Named before it exists**: ``display_name`` is REQUIRED -- a missing or
    blank one is 400 ``missing_display_name`` (a non-string
    ``invalid_display_name``) before anything is minted, copied, provisioned
    or enrolled; there is no fallback to the role or the source file. The row
    is the owner's; a typed name that collides is 409
    ``agent_exists``, the user's to resolve.

    ATOMIC: the member either exists with its own copy of the source, or does
    not exist at all -- and at no moment is a row bound to the SHARED source
    readable. Three steps:

    1. **Resolve the source** before anything is written: an agent name that
       matches no installed file is a 404 here, not a member bound to nothing.
    2. **Copy-on-hire.** Inside the create's config-lock hold, before the row
       exists, the source definition is copied into a member-owned agent file
       (``name`` = the copy's stem, derived from the minted member id) through
       the same private-copy writer the crew editor's first-edit fork uses.
       Lineage lands in the agent_state sidecar, so the fork refresh keeps the
       copy's machine-maintained plumbing current.
    3. **Publish the wrapper row** through the same validated create path
       ``POST /api/agents`` uses -- the id is minted from ``display_name``,
       private memory is provisioned -- ALREADY bound to the copy.

    The copy comes first because a row bound to the shared source, even for
    the moment between a create and a fork, is a row a concurrent thread open
    can resolve and run a session against, and that session would keep using
    the shared template after the hire completed -- the exact hazard this
    verb exists to remove. A source that vanished between 1 and 2 answers 404
    with nothing written; a row that fails to persist after the copy unwinds
    the copy (file and lineage). A caller wanting a shared binding has
    ``POST /api/agents``.

    Two members hired from one file therefore coexist: each owns its own copy
    and its own row.
    """
    denied = await require_owner_dashboard_request(request, "member.hire")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be an object", "code": "body_not_object"}, status=400
        )
    source = body.get("source")
    if not isinstance(source, dict):
        return web.json_response(
            {"error": "source must be an object {kind, agent}", "code": "invalid_source"},
            status=400,
        )
    kind = source.get("kind")
    # ``isinstance`` before the membership test: a list or object here is
    # unhashable and would turn the frozenset lookup into a 500.
    if not isinstance(kind, str) or kind not in _HIRE_SOURCE_KINDS:
        return web.json_response(
            {
                "error": f"source.kind must be one of {sorted(_HIRE_SOURCE_KINDS)}",
                "code": "unsupported_source_kind",
            },
            status=400,
        )
    agent = source.get("agent")
    if kind == "store":
        app = source.get("app")
        if not isinstance(app, str) or not isinstance(agent, str):
            return web.json_response(
                {"error": "source.app and source.agent must be strings", "code": "invalid_source"},
                status=400,
            )
        # The whole store hire runs under the app's lifecycle lock: an update
        # of the app between resolving the listing and copying its materialized
        # agent would copy the NEW bytes while recording the old version and the
        # old spec as the member's pristine BASE -- a merge base that never
        # existed. The lock is the one install/update/uninstall take.
        async with app_lifecycle_lock(app):
            # Step 1 (store): resolve the listing -- installed and enabled app,
            # a card for that agent, a readable shipped spec, a materialized file.
            try:
                store = await asyncio.to_thread(member_templates.resolve_store_template, app, agent)
            except member_templates.TemplateUnavailable as exc:
                return web.json_response({"error": str(exc), "code": exc.code}, status=exc.status)
            return await _hire_from_source(request, body, store.materialized, store)
    return await _hire_from_source(request, body, agent, None)


async def _hire_from_source(
    request: web.Request,
    body: dict,
    agent: object,
    store: member_templates.StoreTemplate | None,
) -> web.Response:
    """Step 1 (local resolve) onward of a hire, for either source kind."""
    # The template-name grammar (the source is a FILE the installed listing
    # offers, dots included -- ``reviewer.v2`` -- and the row is bound to the
    # copy, not to it), checked before the name reaches any lookup: the create
    # path re-checks it, but refusing here keeps the error about the SOURCE
    # rather than about a "kiro_agent" the caller never spelled.
    if not isinstance(agent, str) or not _agents_handlers._TEMPLATE_NAME_RE.match(agent.strip()):
        return web.json_response(
            {"error": "source.agent must name an installed agent", "code": "invalid_source_agent"},
            status=400,
        )
    # Step 1: the source must exist NOW. The create path deliberately tolerates
    # a missing template (a crew may be bound ahead of an install); a hire may
    # not, because its promise is a copy of that file.
    try:
        source_spec, _source_name, _taken, _path = await asyncio.to_thread(
            _agents_handlers._load_template_specs,
            _agents_handlers.kiro_agents_dir_path(),
            agent,
            "api_member_hire",
        )
    except _agents_handlers._AmbiguousTemplateName:
        return web.json_response(
            {
                "error": f"'{agent}' matches more than one template file; rename one first.",
                "code": "ambiguous_template_name",
            },
            status=409,
        )
    if source_spec is None:
        return web.json_response(
            {"error": f"Template '{agent}' not found", "code": "template_not_found"}, status=404
        )
    # The optional text fields must be TEXT. The create core validates the
    # identity fields and the colour but stores `workspace`, `description`
    # and `triggers` as given, and a non-string there is a member that
    # persists and then breaks on use: a list `workspace` raises when the
    # thread opens, a list `description` raises inside `route_crew`. Refused
    # here, before anything is written, naming the field.
    for text_field in ("workspace", "description", "triggers", "session_color"):
        if text_field in body and not isinstance(body[text_field], str):
            return web.json_response(
                {
                    "error": f"{text_field} must be a string",
                    "code": f"invalid_{text_field}",
                },
                status=400,
            )
    # Allowlist, never a spread of the hire body into the create body: the
    # create route accepts fields a hire must not set (memory_store, source
    # tag, avatar image commits). A store hire takes the card's role and
    # triggers where the caller sent NO such key: the card is the default, the
    # user's word wins -- and an explicit empty string is a word ("no
    # triggers"), not an absence. The card's ghost face, when it has one, is
    # the member's face from the first frame (a ghost only -- the manifest
    # validator refuses a picture or a pack there, so nothing here can commit
    # an upload); the caller cannot pass one, a face is picked in the editor.
    card_role = store.card.role if store else ""
    card_triggers = store.card.triggers if store else ""
    create_body: dict[str, object] = {
        "role": body["role"] if "role" in body else card_role,
        "kiro_agent": agent,
        "workspace": body.get("workspace", "default"),
        "memory_store": "default",
        "triggers": body["triggers"] if "triggers" in body else card_triggers,
        "session_color": body.get("session_color", ""),
        "description": body.get("description", ""),
    }
    named = _hire_name(body)
    if isinstance(named, web.Response):
        return named
    create_body.update(named)
    if store is not None and store.card.member_avatar:
        create_body["avatar"] = store.card.member_avatar
    # Steps 2..4 are ONE transaction: a cancellation mid-way (a gateway
    # shutdown, a client that closed the request) must not leave a copy without
    # its row, a row half-published, or a linked member whose link never landed
    # and whose roll-back never ran. The transaction is drained to its own end
    # (success or roll-back) before a cancellation is re-raised.
    return await drained(_hire_transaction(request, agent, create_body, store))


async def _hire_transaction(
    request: web.Request,
    agent: str,
    create_body: dict[str, object],
    store: member_templates.StoreTemplate | None,
) -> web.Response:
    """Steps 2..4 of a hire; see :func:`api_member_hire`. Run under ``drained``."""
    # Steps 2 and 3 are ONE config-lock hold inside the create: the copy is
    # made first, then the row is published already bound to it. No moment
    # exists in which a row bound to the shared source can be read.
    hired_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _enroll(member_id: str, generation: str) -> None:
        # ENROLLMENT: the sealed-sidecar record that makes the row a crewmate
        # (enrolled_member_ids), written with the generation the create minted.
        # A file hire enrolls inside the create's publication (the create
        # unwinds it if the row fails). A STORE hire enrolls LAST, inside step
        # 4's lock hold, after the template link and its files landed: until
        # then the row is a session agent nobody can open a member thread on,
        # so a step-4 failure rolls back a row no one could have started using.
        agent_state.set_crewmate_record(
            member_id,
            generation=generation,
            template=store.ref if store is not None else agent,
            hired_at=hired_at,
        )

    created = await _agents_handlers._create_crew(
        request,
        create_body,
        copy_source=agent,
        # The store hire copies the snapshot resolve verified, never a re-read.
        copy_spec=store.materialized_spec if store is not None else None,
        admit=_slug_collision_refusal,
        enroll=_enroll if store is None else None,
    )
    if created.status != 200:
        return created
    created_payload = _own_payload(created)
    member_id = created_payload.get("name")
    copy_name = created_payload.get("kiro_agent")
    if not (isinstance(member_id, str) and member_id and isinstance(copy_name, str) and copy_name):
        # Our own create route answering a shape this handler does not know is
        # a bug, and one this handler must not paper over with defaults.
        logger.error("hire: the create route answered an unknown shape: %r", created_payload)
        return web.json_response(
            {"error": "create answered without an id", "code": "hire_incomplete"}, status=500
        )
    generation = created_payload.get("memory_store")
    display_name = created_payload.get("display_name")
    if not (isinstance(generation, str) and generation and isinstance(display_name, str)):
        logger.error("hire: the create route answered an unknown shape: %r", created_payload)
        return web.json_response(
            {"error": "create answered without a store", "code": "hire_incomplete"}, status=500
        )

    # Step 4 (store): link the member to its template -- provenance on the row,
    # the pristine copy for a later merge, the initial briefing -- and, last,
    # ENROLL it. Part of the same atom: a member that owns a copy but does not
    # know where it came from can never be offered an update, so a failure
    # here rolls back too. Under the config lock the create just released,
    # held across the guarded row update AND both file publications: every
    # other locked writer (a delete, a rebind, a same-id recreate) waits, so
    # the row the guard checked is the row the pristine copy and the briefing
    # are published for. Between the create and this step the row is NOT a
    # crewmate (no enrollment record): the roster does not list it and the
    # member routes answer 404 for it, so nobody can open the thread of a
    # member this step may still roll back.
    if store is not None:
        try:
            async with _agents_handlers._get_config_lock():
                await asyncio.to_thread(
                    _link_member_to_template,
                    member_id,
                    store,
                    generation=generation,
                    copy_name=copy_name,
                    enroll=_enroll,
                )
        except Exception:
            logger.exception(
                "hire %r: linking to template %s failed; rolling back", member_id, store.ref
            )
            rolled_back = await _roll_back_hire(
                request,
                member_id,
                still_bound_to=copy_name,
                generation=generation,
                copy_name=copy_name,
            )
            if not rolled_back:
                return web.json_response(
                    {
                        "error": (
                            f"'{display_name}' was created but could not be linked to its "
                            f"template and the member could not be removed; delete member "
                            f"'{member_id}' by hand."
                        ),
                        "code": "hire_incomplete",
                        "id": member_id,
                    },
                    status=500,
                )
            return web.json_response(
                {
                    "error": "Could not record the member's template",
                    "code": "template_link_failed",
                    "rolled_back": True,
                },
                status=500,
            )

    try:
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="member.hire",
            outcome="success",
            source="dashboard",
            resources=f"{member_id} <- {store.ref if store else agent} ({copy_name})",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for member.hire failed", exc_info=True)
    # What a caller reads and nothing else: the minted id (what `/members?member=`
    # resolves). The copy the member is bound to, its label, store and source
    # are read back from the roster row.
    return web.json_response({"ok": True, "id": member_id})


def _hire_name(body: dict) -> dict[str, object] | web.Response:
    """The create's ``display_name`` for a hire body.

    A crewmate is named by the owner, before it exists: ``display_name`` is
    REQUIRED and must not be blank or whitespace. There is no fallback to the
    role or to the source file -- a hire that arrives without a name is
    refused (400 ``missing_display_name``) before anything is minted, copied
    or written. The name
    may equal the role when the owner typed it; spaces and Unicode are kept as
    display text (the id is minted separately).
    """
    raw = body.get("display_name")
    if raw is not None and not isinstance(raw, str):
        return web.json_response(
            {"error": "display_name must be a string", "code": "invalid_display_name"},
            status=400,
        )
    if not isinstance(raw, str) or not raw.strip():
        return web.json_response(
            {
                "error": "display_name is required: name the crewmate before hiring it",
                "code": "missing_display_name",
            },
            status=400,
        )
    return {"display_name": raw}


def _slugmates(cfg: KiroCrewConfig | Collection[str], candidate: str) -> tuple[str, list[str]]:
    """The slug *candidate* would take and the OTHER members already on it."""
    try:
        slug = members_mod.slug_for_name(candidate)
    except MemberSlugError:
        return "", []
    return slug, [n for n in _member_names_for_slug(cfg, slug) if n != candidate]


def _slug_collision_refusal(
    cfg: KiroCrewConfig | Collection[str], candidate: str
) -> web.Response | None:
    """Refuse a hire whose minted id would share its slug with another member.

    The slug (`members.slug_for_name`) is lossy -- ``Foo`` and ``foo`` share
    one -- and it keys everything a member lives in: ``members/<slug>/``
    (activity, briefing), its rules and its DM binding. Two members on one
    slug would inherit each other's briefing and be refused their thread and
    rules as a collision; a hire is where that can still be said before
    anything exists. Runs as the create's ``admit`` hook: INSIDE the
    config-lock hold against the snapshot the row is published from, and
    AGAIN inside the cross-process locked publication against the document's
    ``agents`` map as it is on disk -- two processes' pre-lock checks could
    both pass for ``Triage`` and ``triage`` and then serialize into two rows
    on one slug; only the check at the write sees what the other process did.
    """
    slug, others = _slugmates(cfg, candidate)
    if not others:
        return None
    return web.json_response(
        {
            "error": (
                f"'{candidate}' would share its member space with '{others[0]}' "
                f"(both shorten to '{slug}'); choose a name that shortens differently"
            ),
            "code": "slug_collision",
        },
        status=409,
    )


def _own_payload(response: web.Response) -> dict:
    """Decode the JSON body one of OUR handlers just built, or ``{}``.

    The hire route composes the create and fork cores through their HTTP
    shells; their bodies are this package's own contract, pinned by tests, so
    an undecodable one is a bug, not input to tolerate.
    """
    try:
        payload = json.loads(response.text or "{}")
    except ValueError:  # pragma: no cover - our own handler's JSON
        return {}
    return payload if isinstance(payload, dict) else {}


def _link_member_to_template(
    member_id: str,
    store: member_templates.StoreTemplate,
    *,
    generation: str,
    copy_name: str,
    enroll: Callable[[str, str], None],
) -> None:
    """Step 4 of a store hire, on a worker thread.

    Records ``template`` / ``template_version`` on the row inside a locked
    read-modify-write (the row must still be there and still bound to its
    copy -- a concurrent delete or rebind aborts), then -- AFTER that commit
    landed and INSIDE the same cross-process lock hold
    (``update_config_locked(after_write=)``) -- writes the pristine copy the
    role-update merge reads as BASE, writes the initial briefing, and LAST
    enrolls the member (``enroll(id, generation)``: the crewmate record the
    roster and every member route read). The files are slug-keyed and the
    slug is lossy, so they must be published while the row they belong to is
    provably still this hire's: a second gateway that deletes and re-creates
    the id takes this lock to do it, and with the files written outside the
    hold that replacement would receive this hire's base and briefing as its
    own. Enrollment comes last so that a failure anywhere in this step leaves
    a row that was never a crewmate -- not on the roster, no thread to open,
    no rules to save -- which is what makes the caller's roll-back safe to
    run: it removes a row nobody could have started using. A failure anywhere
    in this step also removes the files the step had written so far (the
    pristine copy, the seeded briefing -- ``remove_step_four_files``, still
    inside the hold), so a later member that takes the slug does not inherit
    them. Raises on any failure; the caller rolls the hire back (its delete re-checks the row's
    identity inside its own locked mutation, so a replacement is never rolled
    back, and it refuses a row that is enrolled).
    """

    def mutate(doc: dict) -> dict:
        agents = doc.get("agents")
        row = agents.get(member_id) if isinstance(agents, dict) else None
        if not isinstance(row, dict):
            raise UnknownMemoryStore(f"Crew Member {member_id!r} was removed concurrently")
        if row.get("memory_store") != generation or row.get("kiro_agent") != copy_name:
            raise UnknownMemoryStore(f"Crew Member {member_id!r} was replaced concurrently")
        row["template"] = store.ref
        row["template_version"] = store.version
        return doc

    def publish_files(_doc: dict) -> None:
        slug = members_mod.slug_for_name(member_id)
        wrote_pristine = wrote_briefing = False
        try:
            member_templates.write_pristine_copy(slug, store)
            wrote_pristine = True
            wrote_briefing = member_templates.seed_briefing(slug, store.initial_briefing)
            enroll(member_id, generation)
        except BaseException:
            # Still inside the lock hold, the row still this hire's: the files
            # this step wrote go with it, or the next member to take the slug
            # (a local hire, say) would inherit this hire's base and briefing
            # as its own. The caller rolls the row back afterwards.
            member_templates.remove_step_four_files(
                slug, pristine=wrote_pristine, briefing=wrote_briefing
            )
            raise

    update_config_locked(mutate=mutate, after_write=publish_files)


async def _roll_back_hire(
    request: web.Request,
    member_id: str,
    *,
    still_bound_to: str,
    generation: str,
    copy_name: str | None = None,
) -> bool:
    """Undo a hire that failed after its row landed. True when the member is gone.

    The delete route's own mutation, under the config lock it expects: the row
    is removed, a private V2 store is archived under the retirement marker
    (never erased -- the ordinary fire posture), cached handles are released.

    Only the row THIS hire made is rolled back: it must still be bound to
    *still_bound_to* -- the source the create step bound it to, or the copy the
    fork rebound it to when the failure came after the copy -- AND still carry
    *generation* (the private store name the create minted -- unique per
    creation). The config lock is released between the steps, so a concurrent
    writer may have rebound the member in a gap -- which is exactly what makes
    the fork answer ``stale_binding`` -- or deleted it and recreated a same-id
    member from the same source, whose only tell is the store name. A member
    someone has already started shaping is theirs, not this hire's to delete.
    The check is made twice: once on the parsed config under the in-process
    lock (to answer early), and again INSIDE the delete's cross-process locked
    read-modify-write (``expect``), against the document as it is on disk at
    the write -- a writer in another process (the CLI, a second gateway) is not
    held by the in-process lock, and only the second check sees what it did.
    When the copy step had already produced the member's own agent file
    (*copy_name*), that file and its lineage record go too -- but only while the
    sidecar still says the copy is private to THIS member and no other row is
    bound to it, the same reference check the fork's own unwind makes. A row
    already absent, one that became the default, or one whose binding or
    generation moved is left alone and reported as NOT rolled back, so the
    caller names the member instead. So is a row that is ENROLLED with this
    generation: a store hire enrolls as the last act of step 4, so an enrolled
    row is a hire that completed -- a crewmate someone may already be talking
    to -- and no roll-back removes a crewmate.
    """
    try:
        async with _agents_handlers._get_config_lock():
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
            row = cfg.agents.get(member_id)
            if row is None or member_id == cfg.default_agent:
                return False
            record = await asyncio.to_thread(
                agent_state.get_crewmate_record, member_id, strict=True
            )
            if agent_state.record_covers_store(record, generation):
                logger.warning("hire roll-back: %r is an enrolled crewmate; leaving it", member_id)
                return False
            if row.kiro_agent != still_bound_to or row.memory_store != generation:
                logger.warning(
                    "hire roll-back: %r is not the row this hire made (bound to %r, store %r); "
                    "leaving it",
                    member_id,
                    row.kiro_agent,
                    row.memory_store,
                )
                return False

            def _mine(entry: dict) -> str | None:
                if entry.get("kiro_agent") != still_bound_to:
                    return f"Crew Member {member_id!r} was rebound concurrently; not rolled back"
                if entry.get("memory_store") != generation:
                    return f"Crew Member {member_id!r} was recreated concurrently; not rolled back"
                return None

            try:
                await _agents_handlers._delete_crew_record(request, member_id, expect=_mine)
            except UnknownMemoryStore as exc:
                logger.warning("hire roll-back: %s", exc)
                return False
            if copy_name:
                await asyncio.to_thread(_remove_private_copy, copy_name, member_id)
    except Exception:
        logger.exception("hire roll-back failed for %r", member_id)
        return False
    return True


def _remove_private_copy(copy_name: str, member_id: str) -> None:
    """Delete the agent file a rolled-back hire copied, and its lineage record.

    Under the config FILE lock and, inside it, the spec lock -- the same pair
    the copy was written under. Refuses when the sidecar does not name
    *member_id* as the copy's owner, or when any row in the document is still
    bound to the copy: the file may then be someone else's, and leaving an
    orphan is the recoverable mistake where deleting a live binding is not.
    The binding check reads the document INSIDE the cross-process config lock
    and the unlink happens in the same hold (a read-only mutate: nothing is
    written), so a same-id recreate in another process -- which publishes its
    row under that lock -- either lands before the check and is seen, or
    waits until the copy is gone and fails its own copy step cleanly; it can
    never have its row published between the check and the unlink.
    """
    agents_dir = _agents_handlers.kiro_agents_dir_path()

    def _check_then_unlink(doc: dict) -> None:
        with agents_spec_lock(agents_dir):
            fork = agent_state.get_fork_info(copy_name)
            if not fork or fork.get("private_to") != member_id:
                return None
            rows = doc.get("agents", {}) if isinstance(doc, dict) else {}
            if isinstance(rows, dict) and any(
                isinstance(e, dict) and e.get("kiro_agent") == copy_name for e in rows.values()
            ):
                logger.warning(
                    "roll-back: a crew is bound to copy %r; leaving it in place", copy_name
                )
                return None
            (agents_dir / f"{copy_name}.json").unlink(missing_ok=True)
            agent_state.prune(copy_name)
        return None

    update_config_locked(mutate=_check_then_unlink)
    _agents_handlers.clear_list_agents_cache()
