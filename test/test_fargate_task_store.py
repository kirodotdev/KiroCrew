"""The crew task's persistent store: the volume, the mount point, and the refusals.

A crew's data home is the one directory whose loss is invisible from outside. The
task comes back, answers, and has forgotten every session it ever had, so the
properties here are written as refusals and as an exhaustive case table rather than
as a few examples: every shape of file system id and access point id a caller can
pass gets a verdict, including the ones that are only nearly right.

Two of these tests read files rather than call functions. The container path is a
constant of the IMAGE, and the mount is useless if the two disagree, so the test
reads the Dockerfile's own ``ENV`` and compares. Nothing else can catch that drift:
both sides stay valid on their own.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from kiro_crew.cloud.fargate import taskdef as td
from kiro_crew.cloud.fargate.identity import DocumentRefused, SecretRef

ACCOUNT = "123456789012"
REGION = "us-east-1"
DIGEST = "sha256:" + "e" * 64
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/kirocrew-crew@{DIGEST}"

#: Both live id lengths, short form and long form.
SHORT_FS = "fs-abcd1234"
LONG_FS = "fs-0123456789abcdef0"
SHORT_AP = "fsap-abcd1234"
LONG_AP = "fsap-0123456789abcdef0"

CONTAINER_SOURCE = (
    pathlib.Path(td.__file__).resolve().parents[2]
    / "apps"
    / "builtins"
    / "aws_control"
    / "crew"
    / "runtime"
    / "Dockerfile"
)


def secret_ref(key: str, *, crew: str = "frontdesk") -> SecretRef:
    name = f"kirocrew/crew/{crew}/{key}"
    return SecretRef(
        name=name,
        arn=f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{name}-AbCdEf",
    )


def spec(store) -> td.TaskDefinitionSpec:
    return td.TaskDefinitionSpec(
        image=IMAGE,
        secrets=[secret_ref(td.MODEL_CREDENTIAL_ENV)],
        cpu_architecture="ARM64",
        log=td.default_log_spec(REGION),
        store=store,
    )


def container(document) -> dict:
    return document["containerDefinitions"][0]


def efs(document) -> dict:
    return document["volumes"][0]["efsVolumeConfiguration"]


def tags(document) -> dict:
    return {tag["key"]: tag["value"] for tag in document["tags"]}


# ── the file system id: every shape, with a verdict ──────────────────────────
#
# The accepted set is exactly two lengths of lowercase hex behind `fs-`. Each
# refused row below is a value that reaches the same failure if it is not refused
# here: RegisterTaskDefinition accepts the document, so a revision naming a file
# system that does not resolve is durable in the account, and the TASK is what
# fails, on a mount error, at start.
ACCEPTED_FILE_SYSTEM_IDS = [SHORT_FS, LONG_FS]

REFUSED_FILE_SYSTEM_IDS = [
    "",  # absent: the data home would be the task's own disk
    "   ",  # whitespace only: absent, spelled at length
    "fs-",  # the prefix alone
    "fs-abcd123",  # 7 hex: one short of the short form
    "fs-abcd12345",  # 9 hex: between the two forms
    "fs-0123456789abcdef",  # 16 hex: one short of the long form
    "fs-0123456789abcdef01",  # 18 hex: one past the long form
    "fs-ABCD1234",  # upper case: the account holds one spelling
    "fs-abcg1234",  # g is not hex
    "FS-abcd1234",  # upper-case prefix
    "fsx-abcd1234",  # a different AWS file system service
    "fsap-abcd1234",  # an access point id, which is not a file system
    " fs-abcd1234",  # leading space
    "fs-abcd1234 ",  # trailing space
    "fs-abcd1234\n",  # trailing newline, which a copied value carries
    "vol-abcd1234",  # an EBS volume id
    "fs-abcd1234/x",  # a path appended
]


@pytest.mark.parametrize("file_system_id", ACCEPTED_FILE_SYSTEM_IDS)
def test_both_live_file_system_id_lengths_are_accepted(file_system_id):
    """The short form predates the long one and real file systems still carry it."""
    assert td.StoreSpec(file_system_id=file_system_id).file_system_id == file_system_id


@pytest.mark.parametrize("file_system_id", REFUSED_FILE_SYSTEM_IDS)
def test_every_other_file_system_id_shape_is_refused(file_system_id):
    with pytest.raises(DocumentRefused):
        td.StoreSpec(file_system_id=file_system_id)


def test_an_absent_file_system_id_is_refused_by_what_it_would_cost():
    """The refusal names the loss, because the id is not obviously load-bearing."""
    with pytest.raises(DocumentRefused, match="erased when the task stops"):
        td.StoreSpec(file_system_id="")


def test_a_malformed_file_system_id_is_refused_naming_the_shape():
    with pytest.raises(DocumentRefused, match=r"fs-<8 or 17 lowercase hex>"):
        td.StoreSpec(file_system_id="fs-nope")


# ── the access point id: optional, and validated when given ──────────────────

REFUSED_ACCESS_POINT_IDS = [
    "fsap-",
    "fsap-abcd123",
    "fsap-0123456789abcdef",
    "fsap-ABCD1234",
    "FSAP-abcd1234",
    "fs-abcd1234",  # a file system id, which is not an access point
    " fsap-abcd1234",
    "fsap-abcd1234 ",
]


@pytest.mark.parametrize("access_point_id", [SHORT_AP, LONG_AP])
def test_both_live_access_point_id_lengths_are_accepted(access_point_id):
    store = td.StoreSpec(file_system_id=LONG_FS, access_point_id=access_point_id)
    assert store.access_point_id == access_point_id


@pytest.mark.parametrize("access_point_id", REFUSED_ACCESS_POINT_IDS)
def test_a_malformed_access_point_id_is_refused(access_point_id):
    with pytest.raises(DocumentRefused, match="access point"):
        td.StoreSpec(file_system_id=LONG_FS, access_point_id=access_point_id)


def test_no_access_point_is_a_valid_store_rather_than_a_refusal():
    """Mounting the file system root is a real arrangement, so it is representable.

    The stack that creates the file system can make its root writable by the
    container's user instead of delegating that to an access point. Refusing this
    would refuse that design rather than a mistake. The mount is still authorized
    against the task role: only the access point is absent, not the authorization.
    """
    store = td.StoreSpec(file_system_id=LONG_FS)
    assert store.access_point_id == ""
    authorization = efs(td.task_definition_document(spec(store)))["authorizationConfig"]
    assert authorization == {"iam": "ENABLED"}


def test_every_mount_is_iam_authorized_whether_or_not_it_names_an_access_point():
    """An un-authorized mount is mountable by any task with network reach.

    The same argument that forces transit encryption, so it is checked on BOTH
    branches: a rule that holds on one of two paths is the shape a reviewer reads as
    covered while the sibling path stays open.
    """
    for store in (
        td.StoreSpec(file_system_id=LONG_FS),
        td.StoreSpec(file_system_id=LONG_FS, access_point_id=LONG_AP),
    ):
        configuration = efs(td.task_definition_document(spec(store)))
        assert configuration["authorizationConfig"]["iam"] == "ENABLED"
        assert configuration["transitEncryption"] == "ENABLED"


# ── the document: the volume and the mount point ─────────────────────────────


def test_the_volume_names_the_file_system_the_store_names():
    document = td.task_definition_document(spec(td.StoreSpec(file_system_id=LONG_FS)))
    assert document["volumes"] == [
        {
            "name": td.CREW_STORE_VOLUME_NAME,
            "efsVolumeConfiguration": {
                "fileSystemId": LONG_FS,
                "transitEncryption": "ENABLED",
                "authorizationConfig": {"iam": "ENABLED"},
            },
        }
    ]


def test_transit_encryption_is_on_because_the_aws_default_is_off():
    """The traffic is the crew's transcripts, and nothing else sets this field."""
    store = td.StoreSpec(file_system_id=LONG_FS)
    assert efs(td.task_definition_document(spec(store)))["transitEncryption"] == "ENABLED"


def test_an_access_point_is_the_only_part_of_the_authorization_that_varies():
    """It fixes the POSIX user and scopes the mount to its own root directory."""
    store = td.StoreSpec(file_system_id=LONG_FS, access_point_id=LONG_AP)
    assert efs(td.task_definition_document(spec(store)))["authorizationConfig"] == {
        "accessPointId": LONG_AP,
        "iam": "ENABLED",
    }


def test_the_container_mounts_the_volume_at_the_data_home():
    document = td.task_definition_document(spec(td.StoreSpec(file_system_id=LONG_FS)))
    assert container(document)["mountPoints"] == [
        {
            "sourceVolume": td.CREW_STORE_VOLUME_NAME,
            "containerPath": td.CREW_DATA_HOME,
            "readOnly": False,
        }
    ]


def test_the_mount_is_writable_because_a_crew_writes_its_own_transcripts():
    document = td.task_definition_document(spec(td.StoreSpec(file_system_id=LONG_FS)))
    assert container(document)["mountPoints"][0]["readOnly"] is False


def test_the_mount_point_names_a_volume_the_document_declares():
    """A sourceVolume naming no declared volume is refused by RegisterTaskDefinition.

    Checked as a RELATION between the two halves rather than against the constant,
    so renaming the volume cannot pass by being renamed in one place.
    """
    document = td.task_definition_document(spec(td.StoreSpec(file_system_id=SHORT_FS)))
    declared = {volume["name"] for volume in document["volumes"]}
    assert {mount["sourceVolume"] for mount in container(document)["mountPoints"]} <= declared


def test_an_absent_store_declares_neither_half():
    """Both halves or neither: a volume no container mounts changes nothing."""
    document = td.task_definition_document(spec(None))
    assert "volumes" not in document
    assert "mountPoints" not in container(document)


def test_a_store_does_not_disturb_the_rest_of_the_document():
    """The store adds two keys and changes the revision key, and nothing else.

    The revision-key tag is excluded and then checked the other way round, because
    the store IS in the key: a tag that matched here would mean two different data
    homes share one revision.
    """
    with_store = td.task_definition_document(spec(td.StoreSpec(file_system_id=LONG_FS)))
    without = td.task_definition_document(spec(None))
    assert set(with_store) - set(without) == {"volumes"}
    assert set(container(with_store)) - set(container(without)) == {"mountPoints"}
    for key in set(without) - {"containerDefinitions", "tags"}:
        assert with_store[key] == without[key]
    for key in set(container(without)):
        assert container(with_store)[key] == container(without)[key]
    assert tags(with_store)[td.FINGERPRINT_TAG_KEY] != tags(without)[td.FINGERPRINT_TAG_KEY]
    for key in set(tags(without)) - {td.FINGERPRINT_TAG_KEY}:
        assert tags(with_store)[key] == tags(without)[key]


# ── the revision key ─────────────────────────────────────────────────────────


def test_two_stores_on_different_file_systems_are_different_revisions():
    assert td.revision_fingerprint(
        spec(td.StoreSpec(file_system_id=LONG_FS))
    ) != td.revision_fingerprint(spec(td.StoreSpec(file_system_id=SHORT_FS)))


def test_a_persistent_and_an_ephemeral_data_home_are_different_revisions():
    """The pair most expensive to confuse: one of them silently loses every session."""
    assert td.revision_fingerprint(
        spec(td.StoreSpec(file_system_id=LONG_FS))
    ) != td.revision_fingerprint(spec(None))


def test_the_access_point_is_in_the_key_because_it_decides_what_is_mounted():
    """Same file system, different access point, is a different directory."""
    assert td.revision_fingerprint(
        spec(td.StoreSpec(file_system_id=LONG_FS, access_point_id=LONG_AP))
    ) != td.revision_fingerprint(
        spec(td.StoreSpec(file_system_id=LONG_FS, access_point_id=SHORT_AP))
    )


def test_one_store_gives_one_key():
    """Stated so the tests above read as discrimination rather than instability."""
    assert td.revision_fingerprint(
        spec(td.StoreSpec(file_system_id=LONG_FS))
    ) == td.revision_fingerprint(spec(td.StoreSpec(file_system_id=LONG_FS)))


# ── the container path is the image's, not this module's opinion ─────────────


def test_the_data_home_is_the_directory_the_image_actually_uses():
    """Mounting anywhere else backs a directory the task does not read.

    The image sets three variables to its data home and the supervisor refuses to
    start when two of them disagree, so all three are compared here: a mount at a
    path only one of them names would leave the transcripts somewhere findable and
    the resume somewhere else.
    """
    source = CONTAINER_SOURCE.read_text(encoding="utf-8")
    for variable in ("KIROCREW_HOME", "SMC_DATA_HOME", "SMC_CONFIG_DIR"):
        found = re.search(rf"^\s*{variable}=(\S+)", source, re.MULTILINE)
        assert found, f"{variable} is not set in the container image"
        assert found.group(1) == td.CREW_DATA_HOME


def test_the_image_creates_the_directory_the_mount_replaces():
    """A volume mounted over a path takes the mount's contents, not the image's.

    So the image creating the data home is not what makes it usable -- but the image
    NOT creating it would mean this module is mounting over a path the container was
    never built around, which is the drift worth catching.
    """
    source = CONTAINER_SOURCE.read_text(encoding="utf-8")
    assert td.CREW_DATA_HOME in source
