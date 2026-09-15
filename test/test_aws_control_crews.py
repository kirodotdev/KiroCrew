"""Remote-crew inventory: what the console lists, and what it refuses to list.

Which account a listing runs against is settled before this module is reached: the
route resolves the requested id to a profile and re-probes the live identity,
refusing on a mismatch. So what is load-bearing HERE is the parsing -- an anchored
stack-name match, the live-state filter, and a wire shape the page reads field for
field, since a rename on either side is a silently blank card.
"""

from __future__ import annotations

import json
from unittest import mock

from kiro_crew.apps.builtins.aws_control.backend import crews

#: The account every fixture stack is served from. A real ``describe-stacks``
#: always returns ``StackId``, and the account is written into it, so a fixture
#: without one is not a response AWS could have produced -- and `list_crews`
#: refuses it, deliberately, rather than reading rows whose origin is unknown.
ACCOUNT = "111122223333"


def _arn(name: str, account: str = ACCOUNT) -> str:
    return f"arn:aws:cloudformation:us-west-2:{account}:stack/{name}/abc-123"


_BASE_STACK = {
    "StackName": "smc-base",
    "StackId": _arn("smc-base"),
    "StackStatus": "CREATE_COMPLETE",
}


def _crew_stack(
    name: str,
    *,
    memory: str = "chatbot",
    status: str = "CREATE_COMPLETE",
    account: str = ACCOUNT,
) -> dict:
    return {
        "StackName": f"smc-crew-{name}",
        "StackId": _arn(f"smc-crew-{name}", account),
        "StackStatus": status,
        "Parameters": [
            {"ParameterKey": "Memory", "ParameterValue": memory},
            {"ParameterKey": "ImageUri", "ParameterValue": f"repo/smc@sha256:{'a' * 64}"},
        ],
        "Outputs": [
            {"OutputKey": "ControlBaseUrl", "OutputValue": f"https://x.example/c/{name}"},
        ],
    }


def _service_arn(name: str, account: str = ACCOUNT) -> str:
    return f"arn:aws:ecs:us-west-2:{account}:service/smc/smc-{name}"


def _fake_aws(stacks: list[dict], *, counts=(1, 1), service_account: str = ACCOUNT):
    """Stand in for the CLI chokepoint, dispatching on the sub-command.

    The ECS reply carries ``serviceArn`` because the real ``--query`` asks for it:
    the response has to identify the account that served it, so a fake that
    omitted it would be testing a shape the code never sees.
    """

    def run_aws(args: list[str], profile: str, timeout: int = 30):
        if args[:2] == ["cloudformation", "describe-stacks"]:
            return 0, json.dumps({"Stacks": stacks}), ""
        if args[:2] == ["ecs", "describe-services"]:
            svc = args[args.index("--services") + 1] if "--services" in args else "smc-x"
            arn = _service_arn(svc.removeprefix("smc-"), service_account)
            return 0, json.dumps([arn, *counts]), ""
        raise AssertionError(f"unexpected aws call: {args[:2]}")

    return run_aws


def test_a_crew_stack_becomes_a_listed_crew() -> None:
    with mock.patch.object(
        crews.engine, "run_aws", side_effect=_fake_aws([_BASE_STACK, _crew_stack("baymax")])
    ):
        inv = crews.list_crews("p", "us-west-2", expect_account=ACCOUNT)
    assert [c.name for c in inv.crews] == ["baymax"]
    assert inv.crews[0].memory == "chatbot"
    assert inv.crews[0].stack == "smc-crew-baymax"
    assert inv.base_missing is False


def test_stacks_from_another_account_are_refused_not_listed() -> None:
    """The read must vouch for itself, not lean on a separate identity call.

    The route verifies the profile with its own ``aws`` process and this read runs
    in another, each resolving credentials independently. A source answering with
    a different account on the two invocations authorizes one and reads the other,
    and asking a third time does not settle it: a strictly alternating answer
    passes any before-and-after pair.

    So the account is taken from the response itself. Every stack carries its own
    ARN, the ARN carries the account that served it, and there is no second
    process whose answer could differ from the one that produced these rows.
    """
    foreign = [
        _crew_stack("base", account="999988887777"),
        _crew_stack("theirs", account="999988887777"),
    ]
    with mock.patch.object(crews.engine, "run_aws", side_effect=_fake_aws(foreign)):
        try:
            crews.list_crews("p", "us-west-2", expect_account=ACCOUNT)
        except crews.ForeignAccount as exc:
            # The message must not name the other account: the caller is not
            # entitled to learn which account they just failed to reach.
            assert "999988887777" not in str(exc)
        else:
            raise AssertionError("a read from another account was listed instead of refused")


def test_a_stack_whose_origin_cannot_be_read_is_refused() -> None:
    """An unreadable id says nothing about containment, so it cannot pass.

    A check that agrees when it learns nothing is the shape this whole surface
    exists to avoid: `list_crews` would return rows whose account was never
    established while reporting no problem at all.
    """
    for broken in ({}, {"StackId": ""}, {"StackId": "arn:aws:cloudformation"}):
        stack = {**_crew_stack("odd"), **broken}
        if not broken:
            stack.pop("StackId")
        with mock.patch.object(crews.engine, "run_aws", side_effect=_fake_aws([stack])):
            try:
                crews.list_crews("p", "us-west-2", expect_account=ACCOUNT)
            except crews.ForeignAccount:
                continue
            raise AssertionError(f"an unreadable origin was accepted: {broken!r}")


def test_a_service_from_another_account_is_refused_not_counted() -> None:
    """The second AWS call has to vouch for itself too.

    `list_crews` proving the STACKS came from the right account says nothing about
    the ECS read: that is a separate ``aws`` process resolving credentials again.
    Without an identity in its payload, another account's service supplies the
    running count on this crew's page, and the page reports it as this crew's
    state.
    """
    fake = _fake_aws(
        [_BASE_STACK, _crew_stack("baymax")], counts=(3, 3), service_account="999988887777"
    )
    with mock.patch.object(crews.engine, "run_aws", side_effect=fake):
        try:
            crews.describe_crew("p", "us-west-2", crew="baymax", expect_account=ACCOUNT)
        except crews.ForeignAccount as exc:
            assert "999988887777" not in str(exc)
        else:
            raise AssertionError("a service from another account was counted, not refused")


def test_a_stack_that_merely_contains_the_prefix_is_not_a_crew() -> None:
    """``_STACK_RE`` is anchored. A lookalike must not be read as a crew."""
    stacks = [
        _BASE_STACK,
        {
            "StackName": "not-smc-crew-evil",
            "StackId": _arn("not-smc-crew-evil"),
            "StackStatus": "CREATE_COMPLETE",
        },
        {
            "StackName": "smc-crew-" + "x" * 40,
            "StackId": _arn("smc-crew-" + "x" * 40),
            "StackStatus": "CREATE_COMPLETE",
        },
    ]
    with mock.patch.object(crews.engine, "run_aws", side_effect=_fake_aws(stacks)):
        inv = crews.list_crews("p", "us-west-2", expect_account=ACCOUNT)
    assert inv.crews == []


def test_a_missing_base_stack_is_reported_rather_than_inferred() -> None:
    """Without the base stack no crew can exist, and the UI says so explicitly."""
    with mock.patch.object(crews.engine, "run_aws", side_effect=_fake_aws([])):
        inv = crews.list_crews("p", "us-west-2", expect_account=ACCOUNT)
    assert inv.base_missing is True
    assert inv.crews == []


def test_a_crew_being_deleted_is_still_listed() -> None:
    """Hiding it is how a half-deleted crew becomes a surprise on the next bill."""
    stacks = [_BASE_STACK, _crew_stack("dying", status="DELETE_IN_PROGRESS")]
    with mock.patch.object(crews.engine, "run_aws", side_effect=_fake_aws(stacks)):
        inv = crews.list_crews("p", "us-west-2", expect_account=ACCOUNT)
    assert [c.name for c in inv.crews] == ["dying"]
    assert inv.crews[0].stack_status == "DELETE_IN_PROGRESS"


def test_the_list_view_makes_exactly_one_cli_call() -> None:
    """One ``describe-stacks``, and nothing else.

    Two separate costs are pinned by the same assertion. Fanning an ECS call out
    per crew would make the list slower for every crew nobody opened. And probing
    the caller's identity here would re-answer, in a third CLI process, a question
    the route already answered before this runs.

    MUTATION: re-add an ``sts get-caller-identity`` probe, or move the per-crew ECS
    call into the listing, and this reddens.
    """
    seen: list[str] = []

    def run_aws(args: list[str], profile: str, timeout: int = 30):
        seen.append(" ".join(args[:2]))
        return 0, json.dumps({"Stacks": [_BASE_STACK, _crew_stack("a"), _crew_stack("b")]}), ""

    with mock.patch.object(crews.engine, "run_aws", side_effect=run_aws):
        crews.list_crews("p", "us-west-2", expect_account=ACCOUNT)
    assert seen == ["cloudformation describe-stacks"], seen


def test_opening_a_crew_adds_its_service_state() -> None:
    fake = _fake_aws([_BASE_STACK, _crew_stack("baymax")], counts=(1, 1))
    with mock.patch.object(crews.engine, "run_aws", side_effect=fake):
        found = crews.describe_crew("p", "us-west-2", crew="baymax", expect_account=ACCOUNT)
    assert found is not None
    assert found.service == "smc-baymax"
    assert (found.running, found.desired) == (1, 1)


def test_a_crew_with_no_running_task_reports_the_shortfall() -> None:
    """The UI badges "not serving" off this pair, so the pair is what matters."""
    fake = _fake_aws([_BASE_STACK, _crew_stack("baymax")], counts=(0, 1))
    with mock.patch.object(crews.engine, "run_aws", side_effect=fake):
        found = crews.describe_crew("p", "us-west-2", crew="baymax", expect_account=ACCOUNT)
    assert found is not None and (found.running, found.desired) == (0, 1)


def test_a_list_payload_measures_no_serving_state() -> None:
    """A list makes no ECS call, so both counts stay 0 and the card reads status.

    MUTATION: fan an ECS call out per crew in ``list_crews`` and this reddens.
    """
    fake = _fake_aws([_BASE_STACK, _crew_stack("baymax")])
    with mock.patch.object(crews.engine, "run_aws", side_effect=fake):
        inv = crews.list_crews("p", "us-west-2", expect_account=ACCOUNT)
    assert (inv.crews[0].running, inv.crews[0].desired) == (0, 0)


def test_a_crew_scaled_to_zero_desires_nothing() -> None:
    """Zero desired is not a fault. The UI reads it as idle, not as not-serving."""
    fake = _fake_aws([_BASE_STACK, _crew_stack("parked")], counts=(0, 0))
    with mock.patch.object(crews.engine, "run_aws", side_effect=fake):
        found = crews.describe_crew("p", "us-west-2", crew="parked", expect_account=ACCOUNT)
    assert found is not None and found.desired == 0


def test_opening_a_crew_that_does_not_exist_returns_none() -> None:
    fake = _fake_aws([_BASE_STACK])
    with mock.patch.object(crews.engine, "run_aws", side_effect=fake):
        assert crews.describe_crew("p", "us-west-2", crew="ghost", expect_account=ACCOUNT) is None


def test_the_wire_shape_carries_every_field_the_ui_reads() -> None:
    """types.ts and this dict are one interface; a rename here breaks the page."""
    fake = _fake_aws([_BASE_STACK, _crew_stack("baymax")])
    with mock.patch.object(crews.engine, "run_aws", side_effect=fake):
        payload = crews.to_json(crews.list_crews("p", "us-west-2", expect_account=ACCOUNT))
    assert set(payload) == {"baseMissing", "crews"}
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
    }


def test_mode_comes_from_the_stack_not_from_a_guess() -> None:
    """A crew whose template says persistent must not read as chatbot."""
    fake = _fake_aws([_BASE_STACK, _crew_stack("keeps", memory="persistent")])
    with mock.patch.object(crews.engine, "run_aws", side_effect=fake):
        inv = crews.list_crews("p", "us-west-2", expect_account=ACCOUNT)
    assert inv.crews[0].memory == "persistent"


def test_a_stack_predating_the_memory_parameter_reports_an_empty_mode() -> None:
    """Absent is not chatbot. The UI must be able to say it does not know."""
    stack = _crew_stack("old")
    stack["Parameters"] = [p for p in stack["Parameters"] if p["ParameterKey"] != "Memory"]
    with mock.patch.object(crews.engine, "run_aws", side_effect=_fake_aws([_BASE_STACK, stack])):
        inv = crews.list_crews("p", "us-west-2", expect_account=ACCOUNT)
    assert inv.crews[0].memory == ""
