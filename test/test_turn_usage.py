"""Unit tests for TurnUsage and AcpEvent.usage."""

from kiro_crew.acp.types import AcpEvent, TurnUsage


def test_turn_usage_defaults_all_zero():
    u = TurnUsage()
    assert u.input_tokens == 0
    assert u.output_tokens == 0
    assert u.cache_creation_tokens == 0
    assert u.cache_read_tokens == 0
    assert u.cost_usd == 0.0
    assert u.credits == 0.0
    assert u.num_turns == 0
    assert u.duration_ms == 0


def test_turn_usage_holds_token_and_cost_fields():
    u = TurnUsage(
        input_tokens=10,
        output_tokens=5,
        cache_creation_tokens=2,
        cache_read_tokens=3,
        cost_usd=0.42,
        num_turns=1,
        duration_ms=1200,
    )
    assert u.input_tokens == 10
    assert u.output_tokens == 5
    assert u.cache_creation_tokens == 2
    assert u.cache_read_tokens == 3
    assert u.cost_usd == 0.42
    assert u.num_turns == 1
    assert u.duration_ms == 1200
    assert u.credits == 0.0


def test_turn_usage_holds_credits():
    u = TurnUsage(credits=1.23)
    assert u.credits == 1.23
    assert u.cost_usd == 0.0
    assert u.input_tokens == 0


def test_acp_event_defaults_to_empty_usage():
    e = AcpEvent(kind="complete")
    assert isinstance(e.usage, TurnUsage)
    assert e.usage.credits == 0.0
    assert e.usage.input_tokens == 0


def test_acp_event_usage_is_per_instance():
    a = AcpEvent(kind="complete")
    b = AcpEvent(kind="complete")
    a.usage.credits = 1.0
    # default_factory must give each event its own TurnUsage, not a shared mutable default
    assert b.usage.credits == 0.0
