"""Regression guards: no connect, subscribe, reconnect or read path scans a whole log.

The bar every case here states with a FIXED number: a request, connect, subscribe,
reconnect or read may cost O(entries appended since the last fold), O(units the
caller names) or an O(1) cache hit -- never O(every entry of a log), O(every unit
under the crew-log root) or a listing of the whole root. A cold start with nothing to
resume from may fold once, and that one fold is the setup of each case, not what
it measures.

Every case drives the REAL path -- a real store on a temp data home, the real bus,
the real WS handler, the real eager worker -- and only counts. The meter wraps the
store's own read primitives and passes every value through unchanged, so what is
measured is what production reads. The synthetic logs are small enough to run in
seconds and large enough that a whole-log walk misses its bound by an order of
magnitude.

Cases that fail on the current code are ``xfail(strict=True)`` and name the path
and the change that fixes it, so the PR that fixes a path flips exactly its own
case and must drop the marker.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, bus, eager, emit, lease
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import projection_catalogue, slot_prime
from kiro_crew.crew_log import store as crew_log_store
from kiro_crew.crew_log import store_cuts, store_paths
from kiro_crew.crew_log.session_tree import SessionTree
from kiro_crew.testing.wait import await_until

GATEWAY = "gateway"
BOARD = "dashboard:guard-board"

#: Entries in each unit of the measured slot. A whole-log walk of one unit alone is
#: this many entries, so every bound below sits far under it.
ENTRIES_PER_UNIT = 400
#: Units the measured slot ran under (a reset per unit).
UNITS_IN_SLOT = 6
#: Units of OTHER slots on disk -- what a whole-root listing or a fold of every open
#: slot would pay for.
OTHER_UNITS = 24

#: The constant part of every entry bound: the few records a resume re-reads to land
#: on its boundary, plus slack. Far below one unit's ENTRIES_PER_UNIT.
ENTRY_SLACK = 40
#: Unit opens one read may pay that do not scale with the slot: the newest unit, its
#: neighbour, a header probe.
OPEN_SLACK = 3

_WORK_FILLER_TYPE = "turn/started"
#: A slot fold whose units come from the header index alone (no per-read log walk to
#: resolve them), so a resume case measures the fold and nothing else.
RESUME_FOLD = "ledger"


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    """Own data home, crew log on, no warm cell, subscriber or worker left over."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _reset()
    yield
    _reset()
    _release_write_leases()


def _reset() -> None:
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()
    bus.reset_for_tests()
    emit.reset_caches()


def _release_write_leases() -> None:
    """Release any write lease a handle this test created still holds.

    A ``CrewLog`` handle releases its write lease when it is dropped, through the
    ``weakref.finalize`` the handle arms. A reader that caches handles process-wide
    (``read.dispatch_view``'s session-tree keeps one per unit it scans) holds some of
    this test's handles past the test's own frame, so the refcount drop -- and the
    release that rides it -- waits for a later generational collection. On POSIX that
    wait is harmless, but the next test on this xdist worker (``test_crew_log_types``)
    asserts in its teardown that no lease outlived a test, so a lease released only at
    the next GC reads there as leaked. Releasing them here, where they are created,
    keeps the boundary clean without a ``gc.collect()`` and without touching the other
    file's check. The lease release is idempotent, so the handle's own finalizer
    running later is a no-op.
    """
    for key in list(lease._held):
        while key in lease._held:
            lease.release(key)


# --------------------------------------------------------------------------- #
# The meter
# --------------------------------------------------------------------------- #


class ScanMeter:
    """Counts what the store reads, per operation.

    ``entries`` -- entries decoded off any unit's segments (``_iter_entries_located``,
    the one framer every ``iter_from`` walk goes through).
    ``hashed`` -- raw records a prefix digest walked (``raw_prefix_digest``), the
    decode-free walk a savepoint or warm-cell check pays.
    ``opens`` -- ``CrewLog.open`` calls, one per unit a read touched.
    ``root_listings`` -- ``iterdir`` of the crew-log session ROOT.
    ``copies`` -- fold-state copies (``_Fold.copied``).
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entries = 0
        self.hashed = 0
        self.opens = 0
        self.root_listings = 0
        self.copies = 0
        self._root: "pathlib.Path | None" = None
        meter = self

        real_located = crew_log_store._iter_entries_located

        def located(*args: Any, **kwargs: Any) -> Iterator[Any]:
            for pair in real_located(*args, **kwargs):
                meter.entries += 1
                yield pair

        # Through the facade: it mirrors the patch onto every owner that binds the
        # name, and undoes it on all of them together.
        monkeypatch.setattr(crew_log_store, "_iter_entries_located", located)

        real_digest = CrewLog.raw_prefix_digest

        def digest(self: CrewLog, records: int) -> "tuple[str, int]":
            sha, hashed = real_digest(self, records)
            meter.hashed += int(hashed)
            return sha, hashed

        monkeypatch.setattr(CrewLog, "raw_prefix_digest", digest)

        real_open = CrewLog.open.__func__  # type: ignore[attr-defined]

        def opened(cls: type, *args: Any, **kwargs: Any) -> CrewLog:
            meter.opens += 1
            return real_open(cls, *args, **kwargs)

        monkeypatch.setattr(CrewLog, "open", classmethod(opened))

        real_iterdir = pathlib.Path.iterdir

        def iterdir(self: pathlib.Path) -> Iterator[pathlib.Path]:
            if meter._is_root(self):
                meter.root_listings += 1
            return real_iterdir(self)

        monkeypatch.setattr(pathlib.Path, "iterdir", iterdir)

        # The other two ways to list a directory, so a path that switches primitive
        # is still counted.
        real_scandir = os.scandir
        real_listdir = os.listdir

        def names_root(path: Any) -> bool:
            # A directory fd carries no path to compare, so it is not counted.
            return not isinstance(path, int) and meter._is_root(pathlib.Path(os.fsdecode(path)))

        def scandir(path: Any = ".") -> Any:
            if names_root(path):
                meter.root_listings += 1
            return real_scandir(path)

        def listdir(path: Any = ".") -> "list[Any]":
            if names_root(path):
                meter.root_listings += 1
            return real_listdir(path)

        monkeypatch.setattr(os, "scandir", scandir)
        monkeypatch.setattr(os, "listdir", listdir)

        real_copied = projection_catalogue._Fold.copied

        def copied(self: Any, state: Any) -> Any:
            meter.copies += 1
            return real_copied(self, state)

        monkeypatch.setattr(projection_catalogue._Fold, "copied", copied)

    def _is_root(self, path: pathlib.Path) -> bool:
        if self._root is None:
            try:
                self._root = store_paths.crew_log_root(lg.KIND_SESSION)
            except Exception:
                return False
        return path == self._root

    def reset(self) -> None:
        self.entries = self.hashed = self.opens = self.root_listings = self.copies = 0

    @contextmanager
    def measure(self) -> Iterator["ScanMeter"]:
        self.reset()
        yield self

    def __repr__(self) -> str:
        return (
            f"ScanMeter(entries={self.entries}, hashed={self.hashed}, opens={self.opens}, "
            f"root_listings={self.root_listings}, copies={self.copies})"
        )


@pytest.fixture
def meter(monkeypatch) -> ScanMeter:
    return ScanMeter(monkeypatch)


# --------------------------------------------------------------------------- #
# Synthetic store
# --------------------------------------------------------------------------- #


def _unit(unit_id: str, slot: str, *, filler: int, work_items: int = 0) -> CrewLog:
    """One session unit on *slot*: an opener, *work_items* work creates, then filler."""
    handle = CrewLog.create(lg.KIND_SESSION, unit_id, owner="guard", agent="kirocrew", slot=slot)
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": slot,
            "model": "m",
            "cwd": "/w",
            "owner": "guard",
            "resumed": False,
            "class": {"memory": "persistent"},
        },
        src=GATEWAY,
    )
    items = [
        {
            "type": "work/recorded",
            "data": _work_create(slot, f"{unit_id}-it-{index}"),
        }
        for index in range(work_items)
    ]
    items += [
        {"type": _WORK_FILLER_TYPE, "data": {"turn": index + 1, "actor": "user", "depth": 0}}
        for index in range(filler)
    ]
    for start in range(0, len(items), 200):
        handle.append_many(items[start : start + 200], src=GATEWAY)
    return handle


def _work_create(board: str, item_id: str) -> dict:
    return {
        "slot": board,
        "actor": "conductor",
        "by": board,
        "action": "create",
        "item_id": item_id,
        "title": f"item {item_id}",
    }


def _board_slot() -> "list[CrewLog]":
    """The measured slot: UNITS_IN_SLOT units, each ENTRIES_PER_UNIT entries."""
    return [
        _unit(f"acp-guard-{index:02d}", BOARD, filler=ENTRIES_PER_UNIT - 4, work_items=3)
        for index in range(UNITS_IN_SLOT)
    ]


def _other_slot(index: int) -> str:
    """An open dashboard slot's name as its unit header records it (the bare name)."""
    return f"other-{index:02d}"


def _other_slots() -> None:
    """OTHER_UNITS units, one per open slot, none of them the slot being measured."""
    for index in range(OTHER_UNITS):
        _unit(f"acp-other-{index:02d}", _other_slot(index), filler=ENTRIES_PER_UNIT)


def _append_work(handle: CrewLog, count: int, tag: str) -> int:
    seq = 0
    for index in range(count):
        entry = handle.append("work/recorded", _work_create(BOARD, f"{tag}-{index}"), src=GATEWAY)
        seq = int(entry.seq)
    return seq


def _subscribe_baseline(fold: str = "work") -> "list[Any]":
    seen: list[Any] = []
    bus.subscribe(
        bus.FOLD_ADVANCED, seen.append, scope=bus.SCOPE_SLOT, key=BOARD, fold=fold, baseline=True
    )
    return seen


# --------------------------------------------------------------------------- #
# bus.subscribe(baseline=True)
# --------------------------------------------------------------------------- #


def test_a_warm_header_indexed_baseline_reads_only_the_entries_appended_since(meter):
    """Holds on main: the warm cell continues over the tail of the newest unit only.

    The control for the cases below. A slot fold whose units come from the header index
    pays O(appended) on a warm subscribe, so a later change that made the warm path
    re-read the slot fails here first.
    """
    units = _board_slot()
    _other_slots()
    _subscribe_baseline(RESUME_FOLD)  # queues the one cold prime this process pays
    # The prime runs in the background; let it finish so the read below is the warm one.
    assert slot_prime.drain(30), "the cold prime must finish"
    _append_work(units[-1], 5, "warm")
    with meter.measure():
        _subscribe_baseline(RESUME_FOLD)
    assert meter.entries <= ENTRY_SLACK + 5, meter
    assert meter.hashed <= ENTRY_SLACK, meter
    assert meter.opens <= UNITS_IN_SLOT + OPEN_SLACK, meter


def test_a_warm_baseline_reads_only_the_entries_appended_since(meter):
    """A second subscriber on a warm board reads what was appended, not the slot."""
    units = _board_slot()
    _other_slots()
    _subscribe_baseline()  # the one cold fold this process pays
    assert slot_prime.drain(30), "the cold prime must finish"
    _append_work(units[-1], 5, "warm")
    with meter.measure():
        seen = _subscribe_baseline()
    assert seen, "the baseline must still be delivered"
    assert meter.entries <= ENTRY_SLACK + 5, meter
    assert meter.hashed <= ENTRY_SLACK, meter


def test_a_warm_workstreams_baseline_reads_only_the_entries_appended_since(meter):
    units = _board_slot()
    _subscribe_baseline("workstreams")
    assert slot_prime.drain(30), "the cold prime must finish"
    _append_work(units[-1], 5, "ws")
    with meter.measure():
        _subscribe_baseline("workstreams")
    assert meter.entries <= ENTRY_SLACK + 5, meter
    assert meter.hashed <= ENTRY_SLACK, meter


#: Entries appended between two warm session reads: past the savepoint threshold
#: (checkpoint.MIN_ADVANCE_ENTRIES), so the second read owes a savepoint write.
SAVEPOINT_GAP = 300


def test_a_warm_session_read_that_earns_a_savepoint_reads_only_the_gap(meter):
    from kiro_crew.crew_log import checkpoint

    assert SAVEPOINT_GAP >= checkpoint.MIN_ADVANCE_ENTRIES
    handle = _unit("acp-session-gap", BOARD, filler=ENTRIES_PER_UNIT)
    crew_log.read_projection(handle.id, "status")  # cold fold, then the bundle is warm
    items = [
        {"type": _WORK_FILLER_TYPE, "data": {"turn": index + 1, "actor": "user", "depth": 0}}
        for index in range(SAVEPOINT_GAP)
    ]
    for start in range(0, len(items), 200):
        handle.append_many(items[start : start + 200], src=GATEWAY)
    with meter.measure():
        crew_log.read_projection(handle.id, "status")
    assert meter.entries + meter.hashed <= SAVEPOINT_GAP + ENTRY_SLACK, meter


@pytest.mark.xfail(
    strict=True,
    reason="full root listing on every slot read: store_units.session_units_by_slot lists "
    "every unit dir before its cache check; fixed by #24274 "
    "(crew_log/store_units.py)",
)
def test_a_warm_baseline_does_not_list_the_crew_log_root(meter):
    units = _board_slot()
    _other_slots()
    _subscribe_baseline()
    assert slot_prime.drain(30), "the cold prime must finish"
    _append_work(units[-1], 1, "list")
    with meter.measure():
        _subscribe_baseline()
    assert meter.root_listings == 0, meter


def test_a_cold_prime_copies_the_fold_state_a_fixed_number_of_times(meter):
    """The one allowed cold fold folds onto one state; it does not copy it per entry.

    Driven through the cold fold call itself, not through a subscribe, so the count
    still measures the prime once a subscribe stops folding.
    """
    from kiro_crew.crew_log import projection_slot_memo

    units = [_unit(f"acp-copy-{index}", BOARD, filler=10, work_items=150).id for index in range(2)]
    with meter.measure():
        projection_slot_memo.fold_slot_warm_revised("work", units, slot=BOARD)
    assert meter.copies <= 4, meter


def _savepoint_bytes(handle: CrewLog, fold: str) -> bytes:
    """The slot savepoint *fold* keeps at *handle*'s unit, or ``b""`` when there is none."""
    from kiro_crew.crew_log import checkpoint

    path = checkpoint.slot_checkpoint_path(lg.KIND_SESSION, handle.id, fold)
    return path.read_bytes() if path.exists() else b""


def _earn_a_savepoint_write(handle: CrewLog, read: Any, fold: str) -> None:
    """Append enough for the next warm read to rewrite the savepoint, and check it did.

    A savepoint moves only once a fold is ``MIN_ADVANCE_ENTRIES`` past the saved seq, so
    a smaller gap leaves it where the cold read put it and a restart bound would measure
    the write interval instead of the resume.
    """
    from kiro_crew.crew_log import checkpoint

    before = _savepoint_bytes(handle, fold)
    _append_work(handle, checkpoint.MIN_ADVANCE_ENTRIES + 4, "earn")
    read()  # warm, moves the savepoint on
    assert (
        _savepoint_bytes(handle, fold) != before
    ), "setup did not earn a savepoint write (append >= checkpoint.MIN_ADVANCE_ENTRIES)"


def test_a_baseline_after_restart_resumes_from_the_savepoint(meter):
    """A new process (no warm cell) with a savepoint on disk reads the gap, not the log."""
    # The ledger fold: its units come from the header index, so what this measures is
    # the fold and its savepoint, not the work fold's unit resolution.
    units = _board_slot()
    crew_log.read_slot_projection(BOARD, RESUME_FOLD)  # cold fold, writes a savepoint
    _earn_a_savepoint_write(
        units[-1], lambda: crew_log.read_slot_projection(BOARD, RESUME_FOLD), RESUME_FOLD
    )
    # The restart: every in-memory cell is gone, only the files remain.
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()
    _append_work(units[-1], 3, "gap")
    with meter.measure():
        crew_log.read_slot_projection(BOARD, RESUME_FOLD)
    assert meter.entries + meter.hashed <= ENTRY_SLACK + 3, meter


def test_a_worktree_board_read_with_no_new_entry_reads_no_old_entries(meter):
    """A second ``worktree`` read over a log that has not grown reads nothing old."""
    _board_slot()
    crew_log.read_tree_projection(BOARD, "worktree")  # the one cold fold this process pays
    with meter.measure():
        crew_log.read_tree_projection(BOARD, "worktree")
    assert meter.entries == 0, meter
    assert meter.hashed == 0, meter


def test_a_worktree_read_after_restart_resumes_from_the_savepoint(meter):
    """After a restart the ``worktree`` fold reads the gap past its savepoint, not the log."""
    units = _board_slot()
    crew_log.read_tree_projection(BOARD, "worktree")  # cold fold, writes a savepoint
    _earn_a_savepoint_write(
        units[-1], lambda: crew_log.read_tree_projection(BOARD, "worktree"), "worktree"
    )
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()
    _append_work(units[-1], 3, "gap")
    with meter.measure():
        value = crew_log.read_tree_projection(BOARD, "worktree").value
    assert meter.entries + meter.hashed <= ENTRY_SLACK + 3, meter
    assert [row["board"] for row in value["boards"]] == [BOARD]


# --------------------------------------------------------------------------- #
# Eager advance (the crew-log-eager-fold thread)
# --------------------------------------------------------------------------- #


def _eager_advance(handle: CrewLog, tag: str) -> None:
    seq = _append_work(handle, 1, tag)
    eager.note_commit(handle.id, "work/recorded", seq, board=BOARD)
    assert eager.drain(timeout=20.0)


@pytest.mark.xfail(
    strict=True,
    reason="eager advance of the work and workstreams folds re-walks every conductor unit "
    "to resolve its units (projection_work._work_units, "
    "projection_workstreams._workstreams_binds_in) and opens every unit of the slot "
    "(_fold_slot_warm_locked -> _unit_marks); fixed by the work fold redesign PR "
    "(work fold reads only the slot's own units) and the warm-branch change in "
    "crew_log/projection_slot_memo.py",
)
def test_an_eager_advance_on_an_unchanged_unit_set_opens_a_fixed_number_of_units(meter):
    units = _board_slot()
    _eager_advance(units[-1], "warm-a")  # cold fold, then the cell is warm
    with meter.measure():
        _eager_advance(units[-1], "warm-b")
    assert meter.opens <= OPEN_SLACK, meter
    assert meter.entries <= ENTRY_SLACK + 1, meter


def _drop_cut_counters() -> None:
    """Make every unit look like a log created before the cut counter existed."""
    root = store_paths.crew_log_root(lg.KIND_SESSION)
    dropped = 0
    for unit_dir in root.iterdir():
        counter = store_cuts._cuts_path(unit_dir)
        if counter.exists():
            counter.unlink()
            dropped += 1
    assert dropped, "the store seeds a counter at create; there must be one to drop"


@pytest.mark.xfail(
    strict=True,
    reason="eager warm advance on a log with no cut counter re-hashes the whole newest "
    "unit (_digest_to_carry, _folded_prefix_holds, _vouched) and store_handle._resume_at "
    "refuses the resume point; the work fold's unit resolution also walks every unit "
    "(projection_work._work_units); fixed by the PRs that change "
    "crew_log/projection_slot_memo.py, crew_log/store_handle.py and "
    "crew_log/projection_work.py",
)
def test_an_eager_advance_on_a_log_without_a_cut_counter_reads_only_the_new_entry(meter):
    units = _board_slot()
    _drop_cut_counters()
    _eager_advance(units[-1], "nocut-a")
    with meter.measure():
        _eager_advance(units[-1], "nocut-b")
    assert meter.entries + meter.hashed <= ENTRY_SLACK + 1, meter


# --------------------------------------------------------------------------- #
# Session tree (dispatch_view / slot_chain)
# --------------------------------------------------------------------------- #


def test_a_dispatch_view_for_one_unit_reads_a_fixed_number_of_units(meter, monkeypatch):
    from kiro_crew.crew_log.read import dispatch_view

    units = _board_slot()
    _other_slots()
    dispatch_view((units[-1].id,))  # warm the head cache
    reads = {"n": 0}
    real_read = SessionTree._read

    def counted(self: SessionTree, directory: pathlib.Path) -> Any:
        reads["n"] += 1
        return real_read(self, directory)

    monkeypatch.setattr(SessionTree, "_read", counted)
    with meter.measure():
        dispatch_view((units[-1].id,))
    assert reads["n"] <= UNITS_IN_SLOT + OPEN_SLACK, (reads, meter)
    assert meter.root_listings == 0, meter


# --------------------------------------------------------------------------- #
# WS connect and reconnect (Subagents panel replay)
# --------------------------------------------------------------------------- #


async def _ws_connect_and_replay(app: Any, client: Any, done: asyncio.Event) -> None:
    from kiro_crew.dashboard import token_auth

    token = token_auth.generate_token("local-app")
    tab = await client.ws_connect("/api/ws", params={"token": token})
    try:
        await tab.send_str(json.dumps({"type": "subscribe_subagents"}))
        # The handler reads one message at a time and runs the replay inline, so the
        # unsubscribe below is handled only once the replay has finished. Its effect
        # on the server is the signal, whether or not the replay folded anything.
        await tab.send_str(json.dumps({"type": "unsubscribe_subagents"}))
        await await_until(done, what="the subagent replay finishes")
    finally:
        await tab.close()


@pytest.fixture
def ws_harness(tmp_path, monkeypatch):
    """A real dashboard app with the real ``/api/ws`` handler and many open slots."""
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app, _make_state

    from kiro_crew.dashboard import token_auth, ws
    from kiro_crew.dashboard.state import SlotOrigin

    monkeypatch.setattr(
        "kiro_crew.dashboard.ws.load_declared_events_for_connect",
        lambda _app: (True, frozenset()),
    )
    state = _make_state(tmp_path)
    for index in range(OTHER_UNITS):
        state.get_or_create_slot(_other_slot(index), origin=SlotOrigin.USER)
    loop = asyncio.new_event_loop()
    done = asyncio.Event()
    real_unsubscribe = state.unsubscribe_subagents

    def unsubscribe_and_signal(socket: Any) -> None:
        real_unsubscribe(socket)
        done.set()  # on the loop: the handler calls this from its receive loop

    monkeypatch.setattr(state, "unsubscribe_subagents", unsubscribe_and_signal)

    async def run(body: Any) -> None:
        app = _make_app(state)
        app.middlewares.insert(0, token_auth.token_auth_middleware())
        app["allowed_origins"] = set()
        app.router.add_get("/api/ws", ws.api_ws)
        async with TestClient(TestServer(app)) as client:
            origin = str(client.make_url("/")).rstrip("/")
            app["allowed_origins"].add(origin)
            client.session.headers["Origin"] = origin
            await body(app, client, done)

    yield lambda body: loop.run_until_complete(run(body))
    loop.close()


def test_a_ws_connect_does_not_fold_every_open_slot(meter, ws_harness):
    _other_slots()

    async def body(app: Any, client: Any, done: asyncio.Event) -> None:
        with meter.measure():
            await _ws_connect_and_replay(app, client, done)

    ws_harness(body)
    assert meter.entries + meter.hashed <= ENTRY_SLACK, meter


def test_a_ws_reconnect_with_nothing_new_reads_a_fixed_number_of_entries(meter, ws_harness):
    _other_slots()

    async def body(app: Any, client: Any, done: asyncio.Event) -> None:
        await _ws_connect_and_replay(app, client, done)  # the first connect
        done.clear()
        with meter.measure():
            await _ws_connect_and_replay(app, client, done)

    ws_harness(body)
    assert meter.entries + meter.hashed <= ENTRY_SLACK, meter


# --------------------------------------------------------------------------- #
# Transcript history page (history_projection_reads._row_index)
# --------------------------------------------------------------------------- #


def test_a_first_history_page_in_a_new_process_reads_the_page_not_the_file(tmp_path, monkeypatch):
    from kiro_crew import history, history_projection
    from kiro_crew.history import ConversationLog
    from kiro_crew.history_projection_contract import _TRANSCRIPT_PAGE_INDEX_STRIDE

    monkeypatch.setattr(history, "_SESSION_MAX_BYTES", 10**12)
    key = "dashboard:guard-history"
    rows = 3_000
    first = ConversationLog(base_dir=tmp_path)
    path = first._path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"_type": "metadata", "created_at": "2026-10-01T00:00:00+00:00"})]
    lines.extend(
        json.dumps({"role": "tool", "content": f"row-{row} " + "x" * 200, "ts": f"t{row:08d}"})
        for row in range(rows)
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    first.read_messages_chained_page(key, limit=50)  # the previous process

    framed = {"n": 0}
    real = history_projection.strict_raw_records_with_offsets

    def metered(*args: Any, **kwargs: Any) -> Iterator[Any]:
        for record in real(*args, **kwargs):
            framed["n"] += 1
            yield record

    monkeypatch.setattr(history_projection, "strict_raw_records_with_offsets", metered)
    restarted = ConversationLog(base_dir=tmp_path)  # a new process: no in-memory index
    page = restarted.read_messages_chained_page(key, limit=50)
    assert len(page.messages) == 50
    assert framed["n"] <= 50 + _TRANSCRIPT_PAGE_INDEX_STRIDE + ENTRY_SLACK, framed


# --------------------------------------------------------------------------- #
# GET /api/memory/graph (unbounded get_all_semantic)
# --------------------------------------------------------------------------- #


def test_the_memory_graph_reads_a_bounded_number_of_semantic_rows(tmp_path):
    from kiro_crew.dashboard.handlers.memory_graph import (
        GRAPH_SEMANTIC_LIMIT,
        _build_memory_graph,
    )
    from kiro_crew.vector_memory import VectorMemoryStore

    store = VectorMemoryStore(db_path=tmp_path / "guard-vm.db")
    store.init()
    try:
        for index in range(400):
            store.set_semantic(
                f"project.guard_{index:04d}", {"v": index}, 0.9, "guard", defer_embedding=True
            )
        assert len(store.get_all_semantic(limit=500)) == 400, "the table must be seeded"
        fetched = {"rows": 0}
        real_fetch = store._fetch_all_locked

        def metered(*args: Any, **kwargs: Any) -> Any:
            rows = real_fetch(*args, **kwargs)
            fetched["rows"] += len(rows)
            return rows

        store._fetch_all_locked = metered  # type: ignore[method-assign]
        mem = SimpleNamespace(
            read_preferences=lambda: "",
            read_projects=lambda: "",
            read_recent_history=lambda *a, **k: "",
            vector_store=store,
        )
        _build_memory_graph(mem, [])
        # The cap plus the one row read past it to tell a partial graph; a constant.
        assert fetched["rows"] <= GRAPH_SEMANTIC_LIMIT + 1, fetched
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
