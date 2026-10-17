"""Tests for the member event log's one-time fold of a member's legacy files.

``MemberEventLogService.ensure`` folds a member's pre-log binding, rules and ``activity.jsonl``
into the log once, records the fold's completion in the fenced log directory, and retires the
agent-writable source. These cover resuming an interrupted migration, the counted row-by-row
dedupe, the completion marker's durability and ordering, the forgery path it closes, what may be
read at the legacy path, the lease that serializes a fold across processes, and the provenance
marker an imported row carries. The rest of the log, its projections and its service are covered
by ``test_eventlog.py``; both import the autouse ``_isolated_home`` from ``eventlog_helpers.py``.
"""

from __future__ import annotations

import json
import os
import threading

import pytest
from eventlog_helpers import _isolated_home  # noqa: F401 - autouse fixture

from kiro_crew.eventlog import types
from kiro_crew.eventlog.log import MemberLog
from kiro_crew.eventlog.service import MemberEventLogService


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


class TestLegacyRetirementMemoRequiresDurableMarker:
    """The process memo may not outrun the marker that makes the fold durable.

    What is pinned is the RETRY, not the marker's absence: the entry a failed sync
    leaves behind is kept, because this runs again for a member already retired and
    removing it there would free the live legacy name with no marker recorded. So the
    evidence is that the slug stays unmemoised, the source keeps its live name, and
    the next call in the same process syncs again and records the marker.
    """

    def test_transient_marker_write_failure_retries_in_same_process(self, tmp_path, monkeypatch):
        import kiro_crew.members as members
        from kiro_crew.eventlog import service as service_mod

        monkeypatch.setattr(members, "data_home", lambda: tmp_path)
        root = tmp_path / "members"
        root.mkdir()
        rows = [{"ts": 1000 + i, "member": "Dave", "session": f"session-{i}"} for i in range(2)]
        legacy = members.member_dir("dave") / members.ACTIVITY_FILE_NAME
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
        )

        marker = service_mod._legacy_folded_marker_path("dave")
        assert marker is not None
        real_fsync_dir = service_mod.fsync_dir
        calls = {"n": 0}

        def fail_first(path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("transient marker sync failure")
            return real_fsync_dir(path)

        monkeypatch.setattr(service_mod, "fsync_dir", fail_first)
        svc = MemberEventLogService(root)

        svc.ensure("dave", "Dave")
        assert calls["n"] == 1
        assert "dave" not in svc._legacy_folded, "the process memo outran marker durability"
        assert legacy.exists(), "the source was freed before its marker was durable"

        svc.ensure("dave", "Dave")
        assert calls["n"] == 2, "the same process did not retry marker durability"
        assert marker.exists(), "the retry did not record the fenced marker"
        assert "dave" in svc._legacy_folded


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
        # flake-ok: pre-existing reset before the control pass, moved verbatim; the next line re-pins KIROCREW_HOME and the control re-reads the log from disk
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing reset before the control pass, moved verbatim; the next line re-pins KIROCREW_HOME to tmp_path
        monkeypatch.undo()
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        svc = svc_mod.get_service()
        svc.ensure("orla", "Orla")
        rows = [e for e in svc.history("orla", limit=None) if e["type"] == ACTIVITY_RECORD]
        assert len(rows) == 1
        svc_mod.set_service(None)


class TestOneBadByteCannotBlockEveryLaterActivityWrite:
    """The legacy file is agent-writable and read from ``ensure``.

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
