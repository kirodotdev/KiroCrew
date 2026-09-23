"""The crew task-definition document, and what a revision is keyed on.

The revision-key tests are written so a field ADDED to
:class:`TaskDefinitionSpec` later cannot slip past them: the table classifying
each field as in-key or not is checked against ``dataclasses.fields``, so an
unclassified field fails until someone decides which it is.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from kiro_crew.cloud.fargate import identity as ident
from kiro_crew.cloud.fargate import taskdef as td
from kiro_crew.cloud.fargate.identity import CrewBinding, DocumentRefused, SecretRef

ACCOUNT = "123456789012"
OTHER_ACCOUNT = "210987654321"
REGION = "us-east-1"
DIGEST = "sha256:" + "b" * 64
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/kirocrew-crew@{DIGEST}"

BINDING = CrewBinding(partition="aws", account=ACCOUNT, crew="frontdesk")
OTHER_BINDING = CrewBinding(partition="aws", account=ACCOUNT, crew="backoffice")


def secret_ref(crew: str, key: str, *, account: str = ACCOUNT) -> SecretRef:
    """A secret named twice: canonically, and as the ARN that resolves to it."""
    name = f"kirocrew/crew/{crew}/{key}"
    return SecretRef(
        name=name,
        arn=f"arn:aws:secretsmanager:{REGION}:{account}:secret:{name}-AbCdEf",
    )


def spec(**overrides) -> td.TaskDefinitionSpec:
    base = dict(
        image=IMAGE,
        secrets=[secret_ref("frontdesk", td.MODEL_CREDENTIAL_ENV)],
        cpu_architecture="ARM64",
        log=td.default_log_spec(REGION),
    )
    base.update(overrides)
    return td.TaskDefinitionSpec(**base)  # type: ignore[arg-type]


def container(document) -> dict:
    return document["containerDefinitions"][0]


# Each spec field and a value that differs from the default. Every field is in
# the revision key, because a field RunTask can override is not a spec field at
# all: size is absent, and both roles are derived from the secrets rather than
# supplied. The table is checked against dataclasses.fields below, so a field
# added later fails the suite until it is classified.
FIELD_MUTATIONS = {
    "image": dict(image=f"repo/other@sha256:{'c' * 64}"),
    "secrets": dict(
        secrets=[
            secret_ref("frontdesk", td.MODEL_CREDENTIAL_ENV),
            secret_ref("frontdesk", "EXTRA"),
        ]
    ),
    "cpu_architecture": dict(cpu_architecture="X86_64"),
    "log": dict(log=td.LogSpec(region="eu-west-1", stream_prefix="p")),
}

# What RunTask can override, mapped to the spec field name each would carry if
# anyone put it in the spec. None of them may be a field: excluding a field from
# the hash relies on the hash staying right, while not having the field cannot
# be got wrong.
RUNTASK_OVERRIDABLE = {
    "cpu": "cpu",
    "memory": "memory",
    "ephemeralStorage": "ephemeral_storage_gib",
    "executionRoleArn": "execution_role_arn",
    "taskRoleArn": "task_role_arn",
}


def test_every_spec_field_is_classified_as_in_the_revision_key_or_not():
    """A field added to the spec must be decided, not defaulted into silence."""
    assert {f.name for f in dataclasses.fields(td.TaskDefinitionSpec)} == set(FIELD_MUTATIONS)


@pytest.mark.parametrize("field", sorted(FIELD_MUTATIONS))
def test_the_revision_key_reads_every_field_the_spec_carries(field):
    assert td.revision_fingerprint(spec(**FIELD_MUTATIONS[field])) != td.revision_fingerprint(
        spec()
    )


@pytest.mark.parametrize("override_key", sorted(RUNTASK_OVERRIDABLE))
def test_nothing_runtask_can_override_is_a_field_of_the_spec(override_key):
    """The key cannot read an overridable field, because none is representable."""
    assert RUNTASK_OVERRIDABLE[override_key] not in {
        f.name for f in dataclasses.fields(td.TaskDefinitionSpec)
    }


def test_the_key_does_not_read_size_because_the_spec_carries_none():
    """Keying on size is wrong, and there is no size to key on."""
    assert "cpu" not in {f.name for f in dataclasses.fields(td.TaskDefinitionSpec)}
    assert "memory" not in {f.name for f in dataclasses.fields(td.TaskDefinitionSpec)}
    document = td.task_definition_document(spec())
    assert (document["cpu"], document["memory"]) == (td.REGISTRATION_CPU, td.REGISTRATION_MEMORY)


def test_the_secret_order_in_the_spec_does_not_change_the_key():
    """A revision is keyed on the SET of secret references, so order is not identity."""
    forward = [
        secret_ref("frontdesk", "A"),
        secret_ref("frontdesk", td.MODEL_CREDENTIAL_ENV),
    ]
    assert td.revision_fingerprint(spec(secrets=forward)) == td.revision_fingerprint(
        spec(secrets=list(reversed(forward)))
    )


def test_two_crews_sharing_an_image_and_log_shape_still_get_different_keys():
    """The crew is in the key through its secret ARNs, so the family need not be."""
    other = spec(
        secrets=[secret_ref("backoffice", td.MODEL_CREDENTIAL_ENV)],
    )
    assert td.revision_fingerprint(other) != td.revision_fingerprint(spec())


def test_the_document_records_its_own_revision_key_as_a_tag():
    """The account can answer what a revision was keyed on without a local cache."""
    document = td.task_definition_document(spec())
    tags = {tag["key"]: tag["value"] for tag in document["tags"]}
    assert tags[td.FINGERPRINT_TAG_KEY] == td.revision_fingerprint(spec())
    assert tags[td.CREW_TAG_KEY] == BINDING.crew


def test_the_family_holds_one_crew_and_the_platform_is_pinned():
    document = td.task_definition_document(spec())
    assert document["family"] == ident.task_family(BINDING)
    assert document["requiresCompatibilities"] == ["FARGATE"]
    assert document["networkMode"] == "awsvpc"
    assert document["runtimePlatform"] == {
        "cpuArchitecture": "ARM64",
        "operatingSystemFamily": td.OPERATING_SYSTEM_FAMILY,
    }


def test_the_front_port_is_declared_and_is_not_a_spec_field():
    """The port is a constant of the image, which is why it is not in the key."""
    assert "port" not in {f.name for f in dataclasses.fields(td.TaskDefinitionSpec)}
    assert container(td.task_definition_document(spec()))["portMappings"] == [
        {"containerPort": td.FRONT_PORT, "protocol": "tcp"}
    ]


def test_the_model_credential_arrives_through_secrets_and_nowhere_else():
    """The definition has no plaintext environment at all, so there is no second route."""
    document = td.task_definition_document(spec())
    delivered = {entry["name"]: entry["valueFrom"] for entry in container(document)["secrets"]}
    assert delivered == {
        td.MODEL_CREDENTIAL_ENV: secret_ref("frontdesk", td.MODEL_CREDENTIAL_ENV).arn
    }
    assert "environment" not in container(document)


def test_a_definition_that_does_not_deliver_the_model_credential_is_refused():
    with pytest.raises(DocumentRefused, match=td.MODEL_CREDENTIAL_ENV):
        td.task_definition_document(spec(secrets=[secret_ref("frontdesk", "OTHER")]))


ENV_NAME_SEGMENTS = ["KIRO_API_KEY", "OTHER_KEY", "A", "A_B_C", "TOKEN2", "X" * 40]


@pytest.mark.parametrize("segment", ENV_NAME_SEGMENTS)
def test_the_destination_variable_is_read_out_of_the_secret_name(segment):
    """Whatever the secret is called, that is the variable it lands in."""
    ref = secret_ref("frontdesk", segment)
    assert ident.secret_env_name(ref) == segment
    assert td.secret_destinations(spec(secrets=[ref])) == {segment: ref}


def test_no_second_place_names_the_destination_so_it_cannot_disagree():
    """The spec carries ARNs only, so a mismatched destination is unconstructible.

    Naming the destination beside the ARN would let a document deliver one
    secret's value under another secret's name. That is the credential defect in
    its quietest form: the crew agrees, the reference resolves, and the container
    starts and runs every turn on the wrong value.
    """
    assert dataclasses.fields(td.TaskDefinitionSpec)[1].name == "secrets"
    assert not isinstance(spec().secrets, dict)
    with pytest.raises(TypeError):
        td.TaskDefinitionSpec(  # type: ignore[call-arg]
            image=IMAGE,
            secrets=[secret_ref("frontdesk", td.MODEL_CREDENTIAL_ENV)],
            cpu_architecture="ARM64",
            log=td.default_log_spec(REGION),
            secret_env_names={td.MODEL_CREDENTIAL_ENV: "OTHER_KEY"},
        )


def test_a_secret_named_for_another_variable_lands_in_that_variable():
    """The one remaining way to get the credential wrong now shows in the output.

    A spec naming only OTHER_KEY does not deliver the credential, so the
    definition is refused rather than quietly delivering the wrong value under
    the credential's name.
    """
    with pytest.raises(DocumentRefused, match=td.MODEL_CREDENTIAL_ENV):
        td.task_definition_document(spec(secrets=[secret_ref("frontdesk", "OTHER_KEY")]))


def test_two_secrets_competing_for_one_variable_are_refused():
    """One variable takes one value, so a tie has no winner to pick."""
    duplicate = secret_ref("frontdesk", td.MODEL_CREDENTIAL_ENV)
    with pytest.raises(DocumentRefused, match="two secrets deliver"):
        td.task_definition_document(spec(secrets=[duplicate, duplicate]))


#: The container's supervisor backend, located as a FILE rather than imported.
#: Importing it would make coverage.py measure it in this lane, where these tests
#: exercise a fraction of its lines; its own suite is a separate job that does not
#: feed the combined report, so the merged per-file rate would drop below the
#: floor for a module this change never touched. Reading the source keeps the
#: container authoritative without pulling it into this lane's measurement.
_CONTAINER = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "apps"
    / "builtins"
    / "aws_control"
    / "crew"
    / "runtime"
    / "container"
)
_CREW_BACKEND_SOURCE = _CONTAINER / "supervisor" / "backend.py"


def _container_module(path: pathlib.Path) -> ast.Module:
    assert path.is_file(), f"container source moved: {path}"
    return ast.parse(path.read_text(encoding="utf-8"))


def _env_constants(tree: ast.Module) -> dict[str, str]:
    """Every module-level ``ENV_* = "..."`` in the container, name to value."""
    found: dict[str, str] = {}
    for node in tree.body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
        if (
            target
            and target.startswith("ENV_")
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            found[target] = node.value.value
    return found


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"container no longer defines {name}")


#: Every field of LogSpec, with the value that sends the stream nowhere. Each one
#: fails the same way: the task starts, answers turns, and writes no readable log.
EMPTY_LOG_FIELD = {"region": "", "stream_prefix": ""}


def _log(**overrides) -> td.LogSpec:
    base = dict(region=REGION, stream_prefix="crew")
    base.update(overrides)
    return td.LogSpec(**base)


@pytest.mark.parametrize("field", sorted(EMPTY_LOG_FIELD))
def test_a_log_configuration_that_produces_no_readable_stream_is_refused(field):
    with pytest.raises(DocumentRefused):
        _log(**{field: EMPTY_LOG_FIELD[field]})


def test_every_log_field_is_swept():
    """A field added to LogSpec must be decided, not defaulted into silence."""
    declared = {f.name for f in dataclasses.fields(td.LogSpec)}
    assert declared == set(EMPTY_LOG_FIELD)


def test_a_log_configuration_region_is_refused_by_the_same_rule_as_an_arn_region():
    """One validator, so a region readable in one place is not refused in the other."""
    with pytest.raises(DocumentRefused, match="region"):
        _log(region="us west 2")


def test_the_credential_env_name_matches_the_container_that_reads_it():
    """A drift here delivers the secret under a name the backend does not read."""
    constants = _env_constants(_container_module(_CREW_BACKEND_SOURCE))
    assert td.MODEL_CREDENTIAL_ENV == constants["ENV_KIRO_API_KEY"]
    assert td.CONTROL_SECRET_ENV == constants["ENV_CONTROL_SECRET"]


def _container_credential_env() -> set[str]:
    """The credential variables the CONTAINER declares, read from its own source.

    The container marks a name as credential-bearing in one of two ways, and both
    are structural rather than a list anyone maintains:

    * ``build_backend_env`` POPS it from the model worker's environment. That
      worker auto-approves every tool it calls, so a name deliberately withheld
      from it is a name whose value must not be readable by prompt content.
    * ``require_api_key`` refuses to start without it, which is the model
      credential the task injects from Secrets Manager.

    Variables merely mentioned in ``build_backend_env`` do not qualify: it also
    sets the home, port, bind address and telemetry flag, none of which are
    secret. The distinction is the treatment, not the mention.
    """
    tree = _container_module(_CREW_BACKEND_SOURCE)
    constants = _env_constants(tree)
    built = _function(tree, "build_backend_env")
    popped = {
        call.args[0].id
        for call in ast.walk(built)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "pop"
        and call.args
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id.startswith("ENV_")
    }
    gated = {
        node.id
        for node in ast.walk(_function(tree, "require_api_key"))
        if isinstance(node, ast.Name) and node.id.startswith("ENV_")
    }
    return {constants[name] for name in popped | gated}


def test_every_credential_the_container_reads_is_refused_here():
    """Forward direction: a credential added to the container fails until refused.

    This is the half that stops the refusal covering one name while its sibling
    walks the same path into the CloudTrail record of the request.
    """
    assert _container_credential_env() <= td.CREDENTIAL_ENV


def test_nothing_is_refused_that_the_container_does_not_read_as_a_credential():
    """Reverse direction: a name refused here with no reader shows up as drift."""
    assert td.CREDENTIAL_ENV <= _container_credential_env()


def test_only_the_execution_role_is_positioned_to_fetch_the_secrets():
    document = td.task_definition_document(spec())
    assert document["executionRoleArn"] == ident.execution_role_arn(BINDING)
    assert document["taskRoleArn"] == ident.task_role_arn(BINDING)
    assert document["executionRoleArn"] != document["taskRoleArn"]


def test_neither_role_can_be_supplied_so_neither_can_be_wrong():
    """Both roles are derived from the secrets, so a swap is unconstructible.

    Agreement on the crew would not have caught a swap, because both of a crew's
    roles name that crew. The swap is an escalation rather than a typo: a
    container's model subprocess can read the task role's credential out of its
    own environment and act as it, so a task carrying the EXECUTION role can
    re-read secrets, and a shared execution role holds every crew's.
    """
    fields = {f.name for f in dataclasses.fields(td.TaskDefinitionSpec)}
    assert "execution_role_arn" not in fields
    assert "task_role_arn" not in fields
    with pytest.raises(TypeError):
        td.TaskDefinitionSpec(  # type: ignore[call-arg]
            image=IMAGE,
            secrets=[secret_ref("frontdesk", td.MODEL_CREDENTIAL_ENV)],
            cpu_architecture="ARM64",
            log=td.default_log_spec(REGION),
            execution_role_arn=ident.task_role_arn(BINDING),
        )


def test_the_roles_follow_the_crew_the_secrets_name():
    """Whichever crew the secrets name, the derived roles are that crew's."""
    other = td.task_definition_document(
        spec(secrets=[secret_ref("backoffice", td.MODEL_CREDENTIAL_ENV)])
    )
    assert other["executionRoleArn"] == ident.execution_role_arn(OTHER_BINDING)
    assert other["taskRoleArn"] == ident.task_role_arn(OTHER_BINDING)
    assert other["family"] == ident.task_family(OTHER_BINDING)


ARN_POSITIONS = ["secrets"]
FOREIGN_IDENTITIES = ["crew", "account"]


@pytest.mark.parametrize("position", ARN_POSITIONS)
@pytest.mark.parametrize("foreign", FOREIGN_IDENTITIES)
def test_any_position_naming_another_identity_refuses_the_document(position, foreign):
    """Every crew-bearing position is covered, for every part of the identity.

    Only one position remains: the roles and the family are derived, so a
    stranger can enter a definition through its secrets alone.
    """
    stranger = (
        OTHER_BINDING if foreign == "crew" else CrewBinding("aws", OTHER_ACCOUNT, BINDING.crew)
    )
    mutation = {
        "secrets": dict(
            secrets=[
                secret_ref(stranger.crew, td.MODEL_CREDENTIAL_ENV, account=stranger.account),
                secret_ref("frontdesk", "EXTRA"),
            ]
        ),
    }[position]
    with pytest.raises(DocumentRefused):
        td.task_definition_document(spec(**mutation))


def test_a_second_secret_naming_another_crew_is_refused_even_beside_a_correct_one():
    """One right ARN does not license a wrong one sitting next to it."""
    with pytest.raises(DocumentRefused):
        td.task_definition_document(
            spec(
                secrets=[
                    secret_ref("frontdesk", td.MODEL_CREDENTIAL_ENV),
                    secret_ref("backoffice", "EXTRA"),
                ]
            )
        )


def test_the_output_guard_covers_a_field_no_input_check_knows_about():
    """The agreement check reads the produced document, not a list of fields."""
    document = td.task_definition_document(spec())
    document["someFieldAddedLater"] = ident.execution_role_arn(OTHER_BINDING)
    with pytest.raises(DocumentRefused):
        td._refuse_document_naming_another_crew(document, BINDING)


def test_a_document_coherent_about_the_wrong_crew_is_still_refused():
    """Internal agreement is not enough; it must agree with what the inputs said."""
    other = td.task_definition_document(
        spec(
            secrets=[secret_ref("backoffice", td.MODEL_CREDENTIAL_ENV)],
        )
    )
    with pytest.raises(DocumentRefused, match="established from its inputs"):
        td._refuse_document_naming_another_crew(other, BINDING)


@pytest.mark.parametrize(
    "image",
    [
        f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/kirocrew-crew:latest",
        f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/kirocrew-crew",
        "repo@sha256:" + "b" * 63,
        "repo@sha256:" + "B" * 64,
        "repo@sha512:" + "b" * 64,
        "repo@" + "b" * 64,
        "",
    ],
)
def test_an_image_reference_that_does_not_pin_content_is_refused(image):
    """A movable tag would let the content behind a revision key change."""
    with pytest.raises(DocumentRefused, match="digest-pinned"):
        td.task_definition_document(spec(image=image))


def test_a_digest_pinned_image_is_carried_through_unchanged():
    assert container(td.task_definition_document(spec()))["image"] == IMAGE


@pytest.mark.parametrize("architecture", ["arm64", "X86", "", "AMD64"])
def test_an_unknown_cpu_architecture_is_refused(architecture):
    with pytest.raises(DocumentRefused, match="cpu architecture"):
        td.task_definition_document(spec(cpu_architecture=architecture))


def test_the_log_configuration_is_the_awslogs_driver_with_the_spec_values():
    log = td.LogSpec(region=REGION, stream_prefix="crew")
    document = td.task_definition_document(spec(log=log))
    assert container(document)["logConfiguration"] == {
        "logDriver": "awslogs",
        "options": {
            "awslogs-group": ident.log_group_name(BINDING),
            "awslogs-region": log.region,
            "awslogs-stream-prefix": log.stream_prefix,
        },
    }


def test_the_default_log_spec_carries_no_group_for_a_caller_to_hold():
    assert td.default_log_spec(REGION) == td.LogSpec(
        region=REGION,
        stream_prefix=td.CREW_CONTAINER_NAME,
    )
    assert "log_group" not in {f.name for f in dataclasses.fields(td.LogSpec)}


def test_the_emitted_log_group_is_the_crews_own_whatever_the_caller_passes():
    """The group decides where every turn's transcript lands, so it is derived.

    A caller-supplied group could name another crew's, and nothing would catch it:
    a log group name is not an ARN, so the walk that refuses a foreign crew's ARN
    never sees it.
    """
    document = td.task_definition_document(
        spec(log=td.LogSpec(region="eu-west-1", stream_prefix="p"))
    )
    options = container(document)["logConfiguration"]["options"]
    assert options["awslogs-group"] == ident.log_group_name(BINDING)
    assert options["awslogs-region"] == "eu-west-1"


def test_the_fingerprint_scheme_is_hashed_with_the_payload():
    """A change to which fields are hashed becomes a new key, not a collision."""
    first = td.revision_fingerprint(spec())
    assert len(first) == 64
    assert int(first, 16) >= 0


# ── ECS Exec's effect on the definition ───────────────────────────────────────


def test_the_container_runs_an_init_process_to_reap_the_ssm_agents_children():
    """AWS recommends an init process specifically for ECS Exec.

    The platform bind-mounts its SSM agent into the task, and that agent leaves
    child processes behind. With no pid 1 willing to reap them they accumulate as
    zombies for the task's whole life, so this is set for the same reason the
    channel is enabled at all.
    """
    document = td.task_definition_document(spec())
    assert container(document)["linuxParameters"]["initProcessEnabled"] is True


def test_the_init_process_is_on_the_definition_because_runtask_cannot_override_it():
    """``linuxParameters`` is beyond RunTask's reach, so the definition must carry it.

    Stated as a test rather than a comment because the alternative -- setting it in
    a container override -- would be silently dropped: neither override allowlist
    admits ``linuxParameters``, so the task would run with no init process and
    nothing would report a problem.
    """
    from kiro_crew.cloud.fargate import runtask as rt

    assert "linuxParameters" not in rt.TASK_OVERRIDE_KEYS
    assert "linuxParameters" not in rt.CONTAINER_OVERRIDE_KEYS
    document = td.task_definition_document(spec())
    assert "linuxParameters" in container(document)


def test_the_revision_scheme_was_bumped_when_the_document_gained_a_field():
    """A key computed under the old scheme must not describe the new document.

    The hashed FIELDS did not change when ``initProcessEnabled`` was added, so
    without a scheme bump a revision registered before the change carries an
    IDENTICAL key while running a DIFFERENT document -- and a caller confirming
    "revision N holds the content this spec describes" would accept a task with no
    init process. The new field is a constant and so could never have
    discriminated the two by being hashed; the scheme is the only thing that can.
    """
    assert td.FINGERPRINT_SCHEME == 2


def test_the_scheme_actually_participates_in_the_key(monkeypatch):
    """The bump above is only protection if the scheme reaches the hash."""
    current = td.revision_fingerprint(spec())
    monkeypatch.setattr(td, "FINGERPRINT_SCHEME", 1)
    under_old_scheme = td.revision_fingerprint(spec())
    assert current != under_old_scheme
