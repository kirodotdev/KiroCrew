"""A crew's write is refused rather than validated against a record it cannot prove.

This file pins the writes that refuse, and the reads that answer empty, when the record
may be short: two editors serialize on the crew's lock; a unit listing that fails or
cannot prove a unit refuses the write, a cached short listing included, while a unit
directory with nothing in it yet does not; an append the buffer rejected is a refusal;
a writer still behind, or one whose entry never landed, refuses the next write; an
undrained append is drained before the next fold; a log that cannot be folded reads as
empty; and a read-back that misses the written entry warns with its seq, unit and path.
The entry one write appends and the answer it returns live in
``test_issue_radar_crew_store.py`` and the unit-order file in
``test_issue_radar_crew_store_unit_order.py``.
"""

import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from issue_radar_crew_store_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    OWNER,
    REPO,
    _entries,
    _isolated_crew_log,
    _item,
    _live_crew,
    _pass,
    _record,
    _unit,
)

from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.crew_log.schema import KIND_SESSION


@contextmanager
def _eager_folder_held():
    """Settle the eager folder and hold it between batches for the body.

    The folder reads the store on its own thread after every entry, including the slot
    index scan. A test that patches a store read and then inspects the index has to
    keep that thread out: a scan it started before the patch can assign the index
    after the test's own read and replace what the test is about to check.
    """
    from kiro_crew.crew_log import eager

    assert eager.drain(timeout=10.0), "the eager folder never settled"
    with eager.paused() as held:
        assert held, "the eager folder could not be held between batches"
        yield


def test_a_crews_writes_are_serialized_so_two_editors_cannot_both_pass(tmp_path, monkeypatch):
    """The one-editor refusal is checked against the folded record, so two requests
    validating against the same fold could both pass and both append. Every write
    holds the crew's lock from fold to answer; the second request folds AFTER the
    first landed and is refused."""
    import threading

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    release = threading.Event()
    entered = threading.Event()
    real_fold = cs._fold_checkpoint

    def slow_fold(crew_id, units):
        checkpoint = real_fold(crew_id, units)
        if not entered.is_set():
            entered.set()
            release.wait(5)  # hold the first write inside its critical section
        return checkpoint

    monkeypatch.setattr(cs, "_fold_checkpoint", slow_fold)
    outcomes: dict[int, object] = {}

    def write(number):
        try:
            outcomes[number] = _record(tmp_path, cid, sid, number, {"phase": "implementing"})
        except cs.CrewStoreError as exc:
            outcomes[number] = exc

    first = threading.Thread(target=write, args=(2251,))
    first.start()
    assert entered.wait(5), "the first write never reached its fold"
    # The second write starts while the first is inside its critical section. Without
    # the lock it folds the same editor-less record, passes the one-editor check and
    # appends -- two editing items. With the lock it waits, folds after the first
    # landed, and is refused.
    second = threading.Thread(target=write, args=(2264,))
    second.start()
    # flake-ok: pre-existing 200 ms window for the second writer to reach the held lock, moved verbatim; tracked for a de-flake pass
    # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep, moved verbatim; tracked for a de-flake pass
    time.sleep(0.2)
    release.set()
    first.join(10)
    second.join(10)
    assert isinstance(outcomes[2251], dict) and outcomes[2251]["item"]["phase"] == "implementing"
    assert isinstance(outcomes[2264], cs.CrewStoreError)
    assert "already editing" in str(outcomes[2264])
    assert [i["number"] for i in cs.list_work_items(OWNER, REPO, cid, tmp_path)] == [2251]
    assert cs._crew_write_lock(cid) is cs._crew_write_lock(cid)


def test_a_unit_listing_that_fails_refuses_the_write_and_empties_the_read(tmp_path, monkeypatch):
    """The store's slot listing fails soft for a READ (it runs on every cycle, and an
    empty record says nothing false about what could be read). A WRITE lists strictly:
    folded over an empty record because the scan failed, the one-editor rule would
    admit a second editor, so the write is refused instead."""
    from kiro_crew.crew_log import store as crew_log_store

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 7, {"phase": "implementing"}, "implement", "on #7")

    # The store's own listing: soft for a read, raised for a strict caller.
    def scan_fails(kind):
        raise OSError("the store could not be scanned")

    with monkeypatch.context() as scan:
        scan.setattr(crew_log_store, "_checked_crew_log_root", scan_fails)
        assert crew_log_store.session_units_for_slot(cs.slot_key_for(cid)) == ()
        with pytest.raises(OSError):
            crew_log_store.session_units_for_slot(cs.slot_key_for(cid), strict=True)

    # Through the crew store: a read empties, a write is refused.
    def listing_fails(slot, **kw):
        raise OSError("the store could not be scanned")

    monkeypatch.setattr(crew_log_store, "session_units_for_slot", listing_fails)
    crew_log_projection.forget_slot_folds()
    assert cs.list_work_items(OWNER, REPO, cid, tmp_path) == [], "a read answers the empty record"
    with pytest.raises(cs.CrewLedgerNotRecorded):
        # Issue 8 entering an editing phase would be a SECOND editor beside 7.
        _record(tmp_path, cid, sid, 8, {"phase": "implementing"}, "implement", "on #8")


def test_a_unit_holding_entries_it_cannot_prove_refuses_the_write(tmp_path, monkeypatch):
    """A listing comes back INCOMPLETE, not failed: the root scan succeeds and one
    unit's header will not read. That unit's entries hold the editing item, so a write
    folded over the shorter listing passes the one-editor rule and admits a second
    editor -- the same loss as a scan that failed, reached without any failure."""
    from kiro_crew.crew_log import store as crew_log_store

    crew, retired = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, retired, 7, {"phase": "implementing"}, "implement", "on #7")
    live = _unit(cid)
    slot = cs.slot_key_for(cid)
    retired_dir = crew_log_store.unit_dir_for(KIND_SESSION, retired)
    assert retired_dir is not None, "the retired unit's directory is there to break"

    real_read = crew_log_store._read_header_line

    def header_will_not_read(path):
        if path.parent == retired_dir:
            raise OSError("the header will not read right now")
        return real_read(path)

    # Held so an eager-fold scan that read the retired header before the patch cannot
    # assign the index while this body inspects it (see ``_eager_folder_held``).
    with _eager_folder_held():
        monkeypatch.setattr(crew_log_store, "_read_header_line", header_will_not_read)
        monkeypatch.setattr(crew_log_store, "_slot_index", None)
        crew_log_projection.forget_slot_folds()

        # A READ takes the shorter listing: it says nothing false about what it could read.
        assert crew_log_store.session_units_for_slot(slot) == (live,)
        # A validating caller is refused instead, naming the unit it could not account for.
        monkeypatch.setattr(crew_log_store, "_slot_index", None)
        with pytest.raises(crew_log_store.CrewLogError, match="cannot prove"):
            crew_log_store.session_units_for_slot(slot, strict=True)

        # Through the crew store: issue 8 entering an editing phase is the second editor.
        monkeypatch.setattr(crew_log_store, "_slot_index", None)
        with pytest.raises(cs.CrewLedgerNotRecorded):
            _record(tmp_path, cid, live, 8, {"phase": "implementing"}, "implement", "on #8")


def test_a_unit_directory_with_nothing_in_it_yet_does_not_refuse_the_write(tmp_path):
    """The control, and the reason the refusal is not simply "any unprovable child":
    ``create`` makes the directory and publishes the header as a second step, so a
    scan landing in that window sees a child with no content. It holds no entries, so
    leaving it out loses nothing -- and refusing on it would make every crew write
    fail whenever any session anywhere happened to be starting its log."""
    from kiro_crew.crew_log import store as crew_log_store

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    root = crew_log_store._checked_crew_log_root(KIND_SESSION)
    (root / "acp-mid-create").mkdir()
    crew_log_store._slot_index = None
    crew_log_projection.forget_slot_folds()

    assert crew_log_store.session_units_for_slot(cs.slot_key_for(cid), strict=True) == (sid,)
    item = _item(tmp_path, cid, sid, 11, {"phase": "implementing"})
    assert item["phase"] == "implementing", "the write went through"


def test_a_cached_listing_that_is_short_a_unit_still_refuses_the_write(tmp_path, monkeypatch):
    """The half a scan-only check misses. An unprovable child does not move the root's
    fingerprint, so once a READ has cached the shorter map the next strict caller takes
    a cache HIT and never reaches the scan. The refusal has to be re-derived on the hit
    or the validating caller is served exactly the listing the scan refused."""
    from kiro_crew.crew_log import store as crew_log_store

    crew, retired = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, retired, 7, {"phase": "implementing"}, "implement", "on #7")
    live = _unit(cid)
    slot = cs.slot_key_for(cid)
    retired_dir = crew_log_store.unit_dir_for(KIND_SESSION, retired)
    real_read = crew_log_store._read_header_line

    def header_will_not_read(path):
        if path.parent == retired_dir:
            raise OSError("the header will not read right now")
        return real_read(path)

    # The eager folder scans the slot index on its own thread. A scan that read the
    # retired header before the patch and assigns the map after the read below would
    # replace the cached map with one that proves every unit. Held between batches,
    # no fold scans until the body is done.
    with _eager_folder_held():
        monkeypatch.setattr(crew_log_store, "_read_header_line", header_will_not_read)
        monkeypatch.setattr(crew_log_store, "_slot_index", None)
        crew_log_projection.forget_slot_folds()

        # The read scans and CACHES the map, naming the child it could not prove.
        assert crew_log_store.session_units_for_slot(slot) == (live,)
        cached = crew_log_store._slot_index
        assert cached is not None and cached[2] == (retired_dir.name,), "cached, short a unit"

        # No scan runs now: same fingerprint, still unprovable. The refusal must still fire.
        with pytest.raises(crew_log_store.CrewLogError, match="cannot prove"):
            crew_log_store.session_units_for_slot(slot, strict=True)
        assert crew_log_store._slot_index is cached, "served from the cache, not re-scanned"


def test_an_append_the_buffer_rejected_at_its_ceiling_is_a_refusal_not_a_record(
    tmp_path, monkeypatch
):
    """The writer's buffer can reject an append at SUBMISSION for crossing its memory
    ceiling; that is counted per session and apart from a storage refusal. With the
    writer also not draining inside the budget, a rejected entry is neither in the
    file nor queued -- it will never land -- so the write is refused instead of
    answering the entry as state. ANOTHER session's rejection in the same window is
    not this append's: an accepted append is answered as the queued state, never as a
    refusal that would have the caller re-send an update that lands."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    real_recorded = crew_log_emit.on_radar_recorded
    counts: dict[str, int] = {}
    monkeypatch.setattr(
        crew_log_emit, "overflow_writes", lambda session_id=None: counts.get(session_id, 0)
    )
    monkeypatch.setattr(crew_log_emit, "flush", lambda timeout=None: False)  # saturated writer

    def rejected(session_id, data):
        counts[session_id] = counts.get(session_id, 0) + 1  # the buffer refused to hold it

    monkeypatch.setattr(crew_log_emit, "on_radar_recorded", rejected)
    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert cs.list_work_items(OWNER, REPO, cid, tmp_path) == []

    def queued_while_another_overflows(session_id, data):
        real_recorded(session_id, data)
        counts["some-other-session"] = counts.get("some-other-session", 0) + 1

    monkeypatch.setattr(crew_log_emit, "on_radar_recorded", queued_while_another_overflows)
    res = _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert res["durable"] is False and res["item"]["phase"] == "claimed"


def test_a_crew_whose_log_cannot_be_folded_reads_as_empty_and_not_as_a_crash(tmp_path, monkeypatch):
    """The repository's skip memory and its briefing read ACROSS crews, so one crew's
    damaged log must not raise into them: the pre-projection reader answered empty on
    an index it could not parse, and this keeps that contract. The write path is the
    other half -- it folds strictly, because a write validated against an empty record
    could admit a second editor."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    _pass(tmp_path, cid, sid, 41, "already fixed on main", "already-fixed")
    assert cs.is_skipped(OWNER, REPO, 41, tmp_path), "the control: the pass is readable"

    def raising(*a, **kw):
        raise OSError("the log cannot be read")

    real_fold = cs._projection().fold_slot_warm
    crew_log_projection.forget_slot_folds()
    monkeypatch.setattr(cs._projection(), "fold_slot_warm", raising)

    assert cs.read_ledger(OWNER, REPO, cid, tmp_path)["items"] == [], "reads as the empty record"
    assert cs.read_skips(OWNER, REPO, tmp_path) == {}, "the repository's read still answers"
    assert cs.is_skipped(OWNER, REPO, 41, tmp_path) is False
    # The write half: it folds strictly, so a fold it cannot make RAISES here rather
    # than validating this update against an empty record.
    with pytest.raises((OSError, cs.CrewLedgerNotRecorded)):
        _record(tmp_path, cid, sid, 9, {"phase": "claimed"}, "claim", "took #9")

    monkeypatch.setattr(cs._projection(), "fold_slot_warm", real_fold)
    crew_log_projection.forget_slot_folds()
    assert cs.is_skipped(OWNER, REPO, 41, tmp_path), "nothing was lost while it read as empty"


def test_an_undrained_append_is_drained_before_the_crews_next_write_folds(tmp_path, monkeypatch):
    """A write whose append the writer had not drained answers ``durable: false``; the
    crew's next write drains again BEFORE it folds, so it does not validate against
    a record missing that entry."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    real_flush = crew_log_emit.flush
    calls: list[bool] = []

    def flush(timeout=None):
        drained = real_flush(timeout=timeout)
        calls.append(drained)
        return False if len(calls) == 1 else drained  # the first write's drain "times out"

    monkeypatch.setattr(crew_log_emit, "flush", flush)
    first = _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert first["durable"] is False
    assert any(key[1] == cid for key in cs._undrained), "the crew is marked undrained"
    _record(tmp_path, cid, sid, 7, {"next": "read it"}, "claim", "progress")
    assert len(calls) == 3, "the second write drained once before folding and once after its append"
    assert not any(key[1] == cid for key in cs._undrained)


def test_a_writer_still_behind_at_the_next_write_refuses_it_rather_than_fold_stale(
    tmp_path, monkeypatch
):
    """The crew's last append is still queued and the pre-write drain times out too:
    a fold taken now would be missing that entry, and the one-editor rule checked
    against it could admit a second editor. The write is refused (503, nothing
    changed, send it again); once the writer drains the same write goes through."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    real_flush = crew_log_emit.flush
    behind = {"on": True}

    def flush(timeout=None):
        drained = real_flush(timeout=timeout)
        return False if behind["on"] else drained

    monkeypatch.setattr(crew_log_emit, "flush", flush)
    first = _record(tmp_path, cid, sid, 7, {"phase": "implementing"}, "implement", "on #7")
    assert first["durable"] is False
    appended = len(_entries(sid))
    # #8 would be a SECOND editor: with the fold missing #7's entry it would pass.
    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, sid, 8, {"phase": "implementing"}, "implement", "on #8")
    assert len(_entries(sid)) == appended, "the refused write appended nothing"
    assert any(key[1] == cid for key in cs._undrained), "the mark stays until a drain succeeds"

    behind["on"] = False
    with pytest.raises(cs.CrewStoreError, match="already editing #7"):
        _record(tmp_path, cid, sid, 8, {"phase": "implementing"}, "implement", "on #8")
    assert not any(key[1] == cid for key in cs._undrained)


def test_a_quiet_writer_whose_entry_never_landed_refuses_the_next_write(tmp_path, monkeypatch):
    """A DROP empties the writer's buffer exactly as a successful write does, so
    ``flush`` answers True either way. Releasing the undrain mark on that alone would
    fold a record short one entry and hand the one-editor rule a record that permits a
    second editor -- silently, because the earlier call already answered. The mark
    carries a receipt for the queued entry, so it is released only once that entry is
    found in its unit's own log by content."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    appended = len(_entries(sid))

    # This append is dropped on the floor and its drain reports a timeout, so the crew
    # is marked undrained for an entry that is not in the log and never will be.
    monkeypatch.setattr(crew_log_emit, "on_radar_recorded", lambda *a, **k: None)
    monkeypatch.setattr(crew_log_emit, "flush", lambda timeout=None: False)
    answer = _record(tmp_path, cid, sid, 7, {"next": "dropped"}, "claim", "queued")
    assert answer["durable"] is False
    assert any(key[1] == cid for key in cs._undrained), "the crew is marked undrained"
    assert len(_entries(sid)) == appended, "nothing of that write reached the log"

    # The writer is now QUIET -- but the entry still is not there.
    monkeypatch.setattr(crew_log_emit, "flush", lambda timeout=None: True)
    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, sid, 7, {"next": "after"}, "claim", "next write")
    assert any(key[1] == cid for key in cs._undrained), "a quiet writer did not clear the mark"


def test_a_read_back_that_misses_the_written_entry_warns_with_seq_unit_and_path(caplog):
    """The writer said the entry was written, and the read-back does not find it: that
    is "written but not readable", so it says so at WARNING with the seq it read
    after, the unit and the file it looked in. A DEBUG line hides the only cause a
    CI red can show."""
    import logging

    from kiro_crew.crew_log.schema import Entry

    data = {"crew_id": "c_0a1b2c3d", "owner": OWNER, "repo": REPO, "number": 7}
    other = Entry(
        type=cs.LEDGER_ENTRY_TYPE,
        seq=6,
        time=1_789_000_000_000,
        src="gateway",
        data={**data, "number": 8},
    )
    written = Entry(
        type=cs.LEDGER_ENTRY_TYPE, seq=7, time=1_789_000_000_000, src="gateway", data=data
    )

    class _Handle:
        path = Path("crew-log") / "session" / "unit-x.jsonl"

        def iter_from(self, seq, known=None):
            # The read skips the written entry's seq.
            return iter(e for e in (other, written) if e.seq >= seq and e.seq != 7)

    class _Projection:
        KNOWN_TYPES = None

        @staticmethod
        def open_session_log(session_id):
            assert session_id == "unit-x"
            return _Handle()

    with caplog.at_level(logging.WARNING, logger=cs.logger.name):
        since, landed = cs._landed_since(_Projection(), "unit-x", 5, data)
    assert landed is False
    assert since is not None and [e.seq for e in since] == [6]
    (record,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    text = record.getMessage()
    assert "seq 5" in text and "unit-x" in text and "unit-x.jsonl" in text

    # Control: the same read without the skip finds the entry and warns nothing.
    caplog.clear()

    class _Whole(_Handle):
        def iter_from(self, seq, known=None):
            return iter(e for e in (other, written) if e.seq >= seq)

    _Projection.open_session_log = staticmethod(lambda session_id: _Whole())
    with caplog.at_level(logging.WARNING, logger=cs.logger.name):
        assert cs._landed_since(_Projection(), "unit-x", 5, data)[1] is True
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
