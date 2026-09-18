"""The request must ADDRESS a crew, and the container must never substitute one.

This covers the defect that made the first live deployment look healthy while
serving nobody's crew. The turn probe sent ``{"model": "default"}``; Kiro Crew maps
``model`` to an agent name and validates it against a NAME REGEX rather than against
the agents that exist (``dashboard/openai_compat.py:237``), so the name resolved to
the installed default and a stock agent answered. Every gate passed, because the
question asked was one any agent answers correctly.

Two properties are tested here, and the second is a security property rather than a
correctness one:

1. A request that names nobody is REFUSED. Answering it with a default is the bug.
2. The value that reaches the backend is the DEPLOYED crew name, not the caller's
   string, so no caller can steer the turn to another agent in the container.
"""

from __future__ import annotations

from typing import Any

import pytest
from container.front.app import _forward_body, judge_addressed_crew

TURN = "/v1/chat/completions"


# --- 1. an unaddressed request is refused, not defaulted ---------------------


def test_a_request_naming_nobody_is_refused() -> None:
    crew, refusal = judge_addressed_crew({"messages": []}, "acme-support")
    assert crew is None
    assert refusal is not None and refusal.status_code == 400
    assert b"crew_not_addressed" in refusal.body


def test_an_empty_crew_name_is_refused_like_an_absent_one() -> None:
    """`model: ""` and `model: "  "` are the same omission spelled differently."""
    for value in ("", "   "):
        crew, refusal = judge_addressed_crew({"model": value}, "acme-support")
        assert crew is None, value
        assert refusal is not None and refusal.status_code == 400


def test_the_payload_that_caused_the_original_bug_is_now_refused() -> None:
    """`model: "default"` is the exact request that got a stock agent and a 200.

    It is refused as a crew this deployment does not serve, which is the truthful
    answer: nothing named "default" was ever deployed here.
    """
    crew, refusal = judge_addressed_crew({"model": "default"}, "acme-support")
    assert crew is None
    assert refusal is not None and refusal.status_code == 404
    assert b"crew_not_served_here" in refusal.body


def test_naming_another_crew_is_refused_and_says_which_one_is_here() -> None:
    crew, refusal = judge_addressed_crew({"model": "other-crew"}, "acme-support")
    assert crew is None
    assert refusal is not None and refusal.status_code == 404
    # Withholding the deployed name would make a typo unfixable, and it is not a
    # secret: it is in the route prefix the caller already used.
    assert b"acme-support" in refusal.body


def test_a_non_string_model_is_refused() -> None:
    values: tuple[object, ...] = (1, [], {}, True)
    for value in values:
        crew, refusal = judge_addressed_crew({"model": value}, "acme-support")
        assert crew is None, value
        assert refusal is not None and refusal.status_code == 400


def test_addressing_the_deployed_crew_proceeds() -> None:
    crew, refusal = judge_addressed_crew({"model": "acme-support"}, "acme-support")
    assert refusal is None
    assert crew == "acme-support"


def test_surrounding_whitespace_still_addresses_the_crew() -> None:
    crew, refusal = judge_addressed_crew({"model": " acme-support "}, "acme-support")
    assert refusal is None
    assert crew == "acme-support"


def test_a_container_that_does_not_know_its_own_crew_refuses_everything() -> None:
    """Fail closed. The alternative is accepting any name because we cannot check."""
    crew, refusal = judge_addressed_crew({"model": "anything"}, "")
    assert crew is None
    assert refusal is not None and refusal.status_code == 503
    assert b"crew_not_configured" in refusal.body


# --- 2. the caller cannot choose which agent answers ------------------------


def test_the_forwarded_model_comes_from_the_deployment_not_the_caller() -> None:
    """Forwarding the caller's string made every agent in the container reachable.

    The equality check alone is not enough: a value that passes it after stripping
    must not be handed on in its original form, or the backend's resolver sees a
    string the check did not examine.
    """
    payload: dict[str, Any] = {"model": " acme-support ", "messages": [], "id": "s"}
    body, slot_id, stream = _forward_body(payload, "acme-support")
    assert body["model"] == "acme-support"
    assert slot_id == "s"
    assert stream is False


def test_forwarding_drops_every_field_outside_the_contract() -> None:
    payload = {
        "model": "acme-support",
        "messages": [{"role": "user", "content": "hi"}],
        "id": "slot-9",
        "stream": True,
        "temperature": 0.9,  # not ours to forward
        "tools": [{"x": 1}],  # nor this
        "system": "override me",  # nor this
    }
    body, _, _ = _forward_body(payload, "acme-support")
    assert set(body) == {"model", "messages", "id", "stream"}


@pytest.mark.parametrize("hostile", ["Default", "ACME-SUPPORT", "acme_support"])
def test_a_near_miss_name_does_not_address_the_crew(hostile: str) -> None:
    """Crew matching is exact. A case- or separator-variant is a different crew.

    Kiro Crew's agent names are case-sensitive, so accepting a variant here would
    forward a name the deployment did not install and land on the default again.
    """
    crew, refusal = judge_addressed_crew({"model": hostile}, "acme-support")
    assert crew is None
    assert refusal is not None and refusal.status_code == 404
