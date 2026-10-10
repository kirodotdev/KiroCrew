"""Pre-write file snapshots and the line changes a turn made."""

from __future__ import annotations

import os
import stat as stat_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _MAX_RECONSTRUCT_BYTES,
        _MAX_SLOT_MESSAGES,
        _MAX_SNAPSHOT,
        _MAX_SNAPSHOT_PATH_CHARS,
        _MAX_TURN_SNAPSHOT_CHARS,
        _MAX_TURN_SNAPSHOT_ENTRIES,
        _SNAPSHOT_READ_BYTES,
        _SNAPSHOT_TRUNCATION_MARKER,
        _WRITE_COMMANDS,
        _ChatSlot,
        line_changes_from_file_changes,
        row_mid,
        safe_read_file,
        safe_read_file_bytes_nolink,
        validate_file_path,
    )


class _Snapshot(NamedTuple):
    content: str
    truncated: bool


def _truncate_snapshot(content: str) -> _Snapshot:
    """Cap content while reporting whether the configured limit was exceeded."""
    if len(content) > _MAX_SNAPSHOT:
        content = content[:_MAX_SNAPSHOT] + _SNAPSHOT_TRUNCATION_MARKER
        return _Snapshot(content, True)
    return _Snapshot(content, False)


def _safe_read_snapshot(path: str) -> _Snapshot | None:
    """Read a file's content and truncation state, refusing sensitive paths.

    Reads through ``hooks.safe_read_file_bytes_nolink`` — the same descriptor
    gate the prompt and skill readers use — rather than validating the name and
    then re-opening it. A hardlink alias shares its target's inode but carries
    its own innocent name: ``realpath`` yields the alias, ``is_symlink()`` is
    False, and every name-based check passes while the bytes belong to whatever
    it aliases. The gate opens FIRST (refusing a link at the final component),
    then ``fstat``s that one descriptor and refuses ``st_nlink > 1``, a
    non-regular inode, and a sensitive or out-of-root real path, so the inode
    validated is exactly the inode whose bytes reach the diff chips.
    ``within_root`` is the canonical path's own parent, which also pins the
    opened inode on Windows where ``O_NOFOLLOW`` does not exist.

    Returns the (possibly truncated) text content, or None if the path is
    sensitive / not a regular file / aliased / unreadable — one shape for every
    refusal, so a caller cannot tell a protected target from a missing file.
    """
    try:
        validated = validate_file_path(path)
        if validated is None:
            return None
        raw = safe_read_file_bytes_nolink(
            validated,
            within_root=os.path.dirname(validated),
            max_bytes=_SNAPSHOT_READ_BYTES,
            allow_truncate=True,
        )
        if raw is None:
            return None
        # Git and agent-authored files are UTF-8 regardless of the host's
        # preferred code page; decoding the bytes explicitly matters on Windows,
        # where a text-mode read otherwise defaults to a legacy locale such as
        # cp1252. ``errors="replace"`` also absorbs a code point the byte cap
        # above may have cut in half. Newlines are normalized as the text-mode
        # read did: the strReplace "before" comes from ``hooks.safe_read_file``,
        # a text-mode read, so a CRLF "after" that kept its ``\r`` would show
        # every unchanged line as modified in the diff chip.
        text = raw.decode("utf-8", errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return _truncate_snapshot(text)
    except Exception:
        return None


def _classify_str_replace_before(path: str, raw_params: dict) -> tuple[str | None, str | None]:
    """Classify the on-disk file of a strReplace edit as pre- or post-write.

    Returns ``(before, undecidable_content)``: ``before`` is the proven
    full-file before-content, or ``None``; ``undecidable_content`` is the raw
    disk content when the file is valid as BOTH states, so the caller can settle
    the question later against the turn-end after-content (see
    ``_resolve_pending_str_replace``). At most one of the two is set.

    kiro-cli's ACP diff content block carries only the replaced FRAGMENT as
    ``oldText`` for strReplace — not the whole file. Using it verbatim as the
    before-snapshot makes the chip diff a one-line fragment against the
    full-file after, counting the entire file as additions. ``oldText`` is
    full-file for create, but not for strReplace.

    Instead, read the file from disk and classify it by testing BOTH
    hypotheses explicitly, reconstructing only when exactly one is plausible
    (needle presence alone proves nothing — ``oldStr`` can re-form across
    the replacement seam, e.g. ``oldStr="ab", newStr="a", before="abb"`` →
    after ``"ab"``):

    * ``newStr`` absent → post-write excluded (post-write content always
      contains ``newStr``); pre-write proven iff ``oldStr`` occurs exactly
      once (the tool refuses ambiguous ``oldStr``).
    * ``newStr`` present but not unique (overlap-safe ``find()==rfind()``)
      → post-write can neither be excluded nor reversed → decline.
    * ``newStr`` unique → the single reversal candidate decides: candidate
      tool-consistent AND pre-write plausible → undecidable now, hand the
      content back for deferred resolution (seam shapes, and the common
      ``oldStr ⊂ newStr`` / ``newStr ⊂ oldStr`` edits that add or drop a
      line next to a kept one); consistent only → reverse; inconsistent
      with pre-write plausible → pre-write proven (post-write excluded).

    Returns ``(None, None)`` when reconstruction isn't provable (missing/empty
    params — including an empty ``newStr`` deletion, whose position in the
    after-state is unrecoverable — ``replaceAll`` edits, where oldStr
    uniqueness is not enforced and reversal would over-revert pre-existing
    ``newStr`` occurrences, non-regular or oversized files
    (``_MAX_RECONSTRUCT_BYTES``), unreadable files, or an implausible state);
    the caller then falls through to the pre-existing source-priority chain.
    """
    old_str = raw_params.get("oldStr")
    new_str = raw_params.get("newStr")
    if not isinstance(old_str, str) or not isinstance(new_str, str) or not old_str or not new_str:
        return None, None
    if raw_params.get("replaceAll"):
        # replaceAll is the one mode where strReplace does NOT enforce oldStr
        # uniqueness, so the pre-write proof below doesn't hold and reversing
        # every newStr occurrence over-reverts any that pre-existed the edit,
        # fabricating counts. Position/count is unrecoverable — decline and
        # fall through to the fragment chain.
        return None, None
    try:
        # Bound the read: only regular files —
        # /dev/zero and FIFOs stat as 0 bytes but read unboundedly — and
        # only up to _MAX_RECONSTRUCT_BYTES (re-checked after the read,
        # since stat() races with an external writer growing the file).
        st = Path(path).expanduser().stat()
        if not stat_module.S_ISREG(st.st_mode) or st.st_size > _MAX_RECONSTRUCT_BYTES:
            return None, None
        # Read through hooks.safe_read_file — the symlink-safe chokepoint
        # (re-checks the RESOLVED target + O_NOFOLLOW open, closing the
        # validate→read TOCTOU window). Raw content, no
        # truncation: the cap must apply AFTER the reverse substitution or
        # the needle could be cut mid-file. PermissionError (sensitive
        # target / symlink race) and ordinary read errors both decline via
        # the except-fallback — file-chip capture must never block a turn.
        content = safe_read_file(path)
    except Exception:
        return None, None
    if len(content) > _MAX_RECONSTRUCT_BYTES:
        # Re-check after the read: the stat() gate above races with an
        # external writer growing the file, and the substring scans below
        # are O(n) — keep them bounded.
        return None, None
    pre_write_plausible = content.count(old_str) == 1
    # Post-write content ALWAYS contains newStr (the edit just inserted it),
    # so newStr absent excludes post-write entirely.
    if new_str not in content:
        return (content if pre_write_plausible else None), None
    # newStr present but NOT unique (overlap-safe: find()==rfind()):
    # post-write can neither be excluded (any occurrence could be the edit
    # site) nor reversed (ambiguous). strReplace "ab"→"a" on "aabb" → "aab"
    # looks pre-write-plausible (one "ab") but IS post-write — classifying it
    # pre-write records the after as the before and erases the edit from the
    # chip. Decline.
    if content.count(new_str) != 1 or content.find(new_str) != content.rfind(new_str):
        return None, None
    # newStr unique: the single possible reversal candidate decides.
    candidate = content.replace(new_str, old_str, 1)
    post_write_consistent = candidate.count(old_str) == 1
    if post_write_consistent and pre_write_plausible:
        # Valid as both states. Not a guess either way: the turn-end
        # after-content tells them apart (see _resolve_pending_str_replace).
        return None, content
    if post_write_consistent:
        return candidate, None
    if pre_write_plausible:
        # Post-write EXCLUDED (its only possible edit site is
        # tool-inconsistent), so pre-write is proven even with newStr
        # coincidentally present in the file.
        return content, None
    return None, None


def _pending_str_replace_payload(content: str, old_str: str, new_str: str) -> dict[str, _Snapshot]:
    """Precompute the bounded hypotheses a deferred strReplace resolution needs.

    Every hypothesis is ``_truncate_snapshot``-capped, so the payload never
    retains the raw disk read (up to ``_MAX_RECONSTRUCT_BYTES``) for a whole
    turn — a deferred snapshot of a large file costs no more than an ordinary
    snapshot. ``if_post_write`` is the disk content as seen at snapshot time
    (the FULL-FILE before, if that snapshot was the PRE-write state);
    ``if_pre_write`` is the forward substitution (what the file reads at turn
    end if the snapshot was pre-write).

    Only the pre-write hypothesis is resolvable, so no reverse substitution is
    stored: the before this fix shows is always content that was ACTUALLY on
    disk at snapshot time, never a value synthesised from the turn-end state
    (see ``_resolve_pending_str_replace`` for why the post-write branch is not
    settled).
    """
    return {
        "if_post_write": _truncate_snapshot(content),
        "if_pre_write": _truncate_snapshot(content.replace(old_str, new_str, 1)),
    }


def _resolve_pending_str_replace(pending: dict[str, Any], after: str) -> _Snapshot | None:
    """Settle an undecidable strReplace snapshot against the turn-end after.

    ``pending`` holds the two ``_MAX_SNAPSHOT``-capped hypotheses
    ``_pending_str_replace_payload`` precomputed. The snapshot's disk content is
    settled ONLY as the PRE-write state: if it was pre-write, applying the edit
    makes the file read ``if_pre_write`` (the forward substitution) at turn end,
    and the true full-file before is ``if_post_write`` — the content actually
    read from disk at snapshot time. A match there returns that real captured
    content.

    The POST-write hypothesis is deliberately NOT settled. Proving it would
    require the turn-end disk to equal the snapshot content and then showing a
    REVERSE substitution as the before — a value never on disk at snapshot
    time. The turn-end read is not this slot's to trust: a shell command or a
    concurrent write from another session can restore the file to exactly that
    content, and the reverse substitution would then fabricate a before-diff
    the file never held (crash-data-loss-corruption class). Keeping only the
    pre-write branch means every resolved before is captured disk content, so
    no cross-slot or shell restore can turn a resolution into a fabrication;
    the post-write (append-style) direction keeps the fragment fallback.

    A refused, failed or cancelled write leaves the file UNCHANGED — equal to
    ``if_post_write``, the snapshot content. The classifier only defers an
    undecidable edit, whose two hypotheses differ
    (``if_post_write != if_pre_write``), so an unchanged file fails the
    ``after == if_pre_write`` test and keeps the fragment on its own, with no
    completion gate needed for that case.

    The cases content-only resolution would get WRONG are turn-end states this
    slot cannot attribute to the single tracked edit, and each is removed
    UPSTREAM of this function rather than guarded here:
      * A second identical forward substitution by another WRITE TOOL in the
        same turn: if this snapshot was POST-write and the same edit runs
        again, the turn-end file equals ``if_pre_write`` and this would return
        ``if_post_write`` (the after-first-edit content) as the before. The
        WRITER-ID gate in ``_record_turn_snapshot`` drops the pending
        hypothesis when a second or unknown write tool touches the same
        canonical path, so such a payload never reaches this resolver.
      * A shell command (or any non-write tool) editing the file after a
        post-write snapshot: a shell is opaque, can write any path, and never
        passes through ``_record_turn_snapshot``, so it cannot be gated per
        path. ``_flush_file_changes`` therefore skips resolution for the whole
        turn when a shell tool ran (its ``turn_had_shell`` argument), keeping
        the fragment rather than trusting an unattributable turn-end read.
    This resolver settles only the single-tracked-write case the deferral is
    safe for, and every before it returns is captured disk content.

    Neither hypothesis matching (the file changed again during the turn, or the
    edit lies past the ``_MAX_SNAPSHOT`` truncation so the prefixes agree)
    returns ``None`` and the caller keeps the fragment it already has.
    """
    if_post_write: _Snapshot = pending["if_post_write"]
    if_pre_write: _Snapshot = pending["if_pre_write"]
    if after == if_pre_write.content and after != if_post_write.content:
        return if_post_write
    return None


def _snapshot_write_target(
    raw_params: dict | None,
    diff_old_text: str | None = None,
    diff_path: str = "",
) -> dict | None:
    """Return {"path", "content"} of a file before modification for write tools.

    A strReplace whose on-disk state is valid as both pre- and post-write adds
    ``pending_str_replace`` (the ``_MAX_SNAPSHOT``-capped hypotheses of
    ``_pending_str_replace_payload``) for ``_flush_file_changes`` to settle
    against the turn-end after-content.

    For strReplace, FIRST reconstructs the full-file before via
    ``_classify_str_replace_before`` (disk read + reverse substitution),
    because the ACP diff content block's ``oldText`` is only the replaced
    fragment for that command.

    Otherwise prefers the authoritative ``diff_old_text`` from the ACP diff
    content block (kiro-cli's in-band before-text) over a disk read, because
    by the time we process the event the write has already landed on disk
    (the auto-approved path is a one-way notification — kiro-cli does NOT
    wait for the dashboard to drain its asyncio.Queue before executing the
    write). For create the content block IS full-file: ``""`` for a new
    file, the entire previous content for an overwrite.

    Falls back to a disk read only when no content block is present
    (``diff_old_text is None``), which is the correct path for the
    blocking permission-request flow where the file hasn't been written yet.

    Returns None for non-write tools or when a path can't be resolved. Failures
    (file not found, permission, decode errors) yield empty content rather than
    raising — file-chip capture must never block a turn. Sensitive paths
    (~/.aws, ~/.ssh, etc.) yield None so credentials never enter message meta.
    """
    if not isinstance(raw_params, dict):
        return None
    cmd = raw_params.get("command", "")
    path = raw_params.get("path", "") or diff_path
    if (
        not isinstance(path, str)
        or not path
        or not isinstance(cmd, str)
        or cmd not in _WRITE_COMMANDS
    ):
        return None
    # The path comes from the tool call, so its length is not this function's to
    # trust: an entry keeps its path even when the turn budget drops its content,
    # so an unbounded one would ride onto the message no matter what that budget
    # says. Past _MAX_SNAPSHOT_PATH_CHARS no supported OS can open it anyway.
    if len(path) > _MAX_SNAPSHOT_PATH_CHARS:
        return None
    # Refuse sensitive paths even before the write executes (the file may not
    # exist yet for `create`, which makes _safe_read_snapshot return None for
    # a different reason). validate_file_path is the same hooks.py helper used
    # by the LLM-tool intercept layer, so the security boundary is identical.
    # The canonical form rides on every snapshot: this runs off the event loop,
    # and _flush_file_changes buckets spellings of one file by it without
    # resolving paths itself.
    canonical = validate_file_path(path)
    if canonical is None:
        return None

    # strReplace: the content block's oldText is only the replaced fragment,
    # never the full file — reconstruct the true full-file before from disk +
    # reverse substitution. Falls through to the generic chain when
    # reconstruction is impossible; an undecidable file rides along as
    # ``pending_str_replace`` so _flush_file_changes can settle it against
    # the turn-end after-content instead of shipping the fragment.
    pending: dict[str, Any] | None = None
    if cmd == "strReplace":
        before_full, undecidable = _classify_str_replace_before(path, raw_params)
        if before_full is not None:
            before = _truncate_snapshot(before_full)
            return {
                "path": path,
                "canonical_path": canonical,
                "content": before.content,
                "truncated": before.truncated,
            }
        if undecidable is not None:
            pending = dict(
                _pending_str_replace_payload(
                    undecidable, raw_params["oldStr"], raw_params["newStr"]
                )
            )

    # Prefer authoritative content-block before-text when available.
    if diff_old_text is not None:
        # diff_old_text == "" means "file was created" (no previous content).
        # Apply truncation so content-block-sourced text obeys the same cap as
        # disk-sourced text (security + message-meta size invariant).
        before = _truncate_snapshot(diff_old_text)
        snapshot = {
            "path": path,
            "canonical_path": canonical,
            "content": before.content,
            "truncated": before.truncated,
        }
        if pending is not None:
            snapshot["pending_str_replace"] = pending
        return snapshot

    # Fallback: read from disk (correct on the blocking permission-request path
    # where the write has NOT yet executed).
    content = _safe_read_snapshot(path)
    if content is None:
        # File doesn't exist yet (`create` on a new file is the common case)
        # OR was unreadable. Either way, record an empty before so the chip
        # still surfaces.
        return {"path": path, "canonical_path": canonical, "content": "", "truncated": False}
    snapshot = {
        "path": path,
        "canonical_path": canonical,
        "content": content.content,
        "truncated": content.truncated,
    }
    if pending is not None:
        snapshot["pending_str_replace"] = pending
    return snapshot


def _apply_turn_snapshot_budget(
    entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Spend the turn's snapshot budget on its most recent work.

    Charges path+before+after in LAST-WRITE order, newest first, keeping content
    while the running total fits ``_MAX_TURN_SNAPSHOT_CHARS``. An entry past
    that point keeps its path and loses its content if the path fits; otherwise
    the whole entry is dropped. Retained paths are charged even when content is
    omitted. ``_last_write`` carries that order from the dedupe loop; an entry
    without it is charged in list order, so a caller
    that builds entries directly still gets newest-last semantics.

    One entry is protected from the budget: the most recent one whose content
    actually differs. A protected slot spent on an idempotent write -- a
    format-on-save whose before equals its after, which this flush deliberately
    keeps -- would drop the turn's only real diff to store a diff of nothing. The
    protected entry is kept whole and is NOT charged: the budget governs the
    other entries, so a turn that edits one large file and one small one keeps
    both diffs, and a turn always shows the diff for the change that just
    happened. The stored total is therefore bounded by the budget plus one
    entry's worst case (two per-file caps, their truncation markers and one
    path at ``_MAX_SNAPSHOT_PATH_CHARS``), as the constant's comment states.

    Per-entry metadata also costs space, so the character budget leaves the
    ROW COUNT unbounded. ``_MAX_TURN_SNAPSHOT_ENTRIES`` bounds that separately:
    past it the oldest entries are dropped rather than kept as paths, so every
    field this function retains is bounded -- content and paths by the budget,
    each path by ``_MAX_SNAPSHOT_PATH_CHARS`` at admission, and their number by
    the cap.

    Returns the entries reordered so the ones that kept their content come
    first, then how many were demoted and how many dropped. The card renders only
    its first rows before a "show more" fold, so a reader who opens one turn sees
    the diffs it kept rather than a screen of notices with the real change folded
    away.
    """

    def entry_chars(entry: dict[str, Any]) -> int:
        return (
            len(entry.get("path") or "")
            + len(entry.get("before") or "")
            + len(entry.get("after") or "")
        )

    by_recency = sorted(
        range(len(entries)),
        key=lambda index: entries[index].get("_last_write", index),
        reverse=True,
    )
    # The protected entry is kept whole and stays outside the budget: charging
    # it would let one entry at the per-file cap consume the whole budget by
    # itself and demote a second file of ANY size. It counts toward the row cap.
    protected_index = next(
        (i for i in by_recency if entries[i].get("before") != entries[i].get("after")),
        None,
    )
    kept_chars = 0
    demoted = 0
    dropped: list[int] = []
    retained = 1 if protected_index is not None else 0
    for index in by_recency:
        if index == protected_index:
            continue
        entry = entries[index]
        path_chars = len(entry.get("path") or "")
        if (
            retained >= _MAX_TURN_SNAPSHOT_ENTRIES
            or kept_chars + path_chars > _MAX_TURN_SNAPSHOT_CHARS
        ):
            dropped.append(index)
            continue
        retained += 1
        if kept_chars + entry_chars(entry) <= _MAX_TURN_SNAPSHOT_CHARS:
            kept_chars += entry_chars(entry)
            continue
        entry.update(
            before="",
            after="",
            truncated=True,
            content_omitted=True,
            turn_budget_chars=_MAX_TURN_SNAPSHOT_CHARS,
        )
        entry.pop("snapshot_limit_chars", None)
        kept_chars += path_chars
        demoted += 1
    for entry in entries:
        entry.pop("_last_write", None)
    dropped_set = set(dropped)
    survivors = [e for i, e in enumerate(entries) if i not in dropped_set]
    ordered = [e for e in survivors if not e.get("content_omitted")]
    ordered += [e for e in survivors if e.get("content_omitted")]
    return ordered, demoted, len(dropped)


# Appended to a redacted path cut at its bound.
_PATH_TRUNCATION_MARKER = "..."


def _record_turn_snapshot(
    slot: "_ChatSlot", snapshot: dict[str, Any], writer_key: str = ""
) -> None:
    """Hold one entry per distinct path, ordered by last write.

    A known path moves to the tail and keeps its first before-snapshot and
    truncation flag, so the list grows only by distinct path. Every path is
    held: the row cap and the character budget apply at flush, newest-first,
    and the WakaTime line count reads this same list.

    Identity is the ``canonical_path`` each snapshot carries (the
    ``validate_file_path`` result computed off the event loop), falling back to
    the raw ``path`` only when a snapshot has none (a hand-built test entry).
    ``_flush_file_changes`` buckets by that same canonical key, so matching on
    the raw path here would let two spellings of one file (``/work/f`` and
    ``/work/./f``) record as two entries: a later write under the first
    spelling would then move the ORIGINAL snapshot behind the alias's, and the
    flush — keeping the first entry per canonical key — would persist the
    alias's before and silently drop the true original.

    ``writer_key`` is the bounded identity of the tool call that produced this
    snapshot (``_tcid_identity_key`` of the redacted ``tool_call_id``, ``""``
    for an unknown writer). It gates a deferred ``pending_str_replace``: a
    pending snapshot stays resolvable only while EVERY write recorded for its
    canonical path in the turn carries the SAME known id. The first write
    stamps the held entry with its id; a later write to that path with a
    DIFFERENT id — or an unknown id on either side (``""``) — drops the held
    entry's ``pending_str_replace`` so the flush keeps the fragment instead of
    settling it. This closes the wrong-before case where the first snapshot
    lands post-write and a second identical substitution in the same turn
    (a repeated tool call, a shell ``sed``, another session) leaves the
    turn-end file equal to ``if_pre_write``: without the gate the resolver
    would return the after-first-edit content as the before and under-report
    the change with no flag. The gate fails closed — an unknown writer is
    treated as a different one — so a resolution survives only the
    single-known-writer case the deferral is actually safe for.
    """
    key = snapshot.get("canonical_path") or snapshot["path"]
    for index, held in enumerate(slot._file_changes):
        if (held.get("canonical_path") or held.get("path")) == key:
            # A second writer to this path makes a deferred before unsafe:
            # drop the pending hypothesis unless this write and the held entry
            # share one known id, so the flush falls back to the fragment.
            if held.get("pending_str_replace") is not None and (
                not writer_key or held.get("writer_key") != writer_key
            ):
                held.pop("pending_str_replace", None)
            slot._file_changes.append(slot._file_changes.pop(index))
            return
    if writer_key:
        snapshot["writer_key"] = writer_key
    slot._file_changes.append(snapshot)


def _line_change_input(fc: dict[str, Any], after: _Snapshot | None) -> dict[str, str] | None:
    """One before/after pair for ``line_changes_from_file_changes``, or None.

    An unreadable after is unknown, not an empty file: diffing the before against
    "" would fabricate a full-file deletion. A truncated snapshot on either side
    is a partial file and would report a false total. Both sources carry a
    truncation flag (the before on the change entry, the after on the snapshot),
    so either one leaves the pair uncounted rather than miscounted.
    """
    if after is None or fc.get("truncated") or after.truncated:
        return None
    return {"content": fc.get("content") or "", "after": after.content}


def _turn_line_changes(changes: Any) -> int:
    """The turn's WakaTime line count over every distinct path it wrote.

    Each path is counted once, from its first before to the file on
    disk now. Reads the disk and runs quadratic diffs, so callers on the event
    loop offload it.
    """
    seen: set[str] = set()
    resolved: list[dict[str, str]] = []
    if isinstance(changes, list):
        for fc in changes:
            if not isinstance(fc, dict):
                continue
            path = fc.get("path")
            if not isinstance(path, str) or path in seen:
                continue
            seen.add(path)
            pair = _line_change_input(fc, _safe_read_snapshot(path))
            if pair is not None:
                resolved.append(pair)
    return line_changes_from_file_changes(resolved)


def _turn_rows(
    slot: "_ChatSlot", turn_boundary: int, turn_start_mid: str | None
) -> list[dict[str, Any]]:
    """The window rows appended since this turn began.

    ``turn_start_mid`` is the ``meta.mid`` of the row at the window's tail when
    the turn began, ``""`` when the window was empty. The window is
    front-trimmed at ``_MAX_SLOT_MESSAGES`` and rewritten by a mid-turn clear,
    so a position captured at turn start can stop naming this turn's first row;
    a row identity cannot. Rows after the identified row are this turn's, and a
    tail row that is gone (trimmed away, or cleared) means every row still in
    the window arrived after it.

    ``None`` means the tail row carried no id (a transcript restored from a
    disk format that predates row ids). Then ``turn_boundary``, the window
    index ``_attach_turn_stats`` also scopes to, is the only signal: exact while
    nothing has been trimmed, and clamped to the window otherwise, which yields
    no rows rather than an earlier turn's.
    """
    rows = slot.messages
    if turn_start_mid is not None:
        if turn_start_mid == "":
            return rows
        for index in range(len(rows) - 1, -1, -1):
            if row_mid(rows[index]) == turn_start_mid:
                return rows[index + 1 :]
        return rows
    return rows[min(max(0, turn_boundary), len(rows)) :]


def _note_reply_row(slot: "_ChatSlot", row: dict[str, Any]) -> None:
    """Record ``row`` as a reply the runner in flight appended this turn.

    ``_flush_file_changes`` anchors the turn's chips only to a row recorded
    here. The window is shared: a workflow or sub-agent completion is appended
    into it as an assistant row by another writer while a turn runs
    (``workflow_inject.py``), and position alone cannot tell that row from this
    turn's reply. Identity can. ``getattr`` because test doubles built on
    MagicMock have no list to append to.

    Bounded at ``_MAX_SLOT_MESSAGES``, the window's own cap: a row older than
    that many appends has been front-trimmed out of the window and can no
    longer be an anchor, so its id is dropped from the head of the list. The
    ids are runner-minted (``mint_row_mid``), fixed-length and never
    externally supplied.
    """
    mids = getattr(slot, "_turn_reply_mids", None)
    mid = row_mid(row)
    if isinstance(mids, list) and mid:
        if len(mids) >= _MAX_SLOT_MESSAGES:
            del mids[0]
        mids.append(mid)


def replace_regenerate_target(slot: "_ChatSlot", keep_row: dict[str, Any]) -> None:
    """Remove the marked old-reply rows from the live window, keeping ``keep_row``.

    The single "first content exists" action of an inverted Regenerate: the new
    reply (``keep_row``, already appended) replaces the previous reply by
    DELETING the marked old-reply rows in place. Matched by stable id (robust
    against a mid-turn front-trim or injection shifting positions), with an
    object-identity fallback for rows that carried no id. ``keep_row`` and any
    row the turn itself appended are never removed — only the pre-turn rows the
    marker names. Idempotent and a no-op when no regenerate is pending. Clears
    the markers so a later unrelated flush cannot re-trigger a removal.
    """
    replacing_mids = set(getattr(slot, "_regenerate_replacing_mids", []) or [])
    replacing_rows = getattr(slot, "_regenerate_replacing_rows", []) or []
    replacing_row_ids = {id(r) for r in replacing_rows}
    if replacing_mids or replacing_row_ids:
        kept: list[dict] = []
        for m in slot.messages:
            if m is keep_row:
                kept.append(m)
                continue
            m_mid = row_mid(m)
            if (m_mid and m_mid in replacing_mids) or id(m) in replacing_row_ids:
                continue  # drop the old reply row
            kept.append(m)
        slot.messages = kept
    slot._regenerate_replacing_mids = []
    slot._regenerate_replacing_rows = []


def _append_local_command_reply(
    slot: "_ChatSlot", body: str, cls: str = "msg msg-a"
) -> dict[str, Any]:
    """Append a local (slash) command's assistant reply, replacing a regenerate target.

    A local command (``/goal``, ``/workflow``, ``/prompts``, a blocked command,
    ``/compact``) answers inside ``_run_chat`` with a plain assistant row and
    returns WITHOUT going through ``_flush_segment`` — the single point where an
    inverted Regenerate swaps the old reply out for the new one. So a regenerate
    whose re-sent user message routes to a local command would otherwise leave
    the old reply live beside the new one (``user → OLD reply → NEW reply``).
    Routing every local-command append through here applies the same
    replace-on-first-content as ``_flush_segment``: the fresh row replaces the
    marked old-reply rows and adopts their variant chain. Returns the row.
    """
    row = slot.append("assistant", body, cls)
    _note_reply_row(slot, row)
    pending = getattr(slot, "_pending_variants", None)
    # Only the regenerate turn that armed the markers may consume them. A queued
    # successor (dispatched out of the prior turn's shielded tail before its
    # cleanup runs) carries a bumped turn generation, so it leaves the markers
    # for that turn's finally to clear rather than grafting the old reply here.
    # Both reads are defensive: a slot that carries no regenerate state at all
    # (an unarmed generation of -1 can never equal a turn generation of 0) is
    # simply not an owner, so a lightweight slot without these attributes takes
    # the no-op path rather than raising.
    owns = getattr(slot, "_regenerate_replacing_generation", -1) == getattr(
        slot, "_turn_generation", 0
    )
    if pending and owns:
        replace_regenerate_target(slot, row)
        variants = [v for v in pending if isinstance(v, dict)]
        variants.append({"content": body, "ts": row.get("ts", "")})
        row["variants"] = variants
        row["variant_idx"] = len(variants) - 1
        slot._pending_variants = []
        slot._pending_rewrite = True
        slot._dirty = True
        slot.invalidate_source_links()
    return row
