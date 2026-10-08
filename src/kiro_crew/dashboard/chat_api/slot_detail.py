"""The slot list and the slot detail read: the bounded transcript page, its
durable-prefix and live-window merge, and the context meter fields.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        _FLUSH_SNAPSHOT_RETRIES,
        _TRANSIENT_ROLES,
        _UNOWED_WINDOW_ROLES,
        SLOT_DETAIL_MAX_LIMIT,
        DashboardState,
        OversizedRecord,
        SplitlinesBoundaryRecord,
        TranscriptRevisionChanged,
        _attach_variants,
        _ChatSlot,
        _collapse_wire_rows,
        _live_child_instance,
        _prepare_messages,
        _redact_for_display,
        _redact_meta_for_role,
        _slots_serialization_note,
        carry_provenance,
        deny_app_slot_access,
        effective_session_key,
        folder_principal,
        history_corpus_unreadable,
        logger,
        parse_cls_meta,
        queue_entry_view,
        redact_credentials,
        redact_exfiltration_urls,
        slot_history_key,
        slot_not_found,
    )


async def api_chat_slots(request: web.Request) -> web.Response:
    """GET /api/chat/slots — list all chat slots."""
    state: DashboardState = request.app["state"]
    # Credential-backed check status is owner-only for PRIVATE repos. For a
    # PUBLIC repo the lifecycle is world-visible, so an authenticated
    # dashboard-user (non-owner) may see it too. App-token callers receive
    # source links but neither cached status nor provider work.
    from kiro_crew.dashboard.handlers.source_providers import (
        ensure_gitlab_hosts_loaded,
        is_owner_dashboard_request,
        schedule_check_refresh,
        schedule_visibility_refresh,
    )

    # Same warm-up as the WebSocket connect path: slot source-link extraction is
    # synchronous and cannot load the self-managed GitLab allowlist itself, so a
    # cold direct GET would omit every configured self-hosted MR link.
    try:
        await ensure_gitlab_hosts_loaded()
    except Exception:
        logger.debug("GitLab allowlist warm-up failed; chips may lag one round", exc_info=True)

    include_check_status = is_owner_dashboard_request(request)
    is_dashboard_user = bool(request.get("is_dashboard_user"))
    payloads = state.serialize_slots(
        include_check_status=include_check_status, dashboard_user=is_dashboard_user
    )
    # A crew-member caller admitted here (the chat gate stamped its verified
    # principal) sees ONLY the sessions it owns or created -- the same set the
    # folder tree read shows it -- so admitting the session list for its folder
    # tools to resolve its own slot does not turn the list into an enumeration
    # of the person's and other agents' sessions. The person and app callers get
    # the full, unchanged response (an app row is already app-scoped downstream).
    from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY

    if folder_principal(state, request):
        # ``pin_rank`` is a place in the person's whole pinned order; an app or
        # member caller gets none of it, so a filtered list's rank gaps cannot
        # count the pinned sessions it is not shown.
        from kiro_crew.dashboard.pinned_session_order import PIN_ORDER_FIELDS

        payloads = [{k: v for k, v in p.items() if k not in PIN_ORDER_FIELDS} for p in payloads]
    if str(request.get(MEMBER_CHAT_PRINCIPAL_KEY) or "").startswith("member:"):
        from kiro_crew.dashboard.session_control import member_owns_slot

        caller_key = request.headers.get("X-Session-Key", "").strip()
        payloads = [
            p
            for p in payloads
            if member_owns_slot(state, state._slots.get(str(p.get("key") or "")), caller_key)
        ]
    if include_check_status:
        # Only the OWNER's GET drives provider work. Both the visibility probe
        # and the status refresh run the operator's `gh`/`glab` credentials, so
        # a non-owner request must trigger NEITHER — it renders the
        # owner-populated caches read-only via the fail-closed is_repo_public
        # gate in _project_source_links. Issue links carry no check status, so
        # skip them (the fetch is pull-request-only).
        urls = [
            link["url"]
            for payload in payloads
            for link in payload.get("source_links", [])
            if link.get("kind", "change") == "change"
        ]
        if urls:
            schedule_visibility_refresh(urls, state.push_slots_update)
            schedule_check_refresh(urls, state.push_slots_update)
    # Same offender diagnostic as the slots broadcast, on the same
    # projection: ``web.json_response`` would run this exact dump internally and
    # raise a bare TypeError naming neither slot nor field. Dump here so the
    # failure carries the note; the exception still propagates unchanged.
    # ``json_response`` is ``Response(text=dumps(data), content_type=...)``, so
    # the healthy path is byte-identical.
    try:
        body = json.dumps(payloads)
    except (TypeError, ValueError) as exc:
        exc.add_note(_slots_serialization_note(payloads, path="GET /api/chat/slots"))
        raise
    return web.Response(text=body, content_type="application/json")


def _finite_number(value: Any) -> float | None:
    """Return *value* as a float when it is a real, finite number, else None.

    The context fields are cosmetic, but they ride on the response that carries
    the whole conversation, so anything unserializable reaching `json_response`
    would turn a display nicety into a 500 that blanks the transcript. A
    provider is free to return whatever its accessors return; this is the gate
    that keeps a non-numeric one from ever being emitted.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _context_reading(pct: Any, used: Any, window: Any, *, stale: bool) -> dict[str, Any]:
    """Assemble the context fields from a (pct, used, window) triple.

    ``pct`` is the PRIMARY signal and the only one the bar needs: kiro-cli
    commonly reports ``contextUsagePercentage`` with no ``usage_update``, so a
    resident session routinely knows it is 11% full while knowing neither token
    count. Gating on the window would no-op the whole feature in that case.
    Token counts are optional enrichment for the tooltip's absolute numbers,
    and the frontend already falls back to a model-derived window without them.

    A ``stale`` reading omits ``used`` entirely rather than shipping a count no
    process measured. The tooltip renders an absent ``used`` as a ``~``
    approximation derived from pct, so honesty costs nothing — and leaving the
    count on the wire would make every other consumer of this endpoint render a
    never-measured figure as measured unless it knew to drop it.

    Returns ``{}`` when there is nothing worth showing — no usable pct, and no
    window either. A 0% reading with no tokens is indistinguishable from a
    fresh session that has never had a turn, and both render an empty bar
    anyway, so it is reported as "no reading" rather than as a measurement.
    """
    pct_num = _finite_number(pct)
    window_num = _finite_number(window)
    used_num = _finite_number(used)
    if pct_num is None:
        return {}
    fields: dict[str, Any] = {"context_pct": pct_num, "context_stale": stale}
    if window_num:
        fields["context_window_tokens"] = int(window_num)
        if used_num and not stale:
            fields["context_used_tokens"] = int(used_num)
    if not pct_num and "context_window_tokens" not in fields:
        return {}
    return fields


async def _context_snapshot_fields(state: "DashboardState", slot: "_ChatSlot") -> dict[str, Any]:
    """Context-meter fields for a slot-detail response, or ``{}`` when unknown.

    The meter is fed by turn-scoped ``context_usage`` WS frames, so opening a
    session that has not had a turn *in this tab's lifetime* renders an empty
    bar. This is the open-path source that seeds it.

    Two tiers, in order:

    1. **Live session** — the provider is still resident in the pool, so its
       ``last_prompt_stats`` are authoritative.
    2. **Cold session** — the ACP process expired (idle timeout) or the gateway
       restarted, so the stats are gone. Falls back to the snapshot recorded by
       ``DashboardState.broadcast_context_usage`` and marks it
       ``context_stale``. Resume replays the same transcript via ACP
       ``session/load``, so the pre-shutdown reading approximates the next
       turn's — and that turn overwrites it with measured truth.

    A snapshot taken under a DIFFERENT model is discarded rather than shown:
    its pct and counts are denominated in the old model's window, so rendering
    them against the new one would misreport usage. Dropping them lets the
    frontend fall back to its model-derived window at 0%.

    Never raises: every failure degrades to ``{}`` (an empty bar) rather than
    failing the request the transcript arrives on.
    """
    try:
        return await _context_snapshot_fields_inner(state, slot)
    except Exception:
        logger.debug("context snapshot fields failed for slot %s", slot.key, exc_info=True)
        return {}


async def _context_snapshot_fields_inner(
    state: "DashboardState", slot: "_ChatSlot"
) -> dict[str, Any]:
    provider = state.sessions.get_provider(effective_session_key(slot))
    if provider is not None:
        return _context_reading(
            provider.context_usage_pct(),
            (provider.context_used_tokens() if hasattr(provider, "context_used_tokens") else 0),
            (provider.context_window_tokens() if hasattr(provider, "context_window_tokens") else 0),
            stale=False,
        )
    # Readings from a previous process live in a file, so the first read is
    # disk IO — off the event loop, since this handler serves every chat open.
    await asyncio.to_thread(state.ensure_context_snapshots_loaded)
    snapshot = state.context_snapshot_for(slot.key)
    if snapshot is None:
        return {}
    if snapshot.get("model", "") != slot.model:
        return {}
    return _context_reading(
        snapshot.get("pct"),
        snapshot.get("used_tokens"),
        snapshot.get("window_tokens"),
        stale=True,
    )


def _load_redacted(body: str) -> str:
    """Apply the transcript redaction pair, in the one order the repo uses."""
    redacted, _ = redact_exfiltration_urls(body)
    redacted, _ = redact_credentials(redacted)
    return redacted


def _same_persisted_body(
    disk_body: str, window_body: str, role: str, disk_ts: str = "", window_ts: str = ""
) -> bool:
    """True when *disk_body* is the persisted form of *window_body*.

    A persisted row can differ from its window copy by exactly the redaction
    transform, and EITHER side can be the redacted one, which is why the compare
    applies it symmetrically. A restore redacts on load while keeping ``ts``
    verbatim (``chat_persistence.py:708-709``), so the window holds the redacted
    text; a save redacts every non-user role on the way out
    (``chat_persistence.py:1282-1284``) while the window keeps it verbatim
    (``state.py:2107``), so in a session that was never restored the DISK holds the
    redacted text instead. Redacting one side only cannot converge on that second
    pair — redacting an already-redacted body just reproduces it — so the row reads
    as un-flushed and the persisted suffix is appended twice.

    Applying the transform is also what separates a persisted row from a foreign
    row that merely shares a ``ts``: on a coarse clock two writers flooring off the
    same previous row both emit ``previous + 1µs`` (``history.py:1179-1219``), so
    matching on ``ts`` alone treats an unrelated row as the window's own and drops
    an un-flushed message from a response the client uses as a replacement.

    Two bodies can redact to the same text while being different messages, so the
    redaction-equivalent branch ALSO requires the stamps to match. That costs the
    legitimate case nothing: this branch only ever fires for a row and its own
    persisted copy, which differ by the transform precisely because one side was
    redacted, and both the save and the load copy ``ts`` verbatim
    (``chat_persistence.py:1288`` and ``:714``). A foreign row whose credential
    merely redacts to the same text carries its own writer's stamp, so it does not
    consume the window row.

    The requirement is on this branch ALONE, which is why it does not reintroduce
    the duplication above. A durable injection is byte-identical to its window row,
    so it returns at the plain-equality check and never reaches here — and that pair
    genuinely does carry different stamps, because the two writers mint
    independently.
    """
    if disk_body == window_body:
        return True
    if role == "user":
        return False
    if disk_ts != window_ts:
        return False
    return _load_redacted(disk_body) == _load_redacted(window_body)


def _is_answered_permission(m: dict) -> bool:
    """True for a ``permission`` row whose approval has already been answered.

    A permission row is never persisted, so it is always owed and therefore always
    lands in the tail — i.e. after every row that DID reach disk. For a pending
    approval that is the right place: it is the newest row, and nothing can follow
    it because the agent is blocked waiting on it. An answered one is history, and
    the agent has since produced turns that ARE on disk, so putting it in the tail
    moves it after them and the rendered order does not match what happened.

    The decision is written into the row's ``cls`` JSON in place
    (``state.py`` ``_mark_permission_resolved``), which is also the only place the
    stale-sweep and the slot resolver read it, so ``cls`` is the single source of
    truth here. Truthiness rather than key presence mirrors the client's own
    ``!meta.resolved`` test (``store/chat/selectors.ts`` ``selectSlotPendingApproval``), so an
    empty decision still counts as pending and an actionable approval is never lost.
    """
    if m.get("role") != "permission":
        return False
    meta = parse_cls_meta(m.get("cls", "")) or {}
    return bool(meta.get("resolved"))


def _snapshot_slot_window(slot: "_ChatSlot") -> tuple[int, list[dict]]:
    """Capture ``(_disk_older_count, window)`` as one internally consistent pair.

    Call this on the EVENT LOOP where possible. The two reads have no ``await``
    between them, so no loop-scheduled writer can land in the middle — and the
    finalization that motivates this, ``chat_runner._flush_segment``, is a plain
    ``def`` that is never handed to ``to_thread``, so it cannot interleave with a
    loop capture. It assigns ``slot.messages = head`` and only then appends the
    finalized assistant row, so a reader that lands between those two statements
    sees a transient chunk-free window missing that row. A worker thread CAN land
    there, which is why capturing inside the threaded scan is the weaker option.

    From a thread the pair can still tear, so retry: a front trim bumps
    ``_disk_older_count`` (state.py:2191-2200) between the reads, and a PRE-trim
    window paired with a POST-trim count shortens ``window_disk``, hides the
    trimmed rows' ids and re-appends rows the disk read already returned. A trim
    is the only mutation that changes the window/count relationship, so read the
    count, copy the window, then confirm the count is unchanged. ``slot._lock``
    is an ``asyncio.Lock`` and cannot be acquired from a thread, so this mirrors
    the bounded re-read ``_save_slot_to_history`` uses for the same race
    (chat_persistence.py:1711-1722).
    """
    for _ in range(_FLUSH_SNAPSHOT_RETRIES):
        disk_older_count = slot._disk_older_count
        window = list(slot.messages)
        if slot._disk_older_count == disk_older_count:
            break
    else:
        disk_older_count = slot._disk_older_count
        window = list(slot.messages)
    return disk_older_count, window


def _append_unflushed_tail_from_offset(
    slot: "_ChatSlot",
    all_msgs: list[dict],
    *,
    disk_offset: int,
    snapshot: tuple[int, list[dict]] | None = None,
) -> list[dict]:
    """Append window messages that are not yet on disk to a chained disk read.

    ``all_msgs`` is a disk read, so it omits transient roles while the window
    retains them, and it spans any older sessions a chained read walks. Sizing the
    tail by subtracting the two lengths therefore mixes units AND measures the
    whole file: it both re-appends rows the disk read already returned and lets a
    row from another writer consume a turn that is still owed.

    Takes the window itself rather than a caller-supplied count, so a caller cannot
    pass a length captured before an ``await``; the window can grow while a threaded
    disk read is in flight. ``snapshot`` is the one safe way to supply it: a
    ``(disk_older_count, window)`` PAIR from ``_snapshot_slot_window`` captured on
    the event loop AFTER the disk read, which is consistent by construction and
    cannot observe a mid-finalization window. Passing no snapshot falls back to
    capturing inside this thread, which is weaker — see that helper.

    Prefer message identity. A save copies each window row's ``meta.mid`` to disk,
    so a window row whose id appears in the disk read is persisted. A durable
    injector passes the window row's own id to ``ConversationLog.append``, which
    persists it in the same ``meta.mid`` shape — that copy carrying the id is the
    point: it IS the window row's flushed form and must match. A writer that passes
    no id persists no ``meta``, so its rows cannot be mistaken for a flushed window
    row.

    A disk read holding no ids at all needs a different boundary: a session
    persisted before ids existed, or rows a durable injector appended without
    going through a save. Walk the window and the disk read forward TOGETHER and
    stop at the first row that is not accounted for. A row from another writer no
    longer ENDS the run, which is what sizing the boundary as
    ``len(all_msgs) - slot._disk_older_count`` did — that measures the whole file,
    so a foreign append walked one row too far and dropped the owed turn. Both
    estimators the slot already carries are wrong here for opposite reasons: that
    subtraction over-counts, and ``_disk_window_len`` is not advanced by an
    injector, so it under-counts and would re-append a persisted row.

    The window is matched against the disk region as an ordered SUBSEQUENCE: a row
    that does not match the window row under consideration is SKIPPED rather than
    treated as the end of the window. The save is non-destructive against a
    cross-process append and merges the preserved rows back in TIME order
    (``_interleave_foreign_lines``), so the region can read
    ``[window, foreign, window]`` and an unmatched row means "not mine", not "end of
    window". Ending the run there leaves every persisted row after it in the tail,
    which appends an already-persisted suffix a second time.

    Skipping cannot pass over a row that should have matched: both sequences are
    chronological — the save's merge preserves each side's internal order — so a
    later window row's persisted copy cannot precede the current row's. It is also
    bounded: the disk cursor only ever moves forward, so the scans total
    O(window + region), and the first window row with no match anywhere in the
    remaining region ends the walk, which is the genuine end of the flushed prefix.

    A row matches on role plus content, compared through the redaction transform
    on both sides (``_same_persisted_body``). A shared ``ts`` is never SUFFICIENT —
    on a coarse clock two writers flooring off the same previous row both emit
    ``previous + 1µs`` (``history.py:1179-1219``), so accepting it alone drops an
    un-flushed message — but it is REQUIRED on the redaction-equivalent branch,
    where the only legitimate pair is a row and its own copy and the stamp is
    carried through verbatim.

    Ids are counted over the on-disk WINDOW REGION only,
    ``all_msgs[slot._disk_older_count:]``. The rows before that are the frozen prefix
    — on-disk rows older than the window, so none of them is in ``slot.messages``.
    Counting them would let an occurrence that exists only in the prefix fund a match
    for a window row that was never flushed, and the boundary would then walk past it.
    The fallback below already starts its disk cursor at the same offset.

    Id matching is selected only when EVERY row in that region carries a valid id, not
    merely when some row does. The dual-write injectors stamp both copies with one id
    (``slot.append`` mints it for the window copy and ``append_if_absent`` persists it
    on the durable copy), but the region can still legitimately hold a MIX: transcripts
    written before ids existed, and callers that pass no id. Choosing id matching on the
    strength of one id-carrying
    row then applies it to a row that structurally cannot match, which reads as
    un-flushed and appends the injection a second time. A mixed region belongs on the
    ordered path, which compares the fields both writers do record.

    Ids are matched as a MULTISET, one disk occurrence consumed per window row, not
    as a set. ``meta`` on an inbound message is caller-supplied and an id is minted
    only when one is *absent*, so a caller can post the same id twice. A set then
    matches EVERY window row carrying that id, so the boundary walks past a row that
    was never persisted and the response omits it — the silent-loss direction. One
    disk row is enough for that; two disk rows sharing an id are not required.
    Consuming an occurrence bounds the match to as many rows as really reached disk,
    and the earliest window row is the persisted one because flushes follow window
    order.

    Only string ids are matched. A truthy non-string ``mid`` survives to disk for the
    same caller-supplied reason and would raise ``TypeError`` if hashed.

    The id path selects the owed rows by MEMBERSHIP rather than by a prefix
    boundary. A boundary assumes every persisted row precedes every un-flushed one.
    When it does not, a later match moves the boundary past an un-flushed row and
    the response omits it — a drop, which is worse than the duplication this
    function exists to prevent. Ending the walk at the first miss is not the
    remedy either: a transient row is dropped by the save and so can never match,
    and stopping there re-appends every persisted row after it. Rows the client does
    not need are skipped outright (``_UNOWED_WINDOW_ROLES``), so selecting by
    membership cannot surface one the boundary happened to exclude; a pending
    ``permission`` row is deliberately not among them. Because an id in
    the disk window region proves that row reached disk, the owed set is simply the
    rows whose id did not, kept in window order. Where the persisted rows really
    are a prefix this returns the same answer, so it is a strict generalisation.
    """
    if snapshot is None:
        snapshot = _snapshot_slot_window(slot)
    disk_older_count, window = snapshot
    local_older_count = max(0, disk_older_count - disk_offset)
    window_disk = all_msgs[local_older_count:]
    disk_mid_positions: dict[str, list[int]] = {}
    every_row_has_an_id = bool(window_disk)
    for i, m in enumerate(window_disk):
        meta = m.get("meta")
        mid = meta.get("mid") if isinstance(meta, dict) else None
        if isinstance(mid, str) and mid:
            disk_mid_positions.setdefault(mid, []).append(i)
        else:
            every_row_has_an_id = False
    tail: list[dict]
    if every_row_has_an_id:
        # Membership, not a prefix boundary: see the docstring for why neither a
        # boundary nor a break-on-miss is correct here.
        #
        # Owed rows are MERGED at their window position, not concatenated after the
        # whole disk slice. Window order is authoritative and a persisted row can
        # sit LATER in it than an owed one: _flush_segment pulls a stop_event out of
        # the trailing chunk run and re-appends it AFTER the finalized assistant row
        # (chat_runner.py:2686-2687), so a stop that reached disk during streaming
        # follows a reply that is still owed. Appending owed rows last renders that
        # pair inverted -- stop before the reply it belongs to.
        #
        # Every row here carries an id, so the position is derivable without the
        # body matching the other arm needs. Persisted rows keep their disk order
        # and none is dropped; each owed row is only INSERTED before the disk row of
        # the next window row that reached disk, so this is additive.
        owed_before: dict[int, list[dict]] = {}
        pending: list[dict] = []
        for m in window:
            if m.get("role", "assistant") in _UNOWED_WINDOW_ROLES:
                continue
            if _is_answered_permission(m):
                continue
            meta = m.get("meta")
            mid = meta.get("mid") if isinstance(meta, dict) else None
            positions = disk_mid_positions.get(mid) if isinstance(mid, str) else None
            if positions:
                at = positions.pop(0)
                if pending:
                    owed_before.setdefault(at, []).extend(pending)
                    pending = []
                continue
            pending.append(m)
        if not owed_before and not pending:
            return all_msgs
        merged: list[dict] = list(all_msgs[:local_older_count])
        for i, m in enumerate(window_disk):
            merged.extend(owed_before.get(i, ()))
            merged.append(m)
        merged.extend(pending)
        return merged
    else:
        start = 0
        d = min(local_older_count, len(all_msgs))
        # An owed row is one the disk slice does not already carry, and this arm walks
        # the WHOLE window so that a single owed row cannot strand the rows behind it.
        # There are two ways to be owed, and both route to ``owed_rows``:
        #
        #   1. A TRANSIENT role. A disk read omits transient roles entirely, so such a
        #      row can NEVER be matched and is ALWAYS owed. Only ``_UNOWED_WINDOW_ROLES``
        #      (``done``/``queued``) and an already-answered ``permission`` are genuinely
        #      not owed.
        #   2. A non-transient row the forward scan does not find on disk. Breaking the
        #      loop outright on such a row is wrong: it leaves ``start`` pointing AT the
        #      unmatched row, so ``window[start:]`` re-emits every LATER window row --
        #      including rows already on disk. With a stop_event flushed before reply
        #      finalization (``_flush_segment`` re-appends the stop AFTER the finalized
        #      assistant row, see the note at the top of this function) the window reads
        #      ``[... unflushed reply, flushed stop]``: the reply misses, the loop breaks,
        #      and the persisted stop comes back a second time and out of order -- the
        #      very duplication this function exists to remove.
        #
        # The sibling id-carrying arm above already has the right rule, so mirror it
        # rather than inventing a second one: an unmatched row is held, a later match
        # flushes what is held at ITS disk position, and leftovers stay in the tail.
        # That keeps owed rows in window order instead of after the whole disk slice.
        #
        # Nothing is emitted twice: ``start`` only advances on a match, and a match flushes
        # ``owed_rows`` first, so every flushed row had an index below ``start``. Whatever
        # is left over sits at or after ``start`` and is carried by the trailing slice --
        # but that slice needs the unowed/answered exclusions applied to it as well, for
        # the reason recorded at the slice itself.
        owed_at: dict[int, list[dict]] = {}
        owed_rows: list[dict] = []
        for i, m in enumerate(window):
            role = m.get("role", "assistant")
            if role in _TRANSIENT_ROLES:
                if role not in _UNOWED_WINDOW_ROLES and not _is_answered_permission(m):
                    owed_rows.append(m)
                continue
            body = m.get("content", "")
            probe = d
            while probe < len(all_msgs):
                row = all_msgs[probe]
                if row.get("role", "assistant") == role and _same_persisted_body(
                    row.get("content", ""),
                    body,
                    role,
                    row.get("ts", ""),
                    m.get("ts", ""),
                ):
                    break
                probe += 1
            if probe >= len(all_msgs):
                owed_rows.append(m)
                continue
            if owed_rows:
                owed_at.setdefault(probe, []).extend(owed_rows)
                owed_rows = []
            d = probe + 1
            start = i + 1
        # ``start`` does NOT advance past an unowed row: such a row takes the
        # ``_TRANSIENT_ROLES`` branch above, is correctly kept out of ``owed_rows`` by the
        # guard there, and then ``continue``s -- skipping ``start = i + 1``. So a raw
        # ``window[start:]`` re-admits any unowed row that TRAILS the last match, and the
        # exclusion the guard performed is undone. The sibling arm does not have this hole
        # because it applies both exclusions at the TOP of its loop, so its leftovers can
        # never hold one. Apply the same two exclusions here, which is what actually
        # mirrors it.
        #
        # Two symptoms, one cause. A trailing ``done`` reaches the bounded response and
        # ``_prepare_messages`` then drops it while rendering (``chat_utils.py``), so a
        # page whose only row is that ``done`` renders EMPTY and replaces the transcript.
        # A trailing answered ``permission`` is instead re-ordered after every persisted
        # row -- the misordering ``_is_answered_permission`` exists to prevent.
        #
        # ``chunk``/``streaming`` and a still-PENDING ``permission`` are genuinely owed and
        # MUST survive this filter; narrowing it further would be the opposite defect.
        tail = [
            m
            for m in window[start:]
            if m.get("role", "assistant") not in _UNOWED_WINDOW_ROLES
            and not _is_answered_permission(m)
        ]
        if not owed_at and not tail:
            return all_msgs
        merged_idless: list[dict] = []
        for idx, row in enumerate(all_msgs):
            merged_idless.extend(owed_at.get(idx, ()))
            merged_idless.append(row)
        merged_idless.extend(tail)
        return merged_idless


def _append_unflushed_tail(
    slot: "_ChatSlot",
    all_msgs: list[dict],
    *,
    snapshot: tuple[int, list[dict]] | None = None,
) -> list[dict]:
    """Compatibility wrapper for reconciliation against a complete disk read."""
    return _append_unflushed_tail_from_offset(
        slot,
        all_msgs,
        disk_offset=0,
        snapshot=snapshot,
    )


class _DurablePrefixMismatch(Exception):
    """The slot's raw and durable prefix counters disagree for this snapshot.

    Deterministic for the snapshot, so the bounded path hands the request to
    the full reader instead of retrying.
    """


def _durable_prefix_counter(slot: "_ChatSlot") -> int:
    """The slot's durable-prefix counter, or a value the raw counter can never equal.

    ``_ChatSlot`` always carries the counter. A stand-in that does not gets ``-1``,
    which fails the prefix guard and routes the request to the full reader —
    never a default equal to the raw counter, which would pass the guard vacuously.
    """
    value = getattr(slot, "_disk_older_durable_count", None)
    return -1 if value is None else int(value)


def _bounded_slot_page(
    conversation_log: Any,
    slot: "_ChatSlot",
    history_key: str,
    *,
    limit: int,
    before: int | None,
    snapshot: tuple[int, list[dict]],
    durable_prefix_count: int,
) -> tuple[list[dict], int, bool, int]:
    """Compose one display page from an indexed durable prefix and live suffix.

    The sparse history projection owns durable row ranges. This helper reads at
    most the resident disk/window suffix, applies the established reconciliation
    oracle there, and fetches only the older range intersecting the requested
    page. Unlimited callers keep using the complete-reader path.

    The on-disk suffix is always read, a pending rewrite included: the full
    reader keeps the pre-rewind rows until the rewrite flushes, and the same
    reconciliation walk resolves them here, so both paths index one corpus and a
    ``before`` cursor from either response addresses the same rows.
    """
    disk_older_count, _window = snapshot
    if disk_older_count != durable_prefix_count:
        # Deterministic for this snapshot (both counters are slot state, not file
        # state), so retrying re-reads nothing new: hand over to the full reader.
        raise _DurablePrefixMismatch("raw and durable history prefix counters differ")

    # ``limit=0`` is the projection's total/revision probe: it stats and indexes
    # the chain but decodes no rows.
    total_probe = conversation_log.read_messages_chained_page(history_key, limit=0)
    durable_total = total_probe.total
    revision = total_probe.revision
    prefix_total = min(disk_older_count, durable_total)

    suffix_count = durable_total - prefix_total
    disk_suffix = (
        conversation_log.read_messages_chained_page(
            history_key,
            limit=suffix_count,
            before=durable_total,
            expected_revision=revision,
        ).messages
        if suffix_count > 0
        else []
    )

    merged_suffix = _append_unflushed_tail_from_offset(
        slot,
        disk_suffix,
        disk_offset=prefix_total,
        snapshot=snapshot,
    )
    collapsed_suffix = _collapse_wire_rows(merged_suffix)
    total = prefix_total + len(collapsed_suffix)
    end = total if before is None else max(0, min(before, total))
    start = max(0, end - limit)

    messages: list[dict] = []
    prefix_end = min(end, prefix_total)
    if start < prefix_end:
        messages.extend(
            conversation_log.read_messages_chained_page(
                history_key,
                limit=prefix_end - start,
                before=prefix_end,
                expected_revision=revision,
            ).messages
        )
    suffix_start = max(start, prefix_total) - prefix_total
    suffix_end = max(0, end - prefix_total)
    if suffix_start < suffix_end:
        messages.extend(collapsed_suffix[suffix_start:suffix_end])
    return messages, total, start > 0, start


async def api_chat_slot_detail(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot} — message history for a slot.

    Query params:
      - ``limit``: max messages to return (optional; if omitted, returns ALL messages from disk).
        Clamped to 1..SLOT_DETAIL_MAX_LIMIT (500). A value below 1 is rejected rather than clamped up, because
        no caller asking for 0 wanted exactly one message.
      - ``before``: return messages before this index (legacy pagination, still supported).
        ``before=0`` is valid and yields an empty page.

    Either param being a non-integer is a 400, not an uncaught 500 out of the
    handler.

    By default (no limit), reads the full chained history from disk across
    gateway restarts. Pagination params are retained for backwards compatibility.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_detail")
    if denied is not None:
        return denied

    limit_raw = request.query.get("limit")
    before_raw = request.query.get("before")

    # Both params arrive as strings and were converted at their point of use, so a
    # non-integer escaped as a ValueError and the client saw a 500 for what is
    # plainly a bad request. The branch below still keys off the RAW values, so
    # routing is unchanged.
    try:
        limit = min(int(limit_raw or "200"), SLOT_DETAIL_MAX_LIMIT)
        before = int(before_raw) if before_raw is not None else None
    except ValueError:
        return web.json_response(
            {"error": "limit and before must be integers", "code": "invalid_query_params"},
            status=400,
        )
    # Clamped above but not below, limit=0 made `start == end`: an empty page
    # reporting has_more true, which paginates forever.
    if limit < 1:
        return web.json_response(
            {"error": "limit must be >= 1", "code": "limit_out_of_range"}, status=400
        )

    # No limit → load ALL messages (chained across gateway restarts).
    # In-memory slot.messages is authoritative for the current session.
    # _disk_older_count gates whether to read disk AND provides the stable
    # slice boundary (set at restore/resume, never drifts with new messages).
    if limit_raw is None and before_raw is None:
        mem_msgs = list(slot.messages)
        if slot._disk_older_count > 0 and state.conversation_log:
            history_key = slot_history_key(slot)
            try:
                disk_msgs = await asyncio.to_thread(
                    state.conversation_log.read_messages_chained, history_key
                )
            except Exception:
                logger.warning("read_messages_chained failed for %s", history_key, exc_info=True)
                disk_msgs = []
            older = disk_msgs[: slot._disk_older_count] if disk_msgs else []
            # Re-read the tail after the await: that suspension point lets a message
            # land mid-read, and the client replaces its list with this response.
            messages = older + list(slot.messages)
        elif state.conversation_log:
            # _disk_older_count == 0: the window is supposed to be the whole
            # session. But disk can grow beyond the window (a concurrent writer,
            # a foreign append, or a persistence race). Detect and include any
            # rows the in-memory window is missing.
            # Safety: skip when the slot has unflushed rows or pending rewrites.
            _slot_idle = (
                len(mem_msgs) <= getattr(slot, "_disk_window_len", 0)
                and not getattr(slot, "_pending_rewrite", False)
                and not getattr(slot, "_dirty_flag", False)
            )
            if _slot_idle:
                history_key = slot_history_key(slot)
                try:
                    disk_msgs = await asyncio.to_thread(
                        state.conversation_log.read_messages_chained, history_key
                    )
                except Exception:
                    logger.warning(
                        "read_messages_chained failed for %s", history_key, exc_info=True
                    )
                    disk_msgs = []
                # Re-read after the await to capture anything that arrived mid-read.
                current_mem = list(slot.messages)
                # Post-await re-check: slot may have gained unflushed rows.
                _slot_idle = (
                    len(current_mem) <= getattr(slot, "_disk_window_len", 0)
                    and not getattr(slot, "_pending_rewrite", False)
                    and not getattr(slot, "_dirty_flag", False)
                )
                if _slot_idle and len(disk_msgs) > len(current_mem):
                    # Validate alignment: if rotation shifted offsets, the disk
                    # prefix does not match memory — skip reconciliation to
                    # avoid appending the wrong slice.
                    _aligned = True
                    if current_mem and disk_msgs:
                        # Spot-check last memory row against its expected disk position.
                        last_mem = current_mem[-1]
                        disk_at = (
                            disk_msgs[len(current_mem) - 1]
                            if len(current_mem) <= len(disk_msgs)
                            else None
                        )
                        if disk_at and (
                            last_mem.get("ts", "") != disk_at.get("ts", "")
                            or last_mem.get("role") != disk_at.get("role")
                        ):
                            _aligned = False
                    if not _aligned:
                        messages = current_mem
                    else:
                        # Disk has rows the window does not — reconcile by appending
                        # the missing tail to the slot and returning the union.
                        fresh = disk_msgs[len(current_mem) :]
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
                        # Replayed rows came from disk — drain the replay
                        # frames and mark the window persisted (not dirty) so a
                        # fork/SSE drain or the next save does not duplicate them.
                        slot.drain()
                        slot._resumed_count = len(slot.messages)
                        slot._disk_window_len = len(slot.messages)
                        slot._dirty = False
                        # Use the full disk corpus (which includes the prefix
                        # plus the reconciled tail) rather than slot.messages,
                        # because slot.append may have trimmed the head under
                        # _MAX_SLOT_MESSAGES — returning slot.messages alone
                        # would lose older rows without signaling has_more.
                        messages = disk_msgs
                else:
                    messages = current_mem
            else:
                messages = mem_msgs
        else:
            messages = mem_msgs
        total = len(messages)
        has_more = False
        # This branch returns the whole UN-ARCHIVED corpus. Rows a size
        # rotation moved into archive/ are NOT in it — so when such rows
        # exist, advertise them: `next_before` is their collapsed row count,
        # i.e. the boundary index (in the paginated corpus, which prepends the
        # archived head) of this response's first row. The client's next
        # "load earlier" then pages straight into the archived head instead of
        # this response permanently retiring the affordance. Collapsed in the
        # same units the paginated path slices in; a chunk run split by the
        # rotation cut can make this off by one, which the client's mid-dedupe
        # absorbs.
        #
        # The cursor is exact ONLY while the archived rows are a contiguous
        # PREFIX of the chained corpus (rotation on the first chain member).
        # A LATER member's archive is sandwiched between rows this response
        # already carries: no single cursor can reach it, and paging from the
        # head count would walk past it forever — those rows would simply be
        # unreachable, and a fork index computed against the true corpus
        # would name a different row than the one rendered. That shape is
        # served from the true chained corpus below instead.
        next_before = 0
        if state.conversation_log:
            try:
                rotated = await asyncio.to_thread(
                    state.conversation_log.read_rotated_messages_chained,
                    slot_history_key(slot),
                )
            except Exception:
                # NOT `rotated = []`. An empty list is this handler's encoding of
                # "this session has no archive", so swallowing the failure into it
                # skips the whole block below and serves the live-only corpus with
                # `next_before = 0` and no archive advertised — the same silent
                # truncation, reached by a different route. The read is the only
                # thing that knows the difference, so it has to answer here.
                logger.warning("rotated-archive read failed", exc_info=True)
                return history_corpus_unreadable()
            if rotated:
                rotated_count = len(_collapse_wire_rows(rotated))
                mid_rotation = False
                if rotated_count > 0:
                    try:
                        mid_rotation = await asyncio.to_thread(
                            state.conversation_log.chain_mid_rotation,
                            slot_history_key(slot),
                        )
                    except Exception:
                        # A failed probe leaves `mid_rotation` False, which sends
                        # the request down the prefix-cursor path -- correct ONLY
                        # when the rotation is on the first chain member. If it is
                        # not, that cursor addresses the wrong span and the
                        # sandwiched archived rows become unreachable, which is
                        # exactly the defect the mid-rotation branch exists to
                        # avoid. Not knowing which case this is means not serving.
                        logger.warning("mid-rotation probe failed", exc_info=True)
                        return history_corpus_unreadable()
                if rotated_count > 0 and mid_rotation:
                    # Serve every row at its true position; no cursor needed.
                    try:
                        full_msgs = await asyncio.to_thread(
                            state.conversation_log.read_messages_chained_full,
                            slot_history_key(slot),
                        )
                        tail_snapshot = _snapshot_slot_window(slot)
                        full_msgs = await asyncio.to_thread(
                            _append_unflushed_tail, slot, full_msgs, snapshot=tail_snapshot
                        )
                        messages = full_msgs
                        total = len(messages)
                    except Exception:
                        # FAIL CLOSED. This branch runs only when
                        # `chain_mid_rotation` is true, and that predicate means
                        # a chain member AFTER the first has archive segments --
                        # so the archived block is SANDWICHED, not the corpus's
                        # first `rotated_count` rows. A prefix cursor of
                        # `rotated_count` therefore addresses the wrong span: the
                        # page it returns does not advance past the sandwiched
                        # rows, `has_more` then goes false, and those rows become
                        # unreachable with no error the reader can see or retry.
                        #
                        # The sibling `elif` below uses the same value legitimately
                        # because it runs when the rotation IS on the first member,
                        # where `rotated_count` is exactly the boundary.
                        #
                        # Same retryable shape the fork handler returns for this
                        # identical corpus and identical reason.
                        logger.warning("chained-full mid-rotation read failed", exc_info=True)
                        return history_corpus_unreadable()
                elif rotated_count > 0:
                    has_more = True
                    next_before = rotated_count
                    total += rotated_count
    else:
        history_key = slot_history_key(slot)
        bounded_page: tuple[list[dict], int, bool, int] | None = None
        requires_full_reader = False
        if state.conversation_log:
            try:
                rotated = await asyncio.to_thread(
                    state.conversation_log.read_rotated_messages_chained,
                    history_key,
                )
            except Exception:
                logger.warning("rotated-archive read failed for %s", history_key, exc_info=True)
                return history_corpus_unreadable()
            requires_full_reader = bool(rotated)

        # A memory-only gateway (no conversation log) has no durable rows to
        # index; the fallback below already serves it from the resident window.
        if state.conversation_log and not requires_full_reader:
            # Imported here, not at module scope: this function runs on the
            # handlers module's globals (``chat_api.compose``).
            from kiro_crew.dashboard.transcript_snapshot import (
                PAGE,
                SNAPSHOT_ATTEMPTS,
                RetryRead,
                SlotView,
                SnapshotUnstable,
                read_consistent_transcript,
            )

            # A local the closure can close over as non-optional: mypy does not
            # carry the ``if not slot`` narrowing above into a nested function.
            paged_slot = slot

            async def _read_page(view: SlotView) -> tuple[list[dict], int, bool, int]:
                tail_snapshot = _snapshot_slot_window(paged_slot)
                try:
                    return await asyncio.to_thread(
                        _bounded_slot_page,
                        state.conversation_log,
                        paged_slot,
                        history_key,
                        limit=limit,
                        before=before,
                        snapshot=tail_snapshot,
                        # The count the PAGE witness holds, so the read is measured
                        # against the observation it is checked against.
                        durable_prefix_count=view.durable_older,
                    )
                except (
                    ValueError,
                    RecursionError,
                    OversizedRecord,
                    SplitlinesBoundaryRecord,
                    _DurablePrefixMismatch,
                ):
                    # The caller's to answer, below -- and checked first, so an
                    # exception that is also an OSError (``io.UnsupportedOperation``)
                    # meets the arm it would meet in one ordered chain.
                    raise
                except (TranscriptRevisionChanged, OSError) as exc:
                    # Retryable: a concurrent write moved the revision or a
                    # transient read failure. A bug propagates.
                    last = view.attempt == SNAPSHOT_ATTEMPTS
                    logger.log(
                        logging.WARNING if last else logging.DEBUG,
                        "bounded slot history read failed for %s (attempt %d/%d)%s",
                        history_key,
                        view.attempt,
                        SNAPSHOT_ATTEMPTS,
                        "; falling back to the full reader" if last else "; retrying",
                        exc_info=True,
                    )
                    raise RetryRead from exc

            try:
                bounded_page = (
                    await read_consistent_transcript(state, slot, PAGE, _read_page)
                ).result
            except SnapshotUnstable:
                # The slot kept moving inside every attempt: the full reader below
                # serves the request instead.
                pass
            except UnicodeDecodeError:
                # Same posture as the full reader's strict text-mode read:
                # undecodable transcript bytes are not a page to serve.
                logger.warning(
                    "bounded slot history is not valid UTF-8 for %s", history_key, exc_info=True
                )
                return history_corpus_unreadable()
            except RecursionError:
                # A row nested past the parser's depth is a corrupt corpus,
                # not a page: the unlimited path answers the same 503 for it.
                logger.warning(
                    "bounded slot history row exceeds JSON depth for %s",
                    history_key,
                    exc_info=True,
                )
                return history_corpus_unreadable()
            except ValueError:
                # ``json.loads`` refusing a row for a reason other than
                # malformed JSON (an integer literal past the interpreter's
                # digit limit). Deterministic for the revision, and the full
                # reader raises the same error, so answer the 503 it would
                # answer instead of retrying toward it.
                logger.warning(
                    "bounded slot history row is not decodable for %s",
                    history_key,
                    exc_info=True,
                )
                return history_corpus_unreadable()
            except (OversizedRecord, SplitlinesBoundaryRecord):
                # Deterministic: the same row is over the cap, or holds a
                # boundary only the full reader's splitlines() honours, on
                # every attempt. Go straight to the authoritative full reader.
                logger.debug("bounded slot history hit an unframeable row for %s", history_key)
            except _DurablePrefixMismatch:
                # Deterministic for this snapshot: the raw and durable prefix
                # counters disagree (mid-flush bookkeeping). The full reader
                # below reconciles against the complete corpus.
                logger.debug("bounded slot history prefix counters differ for %s", history_key)

        if bounded_page is not None and state.conversation_log:
            # The bounded reader walks only the live tab chain. A size rotation
            # landing between the archive probe above and the range reads moves
            # the head of this transcript into archive/ where the bounded index
            # cannot see it, so the page would omit those rows and could report
            # `has_more` false. Re-probe after composing: an archive that
            # appeared means the archive-including full reader must serve this
            # request instead.
            try:
                rotated_after = await asyncio.to_thread(
                    state.conversation_log.read_rotated_messages_chained,
                    history_key,
                )
            except Exception:
                logger.warning("rotated-archive re-probe failed for %s", history_key, exc_info=True)
                return history_corpus_unreadable()
            if rotated_after:
                bounded_page = None

        if bounded_page is not None:
            messages, total, has_more, next_before = bounded_page
        else:
            try:
                all_msgs = (
                    await asyncio.to_thread(
                        state.conversation_log.read_messages_chained_full,
                        history_key,
                    )
                    if state.conversation_log
                    else []
                )
            except Exception:
                logger.warning(
                    "read_messages_chained_full failed for %s", history_key, exc_info=True
                )
                return history_corpus_unreadable()
            tail_snapshot = _snapshot_slot_window(slot)
            all_msgs = await asyncio.to_thread(
                _append_unflushed_tail, slot, all_msgs, snapshot=tail_snapshot
            )
            # CLIENT DEPENDENCY on this collapse shape: while a slot streams, the
            # in-flight chunk run folds into ONE trailing row that carries no durable
            # `meta.mid`, and the bounded window ends in it. The dashboard's
            # `warmSlotCache` (website/src/store/chat/slotRefresh.ts) sizes its count-matched
            # request to the durable rows a pane holds and asks for ONE EXTRA row on a
            # running slot so the folded row does not displace a durable one out of
            # the window. A change here that folds the run into more than one row, or
            # stops folding, moves that `+1` out of step with the response.
            all_msgs = await asyncio.to_thread(_collapse_wire_rows, all_msgs)
            total = len(all_msgs)
            end = max(0, min(before, total)) if before is not None else total
            start = max(0, end - limit)
            messages = all_msgs[start:end]
            has_more = start > 0
            next_before = start

    # Snapshot every slot field the response needs BEFORE leaving the event
    # loop: the render below runs in a worker thread, and it must not read
    # attributes the loop keeps mutating mid-turn. `messages` is already a
    # fresh top-level list in both branches above; the message dicts inside it
    # are shared with live mutation, which _prepare_messages tolerates by the
    # same snapshot discipline the flush-thread save path relies on.
    key = slot.key
    workspace = slot.workspace
    running = slot.running
    stopping = slot._stopping
    display_title = slot.display_title
    # Shallow copies, so the off-loop render below reads a frozen entry while
    # the loop keeps editing the live one; the view helper does the redaction.
    # The two origin stamps ride along because the view reads them to decide
    # whether an entry is shown as typed (`queue_entry_is_user_origin`).
    queue_snapshot = [
        {
            "id": q["id"],
            "content": q["content"],
            "kind": q.get("kind", ""),
            "meta": dict(q.get("meta") or {}),
            "_directive_user_origin": q.get("_directive_user_origin", False),
            "_directive_channel_origin": q.get("_directive_channel_origin", False),
        }
        for q in slot._queue
    ]
    context_fields = await _context_snapshot_fields(state, slot)

    def _render(live_child: str) -> str:
        # Off-loop on purpose. _prepare_messages applies a regex-heavy
        # redaction battery to the ENTIRE history; on a multi-MB session that
        # blocked the event loop past the loop-stall watchdog's exit budget
        # and hard-exited the gateway. json.dumps of the same payload is a
        # second loop-blocking cost, so it lives in the thread too.
        prepared = _prepare_messages(messages, running, live_child=live_child, workspace=workspace)
        return json.dumps(
            {
                "key": key,
                # Redacted at emit like every sibling path (_ChatSlot.to_dict
                # does the same for the sidebar payload). Titles can be
                # LLM-generated or set by a rename, so they are content, not
                # configuration.
                "title": _redact_for_display(display_title),
                "running": running,
                "stopping": stopping,
                "messages": prepared,
                "queue": [queue_entry_view(q) for q in queue_snapshot],
                "total": total,
                "has_more": has_more,
                "next_before": next_before,
                # Seeds the context meter on open. Turn-scoped WS frames alone
                # leave it empty for a session reopened in a new tab; omitted
                # entirely (not zeroed) when genuinely unknown, so the frontend
                # can tell "no reading" from "0% used".
                **context_fields,
            }
        )

    # Per-slot single-flight: concurrent refetches of the same slot (WS
    # reconnect + switchSlot + chat_done all refetch) queue here instead of
    # each burning a worker thread on the same multi-MB redaction pass.
    async with slot._detail_render_lock:
        # Resolved INSIDE the lock, immediately before the render: the wait
        # behind another render can outlive a child, and a verdict sampled
        # before it would serve the dead child's link one more time. On the
        # event loop on purpose — the session pool is loop-owned and the probe
        # is two dict lookups plus a returncode read.
        live_child = _live_child_instance(state, slot)
        body = await asyncio.to_thread(_render, live_child)
    return web.Response(text=body, content_type="application/json")
