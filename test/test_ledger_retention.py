"""Ledger retention -- one test per rule the sweep and the removal promise.

Two layers, in the order they depend on each other: ``remove_unit`` (ownership,
ordering, identity last, counted failures) and ``sweep_expired`` (which units it
selects, and every unit it must refuse to touch).

The refusals carry the weight here. A sweep that removes too little costs disk;
a sweep that removes an OPEN session's log, a crew ledger, or a unit some writer
still owns destroys a record nothing can rebuild -- so each of those has its own
test rather than being covered incidentally by the happy path.
"""

from __future__ import annotations

import io
import json
import multiprocessing
import os
import shutil
import time

import pytest

from kiro_crew import ledger as lg
from kiro_crew.ledger import Ledger, store
from kiro_crew.ledger.lease import LEASE_FILE
from kiro_crew.session_ledger import _store_name

SESSION = "s-retain"
CREW = "qa"
DAY_MS = 86_400_000
#: The teardown marker, spelled here so a planted line matches what the sweep reads.
_CLOSED = "session/closed"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _closed_session(
    unit_id: str = SESSION, *, closed_days_ago: float = 90.0, reason: str = "destroyed"
) -> Ledger:
    """A session ledger holding one ``session/closed`` written *closed_days_ago*.

    The default *reason* is a real terminal one. It is not decoration: a unit is
    collectable only when its close reason proves the gateway cleared that ACP id's
    mapping, so a fixture with an invented reason would be a unit the sweep must
    refuse -- which the non-terminal tests below use it for deliberately.
    """
    ledger = Ledger.create(lg.KIND_SESSION, unit_id, owner=CREW, agent="kirocrew")
    closed_at = store.now_ms() - int(closed_days_ago * DAY_MS)
    ledger.append("session/opened", {"resumed": False}, src="gateway")
    _append_at(ledger, "session/closed", {"reason": reason}, closed_at)
    return ledger


def _open_session(unit_id: str = SESSION, *, age_days: float = 90.0) -> Ledger:
    """A session ledger with a header and entries but NO ``session/closed``.

    Its ``session/opened`` is aged too, not just the file: "an open session is
    never touched, HOWEVER OLD it is" is a claim about every clock in the unit, so
    a fixture whose lifecycle entry is fresh would let an entry-time rule keep it
    for the wrong reason.
    """
    ledger = Ledger.create(lg.KIND_SESSION, unit_id, owner=CREW, agent="kirocrew")
    _append_at(
        ledger, "session/opened", {"resumed": False}, store.now_ms() - int(age_days * DAY_MS)
    )
    _age_file(ledger.path, age_days)
    return ledger


def _append_at(ledger: Ledger, entry_type: str, data: dict, time_ms: int) -> None:
    """Append *entry_type*, then rewrite its ``time`` to *time_ms*.

    The writer assigns ``time`` from the clock, and these tests need a close that
    happened months ago. Editing the line afterwards is confined to the test: the
    entry keeps its real shape, so the sweep still reads it exactly as it reads a
    genuinely old one.
    """
    ledger.append(entry_type, data, src="gateway")
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    last["time"] = time_ms
    lines[-1] = json.dumps(last, separators=(",", ":"), sort_keys=True)
    ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _age_file(path, days: float) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def _age_close_entry(unit_id: str, *, days: float) -> None:
    """Rewrite the newest ``session/closed``'s ``time`` to *days* ago.

    The writer assigns ``time`` from the clock, so a test that needs an expired
    close edits the line afterwards. The entry keeps its real shape, so the sweep
    reads it exactly as it reads a genuinely old one.
    """
    path = _unit_dir(lg.KIND_SESSION, unit_id) / "ledger.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    for index in range(len(lines) - 1, -1, -1):
        entry = json.loads(lines[index])
        if entry.get("type") == _CLOSED:
            entry["time"] = store.now_ms() - int(days * DAY_MS)
            lines[index] = json.dumps(entry, separators=(",", ":"), sort_keys=True)
            break
    else:  # pragma: no cover - callers assert the close exists first
        raise AssertionError(f"{unit_id} has no close to age")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _remove(unit_id: str = SESSION, *, kind: str = lg.KIND_SESSION) -> str:
    """``remove_unit`` with the accept-all guard, for the ordering tests.

    Those tests are about the removal's own mechanics -- ownership, order, counted
    failures -- so the re-decision is not what they exercise. The guard's own
    behaviour has its own tests below.
    """
    return store.remove_unit(kind, unit_id, guard=lambda _dir: True)


def _unit_dir(kind: str, unit_id: str):
    return lg.ledger_root(kind) / _store_name(unit_id)


# --- remove_unit: ownership -------------------------------------------------


def test_remove_unit_removes_every_file_and_the_directory():
    ledger = _closed_session()
    directory = ledger.path.parent
    ledger.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    del ledger

    assert _remove() == store.REMOVE_REMOVED
    assert not directory.exists()
    assert not Ledger.exists(lg.KIND_SESSION, SESSION)


def test_remove_unit_refuses_while_this_process_holds_a_handle_that_wrote():
    """The lease is refcounted, so a shared claim would prove nothing.

    A plain ``acquire`` against a unit this process already writes SUCCEEDS by
    incrementing the count. If the removal took that shared lock it would be
    permitted to unlink the segments the live handle is still appending to, so it
    asks for sole ownership and is refused instead.
    """
    ledger = _closed_session()
    # The append is what claims the lease -- ownership is taken lazily, on a
    # handle's first write, never by ``open``.
    ledger.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")

    assert _remove() == store.REMOVE_OWNED
    assert ledger.path.exists()
    # Still writable: the refused removal took nothing and released nothing.
    ledger.append("turn/completed", {"turn": 1, "depth": 0, "stop_reason": "end"}, src="gateway")


def test_remove_unit_refuses_while_another_process_owns_the_unit():
    ledger = _closed_session()
    del ledger
    lease_path = _unit_dir(lg.KIND_SESSION, SESSION) / LEASE_FILE
    ready = multiprocessing.Event()
    done = multiprocessing.Event()
    holder = multiprocessing.Process(target=_hold_lease, args=(str(lease_path), ready, done))
    holder.start()
    try:
        assert ready.wait(timeout=10)
        assert _remove() == store.REMOVE_OWNED
        assert lg.ledger_path(lg.KIND_SESSION, SESSION).exists()
    finally:
        done.set()
        holder.join(timeout=10)


def _hold_lease(lease_path: str, ready, done) -> None:  # pragma: no cover - child process
    from pathlib import Path

    from kiro_crew.platform_compat import file_lock

    path = Path(lease_path)
    path.touch(exist_ok=True)
    with open(path, "r+") as handle:
        with file_lock(handle.fileno(), exclusive=True, required=True, wait=False):
            ready.set()
            done.wait(timeout=30)


def test_a_writer_is_refused_while_a_removal_of_that_unit_is_in_flight():
    """Sole ownership blocks BOTH directions.

    Refusing only the deleter would be one-directional: a writer arriving second
    would join the deleter's own lock through the reference count and append into
    a unit whose files are being unlinked.
    """
    ledger = _closed_session()
    del ledger
    lease_path = _unit_dir(lg.KIND_SESSION, SESSION) / LEASE_FILE
    key = lg.lease.acquire(lease_path, kind=lg.KIND_SESSION, unit_id=SESSION, sole=True)
    try:
        with pytest.raises(lg.LedgerError) as excinfo:
            lg.lease.acquire(lease_path, kind=lg.KIND_SESSION, unit_id=SESSION)
        assert excinfo.value.code == "already_owned"
        with pytest.raises(lg.LedgerError):
            lg.lease.acquire(lease_path, kind=lg.KIND_SESSION, unit_id=SESSION, sole=True)
    finally:
        lg.lease.release(key)
    # Released: ownership is available again, sole or shared.
    lg.lease.release(lg.lease.acquire(lease_path, kind=lg.KIND_SESSION, unit_id=SESSION))


# --- remove_unit: ordering and failure -------------------------------------


def test_remove_unit_removes_the_lease_file_last():
    """Order is the correctness argument, so it is asserted rather than assumed.

    While the lease exists, its path names the file whose lock proves ownership.
    A lease unlinked before the segments would let a second remover take a lock
    on a fresh inode at the same path and unlink the same files concurrently.
    """
    ledger = _closed_session()
    directory = ledger.path.parent
    del ledger
    order: list[str] = []

    # ``Path.unlink`` is the only spelling the removal uses -- for the segments,
    # the lock, and the lease alike (``unlink_lock_in_hold`` included, which the
    # final assertion below proves by observing the lease at all). So patching it
    # sees every unlink in the unit, and an ``os.unlink`` observer would add a
    # channel nothing writes to.
    from pathlib import Path

    real_path_unlink = Path.unlink

    def _record_path(self, *args, **kwargs):
        if self.parent == directory:
            order.append(self.name)
        return real_path_unlink(self, *args, **kwargs)

    Path.unlink = _record_path
    try:
        assert _remove() == store.REMOVE_REMOVED
    finally:
        Path.unlink = real_path_unlink

    assert "ledger.jsonl" in order, order
    assert order[-1] == LEASE_FILE, order
    assert order.index("ledger.jsonl") < order.index(LEASE_FILE), order


def test_remove_unit_counts_a_failure_and_keeps_the_history():
    """A partial removal is reported, never counted as a removal.

    ``rmtree(ignore_errors=True)`` would report success over a subtree it left
    standing. The unit keeps its segments, so it still reads as a ledger and the
    next pass can aim at it again.
    """
    ledger = _closed_session()
    directory = ledger.path.parent
    del ledger
    from pathlib import Path

    real_unlink = Path.unlink

    def _refuse_segment(self, *args, **kwargs):
        if self.name == "ledger.jsonl":
            raise OSError("held")
        return real_unlink(self, *args, **kwargs)

    Path.unlink = _refuse_segment
    try:
        assert _remove() == store.REMOVE_FAILED
    finally:
        Path.unlink = real_unlink

    assert lg.ledger_path(lg.KIND_SESSION, SESSION).exists()
    assert (directory / LEASE_FILE).exists()
    # Still addressable by id, which is what lets a later pass retry it.
    assert Ledger.exists(lg.KIND_SESSION, SESSION)


def test_remove_unit_survives_the_windows_unlink_rule_for_the_held_lease():
    """Windows refuses to unlink a file whose lock is held; the late unlink gets it.

    Reuses ``session_ledger.unlink_lock_in_hold``'s contract rather than a second
    copy of it, so simulating the platform is simulating that one function.
    """
    ledger = _closed_session()
    directory = ledger.path.parent
    del ledger
    store.unlink_lock_in_hold  # the reused helper, patched at the store's binding

    def _refuse_in_hold(_path) -> bool:
        return False

    original = store.unlink_lock_in_hold
    store.unlink_lock_in_hold = _refuse_in_hold
    try:
        assert _remove() == store.REMOVE_REMOVED
    finally:
        store.unlink_lock_in_hold = original
    assert not directory.exists()


def test_remove_unit_answers_absent_for_a_unit_that_has_no_directory():
    assert _remove("never-existed") == store.REMOVE_ABSENT


def test_remove_unit_never_follows_a_unit_directory_linked_to_another_unit():
    """The hazard is a link that stays INSIDE the root, and it needs the guard.

    ``ledger_dir`` returns the RESOLVED path, so such a link passes containment
    and hands the removal its target: without checking the name as written, one
    unit's id would delete another unit's history. Checking the resolved path
    cannot catch it -- the target is not a link.
    """
    victim = _closed_session("s-victim", closed_days_ago=90)
    victim_dir = victim.path.parent
    del victim
    link = lg.ledger_root(lg.KIND_SESSION) / _store_name("s-attacker")
    try:
        link.symlink_to(victim_dir, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform without symlinks
        pytest.skip("symlinks unavailable")

    assert _remove("s-attacker") == store.REMOVE_ABSENT
    assert (victim_dir / "ledger.jsonl").exists()
    assert Ledger.exists(lg.KIND_SESSION, "s-victim")


def test_a_unit_directory_linked_outside_the_root_is_refused_by_containment(tmp_path):
    """The other half: containment refuses it before ownership is even asked for."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("intact", encoding="utf-8")
    root = lg.ledger_root(lg.KIND_SESSION)
    root.mkdir(parents=True, exist_ok=True)
    link = root / _store_name("s-outside")
    try:
        link.symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform without symlinks
        pytest.skip("symlinks unavailable")

    assert _remove("s-outside") == store.REMOVE_ABSENT
    assert (elsewhere / "keep.txt").read_text(encoding="utf-8") == "intact"


def test_a_linked_unit_directory_never_causes_the_target_to_be_removed():
    """The property, not one line: no link in the root reaches its target's history.

    Built so the link is the ONLY route -- the target directory is moved out of
    the sessions root, and the link's own name folds to the id its header carries,
    so containment and the fold-back check both pass and the link itself is all
    that is left to stop the removal.

    Two guards hold it: the sweep skips a linked child in the listing, and
    ``remove_unit`` refuses a unit whose directory NAME is a link (``ledger_dir``
    returns the RESOLVED path, so by then the caller holds the target). Removing
    either alone leaves the property standing, so it is asserted as the property
    and the mutation harness removes both together.
    """
    hidden = _closed_session("s-hidden", closed_days_ago=400)
    hidden_dir = hidden.path.parent
    del hidden
    stash = lg.ledger_root(lg.KIND_SESSION).parent / "stashed-target"
    hidden_dir.rename(stash)
    link = lg.ledger_root(lg.KIND_SESSION) / _store_name("s-hidden")
    try:
        link.symlink_to(stash, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform without symlinks
        pytest.skip("symlinks unavailable")

    assert store.sweep_expired(30) == (0, 0)
    assert (stash / "ledger.jsonl").exists()
    assert _remove("s-hidden") == store.REMOVE_ABSENT
    assert (stash / "ledger.jsonl").exists()


# --- sweep_expired: what it selects ----------------------------------------


def test_sweep_removes_a_session_closed_before_the_cutoff():
    ledger = _closed_session(closed_days_ago=90)
    del ledger

    assert store.sweep_expired(30) == (1, 0)
    assert not Ledger.exists(lg.KIND_SESSION, SESSION)


def test_sweep_keeps_a_session_closed_inside_the_window():
    ledger = _closed_session(closed_days_ago=5)
    del ledger

    assert store.sweep_expired(30) == (0, 0)
    assert Ledger.exists(lg.KIND_SESSION, SESSION)


def test_sweep_never_touches_an_open_session_however_old_it_is():
    """No ``session/closed`` means the session is OPEN, whatever its age says.

    This is the check that decides it, so it is pinned on a unit whose file mtime
    is far past the cutoff: an mtime-only rule would delete a live session's log.
    """
    ledger = _open_session(age_days=400)
    del ledger

    assert store.sweep_expired(30) == (0, 0)
    assert Ledger.exists(lg.KIND_SESSION, SESSION)


def test_sweep_ages_from_the_close_entry_not_the_file_mtime():
    """The entry's own ``time`` wins, because mtime is metadata a restore resets.

    A restored or copied tree carries a fresh mtime on a months-old ledger. Aging
    from mtime would make such a unit read as just-closed and survive retention
    forever, so the writer's own record of when the session ended is authoritative.
    """
    ledger = _closed_session(closed_days_ago=90)
    _age_file(ledger.path, 0)  # as a copy or restore would leave it
    del ledger

    assert store.sweep_expired(30) == (1, 0)
    assert not Ledger.exists(lg.KIND_SESSION, SESSION)


def test_sweep_falls_back_to_mtime_when_the_close_entry_has_no_usable_time():
    """A damaged ``time`` still proves the session ended.

    mtime is then the best available bound on when writing stopped, and it can
    only be at or after the real close, so it errs toward keeping the file.
    """
    ledger = _closed_session(closed_days_ago=1)
    path = ledger.path
    del ledger
    lines = path.read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    last["time"] = 0
    lines[-1] = json.dumps(last, separators=(",", ":"), sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _age_file(path, 90)

    assert store.sweep_expired(30) == (1, 0)
    assert not Ledger.exists(lg.KIND_SESSION, SESSION)


def test_sweep_reads_the_newest_close_when_a_resumed_session_closed_twice():
    """A resumed session appends to the ledger it already had.

    So a file can hold more than one close, and only the newest describes the
    life that ended. Reading the first would expire a session that came back.
    """
    ledger = Ledger.create(lg.KIND_SESSION, SESSION, owner=CREW, agent="kirocrew")
    _append_at(ledger, "session/closed", {"reason": "destroyed"}, store.now_ms() - 90 * DAY_MS)
    ledger.append("session/opened", {"resumed": True}, src="gateway")
    _append_at(ledger, "session/closed", {"reason": "destroyed"}, store.now_ms() - 1 * DAY_MS)
    del ledger

    assert store.sweep_expired(30) == (0, 0)
    assert Ledger.exists(lg.KIND_SESSION, SESSION)


def test_sweep_never_expires_a_session_that_was_REOPENED_after_its_close():
    """Whether a unit is closed is decided by the newest of the lifecycle PAIR.

    A resumed session appends to the ledger it already had, so ``... closed ...
    opened ...`` is a legitimate file whose session is running RIGHT NOW. Reading
    the newest close on its own calls it expired and deletes a live
    conversation's log -- the same rule as "an open session is never touched",
    for the unit that reaches that state by coming back rather than by never
    having left.

    Both lifecycle entries are aged past the cutoff, which is the case the rule
    has to answer: a long-running session revived a year ago and still open is
    old by every clock in the file, and only the ORDER of the pair says it is
    live.
    """
    ledger = Ledger.create(lg.KIND_SESSION, SESSION, owner=CREW, agent="kirocrew")
    _append_at(ledger, _CLOSED, {"reason": "destroyed"}, store.now_ms() - 400 * DAY_MS)
    _append_at(ledger, "session/opened", {"resumed": True}, store.now_ms() - 399 * DAY_MS)
    _append_at(
        ledger,
        "turn/started",
        {"turn": 1, "actor": "user", "depth": 0},
        store.now_ms() - 398 * DAY_MS,
    )
    path = ledger.path
    del ledger
    _age_file(path, 398)

    assert store.sweep_expired(30) == (0, 0)
    assert Ledger.exists(lg.KIND_SESSION, SESSION)


def test_an_in_flight_closer_after_the_teardown_still_reads_as_closed():
    """The other side of that rule: a non-lifecycle entry says nothing.

    The emitter writes a dying turn's closers AFTER ``session/closed`` by design,
    so a scan that treated any later entry as a revival would never collect a
    normally-closed unit.
    """
    ledger = Ledger.create(lg.KIND_SESSION, SESSION, owner=CREW, agent="kirocrew")
    _append_at(ledger, _CLOSED, {"reason": "destroyed"}, store.now_ms() - 90 * DAY_MS)
    ledger.append(
        "tool/completed",
        {"turn": 1, "step": 1, "call_id": "c", "status": "unknown"},
        src="gateway",
    )
    ledger.append(
        "turn/completed",
        {"turn": 1, "depth": 0, "stop_reason": "interrupted"},
        src="gateway",
    )
    del ledger

    assert store.sweep_expired(30) == (1, 0)
    assert not Ledger.exists(lg.KIND_SESSION, SESSION)


def test_the_sweep_re_decides_inside_the_lease_and_stands_down_on_a_revival():
    """The scan is a SNAPSHOT, and ownership ends between turns.

    So a session can be revived, append, finish its turn and release the lease in
    the window between the decision and the delete -- after which an unguarded
    removal takes a live conversation's log while contending with nobody. The
    re-decision runs inside the removal's own hold, which is the only place the
    answer is current.
    """
    ledger = _closed_session(closed_days_ago=90)
    path = ledger.path
    del ledger
    real_guard_input = store._expired_unit_id

    def _revive_then_answer(directory, cutoff_ms):
        # Stand in for the revival landing after the scan and before the hold.
        if getattr(_revive_then_answer, "done", False):
            return real_guard_input(directory, cutoff_ms)
        _revive_then_answer.done = True
        answer = real_guard_input(directory, cutoff_ms)
        revived = Ledger.open(lg.KIND_SESSION, SESSION)
        revived.append("session/opened", {"resumed": True}, src="gateway")
        del revived
        return answer

    store._expired_unit_id = _revive_then_answer
    try:
        assert store.sweep_expired(30) == (0, 0)
    finally:
        store._expired_unit_id = real_guard_input

    assert path.exists()
    assert Ledger.exists(lg.KIND_SESSION, SESSION)


def test_the_guard_is_required_so_no_caller_can_skip_the_re_decision():
    """No default that skips it -- the same stance ``purge_matching`` takes.

    A caller whose reason is not a property of the file passes an accept-all
    guard and says so at its call site, which is a visible decision rather than
    an omitted argument.
    """
    with pytest.raises(TypeError):
        store.remove_unit(lg.KIND_SESSION, SESSION)  # type: ignore[call-arg]


def test_a_unit_another_remover_took_first_is_not_counted_as_a_failure():
    """A race that already collected a unit got the outcome this sweep wanted.

    The delete funnel and this sweep can aim at one unit at once, so a segment
    listed a moment ago can be gone by the time it is read. Counting that as
    ``failed`` would make the number an operator reads as "these units still hold
    history" a lie.
    """
    doomed = _closed_session("s-raced", closed_days_ago=90)
    doomed_path = doomed.path
    del doomed
    other = _closed_session("s-normal", closed_days_ago=90)
    del other
    real_scan = store._scan_tail

    def _delete_then_scan(path):
        if path == doomed_path and path.exists():
            # Stand in for the other remover finishing between the listing and
            # this read.
            path.unlink()
        return real_scan(path)

    store._scan_tail = _delete_then_scan
    try:
        removed, failed = store.sweep_expired(30)
    finally:
        store._scan_tail = real_scan

    assert failed == 0, "a unit another remover took is not a failure"
    assert removed == 1
    assert not Ledger.exists(lg.KIND_SESSION, "s-normal")


def test_sweep_leaves_a_torn_tail_to_the_resume_repair():
    """Unterminated trailing bytes are what ``open(repair=True)`` truncates.

    The sweep cannot tell a dead writer's crash artifact from an append that has
    not reached its fsync -- the bytes are identical -- so deleting the unit would
    destroy the history the repair exists to recover.
    """
    ledger = _closed_session(closed_days_ago=90)
    path = ledger.path
    del ledger
    with open(path, "ab") as handle:
        handle.write(b'{"type":"turn/started","seq":9')

    assert store.sweep_expired(30) == (0, 0)
    assert path.exists()


def test_sweep_still_collects_a_unit_whose_segments_the_READER_refuses():
    """A ledger too damaged to read is still expired, and that is deliberate.

    Segment provenance is checked on the READ path: a filename whose declared
    first seq disagrees with the file's first entry makes ``iter_from`` raise
    ``CODE_BAD_SEGMENT``. Retention does not go through that path -- it reads the
    header line and a bounded tail directly -- so a unit the reader refuses is
    still collectable.

    That is the wanted behaviour, not a gap the sweep should close by borrowing
    the reader's check. Gating removal on readability would make a damaged ledger
    IMMORTAL: the one unit nothing can use would be the one unit retention could
    never reclaim, and the corruption would be preserved forever by the very rule
    meant to protect history. The sweep's own guards do not depend on the entries
    being readable end to end -- the header id must still match the directory, the
    tail must be untorn, and the newest lifecycle entry must still be the close --
    so what it drops is a closed, aged, unowned unit either way.
    """
    ledger = _closed_session(closed_days_ago=90)
    directory = ledger.path.parent
    del ledger
    # A second segment whose header is genuinely this unit's -- so the header half
    # of the check passes -- but whose filename claims a first seq its own first
    # entry does not carry.
    (directory / "ledger.9.jsonl").write_bytes((directory / "ledger.jsonl").read_bytes())

    reader = lg.Ledger.open(lg.KIND_SESSION, SESSION)
    with pytest.raises(lg.LedgerError) as refused:
        list(reader.iter_from())
    assert refused.value.code == lg.CODE_BAD_SEGMENT
    del reader

    assert store.sweep_expired(30) == (1, 0)
    assert not directory.exists()


def test_sweep_never_scans_crew_ledgers():
    """Crew ledgers are out of scope: no writer, and no close to age from.

    Pinned with a crew ledger carrying a ``session/closed`` line WRITTEN DIRECTLY
    to the file, aged past the cutoff. It has to be planted rather than appended,
    because a crew ledger cannot own a ``session`` type through the API
    (``event_type_not_owned``) -- and that is exactly the case scoping must answer:
    this tree is agent-writable, so "no crew ledger can contain that entry" is a
    property of the writer, not of the bytes. Without the scoping such a unit
    reads as an expired session and is removed.
    """
    crew = Ledger.create(lg.KIND_CREW, CREW)
    crew_path = crew.path
    del crew
    planted = {
        "type": _CLOSED,
        "seq": 1,
        "time": store.now_ms() - 400 * DAY_MS,
        "src": "gateway",
        "data": {"reason": "destroyed"},
    }
    with open(crew_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(planted, separators=(",", ":"), sort_keys=True) + "\n")
    _age_file(crew_path, 400)
    expired = _closed_session(closed_days_ago=90)
    del expired

    assert store.sweep_expired(30) == (1, 0)
    assert crew_path.exists()
    assert Ledger.exists(lg.KIND_CREW, CREW)


def test_sweep_skips_a_unit_whose_header_id_does_not_address_its_directory():
    """A directory no id resolves to is not removed, and the reason is the hazard.

    The removal is aimed BY ID, so a directory whose header names a DIFFERENT
    unit would have the removal land on that unit instead. Pinned with a planted
    directory carrying a live unit's id and an expired close: without the check,
    sweeping the plant deletes the live unit's history.
    """
    live = _closed_session("s-live", closed_days_ago=1)  # inside the window
    live_path = live.path
    del live
    plant = lg.ledger_root(lg.KIND_SESSION) / "planted-not-a-fold"
    plant.mkdir(parents=True)
    header = json.loads(live_path.read_text(encoding="utf-8").splitlines()[0])
    aged = {
        "type": _CLOSED,
        "seq": 1,
        "time": store.now_ms() - 400 * DAY_MS,
        "src": "gateway",
        "data": {"reason": "destroyed"},
    }
    (plant / "ledger.jsonl").write_text(
        json.dumps(header, separators=(",", ":"), sort_keys=True)
        + "\n"
        + json.dumps(aged, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    assert store.sweep_expired(30) == (0, 0)
    assert live_path.exists()
    assert Ledger.exists(lg.KIND_SESSION, "s-live")
    assert (plant / "ledger.jsonl").exists()


def test_sweep_skips_a_unit_a_writer_still_owns_and_removes_its_neighbour():
    """One owned unit must not stop the pass, and must not be counted as removed."""
    owned = _closed_session("s-owned", closed_days_ago=90)
    owned.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")
    free = _closed_session("s-free", closed_days_ago=90)
    del free

    assert store.sweep_expired(30) == (1, 0)
    assert Ledger.exists(lg.KIND_SESSION, "s-owned")
    assert not Ledger.exists(lg.KIND_SESSION, "s-free")
    assert owned.path.exists()


def test_a_negative_retention_disables_the_ledger_sweep_like_the_archive_one():
    """One switch, both halves. A user who turned expiry off turned this off too."""
    ledger = _closed_session(closed_days_ago=4000)
    del ledger

    assert store.sweep_expired(-1) == (0, 0)
    assert Ledger.exists(lg.KIND_SESSION, SESSION)


def test_sweep_is_a_no_op_when_the_ledger_root_was_never_created():
    """What makes this de facto gated by the emitter's flag without reading it."""
    assert not lg.ledger_root(lg.KIND_SESSION).exists()
    assert store.sweep_expired(30) == (0, 0)


def test_sweep_refuses_a_linked_session_root_and_reads_nothing_under_it(tmp_path):
    """The walk resolves the CHECKED root, so a linked kind directory is refused once.

    ``ledger_dir`` refuses this root, so the removal was never in danger -- but the
    refusal arrived one unit at a time, AFTER this walk had opened a header and a
    tail from every file the link named. Reading a file outside the data home is
    the thing the root guard exists to prevent, so the walk has to be refused too.

    The unit planted under the link is a REAL expired one, which is what makes the
    return value decide this rather than a mount option: had the walk run, that
    unit would have been selected and then refused by ``ledger_dir``, counting one
    failure. ``(0, 0)`` is reachable only if the root was refused before the walk.
    An atime probe would not do -- ``relatime`` and ``noatime`` are common enough
    that "the file was not read" would assert itself.
    """
    elsewhere = tmp_path / "attacker-writable"
    ledger = _closed_session(unit_id="s-outside", closed_days_ago=90)
    del ledger

    kind_root = lg.ledger_root(lg.KIND_SESSION)
    kind_root.rename(elsewhere)
    kind_root.symlink_to(elsewhere, target_is_directory=True)
    planted = elsewhere / _store_name("s-outside") / "ledger.jsonl"
    assert planted.exists(), "fixture did not plant a readable unit under the link"

    assert store.sweep_expired(30) == (0, 0), "the walk ran under a linked root"
    assert planted.exists(), "the sweep removed a file outside the data home"


def test_sweep_writes_nothing_into_a_ledger_it_keeps():
    """Removal is not rotation: no tombstone, no ``pruned`` entry, ever.

    A citation into a removed unit is already answered by ``Ref.resolve``, which
    reports ``gone`` for a pointer into a unit that has no ledger at all.
    """
    ledger = _open_session(age_days=400)
    path = ledger.path
    del ledger
    before = path.read_bytes()

    store.sweep_expired(30)
    assert path.read_bytes() == before


# --- the history sweep calls it on the same switch -------------------------


def test_history_archive_cleanup_expires_ledgers_on_the_same_setting(monkeypatch):
    """``session.archive_retention_days`` governs both halves, in one pass."""
    from kiro_crew import history

    ledger = _closed_session(closed_days_ago=90)
    del ledger
    monkeypatch.setattr(history, "_last_cleanup", 0.0)

    history._cleanup_old_archives(retention_days=30)
    assert not Ledger.exists(lg.KIND_SESSION, SESSION)


def test_history_archive_cleanup_honours_the_disable_for_ledgers_too(monkeypatch):
    from kiro_crew import history

    ledger = _closed_session(closed_days_ago=4000)
    del ledger
    monkeypatch.setattr(history, "_last_cleanup", 0.0)

    history._cleanup_old_archives(retention_days=-1)
    assert Ledger.exists(lg.KIND_SESSION, SESSION)


def test_history_archive_cleanup_sweeps_ledgers_with_no_archive_directory(monkeypatch):
    """An absent archive dir is not a reason to skip the ledger half.

    A session holds a ledger long before anything of its transcript is archived,
    so an early return there would leave that half uncollected until the first
    archive was ever written.
    """
    from kiro_crew import history

    ledger = _closed_session(closed_days_ago=90)
    del ledger
    monkeypatch.setattr(history, "_last_cleanup", 0.0)
    assert not history._archive_dir(None).exists()

    history._cleanup_old_archives(retention_days=30)
    assert not Ledger.exists(lg.KIND_SESSION, SESSION)


def test_a_failing_ledger_sweep_never_breaks_the_transcript_archive(monkeypatch):
    """The caller is on the archive path: raising here would lose history.

    Retaining too much is a disk-space problem; failing to archive the transcript
    loses the record, so the ledger half is contained.
    """
    from kiro_crew import history

    monkeypatch.setattr(history, "_last_cleanup", 0.0)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("ledger tree unreadable")

    monkeypatch.setattr(store, "sweep_expired", _boom)
    # Returns the archive count without propagating the ledger failure.
    assert history._cleanup_old_archives(retention_days=30) == 0


# --- what a failed removal claims about the history ------------------------


def test_a_partial_removal_reports_that_the_history_is_already_gone(caplog, monkeypatch):
    """Segments go first, so the ordinary failure is history GONE, not history kept.

    Reporting it as kept would send a reader looking for a record this pass
    destroyed, which is the one thing an append-only store must never say.
    """
    ledger = _closed_session()
    del ledger
    real_unlink = store.Path.unlink

    def _refuse_lock(self, *args, **kwargs):
        if self.name.endswith(".lock"):
            raise OSError("held")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(store.Path, "unlink", _refuse_lock)
    with caplog.at_level("WARNING"):
        assert _remove() == store.REMOVE_FAILED

    text = caplog.text
    assert "PARTLY removed" in text
    assert "history file(s) already gone" in text
    assert "history is intact" not in text


def test_a_removal_that_got_nowhere_reports_the_history_intact(caplog, monkeypatch):
    """The other half of the same line: nothing went, so nothing may be implied gone."""
    ledger = _closed_session()
    del ledger
    real_unlink = store.Path.unlink

    def _refuse_everything(self, *args, **kwargs):
        raise OSError("held")

    monkeypatch.setattr(store.Path, "unlink", _refuse_everything)
    with caplog.at_level("WARNING"):
        assert _remove() == store.REMOVE_FAILED

    assert "history is intact" in caplog.text
    assert "PARTLY removed" not in caplog.text
    monkeypatch.setattr(store.Path, "unlink", real_unlink)
    assert Ledger.exists(lg.KIND_SESSION, SESSION)


# --- every destroy records its teardown, which is what makes a unit collectable ---


def _provider_factory_reporting(session_id: str):
    """A provider factory whose mock reports *session_id* as an ACP id.

    ``session_id_of`` reads ``session_id``/``_session_id`` and requires a real
    ``str``, so a bare ``AsyncMock`` attribute -- a Mock, not a string -- makes
    every emitter call a no-op. Set explicitly, or this test would pass by writing
    nothing at all.
    """
    from unittest.mock import AsyncMock

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.is_process_alive = lambda: True
        provider.context_usage_pct = lambda: 0.0
        provider.context_window_tokens = lambda: 0
        provider.has_active_turn = lambda: False
        provider.runtime_info = lambda: (None, None)
        provider.session_id = session_id
        return provider

    return factory


def _newest_lifecycle_type(unit_id: str) -> str:
    """The type of the newest ``session/opened``/``session/closed`` in the unit."""
    path = _unit_dir(lg.KIND_SESSION, unit_id) / "ledger.jsonl"
    newest = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") in {"session/opened", _CLOSED}:
            newest = entry["type"]
    return newest


@pytest.mark.asyncio
async def test_destroying_a_session_records_the_teardown_in_its_ledger(monkeypatch):
    """Without this entry NEITHER half of retention can ever collect the unit.

    The emitter holds a destroyed session's cached handle, and the write lease
    that handle carries, so a removal claiming the lease ``sole`` answers
    ``owned``; the sweep is blocked from the other side, because a unit whose
    newest lifecycle entry is not a close reads as OPEN whatever its age. So the
    close is not bookkeeping -- it is the entry that makes the unit collectable,
    and ``destroy`` writes it for the same reason the reset route does.

    Driven through the REAL ``destroy`` rather than a stub. A mocked teardown that
    omits the emit is exactly how this defect stayed invisible: every removal test
    built its ledger with ``Ledger.create``, which leaves nothing holding the
    unit, so they were green while the gateway could not collect a real one.
    """
    from kiro_crew import session_ledger_emit as emit
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager

    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, "1")
    emit.reset_caches()
    try:
        cfg = KiroCrewConfig()
        cfg.session.timeout_secs = 2
        manager = SessionManager(cfg, provider_factory=_provider_factory_reporting("acp-torn-down"))
        await manager.get_or_create("thread-doomed")

        emit.on_session_opened("acp-torn-down", agent="kirocrew", slot="thread-doomed")
        assert emit.flush(timeout=5.0)
        assert _newest_lifecycle_type("acp-torn-down") == "session/opened"

        # ``SessionManager.destroy`` returns None -- it awaits the lifecycle's own
        # destroy and discards its bool -- so the effect is what is asserted.
        await manager.destroy("thread-doomed")
        assert emit.flush(timeout=5.0)

        assert _newest_lifecycle_type("acp-torn-down") == _CLOSED
    finally:
        emit.reset_caches()


@pytest.mark.asyncio
async def test_a_destroyed_sessions_ledger_is_then_collectable_by_the_sweep(monkeypatch):
    """The end the teardown entry buys: an aged destroyed unit is collected.

    Pinned end to end rather than trusting the entry alone, because the two
    failures this closes are one missing entry seen from two sides -- the funnel
    refused by a lease nobody released, and the sweep skipping a unit that reads
    as open. A test on the entry's presence would prove neither.
    """
    from kiro_crew import session_ledger_emit as emit
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager

    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, "1")
    emit.reset_caches()
    try:
        cfg = KiroCrewConfig()
        cfg.session.timeout_secs = 2
        manager = SessionManager(cfg, provider_factory=_provider_factory_reporting("acp-aged"))
        await manager.get_or_create("thread-aged")
        emit.on_session_opened("acp-aged", agent="kirocrew", slot="thread-aged")
        assert emit.flush(timeout=5.0)
        assert await manager.destroy("thread-aged") is None
        assert emit.flush(timeout=5.0)

        # Age the close the sweep reads, the same way the fixtures above do.
        path = _unit_dir(lg.KIND_SESSION, "acp-aged") / "ledger.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        for index in range(len(lines) - 1, -1, -1):
            entry = json.loads(lines[index])
            if entry.get("type") == _CLOSED:
                entry["time"] = store.now_ms() - 90 * DAY_MS
                lines[index] = json.dumps(entry, separators=(",", ":"), sort_keys=True)
                break
        else:  # pragma: no cover - the assertion above already proved it is there
            raise AssertionError("destroy wrote no close to age")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # No ``reset_caches`` between the close and this sweep, deliberately. The
        # close entry is what RELEASES the emitter's handle -- ``on_session_closed``
        # drops it once no turn of that session is pinned -- so by the time the
        # entry has landed the lease is already free and the ordinary sweep can
        # claim it. That is the same release the delete funnel's flush waits for,
        # observed from the other caller.
        assert store.sweep_expired(30) == (1, 0)
        assert not Ledger.exists(lg.KIND_SESSION, "acp-aged")
    finally:
        emit.reset_caches()


def test_the_header_slot_reader_refuses_a_unit_whose_id_does_not_fold_back():
    """Same refusal the sweep makes: a directory holding another unit's id.

    The reader answers a caller that is CHECKING an id it does not fully trust, so
    a header reachable at the wrong directory would hand back that other unit's
    slot and defeat the check it exists for.
    """
    ledger = Ledger.create(
        lg.KIND_SESSION, "s-real", owner=CREW, agent="kirocrew", slot="dashboard_chat-1"
    )
    del ledger
    assert store.unit_header_slot(lg.KIND_SESSION, "s-real") == "dashboard_chat-1"

    # Same bytes, a directory that answers to a different id.
    source = _unit_dir(lg.KIND_SESSION, "s-real")
    impostor = _unit_dir(lg.KIND_SESSION, "s-impostor")
    shutil.copytree(source, impostor)

    assert store.unit_header_slot(lg.KIND_SESSION, "s-impostor") is None


def test_the_header_slot_reader_answers_none_for_a_unit_that_is_not_there():
    assert store.unit_header_slot(lg.KIND_SESSION, "s-absent") is None


# --- only a close the gateway can PROVE is terminal authorizes collection ---


def test_only_a_destroy_close_authorizes_collection():
    """``destroy`` DELETES the id's mapping, unconditionally, before writing this.

    An id absent from the map cannot be resumed by anything, and the gateway
    performed that deletion itself inside the registry lock. That is the positive
    proof this rule needs, and it comes from inside the fenced tree rather than from
    any file an agent can write.
    """
    ledger = _closed_session(closed_days_ago=90, reason="destroyed")
    del ledger

    assert store.sweep_expired(30) == (1, 0)
    assert not Ledger.exists(lg.KIND_SESSION, SESSION)


@pytest.mark.parametrize(
    "reason", ["reset", "discarded", "shutdown", "crashed", "evicted", "tab_closed", ""]
)
def test_a_close_that_does_not_end_the_ids_life_is_never_collected(reason):
    """The gateway stopped SERVING the session; the conversation can still come back.

    ``reset`` is the one worth naming, because the intuitive reading is wrong: a
    reset does cold-start its successor on a new id, but its own ``clear_sid`` is
    guarded by ``if clear_conversation and session is not None``, so a reset that
    keeps the conversation writes this close while LEAVING the old id mapped --
    still resumable, and its log still needed. ``discarded`` clears the sid
    unconditionally and would qualify on that test, but no path writes it into a
    ledger today, so admitting it would be a rule about a file nothing produces.
    A shutdown, a crash, an eviction and an unknown spelling all leave the id
    mapped too. Retaining costs disk; deleting one of these destroys a live
    conversation's history.
    """
    ledger = _closed_session(closed_days_ago=400, reason=reason)
    del ledger

    assert store.sweep_expired(30) == (0, 0)
    assert Ledger.exists(lg.KIND_SESSION, SESSION)


def test_a_valid_empty_session_map_cannot_authorize_collecting_a_reset_closed_unit():
    """The exact lever the review found, pinned shut.

    ``session_map.json`` is agent-WRITABLE while the ledger tree is bind-masked, and
    a VALID empty map is not a failed read: it reads as "no session is revivable",
    which is a positive answer that would authorize a trusted sweep to delete a
    fenced unit the writer cannot touch directly. So nothing outside the fence takes
    part in the decision. The unit here is expired and closed, and its only
    protection is that its reason does not prove the id is dead.
    """
    from kiro_crew.config.loader import config_dir
    from kiro_crew.session_map import SESSION_MAP_FILENAME

    path = config_dir() / SESSION_MAP_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    ledger = _closed_session(closed_days_ago=400, reason="reset")
    del ledger

    assert store.sweep_expired(30) == (0, 0)
    assert Ledger.exists(lg.KIND_SESSION, SESSION)


def test_a_close_with_no_reason_field_at_all_is_never_collected():
    """Absent proof is not proof. The unit stays and the next pass sees it again."""
    ledger = Ledger.create(lg.KIND_SESSION, SESSION, owner=CREW, agent="kirocrew")
    ledger.append("session/opened", {"resumed": False}, src="gateway")
    _append_at(ledger, _CLOSED, {}, store.now_ms() - 400 * DAY_MS)
    del ledger

    assert store.sweep_expired(30) == (0, 0)
    assert Ledger.exists(lg.KIND_SESSION, SESSION)


def test_a_non_string_reason_is_read_as_absent_rather_than_matched():
    """The reason decides an irreversible deletion, so it is type-checked."""
    ledger = Ledger.create(lg.KIND_SESSION, SESSION, owner=CREW, agent="kirocrew")
    ledger.append("session/opened", {"resumed": False}, src="gateway")
    _append_at(ledger, _CLOSED, {"reason": ["destroyed"]}, store.now_ms() - 400 * DAY_MS)
    del ledger

    assert store.sweep_expired(30) == (0, 0)
    assert Ledger.exists(lg.KIND_SESSION, SESSION)


def test_the_sweep_reads_no_file_outside_the_ledger_tree(monkeypatch):
    """Nothing an agent can write takes part in authorizing a deletion.

    The session map was tried for this and removed: it is agent-writable while this
    tree is bind-masked, and a VALID empty map is not a failed read -- it reads as
    "nothing is revivable" and hands the trusted sweep a positive answer that
    authorizes deleting a fenced unit. This test fails if any read reaches outside
    the ledger root again, which is the only way that lever comes back.
    """
    root = lg.ledger_root(lg.KIND_SESSION).resolve()
    ledger = _closed_session(closed_days_ago=90)
    del ledger
    opened: list[str] = []
    real_open = io.open

    def _record_open(file, *args, **kwargs):
        try:
            resolved = os.path.realpath(str(file))
        except Exception:  # pragma: no cover - defensive
            resolved = str(file)
        opened.append(resolved)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", _record_open)
    monkeypatch.setattr("builtins.open", _record_open)
    assert store.sweep_expired(30) == (1, 0)

    strays = [path for path in opened if not path.startswith(str(root))]
    assert strays == [], f"the sweep read outside the ledger tree: {strays}"


# --- the marker asserts REVOCATION COMPLETED, not merely "a destroy ran" ---


def _newest_close_reason(unit_id: str) -> str:
    """The reason on the newest ``session/closed`` in the unit."""
    path = _unit_dir(lg.KIND_SESSION, unit_id) / "ledger.jsonl"
    reason = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") == _CLOSED:
            reason = (entry.get("data") or {}).get("reason", "")
    return reason


@pytest.mark.asyncio
async def test_a_destroy_that_leaves_another_mapping_writes_a_NON_terminal_reason(monkeypatch):
    """One map key is deleted, so a second key on the same sid still resolves it.

    The ledger unit is keyed by the ACP id, so that other holder shares this very
    log and can resume it. ``destroyed`` claims the id is globally revoked, so it is
    withheld here and the unit stays uncollectable -- retention prefers keeping a
    log it cannot prove is dead.
    """
    from kiro_crew import session_ledger_emit as emit
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager

    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, "1")
    emit.reset_caches()
    try:
        cfg = KiroCrewConfig()
        cfg.session.timeout_secs = 2
        manager = SessionManager(cfg, provider_factory=_provider_factory_reporting("acp-shared"))
        await manager.get_or_create("thread-shared")
        emit.on_session_opened("acp-shared", agent="kirocrew", slot="thread-shared")
        assert emit.flush(timeout=5.0)

        # A surviving holder of the same sid, exactly as a repeated transfer import
        # produces: a different key, the same session id.
        monkeypatch.setattr(
            manager._session_map, "find_key_by_sid", lambda _sid: "dashboard_chat-9-900"
        )
        await manager.destroy("thread-shared")
        assert emit.flush(timeout=5.0)

        assert _newest_close_reason("acp-shared") == "destroyed_sid_retained"
        _age_close_entry("acp-shared", days=400)
        assert store.sweep_expired(30) == (0, 0)
        assert Ledger.exists(lg.KIND_SESSION, "acp-shared")
    finally:
        emit.reset_caches()


@pytest.mark.asyncio
async def test_an_unreadable_map_also_withholds_the_terminal_reason(monkeypatch):
    """An unreadable map is not evidence of revocation, so the claim is withheld."""
    from kiro_crew import session_ledger_emit as emit
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager

    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, "1")
    emit.reset_caches()
    try:
        cfg = KiroCrewConfig()
        cfg.session.timeout_secs = 2
        manager = SessionManager(cfg, provider_factory=_provider_factory_reporting("acp-nomap"))
        await manager.get_or_create("thread-nomap")
        emit.on_session_opened("acp-nomap", agent="kirocrew", slot="thread-nomap")
        assert emit.flush(timeout=5.0)

        def _raise(_sid):
            raise RuntimeError("map unreadable")

        monkeypatch.setattr(manager._session_map, "find_key_by_sid", _raise)
        await manager.destroy("thread-nomap")
        assert emit.flush(timeout=5.0)

        assert _newest_close_reason("acp-nomap") == "destroyed_sid_retained"
    finally:
        emit.reset_caches()


@pytest.mark.asyncio
async def test_a_session_opened_after_a_destroy_makes_the_unit_uncollectable_again(monkeypatch):
    """Even a forged mapping's resume puts the unit back out of reach.

    The rule is the newest LIFECYCLE entry, so an `opened` after a `destroyed` reads
    as a live conversation whatever the close said. Anyone who plants a mapping and
    resumes therefore causes the log to be KEPT, not deleted.
    """
    from kiro_crew import session_ledger_emit as emit
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager

    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, "1")
    emit.reset_caches()
    try:
        cfg = KiroCrewConfig()
        cfg.session.timeout_secs = 2
        manager = SessionManager(cfg, provider_factory=_provider_factory_reporting("acp-revived"))
        await manager.get_or_create("thread-revived")
        emit.on_session_opened("acp-revived", agent="kirocrew", slot="thread-revived")
        assert emit.flush(timeout=5.0)
        monkeypatch.setattr(manager._session_map, "find_key_by_sid", lambda _sid: None)
        await manager.destroy("thread-revived")
        assert emit.flush(timeout=5.0)
        assert _newest_close_reason("acp-revived") == "destroyed"

        _age_close_entry("acp-revived", days=400)
        emit.on_session_opened("acp-revived", agent="kirocrew", slot="thread-revived", resumed=True)
        assert emit.flush(timeout=5.0)
        emit.reset_caches()

        assert store.sweep_expired(30) == (0, 0)
        assert Ledger.exists(lg.KIND_SESSION, "acp-revived")
    finally:
        emit.reset_caches()
