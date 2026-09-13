"""The flush's file IO must not run on the gateway's event loop.

`_flush_file_changes` reads up to `_MAX_RECONSTRUCT_BYTES` per changed path and
runs a difflib pass over the result. One event loop serves every session, so doing
that inline in turn finalization stalls every other session's streaming for its
duration and counts against the loop watchdog. Bounded is not the same as
non-blocking: measured, 2 MiB of repeated lines is ~25 ms and 43k fully-reordered
lines ~85 ms — far from the watchdog's own limit, but this is the "large
synchronous file IO" pattern the rule names, and the pattern is what it prohibits.

These pin the SHAPE rather than a duration. A timing assertion here would be a
threshold that a shared runner falsifies at random.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from kiro_crew import hooks
from kiro_crew.dashboard import chat_runner as cr

_SRC = Path(cr.__file__).read_text(encoding="utf-8")


class _Slot:
    """Minimal stand-in for the fields the flush touches."""

    def __init__(self, changes: list[dict]) -> None:
        self.key = "s1"
        self._file_changes = changes
        self.messages: list[dict] = [{"role": "assistant", "content": "done"}]
        self._dirty = False

    def append(self, *a, **kw) -> None:  # pragma: no cover - not reached here
        self.messages.append({"role": "assistant", "content": "x", "meta": kw.get("meta")})


def test_the_blocking_half_is_a_separate_function_the_loop_can_offload():
    """The IO must be isolated from the slot mutation, or it cannot be offloaded.

    `_read_file_change_snapshots` takes the deduped dict and writes only into it,
    so it is safe in a worker thread; the attach mutates `slot.messages` and must
    stay on the loop. Both callers share these helpers, so the on-loop and
    off-loop paths cannot drift into two implementations of the same rule.
    """
    assert callable(cr._read_file_change_snapshots)
    assert callable(cr._dedupe_file_changes)
    assert callable(cr._attach_file_changes)
    assert inspect.iscoroutinefunction(cr._flush_file_changes_off_loop)
    # The sync entry point survives for callers with no loop, and delegates rather
    # than carrying its own copy of the logic. Matched on the call, not on its
    # argument list, so adding a parameter is not a test failure.
    body = inspect.getsource(cr._flush_file_changes)
    assert "_read_file_change_snapshots(deduped)" in body
    assert "_attach_file_changes(slot, deduped" in body


def test_the_off_loop_flush_hands_the_io_to_a_thread():
    src = inspect.getsource(cr._flush_file_changes_off_loop)
    assert (
        "asyncio.to_thread(_read_file_change_snapshots" in src
    ), "the read/diff must be handed to a worker thread, not awaited inline"
    # The attach is NOT offloaded: it mutates slot state.
    assert "to_thread(_attach_file_changes" not in src


def test_every_production_call_site_uses_the_off_loop_form():
    """A single remaining sync call inside the async turn keeps the rule violated."""
    inline = [
        ln
        for ln in _SRC.splitlines()
        if "_flush_file_changes(slot)" in ln and "_off_loop" not in ln and "def " not in ln
    ]
    assert inline == [], f"synchronous flush still called from the turn: {inline}"
    # Two production sites in the turn, reached two different ways for the reason the
    # cancel-path test below spells out: the success path awaits the flush, the
    # cancel/error path dispatches a wrapper that flushes AND persists, adding no
    # suspension point of its own.
    turn = inspect.getsource(cr._run_chat)
    assert turn.count("await _flush_file_changes_off_loop(slot)") == 1
    assert turn.count("asyncio.create_task(_flush_and_save_off_loop(state, slot))") == 1
    # The wrapper's own await is the third occurrence in the module and is correct;
    # scoping the counts to the turn is what keeps them meaningful.
    assert "await _flush_file_changes_off_loop(slot)" in inspect.getsource(
        cr._flush_and_save_off_loop
    )


def test_the_cancel_path_never_awaits_the_flush():
    """On the cancel/error path the flush is DETACHED, not awaited.

    The `finally` in `_run_chat` is a chain of must-run cleanup, and each step's
    own comment records what a skip costs: the session lease (a leak makes the
    session read as permanently busy until a gateway restart), this turn's
    identity, the steer requeue, the queue drain. `CancelledError` derives from
    `BaseException`, so ANY await in that chain is a point where a cancellation
    delivered mid-suspension slips past `except Exception` and skips every
    remaining step.

    Ordering cannot fix that -- whatever follows the await is what gets lost, so
    there is no safe position, only a different victim. An earlier revision of
    this change placed the await below two of the re-arms and still sat above the
    lease release. What makes it safe is that the frame adds no suspension point
    at all: the work is dispatched as a task and the turn keeps unwinding.
    """
    src = inspect.getsource(cr._run_chat)
    # Split at the dispatch itself rather than trying to find "the right finally":
    # `_run_chat` has several at more than one indentation depth, so any structural
    # guess here is fragile. What matters is only what comes AFTER the cancel-path
    # flush, and the dispatch line names it exactly.
    dispatch = "asyncio.create_task(_flush_and_save_off_loop(state, slot))"
    assert dispatch in src, "the cancel/error path must still dispatch the flush off-loop"
    _before, _sep, after = src.partition(dispatch)
    assert "await _flush_file_changes_off_loop" not in after, (
        "the cancel/error path must not await the flush: that await is a "
        "cancellation point ahead of the session-lease release"
    )
    assert (
        "await _flush_and_save_off_loop" not in after
    ), "and it must not await the persisting wrapper either, for the same reason"
    # And the cleanup chain it must not suspend in front of is genuinely there,
    # so this test cannot pass vacuously if the release ever moves above it.
    assert "state.sessions.release(session_key)" in after, (
        "the lease release must still follow the dispatch -- that adjacency is "
        "the whole reason the dispatch is not an await"
    )
    # Tracked, so the task is not garbage-collected mid-flight.
    assert "_background_tasks.add(_fc_flush_task)" in src
    assert "_fc_flush_task.add_done_callback(state._background_tasks.discard)" in src
    # The success path DOES await it: there it is ordinary sequential code with
    # no cleanup chain behind it, and the save that follows needs the meta.
    assert "await _flush_file_changes_off_loop(slot)" in src


def test_an_evicted_before_body_produces_no_patch_at_all():
    """A missing `before_full` cannot be read as "the before is complete".

    This is the third time this one predicate has been the defect, which is why the
    fix is a flag rather than another special case. `before_full` is absent for three
    unrelated reasons:

      1. the body was never truncated       -> `content` IS the whole body
      2. it was truncated but too large to retain
      3. the accumulator evicted it (per-path dedup, or the count cap)

    Reading absence as (1) means cases 2 and 3 diff a PREFIX against a full after,
    and everything past the cut is reported as an addition that never happened.
    Reading it as "we lack the before" was the previous bug, in the opposite
    direction. `before_capped` answers only "is `before` a prefix", set where the
    truncation happens, so it survives every eviction path.
    """

    class _S:
        def __init__(self) -> None:
            self._file_changes: list[dict] = []
            self.messages: list[dict] = [{"role": "assistant", "content": "x"}]
            self._dirty = False
            self.key = "s"

    # Two edits to DIFFERENT oversized paths, with the retention count forced to 1 so
    # the second one's full body is evicted while its prefix survives.
    big = "keep\n" * (cr._MAX_SNAPSHOT // 5 + 100)
    first = cr._before_entry("/w/a.ts", big)
    second = cr._before_entry("/w/b.ts", big)
    assert first.get("before_capped") and second.get("before_capped")

    slot = _S()
    cr._retain_file_snapshot(slot, first)  # type: ignore[arg-type]
    saved = cr._MAX_RETAINED_FULL_BODIES
    try:
        cr._MAX_RETAINED_FULL_BODIES = 1
        cr._retain_file_snapshot(slot, second)  # type: ignore[arg-type]
    finally:
        cr._MAX_RETAINED_FULL_BODIES = saved

    evicted = slot._file_changes[1]
    assert "before_full" not in evicted, "the second body must have been evicted"
    assert evicted.get("before_capped"), "and its prefix marker must have survived"

    taken = cr._dedupe_file_changes(slot)  # type: ignore[arg-type]
    assert taken is not None
    deduped, _row = taken
    assert deduped["/w/b.ts"].get("before_capped"), "the marker must reach the reader"


def test_an_evicted_body_yields_no_fabricated_patch_end_to_end(tmp_path):
    """The outcome that matters: a prefix must never be diffed against a full after.

    Runs the real reader against a real file, so the assertion is about what a
    reviewer would SEE rather than about an intermediate flag.
    """
    # Sized deliberately BETWEEN the two caps: past `_MAX_SNAPSHOT` so it is a prefix,
    # under `_MAX_RECONSTRUCT_BYTES` so the size bound does not drop it first -- the
    # eviction is what has to remove the full copy here, not the size limit.
    line = "unchanged\n"
    head = line * (cr._MAX_SNAPSHOT // len(line) + 500)
    assert cr._MAX_SNAPSHOT < len(head) < cr._MAX_RECONSTRUCT_BYTES
    on_disk = head + "appended by the edit\n"
    p = tmp_path / "grew.ts"
    p.write_text(on_disk, encoding="utf-8")

    # The before-body is a PREFIX with its full copy evicted -- the state the count
    # cap and the per-path dedup both produce.
    entry = cr._before_entry(str(p), head)
    assert entry.pop("before_full", None) is not None, "precondition: it was retained"
    assert entry.get("before_capped"), "and it is marked as a prefix"

    class _S:
        def __init__(self, changes: list[dict]) -> None:
            self._file_changes = changes
            self.messages: list[dict] = [{"role": "assistant", "content": "x"}]
            self._dirty = False
            self.key = "s"

    slot = _S([entry])
    cr._flush_file_changes(slot)  # type: ignore[arg-type]
    out = slot.messages[-1]["meta"]["file_changes"][0]

    assert not out.get("patch"), (
        "a prefix before-body must not be diffed against the full after -- that "
        f"reports the untouched tail as added; got patch of {len(out.get('patch') or '')} bytes"
    )
    # The row itself still surfaces, with its capped pair.
    assert out["before"] and out["after"]


def test_the_capped_marker_is_never_persisted():
    """Both transient keys must be popped before anything reaches message meta."""

    class _S:
        def __init__(self, changes: list[dict]) -> None:
            self._file_changes = changes
            self.messages: list[dict] = [{"role": "assistant", "content": "x"}]
            self._dirty = False
            self.key = "s"

    big = "keep\n" * (cr._MAX_SNAPSHOT // 5 + 100)
    slot = _S([cr._before_entry("/w/gone.ts", big)])
    cr._flush_file_changes(slot)  # type: ignore[arg-type]
    entry = slot.messages[-1]["meta"]["file_changes"][0]
    assert "before_capped" not in entry
    assert "before_full" not in entry


def test_one_retained_body_is_bounded_by_size_not_only_by_count():
    """The count cap says nothing about the size of any single retained body.

    `_MAX_RETAINED_FULL_BODIES` bounds how MANY full pre-edit bodies sit on the
    accumulator. One caller hands `_before_entry` the ACP `oldText` verbatim, which
    arrives from the agent with no ceiling at all -- so a single edit reporting a
    huge old body would sit in memory for the whole turn however low the count is.

    The ceiling is the one the diff itself obeys: past `_MAX_RECONSTRUCT_BYTES` the
    reader declines to produce a full after-body, so a before-body that large could
    never be paired with one. Dropping it costs that path's patch and nothing else --
    the capped `content` is always kept.
    """
    over = "y" * (cr._MAX_RECONSTRUCT_BYTES + 5_000)
    entry = cr._before_entry("/w/huge.ts", over)
    assert "before_full" not in entry, "an oversized before-body must not be retained"
    # And it must still be MARKED as a prefix. Dropping the body without the marker
    # is the fabrication case: a later reader sees no `before_full`, reads that as
    # "the before is complete", and diffs the prefix against the full after.
    assert entry.get("before_capped"), (
        "the size-bound path must mark the before as a prefix, or the patch gate "
        "cannot tell it from an untruncated body"
    )
    assert entry["content"], "the capped snapshot is still kept"
    # `_truncate_snapshot` appends a marker, so the cap bounds the CONTENT it kept
    # rather than the final string; what matters is that the whole body is gone.
    assert len(entry["content"]) < len(over)
    assert "truncated at" in entry["content"]

    # Just under the ceiling, and past the snapshot cap, is exactly the case the
    # retention exists for -- so the bound must not swallow it.
    under = "z" * (cr._MAX_SNAPSHOT + 5_000)
    assert len(under) < cr._MAX_RECONSTRUCT_BYTES
    kept = cr._before_entry("/w/mid.ts", under)
    assert kept.get("before_full") == under, "a body the diff can still use must be retained"


def test_the_dispatched_flush_persists_its_own_result():
    """A dispatched flush must not leave its result for someone else to save.

    Relying on `slot._dirty` plus the periodic flush loses the rows at shutdown: the
    restart path saves every slot FIRST and stops active turns SECOND, so a
    teardown-time attach lands after the final save and the process is replaced
    before another one happens.

    KNOWN LIMIT, recorded rather than implied away: this covers "the attach landed
    after the save", not "the task never ran at all". The second needs the background
    tasks drained before the final shutdown save, which belongs to the restart path.
    """
    body = inspect.getsource(cr._flush_and_save_off_loop)
    assert "await _flush_file_changes_off_loop(slot)" in body
    assert "await save_slot_off_loop(state, slot)" in body
    # A failed save degrades to the periodic flush rather than losing the attach
    # that already happened.
    assert "except Exception" in body


def test_the_dispatched_flush_still_lands(tmp_path):
    """Detaching must not mean losing it -- the attach has to actually happen."""
    p = tmp_path / "c.ts"
    p.write_text("after\n", encoding="utf-8")
    slot = _Slot([{"path": str(p), "content": "before\n"}])

    async def _drive() -> None:
        task = asyncio.create_task(cr._flush_file_changes_off_loop(slot))  # type: ignore[arg-type]
        await task

    asyncio.run(_drive())
    entry = slot.messages[-1]["meta"]["file_changes"][0]
    assert entry["after"] == "after\n"
    assert slot._dirty is True, "the periodic flush is what persists this path"


def test_a_detached_flush_does_not_steal_the_successor_turns_row(tmp_path):
    """The attach must land on the row the changes were TAKEN against.

    Dispatching the flush means an await sits between reading the accumulator and
    attaching the result. A queued successor turn can start in that window and
    append its own assistant row, so "the most recent assistant message" is no
    longer this turn's -- a recency lookup hangs the cancelled turn's file changes
    on the successor's reply. A positional or recency-based lookup is not an
    identity.
    """
    p = tmp_path / "a.ts"
    p.write_text("after\n", encoding="utf-8")
    slot = _Slot([{"path": str(p), "content": "before\n"}])
    mine = slot.messages[-1]
    mine["content"] = "the cancelled turn's reply"

    async def _drive() -> None:
        taken = cr._dedupe_file_changes(slot)  # type: ignore[arg-type]
        assert taken is not None
        deduped, target_row = taken
        assert target_row is mine
        # The successor turn lands while the read is in flight.
        slot.messages.append({"role": "assistant", "content": "the successor's reply"})
        await asyncio.to_thread(cr._read_file_change_snapshots, deduped)
        cr._attach_file_changes(slot, deduped, target_row)  # type: ignore[arg-type]

    asyncio.run(_drive())

    assert "meta" in mine, "the changes belong to the row that was captured"
    assert mine["meta"]["file_changes"][0]["after"] == "after\n"
    successor = slot.messages[-1]
    assert "meta" not in successor or not (successor.get("meta") or {}).get(
        "file_changes"
    ), "the successor's reply must not be given this turn's file changes"


def test_taking_the_changes_empties_the_accumulator_immediately(tmp_path):
    """Clearing it AFTER the await would discard the next turn's writes.

    The accumulator is emptied in the same on-loop step that reads it, so a turn
    owns exactly what it took. When the clear lived at the end of the attach, a
    detached flush wiped whatever the successor had accumulated while the read was
    in flight.
    """
    p = tmp_path / "b.ts"
    p.write_text("after\n", encoding="utf-8")
    slot = _Slot([{"path": str(p), "content": "before\n"}])

    taken = cr._dedupe_file_changes(slot)  # type: ignore[arg-type]
    assert taken is not None
    assert slot._file_changes == [], "the accumulator must be emptied with the read"

    # The successor turn accumulates while the read would be in flight.
    slot._file_changes.append({"path": str(p), "content": "successor before\n"})
    deduped, target_row = taken
    cr._read_file_change_snapshots(deduped)
    cr._attach_file_changes(slot, deduped, target_row)  # type: ignore[arg-type]

    assert slot._file_changes == [
        {"path": str(p), "content": "successor before\n"}
    ], "the attach must not clear writes it never took"


def test_a_file_that_grew_past_the_cap_still_carries_its_tail(tmp_path):
    """`before_full` absent means "the before is complete", not "we lack it".

    It is attached only when the before-body was itself truncated, so a file that
    was SHORT and grew past the cap has no `before_full`. Reading that as absence
    failed the patch gate, and the row shipped a complete before against an after
    cut at `_MAX_SNAPSHOT` -- the tail of the addition simply gone, with no patch
    carrying it. Same defect this change exists to fix, in the one direction it did
    not cover.
    """
    small_before = "export const A = 1\n"
    big_after = small_before + ("export const FILLER = 2\n" * (cr._MAX_SNAPSHOT // 24 + 200))
    assert len(small_before) <= cr._MAX_SNAPSHOT < len(big_after)

    p = tmp_path / "grew.ts"
    p.write_text(big_after, encoding="utf-8")
    # No `before_full`: the capped before IS the whole body, which is exactly what
    # `_before_entry` does for an untruncated file.
    slot = _Slot([{"path": str(p), "content": small_before}])
    cr._flush_file_changes(slot)  # type: ignore[arg-type]

    entry = slot.messages[-1]["meta"]["file_changes"][0]
    assert entry.get("patch"), "a small file that grew past the cap needs a patch"
    patch = entry["patch"]
    assert "FILLER" in patch, "the patch must carry the added tail"
    # And it must be a patch against the REAL before, not against nothing. Treating
    # the absent `before_full` as an empty body also produces a patch containing
    # the tail -- but one that reports the whole file as new, including the line
    # that was already there. So the discriminator is the pre-existing line: it
    # must appear as context, never as an addition.
    added = [ln for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    assert not any(
        "export const A = 1" in ln for ln in added
    ), "the line that already existed must not be reported as added"
    assert any(
        ln.startswith(" ") and "export const A = 1" in ln for ln in patch.splitlines()
    ), "it should appear as unchanged context"


def test_the_opened_inode_is_checked_against_the_sensitive_path_list(tmp_path, monkeypatch):
    """The fd-pinned sensitive check must run, and it only runs with a root.

    `O_NOFOLLOW` guards only the FINAL path component, so an agent that swaps a
    validated ANCESTOR directory for a link into a credential directory between the
    validate and the open reaches a different inode than the one approved. The
    reader's defence is to resolve the OPENED descriptor's real path and refuse it
    when `is_sensitive_path` matches -- but that whole block is gated on
    `within_root`, so passing a root is what turns it on.

    Asserted on the inode that is actually opened, via the swap window, with
    `is_sensitive_path` standing in for the credential list so the test never has to
    write near a real one.

    KNOWN LIMIT, recorded rather than implied away: this closes the credential case,
    not arbitrary relocation. The reader re-resolves `within_root` itself at read
    time, so a root passed as a path string follows the same swapped link and the
    containment half of the check cannot see the move. Refusing a swap to a
    NON-sensitive location needs the root captured inside the reader (or an fd
    handed to it), which is `hooks.py`'s to fix, not this module's.
    """
    live = tmp_path / "live"
    live.mkdir()
    target = live / "a.ts"
    target.write_text("legitimate\n", encoding="utf-8")

    # The ordinary case must still read, or the guard is just a break.
    assert cr._safe_read_snapshot_raw(str(target)) == "legitimate\n"

    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "a.ts").write_text("SECRET\n", encoding="utf-8")

    real_reader = cr.safe_read_file_bytes_nolink
    roots: list[str | None] = []

    def _swap_then_read(path, within_root=None, **kw):
        roots.append(within_root)
        # Land the swap in the window: the caller has resolved its root, the
        # descriptor is not open yet.
        if live.is_dir() and not live.is_symlink():
            for child in live.iterdir():
                child.unlink()
            live.rmdir()
            live.symlink_to(creds, target_is_directory=True)
        return real_reader(path, within_root, **kw)

    monkeypatch.setattr(cr, "safe_read_file_bytes_nolink", _swap_then_read)
    monkeypatch.setattr(hooks, "is_sensitive_path", lambda p: str(creds) in str(p))

    got = cr._safe_read_snapshot_raw(str(target))

    assert (
        roots and roots[0] is not None
    ), "a root must be passed, or the reader's fd-pinned checks never run at all"
    assert got is None, f"the swapped credential inode must be refused; got {got!r}"
    assert got != "SECRET\n"


def test_retained_full_bodies_are_deduped_by_path_and_bounded():
    """N edits to one large file must not retain N full pre-edit bodies.

    `before_full` holds a whole pre-edit body so the flush can diff past the capped
    prefix, and it lives on the accumulator until the turn ends. The flush keeps
    only the FIRST before per path, so every copy after the first is waste -- and an
    agent looping edits over a 2 MiB file would grow the gateway's memory without
    bound.
    """

    class _S:
        def __init__(self) -> None:
            self._file_changes: list[dict] = []

    slot = _S()
    big = "x" * 1000
    for _ in range(5):
        cr._retain_file_snapshot(slot, {"path": "/w/a.ts", "content": "c", "before_full": big})  # type: ignore[arg-type]

    assert len(slot._file_changes) == 5, "every event is still recorded"
    with_full = [c for c in slot._file_changes if "before_full" in c]
    assert len(with_full) == 1, "only the first full body for a path is retained"
    # The capped content is always kept, so a dropped full body costs the patch for
    # that path, never the row.
    assert all(c.get("content") == "c" for c in slot._file_changes)

    # Distinct paths are bounded by the cap rather than deduped away.
    slot2 = _S()
    for i in range(cr._MAX_RETAINED_FULL_BODIES + 6):
        cr._retain_file_snapshot(slot2, {"path": f"/w/f{i}.ts", "content": "c", "before_full": big})  # type: ignore[arg-type]
    retained = [c for c in slot2._file_changes if "before_full" in c]
    assert len(retained) == cr._MAX_RETAINED_FULL_BODIES


def test_a_rewound_turn_is_discarded_not_resurrected(tmp_path):
    """A captured row that has DISAPPEARED means the turn was rewound.

    Three outcomes have to stay distinguishable, and the last two look identical if
    you only ask "is there a row now":

      - captured row still present   -> write to it
      - no row existed when taken    -> synthesize, so the chips still surface
      - a row existed and is gone    -> DISCARD

    A trim, reset or regenerate removes the turn this belonged to. Synthesizing
    there appends a discarded turn's writes onto a transcript rewritten past them,
    so the user sees changes attributed to history that no longer exists.
    """
    p = tmp_path / "a.ts"
    p.write_text("after\n", encoding="utf-8")
    slot = _Slot([{"path": str(p), "content": "before\n"}])
    mine = slot.messages[-1]

    taken = cr._dedupe_file_changes(slot)  # type: ignore[arg-type]
    assert taken is not None
    deduped, target_row = taken
    assert target_row is mine
    cr._read_file_change_snapshots(deduped)

    # The rewind: the captured row is gone, and a different transcript is in place.
    slot.messages.clear()
    slot.messages.append({"role": "assistant", "content": "a rewritten reply"})

    cr._attach_file_changes(slot, deduped, target_row)  # type: ignore[arg-type]

    assert len(slot.messages) == 1, "no synthetic row may be appended after a rewind"
    survivor = slot.messages[0]
    assert not (survivor.get("meta") or {}).get(
        "file_changes"
    ), "the discarded turn's changes must not land on the rewritten transcript"


def test_no_row_at_take_time_still_synthesizes(tmp_path):
    """The other absence: a turn that died before producing any text.

    `target_row is None` at take time is what tells this apart from a rewind, and it
    must still surface the chips -- otherwise silencing the rewind case would
    silence this one too.
    """
    p = tmp_path / "b.ts"
    p.write_text("after\n", encoding="utf-8")
    slot = _Slot([{"path": str(p), "content": "before\n"}])
    slot.messages.clear()  # no assistant row at all

    cr._flush_file_changes(slot)  # type: ignore[arg-type]

    assert slot.messages, "a turn with writes but no reply must still show its chips"
    assert slot.messages[-1]["meta"]["file_changes"][0]["after"] == "after\n"


def test_resume_prepares_its_window_off_loop():
    """`_prepare_messages` shrinks oversized rows, and that RECOMPUTES a diff.

    On top of its regex-heavy redaction pass it calls `_unified_patch` once per
    oversized file-change row, so a resume replaying a window full of them would run
    those difflib passes on the gateway's one event loop.

    Pinned on the call shape rather than on a duration: a timing assertion here is a
    threshold a shared runner falsifies at random.
    """
    from kiro_crew.dashboard import chat_handlers as ch
    from kiro_crew.dashboard import chat_utils as cu

    # The premise: the wire shrink really does re-derive a patch.
    assert "_unified_patch(" in inspect.getsource(cu._shrink_file_changes_for_wire)

    src = Path(ch.__file__).read_text(encoding="utf-8")
    inline = [
        ln
        for ln in src.splitlines()
        if "_prepare_messages(" in ln
        and "def " not in ln
        and "import" not in ln
        and "to_thread" not in ln
        and not ln.strip().startswith("#")
    ]
    # One inline call remains and is correct: it is inside `_render`, itself already
    # invoked through a worker thread, so offloading again would nest pointlessly.
    assert len(inline) == 1, f"unexpected inline _prepare_messages calls: {inline}"
    assert (
        src.count("asyncio.to_thread(\n            _prepare_messages,")
        + src.count("asyncio.to_thread(\n        _prepare_messages,")
        == 2
    ), "both resume paths must prepare their window off-loop"


def test_the_sync_flush_still_works_for_a_caller_with_no_loop(tmp_path):
    """Delegation must not have broken the synchronous path itself."""
    p = tmp_path / "a.ts"
    p.write_text("after\n", encoding="utf-8")
    slot = _Slot([{"path": str(p), "content": "before\n"}])
    cr._flush_file_changes(slot)  # type: ignore[arg-type]
    entry = slot.messages[-1]["meta"]["file_changes"][0]
    assert entry["before"] == "before\n"
    assert entry["after"] == "after\n"
    assert slot._dirty is True


def test_the_off_loop_flush_produces_the_same_result_as_the_sync_one(tmp_path):
    """Two entry points, one outcome -- otherwise the split is a fork."""
    p = tmp_path / "b.ts"
    p.write_text("after\n", encoding="utf-8")

    sync_slot = _Slot([{"path": str(p), "content": "before\n"}])
    cr._flush_file_changes(sync_slot)  # type: ignore[arg-type]

    off_slot = _Slot([{"path": str(p), "content": "before\n"}])
    asyncio.run(cr._flush_file_changes_off_loop(off_slot))  # type: ignore[arg-type]

    assert (
        sync_slot.messages[-1]["meta"]["file_changes"]
        == off_slot.messages[-1]["meta"]["file_changes"]
    )
