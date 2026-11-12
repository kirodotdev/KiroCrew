"""``_seed_attempts`` decodes only the records that name ``turn/started``, same answer.

It runs on the single writer thread whenever a session is resumed or its attempt map was
evicted, and it needs only a few records of the file. A raw prefilter
(``CrewLog.iter_from(contains=...)``) skips the JSON decode of every other record; the
type check in ``_seed_attempts`` still decides.

Real store on a temp data home. Cost is counted as ``Entry.from_dict`` calls: one per
record actually decoded.
"""

from __future__ import annotations

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import emit_session, store_cuts
from kiro_crew.crew_log.emit_runtime import _tracker
from kiro_crew.crew_log.schema import Entry

SESSION = "acp-seed-prefilter"


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    crew_log_emit.reset_caches()
    yield
    crew_log_emit.reset_caches()


def _old_map(log: CrewLog) -> "dict[int, int]":
    """The attempt map as the full-decode scan built it, for comparison."""
    highest: dict[int, int] = {}
    for entry in log.iter_from(1, strict_seq=False):
        if entry.type != "turn/started":
            continue
        turn = entry.data.get("turn")
        if not isinstance(turn, int) or isinstance(turn, bool):
            continue
        attempt = entry.data.get("attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            attempt = 1
        if attempt > highest.get(turn, 0):
            highest[turn] = attempt
    return highest


def _new_map(session_id: str, log: CrewLog) -> "dict[int, int]":
    crew_log_emit.reset_caches()
    emit_session._seed_attempts(session_id, log)
    return dict(_tracker()._attempts.get(session_id, {}))


def _started(turn: object, attempt: object = None) -> dict:
    data: dict = {"turn": turn, "actor": "user", "depth": 0}
    if attempt is not None:
        data["attempt"] = attempt
    return {"type": "turn/started", "data": data}


def _chatter(n: int, size: int = 400) -> "list[dict]":
    return [
        {
            "type": "turn/completed",
            "data": {"turn": k, "stop_reason": "end_turn", "error": "x" * size},
        }
        for k in range(n)
    ]


def _open() -> CrewLog:
    return CrewLog.create(lg.KIND_SESSION, SESSION, owner="o", agent="kirocrew", slot="chat-seed")


def test_the_prefiltered_scan_gives_the_same_map_on_a_damaged_log():
    """Mixed kinds, a data value naming the type, a cut that reused seqs, a torn tail."""
    log = _open()
    log.append_many(
        [
            _started(1),
            _started(1, 2),
            {"type": "turn/completed", "data": {"turn": 1, "stop_reason": "turn/started"}},
            _started(2, 0),
            _started(3, 4),
            *_chatter(5),
            _started(4),
        ],
        src="gateway",
    )
    # A cut: the last three records are removed and the freed seqs are reused, as the
    # store's own recovery does, with the cut counter raised around it.
    path = log.path
    lines = path.read_bytes().splitlines(keepends=True)
    store_cuts._open_cut(path.parent)
    path.write_bytes(b"".join(lines[:-3]))
    store_cuts._close_cut(path.parent)
    log.release_ownership()
    reopened = CrewLog.open(lg.KIND_SESSION, SESSION)
    reopened.append_many([_started(3, 7), _started(5)], src="gateway")
    reopened.release_ownership()
    # A torn last line: a writer killed mid-append.
    with open(path, "ab") as tail:
        tail.write(b'{"seq": 99, "type": "turn/started", "data": {"turn": 9')
    log = CrewLog.open(lg.KIND_SESSION, SESSION, repair=False)

    old = _old_map(log)
    assert old, "the fixture holds turns"
    assert _new_map(SESSION, log) == old


def test_a_log_with_two_segments_gives_the_same_map():
    """More than one segment falls back to the full walk, so its boundary is still checked."""
    log = _open()
    log.append_many([_started(1), *_chatter(3), _started(2, 3), _started(3)], src="gateway")
    head = log.path
    log.release_ownership()
    lines = head.read_bytes().splitlines(keepends=True)
    (head.parent / "log.4.jsonl").write_bytes(lines[0] + b"".join(lines[4:]))
    head.write_bytes(b"".join(lines[:4]))
    log = CrewLog.open(lg.KIND_SESSION, SESSION, repair=False)
    assert len(lg.segment_paths(lg.KIND_SESSION, SESSION)) == 2
    assert _new_map(SESSION, log) == _old_map(log)


def test_contains_needs_the_lenient_seq_walk():
    log = _open()
    with pytest.raises(ValueError):
        list(log.iter_from(1, contains=b'"turn/started"'))


@pytest.mark.parametrize("filler", [250, 1_000, 4_000, 16_000])
def test_records_decoded_follow_the_turns_not_the_file(filler, _floor_monkeypatch):
    """Flat cost: 40 turns among a growing body of other records, decoded 40 times."""
    log = _open()
    turns = 40
    per_turn = filler // turns
    for turn in range(1, turns + 1):
        log.append_many([_started(turn), *_chatter(per_turn)], src="gateway")
    decoded = {"n": 0}
    real = Entry.from_dict.__func__  # type: ignore[attr-defined]

    def counted(cls, raw):
        decoded["n"] += 1
        return real(cls, raw)

    _floor_monkeypatch.setattr(Entry, "from_dict", classmethod(counted))
    seeded = _new_map(SESSION, log)

    assert seeded == {turn: 1 for turn in range(1, turns + 1)}
    # The 40 matching records, plus the store's own read of the segment's first entry.
    assert decoded["n"] <= turns + 1, decoded
