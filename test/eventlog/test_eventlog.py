"""Tests for the per-member append-only event log backend core."""

from __future__ import annotations

import json
import os
import threading

import pytest

from kiro_crew.crew_log.errors import CrewLogError
from kiro_crew.crew_log.schema import KIND_MEMBER
from kiro_crew.crew_log.store import crew_log_path
from kiro_crew.eventlog import types
from kiro_crew.eventlog.log import LogCorrupt, MemberLog
from kiro_crew.eventlog.members_projections import all_units
from kiro_crew.eventlog.projection import ProjectionRegistry
from kiro_crew.eventlog.service import MemberEventLogService


# ---------------------------------------------------------------------------
# MemberLog
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one.

    A member log is a ``member``-kind crew log now, so the store derives its path
    from the data home rather than taking one. Repointing the home is therefore
    what isolates a test, and it is the same fixture the crew log's own suites use.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _log(tmp_path, slug="alice"):
    return MemberLog(slug)


def test_create_writes_header_once(tmp_path):
    log = _log(tmp_path)
    log.create("Alice")
    log.load()
    assert log.header["type"] == "member"
    assert log.header["version"] == 1
    assert log.header["id"] == "alice"
    assert log.header["name"] == "Alice"
    assert isinstance(log.header["createdAt"], int)
    before = log.path.read_bytes()
    log.create("SomeoneElse")  # no-op
    assert log.path.read_bytes() == before


def test_append_assigns_seq_and_fsyncs(tmp_path):
    log = _log(tmp_path)
    log.create("Alice")
    e0 = log.append(types.MEMBER_MESSAGE, {"ts": 1.0, "preview": "hi"})
    e1 = log.append(types.MEMBER_MESSAGE, {"ts": 2.0, "preview": "yo"})
    assert e0["seq"] == 1
    assert e1["seq"] == 2
    assert e0["type"] == types.MEMBER_MESSAGE
    assert isinstance(e0["time"], int)

    fresh = MemberLog("alice")
    fresh.load()
    assert [e["seq"] for e in fresh.events] == [1, 2]


def test_interleaved_writers_on_the_same_file_keep_seq_contiguous(tmp_path):
    """Two independent MemberLog instances (as two OS processes) append to the
    same log without sharing in-memory state. Each append must re-read committed
    state under the store's own cross-process lock, so each writer sees the
    other's committed entries and takes the next seq -- otherwise both compute a
    seq from a stale view and commit a duplicate."""
    proc_a = MemberLog("alice")
    proc_a.create("Alice")
    # proc_b never shares proc_a's in-memory events list -- it is a separate
    # instance, the way a separate process's singleton would be.
    proc_b = MemberLog("alice")

    a0 = proc_a.append(types.MEMBER_MESSAGE, {"ts": 1.0, "preview": "a0"})
    b0 = proc_b.append(types.MEMBER_MESSAGE, {"ts": 2.0, "preview": "b0"})
    a1 = proc_a.append(types.MEMBER_MESSAGE, {"ts": 3.0, "preview": "a1"})
    b1 = proc_b.append(types.MEMBER_MESSAGE, {"ts": 4.0, "preview": "b1"})

    # Each append re-loaded under the lock, so the four seqs are contiguous and
    # distinct even though the writers alternated across instances.
    assert [a0["seq"], b0["seq"], a1["seq"], b1["seq"]] == [1, 2, 3, 4]

    # A cold reader parses all four in order (a duplicate seq would show up here
    # as a repeated or missing number).
    cold = MemberLog("alice")
    cold.load()
    assert [e["seq"] for e in cold.events] == [1, 2, 3, 4]
    assert [e["data"]["preview"] for e in cold.events] == ["a0", "b0", "a1", "b1"]


def test_append_rejects_unknown_type_and_unserializable(tmp_path):
    log = _log(tmp_path)
    log.create("Alice")
    # A type in a RESERVED namespace that is not in the vocabulary is a typo'd
    # built-in, not a contribution: a contributor's type is `<app>/<name>` with a
    # namespace the built-ins do not own (see types.is_contributed_event_type),
    # and an app cannot be named `member`.
    with pytest.raises(ValueError):
        log.append("member/bogus", {})
    # No namespace at all is refused on either rule.
    with pytest.raises(ValueError):
        log.append("bogus", {})
    with pytest.raises(ValueError):
        log.append(types.MEMBER_MESSAGE, {"x": {1, 2, 3}})  # set not JSON
    # File unchanged by the rejected writes (still just the header).
    fresh = MemberLog("alice")
    fresh.load()
    assert fresh.events == []


def test_load_torn_trailing_line_is_repaired(tmp_path):
    log = _log(tmp_path)
    log.create("Alice")
    log.append(types.MEMBER_MESSAGE, {"ts": 1.0, "preview": "hi"})
    committed = log.path.stat().st_size
    # Simulate a torn partial write: bytes with no trailing newline.
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write('{"type":"member/message","seq":1,"time":123,"dat')

    fresh = MemberLog("alice")
    fresh.load()  # must not raise
    assert [e["seq"] for e in fresh.events] == [1]
    assert fresh.path.stat().st_size == committed  # truncated back


def test_load_skips_a_damaged_committed_line_instead_of_refusing_the_file(tmp_path):
    """A damaged line costs a reader THAT line, not the whole log.

    The store this log is kept in makes that call inside one segment,
    deliberately, and it is the right one for a record whose purpose is to be
    readable after damage: refusing the file turns one unreadable entry into a
    member whose whole history is unopenable, and the entry is not recoverable
    either way. A gap ACROSS segments is still refused, because that is a missing
    file rather than a bad line.
    """
    log = _log(tmp_path)
    log.create("Alice")
    log.append(types.MEMBER_MESSAGE, {"ts": 1.0, "preview": "kept"})
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": types.MEMBER_MESSAGE, "seq": 5, "time": 1, "data": {}}) + "\n")
        fh.write("not json at all\n")

    fresh = MemberLog("alice")
    fresh.load()

    assert [e["seq"] for e in fresh.events] == [1]
    assert [e["data"]["preview"] for e in fresh.events] == ["kept"]
    # Neither damaged line was rewritten or dropped from the file: this layer
    # reads around them, it does not repair them.
    assert "not json at all" in log.path.read_text(encoding="utf-8")


def test_load_raises_corrupt_when_the_header_line_is_unreadable(tmp_path):
    """``LogCorrupt`` survives for the one case that really is unreadable.

    Without a header there is no proof the file belongs to this member, so every
    entry in it is unattributable -- which is the difference from a single damaged
    line. Callers catch this by name, so it stays this module's exception and
    wraps the store's refusal rather than replacing it.
    """
    log = _log(tmp_path)
    log.create("Alice")
    log.append(types.MEMBER_MESSAGE, {"ts": 1.0, "preview": "hi"})
    lines = log.path.read_text(encoding="utf-8").splitlines(keepends=True)
    log.path.write_text("garbage header\n" + "".join(lines[1:]), encoding="utf-8")

    with pytest.raises(LogCorrupt) as ei:
        MemberLog("alice").load()
    assert "no readable header line" in str(ei.value)


def test_history_newest_first_and_before(tmp_path):
    log = _log(tmp_path)
    log.create("Alice")
    for i in range(5):
        log.append(types.MEMBER_MESSAGE, {"ts": float(i), "preview": str(i)})
    h = log.history(before=None, limit=3)
    assert [e["seq"] for e in h] == [5, 4, 3]
    h2 = log.history(before=3, limit=10)
    assert [e["seq"] for e in h2] == [2, 1]


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------
def _registry():
    reg = ProjectionRegistry()
    for u in all_units():
        reg.register(u)
    return reg


def _ev(seq, type, data, time=1000):
    return {"type": type, "seq": seq, "time": time, "data": data}


def test_registry_duplicate_key_raises():
    reg = _registry()
    with pytest.raises(ValueError):
        reg.register(all_units()[0])


def test_drive_emits_only_on_change():
    reg = _registry()
    fired = []
    reg.set_on_change(lambda slug, key, view, seq: fired.append((slug, key, seq)))
    # A slot/opened touches driving only.
    reg.drive("alice", _ev(0, types.SLOT_OPENED, {"slot_key": "s1"}))
    keys = {f[1] for f in fired}
    assert keys == {types.PROJ_DRIVING}


def test_driving_open_set():
    reg = _registry()
    reg.prime(
        "alice",
        [
            _ev(0, types.SLOT_OPENED, {"slot_key": "b"}),
            _ev(1, types.SLOT_OPENED, {"slot_key": "a"}),
            _ev(2, types.SLOT_CLOSED, {"slot_key": "b"}),
        ],
    )
    snap = reg.snapshot("alice")
    assert snap["values"][types.PROJ_DRIVING] == {"open": ["a"]}
    assert snap["asOfSeq"] == 2


def test_wake_states():
    reg = _registry()
    reg.prime("alice", [_ev(0, types.PATROL_STARTED, {"slot_key": "s1"}, time=42)])
    assert reg.snapshot("alice")["values"][types.PROJ_WAKE] == {
        "patrol": "armed",
        "slot_key": "s1",
        "since": 42,
    }
    reg.drive("alice", _ev(1, types.PATROL_STOPPED, {"slot_key": "s1", "reason": "done"}, time=99))
    assert reg.snapshot("alice")["values"][types.PROJ_WAKE] == {
        "patrol": "stopped",
        "slot_key": "s1",
        "stopped_reason": "done",
        "since": 99,
    }


def test_roster_last_wins():
    reg = _registry()
    reg.prime(
        "alice",
        [
            _ev(0, types.MEMBER_CONFIG, {"model": "m1", "starred": False}),
            _ev(1, types.MEMBER_CONFIG, {"model": "m2"}),
            _ev(2, types.MEMBER_BINDING, {"slot_key": "member-alice"}),
            _ev(3, types.MEMBER_MESSAGE, {"ts": 5.0, "preview": "hey"}),
        ],
    )
    roster = reg.snapshot("alice")["values"][types.PROJ_ROSTER]
    assert roster["model"] == "m2"
    assert roster["starred"] is False
    assert roster["slot_key"] == "member-alice"
    assert roster["last_message"] == "hey"


def test_activity_ring_and_counts():
    reg = _registry()
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    events = [_ev(i, types.ACTIVITY_RECORD, {"ts": now_iso, "member": "Alice"}) for i in range(60)]
    reg.prime("alice", events)
    view = reg.snapshot("alice")["values"][types.PROJ_ACTIVITY]
    assert len(view["recent"]) == 50  # ring capped
    assert view["today"] == 50
    assert view["week"] == 50


def test_disposer_removes_unit():
    reg = ProjectionRegistry()
    dispose = reg.register(all_units()[3])  # driving
    reg.prime("alice", [_ev(0, types.SLOT_OPENED, {"slot_key": "s1"})])
    assert types.PROJ_DRIVING in reg.snapshot("alice")["values"]
    dispose()
    assert types.PROJ_DRIVING not in reg.snapshot("alice")["values"]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
def test_service_ensure_append_snapshot(tmp_path, monkeypatch):
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()

    svc = MemberEventLogService(root)
    svc.ensure("alice", "Alice")
    # The log lives under the fenced crew-log tree, not beside the member's other
    # files: the store owns the layout, so the assertion asks it rather than
    # rebuilding the path here and drifting from it.
    assert crew_log_path(KIND_MEMBER, "alice").exists()
    assert not (root / "alice" / "log.jsonl").exists()

    svc.append("alice", types.MEMBER_CONFIG, {"model": "m1"})
    svc.append("alice", types.SLOT_OPENED, {"slot_key": "member-alice"})
    snap = svc.snapshot("alice")
    assert snap["values"][types.PROJ_ROSTER]["model"] == "m1"
    assert snap["values"][types.PROJ_ROSTER]["name"] == "Alice"
    assert snap["values"][types.PROJ_ROSTER]["slug"] == "alice"
    assert snap["values"][types.PROJ_DRIVING] == {"open": ["member-alice"]}
    assert svc.last_seq("alice") == 2
    assert svc.slugs() == ["alice"]
    assert svc.last_seqs() == {"alice": 2}


def test_service_folds_events_a_second_process_wrote_between_our_appends(tmp_path, monkeypatch):
    """The gateway is not this log's only writer: ``kirocrew-core`` runs as its
    own stdio subprocess and records member activity through this same service.

    ``log.append`` re-reads the file, so the seq it returns can sit above the one
    after what this process folded. Driving only that event advanced every cell's
    ``observed_seq`` straight past the intervening seqs, and ``drive`` refuses
    anything at or below that -- so they never folded, and the pushed projection
    and ``snapshot`` undercounted them until a restart re-primed from the file.
    """
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()

    svc = MemberEventLogService(root)
    svc.ensure("erin", "Erin")
    svc.append("erin", types.SLOT_OPENED, {"slot_key": "member-erin"})

    # A second process appends straight to the file, so this service's registry
    # never sees the event.
    other = MemberLog("erin")
    other.load()
    stranger = other.append(types.SLOT_OPENED, {"slot_key": "worker-from-another-process"})

    ours = svc.append("erin", types.SLOT_OPENED, {"slot_key": "member-erin-2"})
    # The gap the fold has to cross: our seq is two above what we last folded.
    assert ours["seq"] == stranger["seq"] + 1

    assert svc.snapshot("erin")["values"][types.PROJ_DRIVING]["open"] == [
        "member-erin",
        "member-erin-2",
        "worker-from-another-process",
    ]


def test_a_read_sees_events_another_process_committed(tmp_path, monkeypatch):
    """A READ has to cross the same cross-process gap an append does.

    The service held a loaded ``MemberLog`` per slug and every read short-circuited
    on its cached events, so a ``select_crew`` subprocess appending through its own
    service stayed invisible here: activity, history and the pushed projections all
    reported the older state until this process happened to append or restart.
    """
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()

    svc = MemberEventLogService(root)
    svc.ensure("erin", "Erin")
    svc.append("erin", types.SLOT_OPENED, {"slot_key": "member-erin"})
    # Load the cache the way a live gateway would: a read before the other write.
    assert svc.snapshot("erin")["values"][types.PROJ_DRIVING]["open"] == ["member-erin"]

    # A second process appends straight to the file. This service is not told.
    other = MemberLog("erin")
    other.load()
    stranger = other.append(types.SLOT_OPENED, {"slot_key": "worker-from-another-process"})

    # No append of our own: the read itself must pick the event up.
    assert svc.last_seq("erin") == stranger["seq"]
    assert svc.snapshot("erin")["values"][types.PROJ_DRIVING]["open"] == [
        "member-erin",
        "worker-from-another-process",
    ]
    assert [e["seq"] for e in svc.history("erin")][:2] == [
        stranger["seq"],
        stranger["seq"] - 1,
    ]


def test_an_unchanged_log_is_not_reparsed_on_every_read(tmp_path, monkeypatch):
    """The refresh must cost a stat, not a parse, or a roster read pays N parses.

    Pinned because the cheap path is the whole reason the refresh is acceptable on
    a hot read: ``last_seqs()`` asks once per member on every connect.
    """
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()

    svc = MemberEventLogService(root)
    svc.ensure("erin", "Erin")
    svc.append("erin", types.SLOT_OPENED, {"slot_key": "member-erin"})
    svc.snapshot("erin")

    log = svc._logs["erin"]
    loads = {"n": 0}
    real_load = log.load

    def counting_load():
        loads["n"] += 1
        real_load()

    monkeypatch.setattr(log, "load", counting_load)
    for _ in range(5):
        svc.snapshot("erin")
        svc.last_seq("erin")
    assert loads["n"] == 0, "an unchanged log was reloaded"


def test_the_log_reports_which_member_it_belongs_to(tmp_path, monkeypatch):
    """Colliding names are SUPPORTED, so this is a query and not a refusal.

    `Review_Agent` and `review-agent` both fold to `review-agent`, and the repo
    keeps that working on purpose: each activity entry stores the exact name, which
    is what `TestRecordActivity::test_colliding_names_stay_attributable` pins. What
    cannot be shared is a whole-member PROJECTION, so a caller serving one needs to
    know whose log it is reading -- and only the header can say.
    """
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()

    svc = MemberEventLogService(root)
    svc.ensure("review-agent", "review-agent")

    assert svc.logged_name("review-agent") == "review-agent"
    assert svc.logged_name("nobody") is None
    # The second member shares the slug, and the log still names the first.
    svc.ensure("review-agent", "Review_Agent")
    assert svc.logged_name("review-agent") == "review-agent"


def test_a_migration_interrupted_after_create_resumes(tmp_path, monkeypatch):
    """`ensure()` returned early whenever the log existed, so a process that died
    after creating the header never migrated the member's legacy bindings, rules or
    activity -- permanently, because the log exists on every later call.

    Written to fail on the EARLY RETURN specifically: it captures the log's seq
    after the interrupted run and requires the next ensure to have appended, so a
    version of ensure that returns without doing anything cannot satisfy it no
    matter what the log happens to contain.
    """
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()

    members.write_dm_binding("fran", member="Fran", slot_key="member-fran")

    svc = MemberEventLogService(root)

    def _die(*_a, **_k):
        raise RuntimeError("process died mid-migration")

    # Patched on the INSTANCE, and deliberately never undone:    # reverts the autouse crew-log-home fixture as well, which silently moves the
    # second phase to a different (real) home where no log exists -- so the early
    # return has nothing to return early from and the test passes for the wrong
    # reason. A second service instance does not carry this instance's patch.
    svc._migrate_legacy = _die  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        svc.ensure("fran", "Fran")

    # The log EXISTS now -- that is the precondition the early return then made
    # permanent -- and it carries nothing the migration was supposed to write.
    log = MemberLog("fran")
    assert log.exists(), "precondition: the interrupted run left a log behind"
    fresh = MemberEventLogService(root)
    seq_before = fresh.last_seq("fran")
    assert not any(
        e["type"] == types.MEMBER_BINDING for e in fresh.history("fran", limit=100)
    ), "precondition: the interrupted run migrated nothing"

    fresh.ensure("fran", "Fran")
    seq_after = fresh.last_seq("fran")
    assert seq_after > seq_before, (
        f"ensure appended nothing on a log whose migration never ran "
        f"(seq {seq_before} -> {seq_after}): an interrupted migration cannot resume"
    )
    types_seen = [e["type"] for e in fresh.history("fran", limit=100)]
    assert types.MEMBER_BINDING in types_seen, f"migration did not resume: {types_seen}"

    # And it does not run twice: a third ensure appends nothing further.
    fresh.ensure("fran", "Fran")
    assert fresh.last_seq("fran") == seq_after


def test_activity_migration_resumes_row_by_row(tmp_path, monkeypatch):
    """Skipping on "any activity record exists" loses every remaining legacy row.

    A crash after the first append leaves exactly one record in the log, and a
    per-TYPE guard reads that as "activity already migrated" -- so the rest of the
    member's history is dropped for good on every later call. The guard is per ROW
    for that reason.

    The legacy rows are written as FILES by hand: ``record_activity`` now writes to
    the event log, so building the fixture with it leaves no legacy file at all and
    the migration under test reads nothing.
    """
    import json as _json

    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()

    legacy_rows = [
        {"ts": 1000 + i, "member": "Gale", "session": f"s{i}", "mode": "persistent"}
        for i in range(4)
    ]
    dest = members.member_dir("gale") / members.ACTIVITY_FILE_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        "".join(_json.dumps(r, sort_keys=True) + "\n" for r in legacy_rows), encoding="utf-8"
    )

    # Create the log WITHOUT migrating, then append only the first row: that is
    # exactly the state a process leaves when it dies after one append.
    svc = MemberEventLogService(root)
    svc._migrate_legacy = lambda *a, **k: None  # type: ignore[method-assign]
    svc.ensure("gale", "Gale")
    log = svc._get_log("gale")
    assert log is not None
    svc.append("gale", types.ACTIVITY_RECORD, dict(legacy_rows[0]))

    def _sessions() -> list[str]:
        return sorted(
            str(e["data"].get("session"))
            for e in log.iter_events()
            if e["type"] == types.ACTIVITY_RECORD
        )

    assert _sessions() == ["s0"], "precondition: only the first row made it"

    # The real migration has to pick up the REST, and add each row once.
    del svc._migrate_legacy  # type: ignore[attr-defined]
    svc._migrate_legacy("gale", "Gale", log)
    assert _sessions() == ["s0", "s1", "s2", "s3"], _sessions()

    # Running it again adds nothing: the dedupe is what makes it re-runnable.
    svc._migrate_legacy("gale", "Gale", log)
    assert _sessions() == ["s0", "s1", "s2", "s3"], _sessions()


def test_identical_legacy_activity_rows_all_survive_a_resumed_migration(tmp_path, monkeypatch):
    """A repeated legacy row is a row, not a duplicate to collapse.

    Legacy activity rows are not distinct: the same member, second, via and
    project is an ordinary shape, so a log can legitimately hold the same row
    several times. A crash after the first append leaves ONE copy, and matching
    the remaining legacy rows against a SET of what the log already holds lets
    that one copy stand for every occurrence -- so the rest are dropped for good
    and the durable history under-counts what the member did.

    Each legacy row must consume one recorded match, which is why the dedupe is
    counted rather than a set.
    """
    import json as _json

    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()

    # Three BYTE-IDENTICAL rows, so every key collides with the others.
    row = {"ts": 1000, "member": "Gale", "session": "s", "mode": "persistent"}
    legacy_rows = [dict(row) for _ in range(3)]
    dest = members.member_dir("gale") / members.ACTIVITY_FILE_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        "".join(_json.dumps(r, sort_keys=True) + "\n" for r in legacy_rows), encoding="utf-8"
    )

    svc = MemberEventLogService(root)
    svc._migrate_legacy = lambda *a, **k: None  # type: ignore[method-assign]
    svc.ensure("gale", "Gale")
    log = svc._get_log("gale")
    assert log is not None
    # The state a process leaves when it dies after appending the first copy.
    svc.append("gale", types.ACTIVITY_RECORD, dict(row))

    def _count() -> int:
        return sum(1 for e in log.iter_events() if e["type"] == types.ACTIVITY_RECORD)

    assert _count() == 1, "precondition: one copy made it before the crash"

    del svc._migrate_legacy  # type: ignore[attr-defined]
    svc._migrate_legacy("gale", "Gale", log)
    assert _count() == 3, f"a repeated legacy row was dropped as a duplicate: {_count()} of 3"

    # Still idempotent: a second pass consumes the three matches and adds none.
    svc._migrate_legacy("gale", "Gale", log)
    assert _count() == 3, f"a re-run duplicated the rows: {_count()}"


def test_service_broadcast_frames(tmp_path, monkeypatch):
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()

    frames = []
    svc = MemberEventLogService(
        root, broadcast=lambda name, payload: frames.append((name, payload))
    )
    svc.ensure("bob", "Bob")
    svc.append("bob", types.PATROL_STARTED, {"slot_key": "member-bob"})
    wake = [f for f in frames if f[1].get("key") == types.PROJ_WAKE]
    assert wake and wake[-1][0] == types.WS_MEMBER_PROJECTION
    assert wake[-1][1]["value"]["patrol"] == "armed"
    assert wake[-1][1]["slug"] == "bob"


def test_service_broadcast_redacts_project_before_egress(tmp_path, monkeypatch):
    """An activity record's `project` can carry a credential/URL; the folded
    projection must be redacted before it leaves over the dashboard WebSocket,
    the same as the /history and /activity HTTP reads."""
    import json

    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()

    frames = []
    svc = MemberEventLogService(
        root, broadcast=lambda name, payload: frames.append((name, payload))
    )
    svc.ensure("dave", "Dave")
    secret = "https://evil.example/x?token=AKIAIOSFODNN7EXAMPLE"
    svc.append("dave", types.ACTIVITY_RECORD, {"ts": 1.0, "member": "Dave", "project": secret})
    activity = [f for f in frames if f[1].get("key") == types.PROJ_ACTIVITY]
    assert activity, "an activity append should broadcast an activity projection"
    blob = json.dumps(activity[-1][1])
    assert secret not in blob
    assert "AKIAIOSFODNN7EXAMPLE" not in blob


def test_redact_projection_value_scrubs_keys_not_just_values():
    """A dict KEY can be agent-authored (a contributed projection key, a nested
    data key), so a credential- or URL-shaped key must be scrubbed too -- redacting
    only values would let it cross to the browser."""
    from kiro_crew.eventlog.service import _redact_projection_value

    secret = "https://evil.example/x?token=AKIAIOSFODNN7EXAMPLE"
    out = _redact_projection_value({secret: {secret: "v"}})
    blob = json.dumps(out)
    assert secret not in blob
    assert "AKIAIOSFODNN7EXAMPLE" not in blob


def test_service_broadcast_never_raises(tmp_path, monkeypatch):
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()

    def boom(name, payload):
        raise RuntimeError("nope")

    svc = MemberEventLogService(root, broadcast=boom)
    svc.ensure("carol", "Carol")
    # Must not raise out of append.
    svc.append("carol", types.SLOT_OPENED, {"slot_key": "member-carol"})


def test_service_migrates_legacy(tmp_path, monkeypatch):
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()

    # Legacy binding + rules + activity for "Dave" (slug "dave").
    members.write_dm_binding("dave", member="Dave", slot_key=members.member_slot_key("dave"))
    members.write_member_rules("dave", member="Dave", text="be nice")
    members.record_activity("Dave", "sess-1", "persistent", via="chat")

    svc = MemberEventLogService(root)
    svc.ensure("dave", "Dave")

    events = svc.history("dave", before=None, limit=100)
    etypes = [e["type"] for e in events]
    assert types.MEMBER_BINDING in etypes
    assert types.MEMBER_RULES in etypes
    assert types.ACTIVITY_RECORD in etypes
    snap = svc.snapshot("dave")
    assert snap["values"][types.PROJ_ROSTER]["slot_key"] == members.member_slot_key("dave")


def test_service_ensure_is_idempotent(tmp_path, monkeypatch):
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()

    svc = MemberEventLogService(root)
    svc.ensure("erin", "Erin")
    svc.append("erin", types.MEMBER_CONFIG, {"model": "m1"})
    seq_before = svc.last_seq("erin")
    svc.ensure("erin", "Erin")  # no-op, must not re-migrate or reset
    assert svc.last_seq("erin") == seq_before


# ---------------------------------------------------------------------------
# MemberLog: publishing the header, and the failures a real filesystem hands back
# ---------------------------------------------------------------------------
def test_create_is_a_no_op_when_another_writer_published_first(tmp_path, monkeypatch):
    """Two spawns can call ``create`` for the same member at once.

    The loser must not clobber the winner's header. The publish itself -- temp
    file, hard link or rename, directory fsync -- belongs to the store now and is
    covered by its own suites; what belongs HERE is the race the check leaves
    open, because ``exists`` and ``create`` are two calls and a writer can publish
    between them. The store refuses the second create with ``already_exists``, and
    the log it refused to overwrite is the one this caller wanted, so the refusal
    is an answer rather than a fault.
    """
    from kiro_crew.crew_log.store import CrewLog

    log = _log(tmp_path)
    log.create("Alice")
    winner = log.path.read_bytes()

    # The race: the existence check answers "absent" for a log that is there.
    monkeypatch.setattr(CrewLog, "exists", classmethod(lambda cls, kind, unit_id: False))

    log.create("Impostor")  # must not raise, must not rewrite

    assert log.path.read_bytes() == winner


def test_create_still_raises_a_refusal_that_is_not_the_race(tmp_path, monkeypatch):
    """Only ``already_exists`` is swallowed; any other refusal is a real fault.

    Swallowing every ``CrewLogError`` would make an unwritable home look like a
    member who simply has a log, and the first read would then report an empty
    history instead of the failure.
    """
    from kiro_crew.crew_log import store as store_mod

    log = _log(tmp_path)

    def _refuse(cls, kind, unit_id, **fields):
        raise CrewLogError("disk is read-only", code="io_failed", field="path")

    monkeypatch.setattr(store_mod.CrewLog, "create", classmethod(_refuse))

    with pytest.raises(CrewLogError):
        log.create("Alice")


def test_contributed_type_round_trips_and_derives_its_emitter(tmp_path):
    """An app's ``<app>/<action>`` is stored as ``app:<app>/<action>`` and read back plain.

    The stored spelling is what lets the log's own ownership rule decide the
    write: the guest namespace is the only one a non-gateway emitter may use, and
    the emitter is DERIVED from the type so a caller cannot attribute an entry to
    a different app than the one it is writing under. The protocol spelling is what
    every app and frame already speaks, so the translation lives here and nothing
    above this layer sees it.
    """
    log = _log(tmp_path)
    log.create("Alice")

    event = log.append("tetris/score", {"points": 7})

    assert event["type"] == "tetris/score"
    # On disk it carries the guest prefix, and the emitter matches the app that
    # owns the type rather than the gateway.
    raw = [
        json.loads(line)
        for line in log.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert raw[-1]["type"] == "app:tetris/score"
    assert raw[-1]["src"] == "app:tetris"
    # A built-in stays unprefixed and is the gateway's own observation.
    log.append(types.MEMBER_MESSAGE, {"ts": 1.0, "preview": "hi"})
    raw = [
        json.loads(line)
        for line in log.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert raw[-1]["type"] == types.MEMBER_MESSAGE
    assert raw[-1]["src"] == "gateway"
    # And a cold reader gives both back in the protocol's spelling.
    cold = MemberLog("alice")
    cold.load()
    assert [e["type"] for e in cold.events] == ["tetris/score", types.MEMBER_MESSAGE]


def test_the_member_log_is_fenced_the_same_way_every_other_crew_log_is(tmp_path):
    """The reason the log lives under ``crew-log`` rather than beside the member.

    Dispatch trust reads this file, so an agent that can rewrite it can rewrite
    what the gateway believes about a member. Both fences are named at the
    ``crew-log`` ROOT, so a kind under it inherits them: the tool gate refuses the
    agent's own file tools, and the launcher hides the tree from a sandboxed
    subprocess. A per-member log kept anywhere else needs its own entry in both
    lists, and the next log added misses them the same way.

    Asserted against the real gate on the real path, with the member's former
    location as the control -- it is exactly the path that was NOT fenced.
    """
    from kiro_crew import sandbox, security
    from kiro_crew.members import member_dir

    log = _log(tmp_path)
    log.create("Alice")

    assert security.is_sensitive_path(str(log.path))
    assert "crew-log" in set(security.paths._CREW_SECRET_LEAVES)
    assert "crew-log" in set(sandbox._CREW_HIDDEN_LEAVES)

    # The control: the old location, which neither list names.
    former = member_dir("alice") / "log.jsonl"
    assert not security.is_sensitive_path(str(former))
    assert "members" not in set(security.paths._CREW_SECRET_LEAVES)


def test_load_of_an_absent_log_is_an_empty_read_not_a_failure(tmp_path):
    """Callers treat "no log for this slug" as an empty history, so absent is an answer.

    The store says so with ``no_ledger``, which is the one refusal this layer
    translates into emptiness; everything else it raises is damage.
    """
    log = MemberLog("nobody")

    log.load()

    assert log.header is None
    assert log.events == []
    assert log.last_seq() == 0
    assert log.history(None, 10) == []


def test_exists_answers_before_anything_is_written(tmp_path):
    """Callers check this to decide whether to ensure a log, so it must not load."""
    log = _log(tmp_path)

    assert log.exists() is False

    log.create("Alice")

    assert log.exists() is True


def test_the_activity_projection_serves_ts_as_a_number_not_the_logged_string():
    """The wire type says ``ts: number`` and the browser does ``e.ts * 1000``.

    ``members.record_activity`` STORES an ISO-8601 string, and the REST activity
    read parses it to epoch seconds before serving. The projection view has to
    serve the same shape: a string reaching the browser makes ``e.ts * 1000``
    NaN and ``e.ts >= todayFloor`` false, which renders zero counts and invalid
    dates on the ordinary path rather than failing loudly.
    """
    from kiro_crew.eventlog.members_projections import ActivityProjection

    proj = ActivityProjection()
    state = {"recent": [{"ts": "2026-09-20T12:00:00Z", "member": "Gale"}]}
    served = proj.view(state)["recent"]

    assert len(served) == 1, served
    got = served[0]["ts"]
    assert isinstance(got, (int, float)) and not isinstance(got, bool), (
        f"ts reached the browser as {type(got).__name__} ({got!r}); "
        "the declared wire type is a number and the page multiplies it"
    )
    assert got > 0


def test_an_activity_row_migrated_from_a_legacy_file_is_not_recorded_twice(tmp_path):
    """The dedupe scan must run AFTER the legacy fold, not before it.

    ``ensure`` is what folds a pre-upgrade ``activity.jsonl`` into the log, so a
    scan ordered before it reads a log the legacy rows have not reached, finds
    nothing to dedupe against, and appends a permanent duplicate. The window is
    the first post-upgrade call for a session that already has legacy activity.
    """
    import json as _json

    import kiro_crew.members as members
    from kiro_crew.eventlog.service import get_service

    svc = get_service()
    slug = members.slug_for_name("Gale")
    session = "s-carried-across-the-upgrade"

    dest = members.member_dir(slug) / members.ACTIVITY_FILE_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        _json.dumps(
            {"ts": "2026-09-20T11:00:00Z", "member": "Gale", "session": session},
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    assert slug not in set(svc.slugs()), "precondition: the log does not exist yet"

    members.record_activity("Gale", session, "persistent", dedupe_session=True)

    log = svc._get_log(slug)
    assert log is not None
    sessions = [
        e["data"].get("session") for e in log.iter_events() if e["type"] == types.ACTIVITY_RECORD
    ]
    assert sessions.count(session) == 1, (
        f"the session was recorded {sessions.count(session)} times: the dedupe "
        f"scan ran before the legacy fold ({sessions})"
    )


def test_a_colliding_members_activity_does_not_reach_the_other_members_view(tmp_path):
    """Two distinct NAMES can fold to one slug and therefore one log. The fold
    cannot separate them -- a projection sees events only, and the owning name is
    in the log HEADER, which is not an event -- so the scoping runs where the
    header name is known, beside the roster name overlay.
    """
    import kiro_crew.members as members
    from kiro_crew.eventlog.service import get_service

    svc = get_service()
    owner, stranger = "Gale", "gale"
    slug = members.slug_for_name(owner)
    assert members.slug_for_name(stranger) == slug, "precondition: the names collide"

    # Stamped relative to NOW rather than to a fixed calendar date. The fold's
    # ``today`` is a ROLLING 24-hour window (``age < day`` in
    # ``ActivityProjection.view``), so a hardcoded date stops being inside it
    # the following day and the count assertion below reds on an unchanged
    # tree -- which is exactly what happened to this test.
    from datetime import datetime, timedelta, timezone

    def _ago(hours: int) -> str:
        stamp = datetime.now(timezone.utc) - timedelta(hours=hours)
        return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")

    svc.ensure(slug, owner)
    svc.append(slug, types.ACTIVITY_RECORD, {"ts": _ago(2), "member": owner})
    svc.append(slug, types.ACTIVITY_RECORD, {"ts": _ago(1), "member": stranger})

    view = svc.snapshot(slug)["values"][types.PROJ_ACTIVITY]
    members_seen = sorted({r.get("member") for r in view["recent"]})
    assert members_seen == [
        owner
    ], f"another member's activity reached this member's view: {members_seen}"
    assert view["today"] == 1, (
        "the counts describe a different set than the list served beside them: "
        f"today={view['today']} with {len(view['recent'])} record(s)"
    )


def test_the_owners_own_activity_survives_the_scoping(tmp_path):
    """CONTROL. Serving nothing would also satisfy the assertion above, so the
    ordinary single-member case has to be checked in the same breath."""
    import kiro_crew.members as members
    from kiro_crew.eventlog.service import get_service

    svc = get_service()
    name = "Solo"
    slug = members.slug_for_name(name)
    svc.ensure(slug, name)
    # Relative to NOW for the same reason as the test above: ``today`` is a
    # rolling 24-hour window, not a calendar day.
    from datetime import datetime, timedelta, timezone

    stamp = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    svc.append(slug, types.ACTIVITY_RECORD, {"ts": stamp, "member": name})

    view = svc.snapshot(slug)["values"][types.PROJ_ACTIVITY]
    assert len(view["recent"]) == 1, view
    assert view["today"] == 1, view


def test_one_unreadable_member_does_not_cost_the_others_their_baseline(tmp_path):
    """`last_seqs` feeds the subscribe baseline. Read as a comprehension over
    `last_seq`, one member with a damaged header raises out of the whole dict, so
    every OTHER member loses its cursor for a fault in a file it does not share.
    """
    import kiro_crew.members as members
    from kiro_crew.eventlog.service import get_service

    svc = get_service()
    good, bad = "Alice", "Bob"
    good_slug, bad_slug = members.slug_for_name(good), members.slug_for_name(bad)
    for name, slug in ((good, good_slug), (bad, bad_slug)):
        svc.ensure(slug, name)
        svc.append(slug, types.ACTIVITY_RECORD, {"ts": "2026-09-20T12:00:00Z", "member": name})

    real_last_seq = svc.last_seq

    def _one_member_is_damaged(slug: str) -> int:
        if slug == bad_slug:
            raise LogCorrupt(tmp_path / "log.jsonl", 1, "header is not readable")
        return real_last_seq(slug)

    svc.last_seq = _one_member_is_damaged  # type: ignore[method-assign]
    try:
        seqs = svc.last_seqs()
    finally:
        del svc.last_seq  # type: ignore[attr-defined]

    assert good_slug in seqs, (
        "a healthy member lost its baseline cursor because a DIFFERENT member's "
        f"log could not be read: {seqs}"
    )
    assert seqs[good_slug] >= 0, seqs
    assert bad_slug not in seqs, (
        "the damaged member must be ABSENT, not reported at -1: -1 is the cursor "
        f"of a member with no log, and a client told that prunes what it has: {seqs}"
    )


class TestAnAppendWaitsOutAnotherProcessesLease:
    """A contention refusal costs a wait, not the event.

    The lease is taken non-blocking, so two writers arriving together do not
    serialize behind the per-append lock -- the second is refused and writes
    nothing. The holder is momentary (it is released within the append that took
    it), which is what makes waiting effective rather than hopeful.
    """

    def test_an_append_lands_once_the_other_holder_releases(self):
        import threading
        import time

        from kiro_crew.crew_log import lease
        from kiro_crew.crew_log.schema import KIND_MEMBER
        from kiro_crew.eventlog.log import MemberLog
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.eventlog.types import ACTIVITY_RECORD

        svc = get_service()
        svc.ensure("waiter", "Waiter")
        lease_path = MemberLog("waiter").path.parent / lease.LEASE_FILE
        # ``sole`` refuses every later acquire in this process with the code a
        # second PROCESS is given, so the contention under test is reproduced
        # without a second interpreter.
        key = lease.acquire(lease_path, kind=KIND_MEMBER, unit_id="waiter", sole=True)
        hold_seconds = 0.2

        def _release_after_holding() -> None:
            time.sleep(hold_seconds)
            lease.release(key)

        holder = threading.Thread(target=_release_after_holding, daemon=True)
        holder.start()
        started = time.monotonic()
        try:
            svc.append("waiter", ACTIVITY_RECORD, {"member": "Waiter", "ts": "x"})
        finally:
            holder.join(timeout=5)
        elapsed = time.monotonic() - started

        assert [e["type"] for e in svc.history("waiter", before=None, limit=None)] == [
            ACTIVITY_RECORD
        ]
        # The append must have WAITED, not raced the release. Without this the test
        # passes whenever the holder happens to let go first, which is a test of
        # timing rather than of the retry -- and it passed with the retry removed.
        assert elapsed >= hold_seconds

    def test_a_holder_past_the_budget_still_reports_rather_than_waiting_forever(self, monkeypatch):
        # CONTROL, two ways. It proves the wait is BOUNDED, so a wedged peer cannot
        # hold the queue behind this event -- and it proves the test above is not
        # passing merely because contention never happened, since the same lease
        # here does refuse.
        from kiro_crew.crew_log import lease
        from kiro_crew.crew_log.errors import CODE_ALREADY_OWNED, CrewLogError
        from kiro_crew.crew_log.schema import KIND_MEMBER
        from kiro_crew.eventlog import log as log_module
        from kiro_crew.eventlog.log import MemberLog
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.eventlog.types import ACTIVITY_RECORD

        monkeypatch.setattr(log_module, "APPEND_CONTENTION_SECONDS", 0.05)
        svc = get_service()
        svc.ensure("waiter", "Waiter")
        lease_path = MemberLog("waiter").path.parent / lease.LEASE_FILE
        key = lease.acquire(lease_path, kind=KIND_MEMBER, unit_id="waiter", sole=True)
        try:
            with pytest.raises(CrewLogError) as caught:
                svc.append("waiter", ACTIVITY_RECORD, {"member": "Waiter", "ts": "y"})
        finally:
            lease.release(key)

        assert caught.value.code == CODE_ALREADY_OWNED
        # Refused BEFORE any byte was written, which is what makes the retry safe.
        assert svc.history("waiter", before=None, limit=None) == []


class TestEveryHardExitDrainsTheMemberEventLog:
    def test_both_gateway_exit_paths_drain_before_exiting(self):
        """``os._exit`` skips ``atexit``, so the module's own hook never runs there.

        Asserted on the SOURCE because neither path can be driven from a unit test:
        one is a signal handler and both end the process. The sibling crew-log
        emitter is drained at these same points for the same reason, so the check is
        that this log is not the one left out.
        """
        from pathlib import Path

        import kiro_crew.slack.gateway as gateway_module

        source = Path(gateway_module.__file__).read_text(encoding="utf-8")
        exits = [i for i, line in enumerate(source.splitlines()) if "os._exit(" in line]
        assert exits, "no hard exit found, so this guard would pass vacuously"
        lines = source.splitlines()
        for index in exits:
            window = "\n".join(lines[max(0, index - 40) : index])
            assert (
                "eventlog_hooks.drain_for_shutdown" in window
            ), f"the hard exit at line {index + 1} does not drain the member event log"


class TestTheLegacyActivitySourceIsRetiredOnceFolded:
    """The fold dedupes by COUNTING matching rows, which cannot date a row.

    That is why counting alone leaves the legacy file a forgery source: a row written
    after the migration finished has no match to consume, so it is appended as a
    trusted `activity/record`. Only a completion marker can tell the two apart.
    """

    @staticmethod
    def _legacy(home, slug, rows):
        from kiro_crew import members

        d = members.member_dir(slug)
        d.mkdir(parents=True, exist_ok=True)
        path = d / members.ACTIVITY_FILE_NAME
        path.write_text(
            "".join(json.dumps(r) + "\n" for r in rows),
            encoding="utf-8",
        )
        return path

    def test_a_row_written_after_the_fold_is_not_imported(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.eventlog import service as svc_mod
        from kiro_crew.eventlog.types import ACTIVITY_RECORD

        svc_mod.set_service(None)
        slug = "alice"
        legacy = self._legacy(tmp_path, slug, [{"ts": 1, "via": "cli", "project": "p"}])

        svc = svc_mod.get_service()
        svc.ensure(slug, "Alice")
        first = [e for e in svc.history(slug, limit=None) if e["type"] == ACTIVITY_RECORD]
        assert len(first) == 1, "the genuine legacy row must be folded in"
        assert not legacy.exists(), "the source must be retired once folded"

        # Whoever can write that directory writes a NEW row under the old name.
        self._legacy(tmp_path, slug, [{"ts": 2, "via": "forged", "project": "p"}])
        svc_mod.set_service(None)
        svc = svc_mod.get_service()
        svc.ensure(slug, "Alice")
        after = [e for e in svc.history(slug, limit=None) if e["type"] == ACTIVITY_RECORD]
        assert (
            len(after) == 1
        ), "a row written after the fold finished was imported as trusted activity"
        svc_mod.set_service(None)

    def test_an_unfolded_source_is_still_imported(self, tmp_path, monkeypatch):
        # CONTROL. Without this, refusing to read the legacy file at all would satisfy
        # the test above while losing every member's history on upgrade.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.eventlog import service as svc_mod
        from kiro_crew.eventlog.types import ACTIVITY_RECORD

        svc_mod.set_service(None)
        self._legacy(tmp_path, "bob", [{"ts": 1, "via": "cli", "project": "p"}])
        svc = svc_mod.get_service()
        svc.ensure("bob", "Bob")
        rows = [e for e in svc.history("bob", limit=None) if e["type"] == ACTIVITY_RECORD]
        assert len(rows) == 1
        svc_mod.set_service(None)

    def test_the_fold_stops_reading_past_its_byte_budget(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.eventlog import service as svc_mod

        rows = [{"ts": i, "via": "cli", "project": "p"} for i in range(200)]
        self._legacy(tmp_path, "carol", rows)
        one_line = len(json.dumps(rows[0]) + "\n")
        monkeypatch.setattr(svc_mod, "MAX_LEGACY_ACTIVITY_BYTES", one_line * 3)
        read, _complete = svc_mod._read_legacy_activity_files("carol")
        # Tied to the budget, not merely fewer than all 200: an unbounded read of
        # this file returns 199, so a loose `< 200` would pass with no bound at all.
        assert len(read) <= 3, f"the budget must stop the read, got {len(read)} rows"
        assert len(read) >= 1, "and must not refuse the file outright"


class TestTheForgeryPathClosesEvenWithNothingToMigrate:
    """The marker records that the fold RAN, not that it found anything.

    A member with no legacy file, or an empty one, yields no rows. Gating the
    retirement on rows leaves that member's marker unwritten forever, so whoever
    writes the file afterwards still gets it imported as trusted activity -- the
    members with nothing to migrate are exactly the ones left exposed.
    """

    def test_a_member_with_no_legacy_file_is_still_retired(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import members
        from kiro_crew.eventlog import service as svc_mod
        from kiro_crew.eventlog.types import ACTIVITY_RECORD

        svc_mod.set_service(None)
        svc = svc_mod.get_service()
        svc.ensure("dave", "Dave")  # nothing to migrate at all

        # Now a row appears under the legacy name.
        d = members.member_dir("dave")
        d.mkdir(parents=True, exist_ok=True)
        (d / members.ACTIVITY_FILE_NAME).write_text(
            json.dumps({"ts": 5, "via": "forged", "project": "p"}) + "\n", encoding="utf-8"
        )
        svc_mod.set_service(None)
        svc = svc_mod.get_service()
        svc.ensure("dave", "Dave")
        rows = [e for e in svc.history("dave", limit=None) if e["type"] == ACTIVITY_RECORD]
        assert rows == [], "a member with nothing to migrate was left exposed"
        svc_mod.set_service(None)

    def test_deleting_the_member_side_marker_does_not_reopen_the_path(self, tmp_path, monkeypatch):
        """The completion fact must not live where the adversary can delete it.

        The member directory is writable by the same party this check defends
        against. With the marker beside the legacy file, the whole guard came off
        with one unlink: remove it, write a fresh ``activity.jsonl``, and the next
        fold imported those rows as trusted history. A guard an adversary can remove
        is not a guard, so the fact is recorded in the FENCED log directory and the
        member-side rename is hygiene only.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import members
        from kiro_crew.eventlog import service as svc_mod
        from kiro_crew.eventlog.types import ACTIVITY_RECORD

        d = members.member_dir("frank")
        d.mkdir(parents=True, exist_ok=True)
        legacy = d / members.ACTIVITY_FILE_NAME
        legacy.write_text(
            json.dumps({"ts": 1, "via": "real", "project": "p"}) + "\n", encoding="utf-8"
        )
        svc_mod.set_service(None)
        svc_mod.get_service().ensure("frank", "Frank")
        before = [
            e
            for e in svc_mod.get_service().history("frank", limit=None)
            if e["type"] == ACTIVITY_RECORD
        ]
        assert len(before) == 1, "precondition: the real legacy row folded"

        # The adversary's move: remove every member-side trace of the migration, then
        # write forged rows under the live name. Asserted rather than assumed, so the
        # test fails on its own premise if the layout changes.
        removed = [p for p in d.iterdir() if p.name.startswith(members.ACTIVITY_FILE_NAME)]
        assert removed, "precondition: the fold left something under the member dir"
        for path in removed:
            path.unlink()
        legacy.write_text(
            json.dumps({"ts": 9, "via": "forged", "project": "p"}) + "\n", encoding="utf-8"
        )

        svc_mod.set_service(None)
        svc_mod.get_service().ensure("frank", "Frank")
        after = [
            e
            for e in svc_mod.get_service().history("frank", limit=None)
            if e["type"] == ACTIVITY_RECORD
        ]
        assert [e["data"].get("via") for e in after] == [
            "real"
        ], "a forged row was imported after the member-side marker was deleted"
        svc_mod.set_service(None)

    def test_one_enormous_line_is_capped_before_it_is_materialised(self, tmp_path, monkeypatch):
        """Iterating a handle hands back a whole LINE, which a budget cannot precede.

        One row written without a newline is the same failure as reading the file
        whole. Asserted on the SIZE of what each read returns, not on the rows that
        come back: an uncapped read of this file also yields no rows, because the
        budget goes negative once the line is already in memory -- so an
        outcome-only assertion passes against the very bug this pins.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import members
        from kiro_crew.eventlog import service as svc_mod

        d = members.member_dir("erin")
        d.mkdir(parents=True, exist_ok=True)
        huge = json.dumps({"ts": 1, "via": "cli", "project": "x" * 200_000})
        (d / members.ACTIVITY_FILE_NAME).write_text(huge, encoding="utf-8")

        cap = 1024
        monkeypatch.setattr(svc_mod, "MAX_LEGACY_ACTIVITY_BYTES", cap)
        got: list[int] = []
        real_opener = svc_mod._open_legacy_regular_file

        class _Spy:
            """Wraps the handle the module's own opener returns.

            Spying on ``_open_legacy_regular_file`` rather than on ``Path.open``:
            the read goes through ``os.open`` + ``os.fdopen`` so it can refuse a
            FIFO and a symlink, which ``Path.open`` cannot express -- and a spy on
            a path the code does not take reports no reads at all rather than
            failing on the property it exists to check.
            """

            def __init__(self, inner):
                self._inner = inner

            def readline(self, *args):
                out = self._inner.readline(*args)
                got.append(len(out))
                return out

            def __enter__(self):
                self._inner.__enter__()
                return self

            def __exit__(self, *exc):
                return self._inner.__exit__(*exc)

        def _spying_opener(path):
            inner = real_opener(path)
            return None if inner is None else _Spy(inner)

        monkeypatch.setattr(svc_mod, "_open_legacy_regular_file", _spying_opener)
        svc_mod._read_legacy_activity_files("erin")

        assert got, "the file must actually be read"
        assert max(got) <= cap + 1, (
            f"a read returned {max(got)} characters against a {cap} cap, so the row "
            "was materialised in full before any budget could look at it"
        )

    def test_a_row_within_the_cap_is_still_read(self, tmp_path, monkeypatch):
        # CONTROL. Without this, capping every read at zero would satisfy both tests
        # above while migrating nothing at all.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import members
        from kiro_crew.eventlog import service as svc_mod

        d = members.member_dir("frank")
        d.mkdir(parents=True, exist_ok=True)
        (d / members.ACTIVITY_FILE_NAME).write_text(
            json.dumps({"ts": 1, "via": "cli", "project": "p"}) + "\n", encoding="utf-8"
        )
        assert len(svc_mod._read_legacy_activity_files("frank")[0]) == 1


class TestOnlyARegularFileIsReadAtTheLegacyPath:
    """The legacy path is AGENT-WRITABLE, so the party the completion marker
    defends against also chooses what KIND of thing sits there.

    A plain ``open`` trusts that choice: a FIFO blocks until a writer appears, and
    no writer ever has to. The read runs inside ``ensure`` under the per-slug lock
    that the roster request path holds, and the hang happens BEFORE the marker can
    be written -- so the FIFO survives a restart and the marker never lands.
    """

    @staticmethod
    def _member_dir(slug):
        from kiro_crew import members

        d = members.member_dir(slug)
        d.mkdir(parents=True, exist_ok=True)
        return d

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFO on this platform")
    def test_a_planted_fifo_does_not_hang_the_fold(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import members
        from kiro_crew.eventlog import service as svc_mod

        d = self._member_dir("gail")
        os.mkfifo(d / members.ACTIVITY_FILE_NAME)

        # Returning AT ALL is the assertion: a blocking open never comes back, so
        # a regression is a hung test rather than a failing one. Bounded so it
        # reports as a failure instead of stalling the shard.
        done: list[tuple] = []
        worker = threading.Thread(
            target=lambda: done.append(svc_mod._read_legacy_activity_files("gail")),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=10)
        assert done, (
            "the read did not return within 10s: a FIFO at the legacy path blocked "
            "the open, and with it the per-slug lock and the request behind it"
        )
        rows, complete = done[0]
        assert rows == [], "nothing is migrated from a FIFO"
        assert complete is True, "a refused non-regular file leaves nothing unread"

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks on this platform")
    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW unavailable")
    def test_a_symlink_at_the_legacy_path_is_not_followed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import members
        from kiro_crew.eventlog import service as svc_mod

        outside = tmp_path / "outside.jsonl"
        outside.write_text(
            json.dumps({"ts": 1, "via": "planted", "project": "elsewhere"}) + "\n",
            encoding="utf-8",
        )
        d = self._member_dir("hana")
        os.symlink(outside, d / members.ACTIVITY_FILE_NAME)

        rows, _complete = svc_mod._read_legacy_activity_files("hana")
        assert rows == [], f"the read followed the symlink and imported {rows}"

    def test_a_regular_file_is_still_read(self, tmp_path, monkeypatch):
        # CONTROL. Without this, refusing every open would satisfy both tests above
        # while losing every member's history on upgrade.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import members
        from kiro_crew.eventlog import service as svc_mod

        d = self._member_dir("iris")
        (d / members.ACTIVITY_FILE_NAME).write_text(
            json.dumps({"ts": 1, "via": "cli", "project": "p"}) + "\n", encoding="utf-8"
        )
        rows, complete = svc_mod._read_legacy_activity_files("iris")
        assert len(rows) == 1 and complete is True

    def test_a_transient_open_failure_leaves_the_fold_incomplete(self, tmp_path, monkeypatch):
        """An open that FAILED is not a file that is absent.

        Reporting a failure as absence lets the caller keep `complete` true, and a
        complete fold writes the durable retirement marker -- which never re-runs. So
        one transient EMFILE or permission blip on an EXISTING legacy file would retire
        the member's rows UNREAD, permanently, with no recovery path.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        import errno

        from kiro_crew import members, platform_compat
        from kiro_crew.eventlog import service as svc_mod

        d = self._member_dir("jade")
        (d / members.ACTIVITY_FILE_NAME).write_text(
            json.dumps({"ts": 1, "via": "cli", "project": "p"}) + "\n", encoding="utf-8"
        )

        def _boom(path, **kwargs):
            raise OSError(errno.EMFILE, "too many open files")

        monkeypatch.setattr(platform_compat, "open_file_no_reparse", _boom)
        rows, complete = svc_mod._read_legacy_activity_files("jade")

        assert complete is False, (
            "a transient open failure was reported as absence, so the fold counts as "
            "complete and the retirement marker will retire these rows unread"
        )
        assert rows == []

    def test_an_absent_file_is_still_absence(self, tmp_path, monkeypatch):
        # CONTROL for the test above. Treating every miss as a FAILURE would leave the
        # fold permanently incomplete for members who simply never had a legacy file,
        # so the migration would never finish and never retire.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.eventlog import service as svc_mod

        self._member_dir("kira")  # exists, but holds no activity file
        rows, complete = svc_mod._read_legacy_activity_files("kira")

        assert rows == []
        assert complete is True, (
            "an absent legacy file was treated as a read failure, so the fold never "
            "completes and the migration never retires"
        )


class TestTheCompletionMarkerIsDurableBeforeTheNameIsFreed:
    """The marker is the ONLY thing separating a genuine legacy row from a forged one,
    and it lives in the fenced log directory the agent cannot write.

    That is not enough on its own: if the legacy name is freed first, a crash between
    the two leaves the name available with nothing recorded, and the next ensure folds
    whatever was written there as trusted history. So the order is the property, and
    what this asserts is the state DURING the window, not the state after a clean run.
    """

    @staticmethod
    def _legacy(slug, rows):
        from kiro_crew import members

        d = members.member_dir(slug)
        d.mkdir(parents=True, exist_ok=True)
        path = d / members.ACTIVITY_FILE_NAME
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return path

    @staticmethod
    def _crash_only_the_retirement(monkeypatch, svc_mod):
        """Fault ONLY the retirement rename.

        ``svc_mod.os`` is the os module itself, so replacing ``os.replace`` outright
        also breaks the event log's own atomic write and the fault never reaches the
        code under test -- it surfaces as an uncaught OSError from the append.
        """
        real = svc_mod.os.replace

        def _selective(src, dst, *a, **k):
            if svc_mod.LEGACY_MIGRATED_SUFFIX in str(dst):
                raise OSError("power cut at the rename")
            return real(src, dst, *a, **k)

        monkeypatch.setattr(svc_mod.os, "replace", _selective)

    def test_a_crash_before_the_rename_leaves_the_marker_recorded(self, tmp_path, monkeypatch):
        """Faults os.replace, which is the instant the live name would be freed. Any
        state the marker reaches only AFTER that call is a state a crash can skip.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.eventlog import service as svc_mod

        svc_mod.set_service(None)
        legacy = self._legacy("ivy", [{"ts": 1, "via": "cli", "project": "p"}])
        self._crash_only_the_retirement(monkeypatch, svc_mod)
        svc = svc_mod.get_service()
        svc.ensure("ivy", "Ivy")

        fenced = svc_mod._legacy_folded_marker_path("ivy")
        assert fenced is not None
        assert fenced.exists(), (
            "the rename ran before the marker was recorded, so a crash between them "
            "frees the legacy name with nothing marking the fold as finished"
        )
        # The source is still under its live name -- and that is safe precisely
        # because the marker is already recorded.
        assert legacy.exists()
        svc_mod.set_service(None)

    def test_a_forged_row_after_that_crash_is_still_refused(self, tmp_path, monkeypatch):
        """The consequence the order exists to prevent, driven end to end."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.eventlog import service as svc_mod
        from kiro_crew.eventlog.types import ACTIVITY_RECORD

        svc_mod.set_service(None)
        self._legacy("jane", [{"ts": 1, "via": "cli", "project": "p"}])
        self._crash_only_the_retirement(monkeypatch, svc_mod)
        svc = svc_mod.get_service()
        svc.ensure("jane", "Jane")

        # Whoever can write that directory appends under the still-live name.
        self._legacy("jane", [{"ts": 2, "via": "forged", "project": "p"}])
        svc_mod.set_service(None)
        svc = svc_mod.get_service()
        svc.ensure("jane", "Jane")
        rows = [e for e in svc.history("jane", limit=None) if e["type"] == ACTIVITY_RECORD]
        assert len(rows) == 1, (
            "a row written after a crash-interrupted retirement was folded in as "
            "trusted activity"
        )
        svc_mod.set_service(None)

    def test_the_marker_is_fsynced_not_only_written(self, tmp_path, monkeypatch):
        # CONTROL on the barrier itself: the ordering is worthless if the directory
        # entry is still only in the page cache when the rename lands.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.eventlog import service as svc_mod

        svc_mod.set_service(None)
        self._legacy("kara", [{"ts": 1, "via": "cli", "project": "p"}])
        synced: list[str] = []
        real = svc_mod.fsync_dir

        def _spy(p, **k):
            synced.append(str(p))
            return real(p, **k)

        monkeypatch.setattr(svc_mod, "fsync_dir", _spy)
        svc = svc_mod.get_service()
        svc.ensure("kara", "Kara")
        fenced = svc_mod._legacy_folded_marker_path("kara")
        assert fenced is not None
        assert str(fenced.parent) in synced, "the marker's directory entry was never fsynced"
        svc_mod.set_service(None)


class TestAnIncompleteFoldIsNotFinalised:
    """Retirement is one-way, so finalising a partial fold discards rows for good.

    The byte budget makes a partial read a NORMAL outcome for a large file, not an
    error -- which is exactly why the retirement cannot be unconditional: the rows
    the budget stopped short of would be renamed out of reach having never been read.
    """

    def test_a_budget_truncated_fold_leaves_the_source_in_place(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import members
        from kiro_crew.eventlog import service as svc_mod

        svc_mod.set_service(None)
        d = members.member_dir("gina")
        d.mkdir(parents=True, exist_ok=True)
        rows = [{"ts": i, "via": "cli", "project": "p"} for i in range(200)]
        legacy = d / members.ACTIVITY_FILE_NAME
        legacy.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        monkeypatch.setattr(svc_mod, "MAX_LEGACY_ACTIVITY_BYTES", len(json.dumps(rows[0])) * 3)

        svc = svc_mod.get_service()
        svc.ensure("gina", "Gina")

        assert legacy.exists(), (
            "an incomplete fold was finalised, so the rows the budget never reached "
            "are renamed out of reach and lost"
        )
        marker = legacy.with_name(legacy.name + svc_mod.LEGACY_MIGRATED_SUFFIX)
        assert not marker.exists()
        svc_mod.set_service(None)

    def test_a_complete_fold_is_still_finalised(self, tmp_path, monkeypatch):
        # CONTROL. Without this, never finalising would satisfy the test above while
        # leaving the forgery path open for every member.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import members
        from kiro_crew.eventlog import service as svc_mod

        svc_mod.set_service(None)
        d = members.member_dir("hank")
        d.mkdir(parents=True, exist_ok=True)
        legacy = d / members.ACTIVITY_FILE_NAME
        legacy.write_text(
            json.dumps({"ts": 1, "via": "cli", "project": "p"}) + "\n", encoding="utf-8"
        )
        svc = svc_mod.get_service()
        svc.ensure("hank", "Hank")
        assert not legacy.exists()
        assert legacy.with_name(legacy.name + svc_mod.LEGACY_MIGRATED_SUFFIX).exists()
        svc_mod.set_service(None)


class TestTheFoldIsSerializedAcrossProcesses:
    """A per-append lease does not serialize a FOLD.

    The append path takes and releases the lease inside each append, so between
    two of them a second process is free to run its own fold: both snapshot a log
    with no activity records, both find every legacy row unmatched, and both
    append it. Retirement then makes the duplication permanent and silent. This
    log has two ordinary writers, so two folds of one member during an upgrade is
    routine rather than exotic.
    """

    @staticmethod
    def _legacy(slug, rows):
        from kiro_crew import members

        d = members.member_dir(slug)
        d.mkdir(parents=True, exist_ok=True)
        path = d / members.ACTIVITY_FILE_NAME
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return path

    def test_the_fold_holds_the_units_lease_for_its_whole_length(self, tmp_path, monkeypatch):
        """Asserted on the lease being HELD while the fold runs, not on it being
        taken: a lease acquired and released around each append is exactly the
        state that already existed and that duplicates rows."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.eventlog import service as svc_mod

        svc_mod.set_service(None)
        self._legacy("nadia", [{"ts": 1, "via": "cli", "project": "p"}])

        held_during_fold: list[bool] = []
        real_locked = svc_mod.MemberEventLogService._migrate_legacy_locked

        def _spy(self, slug, name, log):
            # A SECOND acquire from this process succeeds by reference count, so it
            # cannot answer "is it held". Asked of the lease module's own registry
            # instead, which is what another process's acquire consults.
            from kiro_crew.crew_log import lease as lease_mod

            held_during_fold.append(bool(getattr(lease_mod, "_held", {})))
            return real_locked(self, slug, name, log)

        monkeypatch.setattr(svc_mod.MemberEventLogService, "_migrate_legacy_locked", _spy)
        svc = svc_mod.get_service()
        svc.ensure("nadia", "Nadia")

        assert held_during_fold == [True], (
            "the fold ran without the unit's lease held, so a second process can "
            "fold the same member's legacy rows at the same time"
        )
        svc_mod.set_service(None)

    def test_a_refused_lease_folds_nothing_and_retires_nothing(self, tmp_path, monkeypatch):
        """The refusal path, which is the half that must not lose rows: another
        process holds the log, so this pass writes nothing -- and must leave the
        source under its live name so the next ensure folds it."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.eventlog import service as svc_mod
        from kiro_crew.eventlog.types import ACTIVITY_RECORD

        svc_mod.set_service(None)
        legacy = self._legacy("orla", [{"ts": 1, "via": "cli", "project": "p"}])
        monkeypatch.setattr(
            svc_mod.MemberEventLogService, "_hold_unit", staticmethod(lambda _s: None)
        )
        svc = svc_mod.get_service()
        svc.ensure("orla", "Orla")

        rows = [e for e in svc.history("orla", limit=None) if e["type"] == ACTIVITY_RECORD]
        assert rows == [], "a refused lease must fold nothing"
        assert legacy.exists(), (
            "a refused lease retired the source, so the rows it held are gone and "
            "the next ensure has nothing to fold"
        )

        # CONTROL. With the lease available the same member folds and retires, so
        # the assertions above cannot pass by the fold being broken outright.
        svc_mod.set_service(None)
        monkeypatch.undo()
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        svc = svc_mod.get_service()
        svc.ensure("orla", "Orla")
        rows = [e for e in svc.history("orla", limit=None) if e["type"] == ACTIVITY_RECORD]
        assert len(rows) == 1
        svc_mod.set_service(None)


class TestWhatOneLogRetainsIsBounded:
    """A member log has no compaction and no rotation, so its length grows for the
    member's life -- and a cold roster read loads one per member. Retaining the
    complete history therefore makes one read's cost a function of how long the
    crew has existed.

    Asserted on what is RETAINED, which is the only thing that separates a bound
    from a slice: a list built whole and then trimmed has already peaked.
    """

    def test_a_long_log_retains_only_the_newest_window(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.eventlog import log as log_mod

        monkeypatch.setattr(log_mod, "MAX_RETAINED_EVENTS", 5)
        log = log_mod.MemberLog("pia")
        log.create("Pia")
        for i in range(12):
            log.append(types.ACTIVITY_RECORD, {"ts": i, "via": "cli", "project": f"p{i}"})

        fresh = log_mod.MemberLog("pia")
        fresh.load()
        assert len(fresh.events) == 5, f"retained {len(fresh.events)} of a 5-event bound"
        assert fresh.retained_from == fresh.events[0]["seq"]
        assert fresh.retained_from > 1, "the window must start above the log's first seq"

    def test_a_short_log_is_not_reported_as_a_window(self, tmp_path, monkeypatch):
        # CONTROL. Without this, reporting every load as truncated would satisfy
        # the test above while sending every read to the store for no reason.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.eventlog import log as log_mod

        monkeypatch.setattr(log_mod, "MAX_RETAINED_EVENTS", 5)
        log = log_mod.MemberLog("quinn")
        log.create("Quinn")
        for i in range(3):
            log.append(types.ACTIVITY_RECORD, {"ts": i, "via": "cli", "project": f"p{i}"})

        fresh = log_mod.MemberLog("quinn")
        fresh.load()
        assert fresh.retained_from == 0, "a log within the bound is not a window"

    def test_history_below_the_window_is_still_answered(self, tmp_path, monkeypatch):
        """The bound must not become a wrong answer: a page reaching below the
        retained window is read from the store, whose own page is bounded by the
        limit, so the cost is the page rather than the log."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.eventlog import log as log_mod

        monkeypatch.setattr(log_mod, "MAX_RETAINED_EVENTS", 3)
        log = log_mod.MemberLog("rhea")
        log.create("Rhea")
        for i in range(10):
            log.append(types.ACTIVITY_RECORD, {"ts": i, "via": "cli", "project": f"p{i}"})

        fresh = log_mod.MemberLog("rhea")
        fresh.load()
        assert fresh.retained_from > 1
        # `before` sits below the retained window entirely.
        page = fresh.history(before=fresh.retained_from, limit=2)
        assert len(page) == 2, "a page below the window came back empty"
        assert all(e["seq"] < fresh.retained_from for e in page)
        # And the whole history is still reachable, streamed rather than retained.
        every = fresh.history(before=None, limit=None)
        assert len(every) == 10, f"streamed {len(every)} of the 10 appended events"

    def test_a_fold_sees_every_event_not_just_the_window(self, tmp_path, monkeypatch):
        """The consequence the bound could have broken, asserted on the FOLD.

        The rules and binding items are guarded only by this membership test --
        neither legacy source is retired -- so a test against the retained tail
        reports them unmigrated once their event ages out, and appends a second
        copy on every later ensure. Driven through ``ensure`` rather than asserted
        on the log's own stream, because what the stream carries is a property of
        the log and holds whichever collection the fold happens to read.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import members
        from kiro_crew.eventlog import log as log_mod
        from kiro_crew.eventlog import service as svc_mod

        monkeypatch.setattr(log_mod, "MAX_RETAINED_EVENTS", 2)
        monkeypatch.setattr(members, "read_member_rules", lambda slug, name: "be brief")
        svc_mod.set_service(None)
        svc = svc_mod.get_service()

        svc.ensure("sara", "Sara")
        rules = [e for e in svc.history("sara", limit=None) if e["type"] == types.MEMBER_RULES]
        assert (
            len(rules) == 1
        ), f"precondition: the fold must import the rules once, got {len(rules)}"

        # Age that event out of the retained window, then fold again.
        for i in range(8):
            svc.append("sara", types.ACTIVITY_RECORD, {"ts": i, "via": "cli", "project": f"p{i}"})
        aged = log_mod.MemberLog("sara")
        aged.load()
        assert types.MEMBER_RULES not in {
            e["type"] for e in aged.events
        }, "precondition: the rules event must have aged out of the retained window"

        svc_mod.set_service(None)
        svc = svc_mod.get_service()
        svc.ensure("sara", "Sara")
        again = [e for e in svc.history("sara", limit=None) if e["type"] == types.MEMBER_RULES]
        assert len(again) == 1, (
            f"the fold re-imported the rules it already holds ({len(again)} copies): its "
            "membership test read the retained window, so an event below it reads as absent"
        )
        svc_mod.set_service(None)


class TestARedactionFailureDropsTheFrameInsteadOfPublishingIt:
    """Redaction on this path exists because the value can embed a credential.

    Falling back to the raw view would publish exactly what it was added to withhold,
    on the one input redaction could not handle -- so the failure mode would leak more
    reliably than the success path protects.
    """

    def test_a_frame_whose_redaction_raises_is_not_broadcast(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.eventlog import service as svc_mod

        svc_mod.set_service(None)
        svc = svc_mod.get_service()
        sent: list[tuple] = []

        def _boom(_value):
            raise RecursionError("nesting beyond the interpreter limit")

        monkeypatch.setattr(svc_mod, "_redact_projection_value", _boom)
        svc.ensure("rina", "Rina")
        svc._broadcast = lambda *a: sent.append(a)  # type: ignore[method-assign]
        svc._on_change("rina", "activity", {"secret": "AKIAEXAMPLE"}, 1)

        assert sent == [], f"an unredacted projection reached the socket: {sent!r}"

    def test_a_frame_that_redacts_cleanly_is_still_broadcast(self, tmp_path, monkeypatch):
        # CONTROL. Without this, never broadcasting would satisfy the test above
        # while making the projection feed dead.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.eventlog import service as svc_mod

        svc_mod.set_service(None)
        svc = svc_mod.get_service()
        sent: list[tuple] = []

        svc.ensure("rina", "Rina")
        svc._broadcast = lambda *a: sent.append(a)  # type: ignore[method-assign]
        svc._on_change("rina", "activity", {"plain": "value"}, 1)

        assert len(sent) == 1, "a redactable projection was dropped"


class TestOneBadByteCannotBlockEveryLaterActivityWrite:
    """The legacy file is agent-writable and read on every ``ensure``.

    A strict decode raises from ``readline``, one frame outside the ``except
    ValueError`` that guards ``json.loads`` -- so a single torn byte would propagate
    out of the migration and lose every later activity record, permanently.
    """

    def test_invalid_utf8_skips_its_line_and_the_rest_still_folds(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import members
        from kiro_crew.eventlog import service as svc_mod

        svc_mod.set_service(None)
        d = members.member_dir("bea")
        d.mkdir(parents=True, exist_ok=True)
        good = {"member": "Bea", "ts": "2026-01-01T00:00:00Z", "via": "cli", "project": "p"}
        legacy = d / members.ACTIVITY_FILE_NAME
        # A torn write: one row's bytes are not valid UTF-8.
        legacy.write_bytes(
            json.dumps(good).encode("utf-8")
            + b"\n"
            + b'{"member": "Bea", "via": "\xff\xfe cli"}\n'
            + json.dumps({**good, "ts": "2026-01-02T00:00:00Z"}).encode("utf-8")
            + b"\n"
        )

        svc = svc_mod.get_service()
        svc.ensure("bea", "Bea")

        folded = [
            e
            for e in svc.history("bea", before=None, limit=None)
            if e.get("type") == types.ACTIVITY_RECORD
        ]
        assert (
            len(folded) == 2
        ), f"a malformed byte cost more than its own line: {len(folded)} of 2 good rows"

    def test_a_later_activity_write_still_lands(self, tmp_path, monkeypatch):
        # This is the loss the finding names: not the fold, the writes AFTER it.
        from kiro_crew import members
        from kiro_crew.eventlog import service as svc_mod

        monkeypatch.setattr(members, "data_home", lambda: tmp_path)
        (tmp_path / "members").mkdir(exist_ok=True)
        svc_mod.set_service(None)
        d = members.member_dir("bea")
        d.mkdir(parents=True, exist_ok=True)
        (d / members.ACTIVITY_FILE_NAME).write_bytes(b'{"member": "Bea", "via": "\xff"}\n')

        assert members.record_activity("Bea", "sess-1", "persistent", via="chat") is True


class TestAnImportedLegacyRowIsMarkedAndDoesNotDuplicate:
    """The log is fenced and ordered; the legacy file it folds from is neither.

    A reader is entitled to treat what is in the log as written through those
    guarantees, so a row that was not has to say so.
    """

    def _write_legacy(self, monkeypatch, tmp_path, rows):
        from kiro_crew import members

        d = members.member_dir("pam")
        d.mkdir(parents=True, exist_ok=True)
        (d / members.ACTIVITY_FILE_NAME).write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )

    def test_an_imported_row_carries_the_provenance_marker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.eventlog import service as svc_mod

        svc_mod.set_service(None)
        row = {"member": "Pam", "ts": "2026-01-01T00:00:00Z", "via": "cli", "project": "p"}
        self._write_legacy(monkeypatch, tmp_path, [row])

        svc = svc_mod.get_service()
        svc.ensure("pam", "Pam")

        imported = [
            e["data"]
            for e in svc.history("pam", before=None, limit=None)
            if e.get("type") == types.ACTIVITY_RECORD
        ]
        assert len(imported) == 1
        assert imported[0].get(svc_mod.LEGACY_PROVENANCE_KEY) is True, (
            "an agent-writable legacy row is indistinguishable from a record this "
            f"service appended itself: {imported[0]!r}"
        )

    def test_the_marker_does_not_make_a_second_fold_duplicate_the_row(self, tmp_path, monkeypatch):
        # The marker is inside the row, and migration identity IS the row -- so
        # without the strip in _activity_key every later pass re-appends the file.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.eventlog import service as svc_mod

        svc_mod.set_service(None)
        row = {"member": "Pam", "ts": "2026-01-01T00:00:00Z", "via": "cli", "project": "p"}
        self._write_legacy(monkeypatch, tmp_path, [row])

        svc = svc_mod.get_service()
        svc.ensure("pam", "Pam")
        # Put the source back and re-run the fold, which is what a retirement that
        # did not complete leaves behind.
        self._write_legacy(monkeypatch, tmp_path, [row])
        svc_mod.set_service(None)
        svc2 = svc_mod.get_service()
        svc2.ensure("pam", "Pam")

        again = [
            e
            for e in svc2.history("pam", before=None, limit=None)
            if e.get("type") == types.ACTIVITY_RECORD
        ]
        assert len(again) == 1, f"the row was imported {len(again)} times"

    def test_a_row_this_service_appends_carries_no_marker(self, tmp_path, monkeypatch):
        # CONTROL. Marking everything would satisfy the first test and destroy the
        # distinction the marker exists to draw.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import members
        from kiro_crew.eventlog import service as svc_mod

        monkeypatch.setattr(members, "data_home", lambda: tmp_path)
        (tmp_path / "members").mkdir(exist_ok=True)
        svc_mod.set_service(None)
        assert members.record_activity("Pam", "sess-1", "persistent", via="chat") is True
        svc = svc_mod.MemberEventLogService(tmp_path / "members")

        native = [
            e["data"]
            for e in svc.history("pam", before=None, limit=None)
            if e.get("type") == types.ACTIVITY_RECORD
        ]
        assert native, "no native activity record was written"
        assert svc_mod.LEGACY_PROVENANCE_KEY not in native[0]


class TestPrimingFoldsInOnePassOverAStream:
    """Priming iterated once per DEFINITION, which forces its caller to hand over a
    list -- and that list is as long as the member's life. One pass folding every
    definition as each event arrives lets the caller stream instead.
    """

    def test_a_generator_primes_every_unit(self):
        reg = ProjectionRegistry()
        for unit in all_units():
            reg.register(unit)
        events = [
            {
                "type": types.MEMBER_BINDING,
                "seq": 1,
                "time": "t",
                "data": {"slot_key": "member-tess"},
            },
            {
                "type": types.ACTIVITY_RECORD,
                "seq": 2,
                "time": "t",
                "data": {"ts": 1, "via": "cli", "project": "p"},
            },
        ]
        # A ONE-SHOT iterator, which is what a streaming caller hands over. A
        # per-definition loop drains it on the first unit and folds nothing into
        # the rest, so this fails on any implementation that re-reads. Ordered so
        # the two units fold DIFFERENT events: with one event both could be served
        # by a single pass and the drained-iterator bug would stay invisible.
        reg.prime("tess", iter(events))
        snap = reg.snapshot("tess")
        values = snap.get("values", {})
        assert (
            values.get("roster", {}).get("slot_key") == "member-tess"
        ), "the unit folding the FIRST event lost it"
        assert values.get("activity", {}).get("recent"), (
            "a later unit folded nothing, so priming re-read an iterator that was "
            "already drained"
        )
        assert snap.get("asOfSeq") == 2


def test_a_commit_during_the_read_is_not_hidden_by_the_stamp(tmp_path):
    """A second writer's commit landing DURING a read must not be stamped away.

    `refresh_if_changed` skips the reload when the file's `(size, mtime_ns)` still
    equals the one recorded at load time. So a stat sampled only AFTER the parse
    records a state this instance has not read: an entry committed between the
    iterator's EOF and that sample gets its size and mtime stamped onto the events
    from before it, every later refresh finds "no change", and the entry stays
    invisible for the life of the process. This log has two ordinary writers by
    design, so the window is reachable rather than theoretical.

    Driven with a real second instance (the way the interleaved-writers test above
    models two processes) and a real append, not a faked stat, so the assertion is
    about an event actually becoming visible.
    """
    reader = MemberLog("alice")
    reader.create("Alice")
    reader.append(types.MEMBER_MESSAGE, {"ts": 1.0, "preview": "first"})
    reader.load()
    assert [e["seq"] for e in reader.events] == [1], reader.events

    other = MemberLog("alice")
    real_stat = reader._stat
    calls = {"n": 0}

    def _stat_with_a_commit_in_the_window():
        # Hook the SECOND sample -- the post-read one -- so the commit lands
        # inside the interval the stamp is supposed to describe. Hooking the
        # first would move the whole window instead of splitting it, and both
        # samples would then see the same file.
        calls["n"] += 1
        if calls["n"] == 2:
            other.append(types.MEMBER_MESSAGE, {"ts": 2.0, "preview": "raced"})
        return real_stat()

    reader._stat = _stat_with_a_commit_in_the_window  # type: ignore[method-assign]
    reader.load()
    assert calls["n"] >= 2, (
        "the load sampled the stat fewer than twice, so no commit was injected "
        "into the window and this test measured nothing"
    )
    reader._stat = real_stat  # type: ignore[method-assign]

    # The read itself legitimately missed the raced entry -- it committed after
    # the iterator finished. What must NOT happen is the stamp claiming this
    # instance has seen it.
    assert reader._loaded_stat is None, (
        "the load stamped a file state it had not read "
        f"({reader._loaded_stat!r}), so the raced commit is now invisible"
    )
    assert reader.refresh_if_changed() is True, (
        "the refresh found no change, so the raced commit is hidden for the life "
        "of this instance"
    )
    assert [e["seq"] for e in reader.events] == [1, 2], reader.events
    assert [e["data"]["preview"] for e in reader.events] == ["first", "raced"]
