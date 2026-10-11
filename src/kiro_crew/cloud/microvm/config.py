"""The operator's ``microvm`` block in ``cloud.json``, and the judge of it.

Mirrors ``FargateConfig`` exactly, including the part that matters most:
**incomplete means absent**. A half-written block leaves the lane UNREGISTERED
rather than registered and refusing. A lane that exists and rejects every launch
spends the owner's attention at launch time on a mistake that was visible when
they saved the file.

What this block may contain is identifiers only, never a secret value, for the
same reason the Fargate block says so: this file sits in the owner's config
directory, is read on every request that builds the provisioner list, and travels
in every backup of that directory. The per-crew control secret is named by a path
prefix here and minted into Secrets Manager at launch; its value never touches
this file and never touches the run payload either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Any, Optional

from kiro_crew.cloud.microvm.api import MAX_LIFETIME_SECONDS
from kiro_crew.cloud.microvm.engine import DEFAULT_WALL_SECONDS, MicroVmLaunchSpec

#: Bounds on what one block may retain, for the reason ``cloud/config.py`` gives
#: for its own: the file is readable by a same-uid process outside the sandbox, it
#: is parsed on every provisioner-list build, and an unbounded string read from it
#: is a gateway memory-exhaustion surface with only manual recovery.
_MAX_STRING_LEN = 2048

#: Tells "not written" apart from an explicit JSON ``null``. The two mean opposite
#: things: an absent key takes the engine's default, and a present ``null`` is a
#: value of the wrong type and drops the whole block.
_ABSENT = object()

#: A Lambda MicroVM image reference. Either a full ARN or the service's own image
#: id form. A bare image NAME is refused, because ``RunMicrovm`` rejects one and a
#: block carrying one would register a lane whose every launch fails.
#: A MicroVM image ARN, as the SERVICE spells it, or a bare name.
#:
#: The account field is twelve digits for an image in the owner's account and the
#: literal ``aws`` for a managed base image. The separator before the name is a
#: COLON, both in ``ListManagedMicrovmImages`` and in ``CreateMicrovmImage``'s own
#: reply:
#:
#:     arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1
#:     arn:aws:lambda:us-east-1:123456789012:microvm-image:kirocrew-crew-735b3f74
#:
#: Accepting only ``:<12 digits>:microvm-image/`` -- the wrong account form for a
#: base and the wrong separator for both -- fails every real ARN at
#: ``is_complete``, and because "incomplete means absent" the lane then silently
#: does not register. That costs a lane twice: on the base image, and on a pinned
#: prebuilt image whose identifier came straight from the service. The slash stays
#: accepted in case any surface emits it.
#:
#: The guide's example (``...:123456789012:microvm-image/al2023-base``) matches the
#: narrow pattern and names an image that does not exist, so following the
#: documentation produces a block that passes the check and a launch with no base
#: to build on. It is corrected alongside this.
_IMAGE_RE = re.compile(
    r"^(arn:aws[a-z\-]*:lambda:[a-z0-9\-]+:(\d{12}|aws):microvm-image[:/][\w.\-]+"
    r"|[\w.\-]{3,128})\Z"
)

#: An image VERSION. Pinned, never a word meaning "newest": an unset version
#: resolves at call time, so a build landing mid-launch would retarget the launch
#: onto an image nobody chose.
_VERSION_RE = re.compile(r"^[0-9][\w.\-]{0,63}\Z")

#: A KMS key id, alias or ARN. Required rather than defaulted: the per-crew
#: control secret is the one thing this lane writes that an owner's own key should
#: cover, and an omitted key silently falls back to the AWS-managed one.
_KMS_RE = re.compile(
    r"^(arn:aws[a-z\-]*:kms:[a-z0-9\-]+:\d{12}:key/[\w\-]+|alias/[\w/\-]{1,250}|[0-9a-f\-]{36})\Z"
)

#: An S3 bucket name, by the service's own rules for the subset this lane needs.
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]\Z")

#: A Secrets Manager path prefix. Slash-separated, no leading or trailing slash.
_SECRET_PREFIX_RE = re.compile(r"^[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)*\Z")

#: An IAM role ARN, for the role Lambda assumes to run an image build.
_ROLE_RE = re.compile(r"^arn:aws[a-z\-]*:iam::\d{12}:role/[\w+=,.@\-/]{1,512}\Z")


@dataclass(frozen=True)
class MicroVmConfig:
    """Where an operator writes the MicroVM lane's image, secrets and bounds.

    Every field is required except the two that have engine-owned defaults, and
    the required set is exactly what :meth:`MicroVmLaunchSpec` cannot guess.
    """

    #: The AWS-MANAGED base image a crew's image is built on, as an ARN. REQUIRED.
    #:
    #: A base, not a finished image. A crew's image on this lane is built from
    #: this base plus that crew's own signed bundle, the way the Fargate lane's
    #: task definition is built from a base image plus the same bundle. The build
    #: runs on Lambda from a recipe zip; nothing is built on the owner's machine.
    base_image_arn: str = ""
    #: The role Lambda assumes to run an image build. REQUIRED:
    #: ``CreateMicrovmImage`` marks it required, so a block without it is a lane
    #: whose first launch cannot build the image it needs.
    build_role_arn: str = ""
    #: The bucket the recipe zip is uploaded to for the build role to read.
    #: REQUIRED, because ``codeArtifact`` is required and must be an S3 URI.
    recipe_bucket: str = ""
    #: The crew bundle ``packaging.build`` produced, as a path. The SAME artifact
    #: the Fargate lane builds its crew layer from: this lane zips it and has
    #: Lambda run the build instead of running ``docker build`` locally.
    #:
    #: REQUIRED on the build path and ignored when ``image_identifier`` pins a
    #: prebuilt image, which is the one case where there is nothing to build. A
    #: block that can build but names no bundle is incomplete for the reason a
    #: zero ``wall_seconds`` is: it would register a lane whose every launch fails
    #: at preflight, rather than a lane that does not exist.
    bundle_dir: str = ""
    #: A PREBUILT image to launch instead of building one, as an ARN or id, for an
    #: operator who manages images themselves. Optional, and both of these go
    #: together: an identifier with no version is refused, because an unset
    #: version means "newest at call time" and a build landing mid-launch would
    #: retarget the launch onto an image nobody chose.
    image_identifier: str = ""
    image_version: str = ""
    #: The CMK the per-crew control secret is encrypted with. Required: see
    #: :data:`_KMS_RE`. Created by hand from
    #: ``templates/kirocrew-microvm-base.yaml``; ``provision`` never creates a key,
    #: so the resource that outlives every crew is created by a deliberate act and
    #: not by a launch.
    kms_key_id: str = ""
    #: Secrets Manager path prefix for per-crew control secrets.
    secret_path_prefix: str = "kirocrew/crew"
    #: The Secrets Manager name or ARN of the crew's MODEL credential, as the
    #: operator created it. REQUIRED, and not derived.
    #:
    #: Deriving it from the launch tag does not work, and the reason is that the
    #: tag is not the operator's to choose: a dashboard launch builds its job with
    #: no tag and ``run_launch`` then mints ``kc-<random hex>``. So a secret at
    #: ``<prefix>/<tag>/KIRO_IDENTITY`` cannot exist before the launch that
    #: invents the tag, the guest's read of it fails, and boot stops at its
    #: secrets stage while the VM bills to its wall serving nothing.
    #:
    #: The lane carries the REFERENCE and never the value: this control plane does
    #: not hold the owner's model credential and must not mint one, so the
    #: operator creates the secret once, at a path they choose, and pastes its
    #: name here. Required rather than defaulted, because every default is a path
    #: that may not exist, and an incomplete block leaves the lane unregistered
    #: rather than registering one whose every launch fails at the same stage.
    identity_secret_ref: str = ""
    #: The VM lifetime to ask for, in seconds, or ``None`` to take the platform
    #: maximum. ``None`` rather than a number, so the default lives in the engine
    #: and this is not a second copy of it.
    wall_seconds: Optional[int] = None
    #: The role the VM runs as, when the operator created one. Optional on a
    #: single-owner lane: the containment a per-crew role buys is containment
    #: BETWEEN crews, which matters for a shared roster and is ceremony for one
    #: owner running their own.
    execution_role_arn: str = ""
    #: The role a hybrid activation registers the guest's SSM node under, i.e.
    #: the base template's ``HybridActivationRoleArn`` output. REQUIRED, unlike
    #: ``execution_role_arn``: ``ssm:CreateActivation`` refuses the call without
    #: ``--iam-role``, so a block without it registers a lane whose every launch
    #: fails at the first call.
    activation_role_arn: str = ""
    #: CloudWatch log group for the GUEST's own output, set per VM.
    #:
    #: Optional, and the one optional field whose absence is worth stating: a
    #: launch without it is a launch whose failures leave no trace anywhere. A
    #: MicroVM has no channel in before its SSM node registers, so a guest that
    #: dies before that point is invisible -- the control plane sees an activation
    #: with zero registrations and a VM in RUNNING, and nothing else exists. It is
    #: not REQUIRED because an operator may legitimately decline the log group's
    #: cost, but a lane configured without it cannot be debugged.
    log_group: str = ""

    def is_complete(self) -> bool:
        """True when every field the engine requires is present and well-formed.

        The bound number is judged here too, and for the reason the Fargate block
        judges its own: a ``wall_seconds`` of zero is a lifetime the engine
        refuses, and a block carrying one would otherwise register a lane whose
        first launch raises instead of a lane that does not exist.
        """
        return bool(
            _IMAGE_RE.match(self.base_image_arn or "")
            and _ROLE_RE.match(self.build_role_arn or "")
            and _BUCKET_RE.match(self.recipe_bucket or "")
            and _KMS_RE.match(self.kms_key_id or "")
            and _SECRET_PREFIX_RE.match(self.secret_path_prefix or "")
            and bool(self.activation_role_arn)
            and bool(self.identity_secret_ref)
            and self._prebuilt_image_is_coherent()
            and self._buildable_block_has_a_bundle()
            and self._wall_is_usable()
        )

    def _buildable_block_has_a_bundle(self) -> bool:
        """Whether this block can actually produce the image it claims to.

        Only the build path needs a bundle, which is why this is not a flat
        required field: an operator who pinned a prebuilt image has already
        answered what is in it, and demanding a bundle from them would be
        demanding the input to a build that will not run.

        The path's EXISTENCE is deliberately not checked here. This runs every
        time the roster is read, and a bundle on a volume that is not mounted yet
        would make the lane blink out of the roster rather than fail a launch with
        a reason. Preflight reads the directory; this only asks whether one was
        named.
        """
        return bool(self.image_identifier or self.bundle_dir)

    def _prebuilt_image_is_coherent(self) -> bool:
        """Whether the optional prebuilt-image override is usable or absent.

        Three states, and only one of them is a mistake. Both fields empty is the
        normal case: the lane builds the image. Both filled is an operator pinning
        one they manage. ONE filled is the mistake, and it is the dangerous
        direction -- an identifier with no version resolves to whatever is newest
        at call time, so a build landing mid-launch retargets the launch onto an
        image nobody chose. A block in that state drops rather than registering a
        lane that would launch something unintended.
        """
        if not self.image_identifier and not self.image_version:
            return True
        return bool(
            _IMAGE_RE.match(self.image_identifier or "")
            and _VERSION_RE.match(self.image_version or "")
        )

    def _wall_is_usable(self) -> bool:
        if self.wall_seconds is None:
            return True
        return 1 <= self.wall_seconds <= MAX_LIFETIME_SECONDS

    def launch_spec(self) -> MicroVmLaunchSpec:
        """This block as the engine's own spec.

        The default lifetime is read from the API module's own platform maximum
        rather than copied here, so the two cannot disagree about what the
        platform allows.
        """
        return MicroVmLaunchSpec(
            base_image_arn=self.base_image_arn,
            build_role_arn=self.build_role_arn,
            recipe_bucket=self.recipe_bucket,
            bundle_dir=self.bundle_dir,
            image_identifier=self.image_identifier,
            image_version=self.image_version,
            kms_key_id=self.kms_key_id,
            secret_path_prefix=self.secret_path_prefix,
            identity_secret_ref=self.identity_secret_ref,
            wall_seconds=self.wall_seconds or DEFAULT_WALL_SECONDS,
            execution_role_arn=self.execution_role_arn,
            activation_role_arn=self.activation_role_arn,
            log_group=self.log_group,
        )

    def launch_recipient(self) -> str:
        """What the owner CONFIRMS before a launch on this lane.

        The image and the key, spelled out, because those two decide what code
        runs and whose key protects the secret that reaches it. Deliberately the
        exact strings rather than a fingerprint: a value an owner cannot read is
        one they cannot refuse, and confirming something you cannot compare to
        what you chose is not a confirmation.

        Empty for an incomplete block, which is the same answer the config gives
        such a block elsewhere -- there is no lane, so there is nothing to confirm.
        """
        if not self.is_complete():
            return ""
        if self.image_identifier:
            image = f"prebuilt image {self.image_identifier} version {self.image_version}"
        else:
            # The BASE and the BUNDLE, because those two are what the operator
            # chose and together they are what will run. The built image's own
            # identifier is a digest of them, derived at launch, and a value they
            # cannot know in advance is a value they cannot confirm.
            image = (
                f"an image built on base {self.base_image_arn} from the crew bundle at "
                f"{self.bundle_dir}"
            )
        return f"{image}; secrets encrypted with {self.kms_key_id}"

    @classmethod
    def from_mapping(cls, data: object) -> Optional["MicroVmConfig"]:
        """Read one block, or ``None`` for anything that is not usable.

        Every rejection returns ``None`` rather than a partially-populated object,
        so a caller cannot hold a config that looks present and is not.

        Types are checked per field KIND rather than per field, derived from the
        dataclass, for the reason ``cloud/config.py`` records about its own
        siblings: a hand-written branch per field is what let one field keep
        coercing with ``str()`` after the field beside it was fixed.
        """
        if not isinstance(data, dict):
            return None
        strings: dict[str, Any] = {}
        for name, default in _STRING_FIELD_DEFAULTS.items():
            value = data.get(name, default)
            if not isinstance(value, str) or len(value) > _MAX_STRING_LEN:
                return None
            strings[name] = value
        numbers: dict[str, Optional[int]] = {}
        for name in _INT_FIELD_NAMES:
            raw = data.get(name, _ABSENT)
            if raw is _ABSENT:
                numbers[name] = None
                continue
            # ``bool`` is an ``int`` subclass, so ``true`` would otherwise read as
            # a lifetime of one second. A float is refused too: a fractional
            # number of seconds is not a lifetime, and rounding it would be this
            # reader guessing at a cost bound.
            if isinstance(raw, bool) or not isinstance(raw, int):
                return None
            numbers[name] = raw
        # ONE splat, not two. mypy resolves a ``**`` argument against every
        # parameter it could reach, so two splats of different value types are
        # each checked against the other's fields and both are reported. Merging
        # keeps both derivations intact -- the defaults and the bound names are
        # still read from the dataclass rather than listed at this call site.
        derived: dict[str, Any] = {**strings, **numbers}
        candidate = cls(**derived)
        return candidate if candidate.is_complete() else None


def _string_field_defaults() -> dict[str, str]:
    return {
        f.name: f.default
        for f in fields(MicroVmConfig)
        if f.type in ("str", str) and isinstance(f.default, str)
    }


def _int_field_names() -> tuple[str, ...]:
    return tuple(
        f.name
        for f in fields(MicroVmConfig)
        if f.type in ("Optional[int]", Optional[int]) and f.default is None
    )


_STRING_FIELD_DEFAULTS = _string_field_defaults()
_INT_FIELD_NAMES = _int_field_names()
