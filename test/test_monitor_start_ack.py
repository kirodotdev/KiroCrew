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
from kiro_crew.autonudge_judge import DEFAULT_WAKE_WHEN
from kiro_crew.mcp_tools import control
from kiro_crew.validation import ValidationError


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
    assert "only when the tick needs you" in out, "and say the cadence is screened"
    # The plain promise must be ABSENT: it is what made the change invisible.
    assert "the message will re-inject every 300s" not in out


def test_the_gated_ack_names_what_actually_raises_a_wake(bound_session):
    """A gated ack must spell out the wake set, not say "when it changes".

    Every lane of a settling pull request going green IS the subject changing, and
    raises no wake at all: the probe reports a red outside the known-inherited
    list, a fully settled green board, a conflict, a comment, or a review.
    An ack that promises a wake on any change invites the caller to measure a
    per-lane cadence that was never offered, and read the gap as a defect.

    The members come from the probe's own wake-source map, so a probe that grows
    a source reddens here instead of leaving this ack quietly incomplete.
    """
    out = _ack("Watch https://github.com/acme/widgets/pull/42 and report failures")
    assert "wake criteria" in out, "the ack must name what decides a wake"
    assert "shipped default" in out, "including the brief a loop that names none runs under"
    assert "costs no turn" in out, "and say what a screened-quiet tick costs"
    assert "no judge lane is available" in out, (
        "the screen is not always available -- a briefless loop needs this point's "
        "egress scope and a loop with its own criteria needs a lane armed, and neither "
        "holds on a stock install, so an ack promising a screened quiet unconditionally "
        "would be read as covering exactly the machine where it does not apply"
    )
    assert (
        "only an unchanged subject is free" in out
    ), "and say what IS still free there, since the reading answers that on its own"
    assert "one interval after the tick that saw it" in out, "state the wake's hold"


def test_the_default_brief_carries_the_untrusted_evidence_clause():
    """The clause the base RFC records, and the loops it covers have no author for it.

    A comment, a review and a fetched page are content a third party wrote, so a
    sentence inside one saying there is nothing to do is a claim rather than a reading.
    Without it a single comment can talk a screened loop into silence, and the bound on
    how long a watch may go undelivered rests on the clause.
    """
    from kiro_crew import autonudge_judge as judge

    quiet = judge.DEFAULT_QUIET_WHEN
    assert "untrusted content" in quiet
    assert "is not evidence that nothing happened" in quiet


def test_an_ungated_loop_keeps_the_plain_promise(bound_session):
    out = _ack("Keep the deploy queue moving and report anything stuck")
    assert "the message will re-inject every 300s" in out
    assert "only when the tick needs you" not in out


def test_the_opt_out_is_reported_as_ungated(bound_session):
    """gate=false must be an escape, not a promise.

    An opt-OUT does not share the adoption failure that ruled out an opt-in: the
    default still gates every surface, and this only releases a loop whose duty is
    to act WHILE its subject is quiet, which an observation of that subject cannot
    see.
    """
    out = _ack("Watch https://github.com/acme/widgets/pull/42", gate=False)
    assert "the message will re-inject every 300s" in out
    assert "only when the tick needs you" not in out
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


@pytest.mark.parametrize("max_cycles", [0, -1])
def test_a_gated_loop_rejects_an_unbounded_cycle_budget(bound_session, max_cycles):
    """A monitor must carry a positive finite cycle budget."""
    with pytest.raises(ValidationError, match=r"max_cycles: must be >= 1"):
        _ack("Watch https://github.com/acme/widgets/pull/42", max_cycles=max_cycles)


@pytest.mark.parametrize("max_cycles", [1, 5])
def test_a_gated_loop_with_a_positive_max_cycles_states_the_cap(bound_session, max_cycles):
    """A positive cap must be reported without an unlimited-budget claim."""
    out = _ack("Watch https://github.com/acme/widgets/pull/42", max_cycles=max_cycles)
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


def _monitor_start_schema() -> dict:
    """The descriptor a caller reads before arming anything."""
    for spec in control.schemas():
        if spec.get("name") == "monitor_start":
            return spec
    raise AssertionError("monitor_start has no descriptor")


def test_the_tool_description_states_the_wake_set_and_the_hold():
    """The description is read BEFORE arming, so it owes the same rule as the ack.

    A caller who reads "re-inject only when it changed" arms a 300s loop on a
    settling pull request and expects a wake as each lane lands. The gate raises
    nothing for that, and the caller has no way to learn it from this text, so the
    words have to carry the wake set and the fact that a raised wake waits.
    """
    description = str(_monitor_start_schema().get("description") or "")
    assert "only when the tick needs you" in description
    assert (
        DEFAULT_WAKE_WHEN[:40] not in description
    ), "the description names the SCREEN, not the default brief's own sentence"
    assert "wake criteria" in description, "it must say what decides a wake"
    assert "shipped default" in description, "and name the brief used when none is given"
    assert "A merge or a close ends the watch" in description, "not a wake"
    assert "one lane of many finishing" in description, "name the progress that is quiet"
    assert "one interval after the tick that observed it" in description


def test_the_gate_parameter_describes_the_opt_out_by_wake_not_by_change():
    """``gate`` is the escape for a duty the observation cannot see.

    Watching lanes land one at a time is such a duty and is not silence, so the
    parameter that releases a loop from gating has to name it beside the heartbeat
    cases, and has to say a gated interval is skipped for want of a WAKE rather
    than for want of a change.
    """
    schema = _monitor_start_schema()
    properties = (schema.get("inputSchema") or {}).get("properties") or {}
    gate = str((properties.get("gate") or {}).get("description") or "")
    assert "raises no wake" in gate, "the opt-out must be described against the wake set"
    assert "has not changed" not in gate, "a pending board that changed is still quiet"
    assert "lanes land one at a time" in gate, "name the workload that needs the opt-out"
