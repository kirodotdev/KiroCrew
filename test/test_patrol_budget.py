"""Patrol budget line: the nudge header shows a capped loop what is left.

``nudge_cycle_header`` puts a capped loop's remaining cycles and runtime in
front of the agent every cycle, and marks ``10% or less left`` near the end. An
uncapped loop (a work-ledger patrol, rfc-permanent-patrol.md) keeps the bare tag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.autonudge import NUDGE_RENEW_DUE_SHARE, NudgeLoop, nudge_cycle_header

NOW = 1_800_000_000.0


def _loop(**kw) -> NudgeLoop:
    base = dict(id="l1", slot_key="chat-1-1", message="patrol", cycle_count=0)
    base.update(kw)
    return NudgeLoop(**base)  # type: ignore[arg-type]


class TestHeader:
    def test_uncapped_loop_keeps_the_bare_tag(self):
        assert nudge_cycle_header(_loop(cycle_count=4), now=NOW) == "[auto-nudge cycle 5]"

    def test_capped_loop_adds_both_figures(self):
        loop = _loop(cycle_count=9, max_cycles=240, max_runtime_secs=86400, created_ts=NOW - 3600)
        assert nudge_cycle_header(loop, now=NOW) == (
            "[auto-nudge cycle 10]\n[patrol budget: cycle 10/240, 82800s/86400s runtime left]"
        )

    def test_runtime_without_an_anchor_is_omitted(self):
        loop = _loop(max_cycles=10, max_runtime_secs=600, created_ts=0.0)
        assert nudge_cycle_header(loop, now=NOW).endswith("[patrol budget: cycle 1/10]")

    def test_spent_runtime_is_floored_at_zero_and_due(self):
        loop = _loop(max_runtime_secs=600, created_ts=NOW - 5000)
        assert nudge_cycle_header(loop, now=NOW).endswith("0s/600s runtime left; 10% or less left]")

    def test_renew_due_turns_on_at_the_cycle_threshold(self):
        # 100 cycles: delivering cycle 89 leaves 11 (not due), cycle 90 leaves 10 (due).
        assert "10% or less left" not in nudge_cycle_header(_loop(cycle_count=88, max_cycles=100))
        assert nudge_cycle_header(_loop(cycle_count=89, max_cycles=100)).endswith(
            "; 10% or less left]"
        )

    def test_renew_due_turns_on_at_the_runtime_threshold(self):
        early = _loop(max_runtime_secs=1000, created_ts=NOW - 899)
        late = _loop(max_runtime_secs=1000, created_ts=NOW - 900)
        assert "10% or less left" not in nudge_cycle_header(early, now=NOW)
        assert "10% or less left" in nudge_cycle_header(late, now=NOW)

    @pytest.mark.parametrize(
        "bad", ["24", None, True, -5, object(), float("inf"), float("-inf"), float("nan"), 10**400]
    )
    def test_malformed_caps_give_the_bare_tag(self, bad):
        # A hand-edited store can hold any type; the fire must not raise.
        loop = _loop(cycle_count=2, max_cycles=bad, max_runtime_secs=bad, created_ts=NOW)
        assert nudge_cycle_header(loop, now=NOW) == "[auto-nudge cycle 3]"

    def test_share_matches_the_spec_text(self):
        assert NUDGE_RENEW_DUE_SHARE == 0.10
        spec = Path(__file__).resolve().parents[1] / "docs/system-specs/common/injected-messages.md"
        assert "at or under 10% of its cap" in " ".join(spec.read_text(encoding="utf-8").split())
