"""Tests for the per-crew-member space (``$KIROCREW_HOME/members/<slug>/``).

``KIROCREW_HOME`` is pinned to a per-test tmp dir by the autouse
``_isolate_kirocrew_home`` fixture, so every path here resolves under tmp.
"""

from __future__ import annotations

import json

import pytest

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
        assert record_activity("Code Review", "dashboard_chat-1", "persistent", project="/repo", via="chat")
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
        record_activity("Code Review", "dashboard_chat-1", "persistent", project="/repo", via="chat")
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
        monkeypatch.setattr("kiro_crew.members.member_dir", lambda _s: (_ for _ in ()).throw(OSError("boom")))
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
