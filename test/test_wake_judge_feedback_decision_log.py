"""The judge tick's own record: whether it asked, and the decision-log rows it joins.

``answered`` separates the judge's verdicts from the loop's, and a verdict keeps its
join key only for a decision row that committed, even one committing after the
write budget; only a caller asking for a receipt waits past that budget. The
wake-label row is keyed by the verdict it judges and carries no reply and no
target, and the point's streak bound and the join key's length bound each match the
side they pair with. The shared verdict builder lives in
``wake_judge_feedback_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import types
from pathlib import Path

import pytest
from test_decisions_gate import _STALLED_APPEND_CEILING_SECS
from wake_judge_feedback_helpers import _verdict

from kiro_crew import autonudge_judge as judge
from kiro_crew import irq
from kiro_crew.autonudge import _MAX_QUIET_STREAK
from kiro_crew.config.sections import DecisionProviderConfig, DecisionsConfig, NudgeWakeConfig
from kiro_crew.decisions import gate as decisions_gate
from kiro_crew.decisions import log as decisions_log
from kiro_crew.decisions.points import nudge_wake as point


class TestJudgeTickReportsWhetherItAsked:
    """``answered`` separates the judge's own verdicts from the loop's."""

    def test_a_tick_with_no_new_evidence_did_not_ask(self):
        trace: dict = {}
        verdict = asyncio.run(point.judge_tick("watch it", evidence=None, trace=trace))
        assert verdict.outcome is irq.Outcome.QUIET
        assert trace["answered"] is False

    def test_a_tick_with_a_dropped_target_did_not_ask(self):
        trace: dict = {}
        verdict = asyncio.run(point.judge_tick("watch it", dropped=1, trace=trace))
        assert verdict.outcome is irq.Outcome.FALLBACK
        assert trace["answered"] is False

    def test_a_refused_decision_append_keeps_no_join_key(self, monkeypatch):
        class _Config:
            decisions = DecisionsConfig(nudge_wake=NudgeWakeConfig(provider="llm"))

        class _InvalidOracle:
            async def ask(self, *_args, **_kwargs):
                return None

        captured_receipt: dict = {}
        original_decide = decisions_gate.decide

        async def _decide(*args, receipt, **kwargs):
            answers = await original_decide(*args, receipt=receipt, **kwargs)
            captured_receipt.update(receipt)
            return answers

        monkeypatch.setattr(decisions_gate, "_snapshot", lambda: _Config())
        monkeypatch.setattr(decisions_gate, "_consented_for", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_capability_denied", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_oracle", lambda *_args, **_kwargs: _InvalidOracle())
        monkeypatch.setattr(decisions_log, "append", lambda _row, **_kwargs: False)
        monkeypatch.setattr(point.core, "decide", _decide)

        trace: dict = {}
        verdict = asyncio.run(
            point.judge_tick(
                "watch it",
                evidence=[
                    {
                        "source": "session:chat-1",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "new evidence",
                    }
                ],
                trace=trace,
            )
        )
        row = judge.verdict_entry(
            verdict,
            trace["evidence_items"],
            suppressed=False,
            answered=trace["answered"],
            verdict_id="v1" if trace["answered"] else "",
        )
        assert captured_receipt["row_written"] is False
        assert trace["answered"] is False
        assert "id" not in row

    def test_a_committed_row_keeps_the_join_key_when_sweep_outlives_budget(self, monkeypatch):
        class _Config:
            decisions = DecisionsConfig(nudge_wake=NudgeWakeConfig(provider="llm"))

        class _InvalidOracle:
            async def ask(self, *_args, **_kwargs):
                return None

        captured_receipt: dict = {}
        original_decide = decisions_gate.decide
        original_append = decisions_log.append
        sweep_release = threading.Event()
        append_returned = threading.Event()
        observed: dict = {}

        def _observed_append(row, **kwargs):
            try:
                return original_append(row, **kwargs)
            finally:
                append_returned.set()

        async def _decide(*args, receipt, **kwargs):
            try:
                answers = await original_decide(*args, receipt=receipt, **kwargs)
            finally:
                # The append returns only after its sweep, and the sweep is held until
                # here, so a decision that came back first took the over-budget path.
                observed["append_in_flight"] = not append_returned.is_set()
                sweep_release.set()
            captured_receipt.update(receipt)
            return answers

        monkeypatch.setattr(decisions_gate, "_snapshot", lambda: _Config())
        monkeypatch.setattr(decisions_gate, "_consented_for", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_capability_denied", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_oracle", lambda *_args, **_kwargs: _InvalidOracle())
        monkeypatch.setattr(decisions_gate, "_LOG_BUDGET_SECS", 0.01)
        # The real day-file write is what this case observes, and its duration belongs
        # to the runner. The wait returns the moment the commit signal fires, so the
        # grace is lifted to the gate suite's lost-append ceiling: the real append's
        # commit signal measured 257 ms at worst (2,000 appends under 64 CPU spinners
        # on 32 cores), about 40x inside it, and an append that never commits still
        # fails this case by name, well before the suite's --timeout=120. The 0.10 s
        # production grace is untouched in the gate.
        monkeypatch.setattr(decisions_gate, "_LOG_COMMIT_GRACE_SECS", _STALLED_APPEND_CEILING_SECS)
        # The sweep is held until the decision returns, so "the sweep outlives the write
        # budget" is a fact, not a 0.05 s sleep racing a 0.01 s budget. Its bound is
        # twice the grace, as the gate suite bounds its stalled append, so an append
        # whose commit signal is lost or late runs the grace out while the append is
        # still in its sweep, and the receipt stays unknown.
        monkeypatch.setattr(
            decisions_log,
            "sweep_expired",
            lambda: sweep_release.wait(2 * _STALLED_APPEND_CEILING_SECS),
        )
        monkeypatch.setattr(decisions_log, "append", _observed_append)
        monkeypatch.setattr(point.core, "decide", _decide)

        trace: dict = {}
        verdict = asyncio.run(
            point.judge_tick(
                "watch it",
                evidence=[
                    {
                        "source": "session:chat-1",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "new evidence",
                    }
                ],
                trace=trace,
            )
        )
        row = judge.verdict_entry(
            verdict,
            trace["evidence_items"],
            suppressed=False,
            answered=trace["answered"],
            verdict_id="v1" if trace["answered"] else "",
        )
        assert observed["append_in_flight"] is True
        assert captured_receipt["row_written"] is True
        assert trace["answered"] is True
        assert row["id"] == "v1"

    def test_the_join_key_case_holds_on_a_host_whose_write_outlasts_the_production_grace(
        self, monkeypatch
    ):
        # A slow runner, replayed without a clock: the day-file write is held until the
        # gate's grace wait has begun, and a grace up to the production one has
        # already run out by then. The case above must still see its commit. A
        # grace that runs out never releases the held write, so a regression fails
        # every time instead of racing the write against the gate's check.
        production_grace = decisions_gate._LOG_COMMIT_GRACE_SECS
        grace_started = threading.Event()

        class _SlowHostEvent(threading.Event):
            def wait(self, timeout=None):
                if timeout is None or self.is_set():
                    return super().wait(timeout)
                if timeout <= production_grace:
                    return False
                grace_started.set()
                return super().wait(timeout)

        real_append_line = decisions_log.append_line

        def _held_append_line(*args, **kwargs):
            assert grace_started.wait(
                _STALLED_APPEND_CEILING_SECS
            ), "the gate never started its grace wait"
            return real_append_line(*args, **kwargs)

        monkeypatch.setattr(
            decisions_gate, "threading", types.SimpleNamespace(Event=_SlowHostEvent)
        )
        monkeypatch.setattr(decisions_log, "append_line", _held_append_line)
        self.test_a_committed_row_keeps_the_join_key_when_sweep_outlives_budget(monkeypatch)

    def test_a_write_still_running_when_the_grace_runs_out_keeps_no_join_key(self, monkeypatch):
        class _Config:
            decisions = DecisionsConfig(nudge_wake=NudgeWakeConfig(provider="llm"))

        class _InvalidOracle:
            async def ask(self, *_args, **_kwargs):
                return None

        captured_receipt: dict = {}
        original_decide = decisions_gate.decide
        real_append_line = decisions_log.append_line
        write_release = threading.Event()

        async def _decide(*args, receipt, **kwargs):
            try:
                answers = await original_decide(*args, receipt=receipt, **kwargs)
            finally:
                write_release.set()
            captured_receipt.update(receipt)
            return answers

        def _held_append_line(*args, **kwargs):
            # The write lands only after the decision has returned, so neither the
            # budget nor the zero grace can see it commit: the receipt is unknown.
            write_release.wait(_STALLED_APPEND_CEILING_SECS)
            return real_append_line(*args, **kwargs)

        monkeypatch.setattr(decisions_gate, "_snapshot", lambda: _Config())
        monkeypatch.setattr(decisions_gate, "_consented_for", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_capability_denied", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_oracle", lambda *_args, **_kwargs: _InvalidOracle())
        monkeypatch.setattr(decisions_gate, "_LOG_BUDGET_SECS", 0.01)
        monkeypatch.setattr(decisions_gate, "_LOG_COMMIT_GRACE_SECS", 0)
        monkeypatch.setattr(decisions_log, "append_line", _held_append_line)
        monkeypatch.setattr(decisions_log, "sweep_expired", lambda: 0)
        monkeypatch.setattr(point.core, "decide", _decide)

        trace: dict = {}
        verdict = asyncio.run(
            point.judge_tick(
                "watch it",
                evidence=[
                    {
                        "source": "session:chat-1",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "new evidence",
                    }
                ],
                trace=trace,
            )
        )
        row = judge.verdict_entry(
            verdict,
            trace["evidence_items"],
            suppressed=False,
            answered=trace["answered"],
            verdict_id="v1" if trace["answered"] else "",
        )
        assert captured_receipt["row_written"] is None
        assert trace["answered"] is False
        assert "id" not in row

    def test_a_normalized_row_committing_after_budget_keeps_its_receipt(self, monkeypatch):
        class _Config:
            decisions = DecisionsConfig(nudge_wake=NudgeWakeConfig(provider="llm"))

        class _InvalidOracle:
            async def ask(self, *_args, **_kwargs):
                return None

        captured_receipt: dict = {}
        original_decide = decisions_gate.decide
        original_append = decisions_log.append

        async def _decide(*args, receipt, **kwargs):
            answers = await original_decide(*args, receipt=receipt, **kwargs)
            captured_receipt.update(receipt)
            return answers

        started = threading.Event()
        release = threading.Event()
        commit: dict = {}

        def _late_normalizing_append(row, *, commit_event=None):
            # The gate's commit signal is captured so the DRIVE can fire it, on the
            # event loop, once it has waited past the budget. That makes the commit a
            # deterministic post-budget fact instead of a real day-file write that has
            # to beat the production grace, which is what this case kept regressing on.
            commit["event"] = commit_event
            started.set()
            release.wait(5.0)
            return original_append(dict(row), commit_event=commit_event)

        monkeypatch.setattr(decisions_gate, "_snapshot", lambda: _Config())
        monkeypatch.setattr(decisions_gate, "_consented_for", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_capability_denied", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_oracle", lambda *_args, **_kwargs: _InvalidOracle())
        monkeypatch.setattr(decisions_gate, "_LOG_BUDGET_SECS", 0.005)
        monkeypatch.setattr(decisions_log, "append", _late_normalizing_append)
        monkeypatch.setattr(decisions_log, "sweep_expired", lambda: 0)
        monkeypatch.setattr(point.core, "decide", _decide)

        trace: dict = {}

        async def drive():
            task = asyncio.create_task(
                point.judge_tick(
                    "watch it",
                    evidence=[
                        {
                            "source": "session:chat-1",
                            "kind": point.KIND_TRANSCRIPT_TAIL,
                            "age_s": 1.0,
                            "text": "new evidence",
                        }
                    ],
                    trace=trace,
                )
            )
            # Wait for the worker rather than sleeping a guessed interval past the
            # budget: a sleep chosen to outlast it is what made this case depend on
            # the runner. Once the worker is parked on ``release`` the budget can only
            # expire, so "the append lands after the budget" is a fact.
            # flake-ok: pre-existing 5 s ceiling on a condition poll that fails by name; moved verbatim; tracked for a de-flake pass
            deadline = time.monotonic() + 5.0
            while not started.is_set():
                assert time.monotonic() < deadline, "append worker never started"
                await asyncio.sleep(0.001)
            # flake-ok: outlasts the patched 5 ms budget while the worker is parked, so the budget can only have expired; moved verbatim
            await asyncio.sleep(decisions_gate._LOG_BUDGET_SECS * 4)
            # The post-budget commit, fired here on the event loop. Nothing about the
            # receipt is left to a real write beating the PRODUCTION grace, which stays
            # at the 0.10 s the gate ships: the signal ``gate._write`` reads is set
            # synchronously, after the budget has provably expired, so the only thing
            # the grace still bounds is the wait that is already parked on it.
            commit["event"].set()
            release.set()
            # flake-ok: awaits the tick the release just above frees; the test has already unblocked it; moved verbatim
            return await task

        verdict = asyncio.run(drive())
        row = judge.verdict_entry(
            verdict,
            trace["evidence_items"],
            suppressed=False,
            answered=trace["answered"],
            verdict_id="v1" if trace["answered"] else "",
        )
        assert captured_receipt["row_written"] is True
        assert trace["answered"] is True
        assert row["id"] == "v1"

    def test_a_verdict_with_no_recorded_decision_keeps_no_join_key(self, monkeypatch):
        async def _decide(*_args, receipt, **_kwargs):
            receipt["row_written"] = False
            return None

        monkeypatch.setattr(point.core, "decide", _decide)
        trace: dict = {}
        verdict = asyncio.run(
            point.judge_tick(
                "watch it",
                evidence=[
                    {
                        "source": "session:chat-1",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "new evidence",
                    }
                ],
                trace=trace,
            )
        )
        row = judge.verdict_entry(
            verdict,
            trace["evidence_items"],
            suppressed=False,
            answered=trace["answered"],
            verdict_id="v1" if trace["answered"] else "",
        )
        assert trace["answered"] is False
        assert "id" not in row


class TestDecisionLogGrace:
    """Only a caller asking for a receipt waits beyond the write budget."""

    def test_only_a_receipt_request_pays_the_commit_grace(self, monkeypatch):
        # A lost-run ceiling on each call, never a race: a call that pays no grace was
        # measured back in 0.53 s at worst on a loaded Windows runner, and this sits far
        # under the suite's 120 s ``--timeout``, so a call that DOES wait fails at its
        # ``wait_for``, by name, instead of taking the worker down.
        # flake-ok: a lost-run ceiling under the 15 s grace, sized from the 0.53 s worst case cited above; moved verbatim
        ceiling_secs = 10.0
        # The grace, the hold on every append worker and the LLM lane's provider budget
        # (never under 8 s) all outlast the ceiling: a call that waits on any of them
        # off the loop cannot come back inside it on any host.
        grace_secs = 15.0

        class _Config:
            decisions = DecisionsConfig(
                provider=DecisionProviderConfig(timeout_ms=int(grace_secs * 1000)),
                nudge_wake=NudgeWakeConfig(provider="llm"),
            )

        class _InvalidOracle:
            async def ask(self, *_args, **_kwargs):
                return None

        releases: list[threading.Event] = []
        started: list[threading.Event] = []
        returned: list[threading.Event] = []

        def _late_append(_row, *, commit_event=None):
            # The TEST decides when this returns, so the ordering under assertion is
            # not a race: `started` says the worker thread is running, and `release`
            # is only ever set here by the test itself.
            release = threading.Event()
            started_flag = threading.Event()
            returned_flag = threading.Event()
            releases.append(release)
            started.append(started_flag)
            returned.append(returned_flag)
            started_flag.set()
            release.wait(grace_secs)
            returned_flag.set()
            if commit_event is not None:
                commit_event.set()
            return True

        async def _await_worker(index: int) -> None:
            # A thread this call handed off may not have been scheduled yet, which on
            # some platforms outlasts the write budget. Wait for it rather than assume.
            # flake-ok: pre-existing 5 s ceiling on a condition poll that fails by name; moved verbatim; tracked for a de-flake pass
            deadline = time.monotonic() + 5.0
            while len(started) <= index or not started[index].is_set():
                assert time.monotonic() < deadline, "append worker never started"
                await asyncio.sleep(0.005)

        monkeypatch.setattr(decisions_gate, "_consented_for", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_capability_denied", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_oracle", lambda *_args, **_kwargs: _InvalidOracle())
        # Zero, not a small value: the budget then expires on its first await, so both
        # calls take the over-budget branch on every host, and the receipt call's answer
        # can only come from inside the grace, never from an append that beat the budget.
        monkeypatch.setattr(decisions_gate, "_LOG_BUDGET_SECS", 0)
        monkeypatch.setattr(decisions_gate, "_LOG_COMMIT_GRACE_SECS", grace_secs)
        monkeypatch.setattr(decisions_log, "append", _late_append)

        async def drive() -> None:
            kwargs = {
                "session_key": "chat-1",
                "config": _Config(),
            }
            # Nothing here is timed. A call that waited for the commit off the loop
            # cannot return inside the ceiling, and one that ran the append ON the loop
            # can return only after the hold ended, which ``returned`` records.
            loop = asyncio.get_running_loop()
            asked_at = loop.time()
            try:
                await asyncio.wait_for(
                    decisions_gate.decide(
                        point.POINT,
                        {"loop": {"instruction": "watch it"}},
                        point.build_questions(),
                        **kwargs,
                    ),
                    ceiling_secs,
                )
            except asyncio.TimeoutError:
                pytest.fail(
                    "a call asking for no receipt waited for the commit: not back inside "
                    f"ceiling_secs ({ceiling_secs:.0f}s), {loop.time() - asked_at:.1f}s spent"
                )
            await _await_worker(0)
            assert len(releases) == 1
            assert releases[0].is_set() is False
            assert not returned[0].is_set(), "a call asking for no receipt held the append"
            releases[0].set()
            # Let the handed-off worker retire before the loop closes.
            # flake-ok: pre-existing 20 ms pause so the handed-off worker retires, as the comment above says; moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing pause after the release, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.02)

            receipt: dict = {}
            # Driven as a task so the worker can be freed WHILE this call is inside the
            # grace: that is the property under test, and it cannot be observed by a
            # release that only happens after the call has already returned.
            pending = asyncio.create_task(
                decisions_gate.decide(
                    point.POINT,
                    {"loop": {"instruction": "watch it"}},
                    point.build_questions(),
                    receipt=receipt,
                    **kwargs,
                )
            )
            await _await_worker(1)
            assert len(releases) == 2
            released_at = loop.time()
            releases[1].set()
            try:
                await asyncio.wait_for(pending, ceiling_secs)
            except asyncio.TimeoutError:
                pytest.fail(
                    "the receipt call did not come back once its commit landed: not inside "
                    f"ceiling_secs ({ceiling_secs:.0f}s), {loop.time() - released_at:.1f}s spent"
                )
            assert receipt["row_written"] is True

        asyncio.run(drive())


class TestWakeLabelRow:
    """The calibration row, keyed by the verdict it judges."""

    def test_it_joins_on_the_verdict_id(self):
        row = decisions_log.build_wake_label_row(
            verdict_id="ab12",
            point="nudge.wake",
            session_key="chat-1",
            label="owner_acted",
            value=True,
            tool_calls=1,
            reply_chars=12,
        )
        assert row["verdict_id"] == "ab12"
        assert row["kind"] == decisions_log.KIND_WAKE_LABEL
        assert row["value"] is True

    def test_it_is_told_apart_from_a_decision_row_by_its_kind(self):
        decision = decisions_log.build_row(point="nudge.wake", session_key="chat-1", latency_ms=1)
        assert "kind" not in decision
        label = decisions_log.build_wake_label_row(
            verdict_id="a",
            point="nudge.wake",
            session_key="chat-1",
            label="missed",
            value=False,
            tool_calls=0,
            reply_chars=0,
        )
        assert label["kind"] == "wake_label"

    def test_the_session_is_a_digest_and_not_the_key(self):
        row = decisions_log.build_wake_label_row(
            verdict_id="a",
            point="nudge.wake",
            session_key="chat-1953-1790314195",
            label="missed",
            value=False,
            tool_calls=0,
            reply_chars=0,
        )
        assert "chat-1953" not in json.dumps(row)
        assert row["session"] == decisions_log.session_digest("chat-1953-1790314195")

    def test_an_unknown_label_writes_an_empty_name(self):
        row = decisions_log.build_wake_label_row(
            verdict_id="a",
            point="nudge.wake",
            session_key=None,
            label="whatever",
            value=True,
            tool_calls=0,
            reply_chars=0,
        )
        assert row["label"] == ""

    def test_a_truthy_stand_in_is_not_a_true_value(self):
        row = decisions_log.build_wake_label_row(
            verdict_id="a",
            point="nudge.wake",
            session_key=None,
            label="missed",
            value="yes",
            tool_calls=0,
            reply_chars=0,
        )
        assert row["value"] is False

    def test_an_oversized_id_is_clipped(self):
        row = decisions_log.build_wake_label_row(
            verdict_id="z" * 500,
            point="nudge.wake",
            session_key=None,
            label="missed",
            value=True,
            tool_calls=0,
            reply_chars=0,
        )
        assert len(row["verdict_id"]) == 64

    def test_the_row_carries_no_reply_and_no_target(self):
        row = decisions_log.build_wake_label_row(
            verdict_id="a",
            point="nudge.wake",
            session_key="chat-1",
            label="owner_acted",
            value=True,
            tool_calls=0,
            reply_chars=0,
        )
        assert set(row) == {
            "ts",
            "kind",
            "verdict_id",
            "point",
            "session",
            "label",
            "value",
            "tool_calls",
            "reply_chars",
            "tool_names_known",
        }

    def test_the_row_says_whether_the_dispatches_were_named(self):
        """A count-only label and a named one were decided by two different rules.

        A threshold read that pools them measures a constant label as a measurement, so
        the row has to say which rule produced it.
        """
        for known in (True, False):
            row = decisions_log.build_wake_label_row(
                verdict_id="a",
                point="nudge.wake",
                session_key="chat-1",
                label="owner_acted",
                value=True,
                tool_calls=1,
                reply_chars=0,
                tool_names_known=known,
            )
            assert row["tool_names_known"] is known

    def test_the_named_flag_defaults_to_false_for_an_older_caller(self):
        row = decisions_log.build_wake_label_row(
            verdict_id="a",
            point="nudge.wake",
            session_key="chat-1",
            label="owner_acted",
            value=True,
            tool_calls=1,
            reply_chars=0,
        )
        assert row["tool_names_known"] is False

    def test_it_does_not_accept_a_timestamp_override(self):
        with pytest.raises(TypeError):
            decisions_log.build_wake_label_row(
                verdict_id="a",
                point="nudge.wake",
                session_key="chat-1",
                label="owner_acted",
                value=True,
                tool_calls=0,
                reply_chars=0,
                ts=None,
            )


class TestQuietStreakBound:
    """The point bounds what it retains, and that bound tracks the engine's own."""

    def test_the_point_and_the_engine_cap_the_streak_at_the_same_number(self):
        # The point clamps its own state rather than importing the engine's constant,
        # because the engine already imports this point -- borrowing it back would make
        # a cycle and pull the loop engine into this point's import path. The cost of
        # owning the number is that it can drift, which is what this pins.
        assert point.MAX_QUIET_STREAK == _MAX_QUIET_STREAK

    def test_the_point_does_not_import_the_loop_engine(self):
        # A cycle deferred by a function-local import is still a cycle.
        source = Path(point.__file__).read_text(encoding="utf-8")
        assert "from kiro_crew.autonudge import" not in source
        assert "import kiro_crew.autonudge" not in source


class TestVerdictIdBound:
    """One bound for the join key, so the two sides cannot drift apart."""

    def test_both_modules_name_the_same_bound(self):
        assert judge.MAX_VERDICT_ID_CHARS == decisions_log.MAX_VERDICT_ID_CHARS

    def test_a_clipped_id_comes_out_the_same_on_both_sides_of_the_join(self):
        # One population, and the join is by exact string: a bound that differed by a
        # character on either side would make every oversized id stop matching.
        oversized = "z" * 500
        stored = judge.verdict_entry(
            _verdict(irq.Outcome.WAKE),
            0,
            suppressed=False,
            answered=True,
            verdict_id=oversized,
        )
        label = decisions_log.build_wake_label_row(
            verdict_id=oversized,
            point="nudge.wake",
            session_key="chat-1",
            label="owner_acted",
            value=True,
            tool_calls=0,
            reply_chars=0,
        )
        assert len(stored["id"]) == len(label["verdict_id"])
        assert stored["id"] == label["verdict_id"]
