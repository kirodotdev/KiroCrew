"""Ledger cleanup sweep — the refuse-safe rules, one test per rule.

Pins what docs/system-specs/modules/session-work-ledger.md §2 "Cleanup" states:
a dry run lists and deletes nothing; a purge removes only what the report named;
an in-flight session ledger and a conductor holding an open item are never
candidates at any age; the threshold is a boundary rather than a hint; an
unreadable record is listed but survives a plain purge; the orphan check is
opt-in and still pays the threshold; and the window has one owner, so the CLI
default cannot drift from the module's.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

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
    return directory


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
    assert "2 candidate(s)" in rendered


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

    report = sweep.scan(older_than_days=30)
    listed = [c for c in report.candidates if c.store == directory.name]

    assert listed and listed[0].unreadable
    assert not report.removable
    sweep.purge(report)
    assert directory.is_dir()


def test_a_store_without_a_breadcrumb_is_reported_and_never_purged():
    """Neither delete primitive can be aimed at a store whose key is unknown, so
    the sweep names it for a human instead of guessing."""
    directory = _session_ledger("chat-11-anon", phase="done", age_days=90)
    (directory / "slot_key").unlink()
    (directory / "state.json").write_text("{", encoding="utf-8")
    _backdate(directory, 90)

    report = sweep.scan(older_than_days=30)
    listed = [c for c in report.candidates if c.store == directory.name]
    assert listed and not listed[0].purgeable
    assert "no slot_key breadcrumb" in listed[0].reason

    result = sweep.purge(report, include_unreadable=True)
    assert {c.store for c in result.skipped_unaddressable} == {directory.name}
    assert directory.is_dir()


# ── orphans ───────────────────────────────────────────────────────────────


def test_an_in_flight_session_ledger_is_kept_even_when_its_session_is_gone():
    """``--include-orphans`` does NOT widen the session half. "Gone" is a disk
    inference and the record is irreplaceable, so the in-flight rule has no
    exception — this is the invariant the docstring, the spec and the PR table
    all state."""
    directory = _session_ledger("chat-12-gone", phase="implementing", age_days=900)

    assert directory.name not in _stores(sweep.scan(older_than_days=30))
    assert directory.name not in _stores(sweep.scan(older_than_days=30, include_orphans=True))


def test_a_conductor_with_no_items_is_only_a_candidate_as_an_orphan():
    """An empty conductor is finished-LOOKING, not finished: the same shape is a
    ledger opened seconds ago and one whose creator died before dispatching. So
    emptiness counts only once the session is gone from this machine too."""
    wl.ensure_conductor(CONDUCTOR, goal="never dispatched")
    directory = wl.conductor_dir(CONDUCTOR)
    _backdate(directory / "conductor.json", 90)

    assert directory.name not in _stores(sweep.scan(older_than_days=30))
    with_flag = sweep.scan(older_than_days=30, include_orphans=True)
    assert directory.name in _stores(with_flag)
    assert "no items" in next(c.reason for c in with_flag.candidates if c.store == directory.name)


def test_a_conductor_key_with_a_transcript_is_not_an_orphan():
    """The check uses the real key-to-file mapping: the ledger key is stored
    stripped of its ``dashboard_`` prefix, and the transcript carries it."""
    from kiro_crew.config.paths import config_dir
    from kiro_crew.history import SESSIONS_DIR_NAME

    wl.ensure_conductor("chat-14-alive", goal="still around")
    directory = wl.conductor_dir("chat-14-alive")
    _backdate(directory / "conductor.json", 90)
    sessions = config_dir() / SESSIONS_DIR_NAME
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "dashboard_chat-14-alive.jsonl").write_text("{}\n", encoding="utf-8")

    assert directory.name not in _stores(sweep.scan(older_than_days=30, include_orphans=True))


def test_a_conductor_key_in_the_session_map_is_not_an_orphan():
    from kiro_crew.config.paths import config_dir

    wl.ensure_conductor("chat-15-mapped", goal="bound to a live session")
    directory = wl.conductor_dir("chat-15-mapped")
    _backdate(directory / "conductor.json", 90)
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / "session_map.json").write_text(
        json.dumps({"dashboard:chat-15-mapped": {"sid": "abc"}}), encoding="utf-8"
    )

    assert directory.name not in _stores(sweep.scan(older_than_days=30, include_orphans=True))


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
    from kiro_crew import cli_doctor

    assert sweep.DEFAULT_OLDER_THAN_DAYS == 30
    seen: dict[str, float] = {}

    def _fake_scan(*, older_than_days, include_orphans):
        seen["window"] = older_than_days
        return sweep.SweepReport((), (), older_than_days, include_orphans)

    monkeypatch.setattr(sweep, "scan", _fake_scan)
    cli_doctor._ledger_sweep(
        purge=False, older_than_days=None, include_orphans=False, purge_unreadable=False
    )
    capsys.readouterr()

    assert seen["window"] == sweep.DEFAULT_OLDER_THAN_DAYS


def test_the_doctor_health_pass_does_not_run_in_sweep_mode(monkeypatch, capsys):
    """The sweep is a MODE, like ``--bundle``: it reports on stored state rather
    than on the health of the install, so it must not print the health report."""
    from kiro_crew import cli_doctor

    called: list[bool] = []
    monkeypatch.setattr(
        cli_doctor,
        "_ledger_sweep",
        lambda **kwargs: called.append(True),
    )
    cli_doctor._doctor(ledger_sweep_mode=True)

    assert called == [True]
    assert "Kiro Crew Doctor" not in capsys.readouterr().out


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
        wl.purge_conductor(candidate.key)

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
    removed = sl.purge_matching({key}, set(), lambda k: k, guard=_guard)

    assert seen == [directory.name], "the guard must run for the matched store"
    assert removed == 0
    assert directory.is_dir()


def test_the_guard_deletes_only_the_store_it_cleared():
    directory = _session_ledger("chat-20-finished", phase="done", age_days=90)
    keeper = _session_ledger("chat-21-finished", phase="done", age_days=90)

    removed = sl.purge_matching({"chat-20-finished"}, set(), lambda k: k, guard=lambda _dir: True)

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
    from kiro_crew import cli_doctor

    with pytest.raises(SystemExit) as exit_info:
        cli_doctor._ledger_sweep(
            purge=True, older_than_days=bad, include_orphans=False, purge_unreadable=False
        )

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


# ── the mode's flags do nothing without the mode ───────────────────────────


@pytest.mark.parametrize(
    "kwargs,flag",
    [
        ({"ledger_purge": True}, "--purge"),
        ({"ledger_older_than_days": 5.0}, "--older-than-days"),
        ({"ledger_include_orphans": True}, "--include-orphans"),
        ({"ledger_purge_unreadable": True}, "--purge-unreadable"),
    ],
)
def test_a_sweep_flag_without_the_mode_refuses(kwargs, flag, capsys):
    """Bare ``doctor --purge`` otherwise ran the health pass and exited 0, which
    reads exactly like a purge that found nothing to do."""
    from kiro_crew import cli_doctor

    with pytest.raises(SystemExit) as exit_info:
        cli_doctor._doctor(**kwargs)

    assert exit_info.value.code == 2
    out = capsys.readouterr().out
    assert flag in out
    assert "nothing was scanned" in out
    assert "Kiro Crew Doctor" not in out


def test_an_empty_conductor_whose_session_came_back_is_stood_down_on():
    """The one case the store's own locked census cannot catch: there are no items
    to refuse on, so a zero-item conductor is only protected by the sweep's
    re-derived report. Its session reappearing between the scan and the delete is
    exactly what makes it live again."""
    from kiro_crew.config.paths import config_dir
    from kiro_crew.history import SESSIONS_DIR_NAME

    wl.ensure_conductor(CONDUCTOR, goal="never dispatched")
    directory = wl.conductor_dir(CONDUCTOR)
    _backdate(directory / "conductor.json", 90)

    report = sweep.scan(older_than_days=30, include_orphans=True)
    assert directory.name in _stores(report)

    sessions = config_dir() / SESSIONS_DIR_NAME
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"dashboard_{CONDUCTOR}.jsonl").write_text("{}\n", encoding="utf-8")

    result = sweep.purge(report)

    assert {c.store for c in result.stale} == {directory.name}
    assert not result.removed
    assert directory.is_dir()


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
    assert listed[copy.name].unreadable
    assert "does not match its own slot_key" in listed[copy.name].reason
    assert listed[canonical.name].purgeable, "the real store is unaffected"

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
    hand_built = sweep.SweepReport((forged,), 0, 30.0, False)

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
            wl.purge_conductor(CONDUCTOR)
        assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED
        assert "items directory" in str(caught.value)
        assert (directory / "conductor.json").exists()
    finally:
        items.chmod(0o700)


def test_an_enumeration_error_is_damage_in_both_censuses(monkeypatch):
    """Portable half of the two tests above: neither census may fold an
    enumeration failure into a count."""
    directory = _work_ledger()
    real_scandir = os.scandir

    def _refuse(path, *args, **kwargs):
        if str(path).endswith("items"):
            raise OSError(13, "Permission denied")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", _refuse)

    open_items, closed, unreadable, newest, damage = sweep._item_census(directory)
    assert damage and (open_items, closed, unreadable, newest) == (0, 0, 0, "")

    locked_open, locked_unreadable, locked_damage = wl._census_locked(directory)
    assert locked_damage and (locked_open, locked_unreadable) == (0, 0)

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.purge_conductor(CONDUCTOR)
    assert caught.value.code == wl.CODE_LEDGER_NOT_FINISHED


def test_a_conductor_with_no_items_directory_is_not_reported_as_damaged():
    """An ABSENT items directory is not damage — a conductor that never created an
    item has none, and reporting that as damage would make every empty conductor
    unreadable."""
    wl.ensure_conductor(CONDUCTOR, goal="never dispatched")
    directory = wl.conductor_dir(CONDUCTOR)
    assert not (directory / "items").exists()
    _backdate(directory / "conductor.json", 90)

    report = sweep.scan(older_than_days=30, include_orphans=True)
    listed = [c for c in report.candidates if c.store == directory.name]

    assert listed and not listed[0].unreadable
    assert "no items" in listed[0].reason
