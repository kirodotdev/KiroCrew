"""The monitor_start acknowledgement must state the loop's real cadence.

Gating is inferred from the instruction, so two loops armed through the SAME call
with the same interval can have different cadences. The ack is the only thing the
arming agent reads back, so if it promises a plain per-interval re-injection for a
loop that will actually be observed-and-gated, the change is invisible exactly
where it is decided -- which is the failure this whole feature exists to undo.
"""

from __future__ import annotations

import pytest

from kiro_crew import mcp_core
from kiro_crew.mcp_tools import control


@pytest.fixture()
def bound_session(monkeypatch):
    """Make the handler believe it is on a session that can arm a loop."""
    monkeypatch.setattr(mcp_core, "_autonudge_binding_key", lambda sk: "chat-1-123")
    monkeypatch.setattr(mcp_core, "_current_session_key", lambda: "dashboard:1", raising=False)
    return "dashboard:1"


def _ack(message: str, **extra) -> str:
    args = {"message": message, "interval_secs": 300, "max_cycles": 5}
    args.update(extra)
    return control.monitor_start("monitor_start", args)


def test_a_gated_loop_says_so_in_its_ack(bound_session):
    out = _ack("Watch https://github.com/acme/widgets/pull/42 and report failures")
    assert "acme/widgets#42" in out, "the ack must name the subject being observed"
    assert "only when it changes" in out, "and say the cadence is now event-driven"
    # The plain promise must be ABSENT: it is what made the change invisible.
    assert "the message will re-inject every 300s" not in out


def test_an_ungated_loop_keeps_the_plain_promise(bound_session):
    out = _ack("Keep the deploy queue moving and report anything stuck")
    assert "the message will re-inject every 300s" in out
    assert "only when it changes" not in out


def test_the_opt_out_is_reported_as_ungated(bound_session):
    """gate=false must be an escape, not a promise.

    An opt-OUT does not share the adoption failure that ruled out an opt-in: the
    default still gates every surface, and this only releases a loop whose duty is
    to act WHILE its subject is quiet, which an observation of that subject cannot
    see.
    """
    out = _ack("Watch https://github.com/acme/widgets/pull/42", gate=False)
    assert "the message will re-inject every 300s" in out
    assert "only when it changes" not in out
    assert "acme/widgets#42" not in out


def test_the_ack_carries_the_opt_out_to_the_scheduler(bound_session):
    """The ack is a directive: whoever applies it must see the same decision.

    If the flag stopped at the ack text the loop would still be armed gated, and
    the disclosure would be a lie in the other direction.
    """
    from kiro_crew import session_directive

    raw = _ack("Watch https://github.com/acme/widgets/pull/42", gate=False)
    args = session_directive.decode(raw, "monitor_start")
    assert args is not None, "the ack must still be a decodable monitor_start directive"
    assert args.get("gate") is False

    gated = session_directive.decode(
        _ack("Watch https://github.com/acme/widgets/pull/42"), "monitor_start"
    )
    assert gated is not None and gated.get("gate") is True, "absent means gated"


def test_a_gated_loop_with_zero_max_cycles_does_not_contradict_itself(bound_session):
    """max_cycles=0 means "unlimited", so the ack must describe the cap exactly once.

    The cap clause and the "NO cycle cap" clause live far apart in the ack
    expression, so an explicit zero renders BOTH unless the cadence clause shares
    the tail's truthiness guard -- asserting the loop both counts a cap and has
    none -- on the one parameter that decides whether an unattended loop can spend
    without limit.
    """
    out = _ack("Watch https://github.com/acme/widgets/pull/42", max_cycles=0)
    assert not (
        "cap counts" in out and "NO cycle cap" in out
    ), "an unlimited gated loop must not both count a cap and declare none"
    # The unlimited loop must POSITIVELY declare it has no cap, not just omit the
    # contradiction: silence here would let a bounded-sounding ack ship for a loop
    # that can actually spend without limit.
    assert "NO cycle cap" in out, "a zero-cap gated loop must declare it has no cap"
    assert "cap counts" not in out, "and must not also claim a cap counts turns"


def test_a_gated_loop_with_a_truthy_max_cycles_states_the_cap(bound_session):
    """A truthy cap is the counterpart: the ack must state the cap and NOT deny it.

    The zero-cap case proves the "unlimited" branch renders alone; this proves the
    bounded branch does too, so the two clauses never both appear -- the same
    contradiction, guarded from the other side.
    """
    out = _ack("Watch https://github.com/acme/widgets/pull/42", max_cycles=5)
    assert "cap counts" in out, "a bounded gated loop must state its cap counts turns"
    assert "NO cycle cap" not in out, "and must not also declare it has no cap"


def test_an_ambiguous_instruction_is_reported_as_ungated(bound_session):
    """Two subjects means no inference, so the ack must not claim a gate."""
    out = _ack(
        "Compare https://github.com/acme/widgets/pull/42 with "
        "https://github.com/acme/widgets/pull/43"
    )
    assert "the message will re-inject every 300s" in out
    assert "#42" not in out and "#43" not in out
