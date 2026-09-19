"""The two consumers that project a ceiling's PATTERNS, read across a tier fold.

Both ask the control for its patterns instead of reading ``.allow`` / ``.deny``
off it, because a ceiling folded from two policy tiers is an ``_AndRuleset`` that
holds its patterns in its halves:

* ``may_skip_gate`` withholds the auto-approve grant when the ``mcp`` ruleset
  names the server at ANY granularity -- auto-approve is the one path that never
  reaches ``hooks.on_tool_call``, so a per-tool deny that is not seen here is
  never asked;
* ``resolve_pinned_commands`` projects the ``commands`` force-pins hooks unions
  into the effective denied set, which is what makes a fleet pin
  un-opt-out-able against the user's own per-rule opt-out.

A subordinate tier may only tighten, so neither projection may weaken when one
composes over the authority.
"""

from __future__ import annotations

from typing import Tuple

from kiro_crew.platform.governance import (
    BootControls,
    CapabilityGate,
    Decision,
    GovernanceCeiling,
    compose_tier_ladder,
    gate_decision,
    may_skip_gate,
    parse_policy,
    resolve_pinned_commands,
)

MCP_CENTRAL = {
    "version": 1,
    "boot": {},
    "mcp": {"mode": "deny", "deny": ["@srv/delete"]},
}

# A pure tightening by the ceiling algebra: an allow-mode set is a closed set,
# and ``_AndRuleset.permits`` requires BOTH halves to permit.
MCP_SUBORDINATE = {
    "version": 1,
    "boot": {},
    "mcp": {"mode": "allow", "allow": ["@srv"]},
}

PIN = "rm -rf /*"

COMMANDS_CENTRAL = {
    "version": 1,
    "boot": {},
    "commands": {"mode": "deny", "deny": [PIN]},
}

COMMANDS_SUBORDINATE = {
    "version": 1,
    "boot": {},
    "commands": {"mode": "allow", "allow": ["ls*"]},
}


def test_a_subordinate_tier_must_not_unlock_an_auto_approve_grant() -> None:
    central = parse_policy(MCP_CENTRAL)
    subordinate = parse_policy(MCP_SUBORDINATE)

    # The central per-tool deny is real when the gate asks.
    assert gate_decision(central, None, "mcp__srv__delete").permitted is False

    # Positive control: with the central document ALONE the predicate withholds
    # the blanket grant, because the ruleset names the server.
    assert may_skip_gate("@srv", central) is False

    folded = compose_tier_ladder(central, subordinate)
    assert folded is not None

    # The fold still denies @srv/delete WHEN ASKED, so the decision primitive is
    # not what is under test here -- only the consumer that reads the patterns.
    assert gate_decision(folded, None, "mcp__srv__delete").permitted is False

    # The fold must not hand out the grant that skips the gate.
    assert may_skip_gate("@srv", folded) is False


def test_a_subordinate_tier_must_not_drop_a_central_command_force_pin() -> None:
    central = parse_policy(COMMANDS_CENTRAL)
    subordinate = parse_policy(COMMANDS_SUBORDINATE)

    # Positive control: the central document alone projects the force-pin, which
    # is what hooks unions into the effective denied set.
    assert resolve_pinned_commands(central) == (PIN,)

    folded = compose_tier_ladder(central, subordinate)
    assert folded is not None

    # The pin survives the fold, so the lock stays un-opt-out-able.
    assert resolve_pinned_commands(folded) == (PIN,)


class _OtherRuleset:
    """A ruleset archetype that is neither of the two shapes shipping today.

    It satisfies ``RulesetLike`` structurally and nothing more, so a reader that
    asks the protocol consults it while a reader keyed to the concrete pair reads
    it as carrying no patterns at all.
    """

    def __init__(self, patterns: Tuple[str, ...], pins: Tuple[str, ...]) -> None:
        self._patterns = patterns
        self._pins = pins

    def permits(self, item: str) -> Decision:
        hit = item in self._pins
        return Decision(not hit, f"{item!r} {'denied' if hit else 'permitted'}")

    def declared_patterns(self) -> Tuple[str, ...]:
        return self._patterns

    def force_deny_patterns(self) -> Tuple[str, ...]:
        return self._pins


def _ceiling_with(scope: str, control: object) -> GovernanceCeiling:
    return GovernanceCeiling(version=1, boot=BootControls(), controls={scope: control})


def test_a_conforming_archetype_is_asked_by_both_readers() -> None:
    """Conformance is the test, not membership of the pair shipping today."""
    other = _OtherRuleset(patterns=("@srv/delete",), pins=(PIN,))

    assert may_skip_gate("@srv", _ceiling_with("mcp", other)) is False
    assert resolve_pinned_commands(_ceiling_with("commands", other)) == (PIN,)


def test_an_archetype_carrying_no_patterns_projects_nothing() -> None:
    """A control of another archetype has no patterns, so it names no server."""
    gate = CapabilityGate(enabled=True)

    assert resolve_pinned_commands(_ceiling_with("commands", gate)) == ()
    assert may_skip_gate("@srv", _ceiling_with("mcp", gate)) is True
