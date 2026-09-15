"""Ledger cleanup sweep — the refuse-safe rules, one test per rule.

Pins what docs/system-specs/modules/session-work-ledger.md §2 "Cleanup" states:
a dry run lists and deletes nothing; a purge removes only what the report named;
an in-flight session ledger and a conductor holding an open item are never
candidates at any age; the threshold is a boundary rather than a hint; an
unreadable record is listed but survives a plain purge; a conductor holding no
items is kept at any age; a terminal session ledger written to recently is not
old; and the window has one owner, so the CLI default cannot drift from the
module's.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cli_test_helpers import cli_sandbox_environment  # noqa: F401

from kiro_crew import ledger_sweep as sweep
from kiro_crew import session_ledger as sl
from kiro_crew import work_ledger as wl
from kiro_crew.platform_compat import IS_POSIX

CONDUCTOR = "chat-9-conductor"
WORKER = "chat-9-worker"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


# ── fixtures on disk ──────────────────────────────────────────────────────


def _backdate(path: Path, days: float) -> None:
    stamp = time.time() - days * 86400.0
    os.utime(path, (stamp, stamp))


def _iso_days_ago(days: float) -> str:
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")


def _session_ledger(key: str, *, phase: str, age_days: float) -> Path:
    """A session ledger in *phase*, whose age the sweep will measure as *age_days*."""
    sl.record(key, goal="ship it", phase=phase, event="moved", event_kind="phase")
    directory = sl.ledger_dir(key)
    state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    if phase in sl.TERMINAL_PHASES:
        state["finished_at"] = _iso_days_ago(age_days)
    (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
    _backdate(directory / "state.json", age_days)
    return directory


def _work_item(conductor: str = CONDUCTOR) -> str:
    wl.ensure_conductor(conductor, goal="drive the fleet")
    result = wl.apply_conductor_action(
        conductor, "create", title="port the gate", acceptance={"kind": "human_approval"}
    )
    return str(result["item"].item_id)


def _work_ledger(
    conductor: str = CONDUCTOR, *, closed: bool = True, age_days: float = 90.0
) -> Path:
    """A conductor ledger whose single item is closed (or left open)."""
    item_id = _work_item(conductor)
    if closed:
        wl.apply_conductor_action(conductor, "close", item_id=item_id, state="accepted")
        path = wl.item_path(conductor, item_id)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["closed_at"] = _iso_days_ago(age_days)
        path.write_text(json.dumps(record), encoding="utf-8")
    directory = wl.conductor_dir(conductor)
    _backdate(directory / "conductor.json", age_days)
    _backdate_items(directory, age_days)
    return directory


def _backdate_items(directory: Path, days: float) -> None:
    """Age every non-lock file under ``items/``: the sweep reads their mtimes too."""
    items = directory / "items"
    if items.is_dir():
        for entry in items.iterdir():
            if not entry.name.endswith(".lock"):
                _backdate(entry, days)


def _stores(report: sweep.SweepReport) -> set[str]:
    return {candidate.store for candidate in report.candidates}


# ── dry run ───────────────────────────────────────────────────────────────


def test_dry_run_lists_both_kinds_and_deletes_nothing():
    session = _session_ledger("chat-1-old", phase="done", age_days=90)
    work = _work_ledger()

    report = sweep.scan(older_than_days=30)

    assert _stores(report) == {session.name, work.name}
    assert {c.kind for c in report.candidates} == {sweep.KIND_SESSION, sweep.KIND_WORK}
    assert session.is_dir() and work.is_dir(), "a scan must not remove anything"
    # Every line names the record and why it qualified — the report is what makes
    # the irreversible second command reviewable.
    rendered = "\n".join(sweep.render(report))
    assert session.name in rendered and work.name in rendered
    assert "phase=done" in rendered and "items=0 open/1 closed" in rendered
    assert "2 candidate(s)" in rendered and "0 report-only" in rendered


def test_purge_removes_only_the_candidates():
    stale = _session_ledger("chat-1-old", phase="done", age_days=90)
    live = _session_ledger("chat-2-live", phase="implementing", age_days=90)
    young = _session_ledger("chat-3-young", phase="done", age_days=1)
    work = _work_ledger()
    open_work = _work_ledger("chat-8-busy", closed=False)

    report = sweep.scan(older_than_days=30)
    result = sweep.purge(report)

    assert {c.store for c in result.removed} == {stale.name, work.name}
    assert not result.failed and not result.skipped_unreadable
    assert not stale.exists() and not work.exists()
    assert live.is_dir() and young.is_dir() and open_work.is_dir()


# ── never a candidate, whatever the age ───────────────────────────────────


@pytest.mark.parametrize("phase", ["implementing", "awaiting-ci", "blocked"])
def test_an_in_flight_session_ledger_is_never_a_candidate(phase):
    """The ledger IS what a resumed loop reads to recover its next step, so age
    alone can never make it collectable."""
    directory = _session_ledger("chat-4-busy", phase=phase, age_days=3650)

    report = sweep.scan(older_than_days=1)

    assert directory.name not in _stores(report)
    assert report.kept >= 1


def test_a_conductor_with_an_open_item_is_never_a_candidate():
    directory = _work_ledger(closed=False, age_days=3650)

    report = sweep.scan(older_than_days=1)

    assert directory.name not in _stores(report)
    assert report.kept >= 1


def test_a_bound_workers_open_item_keeps_the_conductor():
    """A binding is a worker's only report channel, and the item census is what
    protects it: the item a live binding names is open, and an open item keeps the
    whole ledger. That is why the sweep needs no separate binding scan."""
    item_id = _work_item()
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key=WORKER)
    assert wl.read_binding(WORKER) == (CONDUCTOR, item_id)
    directory = wl.conductor_dir(CONDUCTOR)
    _backdate(directory / "conductor.json", 90)

    report = sweep.scan(older_than_days=30)

    assert directory.name not in _stores(report)
    assert report.kept >= 1


# ── threshold ─────────────────────────────────────────────────────────────


def test_threshold_is_a_boundary_not_a_hint():
    directory = _session_ledger("chat-5-edge", phase="done", age_days=30.5)

    assert directory.name in _stores(sweep.scan(older_than_days=30))
    assert directory.name not in _stores(sweep.scan(older_than_days=31))


def test_age_falls_back_to_the_state_file_when_the_stamp_is_empty():
    """A terminal record whose ``finished_at`` never landed is still measurable —
    otherwise it would be permanently uncollectable."""
    directory = _session_ledger("chat-6-nostamp", phase="done", age_days=90)
    state_path = directory / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["finished_at"] = ""
    state_path.write_text(json.dumps(state), encoding="utf-8")
    _backdate(state_path, 90)

    candidates = [c for c in sweep.scan(older_than_days=30).candidates if c.store == directory.name]

    assert candidates and candidates[0].age_days >= 89


# ── unreadable ────────────────────────────────────────────────────────────


def test_unreadable_session_state_is_listed_but_survives_a_plain_purge():
    directory = _session_ledger("chat-7-torn", phase="done", age_days=90)
    (directory / "state.json").write_text("{not json", encoding="utf-8")
    _backdate(directory / "state.json", 90)
    _backdate(directory, 90)

    report = sweep.scan(older_than_days=30)
    listed = [c for c in report.candidates if c.store == directory.name]
    assert listed and listed[0].unreadable
    assert "unreadable" in "\n".join(sweep.render(report))
    assert not report.removable, "an unreadable record is not a plain-purge candidate"

    kept = sweep.purge(report)
    assert directory.is_dir()
    assert {c.store for c in kept.skipped_unreadable} == {directory.name}

    gone = sweep.purge(sweep.scan(older_than_days=30), include_unreadable=True)
    assert {c.store for c in gone.removed} == {directory.name}
    assert not directory.exists()


def test_a_torn_item_record_makes_the_whole_conductor_unreadable():
    """``list_work_items`` SKIPS an unreadable item, so "no open items" must not
    be provable by damaging one."""
    item_id = _work_item()
    wl.item_path(CONDUCTOR, item_id).write_text("{tor", encoding="utf-8")
    directory = wl.conductor_dir(CONDUCTOR)
    _backdate(directory / "conductor.json", 90)
    _backdate_items(directory, 90)

    report = sweep.scan(older_than_days=30)
    listed = [c for c in report.candidates if c.store == directory.name]

    assert listed and listed[0].unreadable
    assert not report.removable
    sweep.purge(report)
    assert directory.is_dir()


def test_a_recent_unreadable_item_keeps_an_old_conductor_inside_the_window():
    """An unreadable item carries no stamp the census can read and is written
    without touching the header, so a crash-torn write into an old conductor would
    be invisible to the window and deletable under ``--purge-unreadable`` the
    moment it landed. The age reads the newest write under ``items/`` by mtime,
    in the scanner and in the store's locked recheck alike."""
    from datetime import timedelta

    directory = _work_ledger()  # closed item and header both 90 days old
    torn = directory / "items" / "it_0badf00d.json"
    torn.write_text("{tor", encoding="utf-8")  # written NOW, unreadable

    report = sweep.scan(older_than_days=30)
    assert directory.name not in _stores(report), "a fresh write is activity"

    _backdate(directory / "conductor.json", 90)
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.purge_conductor(CONDUCTOR, allow_unreadable=True, idle_for=timedelta(days=30))
    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
    assert "retention window" in str(caught.value)
    assert torn.exists()


@pytest.mark.parametrize("readable", [False, True])
def test_a_store_without_a_breadcrumb_is_reported_and_never_purged(readable):
    """Neither delete primitive can be aimed at a store whose key is unknown, so
    the sweep names it for a human instead of guessing -- and says "no
    breadcrumb", not "path mismatch", whether or not the record itself reads."""
    directory = _session_ledger("chat-11-anon", phase="done", age_days=90)
    (directory / "slot_key").unlink()
    if not readable:
        (directory / "state.json").write_text("{", encoding="utf-8")
        _backdate(directory / "state.json", 90)
    _backdate(directory, 90)

    report = sweep.scan(older_than_days=30)
    listed = [c for c in report.candidates if c.store == directory.name]
    assert listed and not listed[0].purgeable
    assert "no slot_key breadcrumb" in listed[0].reason
    # ``unreadable`` is a statement about the RECORD: a store whose record reads
    # but whose name is missing is report-only, not corruption.
    assert listed[0].unreadable is (not readable)
    assert (listed[0] in report.report_only) is readable

    result = sweep.purge(report, include_unreadable=True)
    assert {c.store for c in result.skipped_unaddressable} == {directory.name}
    assert directory.is_dir()


# ── the sweep infers nothing about sessions ───────────────────────────────


def test_an_in_flight_session_ledger_is_kept_at_any_age():
    """The in-flight rule has no exception. "The session is gone" would be a disk
    inference, the record is irreplaceable, and this module deliberately knows
    nothing about sessions — so a nine-hundred-day-old in-flight ledger is kept."""
    directory = _session_ledger("chat-12-gone", phase="implementing", age_days=900)

    assert directory.name not in _stores(sweep.scan(older_than_days=30))


def test_a_conductor_with_no_items_is_kept_at_any_age():
    """An empty conductor is finished-LOOKING, not finished: the same shape is a
    ledger opened seconds ago and one whose creator died before dispatching, and
    no lock can hold "its session is gone" still through a delete. It is counted
    as kept, never listed, and a purge over that report cannot reach it."""
    wl.ensure_conductor(CONDUCTOR, goal="never dispatched")
    directory = wl.conductor_dir(CONDUCTOR)
    _backdate(directory / "conductor.json", 900)

    report = sweep.scan(older_than_days=30)

    assert directory.name not in _stores(report)
    assert report.kept == 1
    result = sweep.purge(report, include_unreadable=True)
    assert not result.removed
    assert directory.is_dir()
    assert wl.read_conductor(CONDUCTOR).goal == "never dispatched"


# ── stores that are absent or damaged must not crash a report ─────────────


def test_scan_is_silent_on_a_machine_with_no_ledgers():
    report = sweep.scan(older_than_days=30)
    assert report.candidates == () and report.kept == 0
    assert "no ledger is older than the threshold" in "\n".join(sweep.render(report))


def test_the_bindings_directory_is_not_mistaken_for_a_conductor():
    _work_ledger()
    wl.bindings_dir().mkdir(parents=True, exist_ok=True)

    report = sweep.scan(older_than_days=30)

    assert "bindings" not in _stores(report)


# ── one owner for the window ──────────────────────────────────────────────


def test_the_cli_default_window_comes_from_the_module(monkeypatch, capsys):
    """``--older-than-days`` defaults to ``None`` and the module resolves it, so
    the CLI holds no second literal that could drift from the module's."""
    assert sweep.DEFAULT_OLDER_THAN_DAYS == 30
    seen: dict[str, float] = {}

    def _fake_scan(*, older_than_days):
        seen["window"] = older_than_days
        return sweep.SweepReport((), 0, older_than_days)

    monkeypatch.setattr(sweep, "scan", _fake_scan)
    sweep.run_command(purge=False, older_than_days=None, purge_unreadable=False)
    capsys.readouterr()

    assert seen["window"] == sweep.DEFAULT_OLDER_THAN_DAYS


def test_the_printed_purge_hint_carries_the_windows_the_preview_used(capsys):
    """A bare ``--purge`` in the hint would re-scan at the default window, so an
    operator who previewed with a larger, more conservative window and followed
    the printed command would delete the ledgers between the two windows -- ones
    the report they read never listed. The hint repeats the window verbatim."""
    _work_ledger(age_days=200)

    sweep.run_command(purge=False, older_than_days=90, purge_unreadable=False)

    out = capsys.readouterr().out
    assert "Purge them with: kirocrew ledger-sweep --purge --older-than-days 90" in out
    assert "--purge\n" not in out, "never a bare --purge"


def test_the_sweep_is_its_own_command_and_doctor_stays_read_only(monkeypatch, tmp_path, capsys):
    """``doctor`` is read-only by its own contract and ``--purge`` is irreversible,
    so the sweep is a top-level command: its flags belong to it alone, and a
    modifier handed to ``doctor`` is an argparse error rather than a health pass
    that exits 0 and reads like a purge that found nothing."""
    import sys

    from kiro_crew.cli import main

    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    ran: list[dict[str, object]] = []
    monkeypatch.setattr(sweep, "run_command", lambda **kwargs: ran.append(kwargs))

    monkeypatch.setattr(sys, "argv", ["kirocrew", "ledger-sweep", "--older-than-days", "7"])
    main()
    assert ran == [{"purge": False, "older_than_days": 7.0, "purge_unreadable": False}]

    monkeypatch.setattr(sys, "argv", ["kirocrew", "doctor", "--purge"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments: --purge" in err
    assert len(ran) == 1, "doctor never reaches the sweep"


# ── damage is classified last ──────────────────────────────────────────────


def test_a_torn_item_beside_an_open_one_keeps_the_conductor():
    """One open item outranks every other reading, damage included.

    Classifying damage first would let ONE torn file carry a live open item —
    and the work its worker is still reporting against — into the deletion
    ``--purge-unreadable`` authorises.
    """
    open_item = _work_item()
    torn = wl.apply_conductor_action(
        CONDUCTOR, "create", title="second", acceptance={"kind": "human_approval"}
    )["item"].item_id
    wl.item_path(CONDUCTOR, torn).write_text("{tor", encoding="utf-8")
    directory = wl.conductor_dir(CONDUCTOR)
    _backdate(directory / "conductor.json", 90)

    report = sweep.scan(older_than_days=30)

    assert directory.name not in _stores(report)
    assert wl.read_work_item(CONDUCTOR, open_item) is not None
    result = sweep.purge(report, include_unreadable=True)
    assert not result.removed
    assert directory.is_dir()


def test_a_freshly_written_ledger_that_reads_as_damaged_is_left_alone():
    """A file being replaced right now can itself read as unreadable — a Windows
    read of a file another handle holds open raises — so the age gate runs before
    damage is classified, measured from the directory ``atomic_write`` renames
    into."""
    directory = _session_ledger("chat-16-mid-write", phase="done", age_days=90)
    (directory / "state.json").write_text("{half", encoding="utf-8")
    _backdate(directory / "state.json", 90)
    # Directory mtime stays NOW: this ledger was just written to.

    report = sweep.scan(older_than_days=30)

    assert directory.name not in _stores(report)
    sweep.purge(report, include_unreadable=True)
    assert directory.is_dir()


# ── the report is re-derived before the delete ─────────────────────────────


def test_a_ledger_reopened_after_the_scan_is_not_purged():
    """A report is a snapshot and the gateway keeps running while it is read, so
    the verdict is re-derived immediately before the delete."""
    stale = _session_ledger("chat-17-reopened", phase="done", age_days=90)
    other = _session_ledger("chat-18-still-done", phase="done", age_days=90)

    report = sweep.scan(older_than_days=30)
    assert {stale.name, other.name} <= _stores(report)

    # The session came back to life between the report and the purge.
    sl.record("chat-17-reopened", phase="implementing", event="resumed", event_kind="phase")

    result = sweep.purge(report)

    assert {c.store for c in result.stale} == {stale.name}
    assert {c.store for c in result.removed} == {other.name}
    assert stale.is_dir(), "a reopened ledger must survive a stale report"
    assert not other.exists()
    assert "changed since the scan" in "\n".join(sweep.render(report, purged=result))


def test_a_conductor_that_opened_an_item_after_the_scan_is_not_purged():
    directory = _work_ledger()

    report = sweep.scan(older_than_days=30)
    assert directory.name in _stores(report)

    wl.apply_conductor_action(
        CONDUCTOR, "create", title="new round", acceptance={"kind": "human_approval"}
    )

    result = sweep.purge(report)

    assert {c.store for c in result.stale} == {directory.name}
    assert not result.removed
    assert directory.is_dir()


# ── the delete decision is re-taken under the store's own lock ─────────────


def test_an_item_created_after_the_scan_is_refused_by_the_store_itself():
    """The re-scan is a cheap filter; the census inside ``conductor_lock`` is the
    binding check. ``_create_item`` holds that same lock across its whole
    transaction, so a conductor cannot mint an item while the census and the
    removal run.

    Proven by handing the store a report it agrees with and creating the item
    where only the lock can see it — the sweep's own re-scan is bypassed here on
    purpose, because it is not the check under test.
    """
    directory = _work_ledger()
    report = sweep.scan(older_than_days=30)
    candidate = next(c for c in report.candidates if c.store == directory.name)

    wl.apply_conductor_action(
        CONDUCTOR, "create", title="new round", acceptance={"kind": "human_approval"}
    )

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.purge_conductor(candidate.key, allow_unreadable=False, idle_for=timedelta(0))

    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
    assert directory.is_dir()


def test_a_session_ledger_resumed_after_the_scan_is_refused_under_its_lock():
    """Same property for the session half: ``purge_matching``'s guard re-reads the
    record inside that ledger's own ``_locked`` hold."""
    directory = _session_ledger("chat-19-resumed", phase="done", age_days=90)
    key = "chat-19-resumed"

    seen: list[str] = []

    def _guard(dir_path):
        # Runs under the hold: a resume that lands before this cannot be missed.
        seen.append(dir_path.name)
        state = json.loads((dir_path / "state.json").read_text(encoding="utf-8"))
        return state.get("phase") in sl.TERMINAL_PHASES

    sl.record(key, phase="implementing", event="resumed", event_kind="phase")
    removed = sl.purge_matching({key}, guard=_guard)

    assert seen == [directory.name], "the guard must run for the matched store"
    assert removed == 0
    assert directory.is_dir()


def test_the_guard_deletes_only_the_store_it_cleared():
    directory = _session_ledger("chat-20-finished", phase="done", age_days=90)
    keeper = _session_ledger("chat-21-finished", phase="done", age_days=90)

    removed = sl.purge_matching({"chat-20-finished"}, guard=lambda _dir: True)

    assert removed == 1
    assert not directory.exists()
    assert keeper.is_dir()


# ── a malformed header is damage, not a finished ledger ────────────────────


@pytest.mark.parametrize("payload", ["{not json", "[]", ""])
def test_a_malformed_conductor_record_is_unreadable_not_finished(payload):
    """Presence is not readability. Treating only absence as damage let a torn
    header read as a finished ledger and be purged with a plain ``--purge``."""
    directory = _work_ledger()
    (directory / "conductor.json").write_text(payload, encoding="utf-8")
    _backdate(directory / "conductor.json", 90)

    report = sweep.scan(older_than_days=30)
    listed = [c for c in report.candidates if c.store == directory.name]

    assert listed and listed[0].unreadable
    assert not report.removable
    sweep.purge(report)
    assert directory.is_dir()


# ── printed text is terminal-safe ──────────────────────────────────────────


def test_control_characters_in_stored_text_never_reach_the_terminal():
    """Every printed field is read off disk. A ledger key is a session key — a
    channel puts arbitrary text in one — and a phase is model-written, so an
    unescaped line lets stored bytes repaint the summary the operator is about to
    act on."""
    directory = _session_ledger("chat-22-hostile", phase="done", age_days=90)
    # The breadcrumb is a SESSION KEY, and a channel puts arbitrary text in one.
    (directory / "slot_key").write_text(
        "chat-22\x1b[2K\rdone   0 candidate(s)\x1b]0;pwned\x07", encoding="utf-8"
    )

    rendered = "\n".join(sweep.render(sweep.scan(older_than_days=30)))

    assert "\x1b" not in rendered
    assert "\r" not in rendered
    assert "\x07" not in rendered
    assert "\ufffd" in rendered, "a stripped control must leave a visible mark"


def test_a_very_long_stored_field_is_elided_rather_than_printed_whole():
    directory = _session_ledger("chat-23-long", phase="done", age_days=90)
    (directory / "slot_key").write_text("x" * 400, encoding="utf-8")

    lines = sweep.render(sweep.scan(older_than_days=30))

    assert any("\u2026" in line for line in lines)
    assert all("x" * 400 not in line for line in lines)


# ── the window must be a real number of days ───────────────────────────────


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -1.0])
def test_a_window_that_is_not_a_finite_count_of_days_refuses(bad, capsys):
    """A bare ``window < 0`` is not enough: every comparison against NaN is false,
    so NaN passes the sign check and then makes every ``age < window`` false —
    which admits every ledger in both stores at once."""
    with pytest.raises(SystemExit) as exit_info:
        sweep.run_command(purge=True, older_than_days=bad, purge_unreadable=False)

    assert exit_info.value.code == 2
    assert "finite number of days" in capsys.readouterr().out


def test_nan_would_have_admitted_everything_without_the_finite_check():
    """The mechanism the guard above exists for, asserted directly on the rules:
    with NaN as the window every age comparison is false, so an in-flight ledger
    is the only thing a scan would still keep."""
    _session_ledger("chat-24-young", phase="done", age_days=0)

    assert not sweep.scan(older_than_days=30).candidates
    assert sweep.scan(
        older_than_days=float("nan")
    ).candidates, "NaN admits a zero-age terminal ledger — which is why the CLI refuses it"


def test_an_empty_conductor_with_a_torn_header_is_kept_not_offered_as_unreadable():
    """The no-items keep runs BEFORE the header is judged. ``--purge-unreadable``
    authorises deleting a record that cannot be READ, not one that was never
    shown to be finished -- and an empty conductor with a torn header is both, so
    it is kept, and the store refuses the same shape under its own lock."""
    from datetime import timedelta

    wl.ensure_conductor(CONDUCTOR, goal="never dispatched")
    directory = wl.conductor_dir(CONDUCTOR)
    (directory / "conductor.json").write_text("{torn", encoding="utf-8")
    _backdate(directory / "conductor.json", 90)
    _backdate(directory, 90)

    report = sweep.scan(older_than_days=30)

    assert directory.name not in _stores(report)
    assert report.kept == 1
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.purge_conductor(CONDUCTOR, allow_unreadable=True, idle_for=timedelta(days=30))
    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
    assert "no items" in str(caught.value)
    assert directory.is_dir()


def test_a_recently_truncated_session_record_in_an_old_directory_is_kept():
    """A damaged record has no stamp, so its age is a reading of mtimes -- and an
    in-place write (a truncation, a hand edit) freshens ``state.json`` without
    touching the directory, which only an atomic replace bumps. The newer of the
    two wins, in the scanner and in the guard alike."""
    directory = _session_ledger("chat-33-truncated", phase="done", age_days=90)
    _backdate(directory, 90)
    report = sweep.scan(older_than_days=30)
    assert directory.name in _stores(report)

    (directory / "state.json").write_text("", encoding="utf-8")  # truncated NOW, in place
    _backdate(directory, 90)  # the directory did not see it

    assert directory.name not in _stores(sweep.scan(older_than_days=30)), "young by its file"
    result = sweep.purge(report, include_unreadable=True)
    assert directory.is_dir(), "a readable candidate that no longer reads is stood down on"
    assert {c.store for c in result.stale} == {directory.name}


def test_the_unreadable_session_guard_reads_the_file_mtime_not_only_the_directory():
    """The guard's damaged branch applies the same two-mtime reading as the
    scanner: a store listed as old damage, then written to IN PLACE before the
    purge, is young by its file even though its directory never moved."""
    directory = _session_ledger("chat-34-torn-old", phase="done", age_days=90)
    (directory / "state.json").write_text("{torn", encoding="utf-8")
    _backdate(directory / "state.json", 90)
    _backdate(directory, 90)
    report = sweep.scan(older_than_days=30)
    listed = next(c for c in report.candidates if c.store == directory.name)
    assert listed.unreadable and listed.purgeable

    (directory / "state.json").write_text("{still torn", encoding="utf-8")  # in place, NOW
    _backdate(directory, 90)  # the directory did not see it

    result = sweep.purge(report, include_unreadable=True)

    assert directory.is_dir(), "the guard must read the file's own mtime"
    assert {c.store for c in result.stale} == {directory.name}


def test_a_lock_only_residue_is_listed_as_damage_and_removable_with_the_flag():
    """What a purge leaves when a lock file could not be unlinked -- on Windows a
    writer queued on the conductor lock holds its handle through the post-release
    unlink: a directory with a lock file, no header, no breadcrumb and no items. It
    holds nothing a conductor wrote, so the no-items keep does not apply; it is
    damage, listed for the operator, and ``--purge-unreadable`` removes it."""
    directory = _work_ledger()
    for name in ("conductor.json", "slot_key"):
        (directory / name).unlink()
    import shutil

    shutil.rmtree(directory / "items")
    (directory / ".lock").touch()
    _backdate(directory, 90)
    _backdate(directory / ".lock", 90)

    report = sweep.scan(older_than_days=30)
    listed = [c for c in report.candidates if c.store == directory.name]
    assert listed and listed[0].unreadable and not listed[0].purgeable
    assert (
        "no conductor record" in listed[0].reason and "no slot_key breadcrumb" in listed[0].reason
    )

    # With its breadcrumb still there it is addressable, and the flag removes it.
    (directory / "slot_key").write_text(CONDUCTOR + "\n", encoding="utf-8")
    _backdate(directory / "slot_key", 90)
    _backdate(directory, 90)
    report = sweep.scan(older_than_days=30)
    listed = [c for c in report.candidates if c.store == directory.name]
    assert listed and listed[0].unreadable and listed[0].purgeable
    assert not sweep.purge(report).removed, "a plain purge leaves damage alone"
    assert {c.store for c in sweep.purge(report, include_unreadable=True).removed} == {
        directory.name
    }
    assert not directory.exists()


def test_the_unreadable_session_guard_refuses_a_store_written_to_after_the_scan():
    """The unreadable branch of the guard applies the scanner's age gate for a
    damaged record -- the DIRECTORY mtime -- and not only "still damaged": a
    record being replaced right now reads as damaged, and its directory is fresh
    because ``atomic_write`` renames into it."""
    directory = _session_ledger("chat-32-torn", phase="done", age_days=90)
    (directory / "state.json").write_text("{torn", encoding="utf-8")
    _backdate(directory / "state.json", 90)
    _backdate(directory, 90)
    report = sweep.scan(older_than_days=30)
    listed = next(c for c in report.candidates if c.store == directory.name)
    assert listed.unreadable and listed.purgeable

    # Between the report and the purge a writer lands a file in the store: the
    # record still reads as torn, but the directory was written to just now.
    (directory / "state.json.tmp").write_text("{", encoding="utf-8")
    assert (directory / "state.json").read_text(encoding="utf-8") == "{torn"

    result = sweep.purge(report, include_unreadable=True)

    assert directory.is_dir(), "a damaged store written to since the scan is live"
    assert {c.store for c in result.stale} == {directory.name}


def test_a_header_that_tore_after_the_scan_is_refused_by_a_plain_purge():
    """The scanner lists a store whose header does not read as ``unreadable``,
    which a plain purge skips. A header that tears between the report and the
    purge must meet the same refusal inside the store's lock, or the report's
    "finished" would be trusted over the store's own account of itself."""
    directory = _work_ledger()
    report = sweep.scan(older_than_days=30)
    assert directory.name in _stores(report)

    (directory / "conductor.json").write_text("{torn", encoding="utf-8")
    _backdate(directory / "conductor.json", 90)  # not activity -- damage

    plain = sweep.purge(report)
    assert directory.is_dir(), "a plain purge must not delete a store whose header no longer reads"
    assert {c.store for c in plain.stale} == {directory.name}

    with_flag = sweep.purge(report, include_unreadable=True)
    assert {c.store for c in with_flag.removed} == {directory.name}
    assert not directory.exists()


# ── a link is never followed ───────────────────────────────────────────────


@pytest.mark.skipif(not IS_POSIX, reason="symlink creation needs no privilege on POSIX")
def test_a_linked_items_directory_is_damage_and_the_purge_never_follows_it(tmp_path):
    """An ``items/`` that is a link names another directory's files as this
    store's items. The census reports it as damage, the store refuses the purge
    outright, and the files behind the link are untouched -- whatever flags are
    given."""
    import shutil
    from datetime import timedelta

    directory = _work_ledger()
    elsewhere = tmp_path / "elsewhere"
    shutil.move(str(directory / "items"), str(elsewhere))
    (directory / "items").symlink_to(elsewhere, target_is_directory=True)
    precious = elsewhere / "precious.txt"
    precious.write_text("not a ledger's to delete", encoding="utf-8")
    _backdate(directory / "conductor.json", 90)

    census = wl.census_items(directory)
    assert "is a link" in census.damage

    report = sweep.scan(older_than_days=30)
    listed = [c for c in report.candidates if c.store == directory.name]
    assert listed and listed[0].unreadable and "is a link" in listed[0].reason

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.purge_conductor(CONDUCTOR, allow_unreadable=True, idle_for=timedelta(0))
    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
    result = sweep.purge(report, include_unreadable=True)
    assert not result.removed
    assert precious.read_text(encoding="utf-8") == "not a ledger's to delete"
    assert (directory / "items").is_symlink() and directory.is_dir()


@pytest.mark.skipif(not IS_POSIX, reason="symlink creation needs no privilege on POSIX")
def test_a_linked_store_directory_is_reported_and_never_aimed_at(tmp_path):
    """Both halves: a store directory that is itself a link is listed for the
    operator as ``report`` and is never purged -- the scanners never classify
    through it, and both primitives refuse it."""
    import shutil

    session = _session_ledger("chat-35-linked", phase="done", age_days=90)
    work = _work_ledger()
    moved = {}
    for kind, directory in (("session", session), ("work", work)):
        target = tmp_path / f"{kind}-target"
        shutil.move(str(directory), str(target))
        directory.symlink_to(target, target_is_directory=True)
        moved[kind] = target
        _backdate(target, 90)

    report = sweep.scan(older_than_days=30)
    listed = {c.store: c for c in report.candidates}
    for directory in (session, work):
        assert directory.name in listed, "seen, so an operator can look"
        assert not listed[directory.name].purgeable and not listed[directory.name].unreadable
        assert "is a link" in listed[directory.name].reason

    result = sweep.purge(report, include_unreadable=True)

    assert not result.removed
    assert {c.store for c in result.skipped_unaddressable} == {session.name, work.name}
    for target in moved.values():
        assert target.is_dir() and any(target.iterdir()), "the target is untouched"


# ── a store is only deletable through the key it actually lives under ──────


def test_a_copied_work_store_never_sends_the_purge_at_the_canonical_ledger():
    """Both primitives are aimed by KEY and resolve the directory themselves, so a
    copy carrying another ledger's ``slot_key`` would send the delete at the
    canonical store — one the report never listed and the operator never saw."""
    import shutil

    canonical = _work_ledger()
    copy = canonical.parent / f"{canonical.name}-copy"
    shutil.copytree(canonical, copy)
    assert (copy / "slot_key").read_text(encoding="utf-8").strip() == CONDUCTOR

    report = sweep.scan(older_than_days=30)
    listed = {c.store: c for c in report.candidates}

    assert copy.name in listed, "the copy is reported, so an operator can see it"
    assert not listed[copy.name].purgeable
    assert not listed[copy.name].unreadable, "its record reads; only its name is wrong"
    assert listed[copy.name] in report.report_only
    assert "does not match its own slot_key" in listed[copy.name].reason
    assert listed[canonical.name].purgeable, "the real store is unaffected"
    rendered = "\n".join(sweep.render(report))
    assert f"  report     work    {copy.name}" in rendered
    assert "1 report-only" in rendered and "0 unreadable" in rendered

    result = sweep.purge(report, include_unreadable=True)

    assert {c.store for c in result.skipped_unaddressable} == {copy.name}
    assert copy.is_dir(), "a mismatched store is never deleted, even with the flag"
    assert {c.store for c in result.removed} == {canonical.name}


def test_a_copied_session_store_is_reported_and_never_purged():
    import shutil

    canonical = _session_ledger("chat-25-real", phase="done", age_days=90)
    copy = canonical.parent / f"{canonical.name}-copy"
    shutil.copytree(canonical, copy)

    report = sweep.scan(older_than_days=30)
    listed = {c.store: c for c in report.candidates}

    assert not listed[copy.name].purgeable
    assert not listed[copy.name].unreadable and listed[copy.name] in report.report_only
    assert listed[canonical.name].purgeable

    result = sweep.purge(report, include_unreadable=True)

    assert {c.store for c in result.skipped_unaddressable} == {copy.name}
    assert copy.is_dir()
    assert not canonical.exists()


def test_purge_re_asserts_the_path_identity_on_a_hand_built_report():
    """The scanners refuse a mismatched store, and ``purge`` checks again at the
    last moment a caller-supplied report could disagree with the store's naming."""
    directory = _work_ledger()
    report = sweep.scan(older_than_days=30)
    real = next(c for c in report.candidates if c.store == directory.name)
    forged = sweep.Candidate(
        kind=real.kind,
        store=real.store,
        key=real.key,
        detail=real.detail,
        age_days=real.age_days,
        reason=real.reason,
        unreadable=False,
        path=directory.parent / "somewhere-else",
    )
    hand_built = sweep.SweepReport((forged,), 0, 30.0)

    result = sweep.purge(hand_built)

    assert {c.store for c in result.skipped_unaddressable} == {directory.name}
    assert directory.is_dir()


# ── an items directory that cannot be read is damage, not zero items ───────

_CAN_CHMOD = IS_POSIX and hasattr(os, "geteuid") and os.geteuid() != 0


@pytest.mark.skipif(not _CAN_CHMOD, reason="chmod 000 does not deny root or Windows")
def test_an_unreadable_items_directory_is_damage_not_a_finished_ledger():
    """Read as "zero items" this selects the ORDINARY purge, which removes
    ``conductor.json`` and leaves the item data it could not see standing — a
    ledger destroyed down to the records that made it one."""
    directory = _work_ledger()
    items = directory / "items"
    items.chmod(0o000)
    try:
        report = sweep.scan(older_than_days=30)
        listed = [c for c in report.candidates if c.store == directory.name]

        assert listed and listed[0].unreadable
        assert "items directory could not be read" in listed[0].reason
        assert not report.removable, "a plain --purge must not select it"

        sweep.purge(report)
        assert (directory / "conductor.json").exists(), "the header must survive"
    finally:
        items.chmod(0o700)


@pytest.mark.skipif(not _CAN_CHMOD, reason="chmod 000 does not deny root or Windows")
def test_the_store_refuses_an_unreadable_items_directory_under_its_own_lock():
    """Same property one layer down, where the binding decision is made."""
    directory = _work_ledger()
    items = directory / "items"
    items.chmod(0o000)
    try:
        with pytest.raises(wl.WorkLedgerError) as caught:
            wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0))
        assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
        assert "items directory" in str(caught.value)
        assert (directory / "conductor.json").exists()
    finally:
        items.chmod(0o700)


def test_an_enumeration_error_is_damage_in_the_census(monkeypatch):
    """Portable half of the two tests above: the one census the scanner and the
    locked recheck share must not fold an enumeration failure into a count."""
    directory = _work_ledger()
    real_scandir = os.scandir

    def _refuse(path, *args, **kwargs):
        if str(path).endswith("items"):
            raise OSError(13, "Permission denied")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", _refuse)

    census = wl.census_items(directory)
    assert census.damage
    assert (census.open_items, census.closed, census.unreadable, census.newest_closed_at) == (
        0,
        0,
        0,
        "",
    )

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(0))
    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED


def test_a_conductor_with_no_items_directory_is_not_reported_as_damaged():
    """An ABSENT items directory is not damage — a conductor that never created an
    item has none, and reporting that as damage would make every empty conductor
    unreadable."""
    wl.ensure_conductor(CONDUCTOR, goal="never dispatched")
    directory = wl.conductor_dir(CONDUCTOR)
    assert not (directory / "items").exists()
    _backdate(directory / "conductor.json", 90)

    census = wl.census_items(directory)
    report = sweep.scan(older_than_days=30)

    assert census.damage == "" and census.unreadable == 0
    assert not any(c.unreadable for c in report.candidates)
    assert directory.name not in _stores(report) and report.kept == 1


# ── a ledger's age is its last write, and the guard applies the whole rule ─


def test_a_terminal_session_ledger_written_to_recently_is_not_old():
    """``finished_at`` is re-stamped only by a terminal PHASE write. A goal,
    artifact or event-only update to an already-terminal ledger leaves the stamp
    alone while rewriting ``state.json``, so a record touched a minute ago would
    read as ninety days idle from the stamp. The age is the newer of the two."""
    directory = _session_ledger("chat-30-touched", phase="done", age_days=90)
    assert directory.name in _stores(sweep.scan(older_than_days=30))

    sl.record("chat-30-touched", goal="a note added after the fact")
    state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "done"
    assert (datetime.now().astimezone() - datetime.fromisoformat(state["finished_at"])).days >= 89

    assert directory.name not in _stores(sweep.scan(older_than_days=30))


def test_the_session_guard_refuses_a_terminal_ledger_touched_after_the_scan():
    """The same reading under the lock: a terminal ledger written to between the
    report and the purge is young by its last write, whatever its stamp says."""
    directory = _session_ledger("chat-31-touched", phase="done", age_days=90)
    report = sweep.scan(older_than_days=30)
    assert directory.name in _stores(report)

    sl.record("chat-31-touched", artifacts={"pr": "https://example.invalid/pr/1"})

    result = sweep.purge(report)

    assert directory.is_dir(), "the guard must refuse a ledger written to since the scan"
    assert {c.store for c in result.stale} == {directory.name}


def test_the_newest_closed_at_is_chosen_as_an_instant_not_a_string():
    """Two local-offset stamps can sort lexically in the wrong order across a DST
    change; choosing the string maximum would age the conductor from the OLDER
    instant and let it cross the threshold early."""
    first = _work_item()
    second = wl.apply_conductor_action(
        CONDUCTOR, "create", title="second", acceptance={"kind": "human_approval"}
    )["item"].item_id
    for item_id in (first, second):
        wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    # Lexically "2026-01-10T09:00:00+02:00" > "2026-01-10T08:30:00-05:00", but
    # 07:00Z is EARLIER than 13:30Z.
    stamps = {first: "2026-01-10T09:00:00+02:00", second: "2026-01-10T08:30:00-05:00"}
    for item_id, stamp in stamps.items():
        path = wl.item_path(CONDUCTOR, item_id)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["closed_at"] = stamp
        path.write_text(json.dumps(record), encoding="utf-8")

    census = wl.census_items(wl.conductor_dir(CONDUCTOR))

    assert census.newest_closed_at == stamps[second], "the chronologically later stamp wins"


def test_the_session_guard_refuses_a_ledger_that_finished_again_recently():
    """The guard applies the WHOLE rule -- terminal AND old -- so a session that
    resumed and finished again inside the purge window is refused under its lock,
    even though its phase is terminal."""
    directory = _session_ledger("chat-26-refinished", phase="done", age_days=90)
    report = sweep.scan(older_than_days=30)
    assert directory.name in _stores(report)

    # Between the report and the purge: resumed, then done again -- finished_at
    # is re-stamped to now by ``session_ledger.record``.
    sl.record("chat-26-refinished", phase="implementing", event="resumed", event_kind="phase")
    sl.record("chat-26-refinished", phase="done", event="shipped", event_kind="phase")
    state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "done"

    result = sweep.purge(report)

    assert directory.is_dir(), "the guard must refuse a terminal-but-young ledger"
    assert {c.store for c in result.stale} == {directory.name}


def test_a_work_store_without_a_breadcrumb_is_reported_and_never_purged():
    """The breadcrumb is the ONLY source of a store's key. Recovering it from
    ``conductor.json`` would let a store whose breadcrumb write failed be purged,
    against the rule that a store the sweep cannot name is reported, not deleted."""
    directory = _work_ledger()
    (directory / "slot_key").unlink()

    report = sweep.scan(older_than_days=30)
    listed = [c for c in report.candidates if c.store == directory.name]

    assert listed and not listed[0].purgeable
    assert listed[0].key == ""
    assert "no slot_key breadcrumb" in listed[0].reason

    result = sweep.purge(report, include_unreadable=True)

    assert {c.store for c in result.skipped_unaddressable} == {directory.name}
    assert directory.is_dir()


def test_a_damaged_copied_session_store_is_not_offered_as_purgeable():
    """Completes the path-identity rollout on the fourth branch: a damaged session
    store whose key resolves elsewhere is not even OFFERED under
    --purge-unreadable, rather than relying on purge()'s backstop."""
    import shutil

    canonical = _session_ledger("chat-27-damaged-real", phase="done", age_days=90)
    copy = canonical.parent / f"{canonical.name}-copy"
    shutil.copytree(canonical, copy)
    for directory in (canonical, copy):
        (directory / "state.json").write_text("{torn", encoding="utf-8")
        _backdate(directory / "state.json", 90)
        _backdate(directory, 90)

    report = sweep.scan(older_than_days=30)
    listed = {c.store: c for c in report.candidates}

    assert listed[copy.name].unreadable and not listed[copy.name].purgeable
    assert listed[canonical.name].unreadable and listed[canonical.name].purgeable


# ── a conductor's own header activity counts as activity ───────────────────


def test_a_goal_round_bump_on_old_closed_items_keeps_the_conductor():
    """The newest item close alone is not the ledger's age: ``goal`` rewrites the
    header with no item involved, and a conductor that just bumped its round on a
    set of old closed items is a live conductor about to create."""
    directory = _work_ledger()  # one item closed 90 days ago, header backdated
    assert directory.name in _stores(sweep.scan(older_than_days=30))

    wl.apply_conductor_action(CONDUCTOR, "goal", round_number=7)

    assert directory.name not in _stores(sweep.scan(older_than_days=30))


def test_the_store_refuses_a_recently_touched_header_under_its_own_lock():
    """Same rule one layer down, where the binding decision is made: the sweep
    passes its window and ``purge_conductor`` re-applies it against the newer of
    the newest close and the header mtime."""
    from datetime import timedelta

    directory = _work_ledger()
    wl.apply_conductor_action(CONDUCTOR, "goal", round_number=7)  # header mtime = now

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(days=30))

    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
    assert "retention window" in str(caught.value)
    assert directory.is_dir()
    # And with the header aged back out, the same call goes through.
    _backdate(directory / "conductor.json", 90)
    assert (
        wl.purge_conductor(CONDUCTOR, allow_unreadable=False, idle_for=timedelta(days=30)) is True
    )


def test_a_report_whose_conductor_was_bumped_after_the_scan_is_stood_down_on():
    """End to end through purge(): the pre-filter drops it, and even a hand-built
    report that skips the pre-filter is refused by the store."""
    directory = _work_ledger()
    report = sweep.scan(older_than_days=30)
    assert directory.name in _stores(report)

    wl.apply_conductor_action(CONDUCTOR, "goal", round_number=3)

    result = sweep.purge(report)
    assert {c.store for c in result.stale} == {directory.name}
    assert directory.is_dir()


def test_the_store_window_recheck_holds_on_a_stale_report():
    """The binding check is the store's, under its lock, with the report's window
    passed down: a report built before a header touch is refused by the store."""
    directory = _work_ledger()
    report = sweep.scan(older_than_days=30)
    assert directory.name in _stores(report)

    wl.apply_conductor_action(CONDUCTOR, "goal", round_number=3)  # header mtime = now

    result = sweep.purge(report)

    assert {c.store for c in result.stale} == {directory.name}, "the store refused it"
    assert directory.is_dir()


# ── an unreadable ROOT is an error, never an empty report ──────────────────


@pytest.mark.skipif(not _CAN_CHMOD, reason="chmod 000 does not deny root or Windows")
@pytest.mark.parametrize("which", ["session", "work"])
def test_an_unreadable_ledger_root_raises_instead_of_reporting_nothing_to_clean(which):
    """A clean empty report over a store the sweep could not see is a false
    "nothing to clean". Only an ABSENT root is genuinely empty."""
    _session_ledger("chat-60-x", phase="done", age_days=90)
    _work_ledger()
    root = sl._ledger_root() if which == "session" else wl._work_ledger_root()
    root.chmod(0o000)
    try:
        with pytest.raises(OSError):
            sweep.scan(older_than_days=30)
    finally:
        root.chmod(0o700)


def test_an_absent_ledger_root_is_an_empty_report():
    """The other half: a machine that never recorded a ledger has nothing to sweep,
    and that must not read as an error."""
    assert not sl._ledger_root().exists() and not wl._work_ledger_root().exists()
    report = sweep.scan(older_than_days=30)
    assert report.candidates == () and report.kept == 0


def test_the_cli_reports_an_unreadable_root_as_a_failure(monkeypatch, capsys):
    def _boom(**_kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(sweep, "scan", _boom)
    with pytest.raises(SystemExit) as exit_info:
        sweep.run_command(purge=False, older_than_days=None, purge_unreadable=False)
    assert exit_info.value.code == 1
    assert "could not read the ledger stores" in capsys.readouterr().out
