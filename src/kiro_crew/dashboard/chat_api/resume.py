"""Bringing a session back from History: the request-free core, materialisation,
hydration and window reconciliation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from itertools import islice
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        _REOPEN_REREAD_EXTRA_ATTEMPTS,
        _REOPEN_REREAD_RETRY_SECS,
        _RESUME_APP_NOT_FOUND,
        _STRUCTURED_CONTENT_MAX_CHARS,
        _STRUCTURED_CONTENT_PLACEHOLDER,
        DashboardState,
        ResumeOutcome,
        ResumeRefusal,
        _app_claim_refused,
        _app_resume_refusal,
        _app_slot_acquisition_recheck,
        _attach_variants,
        _ChatSlot,
        _collapse_wire_rows,
        _has_validated_effort_marker,
        _history_key_for,
        _live_child_instance,
        _load_restore_cfg,
        _normalize_slot_key,
        _prepare_messages,
        _redact_meta_for_role,
        _restore_model_fields,
        _restored_agent_name,
        _sync_dashboard_slots,
        _unhide_folder,
        app_owns_transcript_meta,
        audit_app_slot_denial,
        carry_provenance,
        channel_slot_name,
        deny_app_slot_session_access,
        durable_row_count,
        effective_session_key,
        is_channel_session_key,
        logger,
        members_mod,
        note_crew_log_class,
        queue_entry_view,
        read_bounded_json,
        redact_credentials,
        redact_exfiltration_urls,
        sel,
        slot_history_key,
        slot_transcript_key,
        time,
    )


async def _reconcile_slot_window(state: DashboardState, slot: "_ChatSlot") -> None:
    """Detect and reconcile stale in-memory window from disk.

    A live slot's window can fall behind disk when messages are written to the
    session file by a path that does not (or cannot) also push into the
    in-memory window — e.g. a concurrent subagent flush, a channel-origin
    append, or a persistence race during heavy traffic.

    This function compares the slot's believed disk coverage
    (``_disk_older_count + len(messages)``) against the actual on-disk message
    count. If disk has grown beyond what the slot accounts for, the missing
    tail is read and appended to the in-memory window, making the next detail
    or resume response self-healing on refresh.

    Safety: skips reconciliation when the slot has unflushed in-memory rows
    beyond what the last save persisted (``len(messages) > _disk_window_len``),
    because a concurrent flush could persist those rows between the
    ``represented`` snapshot and the disk read, leading to a duplicate
    append. Re-validates after the await to guard against appends that landed
    during the disk read.
    """
    if not state.conversation_log:
        return
    # Safety gate: do not reconcile a slot that has in-memory rows the last
    # flush has not yet persisted, or that is mid-rewind, or that has unsaved
    # in-place edits (dirty) — clearing dirty at the end would erase the edit.
    if len(slot.messages) > getattr(slot, "_disk_window_len", 0):
        return
    if getattr(slot, "_pending_rewrite", False):
        return
    if getattr(slot, "_dirty_flag", False):
        return
    history_key = slot_history_key(slot)
    # Deliberately ``_disk_older_count``, NOT ``_disk_older_durable_count``:
    # this reconciliation reasons about the on-disk FILE LAYOUT (how many disk
    # lines the slot represents), so the all-rows counter is the one whose
    # units match. The durable counter exists for absolute message positions
    # (session_control.read_messages), a different measurement.
    represented = (getattr(slot, "_disk_older_count", 0) or 0) + len(slot.messages)
    try:
        disk_msgs = await asyncio.to_thread(
            state.conversation_log.read_messages_chained, history_key
        )
    except Exception:
        logger.warning("reconcile: read_messages_chained failed for %s", history_key, exc_info=True)
        return
    disk_total = len(disk_msgs)
    if disk_total <= represented:
        return
    # Post-await safety: the slot may have received appends (and a flush) while
    # we were reading disk. Re-check and recompute represented to avoid
    # duplicating rows that arrived during the await.
    if len(slot.messages) > getattr(slot, "_disk_window_len", 0):
        return
    if getattr(slot, "_pending_rewrite", False):
        return
    if getattr(slot, "_dirty_flag", False):
        return
    represented = (getattr(slot, "_disk_older_count", 0) or 0) + len(slot.messages)
    if disk_total <= represented:
        return
    # Validate alignment: if transcript rotation shifted offsets, the disk
    # prefix does not match memory — abort to avoid appending wrong rows.
    # The slot's window starts at disk offset _disk_older_count, so we compare
    # the last memory row against its expected position on disk.
    disk_older = getattr(slot, "_disk_older_count", 0) or 0
    if slot.messages and (disk_older + len(slot.messages)) <= len(disk_msgs):
        last_mem = slot.messages[-1]
        expected_pos = disk_older + len(slot.messages) - 1
        disk_at = disk_msgs[expected_pos]
        if last_mem.get("ts", "") != disk_at.get("ts", "") or last_mem.get("role") != disk_at.get(
            "role"
        ):
            logger.info(
                "reconcile: slot %s alignment mismatch at offset %d — skipping "
                "(possible transcript rotation)",
                slot.key,
                expected_pos,
            )
            return
    # Disk has rows the slot does not know about — append the tail.
    fresh = disk_msgs[represented:]
    logger.info(
        "reconcile: slot %s has %d messages in memory + %d older on disk = %d represented, "
        "but disk has %d; appending %d missing rows",
        slot.key,
        len(slot.messages),
        getattr(slot, "_disk_older_count", 0) or 0,
        represented,
        disk_total,
        len(fresh),
    )
    for msg in fresh:
        role = msg.get("role", "assistant")
        cls = msg.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
        content = msg.get("content", "")
        if role != "user":
            content, _ = redact_exfiltration_urls(content)
            content, _ = redact_credentials(content)
        slot.append(
            role,
            content,
            cls,
            ts=msg.get("ts", ""),
            broadcast=False,
            meta=(
                _redact_meta_for_role(role, msg["meta"])
                if isinstance(msg.get("meta"), dict)
                else None
            ),
            mint_mid=False,
        )
        carry_provenance(slot.messages[-1], msg)
        _attach_variants(slot, msg)
    # The appended rows came from the file, so drain the replay frames and
    # mark the window as persisted (not dirty) — the next save must not
    # re-serialize them, and a fork/SSE drain must not treat them as new.
    slot.drain()
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False


def _resume_session_identity(state: DashboardState, history_key: str) -> str:
    """The session a transcript runs under, spelled as a slot spells its own.

    Counterpart to :func:`effective_session_key`, for the caller that holds a
    history key and no slot. A channel-born transcript's session is the
    channel's own, read from the session map because ``history._safe_key``
    folds every ``:`` to ``_`` irreversibly — ``discord_a_b_c`` cannot be
    unfolded by guessing, and a guess would name a session the channel never
    reads. An unmapped channel key falls back to the dashboard spelling, the
    same "leave it unbound" outcome the restore path takes.
    """
    if is_channel_session_key(history_key) and state.sessions:
        real_key = state.sessions.channel_key_for_stem(channel_slot_name(history_key))
        if isinstance(real_key, str) and is_channel_session_key(real_key):
            return real_key
    return _history_key_for(history_key)


async def _live_slot_for_resume(
    state, request_app: str, history_key: str, name: str, caller_label: str = ""
) -> "ResumeOutcome | None":
    """Answer a resume that a live slot already satisfies, else return None.

    Returns the app-isolation 404 refusal when the caller's app does not own
    the slot, the ``member_pin_mismatch`` 409 when a member thread's stored pin is
    not dispatchable (``caller_label`` is what SEL records), otherwise the dedup
    outcome carrying the EXISTING slot. Called on
    BOTH sides of the threaded transcript read: that await lets a concurrent
    resume publish the slot in between, and ``get_or_create_slot`` would then
    hand it back having never applied this ownership gate for the second
    caller's app.
    """
    canonical = _resume_session_identity(state, history_key)
    existing = state._slots.get(name)
    if not existing:
        for slot in state._slots.values():
            if effective_session_key(slot) == canonical:
                existing = slot
                break
    if existing:
        if (
            deny_app_slot_session_access(request_app, existing, existing.key, "slot_resume")
            is not None
        ):
            return ResumeOutcome(refusal=_RESUME_APP_NOT_FOUND)
        if (
            existing.mode == members_mod.DM_SLOT_MODE
            and not members_mod.is_dispatchable_member_name(existing.agent)
        ):
            sel().log_api_access(
                caller=caller_label,
                operation="chat_resume",
                outcome="denied",
                source="member_pin",
                resources=f"slot={existing.key}",
                error="stored member pin is not dispatchable",
            )
            return ResumeOutcome(
                refusal=ResumeRefusal(
                    "this thread's crew name cannot be dispatched", "member_pin_mismatch", 409
                )
            )
        return ResumeOutcome(slot=existing, already_live=True)
    return None


async def _live_slot_resume_payload(state, existing) -> dict:
    """The resume endpoint's dedup body: the already-open slot's live window."""
    await _reconcile_slot_window(state, existing)
    window = _collapse_wire_rows(existing.messages)
    total = len(window)
    recent = window[-200:] if total > 200 else window
    prepared = _prepare_messages(
        recent,
        existing.running,
        live_child=_live_child_instance(state, existing),
        workspace=existing.workspace,
    )
    next_before = (getattr(existing, "_disk_older_count", 0) or 0) + (total - len(recent))
    return {
        "ok": True,
        "key": existing.key,
        "messages": prepared,
        "queue": [queue_entry_view(q) for q in existing._queue],
        "total": total,
        "has_more": next_before > 0,
        "next_before": next_before,
        "memory_mode": existing.memory_mode,
        "mode": existing.mode,
        "surface": existing.mode,
    }


def _normalise_structured_content(value: object) -> str:
    """Return a redacted STRING for a non-string ``content`` value.

    Serialises the value to JSON (so nested dict keys AND values are captured as
    text), then runs the same exfil-URL + credential redaction applied to a
    plain string ``content``. Never raises and never returns a non-string: a
    value that cannot be serialised (or is unexpectedly huge) collapses to a
    fixed placeholder, which is fail-closed (no unredacted bytes escape) and
    keeps the row serialisable by every downstream path.
    """
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        # Must never raise: ``default=str`` invokes ``str()``/``__repr__`` on
        # unknown objects, which a hostile/corrupt row can make raise anything
        # (not just TypeError/ValueError). Any failure collapses to the
        # placeholder -- fail-closed, no unredacted bytes escape.
        return _STRUCTURED_CONTENT_PLACEHOLDER
    # A pathologically large serialisation is dropped rather than run through the
    # GIL-held redactors: the row is malformed either way, and a placeholder is
    # the safe, cheap result.
    if len(text) > _STRUCTURED_CONTENT_MAX_CHARS:
        return _STRUCTURED_CONTENT_PLACEHOLDER
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _redact_history_rows(rows: list[dict], *, window_limit: int | None = None) -> list[dict]:
    """Content-redact non-user rows BEFORE construction, returning new rows.

    Redaction is regex-heavy and holds the GIL, so it must run where a yield
    corrupts nothing -- BEFORE any slot exists -- and it must be bounded by the
    rows that actually enter the live window, not the whole transcript.

    ``window_limit`` mirrors the materialiser's own windowing: only the newest
    ``window_limit`` rows become the live window that is re-serialized on the next
    save, so only those need redacting here. The older prefix is the FROZEN
    prefix -- already redacted at the write boundary, left verbatim on disk, and
    used only for ``_disk_older_count`` accounting (its content is never
    re-serialized) -- so redacting it would be transcript-sized GIL work on the
    loop for bytes that are never rewritten. Resume passes 500, so its cost is
    bounded by the window regardless of transcript length; import passes ``None``
    (every row enters the window and is persisted by its own save), and runs this
    off the loop (``asyncio.to_thread``) so its larger pass yields freely.

    Read-side redaction is DEFENSE-IN-DEPTH, not the primary protection: non-user
    content is redacted at the write boundary (``chat_runner``), so this covers
    rows written before a redaction rule existed, a hand-edited or legacy JSONL,
    or any write path that bypassed the boundary. It must not be dropped.

    User rows are left untouched (matching the write boundary, which redacts
    non-user text). A non-string ``content`` (legacy/corrupt JSONL, or nested
    multi-part content) is NORMALISED to a single redacted string by
    ``_normalise_structured_content`` rather than being passed through
    unredacted or kept as structure: resume is reachable for such rows precisely
    because this pass is defense-in-depth for transcripts a write-time rule never
    covered, so leaving a non-string row unscrubbed would let a credential in
    nested content (a value OR a dict key) reach a broadcaster, and keeping the
    structure would crash the downstream save/display paths that redact
    ``content`` as a string. Row dicts in the redacted window are shallow-copied so the
    caller's input is not mutated; a normalised structured row replaces only the
    top-level ``content`` on the copy, so no shared nested object is touched
    either. Prefix rows are returned as-is.
    """
    if window_limit is None or len(rows) <= window_limit:
        prefix: list[dict] = []
        window = rows
    else:
        prefix = rows[:-window_limit]
        window = rows[-window_limit:]
    out: list[dict] = list(prefix)
    for m in window:
        if m.get("role", "assistant") != "user":
            content = m.get("content", "")
            if isinstance(content, str):
                content, _ = redact_exfiltration_urls(content)
                content, _ = redact_credentials(content)
                m = {**m, "content": content}
            else:
                # Legacy/corrupt or nested multi-part content: normalise it to a
                # single redacted STRING (scrubbing dict keys and values alike)
                # rather than passing the structure through. Keeping the structure
                # would (a) leave dict keys unredacted and (b) crash the downstream
                # save/display paths, which call the string-only redactors on
                # ``content``. See ``_normalise_structured_content``.
                m = {**m, "content": _normalise_structured_content(content)}
        out.append(m)
    return out


def _materialise_slot_from_history(
    state: DashboardState,
    *,
    name: str | None,
    history_key: str,
    meta: dict,
    all_messages: list[dict],
    app: str = "",
    request_title: str = "",
    member_binding: dict | None = None,
    folder_unhidden: bool = True,
    folder_checked_id: str = "",
    window_limit: int | None = 500,
    disk_meta_observed: bool = True,
    broadcast_rows: bool = True,
    mint_missing_mids: bool = False,
) -> _ChatSlot:
    """Build and hydrate a slot from a persisted history snapshot, UNPUBLISHED.

    This is the single materialisation path shared by two callers: the History
    resume endpoint (:func:`api_chat_slot_resume`) and the transfer importer
    (:func:`~kiro_crew.dashboard.session_transfer.api_chat_slot_import`). Install
    lands the transcript, metadata line and Layer B on disk and then routes
    through here, so "install a session" and "pull a stale session up off disk"
    are the SAME operation rather than two slot-construction paths that drift.

    The inputs are exactly the intersection the two callers share: a resolved
    slot key, the transcript key, a validated metadata snapshot, and the loaded
    transcript rows. It reads nothing from the request, and — this is the
    load-bearing constraint of the fold (RFC section 7.1b) — it applies exactly
    the metadata field set resume applies and DOES NOT apply
    ``executor`` / ``instance_id`` / ``remote_slot``. A relay archive therefore
    comes back LOCAL when materialised here, which is the behaviour to
    preserve: rehydrating the archive marker is the startup restore
    path's job (``_rehydrate_slot_from_history`` in chat_persistence), not this
    one. Do not add remote-binding application here "for consistency" — that is
    the prohibited change, and it is pinned by a test.

    ``member_binding`` / ``folder_unhidden`` / ``folder_checked_id`` are verdicts
    the RESUME guards produce; import passes their no-op defaults (no member key,
    no folder to unhide). ``request_title`` is resume's client-supplied name,
    used only when the persisted metadata carries no title; import writes its
    marked title into the metadata line and passes ``""`` here.

    ``window_limit`` and ``disk_meta_observed`` are facts about the DATA, not
    caller switches. ``window_limit`` is how many newest rows to surface as the
    live window given that earlier rows are already durable elsewhere: resume's
    rows are a window onto a longer on-disk transcript (cap 500, the rest frozen
    on disk), and import writes its older rows to the transcript as the frozen
    prefix before its save and passes the same cap. ``None`` surfaces every row,
    for a caller whose rows are all persisted by the save itself.
    ``disk_meta_observed`` is whether this hydration read an existing transcript
    off disk: resume did (True), import synthesised its metadata (False), and it
    gates the delete-won disk-identity bookkeeping that only means something for a
    real disk read.

    ``broadcast_rows`` and ``mint_missing_mids`` are likewise facts about the
    rows. ``broadcast_rows`` is whether hydrating a row should emit a live
    ``chat_message`` SSE event: resume is an interactive open (True), import is a
    silent replay of a bundle onto a slot that stays REGISTERED and under
    construction throughout its (synchronous) hydration and is retracted from
    ``_slots`` only afterwards, for the async finalization tail; import passes
    False (matching the tunnel importer and the ``append`` docstring's
    list of replay callers) — broadcasting would push an under-construction
    slot's peer content to every client and retire live question cards. ``mint_missing_mids``
    is whether these rows need message ids minted: resume's disk rows already
    carry mids (False), import's bundle rows have none, so it passes True or the
    imported rows land permanently id-less and drop out of mid-keyed features.

    Loads NOTHING itself: ``all_messages`` is handed in ALREADY CONTENT-REDACTED
    by the caller's pre-construction pass (``_redact_history_rows``), because the
    two callers answer "where do the bytes come from" differently — resume reads
    the disk transcript under its own TOCTOU ordering, import redacts and lands
    the bundle. It returns the slot built and hydrated but NOT yet published: it
    stays REGISTERED in ``state._slots`` (so a concurrent same-key resume resolves
    it and dedups) and is held under ``begin_slot_construction``, which keeps it
    out of the serialized payload. Each caller owns its own admission accounting
    and must, at its own tail, call ``end_slot_construction(slot.key)`` and
    ``push_slots_update()`` exactly once to make it visible.

    SYNCHRONOUS. Construction contains no ``await``: content redaction (the only
    regex-heavy, GIL-holding, content-scaling work) runs in the caller's
    pre-construction pass, BEFORE any slot exists, where a yield can corrupt
    nothing. The remaining loop is append + provenance + variant attach, measured
    at single-digit milliseconds even at the transfer bounds (5,000 rows / 20M
    chars) — far under the loop-stall watchdog. Being synchronous is what makes
    the slot unobservable mid-build: no acquirer, serializer or persister runs
    between ``begin_slot_construction`` and ``end_slot_construction`` on the loop,
    and construction is loop-affine (threaded persistence snapshots the window on
    the loop first), so no half-hydrated window is ever seen — no guard needed.
    """
    # Create the slot and mark it under construction inside ONE synchronous
    # ``suspend_slots_push`` block. ``get_or_create_slot`` registers the slot and
    # fires a leading-edge ``push_slots_update()`` before it returns; deferring
    # that push until ``begin_slot_construction`` has run means the single
    # coalesced broadcast at block exit serializes the slot as already under
    # construction, so ``serialize_slots`` hides it. Both statements are
    # synchronous — no ``await`` inside — so the process-global suspend counter is
    # never held across a yield (holding it across the hydrate loop's yields would
    # swallow every other slot mutation on the gateway for the duration).
    # Rollback-protected region covers creation, the construction mark, AND the
    # ``suspend_slots_push`` block exit: that exit flushes the deferred
    # ``push_slots_update()``, which can raise on a pre-existing poisoned slot
    # (a non-serializable field) BEFORE hydration begins. If any of it raises,
    # the except path releases the construction count and drops the slot, so a
    # failed materialisation never leaks a hidden-but-registered slot that
    # consumes capacity for the process lifetime. ``slot`` may be unbound if
    # ``get_or_create_slot`` itself raised (e.g. the under-construction create
    # guard), so the rollback is conditional on it existing.
    from kiro_crew.dashboard.slot_persistence import metadata_codec

    slot = None
    try:
        with state.suspend_slots_push():
            # The BINDING is the pin's authority on a member key — not the
            # transcript's own metadata (the guard above verified identity
            # structurally; metadata lives in the same operator-editable file it
            # would otherwise re-pin from). Passing mode="member" is also what
            # admits the key through the constructor's reservation.
            slot = state.get_or_create_slot(
                name,
                **metadata_codec.slot_args(
                    meta,
                    metadata_codec.Resume(
                        member=(
                            None
                            if member_binding is None
                            else (member_binding.get("member", ""), members_mod.DM_SLOT_MODE)
                        ),
                        app=app,
                        history_key=history_key,
                    ),
                ),
            )
            # Keep the slot REGISTERED in ``state._slots`` throughout hydration, and
            # mark it under construction BEFORE the block's coalesced push flushes.
            # Registration is what gives F1: a concurrent resume of the same key
            # resolves THIS slot and hits the idempotency guard, rather than minting a
            # second slot that would receive this one's transcript. Visibility is
            # handled at the payload, not by removing the slot: ``serialize_slots``
            # omits a slot whose key is in ``_slots_under_construction``, so no frame
            # — not even the creation frame deferred to this block's exit — advertises
            # a tab that resolves to nothing. The builder ends construction and pushes
            # once when the session is genuinely ready (for import, after Layer B is
            # joined), and the first frame any client sees shows a hydrated session.
            state.begin_slot_construction(slot.key)
        # The whole fallible body (the block above's exit flush, plus redaction
        # over up to the transfer bounds of peer content and metadata application)
        # runs under this try: a raise would otherwise leak the reservation
        # permanently and inflate the live-slot cap for the process lifetime.
        _hydrate_slot_from_history(
            state,
            slot,
            meta=meta,
            all_messages=all_messages,
            request_title=request_title,
            member_binding=member_binding,
            folder_unhidden=folder_unhidden,
            folder_checked_id=folder_checked_id,
            window_limit=window_limit,
            disk_meta_observed=disk_meta_observed,
            broadcast_rows=broadcast_rows,
            mint_missing_mids=mint_missing_mids,
        )
    except BaseException:
        # ``slot`` is None if ``get_or_create_slot`` itself raised (nothing was
        # reserved). Otherwise release the construction count, drop the slot from
        # ``_slots`` so a half-built registration does not linger, and discard any
        # ``_restricted_keys`` marker hydration added (non-persistent session) —
        # else a later session at this key is left incorrectly blocked from memory
        # operations. Discard is the full undo: the key belongs to this
        # freshly-constructed slot, so nothing else owns its membership.
        if slot is not None:
            state.end_slot_construction(slot.key)
            state._slots.pop(slot.key, None)
            state._restricted_keys.discard(f"dashboard:{slot.key}")
        raise
    # DELIBERATELY does NOT end construction on success -- the caller ends it at
    # its own tail, after its finalization (import awaits Layer B write/join + a
    # durable save with the slot RETRACTED from ``_slots``). This asymmetry is
    # load-bearing, not an oversight: the create-path guard in
    # ``get_or_create_slot`` refuses a mint whose key is in
    # ``_slots_under_construction``, which is the ONLY thing stopping a named
    # create on the predictable minted key from hijacking the retracted slot's
    # Layer B session during that window. Adding ``end_slot_construction`` here
    # would clear the mark before the tail and silently disable that security
    # guard while every happy-path test still passes. Do not.
    return slot


def _hydrate_slot_from_history(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    meta: dict,
    all_messages: list[dict],
    request_title: str = "",
    member_binding: dict | None = None,
    folder_unhidden: bool = True,
    folder_checked_id: str = "",
    window_limit: int | None = 500,
    disk_meta_observed: bool = True,
    broadcast_rows: bool = True,
    mint_missing_mids: bool = False,
) -> None:
    """Apply persisted metadata to *slot* and hydrate its message window.

    The fallible half of :func:`_materialise_slot_from_history`, split out so the
    caller can wrap it in the construction-count try/except above. Sets the same
    metadata field set resume has always applied and never the remote binding
    (RFC 7.1b); see the parameter docs on the public function.
    """
    from kiro_crew.dashboard.slot_persistence import metadata_codec

    # Every field a resume reads from the line goes through the one field table
    # (``slot_persistence.metadata_codec``), from the SNAPSHOT the guards
    # validated: a second get_metadata here would re-read the file, and a write
    # between the two reads would hydrate values the guards never saw
    # (validate-A / hydrate-B). The remote binding is deliberately NOT among
    # them: a session bound to a remote instance comes back LOCAL here, and
    # rehydrating the binding is the startup restore's job.
    applied = metadata_codec.apply(
        state,
        slot,
        meta,
        metadata_codec.Resume(
            member=(
                None
                if member_binding is None
                else (member_binding.get("member", ""), members_mod.DM_SLOT_MODE)
            ),
            request_title=request_title,
            folder_unhidden=folder_unhidden,
            folder_checked_id=folder_checked_id,
            disk_meta_observed=disk_meta_observed,
        ),
    )
    disk_total = len(all_messages)
    # ``window_limit`` is a fact about the data, not a caller switch: how many of
    # the newest rows to surface as the live window, given that any rows before
    # it are ALREADY DURABLE somewhere the next save will not rewrite. Resume's
    # rows are a window onto a longer on-disk transcript, so it caps at 500 and
    # the earlier rows stay frozen on disk. Import caps the same way and writes
    # the rows before the window to the transcript itself, before its save, so
    # ``_disk_older_count`` counts exactly the rows that write put on disk. A
    # caller passing a cap without that write would claim a frozen prefix of
    # rows that were never written.
    if window_limit is None or disk_total <= window_limit:
        messages = all_messages
    else:
        messages = all_messages[-window_limit:]
    # Stable count of messages older than what we loaded into memory
    slot._disk_older_count = max(0, disk_total - len(messages))
    # Durable-only view of the same prefix, recomputed from the on-disk rows —
    # the base absolute message positions are built over. ``islice`` avoids
    # copying the whole prefix on the event loop. See _ChatSlot.__init__.
    slot._disk_older_durable_count = durable_row_count(islice(all_messages, slot._disk_older_count))
    # Synchronous construction: rows arrive already content-redacted from the
    # caller's pre-construction pass (resume redacts its window, import redacts
    # its bundle before any slot exists), so this loop does only append,
    # provenance and variant attach and holds the loop for single-digit
    # milliseconds even at the transfer bounds. No slot is observable mid-build
    # because there is no await between begin- and end-construction, so no
    # acquirer, serializer or persister can see a half-hydrated window. Content
    # arrives pre-redacted; ``_redact_meta_for_role`` still runs here (bounded by
    # meta, not content) and ``_attach_variants`` redacts variant content (bounded
    # by variant count).
    for m in messages:
        role = m.get("role", "assistant")
        # Carry the persisted ``cls`` like the startup restore paths do: a Stop
        # card's discriminator lives only in that JSON string (see
        # ``is_stop_event_row``), so a resumed session dropping it reads the
        # user's deliberate Stop as an interrupted turn. Import rows carry none
        # and fall through to the role default as before.
        cls = m.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
        content = m.get("content", "")
        slot.append(
            role,
            content,
            cls,
            ts=m.get("ts", ""),
            broadcast=broadcast_rows,
            meta=(
                _redact_meta_for_role(role, m["meta"]) if isinstance(m.get("meta"), dict) else None
            ),
            mint_mid=mint_missing_mids,
        )
        # See the equivalent call in _rehydrate_slot_from_history: resume loads
        # the window that the next save re-serializes.
        carry_provenance(slot.messages[-1], m)
        _attach_variants(slot, m)
    slot.drain()
    slot._resumed_count = len(slot.messages)
    # Loaded window is the on-disk window region; older lines (in
    # _disk_older_count above) are the frozen prefix saves never rewrite,
    # so older on-disk turns are preserved.
    slot._disk_window_len = len(slot.messages)
    # After the window boundary, like the startup restore: a local turn the
    # previous process admitted and never tore down becomes the interruption
    # row here, so a session pulled up off disk and one restored at boot agree;
    # then the title refresh mark is re-based against the rows the window holds
    # (a mark at or below the restored count is left alone, so surfacing every
    # row -- import's ``window_limit=None`` -- changes nothing).
    applied.settle(all_messages)


async def api_chat_slot_resume(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/resume — load a history session into a slot.

    Thin wire wrapper over :func:`resume_slot_from_history`: it reads the
    request, hands the core the request-derived facts, and shapes the outcome
    into the responses this endpoint has always returned.
    """
    state: DashboardState = request.app["state"]
    name = _normalize_slot_key(request.match_info["slot"])
    request_app = request.get("app", "")
    if not state.conversation_log:
        return web.json_response(
            {"error": "no conversation log", "code": "no_conversation_log"}, status=400
        )
    if name.casefold().startswith(members_mod.DM_SLOT_KEY_PREFIX) and request_app:
        # Refused by the core too; checked here first so the body is not read
        # for a request that cannot proceed (the entry-gate posture the core
        # documents).
        outcome = await resume_slot_from_history(
            state, name=name, request_app=request_app, caller_label=request.remote or ""
        )
        assert outcome.refusal is not None
        return _resume_refusal_response(outcome.refusal)
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    outcome = await resume_slot_from_history(
        state,
        name=name,
        history_key=body.get("key", name),
        request_app=request_app,
        caller_label=request.remote or "",
        request_title=body.get("title", ""),
    )
    if outcome.refusal is not None:
        return _resume_refusal_response(outcome.refusal)
    assert outcome.slot is not None
    if outcome.already_live:
        return web.json_response(await _live_slot_resume_payload(state, outcome.slot))
    slot, total = outcome.slot, outcome.total
    recent = slot.messages[-200:] if len(slot.messages) > 200 else slot.messages
    return web.json_response(
        {
            "ok": True,
            "key": slot.key,
            # `total` is the effective hydrated length (durable rows plus a
            # recovered interruption row when one was appended), so this is
            # the raw index the next older page starts from.
            "next_before": total - len(recent),
            "messages": _prepare_messages(
                recent,
                slot.running,
                live_child=_live_child_instance(state, slot),
                workspace=slot.workspace,
            ),
            "queue": [queue_entry_view(q) for q in slot._queue],
            "total": total,
            "has_more": total > len(recent),
            "memory_mode": slot.memory_mode,
            "mode": slot.mode,
            "surface": slot.mode,
        }
    )


def _resume_refusal_response(refusal: ResumeRefusal) -> web.Response:
    return web.json_response({"error": refusal.error, "code": refusal.code}, status=refusal.status)


async def resume_slot_from_history(
    state: "DashboardState",
    *,
    name: str,
    history_key: str | None = None,
    request_app: str = "",
    caller_label: str = "",
    request_title: str = "",
    containment: "Callable[[_ChatSlot], Awaitable[ResumeRefusal | None]] | None" = None,
    final_check: "Callable[[_ChatSlot], ResumeRefusal | None] | None" = None,
) -> ResumeOutcome:
    """Load an archived (history) session back into a live slot.

    The request-free core behind ``POST /api/chat/slots/{slot}/resume``; the
    session-control ``revive`` verb reaches the same path so a controlled revive
    and a human click in the History tab share one materialisation, one set of
    guards and one set of refusal codes. ``name`` is the slot key to publish
    under (any spelling ``_normalize_slot_key`` folds), ``history_key`` the
    transcript to load (``None`` means ``name``), ``request_app`` the app token's
    scope when the caller is an app (empty for the dashboard user and for
    session control), and ``caller_label`` what SEL records as the caller.

    ``containment`` is a caller's LAST gate before publish. It runs once the slot
    is hydrated -- so it reads the fields the slot actually carries, not a
    metadata snapshot from before the transcript read -- and before the slot is
    published, with the slot RETRACTED from ``state._slots`` and its construction
    mark held for the duration (the import path's posture for an awaited tail):
    nothing resolves it, ``serialize_slots`` never shows it, and a named create on
    its key is refused by the construction guard. A refusal it returns discards
    the built slot the way a failed construction is discarded and comes back as
    ``ResumeOutcome.refusal``. The reopen write (clearing ``closed``) runs in the
    same retracted window, after the hook when there is one, with or without a
    hook: it is awaited and verified BEFORE the publish, a refusal before it has
    nothing durable to undo, one after it puts the marker back, and a clear that
    cannot land refuses (``reopen_failed``) rather than publishing a tab that
    would not restore. The hook is an idempotent pre-publish
    check (it may update in-memory bookkeeping such as ``_created_by`` and
    ``_revived_by`` on the built slot, never durable state) and runs TWICE: once before the deferred clear, and once after it as the last
    awaiting act, because the clear and its verification read are awaits during
    which a store-recorded channel binding could land, and ``final_check`` may not
    read the store. The existence and ``created_at`` identity barrier is re-run
    after each of those awaits, the last time synchronously, so a delete or a
    delete-and-recreate inside the window is refused rather than published over
    the replacement, and the marker rollback only ever targets the transcript
    this resume read. The folder un-hide keeps the
    hook-less path's place, before construction: a refused resume can leave a
    folder visible, as a click refused at the member barrier already can.
    ``final_check`` is the SYNCHRONOUS last word, run after the last await and
    immediately before the publish, for the hook's store-free answers (slot
    fields, caps); a refusal there restores the marker the deferred clear just
    dropped. Nothing is published on any refusal. A human click passes neither.

    Refusals come back as :class:`ResumeOutcome.refusal` rather than being
    raised, because the wire wrapper reproduces each one's historical body and
    status and a session-control caller maps them onto its own error class.
    """
    # Fold the requested name with the function that keys the slot table, so
    # every spelling of one slot resolves to that slot: a caller may hold a
    # filename stem, a session key (a notification deep link carries the
    # conversation's own ``slack:<ts>``), or a display-style name. A partial
    # fold leaves the lookup below missing an open tab and falls through to the
    # create path, which re-reads the transcript into the slot it should have
    # returned.
    name = _normalize_slot_key(name)
    if history_key is None:
        history_key = name
    if request_app and history_key == name:
        # An app naming no key, or the bare slot name, means the slot's own
        # transcript. Its ownership can only be read from that canonical key: the
        # bare name has no transcript of its own, so it would read as nobody's.
        history_key = _history_key_for(name)
    if not state.conversation_log:
        return ResumeOutcome(
            refusal=ResumeRefusal("no conversation log", "no_conversation_log", 400)
        )
    # App tokens get the uniform isolation 404 for member-* keys AT ENTRY —
    # before the live-slot probe, the folder unhide, the closed-flag clear, or
    # any transcript read. An app can never own a member slot; running any of
    # those side effects first would let an unauthorized caller mutate the
    # member thread's history state even while the resume itself is refused.
    if name.casefold().startswith(members_mod.DM_SLOT_KEY_PREFIX) and request_app:
        sel().log_api_access(
            caller=request_app,
            operation="chat_resume",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={name}",
            error="app cannot access member slots",
        )
        return ResumeOutcome(refusal=_RESUME_APP_NOT_FOUND)

    # If slot already exists (active session), just return it — no duplicate.
    # Check both by slot name AND by canonical session key to prevent two
    # slots sharing the same kiro-cli process.
    #
    # INVARIANT: both sides of this comparison derive identity through the same
    # rule. A slot answers with ``effective_session_key``, which for a
    # channel-born tab is the channel's own key — so the requested key resolves
    # the same way, via the session map. Two rules in play and a channel
    # transcript matches nothing here: it gets a second tab, so one conversation
    # shows as two sidebar rows backed by two kiro-cli processes.
    resume_outcome = await _live_slot_for_resume(
        state, request_app, history_key, name, caller_label
    )
    if resume_outcome is not None:
        return resume_outcome
    # No live slot: an app's resume builds a NEW slot of its own over this
    # transcript, so it is admitted only to one the app already owns (both the
    # transcript it reads and the one its key would save to). Before any side
    # effect below.
    if await _app_claim_refused(
        state,
        request_app,
        "chat_resume",
        (history_key, _history_key_for(name), slot_transcript_key(name)),
    ):
        return ResumeOutcome(refusal=ResumeRefusal("not found", "slot_not_found", 404))

    # Boundary for the compare-and-clear below, captured BEFORE the metadata read
    # it is compared against. Everything from here to the ``clear_closed`` call is
    # a window in which this session can be closed by somebody else -- including
    # deleted, recreated and closed again -- and the clear must not erase a
    # ``closed`` that landed inside it. Anchored to this read specifically, since
    # ``meta["closed"]`` is the snapshot the clear acts on.
    resume_started_at = time.time()
    # Read the history metadata BEFORE creating the slot: this endpoint RESUMES a
    # persisted conversation, so its origin is a property of that conversation,
    # not of whoever is resuming it. Deriving it from the request would label a
    # resumed CRON slot as USER, and `slots:user` would then hand the dashboard
    # user's private cron output to any app holding that scope. An absent
    # persisted origin stays empty (get_or_create_slot then derives APP for an
    # app token, otherwise leaves it untagged, which is invisible to cross-slot
    # scopes) rather than claiming USER on a conversation we cannot attribute.
    meta = state.conversation_log.get_metadata(history_key)

    # An app may open only a transcript it owns, and only under a name it may
    # hold. A persisted conversation has no live slot for the per-slot checkpoint
    # to judge, so the app recorded on the transcript is its owner record.
    # Identity is positive: a transcript with no recorded app is the person's,
    # never an app's. Checked before every mutation below and before the
    # member-mode 409, so the refusal is the same 404 a missing transcript gets
    # and reveals nothing about the session.
    if request_app:
        refusal = await _app_resume_refusal(state, request_app, name, history_key, meta)
        if refusal is not None:
            return ResumeOutcome(refusal=refusal)

    # ── Member-thread EARLY refusal, before any persistent mutation ────────
    # ``_unhide_folder`` and ``clear_closed`` below write durable state. A
    # resume the member guard is going to 409 anyway must not leave those
    # side effects behind (a closed member thread silently reopened, its
    # folder unhidden). This early check refuses the doomed request first;
    # the full guard further down re-validates right before the slot is
    # created and remains the authoritative TOCTOU barrier — this one only
    # keeps rejected resumes side-effect-free.
    if name.casefold().startswith(members_mod.DM_SLOT_KEY_PREFIX):
        # casefold to MATCH; slice the ORIGINAL bytes. A mixed-case name
        # yields a slug with uppercase, which reads as unbound (slugs are
        # validated lowercase) -> the guard 409s. Fail-closed, and the
        # constructor's own casefolded reservation is never reached with an
        # uncaught ValueError.
        _early_binding = await asyncio.to_thread(members_mod.read_dm_binding_for_slot, name)
        if _early_binding is not None and not members_mod.is_dispatchable_member_name(
            _early_binding.get("member")
        ):
            sel().log_api_access(
                caller=caller_label,
                operation="chat_resume",
                outcome="denied",
                source="member_pin",
                resources=f"slot={name} key={history_key}",
                error="stored member pin is not dispatchable",
            )
            return ResumeOutcome(
                refusal=ResumeRefusal(
                    "this thread's crew name cannot be dispatched", "member_pin_mismatch", 409
                )
            )
        if _early_binding is None or history_key != _history_key_for(name):
            sel().log_api_access(
                caller=caller_label,
                operation="chat_resume",
                outcome="denied",
                source="member_pin",
                resources=f"slot={name} key={history_key}",
                error="member binding missing or foreign history key",
            )
            return ResumeOutcome(
                refusal=ResumeRefusal(
                    "member thread agent is pinned", "member_thread_agent_pinned", 409
                )
            )
    elif str(meta.get("mode", "")) == members_mod.DM_SLOT_MODE:
        # Same early refusal for the mirror case: a member transcript may not
        # ride onto an ordinary key, and that rejection must also precede the
        # mutations. The late twin re-checks against the post-await snapshot.
        sel().log_api_access(
            caller=caller_label,
            operation="chat_resume",
            outcome="denied",
            source="member_pin",
            resources=f"slot={name} key={history_key}",
            error="member transcript on an ordinary key",
        )
        return ResumeOutcome(
            refusal=ResumeRefusal(
                "a member thread can only be resumed on its own member slot",
                "member_mode_key_mismatch",
                409,
            )
        )

    # Read the transcript BEFORE publishing the slot: this await would otherwise
    # expose an empty slot by name, and a concurrent append would land ahead of it.
    all_messages = await asyncio.to_thread(
        state.conversation_log.read_messages_chained, history_key
    )

    # Every remaining await in this handler runs BEFORE the slot is published: one
    # after it would expose an empty slot, and a concurrent append there is ordered
    # ahead of the history the hydrate loop restores further down. They are placed
    # ahead of the re-check too, so nothing can suspend between it and the publish.
    folder_unhidden = True
    # Record WHICH folder that verdict is about. Hoisting this call above the
    # publish is what keeps the window closed, but it also moved it onto the
    # PRE-read ``meta``, while the hydrate below binds ``folder_id`` from the
    # snapshot re-read after the last await. A channel reconciliation landing
    # during the transcript read makes those two ids differ, and an existence
    # verdict earned by the OLD folder says nothing about the NEW one.
    folder_checked_id = ""
    if meta.get("folder_id"):
        folder_checked_id = meta["folder_id"]
        folder_unhidden = await _unhide_folder(state, folder_checked_id)
    cleared_closed: bool = False
    cleared_closed_at: Any = meta.get("closed_at")
    # The reopen write (clearing ``closed``) is DEFERRED to the reserved window
    # after construction, with or without a hook: every refusal before it has no
    # durable change to undo, the write is awaited and verified BEFORE the
    # publish, and a clear that cannot land refuses the resume instead of
    # publishing a tab that would not restore.
    defer_clear = bool(meta.get("closed"))
    # The slot is built retracted under its construction mark, and published only
    # after the awaited tail (the hook, the reopen write) has passed.
    reserved = containment is not None or defer_clear
    if name in getattr(state, "_slots_under_construction", ()):
        # Another resume of this key is between hydration and publish. The same
        # coded conflict is answered again at construction (the window can open
        # after this read); asking here first means the common case refuses
        # before any of the reads below. An app gets the uniform 404 (see
        # _app_resume_refusal).
        if request_app:
            return ResumeOutcome(refusal=_RESUME_APP_NOT_FOUND)
        return ResumeOutcome(
            refusal=ResumeRefusal(
                "this session is being resumed elsewhere; try again", "resume_in_progress", 409
            )
        )
    restored_agent = await asyncio.to_thread(
        _restored_agent_name, _resume_session_identity(state, history_key), meta
    )
    # The model restore needs the provider (config.json) and the effort marker
    # (a file under the config dir). Both are disk reads, so they are taken here,
    # off the loop, before the synchronous construction below.
    _prefetched_effort = meta.get("reasoning_effort")
    restore_cfg, effort_marker = await asyncio.to_thread(
        lambda: (_load_restore_cfg(), _has_validated_effort_marker(_prefetched_effort))
    )
    # Re-check after the await: a concurrent resume can publish the slot while we
    # are suspended, and the publish below would skip the ownership gate above.
    resume_outcome = await _live_slot_for_resume(
        state, request_app, history_key, name, caller_label
    )
    if resume_outcome is not None:
        return resume_outcome

    destination_refusal = None
    if request_app:
        # Reserve the publish name through its off-loop ownership read. Source
        # identity and ownership checks follow it without yielding on success.
        # A refusal reaches the app barrier's closed-marker rollback below.
        destination_refusal = await _app_slot_acquisition_recheck(
            state, request_app, name, None, "slot_resume"
        )
        if destination_refusal is None:
            # The recheck reserves only the publish name. A concurrent resume of
            # the same transcript under another name can publish during its read,
            # so the session-key dedup runs again before the synchronous publish.
            resume_outcome = await _live_slot_for_resume(
                state, request_app, history_key, name, caller_label
            )
            if resume_outcome is not None:
                return resume_outcome

    # Re-check DELETION in the same window and for the same reason. The transcript
    # loaded above can be permanently deleted while we are suspended, and
    # ``delete_session`` leaves NO tombstone -- its own docstring notes that once
    # the delete releases the lock "a concurrent writer can recreate the session".
    # So publishing a slot from content we already hold rewrites, on its next
    # flush, a file the user permanently deleted.
    #
    # ``get_metadata_status``, never ``get_metadata``: the latter returns ``{}`` for
    # BOTH "deleted" and "unreadable", and reading an unreadable metadata line as a
    # deletion would discard a LIVE session -- its docstring says to prefer this
    # wherever an empty result triggers something destructive.
    #
    # Synchronous, like the ``get_metadata`` above it, so this adds no suspension
    # point between the re-checks and the publish -- the property the comment on
    # the awaits above depends on.
    post_read_meta, meta_readable = state.conversation_log.get_metadata_status(history_key)
    # Did this session exist when we looked? Both re-checks below need that, and
    # ``all_messages`` alone is the wrong witness: a METADATA-ONLY session -- a
    # metadata line with no messages, which ``update_metadata`` creates on upsert --
    # has an empty transcript, so gating on it silently disabled both guards for
    # exactly the sessions least able to survive it. The pre-read ``meta`` is the
    # right witness, and it costs nothing: it is already read synchronously above,
    # so consulting it adds no suspension point.
    #
    # A UNION rather than a swap, so the witness is never narrower than it was: a
    # transcript we managed to read is also evidence of prior existence, even where
    # the metadata line was unreadable at pre-read time and ``meta`` came back empty.
    #
    # This is ONE term used by BOTH arms deliberately. Duplicating the predicate
    # at each site is how an empty-transcript hole reaches two sites at once; a
    # single binding means a future change cannot fix one and leave the other
    # behind.
    session_existed = bool(meta or all_messages)
    # Resuming a session that never existed stays untouched: no metadata and no
    # transcript leaves this false, so an absent key is treated as a new
    # conversation rather than a deletion. A legitimately empty session that is
    # still PRESENT is protected by the other terms instead -- ``post_read_meta``
    # is non-empty below, and the identity arm needs two DIFFERING stamps.
    if destination_refusal is None and meta_readable and not post_read_meta and session_existed:
        logger.info(
            "chat resume: session %s was deleted during the transcript read; "
            "refusing to publish a slot that would resurrect it",
            history_key,
        )
        return ResumeOutcome(
            refusal=ResumeRefusal(
                "the session was deleted while it was being resumed", "resume_session_deleted", 409
            )
        )
    # IDENTITY, not merely existence. The arm above fires on metadata being
    # ABSENT, which the delete-then-RECREATE interleaving does not produce: the
    # delete leaves no tombstone, so a writer that recreates the session inside
    # this same window leaves ``post_read_meta`` a NON-EMPTY dict belonging to the
    # NEW conversation. Existence reads that as "still here" and publishes a slot
    # holding the OLD transcript, whose next flush overwrites a session the user
    # is actively using -- the opposite error to the one above, and worse, because
    # the data destroyed is live rather than already-deleted.
    #
    # ``created_at`` is the discriminator because every path that MINTS a metadata
    # line stamps it (``append`` when the file does not exist,
    # ``_update_metadata_locked`` when the line is missing) while
    # ``_rewrite_session_locked`` carries it through verbatim. So a rewrite,
    # compaction or rename does NOT move it and is not refused here; a differing
    # value means this is a different file than the one we read.
    #
    # ABSENT on either side means we cannot compare, and we FALL THROUGH rather
    # than refuse. Refusing would reject legitimate resumes of any transcript
    # whose metadata predates the field -- a visible break for real users -- to
    # close a narrow race. It also neuters the one false positive available here:
    # ``_rewrite_session_locked`` mints a fresh ``created_at`` only when the
    # original lacked one, which is exactly the case this skips. The residual is
    # that a recreate of such a transcript stays undetected; the durable fix for
    # that is a tombstone in ``history.delete_session``, which is out of scope.
    pre_identity = meta.get("created_at")
    post_identity = post_read_meta.get("created_at")
    if (
        destination_refusal is None
        and meta_readable
        and post_read_meta
        and session_existed
        and pre_identity
        and post_identity
        and pre_identity != post_identity
    ):
        logger.info(
            "chat resume: session %s was deleted and recreated during the "
            "transcript read; refusing to publish a slot whose flush would "
            "overwrite the replacement",
            history_key,
        )
        # Same code as the plain-delete arm: from the resumer's point of view the
        # session it asked for was deleted. That it was then recreated does not
        # change what happened to the conversation being resumed, and one code
        # keeps the client contract single-valued.
        return ResumeOutcome(
            refusal=ResumeRefusal(
                "the session was deleted while it was being resumed", "resume_session_deleted", 409
            )
        )

    # ── Member-thread pin guard ─────────────────────────────────────────────
    # Member DM slots are born and re-agented ONLY through
    # POST /api/members/{slug}/thread. The check is STRUCTURAL, not a metadata
    # shape check: a member key may only resume its own canonical history
    # (history metadata lives in the same operator-editable JSONL as the
    # fields it would restore, so matching on meta.agent/meta.mode would let
    # two edited keys put an arbitrary transcript under the member's name).
    # And mode="member" may not ride a transcript onto an ordinary key (an
    # invisible orphan: absent from Sessions AND from the roster). Checked
    # BEFORE the slot exists so a refusal cannot strand a fresh non-member
    # slot on the member key, which would 409 the real thread opener forever.
    #
    # ORDERING: the binding await runs FIRST, and the metadata snapshot is
    # taken synchronously after it — the last operation before the slot is
    # created. Reading metadata before the await would reopen the
    # validated-to-publish race: a delete/recreate landing during the await
    # would hydrate the OLD snapshot against the replacement transcript, and
    # a later dirty flush would overwrite the replacement.
    _member_binding: dict | None = None
    if name.casefold().startswith(members_mod.DM_SLOT_KEY_PREFIX):
        # Same casefold-to-match / original-bytes-slice as the early guard.
        _member_binding = await asyncio.to_thread(members_mod.read_dm_binding_for_slot, name)
        if _member_binding is not None and not members_mod.is_dispatchable_member_name(
            _member_binding.get("member")
        ):
            sel().log_api_access(
                caller=caller_label,
                operation="chat_resume",
                outcome="denied",
                source="member_pin",
                resources=f"slot={name} key={history_key}",
                error="stored member pin is not dispatchable",
            )
            return ResumeOutcome(
                refusal=ResumeRefusal(
                    "this thread's crew name cannot be dispatched", "member_pin_mismatch", 409
                )
            )
        # Re-check the LIVE slot after this await: it is the one suspension
        # point between the earlier ownership re-checks and the publish
        # below. A concurrent resume that published during it would otherwise
        # go unseen — this request would then get_or_create the EXISTING
        # slot and hydrate the disk transcript onto it a second time,
        # persisting duplicated history on the next flush.
        resume_outcome = await _live_slot_for_resume(
            state, request_app, history_key, name, caller_label
        )
        if resume_outcome is not None:
            return resume_outcome
        if _member_binding is None or history_key != _history_key_for(name):
            sel().log_api_access(
                caller=caller_label,
                operation="chat_resume",
                outcome="denied",
                source="member_pin",
                resources=f"slot={name} key={history_key}",
                error="member binding missing or foreign history key (late barrier)",
            )
            return ResumeOutcome(
                refusal=ResumeRefusal(
                    "member thread agent is pinned", "member_thread_agent_pinned", 409
                )
            )
    post_read_meta = state.conversation_log.get_metadata(history_key)
    if _member_binding is not None and post_read_meta != meta:
        # Identity barrier for the window the binding await opened: the
        # transcript was read at `meta`-time (with `all_messages`), and this
        # re-read runs after the await. A delete/recreate landing in between
        # would pair the OLD messages with the REPLACEMENT metadata, and the
        # next dirty flush would overwrite the replacement transcript with
        # them. Equal snapshots bracket the whole window — the pairing is
        # consistent; any drift refuses, and re-opening reads fresh.
        sel().log_api_access(
            caller=caller_label,
            operation="chat_resume",
            outcome="denied",
            source="member_pin",
            resources=f"slot={name} key={history_key}",
            error="metadata drifted across the binding read",
        )
        return ResumeOutcome(
            refusal=ResumeRefusal(
                "this thread changed while resuming; open it again", "member_resume_conflict", 409
            )
        )

    # mypy defers this function (it is checked before the inferred attribute types of
    # ``DashboardState``), and a deferred function's closures do not inherit the
    # narrowing of ``history_key`` to ``str`` above, hence the two ignores below.
    async def _restore_closed_marker() -> bool:
        # Put the ``closed`` marker back and CONFIRM it is there. The
        # compare-and-set answers False for two states that are both fine
        # (somebody re-closed the session, or its file is gone), so the
        # verdict is the re-read, not the write's return value: one retry
        # on a raise, then the marker must be readable on disk.
        #
        # The marker belongs to the transcript this resume READ: a delete and
        # same-key recreate landing inside the clear's own worker call leaves
        # a replacement whose line carries a different ``created_at``, and
        # archiving that would put a marker the user never set onto a live
        # conversation. Both the write's guard and the verdict compare the
        # stamp; a stamp that moved means there is nothing of ours to restore.
        log = state.conversation_log
        if log is None:
            return True

        def _same_transcript(current: dict) -> bool:
            # An app resume read a transcript that app owns; a line now owned by
            # someone else is not that transcript, whatever its stamp says.
            if request_app and not app_owns_transcript_meta(current, request_app):
                return False
            stamp = current.get("created_at")
            return not pre_identity or not stamp or stamp == pre_identity

        fields = {"closed": True, "closed_at": cleared_closed_at}
        for attempt in range(2):
            try:
                await asyncio.to_thread(
                    log.update_metadata_if,
                    history_key,  # type: ignore[arg-type]
                    fields,
                    lambda current: "closed" not in current and _same_transcript(current),
                    require_existing=True,
                )
                break
            except Exception:
                if attempt == 0:
                    logger.warning(
                        "restoring the closed marker of %s failed once; retrying",
                        history_key,
                        exc_info=True,
                    )
        try:
            current, readable = await asyncio.to_thread(
                log.get_metadata_status, history_key  # type: ignore[arg-type]
            )
        except Exception:
            return False
        if not readable:
            return False
        # An absent line means the session was deleted meanwhile, and a moved
        # stamp means it was replaced: in both there is nothing of ours to
        # restore, and nothing of ours that would reopen at the next start.
        return not current or not _same_transcript(current) or "closed" in current

    meta = post_read_meta
    # Late twin of the transcript-ownership refusal above, on the post-await
    # snapshot: a delete and same-key recreate inside the reads replaces the
    # metadata line, so ownership the old snapshot earned says nothing about the
    # transcript about to be adopted. Nothing durable has changed yet (the
    # reopen write runs in the reserved window after construction), so there is
    # no ``closed`` marker to put back.
    if request_app and (
        destination_refusal is not None or not app_owns_transcript_meta(meta, request_app)
    ):
        if destination_refusal is None:
            audit_app_slot_denial(
                request_app, "slot_resume", name, "app does not own this transcript (late barrier)"
            )
        return ResumeOutcome(refusal=_RESUME_APP_NOT_FOUND)
    if _member_binding is None and str(meta.get("mode", "")) == members_mod.DM_SLOT_MODE:
        sel().log_api_access(
            caller=caller_label,
            operation="chat_resume",
            outcome="denied",
            source="member_pin",
            resources=f"slot={name} key={history_key}",
            error="member transcript on an ordinary key (late barrier)",
        )
        return ResumeOutcome(
            refusal=ResumeRefusal(
                "a member thread can only be resumed on its own member slot",
                "member_mode_key_mismatch",
                409,
            )
        )

    # Redact only the newest 500 rows -- the live window the next save
    # re-serializes -- before construction. The older frozen prefix is already
    # redacted on disk and only counted, never rewritten, so redacting it would
    # put transcript-sized GIL regex on the loop for bytes that never change.
    # Bounded by the window, so a long transcript costs the same as a short one.
    all_messages = _redact_history_rows(all_messages, window_limit=500)

    if name in getattr(state, "_slots_under_construction", ()):
        # Another resume of this key is between hydration and publish with the
        # slot retracted (the containment hook's window; the import path's tail
        # has the same shape). ``get_or_create_slot`` would refuse the mint with
        # a bare ``ValueError``; answer with a coded conflict instead, since a
        # retry a moment later finds the key either published or free.
        #
        # Nothing durable has changed yet: the reopen write runs in the reserved
        # window after construction, so the refusal leaves the ``closed`` marker
        # as this resume found it. An app gets the uniform 404 (see
        # _app_resume_refusal).
        if request_app:
            return ResumeOutcome(refusal=_RESUME_APP_NOT_FOUND)
        return ResumeOutcome(
            refusal=ResumeRefusal(
                "this session is being resumed elsewhere; try again", "resume_in_progress", 409
            )
        )
    # A delete still IN FLIGHT is invisible to the existence and identity
    # barrier above: it holds the transcript lock across its whole transaction
    # (index drop, attachment and reply-thread staging, the unlink) and that
    # barrier's read takes no lock, so the file reads as present until the very
    # end. Publishing here leaves a slot the delete does not know about -- a
    # delete handler removes only the slot it claimed before its first await --
    # so the deleted conversation stays open as a tab. Checked HERE, the last
    # synchronous point before construction, because every await above (the
    # member-binding read included) is a window a delete can start in.
    # ``delete_in_flight`` is in-process bookkeeping with no I/O. The reserved
    # path re-runs the same check in ``_identity_refusal`` after its own awaits.
    #
    # The resume cannot know yet whether the delete will go through (a bulk
    # clear skips a pinned row), so it refuses with a retryable
    # ``resume_conflict``: a retry after the delete ends either finds the
    # session gone or opens it. Nothing durable has changed yet (the reopen
    # write comes after construction), so the session is left as it was found.
    if session_existed and state.conversation_log.delete_in_flight(history_key):
        logger.info(
            "chat resume: session %s is being deleted; refusing the resume",
            history_key,
        )
        return ResumeOutcome(
            refusal=ResumeRefusal(
                "this session is being deleted; try again", "resume_conflict", 409
            )
        )
    slot = _materialise_slot_from_history(
        state,
        name=name,
        history_key=history_key,
        meta=meta,
        all_messages=all_messages,
        app=request_app,
        request_title=request_title,
        member_binding=_member_binding,
        folder_unhidden=folder_unhidden,
        folder_checked_id=folder_checked_id,
        # A reserved build must not reach any client before its tail has
        # passed: a refused build is discarded, and frames already pushed for it
        # would describe a session that never appears.
        broadcast_rows=not reserved,
    )
    if _member_binding is None:
        # Restore the protected choice read before construction, not the
        # editable transcript's provisional agent name.
        slot.agent = restored_agent
    # Same model + effort restore as the restart loaders. Without it the resumed
    # slot runs on the default model, and its next save writes ``model: ""``
    # over the user's pick. Resume only: import deliberately does not carry a
    # model (see session_transfer). The marker was read for the pre-await
    # snapshot, so it only vouches for an unchanged effort value.
    _restore_model_fields(
        slot,
        meta,
        cfg=restore_cfg,
        effort_marker=effort_marker and meta.get("reasoning_effort") == _prefetched_effort,
    )
    if containment is not None and not getattr(slot, "_app", "") and post_read_meta.get("app"):
        # The hook reads the slot's own fields; ``_app`` comes from the request
        # (none here), so the line's app scope is restored onto the built slot
        # the way the restart path restores it, and the hook's app check is a
        # real read rather than a constant. From the fresh re-read, as below.
        slot._app = str(post_read_meta["app"])
    if containment is not None:
        # Same for the channel link: the resume core does not hydrate
        # ``linked_session_key`` (the restart path and the History surfacing do),
        # so the hook's link check would read an empty field whatever the line
        # says. Restored from the FRESH re-read (``post_read_meta``), not the
        # pre-transcript snapshot, so a link written to the line inside the read
        # window is what the hook sees; a channel-born key marks the origin the
        # way the surfacing path does.
        fresh_link = str(post_read_meta.get("linked_session_key") or "")
        if fresh_link and not getattr(slot, "linked_session_key", ""):
            slot.linked_session_key = fresh_link
            # Beside the assignment, as every link-setting site records it
            # (``test_crew_log_class_recorder``). The built slot has no open
            # log yet, so this is the in-memory restriction mark; a hook that
            # refuses the linked build discards the slot and the mark with it.
            note_crew_log_class(state, slot)
        if fresh_link or post_read_meta.get("channel_origin"):
            slot.channel_origin = True
    # Hydrated length, not the raw disk count: materialisation may append one
    # unsaved interruption row, and the wrapper's paging cursor has to account
    # for it or the next older page repeats a row.
    total = slot._disk_older_count + len(slot.messages)
    if reserved:
        # Retract while the tail awaits, keep the construction mark: a lookup
        # finds nothing, the payload shows nothing, and a create on this key is
        # refused by the construction guard, so no acquirer can reach a slot the
        # hook or the reopen write may still refuse. The refusal discard mirrors the construction
        # rollback above (mark released, key freed, restricted marker dropped) and
        # puts back the ``closed`` marker this call cleared, so a refused resume
        # leaves the durable session exactly as it found it.
        state._slots.pop(slot.key, None)

        async def _discard() -> ResumeRefusal | None:
            # Durable rollback FIRST, while the construction mark still reserves
            # the key: released earlier, a concurrent resume of the same session
            # could publish in the gap and then have its live slot marked closed
            # by the restore below. The mark is the reservation; it goes last.
            # Returns the rollback failure when the marker could NOT be confirmed
            # restored; that answer outranks the refusal that triggered the
            # discard, because the durable session is now in a state the caller
            # must hear about (it would reopen at the next start).
            rollback: ResumeRefusal | None = None
            if cleared_closed and not await _restore_closed_marker():
                logger.error(
                    "resume of %s refused after its closed marker was cleared and the "
                    "marker could not be confirmed restored; the session may restore as open",
                    history_key,
                )
                rollback = ResumeRefusal(
                    "the session was refused but its closed marker could not be restored; "
                    "close it again from the History tab",
                    "reopen_rollback_failed",
                    503,
                )
            state._restricted_keys.discard(f"dashboard:{slot.key}")
            state.end_slot_construction(slot.key)
            return rollback

        def _identity_refusal(post: dict, readable: bool) -> ResumeRefusal | None:
            # The existence and ``created_at`` identity barrier above ran BEFORE
            # the hook's awaits. A delete, or a delete-and-recreate, landing
            # inside the hook window would otherwise publish a slot holding the
            # old transcript under the new file's key; every later save then
            # takes the delete-won arm and drops its rows. Same terms, same code
            # as the pre-hook barrier, re-read after the last await.
            #
            # UNREADABLE refuses here, unlike the pre-hook barrier, which lets it
            # through to protect legitimate resumes of transcripts that predate
            # the stamp. That leniency is affordable before the build because a
            # bad publish there is still caught by these re-reads; on the LAST
            # read there is nothing after it, and the read that cannot be made
            # is exactly the delete-and-recreate's own signature (the file is
            # being rewritten). A hooked caller retries; the marker rollback is
            # identity-guarded, so a replacement is never archived by it.
            if not readable:
                return ResumeRefusal(
                    "this session changed while resuming; open it again",
                    "resume_conflict",
                    409,
                )
            if (
                session_existed
                and state.conversation_log is not None
                and state.conversation_log.delete_in_flight(history_key)  # type: ignore[arg-type]
            ):
                # Same in-flight arm as the pre-hook barrier: the lock-free read
                # above still sees a file the delete is about to unlink. The
                # refusal goes through ``_discard``, which restores the marker.
                return ResumeRefusal(
                    "this session is being deleted; try again", "resume_conflict", 409
                )
            if not post and session_existed:
                return ResumeRefusal(
                    "the session was deleted while it was being resumed",
                    "resume_session_deleted",
                    409,
                )
            if request_app and not app_owns_transcript_meta(post, request_app):
                # The late barrier's ownership term, re-asserted on every re-read
                # after its awaits. A delete and same-key recreate under another
                # owner, on a line with no ``created_at`` for the identity arm
                # below to compare, is told apart only by its owner. An app gets
                # the uniform 404 (see _app_resume_refusal).
                audit_app_slot_denial(
                    request_app,
                    "slot_resume",
                    name,
                    "app does not own this transcript (reserved window)",
                )
                return _RESUME_APP_NOT_FOUND
            post_created = post.get("created_at")
            if pre_identity and post_created and pre_identity != post_created:
                return ResumeRefusal(
                    "the session was deleted while it was being resumed",
                    "resume_session_deleted",
                    409,
                )
            return None

        async def _status_riding_transient(log: Any, key: str) -> tuple[dict, bool]:
            """``get_metadata_status`` off the loop, waiting out a transient lock.

            The two re-reads below read a session file this resume JUST rewrote
            (the ``clear_closed`` atomic rename, or the clear plus the publish-side
            activity around it). On Windows that file is briefly unopenable while
            an indexer or AV scanner holds it, and on a loaded runner the hold can
            outlast ``get_metadata_status``'s own bounded retry, which then reports
            ``readable=False``. An unreadable read must stay fail-closed -- the
            caller refuses on it, because the in-process witnesses cannot see a
            cross-process delete/recreate while the file is unopenable -- so the
            cure is to let the read SUCCEED, not to publish without it: re-read a
            few more times, pausing off the loop, so the brief hold clears and the
            real identity + marker checks run on a readable line. A line that is
            genuinely gone or corrupt stays unreadable across the extra tries and
            the caller still refuses, exactly as it would without them -- this only
            widens the window a transient lock has to clear, it never accepts an
            unreadable line.
            """
            meta, readable = await asyncio.to_thread(log.get_metadata_status, key)
            attempt = 0
            while not readable and attempt < _REOPEN_REREAD_EXTRA_ATTEMPTS:
                await asyncio.sleep(_REOPEN_REREAD_RETRY_SECS)
                attempt += 1
                meta, readable = await asyncio.to_thread(log.get_metadata_status, key)
            return meta, readable

        # ONE arm for every await between the retraction and the publish. A
        # cancellation (the task torn down mid-resume) is a ``BaseException``,
        # and an ``except Exception`` on any of these awaits would let it skip
        # ``_discard``: the slot is already popped from the table, so the
        # construction mark would stay reserved for the process lifetime
        # (counted by ``live_slot_count``, refusing every later mint on the key,
        # with no scavenge) and a cleared ``closed`` marker would stay cleared.
        # Same shape as the construction rollback above. The discard is
        # shielded so a second cancellation cannot cut the rollback short.
        try:
            refusal = await containment(slot) if containment is not None else None
            if refusal is not None:
                return ResumeOutcome(refusal=(await _discard()) or refusal)
            holder = state._slots.get(slot.key)
            if holder is not None and holder is not slot:
                # Cannot happen while the construction mark holds (the create guard
                # refuses the key); kept as the fail-closed answer rather than
                # clobbering whatever did take it.
                return ResumeOutcome(
                    refusal=(await _discard())
                    or ResumeRefusal(
                        "this session changed while resuming; open it again", "resume_conflict", 409
                    )
                )
            log = state.conversation_log
            try:
                _post_hook, _post_readable = await asyncio.to_thread(
                    log.get_metadata_status, history_key
                )
            except Exception:
                _post_hook, _post_readable = {}, False
            refusal = _identity_refusal(_post_hook, _post_readable)
            if refusal is not None:
                return ResumeOutcome(refusal=(await _discard()) or refusal)
            if defer_clear:
                # The reopen write, after the hook (when there is one) and before
                # the publish. A clear that cannot land refuses: publishing a tab
                # whose line still says ``closed`` would give the person a session
                # that vanishes at the next start. COMPARE-AND-CLEAR against the
                # resume's own boundary, inside the store's lock: a close at or
                # after that instant (someone re-closed it, or deleted, recreated
                # and closed it) keeps its marker, and a marker still present
                # afterwards refuses too. Offloaded because clear_closed takes the
                # per-session cross-process lock, which fails fast on the loop.
                try:
                    # Recorded BEFORE the write is awaited: a cancellation can be
                    # delivered at this very await after the worker has already
                    # written, and a flag set afterwards would then never be set,
                    # leaving the session durably reopened. Likewise a
                    # verification read that comes back unreadable (a
                    # just-rewritten file is transiently unopenable on Windows)
                    # must refuse WITH the restore. Restoring a marker the clear
                    # never removed is a no-op (the restore's guard requires the
                    # marker absent), so an early flag costs nothing.
                    cleared_closed = True
                    await asyncio.to_thread(
                        log.clear_closed, history_key, only_if_closed_before=resume_started_at
                    )
                    _after, _readable = await _status_riding_transient(log, history_key)
                except Exception:
                    logger.warning("Failed to clear closed flag for %s", history_key, exc_info=True)
                    return ResumeOutcome(
                        refusal=(await _discard())
                        or ResumeRefusal(
                            "the session could not be reopened; try again", "reopen_failed", 503
                        )
                    )
                # The clear was itself an await: the identity barrier runs once more
                # on the verification read, before the marker check.
                refusal = _identity_refusal(_after, _readable)
                if refusal is not None:
                    return ResumeOutcome(refusal=(await _discard()) or refusal)
                if not _readable or "closed" in _after:
                    return ResumeOutcome(
                        refusal=(await _discard())
                        or ResumeRefusal(
                            "this session changed while resuming; open it again",
                            "resume_conflict",
                            409,
                        )
                    )
            # The transcript identity is read one final time here. The read runs
            # off the loop so its bounded retries pause between attempts: a
            # just-written session file is transiently unopenable for a few
            # milliseconds on Windows (a sharing violation while an indexer or AV
            # scanner holds it), and an on-loop read exhausts every attempt within
            # microseconds and reports it as unreadable, so it refuses a resume no
            # delete touched.
            #
            # The off-loop hop is an await, so a whole process-local delete can
            # begin AND end inside it: its in-flight marker opens and closes, and
            # ``_identity_refusal`` then reads the marker as clear, ``post`` as the
            # file the read saw before the unlink, and ``created_at`` unchanged --
            # a tab published over a deleted session, whose later saves ``delete_won``
            # drops. The invalidation generation is the witness that survives the
            # marker: every delete bumps it (``delete_session`` -> ``_invalidate_cache``)
            # under the file lock, a transient file lock does not, and reading it
            # is pure in-memory string math. Snapshot it BEFORE the hop and
            # re-read it SYNCHRONOUSLY below the last await, as the last act
            # before the publish.
            #
            # This read runs BEFORE the second containment pass, not after: the
            # store-backed boundaries (channel link, Slack binding, outbound
            # mirror) are read in the hook, so the hook must stay the LAST awaiting
            # act or a binding recorded during this read would publish. The
            # generation re-check below the hook still covers a delete landing in
            # EITHER await.
            last_cache_gen = log._cache_gen(history_key)
            try:
                _last, _last_readable = await _status_riding_transient(log, history_key)
            except Exception:
                _last, _last_readable = {}, False
            refusal = _identity_refusal(_last, _last_readable)
            if refusal is not None:
                return ResumeOutcome(refusal=(await _discard()) or refusal)
            # The deferred reopen write, the ``_after`` read and the off-loop
            # identity read above were awaits taken AFTER the hook answered, and
            # the hook is where the store-backed boundaries are read -- ``final_check``
            # below may not touch the store. A binding recorded in the store during
            # those awaits would otherwise publish. So the hook runs once more here,
            # as the LAST awaiting act: after it only the synchronous ``final_check``,
            # the generation re-check and the publish remain. The hook is a
            # read-only predicate, so the second pass has no side effect of its own.
            if containment is not None:
                refusal = await containment(slot)
                if refusal is not None:
                    return ResumeOutcome(refusal=(await _discard()) or refusal)
            if final_check is not None:
                # The last word, SYNCHRONOUS, after the last await above: the hook's
                # answers that need no store read are re-asserted on the built slot
                # with nothing able to run between this and the publish.
                refusal = final_check(slot)
                if refusal is not None:
                    return ResumeOutcome(refusal=(await _discard()) or refusal)
            # The delete-and-recreate signature after the last await, SYNCHRONOUS
            # and lock-free, where nothing can run between it and the publish. A
            # file read here would reopen the Windows transient this change moved
            # off the loop (a just-rewritten file is briefly unopenable), so two
            # in-memory witnesses stand in for it. A COMPLETED delete bumps the
            # invalidation generation under the file lock (``delete_session`` ->
            # ``_invalidate_cache`` after the unlink), while a transient file lock
            # does not. A delete still IN FLIGHT -- opened during the containment
            # await, past its unlink or not -- has not bumped the generation yet,
            # so the in-flight marker catches it; ``_identity_refusal``'s own
            # marker check now runs before that await, so this is the only place
            # left to see a delete that opens during it. Either witness refuses
            # through ``_discard`` so no slot is published over the session; the
            # caller retries.
            if log._cache_gen(history_key) != last_cache_gen or (
                session_existed and log.delete_in_flight(history_key)
            ):
                return ResumeOutcome(
                    refusal=(await _discard())
                    or ResumeRefusal(
                        "this session changed while resuming; open it again",
                        "resume_conflict",
                        409,
                    )
                )
            state._slots[slot.key] = slot
        except BaseException:
            await asyncio.shield(_discard())
            raise
    # The slot was registered throughout hydration (so a concurrent same-key
    # resume resolved it and hit the idempotency guard) but hidden from the
    # payload while under construction. End construction and push once: this is
    # the first frame any client sees, and it shows a fully hydrated session.
    # Nothing awaits between here and the return.
    state.end_slot_construction(slot.key)
    _sync_dashboard_slots(state)
    state.push_slots_update()
    return ResumeOutcome(slot=slot, total=total)
