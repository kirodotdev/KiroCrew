"""Invariants the member event log holds, one case per mechanism.

One case per finding, each pinning the MECHANISM the finding named rather than a
nearby symptom: an interrupted migration that never resumed, an egress redactor
that could be made to raise, and writers keying the ledger by a lossy slug while
the readers keyed it by the member's persisted identity.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.eventlog import types
from kiro_crew.eventlog.service import (
    _TOO_DEEP,
    MemberEventLogService,
    _redact_projection_value,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


# ---------------------------------------------------------------------------
# F2 -- an interrupted migration must resume, not be skipped forever
# ---------------------------------------------------------------------------
def _plant_legacy(members, slug: str, member: str) -> None:
    """Write the pre-log files by hand, leaving NO event log behind.

    Deliberately not through ``record_activity`` / ``write_dm_binding``: those now
    write to the log itself, so using them would create the very log whose absence
    this test needs.
    """
    members.write_dm_binding(slug, member=member, slot_key=members.member_slot_key(slug))
    members.write_member_rules(slug, member=member, text="be careful")
    d = members.member_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    (d / members.ACTIVITY_FILE_NAME).write_text(
        json.dumps({"ts": 1.0, "member": member, "session": "sess-1", "via": "chat"}) + "\n",
        encoding="utf-8",
    )


def test_a_migration_interrupted_after_the_header_resumes_on_the_next_ensure(tmp_path, monkeypatch):
    """The finding: ``if log.exists(): return`` made a half-migration permanent.

    Interrupted at the real seam -- the log is created, then an append partway
    through the migration fails -- so removing the resume fails this test, and it
    does not depend on the migration's internal ordering.
    """
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()
    _plant_legacy(members, "frank", "Frank")

    svc = MemberEventLogService(root)
    real_append = svc._append_locked
    calls = {"n": 0}

    def _die_after_first(slug, log, type_, data):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("simulated crash mid-migration")
        return real_append(slug, log, type_, data)

    monkeypatch.setattr(svc, "_append_locked", _die_after_first)
    with pytest.raises(OSError):
        svc.ensure("frank", "Frank")

    first_type = svc.history("frank", before=None, limit=100)[0]["type"]
    assert (
        len(svc.history("frank", before=None, limit=100)) == 1
    ), "precondition: exactly one legacy record landed before the crash"

    # A fresh process (fresh service: the memo is per-process) finishes it.
    resumed = MemberEventLogService(root)
    resumed.ensure("frank", "Frank")

    etypes = [e["type"] for e in resumed.history("frank", before=None, limit=100)]
    assert types.MEMBER_BINDING in etypes
    assert types.MEMBER_RULES in etypes
    assert types.ACTIVITY_RECORD in etypes
    # And it did not duplicate what the interrupted run already wrote.
    assert etypes.count(first_type) == 1


def test_resuming_twice_appends_nothing_the_log_already_holds(tmp_path, monkeypatch):
    """Idempotence is what makes the resume safe to run on every ensure."""
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()
    _plant_legacy(members, "gina", "Gina")

    MemberEventLogService(root).ensure("gina", "Gina")
    first = MemberEventLogService(root)
    first.ensure("gina", "Gina")
    seq_after_first_resume = first.last_seq("gina")

    second = MemberEventLogService(root)
    second.ensure("gina", "Gina")
    assert second.last_seq("gina") == seq_after_first_resume


# ---------------------------------------------------------------------------
# F12 -- the egress redactor must be total, not raise on deep input
# ---------------------------------------------------------------------------
def test_the_egress_redactor_bounds_depth_instead_of_raising():
    """A deeply nested value is bounded, so the pass cannot raise on it.

    Built past the interpreter's own limit so a merely-larger bound would not
    pass this: the point is that the function STOPS, not that 32 is generous.
    """
    deep: dict = {"leaf": "AKIAIOSFODNN7EXAMPLE"}
    for _ in range(5000):
        deep = {"n": deep}

    out = _redact_projection_value(deep)

    flat = repr(out)
    assert _TOO_DEEP in flat
    assert "AKIAIOSFODNN7EXAMPLE" not in flat


def test_a_value_inside_the_depth_bound_is_still_redacted_in_full():
    value = {"a": {"b": {"c": "AKIAIOSFODNN7EXAMPLE"}}}
    out = _redact_projection_value(value)
    assert "AKIAIOSFODNN7EXAMPLE" not in repr(out)
    assert _TOO_DEEP not in repr(out)


def test_the_bound_and_the_door_read_the_same_number():
    """Two constants that disagree would be a payload accepted and then unreadable."""
    from kiro_crew.eventlog import contrib

    assert contrib.MAX_VALUE_DEPTH == types.MAX_VALUE_DEPTH


# ---------------------------------------------------------------------------
# F1 -- the ledger key is the member's PERSISTED identity
# ---------------------------------------------------------------------------
def test_the_ledger_key_prefers_the_persisted_member_id_over_the_lossy_slug():
    """Two names folding to one slug is the collision the finding named.

    The readers (`api_members`, `/history`) key on `member_slug`, so a writer that
    keyed on `slug_for_name` wrote where they never look -- and two colliding
    members shared one log.
    """
    from kiro_crew import eventlog_hooks, members

    class _Agent:
        def __init__(self, member_id: str) -> None:
            self.member_id = member_id

    class _Cfg:
        agents = {"Ops Bot": _Agent("ops-bot-2"), "ops bot": _Agent("")}

    cfg = _Cfg()
    assert members.slug_for_name("Ops Bot") == members.slug_for_name("ops bot")
    assert eventlog_hooks.ledger_slug("Ops Bot", cfg) == "ops-bot-2"
    assert eventlog_hooks.ledger_slug("ops bot", cfg) == members.slug_for_name("ops bot")
    assert eventlog_hooks.ledger_slug("Ops Bot", cfg) != eventlog_hooks.ledger_slug("ops bot", cfg)


def test_ledger_slug_answers_none_rather_than_guessing():
    from kiro_crew import eventlog_hooks

    assert eventlog_hooks.ledger_slug("", None) is None
    assert eventlog_hooks.ledger_slug(None, None) is None


def test_emit_for_member_writes_under_the_persisted_identity(tmp_path, monkeypatch):
    import kiro_crew.members as members
    from kiro_crew import eventlog_hooks
    from kiro_crew.eventlog import service as service_mod

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()
    svc = MemberEventLogService(root)
    monkeypatch.setattr(service_mod, "get_service", lambda: svc)

    class _Agent:
        member_id = "pinned-id"

    class _Cfg:
        agents = {"Ops Bot": _Agent()}

    eventlog_hooks.emit_for_member(
        "Ops Bot", types.MEMBER_MESSAGE, {"ts": 1.0, "preview": "hi"}, _Cfg()
    )

    assert svc.last_seq("pinned-id") >= 0
    assert svc.last_seq(members.slug_for_name("Ops Bot")) == -1


# ---------------------------------------------------------------------------
# Contributed projection rows are authority, so they live inside the fences
# ---------------------------------------------------------------------------
def test_contributed_projection_rows_live_under_the_fenced_crew_log_tree():
    """`store.values()` trusts the file and the drawer renders it.

    Outside the fences an agent's own file tools could plant a row, so the answer
    is the same one the member log got: live under the leaf both fences name.
    """
    from kiro_crew.crew_log.store import crew_log_tree_root
    from kiro_crew.eventlog.contrib import contrib_root

    root = contrib_root()
    tree = crew_log_tree_root()
    assert tree == root.parent or tree in root.parents


def test_the_file_tool_gate_refuses_a_contributed_projection_path(tmp_path):
    from kiro_crew.eventlog.contrib import contrib_root
    from kiro_crew.security.paths import is_sensitive_path

    assert is_sensitive_path(str(contrib_root() / "member" / "alice.json"))


def test_the_sandbox_masks_the_tree_those_rows_are_in():
    from kiro_crew import sandbox
    from kiro_crew.crew_log.store import crew_log_tree_root

    assert crew_log_tree_root().name in sandbox._CREW_HIDDEN_LEAVES


# ---------------------------------------------------------------------------
# Participation dedupe is exact, not a window
# ---------------------------------------------------------------------------
def test_a_session_pushed_past_any_window_is_still_deduped(tmp_path, monkeypatch):
    """A windowed probe writes the duplicate as soon as the original scrolls out."""
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()
    svc = MemberEventLogService(root)
    svc.ensure("hank", "Hank")
    svc.append("hank", types.ACTIVITY_RECORD, {"ts": 1.0, "member": "Hank", "session": "s-1"})
    for i in range(500):
        svc.append("hank", types.MEMBER_MESSAGE, {"ts": float(i), "preview": "x"})

    assert svc.has_participation("hank", "Hank", "s-1") is True
    assert svc.has_participation("hank", "Hank", "s-2") is False
    assert svc.has_participation("hank", "Someone Else", "s-1") is False


def test_the_index_survives_a_cold_load(tmp_path, monkeypatch):
    """It is rebuilt from the log, so a restart does not reopen the duplicate."""
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()
    first = MemberEventLogService(root)
    first.ensure("iris", "Iris")
    first.append("iris", types.ACTIVITY_RECORD, {"ts": 1.0, "member": "Iris", "session": "s-9"})

    cold = MemberEventLogService(root)
    assert cold.has_participation("iris", "Iris", "s-9") is True


def test_a_routing_decision_is_not_a_participation_record(tmp_path, monkeypatch):
    """`decided_in` is a distinct fact and must never suppress a participation."""
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()
    svc = MemberEventLogService(root)
    svc.ensure("jane", "Jane")
    svc.append("jane", types.ACTIVITY_RECORD, {"ts": 1.0, "member": "Jane", "decided_in": "s-3"})
    assert svc.has_participation("jane", "Jane", "s-3") is False


def test_record_activity_dedupes_across_a_long_log(tmp_path, monkeypatch):
    """The caller's own contract, end to end through `members.record_activity`."""
    import kiro_crew.members as members
    from kiro_crew.eventlog import service as service_mod

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()
    svc = MemberEventLogService(root)
    monkeypatch.setattr(service_mod, "get_service", lambda: svc)
    monkeypatch.setattr(members, "member_slug", lambda name, config=None: "kate")

    assert (
        members.record_activity("Kate", "sess-1", "persistent", via="chat", dedupe_session=True)
        is True
    )
    for i in range(400):
        svc.append("kate", types.MEMBER_MESSAGE, {"ts": float(i), "preview": "x"})
    assert (
        members.record_activity("Kate", "sess-1", "persistent", via="chat", dedupe_session=True)
        is False
    )
    records = [
        e
        for e in svc.history("kate", before=None, limit=10_000)
        if e.get("type") == types.ACTIVITY_RECORD
    ]
    assert len(records) == 1


def test_a_legacy_session_is_deduped_even_on_the_very_first_append(tmp_path, monkeypatch):
    """The probe must see the member's whole past, which includes the legacy fold.

    With no ledger yet, an absent log answers "nothing recorded"; the ensure that
    follows then folds the legacy activity file in, and the append writes the
    participation record a second time. Ordering ensure first is the fix, so this
    starts from legacy files and NO log.
    """
    import kiro_crew.members as members
    from kiro_crew.eventlog import service as service_mod

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()
    d = members.member_dir("liam")
    d.mkdir(parents=True, exist_ok=True)
    (d / members.ACTIVITY_FILE_NAME).write_text(
        json.dumps({"ts": 1.0, "member": "Liam", "session": "sess-legacy", "via": "chat"}) + "\n",
        encoding="utf-8",
    )

    svc = MemberEventLogService(root)
    monkeypatch.setattr(service_mod, "get_service", lambda: svc)
    monkeypatch.setattr(members, "member_slug", lambda name, config=None: "liam")

    wrote = members.record_activity(
        "Liam", "sess-legacy", "persistent", via="chat", dedupe_session=True
    )

    records = [
        e
        for e in svc.history("liam", before=None, limit=10_000)
        if e.get("type") == types.ACTIVITY_RECORD
    ]
    assert wrote is False, "the legacy record already covers this session"
    assert len(records) == 1, f"duplicated the legacy participation record: {records}"


# ---------------------------------------------------------------------------
# L1 -- a completed migration must be durable, so the legacy files are read ONCE
# ---------------------------------------------------------------------------
def test_a_completed_migration_is_recorded_in_the_log(tmp_path, monkeypatch):
    """The marker goes in, and it goes in LAST.

    Last is the whole point: its job is to prove every legacy record landed, so a
    marker sitting anywhere but the end would claim a migration that had not
    finished.
    """
    import kiro_crew.members as members
    from kiro_crew.eventlog import types

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()
    _plant_legacy(members, "gina", "Gina")

    svc = MemberEventLogService(root)
    svc.ensure("gina", "Gina")

    events = svc.events_after("gina", after=-1, limit=100)
    assert [e["type"] for e in events].count(types.MEMBER_MIGRATED) == 1
    assert events[-1]["type"] == types.MEMBER_MIGRATED, "the marker is appended last"


def test_a_legacy_row_planted_after_the_migration_is_never_imported(tmp_path, monkeypatch):
    """The finding: completion held in memory only, so every restart re-imported.

    The pre-log files sit outside the fence and an agent can write them. With the
    completion remembered per PROCESS, a row planted after the migration is folded
    into the fenced ledger on the next start and counted as history the member
    actually has. A fresh service instance is that next start.
    """
    import kiro_crew.members as members
    from kiro_crew.eventlog import types

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()
    _plant_legacy(members, "hank", "Hank")

    MemberEventLogService(root).ensure("hank", "Hank")

    # Plant a row the migration never saw, exactly as an agent with write access to
    # the pre-log files would.
    (members.member_dir("hank") / members.ACTIVITY_FILE_NAME).write_text(
        json.dumps({"ts": 2.0, "member": "Hank", "session": "forged", "via": "chat"}) + "\n",
        encoding="utf-8",
    )

    reborn = MemberEventLogService(root)
    reborn.ensure("hank", "Hank")

    events = reborn.events_after("hank", after=-1, limit=100)
    sessions = [e["data"].get("session") for e in events if e["type"] == types.ACTIVITY_RECORD]
    assert "forged" not in sessions, "a row planted after the migration was imported"
    assert sessions == ["sess-1"], "only the genuinely migrated record is present"


def test_a_migration_that_crashed_before_the_marker_runs_again(tmp_path, monkeypatch):
    """No marker means not finished, so the legacy files are still read.

    The other half of the durability claim, and the reason the marker is appended
    rather than written up front: a crash before it must leave the migration
    resumable, not recorded as done.
    """
    import kiro_crew.members as members
    from kiro_crew.eventlog import types

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()
    _plant_legacy(members, "iris", "Iris")

    svc = MemberEventLogService(root)
    real_append = svc._append_locked

    def _die_on_the_marker(slug, log, type_, data):
        if type_ == types.MEMBER_MIGRATED:
            raise OSError("simulated crash before the marker")
        return real_append(slug, log, type_, data)

    monkeypatch.setattr(svc, "_append_locked", _die_on_the_marker)
    with pytest.raises(OSError):
        svc.ensure("iris", "Iris")

    mid = svc.events_after("iris", after=-1, limit=100)
    assert types.MEMBER_MIGRATED not in [e["type"] for e in mid], "precondition: no marker"

    reborn = MemberEventLogService(root)
    reborn.ensure("iris", "Iris")
    after = reborn.events_after("iris", after=-1, limit=100)
    assert [e["type"] for e in after].count(types.MEMBER_MIGRATED) == 1
    # Resumed rather than duplicated: each legacy record is appended only when the
    # log does not already hold it.
    sessions = [e["data"].get("session") for e in after if e["type"] == types.ACTIVITY_RECORD]
    assert sessions == ["sess-1"]


class TestLegacyMigrationReadsOneDescriptor:
    """The ceiling and the read must describe the same object.

    These files sit outside the fence. A path-based stat followed by a
    path-based read measures the file the agent shows and loads the file it
    swaps in, so the ceiling is checked against an object the read never sees.
    """

    def test_a_file_that_grows_after_its_stat_is_refused(self, tmp_path, monkeypatch):
        from kiro_crew.eventlog import service as svc_mod

        # A tiny ceiling keeps the test about the rule rather than the machine.
        monkeypatch.setattr(svc_mod, "_MAX_LEGACY_ACTIVITY_BYTES", 64)

        big = b'{"ts": "2026-01-01T00:00:00Z", "member": "m", "project": "p"}\n' * 20
        real_fstat = svc_mod.os.fstat

        class _Small:
            def __init__(self, st):
                self._st = st
                self.st_size = 8

            def __getattr__(self, n):
                return getattr(self._st, n)

        def fake_fstat(fd):
            return _Small(real_fstat(fd))

        monkeypatch.setattr(svc_mod.os, "fstat", fake_fstat)

        path = tmp_path / "activity.jsonl"
        path.write_bytes(big)

        import kiro_crew.members as members_mod

        monkeypatch.setattr(members_mod, "member_dir", lambda slug: tmp_path)
        monkeypatch.setattr(members_mod, "ACTIVITY_FILE_NAME", "activity.jsonl")

        # Record what the read actually ASKS FOR. Asserting only that the rows
        # are refused is not enough: the length check after the read refuses an
        # oversized file either way, so it passes even when the whole file has
        # already been pulled into memory -- which is the exhaustion being
        # closed. The bound is the size handed to read(), so that is what this
        # measures.
        asked: list[object] = []
        real_open = open

        class _Recorder:
            def __init__(self, fh):
                self._fh = fh

            def read(self, n=-1):
                asked.append(n)
                return self._fh.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(*a, **kw):
            return _Recorder(real_open(*a, **kw))

        monkeypatch.setattr(svc_mod, "open", fake_open, raising=False)

        rows, complete = svc_mod._read_legacy_activity_files("m")

        assert rows == [], (
            "a file that passed the ceiling by lying about its size was read whole: "
            f"{len(rows)} rows"
        )
        assert complete is False, (
            "a source that existed but was refused reads as complete, which lets the "
            "migration marker claim rows it never imported"
        )
        assert asked, "the legacy read did not go through a bounded read()"
        assert all(
            isinstance(n, int) and 0 < n <= svc_mod._MAX_LEGACY_ACTIVITY_BYTES + 1 for n in asked
        ), f"the read was not bounded by the ceiling: requested {asked}"

    def test_a_migrated_row_is_marked_as_imported(self, tmp_path, monkeypatch):
        """Provenance: a migrated row must not pass as first-hand history."""
        import kiro_crew.members as members_mod
        from kiro_crew.eventlog import service as svc_mod

        path = tmp_path / "activity.jsonl"
        path.write_text(
            '{"ts": "2026-01-01T00:00:00Z", "member": "m", "project": "p", '
            '"legacy_import": false}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(members_mod, "member_dir", lambda slug: tmp_path)
        monkeypatch.setattr(members_mod, "ACTIVITY_FILE_NAME", "activity.jsonl")

        rows, complete = svc_mod._read_legacy_activity_files("m")

        assert len(rows) == 1
        assert complete is True, "a fully read source must read as complete"
        assert (
            rows[0]["legacy_import"] is True
        ), "a row that declared itself first-hand kept that claim through the import"


class TestAnUnreadLegacySourceIsNotMarkedMigrated:
    """The marker proves the migration finished, so it must not outrun it.

    A legacy source that could not be read has not been migrated. Marking it done
    anyway is permanent: the files are never opened again in this or any later
    process, so those rows are gone. An unmarked migration is retried, and the
    per-step idempotence keeps the retry from duplicating what did land.
    """

    def test_an_unreadable_source_leaves_the_marker_off(self, tmp_path, monkeypatch):
        from kiro_crew.eventlog import service as svc_mod
        from kiro_crew.eventlog import types

        monkeypatch.setattr("kiro_crew.crew_log.store.crew_log_tree_root", lambda: tmp_path)
        monkeypatch.setattr(svc_mod, "_read_legacy_activity_files", lambda slug: ([], False))

        svc = svc_mod.MemberEventLogService(tmp_path)
        svc.ensure("alice", "Alice")

        log = svc._get_log("alice")
        assert log is not None
        types_present = [ev.get("type") for ev in log.all_events()]
        assert types.MEMBER_MIGRATED not in types_present, (
            "the migration marker landed although a legacy source was never read, "
            "so those rows can never be imported"
        )

    def test_a_fully_read_source_does_mark_the_migration(self, tmp_path, monkeypatch):
        """The complement, so the test above cannot pass by never marking at all."""
        from kiro_crew.eventlog import service as svc_mod
        from kiro_crew.eventlog import types

        monkeypatch.setattr("kiro_crew.crew_log.store.crew_log_tree_root", lambda: tmp_path)
        monkeypatch.setattr(svc_mod, "_read_legacy_activity_files", lambda slug: ([], True))

        svc = svc_mod.MemberEventLogService(tmp_path)
        svc.ensure("bob", "Bob")

        log = svc._get_log("bob")
        assert log is not None
        types_present = [ev.get("type") for ev in log.all_events()]
        assert types.MEMBER_MIGRATED in types_present
