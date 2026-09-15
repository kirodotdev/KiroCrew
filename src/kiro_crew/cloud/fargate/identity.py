"""Which crew a Fargate resource belongs to, recovered from its ARN.

A crew's model credential reaches its container through the task definition's
``secrets[].valueFrom``, fetched by the **execution role** before the container
starts. Nothing downstream can tell a wrong credential from a right one: the
container's ``require_api_key`` proves a key was supplied, not that it is this
crew's key. A document that names another crew's secret ARN therefore produces a
task that starts, answers, and serves turns under the wrong identity, with no
error at either end.

**IAM is the primary control.** Each crew's execution role is derived per crew
(:func:`execution_role_arn`), so it can be granted that crew's secret and no
other. A definition naming another crew's ARN then fails when the role cannot
read it, before the container starts.

**Refusing at generation is defence in depth, and it earns its place.** A
refusal here names the problem, while the same mistake reaching AWS surfaces as
a permission error at task start that says nothing about which crew was
confused. It is not the only barrier between crews, and it is not redundant with
the one that is: it is the barrier that explains itself. The refusal would also
be the whole of the guarantee for a deployment that shared one execution role
across crews, because such a role must hold every crew's secret ARN and its
fetch for crew A is then indistinguishable from its fetch for crew B. This
design does not share a role.

So a crew identity is parsed OUT of every ARN a document names, and generation
refuses unless they all name the same one. :class:`CrewBinding` carries the
partition and account as well as the crew, so a document naming another
account's secret is refused by the same code path as one naming another crew.

The role ARN is fully derivable from a binding, which lets the parse be a
VERIFICATION rather than a recovery: a candidate crew is accepted only when
re-deriving the ARN reproduces the input byte for byte. That is what makes the
parse unambiguous for a crew name that itself ends in ``-exec`` or ``-task``,
rather than a claim about how the extracting pattern backtracks.

A secret's own name carries the environment variable it lands in, so
:func:`secret_env_name` derives the destination from the ARN. Nothing names the
destination a second time, which is why a container cannot receive one secret's
value under another secret's name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, NoReturn

#: Resource name-space every crew resource sits in. The task-definition family
#: and both role names are built from it, so one prefix identifies a resource as
#: this launcher's and carries the crew.
FAMILY_PREFIX = "kirocrew-crew-"

#: Suffixes that distinguish the two roles a task carries. Both are derived, so
#: the set is closed and a role ARN naming anything else is refused.
EXECUTION_ROLE_SUFFIX = "exec"
TASK_ROLE_SUFFIX = "task"

#: Secret-name prefix. ``/`` is outside the crew charset, so the crew segment of
#: a secret name is delimited rather than suffix-stripped.
SECRET_NAME_PREFIX = "kirocrew/crew/"

#: CloudWatch log-group prefix for a crew's task.
LOG_GROUP_PREFIX = "/kirocrew/crew/"

#: A crew name: 1 to 32 characters, lower-case alphanumeric and inner hyphens,
#: never starting or ending with a hyphen. A trailing hyphen would make the
#: derived role name end in ``--exec``, and a leading one is not a legal start
#: for the resource names built from it.
_CREW_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")

#: An AWS partition: ``aws``, ``aws-cn``, ``aws-us-gov``.
_PARTITION_RE = re.compile(r"^aws(?:-[a-z0-9]+)*$")

#: A 12-digit account id. Anything shorter or longer is not an account.
_ACCOUNT_RE = re.compile(r"^[0-9]{12}$")

#: A region name as it appears in an ARN.
_REGION_RE = re.compile(r"^[a-z0-9-]{1,32}$")

#: A crew secret's canonical NAME, with no service suffix. This is the shape a
#: caller states; the ARN is checked against it rather than searched for a split.
_SECRET_NAME_RE = re.compile(
    r"^" + re.escape(SECRET_NAME_PREFIX) + r"(?P<crew>[^/]+)/(?P<key>[A-Za-z0-9_]+)$"
)

#: The suffix Secrets Manager appends to a secret's name in its complete ARN.
#: Six characters, and the module cannot derive them: they are chosen by the
#: service. That is the whole reason a secret's name is carried rather than
#: recovered. A role ARN is TOTALLY derivable, so :func:`parse_role_arn` can make
#: the round-trip its own parse; there is no equivalent for this suffix, and a
#: pattern that strips it must guess where the name ends.
_SECRET_SUFFIX_RE = re.compile(r"^[A-Za-z0-9]{6}$")

#: The SHAPE a crew secret's variable segment plus service suffix has. It refuses
#: an ARN carrying no suffix at all. Checking the shape is not the same as
#: recovering the name from it: this says the string could be a conforming
#: secret's complete ARN, and deliberately does not say which part is the
#: variable. The first is a property of the string; the second needs the split
#: point, which only a :class:`SecretRef` states.
_SECRET_TAIL_RE = re.compile(r"^[A-Za-z0-9_]+-[A-Za-z0-9]{6}$")


@dataclass(frozen=True)
class SecretRef:
    """A crew secret named twice: canonically, and as the ARN that resolves it.

    Both are required because the ARN alone cannot say where the secret's name
    ends. A complete secret ARN finishes with a six-character suffix the SERVICE
    chose, and nothing marks the boundary: a secret an operator named
    ``.../KIRO_API_KEY-AbCdEf`` has the complete ARN
    ``.../KIRO_API_KEY-AbCdEf-XyZ123``, while the string ``.../KIRO_API_KEY-AbCdEf``
    is simultaneously that secret's PARTIAL ARN and a well-formed complete ARN for
    a different secret called ``KIRO_API_KEY``. A pattern that strips the suffix
    must pick one reading, and picking the second one derives a destination
    variable that does not belong to the secret ECS resolves.

    Carrying ``name`` removes the guess. The split is stated rather than inferred,
    the ARN is verified to be that name plus exactly one service suffix, and the
    destination variable is read from the VERIFIED name. A name that does not
    correspond to its ARN is refused rather than reconciled, and a name outside
    the crew convention is refused outright instead of being misread, because an
    ``ENV_NAME`` segment cannot contain a hyphen.

    **What this does not do.** It does not guarantee that AWS resolves the ARN to
    the named secret. Whether ``.../KIRO_API_KEY-AbCdEf`` is the complete ARN of
    ``KIRO_API_KEY`` or the partial ARN of ``KIRO_API_KEY-AbCdEf`` depends on which
    secrets exist, which only ``DescribeSecret`` can answer and this module calls
    nothing. What is checked here is that the reference is internally consistent;
    authority for the pairing belongs to the API that created the secret, so a
    caller passes the ARN as ``CreateSecret`` returned it and the ``name`` it was
    given, never a pair assembled by hand. This check then catches a transcription
    error, and the launch engine confirms the rest.
    """

    name: str
    arn: str


class DocumentRefused(ValueError):
    """A task definition or ``RunTask`` request that must not be generated.

    Raised for a document whose own contents would make a security property
    unprovable: more than one crew named in it, a secret reference that can
    resolve to a secret other than the one written, a missing model credential,
    an image reference that does not pin content, or an override outside the set
    the request is allowed to carry.
    """


@dataclass(frozen=True)
class CrewBinding:
    """The one crew a resource belongs to, as named by its own ARN.

    Partition and account are part of the identity, not context. A secret in
    another account under the same crew name is a different secret, and reading
    it needs only a resource policy the crew's owner does not control.

    Validated on construction, so every name derived from a binding is
    well-formed because the binding exists. The parsers are not the only way to
    obtain one: a caller can construct a binding directly, and an unchecked
    ``crew=""`` would derive the role name ``kirocrew-crew--exec`` and the family
    ``kirocrew-crew-``, which name no crew and collide across every crew that
    reaches them. Checking here rather than in each deriver is what stops the
    guarantee depending on which entry point was used.
    """

    partition: str
    account: str
    crew: str

    def __post_init__(self) -> None:
        for field, value, pattern, what in (
            ("partition", self.partition, _PARTITION_RE, "an AWS partition"),
            ("account", self.account, _ACCOUNT_RE, "a 12-digit account id"),
            ("crew", self.crew, _CREW_RE, "a crew name"),
        ):
            if not pattern.match(value):
                _refuse(f"binding {field}={value!r} is not {what}")


def validated_region(region: str, *, source: str = "region") -> str:
    """The region, or refuse. One rule for every place a region is read.

    An ARN's region and a log configuration's region are the same kind of value
    and are checked by the same code, so a region readable in one place cannot be
    refused in the other. An empty region is the case worth naming: it reads as a
    global resource, and in a log configuration it sends the stream nowhere while
    the task still starts and answers.
    """
    if not _REGION_RE.match(region):
        _refuse(f"{source} region={region!r} is not a region")
    return region


def _refuse(reason: str) -> NoReturn:
    raise DocumentRefused(reason)


def _validated_crew(crew: str, *, source: str) -> str:
    """The crew name, or refuse. Charset is checked before anything derives it."""
    if not _CREW_RE.match(crew):
        _refuse(
            f"{source} names crew {crew!r}, which is not a crew name: 1 to 32 characters, "
            "lower-case letters, digits and inner hyphens only"
        )
    return crew


def _split_arn(arn: str, *, source: str) -> tuple[str, str, str, str, str]:
    """Return ``(partition, service, region, account, resource)``, or refuse.

    The resource part is returned unsplit so a caller can see the colons INSIDE
    it, which is how the optional JSON-key tail on a secret reference is
    detected rather than silently discarded.
    """
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn":
        _refuse(f"{source} is not an ARN: {arn!r}")
    partition, service, region, account, resource = parts[1], parts[2], parts[3], parts[4], parts[5]
    if not _PARTITION_RE.match(partition):
        _refuse(f"{source} names partition {partition!r}, which is not an AWS partition")
    if not _ACCOUNT_RE.match(account):
        _refuse(f"{source} names account {account!r}, which is not a 12-digit account id")
    return partition, service, region, account, resource


def parse_role_arn(arn: str, *, source: str = "role ARN") -> CrewBinding:
    """The crew a derived IAM role ARN belongs to, or refuse.

    Recovery and verification are the same step: a candidate crew is obtained by
    removing a known suffix, and it is accepted only when re-deriving the ARN
    from it reproduces the input exactly. Nothing is stripped and then trusted.

    That is what makes a crew whose own name ends in ``-exec`` unambiguous.
    ``kirocrew-crew-a-exec`` yields candidate ``a`` under the execution suffix
    and rebuilds to itself; ``kirocrew-crew-a-exec-exec`` yields candidate
    ``a-exec`` and rebuilds to itself. Exactly one candidate rebuilds, because
    derivation is injective, so there is no second reading to choose between.
    """
    partition, service, region, account, resource = _split_arn(arn, source=source)
    if service != "iam":
        _refuse(f"{source} names service {service!r}, not iam: {arn!r}")
    if region:
        _refuse(f"{source} carries region {region!r}; an IAM ARN has no region")
    if not resource.startswith("role/"):
        _refuse(f"{source} is not a role ARN: {arn!r}")
    name = resource[len("role/") :]
    for suffix, derive in (
        (EXECUTION_ROLE_SUFFIX, execution_role_arn),
        (TASK_ROLE_SUFFIX, task_role_arn),
    ):
        if not (name.startswith(FAMILY_PREFIX) and name.endswith(f"-{suffix}")):
            continue
        crew = name[len(FAMILY_PREFIX) : -len(f"-{suffix}")]
        if not _CREW_RE.match(crew):
            continue
        candidate = CrewBinding(partition=partition, account=account, crew=crew)
        if derive(candidate) == arn:
            return candidate
    _refuse(
        f"{source} does not name a crew role: a role name is "
        f"{FAMILY_PREFIX}<crew>-{EXECUTION_ROLE_SUFFIX} or "
        f"{FAMILY_PREFIX}<crew>-{TASK_ROLE_SUFFIX}, with a crew of 1 to 32 characters, "
        "lower-case letters, digits and inner hyphens only"
    )


def _crew_in_secret_arn(arn: str, *, source: str) -> CrewBinding:
    """The crew a secret ARN names, read from its DELIMITED crew segment.

    Safe where stripping a suffix is not, and the difference is the delimiter. A
    crew sits between two ``/`` characters, and ``/`` is outside the crew charset,
    so the segment's end is marked in the string rather than inferred from it. No
    secret name can make the crew segment ambiguous.

    This establishes the crew and deliberately reports nothing about the
    destination variable, because THAT is the part a secret ARN alone cannot
    settle: the boundary between the name and the service's six-character suffix
    is not marked, so a name ending in a suffix-shaped segment has two readings.
    :func:`parse_secret_arn` takes a :class:`SecretRef` for exactly that reason.

    Every rule about a crew secret ARN other than the name-to-ARN correspondence
    lives here, so the two callers cannot drift to different strictness.
    """
    partition, service, region, account, resource = _split_arn(arn, source=source)
    if service != "secretsmanager":
        _refuse(f"{source} names service {service!r}, not secretsmanager: {arn!r}")
    validated_region(region, source=source)
    if not resource.startswith("secret:"):
        _refuse(f"{source} is not a secret ARN: {arn!r}")
    full = resource[len("secret:") :]
    if ":" in full:
        _refuse(
            f"{source} carries a version or JSON-key tail: {arn!r}. A crew credential is "
            "the whole secret string, so the reference names the secret and nothing else"
        )
    if not full.startswith(SECRET_NAME_PREFIX):
        _refuse(f"{source} does not name a crew secret: {arn!r}")
    rest = full[len(SECRET_NAME_PREFIX) :]
    crew, slash, remainder = rest.partition("/")
    if not slash or not remainder:
        _refuse(
            f"{source} names no variable segment: a crew secret is "
            f"{SECRET_NAME_PREFIX}<crew>/<ENV_NAME> plus the service suffix, and {arn!r} "
            "stops at the crew"
        )
    if not _SECRET_TAIL_RE.match(remainder):
        _refuse(
            f"{source} is not a crew secret name followed by one six-character Secrets "
            f"Manager suffix: {arn!r}. A reference without the suffix is resolved by search "
            "and can return a different secret"
        )
    return CrewBinding(
        partition=partition,
        account=account,
        crew=_validated_crew(crew, source=source),
    )


def parse_secret_arn(ref: SecretRef, *, source: str = "secret reference") -> CrewBinding:
    """The crew a secret belongs to, verifying its ARN against its stated name.

    The ARN is not searched for a place to split. The name says where it ends, and
    the ARN must be exactly that name plus one six-character service suffix, so
    the two cannot describe different secrets.
    """
    match = _SECRET_NAME_RE.match(ref.name)
    if match is None:
        _refuse(
            f"{source} name {ref.name!r} is not a crew secret name, which is "
            f"{SECRET_NAME_PREFIX}<crew>/<ENV_NAME> with no service suffix"
        )
    binding = _crew_in_secret_arn(ref.arn, source=source)
    full = ref.arn.split(":", 5)[5][len("secret:") :]
    prefix = f"{ref.name}-"
    if not full.startswith(prefix) or not _SECRET_SUFFIX_RE.match(full[len(prefix) :]):
        _refuse(
            f"{source} ARN {ref.arn!r} is not the secret named {ref.name!r} plus one "
            "six-character Secrets Manager suffix. An ARN that omits the suffix is resolved "
            "by search and can return a different secret, and one carrying a longer tail "
            "names a secret whose own name this reference does not state"
        )
    return binding


def secret_env_name(ref: SecretRef, *, source: str = "secret reference") -> str:
    """The container environment variable this secret lands in, from its own name.

    Read from the name the reference states and the ARN was verified against, so a
    document cannot deliver one secret's value under another secret's variable.
    That mistake is the credential defect in its quietest form: the crew agrees,
    the reference resolves, and ``require_api_key`` sees a key present, so the
    task starts and every turn runs on the wrong value.

    Validation is :func:`parse_secret_arn`'s, by calling it rather than by
    repeating its checks. Two functions reading one reference to different
    strictness is the same trap as two lists that have to agree, and the looser
    one is the one an attacker reaches for.
    """
    parse_secret_arn(ref, source=source)
    match = _SECRET_NAME_RE.match(ref.name)
    assert match is not None  # parse_secret_arn refuses every name that fails to match
    return match.group("key")


def task_family(binding: CrewBinding) -> str:
    """The task-definition family holding every revision for this crew."""
    return f"{FAMILY_PREFIX}{binding.crew}"


def execution_role_arn(binding: CrewBinding) -> str:
    """The role ECS assumes to fetch this crew's secrets before the task starts."""
    return f"arn:{binding.partition}:iam::{binding.account}:role/{FAMILY_PREFIX}{binding.crew}-{EXECUTION_ROLE_SUFFIX}"  # noqa: E501


def task_role_arn(binding: CrewBinding) -> str:
    """The role the running container carries.

    Separate from the execution role because the container's model subprocess
    can read the task role's credential out of its own environment and act as
    it, so this role is the blast radius a turn reaches. It holds no
    ``secretsmanager`` permission: the credential is already in the container's
    environment by the time this role exists, and a turn that could re-read it
    could read every crew's.
    """
    return f"arn:{binding.partition}:iam::{binding.account}:role/{FAMILY_PREFIX}{binding.crew}-{TASK_ROLE_SUFFIX}"  # noqa: E501


def log_group_name(binding: CrewBinding) -> str:
    """The log group this crew's task writes to."""
    return f"{LOG_GROUP_PREFIX}{binding.crew}"


def _claims_to_be_ours(arn: str) -> bool:
    """True for a string that presents itself as a crew resource of this launcher.

    Deliberately loose: it asks whether the string CLAIMS the name-space, not
    whether it parses. A near-miss that claims it is refused rather than skipped
    over, so a malformed ARN cannot dodge the agreement check by failing to
    parse.
    """
    if not arn.startswith("arn:"):
        return False
    parts = arn.split(":", 5)
    if len(parts) != 6:
        return False
    service, resource = parts[2], parts[5]
    if service == "iam":
        return resource.startswith(f"role/{FAMILY_PREFIX}")
    if service == "secretsmanager":
        return resource.startswith(f"secret:{SECRET_NAME_PREFIX}")
    return False


def _parse_any(arn: str, *, source: str) -> CrewBinding:
    """Parse a crew ARN of either kind, chosen by its own service field."""
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn":
        _refuse(f"{source} is not an ARN: {arn!r}")
    if parts[2] == "iam":
        return parse_role_arn(arn, source=source)
    return _crew_in_secret_arn(arn, source=source)


def _walk_strings(node: Any, path: str) -> Iterator[tuple[str, str]]:
    """Every string in a nested document, with the path it was found at."""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            yield from _walk_strings(value, child)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _walk_strings(value, f"{path}[{index}]")


def bindings_in_document(document: Mapping[str, Any]) -> dict[str, CrewBinding]:
    """Every crew binding named anywhere in a document, keyed by where it sits.

    Walks the document rather than reading named fields, so a field added to the
    shape later is covered without anyone remembering to extend a list. That is
    the difference between guarding the property and guarding the two instances
    of it that were known when the guard was written.
    """
    found: dict[str, CrewBinding] = {}
    for path, value in _walk_strings(document, ""):
        if _claims_to_be_ours(value):
            found[path] = _parse_any(value, source=path)
    return found


def agree(bindings: Mapping[str, CrewBinding]) -> CrewBinding:
    """The single binding every entry shares, or refuse.

    An empty mapping is refused too. A document naming no crew at all cannot
    have its credential delivery checked, and silence is the answer that would
    let it through.
    """
    if not bindings:
        _refuse("no crew is named, so which crew this belongs to cannot be established")
    distinct = sorted({(b.partition, b.account, b.crew) for b in bindings.values()})
    if len(distinct) > 1:
        named = ", ".join(f"{path} -> {b.crew}@{b.account}" for path, b in sorted(bindings.items()))
        _refuse(
            f"more than one crew is named: {named}. A definition that names one crew's secret "
            "beside another crew's role hands the task a working credential for a crew it is "
            "not, and neither end reports it"
        )
    return next(iter(bindings.values()))


def sole_binding(labelled_arns: Mapping[str, str]) -> CrewBinding:
    """The one crew every ARN belongs to, or refuse. Labels name the source."""
    return agree({label: _parse_any(arn, source=label) for label, arn in labelled_arns.items()})
