"""Tests for the per-member append-only event log backend core."""

from __future__ import annotations

import json

import pytest
from eventlog_helpers import _isolated_home  # noqa: F401 - autouse fixture

from kiro_crew.crew_log.errors import CrewLogError
from kiro_crew.crew_log.schema import KIND_MEMBER
from kiro_crew.crew_log.store import crew_log_path
from kiro_crew.eventlog import types
from kiro_crew.eventlog.log import LogCorrupt, MemberLog
from kiro_crew.eventlog.members_projections import all_units
from kiro_crew.eventlog.service import MemberEventLogService
from kiro_crew.projection import ProjectionRegistry


# ---------------------------------------------------------------------------
# MemberLog
# ---------------------------------------------------------------------------
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


def test_redact_projection_value_scrubs_short_url_secret_param():
    """A short URL-embedded credential whose query is under the exfiltration
    heuristic's length floor and whose value is not credential-shaped
    (`?api_key=abc123`) is not altered by the exfiltration or credential passes,
    so the projection chain must apply the URL-secret-parameter layer the
    sibling roster egress carries. The roster projection block of GET /api/members
    and the live member-projection WS push both fold this function and the roster
    ROW masks the same leaf, so the value is masked here too."""
    from kiro_crew.eventlog.service import _redact_projection_value

    leaf = "https://h.example/v1?api_key=abc123"
    out = _redact_projection_value({"roster": {"model": leaf}})
    assert out["roster"]["model"] == "https://h.example/v1?api_key=[REDACTED]"
    assert "abc123" not in json.dumps(out)

    # Nested-list leaf: the str branch runs under list recursion too, so a secret
    # carried inside a list value is masked just as a nested dict value is.
    nested = _redact_projection_value({"roster": {"models": [leaf]}})
    assert nested["roster"]["models"] == ["https://h.example/v1?api_key=[REDACTED]"]
    assert "abc123" not in json.dumps(nested)


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


class TestEnsureCostsAStatOnARepeat:
    """A repeat ``ensure`` is the common case, so it has to be cheap.

    It is called once per member on every roster read and once per message. A pass
    that parses the file again and folds from the first event puts each member's
    whole history on the roster read, so a 76-member install pays a cost that grows
    with every event any of them records.

    Cheap is pinned by what a repeat does NOT do: parse the file again, replace the
    cached instance, fold from the first event, or spend a cross-process lease. The
    tests after that pin what it still MUST do -- see a foreign append, see one from
    the state a member starts life in, create an absent log, and finish the legacy
    fold -- because each of those is a way to make the first four pass for the wrong
    reason.
    """

    @staticmethod
    def _service(tmp_path, monkeypatch) -> MemberEventLogService:
        import kiro_crew.members as members

        monkeypatch.setattr(members, "data_home", lambda: tmp_path)
        root = tmp_path / "members"
        root.mkdir()
        return MemberEventLogService(root)

    def test_a_second_ensure_does_not_refold_an_unchanged_log(self, tmp_path, monkeypatch):
        svc = self._service(tmp_path, monkeypatch)
        svc.ensure("erin", "Erin")
        svc.append("erin", types.MEMBER_CONFIG, {"model": "m1"})

        held = svc._logs["erin"]
        counts = {"prime": 0, "load": 0}
        real_prime = svc._registry.prime
        real_load = held.load

        def counting_prime(store, events):
            counts["prime"] += 1
            real_prime(store, events)

        def counting_load():
            counts["load"] += 1
            real_load()

        monkeypatch.setattr(svc._registry, "prime", counting_prime)
        monkeypatch.setattr(held, "load", counting_load)

        svc.ensure("erin", "Erin")

        assert counts["prime"] == 0, "a repeat ensure folded the member from the first event"
        assert counts["load"] == 0, "a repeat ensure parsed an unchanged file again"
        assert svc._logs["erin"] is held, "a repeat ensure replaced the cached instance"
        # And the state the first pass folded is still what is served.
        assert svc.snapshot("erin")["values"][types.PROJ_ROSTER]["model"] == "m1"

    def test_a_second_ensure_takes_no_cross_process_lease(self, tmp_path, monkeypatch):
        """The lease is the expensive part of a fold that has nothing left to do."""
        svc = self._service(tmp_path, monkeypatch)
        svc.ensure("dave", "Dave")

        holds = {"n": 0}
        real_hold = svc._hold_unit

        def counting_hold(slug):
            holds["n"] += 1
            return real_hold(slug)

        monkeypatch.setattr(svc, "_hold_unit", counting_hold)
        svc.ensure("dave", "Dave")

        assert holds["n"] == 0, "a repeat ensure spent a cross-process lease acquire"

    def test_a_second_ensure_sees_what_another_process_appended(self, tmp_path, monkeypatch):
        """Correctness of the cheap path: it may skip work, never a commit.

        The log has two ordinary writers, so the file can grow between two calls.
        Asked of the REGISTRY rather than through ``snapshot``, because a read
        refreshes too -- going through one could not say whether ``ensure`` crossed
        the gap or the read did.
        """
        svc = self._service(tmp_path, monkeypatch)
        svc.ensure("erin", "Erin")
        svc.append("erin", types.SLOT_OPENED, {"slot_key": "member-erin"})

        other = MemberLog("erin")
        other.load()
        stranger = other.append(types.SLOT_OPENED, {"slot_key": "worker-from-another-process"})

        svc.ensure("erin", "Erin")

        folded = svc._registry.snapshot("erin")["values"][types.PROJ_DRIVING]["open"]
        assert folded == ["member-erin", "worker-from-another-process"], folded
        assert svc._logs["erin"].last_seq() == stranger["seq"]

    def test_a_foreign_append_is_folded_when_the_member_has_folded_nothing_yet(
        self, tmp_path, monkeypatch
    ):
        """The same correctness, from the state a member starts life in.

        A log created with a header and no events leaves every unit's cell at the
        empty watermark, so the catch-up cannot drive a range at it and has to prime.
        This is the common shape on a first roster read, where every member's log is
        published and none of them has recorded anything yet.

        The second half is what makes it matter: a local append drives every cell to
        its own seq, and the catch-up drops anything at or below that afterwards, so a
        foreign row skipped here is gone for the life of the process rather than late.
        """
        svc = self._service(tmp_path, monkeypatch)
        svc.ensure("hana", "Hana")
        assert svc._registry.observed_floor("hana") < 0, "precondition: nothing folded yet"

        other = MemberLog("hana")
        other.load()
        stranger = other.append(types.SLOT_OPENED, {"slot_key": "worker-from-another-process"})

        svc.ensure("hana", "Hana")

        folded = svc._registry.snapshot("hana")["values"][types.PROJ_DRIVING]["open"]
        assert folded == ["worker-from-another-process"], folded

        svc.append("hana", types.SLOT_OPENED, {"slot_key": "member-hana"})

        after = svc._registry.snapshot("hana")["values"][types.PROJ_DRIVING]["open"]
        assert sorted(after) == ["member-hana", "worker-from-another-process"], after
        assert svc._logs["hana"].last_seq() == stranger["seq"] + 1

    def test_a_binding_file_this_process_cannot_reach_is_not_read_as_absent(
        self, tmp_path, monkeypatch
    ):
        """An error reaching the file is not an answer about whether it is there.

        The presence question exists to tell "there is nothing to migrate" from "this
        pass did not read what is there", so an error that reads as absence defeats
        it: the member settles and its binding is never migrated in this process.
        """
        import kiro_crew.members as members
        from kiro_crew.eventlog import service as service_mod

        svc = self._service(tmp_path, monkeypatch)

        class Unreachable:
            def stat(self):
                raise PermissionError("binding file is not reachable by this process")

        monkeypatch.setattr(members, "dm_binding_path", lambda slug: Unreachable())
        assert service_mod._legacy_binding_present("iris") is True

        svc.ensure("iris", "Iris")
        assert "iris" not in svc._legacy_folded, "an unreachable binding settled the member"

    def test_an_absent_log_is_still_created_with_its_header(self, tmp_path, monkeypatch):
        """``ensure`` is the path that PUBLISHES a member's log, cheap repeat or not."""
        svc = self._service(tmp_path, monkeypatch)
        assert not MemberLog("gale").exists(), "precondition: nothing on disk for this slug"

        svc.ensure("gale", "Gale")

        assert crew_log_path(KIND_MEMBER, "gale").exists()
        created = MemberLog("gale")
        created.load()
        assert created.header is not None
        assert created.header["id"] == "gale"
        assert created.header["name"] == "Gale"
        assert svc.logged_name("gale") == "Gale"

    def test_a_first_ensure_still_completes_the_legacy_fold(self, tmp_path, monkeypatch):
        """One pass per process is the budget, and the pass is the FIRST one.

        The activity rows are written as FILES by hand: ``record_activity`` writes to
        the event log, so building that half of the fixture through it would leave no
        legacy file for the fold to read.
        """
        import json as _json

        import kiro_crew.members as members

        svc = self._service(tmp_path, monkeypatch)
        members.write_dm_binding("dave", member="Dave", slot_key=members.member_slot_key("dave"))
        members.write_member_rules("dave", member="Dave", text="be nice")
        rows = [
            {"ts": 1000 + i, "member": "Dave", "session": f"s{i}", "mode": "persistent"}
            for i in range(3)
        ]
        dest = members.member_dir("dave") / members.ACTIVITY_FILE_NAME
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            "".join(_json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
        )

        svc.ensure("dave", "Dave")

        events = svc.history("dave", before=None, limit=100)
        etypes = [e["type"] for e in events]
        assert types.MEMBER_BINDING in etypes, etypes
        assert types.MEMBER_RULES in etypes, etypes
        sessions = sorted(
            str(e["data"].get("session")) for e in events if e["type"] == types.ACTIVITY_RECORD
        )
        assert sessions == ["s0", "s1", "s2"], sessions

    def test_a_short_legacy_read_is_not_recorded_as_a_completed_fold(self, tmp_path, monkeypatch):
        """A read that came back short has rows it never saw, so it settles nothing.

        ``_read_legacy_activity_files`` answers ``complete=False`` by RETURNING, not by
        raising -- an unreachable path, a file over the byte budget, an ``OSError``
        part-way. A memo written on "the pass did not throw" would drop those rows for
        the life of the process, where the unretired source is meant to be re-read.
        """
        import json as _json

        import kiro_crew.members as members
        from kiro_crew.eventlog import service as service_mod

        svc = self._service(tmp_path, monkeypatch)
        rows = [
            {"ts": 1000 + i, "member": "Dave", "session": f"s{i}", "mode": "persistent"}
            for i in range(3)
        ]
        dest = members.member_dir("dave") / members.ACTIVITY_FILE_NAME
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            "".join(_json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
        )

        real_read = service_mod._read_legacy_activity_files
        calls = {"n": 0}

        def short_first(slug):
            calls["n"] += 1
            if calls["n"] == 1:
                # What the byte budget hands back: the rows read so far, and False.
                return [rows[0]], False
            return real_read(slug)

        monkeypatch.setattr(service_mod, "_read_legacy_activity_files", short_first)

        def _sessions() -> list[str]:
            return sorted(
                str(e["data"].get("session"))
                for e in svc.history("dave", before=None, limit=100)
                if e["type"] == types.ACTIVITY_RECORD
            )

        svc.ensure("dave", "Dave")
        assert _sessions() == ["s0"], f"precondition: only the read rows land: {_sessions()}"

        svc.ensure("dave", "Dave")
        assert _sessions() == ["s0", "s1", "s2"], _sessions()

    def test_a_failed_binding_read_is_not_recorded_as_a_completed_fold(self, tmp_path, monkeypatch):
        """Same rule for the binding and rules reads: a swallowed failure settles nothing."""
        import kiro_crew.members as members

        svc = self._service(tmp_path, monkeypatch)
        members.write_dm_binding("dave", member="Dave", slot_key=members.member_slot_key("dave"))

        real_binding = members.read_dm_binding
        calls = {"n": 0}

        def failing_first(slug):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("binding store unreadable")
            return real_binding(slug)

        monkeypatch.setattr(members, "read_dm_binding", failing_first)

        svc.ensure("dave", "Dave")
        etypes = [e["type"] for e in svc.history("dave", before=None, limit=100)]
        assert types.MEMBER_BINDING not in etypes, f"precondition: the read failed: {etypes}"

        svc.ensure("dave", "Dave")
        etypes = [e["type"] for e in svc.history("dave", before=None, limit=100)]
        assert types.MEMBER_BINDING in etypes, etypes

    def test_an_unreadable_binding_file_is_not_recorded_as_a_completed_fold(
        self, tmp_path, monkeypatch
    ):
        """The reachable version of the case above, with nothing patched.

        ``read_dm_binding`` is total by contract: it answers "not bound" for a
        malformed file exactly as it does for an absent one, and it answers by
        RETURNING. A fold that only watched for a raised exception would record this
        member as settled with its binding never migrated -- while the file is still
        sitting there, which is the definition of work a later pass can do.
        """
        import kiro_crew.members as members

        svc = self._service(tmp_path, monkeypatch)
        path = members.dm_binding_path("dave")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json", encoding="utf-8")
        assert members.read_dm_binding("dave") is None, "precondition: it reads as not bound"

        svc.ensure("dave", "Dave")
        etypes = [e["type"] for e in svc.history("dave", before=None, limit=100)]
        assert types.MEMBER_BINDING not in etypes, f"precondition: nothing migrated: {etypes}"

        # The operator repairs the file. A later call in the SAME process has to pick
        # it up, which it can only do if the first pass settled nothing.
        members.write_dm_binding("dave", member="Dave", slot_key=members.member_slot_key("dave"))
        svc.ensure("dave", "Dave")
        etypes = [e["type"] for e in svc.history("dave", before=None, limit=100)]
        assert types.MEMBER_BINDING in etypes, etypes


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
    # ``_recency_counts``), so a hardcoded date stops being inside it
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
        # Ordered by events, not timed: Windows' monotonic clock on Python 3.12 is
        # ~15.6ms steps, so an elapsed-time check has no margin at all. The hold
        # starts only once the append is about to run, so the append's first
        # attempt meets a held lease; ``releasing`` is set just BEFORE the release,
        # so an append that returns while it is still unset never waited.
        appending = threading.Event()
        releasing = threading.Event()

        def _release_after_holding() -> None:
            appending.wait(5)
            time.sleep(hold_seconds)
            releasing.set()
            lease.release(key)

        holder = threading.Thread(target=_release_after_holding, daemon=True)
        holder.start()
        try:
            appending.set()
            svc.append("waiter", ACTIVITY_RECORD, {"member": "Waiter", "ts": "x"})
            waited_out_the_holder = releasing.is_set()
        finally:
            holder.join(timeout=5)

        assert [e["type"] for e in svc.history("waiter", before=None, limit=None)] == [
            ACTIVITY_RECORD
        ]
        # The append must have WAITED, not raced the release. Without this the test
        # passes whenever the holder happens to let go first, which is a test of
        # timing rather than of the retry -- and it passed with the retry removed.
        assert waited_out_the_holder, "the append returned while the other holder still held it"

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

        slack_dir = Path(gateway_module.__file__).resolve().parent
        exits = [
            (path.read_text(encoding="utf-8").splitlines(), i)
            for path in [
                slack_dir / "gateway.py",
                *sorted((slack_dir / "gateway_runtime").glob("*.py")),
            ]
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines())
            if "os._exit(" in line
        ]
        assert exits, "no hard exit found, so this guard would pass vacuously"
        for lines, index in exits:
            window = "\n".join(lines[max(0, index - 40) : index])
            assert (
                "eventlog_hooks.drain_for_shutdown" in window
            ), f"the hard exit at line {index + 1} does not drain the member event log"


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
