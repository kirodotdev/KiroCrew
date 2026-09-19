"""The Fargate scaffolding templates must keep agreeing with ``identity.py``.

Every name in ``kirocrew-fargate-crew.yaml`` is DERIVED on the code side, not
chosen: ``taskdef.py`` builds ``executionRoleArn``, ``taskRoleArn`` and the log
group from the crew name and then refuses a document whose ARNs disagree. So a
rename on either side does not produce a mismatch that runs -- it produces a
launch refusal, at the first launch after the rename, with nothing failing at
the moment the rename lands. These tests assert the agreement by CALLING the
derivation functions, so a rename on either side reds here instead.

Two of them guard a security property rather than a name:

* **The task role gets no secret read.** ``identity.task_role_arn``'s docstring
  states the reason -- the model credential is already in the container's
  environment by the time that role exists, so the permission buys the crew
  nothing it does not have, while a turn that could re-read a secret could read
  every crew's. Granting it would be a one-line, plausible-looking edit that
  nothing else would catch.
* **No action is a prefix wildcard.** ``cloud/aws.py``'s read allowlist documents
  why it enumerates instead of prefixing: a prefix admits
  ``secretsmanager get-secret-value`` and ``ssm get-parameter --with-decryption``.
  So ``secretsmanager:Get*`` on the execution role would grant, through the
  wildcard, exactly what the task role is denied outright.

Static and offline: these read only the template text, so they cannot flake and
need no AWS account, no credential and no deploy.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from yaml_helpers import load_with

from kiro_crew.cloud.fargate.identity import _CREW_RE  # the contract the template mirrors
from kiro_crew.cloud.fargate.identity import _PARTITION_RE  # ditto, for ARN partitions
from kiro_crew.cloud.fargate.identity import (
    SECRET_NAME_PREFIX,
    CrewBinding,
    execution_role_arn,
    log_group_name,
    task_role_arn,
)
from kiro_crew.cloud.fargate.taskdef import CPU_ARCHITECTURES

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "kiro_crew" / "cloud" / "templates"
BASE = TEMPLATES / "kirocrew-fargate-base.yaml"
CREW = TEMPLATES / "kirocrew-fargate-crew.yaml"

#: A crew name for rendering ``${Crew}`` so the rendered value can be compared
#: against what the identity functions derive for the same crew.
SAMPLE_CREW = "demo"
SAMPLE_BINDING = CrewBinding(partition="aws", account="123456789012", crew=SAMPLE_CREW)


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation's short-form intrinsics readable.

    ``yaml.safe_load`` raises on ``!Sub`` and friends, so a test that used it
    would fail for the wrong reason. Each tag becomes ``{"__tag__": name,
    "value": ...}``, which keeps the payload reachable without pretending to
    evaluate the intrinsic.
    """


def _intrinsic(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> dict[str, Any]:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"__tag__": tag_suffix.lstrip("!"), "value": value}


_CfnLoader.add_multi_constructor("!", _intrinsic)


def _load(path: Path) -> dict[str, Any]:
    # Through the repo's helper rather than a direct call with an explicit
    # loader: the two parse identically, but this keeps the safe base class as
    # the only construction path and leaves nothing for a scanner that keys on
    # the call name and cannot see that the loader subclasses SafeLoader.
    return load_with(_CfnLoader, path.read_text(encoding="utf-8"))


def _render(node: Any, crew: str = SAMPLE_CREW) -> str:
    """The literal a ``!Sub`` scalar produces once ``${Crew}`` is substituted."""
    if isinstance(node, str):
        return node
    assert isinstance(node, dict) and node.get("__tag__") == "Sub", node
    text = node["value"]
    assert isinstance(text, str), text
    return text.replace("${Crew}", crew)


def _walk(node: Any) -> Any:
    """Every mapping in a nested document, so a statement cannot hide in a branch.

    Walking beats reading named fields for the same reason ``identity.py`` walks a
    task definition: a policy moved into an ``!If`` branch, or a statement added
    under a new logical id, is covered without anyone extending a list here.
    """
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _walk(value)


def _statement_actions(mapping: dict[str, Any]) -> list[str]:
    """The actions one statement grants, as a list whether written as one or many."""
    action = mapping["Action"]
    return [action] if isinstance(action, str) else list(action)


def _actions(document: dict[str, Any]) -> list[str]:
    """Every IAM action string anywhere in the document."""
    found: list[str] = []
    for mapping in _walk(document):
        if "Action" not in mapping or "Effect" not in mapping:
            continue  # a mapping with Action but no Effect is not a statement
        found.extend(_statement_actions(mapping))
    return found


def _role(document: dict[str, Any], logical_id: str) -> dict[str, Any]:
    resource = document["Resources"][logical_id]
    assert resource["Type"] == "AWS::IAM::Role", resource["Type"]
    return resource["Properties"]


def _pattern(document: dict[str, Any], parameter: str) -> str:
    return document["Parameters"][parameter]["AllowedPattern"]


# --------------------------------------------------------------------------
# The templates must be loadable at all. Every other test depends on it, so it
# is asserted once rather than being the incidental cause of seven failures.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", [BASE, CREW], ids=["base", "crew"])
def test_template_parses_and_declares_resources(path: Path) -> None:
    document = _load(path)
    assert document["AWSTemplateFormatVersion"] == "2010-09-09"
    assert document["Resources"], f"{path.name} declares no resources"


# --------------------------------------------------------------------------
# Names: derived on the code side, mirrored here.
# --------------------------------------------------------------------------


def test_role_names_are_exactly_what_identity_derives() -> None:
    document = _load(CREW)
    expected_exec = execution_role_arn(SAMPLE_BINDING).rsplit("/", 1)[1]
    expected_task = task_role_arn(SAMPLE_BINDING).rsplit("/", 1)[1]

    assert _render(_role(document, "ExecutionRole")["RoleName"]) == expected_exec
    assert _render(_role(document, "TaskRole")["RoleName"]) == expected_task


def test_log_group_name_is_exactly_what_identity_derives() -> None:
    document = _load(CREW)
    log_group = document["Resources"]["LogGroup"]["Properties"]["LogGroupName"]
    assert _render(log_group) == log_group_name(SAMPLE_BINDING)


def test_the_crew_parameter_admits_exactly_the_crew_names_identity_admits() -> None:
    """A behavioural comparison, not a string compare of two regexes.

    Two patterns can be spelled differently and mean the same thing, and can be
    spelled almost identically and differ on the case that matters -- a trailing
    hyphen, which would derive the role name ``kirocrew-crew-demo--exec``.
    """
    template_pattern = re.compile(f"^(?:{_pattern(_load(CREW), 'Crew')})$".replace("^^", "^"))
    candidates = [
        "demo",
        "a",
        "0",
        "crew-with-inner-hyphens",
        "trailing-",
        "-leading",
        "UPPER",
        "under_score",
        "",
        "a" * 32,
        "a" * 33,
        "a--b",
    ]
    for candidate in candidates:
        mine = bool(template_pattern.fullmatch(candidate))
        theirs = bool(_CREW_RE.fullmatch(candidate))
        assert (
            mine == theirs
        ), f"{candidate!r}: template says {mine}, identity._CREW_RE says {theirs}"


# --------------------------------------------------------------------------
# Security properties.
# --------------------------------------------------------------------------


def test_the_task_role_can_read_no_secret() -> None:
    properties = _role(_load(CREW), "TaskRole")
    assert "ManagedPolicyArns" not in properties
    # Case-INSENSITIVE on purpose. The managed policy that would grant this is
    # spelled ``SecretsManagerReadWrite``, so a lower-case substring check reads
    # straight past the most likely way someone grants it.
    rendered = yaml.dump(properties).lower()
    assert "secretsmanager" not in rendered, (
        "the task role must never reach Secrets Manager: the credential is already in the "
        "container's environment, and a turn that could re-read it could read every crew's"
    )


def test_the_task_role_grants_exactly_the_ssm_channel_and_nothing_else() -> None:
    """The task role's whole grant, pinned action by action.

    The invariant is "this role grants nothing beyond what it needs", and it is
    stated exactly here rather than approximated by a blanket check that the role
    carries no policies at all. It carries one: the four ssmmessages actions that
    let the SSM agent open its channel so the crew is reachable from a laptop.
    Naming them action by action is stricter than a blanket check, not looser,
    because it also fails on a fifth action nobody argued for.

    Every absent string below is a real way this could widen. ``ssm:`` rather than
    ``ssmmessages:`` would hand the TASK the caller's session permission, so a
    prompt-reachable turn could start its own sessions. ``kms:`` is the grant a
    session-logging design would want and this lane deliberately does not have.
    """
    properties = _role(_load(CREW), "TaskRole")
    policies = properties["Policies"]
    assert len(policies) == 1, f"expected exactly one policy, found {len(policies)}"

    statements = policies[0]["PolicyDocument"]["Statement"]
    assert len(statements) == 1, f"expected one statement, found {len(statements)}"
    actions = statements[0]["Action"]

    assert set(actions) == {
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
    }, actions
    assert len(actions) == 4, f"duplicate actions would pass the set check: {actions}"

    rendered = yaml.dump(properties).lower()
    # The task never starts a session; it answers one the caller was authorised for.
    assert "ssm:startsession" not in rendered
    assert "ecs:executecommand" not in rendered
    assert "kms:" not in rendered
    # No wildcard ACTION. Resource is "*" and has to be -- an ssmmessages channel
    # has no ARN before it exists -- but a wildcard action would be unbounded.
    for action in actions:
        assert not action.endswith("*"), action


@pytest.mark.parametrize("logical_id", ["ExecutionRole", "TaskRole"], ids=["exec", "task"])
def test_each_role_is_assumable_only_on_behalf_of_this_account(logical_id: str) -> None:
    """Both roles trust ``ecs-tasks`` narrowed by ``aws:SourceAccount``.

    Without the condition the trust policy names a SERVICE, not a caller, so the
    role is assumable on behalf of any task in any account that ECS will act
    for -- the confused-deputy shape. The template says this in a comment, and a
    comment asserting what the adjacent document does not do is this line's
    recurring defect, so it is asserted here instead.
    """
    statements = _role(_load(CREW), logical_id)["AssumeRolePolicyDocument"]["Statement"]
    assert len(statements) == 1, f"expected one trust statement, found {len(statements)}"
    statement = statements[0]

    principal = statement["Principal"]
    assert principal == {"Service": "ecs-tasks.amazonaws.com"}, principal

    condition = statement.get("Condition")
    assert condition, f"{logical_id} trusts the service with no condition at all"
    source_account = condition["StringEquals"]["aws:SourceAccount"]
    assert (
        isinstance(source_account, dict) and source_account.get("__tag__") == "Ref"
    ), f"{logical_id} pins aws:SourceAccount to {source_account!r} rather than this account"
    assert source_account["value"] == "AWS::AccountId", source_account["value"]

    # aws:SourceAccount is the key that narrows this, and it is asserted above.
    # aws:SourceArn is asserted here as ACCOUNT-WIDE on purpose. ECS does not
    # support a cluster-qualified value -- AWS documents that specifying a specific
    # cluster "is not currently supported, you should use the wildcard to specify
    # all clusters" -- so a cluster-scoped ARN never matches what ECS presents and
    # every task launch fails. This assertion pins the SUPPORTED shape and refuses
    # the cluster-qualified one, so a future edit cannot reintroduce a value that
    # reads as tighter while breaking every launch.
    source_arn = condition["ArnLike"]["aws:SourceArn"]
    assert (
        isinstance(source_arn, dict) and source_arn.get("__tag__") == "Sub"
    ), f"{logical_id} pins aws:SourceArn to {source_arn!r} rather than a derived ARN"
    rendered = source_arn["value"]
    assert rendered.endswith(":*"), rendered
    assert "task/" not in rendered, (
        f"{logical_id} qualifies aws:SourceArn with a task or cluster path "
        f"({rendered}), which ECS does not support -- launches would fail"
    )
    for segment in ("${AWS::Partition}", "${AWS::Region}", "${AWS::AccountId}"):
        assert segment in rendered, f"{logical_id} hardcodes instead of {segment}: {rendered}"


def test_no_action_anywhere_is_a_prefix_wildcard() -> None:
    for path in (BASE, CREW):
        for action in _actions(_load(path)):
            assert "*" not in action, (
                f"{path.name} grants {action!r}; a prefix admits the very calls the "
                "enumeration exists to exclude"
            )


@pytest.mark.parametrize("path", [BASE, CREW], ids=["base", "crew"])
def test_no_statement_inverts_the_enumeration(path: Path) -> None:
    """No ``NotAction`` or ``NotResource`` anywhere.

    Either one turns an allow-list into a deny-list, so an ``Allow`` naming three
    excluded actions grants every other call in the service -- the same reach as
    the prefix wildcard the test above forbids, spelled in a way that check reads
    straight past because it collects only ``Action``.
    """
    for mapping in _walk(_load(path)):
        if "Effect" not in mapping:
            continue
        for inverted in ("NotAction", "NotResource"):
            assert inverted not in mapping, (
                f"{path.name} has a statement using {inverted}, which grants everything "
                "it does not name"
            )


def test_the_execution_roles_secret_read_is_scoped_to_one_crew() -> None:
    document = _load(CREW)
    reads = [
        mapping
        for mapping in _walk(_role(document, "ExecutionRole"))
        if "Action" in mapping and "secretsmanager:GetSecretValue" in _statement_actions(mapping)
    ]
    assert len(reads) == 1, f"expected exactly one secret-read statement, found {len(reads)}"

    resources = reads[0]["Resource"]
    resources = [resources] if not isinstance(resources, list) else resources
    assert resources, "the secret read names no resource"
    for resource in resources:
        rendered = _render(resource)
        assert f"secret:{SECRET_NAME_PREFIX}{SAMPLE_CREW}/" in rendered, (
            f"{rendered!r} is not scoped to this crew's secret namespace, so the role "
            "could read a sibling crew's secret"
        )


def test_each_roles_ceiling_covers_its_own_grant() -> None:
    """The invariant F1 was a violation of: a boundary caps, so it must COVER.

    An IAM permissions boundary reduces a role to the intersection of its identity
    policy and the ceiling. So every action a role's policies grant must appear in
    the boundary the template attaches to THAT role, or the grant is silently capped
    away. Getting this wrong does not fail a policy simulator or a template lint --
    it fails at launch, because ECS performs the execution role's secret read and
    log-stream open before the container starts.

    Derived from the template and the two documents rather than restated: a grant
    added to either role without a matching ceiling entry fails here. That is the
    check that distinguishes "both roles carry a boundary" from "each role carries a
    boundary that lets it do its job".
    """
    from kiro_crew.cloud import iam

    document = _load(CREW)
    ceilings = {
        "TaskRole": (
            "PermissionsBoundaryArn",
            _statement_actions(iam.crew_boundary_policy_document()["Statement"][0]),
        ),
        "ExecutionRole": (
            "ExecutionPermissionsBoundaryArn",
            _statement_actions(iam.crew_exec_boundary_policy_document()["Statement"][0]),
        ),
    }
    for logical_id, (parameter, permitted) in ceilings.items():
        properties = _role(document, logical_id)
        assert properties["PermissionsBoundary"] == {
            "__tag__": "Ref",
            "value": parameter,
        }, f"{logical_id} does not carry {parameter}: {properties['PermissionsBoundary']!r}"
        granted = {
            action
            for mapping in _walk(properties.get("Policies", []))
            if "Action" in mapping
            for action in _statement_actions(mapping)
        }
        assert granted, f"{logical_id} grants nothing, so this check proves nothing"
        uncovered = sorted(granted - set(permitted))
        assert not uncovered, (
            f"{logical_id}'s boundary does not permit {uncovered}, so the boundary caps "
            f"them away and the task cannot start"
        )


def test_neither_ceiling_admits_the_other_roles_work() -> None:
    """Two ceilings rather than their union, which is why they are two.

    A single boundary covering both roles would have to permit
    ``secretsmanager:GetSecretValue``, and the task role is the one a prompt can
    reach. Keeping the secret read out of ITS ceiling is what makes "the container
    never holds the secret-reading role" a property of the ceiling too, not only of
    the identity policy.
    """
    from kiro_crew.cloud import iam

    task = set(_statement_actions(iam.crew_boundary_policy_document()["Statement"][0]))
    execution = set(_statement_actions(iam.crew_exec_boundary_policy_document()["Statement"][0]))
    assert (
        "secretsmanager:GetSecretValue" not in task
    ), "the task role's ceiling admits the crew secret read"
    assert not any(
        a.startswith("ssmmessages:") for a in execution
    ), "the execution role's ceiling admits the SSM channel it has no use for"
    assert not task & execution, (task, execution)


def test_the_boundary_parameter_admits_only_the_one_boundary() -> None:
    """The one policy and nothing else, empty included.

    A widened pattern is how a boundary stops being a ceiling: an operator could
    pass a policy they authored, and the role's effective permissions would be
    capped by nothing meaningful. Empty is refused for its own reason:
    ``cloud/source.py ensure_crew_boundary`` creates this policy, so an empty value
    means a deployment whose two roles carry no ceiling at all, which the parameter
    rejects rather than accepting as a mode.
    """
    parameters = _load(CREW)["Parameters"]
    account = "123456789012"
    for name, policy in (
        ("PermissionsBoundaryArn", "kirocrew-crew-boundary"),
        ("ExecutionPermissionsBoundaryArn", "kirocrew-crew-exec-boundary"),
    ):
        parameter = parameters[name]
        assert "Default" not in parameter, (
            f"{name} must stay required: a default would silently deploy its role with "
            "no ceiling"
        )
        pattern = re.compile(parameter["AllowedPattern"])
        assert pattern.fullmatch(f"arn:aws:iam::{account}:policy/{policy}"), name
        for rejected in (
            "",
            f"arn:aws:iam::{account}:policy/anything-else",
            f"arn:aws:iam::{account}:policy/{policy}-2",
            f"arn:aws:iam::{account}:policy/AdministratorAccess",
            f"arn:aws:iam::123:policy/{policy}",
        ):
            assert not pattern.fullmatch(rejected), f"{name} accepted {rejected!r}"
        # Neither parameter may admit the OTHER role's ceiling: that swap is how a
        # deployment would cap a role with a ceiling sized for different work.
        other = (
            "kirocrew-crew-exec-boundary"
            if policy == "kirocrew-crew-boundary"
            else "kirocrew-crew-boundary"
        )
        assert not pattern.fullmatch(
            f"arn:aws:iam::{account}:policy/{other}"
        ), f"{name} accepts the other role's boundary"


def test_both_roles_carry_the_boundary_unconditionally() -> None:
    """No ``!If`` on either role's ``PermissionsBoundary``.

    The EC2 lane sets its boundary unconditionally, and T3's finding was that this
    template did not. A condition here would be indistinguishable from the fixed
    shape in review while re-admitting the ungoverned deployment: the parameter is
    required, so an ``!If`` on it could only ever take its true branch, and its
    presence would invite restoring the empty default that made it meaningful.
    """
    document = _load(CREW)
    assert "HasBoundary" not in document.get("Conditions", {}), (
        "the HasBoundary condition is dead once the parameter is required, and "
        "keeping it invites the empty default back"
    )
    for logical_id, parameter in (
        ("ExecutionRole", "ExecutionPermissionsBoundaryArn"),
        ("TaskRole", "PermissionsBoundaryArn"),
    ):
        boundary = _role(document, logical_id)["PermissionsBoundary"]
        assert boundary == {
            "__tag__": "Ref",
            "value": parameter,
        }, f"{logical_id} does not carry {parameter} unconditionally: {boundary!r}"


def test_both_arn_patterns_accept_exactly_the_partitions_identity_accepts() -> None:
    """Both ARN parameters must admit every partition ``CrewBinding`` admits.

    Compared against ``identity._PARTITION_RE`` candidate by candidate rather than
    by restating its alternation here: a second copy of another module's table
    drifts silently, and this drift would only surface when somebody in that
    partition tried to deploy. A pattern admitting ``aws`` alone rejects a valid
    ``aws-cn`` or ``aws-us-gov`` ARN at CloudFormation parameter validation, so the
    per-crew stack cannot deploy in those partitions at all -- while the resources
    inside the template are already partition-correct, because they build every ARN
    from ``${AWS::Partition}``.
    """
    document = _load(CREW)
    boundary = re.compile(_pattern(document, "PermissionsBoundaryArn"))
    ecr = re.compile(_pattern(document, "EcrRepositoryArn"))
    account = "123456789012"

    for partition in (
        "aws",
        "aws-cn",
        "aws-us-gov",
        "aws-iso",
        "aws-iso-b",
        "AWS",
        "aws_cn",
        "aws-",
        "gcp",
        "",
    ):
        identity_accepts = bool(_PARTITION_RE.fullmatch(partition))
        boundary_arn = f"arn:{partition}:iam::{account}:policy/kirocrew-crew-boundary"
        ecr_arn = f"arn:{partition}:ecr:us-east-1:{account}:repository/kirocrew-crew"
        assert bool(boundary.fullmatch(boundary_arn)) == identity_accepts, (
            f"PermissionsBoundaryArn and identity._PARTITION_RE disagree on partition "
            f"{partition!r} (identity accepts it: {identity_accepts})"
        )
        assert bool(ecr.fullmatch(ecr_arn)) == identity_accepts, (
            f"EcrRepositoryArn and identity._PARTITION_RE disagree on partition "
            f"{partition!r} (identity accepts it: {identity_accepts})"
        )


def test_the_shared_security_group_has_no_ingress() -> None:
    """No inbound at all: a crew is reached outbound-authenticated.

    An ingress rule added here is the difference between a crew reachable by its
    owner and one reachable by the internet, and it would not fail any functional
    check.
    """
    properties = _load(BASE)["Resources"]["TaskSecurityGroup"]["Properties"]
    assert "SecurityGroupIngress" not in properties
    assert properties["SecurityGroupEgress"], "the task must still be able to pull its image"


# --------------------------------------------------------------------------
# The outputs are the launch spec's configuration home, so their KEYS are the
# contract a later change reads. A renamed or dropped output is a stack that
# deploys and a launcher that finds nothing.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            BASE,
            {
                "ClusterName",
                "ClusterArn",
                "SecurityGroupId",
                "SubnetIds",
                "CpuArchitecture",
                "Region",
                "StackTag",
            },
        ),
        (
            CREW,
            {
                "Crew",
                "ExecutionRoleArn",
                "TaskRoleArn",
                "LogGroupName",
                "SecretNamePrefix",
                "SecretArnPattern",
            },
        ),
    ],
    ids=["base", "crew"],
)
def test_every_output_the_launch_spec_reads_is_present(path: Path, expected: set[str]) -> None:
    """Equality, not containment, in both directions.

    A subset check would pass a dropped output, and asserting only presence would
    let an output be quietly added without anyone naming what reads it. Every
    output here is one the launch spec or an operator consumes.
    """
    declared = set(_load(path)["Outputs"])
    assert declared == expected, (
        f"{path.name} outputs {sorted(declared)}; "
        f"missing {sorted(expected - declared)}, unexpected {sorted(declared - expected)}"
    )
    for key in sorted(declared):
        assert _load(path)["Outputs"][key].get("Value") is not None, f"{key} declares no Value"


def test_the_architecture_parameter_admits_exactly_what_taskdef_admits() -> None:
    """The template's AllowedValues and ``CPU_ARCHITECTURES`` are one set.

    Compared by importing that frozenset rather than by restating its members, so
    adding an architecture on the code side without offering it here -- or offering
    one here that ``_refuse_unknown_architecture`` rejects -- reds. The second
    direction is the one that would otherwise ship: the stack would deploy and the
    crew would never start.
    """
    allowed = _load(BASE)["Parameters"]["CpuArchitecture"]["AllowedValues"]
    assert set(allowed) == set(
        CPU_ARCHITECTURES
    ), f"template offers {sorted(allowed)}, taskdef accepts {sorted(CPU_ARCHITECTURES)}"


def test_the_secret_arn_output_is_exactly_the_execution_roles_grant() -> None:
    """The advertised ARN pattern and the granted one must be the same string.

    Two places state this pattern, so they can disagree: widening the policy
    without the output hides the reach, and widening the output without the policy
    sends a caller after a secret the execution role cannot fetch. Rendered with
    the same crew on both sides, so only a real divergence reds.
    """
    document = _load(CREW)
    advertised = _render(document["Outputs"]["SecretArnPattern"]["Value"])

    granted = [
        mapping["Resource"]
        for mapping in _walk(_role(document, "ExecutionRole"))
        if "Action" in mapping and "secretsmanager:GetSecretValue" in _statement_actions(mapping)
    ]
    assert len(granted) == 1, f"expected one secret-read statement, found {len(granted)}"
    resources = granted[0] if isinstance(granted[0], list) else [granted[0]]
    assert [_render(resource) for resource in resources] == [advertised], (
        f"output advertises {advertised!r}, policy grants "
        f"{[_render(resource) for resource in resources]!r}"
    )
