"""The ``lambda-microvms`` calls this lane makes, through the one ``aws`` CLI seam.

Every call goes through :func:`kiro_crew.cloud.aws.checked_json` or
:func:`~kiro_crew.cloud.aws.run_aws`, the same chokepoint the EC2 and Fargate
lanes use. There is no ``boto3`` here, and that is a decision rather than an
omission: a second AWS-call style inside one package is how two lanes end up
resolving credentials differently, and the chokepoint is where the agent-session
refusal, the credential scrub and the sandbox wrapper all live. A call that went
around it would quietly escape all three.

``endpoint_url`` exists for the local harness, which points these calls at a
loopback fake instead of the service. It is a keyword on every function rather
than module state, so a test cannot leave it set for a later caller, and a real
launch that forgot to unset it is not a thing that can happen.

The parameter names below are the CLI's own kebab-case spellings of the API's
camelCase members. Two traps the service does not document together:
``imageIdentifier`` must be an image ARN or id and never a bare name, and the
IAM action prefix for all of this is ``lambda:`` rather than ``lambda-microvms:``
-- so a denial names ``lambda:RunMicrovm`` and an IAM policy written against the
CLI's own service name grants nothing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from kiro_crew.cloud.aws import AWSError, checked_json

logger = logging.getLogger(__name__)

_SERVICE = "lambda-microvms"

#: The platform's maximum MicroVM lifetime, in seconds. Documented as
#: non-adjustable: there is no API that extends it, keep-alive traffic buys
#: liveness inside the window and never a longer window, and the clock covers
#: suspended time as well as running time. Every other number in this lane is
#: derived from it.
MAX_LIFETIME_SECONDS = 28_800

#: Largest ``runHookPayload`` this lane will send. The service's own
#: documentation contradicts itself here -- the guide says 16,384 bytes and the
#: API reference's length constraint says 4,096 -- and botocore's model agrees
#: with the smaller number. Budget against the smaller one: a payload refused at
#: ``RunMicrovm`` is a launch that fails after the activation is already minted.
MAX_RUN_HOOK_PAYLOAD_BYTES = 4_096

#: The connector that gives a VM no inbound network path. It does NOT close the
#: VM's own HTTPS endpoint, which the platform serves regardless -- so this is
#: one layer and never the boundary. The boundary on this lane is that every
#: route the guest serves authenticates its caller.
NO_INGRESS = "NO_INGRESS"

#: The connector that gives a VM outbound internet. Exactly ONE egress connector
#: may be passed: the service answers ``ValidationException`` for two, and for
#: the same connector named twice, so it is a count check rather than the
#: documented maximum.
INTERNET_EGRESS = "INTERNET_EGRESS"

#: The AWS-owned network connector every mode above is a mode OF. The two names
#: are not values the API accepts on their own: they are the last segment of this
#: connector's ARN, and passing a bare name is refused with
#: ``ValidationException: Malformed network connector ARN: INTERNET_EGRESS``.
#: Measured against the real service at both ``RunMicrovm`` and
#: ``CreateMicrovmImage``. A loopback fake cannot see this, because it accepts
#: whatever string the lane hands it.
_AWS_CONNECTOR = "arn:aws:lambda:{region}:aws:network-connector:aws-network-connector"


def connector_arn(mode: str, region: str) -> str:
    """The full connector ARN for *mode* in *region*.

    The connector is AWS-owned and region-scoped, so the ARN cannot be a module
    constant: a lane that hard-coded one region's ARN would launch in that region
    whatever the caller asked for, or fail opaquely.
    """
    if not region:
        raise ValueError(
            "a network connector ARN needs a region: the connector is AWS-owned and "
            "region-scoped, so there is no region-free spelling of it"
        )
    return f"{_AWS_CONNECTOR.format(region=region)}:{mode}"


#: Every state the service's own model declares, in the order a VM moves through
#: them. The contract test pins this against botocore's installed model.
MICROVM_STATES: tuple[str, ...] = (
    "PENDING",
    "RUNNING",
    "SUSPENDING",
    "SUSPENDED",
    "TERMINATING",
    "TERMINATED",
)

#: States from which no further transition happens.
TERMINAL_MICROVM_STATES: frozenset[str] = frozenset({"TERMINATED"})

#: Page size for ``ListMicrovms``, and the service's own maximum for it.
MAX_LIST_RESULTS = 50

#: Every state a MicroVM IMAGE may be in, as the service's own model declares.
MICROVM_IMAGE_STATES: tuple[str, ...] = (
    "CREATING",
    "CREATED",
    "CREATE_FAILED",
    "UPDATING",
    "UPDATED",
    "UPDATE_FAILED",
    "DELETING",
    "DELETE_FAILED",
    "DELETED",
)

#: Image states from which no build will ever complete.
FAILED_IMAGE_STATES: frozenset[str] = frozenset(
    {"CREATE_FAILED", "UPDATE_FAILED", "DELETING", "DELETE_FAILED", "DELETED"}
)

#: The only status at which an image VERSION may be launched. The service
#: declares exactly two, and the distinction is the whole reason a launch resolves
#: a version rather than an image: an image can exist, be CREATED, and still have
#: no version a ``RunMicrovm`` will accept.
IMAGE_VERSION_ACTIVE = "ACTIVE"
IMAGE_VERSION_STATUSES: tuple[str, ...] = ("ACTIVE", "INACTIVE")


@dataclass(frozen=True)
class MicroVm:
    """One MicroVM as the service describes it.

    Only the members this lane reads.

    ``endpoint`` is required on a ``RunMicrovm`` or ``GetMicrovm`` answer, and
    that is why nothing here guesses one: a response missing it is a response this
    lane does not understand, and a guessed endpoint is how a caller reaches
    someone else's VM. ``ListMicrovms`` is the exception and it is the service's
    own -- its item shape carries no endpoint at all -- so the two are read by two
    constructors with two required sets rather than by one lenient reader.
    """

    microvm_id: str
    state: str
    endpoint: str
    image_arn: str = ""
    image_version: str = ""
    maximum_duration_in_seconds: int = 0
    started_at: str = ""
    state_reason: str = ""

    @classmethod
    def from_response(cls, data: object) -> "MicroVm":
        """One ``RunMicrovm`` or ``GetMicrovm`` answer, which always has an endpoint."""
        return cls._read(data, required=("microvmId", "state", "endpoint"))

    @classmethod
    def from_item(cls, data: object) -> "MicroVm":
        """One ``ListMicrovms`` item, which does NOT carry an endpoint.

        The list's member shape is ``MicrovmItem`` and it has five members: the
        id, the state, the image ARN, the image version and the start time. It has
        no ``endpoint`` and no ``maximumDurationInSeconds``, so the CLI drops both
        from a list response -- and a reader that required an endpoint would refuse
        every page.

        That is not a loss, because the one caller of the list is the sweeper and
        it reads the id, the state and the start time. Separate from
        :meth:`from_response` rather than merged into a lenient single reader: a
        single reader that accepted a missing endpoint everywhere would also accept
        a ``GetMicrovm`` answer without one, and the lane's whole reachability
        story rests on never guessing an endpoint.
        """
        return cls._read(data, required=("microvmId", "state"))

    @classmethod
    def _read(cls, data: object, *, required: tuple[str, ...]) -> "MicroVm":
        if not isinstance(data, dict):
            raise AWSError(f"{_SERVICE}: expected a MicroVM object, got {type(data).__name__}")
        missing = [name for name in required if name not in data]
        if missing:
            raise AWSError(
                f"{_SERVICE}: a MicroVM response is missing {missing!r}, which the service's "
                "own output shape marks required"
            )
        return cls(
            microvm_id=str(data["microvmId"]),
            state=str(data["state"]),
            endpoint=str(data.get("endpoint", "")),
            image_arn=str(data.get("imageArn", "")),
            image_version=str(data.get("imageVersion", "")),
            maximum_duration_in_seconds=int(data.get("maximumDurationInSeconds", 0) or 0),
            started_at=str(data.get("startedAt", "")),
            state_reason=str(data.get("stateReason", "")),
        )


def _argv(
    operation: str,
    args: list[str],
    *,
    endpoint_url: str = "",
) -> list[str]:
    cmd = [_SERVICE, operation, *args]
    if endpoint_url:
        cmd += ["--endpoint-url", endpoint_url]
    return cmd


def run_microvm(
    *,
    image_identifier: str,
    image_version: str,
    run_hook_payload: str,
    maximum_duration_in_seconds: int,
    client_token: str,
    execution_role_arn: str = "",
    log_group: str = "",
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 180,
) -> MicroVm:
    """Create a MicroVM and return it in whatever state the service answers with.

    ``PENDING`` on the first answer is the contract, not a slow path: the service
    documents the VM as starting there and transitioning to ``RUNNING`` once
    provisioning completes. A caller that treats the call as synchronous and
    retries on a slow answer creates one VM per retry, each of them billing.

    ``image_version`` is REQUIRED here even though the API makes it optional. An
    unset version means "newest", and newest is resolved at call time -- so a
    build that lands mid-launch retargets the launch onto an image nobody chose.
    A lane that cannot say which image version it is running cannot say what it
    ran.

    ``client_token`` is required for the same reason in the other direction: the
    CLI retries, and the token is what makes two deliveries of one request one VM.
    """
    if not image_identifier:
        raise ValueError("run_microvm needs an image ARN or id; a bare image name is refused")
    if not image_version:
        raise ValueError(
            "run_microvm needs an explicit image version: unset means the newest build at "
            "call time, so a build landing mid-launch would silently retarget it"
        )
    if not client_token:
        raise ValueError(
            "run_microvm needs a client token: the CLI retries, and without one a retried "
            "request creates a second billing MicroVM"
        )
    payload_bytes = len(run_hook_payload.encode("utf-8"))
    if payload_bytes > MAX_RUN_HOOK_PAYLOAD_BYTES:
        raise ValueError(
            f"run hook payload is {payload_bytes} bytes, over the "
            f"{MAX_RUN_HOOK_PAYLOAD_BYTES}-byte limit the API reference states; refusing "
            "before the launch, because a payload rejected at RunMicrovm fails a launch "
            "that has already minted an activation"
        )
    if not 1 <= maximum_duration_in_seconds <= MAX_LIFETIME_SECONDS:
        raise ValueError(
            f"maximum_duration_in_seconds must be between 1 and {MAX_LIFETIME_SECONDS}; "
            f"got {maximum_duration_in_seconds}. The platform maximum is not adjustable and "
            "the value may only be set downward from it"
        )
    import os
    import tempfile

    handle, payload_path = tempfile.mkstemp(prefix="kirocrew-runhook-")
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        fh.write(run_hook_payload)
    os.chmod(payload_path, 0o600)
    args = [
        "--image-identifier",
        image_identifier,
        "--image-version",
        image_version,
        "--ingress-network-connectors",
        connector_arn(NO_INGRESS, region),
        "--egress-network-connectors",
        connector_arn(INTERNET_EGRESS, region),
        # The payload goes in by FILE, never as an argument. It carries this
        # launch's SSM activation id and its single-use activation CODE, and an
        # argument is in this process's argv -- readable through
        # ``/proc/<pid>/cmdline`` by anything on the machine that can list
        # processes, for as long as the call runs. The sandbox does not unshare
        # the PID namespace, so that includes an agent running on this host.
        #
        # What a reader of that code can do is take the crew: register its OWN
        # node under the hybrid activation before the guest does, and because the
        # activation's registration limit is 1 the real guest is then locked out
        # and ``wait_online`` returns the attacker's managed-instance id. Every
        # turn afterwards is tunnelled to them. ``put_secret`` in this same module
        # writes a 0600 file for exactly this reason; so does this.
        "--run-hook-payload",
        f"file://{payload_path}",
        "--maximum-duration-in-seconds",
        str(maximum_duration_in_seconds),
        "--client-token",
        client_token,
        # The platform's own idle policy is disabled DELIBERATELY, and this is the
        # single most load-bearing argument in the call. Platform idle is measured
        # only as inbound traffic on the VM's proxy endpoint, so a crew reached
        # through an SSM port-forward registers as completely idle however busy it
        # is -- and would be suspended out from under a running turn. This lane
        # computes its own idle verdict from the gateway's chat slots instead.
        "--idle-policy",
        json.dumps(
            {
                "autoResumeEnabled": False,
                "maxIdleDurationSeconds": MAX_LIFETIME_SECONDS,
                "suspendedDurationSeconds": MAX_LIFETIME_SECONDS,
            }
        ),
    ]
    if execution_role_arn:
        args += ["--execution-role-arn", execution_role_arn]
    if log_group:
        # PER-VM logging, which is a DIFFERENT setting from the one
        # ``CreateMicrovmImage`` takes, and the difference is not cosmetic: an
        # image-level log group receives the BUILD's output, and a running VM's
        # application output reaches nothing at all without this.
        #
        # Without it a guest that fails to come online is completely dark. The
        # control plane sees an activation with zero registrations and a VM in
        # RUNNING, and there is no other channel into the guest -- the only one a
        # MicroVM has before it registers is the platform's hook call. Measured
        # live: two launches failed with the cause invisible both times. A lane
        # whose failure mode is "no information exists" is not one an operator can
        # run.
        args += ["--logging", json.dumps({"cloudWatch": {"logGroup": log_group}})]
    try:
        data = checked_json(
            _argv("run-microvm", args, endpoint_url=endpoint_url),
            profile,
            region,
            action="lambda:RunMicrovm",
            timeout=timeout,
        )
    finally:
        # In a finally, so a refused call does not leave an activation code on
        # disk. The window is the call's own duration either way, which is what
        # the 0600 mode is for.
        try:
            os.remove(payload_path)
        except OSError:
            pass
    return MicroVm.from_response(data)


def get_microvm(
    microvm_id: str,
    *,
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 60,
) -> MicroVm:
    data = checked_json(
        _argv("get-microvm", ["--microvm-identifier", microvm_id], endpoint_url=endpoint_url),
        profile,
        region,
        action="lambda:GetMicrovm",
        timeout=timeout,
    )
    return MicroVm.from_response(data)


def list_microvms(
    *,
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 60,
) -> list[MicroVm]:
    """Every MicroVM the caller can see, following ``nextToken`` to the end.

    The sweeper reads this, and a sweep that stopped at the first page would
    report the VMs it did not look at as absent -- which on the orphan side means
    reporting a leaking VM as reaped.
    """
    out: list[MicroVm] = []
    next_token = ""
    while True:
        # 50, which is this parameter's own documented maximum -- botocore's model
        # says ``{'min': 1, 'max': 50}``. 100 is refused outright:
        #   ValidationException: Value '100' at 'maxResults' failed to satisfy
        #   constraint: Member must have value less than or equal to 50
        # so the call never returned a page. This is the SWEEPER's only input, and
        # a sweeper that cannot list is a sweeper that reports every leaking VM as
        # absent -- which is why it was failing closed on an unreadable list
        # rather than reporting zero orphans. Measured against the real service.
        args = ["--max-results", str(MAX_LIST_RESULTS)]
        if next_token:
            args += ["--next-token", next_token]
        data = checked_json(
            _argv("list-microvms", args, endpoint_url=endpoint_url),
            profile,
            region,
            action="lambda:ListMicrovms",
            timeout=timeout,
        )
        if not isinstance(data, dict):
            raise AWSError("lambda:ListMicrovms: expected an object")
        for item in data.get("items") or []:
            out.append(MicroVm.from_item(item))
        token = data.get("nextToken")
        if not token or not isinstance(token, str):
            return out
        if token == next_token:
            raise AWSError("lambda:ListMicrovms: the service repeated a page token")
        next_token = token


def terminate_microvm(
    microvm_id: str,
    *,
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 60,
) -> None:
    """Terminate a VM, taking its disk with it.

    The platform delivers no ``SIGTERM`` at teardown and the call returns before
    the guest's own terminate hook fires, so nothing inside the VM can be relied
    on to run after this. That is why the pack sequence terminates LAST.
    """
    checked_json(
        _argv("terminate-microvm", ["--microvm-identifier", microvm_id], endpoint_url=endpoint_url),
        profile,
        region,
        action="lambda:TerminateMicrovm",
        timeout=timeout,
    )


def create_auth_token(
    microvm_id: str,
    *,
    allowed_ports: tuple[int, ...],
    expiration_in_minutes: int,
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 60,
) -> dict[str, str]:
    """A token for the VM's endpoint, scoped to *allowed_ports*.

    The port list is the one control the service genuinely enforces on the
    endpoint: a token scoped to one port is refused on another. It is not a
    substitute for the guest authenticating its callers, because a holder of a
    correctly-scoped token is still an arbitrary internet caller to the guest.
    """
    if not allowed_ports:
        raise ValueError("create_auth_token needs at least one allowed port")
    data = checked_json(
        _argv(
            "create-microvm-auth-token",
            [
                "--microvm-identifier",
                microvm_id,
                "--allowed-ports",
                # ``port=<n>``, not a bare integer. ``allowedPorts`` is a list of
                # TAGGED UNIONS -- one of ``port``, ``range`` or ``allPorts`` -- so
                # the CLI parses each element as shorthand and a bare number is
                # refused client-side, before any request is sent:
                #   Error parsing parameter '--allowed-ports':
                #   Expected: '=', received: 'EOF' for input: 8080
                # Measured against the real service. A loopback fake
                # that accepts whatever the lane sends cannot see this, because the
                # refusal comes from botocore's own model and never leaves the host.
                *[f"port={int(p)}" for p in allowed_ports],
                "--expiration-in-minutes",
                str(expiration_in_minutes),
            ],
            endpoint_url=endpoint_url,
        ),
        profile,
        region,
        action="lambda:CreateMicrovmAuthToken",
        timeout=timeout,
    )
    if not isinstance(data, dict) or not isinstance(data.get("authToken"), dict):
        raise AWSError("lambda:CreateMicrovmAuthToken: response carries no authToken map")
    return {str(k): str(v) for k, v in data["authToken"].items()}


def microvm_status(
    microvm_id: str,
    *,
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
) -> Optional[str]:
    """The VM's state, or ``None`` when the service says it does not exist.

    ``None`` and ``"TERMINATED"`` are different answers and both are useful: the
    first means the platform has forgotten the VM, the second that it remembers a
    dead one. A caller cleaning up treats them the same; a caller deciding whether
    a suspended crew can still be resumed does not.
    """
    try:
        return get_microvm(
            microvm_id, profile=profile, region=region, endpoint_url=endpoint_url
        ).state
    except AWSError as exc:
        if _is_not_found(exc):
            return None
        raise


def _is_not_found(exc: Exception) -> bool:
    """Whether *exc* is the service saying the resource is gone.

    Matched on the error NAME in the message rather than on an HTTP status,
    because the CLI seam hands back stderr text and not a response object. The
    name is the service's own ``ResourceNotFoundException``, which the model
    declares for every operation this module calls.
    """
    return "ResourceNotFoundException" in str(exc)


def describe_activation_registrations(
    activation_id: str,
    *,
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 60,
) -> Optional[dict[str, Any]]:
    """One SSM hybrid activation, or ``None`` when it is gone.

    Lives here rather than in a second SSM module because the only reason this
    lane touches activations is to launch and reap MicroVMs, and the sweeper reads
    ``RegistrationsCount`` from it to tell a leaked activation from a used one.
    """
    data = checked_json(
        [
            "ssm",
            "describe-activations",
            # ``FilterKey`` / ``FilterValues``, which are this filter's own member
            # names. ``key`` / ``value`` is refused client-side:
            #   Unknown parameter in Filters[0]: "key", must be one of:
            #   FilterKey, FilterValues
            # Note these differ from DescribeInstanceInformation's filter, whose
            # members really are ``Key`` and ``Values`` -- two SSM calls in this
            # one lane, two different spellings, and the lane had them swapped.
            # Measured against real SSM.
            "--filters",
            f"FilterKey=ActivationIds,FilterValues={activation_id}",
            *(["--endpoint-url", endpoint_url] if endpoint_url else []),
        ],
        profile,
        region,
        action="ssm:DescribeActivations",
        timeout=timeout,
    )
    if not isinstance(data, dict):
        return None
    entries = data.get("ActivationList") or []
    if not entries or not isinstance(entries[0], dict):
        return None
    return entries[0]


# ── Image build ──────────────────────────────────────────────────────────────
#
# A crew's image is BUILT rather than configured, the way the Fargate lane's task
# definition is built rather than configured. The difference between the two lanes
# is only the build target: Fargate pushes to a registry and registers a task
# definition, and this lane uploads the same recipe as a zip and asks Lambda to
# build it. Nothing is built on the owner's machine either way.
#
# Two shapes here are the service's own and neither is guessable:
#
# ``baseImageArn`` is an AWS-MANAGED MicroVM base image, not our container image.
# Our content -- the crew runtime Dockerfile and that crew's bundle -- travels in
# the zip ``codeArtifact.uri`` points at, and Lambda runs the build.
#
# An image and a usable image are different things. ``CreateMicrovmImage`` answers
# with an image whose ``state`` is ``CREATING``, and a launch needs a VERSION whose
# ``status`` is ``ACTIVE``. An image that reached ``CREATED`` with no active version
# is a build that finished and produced nothing launchable, so the wait below reads
# the version and not the image.


@dataclass(frozen=True)
class MicroVmImage:
    """One MicroVM image as the service describes it."""

    image_arn: str
    name: str
    state: str
    latest_active_version: str = ""
    latest_failed_version: str = ""

    @classmethod
    def from_response(cls, data: object) -> "MicroVmImage":
        if not isinstance(data, dict):
            raise AWSError(f"{_SERVICE}: expected a MicroVM image object")
        missing = [k for k in ("imageArn", "state") if k not in data]
        if missing:
            raise AWSError(
                f"{_SERVICE}: a MicroVM image response is missing {missing!r}, which the "
                "service's own output shape marks required"
            )
        return cls(
            image_arn=str(data["imageArn"]),
            name=str(data.get("name", "")),
            state=str(data["state"]),
            latest_active_version=str(data.get("latestActiveImageVersion", "") or ""),
            latest_failed_version=str(data.get("latestFailedImageVersion", "") or ""),
        )

    @property
    def launchable_version(self) -> str:
        """The version a launch may use, or ``""``.

        Read from ``latestActiveImageVersion`` and nowhere else. The image's own
        ``state`` says whether the BUILD finished; it does not say whether anything
        came out of it that can be run.
        """
        return self.latest_active_version


def create_microvm_image(
    *,
    name: str,
    base_image_arn: str,
    build_role_arn: str,
    code_artifact_uri: str,
    cpu_architecture: str,
    minimum_memory_mib: int,
    hook_port: int,
    run_hook: str,
    ready_hook: str,
    suspend_hook: str,
    resume_hook: str,
    terminate_hook: str,
    environment_variables: Optional[dict[str, str]] = None,
    tags: Optional[dict[str, str]] = None,
    #: Where the BUILD's own logs go. A DIFFERENT setting from the per-VM logging
    #: ``run_microvm`` takes: this one covers the container build and nothing
    #: else. Empty means no build logs, and a build that then fails says only
    #: "The container image build failed" with nowhere to look.
    #:
    #: The build ROLE must be able to write the group. If it cannot, the version
    #: is accepted and then never progresses: PENDING with ``updatedAt`` equal to
    #: ``createdAt``, the image CREATING, no log group created, no timeout and no
    #: error. Measured live, over two hours in that state. Such an image cannot be
    #: cleaned up either -- the version is the last one, so it cannot be deleted,
    #: and the image is not in a deletable state -- so its name, which is the
    #: recipe digest, is burned for that content.
    #: ``templates/kirocrew-microvm-base.yaml`` grants it.
    log_group: str = "",
    client_token: str = "",
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 300,
) -> MicroVmImage:
    """Ask Lambda to build an image from the recipe zip at *code_artifact_uri*.

    Returns as soon as the service accepts the build, with the image in
    ``CREATING``. Waiting for it is :func:`wait_image_active`'s job, because a
    build takes minutes and a single blocking call for that dies to any timeout --
    the same reason ``RunMicrovm`` answers ``PENDING`` rather than provisioning
    inside the request.

    The hook paths are passed explicitly rather than defaulted, because they are
    the only channel the platform has into the guest and a wrong one is a VM that
    boots, answers nothing and bills until its lifetime expires.
    """
    for label, value in (
        ("name", name),
        ("base image ARN", base_image_arn),
        ("build role ARN", build_role_arn),
        ("code artifact URI", code_artifact_uri),
    ):
        if not value:
            raise ValueError(f"create_microvm_image needs a {label}")
    if not code_artifact_uri.startswith("s3://"):
        raise ValueError(
            f"the recipe must be an S3 URI the build role can read; got {code_artifact_uri!r}"
        )
    args = [
        "--name",
        name,
        "--base-image-arn",
        base_image_arn,
        "--build-role-arn",
        build_role_arn,
        "--code-artifact",
        json.dumps({"uri": code_artifact_uri}),
        "--cpu-configurations",
        json.dumps([{"architecture": cpu_architecture}]),
        "--resources",
        json.dumps([{"minimumMemoryInMiB": int(minimum_memory_mib)}]),
        # The platform's lifecycle hooks, and the port the guest serves them on.
        # ``microvmImageHooks`` run at BUILD time and ``microvmHooks`` at run time;
        # they are separate members because they run in different phases, and
        # putting a run hook in the build set is a build that passes and a VM that
        # never starts.
        "--hooks",
        json.dumps(
            {
                "port": int(hook_port),
                "microvmImageHooks": {
                    "ready": ready_hook,
                    # The build-phase budget. Named rather than defaulted so a
                    # slow first boot is a readable timeout instead of a build
                    # that failed for no stated reason.
                    "readyTimeoutInSeconds": 60,
                },
                "microvmHooks": {
                    "run": run_hook,
                    # 60 s is this hook's documented maximum, and the reason the
                    # guest must not wait for its SSM node to come online inside
                    # it: starting the agent fits, waiting for it does not.
                    "runTimeoutInSeconds": 60,
                    "suspend": suspend_hook,
                    "suspendTimeoutInSeconds": 60,
                    "resume": resume_hook,
                    "resumeTimeoutInSeconds": 60,
                    "terminate": terminate_hook,
                    "terminateTimeoutInSeconds": 60,
                },
            }
        ),
        "--egress-network-connectors",
        connector_arn(INTERNET_EGRESS, region),
    ]
    if log_group:
        args += ["--logging", json.dumps({"cloudWatch": {"logGroup": log_group}})]
    if environment_variables:
        args += ["--environment-variables", json.dumps(environment_variables)]
    if tags:
        # One argv element per tag. Two ``Key=`` pairs inside one element is
        # refused as a duplicate member rather than read as the next tag.
        args.append("--tags")
        args.append(json.dumps(tags))
    if client_token:
        args += ["--client-token", client_token]
    data = checked_json(
        _argv("create-microvm-image", args, endpoint_url=endpoint_url),
        profile,
        region,
        action="lambda:CreateMicrovmImage",
        timeout=timeout,
    )
    return MicroVmImage.from_response(data)


def get_microvm_image(
    image_identifier: str,
    *,
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 60,
) -> Optional[MicroVmImage]:
    """One image, or ``None`` when the service does not have it."""
    try:
        data = checked_json(
            _argv(
                "get-microvm-image",
                ["--image-identifier", image_identifier],
                endpoint_url=endpoint_url,
            ),
            profile,
            region,
            action="lambda:GetMicrovmImage",
            timeout=timeout,
        )
    except AWSError as exc:
        if _is_not_found(exc):
            return None
        raise
    return MicroVmImage.from_response(data)


def list_microvm_images(
    *,
    name_filter: str = "",
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 60,
) -> list[MicroVmImage]:
    """Every image the caller can see, following ``nextToken`` to the end.

    Paginated for the reason :func:`list_microvms` is: a cache lookup that stopped
    at the first page would rebuild an image that already exists, and a MicroVM
    image build is minutes and money.
    """
    out: list[MicroVmImage] = []
    next_token = ""
    while True:
        args = ["--max-results", str(MAX_LIST_RESULTS)]
        if name_filter:
            args += ["--name-filter", name_filter]
        if next_token:
            args += ["--next-token", next_token]
        data = checked_json(
            _argv("list-microvm-images", args, endpoint_url=endpoint_url),
            profile,
            region,
            action="lambda:ListMicrovmImages",
            timeout=timeout,
        )
        if not isinstance(data, dict):
            raise AWSError("lambda:ListMicrovmImages: expected an object")
        for item in data.get("items") or []:
            out.append(MicroVmImage.from_response(item))
        token = data.get("nextToken")
        if not token or not isinstance(token, str):
            return out
        if token == next_token:
            raise AWSError("lambda:ListMicrovmImages: the service repeated a page token")
        next_token = token


def image_version_status(
    image_identifier: str,
    image_version: str,
    *,
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 60,
) -> Optional[str]:
    """One version's status, or ``None`` when it does not exist.

    ``ACTIVE`` is the only value a launch may use. Read separately from the image
    because the two answer different questions: the image's ``state`` says whether
    the build finished, and this says whether anything launchable came out of it.
    """
    try:
        data = checked_json(
            _argv(
                "get-microvm-image-version",
                [
                    "--image-identifier",
                    image_identifier,
                    "--image-version",
                    image_version,
                ],
                endpoint_url=endpoint_url,
            ),
            profile,
            region,
            action="lambda:GetMicrovmImageVersion",
            timeout=timeout,
        )
    except AWSError as exc:
        if _is_not_found(exc):
            return None
        raise
    return str(data.get("status", "")) if isinstance(data, dict) else None


def put_recipe_object(
    bucket: str,
    key: str,
    body_path: str,
    *,
    kms_key_id: str = "",
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 300,
) -> None:
    """Upload a recipe zip for the build role to read.

    Unconditional. A recipe object's key IS the digest of its content, so two
    writers racing on the same key are writing identical bytes and a precondition
    would only turn that into a failure.

    The encryption header is ALWAYS sent: a bucket whose policy denies
    unencrypted puts tests the request HEADER, and a bucket default does not set
    one, so an omitted header is a 403 rather than a default-encrypted object.
    Only which algorithm it names depends on whether a key was chosen.
    """
    for label, value in (("bucket", bucket), ("key", key), ("body path", body_path)):
        if not value:
            raise ValueError(f"put_recipe_object needs a {label}")
    args = [
        "s3api",
        "put-object",
        "--bucket",
        bucket,
        "--key",
        key,
        "--body",
        body_path,
    ]
    if kms_key_id:
        args += ["--server-side-encryption", "aws:kms", "--ssekms-key-id", kms_key_id]
    else:
        args += ["--server-side-encryption", "AES256"]
    if endpoint_url:
        args += ["--endpoint-url", endpoint_url]
    checked_json(args, profile, region, action="s3:PutObject", timeout=timeout)
    logger.info("uploaded MicroVM recipe to s3://%s/%s", bucket, key)


def put_secret(
    name: str,
    value: str,
    *,
    kms_key_id: str = "",
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 60,
) -> str:
    """Create or overwrite one Secrets Manager secret. Returns its ARN.

    Create-then-fall-back-to-update, because a relaunch under a tag that was used
    before finds the secret still there: Secrets Manager keeps a deleted secret
    for a recovery window, so ``create`` is not idempotent and a lane that only
    created would fail every second launch of the same tag.

    The VALUE goes in on stdin's place -- as a CLI argument it would be in this
    process's argv, which is readable by anything on the machine that can list
    processes. ``--secret-string`` takes a ``file://`` form for exactly this, so
    the value is written to a file only this user can read and removed
    immediately.
    """
    import os
    import tempfile

    if not name or not value:
        raise ValueError("put_secret needs a name and a value")
    handle, path = tempfile.mkstemp(prefix="kirocrew-secret-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(value)
        os.chmod(path, 0o600)
        create = [
            "secretsmanager",
            "create-secret",
            "--name",
            name,
            "--secret-string",
            f"file://{path}",
            *(["--kms-key-id", kms_key_id] if kms_key_id else []),
            *(["--endpoint-url", endpoint_url] if endpoint_url else []),
        ]
        try:
            data = checked_json(
                create, profile, region, action="secretsmanager:CreateSecret", timeout=timeout
            )
        except AWSError as exc:
            if "ResourceExistsException" not in str(exc):
                raise
            data = checked_json(
                [
                    "secretsmanager",
                    "put-secret-value",
                    "--secret-id",
                    name,
                    "--secret-string",
                    f"file://{path}",
                    *(["--endpoint-url", endpoint_url] if endpoint_url else []),
                ],
                profile,
                region,
                action="secretsmanager:PutSecretValue",
                timeout=timeout,
            )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    arn = str(data.get("ARN", "") or "") if isinstance(data, dict) else ""
    if not arn:
        raise AWSError("secretsmanager: the write returned no ARN")
    # NOTHING about the secret is logged here, not even its name.
    #
    # The name is not the value and never was, but a log line inside a function
    # that takes a secret is a line every scanner reads as a leak -- and arguing
    # with the scanner is worse than not needing to. The caller holds the launch
    # TAG, which is a public launch id, and logs that instead: an operator
    # reading a launch learns the same thing from it, and nothing here is on a
    # path from a credential to a log.
    return arn


def delete_secret(
    name: str,
    *,
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 60,
) -> None:
    """Schedule one secret for deletion, best-effort and logged.

    ``--force-delete-without-recovery`` is deliberately NOT passed. A per-launch
    secret is cheap to leave in the recovery window and the window is the only
    thing that makes a mistaken teardown reversible; forcing the delete makes a
    wrong tag unrecoverable.

    Never raises. This runs on a teardown path, and a secret that outlives its
    crew is a tidy-up item the sweeper can report, while an exception here would
    abandon the rest of the teardown.
    """
    try:
        checked_json(
            [
                "secretsmanager",
                "delete-secret",
                "--secret-id",
                name,
                *(["--endpoint-url", endpoint_url] if endpoint_url else []),
            ],
            profile,
            region,
            action="secretsmanager:DeleteSecret",
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - teardown continues regardless
        # The FAILURE only, with no secret coordinate in it, for the reason
        # :func:`put_secret` gives. A teardown that could not remove a secret is
        # the thing worth seeing; which secret it was belongs to the caller's own
        # log line, where the launch tag names it.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
        logger.warning("could not delete a crew secret: %s", type(exc).__name__)
