"""``MicroVmLaunchEngine``: the five-method ``LaunchEngine`` for the MicroVM lane.

Written against ``cloud/launch_job.py``'s Protocol so the lane inherits the
durable step machine, both rollback paths, the dashboard's progress UI and the
orphan reaper without reimplementing any of them. Four contract traps that
Protocol enforces, and how this engine answers each:

``teardown`` never sees what ``provision`` returned.
    Core makes the launch ``tag`` before preflight and passes it to ``provision``
    and ``teardown`` only; ``provision``'s return value becomes ``job.instance_id``
    and goes nowhere else. So a MicroVM findable only by its own id is a MicroVM
    one rollback cannot delete -- which is why :meth:`provision` writes the record
    under the tag BEFORE it calls ``RunMicrovm``, and writes the id into that
    record the moment the call answers.

A raise out of ``begin_signin`` is NOT rolled back.
    Rollback is scoped to a failed ``provision`` step. ``begin_signin`` runs after
    it, so a raise there would leave a running, unregistered, billing VM.
    :class:`MicroVmSigninHandle` therefore cannot raise: the crew's model
    credential is delivered to the guest at run time and nothing signs it in.

``register`` must raise on failure.
    ``connect.register_instance`` is best-effort and answers ``None``; swallowing
    that marks a launch done for a crew the owner cannot see.

``abort`` must return ``True`` explicitly.
    A missing method records "not confirmed stopped", which is the safe answer and
    the wrong one for a lane with no remote login to stop.

The launch sequence itself has one ordering rule that cost a leaked activation in
the reference implementation: an activation minted and then followed by a failed
``RunMicrovm`` leaves an activation with zero registrations behind, and the
cleanup must terminate the VM FIRST and delete the activation after, because the
activation is part of what the terminate path may still need.
"""

from __future__ import annotations

import logging
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from kiro_crew.cloud import connect
from kiro_crew.cloud.aws import AWSError, checked_json
from kiro_crew.cloud.fargate.identity import validated_region
from kiro_crew.cloud.microvm import api
from kiro_crew.cloud.microvm import recipe as recipe_mod
from kiro_crew.cloud.microvm import states
from kiro_crew.cloud.microvm.image import ImageResolver
from kiro_crew.cloud.microvm.payload import RunHookPayload
from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

# The id and kind this lane publishes, DEFINED THERE and imported here -- the
# same direction ``fargate_engine`` takes it. The descriptor that carries the id
# to the dashboard lives in ``platform.defaults``, so a second literal here would
# be a second spelling of the lane's identity, and the two would drift the first
# time either was renamed. Re-exported because callers of this module already
# read the id from it.
from kiro_crew.platform.defaults import MICROVM_PROVISIONER_ID

logger = logging.getLogger(__name__)

#: The lifetime a launch gets when the operator names none: one hour.
#:
#: NOT the platform's maximum. The maximum is what the platform will allow; this
#: is what the lane should ask for, and the two differ because the home is not
#: kept. A crew left running to an eight-hour wall loses eight hours of
#: conversation at the end of it instead of one.
DEFAULT_WALL_SECONDS = 3600

#: How long a hybrid activation stays usable, in minutes. One hour: long enough
#: for a slow launch, short enough that a leaked one expires before it is worth
#: finding. ``--registration-limit 1`` is the other half -- one activation
#: registers exactly one node, so a leaked code cannot enroll a second machine.
ACTIVATION_EXPIRY_MINUTES = 60

#: Seconds to wait for the guest's SSM node to come ``Online``.
#:
#: Online is NOT "the guest answers": a wait that passed and a first real call
#: that failed with a connect error is how a healthy crew got stamped a pack
#: failure. Callers that need the guest treat this as the floor and then wait on
#: the guest itself.
ONLINE_TIMEOUT_SECONDS = 300

#: Seconds between polls while waiting for the node.
ONLINE_POLL_SECONDS = 5


class LaunchSuperseded(RuntimeError):
    """This launch's crew stopped being the crew on disk while the launch waited.

    Raised when the fenced record write refuses: the row was deleted, reached a
    terminal state, or a newer launch took the tag. The launch has already
    released the VM and node it created by the time this is raised, so a caller
    reports a launch that did not happen rather than one that left something
    behind.

    Distinct from an ordinary launch failure because nothing went wrong: the owner
    asked for the teardown that overtook it, so the record must keep the state
    that teardown wrote rather than being moved to ``launch_failed``.
    """


@dataclass(frozen=True)
class MicroVmLaunchSpec:
    """What an operator must supply before this lane can launch anything.

    The engine refuses to guess any of it, for the reason the Fargate lane states
    about a subnet: an unnamed image version is the same class of error as
    deleting a resource on a guess. ``cloud.json``'s ``microvm`` block is where
    these are written, and ``MicroVmConfig.is_complete`` is what keeps a
    half-filled block from registering a lane that rejects every launch.
    """

    kms_key_id: str
    #: The AWS-managed base image a crew's image is built on. The lane builds the
    #: image from this base plus the crew's own bundle, the way the Fargate lane
    #: builds a task definition from a base image plus the same bundle.
    base_image_arn: str = ""
    #: The role Lambda assumes to run that build.
    build_role_arn: str = ""
    #: The bucket the recipe zip goes to for the build role to read.
    recipe_bucket: str = ""
    #: The crew bundle ``packaging.build`` produced, as a path. The SAME artifact
    #: the Fargate lane builds its crew layer from; this lane zips it rather than
    #: running ``docker build`` over it. Empty when the operator pinned a prebuilt
    #: image instead, which is the only case where no bundle is needed.
    bundle_dir: str = ""
    #: A PREBUILT image to launch instead of building one. Both of these are empty
    #: on the normal path; when set, they bypass the build entirely.
    #:
    #: A version is required alongside an identifier, never optional: unset means
    #: newest at call time, so a build landing mid-launch retargets the launch
    #: onto an image nobody chose.
    image_identifier: str = ""
    image_version: str = ""
    #: Where the per-crew control secret is stored. A path PREFIX, not a value:
    #: this spec is built from a file the operator owns, and a secret in it would
    #: be a secret in their config directory.
    secret_path_prefix: str = "kirocrew/crew"
    #: The crew's model-credential secret, by name or ARN, as the operator
    #: created it. Carried rather than derived: the launch tag is minted by the
    #: launcher, so a name built from it cannot exist before the launch.
    identity_secret_ref: str = ""
    #: The VM's lifetime, in seconds, at most the platform's non-adjustable
    #: maximum. ONE HOUR by default, well under that maximum, because on this lane
    #: the crew's home lives on the VM's disk and goes away with it: the default
    #: is how long a crew can be left before its conversations are gone, so it is
    #: set to a length an owner can hold in their head rather than to the longest
    #: the platform allows. Settable either way -- down for a test crew, up by an
    #: operator who knows what the wall costs them.
    wall_seconds: int = DEFAULT_WALL_SECONDS
    execution_role_arn: str = ""
    #: The IAM role a hybrid activation registers the guest's SSM node under.
    #: REQUIRED by ``ssm:CreateActivation``, which refuses the call without it:
    #:   ParamValidation: the following arguments are required: --iam-role
    #: Measured against real SSM. This is the base template's
    #: ``HybridActivationRoleArn`` output, which before this field had nowhere to
    #: be written -- the template published a role the engine could not pass.
    activation_role_arn: str = ""
    #: CloudWatch log group the GUEST's own output is streamed to, set per VM.
    #:
    #: A different setting from the image's, and the difference is the one that
    #: matters: an image-level log group receives the BUILD's output, and a
    #: running VM's application output reaches nothing unless this is passed.
    #: Measured live, after two launches failed with the cause invisible -- the
    #: control plane could see an activation with zero registrations and a VM in
    #: RUNNING, and a MicroVM has no other channel in before it registers.
    #:
    #: Empty is allowed and means no guest logs, which is a choice an operator can
    #: make; it is not a default this lane recommends.
    log_group: str = ""
    #: Points every AWS call at a loopback fake. For the local harness only; a
    #: real launch leaves it empty.
    endpoint_url: str = ""

    def control_secret_name(self, tag: str) -> str:
        """The Secrets Manager name this crew's control secret lives under."""
        return f"{self.secret_path_prefix}/{tag}/CONTROL_SECRET"


class MicroVmSigninHandle:
    """The sign-in step, which on this lane has nothing to wait for.

    The guest is handed its model credential at run time, through the control
    secret its run payload references, and refuses to serve without one. Nothing
    in the VM runs a device-code flow, so there is no prompt to show and no poller
    to cancel.

    ``already_logged_in`` is ``True`` as a statement of fact, and ``error`` is
    always empty: the one identity this lane cannot honour is refused at preflight,
    before anything is provisioned or billed. Reading those two in the other order
    would describe a handle whose refusal is ignored.

    Presence is not validity. The credential being delivered is enforced at guest
    start; whether it WORKS is established only by a real turn, so this handle
    must not be read as evidence of a working credential.
    """

    def __init__(self, microvm_id: str) -> None:
        self.microvm_id = microvm_id
        self.already_logged_in: bool = True
        self.url: str = ""
        self.code: str = ""
        self.ports: list = []
        self.error: str = ""

    def wait(self, cancel: threading.Event) -> bool:
        """Return at once. A launch cancelled by now does not report a done step."""
        return not cancel.is_set()

    def close(self) -> None:
        """Nothing to release. No browser, no poller, no session."""

    def abort(self) -> bool:
        """``True`` means "confirmed there is no remote login left running".

        Stated rather than left to a missing method, which the launch job records
        as "not confirmed stopped" -- the safe answer, and the wrong one here.
        """
        return True


@dataclass
class MicroVmLaunchEngine:
    """``LaunchEngine`` for AWS Lambda MicroVMs.

    Every AWS call goes through ``cloud/microvm/api.py``, which goes through the
    one ``aws`` CLI chokepoint in ``cloud/aws.py``. That chokepoint's agent-session
    allowlist names no ``lambda-microvms`` pair, so a launch on this lane is a
    human action run from a terminal, exactly as the other two lanes are.
    """

    spec: Optional[MicroVmLaunchSpec] = None
    store: Optional[CrewStore] = None
    #: Injected so the local harness can replace the whole platform without
    #: touching this class. A real launch leaves it ``None`` and gets
    #: ``cloud/microvm/api.py``.
    launcher: Optional["VmLauncher"] = None
    now: Callable[[], float] = time.time

    def _require_spec(self) -> MicroVmLaunchSpec:
        if self.spec is None:
            raise ValueError(
                "this MicroVM engine was constructed without a launch spec, so it cannot "
                "launch: it needs a base image, a build role and a recipe bucket so the "
                "crew's image can be built (or a prebuilt image and its pinned version), "
                "plus a KMS key id. Supply a MicroVmLaunchSpec; "
                "guessing the image version is the same error as launching an image nobody "
                "chose."
            )
        return self.spec

    def _require_store(self) -> CrewStore:
        if self.store is None:
            self.store = CrewStore()
        return self.store

    def _require_launcher(self) -> "VmLauncher":
        if self.launcher is None:
            self.launcher = ApiVmLauncher(self._require_spec())
        return self.launcher

    # ── LaunchEngine ─────────────────────────────────────────────────────────

    def preflight(self, profile: str, region: str) -> None:
        """Validate the region and refuse, by name, what the engine lacks.

        A refusal, not a probe: it makes no MicroVM call. A lane that cannot name
        its image version has nothing to check against the account.
        """
        validated_region(region, source="region")
        spec = self._require_spec()
        if not 1 <= spec.wall_seconds <= api.MAX_LIFETIME_SECONDS:
            raise ValueError(
                f"wall_seconds must be between 1 and {api.MAX_LIFETIME_SECONDS}: the platform "
                "maximum is not adjustable and may only be set downward"
            )
        # Refuse HERE, before anything is provisioned or billed, rather than at
        # the ``RunMicrovm`` call. An image this lane can neither find nor build
        # is a refusal the operator should get from a check, not from a launch
        # that has already minted an activation.
        if not spec.image_identifier and not (
            spec.base_image_arn and spec.build_role_arn and spec.recipe_bucket
        ):
            raise ValueError(
                "this lane cannot obtain an image: it needs either a prebuilt image and its "
                "pinned version, or a base image, a build role and a recipe bucket so the "
                "crew's image can be built from its own bundle"
            )
        if bool(spec.image_identifier) != bool(spec.image_version):
            raise ValueError(
                "a prebuilt image needs both an identifier and a pinned version: an unset "
                "version means the newest build at call time, so one landing mid-launch "
                "would retarget this launch onto an image nobody chose"
            )
        if not spec.image_identifier:
            # The build path, so the bundle is read here rather than minutes into a
            # Lambda build: this is the same layout check the Fargate lane's
            # ``docker build`` performs, moved to the earliest place it can run.
            #
            # The empty case is its own refusal rather than a Path("") -- which is
            # the CWD, a real directory, so it would be checked for a crew layout
            # and reported as "the bundle here is missing everything".
            if not spec.bundle_dir:
                raise ValueError(
                    "this lane builds a crew's image and was given no crew bundle to build "
                    "it from. Write microvm.bundle_dir in ~/.kiro/crew/cloud.json, pointing "
                    "at the directory packaging.build produced -- the same artifact the "
                    "Fargate lane's crew layer is built from."
                )
            recipe_mod.check_layout(Path(spec.bundle_dir))

    @staticmethod
    def _bundle_crew_name(spec: Any) -> str:
        """The crew name the configured bundle declares, or ``""``.

        Empty rather than a refusal: the launch itself works without the name --
        the guest reads its own manifest -- and failing a provision over an
        unreadable manifest would cost the owner a crew to protect a field only
        the turn path reads. The turn path says so instead of guessing.
        """
        if not getattr(spec, "bundle_dir", ""):
            return ""
        try:
            return recipe_mod.bundle_crew_name(Path(spec.bundle_dir))
        except Exception:  # noqa: BLE001 - a name this launch could not read
            return ""

    def provision(self, *, tag: str, size_key: str, profile: str, region: str) -> str:
        """Mint an activation, run the VM, and wait for its node to come online.

        ``size_key`` is accepted and ignored: a MicroVM has no instance type, and
        the launch job passes one for every lane. Refusing it would make the lane
        unlaunchable through the shared route for no benefit.

        The record is written BEFORE ``RunMicrovm``, under the launch tag, because
        ``teardown`` is handed the tag and nothing else -- so a VM created by a
        call whose answer this process never saw is still findable by the rollback.

        On any failure after the activation is minted: terminate the VM if one
        exists, wait for it, and only then delete the activation. Leaving the
        activation is the measured leak -- an activation with zero registrations,
        billing nothing but counting against the account's limits and invisible
        until a sweep looks for it.
        """
        spec = self._require_spec()
        store = self._require_store()
        launcher = self._require_launcher()
        previous = store.get(tag)
        generation = (previous.generation + 1) if previous else 1
        record = store.put(
            CrewRecord(
                tag=tag,
                state=states.PENDING,
                profile=profile,
                region=region,
                wall_seconds=spec.wall_seconds,
                generation=generation,
                created_at=self.now(),
                control_secret_ref=spec.control_secret_name(tag),
                # The name the guest will serve, read from the bundle the image
                # was built from. Recorded here because this is the only moment
                # the control plane holds it: the turn path has an instance row
                # and a tag, and the tag is not the name.
                crew_name=self._bundle_crew_name(spec),
            )
        )
        # The control secret EXISTS before the VM is told to read it.
        #
        # The payload carries a secret REFERENCE rather than a value, so the guest
        # reads it itself -- which means the launcher has to have written it. A VM
        # pointed at a name nobody created ends at its secrets stage and bills
        # until its wall without serving anything.
        #
        # ``tag`` is the CREW's id and is stable for that crew's whole lifetime,
        # so this path is written on every launch of the same crew and a fresh
        # value is minted each time:
        # ``api.put_secret`` creates the secret or puts a new version of it, and
        # the previous VM's value stops being the one that works the moment this
        # one starts.
        #
        # The IDENTITY secret under that same stable path is NOT created here, and
        # is not the lane's to create. Its value is the owner's model credential,
        # which this control plane does not hold and must not mint: the guide
        # tells the operator to create ``<prefix>/<crew>/KIRO_IDENTITY`` once, and
        # it then outlives every launch. That the guest derives the same name from
        # the control reference it is handed, rather than being told it twice, is
        # pinned by ``test_the_identity_path_is_the_one_the_guide_publishes``.
        # What this writes is the one secret the lane itself owns.
        secret_value = secrets.token_urlsafe(32)
        api.put_secret(
            record.control_secret_ref,
            secret_value,
            kms_key_id=spec.kms_key_id,
            profile=profile,
            region=region,
            endpoint_url=spec.endpoint_url,
        )
        del secret_value
        # The TAG, which is this launch's public id. The secret's own name is not
        # logged anywhere: see ``api.put_secret``.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
        logger.info("minted the control secret for microvm crew %s", tag)
        activation = launcher.create_activation(tag=tag, profile=profile, region=region)
        record = store.put(record.evolve(activation_id=activation.activation_id))
        microvm_id = ""
        try:
            payload = RunHookPayload(
                tag=tag,
                activation_id=activation.activation_id,
                activation_code=activation.activation_code,
                region=region,
                control_secret_ref=record.control_secret_ref,
                identity_secret_ref=spec.identity_secret_ref,
                generation=generation,
            )
            vm = launcher.run(
                payload=payload.encode(),
                profile=profile,
                region=region,
                # One token per (tag, generation), so a CLI retry of one logical
                # launch is one VM. Without it a retried request creates a second
                # VM that bills for eight hours and that no record names.
                client_token=f"kc-{tag}-{generation}-{secrets.token_hex(4)}",
            )
            microvm_id = vm.microvm_id
            record = store.put(record.evolve(microvm_id=microvm_id, endpoint=vm.endpoint))
            mi_id = launcher.wait_online(
                activation_id=activation.activation_id, profile=profile, region=region
            )
            # Fenced, because the wait above runs for minutes and a teardown can
            # finish inside it. Writing the record this function read before the
            # wait would put every field back as it was -- including the state a
            # teardown moved to terminated -- so the row would say running for a
            # VM that is gone, which the sweeper reports and nothing can reach.
            if (
                store.patch_live(
                    tag, generation=generation, mi_id=mi_id, last_observed_at=self.now()
                )
                is None
            ):
                # The crew this launch was for is not on disk any more. What IS
                # here is a running VM and a node that just registered, so they
                # are cleaned up rather than recorded: a VM nothing names bills to
                # its wall, and a node that outlives its VM is reported forever.
                logger.warning(
                    "microvm launch %s was torn down while its node came online; "
                    "releasing the VM and node it had already created",
                    tag,
                )
                _deregister_node(mi_id, profile=profile, region=region)
                self._clean_up_failed_launch(
                    launcher,
                    microvm_id=microvm_id,
                    activation_id=activation.activation_id,
                    profile=profile,
                    region=region,
                )
                raise LaunchSuperseded(
                    f"the crew {tag} was torn down while this launch waited for its node to "
                    "come online, so the VM and node it created were released"
                )
        except LaunchSuperseded:
            # Already cleaned up where it was raised, and deliberately NOT moved to
            # launch_failed: the state a teardown wrote is the true one, and the
            # fence refused precisely because that state is there.
            raise
        except Exception as exc:
            logger.error("microvm launch %s failed: %s", tag, exc)
            self._clean_up_failed_launch(
                launcher,
                microvm_id=microvm_id,
                activation_id=activation.activation_id,
                profile=profile,
                region=region,
            )
            try:
                store.apply_event(tag, states.EVENT_LAUNCH_FAILED)
            except states.IllegalTransition:
                # The row moved to a terminal state under us, which is the one
                # case where this event has no edge. The launch's own error is
                # what the owner needs, so it is not replaced by this one.
                logger.info("microvm launch %s could not be marked failed: it is terminal", tag)
            raise
        store.apply_event(tag, states.EVENT_ONLINE)
        # The MI id, not the MicroVM id. This is what the launch job carries into
        # ``register`` as ``instance_id``, and the instances registry accepts an
        # ``mi-`` target with no change -- which is the whole reason this lane runs
        # a full gateway and connects over SSM rather than inventing a transport.
        return mi_id

    def begin_signin(
        self,
        *,
        instance_id: str,
        profile: str,
        region: str,
        login_target: object = None,
    ) -> MicroVmSigninHandle:
        """Hand back a handle that is already done. See :class:`MicroVmSigninHandle`."""
        return MicroVmSigninHandle(instance_id)

    def login_target_refusal(self, target: object) -> str:
        """Why this lane cannot sign a crew in as a non-default identity.

        Declared so the refusal happens at PREFLIGHT, before anything is
        provisioned or billed. Accepting the ``login_target`` keyword says an
        engine can receive a target, not that it can act on one, and this lane
        has no sign-in at all: the guest is handed a credential and refuses to
        serve without it.
        """
        if getattr(target, "is_default", True):
            return ""
        describe = getattr(target, "describe", None)
        name = describe() if callable(describe) else str(target)
        return (
            f"the MicroVM lane cannot sign in as {name}: the crew's model credential is "
            "delivered to the guest at run time through its control secret, and nothing in "
            "the VM runs an interactive sign-in. Launch with the default identity."
        )

    def register(self, *, instance_id: str, tag: str, profile: str, region: str) -> None:
        """Put the crew in the instances registry as an ``ssm`` peer.

        ``connection_method="ssm"`` with the guest's ``mi-`` node, which needs no
        change to the registry, the target validator, the port-forward argv or the
        tunnel manager -- the validator already accepts ``mi-`` and the argv has no
        lane branch by design.

        Raises when registration did not happen. ``register_instance`` is
        best-effort and answers ``None``; treating that as success marks the launch
        done for a crew the owner cannot see or reach.
        """
        row = connect.register_instance(
            instance_id,
            name=tag,
            profile=profile,
            region=region,
            connection_method="ssm",
            provisioner_id=MICROVM_PROVISIONER_ID,
            # The GUEST's user, not the registry's EC2 default. Every in-guest
            # command goes out as ``sudo -u <run_as> -i``, so a user this image
            # does not have fails with ``sudo: unknown user`` -- and the first
            # such command is the dashboard's token mint, whose failure takes the
            # tunnel down with it. The crew is then online, registered and
            # unreachable, with nothing in the control plane saying why.
            ssm_run_as=recipe_mod.GUEST_RUN_AS,
            # The crew's FRONT, not a gateway dashboard. A headless crew serves
            # one turn route on this port and nothing on the dashboard port the
            # registry defaults to, so the default would point every forward at a
            # port nobody listens on -- and the forward comes up, which makes the
            # failure read as a crew that will not answer.
            remote_port=recipe_mod.GUEST_FRONT_PORT,
        )
        if row is None:
            raise RuntimeError(
                f"the MicroVM crew {tag} launched and came online, but it could not be added "
                f"to the instances registry as {instance_id}, so nothing in the dashboard can "
                "reach it. The VM is running and billing: tear the launch down or register it "
                "by hand."
            )

    def teardown(self, *, tag: str, profile: str, region: str) -> bool:
        """Terminate this crew's VM and delete its activation.

        Finds the VM through the RECORD, which is why ``provision`` writes one
        before it calls ``RunMicrovm``: the launch job hands teardown the tag and
        nothing else.

        The crew's home goes with the VM. Nothing on this lane copies it off, so
        a teardown is the end of that crew's conversations and the owner is told so
        before they ask for one.
        """
        store = self._require_store()
        launcher = self._require_launcher()
        spec = self._require_spec()
        record = store.get(tag)
        if record is None:
            return False
        if record.microvm_id:
            try:
                launcher.terminate(record.microvm_id, profile=profile, region=region)
                launcher.wait_terminated(record.microvm_id, profile=profile, region=region)
            except AWSError as exc:
                logger.error("could not terminate MicroVM %s: %s", record.microvm_id, exc)
                return False
            # The wait above runs for minutes, so the row is re-read before
            # anything else is released. Two outcomes, and they are NOT the same.
            #
            # A relaunch of this tag inside that window is a DIFFERENT crew: it
            # minted its own activation and its own node and wrote them here. So
            # the coordinates to release are the ones this teardown read, never
            # the ones it finds now -- deleting what it finds would deregister a
            # live crew's node and delete the activation it is registering
            # through. And its row belongs to that crew, so the state below is not
            # this teardown's to write.
            live = store.get(tag)
            if live is not None and live.generation != record.generation:
                logger.info(
                    "microvm crew %s was relaunched while its teardown waited; releasing "
                    "generation %d's own activation and node and leaving the new crew alone",
                    tag,
                    record.generation,
                )
                if record.activation_id:
                    _delete_activation(record.activation_id, profile=profile, region=region)
                if record.mi_id:
                    _deregister_node(record.mi_id, profile=profile, region=region)
                # Deliberately not the secret: its path is keyed by the TAG, so the
                # crew now holding the tag reads the same one.
                return True
            # Same crew still. Take the fresh copy, because the state decision at
            # the end has to be made against the state on disk rather than the one
            # read before the wait.
            record = live or record
        if record.activation_id:
            _delete_activation(record.activation_id, profile=profile, region=region)
        if record.mi_id:
            # AFTER the activation, so a VM that outlived the terminate confirmation
            # cannot re-register under it between these two calls.
            #
            # Done here rather than left to the guest. The guest's own shutdown path
            # does deregister, and on a live teardown it did not get there: the VM was
            # cut first. A node that outlives its VM is a managed instance the account
            # keeps listing and the sweeper reports forever, so the host does it and
            # the guest doing it as well is harmless.
            _deregister_node(record.mi_id, profile=profile, region=region)
        if record.control_secret_ref:
            # AFTER the VM is confirmed gone. A secret deleted while the crew
            # still runs is a crew whose next read fails, and the read it needs is
            # the one that lets the owner authenticate to it.
            #
            # Scheduled rather than forced, so a teardown of the wrong tag stays
            # reversible inside Secrets Manager's own recovery window.
            api.delete_secret(
                record.control_secret_ref,
                profile=profile,
                region=region,
                endpoint_url=spec.endpoint_url,
            )
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.info("scheduled the control secret for microvm crew %s for deletion", tag)
        if record.state not in states.TERMINAL_STATES:
            # The VM is gone, and so is the home that was on its disk.
            try:
                store.apply_event(tag, states.EVENT_TERMINATED)
            except states.IllegalTransition:
                store.delete(tag)
        return True

    # ── internals ────────────────────────────────────────────────────────────

    def _clean_up_failed_launch(
        self,
        launcher: "VmLauncher",
        *,
        microvm_id: str,
        activation_id: str,
        profile: str,
        region: str,
    ) -> None:
        """Undo a launch that failed after the activation was minted.

        VM first, activation second. The activation is the guest's route to
        registering, so deleting it before the VM is gone removes the only thing
        that could still identify the node the terminate is about to orphan.
        Every step is best-effort and logged: a cleanup that raises replaces the
        launch's own error with its own, and the launch's error is the one the
        owner needs.
        """
        if microvm_id:
            try:
                launcher.terminate(microvm_id, profile=profile, region=region)
                launcher.wait_terminated(microvm_id, profile=profile, region=region)
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the launch error
                logger.error(
                    "a failed MicroVM launch left %s running and it could not be terminated: "
                    "%s. It is billing until the platform's maximum lifetime expires.",
                    microvm_id,
                    exc,
                )
        if activation_id:
            _delete_activation(activation_id, profile=profile, region=region)


@dataclass(frozen=True)
class Activation:
    """An SSM hybrid activation, minted per MicroVM.

    Per VM, never per image. A MicroVM is a snapshot of disk AND memory, so an
    agent registered during the image build bakes one node identity, one private
    key and one machine id into the snapshot -- and every VM launched from that
    image would then share them.
    """

    activation_id: str
    activation_code: str


class VmLauncher:
    """The platform operations a launch needs, as one seam with two implementations.

    Kept as a class rather than a Protocol because both implementations want the
    online-wait loop, which is identical for a real VM and a local container: poll
    a status, give up after a timeout, and never read a liveness field that lies
    for a dead node.
    """

    def create_activation(self, *, tag: str, profile: str, region: str) -> Activation:
        raise NotImplementedError

    def run(self, *, payload: str, profile: str, region: str, client_token: str) -> api.MicroVm:
        raise NotImplementedError

    def wait_online(self, *, activation_id: str, profile: str, region: str) -> str:
        raise NotImplementedError

    def terminate(self, microvm_id: str, *, profile: str, region: str) -> None:
        raise NotImplementedError

    def wait_terminated(self, microvm_id: str, *, profile: str, region: str) -> None:
        raise NotImplementedError


class ApiVmLauncher(VmLauncher):
    """The real platform, through ``cloud/microvm/api.py``."""

    def __init__(self, spec: MicroVmLaunchSpec, *, sleep: Callable[[float], None] = time.sleep):
        self._spec = spec
        self._sleep = sleep

    def create_activation(self, *, tag: str, profile: str, region: str) -> Activation:
        if not self._spec.activation_role_arn:
            raise ValueError(
                "this lane needs the IAM role a hybrid activation registers the guest's SSM "
                "node under: ssm:CreateActivation refuses the call without --iam-role. It is "
                "the base template's HybridActivationRoleArn output; write it as "
                "microvm.activation_role_arn in ~/.kiro/crew/cloud.json."
            )
        # The role NAME, never the ARN. ``iamRole``'s own pattern is
        # ``^[\p{L}\p{N}+=,.@\-_/]*$``, which has no colon in it, so an ARN is
        # refused with a ValidationException naming that pattern. Accepted in
        # either spelling and reduced here, because the base template's output is
        # an ARN and making the operator hand-edit it into a name is how a
        # one-character transcription error becomes a launch failure.
        # Measured against real SSM.
        role = _role_name(self._spec.activation_role_arn)
        data = checked_json(
            [
                "ssm",
                "create-activation",
                "--default-instance-name",
                f"kirocrew-{tag}",
                "--iam-role",
                role,
                "--registration-limit",
                "1",
                "--expiration-date",
                _expiry_iso(ACTIVATION_EXPIRY_MINUTES),
                # ONE argv element per tag. Joining both pairs into a single
                # comma-separated string is refused client-side:
                #   Error parsing parameter '--tags': Second instance of key
                #   "Key" encountered
                # because the CLI parses one element as one structure, and a
                # second ``Key=`` inside it is a duplicate member rather than the
                # next tag. Measured against real SSM.
                "--tags",
                "Key=kirocrew:managed,Value=true",
                f"Key=kirocrew:launch,Value={tag}",
                *(["--endpoint-url", self._spec.endpoint_url] if self._spec.endpoint_url else []),
            ],
            profile,
            region,
            # ``ssm:CreateActivation`` with a ``Tags`` parameter also needs
            # ``ssm:AddTagsToResource``, which the public permissions reference
            # does not mention. Named here so a denial is readable.
            action="ssm:CreateActivation",
        )
        if not isinstance(data, dict) or not data.get("ActivationId"):
            raise AWSError("ssm:CreateActivation returned no ActivationId")
        return Activation(
            activation_id=str(data["ActivationId"]),
            activation_code=str(data.get("ActivationCode", "")),
        )

    def resolve_image(self, *, profile: str, region: str) -> tuple[str, str]:
        """The image this launch runs, as ``(identifier, version)``.

        A prebuilt pin short-circuits everything: an operator who manages images
        themselves has already answered this question. Otherwise the image is
        resolved from its RECIPE -- the base image's digest and the crew bundle's
        digest -- which reuses an already-built image when both match and builds
        one when they do not.

        Raises rather than guessing when neither path is available, because the
        two failures a guess produces are the expensive ones: launching an image
        nobody chose, or rebuilding a multi-minute image on every launch.
        """
        spec = self._spec
        if spec.image_identifier:
            return spec.image_identifier, spec.image_version
        if not (spec.base_image_arn and spec.build_role_arn and spec.recipe_bucket):
            raise ValueError(
                "this lane has no image to launch: it needs either a prebuilt image and its "
                "version, or a base image, a build role and a recipe bucket so the crew's "
                "image can be built from its own bundle. Both are written in the microvm "
                "block of ~/.kiro/crew/cloud.json."
            )
        if not spec.bundle_dir:
            raise ValueError(
                "this lane can build an image but was given no crew bundle to build it "
                "from. Point bundle_dir at the directory packaging.build produced -- the "
                "same artifact the Fargate lane's crew layer is built from. Building from a "
                "guessed bundle is the error this lane refuses to make."
            )
        wheel = _staged_wheel()
        with tempfile.TemporaryDirectory(prefix="kirocrew-recipe-") as scratch:
            assembled = recipe_mod.remember(
                recipe_mod.assemble(
                    bundle_dir=Path(spec.bundle_dir),
                    wheel=wheel,
                    base_image_arn=spec.base_image_arn,
                    out_zip=Path(scratch) / "recipe.zip",
                )
            )
            resolver = ImageResolver(
                base_image_arn=spec.base_image_arn,
                build_role_arn=spec.build_role_arn,
                # S3-managed encryption, NOT the lane's CMK. The recipe is read by
                # the BUILD ROLE, whose template grants it ``s3:GetObject`` on the
                # recipe bucket and no ``kms:Decrypt`` on that key. Encrypting the
                # recipe under it would make every cache-miss build fail with
                # AccessDenied while the upload itself succeeded.
                upload_recipe=recipe_mod.uploader(
                    bucket=spec.recipe_bucket,
                    put_object=lambda bucket, key, path: api.put_recipe_object(
                        bucket,
                        key,
                        str(path),
                        profile=profile,
                        region=region,
                        endpoint_url=spec.endpoint_url,
                    ),
                ),
                profile=profile,
                region=region,
                endpoint_url=spec.endpoint_url,
                log_group=spec.log_group,
            )
            resolved = resolver.resolve(assembled.recipe)
        logger.info(
            "microvm image %s version %s (%s) for bundle %s",
            resolved.image_identifier,
            resolved.image_version,
            "reused" if resolved.reused else "built now",
            assembled.recipe.bundle_digest,
        )
        return resolved.image_identifier, resolved.image_version

    def run(self, *, payload: str, profile: str, region: str, client_token: str) -> api.MicroVm:
        identifier, version = self.resolve_image(profile=profile, region=region)
        return api.run_microvm(
            image_identifier=identifier,
            image_version=version,
            run_hook_payload=payload,
            maximum_duration_in_seconds=self._spec.wall_seconds,
            client_token=client_token,
            execution_role_arn=self._spec.execution_role_arn,
            log_group=self._spec.log_group,
            profile=profile,
            region=region,
            endpoint_url=self._spec.endpoint_url,
        )

    def wait_online(self, *, activation_id: str, profile: str, region: str) -> str:
        """Poll until the node that used this activation reports ``Online``.

        Filtered on the ACTIVATION id, never on a guessed ``mi-`` id. The
        activation is the only thing the control plane knew before the guest
        registered, so discovery by anything else is discovery by guess.
        """
        deadline = time.time() + ONLINE_TIMEOUT_SECONDS
        while time.time() < deadline:
            data = checked_json(
                [
                    "ssm",
                    "describe-instance-information",
                    "--filters",
                    f"Key=ActivationIds,Values={activation_id}",
                    *(
                        ["--endpoint-url", self._spec.endpoint_url]
                        if self._spec.endpoint_url
                        else []
                    ),
                ],
                profile,
                region,
                action="ssm:DescribeInstanceInformation",
            )
            rows = data.get("InstanceInformationList") or [] if isinstance(data, dict) else []
            for row in rows:
                if isinstance(row, dict) and row.get("PingStatus") == "Online":
                    return str(row.get("InstanceId", ""))
            self._sleep(ONLINE_POLL_SECONDS)
        raise TimeoutError(
            f"no SSM managed node registered through activation {activation_id} came online "
            f"within {ONLINE_TIMEOUT_SECONDS}s"
        )

    def terminate(self, microvm_id: str, *, profile: str, region: str) -> None:
        api.terminate_microvm(
            microvm_id, profile=profile, region=region, endpoint_url=self._spec.endpoint_url
        )

    def wait_terminated(self, microvm_id: str, *, profile: str, region: str) -> None:
        deadline = time.time() + ONLINE_TIMEOUT_SECONDS
        while time.time() < deadline:
            status = api.microvm_status(
                microvm_id, profile=profile, region=region, endpoint_url=self._spec.endpoint_url
            )
            if status is None or status in api.TERMINAL_MICROVM_STATES:
                return
            self._sleep(ONLINE_POLL_SECONDS)
        raise TimeoutError(f"MicroVM {microvm_id} did not reach a terminal state")


def _staged_wheel() -> Path:
    """The Kiro Crew wheel the base recipe installs, as staged beside the recipes.

    Read rather than built. A launch runs from an installed Kiro Crew, so building
    a wheel inside a launch would be the launch deciding what version of the
    product the crew runs -- and it would need a build toolchain on the owner's
    machine. ``scripts/build_microvm_image_zip.py`` stages it; a launch that finds
    none says so and names that script.
    """
    vendor = recipe_mod.RUNTIME_DIR / "vendor"
    wheels = sorted(vendor.glob("*.whl"))
    if not wheels:
        raise ValueError(
            f"no Kiro Crew wheel is staged in {vendor}, so this lane cannot assemble a "
            "recipe: the base recipe installs the wheel from the build context. Run "
            "scripts/build_microvm_image_zip.py once to stage it, or pin a prebuilt image "
            "with microvm.image_identifier and microvm.image_version."
        )
    if len(wheels) > 1:
        raise ValueError(
            f"{len(wheels)} wheels are staged in {vendor} and the base recipe's install "
            "step refuses a context holding more than one, which is what keeps its glob "
            "version-agnostic. Remove the stale ones."
        )
    return wheels[0]


def _role_name(value: str) -> str:
    """An IAM role's name, given either its name or its ARN.

    ``arn:aws:iam::123456789012:role/some/path/Name`` -> ``Name``. The path is
    dropped with the ARN because ``iamRole`` takes the name, and a role inside a
    path is named by its last segment.
    """
    text = value.strip()
    if text.startswith("arn:"):
        _, _, after = text.partition(":role/")
        text = after or text
    return text.rstrip("/").rsplit("/", 1)[-1]


def _delete_activation(activation_id: str, *, profile: str, region: str) -> None:
    """Delete one activation, best-effort and logged.

    Best-effort because the caller is already handling a failure and the owner
    needs that failure, not this one. Logged because an activation that survives
    is the measured leak this lane has to sweep for.
    """
    try:
        checked_json(
            ["ssm", "delete-activation", "--activation-id", activation_id],
            profile,
            region,
            action="ssm:DeleteActivation",
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.error(
            "SSM activation %s was not deleted (%s); it will expire on its own, and the "
            "MicroVM sweeper reports it as a zero-registration orphan until then",
            activation_id,
            exc,
        )


def _deregister_node(mi_id: str, *, profile: str, region: str) -> None:
    """Deregister one SSM managed node, best-effort and logged.

    Best-effort for the reason :func:`_delete_activation` is: the caller is finishing
    a teardown and an owner needs that outcome, not this one. Logged because a node
    that survives its VM is listed by the account and reported by the sweeper with
    nothing left to connect to.
    """
    try:
        checked_json(
            ["ssm", "deregister-managed-instance", "--instance-id", mi_id],
            profile,
            region,
            action="ssm:DeregisterManagedInstance",
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.error(
            "SSM managed node %s was not deregistered (%s); it outlives the VM it named "
            "and the MicroVM sweeper reports it until someone removes it",
            mi_id,
            exc,
        )


def _expiry_iso(minutes: int) -> str:
    """An ISO-8601 expiry *minutes* from now, in UTC, as the CLI accepts it."""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
