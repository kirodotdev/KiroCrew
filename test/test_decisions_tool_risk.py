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
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew.decisions import gate
from kiro_crew.decisions import log as _log
from kiro_crew.decisions.points import skills_select as sel
from kiro_crew.decisions.points import tool_risk as tr
from kiro_crew.decisions.types import Answer


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A private ``config_dir`` so the log writes under the test's own tree."""
    monkeypatch.setattr(_log, "log_dir", lambda: tmp_path / "decisions")
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
        with _answering(tr.TIER_CAUTION):
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


# ── the point's own identity ──────────────────────────────────────────────────


class TestThePointIsShipped:
    def test_the_gate_admits_the_name(self):
        assert tr.POINT in gate.DECISION_POINT_NAMES

    def test_safe_is_not_a_flagged_tier(self):
        assert tr.TIER_SAFE not in tr.FLAGGED_TIERS
        assert set(tr.FLAGGED_TIERS) == {tr.TIER_CAUTION, tr.TIER_RISKY}
        assert tr.TIERS == (tr.TIER_SAFE, tr.TIER_CAUTION, tr.TIER_RISKY)

    def test_the_module_imports_no_dashboard_or_permission_code(self):
        """A point may not reach the approval path, even to read it.

        The caller passes the mode in as ``policy``. Importing the dashboard here
        would put an annotation one refactor away from being able to answer a
        permission request.
        """
        source = __import__("pathlib").Path(tr.__file__).read_text(encoding="utf-8")
        for forbidden in ("dashboard", "approve_tool", "reject_tool", "chat_runner"):
            assert forbidden not in source, forbidden
