"""Tests for the per-member append-only event log backend core."""

from __future__ import annotations

import json

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
    assert e0["seq"] == 0
    assert e1["seq"] == 1
    assert e0["type"] == types.MEMBER_MESSAGE
    assert isinstance(e0["time"], int)

    fresh = MemberLog("alice")
    fresh.load()
    assert [e["seq"] for e in fresh.events] == [0, 1]
    assert fresh.committed_bytes == log.path.stat().st_size


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
    assert [a0["seq"], b0["seq"], a1["seq"], b1["seq"]] == [0, 1, 2, 3]

    # A cold reader parses all four in order (a duplicate seq would show up here
    # as a repeated or missing number).
    cold = MemberLog("alice")
    cold.load()
    assert [e["seq"] for e in cold.events] == [0, 1, 2, 3]
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
    assert [e["seq"] for e in fresh.events] == [0]
    assert fresh.path.stat().st_size == committed  # truncated back
    assert fresh.committed_bytes == committed


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

    assert [e["seq"] for e in fresh.events] == [0]
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
    assert [e["seq"] for e in h] == [4, 3, 2]
    h2 = log.history(before=2, limit=10)
    assert [e["seq"] for e in h2] == [1, 0]


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
    assert svc.last_seq("alice") == 1
    assert svc.slugs() == ["alice"]
    assert svc.last_seqs() == {"alice": 1}


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


def test_push_projection_redacts_the_schema_not_only_the_value():
    """The render schema crosses the same live WS boundary as the value and is
    app-authored, so a credential in a schema ``title`` must be scrubbed too --
    redacting only the value would leak it to the browser until a reload."""
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers.eventlog import _push_projection

    sent: dict = {}
    state = SimpleNamespace(
        broadcast_ws=lambda frame, payload: sent.update(frame=frame, payload=payload)
    )
    request = SimpleNamespace(app={"state": state})
    unit = SimpleNamespace(id_field="memberId", frame="member_projection")

    secret = "https://evil.example/x?token=AKIAIOSFODNN7EXAMPLE"
    _push_projection(
        request,
        unit,
        "code-reviewer",
        "demoapp/count",
        value=1,
        seq=1,
        schema={"kind": "badge", "title": secret},
    )

    blob = json.dumps(sent["payload"])
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
    assert log.last_seq() == -1
    assert log.history(None, 10) == []
    assert log.committed_bytes == 0


def test_events_after_pages_oldest_first_from_a_cursor(tmp_path):
    """The catch-up read, and the ORDER is the whole point.

    A consumer that folded up to ``after`` asks for what came next and applies it
    in sequence; handing it ``history``'s newest-first page would apply a later
    event before an earlier one and leave the projection wrong rather than stale.
    """
    log = _log(tmp_path)
    log.create("Alice")
    for i in range(5):
        log.append(types.MEMBER_CONFIG, {"i": i})

    # ``seq`` starts at 0 on the first APPEND -- the header is not an event.
    assert [e["seq"] for e in log.events_after(0, 10)] == [1, 2, 3, 4]
    # Oldest-first, which is the opposite of the timeline page.
    assert [e["seq"] for e in log.history(None, 10)] == [4, 3, 2, 1, 0]
    # Bounded by limit, still from the low end.
    assert [e["seq"] for e in log.events_after(1, 2)] == [2, 3]
    # A cursor past the end is empty rather than an error.
    assert log.events_after(99, 10) == []


def test_events_after_treats_a_negative_limit_as_unbounded(tmp_path):
    """Matches ``history``'s own reading of a negative limit, so the two agree."""
    log = _log(tmp_path)
    log.create("Alice")
    log.append(types.MEMBER_CONFIG, {"i": 0})
    log.append(types.MEMBER_CONFIG, {"i": 1})

    assert [e["seq"] for e in log.events_after(0, -1)] == [1]


def test_exists_answers_before_anything_is_written(tmp_path):
    """Callers check this to decide whether to ensure a log, so it must not load."""
    log = _log(tmp_path)

    assert log.exists() is False

    log.create("Alice")

    assert log.exists() is True
