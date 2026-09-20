"""Timing invariants for the gateway's agent-scratch sweep loop.

The loop body lives in the monolithic ``kiro_crew.slack.gateway`` boot path
and standing up a full gateway to observe its first sweep is impractical, so
the prompt-first-pass behavior is expressed as named module constants
(FEAT-002). These tests pin those constants: the first pass must be prompt (on
the order of a minute) and much smaller than the steady-state hourly cadence,
so a gateway that restarts more often than hourly still reaches a sweep.
"""

from __future__ import annotations

from kiro_crew.slack import gateway


def test_first_sweep_delay_is_prompt_not_an_hour() -> None:
    # Prompt-but-deferred: on the order of a minute, and well under the old
    # full-hour wait so a frequently restarting gateway still sweeps.
    assert 0 < gateway._AGENT_SCRATCH_SWEEP_FIRST_DELAY_SECONDS <= 120


def test_first_sweep_delay_is_much_smaller_than_the_hourly_cadence() -> None:
    assert (
        gateway._AGENT_SCRATCH_SWEEP_FIRST_DELAY_SECONDS
        < gateway._AGENT_SCRATCH_SWEEP_INTERVAL_SECONDS
    )
    # The steady-state cadence is unchanged (hourly).
    assert gateway._AGENT_SCRATCH_SWEEP_INTERVAL_SECONDS == 3600
