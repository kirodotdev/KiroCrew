"""The bytes a dashboard save writes: frozen prefix, live window, foreign lines.

A session file is a frozen prefix (the on-disk lines older than the in-memory
window, never rewritten) followed by the window, re-serialized in full on every
save. This module owns how those become one payload: reading and caching the
prefix, finding the lines another writer appended into the window region and
merging them back in time order, and archiving what a save drops -- the ambiguous
dedup folds and a rewrite's truncated tail. ``_save_slot_to_history`` holds the
transcript lock around it and does the atomic replace.

New rules about what a save keeps from the file it replaces belong here.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from typing import TYPE_CHECKING

from kiro_crew.chat_attachments import same_text_modulo_images
from kiro_crew.dashboard.state import _TRANSIENT_ROLES, row_mid
from kiro_crew.history import _archive_lines, latest_transcript_ts, transcript_sort_key

if TYPE_CHECKING:
    from pathlib import Path

    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger("kiro_crew.dashboard.chat_persistence")


def _diff_dropped_message_lines(old_lines: list[str], new_lines: list[str]) -> list[str]:
    """Return existing message lines that *new_lines* would drop.

    Both inputs are full file-line lists (metadata line at index 0, which is
    skipped on both sides). Compares by normalized JSON (``sort_keys``, so a
    key-order change is not a spurious drop). Corrupted/unparseable old lines
    are treated as dropped (archived). This is the same drop-detection rule
    ``ConversationLog.rewrite_session`` applies; it is factored out here so the
    dashboard rewrite path and ``rewrite_session`` share one definition.
    """
    if old_lines and '"_type"' in old_lines[0]:
        old_lines = old_lines[1:]
    kept_serialized: set[str] = set()
    for ln in new_lines[1:]:
        if not ln.strip():
            continue
        try:
            kept_serialized.add(json.dumps(json.loads(ln), sort_keys=True))
        except ValueError:
            continue
    dropped: list[str] = []
    for ln in old_lines:
        if not ln.strip():
            continue
        try:
            normalized = json.dumps(json.loads(ln), sort_keys=True)
        except ValueError:
            dropped.append(ln)  # corrupted line → archive it
            continue
        if normalized not in kept_serialized:
            dropped.append(ln)
    return dropped


def _archive_dropped_lines(
    state: DashboardState, history_key: str, old_lines: list[str], new_lines: list[str]
) -> None:
    """Archive on-disk message lines that *new_lines* (full file) would drop.

    Used only by the rewrite path (rewind/regenerate/fork), which intentionally
    truncates the in-memory window. The frozen prefix is present unchanged in
    both *old_lines* and *new_lines*, so it is never archived — only the dropped
    window tail is. No-op in the steady-state superset case.
    """
    dropped = _diff_dropped_message_lines(old_lines, new_lines)
    if not dropped:
        return
    base = state.conversation_log._dir if state.conversation_log else None
    _archive_lines(history_key, dropped, reason="compact", base=base)


def _foreign_tail_ts(foreign_lines: list[str]) -> str | None:
    """The newest parseable ``ts`` among *foreign_lines*, or ``None``.

    Named and single-sourced so "how a slot learns the disk tail" is one thing a
    reader can find, rather than a loop inlined in the save. Sits beside
    :func:`_interleave_foreign_lines` because they consume the same input: those
    lines are on-disk rows this slot never observed, which is exactly why they are
    the rows its ordering floor would otherwise miss.

    Malformed lines are skipped rather than propagated -- a corrupt row must not
    become the floor (``latest_transcript_ts`` refuses unparseable candidates for
    the same reason).
    """
    tail: str | None = None
    for line in foreign_lines:
        try:
            row_ts = json.loads(line).get("ts")
        except (json.JSONDecodeError, AttributeError):
            continue
        if isinstance(row_ts, str):
            tail = latest_transcript_ts(tail, row_ts)
    return tail


def _interleave_foreign_lines(
    window_entries: list[dict],
    window_lines: list[str],
    foreign_lines: list[str],
) -> list[str]:
    """Merge this save's window with another writer's lines, in time order.

    A bare ``window + foreign`` concatenation preserves both sets but not the
    conversation: it parks every foreign line after the newest window line. That
    was harmless while foreign appends were rare end-of-file arrivals (a cron
    result landing in a dashboard-only transcript). Once a channel tab shares the
    channel's transcript, foreign lines are ordinary turns of the SAME
    conversation that genuinely happened BETWEEN the window's turns — a channel
    reply that arrived before the user's next dashboard message would be filed
    after it, and the reordered file is what the next turn reads back as context.

    Both sequences are individually already chronological, so this is a two-way
    merge rather than a re-sort: neither side's internal order can change, and a
    line with no parseable ``ts`` inherits the previous key from its own sequence
    so it stays beside the line it was written next to. Exact ties keep the
    window's line first, making the result deterministic.
    """
    if not foreign_lines:
        return window_lines

    def keyed(entries, lines):
        out, last = [], (0, 0.0)
        for entry, line in zip(entries, lines):
            key = transcript_sort_key(entry.get("ts") or "")
            if key[0]:  # unparseable — stay adjacent to the previous line
                key = last
            last = key
            out.append((key, line))
        return out

    parsed_foreign = []
    for line in foreign_lines:
        try:
            parsed_foreign.append(json.loads(line))
        except (ValueError, TypeError):
            # Unparseable bytes are still somebody's acknowledged append: keep
            # them rather than dropping them on the floor.
            parsed_foreign.append({})

    left = keyed(window_entries, window_lines)
    right = keyed(parsed_foreign, foreign_lines)
    merged: list[str] = []
    i = j = 0
    while i < len(left) and j < len(right):
        if right[j][0] < left[i][0]:
            merged.append(right[j][1])
            j += 1
        else:
            merged.append(left[i][1])
            i += 1
    merged.extend(line for _, line in left[i:])
    merged.extend(line for _, line in right[j:])
    return merged


def _frozen_prefix_and_foreign_appends(
    slot: _ChatSlot,
    path,
    disk_older: int,
    window_entries: list[dict],
    *,
    collect_foreign: bool = True,
) -> tuple[str, list[str], list[str]]:
    """Return ``(frozen_prefix, foreign_lines, dedup_dropped)`` for a save.

    ``frozen_prefix`` is the verbatim bytes of the first *disk_older* on-disk
    message lines — the turns OLDER than the in-memory window. They are never
    rewritten, so older history survives a restart that only loaded a recent
    window. The bytes are cached on the slot keyed by ``(mtime, size,
    disk_older)`` so a steady 5s flush is O(window) rather than O(file size).

    ``foreign_lines`` are on-disk message lines in the WINDOW region (the bytes
    after the frozen prefix) that this slot's in-memory *window_entries* do NOT
    represent — i.e. acknowledged appends made by ANOTHER process (subagent /
    cron / CLI) that this slot never saw. ``_save_slot_to_history`` captures its
    ``window`` snapshot BEFORE taking ``_locked``, so a cross-process writer can
    fully append + release between the snapshot and this save acquiring the lock;
    a bare ``meta + frozen + window`` replace would then silently delete that
    acknowledged message. Carrying these lines into the payload makes the save
    non-destructive against cross-process appends. Identity is **id-first**: a
    disk line whose ``meta.mid`` (read via :func:`row_mid`) matches a window
    entry's id, corroborated by body or ``ts`` (same ``(role, content)`` — a
    durable copy — or same ``ts`` — an in-place edit), IS that entry's
    persisted copy, so it is dropped silently (the window re-serializes it)
    and never archived. The corroboration is required because ``meta.mid`` is
    caller-suppliable (``_ChatSlot.append`` preserves a pre-existing id), so a
    bare id equality could pair two genuinely distinct messages; an id match
    with NO corroborating entry falls back to the legacy ladder as if id-less,
    which typically preserves the line. A disk line whose ``meta.mid`` matches
    NO available window entry is a foreign append regardless of body equality,
    which is what tells two genuinely distinct identical-content messages
    apart — it still keeps its ``ts`` group ambiguous for the ts-only tier, so
    its presence can never convert a contested group into a silent id-less
    fold. Only an **id-less** disk line resolves
    through the legacy timestamp-first ladder, unchanged: it is treated as
    ours when
    its ``ts`` matches a window entry (covers in-place edits, which keep ``ts``
    but change content) OR — as a COUNT-BOUNDED tiebreak — its
    ``(role, content)`` matches an as-yet-unconsumed window entry (covers a
    same-process ``append_if_absent`` copy persisted with a FRESH ``ts``
    distinct from the window entry's in-memory ``ts``). The tiebreak is bounded
    so each window entry absorbs AT MOST ONE disk copy: if the on-disk window
    region holds two id-less lines with identical ``(role, content)`` but
    distinct timestamps — the window's own persisted copy PLUS a genuinely
    distinct event from another process (e.g. a repeated identical cron /
    workflow result) — only the first is folded into the window and the second
    is preserved as a foreign append. A plain ``(role, content)`` set collapsed
    those two real events into one; the bounded, timestamp-first identity
    fixes it for id-less lines, and the ``meta.mid`` tier resolves it exactly
    for stamped lines (see also ``docs/system-specs/modules/history.md``).
    ``dedup_dropped`` returns any fresh-``ts`` content-tiebreak drops so the
    caller can route them through the archive — even the residual ambiguous
    case (an id-less distinct message indistinguishable from an
    ``append_if_absent`` copy without a stable id) then loses no data
    permanently. A corroborated id-matched fold is NOT such a drop: the ids
    plus body/``ts`` agreeing makes it unambiguous, so it does not churn the
    archive.

    Fast path: when BOTH the on-disk mtime AND size match the frozen-prefix
    cache, THIS slot was the last writer and nothing has landed since, so the
    prefix is served from cache and the foreign lines preserved by the previous
    save are re-emitted verbatim from cache — the O(file) read/scan runs ONLY
    when the file changed on disk since our last write. Size is part of the key
    because an append always grows the file even inside a single coarse mtime
    tick, so mtime alone is not a safe change signal for a data-loss guard.
    Re-emitting the cached foreign lines (rather than assuming there are none)
    is what makes the fast path non-destructive: a previous save may have
    preserved a cross-process append INTO the on-disk window region, and since
    ``disk_older`` is unchanged those preserved lines would otherwise be dropped
    by a bare frozen-prefix + in-memory-window rebuild on the very next save.

    Returns ``("", [], [])`` when the file is missing/unreadable/has no metadata line.
    """
    try:
        st = path.stat()
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        return ("", [], [])
    cache = slot._frozen_prefix_cache
    if cache is not None and cache[0] == mtime and cache[1] == size and cache[2] == disk_older:
        # File is byte-identical to our last write → prefix AND the foreign
        # lines that write preserved are both served from cache. Returning the
        # cached foreign lines (a copy, so the caller cannot mutate the cache)
        # keeps the fast path non-destructive: the already-preserved
        # cross-process append is re-emitted instead of silently dropped. No
        # scan runs, so there are no fresh dedup drops to archive.
        return (cache[3], list(cache[4]), [])
    try:
        existing = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return ("", [], [])
    if not existing or '"_type"' not in existing[0]:
        return ("", [], [])
    body = existing[1:]  # message lines only (metadata excluded)
    prefix = "".join(body[:disk_older]) if disk_older > 0 else ""
    if not collect_foreign:
        # Rewrite (rewind / regenerate / fork) INTENTIONALLY truncates the
        # window, so a disk window-region line absent from the (truncated) window
        # is ambiguous between a rewound tail (must drop) and a cross-process
        # append (must keep). Those edits are same-session/same-process (not the
        # cross-process loss this scan guards), so skip the scan and let the
        # rewrite's archive-diff handle the dropped tail. Cache with no foreign
        # lines so a subsequent fast path re-emits nothing extra.
        slot._frozen_prefix_cache = (mtime, size, disk_older, prefix, [])
        return (prefix, [], [])
    # Scan the on-disk window region for lines the in-memory window does not
    # carry — those are cross-process appends we must preserve. Identity is
    # id-first (``meta.mid``, the stable per-message id every window append
    # mints and every durable-copy writer carries through), with the legacy
    # timestamp-first ladder — exact triple, ts, then a COUNT-BOUNDED
    # (role, content) tiebreak — retained unchanged for id-less lines (see the
    # module docstring / history.md).
    #
    # Build COUNT-BOUNDED consumption budgets over the window entries so each
    # on-disk window-region line is matched to AT MOST ONE window entry and each
    # window entry absorbs AT MOST ONE disk line. Identity is checked in four
    # tiers of decreasing confidence:
    #   (0) ``meta.mid`` — the stable id stamped at append time and carried onto
    #       durable copies; an id match IS the same message, resolved
    #       first across ALL disk lines so no heuristic tier can steal the
    #       entry, and an id-carrying line whose id matches NO entry is foreign
    #       regardless of body (two distinct identical-content messages carry
    #       distinct ids);
    #   (a) exact (ts, role, content) — an unchanged re-serialization (the common
    #       steady-save case), resolved before the ts/rc passes so a greedy
    #       edit/tiebreak match can never steal an entry a later exact line needs;
    #   (b) ts only — an in-place edit (same ``ts``, changed content: window wins);
    #   (c) (role, content) only — a same-content copy persisted with a FRESH
    #       ``ts`` (the ``append_if_absent`` case), routed to the archive.
    # Tiers (a)-(c) see only id-less disk lines, but every window entry stays
    # indexed in all of them regardless of whether it carries an id: a legacy
    # (pre-id) disk line must still fold into its window row even though the
    # restore minted that row a fresh id.
    # Keying every tier by COUNT (deques of entry indices guarded by a shared
    # ``consumed`` flag) — rather than a ``ts -> entry`` dict plus a per-``ts``
    # ``set`` — is what makes this correct when several messages share one ``ts``.
    # Coarse system clocks (notably Windows' ~15ms tick) can stamp a burst of
    # rapid appends with an IDENTICAL ``datetime.now().isoformat()``; the old
    # dict/set collapsed those colliding-``ts`` entries to a single slot, so a
    # genuine window line was mis-classified as a foreign append and DUPLICATED on
    # disk. The bounded multiset below matches them one-for-one regardless of
    # ``ts`` collisions.
    mid_idx: dict[str, "deque[int]"] = {}
    exact_idx: dict[tuple[object, object, object], "deque[int]"] = {}
    ts_idx: dict[object, "deque[int]"] = {}
    rc_idx: dict[tuple[object, object], "deque[int]"] = {}
    for _i, e in enumerate(window_entries):
        _ets = e.get("ts")
        _erole = e.get("role")
        _econtent = e.get("content", "")
        _emid = row_mid(e)
        if _emid:
            mid_idx.setdefault(_emid, deque()).append(_i)
        if _ets:
            exact_idx.setdefault((_ets, _erole, _econtent), deque()).append(_i)
            ts_idx.setdefault(_ets, deque()).append(_i)
        rc_idx.setdefault((_erole, _econtent), deque()).append(_i)
    consumed = [False] * len(window_entries)

    def _take(dq: "deque[int] | None") -> bool:
        """Consume the first not-yet-consumed entry index in ``dq`` (if any)."""
        if not dq:
            return False
        while dq:
            _idx = dq.popleft()
            if not consumed[_idx]:
                consumed[_idx] = True
                return True
        return False

    # Parse the on-disk window-region lines once (skipping blank/corrupt/transient
    # lines exactly as before), so the matching passes share one parse.
    disk_msgs: list[tuple[str, object, object, object, str | None]] = (
        []
    )  # (norm, ts, role, content, mid)
    for ln in body[disk_older:]:
        if not ln.strip():
            continue
        try:
            entry = json.loads(ln)
        except ValueError:
            continue  # corrupt window-region line — not a preservable message
        if not isinstance(entry, dict) or entry.get("_type") == "metadata":
            continue
        role = entry.get("role")
        if role is None or role in _TRANSIENT_ROLES:
            continue
        norm = ln if ln.endswith("\n") else ln + "\n"
        disk_msgs.append((norm, entry.get("ts"), role, entry.get("content", ""), row_mid(entry)))

    # Pass 0 — ``meta.mid``: id-first identity, resolved across ALL disk lines
    # before any heuristic tier so a greedy lower-confidence match can never
    # steal a window entry whose persisted copy is identified by id. An id
    # match folds ONLY when corroborated by body or ``ts`` — ``meta.mid`` is
    # caller-suppliable (``_ChatSlot.append`` preserves a pre-existing id, and
    # the ``/api/chat`` meta rides through), so a bare id equality is not proof
    # of sameness the way a minted-uuid contract would suggest:
    #   * corroborated (same (role, content) — a durable copy — or same ``ts``
    #     — an in-place edit — or the same text modulo PRESERVED IMAGES: the
    #     durable copy landed by ``append_if_absent`` names an image's stored
    #     copy while the window entry, whose rewrite failed open once the
    #     agent's scratch file was gone, still names the original): consume
    #     the entry and drop the line (the window re-serializes it). The ids
    #     matching exactly makes this NOT a dedup drop, so it is not routed to
    #     the ``foreign-dedup`` archive.
    #   * id matches an unconsumed entry but NEITHER body nor ``ts`` agrees
    #     (an id reused across two genuinely distinct messages): leave the
    #     entry unconsumed and let the line fall through to the legacy tiers
    #     as if id-less — typically preserved as foreign, so the distinct
    #     message stays in the transcript rather than being silently folded.
    #   * id matches NO available window entry (unknown id, or every same-id
    #     entry already absorbed its one copy): FOREIGN regardless of body
    #     equality — two genuinely distinct identical-content messages (e.g. a
    #     cron reporting the same status text twice) carry distinct ids, which
    #     is exactly what the body tiebreak could never tell apart — so it is
    #     excluded from the heuristic tiers below (``mid_foreign``) and
    #     preserved, in disk order, by pass 2.
    # Id-less lines fall through with tier (a)-(c) behaviour unchanged.
    handled = [False] * len(disk_msgs)
    mid_foreign = [False] * len(disk_msgs)
    for _j, (_norm, _ts, _role, _content, mid) in enumerate(disk_msgs):
        if mid is None:
            continue
        _live = [_i for _i in mid_idx.get(mid, ()) if not consumed[_i]]
        if not _live:
            mid_foreign[_j] = True
            continue
        for _i in _live:
            _e = window_entries[_i]
            if (
                (_role, _content) == (_e.get("role"), _e.get("content", ""))
                or (_ts and _ts == _e.get("ts"))
                or (
                    _role == _e.get("role")
                    and isinstance(_content, str)
                    and same_text_modulo_images(
                        _content,
                        _e.get("content", ""),
                        sessions_dir=path.parent,
                        stem=path.stem,
                    )
                )
            ):
                consumed[_i] = True
                handled[_j] = True
                break
        # No corroborated entry → deliberate fallthrough to the legacy tiers.

    # Pass 1 — exact (ts, role, content) over id-less lines: unambiguously our
    # own unchanged re-serialization. Resolving these before the ts/rc passes
    # makes the result independent of the disk-line order (an earlier
    # edit/tiebreak match cannot consume an entry that a later exact
    # line requires).
    for _j, (_norm, ts, role, content, _mid) in enumerate(disk_msgs):
        if handled[_j] or mid_foreign[_j]:
            continue
        if ts and _take(exact_idx.get((ts, role, content))):
            handled[_j] = True

    foreign: list[str] = []
    dedup_dropped: list[str] = []
    # After the exact pass, an in-place EDIT (same ``ts``, changed content) is the
    # only legitimate reason to drop a still-unmatched disk line by ``ts`` alone.
    # But under COLLIDING timestamps a ts-only match is AMBIGUOUS: a foreign
    # cross-process append that happens to share the ``ts`` is indistinguishable
    # from an edited window entry, and greedily consuming the ts budget would
    # silently DROP that acknowledged foreign line (data loss) — the exact guard
    # this scan exists to uphold. So restrict ts-only matching to the UNAMBIGUOUS
    # singleton case: a ``ts`` carried by EXACTLY ONE still-unmatched window entry
    # AND EXACTLY ONE still-unmatched disk line. Any ts group with more than one
    # unmatched line on either side is ambiguous, so its disk lines fall through
    # to the content tiebreak / foreign preservation below (favouring a rare
    # duplicate over irreversible data loss). Counts are taken from the
    # post-exact-pass state and are static for pass 2 (the ``consumed`` guard in
    # ``_take`` still prevents any double-consumption).
    w_unmatched_ts: dict[object, int] = {}
    for _i, e in enumerate(window_entries):
        _wt = e.get("ts")
        if _wt and not consumed[_i]:
            w_unmatched_ts[_wt] = w_unmatched_ts.get(_wt, 0) + 1
    d_unmatched_ts: dict[object, int] = {}
    for _j, (_norm, ts, _role, _content, _mid) in enumerate(disk_msgs):
        if ts and not handled[_j]:
            d_unmatched_ts[ts] = d_unmatched_ts.get(ts, 0) + 1

    # Pass 2 — for still-unmatched disk lines: ts-only (UNAMBIGUOUS in-place edit)
    # then the bounded (role, content) tiebreak, else genuinely foreign. A line
    # pass 0 already ruled foreign by id bypasses both heuristics (its identity
    # is settled) but is emitted HERE so ``foreign`` keeps disk order — the
    # interleave that re-merges these lines breaks ts ties by adjacency, so
    # reordering them relative to other foreign lines is not harmless. Such a
    # line still counts in ``d_unmatched_ts`` above: it keeps its ``ts`` group
    # ambiguous exactly as it did before the id tier existed, so an id-less
    # line sharing the ``ts`` is preserved (a rare stale duplicate) rather
    # than silently ts-folded into an entry the id-foreign line proves
    # contested (an irreversible drop of an acknowledged append).
    for _j, (norm, ts, role, content, _mid) in enumerate(disk_msgs):
        if handled[_j]:
            continue
        if mid_foreign[_j]:
            foreign.append(norm)
            continue
        # ts-match: an in-place edit keeps the ``ts`` but changes content, so the
        # window's version wins and the disk line is dropped silently — but ONLY
        # when the ``ts`` group is an unambiguous 1:1 (else a colliding foreign
        # append could be mistaken for the edit and lost).
        if (
            ts
            and w_unmatched_ts.get(ts, 0) == 1
            and d_unmatched_ts.get(ts, 0) == 1
            and _take(ts_idx.get(ts))
        ):
            continue
        # content tiebreak (bounded): a window entry with this exact
        # (role, content) that no match already consumed absorbs this disk copy —
        # the ``append_if_absent`` fresh-``ts`` case. A drop carrying a DISTINCT
        # non-empty ``ts`` is the genuinely ambiguous case (it could be a distinct
        # message we cannot tell apart without a stable id), so route it through
        # the archive; a ts-less / matching re-serialization is a plain window
        # copy and is dropped silently to avoid archive spam.
        if _take(rc_idx.get((role, content))):
            if ts:
                dedup_dropped.append(norm)
            continue
        # genuinely foreign → preserve verbatim.
        foreign.append(norm)
    # Cache the frozen prefix AND the foreign lines together, keyed on the
    # as-read (mtime, size). If this save's atomic_write later fails, the file
    # on disk is unchanged, so a subsequent save that re-reads the same
    # (mtime, size) must re-emit these same preserved foreign lines rather than
    # drop them — hence they are cached here, not just at the post-write site.
    slot._frozen_prefix_cache = (mtime, size, disk_older, prefix, foreign)
    # Kept lines never fold back into the window, so every re-scan finds them
    # again; warn only about lines not reported before for this slot. Keyed by
    # line hash, not count, so a rotation that swaps lines still reports.
    seen = frozenset(hash(line) for line in foreign)
    fresh = len(seen - slot._foreign_reported)
    if fresh:
        logger.warning(
            "Slot %s save found %d new line(s) another writer appended; keeping %d",
            slot.key,
            fresh,
            len(foreign),
        )
    slot._foreign_reported = seen
    return (prefix, foreign, dedup_dropped)


def compose_payload(
    state: DashboardState,
    slot: _ChatSlot,
    path: Path,
    history_key: str,
    window: list[dict],
    disk_older: int,
    meta_str: str,
    *,
    rewrite: bool,
) -> tuple[str, str, list[str]]:
    """The file a full save writes: ``(payload, frozen_prefix, foreign_lines)``.

    Called under the transcript lock. Archives the ambiguous dedup drops and, for a
    rewrite, the dropped window tail before the caller's atomic replace.
    """
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    # ── Frozen prefix (never rewritten) + freshly serialized window ──
    # Read the verbatim bytes of the on-disk lines OLDER than the
    # in-memory window (cached, O(window) on a steady flush — #5), AND
    # detect any cross-process appends that landed in the on-disk window
    # region since our last write. Then re-serialize the ENTIRE window
    # snapshot so in-place edits and reordering persist, and append the
    # foreign lines so a concurrent cross-process append (landed between
    # this save's pre-lock ``window`` snapshot and the lock) is preserved
    # rather than clobbered by the meta+frozen+window replace.
    # A window longer than the entry cache cannot hit it: this save walks
    # the window in order, so the LRU evicts each entry before the next
    # save reaches it again. Building such a window through the cache
    # would pay the key-hashing cost for a guaranteed 0% hit rate, so the
    # largest windows -- where flush cost hurts most -- go uncached. A
    # window whose payload exceeds the BYTE ceiling self-evicts the same
    # way at a far smaller message count, so it is gated too, on a cheap
    # lower-bound estimate rather than on a measurement that would itself
    # cost what the bypass saves. Gated on the same configured bounds the
    # cache evicts by, so raising them widens the cached path in step.
    cache_max_entries, cache_max_bytes = cp._entry_cache_bounds()
    build_entry = (
        cp._build_message_entry_uncached
        if len(window) > cache_max_entries
        or cp._approx_window_payload_bytes(window) > cache_max_bytes
        else cp._build_message_entry
    )
    # ``path`` is this session's transcript, so its directory and stem are
    # what pairs an attachment with the session that will delete it.
    attachments = (path.parent, path.stem)
    window_entries = [
        e for m in window if (e := build_entry(m, attachments=attachments)) is not None
    ]
    window_lines = [json.dumps(e) + "\n" for e in window_entries]
    frozen_prefix, foreign_lines, dedup_dropped = _frozen_prefix_and_foreign_appends(
        slot, path, disk_older, window_entries, collect_foreign=not rewrite
    )
    # A fresh-``ts`` disk copy folded into the window by the bounded
    # (role, content) tiebreak is redundant with a window entry, so the
    # payload does not carry it. It is nonetheless the genuinely ambiguous
    # case (indistinguishable from a distinct same-content message without
    # a stable per-message id), so archive it before the atomic replace so
    # the trade-off loses no data permanently.
    if dedup_dropped:
        try:
            base = state.conversation_log._dir if state.conversation_log else None
            _archive_lines(history_key, dedup_dropped, reason="foreign-dedup", base=base)
        except Exception:
            logger.warning(
                "Failed to archive foreign-dedup drops for %s",
                history_key,
                exc_info=True,
            )
    payload = (
        meta_str
        + frozen_prefix
        + "".join(_interleave_foreign_lines(window_entries, window_lines, foreign_lines))
    )

    # Refresh the slot's ordering floor from what is actually going to
    # disk, foreign rows included. This is the only place the slot can
    # learn about a row it never observed: the lock is already held and
    # the foreign lines are already in hand, whereas reading the tail per
    # append would put file I/O on the event loop. It does not make the
    # slot fully symmetric with ConversationLog.append -- a foreign row
    # arriving BETWEEN two saves stays invisible until the next one -- but
    # it closes the reachable shape, where a subagent/cron append is
    # observed at the next flush. The monotone rule itself lives on the
    # slot (note_disk_tail), so this cannot move the floor backwards.
    slot.note_disk_tail(
        _foreign_tail_ts(foreign_lines),
        window_entries[-1].get("ts") if window_entries else None,
    )

    # Rewrite paths (rewind/regenerate/fork) intentionally TRUNCATE the
    # window, so the dropped tail must be archived first to stay
    # recoverable. The default save is a superset of what's on disk
    # (frozen prefix unchanged + same-or-grown window), so it drops
    # nothing — and we skip the O(file) archive-diff read there to keep a
    # steady flush O(window). Both sides are passed as proper
    # per-line lists so the normalized-JSON diff matches the
    # frozen-prefix lines (never archived).
    if rewrite and path.exists():
        try:
            old_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            new_lines = payload.splitlines(keepends=True)
            _archive_dropped_lines(state, history_key, old_lines, new_lines)
        except Exception:
            logger.warning("Failed to archive dropped lines for %s", history_key, exc_info=True)
    return payload, frozen_prefix, foreign_lines
