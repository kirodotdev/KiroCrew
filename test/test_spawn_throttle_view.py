"""The ``throttle`` reading ``GET /api/spawn/adaptive`` carries for the chat header.

The chip renders only what this view decides, so the rules live here: a cut is
a cap below its fresh-start value (exec below its start, the spawn gate paused
or below its start), a gate below its ceiling is not one, and a missing,
disabled or fixed-mode controller reads as no cut.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.dashboard.handlers import spawn_resume
from kiro_crew.dashboard.handlers.spawn_resume import throttle_view


def _state(**overrides: object) -> dict:
    """A controller snapshot at rest: exec at its ceiling of 8, gate at 4 of 32."""
    base: dict = {
        "enabled": True,
        "mode": "aimd",
        "effective_exec_cap": 8,
        "exec_ceiling": 8,
        "spawn_gate_capacity": 4,
        "gate_ceiling": 32,
        "paused": False,
        "last_sample": {"queued": 0},
    }
    base.update(overrides)
    return base


def test_no_controller_is_no_cut() -> None:
    assert throttle_view(None) == {"cut": False}
    assert throttle_view({}) == {"cut": False}


def test_disabled_controller_is_no_cut_even_below_its_ceiling() -> None:
    state = _state(enabled=False, effective_exec_cap=2)
    assert throttle_view(state, exec_start=8, gate_start=4) == {"cut": False}


def test_fixed_mode_pins_the_caps_so_it_is_never_a_cut() -> None:
    state = _state(mode="fixed", effective_exec_cap=2, paused=True)
    assert throttle_view(state, exec_start=8, gate_start=4) == {"cut": False}


def test_caps_at_their_start_values_are_not_a_cut() -> None:
    view = throttle_view(_state(), exec_start=8, gate_start=4)
    assert view["cut"] is False
    assert view["exec"] == {"cap": 8, "configured": 8, "cut": False}
    assert view["gate"] == {"cap": 4, "configured": 4, "cut": False, "paused": False}


def test_exec_cap_below_its_start_is_a_cut_with_the_queue_it_holds() -> None:
    state = _state(effective_exec_cap=2, last_sample={"queued": 5})
    view = throttle_view(state, exec_start=8, gate_start=4)
    assert view["cut"] is True
    assert view["exec"] == {"cap": 2, "configured": 8, "cut": True}
    assert view["gate"]["cut"] is False
    assert view["queued"] == 5


def test_exec_ceiling_stands_in_when_no_start_is_known() -> None:
    view = throttle_view(_state(effective_exec_cap=3))
    assert view["exec"] == {"cap": 3, "configured": 8, "cut": True}
    assert view["cut"] is True


def test_gate_below_its_start_is_a_cut_but_below_its_ceiling_is_not() -> None:
    cut = throttle_view(_state(spawn_gate_capacity=1), exec_start=8, gate_start=4)
    assert cut["gate"] == {"cap": 1, "configured": 4, "cut": True, "paused": False}
    assert cut["cut"] is True
    # 6 of a 32 ceiling, above the start of 4: the gate is growing, not cut.
    grown = throttle_view(_state(spawn_gate_capacity=6), exec_start=8, gate_start=4)
    assert grown["gate"]["cut"] is False
    assert grown["cut"] is False


def test_without_a_gate_start_only_a_pause_reads_as_a_gate_cut() -> None:
    low = throttle_view(_state(spawn_gate_capacity=1))
    assert low["gate"]["cut"] is False
    assert low["cut"] is False
    paused = throttle_view(_state(spawn_gate_capacity=1, paused=True))
    assert paused["gate"] == {"cap": 1, "configured": None, "cut": True, "paused": True}
    assert paused["cut"] is True


def test_malformed_numbers_never_read_as_a_cut() -> None:
    state = _state(effective_exec_cap=True, spawn_gate_capacity="1", last_sample={"queued": -3})
    view = throttle_view(state, exec_start=8, gate_start=4)
    assert view["cut"] is False
    assert view["exec"]["cap"] is None
    assert view["gate"]["cap"] is None
    assert view["queued"] == 0
    assert throttle_view(_state(last_sample=None), exec_start=8)["queued"] == 0


def test_start_caps_read_the_live_policy_parameters() -> None:
    params = SimpleNamespace(exec_start=16, gate_start=6)
    ctl = SimpleNamespace(policy=SimpleNamespace(params=params))
    with patch("kiro_crew.adaptive.controller.current", return_value=ctl):
        assert spawn_resume._start_caps() == (16, 6)
    with patch("kiro_crew.adaptive.controller.current", return_value=None):
        assert spawn_resume._start_caps() == (None, None)


def test_start_caps_survive_a_controller_that_cannot_answer() -> None:
    broken = SimpleNamespace(policy=None)
    with patch("kiro_crew.adaptive.controller.current", return_value=broken):
        assert spawn_resume._start_caps() == (None, None)


def test_start_caps_match_a_real_policy_and_its_fresh_caps() -> None:
    """The attribute names are the real policy's, and a fresh policy is not cut."""
    from kiro_crew.adaptive.policy import AdaptivePolicy, PolicyParams

    policy = AdaptivePolicy(PolicyParams(exec_ceiling=8, gate_initial=4, gate_ceiling=32))
    with patch(
        "kiro_crew.adaptive.controller.current", return_value=SimpleNamespace(policy=policy)
    ):
        exec_start, gate_start = spawn_resume._start_caps()
    assert (exec_start, gate_start) == (8, 4)
    fresh = {"enabled": True, **policy.snapshot()}
    assert throttle_view(fresh, exec_start=exec_start, gate_start=gate_start)["cut"] is False


@pytest.mark.asyncio
async def test_route_carries_the_snapshot_and_its_throttle_reading() -> None:
    live = _state(effective_exec_cap=2, spawn_gate_capacity=1, last_sample={"queued": 3})
    params = SimpleNamespace(exec_start=8, gate_start=4)
    ctl = SimpleNamespace(policy=SimpleNamespace(params=params))
    with (
        patch("kiro_crew.resource_status.adaptive_state", return_value=live),
        patch("kiro_crew.adaptive.controller.current", return_value=ctl),
    ):
        body = json.loads((await spawn_resume.api_spawn_adaptive(None)).text)  # type: ignore[arg-type]
    assert body["adaptive"] == live
    assert body["throttle"] == {
        "cut": True,
        "exec": {"cap": 2, "configured": 8, "cut": True},
        "gate": {"cap": 1, "configured": 4, "cut": True, "paused": False},
        "queued": 3,
    }


@pytest.mark.asyncio
async def test_route_without_a_controller_answers_no_cut() -> None:
    with (
        patch("kiro_crew.resource_status.adaptive_state", return_value=None),
        patch("kiro_crew.adaptive.controller.current", return_value=None),
    ):
        body = json.loads((await spawn_resume.api_spawn_adaptive(None)).text)  # type: ignore[arg-type]
    assert body == {"adaptive": {}, "throttle": {"cut": False}}
