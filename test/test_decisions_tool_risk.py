"""``tool.risk``: what it sends, what it records, and everything it refuses.

The point is an ANNOTATION, so almost every property here is about NOT doing
something: not sending a credential, not badging a ``safe`` answer, not calling
the oracle more than once per tool call or more than :data:`MAX_CALLS_PER_TURN`
times per turn, not letting a failure reach the caller. The one positive claim is
that a flagged answer returns the row that was written, so the badge on the card
and the line in the log cannot become two descriptions of one call.

``decide`` is patched at the module the point calls it through, so these drive the
point's own logic rather than the gate's -- the gate has its own suite. The two
tests that need the REAL gate (the scrub, and the point name being admitted) say
so and use it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew.decisions import gate
from kiro_crew.decisions import log as _log
from kiro_crew.decisions.points import skills_select as sel
from kiro_crew.decisions.points import tool_risk as tr
from kiro_crew.decisions.types import Answer

#: The append ceiling these tests run under, in place of the production
#: :data:`tr.LOG_BUDGET_SECS` (0.05 s). The tests here assert on the RECORD, and a
#: record is refused whenever the off-loop append does not finish inside that
#: budget -- so with the production value every one of them also asserts that a
#: new-file ``CreateFile`` plus lock plus write beats 50 ms on the host. It does
#: not on the Windows shard: the same tests came in ``None`` on 12 unrelated heads
#: in two days (0.4 ms measured on a warm Linux host; reproduced here by pricing
#: ``_log.append`` at 60 ms, which fails 8 of them every run). Raised, not removed:
#: a writer that genuinely wedges still returns ``None`` and fails the assertion
#: by name, 20 s in -- well under the suite's ``--timeout=120`` so it is a
#: readable failure rather than a killed worker. The budget's OWN contract is
#: pinned by ``TestRefusals.test_an_outcome_row_that_outlives_its_write_budget_...``
#: with the budget set to 0, which reaches the branch with no clock at all.
GENEROUS_LOG_BUDGET_SECS = 20.0


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A private ``config_dir`` so the log writes under the test's own tree.

    Also lifts the append budget to :data:`GENEROUS_LOG_BUDGET_SECS`: the row on
    disk is what these tests read back, so the host's write latency must not be
    able to decide the verdict.
    """
    monkeypatch.setattr(_log, "log_dir", lambda: tmp_path / "decisions")
    monkeypatch.setattr(tr, "LOG_BUDGET_SECS", GENEROUS_LOG_BUDGET_SECS)
    return tmp_path


def _rows(home) -> list[dict[str, Any]]:
    directory = home / "decisions"
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("decisions-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _answers(tier: str, p: float = 0.88) -> dict[str, Answer]:
    return {tr.QUESTION_ID: Answer(id=tr.QUESTION_ID, value=tier, p=p)}


def _answering(tier: str, p: float = 0.88, *, seen: list[dict] | None = None):
    """Patch ``core.decide`` so it answers *tier*, recording each call's arguments."""

    async def _decide(point, state, questions, *, session_key=None, extra=None, **_kw):
        if seen is not None:
            seen.append(
                {
                    "point": point,
                    "state": state,
                    "questions": questions,
                    "session_key": session_key,
                    "extra": extra,
                }
            )
        return _answers(tier, p)

    return patch.object(tr.core, "decide", _decide)


#: Repo root, for the spec gate below: this file lives at ``test/`` beneath it.
_REPO_ROOT = Path(__file__).resolve().parents[1]

#: The module spec, which names the bar's constant, and the operator doc, which
#: prints only the number a reader sees on the card.
_SPEC = _REPO_ROOT / "docs" / "system-specs" / "modules" / "decisions.md"
_OPERATOR_DOC = _REPO_ROOT / "src" / "kiro_crew" / "docs" / "decisions.md"


# ── the record it returns ─────────────────────────────────────────────────────


class TestTheRecord:
    @pytest.mark.asyncio
    async def test_a_risky_answer_returns_the_row_that_was_written(self, home):
        with _answering(tr.TIER_RISKY, 0.91):
            record = await tr.risk_record(
                tool="bash", arguments="rm -rf /data", policy="trust", session_key="chat-1"
            )

        assert record is not None
        assert record["point"] == tr.POINT
        assert record["tier"] == tr.TIER_RISKY
        assert record["p"] == 0.91
        assert record["tool"] == "bash"
        assert record["policy"] == "trust"
        assert record["flagged"] is True
        assert record["turn_id"]
        # The row on disk IS the record, so the badge and the log cannot drift.
        outcome = [r for r in _rows(home) if r.get("tier")]
        assert outcome == [record]

    @pytest.mark.asyncio
    async def test_a_caution_answer_is_flagged_too(self, home):
        with _answering(tr.TIER_CAUTION, 0.95):
            record = await tr.risk_record(
                tool="fsWrite", arguments="{}", policy="yolo", session_key="chat-1"
            )

        assert record is not None
        assert record["tier"] == tr.TIER_CAUTION
        assert record["flagged"] is True

    @pytest.mark.asyncio
    async def test_a_safe_answer_is_recorded_and_not_badged(self, home):
        """The row is the observation; the badge is the flag. ``safe`` earns only one."""
        with _answering(tr.TIER_SAFE):
            record = await tr.risk_record(
                tool="readFile", arguments="{}", policy="trust", session_key="chat-1"
            )

        assert record is None, "a safe call leaves the tool card exactly as it was"
        outcome = [r for r in _rows(home) if r.get("tier")]
        assert len(outcome) == 1
        assert outcome[0]["tier"] == tr.TIER_SAFE
        assert outcome[0]["flagged"] is False

    @pytest.mark.asyncio
    async def test_a_row_that_was_not_written_returns_nothing(self, home):
        """A badge whose durable row was refused names a turn no verdict can reach."""
        with _answering(tr.TIER_RISKY), patch.object(_log, "append", lambda _row: False):
            record = await tr.risk_record(
                tool="bash", arguments="curl example.test", policy="trust", session_key="chat-1"
            )

        assert record is None

    @pytest.mark.asyncio
    async def test_latency_is_recorded_as_a_whole_number_of_milliseconds(self, home):
        with _answering(tr.TIER_RISKY):
            record = await tr.risk_record(
                tool="bash", arguments="x", policy="trust", session_key="chat-1"
            )

        assert record is not None
        assert isinstance(record["latency_ms"], int)
        assert record["latency_ms"] >= 0


# ── what leaves the machine ───────────────────────────────────────────────────


class TestTheRequest:
    @pytest.mark.asyncio
    async def test_the_state_carries_the_tool_its_arguments_and_the_message(self, home):
        seen: list[dict] = []
        with _answering(tr.TIER_RISKY, seen=seen):
            await tr.risk_record(
                tool="bash",
                arguments="aws s3 rb s3://bucket",
                message="clean up the bucket",
                policy="trust",
                session_key="chat-1",
            )

        assert len(seen) == 1
        assert seen[0]["point"] == tr.POINT
        assert seen[0]["state"] == {
            "tool": "bash",
            "arguments": "aws s3 rb s3://bucket",
            "message": "clean up the bucket",
        }

    @pytest.mark.asyncio
    async def test_the_message_is_omitted_when_there_is_none(self, home):
        seen: list[dict] = []
        with _answering(tr.TIER_SAFE, seen=seen):
            await tr.risk_record(tool="bash", arguments="ls", policy="trust", session_key="chat-1")

        assert "message" not in seen[0]["state"]

    @pytest.mark.asyncio
    async def test_the_one_question_offers_exactly_the_three_tiers(self, home):
        seen: list[dict] = []
        with _answering(tr.TIER_SAFE, seen=seen):
            await tr.risk_record(tool="bash", arguments="ls", policy="trust", session_key="chat-1")

        questions = seen[0]["questions"]
        assert len(questions) == 1
        assert questions[0].id == tr.QUESTION_ID
        assert questions[0].options == list(tr.TIERS)
        # Each tier is described, so the domain is not one a provider has to guess.
        for tier in tr.TIERS:
            assert tier in questions[0].prompt

    @pytest.mark.asyncio
    async def test_the_call_row_names_the_tool_the_policy_and_the_argument_size(self, home):
        seen: list[dict] = []
        with _answering(tr.TIER_RISKY, seen=seen):
            await tr.risk_record(
                tool="bash",
                arguments="rm -rf /data",
                policy="trust_scope",
                session_key="chat-1",
                calls_this_turn=3,
            )

        extra = seen[0]["extra"]
        assert extra["tool"] == "bash"
        assert extra["policy"] == "trust_scope"
        assert extra["arg_chars"] == len("rm -rf /data")
        assert extra["call_index"] == 3
        # One turn id, shared by the call row the gate writes and the outcome row.
        assert extra["turn_id"]

    @pytest.mark.asyncio
    async def test_arguments_are_clipped_to_the_bound(self, home):
        seen: list[dict] = []
        with _answering(tr.TIER_SAFE, seen=seen):
            await tr.risk_record(
                tool="bash",
                arguments="x" * (tr.MAX_ARGUMENT_CHARS * 3),
                message="y" * (tr.MAX_MESSAGE_CHARS * 3),
                policy="trust",
                session_key="chat-1",
            )

        assert len(seen[0]["state"]["arguments"]) == tr.MAX_ARGUMENT_CHARS
        assert len(seen[0]["state"]["message"]) == tr.MAX_MESSAGE_CHARS

    @pytest.mark.asyncio
    async def test_a_credential_in_the_arguments_is_replaced_not_refused(self, home):
        """A secret in a tool argument is ORDINARY, so the point must still fire.

        The gate REFUSES a state carrying a credential, which is right for a
        message: a secret there is a finding. An ``aws`` command is not, and a
        refusal would mean the seam never annotates the calls most worth
        annotating -- so the argument is redacted before the gate ever sees it.
        """
        seen: list[dict] = []
        with _answering(tr.TIER_RISKY, seen=seen):
            record = await tr.risk_record(
                tool="bash",
                arguments="aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE",
                policy="trust",
                session_key="chat-1",
            )

        assert record is not None, "the call is annotated rather than dropped"
        sent = seen[0]["state"]["arguments"]
        assert "AKIAIOSFODNN7EXAMPLE" not in sent
        assert "REDACTED" in sent

    @pytest.mark.asyncio
    async def test_the_redacted_state_gets_past_the_real_scrub(self, home):
        """End to end through ``gate.scrub_reason``, the code that actually decides."""
        state = tr.build_state(
            "bash", "aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE", "set it up"
        )

        assert gate.scrub_reason(state, tr.questions()) is None

    @pytest.mark.asyncio
    async def test_an_unredacted_credential_would_have_been_refused(self, home):
        """The counterfactual, so the redaction above is shown to be load-bearing."""
        raw = {
            "tool": "bash",
            "arguments": "aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE",
        }

        assert gate.scrub_reason(raw, tr.questions()) == gate.ERROR_SCRUBBED_CREDENTIAL

    @pytest.mark.asyncio
    async def test_a_failing_redactor_drops_the_field_rather_than_sending_it(self, home):
        def _boom(_text):
            raise RuntimeError("scanner down")

        with patch("kiro_crew.security.redact_credentials", _boom):
            assert tr.scrubbed("anything at all", 100) == ""


# ── every refusal ─────────────────────────────────────────────────────────────


class TestRefusals:
    @pytest.mark.asyncio
    async def test_a_refusing_gate_leaves_the_card_alone_and_writes_no_outcome(self, home):
        async def _none(*_a, **_kw):
            return None

        with patch.object(tr.core, "decide", _none):
            record = await tr.risk_record(
                tool="bash", arguments="ls", policy="trust", session_key="chat-1"
            )

        assert record is None
        assert [r for r in _rows(home) if r.get("tier")] == []

    @pytest.mark.asyncio
    async def test_an_answer_outside_the_tiers_is_unusable(self, home):
        async def _weird(*_a, **_kw):
            return {tr.QUESTION_ID: Answer(id=tr.QUESTION_ID, value="catastrophic", p=1.0)}

        with patch.object(tr.core, "decide", _weird):
            assert (
                await tr.risk_record(
                    tool="bash", arguments="ls", policy="trust", session_key="chat-1"
                )
                is None
            )

    @pytest.mark.asyncio
    async def test_a_raising_provider_cannot_reach_the_caller(self, home):
        async def _boom(*_a, **_kw):
            raise RuntimeError("provider exploded")

        with patch.object(tr.core, "decide", _boom):
            assert (
                await tr.risk_record(
                    tool="bash", arguments="ls", policy="trust", session_key="chat-1"
                )
                is None
            )

    @pytest.mark.asyncio
    async def test_a_call_that_outlives_the_callers_budget_shows_nothing(self, home):
        async def _slow(*_a, **_kw):
            await asyncio.sleep(5)
            return _answers(tr.TIER_RISKY)

        with (
            patch.object(tr, "wait_budget", lambda: 0.01),
            patch.object(tr.core, "decide", _slow),
        ):
            assert (
                await tr.risk_record(
                    tool="bash", arguments="ls", policy="trust", session_key="chat-1"
                )
                is None
            )

    @pytest.mark.asyncio
    async def test_a_broken_log_cannot_cost_the_call(self, home):
        def _boom(_row):
            raise RuntimeError("disk gone")

        with _answering(tr.TIER_RISKY), patch.object(_log, "append", _boom):
            assert (
                await tr.risk_record(
                    tool="bash", arguments="ls", policy="trust", session_key="chat-1"
                )
                is None
            )

    @pytest.mark.asyncio
    async def test_an_outcome_row_that_outlives_its_write_budget_earns_no_badge(self, home):
        """The badge must not outlive the row a verdict would be filed against.

        The budget is set to ZERO, not to a small number: ``wait_for`` expires
        before the append is even dispatched, so the branch is reached identically
        on every host and the test carries no clock. A small positive value would
        be a bet on the write losing a race it wins on a warm machine.
        """
        with _answering(tr.TIER_RISKY), patch.object(tr, "LOG_BUDGET_SECS", 0):
            record = await tr.risk_record(
                tool="bash", arguments="ls", policy="trust", session_key="chat-1"
            )

        assert record is None, "a row the caller stopped waiting for cannot back a badge"

    def test_read_answer_refuses_a_boolean_probability(self):
        """``True`` is not a probability, and ``isinstance(True, int)`` is True."""
        assert tr.read_answer({tr.QUESTION_ID: Answer(tr.QUESTION_ID, tr.TIER_RISKY, True)}) is None

    def test_read_answer_refuses_a_missing_or_misshapen_answer(self):
        assert tr.read_answer(None) is None
        assert tr.read_answer({}) is None
        assert tr.read_answer({tr.QUESTION_ID: "risky"}) is None
        assert tr.read_answer({tr.QUESTION_ID: Answer(tr.QUESTION_ID, 7, 0.5)}) is None


# ── the two bounds ────────────────────────────────────────────────────────────


class TestBudgets:
    @pytest.mark.asyncio
    async def test_one_oracle_call_per_tool_call(self, home):
        seen: list[dict] = []
        with _answering(tr.TIER_RISKY, seen=seen):
            await tr.risk_record(tool="bash", arguments="ls", policy="trust", session_key="chat-1")

        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_past_the_turn_cap_nothing_is_asked(self, home):
        seen: list[dict] = []
        with (
            _answering(tr.TIER_RISKY, seen=seen),
            patch.object(tr.core, "is_enabled", lambda *_a, **_kw: True),
        ):
            last = await tr.risk_record(
                tool="bash",
                arguments="ls",
                policy="trust",
                session_key="chat-1",
                calls_this_turn=tr.MAX_CALLS_PER_TURN,
            )
            over = await tr.risk_record(
                tool="bash",
                arguments="ls",
                policy="trust",
                session_key="chat-1",
                calls_this_turn=tr.MAX_CALLS_PER_TURN + 1,
            )

        assert last is not None, "the cap admits its own last call"
        assert over is None
        assert len(seen) == 1, "no provider call past the cap"

    @pytest.mark.asyncio
    async def test_the_call_that_crosses_the_cap_says_so_once(self, home):
        with (
            _answering(tr.TIER_RISKY),
            patch.object(tr.core, "is_enabled", lambda *_a, **_kw: True),
        ):
            for index in range(tr.MAX_CALLS_PER_TURN + 1, tr.MAX_CALLS_PER_TURN + 5):
                await tr.risk_record(
                    tool="bash",
                    arguments="ls",
                    policy="trust",
                    session_key="chat-1",
                    calls_this_turn=index,
                )

        capped = [r for r in _rows(home) if r.get("error") == tr.ERROR_TURN_CAP]
        assert len(capped) == 1, "one row per turn, not one per skipped call"
        assert capped[0]["calls"] == tr.MAX_CALLS_PER_TURN + 1

    @pytest.mark.asyncio
    async def test_an_unsampled_session_writes_no_cap_row_either(self, home):
        """The cap row is the one row not produced by ``decide``, so it asks the gate."""
        with patch.object(tr.core, "is_enabled", lambda *_a, **_kw: False):
            await tr.risk_record(
                tool="bash",
                arguments="ls",
                policy="trust",
                session_key="chat-1",
                calls_this_turn=tr.MAX_CALLS_PER_TURN + 1,
            )

        assert _rows(home) == []

    @pytest.mark.parametrize(
        "provider_secs, expected",
        [
            (0.0, tr.WAIT_MARGIN_SECS),
            (1.0, 1.0 + tr.WAIT_MARGIN_SECS),
            (3600.0, tr.MAX_WAIT_SECS),
            (float("inf"), tr.MIN_WAIT_SECS),
            (float("nan"), tr.MIN_WAIT_SECS),
            (-5.0, tr.MIN_WAIT_SECS),
        ],
    )
    def test_the_wait_budget_is_clamped(self, monkeypatch, provider_secs, expected):
        monkeypatch.setattr(tr.core, "timeout_secs", lambda *_a, **_kw: provider_secs)
        assert tr.wait_budget() == expected

    def test_an_unreadable_provider_budget_reads_as_the_floor(self, monkeypatch):
        def _boom(*_a, **_kw):
            raise RuntimeError("no config")

        monkeypatch.setattr(tr.core, "timeout_secs", _boom)
        assert tr.wait_budget() == tr.MIN_WAIT_SECS

    def test_every_point_in_this_package_waits_in_one_shape(self):
        """Held equal to ``skills.select``'s, so a point cannot invent its own ceiling."""
        assert tr.WAIT_MARGIN_SECS == sel.WAIT_MARGIN_SECS
        assert tr.MIN_WAIT_SECS == sel.MIN_WAIT_SECS
        assert tr.MAX_WAIT_SECS == sel.MAX_WAIT_SECS


# ── the confidence a `risky` answer needs ─────────────────────────────────────


class TestTheConfidenceThresholds:
    """Neither flagged tier is printed until it is believed.

    Two things these tests hold, and they pull in opposite directions. A badge must
    not fire on a coin-flip answer -- that is what costs every other badge its
    meaning. And a bar must not be so high that a force-push goes unbadged, which is
    why each value is pinned inside the window it was measured in rather than merely
    asserted to exist.

    The two bars are two INDEPENDENTLY MOVABLE constants, currently measured at
    the same value (0.80): they are read off different populations, so a
    reading that moves one must be able to leave the other alone.
    """

    @pytest.mark.parametrize(
        "tier, p, badged",
        [
            (tr.TIER_RISKY, 0.0, False),
            (tr.TIER_RISKY, 0.42, False),
            (tr.TIER_RISKY, 0.79, False),
            # The bound is INCLUSIVE, the same direction ``memory_recall``'s is.
            (tr.TIER_RISKY, tr.RISKY_CONFIDENCE_THRESHOLD, True),
            (tr.TIER_RISKY, 0.98, True),
            (tr.TIER_RISKY, 1.0, True),
            # ``caution`` has its own bar, currently the same value -- also inclusive.
            (tr.TIER_CAUTION, 0.0, False),
            (tr.TIER_CAUTION, 0.34, False),
            (tr.TIER_CAUTION, 0.79, False),
            (tr.TIER_CAUTION, tr.CAUTION_CONFIDENCE_THRESHOLD, True),
            (tr.TIER_CAUTION, 1.0, True),
            # ``safe`` is never a badge at any confidence at all.
            (tr.TIER_SAFE, 0.0, False),
            (tr.TIER_SAFE, 1.0, False),
        ],
    )
    def test_which_answers_reach_the_card(self, tier, p, badged):
        assert tr.earns_badge(tier, p) is badged

    @pytest.mark.asyncio
    async def test_a_low_confidence_risky_answer_draws_nothing(self, home):
        """The ordinary refusal: no badge, and the reader is told nothing."""
        with _answering(tr.TIER_RISKY, 0.57):
            record = await tr.risk_record(
                tool="bash", arguments="git push", policy="trust", session_key="chat-1"
            )

        assert record is None, "an unconvinced risky must leave the tool card alone"

    @pytest.mark.asyncio
    async def test_but_its_row_is_still_written_and_still_says_risky(self, home):
        """The suppressed answers are the evidence a later bar is read from.

        A refusal that dropped the row would make the bar unmeasurable from this
        build's own logs, which is the one thing the value must not cost.
        """
        with _answering(tr.TIER_RISKY, 0.57):
            await tr.risk_record(
                tool="bash", arguments="git push", policy="trust", session_key="chat-1"
            )

        outcome = [r for r in _rows(home) if r.get("tier")]
        assert len(outcome) == 1
        assert outcome[0]["tier"] == tr.TIER_RISKY
        assert outcome[0]["p"] == 0.57
        # ``flagged`` is what the reader was SHOWN, so it follows the badge and not
        # the tier -- otherwise the row and the card describe one call two ways.
        assert outcome[0]["flagged"] is False

    @pytest.mark.asyncio
    async def test_a_confident_risky_answer_is_badged_and_flagged(self, home):
        with _answering(tr.TIER_RISKY, tr.RISKY_CONFIDENCE_THRESHOLD):
            record = await tr.risk_record(
                tool="bash", arguments="git push --force", policy="trust", session_key="chat-1"
            )

        assert record is not None
        assert record["tier"] == tr.TIER_RISKY
        assert record["flagged"] is True
        assert [r for r in _rows(home) if r.get("tier")] == [record]

    @pytest.mark.asyncio
    async def test_a_suppressed_caution_still_writes_its_row(self, home):
        """The suppressed ``caution`` rows are what a later bar is re-read from.

        Same rule the suppressed ``risky`` rows follow: the badge is refused, the
        observation is not, so the number stays measurable from this build's own log.
        """
        with _answering(tr.TIER_CAUTION, 0.52):
            record = await tr.risk_record(
                tool="fsWrite", arguments="{}", policy="trust", session_key="chat-1"
            )

        assert record is None
        outcome = [r for r in _rows(home) if r.get("tier")]
        assert len(outcome) == 1
        assert outcome[0]["tier"] == tr.TIER_CAUTION
        assert outcome[0]["p"] == 0.52
        assert outcome[0]["flagged"] is False

    @pytest.mark.asyncio
    async def test_a_confident_caution_answer_is_badged_and_flagged(self, home):
        with _answering(tr.TIER_CAUTION, tr.CAUTION_CONFIDENCE_THRESHOLD):
            record = await tr.risk_record(
                tool="fsWrite", arguments="{}", policy="trust", session_key="chat-1"
            )

        assert record is not None
        assert record["tier"] == tr.TIER_CAUTION
        assert record["flagged"] is True

    @pytest.mark.asyncio
    async def test_the_constant_is_what_decides(self, home, monkeypatch):
        """Revert-verify: move the number and the SAME answer changes side.

        Without this the two tests above would pass against a hard-coded literal,
        or against a predicate that ignores the constant entirely.
        """
        monkeypatch.setattr(tr, "RISKY_CONFIDENCE_THRESHOLD", 0.50)
        with _answering(tr.TIER_RISKY, 0.57):
            now_badged = await tr.risk_record(
                tool="bash", arguments="git push", policy="trust", session_key="chat-1"
            )
        assert now_badged is not None, "0.57 must badge once the bar is 0.50"

        monkeypatch.setattr(tr, "RISKY_CONFIDENCE_THRESHOLD", 0.99)
        with _answering(tr.TIER_RISKY, 0.98):
            now_hidden = await tr.risk_record(
                tool="bash", arguments="git push", policy="trust", session_key="chat-2"
            )
        assert now_hidden is None, "0.98 must be refused once the bar is 0.99"

    def test_the_value_sits_inside_the_window_it_was_measured_in(self):
        """A bound on both sides, because both failures are real.

        Under roughly 0.75 the measured answers include a queued monitor edit and a
        comment posted on a pull request -- calls that neither destroy data nor
        leave the workspace, so a badge on them is the flag crying wolf. At 0.90 the
        same reading drops eleven true ones, force-pushes and a credential fix among
        them. A later reading may move the number inside this window on new
        evidence; a value outside it contradicts the evidence there is.
        """
        assert 0.75 <= tr.RISKY_CONFIDENCE_THRESHOLD <= 0.85

    @pytest.mark.asyncio
    async def test_the_caution_constant_is_what_decides(self, home, monkeypatch):
        """Revert-verify for the second bar, and it must move ALONE.

        The ``risky`` bar is left untouched here on purpose: a shared constant, or a
        lookup table built once at import, would pass the parametrised cases above
        and still fail this -- which is the drift two separately named bars exist to
        prevent.
        """
        monkeypatch.setattr(tr, "CAUTION_CONFIDENCE_THRESHOLD", 0.50)
        with _answering(tr.TIER_CAUTION, 0.57):
            now_badged = await tr.risk_record(
                tool="fsWrite", arguments="{}", policy="trust", session_key="chat-1"
            )
        assert now_badged is not None, "0.57 must badge once the caution bar is 0.50"

        monkeypatch.setattr(tr, "CAUTION_CONFIDENCE_THRESHOLD", 0.99)
        with _answering(tr.TIER_CAUTION, 0.98):
            now_hidden = await tr.risk_record(
                tool="fsWrite", arguments="{}", policy="trust", session_key="chat-2"
            )
        assert now_hidden is None, "0.98 must be refused once the caution bar is 0.99"

    def test_they_are_probabilities_and_not_percentages(self):
        """``p`` is a 0..1 probability everywhere in this package, so the bars are too."""
        for bar in (tr.RISKY_CONFIDENCE_THRESHOLD, tr.CAUTION_CONFIDENCE_THRESHOLD):
            assert isinstance(bar, float)
            assert 0.0 < bar <= 1.0

    def test_the_docs_print_the_caution_bar_at_the_value_the_code_ships(self):
        """The prose and the constant cannot drift apart silently.

        The module spec names each bar's CONSTANT beside its value, so a reader
        re-deriving the bar has more than the badge to go on; the spec assertion is
        read against the constant rather than a literal, so the gate follows the
        code instead of pinning a second copy of it. The operator doc prints no
        bare number -- caution and risky are two independently movable bars and a
        shared numeric row would print the wrong one for whichever bar moves next --
        so its assertion pins the qualitative wording instead: risky still reads as
        confidence-gated, and caution still reads as confidence-gated AND carrying
        the file-write carve-out.
        """
        value = f"{tr.CAUTION_CONFIDENCE_THRESHOLD:.2f}"
        spec = _SPEC.read_text(encoding="utf-8")
        assert f"`CAUTION_CONFIDENCE_THRESHOLD` ({value})" in spec, (
            "the module spec must print the caution bar's constant at the value the " "code ships"
        )
        risky_value = f"{tr.RISKY_CONFIDENCE_THRESHOLD:.2f}"
        assert f"`RISKY_CONFIDENCE_THRESHOLD` ({risky_value})" in spec, (
            "the module spec must print the risky bar's constant at the value the " "code ships"
        )
        operator = _OPERATOR_DOC.read_text(encoding="utf-8")
        assert (
            "risky`, and Jev is sure" in operator
        ), "the operator doc must still gate the risky row on confidence, in words"
        assert "caution`, is sure, and the call is not a plain file write" in operator, (
            "the operator doc must still gate the caution row on confidence and the "
            "file-write carve-out, in words"
        )
        # The retired single-bar wording must actually be gone from the BADGE
        # table rows, not just outnumbered by the new ones -- scoped to those
        # rows rather than the whole file, since "or more" is ordinary prose
        # elsewhere on this page (and in its sibling docs) and would red the
        # gate on an unrelated sentence.
        badge_rows = [line for line in operator.splitlines() if "| Trusts the session" in line]
        assert badge_rows, "the operator doc must still carry the tool.risk badge rows"
        badge_text = "\n".join(badge_rows)
        assert "score of" not in badge_text, (
            "the badge rows must not carry the retired shared-bar wording "
            "('score of N.NN or more') now that caution and risky are separately movable bars"
        )
        assert (
            "or more" not in badge_text
        ), "the badge rows must not carry a retired numeric threshold phrase"


class TestTheCautionConfidenceThreshold:
    """``caution`` must be believed too, and a plain file write never draws it."""

    @pytest.mark.asyncio
    async def test_a_hesitant_caution_draws_nothing_but_keeps_its_row(self, home):
        with _answering(tr.TIER_CAUTION, 0.79):
            record = await tr.risk_record(
                tool="bash", arguments="mkdir build", policy="trust", session_key="chat-1"
            )

        assert record is None
        outcome = [r for r in _rows(home) if r.get("tier")]
        assert len(outcome) == 1
        assert outcome[0]["tier"] == tr.TIER_CAUTION
        assert outcome[0]["flagged"] is False

    @pytest.mark.asyncio
    async def test_a_confident_caution_is_badged(self, home):
        with _answering(tr.TIER_CAUTION, tr.CAUTION_CONFIDENCE_THRESHOLD):
            record = await tr.risk_record(
                tool="bash", arguments="mkdir build", policy="trust", session_key="chat-1"
            )

        assert record is not None
        assert record["flagged"] is True

    @pytest.mark.asyncio
    async def test_the_constant_is_what_decides(self, home, monkeypatch):
        monkeypatch.setattr(tr, "CAUTION_CONFIDENCE_THRESHOLD", 0.50)
        with _answering(tr.TIER_CAUTION, 0.57):
            now_badged = await tr.risk_record(
                tool="bash", arguments="mkdir b", policy="trust", session_key="chat-1"
            )
        assert now_badged is not None

        monkeypatch.setattr(tr, "CAUTION_CONFIDENCE_THRESHOLD", 0.99)
        with _answering(tr.TIER_CAUTION, 0.98):
            now_hidden = await tr.risk_record(
                tool="bash", arguments="mkdir b", policy="trust", session_key="chat-2"
            )
        assert now_hidden is None

    def test_the_value_sits_at_or_above_the_risky_bar_and_below_certainty(self):
        """Equal to ``risky``'s bar (both measured at 0.80), and below 1.0 so the
        word can still print."""
        assert tr.RISKY_CONFIDENCE_THRESHOLD <= tr.CAUTION_CONFIDENCE_THRESHOLD < 1.0
        assert isinstance(tr.CAUTION_CONFIDENCE_THRESHOLD, float)

    def test_the_value_sits_inside_the_window_it_was_measured_in(self):
        """A bound on both sides, the same shape ``risky``'s pin uses.

        This build's own measured window for ``caution`` is 0.75..0.85: below it
        the tier is dominated by calls that only read, and above 0.85 the same
        reading starts dropping ordinary rebases and amends -- the tier's own
        target population. A value outside this window contradicts that reading;
        a later reading may move the window, but a drive-by edit must not be able
        to widen it to ``0 < bar <= 1`` and still pass.
        """
        assert 0.75 <= tr.CAUTION_CONFIDENCE_THRESHOLD <= 0.85

    @pytest.mark.parametrize(
        "tier, p, badged",
        [
            (tr.TIER_CAUTION, 0.99, False),
            (tr.TIER_CAUTION, 1.0, False),
            # A risky write is still badged: outside the workspace is what the word names.
            (tr.TIER_RISKY, 0.95, True),
            (tr.TIER_RISKY, 0.79, False),
            (tr.TIER_SAFE, 1.0, False),
        ],
    )
    def test_a_file_write_never_draws_caution(self, tier, p, badged):
        assert tr.earns_badge(tier, p, file_write=True) is badged

    @pytest.mark.asyncio
    async def test_a_file_write_caution_keeps_its_tier_on_the_row(self, home):
        with _answering(tr.TIER_CAUTION, 0.99):
            record = await tr.risk_record(
                tool="Write File",
                arguments='{"path": "README.md"}',
                policy="trust",
                session_key="chat-1",
                tool_kind="edit",
            )

        assert record is None
        outcome = [r for r in _rows(home) if r.get("tier")]
        assert len(outcome) == 1
        assert outcome[0]["tier"] == tr.TIER_CAUTION
        assert outcome[0]["p"] == 0.99
        assert outcome[0]["flagged"] is False
        assert outcome[0]["file_write"] is True

    @pytest.mark.asyncio
    async def test_a_non_file_write_row_carries_file_write_false(self, home):
        """The write carve-out must be re-derivable from the log without the
        harness's own tool title, which the structured-kind test above pins as
        not the signal the point itself reads."""
        with _answering(tr.TIER_CAUTION, 0.99):
            await tr.risk_record(
                tool="bash",
                arguments='{"command": "mkdir build"}',
                policy="trust",
                session_key="chat-1",
                tool_kind="execute",
            )

        outcome = [r for r in _rows(home) if r.get("tier")]
        assert len(outcome) == 1
        assert outcome[0]["file_write"] is False

    @pytest.mark.asyncio
    async def test_a_file_write_risky_is_still_badged(self, home):
        with _answering(tr.TIER_RISKY, 0.95):
            record = await tr.risk_record(
                tool="Write File",
                arguments='{"path": "/home/u/.aws/credentials"}',
                policy="trust",
                session_key="chat-1",
                tool_kind="edit",
            )

        assert record is not None
        assert record["flagged"] is True

    @pytest.mark.asyncio
    async def test_only_the_structured_kind_marks_a_file_write(self, home):
        """The display title is not the signal: a caution titled "Write File" with
        another kind and no diff path is judged on its confidence alone."""
        with _answering(tr.TIER_CAUTION, 0.99):
            record = await tr.risk_record(
                tool="Write File",
                arguments="{}",
                policy="trust",
                session_key="chat-1",
                tool_kind="execute",
            )

        assert record is not None

    @pytest.mark.asyncio
    async def test_a_diff_path_alone_marks_a_file_write(self, home):
        """A real edit whose ACP ``kind`` is empty or ``read`` (agent-influenced,
        per ``is_edit_call``'s own docstring) still carries no caution badge once
        its tool_call frame cached a diff-block path -- the same routing predicate
        the hook edit gate and governance classification share."""
        with _answering(tr.TIER_CAUTION, 0.99):
            record = await tr.risk_record(
                tool="Write File",
                arguments='{"path": "README.md"}',
                policy="trust",
                session_key="chat-1",
                tool_kind="read",
                diff_path="README.md",
            )

        assert record is None

    @pytest.mark.asyncio
    async def test_an_edit_kind_with_no_diff_path_is_still_a_file_write(self, home):
        """``kind="edit"`` alone still routes -- the diff path is an OR, not a
        replacement for the ACP kind."""
        with _answering(tr.TIER_CAUTION, 0.99):
            record = await tr.risk_record(
                tool="Write File",
                arguments="{}",
                policy="trust",
                session_key="chat-1",
                tool_kind="edit",
                diff_path="",
            )

        assert record is None

    @pytest.mark.asyncio
    async def test_a_delete_is_not_the_workspace_edit_the_carve_out_describes(self, home):
        """``delete`` is on the gates' write-plane routing (``is_edit_call``); if
        the badge carve-out read that same predicate, a KAS ``delete_file`` would
        lose its ``caution`` badge on exactly the auto-approving sessions where
        the badge is the only signal. The carve-out's ground is a workspace edit
        "easy to put back"; a deletion is not, so it keeps the badge
        (``is_workspace_edit_call``)."""
        with _answering(tr.TIER_CAUTION, 0.99):
            record = await tr.risk_record(
                tool="Delete File",
                arguments='{"targetFile": "notes.md"}',
                policy="trust",
                session_key="chat-1",
                tool_kind="delete",
                diff_path="",
            )

        assert record is not None
        assert record["tier"] == tr.TIER_CAUTION


# ── the point's own identity ──────────────────────────────────────────────────


class TestThePointIsShipped:
    def test_the_gate_admits_the_name(self):
        assert tr.POINT in gate.DECISION_POINT_NAMES

    def test_safe_never_earns_a_badge_at_any_confidence(self):
        assert tr.TIERS == (tr.TIER_SAFE, tr.TIER_CAUTION, tr.TIER_RISKY)
        for p in (0.0, 0.5, tr.CAUTION_CONFIDENCE_THRESHOLD, tr.RISKY_CONFIDENCE_THRESHOLD, 1.0):
            assert tr.earns_badge(tr.TIER_SAFE, p) is False

    def test_a_flagged_tier_is_necessary_and_not_sufficient(self):
        """One reader for the whole question, so a caller cannot ask half of it.

        A surface that tested the tier alone would print a badge the point refused,
        which is the drift ``earns_badge`` exists to prevent: each flagged tier is
        refused under its OWN bar and admitted at it.
        """
        assert tr.earns_badge(tr.TIER_RISKY, tr.RISKY_CONFIDENCE_THRESHOLD - 0.01) is False
        assert tr.earns_badge(tr.TIER_CAUTION, tr.CAUTION_CONFIDENCE_THRESHOLD - 0.01) is False
        assert tr.earns_badge(tr.TIER_RISKY, tr.RISKY_CONFIDENCE_THRESHOLD) is True
        assert tr.earns_badge(tr.TIER_CAUTION, tr.CAUTION_CONFIDENCE_THRESHOLD) is True

    def test_the_module_imports_no_dashboard_or_permission_code(self):
        """A point may not reach the approval path, even to read it.

        The caller passes the mode in as ``policy``. Importing the dashboard here
        would put an annotation one refactor away from being able to answer a
        permission request.
        """
        source = __import__("pathlib").Path(tr.__file__).read_text(encoding="utf-8")
        for forbidden in ("dashboard", "approve_tool", "reject_tool", "chat_runner"):
            assert forbidden not in source, forbidden
