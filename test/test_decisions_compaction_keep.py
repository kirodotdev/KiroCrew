"""``compaction.keep`` -- the shadow point that scores an automatic compaction.

Four properties this file pins, because each of them is a way the measurement could
be wrong rather than merely absent:

* the FITTING ladder picks the first stage under the ceiling, and a transcript that
  fits nowhere is recorded rather than sent;
* the redaction runs BEFORE the clip, and no tool RESULT byte is on the wire at any
  stage;
* the questions are batched, every batch carries the SAME state, and a partial
  answer publishes nothing;
* the per-call answers map onto the three options and onto the two character arms.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew.decisions import consent as consent_mod
from kiro_crew.decisions import gate as gate_mod
from kiro_crew.decisions.points import compaction_keep as point
from kiro_crew.decisions.types import Answer, Choice

SECRET = "AKIAIOSFODNN7EXAMPLE"


def tool_row(index: int, *, tool: str = "execute_bash", inp: str = "ls", out: str = "ok") -> dict:
    """The DISPATCH row ``chat_runner`` appends when a tool call starts."""
    return {
        "role": "tool",
        "content": f"\U0001f527 {tool}",
        "meta": {"tool_call_id": f"tc{index}", "input": inp, "output": out},
    }


def approval_row(index: int, *, tool: str = "execute_bash", approved: bool = True) -> dict:
    """The row the APPROVAL path appends for the SAME call, with the same id.

    ``chat_runner`` appends this beside the dispatch row on an interactive approval, so
    one logical call reaches the transcript as two or three ``role="tool"`` rows.
    """
    glyph = "\u2705" if approved else "\U0001f6ab"
    suffix = "" if approved else " (rejected)"
    return {
        "role": "tool",
        "content": f"{glyph} {tool}{suffix}",
        "meta": {"tool_call_id": f"tc{index}", "purpose": "", "input": "", "output": ""},
    }


def transcript(calls: int = 3, *, inp: str = "ls -la", out: str = "a" * 100) -> list[dict]:
    """A transcript with *calls* tool rows and a conversation around them.

    Deliberately longer than ``PINNED_TAIL_ROWS`` so the pinning split is exercised
    rather than making every row pinned.
    """
    rows: list[dict] = [{"role": "user", "content": "please do the thing"}]
    for index in range(calls):
        rows.append(tool_row(index, inp=inp, out=out))
        rows.append({"role": "assistant", "content": f"step {index} done"})
    return rows


@pytest.fixture(autouse=True)
def _no_leftover_records():
    point.reset_records()
    yield
    point.reset_records()


class TestTranscriptReading:
    def test_tool_rows_carry_their_real_input_and_result_sizes(self):
        built = point.build_transcript(transcript(calls=2, inp="x" * 30, out="y" * 70))
        assert [call.input_chars for call in built.calls] == [30, 30]
        assert [call.result_chars for call in built.calls] == [70, 70]

    def test_chars_today_counts_only_what_the_replay_keeps(self):
        # The comparison arm is today's recycle replay, which admits user/assistant/
        # inject and drops every tool row. An eligible arm counting tool traffic
        # would make the two arms describe the same thing.
        rows = [
            {"role": "user", "content": "abcde"},
            tool_row(0, inp="i" * 100, out="o" * 100),
            {"role": "assistant", "content": "fgh"},
            {"role": "chunk", "content": "ignored entirely"},
        ]
        built = point.build_transcript(rows)
        assert built.chars_today_eligible == len("abcde") + len("fgh")
        assert built.chars_all == len("abcde") + len("fgh") + 200

    def test_first_row_and_the_newest_six_are_pinned(self):
        rows = [tool_row(index) for index in range(12)]
        built = point.build_transcript(rows)
        pinned = [call.index for call in built.calls if call.pinned]
        assert pinned == [0, 6, 7, 8, 9, 10, 11]

    def test_a_pinned_call_is_never_asked_about(self):
        built = point.build_transcript([tool_row(index) for index in range(12)])
        asked = {question.id for question in point.questions_for(built.calls)}
        assert asked == {f"call_{index}" for index in range(1, 6)}

    def test_an_unreadable_row_is_skipped_rather_than_raising(self):
        rows = [{"role": "user", "content": "ok"}, "not a row", {"role": "tool", "meta": 7}]
        built = point.build_transcript(rows)  # type: ignore[arg-type]
        assert len(built.calls) == 1
        assert built.calls[0].input_chars == 0

    def test_the_call_count_is_bounded(self, monkeypatch):
        monkeypatch.setattr(point, "MAX_CALLS", 4)
        built = point.build_transcript([tool_row(index) for index in range(40)])
        assert len(built.calls) == 4


class TestStateShape:
    def test_no_tool_result_byte_is_sent_at_any_stage(self):
        built = point.build_transcript(transcript(calls=4, out="TOPSECRETOUTPUT" * 20))
        for stage in point.STAGES:
            rendered = json.dumps(point.build_state(built, stage))
            assert "TOPSECRETOUTPUT" not in rendered
            assert "(omitted)" in rendered

    def test_a_result_is_replaced_by_its_own_size(self):
        built = point.build_transcript(transcript(calls=1, out="z" * 4096))
        state = point.build_state(built, point.STAGES[0])
        assert state["calls"][0]["result"] == "ok, 4096 chars (omitted)"

    def test_redaction_routes_through_the_companion_seam(self, monkeypatch):
        """Not the bare baseline: a host with a companion has its OWN patterns.

        The baseline knows only the spellings this repository ships, so a
        baseline-only scrub would send a companion-defined secret verbatim to a third
        party. The shim is stubbed with a marker rather than asserted by name, so the
        test fails if a later edit reaches past it.
        """
        seen: list[str] = []

        def _shim(value: str) -> str:
            seen.append(value)
            return "COMPANION-CLEANED"

        monkeypatch.setattr("kiro_crew.platform.context.redact_via_context", _shim)
        built = point.build_transcript(transcript(calls=1, inp="company-token-abc"))
        state = point.build_state(built, point.STAGES[0])
        assert state["calls"][0]["input"] == "COMPANION-CLEANED"
        assert "company-token-abc" in seen

    def test_a_composition_failure_drops_the_field_rather_than_sending_it(self, monkeypatch):
        """The shim re-raises on a host that could not compose its companion.

        Swallowing that and falling back to the baseline is exactly the silent
        downgrade the shim exists to prevent, so the field is dropped instead.
        """

        def _boom(_value: str) -> str:
            raise RuntimeError("companion unavailable")

        monkeypatch.setattr("kiro_crew.platform.context.redact_via_context", _boom)
        built = point.build_transcript(transcript(calls=1, inp="something"))
        state = point.build_state(built, point.STAGES[0])
        assert state["calls"][0]["input"] == ""

    def test_redaction_runs_before_the_clip(self):
        # The clip is 6 characters and the secret starts at offset 0, so a clip-first
        # implementation would send a 6-character fragment of the key that neither
        # redactor matches. Redaction-first replaces it, and the placeholder is what
        # gets clipped.
        built = point.build_transcript(transcript(calls=1, inp=SECRET))
        stage = point.Stage("tiny", 6, 1.0)
        sent = point.build_state(built, stage)["calls"][0]["input"]
        assert SECRET[:6] not in sent
        assert SECRET not in sent

    def test_a_pinned_call_says_so_in_the_state(self):
        built = point.build_transcript([tool_row(index) for index in range(12)])
        state = point.build_state(built, point.STAGES[0])
        assert state["calls"][0].get("pinned") is True
        assert "pinned" not in state["calls"][3]

    def test_conversation_text_is_verbatim_until_a_stage_cuts_it(self):
        rows = [{"role": "user", "content": "q" * 400}, tool_row(0)]
        built = point.build_transcript(rows)
        assert point.build_state(built, point.STAGES[0])["messages"][0]["text"] == "q" * 400
        halved = point.build_state(built, point.STAGES[3])["messages"][0]["text"]
        assert len(halved) < 400
        assert halved.startswith("q") and halved.endswith("q")

    def test_one_line_stage_leaves_no_newline_in_an_input(self):
        built = point.build_transcript(transcript(calls=1, inp="a\nb\nc"))
        state = point.build_state(built, point.STAGES[-1])
        assert "\n" not in state["calls"][0]["input"]


class TestFitting:
    def test_the_first_stage_under_the_ceiling_wins(self):
        built = point.build_transcript(transcript(calls=2, inp="i" * 50))
        fitted = point.fit_state(built)
        assert fitted is not None
        assert fitted[1].name == point.STAGES[0].name

    def test_the_ladder_shrinks_monotonically(self):
        # Each rung must be no larger than the one above it, or "the first stage that
        # fits" would not be the mildest one that fits.
        built = point.build_transcript(transcript(calls=8, inp="i" * 900))
        sizes = [point.estimated_tokens(point.build_state(built, s)) for s in point.STAGES]
        assert sizes == sorted(sizes, reverse=True)
        assert sizes[-1] < sizes[0]

    def test_a_large_transcript_walks_down_the_ladder(self, monkeypatch):
        # The ceiling is derived from the SECOND rung's own measurement rather than
        # guessed, so the test says "the first rung is too big and the second is not"
        # without encoding today's character counts.
        built = point.build_transcript(transcript(calls=8, inp="i" * 900))
        second = point.estimated_tokens(point.build_state(built, point.STAGES[1]))
        monkeypatch.setattr(point, "STATE_TOKEN_CEILING", second)
        fitted = point.fit_state(built)
        assert fitted is not None
        assert fitted[1].name == point.STAGES[1].name

    def test_a_transcript_that_fits_nowhere_is_refused(self, monkeypatch):
        monkeypatch.setattr(point, "STATE_TOKEN_CEILING", 1)
        built = point.build_transcript(transcript(calls=40))
        assert point.fit_state(built) is None


class TestBatching:
    def test_questions_split_into_bounded_requests_in_order(self):
        asked = [Choice(f"call_{n}", point.PROMPT, options=list(point.OPTIONS)) for n in range(57)]
        groups = point.batches(asked, 25)
        assert [len(group) for group in groups] == [25, 25, 7]
        assert [q.id for q in groups[0]][:2] == ["call_0", "call_1"]

    def test_every_question_carries_the_three_options(self):
        built = point.build_transcript(transcript(calls=9))
        for question in point.questions_for(built.calls):
            assert question.options == [point.OPTION_DROP, point.OPTION_CALL, point.OPTION_BOTH]


class TestAnswerReading:
    def _answers(self, mapping):
        return {qid: Answer(id=qid, value=value, p=0.9) for qid, value in mapping.items()}

    def test_a_complete_batch_reads_back(self):
        asked = [Choice("call_1", point.PROMPT, options=list(point.OPTIONS))]
        read = point.read_decisions(self._answers({"call_1": point.OPTION_BOTH}), asked)
        assert read == {"call_1": (point.OPTION_BOTH, 0.9)}

    def test_a_batch_missing_one_answer_is_unusable_rather_than_partial(self):
        # All-or-nothing per batch: a mapping folded with a gap would be counted into
        # a fraction over calls nobody answered about.
        asked = [
            Choice("call_1", point.PROMPT, options=list(point.OPTIONS)),
            Choice("call_2", point.PROMPT, options=list(point.OPTIONS)),
        ]
        assert point.read_decisions(self._answers({"call_1": point.OPTION_DROP}), asked) is None

    def test_an_option_outside_the_domain_is_unusable(self):
        asked = [Choice("call_1", point.PROMPT, options=list(point.OPTIONS))]
        assert point.read_decisions(self._answers({"call_1": "keep-everything"}), asked) is None

    def test_no_answers_at_all_is_unusable(self):
        asked = [Choice("call_1", point.PROMPT, options=list(point.OPTIONS))]
        assert point.read_decisions(None, asked) is None


class TestTally:
    def test_each_option_lands_on_its_own_counter_and_its_own_chars(self):
        rows = [
            {"role": "user", "content": "u" * 10},
            tool_row(0, inp="i" * 100, out="o" * 1000),
            tool_row(1, inp="i" * 100, out="o" * 1000),
            tool_row(2, inp="i" * 100, out="o" * 1000),
        ]
        built = point.build_transcript(rows)
        # Row 0 is the user message, so rows 1-3 are the calls; with four rows every
        # one of them is inside the pinned tail, so pin nothing by hand and instead
        # assert against the pinning the walk actually produced.
        decisions = {
            call.question_id: (option, 0.5)
            for call, option in zip(
                [c for c in built.calls if not c.pinned],
                [point.OPTION_BOTH, point.OPTION_CALL, point.OPTION_DROP],
            )
        }
        counts = point.tally(built, decisions)
        assert counts["total_calls"] == 3
        assert counts["kept_both"] + counts["kept_call"] + counts["dropped"] == 3
        assert counts["chars_today_eligible"] == 10
        assert counts["chars_jev_eligible"] > counts["chars_today_eligible"]
        assert counts["chars_jev_eligible"] <= counts["chars_all"]

    def test_a_pinned_call_counts_as_kept_whole(self):
        built = point.build_transcript([tool_row(index, inp="i", out="o") for index in range(12)])
        decisions = {
            call.question_id: (point.OPTION_DROP, 0.5) for call in built.calls if not call.pinned
        }
        counts = point.tally(built, decisions)
        assert counts["pinned_calls"] == 7
        assert counts["kept_both"] == 7
        assert counts["dropped"] == 5

    def test_dropping_everything_leaves_the_two_arms_equal(self):
        rows = [{"role": "user", "content": "u" * 10}] + [
            tool_row(index, inp="i" * 5, out="o" * 5) for index in range(9)
        ]
        built = point.build_transcript(rows)
        for call in built.calls:
            call.pinned = False
        decisions = {call.question_id: (point.OPTION_DROP, 0.1) for call in built.calls}
        counts = point.tally(built, decisions)
        assert counts["chars_jev_eligible"] == counts["chars_today_eligible"] == 10


class TestRun:
    """The whole coroutine, with the gate's ``decide`` replaced by a stub.

    ``decide`` is the seam every refusal funnels through, so stubbing it is what lets
    these tests be about the POINT rather than about consent, sampling or transport.
    """

    def _enable(self, monkeypatch):
        monkeypatch.setattr(point.core, "is_enabled", lambda *_a, **_kw: True)
        monkeypatch.setattr(point, "wait_budget", lambda: 30.0)

    def _rows(self, monkeypatch, rows):
        monkeypatch.setattr(point, "read_rows", lambda _key, _before="": rows)

    def _appended(self, monkeypatch) -> list[dict]:
        written: list[dict] = []

        def _append(row):
            written.append(row)
            return True

        monkeypatch.setattr(point._log, "append", _append)
        return written

    def _answer_all(self, monkeypatch, option: str, *, seen: list | None = None):
        async def _decide(_p, state, questions, **_kw):
            if seen is not None:
                seen.append((state, [q.id for q in questions]))
            return {q.id: Answer(id=q.id, value=option, p=0.8) for q in questions}

        monkeypatch.setattr(point.core, "decide", _decide)

    def test_a_complete_run_writes_one_outcome_row_and_publishes_it(self, monkeypatch):
        self._enable(monkeypatch)
        self._rows(monkeypatch, transcript(calls=9))
        written = self._appended(monkeypatch)
        self._answer_all(monkeypatch, point.OPTION_CALL)
        record = asyncio.run(point.score_compaction("sess"))
        assert record is not None
        assert record["point"] == point.POINT
        assert record["error"] is None
        assert record["fitting_stage"] == point.STAGES[0].name
        assert len(written) == 1
        assert point.take_record("sess") == record

    def test_every_batch_carries_the_same_state(self, monkeypatch):
        monkeypatch.setattr(point, "QUESTIONS_PER_REQUEST", 2)
        self._enable(monkeypatch)
        self._rows(monkeypatch, transcript(calls=12))
        self._appended(monkeypatch)
        seen: list = []
        self._answer_all(monkeypatch, point.OPTION_BOTH, seen=seen)
        record = asyncio.run(point.score_compaction("sess"))
        assert record is not None
        assert record["requests"] == len(seen) > 1
        first = json.dumps(seen[0][0], sort_keys=True)
        assert all(json.dumps(state, sort_keys=True) == first for state, _ids in seen)
        # Every non-pinned call is asked about exactly once across the batches.
        asked = [qid for _state, ids in seen for qid in ids]
        assert len(asked) == len(set(asked))

    def test_a_partial_answer_is_recorded_and_never_published(self, monkeypatch):
        monkeypatch.setattr(point, "QUESTIONS_PER_REQUEST", 2)
        self._enable(monkeypatch)
        self._rows(monkeypatch, transcript(calls=12))
        written = self._appended(monkeypatch)
        calls = {"n": 0}

        async def _decide(_p, _state, questions, **_kw):
            calls["n"] += 1
            if calls["n"] == 2:
                return None
            return {q.id: Answer(id=q.id, value=point.OPTION_BOTH, p=0.7) for q in questions}

        monkeypatch.setattr(point.core, "decide", _decide)
        assert asyncio.run(point.score_compaction("sess")) is None
        assert [row["error"] for row in written] == [point.ERROR_PARTIAL]
        assert point.take_record("sess") is None

    def test_no_answer_at_all_writes_nothing_of_its_own(self, monkeypatch):
        # The gate has already written a row per refused request carrying its own
        # error category, so a second row here would say nothing new.
        self._enable(monkeypatch)
        self._rows(monkeypatch, transcript(calls=9))
        written = self._appended(monkeypatch)

        async def _decide(*_a, **_kw):
            return None

        monkeypatch.setattr(point.core, "decide", _decide)
        assert asyncio.run(point.score_compaction("sess")) is None
        assert written == []

    def test_a_state_that_fits_nowhere_is_recorded_and_nothing_is_sent(self, monkeypatch):
        monkeypatch.setattr(point, "STATE_TOKEN_CEILING", 1)
        self._enable(monkeypatch)
        self._rows(monkeypatch, transcript(calls=9))
        written = self._appended(monkeypatch)
        sent = {"n": 0}

        async def _decide(*_a, **_kw):
            sent["n"] += 1
            return None

        monkeypatch.setattr(point.core, "decide", _decide)
        assert asyncio.run(point.score_compaction("sess")) is None
        assert sent["n"] == 0
        assert [row["error"] for row in written] == [point.ERROR_TOO_LARGE]

    def test_the_seam_being_off_reads_no_transcript_at_all(self, monkeypatch):
        # The keystone read is the cheap refusal and goes first: an unconsented
        # machine must not read a megabyte of transcript to learn it sends nothing.
        monkeypatch.setattr(point.core, "is_enabled", lambda *_a, **_kw: False)
        read = {"n": 0}

        def _read(_key, _before=""):
            read["n"] += 1
            return transcript()

        monkeypatch.setattr(point, "read_rows", _read)
        assert asyncio.run(point.score_compaction("sess")) is None
        assert read["n"] == 0

    def test_a_transcript_with_no_tool_calls_is_not_a_measurement(self, monkeypatch):
        self._enable(monkeypatch)
        self._rows(monkeypatch, [{"role": "user", "content": "hello"}])
        written = self._appended(monkeypatch)
        assert asyncio.run(point.score_compaction("sess")) is None
        assert written == []

    def test_a_run_that_outlives_its_budget_records_the_timeout(self, monkeypatch):
        self._enable(monkeypatch)
        monkeypatch.setattr(point, "wait_budget", lambda: 0.01)
        self._rows(monkeypatch, transcript(calls=9))
        written = self._appended(monkeypatch)

        async def _slow(*_a, **_kw):
            await asyncio.sleep(5)

        monkeypatch.setattr(point.core, "decide", _slow)
        assert asyncio.run(point.score_compaction("sess")) is None
        assert [row["error"] for row in written] == [point.ERROR_RUN_TIMEOUT]

    def test_a_refused_row_is_never_published(self, monkeypatch):
        self._enable(monkeypatch)
        self._rows(monkeypatch, transcript(calls=9))
        monkeypatch.setattr(point._log, "append", lambda _row: False)
        self._answer_all(monkeypatch, point.OPTION_BOTH)
        assert asyncio.run(point.score_compaction("sess")) is None
        assert point.take_record("sess") is None


class TestRecordStore:
    def test_a_record_is_claimed_once(self):
        point.publish_record("s", {"turn_id": "t"})
        assert point.take_record("s") == {"turn_id": "t"}
        assert point.take_record("s") is None

    def test_a_newer_record_replaces_the_one_nobody_read(self):
        point.publish_record("s", {"turn_id": "old"})
        point.publish_record("s", {"turn_id": "new"})
        assert point.take_record("s") == {"turn_id": "new"}

    def test_an_expired_record_is_not_handed_to_a_later_compaction(self, monkeypatch):
        # The clock is PINNED rather than relied on to advance: Windows'
        # ``time.monotonic()`` has ~15.6 ms resolution, so a publish and a take in the
        # same statement pair read the same value and nothing would have aged. Moving
        # the clock past the TTL states the property the store has instead of
        # measuring the platform's timer.
        now = [1000.0]
        monkeypatch.setattr(point.time, "monotonic", lambda: now[0])
        point.publish_record("s", {"turn_id": "t"})
        now[0] += point.RECORD_TTL_SECONDS + 1.0
        assert point.take_record("s") is None
        assert point.pending_count() == 0

    def test_a_record_inside_its_ttl_is_still_handed_over(self, monkeypatch):
        # The other side of the same boundary, so the sweep cannot be "expire
        # everything" and pass the test above.
        now = [1000.0]
        monkeypatch.setattr(point.time, "monotonic", lambda: now[0])
        point.publish_record("s", {"turn_id": "t"})
        now[0] += point.RECORD_TTL_SECONDS - 1.0
        assert point.take_record("s") == {"turn_id": "t"}

    def test_the_store_is_bounded(self, monkeypatch):
        monkeypatch.setattr(point, "MAX_PENDING_RECORDS", 3)
        for n in range(10):
            point.publish_record(f"s{n}", {"turn_id": str(n)})
        assert point.pending_count() == 3

    def test_a_keyless_record_is_dropped_rather_than_filed_under_nothing(self):
        point.publish_record("", {"turn_id": "t"})
        assert point.pending_count() == 0
        assert point.take_record("") is None


class TestConsentScope:
    """The third scope, held against the gate rather than against the point.

    A point can only be inert for the right reason if the REFUSAL is the gate's, so
    these drive ``gate`` directly.
    """

    def test_the_point_is_registered(self):
        assert point.POINT in gate_mod.DECISION_POINT_NAMES
        assert point.POINT in gate_mod.POINTS_NEEDING_COMPACTION

    def test_an_absent_scope_refuses_the_point(self):
        assert gate_mod._scope_consented(point.POINT, {}) is False

    def test_the_tool_argument_scope_does_not_grant_this_one(self):
        # The narrower yes must never read as the wider one: tool_args was reviewed as
        # the arguments of the one call about to run.
        state = {consent_mod.STATE_KEY_TOOL_ARGS: True}
        assert gate_mod._scope_consented(point.POINT, state) is False
        assert gate_mod._scope_consented("tool.risk", state) is True

    def test_only_a_literal_true_consents(self):
        for value in ("true", 1, "yes", None):
            assert (
                gate_mod._scope_consented(point.POINT, {consent_mod.STATE_KEY_COMPACTION: value})
                is False
            )
        assert (
            gate_mod._scope_consented(point.POINT, {consent_mod.STATE_KEY_COMPACTION: True}) is True
        )

    def test_a_point_needing_no_scope_is_untouched(self):
        assert gate_mod._scope_consented("skills.select", {}) is True
        assert gate_mod._scope_consented(None, {}) is True


class TestOneCharacterUniverse:
    """``chars_today_eligible <= chars_jev_eligible <= chars_all``: a share is a share.

    An ``inject`` row is the case that broke it: the replay KEEPS it (it is in
    ``context.RECALL_ROLES``) but it carries no tool call, so it is not part of the
    state. A denominator built from the state's roles alone omitted bytes the
    numerator counted, and the card rendered a share above 100%.
    """

    def _rows(self, inject_chars: int) -> list[dict]:
        return [
            {"role": "user", "content": "u" * 10},
            {"role": "inject", "content": "i" * inject_chars},
            tool_row(0, inp="a" * 20, out="b" * 30),
            tool_row(1, inp="a" * 20, out="b" * 30),
        ]

    def test_an_inject_row_is_in_both_arms(self):
        built = point.build_transcript(self._rows(500))
        assert built.chars_today_eligible == 510
        assert built.chars_all >= built.chars_today_eligible

    def test_keeping_everything_cannot_exceed_the_whole_transcript(self):
        # The reported failure shape, directly: a sizable inject row plus calls Jev
        # mostly keeps is what drives the share above 100% without one universe.
        built = point.build_transcript(self._rows(5000))
        for call in built.calls:
            call.pinned = False
        decisions = {call.question_id: (point.OPTION_BOTH, 0.9) for call in built.calls}
        counts = point.tally(built, decisions)
        assert counts["chars_today_eligible"] <= counts["chars_jev_eligible"] <= counts["chars_all"]
        assert counts["chars_jev_eligible"] / counts["chars_all"] <= 1.0

    @pytest.mark.parametrize("option", [point.OPTION_DROP, point.OPTION_CALL, point.OPTION_BOTH])
    def test_the_invariant_holds_for_every_answer(self, option):
        built = point.build_transcript(self._rows(2000))
        for call in built.calls:
            call.pinned = False
        decisions = {call.question_id: (option, 0.5) for call in built.calls}
        counts = point.tally(built, decisions)
        assert counts["chars_today_eligible"] <= counts["chars_jev_eligible"] <= counts["chars_all"]

    def test_the_eligible_arm_is_an_upper_bound_on_the_replay(self):
        """It is named for what it measures: the ROLE filter, and nothing further.

        ``build_session_replay`` applies per-role row quotas and a character budget on
        top of the role filter, so this arm is an upper bound rather than the figure.
        The name is the contract -- the copy on the line says "up to" for the same
        reason -- and this holds the arm to the role filter's own definition so a
        future edit cannot quietly start claiming the narrower thing.
        """
        from kiro_crew.context import RECALL_ROLES

        rows = [
            {"role": role, "content": "x" * 5}
            for role in list(RECALL_ROLES) + ["chunk", "done", "permission"]
        ]
        built = point.build_transcript(rows)
        assert built.chars_today_eligible == 5 * len(RECALL_ROLES)

    def test_a_role_in_neither_set_is_in_neither_arm(self):
        # A ``chunk`` row is unflushed streaming text the durable rows already hold:
        # the replay does not keep it, so neither arm counts it, and adding it to the
        # denominator alone would understate the share instead.
        rows = [{"role": "user", "content": "u" * 10}, {"role": "chunk", "content": "c" * 900}]
        built = point.build_transcript(rows)
        assert built.chars_today_eligible == 10
        assert built.chars_all == 10


class TestTheOverflowIsCounted:
    """A walk that hit :data:`MAX_CALLS` describes a PREFIX, and the count says so.

    A COUNT rather than a flag, and published rather than withheld: "23 of 61 (+140
    not scored)" is a true sentence about a capped session, where a bare "23 of 61"
    is a wrong one and drawing nothing hides a measurement that is accurate about
    everything it covers.
    """

    def test_the_count_is_zero_until_the_cap_is_reached(self, monkeypatch):
        monkeypatch.setattr(point, "MAX_CALLS", 4)
        assert point.build_transcript([tool_row(n) for n in range(4)]).calls_truncated == 0

    def test_it_counts_every_call_past_the_cap(self, monkeypatch):
        monkeypatch.setattr(point, "MAX_CALLS", 4)
        built = point.build_transcript([tool_row(n) for n in range(9)])
        assert len(built.calls) == 4
        assert built.calls_truncated == 5

    def test_the_cap_keeps_the_NEWEST_calls_in_chronological_order(self, monkeypatch):
        """The newest, not the oldest, and still in order.

        A forward walk that simply stops admitting keeps the OLDEST calls -- the wrong
        end: the newest are what the agent is still working from, they are what the
        pinned tail is chosen for, and they are what the docs promise.
        """
        monkeypatch.setattr(point, "MAX_CALLS", 3)
        rows = [tool_row(n, inp=f"cmd-{n}") for n in range(9)]
        built = point.build_transcript(rows)
        assert [call.raw_input for call in built.calls] == ["cmd-6", "cmd-7", "cmd-8"]
        assert built.calls_truncated == 6

    def test_the_admitted_calls_are_the_ones_measured(self, monkeypatch):
        # The arms must describe the admitted set, not the dropped one: the dropped
        # calls' characters are in neither, which is what keeps the two on one universe.
        monkeypatch.setattr(point, "MAX_CALLS", 2)
        rows = [tool_row(0, inp="x" * 900, out="y" * 900)] + [
            tool_row(n, inp="a" * 10, out="b" * 10) for n in (1, 2)
        ]
        built = point.build_transcript(rows)
        assert built.calls_truncated == 1
        assert built.chars_all == 40

    def test_it_travels_on_the_counts(self, monkeypatch):
        monkeypatch.setattr(point, "MAX_CALLS", 3)
        built = point.build_transcript([tool_row(n) for n in range(20)])
        for call in built.calls:
            call.pinned = False
        decisions = {call.question_id: (point.OPTION_BOTH, 0.5) for call in built.calls}
        assert point.tally(built, decisions)["calls_truncated"] == 17

    def test_a_dropped_row_is_in_neither_arm(self, monkeypatch):
        # Left out of BOTH, so the two still describe one universe: a denominator that
        # kept the dropped call while the numerator could not would understate the
        # share. The COUNT is what says the universe is smaller than the session.
        #
        # The cap keeps the NEWEST call, so the 1000-character one is what is measured
        # and the 20-character one is what goes -- the assertion names the admitted
        # call's own size rather than a number that would also hold if the cap kept the
        # wrong end.
        monkeypatch.setattr(point, "MAX_CALLS", 1)
        built = point.build_transcript(
            [tool_row(0, inp="a" * 10, out="b" * 10), tool_row(1, inp="c" * 500, out="d" * 500)]
        )
        assert [call.input_chars for call in built.calls] == [500]
        assert built.chars_all == 1000
        assert built.calls_truncated == 1


class TestAttemptCorrelation:
    """A record belongs to the compaction that produced it, not to the next one.

    Reaching the defect needs two automatic compactions of ONE session inside the
    60-second TTL, with the first scoring straggling past its own notice and
    publishing before the second's read. Rare, and the mislabel it produced was a
    number on the wrong notice -- so the fix is a token rather than a wider TTL.
    """

    def test_every_token_is_new_and_increasing(self):
        minted = [
            point.begin_attempt("s"),
            point.begin_attempt("s"),
            point.begin_attempt("other"),
            point.begin_attempt("s"),
        ]
        assert minted == sorted(minted)
        assert len(set(minted)) == len(minted)

    def test_an_evicted_session_does_not_restart_its_numbering(self, monkeypatch):
        """The one way an equality comparison can ADMIT a record it should refuse.

        Per-session numbering restarts at 1 once the cap drops that session's entry, and
        a re-minted 1 equals the 1 a straggler minted before the eviction is still
        carrying -- so its record would be admitted onto a notice already drawn. A
        process-wide source cannot produce the collision.
        """
        monkeypatch.setattr(point, "MAX_PENDING_RECORDS", 2)
        straggler = point.begin_attempt("s")
        point.begin_attempt("a")
        point.begin_attempt("b")
        assert "s" not in point._attempts
        revived = point.begin_attempt("s")
        assert revived != straggler and revived > straggler
        # And the straggler still cannot publish against the revived token's session.
        point.publish_record("s", {"turn_id": "stale"}, straggler)
        assert point.take_record("s") is None

    def test_minting_retires_whatever_the_previous_attempt_left(self):
        first = point.begin_attempt("s")
        point.publish_record("s", {"turn_id": "a"}, first)
        point.begin_attempt("s")
        # The first compaction's notice has already been drawn; its leftover must not
        # be claimable by the one now in flight.
        assert point.take_record("s") is None

    def test_a_superseded_run_publishes_nothing(self):
        first = point.begin_attempt("s")
        second = point.begin_attempt("s")
        # The straggler publishes AFTER the newer compaction began.
        point.publish_record("s", {"turn_id": "stale"}, first)
        assert point.take_record("s") is None
        point.publish_record("s", {"turn_id": "fresh"}, second)
        assert point.take_record("s") == {"turn_id": "fresh"}

    def test_an_evicted_counter_is_a_MISMATCH_and_never_a_match(self, monkeypatch):
        """A missing counter must refuse, because the token may only ever refuse.

        The cap evicts the oldest counters, so a long-lived gateway loses them for keys
        that are still live. Reading an absent counter as "matches" would let a
        straggler's record be admitted on the strength of a value nobody holds -- the
        token authorizing a publish, which is exactly what it must not be able to do.
        """
        monkeypatch.setattr(point, "MAX_PENDING_RECORDS", 2)
        token = point.begin_attempt("s")
        # Two further sessions evict ``s``'s counter under the cap.
        point.begin_attempt("a")
        point.begin_attempt("b")
        assert "s" not in point._attempts
        point.publish_record("s", {"turn_id": "stale"}, token)
        assert point.take_record("s") is None

    def test_a_tokenless_publish_still_stores(self):
        # ``0`` means "not correlating", which is what a test or a future single-shot
        # caller passes; it must not silently drop.
        point.publish_record("s", {"turn_id": "t"})
        assert point.take_record("s") == {"turn_id": "t"}

    def test_the_token_table_is_bounded(self, monkeypatch):
        monkeypatch.setattr(point, "MAX_PENDING_RECORDS", 3)
        for n in range(12):
            point.begin_attempt(f"s{n}")
        assert len(point._attempts) == 3

    def test_a_truncated_run_publishes_its_record_with_the_overflow_count(self, monkeypatch):
        # PUBLISHED, because the line qualifies itself. The count is what a reader
        # needs in order not to take the total for the session's total.
        #
        # The cap is 6 rather than 3 deliberately: it keeps the NEWEST calls, and the
        # newest three tool rows of this transcript are inside the pinned tail, so a cap
        # of 3 would admit only pinned calls, ask nothing, and return before writing
        # anything. 6 admits three pinned and three askable.
        monkeypatch.setattr(point, "MAX_CALLS", 6)
        monkeypatch.setattr(point.core, "is_enabled", lambda *_a, **_kw: True)
        monkeypatch.setattr(point, "wait_budget", lambda: 30.0)
        monkeypatch.setattr(point, "read_rows", lambda _k, _before="": transcript(calls=20))
        written: list[dict] = []
        monkeypatch.setattr(point._log, "append", lambda row: written.append(row) or True)

        async def _decide(_p, _state, questions, **_kw):
            return {q.id: Answer(id=q.id, value=point.OPTION_BOTH, p=0.8) for q in questions}

        monkeypatch.setattr(point.core, "decide", _decide)
        record = asyncio.run(point.score_compaction("sess", point.begin_attempt("sess")))
        assert record is not None
        assert record["total_calls"] == 6
        assert record["calls_truncated"] == 14
        assert len(written) == 1
        assert point.take_record("sess") == record


class TestOneCallIsOneCall:
    """Tool ROWS are folded by ``tool_call_id`` before anything is counted.

    An interactive approval appends a second ``role="tool"`` row for the same call --
    ``chat_runner``'s approved/rejected row, carrying the same id. Counting rows made
    ``total_calls`` inflated, sent the same input twice in the state, asked two
    questions about one call, and double-counted its characters in the denominator.
    """

    def test_a_dispatch_and_its_approval_row_are_one_call(self):
        built = point.build_transcript(
            [
                {"role": "user", "content": "go"},
                tool_row(0, inp="ls -la", out="a" * 40),
                approval_row(0),
            ]
        )
        assert len(built.calls) == 1
        assert built.calls[0].raw_input == "ls -la"
        assert built.calls[0].result_chars == 40

    def test_the_call_is_counted_once_in_the_denominator(self):
        # The duplicate row carries the same id and empty fields, but an earlier build
        # added its neighbour's lengths a second time whenever the result handler had
        # written the output onto both.
        rows = [
            {"role": "user", "content": "u" * 10},
            tool_row(0, inp="i" * 100, out="o" * 200),
            {
                **approval_row(0),
                "meta": {"tool_call_id": "tc0", "input": "i" * 100, "output": "o" * 200},
            },
        ]
        built = point.build_transcript(rows)
        assert len(built.calls) == 1
        assert built.chars_all == 10 + 100 + 200

    def test_only_one_question_is_asked_about_it(self):
        rows = [{"role": "user", "content": "go"}]
        for n in range(9):
            rows.append(tool_row(n))
            rows.append(approval_row(n))
            rows.append({"role": "assistant", "content": f"step {n}"})
        built = point.build_transcript(rows)
        assert len(built.calls) == 9
        asked = [q.id for q in point.questions_for(built.calls)]
        assert len(asked) == len(set(asked))

    def test_a_rejected_call_still_folds(self):
        built = point.build_transcript(
            [tool_row(0, inp="rm -rf /"), approval_row(0, approved=False)]
        )
        assert len(built.calls) == 1
        assert built.calls[0].raw_input == "rm -rf /"

    def test_a_row_naming_no_call_is_its_own_entry(self):
        # A hook-blocked row carries no meta at all. Two of them cannot be shown to be
        # the same call, so folding them would UNDERcount.
        rows = [
            {"role": "tool", "content": "\U0001f6ab first (hook blocked)"},
            {"role": "tool", "content": "\U0001f6ab second (hook blocked)"},
        ]
        assert len(point.build_transcript(rows).calls) == 2

    def test_the_first_non_empty_field_wins(self):
        # The dispatch row carries the input; the result handler writes the output onto
        # whichever row it matched, which may be either.
        rows = [
            {
                "role": "tool",
                "content": "\U0001f527 bash",
                "meta": {"tool_call_id": "x", "input": "cmd"},
            },
            {
                "role": "tool",
                "content": "\u2705 bash",
                "meta": {"tool_call_id": "x", "output": "out"},
            },
        ]
        built = point.build_transcript(rows)
        assert len(built.calls) == 1
        assert built.calls[0].raw_input == "cmd"
        assert built.calls[0].result_chars == 3

    def test_pinning_is_the_OR_of_the_rows(self):
        # A call whose approval landed inside the newest rows is one the agent is still
        # working through, even if its dispatch row is older.
        rows = [tool_row(n) for n in range(8)] + [approval_row(0)]
        built = point.build_transcript(rows)
        by_id = {call.raw_input: call for call in built.calls}
        assert len(built.calls) == 8
        assert by_id["ls"].pinned is True

    def test_the_cap_counts_CALLS_not_rows(self, monkeypatch):
        # Three rows per call, so a row-based cap would admit a third of the calls it
        # should.
        monkeypatch.setattr(point, "MAX_CALLS", 3)
        rows: list[dict] = []
        for n in range(6):
            rows.append(tool_row(n, inp=f"cmd-{n}"))
            rows.append(approval_row(n))
        built = point.build_transcript(rows)
        assert [call.raw_input for call in built.calls] == ["cmd-3", "cmd-4", "cmd-5"]
        assert built.calls_truncated == 3


class TestRetentionIsBounded:
    """The count caps bound how MANY rows and calls are held, not how big one is.

    One pathological row -- a file read, a base64 blob -- was retained whole and then
    re-redacted by ``build_state`` once per rung of the ladder, so the bytes a single
    transcript cost were unbounded by this module. Each retained field is clipped; every
    LENGTH is taken before the clip, so no tally and neither character arm moves.
    """

    def test_a_huge_message_is_retained_clipped_and_counted_whole(self, monkeypatch):
        monkeypatch.setattr(point, "MAX_FIELD_CHARS", 50)
        content = "x" * 5000
        transcript = point.build_transcript([{"role": "user", "content": content}])
        assert transcript.messages[0]["text"] == "x" * 50
        # The denominator is the transcript's own size, not what was kept of it.
        assert transcript.chars_all == 5000
        assert transcript.chars_today_eligible == 5000

    def test_a_huge_tool_input_is_retained_clipped_and_counted_whole(self, monkeypatch):
        monkeypatch.setattr(point, "MAX_FIELD_CHARS", 50)
        rows = [tool_row(0, inp="y" * 9000, out="z" * 7000)]
        transcript = point.build_transcript(rows)
        call = transcript.calls[0]
        assert call.raw_input == "y" * 50
        # Both lengths are the real ones, which is what the arms and the placeholder use.
        assert call.input_chars == 9000
        assert call.result_chars == 7000
        assert transcript.chars_all == 9000 + 7000
        assert point.result_placeholder(call.result_chars) == "ok, 7000 chars (omitted)"

    def test_the_clip_holds_through_every_rung_of_the_ladder(self, monkeypatch):
        # The bound is on RETENTION, so a state built at any stage can only ever carry
        # what was retained -- there is no rung that reaches past it.
        monkeypatch.setattr(point, "MAX_FIELD_CHARS", 30)
        rows = [
            {"role": "user", "content": "u" * 4000},
            tool_row(0, inp="i" * 4000, out="o" * 4000),
        ]
        transcript = point.build_transcript(rows)
        for stage in point.STAGES:
            state = point.build_state(transcript, stage)
            for row in state["messages"]:
                assert len(row["text"]) <= 30
            for entry in state["calls"]:
                assert len(entry["input"]) <= 30

    def test_a_secret_straddling_the_admission_bound_is_redacted(self, monkeypatch):
        """The bound and the redaction cannot be separated.

        A slice at admission cut a credential spanning the bound into a fragment neither
        redactor matches -- and the fragment is what would travel, because the stage's own
        clip only ever sees what admission kept. So the admission bound is redact-then-clip
        like every other clip in this module.
        """
        monkeypatch.setattr(point, "MAX_FIELD_CHARS", len("filler") + 6)
        # The secret starts at offset 6 and the bound falls 6 characters into it.
        straddling = "filler" + SECRET
        built = point.build_transcript([{"role": "user", "content": straddling}])
        kept = built.messages[0]["text"]
        assert SECRET not in kept
        assert SECRET[:6] not in kept, "a fragment of the key is still a fragment of the key"
        # And the same on a tool input, which is the field a credential usually rides.
        built = point.build_transcript(transcript(calls=1, inp=straddling))
        assert SECRET[:6] not in built.calls[0].raw_input

    def test_the_admission_bound_still_holds_after_redaction(self, monkeypatch):
        # Redaction can LENGTHEN text -- a placeholder is longer than most secrets -- so
        # the clip has to run after it, not instead of it.
        monkeypatch.setattr(point, "MAX_FIELD_CHARS", 24)
        built = point.build_transcript([{"role": "user", "content": SECRET * 8}])
        assert len(built.messages[0]["text"]) <= 24

    def test_the_bound_is_generous_for_a_tool_input_at_every_rung(self):
        # Where the bound must cost nothing: every rung clips a tool input, so the
        # retained copy is already longer than any rung asks for.
        assert point.MAX_FIELD_CHARS > max(stage.input_chars for stage in point.STAGES)

    def test_the_bound_makes_the_whole_transcript_finite(self):
        # The point of the bound. Rows times the per-field cap is the ceiling on what one
        # transcript can hold, and it has to be a size a gateway survives.
        assert point.MAX_ROWS * point.MAX_FIELD_CHARS <= 32 * 1024 * 1024

    def test_a_folded_call_keeps_the_first_non_empty_input_and_its_true_length(self, monkeypatch):
        # The approval path appends a second row for one call. Folding must not lose the
        # real length behind the clipped copy.
        monkeypatch.setattr(point, "MAX_FIELD_CHARS", 10)
        first = tool_row(0, inp="", out="")
        second = tool_row(0, inp="a" * 800, out="b" * 400)
        transcript = point.build_transcript([first, second])
        assert len(transcript.calls) == 1
        assert transcript.calls[0].raw_input == "a" * 10
        assert transcript.calls[0].input_chars == 800
        assert transcript.calls[0].result_chars == 400


class TestTheBoundaryIsTheDecisionMoment:
    """The scoring describes the transcript the compaction was ABOUT.

    ``score_compaction`` runs beside the compaction, so its own read can land after
    ``provider.compact()`` has appended rows -- the summary it produced, a recycle
    notice. Those rows are about the compaction rather than part of what it compacted,
    and scoring them would describe a transcript that did not exist when the decision
    was made. The caller captures the boundary synchronously before the task exists.
    """

    def _stamped(self, when: str, **kw) -> dict:
        row = dict(kw)
        row["ts"] = when
        return row

    def test_a_row_appended_after_the_fire_is_dropped(self, monkeypatch):
        fired = "2026-09-21T10:00:00+00:00"
        rows = [
            self._stamped("2026-09-21T09:59:00+00:00", role="user", content="before"),
            {
                **tool_row(0, inp="pre", out="ok"),
                "ts": "2026-09-21T09:59:30+00:00",
            },
            # What the compaction itself wrote, a moment after the fire.
            self._stamped(
                "2026-09-21T10:00:05+00:00",
                role="assistant",
                content="\u2705 Conversation compacted: Goal / Status",
            ),
            {
                **tool_row(1, inp="post", out="ok"),
                "ts": "2026-09-21T10:00:06+00:00",
            },
        ]
        monkeypatch.setattr(
            "kiro_crew.history.ConversationLog",
            lambda *_a, **_kw: type("L", (), {"read_messages": lambda _s, _k: rows})(),
        )
        kept = point.read_rows("sess", fired)
        contents = [r.get("content", "") for r in kept]
        assert "before" in contents
        assert not any("Conversation compacted" in c for c in contents)
        assert [r["meta"]["input"] for r in kept if r.get("role") == "tool"] == ["pre"]

    def test_without_a_boundary_every_row_is_read(self, monkeypatch):
        # The filter is the CALLER's instruction, not a default: a caller with no
        # boundary (a test, a future single-shot reader) gets the whole transcript.
        rows = [
            self._stamped("2026-09-21T09:59:00+00:00", role="user", content="a"),
            self._stamped("2026-09-21T10:00:05+00:00", role="user", content="b"),
        ]
        monkeypatch.setattr(
            "kiro_crew.history.ConversationLog",
            lambda *_a, **_kw: type("L", (), {"read_messages": lambda _s, _k: rows})(),
        )
        assert len(point.read_rows("sess")) == 2

    def test_a_row_with_no_timestamp_is_kept(self):
        # Exclude only what is PROVABLY newer. A row with no stamp is a legacy line, not
        # something a compaction appended a moment ago.
        assert point._is_after({"role": "user"}, "2026-09-21T10:00:00+00:00") is False

    def test_an_unparseable_timestamp_is_kept(self):
        assert point._is_after({"ts": "not a date"}, "2026-09-21T10:00:00+00:00") is False
        assert point._is_after({"ts": ""}, "2026-09-21T10:00:00+00:00") is False

    def test_the_boundary_itself_is_inclusive(self):
        edge = "2026-09-21T10:00:00+00:00"
        assert point._is_after({"ts": edge}, edge) is False
        assert point._is_after({"ts": "2026-09-21T10:00:00.001+00:00"}, edge) is True

    def test_the_row_cap_is_applied_after_the_boundary(self, monkeypatch):
        # Otherwise a burst of appended rows pushes real ones out of the window before
        # the filter ever sees them.
        monkeypatch.setattr(point, "MAX_ROWS", 3)
        rows = [
            self._stamped(f"2026-09-21T09:5{n}:00+00:00", role="user", content=f"old{n}")
            for n in range(4)
        ] + [
            self._stamped(f"2026-09-21T10:00:0{n}+00:00", role="user", content=f"new{n}")
            for n in range(5)
        ]
        monkeypatch.setattr(
            "kiro_crew.history.ConversationLog",
            lambda *_a, **_kw: type("L", (), {"read_messages": lambda _s, _k: rows})(),
        )
        # Strictly before the burst: the boundary itself is INCLUSIVE, so a row sharing
        # that instant is kept -- which is the right rule and would make this case
        # measure the cap against four rows instead of three.
        kept = point.read_rows("sess", "2026-09-21T09:59:59+00:00")
        assert [r["content"] for r in kept] == ["old1", "old2", "old3"]
