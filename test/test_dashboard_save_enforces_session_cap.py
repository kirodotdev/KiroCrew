"""The dashboard whole-file save must honour the session byte cap.

``_SESSION_MAX_BYTES`` was enforced in exactly one place, ``ConversationLog.append``,
which calls ``_maybe_rotate``. ``_save_slot_to_history`` rewrites the whole file
through ``atomic_write`` and never reached that check, so a transcript written only
by the dashboard grew without bound while ``docs/system-specs/modules/history.md``
documents the cap unqualified.

The enforcement is deliberately scoped to the FROZEN PREFIX, and the scope is the
substance of these tests rather than a caveat on them. The save writes
``meta + frozen_prefix + serialize(window)``, so a line rotation drops from the
window region is one the slot still holds in ``slot.messages``: the next save
re-emits it, rotation drops it again, and the pair churns forever. The prefix is
also where unbounded growth actually lives — the live window is capped at
``_MAX_SLOT_MESSAGES``, so every byte a long session accumulates beyond that is
prefix, re-emitted verbatim by every later save.

So there are two contracts to hold, and a test for each:

* rotation DOES reclaim the prefix (``..._rotates_the_frozen_prefix...``), and
* rotation DECLINES rather than dropping a row the window still holds
  (``..._declines_to_drop_rows_the_live_window_holds``) — the load-bearing one,
  because the failure it guards is invisible in a single save and only shows up
  as repeated work across later ones.
"""

from chat_test_helpers import _make_state

from kiro_crew.history import _SESSION_MAX_BYTES


def _transcript(state, slot):
    from kiro_crew.dashboard.chat_persistence import slot_history_key

    return state.conversation_log._path(slot_history_key(slot))


def _disk_message_rows(path):
    """On-disk MESSAGE lines (metadata line excluded)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines[1:] if lines and '"_type"' in lines[0] else lines


def _count_rotations(monkeypatch):
    """Instrument rotation + archiving. Returns ``(rotations, archived)`` lists.

    ``_maybe_rotate`` takes a keyword-only ``max_drop``; the wrapper forwards
    ``**kw`` rather than naming it, so this keeps working if the signature grows.
    """
    import kiro_crew.history as history_mod
    from kiro_crew.history import ConversationLog

    rotations: list[int] = []
    archived: list[int] = []
    real_rotate = ConversationLog._maybe_rotate
    real_archive = history_mod._archive_lines

    def _counting_rotate(self, rot_path, key, **kw):
        result = real_rotate(self, rot_path, key, **kw)
        rotations.append(result or 0)
        return result

    def _counting_archive(stem, lines, reason=None, base=None):
        archived.append(len(lines))
        return real_archive(stem, lines, reason=reason, base=base)

    monkeypatch.setattr(ConversationLog, "_maybe_rotate", _counting_rotate)
    monkeypatch.setattr(history_mod, "_archive_lines", _counting_archive)
    return rotations, archived


def _fold_window_into_prefix(slot, fold):
    """Emulate the memory trim that creates a frozen prefix, as state.py does it.

    ``_disk_older_count`` only grows when memory trimming folds persisted window
    messages into it, at ``_MAX_SLOT_MESSAGES`` (10,000) messages — which the
    few-large-messages shape that triggers a byte-cap rotation never reaches. So a
    test that needs a prefix has to build one, byte for byte the way the trim does.
    """
    del slot.messages[:fold]
    slot._resumed_count = max(0, slot._resumed_count - fold)
    persisted_trim = min(fold, slot._disk_window_len)
    slot._disk_older_count += persisted_trim
    slot._disk_window_len = max(0, slot._disk_window_len - fold)


def test_dashboard_save_rotates_the_frozen_prefix_down_to_the_byte_cap(tmp_path, monkeypatch):
    """A save whose oversize lives in the frozen prefix must rotate it away.

    This is the enforcement the save path was missing: on unfixed code
    ``_maybe_rotate`` had a single caller (``append``), so this file crossed the
    cap and stayed there with ``rotated_at`` absent from the metadata line -- i.e.
    rotation was never consulted, rather than consulted and declined.

    The prefix is also the only part that can be reclaimed safely, so the
    reconciled counter is asserted alongside the size: rotation moved the
    frozen-prefix boundary, and a save that shrinks the file without lowering
    ``_disk_older_count`` has simply deferred the corruption to the next save.
    """
    from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("capsession")
    path = _transcript(state, slot)

    # Stay UNDER the cap so this first save cannot rotate; the prefix has to exist
    # before rotation, or there is nothing for rotation to reclaim.
    body = "z" * 100_000
    for i in range(15):
        slot.append("user" if i % 2 == 0 else "assistant", f"pre{i}:{body}", "msg")
    slot.drain()
    _save_slot_to_history(state, slot, force=True)
    assert path.stat().st_size <= _SESSION_MAX_BYTES, "setup must not rotate yet"

    _fold_window_into_prefix(slot, 10)
    older_before = slot._disk_older_count
    assert older_before > 0, "the frozen prefix is the precondition"

    # Now cross the cap. The oversize is in the prefix, so rotation can clear it.
    for i in range(15):
        slot.append("user" if i % 2 == 0 else "assistant", f"post{i}:{body}", "msg")
    slot.drain()
    _save_slot_to_history(state, slot, force=True)

    size = path.stat().st_size
    assert size <= _SESSION_MAX_BYTES, (
        f"dashboard save left {size:,} bytes on disk, over the {_SESSION_MAX_BYTES:,} "
        "cap, with the oversize sitting in the reclaimable frozen prefix"
    )
    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    assert "rotated_at" in first_line, (
        "the file fits the cap but carries no rotation stamp, so it was never "
        "rotated -- assert on the mechanism, not just the size"
    )
    assert slot._disk_older_count < older_before, (
        f"rotation shrank the file but left _disk_older_count at {older_before}; "
        "the next save would rebuild its payload from a prefix that is no longer "
        "on disk"
    )


def test_dashboard_save_declines_to_drop_rows_the_live_window_holds(tmp_path, monkeypatch):
    """30 large messages with NO frozen prefix: nothing is safely droppable.

    This is the natural shape for a byte-cap rotation, not an edge case: a
    frozen prefix only appears once memory trimming folds window rows into it at
    ``_MAX_SLOT_MESSAGES`` (10,000), and a session of a few large messages blows
    the 2 MB budget thousands of messages earlier. So on this path
    ``_disk_older_count`` is 0 and EVERY line rotation would drop is a line
    ``slot.messages`` still holds.

    Measured on unfixed code (uncapped ``_maybe_rotate``): the rotating save
    dropped 10 window rows from disk while all 30 stayed in memory, and each of
    the next 5 ordinary saves resurrected and re-dropped the same 10 --
    ``rotations=[10, 10, 10, 10, 10]``, 50 re-archived lines, an O(window) steady
    state turned into O(file) with unbounded archive growth.

    Asserted as data preservation rather than as a size bound, because the size
    bound is what unfixed code satisfies. The invariant is that the file agrees
    with the window the slot holds: all 30 rows are on disk, and no later save
    has anything left to rotate. Declining costs an oversized file until the
    memory trim supplies a prefix or an ``append`` writer rotates it uncapped;
    oversized is recoverable, and a row dropped from under its holder is not.
    """
    from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("churnsession")
    path = _transcript(state, slot)

    body = "z" * 100_000
    for i in range(30):
        slot.append("user" if i % 2 == 0 else "assistant", f"{i}:{body}", "msg")
    slot.drain()
    _save_slot_to_history(state, slot, force=True)

    assert slot._disk_older_count == 0, "this shape must have no frozen prefix"
    assert len(_disk_message_rows(path)) == 30, (
        f"rotation dropped {30 - len(_disk_message_rows(path))} row(s) that "
        "slot.messages still holds; the next save will re-emit them and rotation "
        "will drop them again"
    )
    assert "rotated_at" not in path.read_text(encoding="utf-8").splitlines()[0], (
        "the transcript carries a rotation stamp, so rotation ran on a file whose "
        "every line is live-window content -- it must decline instead"
    )

    # From here on, ordinary saves must be steady: nothing left to rotate, and
    # nothing re-archived. This is the assertion that fails on unfixed code.
    rotations, archived = _count_rotations(monkeypatch)
    for n in range(5):
        slot.append("user", f"tick{n}", "msg")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

    assert rotations == [0, 0, 0, 0, 0], (
        f"post-save rotations {rotations}: rotation is dropping live-window rows, "
        "so each save resurrects them from memory and rotation drops them again"
    )
    assert sum(archived) == 0, (
        f"post-save archiving {archived} ({sum(archived)} line(s)): the same rows "
        "are being archived on every save -- unbounded archive growth"
    )
    rows = _disk_message_rows(path)
    assert len(rows) == 35, (
        f"{len(rows)} row(s) on disk after 30 + 5 messages; the transcript no "
        "longer agrees with the window the slot holds"
    )


def test_saves_after_a_rotation_settle_instead_of_re_rotating(tmp_path, monkeypatch):
    """A rotation that DID reclaim prefix must not make every later save re-rotate.

    Guards the regression that enforcing the cap introduces. Rotation removes
    leading messages, which moves the frozen-prefix boundary the slot is holding.
    If that boundary is not reconciled, each later save rebuilds the payload from
    a prefix that is no longer on disk, RESURRECTING the dropped messages, and
    rotation drops them again.

    The failure is invisible in the file: measured over 5 ordinary post-rotation
    saves, reconciled and unreconciled runs leave byte-identical transcripts with
    no duplicated rows, because rotation re-trims what the stale prefix
    resurrected. What differs is that the unreconciled run rotates on all 5 saves
    and re-archives the same lines each time -- unbounded archive growth and
    O(file) work on a path whose steady state is meant to be O(window). So this
    asserts on rotation and archive activity, not on content; an earlier version
    asserted "no duplicate rows" and passed against the unreconciled code while
    appearing to guard it.
    """
    from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("dupsession")
    path = _transcript(state, slot)

    body = "z" * 100_000
    for i in range(15):
        slot.append("user" if i % 2 == 0 else "assistant", f"pre{i}:{body}", "msg")
    slot.drain()
    _save_slot_to_history(state, slot, force=True)
    assert path.stat().st_size <= _SESSION_MAX_BYTES, "setup must not rotate yet"

    _fold_window_into_prefix(slot, 10)
    assert slot._disk_older_count > 0, "the frozen prefix is the precondition"

    for i in range(15):
        slot.append("user" if i % 2 == 0 else "assistant", f"post{i}:{body}", "msg")
    slot.drain()
    _save_slot_to_history(state, slot, force=True)
    assert path.stat().st_size <= _SESSION_MAX_BYTES, "the rotating save must fit the cap"

    rotations, archived = _count_rotations(monkeypatch)
    for n in range(5):
        slot.append("user", f"tick{n}", "msg")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

    assert rotations == [0, 0, 0, 0, 0], (
        f"post-rotation saves re-rotated {rotations}; the frozen-prefix boundary "
        "was not reconciled, so each save resurrects the dropped messages and "
        "rotation drops them again"
    )
    assert sum(archived) == 0, (
        f"post-rotation saves re-archived {sum(archived)} line(s) ({archived}); "
        "the same dropped messages are being archived repeatedly"
    )
    assert (
        path.stat().st_size <= _SESSION_MAX_BYTES
    ), "the follow-up saves must also respect the cap"


def test_maybe_rotate_reports_how_many_lines_it_dropped(tmp_path):
    """``_maybe_rotate`` returns the dropped count, and 0 when it does nothing.

    The count is the contract the dashboard save relies on to reconcile its
    prefix boundary without re-reading the file. A ``None`` return -- what this
    had while ``append`` was the only caller -- cannot express "nothing moved".
    """
    from kiro_crew.history import ConversationLog

    log = ConversationLog(tmp_path / "logs")
    key = "dashboard:rotate-return"

    log.append(key, "user", "small")
    assert (
        log._maybe_rotate(log._path(key), key) == 0
    ), "an under-cap file must report zero dropped lines, not None"

    body = "z" * 100_000
    for i in range(30):
        log.append(key, "user", f"{i}:{body}")
    # ``append`` already rotates, so the file is at or under the cap here; the
    # return value is exercised on the path above and by the dashboard tests.
    assert log._path(key).stat().st_size <= _SESSION_MAX_BYTES


def test_maybe_rotate_never_drops_more_than_max_drop(tmp_path):
    """``max_drop`` is a hard ceiling, and ``0`` makes rotation a no-op.

    The dashboard save's safety rests entirely on this bound, so it is asserted
    directly rather than only through the save path. The ``0`` case matters most:
    the shrink loop starts from ``min(_SESSION_KEEP_LINES, len(msg_lines))``, so
    without raising the loop's floor a cap of 0 would still rewrite the file down
    to the line cap -- dropping every line it was forbidden to touch.
    """
    from kiro_crew.history import ConversationLog

    log = ConversationLog(tmp_path / "logs")
    body = "z" * 100_000

    def _oversized(key):
        """Write an oversized transcript directly.

        ``append`` rotates as it goes, so it cannot leave an oversized file
        behind to rotate; the fixture has to be built under the cap and then
        overwritten.
        """
        log.append(key, "user", "seed")
        path = log._path(key)
        meta = path.read_text(encoding="utf-8").splitlines(keepends=True)[0]
        rows = [f'{{"role": "user", "content": "{i}:{body}"}}\n' for i in range(30)]
        path.write_text(meta + "".join(rows), encoding="utf-8")
        assert path.stat().st_size > _SESSION_MAX_BYTES, "the fixture must be oversized"
        return path

    # ``max_drop=0``: oversized, but nothing may be dropped.
    key0 = "dashboard:maxdrop-zero"
    path0 = _oversized(key0)
    assert log._maybe_rotate(path0, key0, max_drop=0) == 0, "max_drop=0 must drop nothing"
    assert len(_disk_message_rows(path0)) == 30, "max_drop=0 must not rewrite the file"

    # A positive cap is honoured exactly: uncapped rotation on this same fixture
    # would drop far more than 3 lines to reach the byte budget.
    key3 = "dashboard:maxdrop-three"
    path3 = _oversized(key3)
    dropped = log._maybe_rotate(path3, key3, max_drop=3)
    assert dropped == 3, f"max_drop=3 allowed {dropped} line(s) to be dropped"
    assert len(_disk_message_rows(path3)) == 27, "exactly the capped number must be gone"
    assert (
        log._maybe_rotate(path3, key3, max_drop=0) == 0
    ), "a still-oversized file must stay put once the cap is exhausted"

    # Uncapped rotation on the identical fixture is the control: it drops far
    # more, which is what makes the cap above a real constraint rather than a
    # restatement of what rotation would have done anyway.
    keyu = "dashboard:maxdrop-none"
    pathu = _oversized(keyu)
    assert log._maybe_rotate(pathu, keyu) > 3, (
        "uncapped rotation dropped <= 3 lines on this fixture, so the capped "
        "assertions above prove nothing"
    )


def test_maybe_rotate_declines_to_rewrite_when_archiving_fails(tmp_path, monkeypatch):
    """A failed archive must abort the rewrite instead of deleting what it lost.

    Rotation's own rewrite is what removes the dropped lines, and until the
    archive write lands this transcript is their only copy. So archiving
    best-effort and rewriting anyway converts a recoverable "archive directory is
    unwritable" into permanent transcript loss. The dashboard save makes it
    reachable for the FROZEN PREFIX specifically -- the region ``slot.messages``
    does not hold, so no later save can re-emit what rotation deleted.

    Declining leaves the file oversized, which is the trade ``max_drop`` already
    makes on this path: oversized is recoverable, a dropped row is not.
    """
    import kiro_crew.history as history_mod
    from kiro_crew.history import ConversationLog

    log = ConversationLog(tmp_path / "logs")
    real_archive = history_mod._archive_lines
    body = "z" * 100_000

    def _oversized(key):
        """Write an oversized transcript directly.

        ``append`` rotates as it goes, so it cannot leave an oversized file
        behind; the fixture has to be built under the cap and then overwritten.
        """
        log.append(key, "user", "seed")
        path = log._path(key)
        meta = path.read_text(encoding="utf-8").splitlines(keepends=True)[0]
        rows = [f'{{"role": "user", "content": "{i}:{body}"}}\n' for i in range(30)]
        path.write_text(meta + "".join(rows), encoding="utf-8")
        assert path.stat().st_size > _SESSION_MAX_BYTES, "the fixture must be oversized"
        return path

    key = "dashboard:archive-raises"
    path = _oversized(key)
    before = path.read_text(encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(history_mod, "_archive_lines", _boom)

    assert log._maybe_rotate(path, key) == 0, (
        "rotation reported dropped lines after archiving raised, so it committed "
        "the rewrite and the only copy of those lines is gone"
    )
    assert path.read_text(encoding="utf-8") == before, (
        "the transcript was rewritten even though archiving raised; the dropped "
        "lines existed nowhere else"
    )
    assert "rotated_at" not in path.read_text(encoding="utf-8").splitlines()[0], (
        "the file carries a rotation stamp, so rotation committed despite the " "failed archive"
    )

    # Control on an identical fixture with archiving healthy: this one MUST
    # rotate. Without it the assertions above would also pass on a file rotation
    # simply had nothing to do with.
    monkeypatch.setattr(history_mod, "_archive_lines", real_archive)
    key_ok = "dashboard:archive-succeeds"
    path_ok = _oversized(key_ok)
    assert log._maybe_rotate(path_ok, key_ok) > 0, (
        "rotation declined on the healthy-archive fixture too, so the decline "
        "above is not attributable to the archive failure"
    )
