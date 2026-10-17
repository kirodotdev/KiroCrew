"""Pin this lane's assumptions against botocore's own ``lambda-microvms`` model.

A fake control plane can test everything except whether it still resembles the
service. This is the test that notices an SDK bump changing the API: it reads the
INSTALLED service model and asserts the routes, the required members and the state
enum this lane is written against are still what the model declares.

No AWS call, no credential, no endpoint: ``get_service_model`` reads a JSON
document out of the installed botocore. The reference implementation for this lane
had to bundle that document as a deployment asset, because the runtime it ran on
did not know the service -- a local control plane does not have that problem, and
this test is what turns that into coverage rather than luck.

Skipped, not failed, when botocore is absent or too old to know the service: the
suite must still run for a contributor who did not install the dev group's AWS
dependencies, and a skip here cannot be mistaken for a pass because the skip
reason names the missing piece.
"""

from __future__ import annotations

import pytest

from kiro_crew.cloud.microvm import api

botocore = pytest.importorskip(
    "botocore", reason="botocore is not installed, so there is no service model to read"
)

_SERVICE = "lambda-microvms"


@pytest.fixture(scope="module")
def model():
    """The installed model, or a hard FAILURE when the install contradicts the pin.

    The skip above is legitimate: a contributor without the dev group's AWS
    dependencies has no model to read. Reaching this point and finding botocore
    present but ignorant of the service is a different thing, and skipping on it
    would be the "green while checking nothing" shape -- the whole value of this
    file is noticing an SDK that stopped matching, and an SDK too old to know the
    service at all is the most extreme case of that.

    It happens for a real reason worth naming in the message: the crew image's
    own ``container/requirements.txt`` pins an older ``boto3``, so a venv that
    installed both the dev group and the image's runtime pins ends up on the
    older one. CI keeps them apart (only the crew-container lane installs the
    image pins, and it runs only ``container_tests/``), so this fails loudly for
    a local environment rather than quietly for everyone.
    """
    import botocore.session

    session = botocore.session.get_session()
    try:
        return session.get_service_model(_SERVICE)
    except Exception as exc:  # noqa: BLE001 - UnknownServiceError and friends
        pytest.fail(
            f"botocore {botocore.__version__} is installed but does not know {_SERVICE}, so "
            f"this contract cannot be checked ({type(exc).__name__}). The dev group pins a "
            "version that does know it; the crew image's container/requirements.txt pins an "
            "older boto3, and a venv carrying both ends up on the older one. Reinstall the "
            "dev group's pin in this environment, or run this file in a venv without the "
            "image's runtime pins."
        )


#: ``(operation, HTTP method, request URI)``, as this lane's CLI calls assume.
_ROUTES = (
    ("RunMicrovm", "POST", "/2025-09-09/microvms"),
    ("GetMicrovm", "GET", "/2025-09-09/microvms/{microvmIdentifier}"),
    ("ListMicrovms", "GET", "/2025-09-09/microvms"),
    ("SuspendMicrovm", "POST", "/2025-09-09/microvms/{microvmIdentifier}/suspend"),
    ("ResumeMicrovm", "POST", "/2025-09-09/microvms/{microvmIdentifier}/resume"),
    ("TerminateMicrovm", "DELETE", "/2025-09-09/microvms/{microvmIdentifier}"),
    (
        "CreateMicrovmAuthToken",
        "POST",
        "/2025-09-09/microvms/{microvmIdentifier}/auth-token",
    ),
)


class TestServiceShape:
    def test_the_api_version_is_the_one_the_routes_are_written_against(self, model):
        assert model.api_version == "2025-09-09"

    def test_the_iam_prefix_is_lambda_and_not_the_cli_service_name(self, model):
        """A policy written against ``lambda-microvms:`` grants nothing, and the
        CLI help does not say so anywhere."""
        assert model.endpoint_prefix == "lambda"

    @pytest.mark.parametrize("operation,method,uri", _ROUTES)
    def test_each_route_is_where_this_lane_expects_it(self, model, operation, method, uri):
        op = model.operation_model(operation)
        assert op.http["method"] == method
        assert op.http["requestUri"] == uri


class TestRunMicrovm:
    def test_the_image_identifier_is_the_only_required_input(self, model):
        op = model.operation_model("RunMicrovm")
        assert set(op.input_shape.required_members) == {"imageIdentifier"}

    def test_every_argument_this_lane_sends_is_a_declared_member(self, model):
        """The reference implementation's own incident was a camelCase slip here."""
        members = set(model.operation_model("RunMicrovm").input_shape.members)
        for sent in (
            "imageIdentifier",
            "imageVersion",
            "ingressNetworkConnectors",
            "egressNetworkConnectors",
            "runHookPayload",
            "maximumDurationInSeconds",
            "clientToken",
            "idlePolicy",
            "executionRoleArn",
        ):
            assert sent in members, sent

    def test_the_endpoint_is_a_required_output(self, model):
        """So there is no code here for a VM without one: guessing an endpoint is
        how a caller reaches someone else's VM."""
        required = set(model.operation_model("RunMicrovm").output_shape.required_members)
        for member in ("microvmId", "state", "endpoint", "startedAt"):
            assert member in required, member

    def test_insufficient_capacity_is_a_declared_error(self, model):
        """A fake can only assert our handling; the error's existence is the model's."""
        names = {shape.name for shape in model.operation_model("RunMicrovm").error_shapes}
        assert "InsufficientCapacityException" in names
        assert "ServiceQuotaExceededException" in names

    def test_the_run_hook_payload_bound_matches_the_model(self, model):
        """The service's prose says 16,384 and its own constraint says 4,096.
        Budget against the smaller one, because the larger is refused at launch."""
        shape = model.operation_model("RunMicrovm").input_shape.members["runHookPayload"]
        declared = shape.metadata.get("max")
        if declared is None:
            pytest.skip("the installed model declares no maximum for runHookPayload")
        assert api.MAX_RUN_HOOK_PAYLOAD_BYTES == declared

    def test_the_lifetime_ceiling_matches_the_model(self, model):
        shape = model.operation_model("RunMicrovm").input_shape.members["maximumDurationInSeconds"]
        declared = shape.metadata.get("max")
        if declared is None:
            pytest.skip("the installed model declares no maximum lifetime")
        assert api.MAX_LIFETIME_SECONDS == declared


class TestStateEnum:
    def test_the_states_this_lane_knows_are_the_states_the_model_declares(self, model):
        state = model.operation_model("GetMicrovm").output_shape.members["state"]
        enum = tuple(state.metadata.get("enum") or state.enum)
        assert tuple(api.MICROVM_STATES) == enum

    def test_terminated_is_in_the_enum(self, model):
        state = model.operation_model("GetMicrovm").output_shape.members["state"]
        assert "TERMINATED" in set(state.metadata.get("enum") or state.enum)
        assert api.TERMINAL_MICROVM_STATES <= set(api.MICROVM_STATES)


class TestAuthToken:
    def test_the_port_scope_and_the_expiry_are_both_required(self, model):
        """The port list is the one control the service enforces on the endpoint."""
        op = model.operation_model("CreateMicrovmAuthToken")
        required = set(op.input_shape.required_members)
        assert {"allowedPorts", "expirationInMinutes", "microvmIdentifier"} == required

    def test_the_token_comes_back_as_a_map(self, model):
        op = model.operation_model("CreateMicrovmAuthToken")
        assert "authToken" in op.output_shape.required_members

    def test_a_port_entry_is_a_TAGGED_UNION_and_not_a_number(self, model):
        """Why a bare integer is refused before the request ever leaves the host.

        The lane sent ``8080`` and the CLI answered ``Expected: '=', received:
        'EOF' for input: 8080``, which is botocore parsing each element as the
        shorthand form of a structure. Pinned against the model rather than against
        the error text, because the error is what a wrong shape produces and the
        model is what makes it wrong: ``allowedPorts`` is a list whose member is a
        union, so an element has to name which arm it is.
        """
        member = (
            model.operation_model("CreateMicrovmAuthToken")
            .input_shape.members["allowedPorts"]
            .member
        )
        assert member.type_name == "structure"
        assert member.is_tagged_union is True

    def test_the_arms_the_lane_may_name_are_the_models(self, model):
        """``port`` is the arm the lane uses; the other two exist and are not used.

        Asserted so a future change that reaches for a range or an all-ports token
        finds the arm names here rather than guessing them, and so an SDK bump that
        renames one reds this test instead of a live launch.
        """
        member = (
            model.operation_model("CreateMicrovmAuthToken")
            .input_shape.members["allowedPorts"]
            .member
        )
        assert set(member.members) == {"port", "range", "allPorts"}
        assert member.members["port"].type_name == "integer"

    def test_the_lane_names_the_port_arm_on_every_entry(self, model):
        """The shape check and the lane's own argv, in one place.

        A union member is only valid when it names an arm, so this pins that what
        the lane builds satisfies what the model above declares -- the two halves
        the local harness could not connect, because it accepted any string.
        """
        member = (
            model.operation_model("CreateMicrovmAuthToken")
            .input_shape.members["allowedPorts"]
            .member
        )
        arm = "port"
        assert arm in member.members
        captured: list[list[str]] = []

        def fake(args, profile="", region="", *, action="", timeout=0):
            captured.append(list(args))
            return {"authToken": {"token": "t"}}

        original = api.checked_json
        api.checked_json = fake  # type: ignore[assignment]
        try:
            api.create_auth_token(
                "mvm-1",
                allowed_ports=(8080, 5476),
                expiration_in_minutes=60,
                region="us-east-1",
            )
        finally:
            api.checked_json = original  # type: ignore[assignment]
        argv = captured[0]
        sent = []
        for item in argv[argv.index("--allowed-ports") + 1 :]:
            if item.startswith("--"):
                break
            sent.append(item)
        assert sent == [f"{arm}=8080", f"{arm}=5476"]


class TestListPagination:
    def test_the_list_is_paginated(self, model):
        """A sweep that stopped at page one would report a leaking VM as reaped."""
        op = model.operation_model("ListMicrovms")
        assert "nextToken" in op.input_shape.members
        assert "nextToken" in op.output_shape.members
        assert "items" in op.output_shape.required_members

    def test_a_list_item_carries_no_endpoint(self, model):
        """So the list is parsed by its OWN reader.

        The item shape is a summary: five members, none of them the endpoint. A
        reader that required an endpoint here would refuse every page of the list
        -- which is the sweeper's only input -- while looking like a strictness
        improvement.
        """
        item = model.operation_model("ListMicrovms").output_shape.members["items"].member
        assert "endpoint" not in item.members
        assert "maximumDurationInSeconds" not in item.members

    def test_a_list_item_carries_what_the_sweeper_reads(self, model):
        item = model.operation_model("ListMicrovms").output_shape.members["items"].member
        for member in ("microvmId", "state", "startedAt"):
            assert member in item.required_members, member

    def test_the_two_readers_require_what_their_shapes_require(self, model):
        """Pins the split in the code against the split in the model."""
        get_required = set(model.operation_model("GetMicrovm").output_shape.required_members)
        assert "endpoint" in get_required
        vm = api.MicroVm.from_item(
            {"microvmId": "mvm-1", "state": "RUNNING", "startedAt": "2026-10-06T00:00:00Z"}
        )
        assert vm.endpoint == ""
        with pytest.raises(Exception, match="endpoint"):
            api.MicroVm.from_response({"microvmId": "mvm-1", "state": "RUNNING"})
