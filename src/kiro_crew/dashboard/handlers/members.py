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
import hashlib
import json
import logging
from collections.abc import Callable, Collection
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

import kiro_crew.dashboard.handlers as _h
import kiro_crew.dashboard.handlers.agents as _agents_handlers
from kiro_crew import agent_state, member_gallery, member_templates
from kiro_crew import members as members_mod
from kiro_crew import platform_compat
from kiro_crew.agent import _spec_path_is_safe, agents_spec_lock, write_agent_definition
from kiro_crew.agent_discovery import _read_agent_spec
from kiro_crew.apps.manager import app_lifecycle_lock
from kiro_crew.atomic_write import fsync_dir
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    default_project_dir,
    update_config_locked,
)
from kiro_crew.config.paths import data_home
from kiro_crew.dashboard.chat_persistence import (
    pin_private_agent_store,
    rehydrate_slot_from_history_async,
)
from kiro_crew.dashboard.chat_utils import _history_key_for, drained, effective_session_key
from kiro_crew.dashboard.handlers import sessions as _sessions
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.discover import _redact_external
from kiro_crew.dashboard.state import DashboardState, request_slot_origin
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.member_identity import display_name_too_long, effective_display_name
from kiro_crew.members import MemberSlugError
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.pinned_fs import read_file_pinned, write_file_pinned
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
    return agent_state.enrolled_member_ids(cfg.agents, strict=strict)


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
#: legacy ``aim`` spelling, an installed app's ``app``, a typo, a
#: credential-shaped string -- collapses to ``package``; the sync's prune step
#: prunes the three spellings it wrote itself (``package``, ``aim``, ``app``).
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
    """Bound a record's ``source`` to the three values the roster renders.

    ``kirocrew`` (created here) and ``local`` (a row the sync made for an agent
    file the user wrote) are both *mine*; ``builtin`` is shipped by this
    package; everything else -- ``package``, ``aim``, ``app`` -- came from a
    package or an installed app.
    """
    if raw in (_SOURCE_KIROCREW, "local"):
        return _SOURCE_KIROCREW
    if raw == _SOURCE_BUILTIN:
        return _SOURCE_BUILTIN
    return _SOURCE_PACKAGE


async def api_member_templates(request: web.Request) -> web.Response:
    """GET /api/members/templates — the hire gallery's catalog.

    Every template a member can be hired from, in one shape: the job cards
    enabled installed apps offer (``crew.templates``), the agent files this
    package ships (built-ins) and the user's own agent files
    (:mod:`kiro_crew.member_gallery`). Each card carries the exact ``source``
    body ``POST /api/members`` takes, what the gallery renders (role, duty,
    description, tags, category, starter prompts, face, publisher, version),
    the definition's capabilities, the crewmates already hired from it
    (``hired_as``: enrolled, active, each with the id that opens its DM and
    the display name the roster shows), and -- when a hire would be refused
    right now -- the refusal's code. A read: the listing is assembled from the
    manifests, the agent files and the enrollment records. Owner-gated like
    the hire it leads to. Free text on a card (role, duty, description, tags,
    prompts, the capabilities' names and the hired crewmates' display names --
    an MCP server or skill name is app- or user-authored config too) passes
    the same redactor the roster's identity fields do, and the card's avatar
    passes the roster's avatar shape allowlist (``_roster_avatar``: ``traits``
    values masked, structural keys pinned). The ROUTING identifiers
    -- ``id``, ``agent``, ``source.agent`` / ``source.app``, and a hired
    crewmate's ``id`` -- are what the client posts or navigates back, so they
    cannot be redacted and still work: a card (or a ``hired_as`` entry) whose
    routing identifier the redactor would alter (a hand-authored agent file or
    a hand-edited row named like a credential) is OMITTED from the listing
    instead of shipping the raw value across the dashboard boundary.
    """
    denied = await require_owner_dashboard_request(request, "member.templates")
    if denied is not None:
        return denied
    cards = await asyncio.to_thread(member_gallery.build_catalog)
    payload = []
    for card in cards:
        d = card.to_dict()
        # ``version`` too: an app's version string is app-authored text like
        # its publisher (a semver build suffix can carry anything), and the
        # roster's ``template_version`` already ships through this redactor.
        for key in ("role", "duty", "description", "publisher", "version", "unavailable_reason"):
            d[key] = _identity_text(d[key])
        d["tags"] = [_identity_text(t) for t in d["tags"]]
        d["starter_prompts"] = [
            {k: _identity_text(v) for k, v in s.items()} for s in d["starter_prompts"]
        ]
        d["capabilities"] = [
            {"kind": c["kind"], "name": _identity_text(c["name"])} for c in d["capabilities"]
        ]
        d["hired_as"] = [
            {"id": m["id"], "display_name": _identity_text(m["display_name"])}
            for m in d["hired_as"]
            if _identity_text(m["id"]) == m["id"]
        ]
        # The card's ghost goes through the roster's avatar shape allowlist:
        # ``traits`` values are app-authored text like the rest of the manifest,
        # so they are masked the same way (``_roster_avatar``); a card with no
        # avatar stays ``None`` (the gallery seeds a default face from the id).
        d["avatar"] = _agents_handlers._roster_avatar(d["avatar"]) if d["avatar"] else None
        routing = [d["id"], d["agent"], *(v for v in d["source"].values() if isinstance(v, str))]
        if any(isinstance(v, str) and _identity_text(v) != v for v in routing):
            logger.warning("hire gallery: omitting a card whose identifiers read as a credential")
            continue
        payload.append(d)
    return web.json_response({"templates": payload})


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

    # Serialized against a fire of the same member (``_dm_thread_lock``): a
    # fire that closes and retires a thread must not race an open that is
    # binding one, or the open re-creates the slot and the binding the fire
    # just removed and a same-slug re-hire inherits them.
    async with _dm_thread_lock:
        return await _open_member_thread(request, state, slug)


#: One lock for every DM-thread binding mutation: the thread open (read the
#: binding, bind a slot, write it) and the fire (close the slot, remove the
#: binding). Loop-bound like the config lock. Fire holds it OUTSIDE the config
#: lock; the open never takes the config lock, so the order cannot invert.
_dm_thread_lock = LoopBoundLock()


async def _open_member_thread(
    request: web.Request, state: DashboardState, slug: str
) -> web.Response:
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


async def api_member_briefing_get(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/briefing?member=<name> — the member's own briefing, read-only.

    The drawer's Briefing section (design step 6): what the member keeps in
    ``members/<slug>/briefing.md`` -- seeded once from a template's
    ``initial_briefing``, then the member's own. Read through
    :func:`members.read_member_briefing`, the same pinned, fail-closed read the
    prompt builder uses (no symlink followed anywhere on the agent-writable
    path, non-regular files refused, capped with a visible marker), so the
    drawer can never show a byte the prompt would not. ``member`` is required
    and must derive the slug, the activity endpoint's posture. An absent
    briefing is ``{"text": "", "supported": true}``; where the platform cannot
    read one race-free, ``supported`` is false and the drawer says so instead
    of "no briefing yet". Owner-gated like every other read of a member's own
    text (the briefing is prompt-adjacent and may be anything the member or a
    template wrote), and the text passes the same redactors the identity fields
    do, so a credential-shaped line in a briefing does not reach the browser raw.
    """
    denied = await require_owner_dashboard_request(request, "members.briefing")
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
    try:
        if members_mod.slug_for_name(member) != slug:
            return web.json_response(
                {"error": "member does not derive this slug", "code": "member_slug_mismatch"},
                status=400,
            )
    except MemberSlugError:
        return web.json_response(
            {"error": "invalid member name", "code": "invalid_member_slug"}, status=400
        )
    if not members_mod.member_briefing_supported():
        return web.json_response({"text": "", "supported": False})
    text = await asyncio.to_thread(members_mod.read_member_briefing, slug)
    return web.json_response({"text": _redact_external(text or ""), "supported": True})


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
    # Membership check AND write under the config lock -- the lock a fire
    # holds from its row delete through its cleanup (the rules file included).
    # Checked outside it, a PUT that read the row a moment before the fire
    # took the lock would land the rules AFTER the fire archived or purged
    # them: a live rules file for a member the roster does not hold, inherited by
    # the next member hired onto the slug. Inside the hold the row is re-read
    # and a fired member answers 404, so nothing is written behind the fire.
    async with _agents_handlers._get_config_lock():
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
        admit=_hire_admit,
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


def _hire_admit(cfg: KiroCrewConfig | Collection[str], candidate: str) -> web.Response | None:
    """The create's ``admit`` hook for a hire: the slug must be free of other
    members, and no interrupted fire may still be pending for this id OR for
    any id on this slug -- a marker means lived state under that SLUG (the
    activity, briefing, rules and thread binding are slug-keyed) is still on
    disk waiting to be archived, and a new member on the slug would inherit it
    (or lose its own files to the resumed cleanup). Fire the id again to finish,
    then hire. The plain create runs the same check (``_pending_fire_refusal``
    is its ``admit``), so no creation path reaches a slug a fire is mid-way on.
    """
    refused = _slug_collision_refusal(cfg, candidate)
    if refused is not None:
        return refused
    return _pending_fire_refusal(candidate)


def _pending_fire_refusal(candidate: str) -> web.Response | None:
    """409 ``fire_pending`` when a fire is pending for *candidate*'s id or slug."""
    try:
        pending = _pending_fires_for(candidate)
    except FireMarkerUnreadable as exc:
        # Fail closed: a marker that cannot be read still means a fire on this
        # slug may have left its lived state behind.
        return web.json_response(
            {
                "error": f"{exc}; fix or remove it before hiring under that id",
                "code": "fire_pending",
            },
            status=409,
        )
    if pending:
        return web.json_response(
            {
                "error": f"a fire of {pending[0]!r} did not finish; fire it again to finish "
                f"before hiring {candidate!r} (they share the member space "
                f"{members_mod.slug_for_name(candidate)!r})",
                "code": "fire_pending",
            },
            status=409,
        )
    return None


def _pending_fires_for(candidate: str) -> list[str]:
    """The ids with a pending fire marker that share *candidate*'s slug (the
    candidate's own id included). Raises :class:`FireMarkerUnreadable` when a
    marker on that slug cannot be read."""
    try:
        slug = members_mod.slug_for_name(candidate)
    except MemberSlugError:
        return []
    root = _fire_marker_path(candidate).parent
    try:
        entries = sorted(p.name for p in root.iterdir() if p.name.endswith(".json"))
    except FileNotFoundError:
        return []
    except OSError as exc:
        # A marker directory that cannot be LISTED (permissions, not a
        # directory) is not an empty one: a fire on this slug may have left a
        # marker in it. Fail closed like an unreadable marker, never a 500.
        raise FireMarkerUnreadable(
            f"the fire records under {root.name!r} could not be listed"
        ) from exc
    out: list[str] = []
    for entry in entries:
        member = entry[: -len(".json")]
        if not _AGENT_NAME_RE.match(member):
            continue
        try:
            if members_mod.slug_for_name(member) != slug:
                continue
        except MemberSlugError:
            continue
        if _read_fire_marker(member) is not None:
            out.append(member)
    return out


def _fire_slug_collision_text(other: str, slug: str, display_name: str) -> str:
    """The fire's 409 for a shared slug, naming the repair by the labels the
    navigation actually shows -- **Your Crewmates** (the Crew page's title) for
    a crewmate, **Agent Capabilities › Agents** for a plain crew -- so a reader
    with only this sentence can find the colleague. The page renders the same
    refusal from the structured ``collision`` field, translated."""
    return (
        f"Nothing was fired. Another member, '{other}', has the same short name "
        f"('{slug}') as '{display_name}', so they share files. Rename or fire that member "
        "first: find it under Your Crewmates if it is a crewmate, otherwise under "
        "Agent Capabilities \u203a Agents."
    )


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
        # After the row's commit landed, inside the same lock hold: a rename
        # that fails leaves no base claiming a provenance the row never got,
        # and a same-id fire + rehire (which takes this lock) cannot slip in
        # between the row and its base or receive this hire's briefing.
        slug = members_mod.slug_for_name(member_id)
        wrote_pristine = wrote_briefing = False
        try:
            member_templates.write_pristine_copy(member_id, store, generation=generation)
            wrote_pristine = True
            wrote_briefing = member_templates.seed_briefing(slug, store.initial_briefing)
            enroll(member_id, generation)
        except BaseException:
            # Still inside the lock hold, the row still this hire's: the files
            # this step wrote go with it, or the next member to take the id or
            # the slug (a rehire, a local hire of the same name) would inherit
            # this hire's base and briefing as its own. The caller rolls the
            # row back afterwards.
            member_templates.remove_step_four_files(
                member_id, pristine=wrote_pristine, briefing=wrote_briefing
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
            # The fire clears its resume marker only once every step is on
            # disk; an unlink is a directory entry, durable when its
            # directory is synced (see members.retire_member_space).
            fsync_dir(agents_dir)
            agent_state.prune(copy_name)
        return None

    update_config_locked(mutate=_check_then_unlink)
    _agents_handlers.clear_list_agents_cache()


# ── Role update (design step 4): three-way merge against the template ──


def _member_row(cfg: KiroCrewConfig, member: str) -> KiroCrewAgentConfig | web.Response:
    if not isinstance(member, str) or not _AGENT_NAME_RE.match(member):
        return web.json_response(
            {"error": "invalid member id", "code": "invalid_member"}, status=400
        )
    row = cfg.agents.get(member)
    if row is None:
        return web.json_response(
            {"error": f"Crew Member {member!r} not found", "code": "member_not_found"}, status=404
        )
    return row


def _template_unavailable(exc: member_templates.TemplateUnavailable) -> web.Response:
    # The refusal quotes the ref it resolved -- template-author-typed text from
    # an agent-writable config -- so it ships redacted like every identity string.
    return web.json_response(
        {"error": _redact_external(str(exc)), "code": exc.code}, status=exc.status
    )


def _load_member_spec(member: str, row: KiroCrewAgentConfig) -> tuple[dict, Path] | web.Response:
    """The member's OWN agent file: read, and proven to be its private copy.

    A role update rewrites this file, so the binding must be the copy the hire
    made for THIS member (the sidecar's ``private_to``), read through the same
    no-symlink / in-directory fence the agent editor's PATCH applies, and never
    a shared template another member is bound to.
    """
    agents_dir = _agents_handlers.kiro_agents_dir_path()
    copy_name = row.kiro_agent
    # The binding is free text in a hand-editable, agent-writable config: a
    # non-string (``kiro_agent: []``) or a value outside the agent-name grammar
    # is not the name of any private copy -- answered as such, never handed
    # to the sidecar lookup (an unhashable key there is a 500) or the path.
    if not isinstance(copy_name, str) or not _AGENT_NAME_RE.match(copy_name):
        return web.json_response(
            {
                "error": f"Crew Member {member!r} is not bound to its own copy of the template",
                "code": "not_private_copy",
            },
            status=409,
        )
    fork = agent_state.get_fork_info(copy_name)
    if not fork or fork.get("private_to") != member:
        return web.json_response(
            {
                "error": f"Crew Member {member!r} is not bound to its own copy of the template",
                "code": "not_private_copy",
            },
            status=409,
        )
    path = agents_dir / f"{copy_name}.json"
    if not _spec_path_is_safe(path, agents_dir):
        return web.json_response(
            {"error": "the member's agent file is not a regular file", "code": "agent_unreadable"},
            status=409,
        )
    spec = _read_agent_spec(path, operation="api_member_role_update", source="dashboard")
    if spec is None:
        return web.json_response(
            {"error": "the member's agent file could not be read", "code": "agent_unreadable"},
            status=409,
        )
    return spec, path


def _role_update_plan(
    member: str,
    row: KiroCrewAgentConfig,
) -> (
    tuple[member_templates.StoreTemplate, dict, dict, Path, list[member_templates.FieldDelta]]
    | web.Response
):
    """Everything the GET reports and the POST re-derives, on a worker thread.

    Returns ``(template, pristine, spec, path, deltas)`` or a ready response.
    """
    if not row.template:
        return web.json_response(
            {
                "error": f"Crew Member {member!r} was not hired from a template",
                "code": "not_linked",
            },
            status=409,
        )
    try:
        template = member_templates.resolve_template_ref(row.template)
    except member_templates.TemplateUnavailable as exc:
        return _template_unavailable(exc)
    pristine = member_templates.read_pristine_copy(member, generation=row.memory_store)
    if pristine is None or pristine.get("template") != row.template:
        return web.json_response(
            {
                "error": "the member's pristine copy of the template is missing; there is no "
                "base to merge against. Detach the member, or hire again",
                "code": "pristine_copy_missing",
            },
            status=409,
        )
    loaded = _load_member_spec(member, row)
    if isinstance(loaded, web.Response):
        return loaded
    spec, path = loaded
    deltas = member_templates.plan_role_update(
        pristine, spec, {"role": row.role, "triggers": row.triggers}, template
    )
    return template, pristine, spec, path, deltas


def _member_fingerprint(spec: dict, row: KiroCrewAgentConfig) -> str:
    """A digest of MINE as the plan saw it: the member's definition and card.

    The apply must carry it back: a plan is a decision about the member AS
    REVIEWED, and a member edited in between (a prompt rewritten in the crew
    editor, a role renamed) makes a stale ``theirs`` choice overwrite the newer
    customization. Canonical JSON so a re-read of the same file digests the same.
    """
    payload = json.dumps(
        {"spec": spec, "role": row.role, "triggers": row.triggers},
        sort_keys=True,
        separators=(",", ":"),
        # ``ensure_ascii=True``: the digest is over ``\uXXXX`` escapes, which
        # every str -- including one holding a lone surrogate a member's
        # spec file may carry -- serializes to; ``ensure_ascii=False`` puts the
        # raw code point in the payload and ``.encode("utf-8")`` then raises
        # on the surrogate, turning the plan into a 500.
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _template_fingerprint(template: member_templates.StoreTemplate) -> str:
    """A digest of THEIRS as the plan saw it: the template's materialized
    definition, its card and its version.

    The version alone is not an anchor: an app can re-materialize different
    bytes under the same version (a rewrite of the shipped file, a re-plumbed
    bridge output), and an apply pinned to the version alone would merge a
    template body nobody reviewed. The apply carries this back and a mismatch
    is ``template_changed``, the same refusal a version move gets.
    """
    payload = json.dumps(
        {
            "version": template.version,
            "spec": template.materialized_spec,
            "role": template.card.role,
            "triggers": template.card.triggers,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,  # see _member_fingerprint: encodable for every str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _redact_plan_leaves(value: object) -> object:
    """Run the credential / exfiltration-URL redactors over every string leaf
    (and string key) of a merge-plan field value, rebuilding lists and dicts.

    The plan echoes each side's VALUE -- ``base`` (the pristine copy), ``mine``
    (the member's own agent file) and ``theirs`` (the template as shipped) --
    so the review dialog can show what each side says. Those are agent-file
    fields: a prompt, a tools list, an ``mcpServers`` map whose ``env`` can
    carry a token, a URL with a secret in its query. The file is
    hand-editable and agent-writable, so a credential can sit in any of them,
    and the plan GET would otherwise hand it to the dashboard verbatim. Same
    discipline as the roster's identity strings (``_identity_text``) and the
    MCP app payloads: scrub before the value crosses to the browser. Shapes
    are preserved (the dialog renders by type); non-string scalars pass.

    Iterative (an explicit work stack), like ``member_templates.json_equal``:
    the value is JSON the parser accepted, and ``json.loads`` accepts nesting
    far deeper than the interpreter's recursion limit, so a recursive rebuild
    would raise ``RecursionError`` out of the plan GET and turn a legal file
    into a 500. Each container is rebuilt into a fresh list/dict; the input is
    never mutated (``delta.to_dict()`` hands out the plan's own values).
    """

    def _leaf(v: object) -> object:
        return _redact_external(v) if isinstance(v, str) else v

    def _shell(v: object) -> object:
        # An empty container of the same kind, filled by the stack below.
        return [] if isinstance(v, list) else {}

    if not isinstance(value, (list, dict)):
        return _leaf(value)
    root = _shell(value)
    # (source container, its rebuilt shell); children are appended to the
    # shell in order, so a list keeps its order and a dict its key order.
    stack: list[tuple[object, object]] = [(value, root)]
    while stack:
        src, dst = stack.pop()
        if isinstance(src, list):
            assert isinstance(dst, list)
            for item in src:
                if isinstance(item, (list, dict)):
                    child = _shell(item)
                    dst.append(child)
                    stack.append((item, child))
                else:
                    dst.append(_leaf(item))
        else:
            assert isinstance(src, dict) and isinstance(dst, dict)
            for k, v in src.items():
                key = _redact_external(k) if isinstance(k, str) else k
                if isinstance(v, (list, dict)):
                    child = _shell(v)
                    dst[key] = child
                    stack.append((v, child))
                else:
                    dst[key] = _leaf(v)
    return root


def _redacted_field(delta: member_templates.FieldDelta) -> dict[str, object]:
    """One plan field for the wire: every leaf redacted, then the opaque ``id``
    set back verbatim -- the redactor must never rewrite the handle the apply
    has to match a resolution to."""
    out = _redact_plan_leaves(delta.to_dict())
    if not isinstance(out, dict):  # pragma: no cover - to_dict() is a dict
        return {"id": delta.id}
    out["id"] = delta.id
    return out


def _plan_payload(
    member: str,
    row: KiroCrewAgentConfig,
    template: member_templates.StoreTemplate,
    pristine: dict,
    spec: dict,
    deltas: list[member_templates.FieldDelta],
) -> dict:
    return {
        "member": member,
        # The template ref is template-author-typed text in an agent-writable
        # config, like every identity string the roster ships: redacted at the
        # boundary (the fingerprints below are over the real value).
        "template": _identity_text(row.template),
        # Likewise the version: a config value an agent-writable file carries.
        "member_version": _identity_text(row.template_version),
        "installed_version": template.version,
        # A BASE behind the recorded version -- a pristine write that failed
        # after the row advanced -- keeps the update offered until the base
        # catches up, otherwise later template changes read as false conflicts.
        "update_available": member_templates.needs_update(deltas)
        or template.version != row.template_version
        or pristine.get("version") != row.template_version,
        "member_fingerprint": _member_fingerprint(spec, row),
        "template_fingerprint": _template_fingerprint(template),
        # Values redacted at the boundary; the fingerprints above are taken
        # over the REAL values, so a redacted echo never changes what the
        # apply's expected_version / fingerprint check compares against.
        # The opaque ``id`` (derived from the field name, carrying nothing of
        # it) is what a resolution names; set AFTER the redaction pass so the
        # redactor can never rewrite the handle the apply must match.
        "fields": [_redacted_field(d) for d in deltas],
    }


async def api_member_role_update_get(request: web.Request) -> web.Response:
    """GET /api/members/{member}/role-update — the merge plan, nothing written.

    BASE is the pristine copy the hire recorded, MINE the member's own agent
    file plus its ``role``/``triggers``, THEIRS the template as the app ships it
    now. Each field is ``unchanged`` / ``apply`` (only the template changed) /
    ``keep`` (only the member changed) / ``agree`` / ``conflict`` (both changed
    apart -- the user picks). ``update_available`` is true when applying would
    change the member or the version would move. Values are included so the
    review dialog can show what each side says.
    """
    denied = await require_owner_dashboard_request(request, "member.role_update.plan")
    if denied is not None:
        return denied
    member = request.match_info["member"]
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    row = _member_row(cfg, member)
    if isinstance(row, web.Response):
        return row
    planned = await asyncio.to_thread(_role_update_plan, member, row)
    if isinstance(planned, web.Response):
        return planned
    template, pristine, spec, _path, deltas = planned
    return web.json_response(_plan_payload(member, row, template, pristine, spec, deltas))


async def api_member_role_update_apply(request: web.Request) -> web.Response:
    """POST /api/members/{member}/role-update — apply the merge.

    Body ``{"resolutions": {"<field id>": "mine" | "theirs"}, "expected_version": "1.3.0",
    "member_fingerprint": "<from the plan>", "template_fingerprint": "<from the plan>"}``.
    The plan is re-derived here, under the app's lifecycle lock (so the
    template cannot move mid-merge) and the config lock; ``expected_version``
    is required and must equal the installed version the plan was made against
    (400 when absent, 409 ``template_changed`` otherwise); ``template_fingerprint``
    is required and must equal the digest of THEIRS the plan reported (409
    ``template_changed`` otherwise: the version alone does not pin the bytes an
    app materialized under it); ``member_fingerprint``
    is required and must equal the digest of MINE the plan reported (409
    ``member_changed_since_plan`` otherwise: a member edited between review and
    apply makes a stale ``theirs`` choice overwrite the newer customization);
    and every conflict must carry a
    resolution (409 ``unresolved_conflicts`` naming them). Order of writes:
    the member's agent file first (spec lock, governance funnel, atomic
    replace), then the row (``role``, ``triggers``, ``template_version``) in a
    locked read-modify-write that requires the binding to be unchanged, then
    the pristine copy advances. A crash between steps is recoverable by the
    next plan: an applied spec beside an un-advanced pristine copy reads as
    ``agree``; the reverse order would read the update as the member's own
    customization and lose it. Lived state -- briefing, rules, activity, DM
    thread -- is never touched.
    """
    denied = await require_owner_dashboard_request(request, "member.role_update.apply")
    if denied is not None:
        return denied
    member = request.match_info["member"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be an object", "code": "body_not_object"}, status=400
        )
    raw_resolutions = body.get("resolutions", {})
    if not isinstance(raw_resolutions, dict) or not all(
        isinstance(k, str) and v in ("mine", "theirs") for k, v in raw_resolutions.items()
    ):
        return web.json_response(
            {
                "error": "resolutions must map plan field ids to 'mine' or 'theirs'",
                "code": "invalid_resolutions",
            },
            status=400,
        )
    resolutions: dict[str, str] = dict(raw_resolutions)
    expected_version = body.get("expected_version")
    if not isinstance(expected_version, str) or not expected_version:
        # Required, not optional: an apply is a decision about a plan the user
        # reviewed, and the plan names the version it was made against. Without
        # it the route would apply THEIRS nobody looked at.
        return web.json_response(
            {
                "error": "expected_version is required: the installed version the plan was made against",
                "code": "invalid_expected_version",
            },
            status=400,
        )
    fingerprint = body.get("member_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        return web.json_response(
            {
                "error": "member_fingerprint is required: the plan's digest of the member as reviewed",
                "code": "invalid_member_fingerprint",
            },
            status=400,
        )
    theirs_fingerprint = body.get("template_fingerprint")
    if not isinstance(theirs_fingerprint, str) or not theirs_fingerprint:
        return web.json_response(
            {
                "error": "template_fingerprint is required: the plan's digest of the template as reviewed",
                "code": "invalid_template_fingerprint",
            },
            status=400,
        )
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    row = _member_row(cfg, member)
    if isinstance(row, web.Response):
        return row
    if not row.template:
        return web.json_response(
            {
                "error": f"Crew Member {member!r} was not hired from a template",
                "code": "not_linked",
            },
            status=409,
        )
    app_name = row.template.partition("/")[0]
    async with app_lifecycle_lock(app_name):
        async with _agents_handlers._get_config_lock():
            return await drained(
                asyncio.to_thread(
                    _apply_role_update,
                    member,
                    resolutions,
                    expected_version,
                    fingerprint,
                    theirs_fingerprint,
                )
            )


class _ApplyRefused(Exception):
    """A re-check inside the apply's locked mutation refused it; the response is ready."""


def _apply_role_update(
    member: str,
    resolutions: dict[str, str],
    expected_version: str,
    fingerprint: str,
    theirs_fingerprint: str,
) -> web.Response:
    cfg = KiroCrewConfig.load()
    row = _member_row(cfg, member)
    if isinstance(row, web.Response):
        return row
    planned = _role_update_plan(member, row)
    if isinstance(planned, web.Response):
        return planned
    template, _pristine, spec, path, deltas = planned
    if fingerprint != _member_fingerprint(spec, row):
        return web.json_response(
            {
                "error": "the member changed since the plan was made; review it again",
                "code": "member_changed_since_plan",
            },
            status=409,
        )
    if expected_version != template.version:
        return web.json_response(
            {
                "error": f"the template moved to v{template.version} since the plan was made; "
                "review it again",
                "code": "template_changed",
                "installed_version": template.version,
            },
            status=409,
        )
    if theirs_fingerprint != _template_fingerprint(template):
        return web.json_response(
            {
                "error": f"the template's definition changed under v{template.version} since "
                "the plan was made; review it again",
                "code": "template_changed",
                "installed_version": template.version,
            },
            status=409,
        )
    try:
        new_spec, new_card = member_templates.merge_role_update(
            spec, {"role": row.role, "triggers": row.triggers}, deltas, resolutions
        )
    except member_templates.UnresolvedConflicts as exc:
        return web.json_response(
            {
                "error": "every conflicting field needs a resolution",
                "code": "unresolved_conflicts",
                # Opaque ids (what the client resolves by) and the redacted
                # labels beside them -- never the field names raw, which a
                # hand-edited agent file can make credential-shaped.
                "fields": exc.fields,
                "labels": [_identity_text(label) for label in exc.labels],
            },
            status=409,
        )
    if display_name_too_long(new_card["role"]):
        return web.json_response(
            {"error": "the template's role is too long", "code": "role_too_long"}, status=409
        )
    # The member's id stays the file's declared name whatever the template says.
    new_spec["name"] = spec.get("name", row.kiro_agent)
    agents_dir = _agents_handlers.kiro_agents_dir_path()
    copy_name = row.kiro_agent
    generation = row.memory_store
    stale_card = False
    refused: list[web.Response] = []

    def mutate(doc: dict) -> dict:
        # Every re-check runs INSIDE the config file lock, and the agent file
        # is written only after ALL of them passed -- the row's binding and
        # generation, the card half of MINE, the copy's lineage and the file's
        # own bytes. Written before the card check, a stale card's 409 would
        # leave the merged spec on disk over the member's customizations with
        # the row and base still behind it -- the customization lost, the
        # plan reporting nothing to re-apply. Spec lock INNER, config lock
        # outer: the nesting every other writer of both uses.
        nonlocal stale_card
        agents = doc.get("agents")
        entry = agents.get(member) if isinstance(agents, dict) else None
        if not isinstance(entry, dict):
            raise UnknownMemoryStore(f"Crew Member {member!r} was removed concurrently")
        if entry.get("kiro_agent") != copy_name or entry.get("memory_store") != generation:
            raise UnknownMemoryStore(f"Crew Member {member!r} was rebound concurrently")
        # The LINEAGE too, ref AND version: a detach that committed after the
        # plan strips ``template`` (and its version) without touching the
        # binding or the generation, so the two checks above would let this
        # apply write a merged definition -- and a new base -- over a member
        # the owner just set free of the template; and an apply of this same
        # plan that committed in another process advanced the version while
        # keeping MINE on a conflict, so a second apply that passed on ref
        # alone would write THEIRS over the choice the first one committed. A
        # row whose template or version is not the plan's is not the member
        # this plan was made for.
        if (entry.get("template") or "") != (row.template or "") or (
            entry.get("template_version") or ""
        ) != (row.template_version or ""):
            raise UnknownMemoryStore(
                f"Crew Member {member!r} was detached from, updated from or rebound to "
                "another template concurrently"
            )
        # The CARD half of MINE, re-read under the cross-process lock: a role
        # or triggers edit that landed after the plan (the crew editor, in
        # this or another process) is a customization this merge never saw,
        # and writing the merged card over it would erase it. The binding and
        # generation alone do not see such an edit.
        if (entry.get("role") or "") != row.role or (entry.get("triggers") or "") != row.triggers:
            stale_card = True
            raise UnknownMemoryStore("the member changed since the plan was made; review it again")
        with agents_spec_lock(agents_dir):
            # The lineage, inside the spec lock: the file is rewritten below.
            fork = agent_state.get_fork_info(copy_name)
            if not fork or fork.get("private_to") != member:
                refused.append(
                    web.json_response(
                        {"error": "the member's copy changed hands", "code": "not_private_copy"},
                        status=409,
                    )
                )
                raise _ApplyRefused()
            # And the FILE itself, re-read under the same lock every other
            # writer of it takes (the fork refresh that re-materializes MCP
            # commands and hooks, the agent editor's PATCH): the plan read it
            # before any lock, and a whole-file write of a merge computed from
            # that older snapshot would revert whatever landed in between. The
            # merge is applied only when the bytes under the lock are still
            # the bytes the plan digested.
            current = _read_agent_spec(path, operation="api_member_role_update", source="dashboard")
            if current is None or _member_fingerprint(current, row) != fingerprint:
                refused.append(
                    web.json_response(
                        {
                            "error": "the member changed since the plan was made; review it again",
                            "code": "member_changed_since_plan",
                        },
                        status=409,
                    )
                )
                raise _ApplyRefused()
            write_agent_definition(path, new_spec)
        entry["role"] = new_card["role"]
        entry["triggers"] = new_card["triggers"]
        entry["template_version"] = template.version
        return doc

    def advance_base(_doc: dict) -> None:
        # The pristine copy advances AFTER the row's commit landed and INSIDE
        # the same lock hold. After, because the base is the row's provenance
        # made concrete: a config rename that fails must leave the base where
        # the row still is, or the next plan merges v(n+1) changes against a
        # v(n+2) BASE and reads the template's own edits as the member's.
        # Inside, because a same-id member replaced after this write (fire +
        # rehire, which take this lock) can never receive a late pristine
        # write stamped with the old generation over its own base. The
        # publish is atomic, so a failure here keeps the PREVIOUS base intact:
        # the row is ahead of it by one version, the next plan sees MINE equal
        # to THEIRS on every field the template changed and its apply rewrites
        # the base -- the state self-corrects rather than refusing.
        try:
            member_templates.write_pristine_copy(member, template, generation=generation)
        except OSError:
            logger.warning(
                "role update %r: applied, but the pristine base could not be advanced",
                member,
                exc_info=True,
            )

    try:
        update_config_locked(mutate=mutate, after_write=advance_base)
    except _ApplyRefused:
        return refused[0]
    except UnknownMemoryStore as exc:
        if stale_card:
            return web.json_response(
                {"error": str(exc), "code": "member_changed_since_plan"}, status=409
            )
        return web.json_response({"error": str(exc), "code": "member_changed"}, status=409)
    _agents_handlers.clear_list_agents_cache()
    return web.json_response({"ok": True})


async def api_member_detach(request: web.Request) -> web.Response:
    """POST /api/members/{member}/detach — sever the member from its template.

    Clears ``template`` / ``template_version`` on the row and removes the
    pristine copy; the member's agent file, role, triggers and every piece of
    lived state stay exactly as they are. One-way: a detached member is
    behaviorally a member hired from a local file, and re-linking is a hire.
    """
    denied = await require_owner_dashboard_request(request, "member.detach")
    if denied is not None:
        return denied
    member = request.match_info["member"]
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    row = _member_row(cfg, member)
    if isinstance(row, web.Response):
        return row
    if not row.template:
        return web.json_response(
            {"error": f"Crew Member {member!r} is not linked to a template", "code": "not_linked"},
            status=409,
        )
    async with _agents_handlers._get_config_lock():
        return await drained(
            asyncio.to_thread(_detach_member, member, row.memory_store, row.template)
        )


def _detach_member(member: str, generation: str, template: str) -> web.Response:
    """Sever inside the locked mutation, only from the row the request saw.

    The row is re-read under the lock and must still carry the store
    *generation* (unique per creation) and the *template* the caller validated
    against: a same-id member replaced in between is somebody else's, and its
    provenance and pristine base are not this request's to remove.
    """

    def mutate(doc: dict) -> dict:
        agents = doc.get("agents")
        entry = agents.get(member) if isinstance(agents, dict) else None
        if not isinstance(entry, dict):
            raise UnknownMemoryStore(f"Crew Member {member!r} was removed concurrently")
        if entry.get("memory_store") != generation or entry.get("template") != template:
            raise UnknownMemoryStore(f"Crew Member {member!r} was replaced concurrently")
        entry.pop("template", None)
        entry.pop("template_version", None)
        return doc

    def remove_base(_doc: dict) -> None:
        # After the row's commit and inside the same hold, for the reasons the
        # apply advances its base there: a rename that fails keeps a still
        # linked row's base, and a same-id rehire after this write cannot lose
        # its fresh base to this detach's late removal. A base that survives
        # a failed removal is an orphan: the row does not point at it.
        try:
            member_templates.remove_pristine_copy(member)
        except OSError:
            logger.warning("detach %r: the pristine copy could not be removed", member)

    try:
        update_config_locked(mutate=mutate, after_write=remove_base)
    except UnknownMemoryStore as exc:
        return web.json_response({"error": str(exc), "code": "member_changed"}, status=409)
    _agents_handlers.clear_list_agents_cache()
    return web.json_response({"ok": True})


# ── Fire (design step 5): retire the member, archive what it lived ──


async def api_member_fire(request: web.Request) -> web.Response:
    """POST /api/members/{member}/fire — retire a crew member.

    Body ``{"purge": false}`` (optional). Fire is the reverse of hire: the
    wrapper row goes, the member's own agent file goes, the pristine copy goes;
    the private memory store is archived under the ordinary retirement marker
    (never erased, restorable from the memory admin surface). What the member
    LIVED -- its DM thread, activity, briefing, rules -- is **archived, not
    destroyed**: the open thread is closed the way the tab's ✕ closes a chat
    (its transcript stays in the History tab), and ``members/<slug>/`` moves to
    ``members/.retired/`` with a ``fired.json`` naming the member, its thread's
    history key and its template. ``purge: true`` is the explicit request the
    design reserves for destruction: the lived state is removed instead of
    archived and the thread's transcript is deleted from history through the
    same path ``DELETE /api/sessions/{key}`` takes -- and reported as kept
    when that path refuses (a cron still owns the transcript, the store could
    not be read), never silently.

    Order: close the thread first (a live session must not keep running
    against a row that is about to vanish), then row + store under the config
    lock (the delete route's own mutation; the row must still be the one this
    request saw), then the copy and pristine copy, then the lived state. The
    default member cannot be fired (409 ``cannot_fire_default``).
    """
    denied = await require_owner_dashboard_request(request, "member.fire")
    if denied is not None:
        return denied
    member = request.match_info["member"]
    try:
        body = await request.json() if request.can_read_body else {}
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be an object", "code": "body_not_object"}, status=400
        )
    purge = body.get("purge", False)
    if not isinstance(purge, bool):
        return web.json_response(
            {"error": "purge must be a boolean", "code": "invalid_purge"}, status=400
        )
    state: DashboardState = request.app["state"]
    # Serialized against thread opens (see ``_dm_thread_lock``) for the whole
    # fire: from the binding read to the retirement, no open can re-bind the
    # slug under it.
    async with _dm_thread_lock:
        return await _fire_member(request, state, member, purge)


#: Where a fire records its intent before the row goes: a TOP-LEVEL leaf of the
#: data home, one ``<member id>.json`` per pending fire. NOT under ``trust/``:
#: that subtree is sandbox-VISIBLE (its SEL key and log have in-sandbox readers
#: and appenders), so a spawned shell could rewrite a marker by ``open()`` past
#: the file-tool gate -- and the resume TRUSTS the marker's ``purge``, ``slug``
#: and ``thread_history_key`` (a forged one deletes unrelated state or another
#: transcript). Bind-masked from every sandboxed process
#: (``sandbox._CREW_HIDDEN_LEAVES``), pre-created owner-only before each spawn
#: (``_CREW_PRECREATE_HIDDEN_DIR_LEAVES``: the root is otherwise built by the
#: first fire) and refused to the agent file tools on every OS
#: (``security._CREW_SECRET_LEAVES``); only the gateway reads or writes one.
# The activity writer reads the same directory (``members.fire_pending_for_slug``)
# to refuse recreating a space mid-fire; one constant, owned there.
FIRE_MARKERS_DIR_NAME = members_mod.FIRE_MARKERS_DIR_NAME


def _fire_marker_path(member: str) -> Path:
    """Where a fire records its intent before the row goes: the gateway-only
    ``FIRE_MARKERS_DIR_NAME`` leaf, keyed by the member id."""
    if not _AGENT_NAME_RE.match(member):
        raise MemberSlugError(f"invalid member id {member!r}")
    # The id grammar admits no separator or dot-run, so the name cannot
    # traverse; the path is returned UNRESOLVED on purpose -- resolving it
    # would follow a link planted at the marker's name and hand its target
    # back as the path to write, which is exactly what the pinned write and
    # read below exist to refuse.
    return data_home().resolve() / FIRE_MARKERS_DIR_NAME / f"{member}.json"


def _write_fire_marker(member: str, intent: dict) -> None:
    """Record the fire's intent through the no-follow publish path.

    ``write_file_pinned`` pins the parent and refuses a link planted at the
    marker's own name (and never writes a by-name temp file a same-user
    process could pre-plant as a link): a predictable ``<id>.json.tmp`` under
    ``write_text`` would be a truncation primitive aimed at whatever the link
    names. The directory is owner-only, normally already materialized before
    the first sandbox spawn.
    """
    path = _fire_marker_path(member)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        platform_compat.restrict_dir_to_owner(path.parent)
    except OSError:
        logger.debug("could not tighten mode on %s", path.parent, exc_info=True)
    write_file_pinned(path, json.dumps(intent, ensure_ascii=False), what="member fire record")


class FireMarkerUnreadable(Exception):
    """A fire's intent marker exists but cannot be read as one.

    Distinct from "no marker": an unreadable or malformed marker still means a
    fire of that id may have left lived state on disk, so the callers that ask
    fail CLOSED -- the hire refuses the id and a fire of the vanished member
    says the record must be fixed -- rather than treating it as absent and
    letting a replacement member inherit what the interrupted fire left.
    """


def _read_fire_marker(member: str) -> dict | None:
    """The recorded intent of a pending fire of *member*, ``None`` when there is
    none. Only a MISSING file is "none"; anything else that keeps the marker
    from reading raises :class:`FireMarkerUnreadable`."""
    try:
        text = read_file_pinned(_fire_marker_path(member), what="member fire record")
    except FileNotFoundError:
        return None
    except (OSError, MemberSlugError) as exc:
        raise FireMarkerUnreadable(f"the fire record for {member!r} could not be read") from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise FireMarkerUnreadable(f"the fire record for {member!r} is not readable JSON") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("slug"), str):
        raise FireMarkerUnreadable(f"the fire record for {member!r} does not name its member")
    return raw


def _clear_fire_marker(member: str) -> bool:
    """Remove the fire's intent marker; True when it is gone.

    False is a failure the caller must report: a marker left behind reads as
    a PENDING fire, and every later creation on the slug is refused with
    ``fire_pending`` until it is cleared.

    The caller runs this LAST, after every archive step has been made durable
    (``fired.json`` fsync'd, each directory a rename or unlink touched synced):
    the marker is the fire's only resume anchor, so its unlink must never reach
    disk ahead of the work it says is done. The marker directory is synced
    afterwards too, best-effort -- the unlink is committed at that point, and a
    marker that survives a crash only makes the next fire resume idempotent
    steps, which is the safe direction.
    """
    try:
        path = _fire_marker_path(member)
        path.unlink(missing_ok=True)
    except (OSError, MemberSlugError):
        logger.warning("fire %r: the intent marker could not be removed", member)
        return False
    fsync_dir(path.parent, best_effort=True)
    return True


async def _fire_member(
    request: web.Request, state: DashboardState, member: str, purge: bool
) -> web.Response:
    # Function-local on purpose: ``chat_handlers`` imports ``chat_runner``, which
    # imports this handlers package -- a module-scope import here is a cycle
    # (``ImportError: partially initialized module``); every import without
    # that cycle is module-scope.
    from kiro_crew.dashboard.chat_handlers import SlotCloseError, close_slot

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    if member not in cfg.agents:
        # A fire interrupted after the row went (the gateway died, a disk
        # error) left its intent marker; the retry RESUMES the cleanup instead
        # of answering 404 for a member whose lived state is still on disk.
        try:
            pending = await asyncio.to_thread(_read_fire_marker, member)
        except FireMarkerUnreadable as exc:
            return web.json_response(
                {
                    "error": f"{exc}; the interrupted fire's files were left in place -- fix or "
                    f"remove the record under {FIRE_MARKERS_DIR_NAME} to finish by hand",
                    "code": "fire_marker_unreadable",
                },
                status=500,
            )
        if pending is not None:
            async with _agents_handlers._get_config_lock():
                return await _finish_fire(request, state, member, pending, purge)
    row = _member_row(cfg, member)
    if isinstance(row, web.Response):
        return row
    if member == cfg.default_agent:
        return web.json_response(
            {
                "error": "The default member cannot be fired; make another member the default first",
                "code": "cannot_fire_default",
            },
            status=409,
        )
    # Only a CREWMATE can be fired: the fire is the member lifecycle's exit and
    # removes a row, its agent file and its uploaded picture. A row that is
    # not enrolled -- a built-in, an app's materialized agent, a synced file,
    # a plain crew -- is a session agent the crew manager owns, and this route
    # answers 404 for it the way every member route does; an unreadable
    # record is 503, never "not a member" (the write must not fail open).
    try:
        enrolled = await asyncio.to_thread(enrolled_member_ids, cfg, strict=True)
    except (OSError, ValueError):
        logger.warning("crewmate record unreadable; refusing the fire", exc_info=True)
        return web.json_response(
            {"error": "the crewmate record could not be read", "code": "members_unavailable"},
            status=503,
        )
    if member not in enrolled:
        return web.json_response(
            {"error": f"Crew Member {member!r} not found", "code": "member_not_found"}, status=404
        )
    generation = row.memory_store
    copy_name = row.kiro_agent
    slug = members_mod.slug_for_name(member)
    display_name = effective_display_name(member, row.display_name)
    # The lived state -- ``members/<slug>/``, the rules, the binding -- is
    # keyed by the lossy slug. Another live member on the same slug shares it,
    # so retiring it would take that colleague's activity, briefing, rules and
    # thread along; that member is renamed (or fired) first.
    slugmates = [n for n in _member_names_for_slug(cfg, slug) if n != member]
    if slugmates:
        return web.json_response(
            {
                "error": _fire_slug_collision_text(slugmates[0], slug, display_name),
                "code": "slug_collision",
                # Structured for the page, which says the same thing in the
                # user's language with the navigation's own labels.
                "collision": {"member": slugmates[0], "slug": slug},
            },
            status=409,
        )
    # The thread is this member's only when the binding names it exactly AND
    # points at THIS member's canonical slot -- the key derived from the slug
    # and the row's current private-store generation, the one the thread open
    # would bind. A binding naming a ghost of the same slug (a deleted member)
    # attributes nothing to fire; so does one left by a same-ID predecessor
    # (delete, re-create, fire): its slot carries the OLD generation, and
    # accepting it would purge the predecessor's transcript as ours.
    binding = await asyncio.to_thread(members_mod.read_dm_binding, slug)
    if binding is not None:
        expected_slot, expected_store = await asyncio.to_thread(
            _member_thread_slot, cfg, member, slug
        )
        if (
            binding.get("member") != member
            or binding.get("slot_key") != expected_slot
            or (binding.get("memory_store") or "") != expected_store
        ):
            binding = None
    slot_key = str(binding.get("slot_key") or "") if binding else ""
    thread_history_key = _history_key_for(slot_key) if slot_key else ""

    # 1. The thread: close it like the tab does. A failure leaves everything
    # as it was -- the member still exists and the user can retry.
    slot = state._slots.get(slot_key) if slot_key else None
    if slot is not None:
        try:
            await close_slot(state, slot, slot_key)
        except SlotCloseError as exc:
            return web.json_response(
                {"error": exc.message, "code": "thread_close_failed"}, status=500
            )

    intent = {
        "member": member,
        "slug": slug,
        "copy_name": copy_name,
        "purge": purge,
        "record": {
            "display_name": display_name,
            "slot_key": slot_key,
            "thread_history_key": thread_history_key,
            "template": row.template,
            "template_version": row.template_version,
            "fired_at": _utc_now_iso(),
        },
    }
    # 2. Row + store, under the config lock, only while the row is still the
    # one this request saw. The intent marker is written FIRST, in the same
    # hold: from here on the fire is resumable -- a cleanup step that fails
    # after the row is gone leaves the marker, the request says so, and the
    # next fire of this member finishes the job.
    async with _agents_handlers._get_config_lock():
        fresh = await asyncio.to_thread(KiroCrewConfig.load)
        current = fresh.agents.get(member)
        if current is None:
            return web.json_response(
                {"error": f"Crew Member {member!r} not found", "code": "member_not_found"},
                status=404,
            )
        if current.memory_store != generation or current.kiro_agent != copy_name:
            return web.json_response(
                {"error": f"Crew Member {member!r} changed concurrently", "code": "member_changed"},
                status=409,
            )
        try:
            await asyncio.to_thread(_write_fire_marker, member, intent)
        except (OSError, MemberSlugError) as exc:
            logger.exception("fire %r: the intent marker could not be written", member)
            return web.json_response(
                {"error": f"could not record the fire: {exc}", "code": "fire_not_recorded"},
                status=500,
            )

        def _still_this_member(entry: dict) -> str | None:
            # Re-checked INSIDE the delete's cross-process locked mutation: the
            # in-process lock does not hold a writer in another process, and a
            # row it replaced after the read above is not the one being fired.
            if entry.get("memory_store") != generation or entry.get("kiro_agent") != copy_name:
                return f"Crew Member {member!r} changed concurrently"
            # ... and still a crewmate of THIS generation: a fire or a delete in
            # another process that un-enrolled it since the check above makes
            # this row a session agent, not this fire's to remove.
            try:
                record = agent_state.get_crewmate_record(member, strict=True)
            except (OSError, ValueError):
                return "the crewmate record could not be read; nothing was fired"
            if not agent_state.record_covers_store(record, generation):
                return f"Crew Member {member!r} is not an enrolled crewmate; nothing was fired"
            return None

        slug_shared_with = ""

        def _space_still_ours(agents_doc: dict) -> str | None:
            # The slug check above ran against a pre-lock read. A plain create
            # in another process (its admit refuses only a PENDING fire, and
            # this fire's marker landed after that create's own admit could
            # have run) may have published a same-slug row since; the space
            # this fire archives or purges is then shared, and removing it
            # would take the new member's lived state with it.
            nonlocal slug_shared_with
            others = [n for n in _member_names_for_slug(agents_doc, slug) if n != member]
            if others:
                slug_shared_with = others[0]
                return _fire_slug_collision_text(others[0], slug, display_name)
            return None

        try:
            await _agents_handlers._delete_crew_record(
                request, member, expect=_still_this_member, expect_others=_space_still_ours
            )
        except UnknownMemoryStore as exc:
            await asyncio.to_thread(_clear_fire_marker, member)
            if slug_shared_with:
                return web.json_response(
                    {
                        "error": str(exc),
                        "code": "slug_collision",
                        "collision": {"member": slug_shared_with, "slug": slug},
                    },
                    status=409,
                )
            return web.json_response({"error": str(exc), "code": "member_changed"}, status=409)
        # Steps 3 and 4 run in the SAME config-lock hold: a hire of the same id
        # (which takes this lock to publish its row) cannot interleave and have
        # this fire's cleanup remove the copy, pristine copy or space the new
        # member just received.
        return await _finish_fire(request, state, member, intent, purge)


async def _finish_fire(
    request: web.Request, state: DashboardState, member: str, intent: dict, purge: bool
) -> web.Response:
    """Steps 3 and 4 of a fire -- the member's files and its lived state --
    from the recorded intent. Every step is idempotent, so this runs the same
    on a fresh fire and on a resume. Success is answered only once everything
    is done; a step that fails leaves the marker and answers 500
    ``fire_incomplete`` (the next fire of this member resumes here).

    *purge* is the CALLER's word for this request, so a resume asked with
    ``purge: true`` purges what the interrupted archive left; the recorded
    intent supplies everything else.
    """
    # Under the config lock (both callers). The marker is the fired member's:
    # a row under this id NOW is somebody else -- the hire refuses to publish
    # while a fire is pending, so this is a hand-edited config -- and its copy,
    # pristine copy and space are not this fire's to remove.
    current = (await asyncio.to_thread(KiroCrewConfig.load)).agents.get(member)
    if current is not None:
        return web.json_response(
            {
                "error": f"a member {member!r} exists again; the interrupted fire's files were "
                "left in place",
                "code": "member_changed",
            },
            status=409,
        )
    slug = str(intent.get("slug") or "")
    copy_name = str(intent.get("copy_name") or "")
    raw_record = intent.get("record")
    record: dict = raw_record if isinstance(raw_record, dict) else {}
    # Exactly ``True``: the marker is a file, and a hand-edited ``"false"`` must
    # not read as a purge the user never asked for.
    purge = purge or intent.get("purge") is True
    thread_history_key = str(record.get("thread_history_key") or "")
    try:
        # 3. The member's own definition and its pristine copy. The copy goes
        # only while the sidecar still names this member as its owner and no
        # row is bound to it -- the same rule the hire's roll-back applies.
        if copy_name:
            await asyncio.to_thread(_remove_private_copy, copy_name, member)
        await asyncio.to_thread(member_templates.remove_pristine_copy, member)
        # The enrollment record goes with the definition: a fired crewmate is
        # off the roster for good, and no later sync, re-list or restart may
        # bring it back (a row minted under this id again is a NEW hire, which
        # writes its own record). Idempotent -- a resume finds it already gone.
        await asyncio.to_thread(agent_state.clear_crewmate_record, member)
        # 4. Lived state: archived, or removed on explicit request. The archive
        # entry's name is chosen ONCE and recorded in the marker before
        # anything moves, so an attempt interrupted between the rename and the
        # record resumes into that same entry (or purges that exact entry)
        # instead of minting a second one and leaving the first unrecorded.
        archive_name = intent.get("archive_name")
        if not isinstance(archive_name, str) or not archive_name:
            archive_name = members_mod.new_archive_name(slug)
            intent = dict(intent, archive_name=archive_name)
            await asyncio.to_thread(_write_fire_marker, member, intent)
        archived = await asyncio.to_thread(
            members_mod.retire_member_space,
            slug,
            member=member,
            record=record,
            purge=purge,
            archive_name=archive_name,
        )
    except (OSError, ValueError, MemberSlugError) as exc:
        logger.exception("fire %r: the member's files could not be retired", member)
        return web.json_response(
            {
                "error": f"the member's row is gone but its files could not be retired: {exc}; "
                "fire it again to finish",
                "code": "fire_incomplete",
            },
            status=500,
        )
    # A thread that was bound but never wrote a transcript (opened, nothing
    # said) has nothing in History to archive or purge: "none", the same as a
    # member that never opened one. Probed through the log's own key resolver.
    log = state.conversation_log
    has_transcript = bool(
        thread_history_key
        and log is not None
        and await asyncio.to_thread(log.has_log, thread_history_key)
    )
    thread_state = "archived" if has_transcript else "none"
    kept_reason = ""
    if purge and has_transcript:
        kept_reason = await _purge_thread_history(state, thread_history_key)
        thread_state = "kept" if kept_reason else "purged"
    if await asyncio.to_thread(members_mod.member_space_exists, slug):
        # The space came back between the archive and here -- a member-space
        # writer (an activity record for an in-flight routing decision) that
        # ran ahead of the marker check. Live state the fire did not retire is
        # not a finished fire: the marker stays, so the next member on the slug
        # is still refused, and the next fire of this member archives (or
        # purges) the recreated space into the same entry.
        return web.json_response(
            {
                "error": "the member's space was written again while it was being fired; "
                "fire it again to finish",
                "code": "fire_incomplete",
            },
            status=500,
        )
    if not await asyncio.to_thread(_clear_fire_marker, member):
        # Everything else is done, but a marker left behind is a fire still
        # PENDING to every creation on this slug: not a success. Resumable --
        # every step above is idempotent, and the next fire's only remaining
        # work is this unlink.
        return web.json_response(
            {
                "error": "the member was fired but its intent marker could not be removed; "
                "fire it again to finish",
                "code": "fire_incomplete",
            },
            status=500,
        )
    # The response says what happened to the conversation, not where it lives:
    # the notice links the History pane, which finds it by name, and no reader
    # takes the key -- a field nothing reads is surface to keep honest for nothing.
    thread: dict[str, str] = {"state": thread_state}
    if kept_reason:
        # WHY the transcript stayed, so the notice can name the repair instead
        # of guessing: a scheduled job's claim, an unreadable store, a refusal.
        thread["kept_reason"] = kept_reason
    try:
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="member.fire",
            outcome="success",
            source="dashboard",
            resources=f"{member} purge={purge} thread={thread_state}",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for member.fire failed", exc_info=True)
    _agents_handlers.clear_list_agents_cache()
    state.push_refresh("agents")
    return web.json_response(
        {
            "ok": True,
            "thread": thread,
            "lived_state": "purged" if purge else ("archived" if archived else "none"),
        }
    )


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _purge_thread_history(state: DashboardState, key: str) -> str:
    """Delete a fired member's thread transcript exactly the way
    ``DELETE /api/sessions/{key}`` does -- the ONE shared sequence,
    :func:`sessions.delete_history_transcript` (claim the slot/transcript route,
    bind cron ownership, unlink under the transcript lock, remove the slot).
    Returns ``""`` when the transcript is gone, else WHY it was kept --
    ``cron_claim_unreadable`` (a scheduled job's claim on the transcript could
    not be read, so deleting it would strand that job), ``store_unreadable``
    (the cron store could not be read), ``refused`` (no conversation log, the
    transcript lock timed out, or the delete itself declined) -- so the notice
    names the repair instead of guessing at one.
    """
    outcome = (await _sessions.delete_history_transcript(state, key)).reason
    if outcome == _sessions.HISTORY_DELETE_NO_LOG:
        return "refused"
    if outcome == _sessions.HISTORY_DELETE_STORE_UNREADABLE:
        logger.warning("fire: the cron store could not be read; thread %s kept", key)
    elif outcome == _sessions.HISTORY_DELETE_CRON_CLAIM_UNREADABLE:
        logger.warning("fire: a cron owner claim on thread %s is unreadable; kept", key)
    return outcome
