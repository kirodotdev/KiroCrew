"""The Crew Members roster: ``GET /api/members`` and the member-log reads behind it.

One row per global crew, built from the config, the DM binding, the live slot and the
member's own log; the reconcile that keeps that log in step with the config and the
transcript's speech-only preview; the recency a row is ordered by; the ``source``
vocabulary a row's origin is bounded to (``normalize_member_source``, which the event-log
hooks and crew updates share); and Mate's welcome fields. Also the attribution rules a projection is served under -- which members have
a log, whether an absent one is confirmed, and the ``asOfSeq`` sentinels -- which the
drawer's projections read shares.

Served through :mod:`kiro_crew.dashboard.handlers.members`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import Any

from aiohttp import web

from kiro_crew import members as members_mod
from kiro_crew.config.loader import load_config_with_content_stamp
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.members_access import (
    _audit_owner_read,
    _deny_app_caller,
    _member_name_is_addressable,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.external_text import redact_external_text
from kiro_crew.members import MemberSlugError

logger = logging.getLogger("kiro_crew.dashboard.handlers.members")


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


def _member_is_created(member_id: object) -> bool:
    """Whether a crew record is a created crewmate: it carries a member id.

    ``member_id`` is assigned when a member's memory is allocated and is never
    coerced by the loader, so only a non-blank string counts; a hand-edited
    non-string or a blank reads as not created.
    """
    return isinstance(member_id, str) and bool(member_id.strip())


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
    # Owner gate, the briefing and rules endpoints' boundary: every row carries the
    # owner's view of a crewmate (its last DM line, its slot, its projections), and
    # a non-owner dashboard session holds the same empty app claim the app-caller
    # guard lets through. Gated before the config load, so a denial costs no read.
    owner_denied = await require_owner_dashboard_request(request, "members.list.read")
    if owner_denied is not None:
        return owner_denied
    _audit_owner_read(request, "members.list.read")
    state: DashboardState | None = request.app.get("state")
    # Loaded WITH the digest of the bytes it was parsed from. Every config-derived row
    # field below comes from this one load, and the per-row reconcile that writes them
    # into the member log runs several awaited reads later, so a save landing in
    # between would be overwritten by the values held here. The digest is what lets
    # that reconcile refuse instead.
    cfg, config_stamp = await asyncio.to_thread(load_config_with_content_stamp)
    # The reconcile below corrects the log FROM this config, so it may run only while
    # the config is both current and faithful. Currency is the digest: without one there
    # is nothing to check the live file against. Faithfulness is
    # ``degraded_sections``: a file that read whole but would not parse leaves field
    # DEFAULTS standing in for what the operator wrote, and correcting the log from
    # those defaults would overwrite good values because of a typo -- the same
    # projection regression this guard exists to prevent. The permission rides on the
    # stamp itself rather than a separate flag, so no row can reach the reconcile
    # without one. Either way the rows below still render from the config in hand; only
    # the correcting write is withheld.
    reconcile_stamp = None if cfg.degraded_sections else config_stamp
    if reconcile_stamp is None:
        logger.warning(
            "the agents config is %s, so this roster read reconciles no member/config; "
            "a read of a whole, parseable config does",
            "degraded to defaults" if cfg.degraded_sections else "unnamed by any content",
        )

    # The roster's redaction chokepoint, shared with ``GET /api/agents`` so the
    # two endpoints cannot drift apart. Function-local for the same reason
    # ``agent_panel`` is below: ``handlers.agents`` reaches back into this
    # package at import time, so a module-level import would close the cycle.
    from kiro_crew.dashboard.handlers.agents import _roster_avatar, _roster_mask

    rows: list[dict] = []
    for name, agent_cfg in cfg.agents.items():
        if not _member_name_is_addressable(name):
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
                # The profile card's own settings. The effort is free text like
                # the model; the approval mode is load-time coerced to a fixed
                # vocabulary ("" = never chosen, which opens the thread in trust).
                "reasoning_effort": _roster_mask(agent_cfg.reasoning_effort),
                "approval_mode": agent_cfg.approval_mode,
                # The dispatched-worker pair, the profile card's other two
                # pickers: what `session_create` pins on a child this crewmate
                # opens without naming a model or effort. Masked like the
                # crewmate's own model; the effort is load-time coerced.
                "worker_model": _roster_mask(agent_cfg.worker_model),
                "worker_reasoning_effort": agent_cfg.worker_reasoning_effort,
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
                # Whether this record was CREATED ON THE DASHBOARD: the crew
                # manager's own origin (`source == "kirocrew"`) AND a member id,
                # which is assigned when its member memory is allocated. With
                # `has_dm_message` below it decides whether the Crewmates page
                # lists the row unasked; anything else (an app's own stamp, a
                # sync-generated row, a legacy kirocrew row) is listed only once
                # its DM thread holds a message, and is otherwise reached
                # through the search box. A boolean, never the id itself.
                "dashboard_created": agent_cfg.source == _SOURCE_KIROCREW
                and _member_is_created(agent_cfg.member_id),
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

    # Last activity, for the roster's most-recent-first ordering. The DM
    # transcript's mtime is the one durable signal that survives restarts and
    # covers live and dormant threads alike. File stats are IO — one thread
    # hop for the whole roster, mirroring the binding reads above.
    # Slot keys whose DM thread holds at least one message (filled by the tail
    # read below; see `has_dm_message`).
    has_message: set[str] = set()

    def _read_transcript_tails() -> dict[str, tuple[float, str, bool, bool]]:
        if state is None or state.conversation_log is None:
            return {}

        def _sanitize(text: str) -> str:
            return redact_external_text(text)

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
            # Whether the thread holds a message at all, for the roster's
            # listing rule. `has_messages` stops at the first non-metadata row
            # (two lines in practice), so this stays one short read per bound
            # row -- the same evidence the crewmate prune uses: a metadata-only
            # transcript (a thread opened, nothing sent) is not a message. A
            # file that cannot be read counts as holding one, so an unreadable
            # thread never hides the row. It runs BEFORE the mtime gate below:
            # a transcript that cannot be stat'd is unreadable, not absent
            # (`has_messages` answers an absent file as empty).
            try:
                if state.conversation_log.has_messages(log_key):
                    has_message.add(row["slot_key"])
            except OSError:
                has_message.add(row["slot_key"])
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
    await asyncio.to_thread(_stamp_welcome_claimed, rows)
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
        # The TRANSCRIPT's epoch, which is what `reconcile_member_preview` below
        # carries into its correcting event. The row's shipped `last_active_ts`
        # is decided after the projections are folded (see the final loop): the
        # crew log is the recency authority, and this value is its floor.
        row["last_active_ts"] = mt
        row["last_message"] = preview
        # A live slot's rows count before they reach the disk: a greeting that
        # has just been appended is a message whether or not it has flushed.
        live = state._slots.get(row["slot_key"]) if (state and row["slot_key"]) else None
        live_rows = getattr(live, "messages", None) if live is not None else None
        row["has_dm_message"] = row["slot_key"] in has_message or (
            isinstance(live_rows, (list, tuple)) and len(live_rows) > 0
        )
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
                        "member slug %r is shared by %d members, so none of them gets a "
                        "projection; recreate the one not owning its thread under a new slug",
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
                        "member slug %r logs %r, so %r gets no projection; recreate "
                        "the member not owning this slug's thread under a new slug",
                        slug,
                        logged_name,
                        row["name"],
                    )
                    out[slug] = {"asOfSeq": _SEQ_UNATTRIBUTABLE, "values": {}}
                    continue
                snap = svc.snapshot(slug)
                agent_cfg = agent_cfgs.get(row["name"])
                appended = False
                # Every read of a whole, parseable config, for every member whose
                # log exists. The reconcile compares the folded roster against the
                # live config and returns before writing when they match, so a
                # config that has not drifted costs one field comparison -- and a
                # config edited by hand rather than through the dashboard reaches
                # the log on the next read with nothing to remember between
                # requests.
                #
                # Two guards gate this WRITE. First OWNERSHIP: serving the
                # placeholder log's projection is a READ, but reconciling this
                # row's config or preview into it is a WRITE into a log whose owner
                # cannot be told from a retired member handed the same slug (the
                # startup sweep in ``eventlog_hooks`` refuses for the same reason),
                # so the write-through runs only for a log with no header yet or one
                # the exact name owns. Second FAITHFULNESS: ``reconcile_stamp`` is
                # None for a config that is not current or not faithful, which is
                # what withholds the write; see where it is decided.
                owned = logged_name is None or logged_name == row["name"]
                if agent_cfg is not None and owned and reconcile_stamp is not None:
                    values = snap.get("values", {}) if isinstance(snap, dict) else {}
                    appended = (
                        eventlog_hooks.reconcile_member_config(
                            slug,
                            row["name"],
                            agent_cfg,
                            values.get("roster", {}),
                            config_stamp=reconcile_stamp,
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
                if owned and row["slot_key"] in preview_authoritative:
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
        # Fresh start's fold boundary rides in here already: `reset_at` folds
        # into the roster projection below, and `_roster_only` ships the whole
        # roster view, so the client reads it off `row["projections"]` (and the
        # same value arrives live over the `member_projection` WS frame) --
        # no second, top-level copy of the field on the row.
        row["projections"] = _redact_projection_value(_roster_only(block))
        row["last_active_ts"] = _recency_for_row(block, row.get("last_active_ts"))

    # The Recent order and the landing read `last_active_ts`: the last message
    # in the crewmate's DM thread, whoever sent it (the user, the crewmate, a
    # scheduled run), as a messages app orders its conversations. Being listed
    # is `has_dm_message`: the thread holds a message.

    return web.json_response({"members": rows})


def _stamp_welcome_claimed(rows: list[dict]) -> None:
    """Mark Mate's row with whether its welcome is owed and its marker claimed.

    The Crewmates page lands on Mate only while its welcome is still coming:
    owed, the marker unclaimed, and the thread holding no row
    (rfc-mate-landing-once). A welcome that was claimed and then failed leaves
    the thread empty for good, and one that was never owed is refused before
    the claim, so the row count alone would land the user on Mate on every
    visit. Only Mate's row carries the fields; a marker that cannot be checked
    leaves ``welcome_claimed`` off and an unreadable owed record reads as not
    owed, both of which the page treats as "seen" (the ordinary landing).
    Blocking IO.
    """
    from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME
    from kiro_crew.dashboard.mate_welcome import greeting_marker_path

    for row in rows:
        if row.get("name") != ASSISTANT_MEMBER_NAME:
            continue
        row["welcome_owed"] = members_mod.welcome_owed(row["slug"], row["name"])
        try:
            row["welcome_claimed"] = greeting_marker_path(row["slug"]).exists()
        except (OSError, ValueError):
            row.pop("welcome_claimed", None)


def _recency_for_row(block: dict, transcript_ts: Any) -> float:
    """The roster row's ``last_active_ts``: the crew log's fold, floored by the
    transcript.

    The **crew log is the authority**, and that is the whole point of reading it
    here. ``RosterProjection`` sets ``last_active_ts`` on every
    ``member/message`` — including a machinery row that carries no preview — so
    it answers "when was this member last active", which is the question the
    Recent sort asks. The transcript's last SPEECH row answers a narrower one:
    when did this member last SAY something. Ordering by that put a crewmate the
    user had just messaged below one whose agent had spoken longer ago, and it
    could not move at all for a member whose log exists but whose transcript
    rows had not been flushed yet. The same fold is what the pushed
    ``member_projection`` frame carries, so taking it here makes the cold row
    and the live frame one value and lets a send reorder the list with no
    roster refetch.

    The transcript is kept as a **floor**, not as a rival: these events are
    appended on a best-effort hook that a queue ceiling may drop, and a member
    whose log has no ``last_active_ts`` at all (no log yet, a shared slug, a
    read the store would not prove — every one of which answers an empty
    ``values``) has only the transcript. Taking the greater of the two can
    therefore lose neither, and because it is monotone a lagging fold can never
    walk a row's recency backwards.
    """
    from kiro_crew.eventlog import types as eventlog_types

    floor = _as_epoch(transcript_ts)
    values = block.get("values") if isinstance(block, dict) else None
    roster = values.get(eventlog_types.PROJ_ROSTER) if isinstance(values, dict) else None
    folded = _as_epoch(roster.get("last_active_ts")) if isinstance(roster, dict) else 0.0
    return max(floor, folded)


def _as_epoch(value: Any) -> float:
    """*value* as epoch seconds, or ``0.0`` for anything that is not a number.

    A projection field is whatever the event carried, so this must survive
    ``None`` (a ``member/message`` with no ``ts``), a bool, and a string,
    without letting any of them become an ordering key. ``bool`` is excluded
    explicitly: it is an ``int`` subclass, so ``True`` would otherwise rank a
    member one second after the epoch instead of not at all.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    if value != value or value in (float("inf"), float("-inf")):  # NaN / infinities
        return 0.0
    return float(value) if value > 0 else 0.0
