"""Pre-write file snapshots, the line changes a turn made, the per-turn snapshot budget and
the end-of-turn file-change flush."""

from __future__ import annotations

import difflib
import hashlib
import os
import stat as stat_module
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from kiro_crew.dashboard.state import row_mid
from kiro_crew.hooks import safe_read_file_bytes_nolink
from kiro_crew.security import redact
from kiro_crew.wakatime.heartbeats import line_changes_from_file_changes

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _MAX_SLOT_MESSAGES,
        _MAX_TURN_FULL_CONTENT_CHARS,
        _ChatSlot,
        logger,
        safe_read_file,
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
    return _safe_read_file_text(path, whole=False)


def _safe_read_file_text(path: str, *, whole: bool) -> _Snapshot | None:
    """The one gated read behind ``_safe_read_snapshot``; see it for the gate.

    ``whole=True`` returns the WHOLE file, uncut (``truncated`` False), or None
    when it exceeds ``_MAX_RECONSTRUCT_BYTES`` -- the bound the strReplace
    reconstruction already reads to. A cut read is not the whole file and must
    not be compared as one, so one byte past the bound is read to tell a larger
    file apart, and it is refused rather than cut.
    """
    max_bytes = _MAX_RECONSTRUCT_BYTES if whole else _SNAPSHOT_READ_BYTES
    try:
        validated = validate_file_path(path)
        if validated is None:
            return None
        raw = safe_read_file_bytes_nolink(
            validated,
            within_root=os.path.dirname(validated),
            max_bytes=max_bytes + 1 if whole else max_bytes,
            allow_truncate=True,
        )
        if raw is None or (whole and len(raw) > max_bytes):
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
        return _Snapshot(text, False) if whole else _truncate_snapshot(text)
    except Exception:
        return None


def _budgeted_opcodes(
    a: list[str], b: list[str], budget: int
) -> Sequence[tuple[str, int, int, int, int]] | None:
    """``SequenceMatcher(None, a, b).get_opcodes()``, or None past ``budget``.

    ``find_longest_match`` walks, for each line of its ``a`` range, every place
    that line occurs in ``b``. On a file of repeated rows (a log, a CSV, fixture
    data) no line is rare enough for autojunk and too common to be skipped, so
    the walk grows with lines times repeats, and many changes repeat it per
    block: a 1.3 MB file with one line in fifty changed took minutes. Each call
    is charged that walk's upper bound before it runs; once the total passes
    ``budget`` the diff stops and returns None. The charge is computed in one
    pass over the range, never more than the walk itself costs.
    """

    class _OverBudget(Exception):
        pass

    class _Matcher(difflib.SequenceMatcher):
        spent = 0

        def find_longest_match(
            self, alo: int = 0, ahi: int | None = None, blo: int = 0, bhi: int | None = None
        ) -> difflib.Match:
            # ``a`` and ``b2j`` are set by ``set_seqs``; the stubs do not declare them.
            seqs: Any = self
            ahi = len(seqs.a) if ahi is None else ahi
            occurs = seqs.b2j
            self.spent += (ahi - alo) + sum(len(occurs.get(line, ())) for line in seqs.a[alo:ahi])
            if self.spent > budget:
                raise _OverBudget
            return super().find_longest_match(alo, ahi, blo, bhi)

    try:
        return _Matcher(None, a, b).get_opcodes()
    except _OverBudget:
        return None


def _excerpt_pair(before: str, after: str) -> tuple[str, str] | None:
    """Cut both whole sides to windows around each change, or None if they won't fit.

    The per-file cap keeps a large file's FIRST ``_MAX_SNAPSHOT`` characters, so
    an edit past that point leaves two identical prefixes: the diff is empty and
    the entry reads as unchanged. Here each changed line run (``difflib``
    opcodes) gets a window with ``_EXCERPT_CONTEXT_LINES`` of context, windows
    that touch are merged, and every run of lines left out -- above, between,
    below -- becomes one in-band ``... (N lines not shown)`` line. Left-out runs
    are equal on both sides, so the windowed diff shows exactly the lines the
    turn changed and counts them correctly.

    Returns None when the sides are identical, a side would exceed
    ``_MAX_SNAPSHOT``, or the line diff passes ``_EXCERPT_DIFF_BUDGET``; the
    caller then keeps the top cut and the truncated flag. Runs off the event
    loop (``_read_turn_afters``), over up to two megabytes per side. The lines
    both sides share at the top and bottom are set aside before the diff, so a
    turn's usual one or two nearby edits diff only the lines between them.
    """
    if before == after:
        return None
    # The longer side holds at least the length gap in changed text, so its
    # window cannot fit; no diff needed to know.
    if abs(len(before) - len(after)) > _MAX_SNAPSHOT:
        return None
    a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
    shortest = min(len(a), len(b))
    head = 0
    while head < shortest and a[head] == b[head]:
        head += 1
    tail = 0
    while tail < shortest - head and a[-1 - tail] == b[-1 - tail]:
        tail += 1
    opcodes = _budgeted_opcodes(
        a[head : len(a) - tail], b[head : len(b) - tail], _EXCERPT_DIFF_BUDGET
    )
    if opcodes is None:
        return None
    ctx = _EXCERPT_CONTEXT_LINES
    # Changed runs as [a_start, a_end, b_start, b_end]; a gap of at most two
    # contexts joins the run to the one before it.
    runs: list[list[int]] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        i1, i2, j1, j2 = i1 + head, i2 + head, j1 + head, j2 + head
        if runs and i1 - runs[-1][1] <= 2 * ctx:
            runs[-1][1], runs[-1][3] = i2, j2
        else:
            runs.append([i1, i2, j1, j2])
    # Widen each run by its context. The lines it takes are equal on both
    # sides, and every gap left between runs is longer than two contexts.
    for run in runs:
        lead = min(ctx, run[0], run[2])
        trail = min(ctx, len(a) - run[1], len(b) - run[3])
        run[0], run[2] = run[0] - lead, run[2] - lead
        run[1], run[3] = run[1] + trail, run[3] + trail
    sides = []
    for lines, lo, hi in ((a, 0, 1), (b, 2, 3)):
        out, shown_to = [], 0
        for run in runs:
            if run[lo] > shown_to:
                out.append(f"... ({run[lo] - shown_to} lines not shown)\n")
            out.extend(lines[run[lo] : run[hi]])
            shown_to = run[hi]
        if shown_to < len(lines):
            # Only the file's last line can lack a newline, and it is hidden here.
            out.append(f"... ({len(lines) - shown_to} lines not shown)\n")
        sides.append("".join(out))
    if max(len(sides[0]), len(sides[1])) > _MAX_SNAPSHOT or sides[0] == sides[1]:
        return None
    return sides[0], sides[1]


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

    Every hypothesis is ``_truncate_snapshot``-capped, so these two cost no more
    than an ordinary snapshot. The raw disk read (up to
    ``_MAX_RECONSTRUCT_BYTES``) is held only as the payload's separate
    ``whole_candidate``, which ``_snapshot_write_target`` adds and
    ``_record_turn_snapshot`` bounds per turn. ``if_post_write`` is the disk content as seen at snapshot time
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
    ``after == if_pre_write`` test and is never settled here; the runner then
    keeps only the call's path, or drops it when content proves no change
    (``_settle_failed_write``).

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

    A before PROVEN to be the whole file (the strReplace reconstruction, a
    disk read, or create's content block) also carries ``full_content``, at
    most ``_MAX_RECONSTRUCT_BYTES``, so the flush can tell a change past the
    per-file cut from no change and window the stored sides on it
    (``_excerpt_pair``). A strReplace or insert content block is a fragment and
    never carries it. It lives only in the in-turn accumulator, bounded per
    turn by ``_record_turn_snapshot``, and never reaches message meta.

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
            snapshot = {
                "path": path,
                "canonical_path": canonical,
                "content": before.content,
                "truncated": before.truncated,
            }
            # The proven whole-file before, bounded by _MAX_RECONSTRUCT_BYTES
            # in the classifier.
            snapshot["full_content"] = before_full
            return snapshot
        if undecidable is not None:
            pending = dict(
                _pending_str_replace_payload(
                    undecidable, raw_params["oldStr"], raw_params["newStr"]
                )
            )
            # The capped hypotheses above agree past the cut, so they cannot
            # settle an edit there. Keep the WHOLE disk content as a candidate
            # before (bounded by _MAX_RECONSTRUCT_BYTES in the classifier, and
            # per turn by _record_turn_snapshot); _read_turn_afters accepts it
            # only on the same pre-write test run on whole texts. It rides in
            # the pending payload, so the writer gate drops it with the rest.
            pending["whole_candidate"] = (undecidable, raw_params["oldStr"], raw_params["newStr"])

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
        # Only create's content block is the whole previous file; strReplace
        # and insert carry a fragment, which must never be diffed as one.
        if cmd == "create" and len(diff_old_text) <= _MAX_RECONSTRUCT_BYTES:
            snapshot["full_content"] = diff_old_text
            # The backend builds this block before it writes, so it is the file
            # as the call found it whenever the dashboard reads the event; a disk
            # read here could already hold part of the write. It lets a failed
            # call be shown to have left the file as it was (_settle_failed_write).
            snapshot["pre_call_digest"] = _disk_digest(diff_old_text)
        if pending is not None:
            snapshot["pending_str_replace"] = pending
        return snapshot

    # Fallback: read from disk (correct on the blocking permission-request path
    # where the write has NOT yet executed). One read: the whole file when it is
    # within _MAX_RECONSTRUCT_BYTES (capped here exactly as the ordinary read
    # caps it), else the ordinary capped read and no whole text.
    whole = _safe_read_file_text(path, whole=True)
    content = _truncate_snapshot(whole.content) if whole is not None else _safe_read_snapshot(path)
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
    if whole is not None:
        snapshot["full_content"] = whole.content
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
    differs or may differ (a truncated side hides what lies past it). A protected slot spent on an idempotent write -- a
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
    # A truncated entry MAY differ: two equal top cuts say nothing about the text
    # past them, so an edit there (no windows: a cancel, a file over the whole-read
    # bound, windows that would not fit) still counts as a change.
    protected_index = next(
        (
            i
            for i in by_recency
            if not entries[i].get("content_omitted")
            and (entries[i].get("truncated") or entries[i].get("before") != entries[i].get("after"))
        ),
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
        if (held.get("canonical_path") or held.get("path")) == key and held.get("unverified_write"):
            # A failed write left this path unprovable, and its before is not
            # the file as this write found it. This write's own snapshot is, so
            # it replaces the held entry: the card shows what this write
            # changed, never a hunk built on the failed call's before.
            slot._file_changes.pop(index)
            break
    for index, held in enumerate(slot._file_changes):
        if (held.get("canonical_path") or held.get("path")) == key:
            # A second writer to this path makes a deferred before unsafe:
            # drop the pending hypothesis unless this write and the held entry
            # share one known id, so the flush falls back to the fragment.
            if not writer_key or held.get("writer_key") != writer_key:
                held.pop("pending_str_replace", None)
                # Two writers, or an unknown one: a failure of either cannot void it.
                held.pop("writer_key", None)
            slot._file_changes.append(slot._file_changes.pop(index))
            break
    else:
        if writer_key:
            snapshot["writer_key"] = writer_key
        slot._file_changes.append(snapshot)
    # Whole texts held for the flush (``full_content``, a pending candidate's
    # ``whole_candidate``) are up to _MAX_RECONSTRUCT_BYTES each and the list
    # grows by distinct path, so they are bounded per turn: the newest writes
    # keep theirs -- the flush protects the most recent changed entry -- and
    # past _MAX_TURN_FULL_CONTENT_CHARS older ones drop it and keep the top cut.
    total = 0
    for held in reversed(slot._file_changes):
        holders: tuple[tuple[Any, str], ...] = (
            (held, "full_content"),
            (held.get("pending_str_replace"), "whole_candidate"),
        )
        for holder, name in holders:
            whole = holder.get(name) if isinstance(holder, dict) else None
            if whole is None:
                continue
            size = len(whole) if isinstance(whole, str) else len(whole[0])
            total += size
            if total > _MAX_TURN_FULL_CONTENT_CHARS:
                holder.pop(name)
                total -= size


def _disk_digest(text: str) -> str:
    """A fixed-size fingerprint of a file's text, newlines normalized as reads do."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _failed_write_entries(slot: "_ChatSlot", writer_key: str) -> list[dict[str, Any]]:
    """The entries a write that failed or was refused recorded alone.

    Only entries whose every write came from this one call qualify; one another
    writer also touched has no ``writer_key`` (``_record_turn_snapshot``) and
    keeps its first before, since a later write may have changed the file.
    ``_settle_failed_write`` decides what each one shows.
    """
    changes = getattr(slot, "_file_changes", None)
    if not writer_key or not isinstance(changes, list):
        return []
    return [held for held in changes if held.get("writer_key") == writer_key]


def _settle_failed_write(
    entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a failed or refused call's entries into (no change, changed).

    Content decides, never the backend's status word: neither ``failed`` nor
    ``refused`` proves the tool left the file alone. A whole pre-call snapshot
    (``pre_call_digest``: create's content block, built by the backend before it
    writes) is compared with a fresh whole read: equal means no change, so the
    entry goes; different means real bytes changed, so it stays and shows that
    diff. Without one the outcome is unprovable: the before is a fragment, a
    reverse substitution or a disk read that may already hold part of the
    write, so neither "no change" nor the requested hunk can be claimed.
    Unprovable entries are returned in neither list and stay ``unverified_write``.

    Reads up to ``_MAX_RECONSTRUCT_BYTES`` per path, so it runs off the loop.
    A file that is gone, unreadable or past the bound is unprovable too.
    """
    same: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for held in entries:
        digest = held.get("pre_call_digest")
        if not digest:
            continue
        now = _safe_read_file_text(held["path"], whole=True)
        if now is not None:
            (same if _disk_digest(now.content) == digest else changed).append(held)
    return same, changed


def _forget_entries(slot: "_ChatSlot", entries: list[dict[str, Any]]) -> None:
    """Remove exactly these held entries (by identity) from the turn's list."""
    changes = getattr(slot, "_file_changes", None)
    if not entries or not isinstance(changes, list):
        return
    drop = {id(held) for held in entries}
    changes[:] = [held for held in changes if id(held) not in drop]


def _read_turn_afters(
    changes: Any, turn_had_shell: bool = False
) -> dict[str, tuple[_Snapshot | None, tuple[str, str] | None]]:
    """Each distinct path's turn-end after, and its whole before/after when cut.

    For the runner's ``_turn_after_reads``: keyed like the flush buckets
    (canonical path, else raw path), first entry per key, valued
    ``(capped after, (whole before, whole after) or None)``. A path with a whole
    before gets one WHOLE read of its after (capped back for the stored side);
    the whole pair is returned only when a side passes the per-file cut, for the
    runner to redact and window (``_excerpt_pair``). The whole
    before is ``full_content``, or a pending strReplace's ``whole_candidate``
    accepted on the pre-write test the capped resolver runs, here on whole
    texts: the edit applied to the disk content gives the turn-end file, which
    differs from it. Like the capped resolver it never settles the post-write
    case (the disk already held the after): completion and unchanged bytes
    cannot tell that from a concurrent restore, so such an edit keeps the top
    cut. A shell in the turn, or a second writer, blocks it, as they block the
    capped resolver. Every other path gets the ordinary capped read.

    Reads up to two megabytes per path, so it runs OFF the event loop inside
    ``_turn_after_reads``. A flush given no reads reads the capped snapshot
    inline and never reads the whole file or diffs.
    """
    reads: dict[str, tuple[_Snapshot | None, tuple[str, str] | None]] = {}
    if not isinstance(changes, list):
        return reads
    for fc in changes:
        if not isinstance(fc, dict) or not isinstance(fc.get("path"), str):
            continue
        key = fc.get("canonical_path") or fc["path"]
        if key in reads:
            continue
        before = fc.get("full_content")
        pending = fc.get("pending_str_replace")
        candidate = pending.get("whole_candidate") if isinstance(pending, dict) else None
        whole = None
        if before is not None or (candidate is not None and not turn_had_shell):
            whole = _safe_read_file_text(fc["path"], whole=True)
        if whole is None:
            reads[key] = (_safe_read_snapshot(fc["path"]), None)
            continue
        if before is None and candidate is not None:
            disk, old, new = candidate
            if whole.content != disk and whole.content == disk.replace(old, new, 1):
                before = disk
        after = _truncate_snapshot(whole.content)
        cut = after.truncated or (before is not None and len(before) > _MAX_SNAPSHOT)
        reads[key] = (after, (before, whole.content) if cut and before else None)
    return reads


def _line_change_input(fc: dict[str, Any], after: _Snapshot | None) -> dict[str, str] | None:
    """One before/after pair for ``line_changes_from_file_changes``, or None.

    An unreadable after is unknown, not an empty file: diffing the before against
    "" would fabricate a full-file deletion. A truncated snapshot on either side
    is a partial file and would report a false total. Both sources carry a
    truncation flag (the before on the change entry, the after on the snapshot),
    so either one leaves the pair uncounted rather than miscounted.
    """
    if after is None or fc.get("truncated") or after.truncated or fc.get("unverified_write"):
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


# ── File-chip snapshots ────────────────────────────────────────────────────
# When the agent invokes a write tool, capture the file's content BEFORE the
# write executes. After the turn ends, capture the AFTER content and attach
# {path, before, after} entries to the assistant message meta. Frontend
# renders these as file-change chips with click-through to a Monaco diff.

_WRITE_COMMANDS = frozenset({"create", "strReplace", "insert"})


_MAX_SNAPSHOT = 200_000  # cap per-file snapshot to bound message meta size


# Appended to a snapshot side cut at ``_MAX_SNAPSHOT``, so one stored side is at
# most ``_MAX_SNAPSHOT`` plus this marker.
_SNAPSHOT_TRUNCATION_MARKER = f"\n... (truncated at {_MAX_SNAPSHOT} chars)"


# Reconstruction reads the whole file synchronously on the event loop; past
# this size the stored snapshot is truncated to _MAX_SNAPSHOT anyway, so
# reconstruction declines instead of stalling the loop on a huge file.
_MAX_RECONSTRUCT_BYTES = 2_000_000


# Unchanged lines ``_excerpt_pair`` shows around each changed run of a windowed
# large-file diff. Runs closer than twice this share one window.
_EXCERPT_CONTEXT_LINES = 20


# Work ``_excerpt_pair``'s line diff may do, in line-occurrence visits
# (``_budgeted_opcodes``). Repeated lines make that diff superlinear; past this
# the windows are given up and the top cut is kept. About half a second of CPU.
_EXCERPT_DIFF_BUDGET = 4_000_000


# Chars of path+before+after one TURN's file_changes may carry across every
# entry EXCEPT its protected one -- the most recent entry whose content differs
# or, truncated, may differ,
# which ``_apply_turn_snapshot_budget`` keeps whole and never charges here.
# ``_MAX_SNAPSHOT`` bounds one file; without a turn bound a single turn that
# rewrites a dozen large files still attaches megabytes to one message, and
# every later read of that transcript pays for it. Two per-file caps' worth, so
# beside the protected entry the budget holds paths and one more large
# before/after. An entry truncated on BOTH sides exceeds it and is demoted.
# The most one turn stores is this budget plus the protected entry: at most
# ``2 * _MAX_SNAPSHOT`` plus two markers and ``_MAX_SNAPSHOT_PATH_CHARS``.
_MAX_TURN_SNAPSHOT_CHARS = 2 * _MAX_SNAPSHOT


# Chars of PATH one snapshot entry may carry. The path is LLM-supplied and is
# retained even for an entry whose content the turn budget drops, so without a
# bound here one tool call naming a megabyte-long path puts that megabyte on the
# message. The bound sits at the longest path a supported OS can open: Windows
# with the extended-length prefix (``\\?\``, which ``strip_extended_length_prefix``
# handles) reaches 32,767 characters, well past Linux's 4,096 and macOS's 1,024,
# so a longer string cannot name a file anywhere and refusing it loses no real
# snapshot.
_MAX_SNAPSHOT_PATH_CHARS = 32_767


# Entries one turn's file_changes may carry. Bounding content and path length
# still leaves the ROW COUNT open, and a path-only row is not free: without this
# a turn touching thousands of files puts thousands of rows on one message. Far
# above any real turn -- the largest observed touched 11 files. The flush applies
# it newest-first (``_apply_turn_snapshot_budget``), so a turn past it keeps the
# files it wrote last and reports how many files it dropped. The in-turn
# accumulator holds every distinct path once, so a repeated write adds no row.
_MAX_TURN_SNAPSHOT_ENTRIES = 200


# Bytes the snapshot read pulls before ``_truncate_snapshot`` caps it. A UTF-8
# code point is at most four bytes, so ``_MAX_SNAPSHOT`` characters never span
# more than ``4 * _MAX_SNAPSHOT`` bytes — reading one code point's worth beyond
# that keeps the marker honest: a file longer than the cap always decodes to
# MORE than ``_MAX_SNAPSHOT`` characters, whether it was cut here or read whole,
# so the truncation marker is appended exactly when the file exceeds the cap.
_SNAPSHOT_READ_BYTES = 4 * _MAX_SNAPSHOT + 4


def _cap_redacted(text: str, limit: int, marker: str) -> tuple[str, bool]:
    """Hold redacted text to ``limit`` chars plus ``marker``; say whether it was cut.

    The snapshot bounds apply before redaction, and redaction can grow text: each
    short credential becomes a 22-character tag, so a side dense with them grows
    about threefold. Re-applying the bound to the redacted text is what keeps a
    stored field within it -- including the protected entry, which the turn
    budget never charges. Text already within ``limit`` plus the marker is
    returned unchanged, so a side ``_truncate_snapshot`` cut before redaction and
    redaction did not grow keeps its one marker.

    The cut runs on redacted text, so what it can split is a redaction tag's own
    text, never the secret the tag replaced.
    """
    if len(text) <= limit + len(marker):
        return text, False
    return text[:limit] + marker, True


def _turn_after_reads(
    changes: list[dict[str, Any]], turn_had_shell: bool
) -> dict[str, tuple[_Snapshot | None, tuple[str, str] | None]]:
    """``_flush_file_changes``'s ``after_reads``: whole reads, redaction and windows.

    Runs OFF the event loop (the caller wraps it in ``asyncio.to_thread``): reads
    up to two megabytes per path and diffs them. The WHOLE texts are redacted
    before ``_excerpt_pair`` cuts windows: a window can start inside a credential
    (a PEM body below its BEGIN line), and the redactor recognizes one only with
    its delimiters in view. The flush redacts the windows again, which leaves
    already-redacted text unchanged. Whole sides proven equal store two empty
    sides: a no-change entry, not a truncated one that could take the turn
    budget's protected slot from a real change.
    """
    reads: dict[str, tuple[_Snapshot | None, tuple[str, str] | None]] = {}
    for key, (after, whole) in _read_turn_afters(changes, turn_had_shell).items():
        excerpt = None
        if whole:
            excerpt = (
                ("", "")
                if whole[0] == whole[1]
                else _excerpt_pair(redact(whole[0]), redact(whole[1]))
            )
        reads[key] = (after, excerpt)
    return reads


def _flush_file_changes(
    slot: "_ChatSlot",
    turn_boundary: int = 0,
    turn_start_mid: str | None = None,
    turn_had_shell: bool = False,
    after_reads: dict[str, tuple[_Snapshot | None, tuple[str, str] | None]] | None = None,
) -> None:
    """Attach accumulated file changes to this turn's last assistant message.

    ``after_reads`` is ``_turn_after_reads`` run off the event loop by the
    caller: each path's capped after and, for a change past a large file's
    per-file cut, its windowed before/after, which replace the top cuts here.
    Without it each after is the ordinary capped read, inline, and such a change
    keeps the top cut.

    Dedups by canonical path (first before, last after), reads the AFTER content
    from disk, and settles a ``pending_str_replace`` snapshot against that
    turn-end after-content by content ALONE — only the pre-write hypothesis is
    resolvable, so a settled before is always content captured from disk at
    snapshot time, never a value synthesised from the turn-end state. A pending
    snapshot is settled only when the turn-end state is attributable to a single
    tracked write: ``_record_turn_snapshot`` drops the pending hypothesis when a
    second (or unknown) WRITE TOOL touches the same canonical path, and
    ``turn_had_shell`` drops ALL pending resolution for the turn when a shell
    tool ran — a shell command is opaque, can write any path, and never passes
    through ``_record_turn_snapshot``, so a post-write snapshot followed by a
    shell edit could otherwise leave the resolver settling an intermediate state
    as the before. Both cases fall back to the fragment rather than a wrong
    before (see ``_resolve_pending_str_replace`` for the resolution rule and
    ``_record_turn_snapshot`` for the write-tool gate). Writes the list
    to message meta as ``file_changes``. Files the turn budget dropped are
    counted in ``file_changes_omitted_files``, a plain int present only when it
    is non-zero. Called on every exit path (success / cancel / error) so users
    always see what was modified, even on aborted turns.

    Only rows appended by this turn are scanned (see ``_turn_rows`` for the
    two ways the turn's start is identified), so a turn that changed files but
    ended without an assistant row gets its own synthetic anchor instead of
    writing its chips onto the previous turn's answer. Within those rows the
    anchor must also be a reply THIS runner appended (``slot._turn_reply_mids``,
    filled by ``_note_reply_row``): the live window is shared, and an assistant
    row another writer injected mid-turn (a workflow or sub-agent completion
    envelope) is not this turn's reply even when it is the newest assistant row
    in the turn. A slot that tracks no identities (a test double without the
    list) keeps the position rule.
    """
    # Defensive: only proceed when a real, non-empty list is present. Tests
    # using MagicMock slots leave _file_changes as a MagicMock attribute
    # (always truthy), so an isinstance check is needed in addition to the
    # length check to avoid a synthetic message getting fabricated when no
    # writes actually happened.
    fc_changes = getattr(slot, "_file_changes", None)
    if not isinstance(fc_changes, list) or not fc_changes:
        return
    # Dedup: keep first before for each path (truest "before") since a file
    # may be modified multiple times in one turn.
    deduped: dict[str, dict[str, Any]] = {}
    pending_by_path: dict[str, dict[str, Any]] = {}
    for write_order, fc in enumerate(slot._file_changes):
        p = fc["path"]
        # ``/tmp/f`` and ``/tmp/./f`` share one bucket: the key is the canonical
        # path ``_snapshot_write_target`` computes off the event loop at snapshot
        # time, so this loop resolves no path itself. One bucket keeps the first
        # before for a file written under two spellings.
        key = fc.get("canonical_path") or p
        if key not in deduped:
            before_truncated = bool(fc.get("truncated", False))
            deduped[key] = {
                "path": p,
                "before": fc["content"],
                "after": "",
                "_before_truncated": before_truncated,
            }
            if fc.get("pending_str_replace"):
                pending_by_path[key] = fc["pending_str_replace"]
        if fc.get("unverified_write"):
            deduped[key]["_unverified_write"] = True
        # The accumulator moves a path's first snapshot to the tail on every
        # write, so its position carries last-write recency without extra rows.
        # Assigning on every occurrence also supports directly supplied repeats.
        deduped[key]["_last_write"] = write_order
    # Read after-content once per path. Uses _safe_read_snapshot so sensitive
    # paths and unreadable files yield empty after rather than crashing or
    # leaking credentials.
    for key, entry in deduped.items():
        before_truncated = entry.pop("_before_truncated")
        # The caller's off-loop reads when it made them (_read_turn_afters);
        # otherwise only the ordinary capped snapshot, read inline: no whole-file
        # read or diff on the event loop, so such an entry keeps the top cut.
        read = after_reads.get(key) if after_reads is not None else None
        after, excerpt = read if read is not None else (_safe_read_snapshot(entry["path"]), None)
        entry["after"] = after.content if after is not None else ""
        pending = pending_by_path.get(key)
        # Resolve a deferred strReplace against the turn-end content. The
        # resolver settles only the pre-write hypothesis, so a resolved before
        # is always the content captured from disk at snapshot time. The
        # pending payload is present here only when a single known write tool
        # touched the path this turn — _record_turn_snapshot already dropped it
        # if a second or unknown WRITE TOOL did. A shell tool is opaque and
        # never recorded there, so a shell write after a post-write snapshot
        # could shape the turn-end read into a false pre-write match: when a
        # shell ran this turn (turn_had_shell), skip resolution entirely and
        # keep the fragment rather than trust an unattributable turn-end state.
        if pending is not None and after is not None and not turn_had_shell:
            resolved = _resolve_pending_str_replace(pending, after.content)
            if resolved is not None:
                # Already _MAX_SNAPSHOT-capped by _pending_str_replace_payload;
                # re-truncating would append a second marker.
                entry["before"] = resolved.content
                before_truncated = resolved.truncated
        if excerpt is not None:
            # The per-file cap keeps a side's first _MAX_SNAPSHOT chars, so an
            # edit past it leaves two equal cuts: no diff, and the turn budget
            # does not protect an entry that reads as unchanged. The windows
            # _read_turn_afters cut from the WHOLE sides show every change, and
            # each side is within _MAX_SNAPSHOT, so every bound below holds.
            entry["before"], entry["after"] = excerpt
        elif before_truncated or (after is not None and after.truncated):
            entry.update(truncated=True, snapshot_limit_chars=_MAX_SNAPSHOT)
        if entry.pop("_unverified_write", False):
            # A failed write with no proven content change: no diff can be
            # claimed (_settle_failed_write), so only the path is kept,
            # with the reason the card states.
            entry.update(
                before="",
                after="",
                truncated=True,
                content_omitted=True,
                omitted_reason="unverified_write",
            )
            entry.pop("snapshot_limit_chars", None)
    # Scrub exfil URLs and credentials from path/before/after BEFORE attaching
    # to message meta. _save_slot_to_history runs _redact_meta on persist, but
    # the in-memory slot.messages reaches the dashboard UI via SSE/WS BEFORE
    # persistence — so without this, a config file containing an AKIA* key
    # (path not on the sensitive-path list) would briefly appear in the chip
    # diff. Redact in place so both the live and persisted views are clean.
    # `security.redact` IS the canonical exfil-first composition — the URL pass
    # classifies partly by query length and replaces the whole URL, so a
    # hand-sequenced pair here risks re-introducing the creds-first ordering
    # that defeats it.
    #
    # Redaction can grow text past the bounds applied before it, so each field is
    # held to its bound again afterwards. A side cut here is flagged truncated on
    # the STORED entry only: the WakaTime line count reads the accumulator's own
    # entries, before this flush runs, so the flag cannot drop a file from it.
    for entry in deduped.values():
        entry["path"], _ = _cap_redacted(
            redact(entry["path"]),
            _MAX_SNAPSHOT_PATH_CHARS - len(_PATH_TRUNCATION_MARKER),
            _PATH_TRUNCATION_MARKER,
        )
        side_cut = False
        for side in ("before", "after"):
            if entry[side]:
                entry[side], cut = _cap_redacted(
                    redact(entry[side]), _MAX_SNAPSHOT, _SNAPSHOT_TRUNCATION_MARKER
                )
                side_cut = side_cut or cut
        if side_cut:
            entry.update(truncated=True, snapshot_limit_chars=_MAX_SNAPSHOT)
    # No-op entries (before == after, e.g. an idempotent format-on-save)
    # are deliberately KEPT: the dashboard renders an explicit "no changes"
    # caption for them (FileChangeChips) instead of a contentless diff, so
    # the UI is the single place that answers the no-op state. Dropping them in
    # the backend would compare post-truncation/post-redaction content, which
    # would silently discard real changes past the snapshot limit or inside
    # redacted spans.
    fc_list = list(deduped.values())
    # Bound the whole turn AFTER redaction, so the budget measures the content
    # that actually gets stored rather than the pre-redaction size.
    fc_list, _demoted, _dropped = _apply_turn_snapshot_budget(fc_list)
    file_meta: dict[str, Any] = {"file_changes": fc_list}
    if _dropped:
        file_meta["file_changes_omitted_files"] = _dropped
    # Attach to the most recent reply row this runner appended this turn; if
    # none exists (turn aborted before any text, or every assistant row in the
    # turn was injected by another writer), create a synthetic message so the
    # chips still surface without touching a row that is not this turn's reply.
    reply_mids = getattr(slot, "_turn_reply_mids", None)
    for m in reversed(_turn_rows(slot, turn_boundary, turn_start_mid)):
        if m.get("role") != "assistant":
            continue
        if isinstance(reply_mids, list) and row_mid(m) not in reply_mids:
            continue
        meta = m.setdefault("meta", {})
        meta.update(file_meta)
        # The count describes the file_changes written beside it, so a
        # flush that drops nothing leaves no earlier count behind.
        if not _dropped:
            meta.pop("file_changes_omitted_files", None)
        break
    else:
        # broadcast=False: the synthetic message reaches the UI via the same
        # SSE/WS path the dashboard already drains for this slot. Default
        # broadcast=True would schedule a fan-out via asyncio.ensure_future(),
        # which (a) is redundant here and (b) raises RuntimeError when the
        # function is invoked from a sync context like a unit test.
        # Content-less on purpose. The turn's error row (every ``except``
        # branch appends it before this ``finally`` runs) is already in the
        # window, so this row lands AFTER it; an assistant row with text there
        # would be read by ``state.is_turn_interrupted`` and its frontend twin
        # ``selectTurnInterrupted`` as the turn's reply, hiding the trailing
        # error and with it the Resume control. Every last-conversational-row
        # scan looks through an assistant row whose ``content`` is empty, and
        # the transcript still draws it because the chips are content
        # (``invisibleText.ts::isHiddenInvisibleAssistantRow``). The chips say
        # what is known here -- files changed -- and the row beside them says
        # whether the turn stopped, errored, or queued a continuation.
        slot.append(
            "assistant",
            "",
            "msg msg-a",
            broadcast=False,
            meta=file_meta,
        )
    logger.info(
        "Attached %d file_changes to slot %s "
        "(%d path-only, %d dropped, over the %d-char turn budget)",
        len(fc_list),
        slot.key,
        _demoted,
        _dropped,
        _MAX_TURN_SNAPSHOT_CHARS,
    )
    # Honour the in-place-mutation contract (see _mark_permission_resolved in
    # state_parts/transcript_rows.py): "the periodic flush skips non-dirty slots, so an unflagged
    # in-place mutation can be lost on restart". The assistant-message branch
    # above mutates meta in place without appending, so nothing else marks the
    # slot dirty. That matters on the error/cancel call path, which — unlike the
    # success path — is NOT followed by an explicit save_slot_off_loop: without
    # this flag a periodic flush that snapshotted the message just before this
    # write clears _dirty, and the file_changes never reach disk.
    # (slot.append in the else branch already sets it; setting it once here
    # covers both branches and cannot be missed by a later edit.)
    slot._dirty = True
    slot._file_changes = []
    if isinstance(reply_mids, list):
        reply_mids.clear()
