"""Conductor work ledger store: a damaged or unreadable file reads as absent, never as a crash.

A torn, truncated, oversized, non-UTF-8 or misnamed record reads as absent and hides none of
its siblings, while a TRANSIENT read error fails the write guards closed instead of resetting
the header, truncating the event log or admitting a second binding.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, WORKER, _bytes_on_disk, _new_item

from kiro_crew import work_ledger as wl

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated_home = work_ledger_helpers._isolated_home


# ── corruption reads as absent ────────────────────────────────────────────


def test_a_truncated_item_file_reads_as_absent():
    item_id = _new_item()
    path = wl.item_path(CONDUCTOR, item_id)
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw[: len(raw) // 2], encoding="utf-8")
    assert wl.read_work_item(CONDUCTOR, item_id) is None
    assert wl.list_work_items(CONDUCTOR) == []


def test_a_non_utf8_item_file_reads_as_absent():
    item_id = _new_item()
    wl.item_path(CONDUCTOR, item_id).write_bytes(b"\xff\xfe not utf-8")
    assert wl.read_work_item(CONDUCTOR, item_id) is None


def test_an_oversized_item_file_reads_as_absent(monkeypatch, caplog):
    item_id = _new_item()
    monkeypatch.setattr(wl, "MAX_RECORD_BYTES", 4)
    with caplog.at_level("WARNING"):
        assert wl.read_work_item(CONDUCTOR, item_id) is None
    assert "size ceiling" in caplog.text


def test_a_corrupt_conductor_file_reads_as_absent():
    wl.ensure_conductor(CONDUCTOR, goal="g")
    (wl.conductor_dir(CONDUCTOR) / "conductor.json").write_text("{ torn", encoding="utf-8")
    assert wl.read_conductor(CONDUCTOR) is None


def test_a_malformed_item_filename_is_skipped_not_raised():
    good = _new_item(title="good")
    (wl.items_dir(CONDUCTOR) / "it_bad.json").write_text("{}", encoding="utf-8")
    (wl.items_dir(CONDUCTOR) / "it_1a2b3c4d5.json").write_text("{}", encoding="utf-8")
    assert [item.item_id for item in wl.list_work_items(CONDUCTOR)] == [good]
    # create walks the same listing for the cap, so it must not crash either.
    wl.apply_conductor_action(CONDUCTOR, "create", title="next", acceptance={})


def test_one_torn_item_does_not_hide_its_siblings():
    good = _new_item(title="good")
    bad = _new_item(title="bad")
    wl.item_path(CONDUCTOR, bad).write_text("{", encoding="utf-8")
    assert [item.item_id for item in wl.list_work_items(CONDUCTOR)] == [good]


def test_an_item_nested_too_deep_for_the_parser_reads_as_absent():
    """A 20 KB item file of ten thousand nested arrays is under the ceiling and
    raises ``RecursionError`` from inside ``json.loads``; it reads the way any
    other content the reader cannot trust does — absent — and hides no sibling."""
    good = _new_item(title="good")
    deep = _new_item(title="deep")
    wl.item_path(CONDUCTOR, deep).write_text("[" * 10_000 + "]" * 10_000, encoding="utf-8")
    assert wl.read_work_item(CONDUCTOR, deep) is None
    assert [item.item_id for item in wl.list_work_items(CONDUCTOR)] == [good]


def test_a_wrong_typed_stored_field_resets_to_its_default_without_raising():
    item_id = _new_item()
    path = wl.item_path(CONDUCTOR, item_id)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored.update(
        {
            "state": "teleported",
            "status": 17,
            "verdict": "maybe",
            "round": "many",
            "artifacts": {"keep": "me", "drop": 5},
            "title": None,
            "pr": True,
            "acceptance": "not an object",
        }
    )
    path.write_text(json.dumps(stored), encoding="utf-8")
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    assert (item.state, item.status, item.verdict, item.round) == ("open", None, None, 0)
    assert item.artifacts == {"keep": "me"}
    assert (item.title, item.pr, item.acceptance) == ("", None, {})


def test_an_item_file_storing_a_different_id_reads_as_absent(caplog):
    """Honouring a mismatched stored id would let a write taken
    under this item's lock land on another item's path."""
    a = _new_item(title="a")
    b = _new_item(title="b")
    stored = json.loads(wl.item_path(CONDUCTOR, a).read_text(encoding="utf-8"))
    stored["item_id"] = b
    wl.item_path(CONDUCTOR, a).write_text(json.dumps(stored), encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert wl.read_work_item(CONDUCTOR, a) is None
    assert "treating as absent" in caplog.text
    b_before = wl.item_path(CONDUCTOR, b).read_bytes()
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "decide", item_id=a, decision="x")
    assert caught.value.code == wl.CODE_UNKNOWN_ITEM
    assert wl.item_path(CONDUCTOR, b).read_bytes() == b_before


def _fault_record_read(monkeypatch, *, name: str | None = None, parent: Path | None = None):
    """Make a record read raise a Windows-style sharing violation, and undo it.

    Records are read through ``atomic_write.read_bytes_with_retry``, so the fault
    belongs on ``Path.read_bytes``. On POSIX that helper treats ``PermissionError``
    as a genuine access fault and re-raises on the first attempt, so a test pinning
    the fail-closed contract sees the error directly. Returns the callable that
    restores the real reader.

    Exactly one selector: ``name`` faults a single file, ``parent`` faults every
    read inside one directory.
    """
    real_read_bytes = Path.read_bytes

    def flaky(self, *a, **kw):
        if (name is not None and self.name == name) or (
            parent is not None and self.parent == parent
        ):
            raise PermissionError("sharing violation")
        return real_read_bytes(self, *a, **kw)

    monkeypatch.setattr(Path, "read_bytes", flaky)

    def restore() -> None:
        monkeypatch.setattr(Path, "read_bytes", real_read_bytes)

    return restore


def test_the_bind_guard_fails_closed_on_a_transient_read_error(monkeypatch):
    """A prior item that is present but momentarily unreadable must
    NOT read as stale, or the worker is rebound and its open item stranded."""
    first = _new_item(title="first")
    second = _new_item(title="second")
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=first, worker_session_key=WORKER)
    restore = _fault_record_read(monkeypatch, name=f"{first}.json")
    with pytest.raises(PermissionError):
        wl.apply_conductor_action(CONDUCTOR, "bind", item_id=second, worker_session_key=WORKER)
    restore()
    assert wl.read_binding(WORKER) == (CONDUCTOR, first)
    second_item = wl.read_work_item(CONDUCTOR, second)
    assert second_item is not None and second_item.worker_session_key is None


def test_the_bind_guard_fails_closed_when_the_binding_itself_is_unreadable(monkeypatch):
    """The same strictness applies to the BINDING read, or a transient
    error there reads as unbound and the open item is stranded."""
    first = _new_item(title="first")
    second = _new_item(title="second")
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=first, worker_session_key=WORKER)
    binding_before = wl.binding_path(WORKER).read_bytes()
    restore = _fault_record_read(monkeypatch, parent=wl.bindings_dir())
    with pytest.raises(PermissionError):
        wl.apply_conductor_action(CONDUCTOR, "bind", item_id=second, worker_session_key=WORKER)
    restore()
    assert wl.binding_path(WORKER).read_bytes() == binding_before
    assert wl.read_binding(WORKER) == (CONDUCTOR, first)
    # The lenient reader still answers 'unbound' for a worker tool.
    _fault_record_read(monkeypatch, parent=wl.bindings_dir())
    assert wl.read_binding(WORKER) is None


def test_a_transient_read_error_does_not_reset_the_conductor_header(monkeypatch):
    """ensure_conductor must not mint a fresh header over one it
    merely failed to read."""
    wl.ensure_conductor(CONDUCTOR, goal="keep me", depth=1)
    wl.apply_conductor_action(CONDUCTOR, "goal", round_number=4)
    before = (wl.conductor_dir(CONDUCTOR) / "conductor.json").read_bytes()
    restore = _fault_record_read(monkeypatch, name="conductor.json")
    with pytest.raises(PermissionError):
        wl.ensure_conductor(CONDUCTOR, goal="other")
    # The action entry point's lenient pre-lock read answers ``no_ledger`` first;
    # either way nothing is written.
    with pytest.raises((PermissionError, wl.WorkLedgerError)):
        wl.apply_conductor_action(CONDUCTOR, "goal", goal="other")
    with pytest.raises(PermissionError):
        wl._write_goal(CONDUCTOR, wl.ConductorRecord(slot_key=CONDUCTOR), "other", None)
    restore()
    assert (wl.conductor_dir(CONDUCTOR) / "conductor.json").read_bytes() == before
    record = wl.read_conductor(CONDUCTOR)
    assert record is not None and (record.goal, record.round, record.depth) == ("keep me", 4, 1)


def test_a_transient_read_error_does_not_truncate_the_event_log(monkeypatch):
    """The log writer rewrites from what it read, so an unreadable
    log must fail the write, not be replaced by a one-line log."""
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="one")
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="two")
    before = _bytes_on_disk(item_id)
    real_read_text = Path.read_text

    def flaky(self, *a, **kw):
        if self.suffix == ".jsonl":
            raise PermissionError("sharing violation")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", flaky)
    with pytest.raises(PermissionError):
        wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="three")
    monkeypatch.setattr(Path, "read_text", real_read_text)
    assert _bytes_on_disk(item_id) == before
    assert len(wl.read_events(CONDUCTOR, item_id)) == 3


def test_an_interrupted_bind_can_be_retried(caplog):
    """A binding whose item does not name the worker back is the
    half-state a kill between bind's two writes leaves; the retry must succeed."""
    item_id = _new_item()
    # Simulate the crash: binding written, item never updated.
    wl._write_binding(WORKER, CONDUCTOR, item_id)
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.worker_session_key is None
    with caplog.at_level("WARNING"):
        wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key=WORKER)
    assert "interrupted bind" in caplog.text
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.worker_session_key == WORKER
    assert wl.read_binding(WORKER) == (CONDUCTOR, item_id)
    # And a genuinely live binding (item names the worker) still refuses.
    other = _new_item(title="other")
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "bind", item_id=other, worker_session_key=WORKER)
    assert caught.value.code == wl.CODE_ALREADY_BOUND


def test_lenient_reads_still_treat_a_transient_error_as_absent(monkeypatch):
    item_id = _new_item()
    _fault_record_read(monkeypatch, name=f"{item_id}.json")
    assert wl.read_work_item(CONDUCTOR, item_id) is None
    assert wl.list_work_items(CONDUCTOR) == []


def test_round_number_is_refused_on_actions_that_do_not_take_it():
    item_id = _new_item()
    for action, kwargs in (
        ("bind", {"worker_session_key": WORKER}),
        ("verdict", {"verdict": "pass"}),
        ("close", {"state": "accepted"}),
    ):
        before = _bytes_on_disk(item_id)
        with pytest.raises(wl.WorkLedgerError) as caught:
            wl.apply_conductor_action(CONDUCTOR, action, item_id=item_id, round_number=3, **kwargs)
        assert caught.value.field == "round_number"
        assert _bytes_on_disk(item_id) == before


def test_records_built_from_a_non_object_are_empty_defaults():
    assert wl.ConductorRecord.from_dict("nope").goal == ""
    assert wl.WorkItem.from_dict(None).state == "open"
    assert wl.WorkEvent.from_dict([1]) is None


def test_read_helpers_backfill_an_id_the_stored_record_lost():
    item_id = _new_item()
    path = wl.item_path(CONDUCTOR, item_id)
    stored = json.loads(path.read_text(encoding="utf-8"))
    del stored["item_id"]
    path.write_text(json.dumps(stored), encoding="utf-8")
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.item_id == item_id

    conductor_file = wl.conductor_dir(CONDUCTOR) / "conductor.json"
    header = json.loads(conductor_file.read_text(encoding="utf-8"))
    del header["slot_key"]
    conductor_file.write_text(json.dumps(header), encoding="utf-8")
    record = wl.read_conductor(CONDUCTOR)
    assert record is not None and record.slot_key == CONDUCTOR
