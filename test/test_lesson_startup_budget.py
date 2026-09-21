"""Startup lesson admission: bounded, tiered, and loud about what it withheld.

The behaviour under test replaces one that deliberately kept EVERY eligible rule
until a window-scaled ceiling. These cases pin the four properties that made the
replacement safe rather than just smaller:

- a growing pile of past findings does not grow what a bare greeting costs;
- an AUTHORED standing rule is never displaced by recency or relevance, and in
  particular not by an unstated legacy row;
- an overflow is reported with exact counts and a way to read the rest, never
  silently dropped;
- a caller that names no budget is byte-identical to the previous behaviour.

Both lesson tiers are covered: the SQLite/vector store and the JSONL fallback,
which have separate renderers and separate ordering signals.
"""

from __future__ import annotations

import json
import zlib
from unittest.mock import Mock

import pytest

from kiro_crew.dashboard.handlers.cron import _candidate_applies
from kiro_crew.learn import _MAX_LESSONS_TOTAL, Lesson, LessonStore, _prune_to_total
from kiro_crew.lesson_validation import (
    LESSON_APPLIES_ALWAYS,
    LESSON_APPLIES_ON_TOPIC,
    LESSON_APPLIES_UNSTATED,
    LESSON_REFUSED_AT_CAPACITY,
    authored_lesson_applies,
    normalize_lesson_applies,
    render_lesson_tier,
    tighter_lesson_budget,
)
from kiro_crew.vector_memory import VectorMemoryStore, _lesson_applies

DIRECTIVE_BUDGET = 7_000
EXPERIENCE_BUDGET = 1_500


def _lexical_embedding(text: str) -> list[float]:
    """A deterministic stand-in for the embedding model, keyed on shared words.

    Two texts that share significant words score close together, which is all the
    contradiction window's similarity band needs. Fixed 32 dimensions so a store
    never sees a dimension change, and no network or model call.

    ``crc32``, not ``hash()``: string hashing is salted per interpreter process,
    so a bucket assignment from ``hash()`` differs between runs and between xdist
    workers -- the same input would score differently depending on which worker
    picked it up.
    """
    vector = [0.0] * 32
    for token in text.lower().split():
        if len(token) >= 3:
            vector[zlib.crc32(token.encode()) % 32] += 1.0
    return vector


def _jsonl(tmp_path, rows: list[Lesson]) -> LessonStore:
    store = LessonStore(base_dir=tmp_path)
    for row in rows:
        store.save(row)
    return store


def _rule(marker: str, size: int = 200) -> str:
    """A rule whose text is LEXICALLY DISTINCT from every other marker's.

    Padding every fixture rule with the same filler word makes them collide in
    ``write_lesson``'s topic-overlap dedup (shared keywords >= 50% of the larger
    set), so each write replaces the previous one and the store ends up holding
    exactly one lesson. The marker is repeated into the filler so each rule's
    keyword set is its own.
    """
    filler = f"{marker.lower()}pad "
    # Stripped, because ``write_lesson`` stores the stripped rule: an assertion
    # against an unstripped fixture string never matches what was rendered.
    return (f"{marker} " + (filler * (size // len(filler) + 1))[:size]).strip()


class TestAuthoredVocabulary:
    def test_absent_reads_as_unstated_rather_than_guessing(self):
        assert _lesson_applies({"rule": "r"}) == LESSON_APPLIES_UNSTATED
        assert _lesson_applies("legacy string row") == LESSON_APPLIES_UNSTATED

    @pytest.mark.parametrize("stored", [123, None, [], {}, "standing", "DIRECTIVE"])
    def test_an_unrecognized_stored_value_reads_as_unstated(self, stored):
        # The READ path must never raise: a hand-edited or future-schema row is
        # assembled into a prompt beside every other lesson.
        assert _lesson_applies({"rule": "r", "applies": stored}) == LESSON_APPLIES_UNSTATED

    @pytest.mark.parametrize(
        "given,expected",
        [
            (None, None),
            ("", None),
            ("always", LESSON_APPLIES_ALWAYS),
            ("  Always  ", LESSON_APPLIES_ALWAYS),
            ("on_topic", LESSON_APPLIES_ON_TOPIC),
        ],
    )
    def test_the_write_path_normalizes_what_it_accepts(self, given, expected):
        assert normalize_lesson_applies(given) == expected

    @pytest.mark.parametrize("bad", ["directive", "sometimes", 1, ["always"]])
    def test_the_write_path_refuses_a_misspelling_instead_of_clamping(self, bad):
        # There is no safe clamp: "always" would inject a note into every session
        # and "on_topic" would demote a standing rule. Both are silent, so this
        # raises and the caller's bug reaches the caller.
        with pytest.raises(ValueError, match="lesson tier must be one of"):
            normalize_lesson_applies(bad)


class TestBudgetArithmetic:
    def test_zero_means_unbounded_and_never_wins_a_minimum(self):
        assert tighter_lesson_budget(0, 0) == 0
        assert tighter_lesson_budget(0, 500) == 500
        assert tighter_lesson_budget(500, 0) == 500
        assert tighter_lesson_budget(500, 200) == 200

    def test_an_empty_tier_renders_no_labelled_block(self):
        block, omitted = render_lesson_tier(
            [], 100, header="[H]", footer="[F]", omission="[{count}/{total}/{limit}]"
        )
        assert block == ""
        assert omitted == 0

    def test_a_budget_too_small_for_one_lesson_still_reports_the_omission(self):
        # The one outcome this must always be able to produce. A block that
        # rendered empty, or nothing at all, is indistinguishable from "this user
        # has no rules" -- the failure the budget itself introduces.
        block, omitted = render_lesson_tier(
            [(object(), "x" * 500)], 20, header="[H]", footer="[F]", omission="[{count}/{total}]"
        )
        assert omitted == 1
        assert "[1/1]" in block
        assert "[H]" in block and "[F]" in block


class TestJsonlStartupAdmission:
    def test_growing_findings_do_not_grow_a_greeting(self, tmp_path):
        """Acceptance A: startup cost is bounded in the volume of past findings."""
        small = _jsonl(
            tmp_path / "small",
            [Lesson(ts="1", rule=_rule("KEEP"), category="preference", applies="always")]
            + [
                Lesson(ts=str(i + 2), rule=_rule(f"F{i}"), category="knowledge", applies="on_topic")
                for i in range(5)
            ],
        )
        large = _jsonl(
            tmp_path / "large",
            [Lesson(ts="1", rule=_rule("KEEP"), category="preference", applies="always")]
            + [
                Lesson(ts=str(i + 2), rule=_rule(f"F{i}"), category="knowledge", applies="on_topic")
                for i in range(150)
            ],
        )
        kwargs = dict(
            cap=99_000, directive_budget=DIRECTIVE_BUDGET, experience_budget=EXPERIENCE_BUDGET
        )
        small_ctx = small.get_context(**kwargs)
        large_ctx = large.get_context(**kwargs)

        # 30x the findings must not buy a materially larger prompt.
        assert len(large_ctx) < len(small_ctx) + EXPERIENCE_BUDGET
        # And the standing rule survives in both.
        assert _rule("KEEP") in small_ctx
        assert _rule("KEEP") in large_ctx

    def test_an_authored_rule_outranks_newer_unstated_rows(self, tmp_path):
        """Acceptance B: authored rules are not displaced by recency.

        Measured regression: six authored rules all fell out of the budget behind
        forty NEWER unstated rows, because the tier was ranked on recency alone.
        """
        rows = [
            Lesson(
                ts=f"2026-01-{i + 1:02d}",
                rule=_rule(f"AUTHORED{i}"),
                category="preference",
                applies="always",
            )
            for i in range(6)
        ] + [
            Lesson(ts=f"2026-03-{i + 1:02d}", rule=_rule(f"LEGACY{i}"), category="knowledge")
            for i in range(60)
        ]
        store = _jsonl(tmp_path, rows)
        out = store.get_context(
            cap=99_000, directive_budget=DIRECTIVE_BUDGET, experience_budget=EXPERIENCE_BUDGET
        )

        for i in range(6):
            assert _rule(f"AUTHORED{i}") in out, "an authored rule was displaced"
        first_authored = min(out.index(_rule(f"AUTHORED{i}")) for i in range(6))
        legacy_positions = [
            out.index(_rule(f"LEGACY{i}")) for i in range(60) if _rule(f"LEGACY{i}") in out
        ]
        assert legacy_positions, "this fixture must admit some legacy rows too"
        assert first_authored < min(legacy_positions)

    def test_an_unstated_row_is_served_as_a_rule_not_as_a_finding(self, tmp_path):
        """Acceptance D: legacy rows keep reaching the model, in the rule tier."""
        store = _jsonl(tmp_path, [Lesson(ts="1", rule=_rule("LEGACY"), category="knowledge")])
        out = store.get_context(
            cap=99_000, directive_budget=DIRECTIVE_BUDGET, experience_budget=EXPERIENCE_BUDGET
        )
        assert "[Learned corrections" in out
        assert "[Learned experience" not in out
        assert _rule("LEGACY") in out

    def test_overflow_names_its_counts_and_a_way_to_read_the_rest(self, tmp_path):
        """Acceptance C: the capacity limit is explicit, never a silent trim."""
        store = _jsonl(
            tmp_path,
            [
                Lesson(ts=str(i), rule=_rule(f"R{i}", 400), category="preference", applies="always")
                for i in range(60)
            ],
        )
        out = store.get_context(cap=99_000, directive_budget=DIRECTIVE_BUDGET)
        assert "retained rules above the" in out
        assert "-character rule budget" in out
        assert "read them with learn_list" in out
        assert "not a judgement" in out

    def test_a_caller_naming_no_budget_is_unchanged(self, tmp_path):
        """Every caller predating the budgets keeps its exact bytes."""
        rows = [Lesson(ts=str(i), rule=_rule(f"R{i}"), category="preference") for i in range(4)]
        store = _jsonl(tmp_path, rows)
        assert store.get_context() == store.get_context(directive_budget=0, experience_budget=0)
        # The shipped block shape: one newline after the footer, no notice.
        out = store.get_context()
        assert out.endswith("[End of learned corrections]\n")
        assert "[Context budget" not in out


class TestFindingsAreOrderedByRelevance:
    """A budget is only safe if the finding the task needs still arrives.

    The JSONL tier has no relevance score, so newest-first alone drops an OLD
    finding the request is actually about while newer irrelevant ones fit. Measured
    on a 199-lesson store before this ordering existed: the relevant finding was
    injected when everything was injected, and fell outside the findings budget once
    the budget bound.
    """

    def test_an_old_relevant_finding_survives_a_crowd_of_newer_ones(self, tmp_path):
        rows = [
            Lesson(ts="2026-01-01", rule=_rule("RULE"), category="preference", applies="always")
        ]
        rows.append(
            Lesson(
                ts="2026-01-02",
                category="knowledge",
                applies="on_topic",
                rule="when the quokka deployment times out raise its readiness probe delay",
            )
        )
        for i in range(150):
            rows.append(
                Lesson(
                    ts=f"2026-06-{i % 28 + 1:02d}",
                    rule=_rule(f"FILL{i}"),
                    category="knowledge",
                    applies="on_topic",
                )
            )
        store = _jsonl(tmp_path, rows)

        out = store.get_context(
            cap=99_000,
            directive_budget=DIRECTIVE_BUDGET,
            experience_budget=EXPERIENCE_BUDGET,
            query_text="the quokka deployment keeps timing out",
        )

        assert "quokka deployment times out" in out, "the relevant finding was dropped"
        assert _rule("RULE") in out, "the standing rule must still arrive"

    def test_a_rule_is_never_withheld_or_outranked_by_an_untagged_row(self, tmp_path):
        """Rules ARE relevance-ordered now, matching the vector store. Two invariants
        survive that and are what this pins: a rule is never withheld for being
        unrelated to the message, and an untagged row never outranks an authored one
        however well it matches. The previous claim -- that the rule tier is untouched
        by the request -- was never true of the vector path, which ranks its whole
        eligible set, so stating it here left the two stores describing opposite
        policies.
        """
        rows = [
            Lesson(
                ts="2026-01-01",
                rule="never force push to a protected branch",
                category="preference",
                applies="always",
            ),
            Lesson(
                ts="2026-01-02",
                rule="untagged note naming the quokka formatter",
                category="knowledge",
            ),
        ]
        store = _jsonl(tmp_path, rows)
        out = store.get_context(
            directive_budget=DIRECTIVE_BUDGET, query_text="quokka quokka quokka"
        )
        # The authored rule arrives even though the request names the other row, and
        # it arrives FIRST: precedence is per tier, so relevance decides only inside
        # each sub-list.
        assert "never force push to a protected branch" in out
        assert out.index("never force push to a protected branch") < out.index(
            "untagged note naming the quokka formatter"
        )

    def test_a_supplied_request_that_matches_nothing_withholds_the_findings(self, tmp_path):
        """A zero-overlap REQUEST and no request at all are two different events.

        Treating them alike lets a supplied-but-unrelated request spend the whole
        findings allowance on the newest rows. With no query supplied there is no
        signal to judge by, so that case renders every finding and stays
        byte-identical to a caller that names no budget.
        """
        rows = [
            Lesson(ts=str(i), rule=_rule(f"F{i}"), category="knowledge", applies="on_topic")
            for i in range(4)
        ]
        store = _jsonl(tmp_path, rows)
        unrelated = store.get_context(query_text="nothing here matches")
        no_query = store.get_context()
        assert "Withheld all 4 past findings" in unrelated
        assert _rule("F0") not in unrelated
        # No request supplied: unchanged, and every finding still rendered.
        assert "Withheld all" not in no_query
        assert _rule("F0") in no_query


class TestVectorStartupAdmission:
    def _store(self, tmp_path):
        store = VectorMemoryStore(db_path=tmp_path / "lessons.db")
        store.init()
        return store

    def test_findings_are_separated_from_rules_and_both_are_bounded(self, tmp_path):
        store = self._store(tmp_path)
        try:
            store.write_lesson(_rule("RULE"), category="preference", applies="always")
            for i in range(80):
                store.write_lesson(_rule(f"FINDING{i}"), category="knowledge", applies="on_topic")
            # Startup must never reach the embedding model.
            store.embed_fn = Mock(side_effect=AssertionError("background model call"))

            out = store.get_lessons_context(
                "",
                background=True,
                hard_cap=99_000,
                directive_budget=DIRECTIVE_BUDGET,
                experience_budget=EXPERIENCE_BUDGET,
            )

            assert _rule("RULE") in out
            assert "[Learned corrections" in out
            assert "[Learned experience" in out
            kept = sum(1 for i in range(80) if _rule(f"FINDING{i}") in out)
            assert 0 < kept < 80
            # The findings notice names its own budget and the same
            # budget-not-judgement explanation the rule notice carries.
            assert "past findings above the" in out
            assert "-character findings budget" in out
            assert "not a judgement" in out
            assert len(out) <= DIRECTIVE_BUDGET + EXPERIENCE_BUDGET + 1_000
        finally:
            store.close()

    def test_the_authored_value_is_persisted_and_read_back(self, tmp_path):
        store = self._store(tmp_path)
        try:
            store.write_lesson("Never force push", category="preference", applies="on_topic")
            rows = store.get_lessons()
            assert len(rows) == 1
            decoded = json.loads(rows[0]["value_json"])
            assert decoded["applies"] == LESSON_APPLIES_ON_TOPIC
            assert _lesson_applies(decoded) == LESSON_APPLIES_ON_TOPIC
        finally:
            store.close()

    def test_an_unstated_row_keeps_its_exact_stored_shape(self, tmp_path):
        store = self._store(tmp_path)
        try:
            store.write_lesson("Prefer dark mode", category="preference")
            decoded = json.loads(store.get_lessons()[0]["value_json"])
            # Absent, not a stored sentinel: the row is byte-identical to what this
            # writer produced before the field existed.
            assert "applies" not in decoded
        finally:
            store.close()

    def test_a_misspelled_value_reaches_the_caller(self, tmp_path):
        store = self._store(tmp_path)
        try:
            with pytest.raises(ValueError):
                store.write_lesson("r", applies="directive")
        finally:
            store.close()


class TestRetentionProtectsAuthoredRules:
    def test_findings_are_evicted_before_authored_rules(self):
        """A prompt omission is announced and recoverable; a prune is not.

        A tier-blind ``[-MAX:]`` here deletes the user's oldest authored standing
        rules from disk as cheap findings accumulate.
        """
        authored = [
            Lesson(ts=f"a{i}", rule=f"rule {i}", category="preference", applies="always")
            for i in range(10)
        ]
        findings = [
            Lesson(ts=f"f{i}", rule=f"finding {i}", category="knowledge", applies="on_topic")
            for i in range(_MAX_LESSONS_TOTAL)
        ]
        kept = _prune_to_total(authored + findings)

        assert len(kept) == _MAX_LESSONS_TOTAL
        assert all(lesson in kept for lesson in authored), "an authored rule was deleted"

    def test_unstated_rows_are_evicted_before_authored_rules(self):
        authored = [
            Lesson(ts=f"a{i}", rule=f"rule {i}", category="preference", applies="always")
            for i in range(10)
        ]
        unstated = [
            Lesson(ts=f"u{i}", rule=f"legacy {i}", category="knowledge")
            for i in range(_MAX_LESSONS_TOTAL)
        ]
        kept = _prune_to_total(authored + unstated)

        assert len(kept) == _MAX_LESSONS_TOTAL
        assert all(lesson in kept for lesson in authored)

    def test_authored_rules_alone_still_hit_the_bound_oldest_first(self):
        authored = [
            Lesson(ts=f"a{i:04d}", rule=f"rule {i}", category="preference", applies="always")
            for i in range(_MAX_LESSONS_TOTAL + 5)
        ]
        kept = _prune_to_total(authored)

        assert len(kept) == _MAX_LESSONS_TOTAL
        assert kept[0].rule == "rule 5", "eviction within a class is oldest-first"

    def test_under_the_bound_nothing_is_touched(self):
        rows = [Lesson(ts=str(i), rule=f"r{i}", category="tool") for i in range(5)]
        assert _prune_to_total(rows) is rows

    def test_kept_rows_retain_their_stored_order(self):
        rows = [
            Lesson(ts=f"f{i}", rule=f"finding {i}", category="knowledge", applies="on_topic")
            for i in range(5)
        ] + [
            Lesson(ts=f"a{i}", rule=f"rule {i}", category="preference", applies="always")
            for i in range(_MAX_LESSONS_TOTAL)
        ]
        kept = _prune_to_total(rows)
        assert kept == [row for row in rows if row in kept]

    def test_saving_past_the_cap_through_the_store_keeps_authored_rules(self, tmp_path):
        """The same guarantee through the REAL route, not the helper.

        The cases above call ``_prune_to_total`` directly, which leaves the
        store's own choice of pruner unpinned: restoring the tier-blind
        ``updated[-MAX:]`` in ``save`` passes every one of them. This case is the
        one that fails when that happens, so it is what actually guards the fix.
        """
        store = LessonStore(base_dir=tmp_path)
        for i in range(6):
            store.save(
                Lesson(
                    ts=f"2026-01-{i + 1:02d}",
                    rule=f"authored rule {i}",
                    category="preference",
                    applies="always",
                )
            )
        for i in range(_MAX_LESSONS_TOTAL + 20):
            store.save(
                Lesson(
                    ts=f"2026-06-{i % 28 + 1:02d}",
                    rule=f"finding number {i}",
                    category="knowledge",
                    applies="on_topic",
                )
            )

        stored = store.load_all()
        assert len(stored) == _MAX_LESSONS_TOTAL
        surviving = {lesson.rule for lesson in stored}
        for i in range(6):
            assert f"authored rule {i}" in surviving, "the store deleted an authored rule"

    def test_a_submission_the_prune_drops_is_refused_not_reported_inserted(self, tmp_path):
        """A write that did not land must not report success.

        At the cap a new ``on_topic`` row is the first eviction candidate, so it can
        be the row the prune removes. Reporting ``inserted`` there is a silent false
        success: the caller never learns to retry and the lesson is simply absent.
        """
        store = LessonStore(base_dir=tmp_path)
        for i in range(_MAX_LESSONS_TOTAL):
            store.save(
                Lesson(
                    ts=f"2026-01-{i % 28 + 1:02d}",
                    rule=f"standing rule {i}",
                    category="preference",
                    applies="always",
                )
            )
        assert len(store.load_all()) == _MAX_LESSONS_TOTAL

        outcome = store.save(
            Lesson(
                ts="2026-09-01",
                rule="a brand new finding",
                category="knowledge",
                applies="on_topic",
            )
        )

        assert outcome == "refused"
        assert "a brand new finding" not in {le.rule for le in store.load_all()}

    def test_an_unstated_row_is_not_serialized_with_a_null_tier(self, tmp_path):
        """Absence is absence in BOTH stores, so it reads as unstated in both."""
        store = LessonStore(base_dir=tmp_path)
        store.save(Lesson(ts="1", rule="prefer dark mode", category="preference"))
        line = store.path.read_text(encoding="utf-8").strip().splitlines()[0]
        assert "applies" not in json.loads(line)


def test_the_contradiction_sweep_drops_standing_rule_candidates():
    """The API route's sweep is a SECOND deletion path and needs the same guard.

    ``write_lesson``'s dedup scan refuses to let a finding retire a rule, but the
    route also runs a model-judged contradiction sweep that ends in
    ``delete_semantic``. Guarding one and not the other leaves the rule deletable.
    """
    from kiro_crew.dashboard.handlers.cron import _candidate_applies

    assert _candidate_applies({"applies": "on_topic"}) == LESSON_APPLIES_ON_TOPIC
    assert _candidate_applies({"applies": "always"}) == LESSON_APPLIES_ALWAYS
    # Read through value_json too, which is the shape the sweep actually receives.
    assert (
        _candidate_applies({"value_json": json.dumps({"rule": "r", "applies": "on_topic"})})
        == LESSON_APPLIES_ON_TOPIC
    )
    # Everything unreadable answers unstated, the PROTECTED side: a candidate this
    # cannot classify is never deleted by a finding.
    for unreadable in [{}, {"applies": "sometimes"}, {"value_json": "not json"}, None, "row"]:
        assert _candidate_applies(unreadable) == LESSON_APPLIES_UNSTATED


class TestAFindingNeverRetiresAStandingRule:
    """The dedup scan decides on text alone, so it needs the tier guard.

    A longer ``on_topic`` write that merely overlaps an authored ``always`` rule
    would otherwise tombstone it and leave only the topic-scoped row -- a silent,
    irreversible loss of the rule the user was most explicit about.
    """

    def _store(self, tmp_path):
        store = VectorMemoryStore(db_path=tmp_path / "lessons.db")
        store.init()
        return store

    def test_a_longer_on_topic_write_does_not_supersede_an_always_rule(self, tmp_path):
        store = self._store(tmp_path)
        try:
            store.write_lesson("never force push to a shared branch", applies="always")
            result = store.write_lesson(
                "while a release is running never force push to a shared branch",
                applies="on_topic",
            )
            rules = {json.loads(row["value_json"])["rule"] for row in store.get_lessons()}
            assert "never force push to a shared branch" in rules, "the standing rule was retired"
            assert result.superseded == ()
        finally:
            store.close()

    def test_a_longer_on_topic_write_does_not_supersede_an_unstated_row(self, tmp_path):
        """The case that matters on a real install: every legacy row is unstated.

        An unstated row is served AS a standing rule at injection, so the write side
        has to protect it the same way. A guard keyed on ``always`` alone protects
        nothing on a store whose lessons all predate the field.
        """
        store = self._store(tmp_path)
        try:
            store.write_lesson("never force push to a shared branch")  # no tier stated
            result = store.write_lesson(
                "while a release is running never force push to a shared branch",
                applies="on_topic",
            )
            rules = {json.loads(row["value_json"])["rule"] for row in store.get_lessons()}
            assert "never force push to a shared branch" in rules, "a legacy rule was retired"
            assert result.superseded == ()
        finally:
            store.close()

    def test_an_always_write_may_still_supersede_a_finding(self, tmp_path):
        """Asymmetric on purpose: promoting guidance to a rule is what the user asked."""
        store = self._store(tmp_path)
        try:
            store.write_lesson("never force push to a shared branch", applies="on_topic")
            result = store.write_lesson(
                "while a release is running never force push to a shared branch",
                applies="always",
            )
            assert result.superseded, "an always write must still be able to retire a finding"
        finally:
            store.close()

    def test_an_on_topic_write_still_declines_when_already_covered(self, tmp_path):
        """The guard gates deletion only; declining a covered submission loses nothing."""
        store = self._store(tmp_path)
        try:
            store.write_lesson(
                "while a release is running never force push to a shared branch",
                applies="always",
            )
            result = store.write_lesson("never force push to a shared branch", applies="on_topic")
            assert result.reason == "substring_covered"
        finally:
            store.close()

    def test_a_standing_write_is_not_declined_as_covered_by_a_finding(self, tmp_path):
        """The mirror of the decline, and the direction tiering makes conditional.

        "Already covered" holds only while the covering row arrives at least as
        often as the submission would. A covering ``on_topic`` row arrives only when
        the request is about it, so declining a STANDING submission contained in one
        drops the rule from every unrelated turn while reporting `substring_covered`
        for a row that does not cover it there -- and a re-submit is declined
        identically, so nothing recovers it.
        """
        store = self._store(tmp_path)
        try:
            store.write_lesson(
                "while a release is running never force push to a shared branch",
                applies="on_topic",
            )
            result = store.write_lesson("never force push to a shared branch", applies="always")
            assert result.reason != "substring_covered", "the standing rule was refused"
            rules = {json.loads(row["value_json"])["rule"] for row in store.get_lessons()}
            assert "never force push to a shared branch" in rules, "the standing rule is absent"
        finally:
            store.close()

    def test_an_unstated_submission_is_also_not_declined_by_a_finding(self, tmp_path):
        """An unstated submission is served AS a standing rule, so it needs the same.

        Every write predating this field, and every surface that states no tier,
        lands here -- so a guard keyed on ``always`` alone would leave the ordinary
        case unprotected.
        """
        store = self._store(tmp_path)
        try:
            store.write_lesson(
                "while a release is running never force push to a shared branch",
                applies="on_topic",
            )
            result = store.write_lesson("never force push to a shared branch")  # no tier stated
            assert result.reason != "substring_covered"
            rules = {json.loads(row["value_json"])["rule"] for row in store.get_lessons()}
            assert "never force push to a shared branch" in rules
        finally:
            store.close()

    def test_migration_carries_the_authored_tier(self, tmp_path, monkeypatch):
        """Dropping ``applies`` on migration promotes a finding into a standing rule.

        Same direction the scope gate already refuses to move: an omitted tier reads
        as unstated, which every read path serves as a standing rule, so a row the
        user filed as a past finding would start arriving in every session --
        silently widening exactly what the tier exists to bound. A malformed value
        migrates the row as unstated instead of aborting the migration, because this
        loop reads the file directly.
        """
        from kiro_crew import vector_memory as vm

        home = tmp_path / "home"
        home.mkdir()
        rows = [
            {
                "ts": "t",
                "rule": "flush the widget cache after a rebuild",
                "category": "knowledge",
                "applies": "on_topic",
            },
            {
                "ts": "t",
                "rule": "never force push to a protected branch",
                "category": "preference",
                "applies": "always",
            },
            {"ts": "t", "rule": "prefer ripgrep over ack when searching", "category": "knowledge"},
            {
                "ts": "t",
                "rule": "deployments need a manual smoke check",
                "category": "knowledge",
                "applies": "directive",
            },
        ]
        (home / "lessons.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        monkeypatch.setattr(vm, "config_dir", lambda: home)
        store = self._store(tmp_path)
        try:
            store.migrate_from_markdown()
            stored = {}
            for row in store.get_lessons():
                decoded = json.loads(row["value_json"])
                stored[decoded["rule"]] = decoded.get("applies")
            assert (
                stored["flush the widget cache after a rebuild"] == "on_topic"
            ), "the finding was promoted to a standing rule"
            assert stored["never force push to a protected branch"] == "always"
            # Absent stays absent: an unstated row is byte-identical to what a
            # pre-field writer produced, and a junk value migrates the same way
            # rather than failing the whole migration.
            assert stored["prefer ripgrep over ack when searching"] is None
            assert stored["deployments need a manual smoke check"] is None
        finally:
            store.close()


class TestContradictionCandidatesCarryTheirTier:
    """A sweep that cannot read a row's tier narrows to nothing, not to a guard.

    The caller refuses to let a past finding delete a standing or untiered rule,
    and it applies that refusal per candidate. A candidate row without its tier
    reads as untiered for EVERY row, so the refusal matched all of them and the
    model-judged contradiction sweep became a permanent no-op for findings --
    over-protective, so nothing was wrongly deleted, but the feature was off
    rather than narrowed. These cases pin the tier onto the row the sweep sees.
    """

    def _store(self, tmp_path):
        store = VectorMemoryStore(db_path=tmp_path / "lessons.db")
        store.init()
        # A deterministic stand-in for the embedding model: two texts sharing a
        # significant word land near each other, which is all the similarity
        # window needs. Keeps the case off the real model and off the network.
        store.embed_fn = lambda text: _lexical_embedding(text)
        return store

    def test_a_candidate_row_reports_its_authored_tier(self, tmp_path):
        store = self._store(tmp_path)
        try:
            store.write_lesson("prefer squash merges on release branches", applies="on_topic")
            store.write_lesson("never rewrite history on a shared branch", applies="always")
            store.write_lesson("tabs are fine in generated files")  # no tier stated
            candidates = store.find_contradiction_candidates(
                "sometimes rewrite history on a shared branch",
                threshold_low=0.0,
                threshold_high=0.999,
            )
            assert candidates, "the similarity window returned nothing to classify"
            by_rule = {c["rule"]: c for c in candidates}
            assert all(
                "applies" in c for c in candidates
            ), "a candidate row reached the sweep with no tier, so the sweep cannot narrow"
            # Read back through the handler's own reader, not by comparing the
            # raw value: the handler is what decides whether the sweep may
            # delete the row, so THAT answer is the one worth pinning.
            assert (
                _candidate_applies(by_rule["prefer squash merges on release branches"])
                == LESSON_APPLIES_ON_TOPIC
            )
            assert (
                _candidate_applies(by_rule["never rewrite history on a shared branch"])
                == LESSON_APPLIES_ALWAYS
            )
            assert (
                _candidate_applies(by_rule["tabs are fine in generated files"])
                == LESSON_APPLIES_UNSTATED
            )
        finally:
            store.close()

    def test_a_finding_submission_still_finds_a_finding_to_judge(self, tmp_path):
        """The sweep's whole purpose: an on_topic write must retain SOME candidate.

        This is the case the missing tier broke. The filter the handler applies
        keeps only same-tier candidates, so with the tier absent this list was
        empty on every finding submission and no contradiction was ever judged.
        """
        store = self._store(tmp_path)
        try:
            store.write_lesson("prefer squash merges on release branches", applies="on_topic")
            store.write_lesson("never rewrite history on a shared branch", applies="always")
            candidates = store.find_contradiction_candidates(
                "prefer merge commits on release branches",
                threshold_low=0.0,
                threshold_high=0.999,
            )
            survivors = [c for c in candidates if _candidate_applies(c) == LESSON_APPLIES_ON_TOPIC]
            assert survivors, "an on_topic write had every candidate filtered out"
            assert all(
                _candidate_applies(c) != LESSON_APPLIES_ALWAYS for c in survivors
            ), "a standing rule survived into a finding's deletion set"
        finally:
            store.close()

    def test_an_old_exact_match_survives_a_store_that_predates_the_tier(self, tmp_path):
        """The legacy case: every row is untagged, so the RULE tier holds the match.

        Leaving that tier newest-first lost an exact-topic match the vector store
        kept, because the vector path ranks its whole eligible set. Both tiers are
        now ordered per tier, so authored precedence survives and relevance decides
        inside each sub-list.
        """
        # Sized like the filler rows on purpose. A SHORT relevant row would be
        # rescued by the skip-and-continue fill alone, which makes the fixture
        # unable to tell ordering from fitting; at filler size only being sorted
        # first can save it.
        target = "raise the SQLite WAL busy_timeout when writes contend " + " ".join(
            f"wal{i}" for i in range(28)
        )
        rows = [Lesson(ts="2026-01-01", category="knowledge", rule=target)]
        for i in range(150):
            rows.append(
                Lesson(ts=f"2026-06-{i % 28 + 1:02d}", rule=_rule(f"FILL{i}"), category="knowledge")
            )
        store = _jsonl(tmp_path, rows)

        out = store.get_context(
            directive_budget=DIRECTIVE_BUDGET,
            experience_budget=EXPERIENCE_BUDGET,
            query_text="SQLite WAL busy_timeout write contention",
        )
        assert "busy_timeout" in out, "the exact-topic match fell outside the rule budget"

    def test_relevance_does_not_outrank_an_authored_rule(self, tmp_path):
        """Per-tier sorting, not one pool: an untagged row cannot jump a rule.

        Ordering the two sub-lists together would let a request-matching untagged
        note displace the rules the user was most explicit about, which is the
        precedence this tier exists to hold.
        """
        rows = [
            Lesson(ts="2026-01-01", rule=_rule("AUTHORED"), category="preference", applies="always")
        ]
        for i in range(150):
            rows.append(
                Lesson(
                    ts=f"2026-06-{i % 28 + 1:02d}",
                    category="knowledge",
                    rule=f"untagged note about quokka deployments {i} " + _rule(f"U{i}"),
                )
            )
        store = _jsonl(tmp_path, rows)

        out = store.get_context(
            directive_budget=DIRECTIVE_BUDGET,
            experience_budget=EXPERIENCE_BUDGET,
            query_text="quokka deployments",
        )
        assert _rule("AUTHORED") in out, "an untagged row displaced the authored rule"

    def test_an_entry_too_large_to_fit_is_skipped_not_final(self, tmp_path):
        """A row that does not fit must not end the selection.

        Stopping at the first oversized row wastes room a shorter, later row could
        have used. Skipping keeps a SUPERSET: the oversized row is dropped either
        way, so continuing can only add rows that do fit.
        """
        short = "flush the widget cache"
        entries: list[tuple[object, str]] = [
            (object(), "x" * 400),  # fits
            (object(), "y" * 400),  # fits
            (object(), "z" * 900),  # does NOT fit in what remains
            (object(), short),  # fits in the room the oversized row left
        ]
        block, omitted = render_lesson_tier(
            entries,
            1_000,
            header="[H]",
            footer="[F]",
            omission="[omitted {count} of {total} over {limit}]",
        )
        assert short in block, "a fitting row after an oversized one was dropped"
        assert "z" * 900 not in block, "the oversized row must still be omitted"
        assert omitted == 1
        assert len(block) <= 1_000 + len("[omitted 1 of 4 over 1000]\n\n")


class TestAFindingIsWithheldWhenNothingMatches:
    """A tier of findings is not filler: an unrelated request gets none of it.

    `on_topic` means "worth having when the task touches it". Ordering alone left
    a greeting spending the whole findings allowance on the NEWEST rows, which are
    unrelated by construction -- and that filler never rescued a near-miss either,
    because it surfaces the newest rows rather than the closest ones. The frame is
    still rendered, so the next turn can see that findings exist and how to reach
    them; a silently absent block reads as "this user has no findings".
    """

    def _vector(self, tmp_path):
        store = VectorMemoryStore(db_path=tmp_path / "lessons.db")
        store.init()
        return store

    def test_a_greeting_gets_no_findings_from_either_store(self, tmp_path):
        rows = [
            Lesson(
                ts=f"2026-06-{i % 28 + 1:02d}",
                category="knowledge",
                applies="on_topic",
                rule=f"finding {i} about quokka deployment step {i}",
            )
            for i in range(20)
        ]
        store = _jsonl(tmp_path, rows)
        out = store.get_context(
            directive_budget=DIRECTIVE_BUDGET,
            experience_budget=EXPERIENCE_BUDGET,
            query_text="hi",
        )
        assert "quokka" not in out, "a greeting was served finding filler"
        assert "Withheld all 20 past findings" in out, "the withholding was silent"
        assert "memory_recall" in out, "the reader was not told how to reach them"

        vs = self._vector(tmp_path / "v")
        try:
            for i in range(20):
                vs.write_lesson(f"finding {i} about quokka deployment step {i}", applies="on_topic")
            vout = vs.get_lessons_context(
                background=True,
                directive_budget=DIRECTIVE_BUDGET,
                experience_budget=EXPERIENCE_BUDGET,
                query_text="hi",
            )
            assert "quokka" not in vout, "the vector store served filler for a greeting"
            assert "Withheld all" in vout
        finally:
            vs.close()

    def test_a_matching_request_still_gets_its_finding(self, tmp_path):
        """The admission test must not cost recall: one shared word is enough."""
        rows = [
            Lesson(
                ts=f"2026-06-{i % 28 + 1:02d}",
                category="knowledge",
                applies="on_topic",
                rule=f"finding {i} about quokka deployment step {i}",
            )
            for i in range(20)
        ]
        store = _jsonl(tmp_path, rows)
        out = store.get_context(
            directive_budget=DIRECTIVE_BUDGET,
            experience_budget=EXPERIENCE_BUDGET,
            query_text="the quokka deployment keeps timing out",
        )
        assert "quokka" in out, "a matching request lost its findings"
        assert "Withheld all" not in out

    def test_standing_rules_are_never_withheld_on_relevance(self, tmp_path):
        """Admission is for findings ONLY. A rule applies whatever the message is."""
        rows = [
            Lesson(
                ts="2026-01-01", rule=_rule("STANDING"), category="preference", applies="always"
            ),
            Lesson(ts="2026-01-02", rule=_rule("LEGACY"), category="knowledge"),
            Lesson(
                ts="2026-01-03",
                category="knowledge",
                applies="on_topic",
                rule="finding about quokka deployments",
            ),
        ]
        store = _jsonl(tmp_path, rows)
        out = store.get_context(
            directive_budget=DIRECTIVE_BUDGET,
            experience_budget=EXPERIENCE_BUDGET,
            query_text="hi",
        )
        assert _rule("STANDING") in out, "a standing rule was withheld on relevance"
        assert _rule("LEGACY") in out, "an untagged row is served as a rule and must arrive"
        assert "quokka" not in out

    def test_an_empty_findings_tier_renders_nothing(self, tmp_path):
        """No findings at all is not the same event and gets no block."""
        store = _jsonl(
            tmp_path,
            [
                Lesson(
                    ts="2026-01-01", rule=_rule("ONLYRULE"), category="preference", applies="always"
                )
            ],
        )
        out = store.get_context(
            directive_budget=DIRECTIVE_BUDGET,
            experience_budget=EXPERIENCE_BUDGET,
            query_text="hi",
        )
        assert "[Learned experience" not in out
        assert "Withheld all" not in out


class TestTheTierIsVisibleWhereOverflowSendsTheReader:
    """Every overflow notice names ``learn_list``, so the tier must show there.

    A rule misfiled as a finding stops arriving on unrelated sessions. Re-tiering
    is a remove plus a re-add, which a reader can only decide to do if the
    listing tells them the row is filed as a finding at all.
    """

    def test_the_route_reports_an_authored_tier_and_omits_an_unstated_one(self):
        # ``None`` is not a third tier: an untiered row is served as a standing
        # rule, so reporting a value for it would name a distinction the
        # injection path does not make.
        assert authored_lesson_applies("on_topic") == LESSON_APPLIES_ON_TOPIC
        assert authored_lesson_applies("always") == LESSON_APPLIES_ALWAYS
        assert authored_lesson_applies(None) is None
        assert authored_lesson_applies("") is None
        assert authored_lesson_applies("directive") is None

    def test_a_malformed_stored_tier_does_not_fail_the_page(self):
        """Opposite of the write path on purpose: a listing serves stored rows.

        ``normalize_lesson_applies`` RAISES on a value that is present but not one
        of the two literals, because a write surface that misspells the tier has a
        bug and must hear about it. A listing has no such latitude -- it renders
        rows an older path or a hand-edit already put in the file, and failing the
        whole page over one of them hides every other row on it.

        Both sides agree on surrounding whitespace and case, which is
        canonicalization rather than a malformed value.
        """
        with pytest.raises(ValueError):
            normalize_lesson_applies("directive")
        assert authored_lesson_applies("directive") is None
        assert normalize_lesson_applies("  Always ") == LESSON_APPLIES_ALWAYS
        assert authored_lesson_applies("  Always ") == LESSON_APPLIES_ALWAYS
        # Non-string shapes reach the reader from a hand-edited file; the write
        # path rejects them, the listing must not.
        with pytest.raises(ValueError):
            normalize_lesson_applies(17)
        assert authored_lesson_applies({"applies": "always"}) is None
        assert authored_lesson_applies(17) is None

    def test_learn_list_marks_a_finding_and_leaves_a_rule_bare(self):
        from kiro_crew.mcp_tools.learn import _applies_suffix

        assert _applies_suffix({"applies": "on_topic"}) == " (applies: on_topic)"
        # A standing rule and an untiered row both arrive every session, so
        # neither is marked: marking one and not the other would show a
        # difference that does not exist.
        assert _applies_suffix({"applies": "always"}) == ""
        assert _applies_suffix({}) == ""
        assert _applies_suffix({"applies": None}) == ""

    def test_the_findings_header_does_not_claim_an_order_the_sort_overrides(self, tmp_path):
        """The block says "relevant ones first" because that is what it renders.

        ``order_findings_by_relevance`` runs before this block is built and
        deliberately lets an OLDER relevant finding outrank newer irrelevant ones,
        so a header reading "newest first" describes the pre-sort order and is
        false for exactly the store this change exists to serve. The vector tier
        already said "relevant ones first"; the two renderers now agree.
        """
        store = _jsonl(
            tmp_path,
            [
                Lesson(
                    ts="1",
                    rule="widget rebuild needs a cache flush",
                    category="knowledge",
                    applies="on_topic",
                )
            ],
        )
        out = store.get_context(
            directive_budget=DIRECTIVE_BUDGET,
            experience_budget=EXPERIENCE_BUDGET,
            query_text="widget",
        )
        assert "[Learned experience — past findings, relevant ones first." in out
        assert "newest first" not in out

    def test_the_capacity_message_does_not_tell_the_caller_to_reword(self, monkeypatch):
        """Driven through ``learn_add`` itself, not a helper: the wording lives there."""
        from kiro_crew import mcp_core
        from kiro_crew.mcp_tools import learn as mcp_learn

        monkeypatch.setattr(
            mcp_core,
            "_post",
            lambda *a, **k: {"outcome": "refused", "reason": LESSON_REFUSED_AT_CAPACITY},
        )
        out = mcp_learn.learn_add("learn_add", {"rule": "always run the formatter before pushing"})
        assert "NOT saved" in out
        assert "learn_remove" in out, "the caller needs the action that frees a row"
        # The volatile branch's advice is actively wrong here.
        assert "plainer wording" not in out
        assert "volatile" not in out
