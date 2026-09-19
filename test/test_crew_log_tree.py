"""The session tree -- one test per rule the fold and its scanner promise.

The fold is pure and the scanner is a cache over immutable bytes, so the
properties here are the ones a second implementation would be free to break:
input order does not change the tree, an orphan and a cycle degrade to root
rather than to an error, a record without a parent never retracts one, and a
scan re-reads only what changed on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import store as crew_store
from kiro_crew.crew_log import tree
from kiro_crew.crew_log.tree import OpenedRecord, SessionTree, fold_tree, parent_payload
from kiro_crew.session_ledger import _store_name

GATEWAY = "gateway"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _rec(sid: str, slot: str, created: int = 1, parent: str | None = None):
    return OpenedRecord(sid=sid, slot=slot, created_at=created, parent_slot=parent)


def _log(sid: str, slot: str) -> CrewLog:
    return CrewLog.create(lg.KIND_SESSION, sid, owner="raymond", agent="kirocrew", slot=slot)


def _opened(
    handle: CrewLog,
    slot: str,
    *,
    parent: dict[str, str] | None = None,
    resumed: bool = False,
) -> None:
    data = {
        "agent": "kirocrew",
        "slot": slot,
        "model": "opus",
        "cwd": "/w",
        "owner": "raymond",
        "resumed": resumed,
    }
    if parent is not None:
        data["parent"] = parent
    handle.append("session/opened", data, src=GATEWAY)


# ── fold_tree: the pure rules ──────────────────────────────────────────────


def test_a_session_nobody_created_is_a_root_with_no_parent() -> None:
    nodes = fold_tree([_rec("s1", "chat-1")])
    assert nodes["chat-1"].parent_slot is None
    assert nodes["chat-1"].parent_slot is None
    assert nodes["chat-1"].cycle is False


def test_a_child_nests_under_its_creator() -> None:
    nodes = fold_tree(
        [
            _rec("p-sid", "chat-1", created=1),
            _rec("c-sid", "chat-2", created=2, parent="chat-1"),
        ]
    )
    child = nodes["chat-2"]
    assert child.parent_slot == "chat-1"
    assert child.parent_slot == "chat-1"
    assert nodes["chat-1"].parent_slot is None


def test_an_orphan_keeps_its_citation_but_is_a_root() -> None:
    nodes = fold_tree([_rec("c-sid", "chat-2", parent="chat-gone")])
    node = nodes["chat-2"]
    assert node.cycle is False
    assert node.cycle is False
    assert node.parent_slot == "chat-gone"
    assert "chat-gone" not in nodes


def test_the_parent_chain_reaches_the_root_through_every_link() -> None:
    # The fold carries each slot's parent and nothing more; the one consumer
    # that nests (the Sessions table) walks the chain itself.
    nodes = fold_tree(
        [
            _rec("a", "A", created=1),
            _rec("b", "B", created=2, parent="A"),
            _rec("c", "C", created=3, parent="B"),
            _rec("d", "D", created=4, parent="C"),
        ]
    )
    assert [nodes[s].parent_slot for s in "ABCD"] == [None, "A", "B", "C"]
    assert not any(nodes[s].cycle for s in "ABCD")


def test_a_cycle_marks_every_member_and_a_hanger_on_keeps_its_edge() -> None:
    nodes = fold_tree(
        [
            _rec("a", "A", created=1, parent="B"),
            _rec("b", "B", created=2, parent="A"),
            _rec("c", "C", created=3, parent="A"),
        ]
    )
    assert nodes["A"].cycle is True
    assert nodes["B"].cycle is True
    # C is not ON the cycle; it hangs off A and keeps that edge.
    assert nodes["C"].cycle is False and nodes["C"].parent_slot == "A"
    # The citations survive: the fold refuses to FOLLOW them, not to report them.
    assert nodes["A"].parent_slot == "B"


def test_a_session_citing_itself_is_a_cycle_of_one() -> None:
    nodes = fold_tree([_rec("a", "A", parent="A")])
    assert nodes["A"].cycle is True
    assert nodes["A"].parent_slot == "A"


def test_any_opened_with_a_parent_wins_and_one_without_never_retracts_it() -> None:
    # The Design-lane shape: create (parent) -> re-attach in-process (parent)
    # -> gateway restart -> re-attach on a new log (no parent, the mint witness
    # is gone). Folds to the parent the first log recorded.
    created = _rec("c1", "chat-2", created=10, parent="chat-1")
    after_restart = _rec("c2", "chat-2", created=20)
    creator = _rec("p", "chat-1", created=1)
    nodes = fold_tree([creator, created, after_restart])
    assert nodes["chat-2"].parent_slot == "chat-1"
    assert nodes["chat-2"].parent_slot == "chat-1"


def test_a_parent_on_a_later_log_is_still_taken() -> None:
    # ANY record of the slot carrying a parent, not only the oldest.
    nodes = fold_tree(
        [
            _rec("p", "chat-1", created=1),
            _rec("c1", "chat-2", created=10),
            _rec("c2", "chat-2", created=20, parent="chat-1"),
        ]
    )
    assert nodes["chat-2"].parent_slot == "chat-1"


def test_input_order_does_not_change_the_tree() -> None:
    records = [
        _rec("p", "chat-1", created=1),
        _rec("c1", "chat-2", created=10, parent="chat-1"),
        _rec("c2", "chat-2", created=20, parent="chat-7"),
        _rec("g", "chat-3", created=30, parent="chat-2"),
    ]
    forward = fold_tree(records)
    backward = fold_tree(reversed(records))
    assert forward == backward
    # Two records of one slot disagree (forged or damaged input): the OLDEST
    # log's word stands, whichever order the scan handed them over in.
    assert forward["chat-2"].parent_slot == "chat-1"


def test_a_record_without_a_slot_has_no_place_in_the_tree() -> None:
    nodes = fold_tree([_rec("s", "", parent="chat-1"), _rec("p", "chat-1")])
    assert set(nodes) == {"chat-1"}


# ── the scanner over real logs ─────────────────────────────────────────────


def test_scan_folds_a_created_child_under_its_creator() -> None:
    creator = _log("p-sid", "chat-1")
    _opened(creator, "chat-1")
    child = _log("c-sid", "chat-2")
    _opened(child, "chat-2", parent={"slot": "chat-1", "sid": "p-sid"})

    nodes = SessionTree().snapshot()
    assert nodes["chat-2"].parent_slot == "chat-1"
    assert nodes["chat-2"].parent_slot == "chat-1"
    assert nodes["chat-1"].parent_slot is None


def test_no_crew_log_root_is_an_empty_tree_not_an_error() -> None:
    scanner = SessionTree()
    assert scanner.records() == []
    assert scanner.snapshot() == {}


def test_a_header_only_log_is_read_again_once_its_first_entry_lands() -> None:
    # The emitter creates the file and appends session/opened in two writes; a
    # scan between them must not cache "no parent" for the life of the log.
    scanner = SessionTree()
    child = _log("c-sid", "chat-2")
    assert scanner.records() == []
    _opened(child, "chat-2", parent={"slot": "chat-1", "sid": "p-sid"})
    [record] = scanner.records()
    assert record.parent_slot == "chat-1"


def test_a_second_scan_costs_no_read_for_an_unchanged_log(monkeypatch) -> None:
    _opened(_log("p-sid", "chat-1"), "chat-1")
    _opened(_log("c-sid", "chat-2"), "chat-2", parent={"slot": "chat-1", "sid": "p-sid"})
    scanner = SessionTree()
    first = scanner.records()
    assert len(first) == 2

    def _no_reads(*_args, **_kwargs):
        raise AssertionError("an unchanged log was re-read")

    monkeypatch.setattr(tree, "read_head", _no_reads)
    assert sorted(scanner.records(), key=lambda r: r.sid) == sorted(first, key=lambda r: r.sid)


def test_a_refused_unit_is_cached_and_costs_one_read(monkeypatch) -> None:
    # A planted bad head must not cost a read per scan for the life of the log.
    _opened(_log("p-sid", "chat-1"), "chat-1")
    root = crew_store.crew_log_root(lg.KIND_SESSION)
    forged = root / _store_name("other-sid")
    forged.mkdir()
    (forged / "log.jsonl").write_bytes((root / _store_name("p-sid") / "log.jsonl").read_bytes())
    scanner = SessionTree()
    assert [r.sid for r in scanner.records()] == ["p-sid"]
    reads: list[Path] = []
    real = tree.read_head

    def _counting(path: Path):
        reads.append(path)
        return real(path)

    monkeypatch.setattr(tree, "read_head", _counting)
    assert [r.sid for r in scanner.records()] == ["p-sid"]
    assert reads == []


def test_a_read_that_fails_is_not_cached_and_the_next_scan_reads_the_log(monkeypatch) -> None:
    # An OSError out of read_head is a read that saw no bytes, not a verdict on
    # them. Caching it as "no record" would drop the session's creator from the
    # tree until the segment rolled or the process restarted, for a moment's
    # I/O fault after a stat that succeeded.
    _opened(_log("p-sid", "chat-1"), "chat-1")
    child = _log("c-sid", "chat-2")
    _opened(child, "chat-2", parent={"slot": "chat-1", "sid": "p-sid"})
    real = tree.read_head
    failures = {"left": 1}

    def _flaky(path: Path):
        if path == child.path and failures["left"]:
            failures["left"] -= 1
            raise OSError(5, "input/output error")
        return real(path)

    monkeypatch.setattr(tree, "read_head", _flaky)
    scanner = SessionTree()
    # The failed read leaves that unit out of THIS pass only, and caches nothing
    # for it; the parent's log, read fine, is cached as usual.
    assert [r.sid for r in scanner.records()] == ["p-sid"]
    assert _store_name("c-sid") not in scanner._heads
    assert _store_name("p-sid") in scanner._heads
    # Next pass: the same bytes on the same inode are read and the child is back.
    assert sorted(r.sid for r in scanner.records()) == ["c-sid", "p-sid"]
    [record] = [r for r in scanner.records() if r.sid == "c-sid"]
    assert record.parent_slot == "chat-1"


def test_a_retained_string_past_its_bound_refuses_the_whole_record() -> None:
    # Every string the scanner keeps is bounded by the constant its writer
    # shares; an oversize one is not something the gateway wrote, so the unit
    # contributes nothing rather than a truncated key that matches nothing.
    from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN, MAX_SHORT_STRING

    directory = Path("/units") / _store_name("c-sid")
    header = {"type": "session", "id": "c-sid", "slot": "chat-2", "createdAt": 5}
    opened = {
        "seq": 1,
        "type": "session/opened",
        "time": 1,
        "src": GATEWAY,
        "data": {"slot": "chat-2", "parent": {"slot": "chat-1", "sid": "p-sid"}},
    }
    from kiro_crew.crew_log.schema import Entry

    entry = Entry.from_dict(opened)
    assert entry is not None
    good = tree.opened_record(directory, header, entry)
    assert good is not None and good.parent_slot == "chat-1"

    long_sid = "s" * (MAX_ACP_SESSION_ID_LEN + 1)
    long_slot = "k" * (MAX_SHORT_STRING + 1)
    over_slot = Entry.from_dict({**opened, "data": {"parent": {"slot": long_slot}}})
    assert tree.opened_record(directory, header, over_slot) is None
    assert tree.opened_record(directory, {**header, "slot": long_slot}, entry) is None
    # ``parent.sid`` is not read (the tree is keyed by slot), so it is neither
    # retained nor bounded: an oversize one changes nothing about the record.
    over_sid = Entry.from_dict({**opened, "data": {"parent": {"slot": "chat-1", "sid": long_sid}}})
    with_over_sid = tree.opened_record(directory, header, over_sid)
    assert with_over_sid is not None and with_over_sid.parent_slot == "chat-1"
    # A header id past the bound can never fold to a real directory anyway; the
    # bound refuses it before the fold is even computed.
    assert (
        tree.opened_record(
            Path("/units") / _store_name(long_sid), {**header, "id": long_sid}, entry
        )
        is None
    )
    # Within the bounds, a slot-less header is a legal record with no place in the tree.
    slotless = tree.opened_record(directory, {**header, "slot": None}, entry)
    assert slotless is not None and slotless.slot == ""


def test_a_shorter_file_under_the_same_name_and_inode_is_re_read() -> None:
    # A filesystem hands a freed inode number to the next file it creates, so a
    # segment removed and recreated under the same name can answer the same
    # (st_dev, st_ino) -- CI's ext4 does, this host's filesystem does not, so
    # the recycled inode is simulated by rewriting the segment IN PLACE. A
    # segment is append-only and never shrinks, so the cache also keeps the size
    # it read and treats a SHORTER file as a different one.
    handle = _log("c-sid", "chat-2")
    path = crew_store.crew_log_path(lg.KIND_SESSION, "c-sid")
    header_line = path.read_bytes().split(b"\n", 1)[0]
    _opened(handle, "chat-2", parent={"slot": "chat-1", "sid": "p-sid"})
    scanner = SessionTree()
    [before] = scanner.records()
    assert before.parent_slot == "chat-1"
    identity = (path.stat().st_dev, path.stat().st_ino)
    read_size = path.stat().st_size
    opened_without_parent = {
        "seq": 1,
        "type": "session/opened",
        "time": 1,
        "src": GATEWAY,
        "data": {
            "agent": "kirocrew",
            "slot": "chat-2",
            "model": "opus",
            "cwd": "/w",
            "owner": "raymond",
        },
    }
    path.write_bytes(header_line + b"\n" + json.dumps(opened_without_parent).encode() + b"\n")
    assert (path.stat().st_dev, path.stat().st_ino) == identity
    assert path.stat().st_size < read_size
    [after] = scanner.records()
    assert after.parent_slot is None


def test_a_torn_first_entry_is_not_cached_and_is_read_once_it_lands() -> None:
    # A scan that lands inside the session/opened append sees an unterminated
    # line 2. Caching that as a refusal would hide the parent for as long as
    # the file exists, because the bytes complete on the SAME inode and the
    # stat-based identity never changes. So it is not cached, and the next
    # scan reads the landed entry.
    handle = _log("c-sid", "chat-2")
    path = crew_store.crew_log_path(lg.KIND_SESSION, "c-sid")
    before = path.read_bytes()
    _opened(handle, "chat-2", parent={"slot": "chat-1", "sid": "p-sid"})
    full = path.read_bytes()
    entry_line = full[len(before) :]
    cut = len(entry_line) // 2
    path.write_bytes(before + entry_line[:cut])
    scanner = SessionTree()
    assert scanner.records() == []
    assert scanner._heads == {}
    with open(path, "ab") as tail:
        tail.write(entry_line[cut:])
    [record] = scanner.records()
    assert record.parent_slot == "chat-1"


def test_a_removed_unit_leaves_the_records_and_the_cache() -> None:
    _opened(_log("p-sid", "chat-1"), "chat-1")
    scanner = SessionTree()
    assert len(scanner.records()) == 1
    directory = crew_store.crew_log_dir(lg.KIND_SESSION, "p-sid")
    for child in directory.iterdir():
        child.unlink()
    directory.rmdir()
    assert scanner.records() == []
    assert scanner._heads == {}


def test_units_past_the_cap_are_neither_read_nor_cached_and_the_overflow_is_reported(
    monkeypatch,
) -> None:
    # The cap is the ONE bound on the scanner's work and retention (a-bound
    # rule): past it a unit is not read (no head cache entry can appear for it)
    # and the scan says THAT something was left, so a snapshot can say so.
    # Which units are admitted follows the directory's own order; with no live
    # rows named, that is not something the page depends on.
    for sid, slot in (("a-sid", "chat-1"), ("b-sid", "chat-2"), ("c-sid", "chat-3")):
        _opened(_log(sid, slot), slot)
    monkeypatch.setattr(tree, "TREE_UNIT_CAP", 2)
    reads: list[Path] = []
    real = tree.read_head

    def _counting(path: Path):
        reads.append(path)
        return real(path)

    monkeypatch.setattr(tree, "read_head", _counting)
    scanner = SessionTree()
    assert scanner.over_cap is False
    admitted = sorted(r.sid for r in scanner.records())
    assert len(admitted) == 2 and set(admitted) < {"a-sid", "b-sid", "c-sid"}
    assert scanner.over_cap is True
    assert len(reads) == 2
    assert set(scanner._heads) == {_store_name(sid) for sid in admitted}
    # A slot whose log went unread is absent from the tree, not present as a
    # root: the payload's over-cap flag is what says the tree is not the store.
    (left,) = {"chat-1", "chat-2", "chat-3"} - set(scanner.snapshot())
    assert left in {"chat-1", "chat-2", "chat-3"}


def test_a_unit_that_falls_past_the_cap_is_evicted_so_the_cache_never_exceeds_it(
    monkeypatch,
) -> None:
    for sid, slot in (("b-sid", "chat-2"), ("c-sid", "chat-3")):
        _opened(_log(sid, slot), slot)
    monkeypatch.setattr(tree, "TREE_UNIT_CAP", 2)
    scanner = SessionTree()
    assert sorted(r.sid for r in scanner.records()) == ["b-sid", "c-sid"]
    assert scanner.over_cap is False
    # A third unit pushes the population past the cap: whichever unit the
    # listing now leaves out, the cache drops its head rather than keeping
    # cap + 1 entries.
    _opened(_log("a-sid", "chat-1"), "chat-1")
    admitted = {r.sid for r in scanner.records()}
    assert len(admitted) == 2
    assert scanner.over_cap is True
    assert len(scanner._heads) == 2
    assert set(scanner._heads) == {_store_name(sid) for sid in admitted}


def test_live_units_are_admitted_first_so_past_the_cap_only_closed_logs_go_unread(
    monkeypatch,
) -> None:
    # The rows on screen are what the tree is folded for. Naming their units
    # admits them ahead of the store's listing, so a live session the listing
    # would leave out is still read, and what the cap leaves out is a closed
    # session's log that no row nests on. An id with no log is skipped.
    for sid, slot in (("a-sid", "chat-1"), ("b-sid", "chat-2"), ("c-sid", "chat-3")):
        _opened(_log(sid, slot), slot)
    _opened(_log("c-child", "chat-4"), "chat-4", parent={"slot": "chat-3", "sid": "c-sid"})
    monkeypatch.setattr(tree, "TREE_UNIT_CAP", 2)
    scanner = SessionTree()
    assert sorted(r.sid for r in scanner.records(["c-sid", "c-child", "ghost-sid"])) == [
        "c-child",
        "c-sid",
    ]
    assert scanner.over_cap is True
    assert set(scanner._heads) == {_store_name("c-sid"), _store_name("c-child")}
    # The fold has both live rows: the child cites its live creator.
    assert scanner.snapshot(["c-sid", "c-child"])["chat-4"].parent_slot == "chat-3"
    # Without a preferred set the same store admits two of the four in its own
    # order: the caller's naming is what protects the rows on screen.
    assert len(list(scanner.records())) == 2
    assert scanner.over_cap is True


def test_the_preferred_set_is_itself_bounded_by_the_cap(monkeypatch) -> None:
    for sid, slot in (("a-sid", "chat-1"), ("b-sid", "chat-2"), ("c-sid", "chat-3")):
        _opened(_log(sid, slot), slot)
    monkeypatch.setattr(tree, "TREE_UNIT_CAP", 2)
    scanner = SessionTree()
    # Three live ids, cap two: the first two named are admitted, the third is
    # left with the rest, and the cache never exceeds the cap.
    assert sorted(r.sid for r in scanner.records(["c-sid", "b-sid", "a-sid"])) == ["b-sid", "c-sid"]
    assert scanner.over_cap is True
    assert len(scanner._heads) == 2


def test_every_loop_of_a_scan_is_cut_by_the_cap_whatever_the_input(monkeypatch) -> None:
    # The invariant the module states: probes, listing and cache are each
    # bounded by TREE_UNIT_CAP regardless of what the caller or the store hand
    # it. Measured, not read off the code: absent ids cost a probe each but no
    # more than the cap of them; a store three times the cap is examined for at
    # most cap + 1 candidates (the excluded live units aside); the cache holds
    # at most the cap.
    cap = 3
    monkeypatch.setattr(tree, "TREE_UNIT_CAP", cap)
    for n in range(cap * 3):
        _opened(_log(f"unit-{n}", f"chat-{n}"), f"chat-{n}")
    probes: list[str] = []
    real_dir_for = tree.unit_dir_for

    def _probing(kind: str, unit_id: str):
        probes.append(unit_id)
        return real_dir_for(kind, unit_id)

    monkeypatch.setattr(tree, "unit_dir_for", _probing)
    examined: list[Path] = []
    real_is_link = crew_store.is_link

    def _watching(path: Path) -> bool:
        examined.append(path)
        return real_is_link(path)

    monkeypatch.setattr(crew_store, "is_link", _watching)
    scanner = SessionTree()
    # Ten times the cap in absent ids: exactly cap probes, nothing admitted for them.
    ghosts = [f"ghost-{n}" for n in range(cap * 10)]
    assert len(list(scanner.records(ghosts))) == cap
    assert len(probes) == cap
    # unit_dir_for checks each probed name for a link too; the LISTING's share
    # is what is bounded here: cap candidates and one look-ahead.
    probed = {_store_name(g) for g in ghosts}
    assert len([e for e in examined if e.name not in probed]) == cap + 1
    assert len(scanner._heads) <= cap
    assert scanner.over_cap is True
    # Live ids that exist fill the cap by themselves: the listing then only
    # looks one candidate ahead to learn there is more.
    probes.clear()
    examined.clear()
    live = [f"unit-{n}" for n in range(cap)]
    assert sorted(r.sid for r in scanner.records(live)) == sorted(live)
    assert len(probes) == cap
    probed = {_store_name(unit) for unit in live}
    # The look-ahead only: excluded live units are skipped unexamined.
    assert len([e for e in examined if e.name not in probed]) == 1
    assert len(scanner._heads) == cap
    assert scanner.over_cap is True


def test_unit_dir_for_answers_for_a_directory_and_not_for_an_absent_one() -> None:
    _opened(_log("p-sid", "chat-1"), "chat-1")
    root = crew_store.crew_log_root(lg.KIND_SESSION)
    assert crew_store.unit_dir_for(lg.KIND_SESSION, "p-sid") == root / _store_name("p-sid")
    assert crew_store.unit_dir_for(lg.KIND_SESSION, "ghost-sid") is None


@requires_symlinks
def test_unit_dir_for_refuses_a_linked_entry() -> None:
    # A linked entry answers for a directory that may sit outside the tree, the
    # same refusal unit_header_slot and the sweep make.
    _opened(_log("p-sid", "chat-1"), "chat-1")
    root = crew_store.crew_log_root(lg.KIND_SESSION)
    (root / _store_name("linked-sid")).symlink_to(
        root / _store_name("p-sid"), target_is_directory=True
    )
    assert crew_store.unit_dir_for(lg.KIND_SESSION, "linked-sid") is None


def test_unit_dirs_neither_lists_nor_counts_an_excluded_name() -> None:
    for sid, slot in (("a-sid", "chat-1"), ("b-sid", "chat-2"), ("c-sid", "chat-3")):
        _opened(_log(sid, slot), slot)
    everything = {_store_name(sid) for sid in ("a-sid", "b-sid", "c-sid")}
    listed, more = crew_store.unit_dirs(lg.KIND_SESSION, limit=1, exclude={_store_name("a-sid")})
    assert len(listed) == 1 and listed[0].name in everything - {_store_name("a-sid")}
    assert more is True
    # Excluding all but one leaves exactly that one and nothing more.
    listed, more = crew_store.unit_dirs(
        lg.KIND_SESSION, limit=5, exclude=everything - {_store_name("b-sid")}
    )
    assert [d.name for d in listed] == [_store_name("b-sid")]
    assert more is False
    # A limit of zero lists nothing and still says whether anything exists: the
    # caller filled its cap with named units and wants to know if the store
    # holds more.
    assert crew_store.unit_dirs(lg.KIND_SESSION, limit=0) == ([], True)
    assert crew_store.unit_dirs(lg.KIND_SESSION, limit=0, exclude=everything) == ([], False)


# ── store helpers ──────────────────────────────────────────────────────────


def test_read_head_returns_the_header_and_first_entry() -> None:
    handle = _log("c-sid", "chat-2")
    _opened(handle, "chat-2", parent={"slot": "chat-1"})
    header, entry, announced = crew_store.read_head(handle.path)
    assert header is not None and header["id"] == "c-sid" and header["slot"] == "chat-2"
    assert entry is not None and entry.type == "session/opened"
    assert entry.data["parent"] == {"slot": "chat-1"}
    assert announced is True


def test_read_head_tells_a_missing_second_line_from_a_damaged_one() -> None:
    handle = _log("c-sid", "chat-2")
    # Header only: the announce has not landed. Line 2 does not exist.
    header, entry, announced = crew_store.read_head(handle.path)
    assert header is not None and entry is None and announced is False
    header_line = handle.path.read_bytes().split(b"\n", 1)[0]
    damaged_entry = handle.path.parent / "damaged-entry.jsonl"
    damaged_entry.write_bytes(header_line + b"\n{not json\n")
    header, entry, announced = crew_store.read_head(damaged_entry)
    assert header is not None and entry is None and announced is True
    # An UNTERMINATED line 2 that does not parse is a read that landed inside
    # the append, not damage: reported as "nothing behind the header yet" so a
    # caller caches no refusal for bytes that complete a moment later.
    torn_entry = handle.path.parent / "torn-entry.jsonl"
    torn_entry.write_bytes(header_line + b'\n{"seq": 1, "type": "session/op')
    header, entry, announced = crew_store.read_head(torn_entry)
    assert header is not None and entry is None and announced is False
    damaged_header = handle.path.parent / "damaged.jsonl"
    damaged_header.write_bytes(b"\xff\xfe not json\n{}\n")
    assert crew_store.read_head(damaged_header) == (None, None, False)
    # A file that cannot be opened is not a verdict on any bytes: the OSError
    # comes out as is, so a caller can tell "read failed" from "damaged".
    with pytest.raises(OSError):
        crew_store.read_head(handle.path.parent / "absent.jsonl")


def test_a_damaged_first_entry_is_refused_once_not_read_every_scan(monkeypatch) -> None:
    handle = _log("c-sid", "chat-2")
    handle.path.write_bytes(handle.path.read_bytes().split(b"\n", 1)[0] + b"\n{not json\n")
    scanner = SessionTree()
    assert scanner.records() == []
    reads: list[Path] = []
    real = tree.read_head

    def _counting(path: Path):
        reads.append(path)
        return real(path)

    monkeypatch.setattr(tree, "read_head", _counting)
    assert scanner.records() == []
    assert reads == []


def test_oldest_segment_prefers_the_head_and_falls_back_to_the_lowest_numbered(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "unit"
    directory.mkdir()
    assert crew_store.oldest_segment(directory) is None
    (directory / "log.40.jsonl").write_bytes(b"")
    (directory / "log.9.jsonl").write_bytes(b"")
    (directory / "log.jsonl.bak").write_bytes(b"")
    assert crew_store.oldest_segment(directory) == directory / "log.9.jsonl"
    (directory / "log.jsonl").write_bytes(b"")
    assert crew_store.oldest_segment(directory) == directory / "log.jsonl"


def test_unit_dirs_lists_only_real_directories_under_a_real_root(tmp_path: Path) -> None:
    assert crew_store.unit_dirs(lg.KIND_SESSION, limit=8) == ([], False)
    _opened(_log("p-sid", "chat-1"), "chat-1")
    root = crew_store.crew_log_root(lg.KIND_SESSION)
    (root / "stray.txt").write_bytes(b"")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    kept, more = crew_store.unit_dirs(lg.KIND_SESSION, limit=8)
    assert ([p.name for p in kept], more) == ([_store_name("p-sid")], False)


def test_unit_dirs_stops_one_candidate_past_the_limit(monkeypatch) -> None:
    # Bounded in WORK: the listing examines limit + 1 candidates and returns,
    # so a huge root costs the caller the limit and not the population. Which
    # units come first is the directory's business; nothing on the page depends
    # on it, since live units are admitted by name ahead of this listing.
    for sid, slot in (
        ("c-sid", "chat-3"),
        ("a-sid", "chat-1"),
        ("d-sid", "chat-4"),
        ("b-sid", "chat-2"),
    ):
        _opened(_log(sid, slot), slot)
    examined: list[Path] = []
    real = crew_store.is_link

    def _watching(path: Path) -> bool:
        examined.append(path)
        return real(path)

    monkeypatch.setattr(crew_store, "is_link", _watching)
    kept, more = crew_store.unit_dirs(lg.KIND_SESSION, limit=2)
    assert len(kept) == 2 and more is True
    assert len(examined) == 3
    # A limit no population reaches keeps everything, with nothing left over.
    examined.clear()
    kept, more = crew_store.unit_dirs(lg.KIND_SESSION, limit=100)
    assert sorted(p.name for p in kept) == sorted(
        _store_name(s) for s in ("a-sid", "b-sid", "c-sid", "d-sid")
    )
    assert more is False
    assert len(examined) == 4


# ── the wire payload ───────────────────────────────────────────────────────


def test_parent_payload_nests_on_the_live_creator_and_keeps_the_citation() -> None:
    nodes = fold_tree(
        [
            _rec("p", "chat-1", created=1),
            _rec("c", "chat-2", created=2, parent="chat-1"),
            _rec("o", "chat-3", created=3, parent="chat-gone"),
        ]
    )
    live = {"chat-1": "dashboard:chat-1", "dashboard:chat-1": "dashboard:chat-1"}
    assert parent_payload(nodes["chat-1"], live, "dashboard:chat-1") is None
    assert parent_payload(nodes["chat-2"], live, "dashboard:chat-2") == {
        "slot": "chat-1",
        "key": "dashboard:chat-1",
    }
    # The creator is not running: no edge to nest on, the citation stays.
    assert parent_payload(nodes["chat-3"], live, "dashboard:chat-3") == {
        "slot": "chat-gone",
        "key": None,
    }
    assert parent_payload(None, live, "dashboard:chat-9") is None


def test_parent_payload_never_nests_a_cycle_or_a_row_under_itself() -> None:
    nodes = fold_tree(
        [
            _rec("a", "A", created=1, parent="B"),
            _rec("b", "B", created=2, parent="A"),
            _rec("s", "S", created=3, parent="S"),
        ]
    )
    live = {"A": "dashboard:A", "B": "dashboard:B", "S": "dashboard:S"}
    assert parent_payload(nodes["A"], live, "dashboard:A")["key"] is None  # type: ignore[index]
    assert parent_payload(nodes["S"], live, "dashboard:S")["key"] is None  # type: ignore[index]
