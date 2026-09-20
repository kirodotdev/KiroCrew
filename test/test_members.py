"""Tests for the per-crew-member space (``$KIROCREW_HOME/members/<slug>/``).

``KIROCREW_HOME`` is pinned to a per-test tmp dir by the autouse
``_isolate_kirocrew_home`` fixture, so every path here resolves under tmp.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.crew_log.schema import KIND_MEMBER
from kiro_crew.crew_log.store import crew_log_path
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


def _log_path(slug: str):
    """Where a member's append-only log lives: inside the fenced ``crew-log`` tree.

    Asked of the store rather than composed here. The directory under it is a
    readable-plus-digest fold of the slug, so a hand-built path would be wrong,
    and asking means these tests follow the layout instead of pinning a copy of it.
    """
    return crew_log_path(KIND_MEMBER, slug)


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

    def test_non_ascii_name_still_falls_back_to_member(self):
        # slugify() hash-falls-back for all-non-ASCII names; the member
        # contract keeps the constant so data stored under "member" stays
        # addressable.
        assert slug_for_name("\u4f1a\u8bae\u7eaa\u8981") == "member"

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

    def test_creates_the_member_log_on_demand(self):
        # The member's space is the per-member append-only log, kept as a
        # ``member``-kind crew log: record_activity ensures it exists with a
        # header line followed by one activity/record envelope.
        record_activity("Brand New", "s1", "persistent")
        log = _log_path("brand-new")
        assert log.is_file()
        lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
        entries = [json.loads(ln) for ln in lines]
        # A header first, then the activity record somewhere after it. Found by
        # SEARCHING rather than by indexing line 1: the position only holds while
        # nothing else is in the log, and where the record sits is not what "creates
        # the log on demand" claims.
        assert entries[0]["type"] == "member"
        records = [e for e in entries[1:] if e["type"] == "activity/record"]
        assert len(records) == 1
        assert records[0]["data"]["session"] == "s1"

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

    def test_appends_are_fsynced(self, monkeypatch):
        # Durability is now the contract: the append-only log fsyncs every
        # committed record, so a crash cannot lose an activity the caller was
        # told landed. The old best-effort activity.jsonl (no fsync) is gone.
        calls = []
        monkeypatch.setattr("os.fsync", lambda fd: calls.append(fd))
        record_activity("M", "s1", "persistent")
        assert calls, "record_activity must fsync at least once per record"

    def test_reports_failure_instead_of_raising(self, monkeypatch):
        # Total by contract: the call sites have no guard, and one of them
        # (mcp_core) has no logger, so a raise here would surface as a tool error.
        # Patched at the service lookup because that is the first thing on the
        # write path now that the log's location comes from the store, not from
        # member_dir -- a patch on the old path would pass without proving
        # anything.
        monkeypatch.setattr(
            "kiro_crew.eventlog.service.get_service",
            lambda: (_ for _ in ()).throw(OSError("boom")),
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
        # The member directory holds only the LEGACY file now -- the log moved
        # under the fenced crew-log tree -- so nothing has created it yet.
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"ts":"x","session":"torn"')  # no newline: torn write
        record_activity("M", "s2", "persistent")
        assert [r["session"] for r in read_activity("m")] == ["s1", "s2"]

    def test_skips_torn_lines_and_keeps_the_rest(self):
        record_activity("M", "s1", "persistent")
        path = member_dir("m") / ACTIVITY_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n{not valid json\n")
        record_activity("M", "s2", "persistent")
        assert [r["session"] for r in read_activity("m")] == ["s1", "s2"]

    def test_skips_non_object_rows(self):
        record_activity("M", "s1", "persistent")
        path = member_dir("m") / ACTIVITY_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n" + json.dumps(["not", "a", "dict"]))
        assert len(read_activity("m")) == 1

    def test_limit_returns_the_most_recent(self):
        for i in range(5):
            record_activity("M", f"s{i}", "persistent")
        assert [r["session"] for r in read_activity("m", limit=2)] == ["s3", "s4"]


class TestNoRotationAppendOnly:
    """The per-member log is append-only: never rotated, never truncated for
    retention, so no record is ever dropped.

    Replaces the former ``activity.jsonl`` byte-cap rotation suite: durability
    now comes from one fsynced append per record into a single ``log.jsonl``
    (header line + one envelope per event), and read_activity pages that one
    log rather than spanning generations.
    """

    def test_many_records_stay_in_one_log(self):
        n = 40
        for i in range(n):
            assert record_activity("M", f"s{i}", "persistent")
        log = _log_path("m")
        lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
        # Every record is still here; nothing rotated aside. Counting the RECORDS
        # rather than the lines is what says that: a line count also passes when a
        # record is lost and some other entry takes its place, and it fails when the
        # log legitimately holds an entry this test never wrote.
        records = [ln for ln in lines if json.loads(ln)["type"] == "activity/record"]
        assert len(records) == n
        # Retention in this store is dropping whole segments off the front, and
        # nothing rotates yet, so a second segment would mean the append-only
        # claim had been broken.
        assert [p.name for p in log.parent.glob("log.*.jsonl")] == []

    def test_read_limit_returns_most_recent_oldest_first(self):
        n = 10
        for i in range(n):
            assert record_activity("M", f"s{i}", "persistent")
        got = read_activity("m", limit=3)
        # The most recent k, oldest-first within that window.
        assert [r["session"] for r in got] == ["s7", "s8", "s9"]

    def test_no_records_are_dropped(self):
        n = 25
        for i in range(n):
            assert record_activity("M", f"s{i}", "persistent")
        assert [r["session"] for r in read_activity("m")] == [f"s{i}" for i in range(n)]

    def test_dedupe_still_works_after_many_records(self):
        assert record_activity("M", "dedup-me", "persistent", via="chat", dedupe_session=True)
        for i in range(30):
            assert record_activity("M", f"filler-{i}", "persistent")
        # The original pair is still suppressed — the dedupe probe reads the
        # one append-only log, so a burst of later records cannot re-inflate it.
        assert (
            record_activity("M", "dedup-me", "persistent", via="chat", dedupe_session=True) is False
        )
        assert len([r for r in read_activity("m") if r.get("session") == "dedup-me"]) == 1


class TestLogCorruptionContract:
    """The append-only log's damage contract, now that the log is a crew log.

    A torn TRAILING line (partial write, no newline) is repaired by truncation on
    load, and the next record_activity appends with a contiguous seq. A damaged
    line INSIDE the committed region costs a reader that line and nothing else.
    An unreadable HEADER is the fatal case -- without it nothing in the file is
    attributable -- and it raises LogCorrupt in the log layer, which
    record_activity reports as False and read_activity as [] rather than raising.

    The service caches a loaded MemberLog per slug, so a file mutated behind
    its back is only observed after the singleton is dropped — these tests
    ``set_service(None)`` to force the next call to reload from disk, which is
    exactly the cold-start / other-process path the damage contract exists
    for (a live process never tears its own committed region).
    """

    @staticmethod
    def _reset_service():
        from kiro_crew.eventlog.service import set_service

        set_service(None)

    def test_torn_trailing_line_is_repaired_and_next_append_is_contiguous(self):
        assert record_activity("M", "s1", "persistent")
        log = _log_path("m")
        # A write interrupted before its newline leaves a torn trailing line.
        with open(log, "a", encoding="utf-8") as fh:
            fh.write('{"type":"activity/record","seq":2,"time":123,"dat')
        self._reset_service()
        # The next record reloads, repairs (truncates) the torn tail, and
        # appends with a contiguous seq — no gap, no lost record.
        assert record_activity("M", "s2", "persistent")
        rows = read_activity("m")
        assert [r["session"] for r in rows] == ["s1", "s2"]
        lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
        # The header carries no seq, so contiguity is a claim about the entries after
        # it: they run 1, 2, 3, ... with no gap, which means the torn tail was
        # truncated and the next append took the very next number. Stated as "no gap"
        # rather than as a line count and a literal pair, because the count moves
        # when the log holds any other entry while contiguity does not -- and a gap
        # is the defect this test exists to catch.
        seqs = [json.loads(ln)["seq"] for ln in lines[1:]]
        assert seqs == list(range(1, len(seqs) + 1)), "no gap, no lost record"
        assert len(seqs) >= 2  # at least the two committed records

    def test_a_damaged_committed_line_costs_that_line_and_nothing_else(self):
        """A damaged line inside the committed region is skipped, not fatal.

        This changed with the move into the crew log store and the new answer is
        the better one for a record whose purpose is to survive damage: refusing
        the file turns one bad line into a member whose entire history is
        unreadable, and the bad line is unrecoverable either way. So the reads
        keep working and the next append still lands.
        """
        assert record_activity("M", "s1", "persistent")
        log = _log_path("m")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("not valid json at all\n")  # committed damage
        self._reset_service()

        assert record_activity("M", "s2", "persistent")
        assert [r["session"] for r in read_activity("m")] == ["s1", "s2"]

    def test_an_unreadable_header_degrades_both_public_funcs(self):
        """Without a header nothing in the file is attributable, so it IS fatal.

        That is the difference from one damaged line, and it is the case the
        ``LogCorrupt`` contract now covers: record_activity is documented
        best-effort so it reports False, and read_activity degrades to [] rather
        than raising through to the drawer and the counters.
        """
        assert record_activity("M", "s1", "persistent")
        log = _log_path("m")
        lines = log.read_text(encoding="utf-8").splitlines(keepends=True)
        log.write_text("garbage header\n" + "".join(lines[1:]), encoding="utf-8")
        self._reset_service()

        assert record_activity("M", "s2", "persistent") is False
        assert read_activity("m") == []

    def test_corruption_contract_matches_the_service(self):
        # Guard the claim against drift: the log layer really raises LogCorrupt
        # for an unreadable header, and both public members funcs convert that
        # into their non-raising contracts once the stale in-memory log is gone.
        from kiro_crew.eventlog.log import LogCorrupt, MemberLog

        assert record_activity("M", "s1", "persistent")
        log = _log_path("m")
        lines = log.read_text(encoding="utf-8").splitlines(keepends=True)
        log.write_text("garbage header\n" + "".join(lines[1:]), encoding="utf-8")
        with pytest.raises(LogCorrupt):
            MemberLog("m").load()
        self._reset_service()
        assert record_activity("M", "s2", "persistent") is False
        assert read_activity("m") == []
