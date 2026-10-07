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
GUEST_ENVIRONMENT: dict[str, str] = {
    "SMC_REQUIRE_AUTH_ALL_ROUTES": "1",
    # The MicroVM is the isolation boundary, so the guest does not also need a user
    # namespace around the model subprocess.
    "KIROCREW_ALLOW_UNSANDBOXED": "1",
    # Without it the guest fetches a 639 MB embedding model minutes into its life,
    # inside the window a pack runs in.
    "KIROCREW_SKIP_MODEL_DOWNLOAD": "1",
}

#: An image name the service accepts: the characters, and the length.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")

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

    def __post_init__(self) -> None:
        for label, value in (
            ("base image ARN", self.base_image_arn),
            ("base digest", self.base_digest),
            ("bundle digest", self.bundle_digest),
        ):
            if not value:
                raise ValueError(
                    f"a recipe needs a {label}: an image identified by less than its whole "
                    "input is an image a cache can serve for the wrong content"
                )

    def digest(self) -> str:
        """This recipe's identity, as hex.

        All THREE fields, including the base image ARN. ``base_digest`` covers the
        recipes and the wheel -- the content this lane builds -- and says nothing
        about which AWS-managed base the build runs ON. Leaving the ARN out means
        an operator who changes ``microvm.base_image_arn`` gets a cache hit on the
        image built against the old base, and the reuse is silent: the name
        matches, so nothing compares further.

        The fields are joined with labels and newlines, which cannot occur in any
        of them, so no set of inputs can produce another set's digest by running
        together.
        """
        material = (
            f"arn={self.base_image_arn}\n"
            f"base={self.base_digest}\n"
            f"bundle={self.bundle_digest}\n"
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
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time

    def lookup(self, recipe: Recipe) -> Optional[ResolvedImage]:
        """This recipe's launchable image, or ``None``.

        Matched by NAME, which is the recipe digest, so a hit is a content match
        by construction rather than by a remembered association. An image that
        exists but has no active version is a miss, not a hit: it is a build that
        finished and produced nothing a launch can use.
        """
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
            version = image.launchable_version
            if not version:
                logger.info(
                    "microvm image %s exists in state %s with no active version, so it "
                    "cannot be reused",
                    name,
                    image.state,
                )
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
            return ResolvedImage(
                image_identifier=image.image_arn, image_version=version, reused=True
            )
        return None

    def resolve(self, recipe: Recipe) -> ResolvedImage:
        """The image for *recipe*, reused when it exists and built when it does not.

        The lookup comes first and the upload second, so a reused image costs no
        upload and no build. A cache hit here is the difference between a launch
        measured in seconds and one measured in minutes.
        """
        found = self.lookup(recipe)
        if found is not None:
            logger.info("reusing MicroVM image %s", found.image_identifier)
            return found
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
            run_hook=HOOK_ENABLED,
            ready_hook=HOOK_ENABLED,
            suspend_hook=HOOK_ENABLED,
            resume_hook=HOOK_ENABLED,
            terminate_hook=HOOK_ENABLED,
            environment_variables=GUEST_ENVIRONMENT,
            log_group=self.log_group,
            tags={
                "kirocrew:managed": "true",
                # The whole digest, not the truncated form in the name, so the
                # inputs an image was built from are recoverable from the image
                # itself rather than only from whoever built it.
                "kirocrew:recipe": recipe.digest(),
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

    def wait_active(self, image: api.MicroVmImage, recipe: Recipe) -> ResolvedImage:
        """Poll until the build yields an ACTIVE version, or give up with a reason.

        Reads the VERSION's status and not the image's state, because they answer
        different questions and only one of them gates a launch. A failed build is
        raised as soon as the image says so rather than waited out: the states in
        :data:`api.FAILED_IMAGE_STATES` are ones no amount of waiting leaves.
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
                raise RuntimeError(
                    f"the MicroVM image build for {current.name} ended in state "
                    f"{current.state}"
                    + (
                        f" (failed version {current.latest_failed_version})"
                        if current.latest_failed_version
                        else ""
                    )
                )
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
                        reused=False,
                    )
            self.sleep(BUILD_POLL_SECONDS)
        raise TimeoutError(
            f"the MicroVM image build for {image.name} produced no active version within "
            f"{BUILD_TIMEOUT_SECONDS}s. The build is not cancelled, so the next launch's "
            "cache lookup finds it if it completes."
        )
