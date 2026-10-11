"""The argument shapes the real service refuses, pinned one test per refusal.

Every assertion here corresponds to a call the live service rejected during the
lane's first smoke run against a real account. They are grouped in one file
because they share a cause: **a loopback fake cannot see any of them.** The fake
accepts whatever string the lane hands it, and three of these four refusals come
from botocore's own model and never leave the host at all. So the local harness
was green on every one of them while the live call failed.

That is the lesson this file encodes, and it is why the assertions are on the
ARGV the lane builds rather than on a response: the thing that was wrong is what
the lane says, and the only local way to check what it says is to read it.
"""

from __future__ import annotations

import pytest

from kiro_crew.cloud.microvm import api


@pytest.fixture()
def captured(monkeypatch):
    """Capture the argv each call builds, and answer with a usable shape."""
    calls: list[list[str]] = []

    def fake(args, profile="", region="", *, action="", timeout=0):
        calls.append(list(args))
        if "run-microvm" in args or "get-microvm" in args:
            return {
                "microvmId": "mvm-1",
                "state": "PENDING",
                "endpoint": "https://mvm-1.example/",
                "imageArn": "arn:aws:lambda:us-east-1:1:microvm-image/x",
                "imageVersion": "1",
                "maximumDurationInSeconds": 900,
                "startedAt": "2026-10-06T00:00:00Z",
            }
        if "list-microvms" in args:
            return {"items": []}
        if "create-microvm-auth-token" in args:
            return {"authToken": {"token": "t"}}
        if "describe-activations" in args:
            return {"ActivationList": [{"ActivationId": "act-1", "RegistrationsCount": 0}]}
        return {}

    monkeypatch.setattr(api, "checked_json", fake)
    return calls


def _value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _values_after(argv: list[str], flag: str) -> list[str]:
    """Every consecutive non-flag argument following *flag*."""
    out: list[str] = []
    for item in argv[argv.index(flag) + 1 :]:
        if item.startswith("--"):
            break
        out.append(item)
    return out


class TestNetworkConnectorArn:
    """A bare connector NAME is refused; the value is a region-scoped ARN."""

    def test_the_arn_ends_in_the_mode(self):
        arn = api.connector_arn(api.INTERNET_EGRESS, "us-east-1")
        assert arn.endswith(f":{api.INTERNET_EGRESS}")
        assert arn.startswith("arn:aws:lambda:us-east-1:aws:network-connector:")

    def test_the_arn_carries_the_callers_region(self):
        """A hard-coded region would launch in that region whatever was asked."""
        assert ":us-west-2:" in api.connector_arn(api.NO_INGRESS, "us-west-2")
        assert ":eu-central-1:" in api.connector_arn(api.NO_INGRESS, "eu-central-1")

    def test_there_is_no_region_free_spelling(self):
        with pytest.raises(ValueError, match="needs a region"):
            api.connector_arn(api.NO_INGRESS, "")

    def test_the_run_hook_payload_never_reaches_the_argument_list(self, captured):
        """The payload carries this launch's SSM activation id and single-use CODE.

        An argument is in this process's argv, readable through
        ``/proc/<pid>/cmdline`` by anything on the machine that can list
        processes -- and the sandbox does not unshare the PID namespace. A reader
        of that code can register its OWN node under the hybrid activation before
        the guest; the registration limit is 1, so the real guest is locked out
        and ``wait_online`` returns the attacker's managed-instance id, after
        which every turn is tunnelled to them.
        """
        secret_payload = '{"ssm":{"id":"act-1","code":"single-use-activation-code"}}'
        seen: dict[str, object] = {}

        def capture(argv, profile="", region="", *, action="", timeout=0):
            seen["argv"] = list(argv)
            reference = _value_after(list(argv), "--run-hook-payload")
            path = reference[len("file://") :]
            with open(path, encoding="utf-8") as fh:
                seen["file_contents"] = fh.read()
            seen["mode"] = os.stat(path).st_mode & 0o777
            seen["path"] = path
            return {
                "microvmId": "mvm-1",
                "state": "RUNNING",
                "endpoint": "https://mvm-1.example.aws",
            }

        import os

        monkeypatch_target = api
        original = monkeypatch_target.checked_json
        monkeypatch_target.checked_json = capture  # type: ignore[assignment]
        try:
            api.run_microvm(
                image_identifier="arn:aws:lambda:us-east-1:1:microvm-image/x",
                image_version="1",
                run_hook_payload=secret_payload,
                maximum_duration_in_seconds=900,
                client_token="tok",
                region="us-east-1",
            )
        finally:
            monkeypatch_target.checked_json = original  # type: ignore[assignment]

        argv = seen["argv"]
        assert all(
            "single-use-activation-code" not in str(a) for a in argv
        ), "the activation code reached argv, where any process on the host can read it"
        assert _value_after(argv, "--run-hook-payload").startswith("file://")
        # The guest still receives the real payload, by file.
        assert seen["file_contents"] == secret_payload
        # The mode only where a mode means something: Windows honours just the
        # read-only bit, so chmod(0o600) leaves st_mode at 0o666 there and the
        # number says nothing about who can read the file. The property this test
        # exists for -- the activation code travels by file and never by argv --
        # is asserted above on every platform.
        if os.name == "posix":
            assert seen["mode"] == 0o600
        # And the file does not outlive the call.
        assert not os.path.exists(seen["path"])

    def test_run_microvm_sends_arns_and_not_bare_names(self, captured):
        """The refusal this pins: ``Malformed network connector ARN: INTERNET_EGRESS``."""
        api.run_microvm(
            image_identifier="arn:aws:lambda:us-east-1:1:microvm-image/x",
            image_version="1",
            run_hook_payload="{}",
            maximum_duration_in_seconds=900,
            client_token="tok",
            region="us-east-1",
        )
        argv = captured[0]
        ingress = _value_after(argv, "--ingress-network-connectors")
        egress = _value_after(argv, "--egress-network-connectors")
        for value in (ingress, egress):
            assert value.startswith("arn:aws:lambda:us-east-1:aws:network-connector:")
        assert ingress != api.NO_INGRESS
        assert egress != api.INTERNET_EGRESS

    def test_exactly_one_egress_connector_is_passed(self, captured):
        """The service refuses two, and the same one twice: it is a count check."""
        api.run_microvm(
            image_identifier="arn:aws:lambda:us-east-1:1:microvm-image/x",
            image_version="1",
            run_hook_payload="{}",
            maximum_duration_in_seconds=900,
            client_token="tok",
            region="us-east-1",
        )
        assert len(_values_after(captured[0], "--egress-network-connectors")) == 1


class TestListPageSize:
    """100 is refused outright, and the sweeper's only input is this list."""

    def test_the_page_size_is_the_services_own_maximum(self):
        assert api.MAX_LIST_RESULTS == 50

    def test_the_list_call_asks_for_that_page_size(self, captured):
        api.list_microvms(region="us-east-1")
        assert _value_after(captured[0], "--max-results") == "50"

    def test_the_page_size_is_inside_the_models_declared_bound(self):
        """Pinned against the model, not against a remembered number.

        ``maxResults`` declares ``{'min': 1, 'max': 50}``; a page size over it is
        refused before a page is ever returned, which reads to the sweeper as an
        unreadable list rather than as zero orphans.
        """
        botocore = pytest.importorskip("botocore")
        import botocore.session

        try:
            model = botocore.session.get_session().get_service_model("lambda-microvms")
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"the installed botocore does not know the service: {exc}")
        shape = model.operation_model("ListMicrovms").input_shape.members["maxResults"]
        declared_max = shape.metadata.get("max")
        if declared_max is None:
            pytest.skip("the installed model declares no maximum for maxResults")
        assert api.MAX_LIST_RESULTS == declared_max


class TestAuthTokenPorts:
    """``allowedPorts`` is a list of tagged unions, so a bare integer is refused."""

    def test_each_port_is_tagged(self, captured):
        api.create_auth_token(
            "mvm-1", allowed_ports=(8080, 5476), expiration_in_minutes=60, region="us-east-1"
        )
        assert _values_after(captured[0], "--allowed-ports") == ["port=8080", "port=5476"]

    def test_no_port_is_sent_as_a_bare_number(self, captured):
        """``Expected: '=', received: 'EOF' for input: 8080`` -- refused client-side."""
        api.create_auth_token(
            "mvm-1", allowed_ports=(8080,), expiration_in_minutes=60, region="us-east-1"
        )
        for value in _values_after(captured[0], "--allowed-ports"):
            assert "=" in value, value

    def test_an_empty_port_list_is_refused_before_the_call(self, captured):
        with pytest.raises(ValueError, match="at least one allowed port"):
            api.create_auth_token(
                "mvm-1", allowed_ports=(), expiration_in_minutes=60, region="us-east-1"
            )
        assert captured == []


class TestActivationFilterSpelling:
    """Two SSM calls in one lane, two different filter member names."""

    def test_describe_activations_uses_its_own_member_names(self, captured):
        api.describe_activation_registrations("act-1", region="us-east-1")
        value = _value_after(captured[0], "--filters")
        assert value == "FilterKey=ActivationIds,FilterValues=act-1"

    def test_it_does_not_use_the_other_calls_spelling(self, captured):
        """``key``/``value`` is refused: ``must be one of: FilterKey, FilterValues``."""
        api.describe_activation_registrations("act-1", region="us-east-1")
        value = _value_after(captured[0], "--filters")
        assert not value.startswith("key=")
        assert "value=" not in value

    def test_the_two_spellings_are_pinned_against_the_models(self):
        """The lane had them swapped, so both are checked against the real models.

        ``DescribeActivations`` takes ``FilterKey``/``FilterValues``;
        ``DescribeInstanceInformation`` takes ``Key``/``Values``. Nothing but the
        models decides which is which, so neither is asserted from memory.
        """
        botocore = pytest.importorskip("botocore")
        import botocore.session

        session = botocore.session.get_session()
        ssm = session.get_service_model("ssm")
        activations = ssm.operation_model("DescribeActivations").input_shape.members["Filters"]
        assert set(activations.member.members) == {"FilterKey", "FilterValues"}
        info = ssm.operation_model("DescribeInstanceInformation").input_shape.members["Filters"]
        assert set(info.member.members) == {"Key", "Values"}


class TestMicroVmReaders:
    """The two readers have two required sets, so neither guesses an endpoint."""

    def test_a_run_answer_is_read_with_its_endpoint(self):
        vm = api.MicroVm.from_response(
            {"microvmId": "mvm-9", "state": "RUNNING", "endpoint": "https://mvm-9/"}
        )
        assert vm.microvm_id == "mvm-9"
        assert vm.state == "RUNNING"
        assert vm.endpoint == "https://mvm-9/"

    def test_a_list_item_is_read_without_an_endpoint(self):
        vm = api.MicroVm.from_item({"microvmId": "mvm-9", "state": "RUNNING"})
        assert vm.microvm_id == "mvm-9"
        assert vm.endpoint == ""

    def test_a_non_object_response_is_refused(self):
        from kiro_crew.cloud.aws import AWSError

        with pytest.raises(AWSError, match="expected a MicroVM object"):
            api.MicroVm.from_response(["not", "a", "dict"])

    def test_a_response_missing_a_required_member_is_refused(self):
        """A ``GetMicrovm`` answer without an endpoint is one this lane rejects."""
        from kiro_crew.cloud.aws import AWSError

        with pytest.raises(AWSError, match="missing"):
            api.MicroVm.from_response({"microvmId": "mvm-9", "state": "RUNNING"})


class TestRunMicrovmValidation:
    """The launch refuses, before any call, every input that mints a bad VM."""

    def test_a_bare_image_name_is_refused(self, captured):
        with pytest.raises(ValueError, match="bare image name"):
            api.run_microvm(
                image_identifier="",
                image_version="1",
                run_hook_payload="{}",
                maximum_duration_in_seconds=900,
                client_token="tok",
                region="us-east-1",
            )
        assert captured == []

    def test_an_unset_image_version_is_refused(self, captured):
        """Unset means newest at call time, which a mid-launch build retargets."""
        with pytest.raises(ValueError, match="explicit image version"):
            api.run_microvm(
                image_identifier="arn:aws:lambda:us-east-1:1:microvm-image/x",
                image_version="",
                run_hook_payload="{}",
                maximum_duration_in_seconds=900,
                client_token="tok",
                region="us-east-1",
            )
        assert captured == []

    def test_a_missing_client_token_is_refused(self, captured):
        """Without it a retried request creates a second billing MicroVM."""
        with pytest.raises(ValueError, match="client token"):
            api.run_microvm(
                image_identifier="arn:aws:lambda:us-east-1:1:microvm-image/x",
                image_version="1",
                run_hook_payload="{}",
                maximum_duration_in_seconds=900,
                client_token="",
                region="us-east-1",
            )
        assert captured == []

    def test_a_payload_over_the_reference_limit_is_refused(self, captured):
        with pytest.raises(ValueError, match="over the"):
            api.run_microvm(
                image_identifier="arn:aws:lambda:us-east-1:1:microvm-image/x",
                image_version="1",
                run_hook_payload="x" * (api.MAX_RUN_HOOK_PAYLOAD_BYTES + 1),
                maximum_duration_in_seconds=900,
                client_token="tok",
                region="us-east-1",
            )
        assert captured == []

    def test_a_zero_duration_is_refused(self, captured):
        with pytest.raises(ValueError, match="between 1 and"):
            api.run_microvm(
                image_identifier="arn:aws:lambda:us-east-1:1:microvm-image/x",
                image_version="1",
                run_hook_payload="{}",
                maximum_duration_in_seconds=0,
                client_token="tok",
                region="us-east-1",
            )
        assert captured == []

    def test_a_duration_over_the_platform_maximum_is_refused(self, captured):
        """The maximum is not adjustable and the value may only be set downward."""
        with pytest.raises(ValueError, match="between 1 and"):
            api.run_microvm(
                image_identifier="arn:aws:lambda:us-east-1:1:microvm-image/x",
                image_version="1",
                run_hook_payload="{}",
                maximum_duration_in_seconds=api.MAX_LIFETIME_SECONDS + 1,
                client_token="tok",
                region="us-east-1",
            )
        assert captured == []

    def test_the_platform_maximum_is_eight_hours(self):
        assert api.MAX_LIFETIME_SECONDS == 28_800

    def test_the_idle_policy_disables_platform_auto_resume(self, captured):
        """Platform idle is inbound traffic only, so a port-forwarded crew looks idle."""
        import json

        api.run_microvm(
            image_identifier="arn:aws:lambda:us-east-1:1:microvm-image/x",
            image_version="1",
            run_hook_payload="{}",
            maximum_duration_in_seconds=900,
            client_token="tok",
            region="us-east-1",
        )
        policy = json.loads(_value_after(captured[0], "--idle-policy"))
        assert policy["autoResumeEnabled"] is False

    def test_the_optional_role_and_log_group_are_passed_when_given(self, captured):
        """Per-VM logging is a different setting from the image's build log group."""
        import json

        api.run_microvm(
            image_identifier="arn:aws:lambda:us-east-1:1:microvm-image/x",
            image_version="1",
            run_hook_payload="{}",
            maximum_duration_in_seconds=900,
            client_token="tok",
            execution_role_arn="arn:aws:iam::1:role/exec",
            log_group="/kirocrew/vm",
            region="us-east-1",
        )
        argv = captured[0]
        assert _value_after(argv, "--execution-role-arn") == "arn:aws:iam::1:role/exec"
        logging_cfg = json.loads(_value_after(argv, "--logging"))
        assert logging_cfg["cloudWatch"]["logGroup"] == "/kirocrew/vm"

    def test_a_launch_returns_the_state_the_service_answered_with(self, captured):
        """``PENDING`` on the first answer is the contract, not a slow path."""
        vm = api.run_microvm(
            image_identifier="arn:aws:lambda:us-east-1:1:microvm-image/x",
            image_version="1",
            run_hook_payload="{}",
            maximum_duration_in_seconds=900,
            client_token="tok",
            region="us-east-1",
        )
        assert vm.state == "PENDING"


class TestGetMicrovm:
    def test_a_get_reads_the_vm_by_identifier(self, captured):
        vm = api.get_microvm("mvm-1", region="us-east-1")
        assert vm.microvm_id == "mvm-1"
        assert _value_after(captured[0], "--microvm-identifier") == "mvm-1"


class TestListMicrovmsPagination:
    """The sweeper's only input, so a sweep must follow ``nextToken`` to the end."""

    def test_the_list_follows_the_next_token_across_pages(self, monkeypatch):
        pages = [
            {"items": [{"microvmId": "mvm-1", "state": "RUNNING"}], "nextToken": "t2"},
            {"items": [{"microvmId": "mvm-2", "state": "SUSPENDED"}]},
        ]
        seen_tokens: list[str] = []

        def fake(args, profile="", region="", *, action="", timeout=0):
            seen_tokens.append(
                args[args.index("--next-token") + 1] if "--next-token" in args else ""
            )
            return pages.pop(0)

        monkeypatch.setattr(api, "checked_json", fake)
        vms = api.list_microvms(region="us-east-1")
        assert [vm.microvm_id for vm in vms] == ["mvm-1", "mvm-2"]
        assert seen_tokens == ["", "t2"]

    def test_a_repeated_page_token_is_refused(self, monkeypatch):
        from kiro_crew.cloud.aws import AWSError

        monkeypatch.setattr(api, "checked_json", lambda *a, **k: {"items": [], "nextToken": "same"})
        with pytest.raises(AWSError, match="repeated a page token"):
            api.list_microvms(region="us-east-1")

    def test_a_non_object_list_response_is_refused(self, monkeypatch):
        from kiro_crew.cloud.aws import AWSError

        monkeypatch.setattr(api, "checked_json", lambda *a, **k: ["nope"])
        with pytest.raises(AWSError, match="expected an object"):
            api.list_microvms(region="us-east-1")


class TestLifecycleCalls:
    """terminate names its one identifier and nothing else."""

    def test_terminate_names_the_vm(self, captured):
        api.terminate_microvm("mvm-1", region="us-east-1")
        assert "terminate-microvm" in captured[0]
        assert _value_after(captured[0], "--microvm-identifier") == "mvm-1"


class TestAuthTokenResponse:
    def test_a_response_without_an_auth_token_map_is_refused(self, monkeypatch):
        from kiro_crew.cloud.aws import AWSError

        monkeypatch.setattr(api, "checked_json", lambda *a, **k: {"authToken": "not-a-map"})
        with pytest.raises(AWSError, match="no authToken map"):
            api.create_auth_token(
                "mvm-1", allowed_ports=(8080,), expiration_in_minutes=60, region="us-east-1"
            )

    def test_the_auth_token_map_is_returned_as_strings(self, captured):
        token = api.create_auth_token(
            "mvm-1", allowed_ports=(8080,), expiration_in_minutes=60, region="us-east-1"
        )
        assert token == {"token": "t"}


class TestMicrovmStatus:
    """``None`` and ``TERMINATED`` are different answers and both are useful."""

    def test_an_existing_vm_reports_its_state(self, captured):
        assert api.microvm_status("mvm-1", region="us-east-1") == "PENDING"

    def test_a_gone_vm_reports_none(self, monkeypatch):
        from kiro_crew.cloud.aws import AWSError

        def fake(args, profile="", region="", *, action="", timeout=0):
            raise AWSError("ResourceNotFoundException: no such microvm", action=action)

        monkeypatch.setattr(api, "checked_json", fake)
        assert api.microvm_status("mvm-gone", region="us-east-1") is None

    def test_any_other_error_propagates(self, monkeypatch):
        from kiro_crew.cloud.aws import AWSError

        def fake(args, profile="", region="", *, action="", timeout=0):
            raise AWSError("AccessDeniedException", action=action)

        monkeypatch.setattr(api, "checked_json", fake)
        with pytest.raises(AWSError):
            api.microvm_status("mvm-1", region="us-east-1")

    def test_a_not_found_is_matched_on_the_error_name(self):
        from kiro_crew.cloud.aws import AWSError

        assert api._is_not_found(AWSError("ResourceNotFoundException")) is True
        assert api._is_not_found(AWSError("ValidationException")) is False


class TestDescribeActivationRegistrations:
    def test_the_matching_activation_entry_is_returned(self, captured):
        entry = api.describe_activation_registrations("act-1", region="us-east-1")
        assert entry["RegistrationsCount"] == 0

    def test_a_non_object_response_reports_none(self, monkeypatch):
        monkeypatch.setattr(api, "checked_json", lambda *a, **k: ["nope"])
        assert api.describe_activation_registrations("act-1", region="us-east-1") is None

    def test_an_empty_activation_list_reports_none(self, monkeypatch):
        monkeypatch.setattr(api, "checked_json", lambda *a, **k: {"ActivationList": []})
        assert api.describe_activation_registrations("act-1", region="us-east-1") is None


class TestMicroVmImageReader:
    def test_an_image_is_read_from_its_response(self):
        image = api.MicroVmImage.from_response(
            {"imageArn": "arn:img", "state": "CREATED", "latestActiveImageVersion": "3"}
        )
        assert image.image_arn == "arn:img"
        assert image.launchable_version == "3"

    def test_an_image_with_no_active_version_is_not_launchable(self):
        """A build can finish CREATED and still produce nothing launchable."""
        image = api.MicroVmImage.from_response({"imageArn": "arn:img", "state": "CREATED"})
        assert image.launchable_version == ""

    def test_a_non_object_image_response_is_refused(self):
        from kiro_crew.cloud.aws import AWSError

        with pytest.raises(AWSError, match="expected a MicroVM image object"):
            api.MicroVmImage.from_response("nope")

    def test_an_image_response_missing_a_required_member_is_refused(self):
        from kiro_crew.cloud.aws import AWSError

        with pytest.raises(AWSError, match="missing"):
            api.MicroVmImage.from_response({"imageArn": "arn:img"})


class TestCreateMicrovmImage:
    """The recipe travels in the zip; the hook paths are the only channel in."""

    @pytest.fixture()
    def image_calls(self, monkeypatch):
        calls: list[list[str]] = []

        def fake(args, profile="", region="", *, action="", timeout=0):
            calls.append(list(args))
            return {"imageArn": "arn:img", "state": "CREATING"}

        monkeypatch.setattr(api, "checked_json", fake)
        return calls

    def _kwargs(self, **overrides):
        base = dict(
            name="kc-image",
            base_image_arn="arn:aws:lambda:us-east-1:aws:microvm-base-image/al2023",
            build_role_arn="arn:aws:iam::1:role/build",
            code_artifact_uri="s3://bucket/recipe.zip",
            cpu_architecture="arm64",
            minimum_memory_mib=2048,
            hook_port=9000,
            run_hook="/hooks/run",
            ready_hook="/hooks/ready",
            suspend_hook="/hooks/suspend",
            resume_hook="/hooks/resume",
            terminate_hook="/hooks/terminate",
            region="us-east-1",
        )
        base.update(overrides)
        return base

    def test_a_missing_required_field_is_refused(self, image_calls):
        with pytest.raises(ValueError, match="needs a name"):
            api.create_microvm_image(**self._kwargs(name=""))
        assert image_calls == []

    def test_a_recipe_uri_that_is_not_s3_is_refused(self, image_calls):
        with pytest.raises(ValueError, match="S3 URI"):
            api.create_microvm_image(**self._kwargs(code_artifact_uri="https://x/recipe.zip"))
        assert image_calls == []

    def test_the_run_hook_rides_in_the_runtime_hook_set(self, image_calls):
        """A run hook in the build set is a build that passes and a VM that never starts."""
        import json

        image = api.create_microvm_image(**self._kwargs())
        assert image.state == "CREATING"
        hooks = json.loads(_value_after(image_calls[0], "--hooks"))
        assert hooks["microvmHooks"]["run"] == "/hooks/run"
        assert hooks["microvmImageHooks"]["ready"] == "/hooks/ready"

    def test_the_egress_connector_is_an_arn_and_not_a_bare_name(self, image_calls):
        api.create_microvm_image(**self._kwargs())
        egress = _value_after(image_calls[0], "--egress-network-connectors")
        assert egress.startswith("arn:aws:lambda:us-east-1:aws:network-connector:")

    def test_environment_variables_and_tags_and_token_are_passed_when_given(self, image_calls):
        import json

        api.create_microvm_image(
            **self._kwargs(
                environment_variables={"KIROCREW_SKIP_MODEL_DOWNLOAD": "1"},
                tags={"crew": "kc-abc"},
                client_token="ct",
            )
        )
        argv = image_calls[0]
        assert json.loads(_value_after(argv, "--environment-variables")) == {
            "KIROCREW_SKIP_MODEL_DOWNLOAD": "1"
        }
        assert json.loads(_value_after(argv, "--tags")) == {"crew": "kc-abc"}
        assert _value_after(argv, "--client-token") == "ct"


class TestGetMicrovmImage:
    def test_an_image_is_returned_when_present(self, monkeypatch):
        monkeypatch.setattr(
            api, "checked_json", lambda *a, **k: {"imageArn": "arn:img", "state": "CREATED"}
        )
        image = api.get_microvm_image("arn:img", region="us-east-1")
        assert image is not None
        assert image.image_arn == "arn:img"

    def test_a_missing_image_reports_none(self, monkeypatch):
        from kiro_crew.cloud.aws import AWSError

        def fake(args, profile="", region="", *, action="", timeout=0):
            raise AWSError("ResourceNotFoundException", action=action)

        monkeypatch.setattr(api, "checked_json", fake)
        assert api.get_microvm_image("arn:gone", region="us-east-1") is None

    def test_any_other_error_propagates(self, monkeypatch):
        from kiro_crew.cloud.aws import AWSError

        def fake(args, profile="", region="", *, action="", timeout=0):
            raise AWSError("AccessDeniedException", action=action)

        monkeypatch.setattr(api, "checked_json", fake)
        with pytest.raises(AWSError):
            api.get_microvm_image("arn:img", region="us-east-1")


class TestListMicrovmImages:
    def test_the_list_follows_the_next_token_across_pages(self, monkeypatch):
        pages = [
            {"items": [{"imageArn": "arn:1", "state": "CREATED"}], "nextToken": "t2"},
            {"items": [{"imageArn": "arn:2", "state": "CREATED"}]},
        ]
        monkeypatch.setattr(api, "checked_json", lambda *a, **k: pages.pop(0))
        images = api.list_microvm_images(region="us-east-1")
        assert [img.image_arn for img in images] == ["arn:1", "arn:2"]

    def test_a_name_filter_is_passed_through(self, monkeypatch):
        calls: list[list[str]] = []

        def fake(args, profile="", region="", *, action="", timeout=0):
            calls.append(list(args))
            return {"items": []}

        monkeypatch.setattr(api, "checked_json", fake)
        api.list_microvm_images(name_filter="kc-image", region="us-east-1")
        assert _value_after(calls[0], "--name-filter") == "kc-image"

    def test_a_non_object_response_is_refused(self, monkeypatch):
        from kiro_crew.cloud.aws import AWSError

        monkeypatch.setattr(api, "checked_json", lambda *a, **k: ["nope"])
        with pytest.raises(AWSError, match="expected an object"):
            api.list_microvm_images(region="us-east-1")

    def test_a_repeated_page_token_is_refused(self, monkeypatch):
        from kiro_crew.cloud.aws import AWSError

        monkeypatch.setattr(api, "checked_json", lambda *a, **k: {"items": [], "nextToken": "same"})
        with pytest.raises(AWSError, match="repeated a page token"):
            api.list_microvm_images(region="us-east-1")


class TestImageVersionStatus:
    """``ACTIVE`` is the only status a launch may use."""

    def test_a_present_version_reports_its_status(self, monkeypatch):
        monkeypatch.setattr(api, "checked_json", lambda *a, **k: {"status": "ACTIVE"})
        assert api.image_version_status("arn:img", "3", region="us-east-1") == "ACTIVE"

    def test_a_missing_version_reports_none(self, monkeypatch):
        from kiro_crew.cloud.aws import AWSError

        def fake(args, profile="", region="", *, action="", timeout=0):
            raise AWSError("ResourceNotFoundException", action=action)

        monkeypatch.setattr(api, "checked_json", fake)
        assert api.image_version_status("arn:img", "9", region="us-east-1") is None


class TestPutRecipeObject:
    """Unconditional, because a recipe key is its content digest."""

    def test_a_missing_argument_is_refused(self, monkeypatch):
        monkeypatch.setattr(api, "checked_json", lambda *a, **k: {})
        with pytest.raises(ValueError, match="needs a bucket"):
            api.put_recipe_object("", "k", "/tmp/body", region="us-east-1")

    def test_a_cmk_upload_names_the_key(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            api, "checked_json", lambda args, *a, **k: calls.append(list(args)) or {}
        )
        api.put_recipe_object(
            "b", "k", "/tmp/body", kms_key_id="arn:aws:kms:::key/x", region="us-east-1"
        )
        argv = calls[0]
        assert _value_after(argv, "--server-side-encryption") == "aws:kms"
        assert _value_after(argv, "--ssekms-key-id") == "arn:aws:kms:::key/x"

    def test_a_keyless_upload_still_names_an_algorithm(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            api, "checked_json", lambda args, *a, **k: calls.append(list(args)) or {}
        )
        api.put_recipe_object("b", "k", "/tmp/body", region="us-east-1")
        assert _value_after(calls[0], "--server-side-encryption") == "AES256"


class TestPutSecret:
    """The value never reaches argv; it rides a 0600 file that is removed at once."""

    def test_a_secret_value_never_reaches_the_argument_list(self, monkeypatch):
        import os

        seen: dict[str, object] = {}

        def fake(args, profile="", region="", *, action="", timeout=0):
            assert all("super-secret-value" not in str(a) for a in args)
            secret_string = args[args.index("--secret-string") + 1]
            assert secret_string.startswith("file://")
            path = secret_string[len("file://") :]
            seen["path"] = path
            assert os.path.exists(path), "the value file must exist while the call runs"
            seen["mode"] = os.stat(path).st_mode & 0o777
            with open(path, encoding="utf-8") as fh:
                seen["content"] = fh.read()
            return {"ARN": "arn:aws:secretsmanager:us-east-1:1:secret:kc/abc"}

        monkeypatch.setattr(api, "checked_json", fake)
        arn = api.put_secret("kc/abc", "super-secret-value", region="us-east-1")
        assert arn.startswith("arn:aws:secretsmanager")
        assert seen["content"] == "super-secret-value"
        # The mode is asserted only where a mode means something. Windows honours
        # just the read-only bit, so ``chmod(0o600)`` leaves ``st_mode & 0o777``
        # at 0o666 there and the number says nothing about who can read the file.
        # The property this test exists for -- the value travels by file and never
        # by argv -- is checked above on every platform.
        if os.name == "posix":
            assert seen["mode"] == 0o600
        assert not os.path.exists(seen["path"]), "the value file must be removed after the call"

    def test_a_name_or_value_that_is_empty_is_refused(self, monkeypatch):
        monkeypatch.setattr(api, "checked_json", lambda *a, **k: {"ARN": "arn:x"})
        with pytest.raises(ValueError, match="needs a name and a value"):
            api.put_secret("kc/abc", "", region="us-east-1")

    def test_an_existing_secret_falls_back_to_put_secret_value(self, monkeypatch):
        from kiro_crew.cloud.aws import AWSError

        actions: list[str] = []

        def fake(args, profile="", region="", *, action="", timeout=0):
            actions.append(action)
            if action == "secretsmanager:CreateSecret":
                raise AWSError("ResourceExistsException: already there", action=action)
            return {"ARN": "arn:aws:secretsmanager:us-east-1:1:secret:kc/abc"}

        monkeypatch.setattr(api, "checked_json", fake)
        arn = api.put_secret("kc/abc", "v", region="us-east-1")
        assert actions == ["secretsmanager:CreateSecret", "secretsmanager:PutSecretValue"]
        assert arn.startswith("arn:aws:secretsmanager")

    def test_a_create_failure_that_is_not_already_exists_propagates(self, monkeypatch):
        import os

        from kiro_crew.cloud.aws import AWSError

        seen: dict[str, str] = {}

        def fake(args, profile="", region="", *, action="", timeout=0):
            seen["path"] = args[args.index("--secret-string") + 1][len("file://") :]
            raise AWSError("AccessDeniedException", action=action)

        monkeypatch.setattr(api, "checked_json", fake)
        with pytest.raises(AWSError):
            api.put_secret("kc/abc", "v", region="us-east-1")
        assert not os.path.exists(seen["path"]), "the value file is removed even on failure"

    def test_a_write_that_returns_no_arn_is_refused(self, monkeypatch):
        from kiro_crew.cloud.aws import AWSError

        monkeypatch.setattr(api, "checked_json", lambda *a, **k: {})
        with pytest.raises(AWSError, match="no ARN"):
            api.put_secret("kc/abc", "v", region="us-east-1")


class TestDeleteSecret:
    """A teardown path, so it names the secret to delete and never raises."""

    def test_it_schedules_a_recoverable_delete(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            api, "checked_json", lambda args, *a, **k: calls.append(list(args)) or {}
        )
        api.delete_secret("kc/abc", region="us-east-1")
        argv = calls[0]
        assert "delete-secret" in argv
        assert _value_after(argv, "--secret-id") == "kc/abc"
        assert "--force-delete-without-recovery" not in argv

    def test_a_failure_is_swallowed_so_the_teardown_continues(self, monkeypatch):
        def fake(args, profile="", region="", *, action="", timeout=0):
            raise RuntimeError("boom")

        monkeypatch.setattr(api, "checked_json", fake)
        api.delete_secret("kc/abc", region="us-east-1")
