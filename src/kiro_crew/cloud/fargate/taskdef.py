"""The ``RegisterTaskDefinition`` document for a crew's Fargate task, as data.

Nothing here calls AWS. Every AWS interaction in this package goes through the
single ``aws`` CLI chokepoint in :mod:`kiro_crew.cloud.aws`, which refuses a
non-read-only call from an agent session. Producing the document as a value
instead is what makes the two security decisions it carries reviewable without a
credential: they are properties of a ``dict``, and a test can state them.

## What a revision is keyed on

One family per crew, one revision per (image digest, secret ARN set, cpu
architecture, log configuration, store). The key is not a preference, it is what
``RunTask`` can and cannot override. ``RunTask`` overrides ``cpu``, ``memory``,
``ephemeralStorage``, ``taskRoleArn``, ``executionRoleArn`` and a container's
``command`` and ``environment``. It cannot override ``image``, ``secrets``,
``logConfiguration``, ``runtimePlatform``, ``volumes`` or ``mountPoints``. Those
are therefore the only fields a launch cannot bend at run time, so they are the
only ones that can force a new revision.

Two consequences follow, and :func:`revision_fingerprint` is written so both are
testable rather than asserted. Keying on size is wrong, because size is an
override. Registering per launch is wrong, because ``RegisterTaskDefinition``
has no upsert and leaks a revision on every call. Steady state is one API call,
two on the first launch of a new digest.

``portMappings``, ``networkMode`` and ``requiresCompatibilities`` are also
beyond ``RunTask``'s reach and are still not in the key, because the key is the
fields that are unoverridable AND variable. The front port is a constant of the
image, so it cannot vary and cannot force a revision. It is a module constant
here rather than an input for exactly that reason.

## How the credential arrives

Through ``secrets[].valueFrom``, fetched by the execution role before the
container starts. Not through a ``ContainerOverride``, which has no ``secrets``
field at all: the only way to put a credential in a ``RunTask`` request is
``environment``, in plain text, where it is written to the CloudTrail record of
the request and can be read back out of ``DescribeTasks``. The value in question
is a long-lived model credential.

:func:`task_definition_document` therefore refuses a spec that does not deliver
the model credential through ``secrets``, and
:func:`kiro_crew.cloud.fargate.runtask.run_task_request` refuses an
``environment`` override naming anything ``secrets`` delivers. The two halves
are one property: the credential always arrives, and it arrives only this way.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Collection, Mapping, Optional, Sequence

from kiro_crew.cloud.fargate.identity import (
    CrewBinding,
    DocumentRefused,
    SecretRef,
    agree,
    bindings_in_document,
    execution_role_arn,
    log_group_name,
    secret_env_name,
    sole_binding,
    task_family,
    task_role_arn,
    validated_region,
)
from kiro_crew.cloud.iam import MANAGED_TAG_KEY

#: The single container in a crew task. Named so a ``ContainerOverride`` can
#: address it: an override without a matching container name is ignored.
CREW_CONTAINER_NAME = "crew"

#: The environment variable carrying the model credential inside the container.
#: The container's own definition of this name is the authority; a test pins the
#: two together, because a drift here delivers the secret under a name the
#: backend does not read and the backend refuses to start.
MODEL_CREDENTIAL_ENV = "KIRO_API_KEY"

#: The front process's control-plane secret. It gates every route except the two
#: customer ones and keys the audit HMAC, so its value is a credential in the
#: same sense the model key's is.
CONTROL_SECRET_ENV = "SMC_CONTROL_SECRET"

#: Every environment variable whose value the container reads as a credential.
#: A ``RunTask`` request refuses all of them, and the refusal is over this SET
#: rather than over any one name: covering one credential and not its sibling is
#: how a guard passes review while the same path stays open beside it.
#:
#: The container curates the set, not this module. It marks a name as a
#: credential in one of two ways, both of which a test derives from the container
#: source with ``ast`` in both directions: the supervisor POPS it from the model
#: worker's environment (so prompt-reachable code cannot read it), or
#: ``require_api_key`` refuses to start without it. A credential added there
#: fails that test until it is refused here, and a name refused here that the
#: container does not read shows up as drift rather than accumulating quietly.
CREDENTIAL_ENV = frozenset({MODEL_CREDENTIAL_ENV, CONTROL_SECRET_ENV})

#: The port the task's front process listens on. A constant of the image, not a
#: launch parameter, so ``portMappings`` can stay out of the revision key.
FRONT_PORT = 8080

#: The data home inside the container: where session transcripts, the archive,
#: artifacts and the installed crew bundle live. A constant of the image, which
#: creates this directory and sets ``KIROCREW_HOME``, ``SMC_DATA_HOME`` and
#: ``SMC_CONFIG_DIR`` to it, so a mount anywhere else backs a directory nothing
#: reads and leaves the transcripts on the task's own disk. Pinned to the image's
#: own ``ENV`` by a test that reads the Dockerfile, because the two agreeing is
#: the whole property and a drift on either side is silent.
#:
#: POSIX-only by construction, and not a host path. It is a path inside a Linux
#: container, emitted as a string into the ``RegisterTaskDefinition`` document
#: beside ``operatingSystemFamily`` LINUX; nothing on the machine producing the
#: document ever opens it. Resolving it through the running host's temporary
#: directory or path rules would put that host's layout into an AWS API document,
#: which is why it is spelled literally here.
CREW_DATA_HOME = "/var/lib/kirocrew"

#: Name tying the task's volume to the container's mount point. Both halves are
#: generated here, so the name is a module constant rather than an input: a
#: ``mountPoints[].sourceVolume`` naming no declared volume is refused by
#: ``RegisterTaskDefinition``, and a caller cannot get a value it never supplies
#: wrong.
CREW_STORE_VOLUME_NAME = "crew-data"

#: Task-level cpu and memory exist only because ``RegisterTaskDefinition``
#: requires them for a Fargate task. Every launch overrides them, so this pair
#: is the smallest valid Fargate combination and never an intended runtime
#: shape. ``run_task_request`` refuses a launch that supplies no size rather
#: than letting this floor become the size a task actually runs at.
REGISTRATION_CPU = "256"
REGISTRATION_MEMORY = "512"

#: Fargate CPU architectures. Part of ``runtimePlatform``, so part of the key.
CPU_ARCHITECTURES = frozenset({"X86_64", "ARM64"})

#: The only operating-system family a crew image is built for.
OPERATING_SYSTEM_FAMILY = "LINUX"

#: Tag recording the revision key on the revision itself, so the account can
#: answer what a revision was keyed on. Without it a lost local cache has no
#: witness, and the launcher re-registers, which is the revision leak the key
#: exists to prevent.
FINGERPRINT_TAG_KEY = "kirocrew:revision-key"

#: Tag naming the crew, so a revision and a task can be attributed without
#: parsing an ARN out of them.
#: The value the managed marker must carry. It is DERIVED and no caller reaches it.
#:
#: Whatever identifies a resource as this system's is what teardown finds it by,
#: and the rest of the code base matches this tag BY VALUE, not merely by key:
#: ``cloud/ec2.py`` discovers with ``Key=kirocrew:managed,Values=true`` and refuses
#: to touch a stack whose value is not ``true``, and ``cloud/iam.py`` conditions
#: resource permissions on ``aws:ResourceTag/kirocrew:managed`` equalling ``true``.
#: A task carrying any other value is therefore a RUNNING task that teardown does
#: not enumerate and the teardown role is not permitted to stop, billing in the
#: owner's account with nothing pointing at it. That is the failure the RFC's third
#: exit criterion is about, so the marker is not a caller's field to fill.
MANAGED_TAG_VALUE = "true"

CREW_TAG_KEY = "kirocrew:crew"

#: Version of the fingerprint payload, hashed with it. A future change to which
#: fields are hashed becomes a visibly different key rather than a silent
#: collision with keys computed under the old shape.
#:
#: Scheme 3 hashes the store alongside the other four unoverridable fields, and
#: the container definition carries ``linuxParameters.initProcessEnabled``. Two
#: separate reasons a key must not be read across schemes: a hashed field set that
#: differs, and a constant in the document that does not appear in the payload at
#: all. The second is why the scheme exists -- hashing cannot tell two documents
#: apart over a field neither of them varies -- so a caller confirming "revision N
#: holds the content this spec describes" compares schemes, not only hashes.
FINGERPRINT_SCHEME = 3

#: A digest-pinned image reference: ``<repository>@sha256:<64 hex>``. A tag is
#: refused. A tag can be moved after a revision is registered, which leaves the
#: revision key identifying something other than the image content, and that
#: identity is the premise every property built on the key depends on.
_DIGEST_REF_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}\Z")

#: An EFS file system id: ``fs-`` and 8 or 17 lowercase hex digits. Both lengths
#: are live -- the short form predates the long one and existing file systems
#: still carry it -- so accepting one and not the other refuses a real store.
#:
#: Anchored at both ends and lowercase-only, which refuses a surrounding space and
#: a re-cased id rather than repairing either. An id is a NAME: the account has
#: exactly one spelling of it, so a value that needs repair to match is a value
#: that came from somewhere other than the file system it is meant to name.
_FILE_SYSTEM_ID_RE = re.compile(r"^fs-(?:[0-9a-f]{8}|[0-9a-f]{17})\Z")

#: An EFS access point id, same two lengths and the same anchoring, for the same
#: reasons.
_ACCESS_POINT_ID_RE = re.compile(r"^fsap-(?:[0-9a-f]{8}|[0-9a-f]{17})\Z")


@dataclass(frozen=True)
class LogSpec:
    """Where a crew task's container log stream goes, minus the part it decides.

    The log GROUP is deliberately not a field. It is derived from the crew inside
    :func:`task_definition_document`, because it decides where the transcript of
    every turn is written and a caller-supplied group can name another crew's.
    Nothing catches that: a log group name is not an ARN, so the document walk that
    refuses a foreign crew's ARN never sees it, and the task starts, answers, and
    writes its turns where the wrong people can read them.

    What remains is genuinely the caller's. The region says which regional log
    endpoint receives the group, and the stream prefix distinguishes streams inside
    the crew's own group, which the crew already fixes, so neither can reach
    another crew. Both are validated on construction, because each empty value
    fails the same way: the task runs and produces no readable log stream.
    """

    region: str
    stream_prefix: str

    def __post_init__(self) -> None:
        validated_region(self.region, source="log configuration")
        if not self.stream_prefix.strip():
            raise DocumentRefused(
                f"log configuration stream_prefix={self.stream_prefix!r} is empty, so the "
                "task would run and produce no readable log stream"
            )


@dataclass(frozen=True)
class StoreSpec:
    """The file system backing a crew's data home, named exactly or refused.

    A task's own disk is erased when the task stops, and the only storage knob a
    ``RunTask`` request can set is ``ephemeralStorage``, which is that disk. So a
    crew whose data home is not a declared volume loses every transcript, every
    archived session and its installed bundle the moment it stops -- and it comes
    back looking healthy, which is why the loss is worth refusing rather than
    warning about. ``volumes`` and ``mountPoints`` live only on the task
    DEFINITION, so this is the one place the store can be named.

    Both ids are validated on construction, and an invalid one fails the same way:
    ``RegisterTaskDefinition`` accepts the document, and the TASK then fails to
    start on a mount error, after a revision naming a nonexistent file system is
    already durable in the account.

    ``transit_encryption`` and ``iam`` authorization are NOT fields. Both are forced
    on: the traffic is the crew's transcripts and the AWS default is off, and an
    un-authorized mount is mountable by any task with network reach to the file
    system. A field for either would make the safe value a thing to remember; see
    :func:`_store_volume`.
    """

    file_system_id: str
    #: An EFS access point, which fixes the POSIX user and group every file
    #: operation on the mount runs as. Empty means none, and that is a real
    #: choice rather than an oversight: the container runs as a non-root user, so
    #: without an access point the file system's own root must already be
    #: writable by that user, which is the stack's job where it creates the file
    #: system. With one, the access point does it and the stack does not have to.
    access_point_id: str = ""

    def __post_init__(self) -> None:
        if not self.file_system_id.strip():
            raise DocumentRefused(
                "the store names no file system, so the crew's data home would be the "
                "task's own disk: every transcript, the session archive and the installed "
                "bundle are erased when the task stops. Name the EFS file system that "
                "backs it, as fs-<8 or 17 hex>"
            )
        if not _FILE_SYSTEM_ID_RE.match(self.file_system_id):
            raise DocumentRefused(
                f"store file system id {self.file_system_id!r} is not an EFS file system id. "
                "A revision naming one that does not resolve registers, and the task then "
                "fails to start on a mount error. Use fs-<8 or 17 lowercase hex>"
            )
        if self.access_point_id and not _ACCESS_POINT_ID_RE.match(self.access_point_id):
            raise DocumentRefused(
                f"store access point id {self.access_point_id!r} is not an EFS access point "
                "id. Use fsap-<8 or 17 lowercase hex>, or leave it empty to mount the file "
                "system root"
            )


@dataclass(frozen=True)
class TaskDefinitionSpec:
    """Everything a crew's task definition is built from.

    Deliberately holds no crew name, no role ARN, and no environment-variable
    name. The crew is read out of the secret ARNs, both roles are derived from
    it, and each secret's destination variable is derived from its own name
    (:func:`kiro_crew.cloud.fargate.identity.secret_env_name`). None of those can
    therefore disagree with the secrets the definition delivers: the
    disagreement is unrepresentable rather than refused.

    ``log`` and ``image`` stay inputs, and the reason is which way each one fails.
    A foreign log group fails at task start, because a per-crew execution role
    carries ``logs`` permission for its own group only, so IAM can be the guard
    there. One image serves every crew by design, so its registry account is not
    a crew-identity question, and the content behind it is pinned by digest.
    """

    image: str
    secrets: Sequence[SecretRef]
    cpu_architecture: str
    log: LogSpec
    #: The file system backing the data home, or ``None`` for a task whose data
    #: home is its own disk and whose sessions therefore end with it.
    #:
    #: REQUIRED and carries no default, which is the point: the ephemeral answer
    #: stays representable, because a crew that runs one job and exits does not
    #: need a file system, but it has to be WRITTEN at every construction site
    #: instead of being what a caller gets by not thinking about storage. An
    #: absent field cannot be reviewed; ``store=None`` can.
    store: Optional[StoreSpec]


def secret_destinations_for(secrets: Sequence[SecretRef]) -> dict[str, SecretRef]:
    """Each container variable *secrets* delivers, mapped to the secret behind it.

    Takes the REFERENCES rather than a whole spec, because that is all the rule needs. It
    is split out so a caller holding only secrets -- the launcher's config gate, deciding
    whether a saved block names the model credential -- can apply this exact rule instead
    of approximating it. Approximating it is what registered a lane the engine then
    refused, three separate times.

    Refuses two references whose derived variable is the same. Two secrets competing for
    one destination have no defined winner, and the container would read whichever the
    document happened to list first.
    """
    destinations: dict[str, SecretRef] = {}
    for ref in secrets:
        name = secret_env_name(ref, source="secrets[].valueFrom")
        if name in destinations:
            raise DocumentRefused(
                f"two secrets deliver {name}: {destinations[name].arn!r} and {ref.arn!r}. One "
                "variable takes one value, so which of the two the container reads is "
                "not defined"
            )
        destinations[name] = ref
    return destinations


def secret_destinations(spec: TaskDefinitionSpec) -> dict[str, SecretRef]:
    """Each container variable this spec delivers, mapped to the secret behind it.

    The spec-shaped spelling of :func:`secret_destinations_for`, kept because every
    engine-side caller has a spec in hand. It DELEGATES rather than repeating the rule.
    """
    return secret_destinations_for(spec.secrets)


def credential_recipient(image: str, secrets: Sequence[SecretRef]) -> str:
    """Who a launch with this *image* and these *secrets* hands the model credential to.

    ONE renderer, called by both sides of the confirmation: the launcher's config renders
    what it will show an operator (``CloudConfig.fargate_config().credential_recipient()``)
    and the engine renders what it is about to launch
    (``fargate_engine.FargateLaunchEngine.provision``). A second spelling anywhere would let
    the two disagree over the same pair of values, and a comparison between two renderings is
    a comparison of the renderings, not of the recipient.

    Two values, because two of them together decide who receives it: the image, which is the
    container the credential lands in, and the ARN of the secret whose value the task's
    execution role fetches and delivers there. Which reference that is comes from
    :func:`secret_destinations_for`, not from a name match, so it is the same reference the
    task definition will actually carry.

    Raises ``DocumentRefused`` for a secret set this module already refuses, and ``KeyError``
    for one that delivers no model credential -- both are sets no lane is registered for.
    """
    credential = secret_destinations_for(secrets)[MODEL_CREDENTIAL_ENV]
    return f"{image} <- {credential.arn}"


def spec_binding(spec: TaskDefinitionSpec) -> CrewBinding:
    """The one crew every secret in a spec belongs to, or refuse.

    The single place a binding is established, so the document and the
    ``RunTask`` request that follows it cannot disagree about whose task it is.
    Both roles and the family are derived from what this returns.
    """
    return sole_binding(
        {f"secrets[{index}].valueFrom": ref.arn for index, ref in enumerate(spec.secrets)}
    )


def revision_fingerprint(spec: TaskDefinitionSpec) -> str:
    """A stable hash of exactly the fields ``RunTask`` cannot override.

    Reads ``image``, ``secrets``, ``cpu_architecture``, ``log`` and ``store`` and
    nothing else. The roles are absent because ``RunTask`` overrides both, and size
    is absent because there is no size to read.

    ``store`` is in the key for the same reason the other four are: ``volumes`` and
    ``mountPoints`` are task-definition fields a ``RunTask`` request cannot bend, and
    the store varies. Leaving it out would let one revision serve two specs whose
    data homes are different file systems -- or one persistent and one ephemeral --
    and the launcher would reuse whichever was registered first.

    A secret contributes BOTH its canonical name and its ARN, because the pair is
    what the definition delivers. Two references can carry one ARN under different
    names only if one of them is refused, but the key does not depend on that: it
    reads what the document says rather than what a reader could recover from it.
    """
    payload = {
        "scheme": FINGERPRINT_SCHEME,
        "image": spec.image,
        "secrets": sorted([ref.name, ref.arn] for ref in spec.secrets),
        "cpuArchitecture": spec.cpu_architecture,
        "logConfiguration": {
            "logGroup": log_group_name(spec_binding(spec)),
            "region": spec.log.region,
            "streamPrefix": spec.log.stream_prefix,
        },
        "store": (
            None
            if spec.store is None
            else {
                "fileSystemId": spec.store.file_system_id,
                "accessPointId": spec.store.access_point_id,
            }
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _store_volume(store: StoreSpec) -> dict[str, Any]:
    """The task's ``volumes`` entry for *store*.

    Transit encryption and IAM authorization are set here rather than accepted, and
    the reason is the AWS default in both cases. ``transitEncryption`` defaults to
    DISABLED, so a document that does not mention it mounts the crew's transcripts
    over an unencrypted NFS connection. Mount authorization without ``iam`` falls
    back to the file system's own policy plus network reach, so any task that can
    reach the mount target can mount it; with ``iam`` ENABLED the mount is
    authorized against the task role, which is derived per crew. Neither is a field,
    because a field would make the safe value something a caller has to remember and
    every caller is this module.

    ``accessPointId`` is the only part of ``authorizationConfig`` that varies. An
    access point additionally fixes the POSIX user the mount operates as and scopes
    what the mount can see to the access point's own root directory.
    """
    authorization: dict[str, Any] = {"iam": "ENABLED"}
    if store.access_point_id:
        authorization["accessPointId"] = store.access_point_id
    return {
        "name": CREW_STORE_VOLUME_NAME,
        "efsVolumeConfiguration": {
            "fileSystemId": store.file_system_id,
            "transitEncryption": "ENABLED",
            "authorizationConfig": authorization,
        },
    }


def _store_mount_point() -> dict[str, Any]:
    """The container's ``mountPoints`` entry for the crew store.

    Takes no argument, because nothing about it varies: the volume name and the
    container path are both module constants, and the mount is writable because a
    crew that cannot write its own transcripts is the failure this exists to fix.
    Produced next to :func:`_store_volume` so the two halves of one mount are added
    or removed together -- a volume no container mounts is a document that registers
    and changes nothing, which is the shape a reviewer cannot see.
    """
    return {
        "sourceVolume": CREW_STORE_VOLUME_NAME,
        "containerPath": CREW_DATA_HOME,
        "readOnly": False,
    }


def _refuse_undigested_image(image: str) -> None:
    if not _DIGEST_REF_RE.match(image):
        raise DocumentRefused(
            f"image {image!r} is not digest-pinned. A revision is keyed on the image, so a "
            "movable tag would let the content behind a key change after it was registered. "
            "Use <repository>@sha256:<64 hex>"
        )


def _refuse_absent_credential(destinations: Collection[str]) -> None:
    if MODEL_CREDENTIAL_ENV not in destinations:
        raise DocumentRefused(
            f"the definition does not deliver {MODEL_CREDENTIAL_ENV} through secrets. The task "
            "injects the model credential from Secrets Manager before the container starts, and "
            "a RunTask request cannot carry it: ContainerOverride has no secrets field, so the "
            "only remaining route is plain text in the request"
        )


def _refuse_unknown_architecture(cpu_architecture: str) -> None:
    if cpu_architecture not in CPU_ARCHITECTURES:
        raise DocumentRefused(
            f"cpu architecture {cpu_architecture!r} is not one of "
            f"{', '.join(sorted(CPU_ARCHITECTURES))}"
        )


def _refuse_document_naming_another_crew(
    document: Mapping[str, Any], expected: CrewBinding
) -> None:
    """Re-read the finished document and refuse unless it names one crew.

    The inputs were already checked, so this looks redundant and is not: it
    reads the OUTPUT, by walking it, so a field added to the document later is
    covered without being added to a list. The checked property is "no crew
    other than this one is named anywhere in what we are about to register",
    which is what the guard has to be about.
    """
    found = bindings_in_document(document)
    agreed = agree(found)
    if agreed != expected:
        raise DocumentRefused(
            f"the generated document names crew {agreed.crew!r} where {expected.crew!r} was "
            "established from its inputs"
        )


def task_definition_document(spec: TaskDefinitionSpec) -> dict[str, Any]:
    """The ``RegisterTaskDefinition`` request body for a crew, or refuse.

    Refuses, in order: an image that is not digest-pinned, a spec that does not
    deliver the model credential through ``secrets``, an unknown cpu
    architecture, secrets that do not all name one crew, and finally a produced
    document that names a crew other than the one its inputs established. Both
    roles and the family are derived from that crew rather than supplied, so
    there is no fourth refusal for a role naming the wrong one.

    A store's own refusals are made where the store is CONSTRUCTED, not here, so an
    unusable file system id cannot be held in a spec that a caller then passes to
    ``revision_fingerprint`` -- which would compute a key for a document that can
    never be registered.

    ``volumes`` and the container's ``mountPoints`` appear together, or neither
    appears. A spec whose ``store`` is ``None`` gets a document with no volume, which
    is a task whose data home is its own disk.
    """
    _refuse_undigested_image(spec.image)
    destinations = secret_destinations(spec)
    _refuse_absent_credential(destinations)
    _refuse_unknown_architecture(spec.cpu_architecture)
    binding = spec_binding(spec)
    document: dict[str, Any] = {
        "family": task_family(binding),
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": REGISTRATION_CPU,
        "memory": REGISTRATION_MEMORY,
        "runtimePlatform": {
            "cpuArchitecture": spec.cpu_architecture,
            "operatingSystemFamily": OPERATING_SYSTEM_FAMILY,
        },
        "executionRoleArn": execution_role_arn(binding),
        "taskRoleArn": task_role_arn(binding),
        "containerDefinitions": [
            {
                "name": CREW_CONTAINER_NAME,
                "image": spec.image,
                "essential": True,
                "portMappings": [{"containerPort": FRONT_PORT, "protocol": "tcp"}],
                # An init process inside the container, which AWS recommends
                # specifically for ECS Exec: the SSM agent the Fargate platform
                # bind-mounts in leaves child processes behind, and with no pid 1
                # willing to reap them they accumulate as zombies for the task's
                # whole life. Set on the definition because RunTask cannot
                # override ``linuxParameters``.
                "linuxParameters": {"initProcessEnabled": True},
                "secrets": [
                    {"name": name, "valueFrom": destinations[name].arn}
                    for name in sorted(destinations)
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": log_group_name(binding),
                        "awslogs-region": spec.log.region,
                        "awslogs-stream-prefix": spec.log.stream_prefix,
                    },
                },
                **({"mountPoints": [_store_mount_point()]} if spec.store is not None else {}),
            }
        ],
        **({"volumes": [_store_volume(spec.store)]} if spec.store is not None else {}),
        "tags": [
            {"key": MANAGED_TAG_KEY, "value": MANAGED_TAG_VALUE},
            {"key": FINGERPRINT_TAG_KEY, "value": revision_fingerprint(spec)},
            {"key": CREW_TAG_KEY, "value": binding.crew},
        ],
    }
    _refuse_document_naming_another_crew(document, binding)
    return document


def default_log_spec(region: str) -> LogSpec:
    """The conventional log configuration in one region.

    Takes no binding: the group is derived from the crew where the document is
    built, so there is no group here for a caller to hold or to pass on.
    """
    return LogSpec(region=region, stream_prefix=CREW_CONTAINER_NAME)
