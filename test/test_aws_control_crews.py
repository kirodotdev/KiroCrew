"""Remote-crew inventory: what the console lists, and what it refuses to list.

The account binding is the load-bearing test here. ``profile`` is a name a child
CLI process resolves, so a profile repointed from account A to account B would let
a request for A's crews report B's. That is a disclosure, not an error, and it is
why every listing re-derives the account through the same profile before it reports
anything.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from kiro_crew.apps.builtins.aws_control.backend import crews

ACCOUNT = "111122223333"

_BASE_STACK = {"StackName": "smc-base", "StackStatus": "CREATE_COMPLETE"}


def _crew_stack(name: str, *, memory: str = "chatbot", status: str = "CREATE_COMPLETE") -> dict:
    return {
        "StackName": f"smc-crew-{name}",
        "StackStatus": status,
        "Parameters": [
            {"ParameterKey": "Memory", "ParameterValue": memory},
            {"ParameterKey": "ImageUri", "ParameterValue": f"repo/smc@sha256:{'a' * 64}"},
        ],
        "Outputs": [
            {"OutputKey": "ControlBaseUrl", "OutputValue": f"https://x.example/c/{name}"},
        ],
    }


def _fake_aws(stacks: list[dict], *, account: str = ACCOUNT, counts=(1, 1)):
    """Stand in for the CLI chokepoint, dispatching on the sub-command."""

    def run_aws(args: list[str], profile: str, timeout: int = 30):
        if args[:2] == ["sts", "get-caller-identity"]:
            return 0, account + "\n", ""
        if args[:2] == ["cloudformation", "describe-stacks"]:
            return 0, json.dumps({"Stacks": stacks}), ""
        if args[:2] == ["ecs", "describe-services"]:
            return 0, json.dumps(list(counts)), ""
        raise AssertionError(f"unexpected aws call: {args[:2]}")

    return run_aws


def test_a_crew_stack_becomes_a_listed_crew() -> None:
    with mock.patch.object(
        crews.engine, "run_aws", side_effect=_fake_aws([_BASE_STACK, _crew_stack("baymax")])
    ):
        inv = crews.list_crews("p", "us-west-2", account=ACCOUNT)
    assert [c.name for c in inv.crews] == ["baymax"]
    assert inv.crews[0].memory == "chatbot"
    assert inv.crews[0].stack == "smc-crew-baymax"
    assert inv.base_missing is False


def test_the_listing_refuses_when_the_profile_resolves_elsewhere() -> None:
    """The disclosure guard. MUTATION: drop _assert_account and this reddens."""
    fake = _fake_aws([_BASE_STACK, _crew_stack("baymax")], account="999988887777")
    with mock.patch.object(crews.engine, "run_aws", side_effect=fake):
        with pytest.raises(crews.AccountMismatch):
            crews.list_crews("p", "us-west-2", account=ACCOUNT)


def test_a_stack_that_merely_contains_the_prefix_is_not_a_crew() -> None:
    """``_STACK_RE`` is anchored. A lookalike must not be read as a crew."""
    stacks = [
        _BASE_STACK,
        {"StackName": "not-smc-crew-evil", "StackStatus": "CREATE_COMPLETE"},
        {
            "StackName": "smc-crew-" + "x" * 40,
            "StackStatus": "CREATE_COMPLETE",
        },
    ]
    with mock.patch.object(crews.engine, "run_aws", side_effect=_fake_aws(stacks)):
        inv = crews.list_crews("p", "us-west-2", account=ACCOUNT)
    assert inv.crews == []


def test_a_missing_base_stack_is_reported_rather_than_inferred() -> None:
    """Without the base stack no crew can exist, and the UI says so explicitly."""
    with mock.patch.object(crews.engine, "run_aws", side_effect=_fake_aws([])):
        inv = crews.list_crews("p", "us-west-2", account=ACCOUNT)
    assert inv.base_missing is True
    assert inv.crews == []


def test_a_crew_being_deleted_is_still_listed() -> None:
    """Hiding it is how a half-deleted crew becomes a surprise on the next bill."""
    stacks = [_BASE_STACK, _crew_stack("dying", status="DELETE_IN_PROGRESS")]
    with mock.patch.object(crews.engine, "run_aws", side_effect=_fake_aws(stacks)):
        inv = crews.list_crews("p", "us-west-2", account=ACCOUNT)
    assert [c.name for c in inv.crews] == ["dying"]
    assert inv.crews[0].stack_status == "DELETE_IN_PROGRESS"


def test_the_list_view_makes_no_per_crew_ecs_call() -> None:
    """A list that fanned out N calls would be slow for every crew nobody opened."""
    seen: list[str] = []

    def run_aws(args: list[str], profile: str, timeout: int = 30):
        seen.append(" ".join(args[:2]))
        if args[:2] == ["sts", "get-caller-identity"]:
            return 0, ACCOUNT, ""
        return 0, json.dumps({"Stacks": [_BASE_STACK, _crew_stack("a"), _crew_stack("b")]}), ""

    with mock.patch.object(crews.engine, "run_aws", side_effect=run_aws):
        crews.list_crews("p", "us-west-2", account=ACCOUNT)
    assert "ecs describe-services" not in seen, seen


def test_opening_a_crew_adds_its_service_state() -> None:
    fake = _fake_aws([_BASE_STACK, _crew_stack("baymax")], counts=(1, 1))
    with mock.patch.object(crews.engine, "run_aws", side_effect=fake):
        found = crews.describe_crew("p", "us-west-2", account=ACCOUNT, crew="baymax")
    assert found is not None
    assert found.service == "smc-baymax"
    assert (found.running, found.desired) == (1, 1)
    assert found.healthy is True


def test_a_crew_with_no_running_task_is_not_healthy() -> None:
    fake = _fake_aws([_BASE_STACK, _crew_stack("baymax")], counts=(0, 1))
    with mock.patch.object(crews.engine, "run_aws", side_effect=fake):
        found = crews.describe_crew("p", "us-west-2", account=ACCOUNT, crew="baymax")
    assert found is not None and found.healthy is False


def test_health_is_unknown_on_a_list_payload_rather_than_false() -> None:
    """A list makes no ECS call, so a boolean would say every crew is down.

    MUTATION: return ``self.desired > 0 and self.running == self.desired`` and this
    reddens. Found by the UI track, which had to work around the false value.
    """
    fake = _fake_aws([_BASE_STACK, _crew_stack("baymax")])
    with mock.patch.object(crews.engine, "run_aws", side_effect=fake):
        inv = crews.list_crews("p", "us-west-2", account=ACCOUNT)
    assert inv.crews[0].healthy is None, "a list payload claimed to know the health"


def test_a_crew_scaled_to_zero_is_unknown_not_unhealthy() -> None:
    """Nothing is faulty and nothing was asked for. False would blame the crew."""
    fake = _fake_aws([_BASE_STACK, _crew_stack("parked")], counts=(0, 0))
    with mock.patch.object(crews.engine, "run_aws", side_effect=fake):
        found = crews.describe_crew("p", "us-west-2", account=ACCOUNT, crew="parked")
    assert found is not None and found.healthy is None


def test_opening_a_crew_that_does_not_exist_returns_none() -> None:
    fake = _fake_aws([_BASE_STACK])
    with mock.patch.object(crews.engine, "run_aws", side_effect=fake):
        assert crews.describe_crew("p", "us-west-2", account=ACCOUNT, crew="ghost") is None


def test_the_wire_shape_carries_every_field_the_ui_reads() -> None:
    """types.ts and this dict are one interface; a rename here breaks the page."""
    fake = _fake_aws([_BASE_STACK, _crew_stack("baymax")])
    with mock.patch.object(crews.engine, "run_aws", side_effect=fake):
        payload = crews.to_json(crews.list_crews("p", "us-west-2", account=ACCOUNT))
    assert set(payload) == {"account", "region", "baseMissing", "crews"}
    assert set(payload["crews"][0]) == {
        "name",
        "stack",
        "stackStatus",
        "memory",
        "service",
        "running",
        "desired",
        "image",
        "controlBase",
        "region",
        "healthy",
    }


def test_mode_comes_from_the_stack_not_from_a_guess() -> None:
    """A crew whose template says persistent must not read as chatbot."""
    fake = _fake_aws([_BASE_STACK, _crew_stack("keeps", memory="persistent")])
    with mock.patch.object(crews.engine, "run_aws", side_effect=fake):
        inv = crews.list_crews("p", "us-west-2", account=ACCOUNT)
    assert inv.crews[0].memory == "persistent"


def test_a_stack_predating_the_memory_parameter_reports_an_empty_mode() -> None:
    """Absent is not chatbot. The UI must be able to say it does not know."""
    stack = _crew_stack("old")
    stack["Parameters"] = [p for p in stack["Parameters"] if p["ParameterKey"] != "Memory"]
    with mock.patch.object(crews.engine, "run_aws", side_effect=_fake_aws([_BASE_STACK, stack])):
        inv = crews.list_crews("p", "us-west-2", account=ACCOUNT)
    assert inv.crews[0].memory == ""
