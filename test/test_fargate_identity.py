"""Crew identity recovered from an ARN: the parse, and the agreement it feeds.

Every assertion here is written against the property rather than against one
instance of it. A check that pins one crew name, or one of the two fields that
can disagree, goes green while a second spelling of the same defect walks past
it.
"""

from __future__ import annotations

import dataclasses
import inspect
import typing

import pytest

from kiro_crew.cloud.fargate import identity as ident
from kiro_crew.cloud.fargate.identity import CrewBinding, DocumentRefused

ACCOUNT = "123456789012"
OTHER_ACCOUNT = "210987654321"
REGION = "us-east-1"

#: Crew names that must all survive the round trip. The suffix-shaped names are
#: the reason the parse verifies instead of stripping: ``a-exec`` derives a role
#: name ending in the same two suffixes the parse looks for.
ROUND_TRIP_CREWS = [
    "a",
    "frontdesk",
    "exec",
    "task",
    "a-exec",
    "a-task",
    "a-exec-task",
    "worker-9",
    "x" * 32,
]


def binding(crew: str, *, account: str = ACCOUNT, partition: str = "aws") -> CrewBinding:
    return CrewBinding(partition=partition, account=account, crew=crew)


def secret_name(crew: str, key: str = "KIRO_API_KEY") -> str:
    return f"kirocrew/crew/{crew}/{key}"


def secret_arn(crew: str, key: str = "KIRO_API_KEY", *, account: str = ACCOUNT) -> str:
    """A complete secret ARN as a bare string, for the readers that take one."""
    return f"arn:aws:secretsmanager:{REGION}:{account}:secret:{secret_name(crew, key)}-AbCdEf"


def secret_ref(crew: str, key: str = "KIRO_API_KEY", *, account: str = ACCOUNT) -> ident.SecretRef:
    """A secret named twice, which is what the destination-bearing readers take."""
    return ident.SecretRef(name=secret_name(crew, key), arn=secret_arn(crew, key, account=account))


@pytest.mark.parametrize("crew", ROUND_TRIP_CREWS)
@pytest.mark.parametrize("derive", [ident.execution_role_arn, ident.task_role_arn])
def test_a_derived_role_arn_parses_back_to_the_crew_it_was_derived_from(crew, derive):
    """The parse is a verification: whatever it recovers must rebuild the input."""
    expected = binding(crew)
    assert ident.parse_role_arn(derive(expected)) == expected


@pytest.mark.parametrize("crew", ROUND_TRIP_CREWS)
def test_a_secret_arn_parses_back_to_the_crew_it_names(crew):
    assert ident.parse_secret_arn(secret_ref(crew)) == binding(crew)


@pytest.mark.parametrize("segment", ["KIRO_API_KEY", "OTHER", "A", "A_B", "T2"])
def test_the_destination_variable_comes_from_the_secret_name(segment):
    """The ARN names its own destination, so nothing else has to agree with it."""
    assert ident.secret_env_name(secret_ref("a", segment)) == segment


def test_a_crew_whose_name_ends_in_a_role_suffix_is_not_confused_with_a_shorter_one():
    """``a-exec`` and ``a`` are different crews and every derived name says so."""
    long_crew, short_crew = binding("a-exec"), binding("a")
    assert ident.execution_role_arn(long_crew) != ident.execution_role_arn(short_crew)
    assert ident.parse_role_arn(ident.execution_role_arn(long_crew)).crew == "a-exec"
    assert ident.parse_role_arn(ident.execution_role_arn(short_crew)).crew == "a"
    assert ident.parse_role_arn(ident.task_role_arn(long_crew)).crew == "a-exec"
    # The one string these two crews do share is a family against a role name,
    # which are different name-spaces and never compared to each other.
    assert ident.task_family(long_crew) not in {
        ident.task_family(short_crew),
        ident.log_group_name(short_crew),
    }


@pytest.mark.parametrize(
    "derive",
    [ident.task_family, ident.execution_role_arn, ident.task_role_arn, ident.log_group_name],
)
def test_no_two_crews_share_a_derived_name(derive):
    """Derivation is injective per name-space, so a name identifies one crew."""
    produced = {derive(binding(crew)) for crew in ROUND_TRIP_CREWS}
    assert len(produced) == len(ROUND_TRIP_CREWS)


@pytest.mark.parametrize(
    "crew",
    ["", "-a", "a-", "A", "a_b", "a/b", "a" * 33, "a b", "a.b", "0" * 33],
)
def test_a_name_outside_the_crew_charset_is_refused(crew):
    with pytest.raises(DocumentRefused):
        ident.parse_secret_arn(secret_ref(crew))


@pytest.mark.parametrize(
    "arn",
    [
        f"arn:aws:iam::{ACCOUNT}:role/kirocrew-crew-a-admin",
        f"arn:aws:iam::{ACCOUNT}:role/kirocrew-crew--exec",
        f"arn:aws:iam::{ACCOUNT}:role/kirocrew-crew-exec",
        f"arn:aws:iam::{ACCOUNT}:role/other-a-exec",
        f"arn:aws:iam::{ACCOUNT}:user/kirocrew-crew-a-exec",
        f"arn:aws:iam:{REGION}:{ACCOUNT}:role/kirocrew-crew-a-exec",
        f"arn:aws:ecs::{ACCOUNT}:role/kirocrew-crew-a-exec",
        f"arn:notaws:iam::{ACCOUNT}:role/kirocrew-crew-a-exec",
        "arn:aws:iam::12345:role/kirocrew-crew-a-exec",
        "kirocrew-crew-a-exec",
        "",
    ],
)
def test_a_role_arn_that_is_not_a_derived_crew_role_is_refused(arn):
    with pytest.raises(DocumentRefused):
        ident.parse_role_arn(arn)


#: Every field of CrewBinding, with a value that is not one. Constructed directly
#: rather than through a parser, because the parsers are not the only entry point
#: and a binding built by hand derives the same names.
NOT_A_BINDING = {
    "partition": ["", "gcp", "AWS", "aws "],
    "account": ["", "1234", "12345678901a", "1234567890123"],
    "crew": ["", "-a", "a-", "A", "a_b", "a" * 33, "a/b"],
}


@pytest.mark.parametrize(
    "field,value",
    [(field, value) for field, values in sorted(NOT_A_BINDING.items()) for value in values],
)
def test_a_binding_that_would_derive_a_name_naming_no_crew_cannot_be_constructed(field, value):
    """Validated in the type, so every derived name is well-formed by construction.

    An unchecked ``crew=""`` derives the role ``kirocrew-crew--exec`` and the
    family ``kirocrew-crew-``, which name no crew and collide across every crew
    reaching them. Checking in the type rather than in each deriver is what stops
    the guarantee depending on which entry point the caller used.
    """
    fields = {"partition": "aws", "account": ACCOUNT, "crew": "a", field: value}
    with pytest.raises(DocumentRefused):
        ident.CrewBinding(**fields)


def test_every_binding_field_is_swept():
    """A field added to CrewBinding must be decided, not defaulted into silence."""
    declared = {f.name for f in dataclasses.fields(ident.CrewBinding)}
    assert declared == set(NOT_A_BINDING)


@pytest.mark.parametrize("region", ["", " ", "us west 2", "a" * 33])
def test_both_readers_of_a_region_refuse_the_same_values(region):
    """An ARN's region and a log configuration's region are one rule, not two."""
    with pytest.raises(DocumentRefused):
        ident.validated_region(region)


@pytest.mark.parametrize(
    "crew",
    ["", "-a", "a-", "A", "a_b", "a/b", "a" * 33, "a b", "a.b", "0" * 33],
)
def test_the_crew_charset_is_one_rule_whoever_asks(crew):
    """The same table the ARN readers refuse, refused by the name check on its own.

    A crew name is read before any ARN exists -- the bundle builder decides one while the
    operator is still choosing what the bundle carries -- so the charset has to be askable
    without an ARN to wrap it in. Sharing the table with
    ``test_a_name_outside_the_crew_charset_is_refused`` is the point: one table rather than
    two, because two tables that have to agree is the trap two validators already were.
    """
    with pytest.raises(DocumentRefused):
        ident.validated_crew_name(crew)


def test_the_name_check_admits_exactly_what_a_derivation_accepts():
    """Non-vacuity, and the bound stated at both ends.

    A single character and the full 32 are the edges the pattern has to admit, and every
    derived name is built from an admitted crew, so a check that refused them would refuse
    a launchable crew.
    """
    for crew in ("a", "0", "a-b", "a" * 32, "a" + "-" * 30 + "b"):
        assert ident.validated_crew_name(crew) == crew
        binding = ident.CrewBinding(partition="aws", account=ACCOUNT, crew=crew)
        assert ident.task_family(binding).endswith(crew)


def test_the_charset_is_reachable_without_reading_the_pattern():
    """A caller that cannot import the question will re-spell the answer.

    That is the failure the package surface exists to stop, and the crew charset is on the
    limb it names: a caller cannot build an acceptable crew name without reading it.
    """
    from kiro_crew.cloud import fargate as fargate_package

    assert "validated_crew_name" in fargate_package.__all__
    assert fargate_package.validated_crew_name is ident.validated_crew_name


#: One table rather than two, because two tables that have to agree is the same
#: trap as two validators: the looser one is the one an attacker reaches for, and
#: nothing makes a case added to one appear in the other.
GOOD_NAME = "kirocrew/crew/a/KIRO_API_KEY"


def _arn(name: str, *, service: str = "secretsmanager", kind: str = "secret", region=None) -> str:
    return f"arn:aws:{service}:{REGION if region is None else region}:{ACCOUNT}:{kind}:{name}"


UNUSABLE_SECRET_REFERENCES = [
    # No six-character suffix: resolved by search, so it can return a
    # different secret than the one written.
    ident.SecretRef(GOOD_NAME, _arn(GOOD_NAME)),
    # A longer tail. This is the complete ARN of a secret whose own name ends in
    # a suffix-shaped segment, so claiming it is KIRO_API_KEY names a different
    # secret than the one the ARN resolves to. This is the reading the previous
    # pattern accepted.
    ident.SecretRef(GOOD_NAME, _arn(f"{GOOD_NAME}-AbCdEf-XyZ123")),
    # A name outside the crew convention: an ENV_NAME segment has no hyphen, so
    # the module refuses to deliver a secret it cannot name.
    ident.SecretRef(f"{GOOD_NAME}-AbCdEf", _arn(f"{GOOD_NAME}-AbCdEf-XyZ123")),
    # The name and the ARN describe different secrets.
    ident.SecretRef("kirocrew/crew/a/OTHER_KEY", _arn(f"{GOOD_NAME}-AbCdEf")),
    # The name and the ARN name different crews.
    ident.SecretRef("kirocrew/crew/b/KIRO_API_KEY", _arn(f"{GOOD_NAME}-AbCdEf")),
    # A JSON-key or version tail makes one reference mean part of a secret.
    ident.SecretRef(GOOD_NAME, _arn(f"{GOOD_NAME}-AbCdEf:k::")),
    ident.SecretRef("other/crew/a/KIRO_API_KEY", _arn("other/crew/a/KIRO_API_KEY-AbCdEf")),
    # A conforming NAME paired with an ARN outside the crew namespace, and with one
    # that stops at the crew. The name claiming the namespace is not evidence that
    # the ARN sits in it, so the ARN is checked on its own terms.
    ident.SecretRef(GOOD_NAME, _arn("other/crew/a/KIRO_API_KEY-AbCdEf")),
    ident.SecretRef(GOOD_NAME, _arn("kirocrew/crew/a")),
    ident.SecretRef("kirocrew/crew/a", _arn("kirocrew/crew/a-AbCdEf")),
    ident.SecretRef(GOOD_NAME, _arn(f"{GOOD_NAME}-AbCdEf", kind="parameter")),
    ident.SecretRef(GOOD_NAME, _arn(f"{GOOD_NAME}-AbCdEf", service="ssm")),
    # An empty region is not a region, and both readers must say so.
    ident.SecretRef(GOOD_NAME, _arn(f"{GOOD_NAME}-AbCdEf", region="")),
    ident.SecretRef(GOOD_NAME, f"arn:aws:secretsmanager:{REGION}:1:secret:{GOOD_NAME}-AbCdEf"),
    ident.SecretRef(GOOD_NAME, f"arn:aws:iam::{ACCOUNT}:role/kirocrew-crew-a-exec"),
    ident.SecretRef(GOOD_NAME, "not-an-arn"),
]


@pytest.mark.parametrize("ref", UNUSABLE_SECRET_REFERENCES, ids=lambda r: f"{r.name}|{r.arn}"[-52:])
@pytest.mark.parametrize("reader", [ident.parse_secret_arn, ident.secret_env_name])
def test_both_readers_of_a_reference_refuse_the_same_ones(ref, reader):
    """One reference cannot be readable to one function and refused by the other."""
    with pytest.raises(DocumentRefused):
        reader(ref)


def test_no_reader_recovers_a_destination_from_an_arn_alone():
    """The inversion is enforced by the signature, not just performed once.

    A secret's destination variable can only be read from a reference that states
    the secret's name, because an ARN alone does not say where the name ends. This
    fails if either reader is given back a plain-string entry point, which is how
    the practice would return: one convenient overload, and the split is being
    guessed again.
    """
    for reader in (ident.parse_secret_arn, ident.secret_env_name):
        first = list(inspect.signature(reader).parameters)[0]
        # The module uses postponed annotations, so resolve rather than compare text.
        resolved = typing.get_type_hints(reader)[first]
        assert resolved is ident.SecretRef, (
            f"{reader.__name__} takes {resolved!r}; a destination read from a bare "
            "ARN is read from a string whose name boundary is not marked"
        )


def test_a_role_arn_is_recovered_by_round_trip_because_it_is_totally_derivable():
    """Why the role parser needs no second field and the secret reader does.

    A role ARN has no service-generated component, so re-deriving it from a
    candidate crew reproduces the input exactly and the round trip can BE the
    parse. A secret ARN ends in six characters the service chose, which nothing
    here can reproduce, so there is no round trip to verify against and the name
    is carried instead. Same defect, two different fixes, because the strings
    differ in whether they are derivable.
    """
    for crew in ROUND_TRIP_CREWS:
        arn = ident.execution_role_arn(binding(crew))
        assert ident.execution_role_arn(ident.parse_role_arn(arn)) == arn


def test_a_name_ending_in_a_suffix_shape_is_no_longer_read_as_a_different_secret():
    """The destination is not recovered by choosing where the name ends.

    A reference states the name, so the ARN is checked against it rather than
    split on the guess that its last six characters are the service's. Both
    readings of the ambiguous string are refused: one because the ARN is not the
    stated name plus one suffix, the other because the stated name is not a crew
    secret name.
    """
    arn = _arn(f"{GOOD_NAME}-AbCdEf-XyZ123")
    with pytest.raises(DocumentRefused, match="followed by one six-character"):
        ident.secret_env_name(ident.SecretRef(GOOD_NAME, arn))
    with pytest.raises(DocumentRefused, match="not a crew secret name, which is"):
        ident.secret_env_name(ident.SecretRef(f"{GOOD_NAME}-AbCdEf", arn))


def test_a_well_formed_reference_still_reads_its_destination():
    """The refusals are not a blanket one: the conforming pair works."""
    ref = ident.SecretRef(GOOD_NAME, _arn(f"{GOOD_NAME}-AbCdEf"))
    assert ident.secret_env_name(ref) == "KIRO_API_KEY"
    assert ident.parse_secret_arn(ref) == binding("a")


@pytest.mark.parametrize("component", ["partition", "account", "crew"])
def test_agreement_fails_on_any_component_of_the_identity(component):
    """Partition, account and crew are one identity, checked by one code path."""
    left = binding("a")
    right = {
        "partition": CrewBinding("aws-cn", ACCOUNT, "a"),
        "account": CrewBinding("aws", OTHER_ACCOUNT, "a"),
        "crew": CrewBinding("aws", ACCOUNT, "b"),
    }[component]
    with pytest.raises(DocumentRefused):
        ident.agree({"first": left, "second": right})


def test_agreement_on_one_crew_returns_it():
    only = binding("a")
    assert ident.agree({"first": only, "second": only}) == only


def test_naming_no_crew_at_all_is_refused_rather_than_passing_quietly():
    with pytest.raises(DocumentRefused):
        ident.agree({})


def test_sole_binding_reads_a_mixed_set_of_role_and_secret_arns():
    expected = binding("a-exec")
    assert (
        ident.sole_binding(
            {
                "executionRoleArn": ident.execution_role_arn(expected),
                "taskRoleArn": ident.task_role_arn(expected),
                "secrets[KIRO_API_KEY].valueFrom": secret_arn("a-exec"),
            }
        )
        == expected
    )


@pytest.mark.parametrize(
    "swapped",
    [
        {"executionRoleArn": ident.execution_role_arn(CrewBinding("aws", ACCOUNT, "b"))},
        {"taskRoleArn": ident.task_role_arn(CrewBinding("aws", ACCOUNT, "b"))},
        {"secrets[KIRO_API_KEY].valueFrom": secret_arn("b")},
        {"secrets[KIRO_API_KEY].valueFrom": secret_arn("a", account=OTHER_ACCOUNT)},
    ],
)
def test_one_field_naming_another_crew_refuses_the_whole_set(swapped):
    """Whichever field disagrees, the same refusal fires."""
    base = binding("a")
    labelled = {
        "executionRoleArn": ident.execution_role_arn(base),
        "taskRoleArn": ident.task_role_arn(base),
        "secrets[KIRO_API_KEY].valueFrom": secret_arn("a"),
    }
    labelled.update(swapped)
    with pytest.raises(DocumentRefused):
        ident.sole_binding(labelled)


def test_bindings_are_found_at_any_depth_and_labelled_by_position():
    """The walk is over the document, so a nested field is not a blind spot."""
    document = {
        "top": ident.execution_role_arn(binding("a")),
        "nested": {"list": [{"deep": secret_arn("a")}, "unrelated"]},
        "strangers": [
            "arn:aws:s3:::bucket/key",
            "arn:aws:ecs:us-east-1:1:cluster/c",
            "arn:aws:iam",
            "arn:",
            7,
            None,
        ],
    }
    found = ident.bindings_in_document(document)
    assert set(found) == {"top", "nested.list[0].deep"}
    assert set(found.values()) == {binding("a")}


@pytest.mark.parametrize("value", ["not-an-arn", "", "arn:aws:iam", "arn"])
def test_a_labelled_value_that_is_not_an_arn_at_all_is_refused(value):
    with pytest.raises(DocumentRefused, match="is not an ARN"):
        ident.sole_binding({"somewhere": value})


@pytest.mark.parametrize(
    "malformed",
    [
        f"arn:aws:iam::{ACCOUNT}:role/kirocrew-crew-a-admin",
        f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:kirocrew/crew/a/KIRO_API_KEY",
    ],
)
def test_a_string_claiming_the_crew_namespace_must_parse_rather_than_be_skipped(malformed):
    """Failing to parse is not a way out of the agreement check."""
    with pytest.raises(DocumentRefused):
        ident.bindings_in_document({"somewhere": malformed})


def test_the_parse_refuses_when_the_deriver_stops_agreeing_with_it(monkeypatch):
    """The round-trip is a coupling check between the parser and the deriver.

    It cannot fire today, because the two agree. It exists so that a change to
    the derived role-name format cannot leave the parser silently reporting a
    crew the ARN does not name: the parse would recover a candidate, fail to
    rebuild the input, and refuse instead of guessing.
    """
    valid = ident.execution_role_arn(binding("a"))
    monkeypatch.setattr(ident, "execution_role_arn", lambda b: valid + "-moved")
    with pytest.raises(DocumentRefused):
        ident.parse_role_arn(valid)


@pytest.mark.parametrize("crew", ROUND_TRIP_CREWS)
def test_no_two_crews_share_a_derived_name_across_the_whole_table(crew):
    """Injectivity per crew, so a derived name identifies exactly one of them."""
    others = {c for c in ROUND_TRIP_CREWS if c != crew}
    mine = ident.execution_role_arn(binding(crew))
    assert mine not in {ident.execution_role_arn(binding(c)) for c in others}
