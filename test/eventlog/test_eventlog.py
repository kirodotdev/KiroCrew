"""Tests for the per-member append-only event log backend core."""

from __future__ import annotations

import json
import os

import pytest

from kiro_crew.eventlog import types
from kiro_crew.eventlog.log import LogCorrupt, MemberLog
from kiro_crew.eventlog.members_projections import all_units
from kiro_crew.eventlog.projection import ProjectionRegistry
from kiro_crew.eventlog.service import MemberEventLogService


# ---------------------------------------------------------------------------
# MemberLog
# ---------------------------------------------------------------------------
def _log(tmp_path, slug="alice"):
    d = tmp_path / slug
    d.mkdir()
    return MemberLog(d / "log.jsonl")


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

    fresh = MemberLog(log.path)
    fresh.load()
    assert [e["seq"] for e in fresh.events] == [0, 1]
    assert fresh.committed_bytes == log.path.stat().st_size


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
    fresh = MemberLog(log.path)
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

    fresh = MemberLog(log.path)
    fresh.load()  # must not raise
    assert [e["seq"] for e in fresh.events] == [0]
    assert fresh.path.stat().st_size == committed  # truncated back
    assert fresh.committed_bytes == committed


def test_load_seq_gap_raises_corrupt(tmp_path):
    log = _log(tmp_path)
    log.create("Alice")
    # Hand-write an event with the wrong seq, newline-terminated (committed).
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": types.MEMBER_MESSAGE, "seq": 5, "time": 1, "data": {}}) + "\n")
    with pytest.raises(LogCorrupt) as ei:
        MemberLog(log.path).load()
    assert ei.value.line_no == 2


def test_load_unparseable_committed_line_raises(tmp_path):
    log = _log(tmp_path)
    log.create("Alice")
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
    with pytest.raises(LogCorrupt):
        MemberLog(log.path).load()


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
    assert (root / "alice" / "log.jsonl").exists()

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
