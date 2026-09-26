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
from collections import Counter
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from kiro_crew.autonudge import AutoNudgeService

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

#: ``asOfSeq`` for a member whose log does not exist yet. The log's own empty
#: position, since ``last_seq`` answers 0 for an empty log and a recorded event
#: starts at 1, so this is an ATTRIBUTABLE baseline: the client clears whatever
#: stale row the slug still holds and keeps applying live frames, every one of
#: which outranks it under higher-seq-wins.
_SEQ_NO_LOG = 0

#: ``asOfSeq`` for a slug that cannot be attributed AT ALL -- two members fold to
#: it, its log records a different member, or the read failed. Negative is not a
#: position: the client clears the slug and then REFUSES its live frames until a
#: roster read carries a real sequence. Right for a refusal, and wrong for a member
#: that merely has no log yet, whose frames must still land. The two cases share a
#: shape (no values) and differ only here, which is why they are named rather than
#: written as literals at each exit.
_SEQ_UNATTRIBUTABLE = -1


def _roster_only(snap: dict) -> dict:
    """*snap* narrowed to the one view a roster ROW renders.

    The list paints one thing per member -- the roster line -- while the activity,
    wake and driving views belong to the drawer, which opens for a single member at
    a time and reads them from that member's own route. Shipping all four on the
    list makes every row carry three views nothing on it reads, and the cost of
    each is the fold it walks.

    ``asOfSeq`` is carried through unchanged because it is a property of the LOG,
    not of the subset: the client seeds each key at that sequence under its
    higher-seq-wins rule, so a live frame that already moved a row past it keeps
    winning, and a key absent from this block leaves whatever the client holds for
    it untouched.
    """
    from kiro_crew.eventlog import types as eventlog_types

    values = snap.get("values", {}) if isinstance(snap, dict) else {}
    roster = values.get(eventlog_types.PROJ_ROSTER) if isinstance(values, dict) else None
    return {
        "asOfSeq": (
            snap.get("asOfSeq", _SEQ_UNATTRIBUTABLE)
            if isinstance(snap, dict)
            else _SEQ_UNATTRIBUTABLE
        ),
        "values": {} if roster is None else {eventlog_types.PROJ_ROSTER: roster},
    }


def _logged_slugs(svc) -> set[str]:
    """Every member that ALREADY has a log, without creating one for any member.

    A read of the roster must not bring a member's log into existence: opening the
    page is not an event in that member's life, and a create per row is one
    directory, one header write and one fsync each. ``slugs()`` answers from the
    store's own unit listing -- one pass for the whole roster rather than a probe
    per row -- and is the same enumeration the subscribe baseline trusts to say
    which members have a cursor at all.

    An unreadable store yields the empty set, which is indistinguishable from a
    store holding no logs at all -- so a row this listing omits is checked against
    the filesystem (:func:`_absence_is_confirmed`) before it is served as a member
    that has simply never been written about. A store fault stays a display
    degradation rather than a failed endpoint, but it degrades to a CLEARED row
    instead of a stale one.
    """
    try:
        return set(svc.slugs())
    except Exception:
        logger.debug("member log enumeration failed", exc_info=True)
        return set()


def _absence_is_confirmed(slug: str) -> bool:
    """Is *slug*'s log genuinely ABSENT, rather than merely unprovable?

    Asked only for a slug the enumeration did not return, because a slug missing
    from that listing is not by itself evidence of absence: ``unit_ids`` answers
    the empty list for a root it refuses and SKIPS a unit whose header it cannot
    read, cannot parse, or that does not fold back to the directory holding it. So
    one unreadable header, or one fault iterating the root, presents as "this
    member has no log".

    The distinction decides which sequence the row is served at, and only one of
    the two is safe to guess. An empty baseline keeps every cached row ABOVE it and
    goes on accepting live frames, so reporting an unreadable log that way leaves a
    stale roster row and stale drawer views on display as though current. The
    refusal sentinel clears the slug unconditionally, which is the honest answer
    when the log cannot be read.

    ``crew_log_dir`` re-asks what the listing swallowed, and its own rules are what
    make the answer trustworthy in both directions: an ABSENT root is the ordinary
    fresh-install case and is not an error, while a linked or off-tree root raises
    and a child under an unreadable root cannot be stat'd. Anything that cannot
    answer is reported as not-confirmed, so the uncertain case takes the sentinel
    that clears rather than the one that preserves.
    """
    try:
        from kiro_crew.crew_log.schema import KIND_MEMBER
        from kiro_crew.crew_log.store import crew_log_dir

        return not crew_log_dir(KIND_MEMBER, slug).exists()
    except Exception:
        logger.debug("member log absence unprovable for %r", slug, exc_info=True)
        return False


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


def _slot_flush_generation(slot: object) -> tuple[int, int, int] | None:
    """The three counters a live slot's persistence state is made of.

    ``(len(messages), _disk_window_len, _dirty_gen)``: a row appended, a flush
    that persisted rows, or an in-place edit each moves one of them. Sampled
    BEFORE the roster observation and compared AFTER the transcript read, so a
    slot whose state moved anywhere inside that window -- a reply that landed
    after the pre-await sample but before the disk read -- is refused the
    preview correction, the same observe/revalidate pair the roster fields
    already get. ``None`` when there is no live slot (dormant threads carry no
    in-memory rows, so their disk read is the only copy).
    """
    if slot is None:
        return None
    messages = getattr(slot, "messages", None)
    count = len(messages) if isinstance(messages, (list, tuple)) else 0
    return (
        count,
        int(getattr(slot, "_disk_window_len", 0) or 0),
        int(getattr(slot, "_dirty_gen", 0) or 0),
    )


def _slot_has_unflushed_rows(slot: object) -> bool:
    """Does this live slot hold rows the transcript on disk does not yet?

    The same three gates ``chat_handlers._reconcile_slot_window`` checks before
    trusting a disk read against a live window: in-memory rows past the last
    flush (``len(messages) > _disk_window_len``), a rewind in flight, or unsaved
    in-place edits. Module level so the roster read and its test share ONE
    definition. Used by ``api_members`` to refuse the preview correction for a
    member whose latest speech has reached the member log (the live emit fires at
    in-memory append time) but not yet the transcript file the speech-only read
    walks -- the disk read would return the PREVIOUS speech, the roster observed
    before it already holds the new one, and the correction would durably
    append the older quote on top. A later read, after the flush, still sees
    the roster and the transcript agree, so skipping here loses nothing.
    """
    if slot is None:
        return False
    messages = getattr(slot, "messages", None)
    pending = False
    if isinstance(messages, (list, tuple)):
        pending = len(messages) > int(getattr(slot, "_disk_window_len", 0) or 0)
    return bool(
        pending or getattr(slot, "_pending_rewrite", False) or getattr(slot, "_dirty_flag", False)
    )


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
                # Presentation label only, masked like the other free text. The
                # page shows it in place of `name` when non-empty; `name` stays
                # the identity every per-member route and binding is keyed on.
                "display_name": _roster_mask(agent_cfg.display_name),
            }
        )

    # Binding reads are file IO — one thread hop for the whole roster, not one
    # per row. Colliding slugs read the same file twice at most.
    def _read_bindings() -> dict[str, dict | None]:
        return {row["slug"]: members_mod.read_dm_binding(row["slug"]) for row in rows}

    bindings = await asyncio.to_thread(_read_bindings)

    # Perpetual mode reads the live registry in memory per row. Admission is
    # one sealed-file snapshot for the whole roster, offloaded once; per-row
    # checks stay O(1) and do no IO. The roster and team view read it as --
    # "on" while a loop on its own thread is active, "off" while a loop
    # record is paused or lacks admission (reason lives on the detail page),
    # "none" when nothing was ever armed. A structured monitor is not the
    # switch's loop and reads as "none". Absent service (KIROCREW_AUTONUDGE
    # unset) reads "none" for every row.
    from kiro_crew.autonudge_selfarm import recorded_arm_parties

    nudge_svc = _autonudge_instance()
    arm_parties = await asyncio.to_thread(recorded_arm_parties) if nudge_svc else {}

    unflushed_slot_keys: set[str] = set()
    flush_generation_before: dict[str, tuple[int, int, int] | None] = {}
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
        # Read BEFORE the roster observation and the transcript read below: a
        # slot with unflushed rows has speech on the member log the disk does
        # not hold yet, so its speech-only read must not become a correction.
        if slot is not None and _slot_has_unflushed_rows(slot):
            unflushed_slot_keys.add(slot_key)
        if slot_key:
            flush_generation_before[slot_key] = _slot_flush_generation(slot)
        row["perpetual"] = perpetual_state_of(nudge_svc, slot_key, arm_parties=arm_parties)

    # Last activity, for the roster's most-recent-first ordering. The DM
    # transcript's mtime is the one durable signal that survives restarts and
    # covers live and dormant threads alike. File stats are IO — one thread
    # hop for the whole roster, mirroring the binding reads above.
    def _read_transcript_tails() -> dict[str, tuple[float, str, bool, bool]]:
        if state is None or state.conversation_log is None:
            return {}

        def _sanitize(text: str) -> str:
            # Same redaction chain the sessions list uses, injected so it
            # runs BEFORE the preview's length cap — a credential split by
            # truncation leaves a partial token the patterns cannot match.
            text, _ = _h.redact_exfiltration_urls(text)
            text, _ = _h.redact_credentials(text)
            return text

        out: dict[str, tuple[float, str, bool, bool]] = {}
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
            # Speech only: the row's preview quotes what the member's chat
            # draws (its speech), never a tool call or a patrol turn.
            # `last_speech_info`, not `last_message_info`: the fourth value says
            # whether the tail walk reached the start of the log. An EMPTY
            # answer from a walk that did not is "spoke further back than the
            # windows reach", not "never spoke", and must never be written
            # into the member log as the authority (the reconcile below).
            preview, msg_ts, stopped, exhaustive = state.conversation_log.last_speech_info(
                log_key, sanitize=_sanitize
            )
            # Order by the newest MESSAGE, not the file: metadata writes and
            # rehydration bump the mtime without any new message, which made
            # rows reorder with no visible cause. mtime remains only as the
            # fallback for pre-timestamp transcript rows.
            out[row["slot_key"]] = (msg_ts or mt, preview, stopped, exhaustive)
        return out

    # Which members have a log, enumerated ONCE for this whole request. Both
    # closures below need it, and `slugs()` is uncached: it walks the member-kind
    # root and reads a header per member, so asking twice pays the whole-roster
    # enumeration twice for one answer that cannot change between them within a
    # request. Hoisted here rather than memoized inside the helper, because a
    # process-lifetime cache would have to be invalidated by every writer, and a
    # request is exactly the window where one reading is correct.
    def _logged_slugs_now() -> set[str]:
        from kiro_crew.eventlog.service import get_service

        return _logged_slugs(get_service())

    logged = await asyncio.to_thread(_logged_slugs_now)

    def _observe_rosters() -> dict[str, dict]:
        # The roster projection as it stood BEFORE the transcript read below.
        # `reconcile_member_preview` corrects the folded preview to what the
        # transcript says, and refuses when the roster has moved since THIS
        # observation: a live `member/message` that lands after it is either
        # already in the transcript the read sees (so the read agrees with
        # it) or newer than the read (so the correction is stale and refused).
        # Observing AFTER the read would let a message in between be read as
        # unchanged and then overwritten by the older transcript answer.
        #
        # Scoped to the members that correction can REACH: it is attempted only for
        # a slug whose row holds a slot key (a row without one never enters
        # `preview_authoritative`) and whose log already exists (a member with no
        # log has no folded preview to drift). Observing the rest costs a fold each
        # to produce a value nothing compares.
        from kiro_crew.eventlog.service import get_service

        svc = get_service()
        seen: dict[str, dict] = {}
        for row in rows:
            slug = row["slug"]
            if slug in seen or not row["slot_key"] or slug not in logged:
                continue
            try:
                snap = svc.snapshot(slug)
                values = snap.get("values", {}) if isinstance(snap, dict) else {}
                seen[slug] = dict(values.get("roster") or {})
            except Exception:
                seen[slug] = {}
        return seen

    observed_rosters = await asyncio.to_thread(_observe_rosters)
    tails = await asyncio.to_thread(_read_transcript_tails)
    # Slot keys whose speech-only read is trustworthy enough to correct the
    # member log with: a non-empty quote, or an empty one from a walk that
    # reached the start of the log -- and, either way, only for a slot whose
    # in-memory rows had all been flushed when this read started
    # (`_slot_has_unflushed_rows`). An empty read that ran out of window, or a
    # read racing a flush, is left alone -- the row still carries the read here
    # (the client falls back to the folded quote), but nothing is written.
    preview_authoritative: set[str] = set()
    for row in rows:
        mt, preview, stopped, exhaustive = tails.get(row["slot_key"], (0.0, "", False, False))
        row["last_active_ts"] = mt
        row["last_message"] = preview
        if not (preview or exhaustive) or row["slot_key"] in unflushed_slot_keys:
            continue
        # Re-ask AFTER the awaits: a slot that was clean at the pre-await sample
        # can have appended a reply during the roster observation or the disk
        # read (its member/message emit fires at in-memory append time, the
        # transcript copy lands at flush), and the disk read would then hold
        # the PREVIOUS speech. Both the state now and the generation since the
        # sample must agree, or the correction is refused for this read.
        slot_now = state._slots.get(row["slot_key"]) if state else None
        if slot_now is not None and _slot_has_unflushed_rows(slot_now):
            continue
        if _slot_flush_generation(slot_now) != flush_generation_before.get(row["slot_key"]):
            continue
        preview_authoritative.add(row["slot_key"])
        # A locale-independent boolean, NEVER the word "Stopped": the preview
        # is computed here where the client's locale is unknown, which is why
        # the trailing stop is SKIPPED from `last_message` rather than rendered
        # as a sentence. This flag lets the locale-aware client render its own
        # "Stopped" chip beside the preview, so a thread the user has stopped
        # does not read as ongoing work. Omitted when false so the common row
        # stays byte-for-byte what it is without it.
        if stopped:
            row["last_message_stopped"] = True

    # Per-member event-log projections. Off-loop because snapshot is synchronous
    # file IO. Best-effort: a logging fault never breaks the roster, so a member
    # whose log cannot be read falls back to an empty projection rather than
    # failing the endpoint.
    #
    # A READ, for the member logs that already exist. The config-derived row fields
    # above come straight from the config this request loaded, so they are right for
    # every member whether or not a log exists; what the projection adds is the
    # log's own record of them, which the client prefers when present because a
    # pushed `member_projection` frame has to be able to move a row. Keeping the two
    # in step is the reconcile's job, which runs for every member whose log exists
    # and returns before writing when the two already agree.
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
                    out[slug] = {"asOfSeq": _SEQ_UNATTRIBUTABLE, "values": {}}
                    continue
                # No log yet: the row is served from live config alone and nothing is
                # written. A member with no log has no recorded state that can be
                # stale, so there is nothing here for either reconcile to correct --
                # and creating the log to record that would make merely LOOKING at
                # the roster the event that brings every member's log into being.
                #
                # An EMPTY BASELINE, not the refusal sentinel: this member is named,
                # addressable and has simply not been written about yet, so the row
                # must keep accepting the live frames that arrive the moment it is.
                # Only for an absence the filesystem CONFIRMS, though: the listing
                # drops a log whose header it cannot prove, and an empty baseline
                # preserves every cached row above it, so guessing here would leave
                # a stale row on display as though it were current.
                if slug not in logged:
                    if _absence_is_confirmed(slug):
                        out[slug] = {"asOfSeq": _SEQ_NO_LOG, "values": {}}
                        continue
                    # A directory the listing did not account for is either a log
                    # created SINCE the listing was taken -- this member's first
                    # event, whose own frame is already on its way to the client --
                    # or one the store will not prove. The two need opposite
                    # answers, and only a fresh listing tells them apart: it is the
                    # question "will the store stand behind this log", which is what
                    # a snapshot cannot answer, because a damaged header line is
                    # skipped on load and the surviving events still fold to a real
                    # sequence. Asked for this slug alone, and reaching here is rare:
                    # a member who has never been written about has no directory.
                    if slug not in _logged_slugs(svc):
                        out[slug] = {"asOfSeq": _SEQ_UNATTRIBUTABLE, "values": {}}
                        continue
                    # The store proves it now, so it is an ordinary readable log and
                    # takes the ordinary path below.
                # A slug is LOSSY, and colliding names are supported: `Review_Agent`
                # and `review-agent` both fold to `review-agent`, and each activity
                # entry keeps the exact name so attribution survives. What does NOT
                # survive is a whole-member PROJECTION: one log holds one member's
                # folded roster, activity, wake and driving state, so serving it on a
                # second member's row renders the first member's work as the second's.
                # The header names the member the log belongs to, so a row that is
                # not that member is served an empty projection instead of a wrong
                # one. Logged at warning level because a blank row needs its reason.
                # A header holding the SLUG is exempt: the header is written only
                # while the log is fresh, so a writer with no name in hand (the
                # message path passes None) locks the slug in as the name for good.
                # That placeholder names nobody, and a slug is a lossy fold, so it
                # differs from almost every real name -- reading it as a second member
                # would blank a member's own state over a value that never was a name.
                logged_name = svc.logged_name(slug)
                if logged_name is not None and logged_name not in (row["name"], slug):
                    logger.warning(
                        "member slug %r logs %r, so %r gets no projection; "
                        "rename one member so their slugs differ",
                        slug,
                        logged_name,
                        row["name"],
                    )
                    out[slug] = {"asOfSeq": _SEQ_UNATTRIBUTABLE, "values": {}}
                    continue
                snap = svc.snapshot(slug)
                agent_cfg = agent_cfgs.get(row["name"])
                appended = False
                # Every read, for every member whose log exists. The reconcile
                # compares the folded roster against the live config and returns
                # before writing when they match, so a config that has not drifted
                # costs one field comparison -- and a config edited by hand rather
                # than through the dashboard reaches the log on the next read with
                # nothing to remember between requests.
                if agent_cfg is not None:
                    values = snap.get("values", {}) if isinstance(snap, dict) else {}
                    appended = (
                        eventlog_hooks.reconcile_member_config(
                            slug, row["name"], agent_cfg, values.get("roster", {})
                        )
                        is not None
                    )
                # The transcript's speech-only preview (read above) is the
                # authority for the roster's `last_message`; a fold that still
                # quotes a pre-speech-only machinery preview is corrected here,
                # to blank when the member has never spoken. Compared against
                # the roster observed BEFORE the transcript read (not this
                # later snapshot), so a message that spoke in between refuses
                # the correction instead of being overwritten by it.
                preview_appended = False
                if row["slot_key"] in preview_authoritative:
                    preview_appended = bool(
                        eventlog_hooks.reconcile_member_preview(
                            slug,
                            row["name"],
                            row.get("last_message", ""),
                            row.get("last_active_ts"),
                            observed_rosters.get(slug, {}),
                        )
                    )
                if appended or preview_appended:
                    # Re-snapshot only when a reconcile appended (the roster
                    # fields would otherwise be stale for this response).
                    snap = svc.snapshot(slug)
                out[slug] = (
                    snap
                    if isinstance(snap, dict)
                    else {"asOfSeq": _SEQ_UNATTRIBUTABLE, "values": {}}
                )
            except Exception:
                logger.debug("member projections failed for %r", slug, exc_info=True)
                out[slug] = {"asOfSeq": _SEQ_UNATTRIBUTABLE, "values": {}}
        return out

    projections = await asyncio.to_thread(_project_rows)
    # Same network-boundary redaction as the /history read and the projection
    # WS push: a projection block carries agent-authored free-text (an activity
    # record's `project`, message previews) and `svc.snapshot()` returns it raw,
    # so the credential + exfiltration-URL chain has to run before it crosses to
    # the browser or the roster list leaks what the sibling reads scrub. Narrowed to
    # the roster view FIRST so the chain runs over what the row ships rather than
    # over three views the row discards afterwards.
    from kiro_crew.eventlog.service import _redact_projection_value

    for row in rows:
        block = projections.get(row["slug"], {"asOfSeq": _SEQ_UNATTRIBUTABLE, "values": {}})
        row["projections"] = _redact_projection_value(_roster_only(block))

    return web.json_response({"members": rows})


def _autonudge_instance() -> "AutoNudgeService | None":
    from kiro_crew.autonudge import get_instance as _autonudge_get

    return _autonudge_get()


def perpetual_state_of(
    svc: "AutoNudgeService | None",
    slot_key: str,
    *,
    arm_parties: dict[tuple[str, str], str] | None = None,
) -> str:
    """The roster's reading of one crewmate's Perpetual mode: on / off / none.

    ``on`` = a loop on the crewmate's own thread is active AND its sealed
    record admits it. The switch reads ON for an admitted active finite loop
    too -- "on" is "waking on its own", not "uncapped"; the detail page shows
    that loop's wake count against its cap. ``off`` = a loop record is paused,
    OR it is active but its trusted admission was retired, quarantined or lost
    after key rotation. That second shape must be visible as OFF because the
    fire guard refuses every wake. ``none`` = no loop record, a structured
    monitor (which the switch never converts), no bound thread, or no service.
    """
    from kiro_crew.autonudge import is_structured_monitor_loop
    from kiro_crew.autonudge_selfarm import read_arm_party_strict

    if svc is None or not slot_key:
        return "none"
    loop = svc.get_by_slot(slot_key)
    if loop is None or is_structured_monitor_loop(loop):
        return "none"
    if not loop.active:
        return "off"
    if arm_parties is not None:
        party = arm_parties.get((str(loop.id), slot_key), "")
    else:
        try:
            party = read_arm_party_strict(loop.id, slot_key)
        except OSError:
            return "off"
    return "on" if party else "off"


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


async def api_member_projections(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/projections?member=<name> — one member's folded views.

    What the detail drawer mounts on. The roster list carries only the view a list
    ROW paints; the drawer paints the activity timeline, the patrol state and the
    driven-slot list, and it is open for exactly one member at a time, so it reads
    them here instead of every row carrying three views nothing on it reads.

    Serves the WHOLE snapshot rather than a named subset: the drawer reads three
    views today, the projection set is extensible (an app contributes its own
    ``<app>/<name>`` key), and a per-key allowlist here would silently withhold a
    view the moment one is added. The block is the same shape the roster ships and
    the same shape a ``member_projection`` frame carries, so the client seeds it
    through one code path.

    ``member`` (query, REQUIRED) is the exact crew name, for the same reason the
    activity read requires it: a slug is a lossy fold, two names can share one log,
    and a whole-member projection served on the wrong name renders one member's work
    as another's. The name is checked against the log's own header, and a mismatch
    answers 409 rather than a blank body -- the drawer has to be able to say WHY it
    is empty.

    A member with no log answers an empty BASELINE (``asOfSeq`` 0, no values),
    which is exactly what the roster sends for the same member, and the read does
    not create one: opening a drawer is not an event in that member's life. The
    negative sentinel is kept for a slug that cannot be attributed, because the
    client stops applying live frames to those and a member about to be written
    about for the first time must keep receiving them.

    Redacted through the projection chain before it leaves, the same as the roster
    block, the ``/history`` read and the WS push.
    """
    denied = await _deny_app_caller(request, "members.projections")
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

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    # The member has to OWN the slug, not merely fail to collide with it. Three
    # separate questions, and the same order the briefing read asks them in: does
    # this name derive this slug, does the name exist in config, and is it the only
    # name deriving the slug. Without the first, `?member=` is free to name a
    # SECOND member while the path names a log: a log whose header holds the slug
    # placeholder passes the header check below for any name, and the reconcile
    # would then append the named member's config fields into another member's log
    # and record the pass as done. The header check cannot stand in for this --
    # it asks which member the log RECORDS, and the placeholder records nobody.
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
    # Two rows folding to one slug is the case the roster blanks for BOTH of them,
    # and it has to blank here too: neither member can be told apart in a slug-keyed
    # log, so serving either one's views under this slug is a guess.
    # `_member_names_for_slug` is the addressability question, which is the one being
    # asked -- whether this ROUTE can name a single member behind the slug.
    if _member_names_for_slug(cfg, slug) != [member]:
        return web.json_response(
            {"error": "member slug is shared", "code": "member_slug_shared"}, status=409
        )

    def _read() -> dict | None:
        from kiro_crew.eventlog.service import _redact_projection_value, get_service

        svc = get_service()
        if slug not in _logged_slugs(svc):
            if _absence_is_confirmed(slug):
                # Empty baseline, for the reason the roster read gives at the same
                # exit: a member with no log is attributable, so its frames must
                # still land.
                return {"asOfSeq": _SEQ_NO_LOG, "values": {}}
            if slug not in _logged_slugs(svc):
                # The listing omits a log whose header it cannot prove, and a fresh
                # listing still omits this one, so this is a failed READ of the one
                # member the request is about. Raising takes the route's own 500,
                # which the drawer renders as an error instead of painting an
                # affirmative "nothing scheduled" over state it could not read.
                raise OSError(f"member log for {slug!r} is present but cannot be proved")
            # The store proves it now -- a log created since the first listing --
            # so it reads like any other.
        logged_name = svc.logged_name(slug)
        if logged_name is not None and logged_name not in (member, slug):
            return None
        snap = svc.snapshot(slug)
        # A PURE read, and deliberately no config reconcile. The reconcile compares a
        # config this request loaded against the folded roster and appends what
        # differs, so a save that lands between the load and the snapshot is undone by
        # an append carrying the older values. The roster read is where that
        # comparison belongs -- it runs for every logged member on every poll, and the
        # correcting append raises the log's sequence, so the corrected value outranks
        # an uncorrected one under the client's higher-seq-wins rule. Doing it here as
        # well buys one member's correction and costs a second writer on a read path.
        if not isinstance(snap, dict):
            return {"asOfSeq": _SEQ_UNATTRIBUTABLE, "values": {}}
        return {
            "asOfSeq": snap.get("asOfSeq", _SEQ_UNATTRIBUTABLE),
            "values": _redact_projection_value(snap.get("values", {})),
        }

    try:
        block = await asyncio.to_thread(_read)
    except Exception:
        logger.debug("member projections read failed for %r", slug, exc_info=True)
        return web.json_response(
            {"error": "could not read member projections", "code": "member_projections_failed"},
            status=500,
        )
    if block is None:
        return web.json_response(
            {"error": "member slug logs another member", "code": "member_slug_foreign"}, status=409
        )
    return web.json_response(block)


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
    "the user turns Perpetual mode off on your detail page."
)

PERPETUAL_BANNER = "Keeps working on its own until the owner turns Perpetual mode off"


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
    ``armed_by`` in the arm record (``autonudge_selfarm``'s own masked leaf, which
    no sandboxed process can write).

    ON with no loop: arm a NEW loop with ``max_cycles=0`` and
    ``max_runtime_secs=0`` -- unlimited, which is the point of the mode and
    the owner's deliberate choice here. Existing active loops follow the
    takeover rules below.
    ON with a stopped loop: resume THAT loop and lift its caps to unlimited,
    keeping its cycle accounting and its instruction.
    ON with an active finite self-arm: emit the critical start audit, take
    owner admission and clear both caps. An unlimited self-arm stays self-owned;
    a missing admission is repaired, and a finite owner arm has both caps
    cleared. Audit, trust-write or cap-update failure: 503 or its update status.
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
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    # The slug the roster row carries is ``member_slug(name, cfg)`` -- the
    # persisted ``member_id``, which can carry a collision suffix -- so the
    # match is made with the same derivation the sibling routes use, never
    # the bare name-derived ``slug_for_name``.
    try:
        if members_mod.member_slug(member, cfg) != slug:
            return _perpetual_error("member does not match slug", "member_slug_mismatch", 400)
    except MemberSlugError:
        return _perpetual_error("member does not match slug", "member_slug_mismatch", 400)
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


async def _audit_perpetual_owner_arm(loop: Any, slot_key: str, caller: str) -> bool:
    """Emit the add path's critical audit before repairing owner admission."""

    try:
        await asyncio.to_thread(
            lambda: _sel().log_tool_invocation(
                session_key=slot_key,
                source="dashboard",
                tool_name="autonudge_start",
                outcome="invoked",
                critical=True,
                metadata={
                    "slot_key": slot_key,
                    "idle_secs": int(loop.idle_secs),
                    "max_cycles": int(loop.max_cycles),
                    "max_runtime_secs": int(loop.max_runtime_secs),
                    "caller": caller,
                    "self_armed": False,
                    "owner_armed": True,
                },
            )
        )
    except Exception:  # noqa: BLE001 - fail closed: no audit means no authorization
        logger.error("owner-arm repair denied: SEL audit unavailable", exc_info=True)
        return False
    return True


async def _audit_perpetual_revoke(loop_id: str, slot_key: str, caller: str) -> bool:
    """Critical SEL event for a revoke that no audited update precedes.

    The owner's OFF on an ACTIVE loop revokes right after
    ``authorize_and_update_nudge``, whose AUDIT-OR-DENY critical ``invoked``
    event already records the change. OFF on a loop found PAUSED skips that
    update, so this event stands in for it: ``perpetual_revoke`` ``invoked``,
    critical, naming the loop and the caller. Returns ``False`` when the write
    failed -- the caller then refuses the revoke, because an authorization
    must not disappear unrecorded. Offloaded: SEL is blocking file IO.
    """
    try:
        await asyncio.to_thread(
            lambda: _sel().log_tool_invocation(
                session_key=slot_key,
                source="dashboard",
                tool_name="perpetual_revoke",
                outcome="invoked",
                critical=True,
                metadata={"loop_id": loop_id, "caller": caller},
            )
        )
    except Exception:  # noqa: BLE001 - the refusal is the caller's, logged here
        logger.error("perpetual revoke SEL audit unavailable; revoke refused", exc_info=True)
        return False
    return True


async def _restore_owner_loop_after_revoke_failure(
    svc: Any, loop_id: str, *, caller: str
) -> Any | None:
    """Roll OFF's own pause back to active after a refused revoke.

    An owner OFF is one transition -- pause AND revoke -- and a pause whose
    revoke was refused is not a completed OFF: the entry is the whole of the
    fire-time admission and the loop store is agent-writable, so a paused row
    with the entry standing is exactly the forge-usable state OFF exists to
    remove. Same rule as the member's own stop
    (``session_directive_apply._stop_owner_armed_member_loop``): the pause is
    rolled back so the row and the entry agree again (armed, active), and the
    owner is told OFF did not happen. The resume goes through the same audited
    chokepoint the pause did (AUDIT-OR-DENY: a refused audit is a refused
    restore), so the store never changes unrecorded. Issued as its own task and
    awaited through a shield: a cancellation settles the resume (re-shielded
    across repeat cancels) before propagating, and the store is re-read to
    learn what landed. Returns the restored loop, or ``None`` when the row is
    still paused -- the caller then reports that owner recovery is needed.
    """
    from kiro_crew.autonudge_authz import _settle_after_cancel, authorize_and_update_nudge

    def _current_if_active() -> Any | None:
        row = svc.get_by_id(loop_id) if callable(getattr(svc, "get_by_id", None)) else None
        return row if row is not None and bool(getattr(row, "active", False)) else None

    resume = asyncio.ensure_future(
        authorize_and_update_nudge(
            svc=svc, loop_id=loop_id, active=True, source="dashboard", caller=caller
        )
    )
    try:
        restored, error, _status = await asyncio.shield(resume)
    except asyncio.CancelledError:
        await _settle_after_cancel(resume)
        if _current_if_active() is None:
            logger.error(
                "owner-armed loop %s remained paused after a cancelled OFF rollback", loop_id
            )
        raise
    except Exception:  # noqa: BLE001 - a failed rollback is reported, not raised
        logger.error(
            "owner-armed loop %s could not be restored after its revoke failed",
            loop_id,
            exc_info=True,
        )
        return None
    if error is not None or restored is None or not bool(getattr(restored, "active", False)):
        logger.error(
            "owner-armed loop %s remained paused after its revoke failed: %s",
            loop_id,
            error or "resume refused",
        )
        return None
    return restored


async def _revoke_or_restore_owner_off(
    svc: Any, loop_id: str, *, caller: str
) -> tuple[bool, Any | None]:
    """Strict revoke of the entry OFF just paused; on refusal, roll the pause back.

    ``(revoked, restored)``: ``(True, None)`` when the entry is gone;
    ``(False, loop)`` when the revoke was refused and the loop is active again;
    ``(False, None)`` when the revoke was refused AND the restore did not land
    (the row is paused with the entry standing -- the owner's next OFF retries
    the revoke on the already-paused path). Cancellation joins the revoke
    thread, then runs the same rollback if the revoke did not succeed, before
    propagating -- a cancel must not strand a paused row with its admission
    intact any more than an error may.
    """
    from kiro_crew.autonudge_authz import _settle_after_cancel
    from kiro_crew.autonudge_selfarm import await_thread_to_completion, revoke_arm

    revocation = asyncio.ensure_future(await_thread_to_completion(revoke_arm, loop_id))
    try:
        await asyncio.shield(revocation)
    except asyncio.CancelledError:
        await _settle_after_cancel(revocation)
        if revocation.cancelled() or revocation.exception() is not None:
            logger.error(
                "owner-arm record could not be revoked during a cancelled OFF of loop %s; "
                "restoring the loop",
                loop_id,
            )
            if await _restore_owner_loop_after_revoke_failure(svc, loop_id, caller=caller) is None:
                logger.error("cancelled OFF left owner-armed loop %s paused", loop_id)
        raise
    except OSError:
        logger.error(
            "owner-arm record could not be revoked on OFF of loop %s; restoring the loop",
            loop_id,
            exc_info=True,
        )
        return False, await _restore_owner_loop_after_revoke_failure(svc, loop_id, caller=caller)
    return True, None


async def _perpetual_mutation(
    *,
    state: DashboardState,
    svc: Any,
    slug: str,
    member: str,
    slot_key: str,
    enabled: bool,
    caller: str,
) -> web.Response:
    """The switch's effect, under the slot lock, start to finish.

    Re-runs the ownership resolution under the lock, against a FRESH
    configuration read there (the route's own snapshot is not passed in: it
    served to answer a stale page fast and to pick the lock, and a member
    deleted or rebound while this request waited must not pass the recheck on
    the configuration it was still in),
    and aborts (409 ``member_slot_conflict``) unless it still answers the key
    the lock was taken for -- the binding or the live slot moved while this
    request waited; 404 ``member_not_found`` when the member is gone. Then
    re-reads the loop under the lock: two presses on the same switch, or a
    press racing the member's own re-arm, must see each other's result.
    """
    from kiro_crew.autonudge import is_structured_monitor_loop
    from kiro_crew.autonudge_authz import (
        _settle_after_cancel,
        authorize_and_add_nudge,
        authorize_and_update_nudge,
    )
    from kiro_crew.autonudge_selfarm import await_thread_to_completion, revoke_arm
    from kiro_crew.dashboard.handlers.autonudge import _serialize

    async with _perpetual_lock(slot_key):
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if member not in cfg.agents:
            return _perpetual_error("no crew member for this slug", "member_not_found", 404)
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
            loop = existing
            # EVERY successful OFF ends with the arming authorization revoked --
            # the loop paused by this OFF, and a loop found already paused (the
            # member stopped it, or an earlier OFF left its entry standing)
            # alike. The trust entry is the whole of an owner arm's fire-time
            # admission (the self-arm path also needs the store's own bit; the
            # owner path has none), and the loop store is agent-writable -- so
            # a paused record that kept its entry could be revived by a forged
            # ``active: true`` and wake unattended after the owner said stop.
            # The record itself stays (reason ``manual``, shown as "turned off
            # by you", or the member's own); the owner's next ON re-records the
            # entry through the takeover path before it resumes. Joined on
            # cancellation like every trust write. A failed revoke is REPORTED,
            # not swallowed, and never with an ``ok``.
            if existing.active:
                # The pause is issued as its OWN task so a cancellation of this
                # mutation (the shutdown drain) can still wait for it to settle:
                # the service shields its persist, so ``active: false`` may
                # commit after the cancel -- and a paused row that kept its
                # owner entry is exactly the forge-usable state the revoke below
                # exists to remove. On cancel: settle the pause (re-shielded
                # across repeat cancels), then, if the row is paused, revoke
                # (joined, strict) -- and if THAT is refused, roll the pause back
                # -- before the cancel propagates.
                pause = asyncio.ensure_future(
                    authorize_and_update_nudge(
                        svc=svc,
                        loop_id=existing.id,
                        active=False,
                        source="dashboard",
                        caller=caller,
                    )
                )
                try:
                    loop, error, status = await asyncio.shield(pause)
                except asyncio.CancelledError:
                    await _settle_after_cancel(pause)
                    paused_row = svc.get_by_id(existing.id)
                    if paused_row is not None and not bool(getattr(paused_row, "active", True)):
                        await _revoke_or_restore_owner_off(svc, existing.id, caller=caller)
                    raise
                if error is not None:
                    return _perpetual_error(error, "perpetual_off_failed", status)
                # Pause and revoke are ONE transition. A revoke the record
                # refuses rolls the pause back (the member's own stop does the
                # same), so a paused row never stands with its admission intact
                # on a 503 the owner may not read: the loop is active again,
                # armed as before, and OFF simply did not happen. Only when the
                # rollback itself fails is the row left paused -- said so, with
                # the entry still standing; the owner's next OFF meets it on the
                # already-paused path below and retries the revoke.
                revoked, restored = await _revoke_or_restore_owner_off(
                    svc, existing.id, caller=caller
                )
                if not revoked:
                    if restored is not None:
                        return _perpetual_error(
                            "Perpetual mode was not turned off: its authorization could not "
                            "be revoked, so the loop was kept running -- turn it off again "
                            "to retry",
                            "perpetual_off_failed",
                            503,
                        )
                    return _perpetual_error(
                        "Perpetual mode is paused, but its authorization could not be "
                        "revoked and the loop could not be restored -- turn it off again "
                        "to retry the revoke",
                        "perpetual_off_failed",
                        503,
                    )
                return web.json_response({"ok": True, "loop": _serialize(loop)})
            # The loop is already paused, so the audited update above is
            # skipped -- but the revoke below removes an AUTHORIZATION, and
            # that must never happen off the audit chokepoint. Same
            # AUDIT-OR-DENY contract as the update: a critical SEL event is
            # written first, and if it cannot be, the revoke is refused
            # (503) with the entry standing -- the owner presses OFF again.
            if not await _audit_perpetual_revoke(existing.id, slot_key, caller):
                return _perpetual_error(
                    "audit log unavailable — authorization not revoked; turn it off "
                    "again to retry",
                    "perpetual_off_failed",
                    503,
                )
            # Nothing of this OFF's to roll back: the pause was someone else's
            # (the member's stop, or an earlier OFF whose rollback failed), and
            # its reason is theirs to keep. A refused revoke is reported with
            # the row as found, and pressing OFF again retries exactly this step.
            try:
                await await_thread_to_completion(revoke_arm, existing.id)
            except OSError:
                logger.error("owner-arm record could not be revoked on OFF", exc_info=True)
                return _perpetual_error(
                    "Perpetual mode is off, but its authorization could not be revoked "
                    "-- turn it off again to retry",
                    "perpetual_off_failed",
                    503,
                )
            return web.json_response({"ok": True, "loop": _serialize(loop)})
        if existing is not None:
            if existing.active:
                loop, error, status = await _takeover_active_loop(
                    svc, existing, slot_key, caller=caller
                )
                if error is not None:
                    return _perpetual_error(error, "perpetual_on_failed", status)
                return web.json_response({"ok": True, "loop": _serialize(loop)})
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


#: The per-slot lock for the switch's mutations is ``autonudge_selfarm.
#: perpetual_slot_lock`` (defined next to the record it guards, because the
#: fire path in the gateway and the directive consumer take the same lock and
#: neither may import this handler for it). Re-exported under the names this
#: module's callers and tests use; the dicts are the SAME objects, so a holder
#: taken here is seen by every other taker.
from kiro_crew.autonudge_selfarm import (  # noqa: E402,F401 - re-export, see above
    _PERPETUAL_LOCK_USERS,
    _PERPETUAL_LOCKS,
)
from kiro_crew.autonudge_selfarm import perpetual_slot_lock as _perpetual_lock  # noqa: E402


async def _takeover_active_loop(
    svc: Any, existing: Any, slot_key: str, *, caller: str
) -> tuple[Any | None, str | None, int]:
    """Take owner control of an active loop without losing its prior party.

    A healthy unlimited self-arm stays self-owned. A finite self-arm, an
    unrecorded active loop, or a finite owner arm becomes an unlimited owner
    arm. The trust rewrite is token-keyed and rolls back when the cap update
    fails or is cancelled before both zero caps reach the store.
    """
    from kiro_crew.autonudge_authz import (
        _settle_after_cancel,
        authorize_and_update_nudge,
    )
    from kiro_crew.autonudge_selfarm import (
        ARMED_BY_OWNER,
        ARMED_BY_SELF,
        await_thread_to_completion,
        read_arm_party_strict,
        record_owner_arm,
    )

    loop_id = str(existing.id)
    try:
        previous_party = await asyncio.to_thread(read_arm_party_strict, loop_id, slot_key)
    except OSError:
        logger.error("trust record unreadable; active loop not taken over", exc_info=True)
        return None, "trust record unreadable — active loop not taken over", 503

    max_cycles = int(getattr(existing, "max_cycles", 0) or 0)
    max_runtime_secs = int(getattr(existing, "max_runtime_secs", 0) or 0)
    finite = max_cycles > 0 or max_runtime_secs > 0
    if previous_party in (ARMED_BY_SELF, ARMED_BY_OWNER) and not finite:
        return existing, None, 200

    token = ""

    async def _rollback() -> None:
        if not token:
            return
        try:
            await await_thread_to_completion(
                _restore_arm_party, loop_id, slot_key, previous_party, token
            )
        except asyncio.CancelledError:
            logger.warning(
                "cancelled while restoring active-loop trust for %s on %s",
                loop_id,
                slot_key,
            )
            raise
        except Exception:  # noqa: BLE001 - best effort after a refused update
            logger.warning("active-loop trust restore failed for %s", loop_id, exc_info=True)

    if previous_party != ARMED_BY_OWNER:
        if not await _audit_perpetual_owner_arm(existing, slot_key, caller):
            return None, "audit log unavailable — owner authorization not recorded", 503
        token = uuid.uuid4().hex
        try:
            await await_thread_to_completion(record_owner_arm, loop_id, slot_key, txn=token)
        except asyncio.CancelledError:
            await _rollback()
            raise
        except OSError:
            logger.error("owner-arm record unavailable; active loop not taken over", exc_info=True)
            return None, "owner-arm record unavailable — active loop not taken over", 503

    if not finite:
        return existing, None, 200

    update = asyncio.ensure_future(
        authorize_and_update_nudge(
            svc=svc,
            loop_id=loop_id,
            active=True,
            max_cycles=0,
            max_runtime_secs=0,
            source="dashboard",
            caller=caller,
        )
    )
    try:
        loop, error, status = await asyncio.shield(update)
    except asyncio.CancelledError:
        await _settle_after_cancel(update)
        stored = svc.get_by_id(loop_id)
        committed = (
            stored is not None
            and bool(getattr(stored, "active", False))
            and int(getattr(stored, "max_cycles", -1)) == 0
            and int(getattr(stored, "max_runtime_secs", -1)) == 0
        )
        if not committed:
            await _rollback()
        raise
    except Exception:
        await _rollback()
        raise

    committed = (
        loop is not None
        and bool(getattr(loop, "active", False))
        and int(getattr(loop, "max_cycles", -1)) == 0
        and int(getattr(loop, "max_runtime_secs", -1)) == 0
    )
    if error is not None or not committed:
        await _rollback()
        return None, error or "loop caps were not cleared", status if error is not None else 409
    return loop, None, 200


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
    emit the critical owner-arm audit before authorization changes; rewrite the
    entry to ``owner`` stamped with THIS takeover's token (skipped when it
    already says owner: nothing changed, nothing to restore); resume the loop
    with its caps lifted and its cycle accounting kept; decide on the update's
    FINAL RESULT -- the loop the awaited service call returned, never the
    in-memory record, since the service rolls its fields back when the persist
    fails and only the returned value reflects what was written.

    Rollback: on a refused resume or a raised one (the service has rolled its
    own state back by then), the entry is put back to the prior party -- but
    only if it still carries this takeover's token, so a later takeover's
    entry is never overwritten by an earlier one's cleanup. A CANCELLATION of
    the task (gateway shutdown) has two shapes. Before the resume is issued --
    during the joined owner-entry write -- the outcome is certain (the loop is
    still stopped), so the entry is restored to its prior party, joined, and
    then the cancel propagates: an owner entry over a loop the UI shows OFF
    would be the whole admission for a forged ``active: true``. During the
    resume itself the outcome is unknown at the moment of the cancel -- the
    service shields its persist and may still commit -- so the resume is issued
    as its OWN task and the cancel WAITS for it to settle (re-shielded across
    repeat cancels), then reads the store: a loop that resumed keeps its owner
    entry (it is now a running owner arm); a loop that did not is put back to
    its prior party, token-keyed and joined, before the cancel propagates. An
    owner entry left over a loop that never resumed would otherwise be the
    same forge-usable state as the pre-resume case. One entry per loop, one
    party per entry: see ``record_owner_arm``.
    """
    from kiro_crew.autonudge_authz import (
        _settle_after_cancel,
        authorize_and_update_nudge,
    )
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
        if not await _audit_perpetual_owner_arm(existing, slot_key, caller):
            return None, "audit log unavailable — owner authorization not recorded", 503
        token = uuid.uuid4().hex
        try:
            await await_thread_to_completion(record_owner_arm, loop_id, slot_key, txn=token)
        except asyncio.CancelledError:
            # The write was JOINED, so the entry may now say owner -- and the
            # resume below was never issued, so nothing is indeterminate here:
            # the loop is still stopped and the store shows it OFF. An owner
            # entry standing over it would be the whole fire-time admission for
            # a forged ``active: true`` on the agent-writable row, so the entry
            # is put back to the party it had (token-keyed: only THIS
            # takeover's write is undone), joined, before the cancel goes on.
            logger.warning(
                "cancelled while writing the owner entry for %s on %s; restoring the "
                "prior party before the resume was issued",
                loop_id,
                slot_key,
            )
            await await_thread_to_completion(
                _restore_arm_party, loop_id, slot_key, previous_party, token
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

    resume = asyncio.ensure_future(
        authorize_and_update_nudge(
            svc=svc,
            loop_id=loop_id,
            active=True,
            max_cycles=0,
            max_runtime_secs=0,
            source="dashboard",
            caller=caller,
        )
    )
    try:
        loop, error, status = await asyncio.shield(resume)
    except asyncio.CancelledError:
        # The resume's persist is shielded and may still land after this
        # cancel -- so wait for it, then decide on what the STORE says. A loop
        # that resumed is a running owner arm and keeps its entry; a loop that
        # did not (the update refused, raised, or its persist failed) must not
        # keep an owner entry it never earned: the fire-time guard admits an
        # owner wake on that entry alone and the row is agent-writable.
        await _settle_after_cancel(resume)
        resumed_row = svc.get_by_id(loop_id)
        if resumed_row is None or not bool(getattr(resumed_row, "active", False)):
            logger.warning(
                "cancelled during the resume of %s on %s and the loop did not resume; "
                "restoring the prior party",
                loop_id,
                slot_key,
            )
            await _rollback()
        raise
    except Exception:
        await _rollback()
        raise
    if error is not None or loop is None or not bool(getattr(loop, "active", False)):
        await _rollback()
        return None, error or "loop did not resume", status if error is not None else 409
    return loop, None, 200
