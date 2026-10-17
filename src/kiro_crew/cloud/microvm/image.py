"""Resolve a crew's MicroVM image: reuse the built one, or build it once.

A crew's image on this lane is BUILT, exactly as the Fargate lane's task
definition is built rather than configured, and from the same two inputs: an
AWS-managed base image, and that crew's own signed bundle. The only difference
between the lanes is the build target. Fargate pushes to a registry and registers
a task definition; this lane uploads the same recipe as a zip and asks Lambda to
build it.

**Why a cache rather than a build per launch.** A MicroVM image build is minutes
of someone else's compute and a charge on the owner's account, and the inputs
rarely change: a crew relaunched with the same base and the same bundle wants the
image it already has. So the identity of an image here is the identity of its
INPUTS -- :func:`recipe_digest` over the base image digest and the bundle digest --
and that digest is both the image's name and the thing a lookup matches on.

Keying on the inputs rather than on a counter is what makes the cache safe to
trust. A name like ``kirocrew-crew-7`` says which build it was and nothing about
what went into it, so a reused image can silently be the wrong content. A name
that IS the digest of its inputs cannot: either the digest matches, in which case
the content matches, or it does not match and nothing is reused.

**An image is not a launchable image.** ``CreateMicrovmImage`` answers with an
image in ``CREATING``, and ``RunMicrovm`` needs a VERSION whose status is
``ACTIVE``. An image that reached ``CREATED`` with no active version is a build
that finished and produced nothing runnable, which is why
:func:`resolve_image` reads the version rather than the image's own state before
it calls anything launchable.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from kiro_crew.cloud.microvm import api

logger = logging.getLogger(__name__)

#: How long to wait for a build. Builds run on Lambda's own capacity and are
#: measured in minutes, so this is generous: a timeout here costs the owner the
#: build they already paid for, and the image survives to be found by the next
#: launch's cache lookup anyway.
BUILD_TIMEOUT_SECONDS = 1800

#: Seconds between polls while a build runs. Slow on purpose: nothing about a
#: multi-minute build is improved by asking every second, and the poll is a
#: billable API call.
BUILD_POLL_SECONDS = 15

#: Memory floor for a crew VM, in MiB. A crew runs a model subprocess and a
#: backend; the measured home alone is megabytes but the working set is not.
DEFAULT_MINIMUM_MEMORY_MIB = 2048

#: The CPU architecture the crew runtime image is built for, in the API's own
#: spelling. ``CreateMicrovmImage`` takes an enum, not a machine string:
#:
#:     Value 'arm64' at 'cpuConfigurations.1.member.architecture' failed to
#:     satisfy constraint: Member must satisfy enum value set: [ARM_64, X86_64]
#:
#: Measured against the real API. The guest reports ``aarch64`` from
#: ``uname -m``; this is only what the request field is called.
DEFAULT_CPU_ARCHITECTURE = "ARM_64"

#: The port the guest serves the platform's lifecycle hooks on.
HOOK_PORT = 8080

#: Where the guest serves each hook. The platform is the only caller of these and
#: it cannot carry a header on all of them, so each answers a fixed reply that
#: reads and discloses nothing.
HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1"

#: What a hook member in ``CreateMicrovmImage`` is set to. The member is a
#: SWITCH, not the path it once was -- see the call site for the measured
#: refusal. The paths are the platform's and are still what the guest serves
#: under :data:`HOOK_PREFIX`.
HOOK_ENABLED = "ENABLED"
HOOK_DISABLED = "DISABLED"

#: Which of the platform's five hooks the guest serves, as the switches the
#: create call is sent.
#:
#: A CONSTANT rather than five literals at the call site, because this is half of
#: what an image IS: a hook the image was built without is a channel the platform
#: never opens, and a VM built that way boots, answers nothing and bills to its
#: wall. :func:`build_settings_digest` reads it, so turning one off changes every
#: image name and no launch can reuse an image built with the old set.
HOOK_SWITCHES: dict[str, str] = {
    "run": HOOK_ENABLED,
    "ready": HOOK_ENABLED,
    "suspend": HOOK_ENABLED,
    "resume": HOOK_ENABLED,
    "terminate": HOOK_ENABLED,
}

#: What the guest is built with, baked into the image rather than sent at launch.
#:
#: ``SMC_REQUIRE_AUTH_ALL_ROUTES`` is the load-bearing one and it is set HERE for a
#: reason worth stating. One image serves both lanes, and the two are bounded by
#: different things: a Fargate task sits in a private subnet behind a security
#: group with zero ingress and is handed its turn URL to point a client at, while a
#: MicroVM carries its own internet-reachable endpoint that no connector closes. So
#: the container's strictness is a property of the DEPLOYMENT, the flag defaults to
#: off so that a deployment saying nothing keeps whatever its own placement already
#: asserts, and this lane -- the one with no network bound to lean on -- turns it on
#: for the images it builds. Setting it globally instead would impose this lane's
#: answer on deployments whose placement never asked for it, the moment they were
#: relaunched on the image.
#: Image-level names that say which account and region a launch of this image may
#: carry a payload for. READ BY THE GUEST's ``/run``, which refuses a payload whose
#: claimed region, or whose secret ARNs, name anything else.
#:
#: Baked into the image rather than sent at launch, which is the whole point: the
#: hook port is the one thing a MicroVM exposes and ``/run`` is unauthenticated,
#: so a value the payload itself carried would be a check the caller sets. Anyone
#: can mint a valid activation in their OWN account; a caller who reaches the hook
#: port first registers the VM there and the platform's real ``/run`` is then
#: answered ``repeat_ignored``.
#:
#: Spelled HERE and read by name in the guest's ``hooks.py``. Two constants is the
#: cost of the host and the guest being different programs; the image gate on the
#: guest side reads these same two names.
EXPECTED_ACCOUNT_ENV = "SMC_MICROVM_ACCOUNT"
EXPECTED_REGION_ENV = "SMC_MICROVM_REGION"

GUEST_ENVIRONMENT: dict[str, str] = {
    "SMC_REQUIRE_AUTH_ALL_ROUTES": "1",
    # The MicroVM is the isolation boundary, so the guest does not also need a user
    # namespace around the model subprocess.
    "KIROCREW_ALLOW_UNSANDBOXED": "1",
    # Without it the guest fetches a 639 MB embedding model minutes into its life,
    # inside the window a pack runs in.
    "KIROCREW_SKIP_MODEL_DOWNLOAD": "1",
}


def guest_environment(*, account_id: str = "", region: str = "") -> dict[str, str]:
    """:data:`GUEST_ENVIRONMENT` plus the identity this image may be launched with.

    An empty *account_id* or *region* leaves its name out of the environment
    ENTIRELY rather than setting it empty, because the guest reads an absent or
    blank value as "not pinned" -- so the two spellings mean the same thing to it,
    and the shorter one does not describe an image as carrying a pin it does not
    have.

    Which makes an unpinned build a real possibility, and a deliberate one: an
    operator building an image by hand without naming an account gets a guest that
    accepts any well-formed payload. A launch always pins, because it knows both
    values; :func:`MicroVmLaunchEngine`'s launcher derives the account from the
    operator's own role ARNs rather than asking STS, for the reason
    ``cloud/source.py`` gives about the bucket owner -- a transient STS failure
    would answer ``""``, and ``""`` here silently turns the check off.

    A copy, never the module dict: a caller that mutated the result would be
    editing the constant every other image is built from.
    """
    env = dict(GUEST_ENVIRONMENT)
    if account_id:
        env[EXPECTED_ACCOUNT_ENV] = account_id
    if region:
        env[EXPECTED_REGION_ENV] = region
    return env


#: An image name the service accepts: the characters, and the length.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class ImageBuildFailed(RuntimeError):
    """The image under this recipe's name is in a state no build will leave.

    Its own type rather than a bare ``RuntimeError`` because the recovery is
    specific and a caller cannot guess it: the name IS the recipe digest, so
    resubmitting the same content creates nothing, and the failed image has to be
    deleted (or its content changed) before that name can build again. The message
    names the ARN for exactly that reason.
    """


def build_settings_digest(
    *,
    cpu_architecture: str,
    minimum_memory_mib: int,
    environment: dict[str, str],
    hook_port: int,
    hooks: dict[str, str],
) -> str:
    """The identity of every ``CreateMicrovmImage`` input the recipe zip does NOT carry.

    The zip holds the Dockerfiles, the serving code, the wheel and the crew's
    bundle, and :meth:`Recipe.digest` covers all of it. These five are sent
    BESIDE the zip and change what the image is just as much: a different
    ``SMC_REQUIRE_AUTH_ALL_ROUTES`` is a guest with a different auth posture, a
    different memory floor is a VM that may not fit its own crew, a different
    architecture is a binary set that does not run, and a hook turned off is a
    channel the platform never opens.

    So they belong in the cache key. Without them the name matches, nothing
    compares further, and the launch silently runs the image built with the old
    settings -- the same class of silent reuse the base image ARN was added to
    :meth:`Recipe.digest` to close.

    Labelled lines joined with newlines, which none of the values may contain, so
    no set of settings can produce another set's digest by running together. The
    mappings are sorted, because a dict's order is not part of what it means.
    """
    env = "".join(f"env.{key}={environment[key]}\n" for key in sorted(environment))
    hook = "".join(f"hook.{key}={hooks[key]}\n" for key in sorted(hooks))
    material = (
        f"arch={cpu_architecture}\nmemory={minimum_memory_mib}\nport={hook_port}\n{env}{hook}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def default_build_digest(*, account_id: str = "", region: str = "") -> str:
    """:func:`build_settings_digest` over the settings this module ships.

    What :func:`recipe.assemble` stamps into the recipe it builds, and what
    :meth:`ImageResolver.build_settings_digest` returns for a resolver that
    overrode none of them.

    *account_id* and *region* are the guest's pins, and they are arguments rather
    than module constants because they are the one build setting that is not the
    same for every install. They belong in the digest for the same reason the
    others do, and more sharply: an image built unpinned and reused for a pinned
    launch would be a VM whose ``/run`` accepts a payload for someone else's
    account, with the control plane believing it refuses one.
    """
    return build_settings_digest(
        cpu_architecture=DEFAULT_CPU_ARCHITECTURE,
        minimum_memory_mib=DEFAULT_MINIMUM_MEMORY_MIB,
        environment=guest_environment(account_id=account_id, region=region),
        hook_port=HOOK_PORT,
        hooks=HOOK_SWITCHES,
    )


#: How many hex characters of the recipe digest go in the image name. Enough that
#: a collision is not a thing anyone will meet, short enough that the name stays
#: inside the service's own bound with the prefix.
_DIGEST_IN_NAME = 32


@dataclass(frozen=True)
class Recipe:
    """What an image is built FROM, and therefore what identifies it.

    Both digests, and nothing else. Not a version number, not a timestamp, not the
    crew's name: anything that varies without the content varying would make two
    identical images, and anything that stays the same while the content changes
    would make one image serve two different bundles.
    """

    #: The AWS-managed base image this is built on, as an ARN.
    base_image_arn: str
    #: The digest of that base, which is what actually pins the content. The ARN
    #: names the image; the digest is what says which one.
    base_digest: str
    #: The digest of this crew's signed bundle, as ``packaging.build`` produced it.
    bundle_digest: str
    #: :func:`build_settings_digest` over the settings the build is sent beside
    #: the zip: the guest environment, the memory floor, the architecture, the
    #: hook port and which hooks are on.
    #:
    #: Carried on the recipe rather than applied by the resolver, because the
    #: recipe is what both the image name and the uploaded object are keyed on,
    #: and two keys for one artifact is how the zip under a name stops being the
    #: zip that name was built from.
    build_digest: str = ""

    def __post_init__(self) -> None:
        for label, value in (
            ("base image ARN", self.base_image_arn),
            ("base digest", self.base_digest),
            ("bundle digest", self.bundle_digest),
            ("build-settings digest", self.build_digest),
        ):
            if not value:
                raise ValueError(
                    f"a recipe needs a {label}: an image identified by less than its whole "
                    "input is an image a cache can serve for the wrong content"
                )

    def digest(self) -> str:
        """This recipe's identity, as hex.

        All FOUR fields, including the base image ARN. ``base_digest`` covers the
        recipes and the wheel -- the content this lane builds -- and says nothing
        about which AWS-managed base the build runs ON. Leaving the ARN out means
        an operator who changes ``microvm.base_image_arn`` gets a cache hit on the
        image built against the old base, and the reuse is silent: the name
        matches, so nothing compares further.

        ``build_digest`` is here for the same reason and closes the same hole on
        the other side of the call: the guest environment, the memory floor, the
        architecture and the hook switches are sent beside the zip, so an image
        identified by the zip alone can be reused for a build nobody would ask
        for twice. See :func:`build_settings_digest`.

        The fields are joined with labels and newlines, which cannot occur in any
        of them, so no set of inputs can produce another set's digest by running
        together.
        """
        material = (
            f"arn={self.base_image_arn}\n"
            f"base={self.base_digest}\n"
            f"bundle={self.bundle_digest}\n"
            f"build={self.build_digest}\n"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def image_name(self, prefix: str = "kirocrew-crew") -> str:
        """The image's name, which IS its recipe digest.

        A name that merely labels a build ("...-7") says nothing about content, so
        a cache keyed on it can serve the wrong image. This name cannot: a lookup
        either finds the digest it wants or finds nothing.
        """
        name = f"{prefix}-{self.digest()[:_DIGEST_IN_NAME]}"
        if not _NAME_RE.match(name):
            raise ValueError(f"derived image name {name!r} is not one the service accepts")
        return name


@dataclass(frozen=True)
class ResolvedImage:
    """An image a launch may actually use, and how it was obtained."""

    image_identifier: str
    image_version: str
    #: ``True`` when this came from the cache, ``False`` when it was built now.
    #: Recorded because a build is minutes and money, so "did we build?" is a
    #: question an operator reading a slow launch will ask.
    reused: bool

    def __post_init__(self) -> None:
        if not self.image_identifier or not self.image_version:
            raise ValueError(
                "a resolved image needs both an identifier and a version: a launch against "
                "an image with no active version is refused by the service"
            )


@dataclass
class ImageResolver:
    """Find this recipe's image, or build it once.

    Every AWS call goes through ``cloud/microvm/api.py``, so the one ``aws`` CLI
    chokepoint still applies. ``upload_recipe`` is injected because what goes in
    the zip is the packaging code's answer and not this module's: this module
    owns identity and caching, and nothing about what the recipe CONTAINS.
    """

    base_image_arn: str
    build_role_arn: str
    #: Uploads the recipe for this digest and returns its ``s3://`` URI. Called
    #: ONLY when the cache misses, so a reused image costs no upload.
    upload_recipe: Callable[[Recipe], str]
    profile: str = ""
    region: str = ""
    endpoint_url: str = ""
    name_prefix: str = "kirocrew-crew"
    #: Where the image BUILD's own logs go, passed straight through. Without it a
    #: failed build says only "The container image build failed" and writes
    #: nothing anywhere.
    log_group: str = ""
    cpu_architecture: str = DEFAULT_CPU_ARCHITECTURE
    minimum_memory_mib: int = DEFAULT_MINIMUM_MEMORY_MIB
    #: The account and region the guest's ``/run`` will accept a payload for,
    #: baked into the image. See :func:`guest_environment`.
    #:
    #: ``pinned_region`` is kept separate from :attr:`region`, which is where the
    #: API calls go, so that what the image CLAIMS and where it is BUILT are two
    #: statements rather than one -- an unpinned image is a thing an operator can
    #: deliberately build, and it must not become one by reading a field whose job
    #: is routing.
    pinned_account: str = ""
    pinned_region: str = ""
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time

    def guest_environment(self) -> dict[str, str]:
        """The environment THIS resolver bakes into the images it builds."""
        return guest_environment(account_id=self.pinned_account, region=self.pinned_region)

    def build_settings_digest(self) -> str:
        """:func:`build_settings_digest` over what THIS resolver will send."""
        return build_settings_digest(
            cpu_architecture=self.cpu_architecture,
            minimum_memory_mib=self.minimum_memory_mib,
            environment=self.guest_environment(),
            hook_port=HOOK_PORT,
            hooks=HOOK_SWITCHES,
        )

    def _checked(self, recipe: Recipe) -> Recipe:
        """*recipe*, refused if its build digest is not the one this resolver sends.

        A refusal rather than a silent re-stamp. The recipe's digest is also the
        key the uploaded zip is stored under, so re-stamping it here would look
        for a zip under a name nothing staged -- and accepting the mismatch would
        reuse an image whose settings are not the ones about to be sent, which is
        the whole hole ``build_digest`` closes.
        """
        mine = self.build_settings_digest()
        if recipe.build_digest != mine:
            raise ValueError(
                "this recipe was assembled for different image build settings "
                f"({recipe.build_digest[:12]}) than this resolver sends ({mine[:12]}): the "
                "architecture, memory floor, guest environment and hook switches are half of "
                "what an image is, so reusing one across them would launch content nobody "
                "asked for. Re-assemble the recipe."
            )
        return recipe

    def find(self, recipe: Recipe) -> Optional[api.MicroVmImage]:
        """The image stored under this recipe's name, whatever state it is in.

        Matched by NAME, which is the recipe digest, so a hit is a content match
        by construction rather than by a remembered association. Separate from
        :meth:`lookup` because "is there an image here" and "may a launch use it"
        are different questions with different answers for ``CREATING`` -- and a
        caller that cannot tell them apart resubmits a name that is already taken.
        """
        self._checked(recipe)
        name = recipe.image_name(self.name_prefix)
        for image in api.list_microvm_images(
            name_filter=name,
            profile=self.profile,
            region=self.region,
            endpoint_url=self.endpoint_url,
        ):
            if image.name != name:
                # ``nameFilter`` is a filter, not an equality check, so a prefix
                # match can return a sibling. Compared exactly here, because
                # launching a sibling's image is launching unknown content.
                continue
            return image
        return None

    def _launchable(self, image: api.MicroVmImage) -> Optional[ResolvedImage]:
        """*image* as a launchable pair, or ``None`` while it has no active version.

        An image existing and an image being launchable are different facts: the
        state says whether the BUILD finished, and the version status says whether
        anything came out of it that ``RunMicrovm`` accepts.
        """
        version = image.launchable_version
        if not version:
            return None
        status = api.image_version_status(
            image.image_arn,
            version,
            profile=self.profile,
            region=self.region,
            endpoint_url=self.endpoint_url,
        )
        if status != api.IMAGE_VERSION_ACTIVE:
            return None
        return ResolvedImage(image_identifier=image.image_arn, image_version=version, reused=True)

    def _refuse_failed(self, image: api.MicroVmImage) -> None:
        """Raise when *image* is in a state no amount of waiting or retrying leaves.

        Named rather than returned as a miss. The name is the recipe digest, so a
        miss sends the caller straight back to ``CreateMicrovmImage`` with the
        same name -- and that name is burned for this content: the failed version
        is the image's last one, so it cannot be deleted, and the image is not in
        a deletable state. The ARN is in the message because deleting it (or
        changing the content) is the only way forward and an operator needs the
        handle to do either.
        """
        if image.state not in api.FAILED_IMAGE_STATES:
            return
        failed = (
            f" (failed version {image.latest_failed_version})"
            if image.latest_failed_version
            else ""
        )
        raise ImageBuildFailed(
            f"the MicroVM image {image.name} is in state {image.state}{failed}, and its name "
            "is this recipe's own digest, so resubmitting the same content builds nothing. "
            f"Delete {image.image_arn} or change the recipe."
        )

    def lookup(self, recipe: Recipe) -> Optional[ResolvedImage]:
        """This recipe's launchable image, or ``None`` when there is none to reuse.

        ``None`` means exactly one thing -- nothing here is launchable yet -- and
        it is NOT an invitation to resubmit the name: an image in ``CREATING`` is
        a build already running under it. :meth:`resolve` is what tells those
        apart, by asking :meth:`find` rather than this.

        Raises :class:`ImageBuildFailed` for an image no build will complete, so
        a caller cannot read a burned name as an empty one.
        """
        image = self.find(recipe)
        if image is None:
            return None
        self._refuse_failed(image)
        found = self._launchable(image)
        if found is None:
            logger.info(
                "microvm image %s exists in state %s with no active version, so it "
                "cannot be reused",
                image.name,
                image.state,
            )
        return found

    def resolve(self, recipe: Recipe) -> ResolvedImage:
        """The image for *recipe*, reused when it exists and built when it does not.

        The lookup comes first and the upload second, so a reused image costs no
        upload and no build. A cache hit here is the difference between a launch
        measured in seconds and one measured in minutes.

        Three outcomes for an image that already exists under this name, and they
        are not interchangeable:

        ``CREATE_FAILED`` and the rest of :data:`api.FAILED_IMAGE_STATES`
            Refused, naming the ARN. The name is the recipe digest and cannot be
            rebuilt, so resubmitting it dispatches no build and leaves the caller
            waiting out a timeout for one that was never started.
        ``CREATING``, or ``CREATED`` with no ACTIVE version yet
            Waited on. Another launch's build is already running under this name;
            a second ``CreateMicrovmImage`` adds nothing and the version it would
            report is the one this wait is already watching.
        An ACTIVE version
            Reused.
        """
        image = self.find(recipe)
        if image is not None:
            self._refuse_failed(image)
            found = self._launchable(image)
            if found is not None:
                logger.info("reusing MicroVM image %s", found.image_identifier)
                return found
            # A build is already under way under this name, which is the recipe's
            # own digest. Waiting is the only move: the name cannot be submitted
            # twice and the version this build produces is the one a launch wants.
            logger.info(
                "microvm image %s is in state %s with no active version yet; waiting for the "
                "build already running under its name rather than resubmitting it",
                image.name,
                image.state,
            )
            return self.wait_active(image, recipe, reused=True)
        uri = self.upload_recipe(recipe)
        image = api.create_microvm_image(
            name=recipe.image_name(self.name_prefix),
            base_image_arn=self.base_image_arn,
            build_role_arn=self.build_role_arn,
            code_artifact_uri=uri,
            cpu_architecture=self.cpu_architecture,
            minimum_memory_mib=self.minimum_memory_mib,
            hook_port=HOOK_PORT,
            # ENABLED/DISABLED, not a path. Each hook member is a SWITCH and the
            # platform owns the paths it calls, so a path here is refused against
            # an enum:
            #
            #     Value '/aws/lambda-microvms/runtime/v1/ready' at
            #     'hooks.microvmImageHooks.ready' failed to satisfy constraint:
            #     Member must satisfy enum value set: [DISABLED, ENABLED]
            #
            # Measured against the real API. HOOK_PREFIX remains the truth about
            # where the guest SERVES them, because those are the paths the
            # platform calls; only the request shape differs from it.
            #
            # Read from :data:`HOOK_SWITCHES` rather than written out five times,
            # because that mapping is what :func:`build_settings_digest` hashes:
            # a switch changed in one place and not the other would be an image
            # built with a channel the cache key says is still there.
            run_hook=HOOK_SWITCHES["run"],
            ready_hook=HOOK_SWITCHES["ready"],
            suspend_hook=HOOK_SWITCHES["suspend"],
            resume_hook=HOOK_SWITCHES["resume"],
            terminate_hook=HOOK_SWITCHES["terminate"],
            # The composed environment, not the bare constant: it carries the
            # account and region the guest's ``/run`` refuses a payload outside,
            # and the digest in this image's own name is taken over the same
            # mapping.
            environment_variables=self.guest_environment(),
            log_group=self.log_group,
            tags={
                "kirocrew:managed": "true",
                # The whole digest, not the truncated form in the name, so the
                # inputs an image was built from are recoverable from the image
                # itself rather than only from whoever built it.
                "kirocrew:recipe": recipe.digest(),
                # The settings half, separately, because the recipe digest covers
                # it as a hash and an operator reading an image needs to be able
                # to tell which settings it was built with.
                "kirocrew:build-settings": recipe.build_digest,
            },
            # Content PLUS this attempt. A purely content-derived token is an
            # idempotency key that outlives the image it created, so a RETRY
            # after a failed build is swallowed: the call returns the old
            # record's shape, no build is dispatched, and the version sits
            # PENDING with updatedAt equal to createdAt -- the same undiagnosable
            # state as a build role that cannot log. Measured live, by
            # deleting a CREATE_FAILED image and resubmitting the same recipe.
            #
            # The IMAGE's identity is still purely content: the name is the
            # recipe digest and the cache hit is matched on it. Only the retry
            # key varies, which is what a retry key is for.
            client_token=f"{recipe.digest()[:_DIGEST_IN_NAME]}-{int(self.now())}"[:64],
            profile=self.profile,
            region=self.region,
            endpoint_url=self.endpoint_url,
        )
        return self.wait_active(image, recipe)

    def wait_active(
        self, image: api.MicroVmImage, recipe: Recipe, *, reused: bool = False
    ) -> ResolvedImage:
        """Poll until the build yields an ACTIVE version, or give up with a reason.

        Reads the VERSION's status and not the image's state, because they answer
        different questions and only one of them gates a launch. A failed build is
        raised as soon as the image says so rather than waited out: the states in
        :data:`api.FAILED_IMAGE_STATES` are ones no amount of waiting leaves.

        *reused* is what this caller knows and this method cannot: waiting on a
        ``CREATING`` image the cache lookup found is not the same event as waiting
        on a build this launch dispatched, and "did we build?" is the question an
        operator reading a slow launch asks.
        """
        deadline = self.now() + BUILD_TIMEOUT_SECONDS
        while self.now() < deadline:
            current = api.get_microvm_image(
                image.image_arn,
                profile=self.profile,
                region=self.region,
                endpoint_url=self.endpoint_url,
            )
            if current is None:
                raise RuntimeError(
                    f"the MicroVM image build for {image.name} vanished while it was running; "
                    "nothing was launched"
                )
            if current.state in api.FAILED_IMAGE_STATES:
                # The same refusal the cache lookup gives, so a build that fails
                # while this launch watches it and one that had already failed
                # read the same and both name the ARN.
                self._refuse_failed(current)
            version = current.launchable_version
            if version:
                status = api.image_version_status(
                    current.image_arn,
                    version,
                    profile=self.profile,
                    region=self.region,
                    endpoint_url=self.endpoint_url,
                )
                if status == api.IMAGE_VERSION_ACTIVE:
                    return ResolvedImage(
                        image_identifier=current.image_arn,
                        image_version=version,
                        reused=reused,
                    )
            self.sleep(BUILD_POLL_SECONDS)
        raise TimeoutError(
            f"the MicroVM image build for {image.name} produced no active version within "
            f"{BUILD_TIMEOUT_SECONDS}s. The build is not cancelled, so the next launch's "
            "cache lookup finds it if it completes."
        )
