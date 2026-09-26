"""The ``RunTask`` request: what a launch may override, and what it may not carry.

``ContainerOverride`` has no ``secrets`` field, so the request is the one place
the credential decision can be undone. The assertions below walk the produced
request rather than reading the fields a defect was once found in.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pathlib
import re

import pytest

from kiro_crew.cloud import fargate as fargate_package
from kiro_crew.cloud.fargate import identity as ident
from kiro_crew.cloud.fargate import runtask as rt
from kiro_crew.cloud.fargate import taskdef as td
from kiro_crew.cloud.fargate.identity import CrewBinding, DocumentRefused, SecretRef
from kiro_crew.cloud.iam import MANAGED_TAG_KEY

ACCOUNT = "123456789012"
REGION = "us-east-1"
BINDING = CrewBinding(partition="aws", account=ACCOUNT, crew="frontdesk")
IMAGE = f"repo/kirocrew-crew@sha256:{'d' * 64}"
SECOND_SECRET = "EXTRA_TOKEN"


def other_crew_ref(crew: str, key: str) -> SecretRef:
    """A reference naming a crew other than the fixture's, for the crossing tests."""
    name = f"kirocrew/crew/{crew}/{key}"
    return SecretRef(
        name=name,
        arn=f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{name}-AbCdEf",
    )


def secret_ref(key: str) -> SecretRef:
    """A secret named twice: canonically, and as the ARN that resolves to it."""
    name = f"kirocrew/crew/{BINDING.crew}/{key}"
    return SecretRef(
        name=name,
        arn=f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{name}-AbCdEf",
    )


TASKDEF = td.TaskDefinitionSpec(
    image=IMAGE,
    secrets=[secret_ref(td.MODEL_CREDENTIAL_ENV), secret_ref(SECOND_SECRET)],
    cpu_architecture="ARM64",
    log=td.default_log_spec(REGION),
    # A RunTask request carries no volume or mount point at all, so the store cannot
    # change what this module produces. Stated as absent to say so, rather than to
    # describe a launch anyone would make.
    store=None,
)

PLACEMENT = rt.Placement(cluster="crews", subnets=("subnet-a",), security_groups=("sg-a",))
SIZE = rt.TaskSize(cpu="2048", memory="8192")


def request(**overrides) -> dict:
    base = dict(
        revision=12,
        taskdef=TASKDEF,
        placement=PLACEMENT,
        size=SIZE,
        launch_tag="run-1",
    )
    base.update(overrides)
    return rt.run_task_request(**base)  # type: ignore[arg-type]


def strings(node):
    """Every string anywhere in a request, so a check cannot miss a position."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from strings(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from strings(value)


ARGUMENT_SHAPES = [
    {},
    {"environment": {"KIROCREW_TELEMETRY_DISABLED": "1"}},
    {"started_by": "kirocrew-launch"},
    {"size": rt.TaskSize(cpu="1024", memory="4096", ephemeral_storage_gib=50)},
    {
        "environment": {"A": "1", "B": "2"},
        "started_by": "x",
        "size": rt.TaskSize(cpu="256", memory="512", ephemeral_storage_gib=21),
    },
]


@pytest.mark.parametrize("shape", ARGUMENT_SHAPES)
def test_the_override_never_carries_a_key_outside_the_allowlist(shape):
    produced = request(**shape)
    assert set(produced["overrides"]) <= rt.TASK_OVERRIDE_KEYS
    for container_override in produced["overrides"]["containerOverrides"]:
        assert set(container_override) <= rt.CONTAINER_OVERRIDE_KEYS


@pytest.mark.parametrize("shape", ARGUMENT_SHAPES)
def test_no_role_is_named_anywhere_in_the_request(shape):
    """Overriding the execution role would reopen the hole the document closes."""
    produced = request(**shape)
    assert "executionRoleArn" not in set(strings(produced))
    assert "taskRoleArn" not in set(strings(produced))
    assert not [text for text in strings(produced) if ":role/" in text]


@pytest.mark.parametrize("shape", ARGUMENT_SHAPES)
def test_no_secret_reference_is_named_anywhere_in_the_request(shape):
    """The credential travels in the definition, so the request never mentions it."""
    produced = request(**shape)
    assert not [text for text in strings(produced) if "secretsmanager" in text]
    assert td.MODEL_CREDENTIAL_ENV not in set(strings(produced))


def test_a_planted_key_in_a_produced_override_is_refused():
    """The allowlist reads the output, so a future edit cannot widen it silently."""
    with pytest.raises(DocumentRefused, match="outside the set"):
        rt._refuse_keys_outside(
            {"cpu": "256", "executionRoleArn": "x"}, rt.TASK_OVERRIDE_KEYS, "overrides"
        )


@pytest.mark.parametrize(
    "size",
    [
        rt.TaskSize(cpu="", memory="8192"),
        rt.TaskSize(cpu="2048", memory=""),
        rt.TaskSize(cpu="0", memory="8192"),
        rt.TaskSize(cpu="2048", memory="0"),
        rt.TaskSize(cpu="2 vCPU", memory="8192"),
        rt.TaskSize(cpu="2048", memory="8 GB"),
        rt.TaskSize(cpu="-1", memory="8192"),
        rt.TaskSize(cpu="2048", memory="8192", ephemeral_storage_gib=0),
        rt.TaskSize(cpu="2048", memory="8192", ephemeral_storage_gib=-5),
    ],
)
def test_a_launch_that_states_no_usable_size_is_refused(size):
    """The registration floor satisfies the API and is never a runtime shape."""
    with pytest.raises(DocumentRefused):
        request(size=size)


def test_omitting_the_size_argument_is_not_possible():
    with pytest.raises(TypeError):
        rt.run_task_request(  # type: ignore[call-arg]
            revision=1,
            taskdef=TASKDEF,
            placement=PLACEMENT,
            launch_tag="run-1",
        )


def test_the_definition_that_runs_is_the_one_the_spec_determines():
    """The family is derived, so the request and the refusals read one definition."""
    assert request()["taskDefinition"] == f"{ident.task_family(BINDING)}:12"


def test_the_family_cannot_be_named_by_the_caller():
    """A free identifier would sit outside every refusal, which all read the spec.

    Crew A's spec paired with crew B's identifier would run B, be tagged A, and
    pass each check, because each reads the argument that is not in charge.
    """
    parameters = set(inspect.signature(rt.run_task_request).parameters)
    assert "task_definition" not in parameters
    assert "family" not in parameters
    assert "task_definition_arn" not in parameters
    assert "revision" in parameters
    with pytest.raises(TypeError):
        rt.run_task_request(  # type: ignore[call-arg]
            task_definition=f"{ident.task_family(BINDING)}:12",
            taskdef=TASKDEF,
            placement=PLACEMENT,
            size=SIZE,
            launch_tag="run-1",
        )


@pytest.mark.parametrize("crew", ["frontdesk", "backoffice", "a-exec"])
def test_the_emitted_family_follows_the_spec_for_every_crew(crew):
    """Whichever crew the spec's secrets name, that is the family that runs."""
    binding = CrewBinding(partition="aws", account=ACCOUNT, crew=crew)
    other = td.TaskDefinitionSpec(
        image=IMAGE,
        secrets=[other_crew_ref(crew, td.MODEL_CREDENTIAL_ENV)],
        cpu_architecture="ARM64",
        log=td.default_log_spec(REGION),
        store=None,
    )
    produced = request(taskdef=other, revision=3)
    assert produced["taskDefinition"] == f"{ident.task_family(binding)}:3"
    assert {tag["key"]: tag["value"] for tag in produced["tags"]}[td.CREW_TAG_KEY] == crew


@pytest.mark.parametrize("revision", [0, -1, -99])
def test_a_revision_ecs_would_never_have_issued_is_refused(revision):
    with pytest.raises(DocumentRefused, match="revision"):
        request(revision=revision)


@pytest.mark.parametrize(
    "size",
    [
        rt.TaskSize(cpu="1024", memory="2048"),
        rt.TaskSize(cpu="4096", memory="30720"),
        rt.TaskSize(cpu="16384", memory="122880", ephemeral_storage_gib=200),
    ],
)
def test_the_running_size_is_the_one_the_launch_asked_for(size):
    """The definition's floor never reaches the override, whatever the caller sends."""
    produced = request(size=size)
    assert produced["overrides"]["cpu"] == size.cpu
    assert produced["overrides"]["memory"] == size.memory
    assert (size.cpu, size.memory) != (td.REGISTRATION_CPU, td.REGISTRATION_MEMORY)


@pytest.mark.parametrize("name", sorted(td.secret_destinations(TASKDEF)))
def test_an_environment_override_naming_any_delivered_secret_is_refused(name):
    """Every secret the definition delivers is covered, not only the credential."""
    with pytest.raises(DocumentRefused):
        request(environment={name: "whatever"})


def test_the_credential_is_refused_even_when_the_spec_does_not_deliver_it():
    """The unconditional refusal reads no part of the spec.

    A refusal computed from an intersection with the spec's secrets holds only
    when the spec is complete, and a guarantee resting on what an input contains
    is not a guarantee. No credential travels as plaintext whether the definition
    mentions it or not.
    """
    without_credential = td.TaskDefinitionSpec(
        image=IMAGE,
        secrets=[secret_ref(SECOND_SECRET)],
        cpu_architecture="ARM64",
        log=td.default_log_spec(REGION),
        store=None,
    )
    assert td.MODEL_CREDENTIAL_ENV not in td.secret_destinations(without_credential)
    with pytest.raises(DocumentRefused, match="derives or refuses"):
        request(taskdef=without_credential, environment={td.MODEL_CREDENTIAL_ENV: "plaintext"})


@pytest.mark.parametrize("name", sorted(td.CREDENTIAL_ENV))
def test_every_credential_the_container_reads_is_refused_in_an_override(name):
    """The refusal is over the SET, so adding a member extends this test itself.

    Covering one credential and leaving its sibling the same path is how a guard
    passes review while the hole stays open beside it.
    """
    with pytest.raises(DocumentRefused, match="derives or refuses"):
        request(environment={name: "plaintext"})


def test_the_control_secret_is_refused_though_no_spec_field_mentions_it():
    """The control secret is not a spec input, and is still refused as plaintext.

    It gates every control route and keys the audit HMAC, so its value is a
    credential in the same sense the model key's is. Where it legitimately comes
    from is a separate question this module does not answer.
    """
    assert td.CONTROL_SECRET_ENV in td.CREDENTIAL_ENV
    assert td.CONTROL_SECRET_ENV not in td.secret_destinations(TASKDEF)
    with pytest.raises(DocumentRefused, match="derives or refuses"):
        request(environment={td.CONTROL_SECRET_ENV: "plaintext"})


def test_the_shadowing_refusal_stands_on_its_own_for_a_non_credential_secret():
    """The general case has its own message and does not borrow the credential rule."""
    with pytest.raises(DocumentRefused, match="delivers from Secrets Manager"):
        request(environment={SECOND_SECRET: "whatever"})


@pytest.mark.parametrize("name", sorted(rt.CLOSED_ENV))
def test_no_name_in_the_closed_set_can_be_supplied_by_a_caller(name):
    """One refusal covers the whole set, so a member added to it is covered too."""
    with pytest.raises(DocumentRefused, match="derives or refuses"):
        request(environment={name: "whatever"})


#: Container variables deliberately left to the caller, with the reason each one
#: cannot contradict what this request asserts. Membership here is a decision, so
#: it is written down rather than implied by absence.
CALLER_OWNED_ENV = {
    "SMC_BACKEND_PORT": "loopback hop inside the task; nothing outside it observes the port",
    "SMC_BACKEND_RUN_DIR": "filesystem layout inside the task",
    "SMC_CONFIG_DIR": "filesystem layout; the supervisor refuses when it disagrees with the home",
    "SMC_DATA_HOME": "filesystem layout inside the task",
    "SMC_ROUTE_PREFIX": "the path routes answer on; the caller reaching the task chose it",
    "SMC_BACKUP_BUCKET": "the spec says nothing about buckets, so a value cannot contradict it",
    "SMC_BACKUP_PREFIX": "as the bucket: outside anything the spec or the definition asserts",
    "SMC_BACKUP_INTERVAL_SECS": "how often the task copies its own state; no assertion here names a cadence",
}


def _container_env_names() -> set[str]:
    """Every ``SMC_`` variable the CONTAINER reads, from its own config source.

    Read from ``load()``, which is the container's one place for turning the
    environment into settings, so this set is the container's to grow and not
    this module's to remember.

    Parsed as a file rather than imported, for the same reason the credential
    derivation is: importing the container into this lane makes coverage.py
    measure it here, where these tests exercise a fraction of its lines, and its
    own suite runs in a job that does not feed the combined report.
    """
    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "kiro_crew"
        / "apps"
        / "builtins"
        / "aws_control"
        / "crew"
        / "runtime"
        / "container"
        / "common"
        / "config.py"
    )
    assert source.is_file(), f"container config moved: {source}"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    load = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "load"
    )
    return {
        node.value
        for node in ast.walk(load)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("SMC_")
    }


def test_every_variable_the_container_reads_is_classified():
    """A name added to the container must be decided before it can arrive.

    This is the test that stops the fifth round of the same defect. Each of the
    previous four closed one variable through which a caller could contradict the
    spec; a name the container gains that is neither derived, refused, nor
    deliberately left to the caller fails here rather than shipping as an open
    channel nobody has reviewed.
    """
    classified = rt.DERIVED_ENV | rt.REFUSED_ENV | set(CALLER_OWNED_ENV)
    container = _container_env_names()
    assert container - classified == set(), "container reads names this module does not classify"
    assert {
        n for n in classified if n.startswith("SMC_")
    } - container == set(), "this module classifies SMC_ names the container does not read"


def test_the_closed_set_and_the_caller_set_do_not_overlap():
    """A name is the caller's or it is not; it cannot be quietly both."""
    assert rt.CLOSED_ENV.isdisjoint(set(CALLER_OWNED_ENV))
    assert rt.DERIVED_ENV.isdisjoint(rt.REFUSED_ENV)


@pytest.mark.parametrize("name", sorted(CALLER_OWNED_ENV))
def test_a_caller_owned_variable_passes_through_untouched(name):
    """The rule is not "refuse overrides", so the legitimate ones still work."""
    produced = request(environment={name: "caller-value"})
    emitted = {
        e["name"]: e["value"] for e in produced["overrides"]["containerOverrides"][0]["environment"]
    }
    assert emitted[name] == "caller-value"


def test_the_derived_values_win_over_nothing_because_a_collision_is_refused():
    """Precedence never arises: a caller cannot supply a derived name at all."""
    for name in sorted(rt.DERIVED_ENV):
        with pytest.raises(DocumentRefused, match="derives or refuses"):
            request(environment={name: "caller-value"})
    assert set(rt.derived_environment(BINDING)) == rt.DERIVED_ENV


def test_an_unrelated_environment_override_is_carried_as_a_sorted_list():
    produced = request(environment={"B": "2", "A": "1"})
    assert produced["overrides"]["containerOverrides"][0]["environment"] == [
        {"name": "A", "value": "1"},
        {"name": "B", "value": "2"},
        {"name": "SMC_CREW_NAME", "value": BINDING.crew},
        {"name": "SMC_SINGLE_PRINCIPAL", "value": "1"},
        {"name": rt.TASK_TTL_ENV, "value": "0"},
    ]


def test_the_task_lifetime_is_carried_into_the_container_override():
    """A bound the task holds itself is one no future launch has to arrive to apply."""
    emitted = {
        e["name"]: e["value"]
        for e in request(ttl_seconds=3600)["overrides"]["containerOverrides"][0]["environment"]
    }
    assert emitted[rt.TASK_TTL_ENV] == "3600"


def test_an_absent_lifetime_is_written_as_unbounded():
    """No lifetime asked for is the behaviour of no lifetime at all, said out loud.

    The variable is always present so the container never has to tell a launcher
    that means "unbounded" apart from one that forgot to say anything, and ``"0"``
    is what the container reads as no deadline.
    """
    for produced in (request(), request(ttl_seconds=0)):
        emitted = {
            e["name"]: e["value"]
            for e in produced["overrides"]["containerOverrides"][0]["environment"]
        }
        assert emitted[rt.TASK_TTL_ENV] == "0"


def test_a_caller_cannot_supply_the_task_lifetime():
    """The bound is a cost cap, so the channel that could raise it stays closed.

    A caller who could name this could name a lifetime longer than the sweep's and
    keep a task past the bound the launcher enforces, which is opting out of the
    cap rather than configuring it.
    """
    assert rt.TASK_TTL_ENV in rt.DERIVED_ENV
    assert rt.TASK_TTL_ENV in rt.CLOSED_ENV
    with pytest.raises(DocumentRefused, match="derives or refuses"):
        request(environment={rt.TASK_TTL_ENV: "999999"})


def test_a_negative_lifetime_is_refused_at_generation():
    """A deadline already past would cost a task that never did any work.

    Refused here rather than at the container's own startup check, for the reason
    the whole module exists: a launch-time failure is harder to read than a
    generation-time one, and this one would arrive as a container fault.
    """
    with pytest.raises(DocumentRefused, match="not a lifetime"):
        request(ttl_seconds=-1)


def test_the_container_override_addresses_the_crew_container_by_name():
    """An override whose name matches no container is accepted by ECS and ignored."""
    produced = request(environment={"A": "1"})
    assert produced["overrides"]["containerOverrides"][0]["name"] == td.CREW_CONTAINER_NAME


def test_the_image_command_cannot_be_replaced():
    """Replacing the command replaces the supervisor while keeping its credentials.

    Secrets are injected and the task role attached before any command runs, so an
    override would run arbitrary code holding the model credential with none of
    the supervisor's sandbox verification or environment scrubbing.
    """
    assert "command" not in rt.CONTAINER_OVERRIDE_KEYS
    assert "command" not in set(inspect.signature(rt.run_task_request).parameters)
    with pytest.raises(TypeError):
        request(command=["sh", "-c", "true"])


def test_the_derived_identity_is_emitted_even_with_no_caller_environment():
    """The crew, the trust domain and the lifetime are written here, not requested."""
    produced = request()
    container_override = produced["overrides"]["containerOverrides"][0]
    assert set(container_override) == {"name", "environment"}
    emitted = {e["name"]: e["value"] for e in container_override["environment"]}
    assert emitted == {
        "SMC_CREW_NAME": BINDING.crew,
        "SMC_SINGLE_PRINCIPAL": "1",
        rt.TASK_TTL_ENV: "0",
    }
    assert "ephemeralStorage" not in produced["overrides"]


def test_the_task_is_not_published_to_the_internet_by_default():
    network = request()["networkConfiguration"]["awsvpcConfiguration"]
    assert network["assignPublicIp"] == "DISABLED"
    assert network["subnets"] == ["subnet-a"]
    assert network["securityGroups"] == ["sg-a"]


def test_a_public_address_is_only_assigned_when_asked_for():
    produced = request(
        placement=rt.Placement(
            cluster="crews",
            subnets=("subnet-a",),
            security_groups=("sg-a",),
            assign_public_ip=True,
        )
    )
    assert produced["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"] == "ENABLED"


def test_the_request_launches_exactly_one_fargate_task_from_the_named_definition():
    produced = request()
    assert produced["launchType"] == "FARGATE"
    assert produced["count"] == 1
    assert produced["cluster"] == "crews"


#: EVERY caller-supplied field that reaches an emitted payload, and which of three
#: it is. Six reviews found six members of one family one at a time, so the answer
#: is a list checked against the code rather than a claim in prose.
#:
#: "derived"  - this module writes it; no caller value reaches it.
#: "closed"   - the caller may pass it, and named values are refused.
#: "caller"   - deliberately the caller's, with the reason it cannot contradict
#:              anything the request already asserts.
INPUT_DISPOSITION = {
    # run_task_request parameters
    ("run_task_request", "revision"): "closed",
    ("run_task_request", "taskdef"): "closed",
    ("run_task_request", "placement"): "closed",
    ("run_task_request", "size"): "closed",
    ("run_task_request", "launch_tag"): "caller",
    ("run_task_request", "environment"): "closed",
    ("run_task_request", "started_by"): "caller",
    ("run_task_request", "ttl_seconds"): "closed",
    # Placement fields
    ("Placement", "cluster"): "caller",
    ("Placement", "subnets"): "caller",
    ("Placement", "security_groups"): "caller",
    ("Placement", "assign_public_ip"): "caller",
    # TaskSize fields
    ("TaskSize", "cpu"): "closed",
    ("TaskSize", "memory"): "closed",
    ("TaskSize", "ephemeral_storage_gib"): "closed",
    # TaskDefinitionSpec fields
    ("TaskDefinitionSpec", "image"): "closed",
    ("TaskDefinitionSpec", "secrets"): "closed",
    ("TaskDefinitionSpec", "cpu_architecture"): "closed",
    ("TaskDefinitionSpec", "log"): "closed",
    ("TaskDefinitionSpec", "store"): "closed",
    # StoreSpec fields
    ("StoreSpec", "file_system_id"): "closed",
    ("StoreSpec", "access_point_id"): "closed",
    # LogSpec fields
    ("LogSpec", "region"): "caller",
    ("LogSpec", "stream_prefix"): "caller",
}

#: What the emitted payloads carry that NO caller value reaches. Named so that a
#: field moving out of this set is a visible change rather than a quiet one.
DERIVED_IN_PAYLOAD = frozenset(
    {
        "family",
        "taskDefinition",
        "executionRoleArn",
        "taskRoleArn",
        "networkMode",
        "requiresCompatibilities",
        "registration cpu",
        "registration memory",
        "container name",
        "container portMappings",
        "container secrets",
        "awslogs-group",
        "SMC_CREW_NAME",
        "SMC_SINGLE_PRINCIPAL",
        rt.TASK_TTL_ENV,
        MANAGED_TAG_KEY,
        td.FINGERPRINT_TAG_KEY,
        td.CREW_TAG_KEY,
    }
)


def test_every_caller_supplied_field_has_a_disposition():
    """The enumeration is checked against the code, so a new field must be placed.

    This is the anti-drift half of the list in the PR description: adding a
    parameter or a dataclass field without deciding whether it is derived, closed
    or the caller's fails here.
    """
    declared: set[tuple[str, str]] = set()
    for name, obj in (
        ("Placement", rt.Placement),
        ("TaskSize", rt.TaskSize),
        ("TaskDefinitionSpec", td.TaskDefinitionSpec),
        ("LogSpec", td.LogSpec),
        ("StoreSpec", td.StoreSpec),
    ):
        declared |= {(name, f.name) for f in dataclasses.fields(obj)}
    signature = inspect.signature(rt.run_task_request)
    declared |= {("run_task_request", p) for p in signature.parameters}
    assert declared == set(INPUT_DISPOSITION), (
        "these caller-supplied fields have no recorded disposition: "
        f"{sorted(declared ^ set(INPUT_DISPOSITION))}"
    )


@pytest.mark.parametrize("value", ["has space", "semi;colon", "quote'", "new\nline", "sla/sh"])
def test_a_correlation_value_outside_the_api_charset_is_refused(value):
    """Refused here rather than at launch, where the reason is harder to read.

    Both fields are free text a caller chooses, and the API takes letters, digits,
    hyphen and underscore. Leaving it to AWS is the reasoning that produced several
    earlier findings.
    """
    with pytest.raises(DocumentRefused, match="hyphen and underscore"):
        request(started_by=value)
    with pytest.raises(DocumentRefused, match="hyphen and underscore"):
        request(launch_tag=value)


def _managed_value_the_codebase_filters_on() -> set[str]:
    """The marker VALUE other modules match, read from their own source.

    ``cloud/ec2.py`` is the working precedent: it tags ``kirocrew:managed=true`` and
    discovers with ``Key=kirocrew:managed,Values=true``. Read rather than restated,
    so a change there fails here instead of silently making this module's tasks
    invisible to whatever looks for them.

    Parsed as a file, not imported: importing it would make coverage.py measure it
    in this lane, where these tests exercise none of it.
    """
    source = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "cloud" / "ec2.py"
    assert source.is_file(), f"ec2 module moved: {source}"
    text = source.read_text(encoding="utf-8")
    found = set(re.findall(r"\{MANAGED_TAG_KEY\}=([A-Za-z0-9_-]+)", text))
    found |= set(re.findall(r"Key=\{MANAGED_TAG_KEY\},Values=([A-Za-z0-9_-]+)", text))
    found |= set(re.findall(r"tags\.get\(MANAGED_TAG_KEY\) != \"([A-Za-z0-9_-]+)\"", text))
    assert found, "ec2.py no longer states a managed marker value"
    return found


def test_the_managed_marker_is_the_value_other_modules_discover_by():
    """Teardown finds a task by this tag, and it matches on the VALUE.

    A caller-chosen value would produce a RUNNING task that a discovery filter for
    the marker does not enumerate and the teardown role's resource conditions do
    not cover, billing in the owner's account with nothing pointing at it.
    """
    assert _managed_value_the_codebase_filters_on() == {td.MANAGED_TAG_VALUE}


@pytest.mark.parametrize("launch_tag", ["run-1", "not-true", "x", "TRUE"])
def test_no_launch_tag_can_change_the_managed_marker(launch_tag):
    """The caller's value lands in its own key and cannot reach the marker."""
    tags = {tag["key"]: tag["value"] for tag in request(launch_tag=launch_tag)["tags"]}
    assert tags[MANAGED_TAG_KEY] == td.MANAGED_TAG_VALUE
    assert tags[rt.LAUNCH_TAG_KEY] == launch_tag
    assert tags[td.CREW_TAG_KEY] == BINDING.crew


def test_the_marker_and_the_correlation_value_are_separate_keys():
    """One key cannot answer both questions.

    The marker has to be constant for teardown to match it; the correlation value
    has to vary for a caller to find their own task. This mirrors ``cloud/ec2.py``,
    which tags ``kirocrew:managed=true`` beside ``kirocrew:instance=<tag>``.
    """
    assert rt.LAUNCH_TAG_KEY != MANAGED_TAG_KEY


def test_a_launch_with_no_tag_is_refused_because_teardown_could_not_find_it():
    with pytest.raises(DocumentRefused, match="teardown"):
        request(launch_tag="")


#: Every field of every public type here, and the empty form of its own type.
#: ``None`` marks a field whose default is a deliberate exception, recorded in the
#: module docstring. Driven from ``dataclasses.fields`` so a field added later
#: appears here as a failure rather than as an untested permissive default.
EMPTY_BY_FIELD = {
    ("Placement", "cluster"): "",
    ("Placement", "subnets"): (),
    ("Placement", "security_groups"): (),
    ("Placement", "assign_public_ip"): None,
    ("TaskSize", "cpu"): "",
    ("TaskSize", "memory"): "",
    ("TaskSize", "ephemeral_storage_gib"): 0,
}


#: Which ``run_task_request`` argument each swept type arrives as.
SWEPT_ARGUMENT = {"Placement": "placement", "TaskSize": "size"}


def _swept_types():
    return {"Placement": rt.Placement, "TaskSize": rt.TaskSize}


@pytest.mark.parametrize("type_name", sorted(_swept_types()))
def test_every_field_of_every_launch_type_is_swept(type_name):
    """The sweep is enforced, so a new field cannot arrive as a silent default."""
    declared = {f.name for f in dataclasses.fields(_swept_types()[type_name])}
    swept = {field for (owner, field) in EMPTY_BY_FIELD if owner == type_name}
    assert declared == swept, f"{type_name} has fields that no emptiness test decides"


@pytest.mark.parametrize(
    "owner,field",
    sorted(key for key, empty in EMPTY_BY_FIELD.items() if empty is not None),
)
def test_no_field_lets_empty_mean_something_other_than_a_refusal(owner, field):
    base = {"Placement": PLACEMENT, "TaskSize": SIZE}[owner]
    altered = dataclasses.replace(base, **{field: EMPTY_BY_FIELD[(owner, field)]})
    with pytest.raises(DocumentRefused):
        request(**{SWEPT_ARGUMENT[owner]: altered})


@pytest.mark.parametrize("cpu", ["777", "128", "32768"])
def test_a_cpu_size_fargate_does_not_offer_is_refused(cpu):
    """A positive integer is not thereby a Fargate size."""
    with pytest.raises(DocumentRefused, match="not a Fargate CPU size"):
        request(size=rt.TaskSize(cpu=cpu, memory="2048"))


def test_an_invalid_cpu_and_memory_pair_is_refused_here_not_at_launch():
    """RunTask refuses this pair anyway; deferring to it is the reasoning to avoid."""
    with pytest.raises(DocumentRefused, match="steps of"):
        request(size=rt.TaskSize(cpu="1024", memory="1024"))


@pytest.mark.parametrize("gib", [1, 20, 201])
def test_ephemeral_storage_outside_the_fargate_range_is_refused(gib):
    with pytest.raises(DocumentRefused, match="21 to 200"):
        request(size=rt.TaskSize(cpu="1024", memory="2048", ephemeral_storage_gib=gib))


@pytest.mark.parametrize("cpu", sorted(rt.FARGATE_MEMORY_FOR_CPU, key=int))
def test_every_declared_cpu_size_accepts_its_own_range_ends(cpu):
    """The table is usable at both ends, so the refusal is not a blanket one."""
    low, high, _ = rt.FARGATE_MEMORY_FOR_CPU[cpu]
    for memory in (low, high):
        produced = request(size=rt.TaskSize(cpu=cpu, memory=str(memory)))
        assert produced["overrides"]["cpu"] == cpu


def test_the_registration_floor_is_accepted_when_a_caller_states_it_deliberately():
    """Requiring the field removed the silence; the floor's value was never wrong."""
    produced = request(size=rt.TaskSize(cpu=td.REGISTRATION_CPU, memory=td.REGISTRATION_MEMORY))
    assert produced["overrides"]["cpu"] == td.REGISTRATION_CPU


def test_a_public_address_stays_a_plain_default_because_it_is_not_the_boundary():
    """False is the safe direction, and the security group is what bounds reach."""
    assert rt.Placement(cluster="c", subnets=("s",), security_groups=("g",)).assign_public_ip is (
        False
    )


def test_an_empty_security_group_is_refused_rather_than_left_to_the_vpc_default():
    """ECS substitutes the VPC default group, which is wider than any launch means.

    The default group admits traffic from anything else in it, and the front
    process answers a turn without the control secret, so a workload sharing that
    group could take a turn on this crew using this crew's model credential.
    """
    with pytest.raises(DocumentRefused, match="VPC default group"):
        request(placement=rt.Placement(cluster="crews", subnets=("subnet-a",), security_groups=()))


def test_started_by_is_carried_only_when_given_and_only_within_the_api_cap():
    assert "startedBy" not in request()
    assert request(started_by="kirocrew-launch")["startedBy"] == "kirocrew-launch"
    with pytest.raises(DocumentRefused, match="startedBy"):
        request(started_by="x" * (rt.STARTED_BY_MAX + 1))


def test_a_request_built_from_a_definition_naming_two_crews_is_refused():
    """The request re-establishes the binding, so it cannot outlive a bad definition."""
    crossed = td.TaskDefinitionSpec(
        image=IMAGE,
        secrets=[
            secret_ref(td.MODEL_CREDENTIAL_ENV),
            other_crew_ref("backoffice", SECOND_SECRET),
        ],
        cpu_architecture="ARM64",
        log=td.default_log_spec(REGION),
        store=None,
    )
    with pytest.raises(DocumentRefused):
        request(taskdef=crossed)


#: Constants deliberately kept OFF the package surface, with the reason. Each is a
#: fragment a deriver function is built FROM, and that function is already exported,
#: so exporting the fragment would offer a second way to spell what the function
#: returns. That is the liability this module has closed four times, so the
#: exclusion is a decision rather than an oversight -- which is why it carries its
#: reason here instead of being a bare list.
SURFACE_EXCLUSIONS = {
    "FAMILY_PREFIX": "task_family() returns the built family name",
    "SECRET_NAME_PREFIX": "the crew secret name is checked by parse_secret_arn()",
    "EXECUTION_ROLE_SUFFIX": "execution_role_arn() returns the built role ARN",
    "TASK_ROLE_SUFFIX": "task_role_arn() returns the built role ARN",
}


def _public_constants(module) -> dict[str, object]:
    """Module-level public constants defined in this module, not merely imported."""
    found = {}
    for name in dir(module):
        if name.startswith("_") or not name.isupper():
            continue
        value = getattr(module, name)
        if isinstance(value, (str, int, float, frozenset, dict, tuple, set)):
            found[name] = value
    return found


def _constants_a_caller_must_state(source: pathlib.Path) -> set[str]:
    """Public constants named inside any function that can refuse.

    A function refuses if it raises ``DocumentRefused`` itself or calls something
    in the same module that does, resolved to a fixpoint: the refusal is usually
    delegated to a helper, and a one-level scan would miss every caller of it,
    which is most of them.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def raises_refusal(node: ast.AST) -> bool:
        return any(
            isinstance(inner, ast.Raise)
            and isinstance(inner.exc, ast.Call)
            and isinstance(inner.exc.func, ast.Name)
            and inner.exc.func.id == "DocumentRefused"
            for inner in ast.walk(node)
        )

    refusing = {name for name, node in functions.items() if raises_refusal(node)}
    growing = True
    while growing:
        growing = False
        for name, node in functions.items():
            if name in refusing:
                continue
            called = {
                inner.func.id
                for inner in ast.walk(node)
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
            }
            if called & refusing:
                refusing.add(name)
                growing = True

    named: set[str] = set()
    for name in refusing:
        for inner in ast.walk(functions[name]):
            if isinstance(inner, ast.Name) and inner.id.isupper():
                if not inner.id.startswith("_"):
                    named.add(inner.id)
    return named


def _scalars_in(payload: object) -> set:
    """Every scalar anywhere in a nested payload, mapping keys included."""
    seen: set = set()
    pending = [payload]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                seen.add(key)
                pending.append(value)
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
        elif isinstance(item, (str, int, float, bool)):
            seen.add(item)
    return seen


def test_every_constant_a_consumer_must_import_is_on_the_package_surface():
    """A name a consumer cannot import is a name it will re-spell, and re-spelling silently broke teardown.

    A launch engine that wrote its own ``"kirocrew:task"`` rather than importing
    ``LAUNCH_TAG_KEY`` classified every managed task as foreign, so its teardown
    planner returned an empty delete set and reported success while every task kept
    running. Absent from the export list read as a value.

    So this derives the requirement instead of listing it, on the two limbs the
    package docstring states: a caller cannot build an acceptable input without the
    constant (it is named inside a refusal), or cannot interpret a produced payload
    without it (its value appears in one). Either way the next constant added is
    covered before a consumer trips over it.
    """
    package_dir = pathlib.Path(ident.__file__).parent
    produced = _scalars_in(td.task_definition_document(TASKDEF)) | _scalars_in(request())

    required: dict[str, set[str]] = {}
    for label, module in (("identity", ident), ("taskdef", td), ("runtask", rt)):
        must_state = _constants_a_caller_must_state(package_dir / f"{label}.py")
        for name, value in _public_constants(module).items():
            if name in must_state:
                required.setdefault(name, set()).add("stated by the caller")
            members = value if isinstance(value, (frozenset, set, tuple)) else (value,)
            if any(member in produced for member in members if isinstance(member, str)):
                required.setdefault(name, set()).add("read from the payload")

    surface = set(fargate_package.__all__)
    missing = sorted(name for name in required if name not in surface | SURFACE_EXCLUSIONS.keys())
    assert not missing, (
        "these constants are required by the contract but cannot be imported from "
        f"kiro_crew.cloud.fargate, so a consumer must re-spell them: {missing}"
    )

    stale = sorted(name for name in SURFACE_EXCLUSIONS if name in surface)
    assert not stale, (
        "these are recorded as deliberately excluded yet are exported, so the "
        f"recorded reason no longer describes the code: {stale}"
    )


def test_everything_the_package_exports_actually_resolves():
    """An `__all__` entry naming nothing is the same defect facing the other way."""
    unresolved = [n for n in fargate_package.__all__ if not hasattr(fargate_package, n)]
    assert not unresolved, f"__all__ names attributes the package does not have: {unresolved}"
    assert len(set(fargate_package.__all__)) == len(fargate_package.__all__)


# ── The SSM channel the owner reaches the task through ─────────────────────────


@pytest.mark.parametrize("shape", ARGUMENT_SHAPES)
def test_execute_command_is_enabled_so_the_task_is_reachable(shape):
    """Without this the task has no SSM channel and this lane publishes no ingress.

    Not a parameter: ECS cannot turn the flag on for a task that is already
    running, so a task launched without it is unreachable with no remedy but
    teardown and relaunch.
    """
    assert request(**shape)["enableExecuteCommand"] is True


@pytest.mark.parametrize("shape", ARGUMENT_SHAPES)
def test_execute_command_is_a_request_field_not_a_task_override(shape):
    """It belongs beside ``cluster``, NOT inside ``overrides``.

    Placement is the whole point: ``TASK_OVERRIDE_KEYS`` governs ``overrides`` and
    nothing else, so putting this key there would be refused by
    ``_refuse_keys_outside`` -- and adding it to that allowlist to make it fit
    would widen the set that keeps ``executionRoleArn`` and ``taskRoleArn`` out.
    Asserted as ABSENT from the override so a later move fails here.
    """
    produced = request(**shape)
    assert "enableExecuteCommand" not in produced["overrides"]
    assert "enableExecuteCommand" not in strings(produced["overrides"])
    # The allowlist that would have to be widened is still exactly the four keys.
    assert rt.TASK_OVERRIDE_KEYS == frozenset(
        {"cpu", "memory", "ephemeralStorage", "containerOverrides"}
    )


@pytest.mark.parametrize("shape", ARGUMENT_SHAPES)
def test_the_task_takes_no_public_address_by_default(shape):
    """No public ingress, ever: the owner reaches this task over SSM or not at all.

    The tunnel does not need a public address and a public address is not needed
    for anything else, so DISABLED is the only value a default launch produces.
    """
    vpc = request(**shape)["networkConfiguration"]["awsvpcConfiguration"]
    assert vpc["assignPublicIp"] == "DISABLED"
