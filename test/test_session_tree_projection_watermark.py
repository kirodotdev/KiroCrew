"""The session tree's boot replay reads each unit's log from where the last boot stopped.

The checkpoint keeps, per unit, a watermark: the newest segment it read and the byte
offset it read up to. A boot reads only the bytes after it and folds the decisions and
spawns found there, so the cost of a boot after a small append is the append, whatever
the size of the log behind it. A unit whose segments changed in any other way -- a new
segment, a shorter file, a replaced file -- is read in full, and only that unit.

Bytes are counted by wrapping ``open`` for files inside the store root, keyed by unit.
"""

from __future__ import annotations

import builtins
import os
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from crew_log_drain import assert_drained

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log.session_tree_projection import SessionTreeProjection
from kiro_crew.crew_log.store import crew_log_root
from kiro_crew.session_ledger import _store_name

GATEWAY = "gateway"
#: Filler entries per unit for the four log sizes, 1x / 4x / 16x / 64x.
SIZES = (250, 1000, 4000, 16000)
#: Records a delta boot decodes from a unit that took one append, whatever its size:
#: the resume point's record checked, read again by the walk, and the appended one.
DELTA_DECODED = 3


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    _floor_monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_NoPool", (), {"submit": staticmethod(lambda *a, **k: None)}),
    )
    stp.reset_for_tests()
    yield
    stp.reset_for_tests()
    emit.reset_caches()


class _CountingFile:
    """A binary file handle that adds every byte it hands out to *bucket*."""

    def __init__(self, handle: Any, bucket: Counter[str], key: str) -> None:
        self._handle = handle
        self._bucket = bucket
        self._key = key

    def _count(self, blob: bytes) -> bytes:
        self._bucket[self._key] += len(blob)
        return blob

    def read(self, *args: Any) -> bytes:
        return self._count(self._handle.read(*args))

    def read1(self, *args: Any) -> bytes:
        return self._count(self._handle.read1(*args))

    def readline(self, *args: Any) -> bytes:
        return self._count(self._handle.readline(*args))

    def readinto(self, buffer: Any) -> int:
        n = self._handle.readinto(buffer)
        self._bucket[self._key] += n or 0
        return n

    def __iter__(self) -> Iterator[bytes]:
        for line in self._handle:
            yield self._count(line)

    def __enter__(self) -> "_CountingFile":
        return self

    def __exit__(self, *exc: Any) -> None:
        self._handle.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)


@pytest.fixture
def bytes_read(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """Bytes read from each unit's directory, keyed by the unit's directory name."""
    bucket: Counter[str] = Counter()
    real_open = builtins.open

    def counting_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(file, mode, *args, **kwargs)
        if "b" not in mode or "r" not in mode or not isinstance(file, (str, os.PathLike)):
            return handle
        path = Path(file)
        try:
            root = crew_log_root(lg.KIND_SESSION)
        except Exception:
            return handle
        if path.parent.parent != root:
            return handle
        return _CountingFile(handle, bucket, path.parent.name)

    monkeypatch.setattr(builtins, "open", counting_open)
    return bucket


@pytest.fixture
def decoded(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """Records the store's framer decodes from each unit, keyed by directory name."""
    from kiro_crew.crew_log import store as crew_store

    bucket: Counter[str] = Counter()
    real = crew_store._iter_entries_located

    def counting(path: Path, *args: Any, **kwargs: Any) -> Any:
        for found in real(path, *args, **kwargs):
            bucket[path.parent.name] += 1
            yield found

    monkeypatch.setattr(crew_store, "_iter_entries_located", counting)
    return bucket


def _unit(sid: str, slot: str, filler: int) -> CrewLog:
    handle = CrewLog.create(lg.KIND_SESSION, sid, owner="raymond", agent="kirocrew", slot=slot)
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": slot,
            "model": "opus",
            "cwd": "/w",
            "owner": "raymond",
            "resumed": False,
        },
        src=GATEWAY,
    )
    # The filler is never asserted on, so it lands in one raw write; the records the
    # tests assert on still go through ``CrewLog.append``. Reopened after, so the
    # handle's tail and seq account for the bytes written under it.
    path = _dir(sid) / "log.jsonl"
    first = len(path.read_bytes().splitlines())
    with open(path, "ab") as raw:
        raw.write(
            b"".join(
                b'{"type":"turn/started","seq":%d,"time":1,"src":"gateway",'
                b'"data":{"turn":%d,"actor":"user","depth":0}}\n' % (first + i, i + 1)
                for i in range(filler)
            )
        )
    del handle
    emit.reset_caches()
    return CrewLog.open(lg.KIND_SESSION, sid)


def _small_append(handle: CrewLog) -> None:
    handle.append("turn/started", {"turn": 0, "actor": "user", "depth": 0}, src=GATEWAY)


def _boot() -> SessionTreeProjection:
    """One gateway start: seed from the checkpoint on disk, then write it back."""
    projection = SessionTreeProjection()
    projection.ensure_seeded()
    projection.flush_checkpoint()
    projection.cancel_pending_checkpoint()
    return projection


def _dir(sid: str) -> Path:
    return crew_log_root(lg.KIND_SESSION) / _store_name(sid)


def _log_size(sid: str) -> int:
    return sum(p.stat().st_size for p in _dir(sid).glob("log*.jsonl"))


def _parent(projection: SessionTreeProjection, slot: str) -> str | None:
    node = projection.nodes().get(slot)
    return None if node is None else node.parent_slot


def test_a_boot_after_a_small_append_reads_only_the_append_at_every_log_size(
    decoded: Counter[str],
) -> None:
    handles = {f"s-{n}": _unit(f"s-{n}", f"slot-{n}", n) for n in SIZES}
    _boot()  # first start on this build: cold scan
    _boot()  # second start: replay that sets each unit's watermark
    for handle in handles.values():
        _small_append(handle)
    assert_drained()
    decoded.clear()

    _boot()

    read = {sid: decoded[_store_name(sid)] for sid in handles}
    assert set(read.values()) == {DELTA_DECODED}, read


def test_a_boot_with_nothing_appended_opens_no_unit_log(bytes_read: Counter[str]) -> None:
    _unit("s-a", "slot-a", SIZES[0])
    _boot()
    _boot()
    assert_drained()
    bytes_read.clear()

    _boot()

    assert bytes_read[_store_name("s-a")] == 0, dict(bytes_read)


@pytest.mark.parametrize(
    ("entry_type", "data", "expected"),
    [
        ("session/adopted", {"parent": {"slot": "slot-b"}}, "slot-b"),
        ("session/released", {}, None),
    ],
)
def test_a_decision_written_while_the_gateway_was_down_is_applied_from_the_delta(
    decoded: Counter[str], entry_type: str, data: dict[str, Any], expected: str | None
) -> None:
    adopted = _unit("s-a", "slot-a", SIZES[1])
    _unit("s-b", "slot-b", 1)
    adopted.append("session/adopted", {"parent": {"slot": "slot-c"}}, src=GATEWAY)
    _unit("s-c", "slot-c", 1)
    _boot()
    assert _parent(_boot(), "slot-a") == "slot-c"

    # Written by a process the next gateway did not see: no projection applied it.
    adopted.append(entry_type, data, src=GATEWAY)
    assert_drained()
    decoded.clear()

    booted = _boot()

    assert _parent(booted, "slot-a") == expected
    assert decoded[_store_name("s-a")] == DELTA_DECODED, dict(decoded)


def _rotate(sid: str) -> None:
    """Start a newer segment, the shape a rotating writer leaves."""
    directory = _dir(sid)
    lines = (directory / "log.jsonl").read_bytes().splitlines(keepends=True)
    seq = len(lines)
    (directory / f"log.{seq}.jsonl").write_bytes(
        lines[0] + b'{"type":"turn/started","seq":%d,"time":1,"src":"gateway",'
        b'"data":{"turn":1,"actor":"user","depth":0}}\n' % seq
    )


def _shrink(sid: str) -> None:
    """Cut the log back to an earlier line boundary."""
    path = _dir(sid) / "log.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[: len(lines) // 2]))


def _replace_inode(sid: str) -> None:
    """Same bytes, new file: what a copy-and-rename restore leaves."""
    path = _dir(sid) / "log.jsonl"
    copy = path.with_name("log.copy")
    copy.write_bytes(path.read_bytes())
    os.replace(copy, path)


@pytest.mark.parametrize("change", [_rotate, _shrink, _replace_inode])
def test_a_unit_whose_segments_changed_is_read_in_full_and_only_that_unit(
    decoded: Counter[str], change: Any
) -> None:
    _unit("s-changed", "slot-x", SIZES[1])
    steady = _unit("s-steady", "slot-y", SIZES[1])
    _boot()
    _boot()
    change("s-changed")
    _small_append(steady)
    assert_drained()
    decoded.clear()

    _boot()

    assert decoded[_store_name("s-changed")] >= SIZES[1] // 2, dict(decoded)
    assert decoded[_store_name("s-steady")] == DELTA_DECODED, dict(decoded)


def test_a_fallback_read_finds_a_decision_in_a_replaced_file(bytes_read: Counter[str]) -> None:
    _unit("s-a", "slot-a", SIZES[0])
    _unit("s-b", "slot-b", 1)
    _boot()
    _boot()
    path = _dir("s-a") / "log.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    seq = len(lines)
    adopt = (
        b'{"type":"session/adopted","seq":%d,"time":5,"src":"gateway",'
        b'"data":{"parent":{"slot":"slot-b"}}}\n' % seq
    )
    # Inserted BEFORE the old end, so a delta read from the old offset cannot see it.
    rebuilt = path.with_name("log.copy")
    rebuilt.write_bytes(b"".join(lines[:2]) + adopt + b"".join(lines[2:]))
    os.replace(rebuilt, path)

    assert _parent(_boot(), "slot-a") == "slot-b"


def _children(projection: SessionTreeProjection) -> set[str]:
    return {spawn.slot for spawn in projection._spawned.values()}


def test_a_rolled_back_spawn_replaced_at_the_same_length_is_re_read() -> None:
    """A failed append is rolled back as a CUT, and the next append can bring the file
    back to the very length and inode the mark holds. The cut counter moved, so the
    unit is read in full: the rolled-back spawn goes and its replacement is found."""
    handle = _unit("s-a", "slot-a", SIZES[0])
    _boot()
    handle.append("session/spawned", {"child": {"slot": "W1"}}, src=GATEWAY)
    assert_drained()
    assert _children(_boot()) == {"W1"}

    path = _dir("s-a") / "log.jsonl"
    before = path.stat()
    lines = path.read_bytes().splitlines(keepends=True)
    replacement = lines[-1].replace(b'"W1"', b'"W2"')
    cuts = _dir("s-a") / ".cuts"
    settled = int(cuts.read_text())
    # What a rollback leaves: the line removed in place, the counter raised past it
    # (once before the cut and once after), then the next append of the same size.
    with open(path, "r+b") as raw:
        raw.truncate(before.st_size - len(lines[-1]))
        raw.seek(0, os.SEEK_END)
        raw.write(replacement)
    cuts.write_text(str(settled + 2))
    after = path.stat()
    assert (after.st_ino, after.st_size) == (before.st_ino, before.st_size)

    assert _children(_boot()) == {"W2"}


@pytest.mark.parametrize("child", ['"bad"', '{"slot":[]}'], ids=["not-a-map", "slot-not-a-str"])
def test_a_malformed_spawn_after_the_mark_does_not_abort_the_seed(child: str) -> None:
    _unit("s-a", "slot-a", SIZES[0])
    _boot()
    _boot()
    path = _dir("s-a") / "log.jsonl"
    seq = len(path.read_bytes().splitlines())
    with open(path, "ab") as raw:
        raw.write(
            b'{"type":"session/spawned","seq":%d,"time":5,"src":"gateway","data":{"child":%s}}\n'
            % (seq, child.encode())
        )

    booted = _boot()

    assert "slot-a" in booted.nodes()
    assert booted.reading().incomplete is False


def _adopted_unit() -> tuple[CrewLog, Path]:
    """slot-a adopted under slot-c, booted twice so the adoption is checkpointed and the
    unit holds a mark."""
    handle = _unit("s-a", "slot-a", SIZES[0])
    _unit("s-c", "slot-c", 1)
    handle.append("session/adopted", {"parent": {"slot": "slot-c"}}, src=GATEWAY)
    assert_drained()
    _boot()
    assert _parent(_boot(), "slot-a") == "slot-c"
    return handle, _dir("s-a") / "log.jsonl"


def test_a_complete_release_missing_its_newline_is_applied_and_read_again_once() -> None:
    """An interrupted append can leave a whole final record with no terminator. It is
    folded, as a full read folds it, and no mark is kept past a record whose end cannot
    be proved; once the newline lands the unit is marked and the tree does not move."""
    _handle, path = _adopted_unit()
    seq = len(path.read_bytes().splitlines())
    release = b'{"type":"session/released","seq":%d,"time":9,"src":"gateway","data":{}}' % seq
    with open(path, "ab") as raw:
        raw.write(release)

    first = _boot()
    assert _parent(first, "slot-a") is None
    held = dict(first._edges)
    assert "s-a" not in first._marks

    with open(path, "ab") as raw:
        raw.write(b"\n")
    second = _boot()
    assert _parent(second, "slot-a") is None
    assert second._edges == held
    assert second._marks["s-a"][2] == path.stat().st_size - len(release) - 1


def test_an_unparseable_partial_tail_is_left_for_the_next_read() -> None:
    _handle, path = _adopted_unit()
    size = path.stat().st_size
    with open(path, "ab") as raw:
        raw.write(b'{"type":"session/released","se')

    booted = _boot()

    assert _parent(booted, "slot-a") == "slot-c"
    assert booted.reading().incomplete is False
    assert path.stat().st_size == size


# --------------------------------------------------------------------------- #
# Equivalence: a watermark boot serves the tree a full read of the same bytes does.
#
# The reference is the cold scan, ``SessionTree().reading(with_edges=True)``, which this
# PR does not touch: it is main's full read of every unit (``find_last_tree_edge``,
# ``find_spawned_entries``). Each case boots twice so the unit holds a mark, changes the
# store while no gateway runs, boots once more, and compares that tree with the scan's.
# --------------------------------------------------------------------------- #


def _tree(nodes: Any) -> dict[str, Any]:
    return {slot: (node.parent_slot, getattr(node, "cycle", None)) for slot, node in nodes.items()}


def _assert_same_as_full_read(booted: SessionTreeProjection) -> None:
    from kiro_crew.crew_log.session_tree import SessionTree

    scan = SessionTree().reading(with_edges=True)
    assert _tree(booted.nodes()) == _tree(scan.nodes)
    assert booted.reading().incomplete == scan.incomplete


def _creator(sid: str, slot: str, filler: int, children: list[str]) -> CrewLog:
    handle = _unit(sid, slot, filler)
    for child in children:
        handle.append("session/spawned", {"child": {"slot": child}}, src=GATEWAY)
    return handle


def _warm() -> tuple[CrewLog, CrewLog]:
    """Two units with a mark each: a creator with two children and an adopted one."""
    creator = _creator("s-a", "slot-a", SIZES[0], ["W1", "W2"])
    adopted = _unit("s-b", "slot-b", SIZES[0])
    adopted.append("session/adopted", {"parent": {"slot": "slot-a"}}, src=GATEWAY)
    assert_drained()
    _boot()
    _boot()
    return creator, adopted


@pytest.mark.parametrize("filler", SIZES)
def test_the_tree_after_an_append_equals_a_full_read_at_every_size(filler: int) -> None:
    creator = _creator("s-a", "slot-a", filler, ["W1"])
    _unit("s-b", "slot-b", filler)
    assert_drained()
    _boot()
    _boot()
    creator.append("session/spawned", {"child": {"slot": "W2"}}, src=GATEWAY)
    _small_append(creator)
    assert_drained()

    _assert_same_as_full_read(_boot())


def _new_unit() -> None:
    _unit("s-new", "slot-n", 10).append(
        "session/adopted", {"parent": {"slot": "slot-a"}}, src=GATEWAY
    )


def _removed_unit() -> None:
    import shutil

    emit.reset_caches()
    shutil.rmtree(_dir("s-b"))


def _adopt_while_down() -> None:
    CrewLog.open(lg.KIND_SESSION, "s-a").append(
        "session/adopted", {"parent": {"slot": "slot-b"}}, src=GATEWAY
    )


def _release_while_down() -> None:
    CrewLog.open(lg.KIND_SESSION, "s-b").append("session/released", {}, src=GATEWAY)


def _raw_line(sid: str, body: bytes, newline: bool = True) -> None:
    path = _dir(sid) / "log.jsonl"
    seq = len(path.read_bytes().splitlines())
    with open(path, "ab") as raw:
        raw.write(body % seq + (b"\n" if newline else b""))


def _unterminated_release() -> None:
    _raw_line(
        "s-b", b'{"type":"session/released","seq":%d,"time":9,"src":"gateway","data":{}}', False
    )


def _malformed_child() -> None:
    _raw_line(
        "s-a",
        b'{"type":"session/spawned","seq":%d,"time":9,"src":"gateway","data":{"child":"bad"}}',
    )


def _rollback() -> None:
    path = _dir("s-a") / "log.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    spawned = next(i for i in range(len(lines) - 1, -1, -1) if b'"W2"' in lines[i])
    cuts = _dir("s-a") / ".cuts"
    with open(path, "r+b") as raw:
        raw.truncate(sum(len(line) for line in lines[:spawned]))
        raw.seek(0, os.SEEK_END)
        raw.write(lines[spawned].replace(b'"W2"', b'"W3"') + b"".join(lines[spawned + 1 :]))
    cuts.write_text(str(int(cuts.read_text()) + 2))


@pytest.mark.parametrize(
    "change",
    [
        _new_unit,
        _removed_unit,
        lambda: _rotate("s-a"),
        lambda: _shrink("s-a"),
        lambda: _replace_inode("s-b"),
        _adopt_while_down,
        _release_while_down,
        _unterminated_release,
        _malformed_child,
        _rollback,
    ],
    ids=[
        "new-unit",
        "removed-unit",
        "rotation",
        "shrink",
        "inode-change",
        "adopt-while-down",
        "release-while-down",
        "unterminated-last-record",
        "malformed-child",
        "cuts-rollback",
    ],
)
def test_the_tree_after_a_change_while_down_equals_a_full_read(change: Any) -> None:
    _warm()
    change()
    assert_drained()

    _assert_same_as_full_read(_boot())


def test_a_respawn_past_the_per_unit_cap_equals_a_full_read(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full read keeps the newest *cap* spawned ENTRIES, so a child spawned twice
    after the mark can push an older child out of the full read's answer."""
    from kiro_crew.crew_log import session_tree as stp_session_tree

    _floor_monkeypatch.setattr(stp_session_tree, "SPAWNED_PER_UNIT_CAP", 2)
    creator = _creator("s-a", "slot-a", 10, ["W1", "W2"])
    assert_drained()
    _boot()
    _boot()
    for _ in range(2):
        creator.append("session/spawned", {"child": {"slot": "W2"}}, src=GATEWAY)
    assert_drained()

    _assert_same_as_full_read(_boot())


def test_a_unit_with_no_cut_counter_is_read_once_then_skipped_until_it_changes(
    bytes_read: Counter[str], decoded: Counter[str]
) -> None:
    """A log written before ``.cuts`` existed cannot prove an append, so its mark only
    vouches for the exact bytes: unchanged, it costs one stat; changed, a full read of
    that unit alone, and the tree still matches a full read."""
    legacy = _unit("s-old", "slot-o", SIZES[1])
    steady = _unit("s-new", "slot-n", SIZES[1])
    assert_drained()
    (_dir("s-old") / ".cuts").unlink()
    _boot()
    _boot()
    bytes_read.clear()

    _boot()
    assert bytes_read[_store_name("s-old")] == 0, dict(bytes_read)

    legacy.append("session/spawned", {"child": {"slot": "W1"}}, src=GATEWAY)
    _small_append(steady)
    assert_drained()
    decoded.clear()

    booted = _boot()
    assert decoded[_store_name("s-old")] >= SIZES[1], dict(decoded)
    assert decoded[_store_name("s-new")] == DELTA_DECODED, dict(decoded)
    _assert_same_as_full_read(booted)


def test_a_creator_past_the_unit_cap_keeps_its_spawns_across_boots(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past ``TREE_UNIT_CAP`` the oldest creator's record is evicted but its spawns stay
    in the checkpoint. The next boot reads that creator's head again, and its spawns are
    kept for it, as main's replay keeps them."""
    from kiro_crew.crew_log import session_tree as stp_session_tree

    _floor_monkeypatch.setattr(stp_session_tree, "TREE_UNIT_CAP", 2)
    _floor_monkeypatch.setattr(stp, "TREE_UNIT_CAP", 2)
    _creator("s-a", "slot-a", 1, ["W1"])
    _unit("s-b", "slot-b", 1)
    assert_drained()
    # Within the cap first, so the cold scan holds the creator whatever the listing order.
    assert ("W1", "s-a") in _boot()._spawned
    _unit("s-c", "slot-c", 1)
    assert_drained()
    # Past it, the oldest record (the creator's) is evicted; its spawn must stay.
    for _ in range(3):
        assert ("W1", "s-a") in _boot()._spawned


def test_a_damaged_decision_row_reads_its_unit_in_full() -> None:
    """A checkpoint row that does not decode is dropped. The mark of the unit it came
    from does not cover what is held for that unit, so the unit is read in full."""
    import json

    _handle, _path = _adopted_unit()
    checkpoint = crew_log_root(lg.KIND_SESSION) / "projections" / stp.CHECKPOINT_NAME
    payload = json.loads(checkpoint.read_text())
    for row in payload["edges"]:
        row["seq"] = "damaged"
    checkpoint.write_text(json.dumps(payload))

    assert _parent(_boot(), "slot-a") == "slot-c"


def test_a_refused_decision_after_the_mark_drops_the_held_one_as_a_full_read_does() -> None:
    """The log's newest decision is a ``session/adopted`` whose parent names no slot. A
    full read takes it as the newest decision, refuses it, and holds none; so must the
    delta."""
    _warm()
    _raw_line(
        "s-b",
        b'{"type":"session/adopted","seq":%d,"time":9,"src":"gateway","data":{"parent":{}}}',
    )
    assert_drained()

    _assert_same_as_full_read(_boot())


def test_an_oversized_spawn_after_the_mark_is_a_fault_as_in_a_full_read() -> None:
    """A newline-terminated record past ``MAX_ENTRY_BYTES`` cannot be read whole. The full
    read raises on it; the boot keeps the held spawns, reports the unit incomplete and
    keeps the mark from before the damage."""
    from kiro_crew.crew_log.schema import MAX_ENTRY_BYTES

    _warm()
    damaged_seq = len((_dir("s-a") / "log.jsonl").read_bytes().splitlines())
    pad = b"x" * (MAX_ENTRY_BYTES + 16)
    _raw_line(
        "s-a",
        b'{"type":"session/spawned","seq":%d,"time":9,"src":"gateway",'
        b'"data":{"child":{"slot":"W9"},"pad":"' + pad + b'"}}',
    )
    _small_append(CrewLog.open(lg.KIND_SESSION, "s-a"))
    assert_drained()

    booted = _boot()

    assert {"W1", "W2"} <= _children(booted)
    assert booted.reading().incomplete is True
    assert booted._marks["s-a"][3] < damaged_seq, "no mark past the damage"


def test_an_oversized_spawn_at_the_end_of_the_log_is_a_fault_as_in_a_full_read() -> None:
    """The oversized record is the log's last, so no later seq shows the skip. The full
    read still raises on it: the unit is reported incomplete, keeps its held spawns,
    and gets no mark past the damage."""
    from kiro_crew.crew_log.schema import MAX_ENTRY_BYTES

    _warm()
    damaged_seq = len((_dir("s-a") / "log.jsonl").read_bytes().splitlines())
    pad = b"x" * (MAX_ENTRY_BYTES + 16)
    _raw_line(
        "s-a",
        b'{"type":"session/spawned","seq":%d,"time":9,"src":"gateway",'
        b'"data":{"child":{"slot":"W9"},"pad":"' + pad + b'"}}',
    )

    for _ in range(2):
        booted = _boot()
        assert {"W1", "W2"} <= _children(booted)
        assert booted.reading().incomplete is True
        assert booted._marks["s-a"][3] < damaged_seq, "no mark past the damage"


def test_a_release_after_an_oversized_record_is_applied_while_the_spawns_are_kept() -> None:
    """The spawn read raises on the oversized record and the decision read does not, as
    in main's replay: the release is applied, the held spawns stay, the unit is reported
    incomplete, and it keeps the mark from before the damage."""
    from kiro_crew.crew_log.schema import MAX_ENTRY_BYTES

    _warm()
    damaged_seq = len((_dir("s-b") / "log.jsonl").read_bytes().splitlines())
    pad = b"x" * (MAX_ENTRY_BYTES + 16)
    _raw_line(
        "s-b",
        b'{"type":"session/spawned","seq":%d,"time":9,"src":"gateway",'
        b'"data":{"child":{"slot":"W9"},"pad":"' + pad + b'"}}',
    )
    _raw_line("s-b", b'{"type":"session/released","seq":%d,"time":10,"src":"gateway","data":{}}')

    booted = _boot()

    assert _parent(booted, "slot-b") is None
    assert {"W1", "W2"} <= _children(booted)
    assert booted.reading().incomplete is True
    assert booted._marks["s-b"][3] < damaged_seq, "no mark past the damage"


def test_a_replaced_older_segment_is_read_as_a_full_read_does() -> None:
    """A rotated unit's older segment is replaced by a copy without its adoption, while
    the newest segment and the cut counter stay as they were. The full read finds no
    decision and drops the edge; the boot must do the same."""
    _unit("s-p", "slot-p", 1)
    handle = _unit("s-a", "slot-a", SIZES[0])
    handle.append("session/adopted", {"parent": {"slot": "slot-p"}}, src=GATEWAY)
    assert_drained()
    _rotate("s-a")
    _boot()
    assert _parent(_boot(), "slot-a") == "slot-p"

    older = _dir("s-a") / "log.jsonl"
    kept = [line for line in older.read_bytes().splitlines(keepends=True) if b"adopted" not in line]
    copy = older.with_name("log.copy")
    copy.write_bytes(b"".join(kept))
    os.replace(copy, older)

    _assert_same_as_full_read(_boot())


def test_a_mark_is_dropped_when_its_record_is_evicted_past_the_cap(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap evicts the oldest record; its mark goes with it, as main drops the
    unit's scan cache, so the checkpoint holds no more marks than records."""
    from kiro_crew.crew_log import session_tree as stp_session_tree

    _floor_monkeypatch.setattr(stp_session_tree, "TREE_UNIT_CAP", 2)
    _floor_monkeypatch.setattr(stp, "TREE_UNIT_CAP", 2)
    _unit("s-a", "slot-a", 1)
    _unit("s-b", "slot-b", 1)
    assert_drained()
    _boot()
    _boot()
    _unit("s-c", "slot-c", 1)
    assert_drained()

    booted = _boot()

    assert set(booted._marks) <= set(booted._records), (booted._marks, booted._records)


def test_an_oversized_first_entry_on_a_full_walk_is_a_fault_as_in_a_full_read() -> None:
    """The oldest segment's FIRST entry is oversized, so a full walk's first yielded seq
    is already past it. Main's full read raises on it: the held spawns stay, the unit
    is reported incomplete, and no mark vouches for the damaged bytes."""
    from kiro_crew.crew_log.schema import MAX_ENTRY_BYTES

    _warm()
    path = _dir("s-a") / "log.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    pad = b"x" * (MAX_ENTRY_BYTES + 16)
    first = (
        b'{"type":"session/spawned","seq":1,"time":9,"src":"gateway",'
        b'"data":{"child":{"slot":"W9"},"pad":"' + pad + b'"}}\n'
    )
    copy = path.with_name("log.copy")
    copy.write_bytes(lines[0] + first + b"".join(lines[2:]))
    os.replace(copy, path)

    for _ in range(2):
        booted = _boot()
        assert {"W1", "W2"} <= _children(booted)
        assert booted.reading().incomplete is True
        held = booted._marks.get("s-a")
        assert held is None or held[7] != path.stat().st_size, "no mark for the damaged bytes"


def test_a_record_reusing_the_marked_seq_is_read_as_a_full_read_does() -> None:
    """After the mark, a release reuses the seq of the record the mark names. The walk
    would drop it as non-advancing; main's full read applies it."""
    _warm()
    path = _dir("s-b") / "log.jsonl"
    seq = len(path.read_bytes().splitlines()) - 1
    with open(path, "ab") as raw:
        raw.write(
            b'{"type":"session/released","seq":%d,"time":10,"src":"gateway","data":{}}\n' % seq
        )

    _assert_same_as_full_read(_boot())


def test_a_full_walk_that_yields_nothing_is_read_as_a_full_read_does() -> None:
    """The unit's only entry past its header is oversized, so a full walk yields nothing
    at all. Main's full read raises on it: held spawns stay, the unit is incomplete."""
    from kiro_crew.crew_log.schema import MAX_ENTRY_BYTES

    _warm()
    path = _dir("s-a") / "log.jsonl"
    header = path.read_bytes().splitlines(keepends=True)[0]
    pad = b"x" * (MAX_ENTRY_BYTES + 16)
    copy = path.with_name("log.copy")
    copy.write_bytes(
        header + b'{"type":"session/spawned","seq":1,"time":9,"src":"gateway",'
        b'"data":{"child":{"slot":"W9"},"pad":"' + pad + b'"}}\n'
    )
    os.replace(copy, path)

    booted = _boot()

    assert {"W1", "W2"} <= _children(booted)
    assert booted.reading().incomplete is True


def test_a_segment_gap_is_read_as_a_full_read_does() -> None:
    """A later segment that does not continue the earlier one is a provenance refusal
    in ``iter_from``; the unit is answered by main's full read."""
    _warm()
    directory = _dir("s-a")
    lines = (directory / "log.jsonl").read_bytes().splitlines(keepends=True)
    seq = len(lines) + 5
    (directory / f"log.{seq}.jsonl").write_bytes(
        lines[0] + b'{"type":"session/spawned","seq":%d,"time":9,"src":"gateway",'
        b'"data":{"child":{"slot":"W7"}}}\n' % seq
    )

    _assert_same_as_full_read(_boot())


def _write_checkpoint(mutate: Any) -> None:
    import json

    checkpoint = crew_log_root(lg.KIND_SESSION) / "projections" / stp.CHECKPOINT_NAME
    payload = json.loads(checkpoint.read_text())
    mutate(payload)
    checkpoint.write_text(json.dumps(payload))


def test_an_edges_row_that_names_no_unit_discards_the_checkpoint() -> None:
    """A damaged decision row with no ``sid`` cannot say whose mark it voids, so the
    whole checkpoint is refused and the boot rebuilds cold."""
    from kiro_crew.crew_log.session_tree_projection_checkpoint import _load_checkpoint

    _adopted_unit()
    _write_checkpoint(lambda payload: payload["edges"].append({"slot": "slot-z", "seq": 1}))

    assert _load_checkpoint() is None


def test_a_damaged_spawned_row_drops_its_units_mark() -> None:
    """A spawn row that does not decode is dropped, and so is the mark of the unit it
    names, so that unit is read in full and its spawns come back from the log."""
    from kiro_crew.crew_log.session_tree_projection_checkpoint import _load_checkpoint

    _warm()

    def damage(payload: Any) -> None:
        payload["spawned"][0]["seq"] = "damaged"

    _write_checkpoint(damage)
    loaded = _load_checkpoint()

    assert loaded is not None
    assert "s-a" not in loaded[3]
    assert "s-b" in loaded[3]
    assert {"W1", "W2"} <= _children(_boot())
