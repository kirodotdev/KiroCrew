"""Tests for the per-crew-member space (``$KIROCREW_HOME/members/<slug>/``).

``KIROCREW_HOME`` is pinned to a per-test tmp dir by the autouse
``_isolate_kirocrew_home`` fixture, so every path here resolves under tmp.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import members
from kiro_crew.members import (
    ACTIVITY_FILE_NAME,
    MemberSlugError,
    member_dir,
    members_root,
    read_activity,
    record_activity,
    slug_for_name,
    validate_slug,
)


class TestSlugForName:
    def test_normalizes_spaces_and_case(self):
        assert slug_for_name("Code Review") == "code-review"

    def test_strips_accents_to_ascii(self):
        assert slug_for_name("Café Crew") == "cafe-crew"

    def test_collapses_punctuation_runs_to_single_hyphen(self):
        assert slug_for_name("PR   triage!!! (fast)") == "pr-triage-fast"

    def test_punctuation_only_name_falls_back_to_member(self):
        # Not "artifact": the fallback noun must read as a member, since the
        # shared slugify() belongs to the artifact store.
        assert slug_for_name("!!!") == "member"

    def test_result_always_satisfies_the_slug_pattern(self):
        for name in ("Code Review", "Café Crew", "!!!", "a" * 200, "-leading", "trailing-"):
            validate_slug(slug_for_name(name))

    def test_long_name_is_truncated_without_trailing_hyphen(self):
        slug = slug_for_name("x" * 100)
        assert len(slug) <= 80
        assert not slug.endswith("-")


class TestValidateSlug:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "Has-Upper",
            "has space",
            "has/slash",
            "has.dot",
            "..",
            "-leading",
            "trailing-",
            "a" * 81,
        ],
    )
    def test_rejects_unsafe_or_malformed(self, bad):
        with pytest.raises(MemberSlugError):
            validate_slug(bad)

    @pytest.mark.parametrize("good", ["a", "1", "code-review", "a" * 80])
    def test_accepts_well_formed(self, good):
        assert validate_slug(good) == good

    def test_rejects_non_string(self):
        with pytest.raises(MemberSlugError):
            validate_slug(None)  # type: ignore[arg-type]


class TestMemberDir:
    def test_resolves_under_members_root(self):
        assert member_dir("code-review").parent == members_root().resolve()

    def test_does_not_create_the_directory(self):
        assert not member_dir("code-review").exists()

    @pytest.mark.parametrize("attempt", ["../escape", "..", "a/../../b", "/etc"])
    def test_refuses_traversal_shaped_names(self, attempt):
        # The slug pattern is the primary defence; this asserts the boundary
        # holds rather than trusting the caller to have validated first.
        with pytest.raises(MemberSlugError):
            member_dir(attempt)


class TestRecordActivity:
    def test_writes_a_pointer_entry(self):
        assert record_activity(
            "Code Review", "dashboard_chat-1", "persistent", project="/repo", via="chat"
        )
        rows = read_activity("code-review")
        assert len(rows) == 1
        assert rows[0]["session"] == "dashboard_chat-1"
        assert rows[0]["project"] == "/repo"
        assert rows[0]["via"] == "chat"
        assert rows[0]["ts"].endswith("Z")

    def test_entry_carries_the_exact_member_name(self):
        # Slugification is lossy, so the directory alone cannot identify the
        # member; attribution has to survive two names sharing one slug.
        record_activity("Review_Agent", "s1", "persistent")
        assert read_activity("review-agent")[0]["member"] == "Review_Agent"

    def test_colliding_names_stay_attributable(self):
        record_activity("Review_Agent", "s1", "persistent")
        record_activity("review-agent", "s2", "persistent")
        rows = read_activity("review-agent")
        assert [(r["member"], r["session"]) for r in rows] == [
            ("Review_Agent", "s1"),
            ("review-agent", "s2"),
        ]

    def test_entry_carries_no_content_only_pointers(self):
        record_activity(
            "Code Review", "dashboard_chat-1", "persistent", project="/repo", via="chat"
        )
        assert set(read_activity("code-review")[0]) == {"ts", "member", "session", "project", "via"}

    def test_appends_rather_than_overwrites(self):
        record_activity("M", "s1", "persistent", via="chat")
        record_activity("M", "s2", "persistent", via="chat")
        assert [r["session"] for r in read_activity("m")] == ["s1", "s2"]

    def test_creates_the_member_directory_on_demand(self):
        record_activity("Brand New", "s1", "persistent")
        assert (member_dir("brand-new") / ACTIVITY_FILE_NAME).is_file()

    def test_omits_empty_optional_fields(self):
        record_activity("M", "s1", "persistent")
        assert set(read_activity("m")[0]) == {"ts", "member", "session"}

    @pytest.mark.parametrize("mode", ["incognito", "temporary", "INCOGNITO", " temporary "])
    def test_no_trace_modes_write_nothing(self, mode):
        assert record_activity("M", "s1", mode) is False
        assert read_activity("m") == []

    @pytest.mark.parametrize("mode", ["", "   ", "unknown", "Persistent-ish", "PERSISTENT_v2"])
    def test_unrecognized_mode_fails_closed(self, mode):
        # Allowlist, not denylist: a brand-new session whose metadata has not
        # flushed yet reports an empty mode, and that must not be treated as
        # traceable just because it is not spelled "incognito".
        assert record_activity("M", "s1", mode) is False
        assert read_activity("m") == []

    def test_persistent_mode_still_writes(self):
        assert record_activity("M", "s1", "persistent") is True

    @pytest.mark.parametrize("mode", ["PERSISTENT", " persistent "])
    def test_persistent_match_is_case_and_space_insensitive(self, mode):
        assert record_activity("M", "s1", mode) is True

    def test_dedupe_suppresses_a_repeat_session_pointer(self):
        # The chat site's `is_new` tracks the PROVIDER session, so a dead
        # provider cold-starting the same conversation would append twice and
        # inflate the counts this log feeds.
        assert record_activity("M", "s1", "persistent", via="chat", dedupe_session=True) is True
        assert record_activity("M", "s1", "persistent", via="chat", dedupe_session=True) is False
        assert len(read_activity("m")) == 1

    def test_dedupe_still_allows_a_different_session(self):
        record_activity("M", "s1", "persistent", dedupe_session=True)
        assert record_activity("M", "s2", "persistent", dedupe_session=True) is True
        assert [r["session"] for r in read_activity("m")] == ["s1", "s2"]

    def test_dedupe_is_per_member_not_per_file(self):
        # Colliding slugs share one file; dedupe must key on member AND session
        # or one member's entry would suppress the other's.
        record_activity("Review_Agent", "s1", "persistent", dedupe_session=True)
        assert record_activity("review-agent", "s1", "persistent", dedupe_session=True) is True
        assert len(read_activity("review-agent")) == 2

    def test_routing_decisions_are_not_deduped(self):
        # Each select_crew bind is a distinct event even within one session.
        record_activity("M", "s1", "persistent", via="select_crew")
        record_activity("M", "s1", "persistent", via="select_crew")
        assert len(read_activity("m")) == 2

    def test_routing_decision_uses_a_distinct_session_field(self):
        # A decision is recorded in the session that MADE it (the parent); the
        # member runs elsewhere. Filing it under `session` would let a consumer
        # count a session the member never ran in.
        record_activity("M", "parent-1", "persistent", via="select_crew")
        row = read_activity("m")[0]
        assert row["decided_in"] == "parent-1"
        assert "session" not in row

    def test_participation_and_decisions_are_countable_apart(self):
        record_activity("M", "chat-1", "persistent", via="chat")
        record_activity("M", "parent-1", "persistent", via="select_crew")
        rows = read_activity("m")
        assert [r["session"] for r in rows if "session" in r] == ["chat-1"]
        assert [r["decided_in"] for r in rows if "decided_in" in r] == ["parent-1"]

    def test_memory_mode_cannot_be_omitted(self):
        # Required positionally so a caller cannot silently log a private
        # session by forgetting an opt-in keyword.
        with pytest.raises(TypeError):
            record_activity("M", "s1")  # type: ignore[call-arg]

    @pytest.mark.parametrize("member,session", [("", "s1"), ("M", ""), ("", "")])
    def test_requires_both_member_and_session(self, member, session):
        assert record_activity(member, session, "persistent") is False

    def test_does_not_fsync(self, monkeypatch):
        # A durability barrier is a blocking kernel syscall; this log is
        # advisory and one call site shares the gateway event loop.
        calls = []
        monkeypatch.setattr("os.fsync", lambda fd: calls.append(fd))
        record_activity("M", "s1", "persistent")
        assert calls == []

    def test_reports_failure_instead_of_raising(self, monkeypatch):
        # Total by contract: the call sites have no guard, and one of them
        # (mcp_core) has no logger, so a raise here would surface as a tool error.
        monkeypatch.setattr(
            "kiro_crew.members.member_dir", lambda _s: (_ for _ in ()).throw(OSError("boom"))
        )
        assert record_activity("M", "s1", "persistent") is False


class TestReadActivity:
    def test_missing_member_reads_empty(self):
        assert read_activity("never-existed") == []

    def test_invalid_slug_reads_empty_rather_than_raising(self):
        assert read_activity("../escape") == []

    def test_torn_fragment_does_not_swallow_the_next_record(self):
        # A write interrupted before its newline leaves a fragment on the last
        # line. The next record must not be glued onto it, or BOTH are lost.
        record_activity("M", "s1", "persistent")
        path = member_dir("m") / ACTIVITY_FILE_NAME
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"ts":"x","session":"torn"')  # no newline: torn write
        record_activity("M", "s2", "persistent")
        assert [r["session"] for r in read_activity("m")] == ["s1", "s2"]

    def test_skips_torn_lines_and_keeps_the_rest(self):
        record_activity("M", "s1", "persistent")
        path = member_dir("m") / ACTIVITY_FILE_NAME
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n{not valid json\n")
        record_activity("M", "s2", "persistent")
        assert [r["session"] for r in read_activity("m")] == ["s1", "s2"]

    def test_skips_non_object_rows(self):
        record_activity("M", "s1", "persistent")
        path = member_dir("m") / ACTIVITY_FILE_NAME
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n" + json.dumps(["not", "a", "dict"]))
        assert len(read_activity("m")) == 1

    def test_limit_returns_the_most_recent(self):
        for i in range(5):
            record_activity("M", f"s{i}", "persistent")
        assert [r["session"] for r in read_activity("m", limit=2)] == ["s3", "s4"]


class TestRecordActivityRotation:
    """The activity log rotates at the size cap — bounded disk, no lost record.

    Mirrors the ``slow_commands.jsonl`` rotation tests in
    ``test_subagent_persistence.py``: the shared ``rotate_jsonl_at`` helper
    runs before the append, lock-guarded because this log is append-only from
    multiple processes.
    """

    CAP = 400  # bytes — small enough to cross with a handful of records

    @pytest.fixture(autouse=True)
    def small_cap(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.members._ACTIVITY_LOG_MAX_BYTES", self.CAP)

    def test_rotation_keeps_every_record(self):
        """One rotation: older records land in ``.jsonl.1`` and stay visible
        through :func:`read_activity`, which spans both generations."""
        live = member_dir("m") / ACTIVITY_FILE_NAME
        rotated = live.with_name(live.name + ".1")
        n = 0
        while not rotated.exists():
            assert record_activity("M", f"s{n}", "persistent")
            n += 1
            assert n < 100, "cap never triggered a rotation"
        # The rotated generation holds the pre-rotation records intact...
        old = [
            json.loads(row)
            for row in rotated.read_text(encoding="utf-8").splitlines()
            if row.strip()
        ]
        assert [r["session"] for r in old] == [f"s{i}" for i in range(n - 1)]
        # ...the live file holds exactly the one record written after...
        live_rows = [
            json.loads(row) for row in live.read_text(encoding="utf-8").splitlines() if row.strip()
        ]
        assert [r["session"] for r in live_rows] == [f"s{n - 1}"]
        assert live.stat().st_size < self.CAP
        # ...and the PUBLIC reader still returns the full history, oldest
        # first: rotation must not hide records from consumers.
        assert [r["session"] for r in read_activity("m")] == [f"s{i}" for i in range(n)]

    def test_read_activity_limit_spans_generations(self):
        """``limit`` counts across both generations, newest last."""
        live = member_dir("m") / ACTIVITY_FILE_NAME
        rotated = live.with_name(live.name + ".1")
        n = 0
        while not rotated.exists():
            assert record_activity("M", f"s{n}", "persistent")
            n += 1
            assert n < 100, "cap never triggered a rotation"
        got = read_activity("m", limit=n)
        assert [r["session"] for r in got] == [f"s{i}" for i in range(n)]

    def test_dedupe_survives_rotation(self):
        """A member/session pair whose entry was rotated aside must STILL be
        suppressed: the dedupe probe reads both generations, so rotation
        cannot re-inflate the counts it protects."""
        live = member_dir("m") / ACTIVITY_FILE_NAME
        rotated = live.with_name(live.name + ".1")
        assert record_activity("M", "dedup-me", "persistent", via="chat", dedupe_session=True)
        n = 0
        while not rotated.exists():
            assert record_activity("M", f"filler-{n}", "persistent")
            n += 1
            assert n < 100, "cap never triggered a rotation"
        # The original entry now lives only in the rotated generation.
        assert "dedup-me" in rotated.read_text(encoding="utf-8")
        assert "dedup-me" not in live.read_text(encoding="utf-8")
        assert (
            record_activity("M", "dedup-me", "persistent", via="chat", dedupe_session=True) is False
        )

    def test_total_bytes_stay_bounded(self):
        """The property the issue is about: many appends, bounded total disk.

        One rotation alone does not prove boundedness — total bytes across
        BOTH generations must stay bounded no matter how many records land.
        """
        for i in range(300):
            record_activity("M", f"session-{i:04d}", "persistent")
        live = member_dir("m") / ACTIVITY_FILE_NAME
        rotated = live.with_name(live.name + ".1")
        # Each generation may overshoot the cap by at most one record (the
        # size check runs before the append), so bound each at CAP plus a
        # generous one-record slack.
        slack = 200
        assert live.stat().st_size <= self.CAP + slack
        assert rotated.exists()
        assert rotated.stat().st_size <= self.CAP + slack
        assert live.stat().st_size + rotated.stat().st_size <= 2 * (self.CAP + slack)

    def test_rotation_failure_still_appends(self):
        """Best-effort contract: a failing rotation never drops the record and
        never breaks the ``record_activity`` return contract. A directory
        squatting on the rotation target makes ``os.replace`` raise a REAL
        ``OSError`` on both POSIX and Windows — no stdlib patching."""
        record_activity("M", "seed", "persistent")
        live = member_dir("m") / ACTIVITY_FILE_NAME
        with open(live, "a", encoding="utf-8") as fh:
            fh.write("x" * (self.CAP + 10) + "\n")
        live.with_name(live.name + ".1").mkdir()

        assert record_activity("M", "after-fail", "persistent") is True
        assert live.with_name(live.name + ".1").is_dir()
        assert "after-fail" in live.read_text(encoding="utf-8")


class TestOverCapRecordFailsClosed:
    """#6345: the activity log is agent-writable and its read decides an append.

    ``for line in fh`` would materialise one crafted newline-free line whole.
    The reader now aborts on an over-cap record, and because the
    ``dedupe_session`` probe cannot prove absence from a log it could not
    finish reading, it declines to append rather than risk a duplicate.
    """

    @pytest.fixture(autouse=True)
    def _small_cap(self, monkeypatch):
        # raising=False so this file also RUNS against a pre-fix source, where
        # the attribute does not exist: the tests then fail on behaviour (the
        # append that should have been refused) rather than erroring on a
        # missing name. Same idiom as test_session_digest's `create=True`.
        monkeypatch.setattr(members, "_RECORD_CAP", 200, raising=False)

    @staticmethod
    def _log_for(slug: str):
        path = member_dir(slug) / ACTIVITY_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_over_cap_record_refuses_to_append(self):
        """Red on base: base skips the unparseable line, finds no match, and appends."""
        log = self._log_for("m")
        log.write_bytes(b"y" * 400 + b"\n")
        before = log.read_bytes()
        assert (
            record_activity("m", "s1", "persistent", dedupe_session=True) is False
        ), "must not append when the log could not be read in full"
        assert log.read_bytes() == before, "log was modified despite the refusal"

    def test_a_normal_log_still_appends(self):
        """The refusal is specific to an over-cap record, not to every read."""
        assert record_activity("m", "s1", "persistent", dedupe_session=True) is True
        assert [r["session"] for r in read_activity("m")] == ["s1"]

    def test_dedupe_still_suppresses_a_repeat_within_cap(self):
        assert record_activity("m", "s1", "persistent", dedupe_session=True) is True
        assert record_activity("m", "s1", "persistent", dedupe_session=True) is False
        assert len(read_activity("m")) == 1

    def test_read_activity_still_degrades_for_display(self):
        """The public reader keeps its documented non-raising contract.

        An over-cap record stops that generation, but the rows already read are
        returned and no exception escapes — only the write path fails closed.
        """
        log = self._log_for("m")
        log.write_bytes(
            json.dumps({"member": "m", "session": "s1"}).encode() + b"\n" + b"y" * 400 + b"\n"
        )
        rows = read_activity("m")
        assert [r["session"] for r in rows] == ["s1"]

    def test_a_cr_delimited_log_does_not_produce_a_duplicate(self):
        """The log is read binary now, so its boundaries must stay universal.

        These logs were read in TEXT mode, where a bare carriage return ended a
        record. The reader splits on it too, so this file parses as two records
        and the dedupe probe still sees the prior entry. Splitting only on LF
        would glue them into one unparseable line, the probe would see no prior
        entry, and a duplicate would be appended -- which is what an earlier
        revision of this PR did.
        """
        log = self._log_for("m")
        first = json.dumps({"member": "m", "session": "s1"})
        log.write_bytes(first.encode() + b"\r" + first.encode() + b"\n")
        before = log.read_bytes()
        assert record_activity("m", "s1", "persistent", dedupe_session=True) is False
        assert log.read_bytes() == before, "a CR-delimited log must not gain a duplicate entry"
        assert len(read_activity("m")) == 2, "both CR-delimited records must be read"

    def test_record_exactly_at_cap_is_not_refused(self):
        """The cap is inclusive, so a legitimate record at the limit still reads."""
        log = self._log_for("m")
        entry = {"member": "m", "session": "s1", "pad": ""}
        pad = 200 - len(json.dumps(entry).encode())
        entry["pad"] = "z" * pad
        raw = json.dumps(entry).encode()
        assert len(raw) == 200
        log.write_bytes(raw + b"\n")
        assert record_activity("m", "s1", "persistent", dedupe_session=True) is False, (
            "the at-cap record must be READ (and so suppress the duplicate), "
            "not refused as over-cap"
        )
