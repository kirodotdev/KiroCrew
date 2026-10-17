"""The ``AutoNudgeService`` wiring the wake judge's feedback runs through.

The loaders bound what a loop record restores, the stored pull-request baseline
and the verdict history label by label; the turn-completion hook labels the
delivery a landed nudge turn ends and the quiets behind it; the tick records what
it decided while the fire path stamps what went out; and a turn the probe's own
floor fires takes the label of the turn it delivered. The shared verdict and row
builders live in ``wake_judge_feedback_helpers.py``.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
from pathlib import Path

import pytest
from wake_judge_feedback_helpers import _delivered, _suppressed, _verdict

from kiro_crew import autonudge_judge as judge
from kiro_crew import irq
from kiro_crew.autonudge import (
    _MAX_QUIET_STREAK,
    AutoNudgeService,
    NudgeLoop,
    _bounded_judge_recent,
)
from kiro_crew.decisions import log as decisions_log
from kiro_crew.monitoring.models import MonitorState


class TestTheStoredPullRequestBaselineIsBoundOnLoad:
    """The clip on the WRITE path constrains only what this build wrote.

    The store is writable by an auto-approved agent shell, so the bound that matters is
    the one the loader applies to whatever the file holds. Without it an oversized value
    is retained and re-serialized on every persist, for the life of the loop.
    """

    @staticmethod
    def _bound(raw):
        from kiro_crew.autonudge import _bounded_judge_pr_seen

        return _bounded_judge_pr_seen(raw)

    def test_an_oversized_id_list_is_capped(self):
        from kiro_crew import autonudge as _an

        loaded = self._bound({"digest": "a" * 16, "remarks": [f"c{i}" for i in range(5_000)]})
        assert len(loaded["remarks"]) == _an._JUDGE_PR_SEEN_REMARKS

    def test_a_long_id_and_a_long_digest_are_clipped(self):
        from kiro_crew import autonudge as _an

        loaded = self._bound({"digest": "d" * 9_000, "remarks": ["x" * 9_000]})
        assert len(loaded["digest"]) == _an._JUDGE_PR_SEEN_DIGEST_CHARS
        assert len(loaded["remarks"][0]) == _an._JUDGE_MAX_TARGET_CHARS

    def test_an_unknown_key_is_not_retained(self):
        loaded = self._bound({"digest": "a" * 16, "remarks": ["c1"], "prose": "x" * 9_000})
        assert set(loaded) == {"digest", "remarks"}, "a row that grew a field is not carried"

    def test_a_wrong_shape_loads_as_no_baseline(self):
        for raw in ("not a dict", 7, None, []):
            assert self._bound(raw) == {}, raw
        assert self._bound({"digest": 7, "remarks": [1, None, "c1"]}) == {
            "remarks": ["c1"]
        }, "a rejected digest or id costs at most a repeat delivery, which fires"


class TestStoredHistoryLoader:
    """What survives a round trip through the record, and what is refused on the way in."""

    def test_a_row_is_rebuilt_from_allowlisted_keys(self):
        loaded = _bounded_judge_recent(
            [
                {
                    "outcome": "quiet",
                    "at": 12.0,
                    "evidence_items": 2,
                    "suppressed": True,
                    "answered": True,
                    "missed": True,
                    "id": "q1",
                    "text": "a worker said something",
                    "target": "chat-1953",
                }
            ]
        )
        assert loaded == [
            {
                "outcome": "quiet",
                "at": 12.0,
                "evidence_items": 2,
                "suppressed": True,
                "answered": True,
                "missed": True,
                "id": "q1",
            }
        ]

    def test_a_labelled_delivery_keeps_its_stamp(self):
        row = _delivered(5.0, "d1")
        row["owner_acted"] = True
        loaded = _bounded_judge_recent([row])
        assert loaded[0]["delivered"] is True
        assert loaded[0]["owner_acted"] is True

    def test_an_unlabelled_delivery_is_marked_forfeited(self):
        # Its turn belongs to a process that is gone, so no label can ever be read for
        # it, and the next turn on that slot would be labelled in its place. The stamp
        # stays, so the row still closes its own label cycle.
        loaded = _bounded_judge_recent([_delivered(5.0, "d1")])
        assert loaded[0]["delivered"] is True
        assert loaded[0]["forfeited"] is True
        assert loaded[0]["outcome"] == "wake"

    def test_a_missed_label_on_a_delivery_is_dropped_and_forfeited(self):
        row = _delivered(5.0, "d1")
        row["missed"] = False
        loaded = _bounded_judge_recent([row])
        assert "missed" not in loaded[0]
        assert loaded[0]["forfeited"] is True

    def test_an_owner_acted_label_on_a_suppression_is_dropped(self):
        row = _suppressed(5.0, "q1")
        row["owner_acted"] = True
        loaded = _bounded_judge_recent([row])
        assert "owner_acted" not in loaded[0]

    def test_each_state_keeps_only_its_own_legitimate_label(self):
        delivery = _delivered(5.0, "d1")
        delivery.update({"owner_acted": True, "missed": False})
        suppression = _suppressed(6.0, "q1")
        suppression.update({"missed": True, "owner_acted": False})

        loaded = _bounded_judge_recent([delivery, suppression])

        assert loaded[0]["owner_acted"] is True
        assert "missed" not in loaded[0]
        assert "forfeited" not in loaded[0]
        assert loaded[1]["missed"] is True
        assert "owner_acted" not in loaded[1]

    def test_an_incompatible_label_cannot_capture_a_later_turn(self):
        crafted = _delivered(5.0, "crafted")
        crafted["missed"] = False
        loaded = _bounded_judge_recent([_suppressed(4.0, "q1"), crafted])

        labelled, changed = judge.label_latest_delivery(loaded, acted=True)

        assert changed == []
        assert labelled == loaded
        assert "missed" not in labelled[0]
        assert "owner_acted" not in labelled[1]
        assert labelled[1]["forfeited"] is True

    def test_a_row_claiming_both_states_cannot_defeat_forfeiture(self):
        # The states are exclusive, so only a hand-written row claims both -- and that
        # row is the one shape whose `missed` IS admitted onto a delivery, since the
        # suppressed side of the claim admits it. Forfeiture therefore turns on the
        # presence of a legitimate `owner_acted` and on nothing else: a row carrying
        # only `missed` is still forfeited, and so still takes no later turn's label.
        crafted = _delivered(5.0, "crafted")
        crafted["suppressed"] = True
        crafted["missed"] = False

        loaded = _bounded_judge_recent([_suppressed(4.0, "q1"), crafted])

        assert loaded[1]["missed"] is False
        assert loaded[1]["forfeited"] is True

        labelled, changed = judge.label_latest_delivery(loaded, acted=True)

        assert changed == []
        assert "owner_acted" not in labelled[1]
        assert "missed" not in labelled[0]

    def test_a_forfeited_row_remains_a_boundary(self):
        loaded = _bounded_judge_recent([_delivered(5.0, "d1")])
        stamped, changed = judge.confirm_delivery(loaded)
        assert changed is False
        assert stamped[0]["delivered"] is True
        assert stamped[0]["forfeited"] is True

    def test_a_row_naming_no_outcome_is_dropped(self):
        assert _bounded_judge_recent([{"at": 1.0, "suppressed": True}]) == []

    def test_a_non_list_loads_as_no_history(self):
        assert _bounded_judge_recent({"outcome": "quiet"}) == []
        assert _bounded_judge_recent(None) == []
        assert _bounded_judge_recent("quiet") == []

    def test_rows_that_are_not_objects_are_skipped(self):
        assert _bounded_judge_recent([None, 3, "x", {"outcome": "wake"}]) == [{"outcome": "wake"}]

    def test_a_longer_stored_list_is_trimmed_to_the_newest(self):
        rows = [_suppressed(float(index)) for index in range(judge.MAX_STORED_VERDICTS + 6)]
        loaded = _bounded_judge_recent(rows)
        assert len(loaded) == judge.MAX_STORED_VERDICTS
        assert loaded[-1]["at"] == float(judge.MAX_STORED_VERDICTS + 5)

    def test_a_far_oversized_history_builds_only_the_capped_newest_rows(self, monkeypatch, caplog):
        total = judge.MAX_STORED_VERDICTS * 20
        rows = [_suppressed(float(index)) for index in range(total)]
        built: list[float] = []

        def _build(item):
            built.append(item["at"])
            return dict(item)

        monkeypatch.setattr("kiro_crew.autonudge._bounded_judge_verdict", _build)
        with caplog.at_level("DEBUG", logger="kiro_crew.autonudge"):
            loaded = _bounded_judge_recent(rows)

        assert len(built) == judge.MAX_STORED_VERDICTS
        assert [row["at"] for row in loaded] == [
            float(index) for index in range(total - judge.MAX_STORED_VERDICTS, total)
        ]
        discarded = [message for message in caplog.messages if "discarded" in message]
        assert len(discarded) == 1
        assert str(total - judge.MAX_STORED_VERDICTS) in discarded[0]

    def test_a_non_boolean_flag_does_not_survive(self):
        loaded = _bounded_judge_recent(
            [{"outcome": "quiet", "missed": "yes", "suppressed": 1, "answered": "y"}]
        )
        assert loaded == [{"outcome": "quiet"}]


class TestTurnCompleteLabelling:
    """The hook: a finished turn labels the verdict that delivered it."""

    def test_runner_labels_only_landed_nudge_turns(self):
        runner_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "kiro_crew"
            / "dashboard"
            / "chat_runner.py"
        )
        tree = ast.parse(
            "\n".join(
                turn_file.read_text(encoding="utf-8")
                for turn_file in [
                    *sorted(runner_path.with_name("chat_turn").glob("*.py")),
                    runner_path,
                ]
                if turn_file.name != "__init__.py"
            )
        )
        # The turn records the facts its tail hands to notify_turn_complete; the
        # helper that makes the call forwards each one under its own name. Each
        # is pinned unique: a second record, or a second notify call (the
        # double-notify a direct call beside the helper would bring back), must
        # fail here wherever it lands in the file.
        records = [
            {
                key.value: value
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant)
            }
            for node in ast.walk(tree)
            if isinstance(node, ast.Dict)
            and any(
                isinstance(key, ast.Constant) and key.value in {"nudge_turn", "reply_flushed"}
                for key in node.keys
            )
        ]
        assert len(records) == 1, "exactly one record carries the nudge facts"
        [facts] = records
        gate = facts["nudge_turn"]
        assert isinstance(gate, ast.BoolOp)
        assert isinstance(gate.op, ast.And)
        assert [part.id for part in gate.values if isinstance(part, ast.Name)] == [
            "_directive_self_wake",
            "_turn_landed",
        ]
        assert isinstance(facts["reply_flushed"], ast.Name)
        assert facts["reply_flushed"].id == "_turn_flushed_visible_text"
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "notify_turn_complete"
        ]
        assert len(calls) == 1, "the tail's helper is the one notify_turn_complete call"
        forwarded = {keyword.arg: keyword.value for keyword in calls[0].keywords}
        assert all(
            isinstance(forwarded[name], ast.Name) and forwarded[name].id == name
            for name in ("nudge_turn", "reply_flushed")
        ), forwarded

    @staticmethod
    def _loop(history: list) -> NudgeLoop:
        return NudgeLoop(
            id="loop-1",
            slot_key="chat-1",
            message="watch chat-1953",
            idle_secs=300,
            active=True,
            gate=True,
            judge_recent_verdicts=history,
        )

    def _service(self, tmp_path, history: list):
        service = AutoNudgeService(base_dir=tmp_path)
        loop = self._loop(history)
        service._loops[loop.id] = loop
        return service, loop

    @staticmethod
    def _complete(service: AutoNudgeService, **kwargs) -> None:
        """Drive the hook the way the gateway does: from inside a running loop.

        The hook re-arms the loop's timer, which schedules a task, so it has a running
        loop by contract. Driving it without one would test a shape production never
        reaches and would fail on the re-arm rather than on anything under test.
        """

        async def drive() -> None:
            service.notify_turn_complete("chat-1", **kwargs)
            # Drain the label task and every persist or append task it creates, so
            # nothing is left pending when the loop closes.
            # flake-ok: a zero-length yield so the hook's label task starts before the drain loop reads it; moved verbatim
            await asyncio.sleep(0)
            while service._inflight_adds:
                for task in list(service._inflight_adds):
                    with contextlib.suppress(Exception):
                        # flake-ok: awaits each in-flight add the hook created; each ends on its own persist or append; moved verbatim
                        await task
                # flake-ok: a zero-length yield so the tasks the drained ones scheduled take a turn before the loop re-reads the set; moved verbatim
                await asyncio.sleep(0)

        asyncio.run(drive())

    def test_an_acting_turn_labels_the_delivery_and_the_quiets_behind_it(self, tmp_path):
        service, loop = self._service(tmp_path, [_suppressed(1.0, "q1"), _delivered(2.0, "d1")])
        try:
            self._complete(service, tool_calls=3, reply_text="pushed a fix", nudge_turn=True)
        finally:
            service.stop()
        assert loop.judge_recent_verdicts[-1]["owner_acted"] is True
        assert loop.judge_recent_verdicts[0]["missed"] is True

    def test_retroactive_missed_rows_carry_distance_from_the_delivery(self, tmp_path, monkeypatch):
        written: list[dict] = []
        monkeypatch.setattr(decisions_log, "append", lambda row: written.append(row) or True)
        history = [
            _suppressed(100.0, "q-old"),
            _suppressed(195.0, "q-new"),
            _delivered(200.0, "d1"),
        ]
        service, loop = self._service(tmp_path, history)
        try:
            self._complete(
                service,
                tool_calls=3,
                reply_text="  fixed it  ",
                nudge_turn=True,
            )
        finally:
            service.stop()

        missed = {row["verdict_id"]: row for row in written if row["label"] == "missed"}
        assert {
            verdict_id: (row["position_back"], row["age_s"]) for verdict_id, row in missed.items()
        } == {"q-old": (2, 100.0), "q-new": (1, 5.0)}
        assert all(isinstance(row["position_back"], int) for row in missed.values())
        assert all(isinstance(row["age_s"], float) for row in missed.values())
        assert "fixed it" not in json.dumps(written)

    def test_label_rows_carry_rederivable_inputs_without_reply_text(self, tmp_path, monkeypatch):
        written: list[dict] = []
        monkeypatch.setattr(decisions_log, "append", lambda row: written.append(row) or True)
        service, loop = self._service(tmp_path, [_suppressed(1.0, "q1"), _delivered(2.0, "d1")])
        try:
            self._complete(
                service,
                tool_calls=3,
                reply_text="  fixed it  ",
                nudge_turn=True,
            )
        finally:
            service.stop()
        assert len(written) == 2
        assert {row["tool_calls"] for row in written} == {3}
        assert {row["reply_chars"] for row in written} == {8}
        assert "fixed it" not in json.dumps(written)

    def test_an_idle_turn_clears_the_quiets_behind_it(self, tmp_path):
        service, loop = self._service(tmp_path, [_suppressed(1.0, "q1"), _delivered(2.0, "d1")])
        try:
            self._complete(service, tool_calls=0, reply_text="Nothing new.", nudge_turn=True)
        finally:
            service.stop()
        assert loop.judge_recent_verdicts[-1]["owner_acted"] is False
        assert loop.judge_recent_verdicts[0]["missed"] is False

    def test_a_flushed_reply_with_a_short_final_segment_reads_as_acting(self, tmp_path):
        service, loop = self._service(tmp_path, [_delivered(1.0, "d1")])
        try:
            self._complete(
                service,
                tool_calls=0,
                reply_text="Done.",
                reply_flushed=True,
                nudge_turn=True,
            )
        finally:
            service.stop()
        assert loop.judge_recent_verdicts[-1]["owner_acted"] is True

    def test_a_turn_with_no_counts_reads_as_acting(self, tmp_path, monkeypatch):
        # An older runner passes neither, and the rule's safe direction is not to
        # teach the judge that this wake was wasted.
        written: list[dict] = []
        monkeypatch.setattr(decisions_log, "append", lambda row: written.append(row) or True)
        service, loop = self._service(tmp_path, [_delivered(1.0, "d1")])
        try:
            self._complete(service, nudge_turn=True)
        finally:
            service.stop()
        assert loop.judge_recent_verdicts[-1]["owner_acted"] is True
        assert written[0]["tool_calls"] is None
        assert written[0]["reply_chars"] == 0

    def test_an_undelivered_wake_is_not_labelled_by_the_turn_that_refused_it(self, tmp_path):
        # The finding, at the seam: a fire refused because the slot was busy leaves an
        # unstamped row, and the user's own turn must not be scored against it.
        pending = judge.verdict_entry(
            _verdict(irq.Outcome.WAKE), 2, suppressed=False, answered=True, verdict_id="lost"
        )
        service, loop = self._service(tmp_path, [pending])
        try:
            self._complete(
                service,
                tool_calls=9,
                reply_text="the owner did their own work",
                nudge_turn=True,
            )
        finally:
            service.stop()
        assert "owner_acted" not in loop.judge_recent_verdicts[-1]

    @pytest.mark.parametrize("nudge_turn", [False, None])
    def test_a_turn_that_is_not_the_loop_turn_does_not_label(self, tmp_path, nudge_turn):
        service, loop = self._service(tmp_path, [_delivered(1.0, "d1")])
        try:
            self._complete(
                service,
                tool_calls=4,
                reply_text="the owner did unrelated work",
                nudge_turn=nudge_turn,
            )
        finally:
            service.stop()
        assert "owner_acted" not in loop.judge_recent_verdicts[-1]

    def test_a_nudge_turn_that_did_not_land_does_not_label(self, tmp_path):
        service, loop = self._service(tmp_path, [_suppressed(1.0, "q1"), _delivered(2.0, "d1")])
        try:
            self._complete(service, tool_calls=0, reply_text="", nudge_turn=False)
        finally:
            service.stop()
        assert "owner_acted" not in loop.judge_recent_verdicts[-1]
        assert "missed" not in loop.judge_recent_verdicts[0]

    def test_a_loop_with_no_judge_history_is_untouched(self, tmp_path):
        service, loop = self._service(tmp_path, [])
        try:
            self._complete(service, tool_calls=0, reply_text="")
        finally:
            service.stop()
        assert loop.judge_recent_verdicts == []

    def test_the_hook_still_rearms_the_timer(self, tmp_path):
        # The label must never be able to cost the loop its re-arm: that is the hook's
        # original job and everything here is an observation beside it.
        service, loop = self._service(tmp_path, [_delivered(1.0, "d1")])
        armed: list = []
        service._arm_from_deadline = lambda target: armed.append(target.id)  # type: ignore[assignment]
        try:
            service.notify_turn_complete("chat-1", tool_calls=1, reply_text="")
        finally:
            service.stop()
        assert armed == ["loop-1"]

    def test_a_ruined_history_does_not_stop_the_rearm(self, tmp_path):
        service, loop = self._service(tmp_path, [object()])  # type: ignore[list-item]
        armed: list = []
        service._arm_from_deadline = lambda target: armed.append(target.id)  # type: ignore[assignment]
        try:
            service.notify_turn_complete("chat-1", tool_calls=0, reply_text="")
        finally:
            service.stop()
        assert armed == ["loop-1"]

    def test_a_failed_label_persist_publishes_nothing_and_restores_the_row(self, tmp_path):
        service, loop = self._service(tmp_path, [_delivered(1.0, "d1")])
        armed: list = []
        published: list = []
        service._arm_from_deadline = lambda target: armed.append(target.id)  # type: ignore[assignment]

        async def _reject(_loop) -> bool:
            return False

        service._persist_judge_state = _reject  # type: ignore[method-assign]
        service._append_judge_labels = (  # type: ignore[method-assign]
            lambda _loop, changed, *, tool_calls=None, reply_chars=0, **_rest: published.extend(
                changed
            )
        )
        try:
            self._complete(service, tool_calls=4, reply_text="", nudge_turn=True)
        finally:
            service.stop()
        assert armed == ["loop-1"]
        assert published == []
        assert "owner_acted" not in loop.judge_recent_verdicts[-1]

    def test_a_detached_loop_publishes_no_labels(self, tmp_path):
        service, loop = self._service(tmp_path, [_delivered(1.0, "d1")])
        published: list = []

        async def _detach_loop(_loop) -> bool:
            service._loops.pop(loop.id)
            return True

        service._persist_judge_state = _detach_loop  # type: ignore[method-assign]
        service._append_judge_labels = (  # type: ignore[method-assign]
            lambda _loop, changed, *, tool_calls=None, reply_chars=0, **_rest: published.extend(
                changed
            )
        )
        try:
            self._complete(service, tool_calls=1, reply_text="", nudge_turn=True)
        finally:
            service.stop()
        assert published == []

    def test_a_replaced_history_publishes_no_labels(self, tmp_path):
        service, loop = self._service(tmp_path, [_suppressed(1.0, "q1"), _delivered(2.0, "d1")])
        published: list = []

        async def _replace_history(_loop) -> bool:
            loop.judge_recent_verdicts = []
            return True

        service._persist_judge_state = _replace_history  # type: ignore[method-assign]
        service._append_judge_labels = (  # type: ignore[method-assign]
            lambda _loop, changed, *, tool_calls=None, reply_chars=0, **_rest: published.extend(
                changed
            )
        )
        try:
            self._complete(service, tool_calls=1, reply_text="", nudge_turn=True)
        finally:
            service.stop()
        assert loop.judge_recent_verdicts == []
        assert published == []

    def test_a_failed_persist_does_not_resurrect_a_cleared_history(self, tmp_path):
        # An update landing during the write clears the history ON PURPOSE -- a retarget
        # or a criteria change, both of which drop it because its rows answer a question
        # that is gone. An unconditional snapshot restore would put those rows back, and
        # nothing downstream purges them, so the next tick reads a hit rate for the old
        # subject. The restore therefore only fires when the list it labelled is still
        # the one on the loop.
        service, loop = self._service(tmp_path, [_suppressed(1.0, "q1"), _delivered(2.0, "d1")])

        async def _clear_then_fail(_loop) -> bool:
            loop.judge_recent_verdicts = []
            return False

        service._persist_judge_state = _clear_then_fail  # type: ignore[method-assign]
        try:
            self._complete(service, tool_calls=1, reply_text="", nudge_turn=True)
        finally:
            service.stop()
        assert loop.judge_recent_verdicts == [], "the deliberate clear stands"

    def test_a_raising_publication_does_not_resurrect_a_cleared_history(self, tmp_path):
        # The same rule on the other branch, and reaching it takes care: the publication
        # guard returns early on an already-cleared history, so the clear has to happen
        # DURING the publication for the raise to land in the handler at all.
        service, loop = self._service(tmp_path, [_suppressed(1.0, "q1"), _delivered(2.0, "d1")])

        async def _succeed(_loop) -> bool:
            return True

        def _clear_then_raise(_loop, _changed, *, tool_calls=None, reply_chars=0, **_rest):
            loop.judge_recent_verdicts = []
            raise RuntimeError("publication failed")

        service._persist_judge_state = _succeed  # type: ignore[method-assign]
        service._append_judge_labels = _clear_then_raise  # type: ignore[method-assign]
        try:
            self._complete(service, tool_calls=1, reply_text="", nudge_turn=True)
        finally:
            service.stop()
        assert loop.judge_recent_verdicts == [], "the deliberate clear stands"


class TestVerdictRecordedOnTheTick:
    """The tick records what it decided; the fire path records what went out."""

    @staticmethod
    def _loop() -> NudgeLoop:
        return NudgeLoop(
            id="loop-1",
            slot_key="chat-1",
            message="watch chat-1953",
            idle_secs=300,
            active=True,
            gate=True,
        )

    def test_a_suppressed_verdict_is_recorded_as_withholding_the_turn(self, tmp_path):
        service = AutoNudgeService(base_dir=tmp_path)
        loop = self._loop()
        try:
            service._record_judge_verdict(
                loop, _verdict(irq.Outcome.QUIET), 2, "v1", suppressed=True, answered=True
            )
        finally:
            service.stop()
        row = loop.judge_recent_verdicts[-1]
        assert row["suppressed"] is True
        assert "delivered" not in row
        assert row["id"] == "v1"

    def test_a_wake_verdict_is_recorded_undecided(self, tmp_path):
        service = AutoNudgeService(base_dir=tmp_path)
        loop = self._loop()
        try:
            service._record_judge_verdict(
                loop, _verdict(irq.Outcome.WAKE), 5, "v2", suppressed=False, answered=True
            )
        finally:
            service.stop()
        row = loop.judge_recent_verdicts[-1]
        assert "delivered" not in row
        assert "suppressed" not in row

    def test_the_fire_path_stamps_the_delivery(self, tmp_path):
        service = AutoNudgeService(base_dir=tmp_path)
        loop = self._loop()
        try:
            service._record_judge_verdict(
                loop, _verdict(irq.Outcome.WAKE), 5, "v2", suppressed=False, answered=True
            )
            service._confirm_judge_delivery(loop)
        finally:
            service.stop()
        assert loop.judge_recent_verdicts[-1]["delivered"] is True

    def test_a_failed_persist_withdraws_the_suppression(self, tmp_path):
        service = AutoNudgeService(base_dir=tmp_path)
        loop = self._loop()
        try:
            service._record_judge_verdict(
                loop, _verdict(irq.Outcome.QUIET), 1, "v3", suppressed=True, answered=True
            )
            service._withdraw_judge_suppression(loop)
        finally:
            service.stop()
        row = loop.judge_recent_verdicts[-1]
        assert "suppressed" not in row
        assert "delivered" not in row

    def test_withdrawing_from_an_empty_history_does_not_raise(self, tmp_path):
        service = AutoNudgeService(base_dir=tmp_path)
        loop = self._loop()
        try:
            service._withdraw_judge_suppression(loop)
            service._confirm_judge_delivery(loop)
        finally:
            service.stop()
        assert loop.judge_recent_verdicts == []


class TestProbeFloorFiresAJudgedTick:
    """The probe's own floor can fire the turn a judge row claims it withheld."""

    def test_a_floor_fire_takes_no_missed_label_for_the_turn_it_delivered(
        self, tmp_path, monkeypatch
    ):
        """The row the probe floor fires is labelled by that turn, not as a suppression.

        The judge's streak and the probe's streak are independent fields, so a gated
        loop whose judge arms mid-life reaches the probe's floor with the judge's own
        streak still below its floor. The judge has already stamped this tick's row as
        withholding the turn, and the floor then delivers it anyway. Left standing, that
        row is skipped by the delivery stamp and takes ``missed`` from the retroactive
        pass of the next delivery -- a wrong row in the one log this feature produces.
        """
        monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
        monkeypatch.setattr(
            irq,
            "poll",
            lambda *_a, **_k: irq.Verdict(irq.Outcome.QUIET, "nothing yet", ()),
        )
        monitor = MonitorState(
            kind="gh-pr",
            target="acme/widgets#42",
            objective="review_ready",
            created_ts=1_000.0,
        )
        monitor.quiet_streak = _MAX_QUIET_STREAK - 1
        loop = NudgeLoop(
            id="loop-floor",
            slot_key="chat-1",
            message="watch https://github.com/acme/widgets/pull/42 until green",
            idle_secs=30,
            active=True,
            monitor=monitor,
            gate=True,
        )
        service = AutoNudgeService(base_dir=tmp_path)
        service._loops[loop.id] = loop
        persisted: list[str] = []
        real_persist = service._persist_judge_state

        async def _recording_persist(target_loop: NudgeLoop) -> bool:
            persisted.append("start")
            landed = await real_persist(target_loop)
            persisted.append("done")
            return landed

        service._persist_judge_state = _recording_persist  # type: ignore[method-assign]

        async def _judge_suppresses(target_loop: NudgeLoop) -> bool:
            """What the judge leaves behind when it answers quiet below its own floor."""
            service._record_judge_verdict(
                target_loop,
                _verdict(irq.Outcome.QUIET),
                2,
                "v-floor",
                suppressed=True,
                answered=True,
            )
            return True

        service._judge_tick_is_quiet = _judge_suppresses  # type: ignore[method-assign]

        async def drive() -> None:
            assert await service._monitor_tick_is_quiet(loop) is False, "the floor must fire"
            # Only the floor branch owes this charge, so it is what proves the tick
            # reached the floor rather than returning early on a fallback.
            assert loop.id in service._pending_floor_tick, "the floor branch ran"
            # The withdrawal is a memory edit, so the tick has to await its durable
            # write: a deferred one that never lands leaves a restart reading the row
            # as suppressed and the next delivery writing the false label back.
            assert persisted == ["start", "done"], "the withdrawal landed before the fire"
            # The fire lands, so the corrected row is stamped like any other delivery
            # and the turn it delivered is what labels it.
            service._confirm_judge_delivery(loop)
            history, _ = judge.label_latest_delivery(loop.judge_recent_verdicts, acted=True)
            loop.judge_recent_verdicts = history
            # A later real wake. Its retroactive pass is what writes the wrong label on
            # a row still claiming to have withheld its turn.
            service._record_judge_verdict(
                loop, _verdict(irq.Outcome.WAKE), 3, "v-wake", suppressed=False, answered=True
            )
            service._confirm_judge_delivery(loop)
            history, _ = judge.label_latest_delivery(loop.judge_recent_verdicts, acted=True)
            loop.judge_recent_verdicts = history
            # flake-ok: a zero-length yield so the tasks this drive scheduled start before the loop closes; moved verbatim
            await asyncio.sleep(0)

        try:
            asyncio.run(drive())
        finally:
            service.stop()

        floor_row = next(row for row in loop.judge_recent_verdicts if row.get("id") == "v-floor")
        assert "suppressed" not in floor_row, "the claim to have withheld the turn is withdrawn"
        assert floor_row["owner_acted"] is True, "the turn it delivered is what labels it"
        assert "missed" not in floor_row, "and no later delivery labels it as a suppression"

    def test_a_floor_fire_leaves_an_earlier_tick_s_suppression_alone(self, tmp_path, monkeypatch):
        """With no lane armed, the newest row belongs to an earlier tick and stands.

        Nothing is asked on this tick, so there is no claim of its own to withdraw. The
        newest row is a suppression that is TRUE, and clearing it would return it to the
        undecided state, where the fire path stamps it delivered and the finished turn
        credits it with a delivery it never produced.
        """
        monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
        monkeypatch.setattr(
            irq,
            "poll",
            lambda *_a, **_k: irq.Verdict(irq.Outcome.QUIET, "nothing yet", ()),
        )
        monitor = MonitorState(
            kind="gh-pr",
            target="acme/widgets#42",
            objective="review_ready",
            created_ts=1_000.0,
        )
        monitor.quiet_streak = _MAX_QUIET_STREAK - 1
        loop = NudgeLoop(
            id="loop-floor-unarmed",
            slot_key="chat-1",
            message="watch https://github.com/acme/widgets/pull/42 until green",
            idle_secs=30,
            active=True,
            monitor=monitor,
            gate=True,
        )
        loop.judge_recent_verdicts = [_suppressed(1.0, "v-old")]
        service = AutoNudgeService(base_dir=tmp_path)
        service._loops[loop.id] = loop
        persisted: list[str] = []
        real_persist = service._persist_judge_state

        async def _recording_persist(target_loop: NudgeLoop) -> bool:
            persisted.append("start")
            return await real_persist(target_loop)

        service._persist_judge_state = _recording_persist  # type: ignore[method-assign]

        async def _no_lane_armed(_target_loop: NudgeLoop) -> None:
            return None

        service._judge_tick_is_quiet = _no_lane_armed  # type: ignore[method-assign]

        async def drive() -> None:
            assert await service._monitor_tick_is_quiet(loop) is False, "the floor must fire"
            assert loop.id in service._pending_floor_tick, "the floor branch ran"
            # No row of this tick's to make durable, so the streak reset rides the
            # deferred write exactly as it does without a judge.
            assert persisted == [], "no judge write is forced on a tick that asked nothing"
            service._confirm_judge_delivery(loop)
            history, _ = judge.label_latest_delivery(loop.judge_recent_verdicts, acted=True)
            loop.judge_recent_verdicts = history
            # flake-ok: a zero-length yield so the tasks this drive scheduled start before the loop closes; moved verbatim
            await asyncio.sleep(0)

        try:
            asyncio.run(drive())
        finally:
            service.stop()

        old_row = next(row for row in loop.judge_recent_verdicts if row.get("id") == "v-old")
        assert old_row.get("suppressed") is True, "a suppression this tick did not make stands"
        assert "delivered" not in old_row, "and it is credited with no delivery"
        assert "owner_acted" not in old_row
