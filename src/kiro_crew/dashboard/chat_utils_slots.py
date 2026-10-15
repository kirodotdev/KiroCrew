"""Which session a dashboard chat slot speaks for, and what its memory mode changes.

The keys a slot resolves to -- its slot name for a session, its history and transcript
keys, its effective session key -- and the restricted-session bookkeeping that keeps a
slot's memory mode across a session handover. Also what that mode does to a turn: the
incognito and temporary prompt prefixes, the installed theme's persona on a first turn,
memory consolidation (refused for a restricted session), the open-slot set published
for orphan reaping, and the 409 for a slot still under construction.

Served through :mod:`kiro_crew.dashboard.chat_utils`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from dataclasses import dataclass

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState, _ChatSlot, _normalize_slot_key
from kiro_crew.execution_context import (
    ExecutionContext,
    canonical_memory_mode,
    read_live_session_execution,
    rollback_live_session_tightening,
    stricter_memory_mode,
    tighten_live_session_execution,
)
from kiro_crew.history import is_incognito_transcript, transcript_lock_stems, transcript_stems
from kiro_crew.hooks import safe_read_file
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.sel import sel
from kiro_crew.session_surface import has_dashboard_surface, set_dashboard_surfaced
from kiro_crew.validation import THEME_CONSENT_SHA_RE

logger = logging.getLogger("kiro_crew.dashboard.chat_utils")


def _resettle_restricted_key(state: DashboardState, name: str) -> None:
    """Re-derive ``dashboard:{name}``'s restricted marker from whoever owns ``name`` NOW.

    ``state._restricted_keys`` is keyed by SESSION KEY, not by slot identity, so the
    marker describes whatever object holds the key -- never the object a close happens
    to be carrying. Every exit of a teardown, and every tightening of a live slot,
    owes the one postcondition this function IS: ``dashboard:{name}`` is in the set
    iff the slot currently at ``name`` is restricted, an absent key counting as
    unrestricted.

    Two shapes of exit need it, and they need opposite answers. An ordinary close
    pops the slot for good, so the marker must be DROPPED -- otherwise an incognito
    tab's key stays blocked for every later holder of it. A close that yields the key
    to a concurrent same-key replacement must re-derive from the REPLACEMENT:
    ``_is_restricted_session`` tests the key BEFORE it looks at the slot, so an
    incognito original's leftover marker makes every memory, artifact and mcp-apps
    call on a PERSISTENT replacement answer 403 for as long as that tab lives.

    Re-derived rather than blindly discarded, because a replacement that is itself
    restricted has to KEEP the marker: dropping it is the fail-OPEN direction.
    """
    key = f"dashboard:{name}"
    current = state._slots.get(name)
    if current is not None and current.is_restricted:
        state._restricted_keys.add(key)
    else:
        state._restricted_keys.discard(key)


def tighten_live_slot_memory_mode(
    state: DashboardState,
    name: str,
    memory_mode: object,
    *,
    expected_slot: _ChatSlot | None = None,
) -> bool:
    """Tighten the current slot to *memory_mode* and re-derive its key marker.

    ``expected_slot`` prevents an off-loop save completion for a retired slot
    from changing a successor. The operation is synchronous so callers perform
    the slot write and marker update in one event-loop turn.
    """
    current = state._slots.get(name)
    if current is None or (expected_slot is not None and current is not expected_slot):
        return False
    current_mode = canonical_memory_mode(getattr(current, "memory_mode", "persistent"))
    tightened = stricter_memory_mode(current_mode, canonical_memory_mode(memory_mode))
    changed = tightened != current_mode
    if changed:
        current.memory_mode = tightened
    _resettle_restricted_key(state, name)
    return changed


def apply_pending_slot_memory_mode(state: DashboardState, slot: _ChatSlot) -> bool:
    """Apply a save thread's folded mode to its still-live slot on the event loop.

    The pending value is never cleared: it is monotonic (a save thread only ever
    folds it stricter) and tightening is idempotent, so re-applying it is free,
    while a clear would race a producer that lands between the read and the
    clear and lose its stricter value until the next turn's read-back.
    """
    pending = getattr(slot, "_pending_memory_mode", None)
    if pending is None:
        return False
    return tighten_live_slot_memory_mode(state, slot.key, pending, expected_slot=slot)


def _history_key_for(slot_key: str) -> str:
    """Canonical history key for a dashboard chat slot.

    Takes a SLOT KEY, never a session key: the ``dashboard:`` prefix it adds is
    unconditional, so feeding it a channel session key yields the nonexistent
    ``dashboard:slack:<ts>``. Slots whose conversation lives on a channel must
    go through :func:`effective_session_key` instead.
    """
    if slot_key.startswith("dashboard:"):
        return slot_key
    while slot_key.startswith("dashboard_"):
        slot_key = slot_key[len("dashboard_") :]
    return f"dashboard:{slot_key}"


def dashboard_slot_key(session_key: str) -> str:
    """The dashboard slot name displaying *session_key*, or ``""`` if none.

    Answers "which tab shows this conversation?" — the question that
    ``session_key.startswith("dashboard:")`` plus a prefix strip approximates,
    and gets wrong for a channel-born conversation, whose session key is the
    channel's own even while its tab is open.

    Use it wherever dashboard behaviour is gated on the user having a tab to
    receive it: routing a notice, addressing a card, honouring a dashboard-only
    directive.
    """
    if session_key.startswith("cron:"):
        # A cron-born tab is named ``cron-<job_id>`` (see cron_inject.py), which
        # is NOT the session key folded: ``_normalize_slot_key`` turns
        # ``cron:<id>`` into ``cron_<id>`` (underscore), a slot that has never
        # existed. Consumers that trusted the fold — sub-agent completion
        # injection, compaction/recycle notices — silently missed the open cron
        # tab ("parent slot cron_<id> gone, notification only"), so agent
        # results reached the bell icon but never the conversation.
        #
        # Per-run execution keys carry extra segments — ``cron:<job_id>:<run_id>``
        # for stateless jobs, ``cron:<job_id>:<agent>`` for agent sequences —
        # while the surface registry only ever holds the slot's linked key
        # (``cron:<job_id>``), so the surface gate is checked against both
        # spellings. Whichever matched, the displaying tab is the job's own.
        job_id = session_key.removeprefix("cron:").split(":", 1)[0]
        if not (has_dashboard_surface(session_key) or has_dashboard_surface(f"cron:{job_id}")):
            return ""
        return _normalize_slot_key(f"cron-{job_id}")
    if not has_dashboard_surface(session_key):
        return ""
    return _normalize_slot_key(session_key)


def subagent_event_slot(parent_session_key: str) -> str:
    """The ``slot`` value a per-slot WS event must carry for *parent_session_key*.

    The frontend routes ``subagent_*`` / ``batch_finished`` frames by EXACT
    match between the frame's ``slot`` and the tab's slot key, so a bare
    ``removeprefix("dashboard:")`` breaks every non-dashboard parent: a
    cron-born tab is named ``cron-<id>`` while its session key is
    ``cron:<id>``, and a channel-born tab is named by its transcript stem
    (``slack_<ts>``) while its session key stays ``slack:<ts>``. Frames tagged
    with those raw keys route to a slot no tab reads — the Subagents panel
    showed "No subagents running" for the entire life of every agent spawned
    from such a session.

    :func:`dashboard_slot_key` owns the real mapping; fall back to the old
    prefix-strip when it answers ``""`` (no open tab — nothing routes anywhere
    either way, but keeping the raw key preserves the historical payload for
    external WS consumers and log lines).
    """
    return dashboard_slot_key(parent_session_key) or parent_session_key.removeprefix("dashboard:")


def slot_transcript_key(slot_key: str) -> str:
    """Transcript key for a slot known only by NAME, with no slot object yet.

    Used by the restore paths, which build slots *from* disk and so cannot ask a
    slot for its own key. A channel-born slot's name is its transcript's
    filename stem (``slack_1785370133.085469``), and ``history._safe_key`` folds
    both that stem and the live ``slack:1785370133.085469`` onto the same
    ``.jsonl`` — so the stem already addresses the channel transcript and needs
    no translation.

    This resolves the FILE only. The session key cannot be recovered this way
    (``_safe_key`` folds every ``:`` to ``_``, so ``discord_a_b_c`` is ambiguous)
    and is read back from the persisted ``linked_session_key`` instead.
    """
    if is_channel_session_key(slot_key):
        return slot_key
    return _history_key_for(slot_key)


def slot_history_key(slot: _ChatSlot) -> str:
    """The TRANSCRIPT key for *slot* — the file its conversation is stored in.

    Differs from :func:`effective_session_key` in exactly one case, and that
    case is a real one: a channel-born slot the dashboard could not bind.
    ``surface_channel_session`` deliberately surfaces such a slot **unbound**
    when ``channel_key_for_stem`` cannot resolve its key (the session map was
    pruned, or the thread predates it), because guessing would route replies to
    a session the channel never reads. For that slot ``linked_session_key`` is
    empty, so ``effective_session_key`` falls back to ``_history_key_for``,
    which prefixes ``dashboard:`` and names a file NO restore path reads —
    while every read path resolves the same slot through
    :func:`slot_transcript_key` and gets the channel transcript. Reads and
    writes then address different files: a close flag, a fork, or a backfill
    lands on (or is looked for in) a phantom transcript.

    Resolving the fallback through :func:`slot_transcript_key` puts both back on
    one file. Deliberately does NOT change the slot's SESSION identity — an
    unbound channel slot keeps running under ``dashboard:<name>``, so approval
    policy and restricted-key bookkeeping keyed on that prefix stay intact.

    Gated on the slot's ``channel_origin`` provenance, NOT on its name's shape.
    A name is not provenance: ``POST /api/chat/slots`` accepts a client-supplied
    slot name, so keying off the ``slack_<ts>`` shape alone would let a fresh
    dashboard conversation write itself into an existing thread's transcript and
    merge two unrelated histories. Only the paths that adopt an EXISTING channel
    conversation (``surface_channel_session``, the restore, a History resume)
    set the flag.

    Use this wherever a slot is turned into a transcript path; use
    :func:`effective_session_key` where a slot is turned into a session.
    """
    linked = getattr(slot, "linked_session_key", "")
    if linked:
        return linked
    if getattr(slot, "channel_origin", False):
        return slot_transcript_key(slot.key)
    return _history_key_for(slot.key)


def session_key_for(slot_key: str, linked_session_key: str = "") -> str:
    """:func:`effective_session_key`'s rule, for a slot that does not exist yet.

    The restore paths build slots FROM disk, so a step that has to address the
    session before the build has only the slot NAME and the persisted
    ``linked_session_key`` from the transcript's metadata. This is the same
    relationship :func:`slot_transcript_key` has to the slot's FILE -- except that
    one resolves the file and cannot recover the session key at all, so it is the
    wrong fallback here and a channel-born slot would be addressed as a
    ``dashboard:`` session that does not exist.

    :func:`effective_session_key` delegates to this rather than repeating it: a
    second spelling of the rule would drift, and a restore asking with a key the
    live path never uses matches nothing and fails in SILENCE.
    """
    return linked_session_key or _history_key_for(slot_key)


def effective_session_key(slot: _ChatSlot) -> str:
    """The session key for *slot* — the session its turns run on.

    A channel-born slot carries the real channel key (``slack:<ts>``) in
    ``linked_session_key``, so its turns run on the channel's own session and
    its transcript IS the channel transcript: ``history._safe_key`` folds
    ``slack:<ts>`` and the ``slack_<ts>`` filename stem onto the same
    ``.jsonl``, so one key addresses both the live session and the file the
    channel side appends to. Everything else derives from the slot key.

    Use this anywhere a slot's SESSION is addressed — resolving the session its
    turns run on, mirroring its links. For the slot's TRANSCRIPT use
    :func:`slot_history_key`, which resolves the unbound-channel-slot case onto
    the file the read paths actually use. Reserve :func:`_history_key_for` for
    the cases that genuinely start from a slot key with no slot in hand, and
    :func:`session_key_for` for the ones that have no slot YET.
    """
    return session_key_for(slot.key, getattr(slot, "linked_session_key", "") or "")


def replacement_shares_transcript(state: DashboardState, name: str, slot: _ChatSlot) -> bool:
    """Whether a different slot at *name* writes *slot*'s transcript file."""
    current = state._slots.get(name)
    if current is None or current is slot:
        return False
    return bool(
        set(transcript_stems(slot_history_key(current)))
        & set(transcript_stems(slot_history_key(slot)))
    )


@dataclass(frozen=True)
class ReplacementTightening:
    """State changed before a rows-only hand-over write."""

    replacement: _ChatSlot
    previous_mode: str
    tightened_mode: str
    replacement_key: str
    previous_execution: ExecutionContext | None
    tightened_execution: ExecutionContext | None


def tighten_replacement_to_restricted_original(
    state: DashboardState, name: str, slot: _ChatSlot
) -> ReplacementTightening | None:
    """Tighten a same-transcript replacement before *slot*'s rows are written.

    A rows-only writer can outlive the slot that produced its rows. If another
    slot has taken over the same key and file, the replacement must become at
    least as restricted before those rows reach disk; otherwise live-slot gates
    can derive from private rows while the transcript line is being ratcheted.

    The live carrier compare-and-set runs before the slot or restricted marker
    mutates. If another turn rebinds the carrier, retry once from a fresh read;
    a second conflict propagates with every slot-owned value unchanged. Callers
    therefore receive either a complete tightening plus its rollback witness,
    or no slot/marker mutation to roll back.
    """
    replacement = state._slots.get(name)
    if replacement is None or replacement is slot:
        return None
    if not replacement_shares_transcript(state, name, slot):
        return None
    if not slot.messages and slot._disk_older_count <= 0:
        return None
    current = canonical_memory_mode(getattr(replacement, "memory_mode", "persistent"))
    retained = stricter_memory_mode(
        current, canonical_memory_mode(getattr(slot, "memory_mode", "persistent"))
    )
    if retained == current:
        return None
    # Tighten the replacement's LIVE carrier in place (compare-and-set) rather
    # than clearing it: a clear evicts the only in-memory record of a live
    # member-bound session's identity, so the store-binding check for the rest
    # of that turn sees no execution and skips -- the fail-OPEN direction -- and
    # the next turn's fold has nothing to fold into. A persistent replacement
    # has no live carrier (its record is durable, and the save that lands these
    # rows tightens that record with the line); its next restricted binding
    # withdraws the vouched entry. Only ever tighter: ``with_mode`` never loosens.
    replacement_key = effective_session_key(replacement)
    previous_execution = read_live_session_execution(replacement_key)
    try:
        tightened_execution = tighten_live_session_execution(
            replacement_key, retained, expected=previous_execution
        )
    except UnknownMemoryStore:
        previous_execution = read_live_session_execution(replacement_key)
        tightened_execution = tighten_live_session_execution(
            replacement_key, retained, expected=previous_execution
        )
    replacement.memory_mode = retained
    _resettle_restricted_key(state, name)
    logger.info(
        "Slot %s: the replacement holding this key was tightened from %s to %s because "
        "it shares the transcript of the %s original whose rows are being written",
        name,
        current,
        retained,
        retained,
    )
    return ReplacementTightening(
        replacement=replacement,
        previous_mode=current,
        tightened_mode=retained,
        replacement_key=replacement_key,
        previous_execution=previous_execution,
        tightened_execution=tightened_execution,
    )


async def restore_replacement_if_handover_did_not_land(
    state: DashboardState,
    name: str,
    tightened: ReplacementTightening | None,
    history_key: str,
) -> bool:
    """Undo a pre-write tightening only while its exact witness is still current.

    The durable line is read under the same transcript lock every line writer
    takes, and the generation-guarded carrier rollback runs inside that hold.
    The carrier registry has its own thread-safe lock, so this worker step does
    not touch loop-owned slot state. A writer that tightens the line records the
    live holder's monotonic pending mode after its atomic rewrite and before
    releasing the transcript lock. Therefore a writer ordered before this read
    is visible in ``durable_mode``; one ordered after it is visible in
    ``_pending_memory_mode`` before loop-owned state is loosened. The loop then
    checks that pending witness plus the exact slot, mode and marker without
    another await before mutating.

    An unreadable or busy line cannot prove rollback safe and leaves every live
    restriction in place. Nor can rollback loosen below the locked line: when
    ``stricter(previous_mode, durable_mode)`` is restricted, mode, marker and
    carrier all stay at the attempted tightening.
    """
    if tightened is None:
        return False
    conversation_log = getattr(state, "conversation_log", None)
    if conversation_log is None:
        return False

    def _validate_and_rollback_carrier() -> tuple[str, str, bool, bool]:
        with conversation_log.derivation_hold(transcript_lock_stems(history_key)):
            metadata, readable = conversation_log.get_metadata_status(history_key)
            if not readable:
                return "persistent", "persistent", False, False
            durable_mode = canonical_memory_mode(metadata.get("memory_mode"))
            rollback_mode = stricter_memory_mode(tightened.previous_mode, durable_mode)
            if is_incognito_transcript(rollback_mode):
                return durable_mode, rollback_mode, True, False
            rolled_back = rollback_live_session_tightening(
                tightened.replacement_key,
                tightened.previous_execution,
                expected=tightened.tightened_execution,
            )
            return durable_mode, rollback_mode, True, rolled_back

    try:
        durable_mode, rollback_mode, readable, carrier_rolled_back = await asyncio.to_thread(
            _validate_and_rollback_carrier
        )
    except Exception:
        logger.info(
            "Slot %s: could not lock and verify the failed hand-over line %s; "
            "keeping the replacement restricted",
            name,
            history_key,
            exc_info=True,
        )
        return False
    if not readable:
        logger.info(
            "Slot %s: the privacy line for %s is unreadable after a failed hand-over; "
            "keeping the replacement restricted",
            name,
            history_key,
        )
        return False
    if is_incognito_transcript(rollback_mode):
        logger.info(
            "Slot %s: the locked line for %s requires %s; keeping the replacement at %s",
            name,
            history_key,
            rollback_mode,
            tightened.tightened_mode,
        )
        return False
    if not carrier_rolled_back:
        return False

    # Imported lazily: chat_persistence imports this module. The pending read is
    # the only worker-owned value consulted here; every live-state check and the
    # mutation below remains in this uninterrupted event-loop turn.
    from kiro_crew.dashboard.chat_persistence import pending_slot_memory_mode

    replacement = tightened.replacement
    if state._slots.get(name) is not replacement:
        return False
    if canonical_memory_mode(getattr(replacement, "memory_mode", "persistent")) != (
        tightened.tightened_mode
    ):
        return False
    if f"dashboard:{name}" not in state._restricted_keys:
        return False
    pending_mode = pending_slot_memory_mode(replacement)
    if pending_mode is not None and is_incognito_transcript(pending_mode):
        # The carrier rollback was atomic with the older line snapshot. A writer
        # that committed a tighter line afterwards published this pending witness;
        # restore the carrier tightening while its generation is still the one we
        # rolled back, and leave the loop-owned mode and marker untouched.
        try:
            tighten_live_session_execution(
                tightened.replacement_key,
                stricter_memory_mode(tightened.tightened_mode, pending_mode),
                expected=tightened.previous_execution,
            )
        except UnknownMemoryStore:
            pass
        return False
    replacement.memory_mode = rollback_mode
    _resettle_restricted_key(state, name)
    logger.info(
        "Slot %s: restored the replacement from %s to %s because the failed "
        "hand-over left %s at %s",
        name,
        tightened.tightened_mode,
        rollback_mode,
        history_key,
        durable_mode,
    )
    return True


def reject_if_slot_under_construction(
    state: DashboardState, slot: _ChatSlot
) -> web.Response | None:
    """A 409 for a mutating handler that reached a slot under construction.

    Defense-in-depth over the construction mark. The primary protection for the
    import path is that it RETRACTS the slot from ``_slots`` across its async
    Layer B write/join and durable save (see ``api_chat_slot_import``), so a raw
    ``state._slots.get(name)`` acquirer finds nothing during that tail. This check
    is the belt-and-braces layer: it refuses any mutating handler (regenerate,
    variant-switch, edit-resend, rewind) that resolves a slot still marked under
    construction, keyed on ``_slots_under_construction`` rather than registration,
    so it holds regardless of whether a given construction path retracts. Refused
    the same way ``slot.running`` is. The hydrate loop itself is synchronous, so
    there is no in-loop window; this guards the async finalization tail.

    Returns a response to return as-is, or ``None`` to proceed.
    """
    if slot.key in getattr(state, "_slots_under_construction", ()):
        return web.json_response(
            {"error": "slot is being restored", "code": "slot_under_construction"},
            status=409,
        )
    return None


_INCOGNITO_PREFIX = (
    "[INCOGNITO SESSION] This is an ephemeral session. "
    "Do NOT call learn_add or any memory-writing tool. "
    "learn_remove and cron tools are allowed (active user actions). "
    "If the user asks to save a lesson, respond: "
    "'⚠️ Incognito mode — lessons are not saved in this session.'\n\n"
)

_TEMPORARY_PREFIX = (
    "[TEMPORARY SESSION] This is a blank-slate ephemeral session. "
    "The user has explicitly chosen ephemeral mode. "
    "There are NO memory reads or writes — no preferences, no history, "
    "no lessons, no episodic memory, no projects. "
    "Do NOT reference prior conversations or stored preferences. "
    "Do NOT call learn_add, learn_list, or any memory tool. "
    "Treat this as a completely fresh conversation with no prior context.\n\n"
)


def _apply_incognito_prefix(slot, message: str) -> str:
    """Prepend incognito/temporary instruction for non-persistent sessions."""
    if slot.memory_mode == "temporary":
        return _TEMPORARY_PREFIX + message
    if slot.memory_mode == "incognito":
        return _INCOGNITO_PREFIX + message
    return message


def _maybe_inject_persona(
    message: str,
    color_theme: str,
    is_new: bool,
    theme_consent_sha: str | None = None,
) -> str:
    """Append a theme persona to *message* on the first turn, when an installed
    theme (value ``custom-<slug>``) ships a validated ``persona.md``.

    ALL personas come from installed packs and are gated on **content-bound**
    consent: the caller threads ``theme_consent_sha`` (the sha256 hex the user
    granted in the consent modal, from the frontend), and the pack's persona is
    injected only when it equals sha256 of the persona text actually read from
    disk *now*. A stale hash (e.g. a reinstall rewrote ``persona.md``) or a
    missing hash fails closed, so a never-consented persona can never be
    injected. The legacy boolean ``theme_consent`` request field does not grant
    injection on its own -- consent is content-bound. There is
    no built-in / unconditional persona path."""
    if not is_new:
        return message
    # Installed themes may carry a persona.md (validated at install, §6.5).
    # Persona activation for INSTALLED packs is content-bound: the sha256 the
    # user consented to must equal the hash of the persona text we read now
    # (fail closed on None/mismatch/non-str). This closes the reinstall-swap
    # gap where a client-asserted boolean would inject a never-consented
    # persona after persona.md changed. The THEME_CONSENT_SHA_RE full-match is
    # also a hard guard that only pure 64-hex ASCII ever reaches
    # hmac.compare_digest below (which raises TypeError on non-ASCII).
    if (
        color_theme.startswith("custom-")
        and isinstance(theme_consent_sha, str)
        and THEME_CONSENT_SHA_RE.fullmatch(theme_consent_sha)
    ):
        text = _installed_theme_persona(color_theme[len("custom-") :])
        if text:
            actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if hmac.compare_digest(actual, theme_consent_sha):
                return message + f"\n[THEME PERSONA]\n{text}\n[END THEME PERSONA]\n\n"
    return message


def _installed_theme_persona(slug: str) -> str:
    """Read an installed theme's ``persona.md`` (bounded), or '' if none.

    Defense-in-depth: re-validate the slug (no traversal) even though install
    already did, and cap the length at the install-time bound. A lazy import of
    ``config_dir`` avoids a circular import with the handlers package.
    """
    if not slug or not all(("a" <= c <= "z") or ("0" <= c <= "9") or c == "-" for c in slug):
        return ""
    try:
        from kiro_crew.config.loader import config_dir

        p = config_dir() / "themes" / slug / "persona.md"
        if not p.is_file() or p.is_symlink():
            return ""
        text = safe_read_file(str(p))
        return text[:2000] if text else ""
    except Exception:
        logger.warning("Installed theme persona load failed", exc_info=True)
        return ""


def _maybe_consolidate(state, slot) -> None:
    """Run memory consolidation unless session is restricted."""
    if state.consolidator and not slot.is_restricted:
        state.consolidator.maybe_consolidate(effective_session_key(slot))
    elif state.consolidator and slot.is_restricted:
        sel().log_api_access(
            caller=f"dashboard:{slot.key}",
            operation="consolidate",
            outcome="denied",
            source="dashboard",
            resources="restricted_session_block",
        )


def _sync_dashboard_slots(state: "DashboardState") -> None:
    """Publish the open slots' session keys to SessionManager and the surface registry.

    SessionManager uses the set to reap orphaned sessions; the surface registry
    lets layers with no dashboard import ask whether a session has an open tab
    (see :mod:`kiro_crew.session_surface`). A channel-born slot contributes its
    channel key, which is what both consumers must match against.
    """
    keys = {effective_session_key(s) for s in state._slots.values()}
    state.sessions.set_active_dashboard_slots(keys)
    set_dashboard_surfaced(keys)
