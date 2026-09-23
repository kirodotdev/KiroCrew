"""Persisted cloud-launcher config — **profile name only, never credentials**.

Stores the AWS *profile name*, region, and the most-recent instance tag under
``~/.kiro/crew/cloud.json``. AWS credentials are never written here — they are
resolved by the ``aws`` CLI's own provider chain from the profile.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Optional

from kiro_crew.cloud.fargate.identity import SecretRef, sole_binding
from kiro_crew.cloud.fargate.taskdef import (
    CPU_ARCHITECTURES,
    MODEL_CREDENTIAL_ENV,
    DocumentRefused,
    _refuse_undigested_image,
)
from kiro_crew.cloud.fargate.taskdef import credential_recipient as _render_credential_recipient
from kiro_crew.cloud.fargate.taskdef import (
    secret_destinations_for,
)
from kiro_crew.cloud.fargate_engine import TaskBounds
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

_FILENAME = "cloud.json"
DEFAULT_REGION = "us-east-1"

# Cap at 51 (not 63) to match ec2._TAG_RE / validate_tag: a longer last_tag
# would pass THIS sanitizer but then raise ValidationError on resume (the IAM
# role name kirocrew-ec2-<tag> maxes at 64), defeating the "just treat it as no
# last launch" intent. Keep in lockstep with ec2._TAG_RE.
#: Longest ``last_tag`` this module accepts. NAMED because two places need the number --
#: the pattern below and the room reserved for a tag provisioning has not written yet -- and
#: a second literal 51 in either would be free to drift from the other.
_TAG_MAX_LEN = 51
_TAG_RE = re.compile(rf"^[a-zA-Z0-9-]{{1,{_TAG_MAX_LEN}}}\Z")


def tag_is_wellformed(tag: str) -> bool:
    """Whether *tag* is a launch tag this module would carry into the resume path.

    Exported so ``launch_state`` applies this pattern instead of spelling it again: two
    copies of one charset is the drift this module has already paid for elsewhere, and the
    looser copy is the one a malformed value reaches ``validate_tag`` through.
    """
    return bool(tag) and bool(_TAG_RE.match(tag))


#: A digest-pinned image reference, the only form ``cloud/fargate/taskdef.py``
#: accepts. Checked here so a hand-edited ``cloud.json`` naming a movable tag is
#: treated as no Fargate configuration at all, rather than becoming a refusal at
#: the first launch -- which is where the operator has least context for it.


#: Upper bounds on how much this reader will retain from one ``fargate`` block. The
#: file is not writable through the agent file-edit tool and is mounted read-only in
#: the sandbox, but a same-UID process outside a sandbox can still write it, so the
#: reader cannot assume the bytes are small. ``load`` treats an unreadable or unparseable
#: file as absent rather than raising, so an oversized-but-valid-JSON document would be
#: parsed and every string retained, and the read runs on every request that builds
#: the provisioner list -- an unbounded list or an unbounded string is a gateway
#: memory-exhaustion surface with only manual recovery. A block that exceeds any bound
#: reads as absent, the same as any other malformed block: the ceiling is generous
#: next to any real placement, so a legitimate operator never meets it.
_MAX_LIST_ITEMS = 64
_MAX_STRING_LEN = 2048

#: Marks a key that is not in the block at all, as distinct from one whose value is
#: JSON ``null``. ``data.get(name)`` collapses the two, and they mean opposite things
#: here: an ABSENT bound takes the engine's default, while a present ``null`` is a
#: value of the wrong type and drops the whole block, the way ``assign_public_ip:
#: null`` already does. A module-level sentinel rather than a default argument so the
#: two readings cannot be confused at the call site.
_ABSENT = object()


#: Ceiling on the whole file, checked BEFORE it is parsed. ``json.loads`` builds its
#: result in memory, so a bound applied to the parsed document is applied too late;
#: the read itself is what must refuse. Generous next to a real ``cloud.json`` of a
#: few hundred bytes, and an over-sized file falls back to defaults exactly as a
#: corrupt one does rather than raising into every cloud command.
_MAX_FILE_BYTES = 1 << 20


@dataclass(frozen=True)
class FargateConfig:
    """Where an operator writes the Fargate lane's placement, image and secrets.

    **Identifiers only, never a secret value.** A crew secret is named by its
    canonical name and its ARN; the value is fetched by the task's execution role
    from Secrets Manager before the container starts, so nothing here is a
    credential and this file's no-secrets contract holds unchanged.

    The engine takes these four fields as a ``FargateLaunchSpec`` and refuses to
    guess any of them -- "an unnamed subnet or security group is the same class of
    error as deleting a task on a guess". This is the place they are written down.
    """

    cluster: str = ""
    subnets: tuple[str, ...] = ()
    security_groups: tuple[str, ...] = ()
    image: str = ""
    #: ``(canonical name, ARN)`` pairs. A pair, not a bare ARN: an ARN alone
    #: cannot say where the secret's NAME ends, because the service appends a
    #: six-character suffix and nothing marks the boundary.
    secrets: tuple[tuple[str, str], ...] = ()
    cpu_architecture: str = "X86_64"
    #: False is the safe direction, and the flag is not the boundary -- a task in
    #: a public subnet with no NAT gateway cannot pull its image without one.
    assign_public_ip: bool = False
    #: How long one of this lane's tasks may run before the launcher stops it, in
    #: seconds, or ``None`` when the operator did not say.
    #:
    #: ``None`` rather than a number, because the default belongs to the engine and
    #: this is the field that would copy it. ``TaskBounds`` already holds six hours
    #: and states where the number was read from, so an omitted key takes whatever
    #: that dataclass says and cannot drift from it -- the same reason
    #: :func:`_cpu_architectures` reads the engine's set instead of listing it.
    #:
    #: A lane whose tasks legitimately run for hours raises its own bound here, so
    #: the engine's six hours is a default and not a ceiling, and the number that
    #: stops a task is readable from the same file the operator edits rather than
    #: from Python.
    task_ttl_seconds: Optional[int] = None

    def is_complete(self) -> bool:
        """True when every field the engine requires is present and well-formed.

        INCOMPLETE MEANS ABSENT, and that is the whole design of this method. A
        half-written block must leave the lane unregistered rather than registered
        and refusing: a lane that exists and rejects every launch spends the
        operator's attention at launch time on a mistake that was visible when
        they saved the file.

        The secrets must include one named for the model credential, because the
        engine refuses a task definition that delivers none: a block with every
        placement field and no credential secret is the offered-and-refusing state
        in its most likely form.

        The whole SET is judged, not just one name: the engine's own
        ``secret_destinations`` derives every reference's destination (refusing two
        that collide) and ``sole_binding`` refuses a set naming more than one crew. So
        a valid credential beside a malformed or cross-crew reference leaves the lane
        unregistered rather than registering one whose every launch then fails. None of
        it is a second copy of those rules -- both are called, not reimplemented.

        The bound number is judged the same way and for the same reason. A
        ``task_ttl_seconds`` of zero is a lifetime the engine refuses, and a block
        carrying one would otherwise register a lane whose first launch raises out of
        ``TaskBounds`` rather than a lane that does not exist.
        """
        return bool(
            self.cluster
            and self.subnets
            and self.security_groups
            and _digest_pinned(self.image or "")
            and self.cpu_architecture in _cpu_architectures()
            and _names_model_credential(self.secrets)
            and self._bounds_are_usable()
        )

    def _bounds_are_usable(self) -> bool:
        """Whether the ENGINE accepts the bound number in this block.

        Delegates the range rule to :class:`TaskBounds`, which owns it, exactly as
        :func:`_digest_pinned` delegates the image rule to ``taskdef``'s own refusal.
        A second copy of "a lifetime of zero or less is not a lifetime" here would be
        free to disagree with the one enforced at launch, and the looser copy is the
        one a bad value would reach the engine through.
        """
        try:
            self.task_bounds()
        except ValueError:
            return False
        return True

    def task_bounds(self) -> TaskBounds:
        """This block's bound, as the engine's own :class:`TaskBounds`.

        The number is passed only when the operator wrote one, so an omitted key
        takes ``TaskBounds``'s own default rather than a copy kept here. That is why
        neither default's VALUE appears anywhere in this module: the engine stays the
        single answer to "how long, and how many", and
        ``test_the_spec_and_this_module_track_the_engine_numbers`` pins that as a check
        rather than leaving it a promise.

        Only the LIFETIME is operator-reachable. ``TaskBounds.max_running`` keeps the
        engine's fixed cap and has no ``cloud.json`` key, because ``fargate_engine``
        describes it as a ceiling an operator reaches "only by fanning out deliberately
        or by leaking, and the second is what it exists to catch" -- a key that raised
        it would work against the thing it is for. Raising it is a code change.

        The key is ``task_ttl_seconds`` while the engine's field is ``ttl_seconds``, and
        that mapping lives here and nowhere else. The key carries ``task`` deliberately:
        this lane already has a second TTL an operator meets, ``connect.mint_token``'s
        ``ttl="6h"`` session token, and the two bound different things -- a bare
        ``ttl_seconds`` in ``cloud.json`` would invite reading one as the other.

        Raises ``ValueError`` for a number the engine rejects, which is what
        :meth:`_bounds_are_usable` reads. A caller holding a complete block never sees
        it: :meth:`from_mapping` returns ``None`` for a block this refuses.
        """
        supplied: dict[str, int] = {}
        if self.task_ttl_seconds is not None:
            supplied["ttl_seconds"] = self.task_ttl_seconds
        return TaskBounds(**supplied)

    def credential_recipient(self) -> str:
        """The thing this block would hand the model credential to, in one line.

        Two values, because two of them together decide who receives it: the image, which
        is the container that gets the credential in its environment, and the ARN of the
        secret whose value the task's execution role fetches and delivers there. The image
        reference is digest-pinned (``is_complete`` requires it), so it names the registry,
        the repository and the exact content -- but the digest rule constrains the FORM of
        the reference, never who owns the registry, which is why the reference itself has to
        be read by a person rather than merely validated.

        This is what an operator CONFIRMS at launch (see
        ``fargate_engine.FargateLaunchSpec.confirmed_recipient``). It is deliberately the
        exact strings and not a digest or a shortened form of them: a fingerprint an
        operator cannot read is one they cannot refuse, and confirming a value you cannot
        compare to what you chose is not a confirmation.

        Which secret carries the credential, and how the pair is rendered, both come from the
        ENGINE's own ``taskdef.credential_recipient`` (aliased on import, since this method
        carries the same name) -- the same function the launch path
        calls on what it is about to run. One renderer, so the two cannot name a different
        recipient or spell the same one differently, and there is no second copy to drift.

        Empty for a block that is not complete, which is the same answer
        :meth:`CloudConfig.fargate_config` gives such a block: there is no lane, so there is
        nothing to confirm.
        """
        if not self.is_complete():
            return ""
        refs = tuple(SecretRef(name=name, arn=arn) for name, arn in self.secrets)
        return _render_credential_recipient(self.image, refs)

    @classmethod
    def from_mapping(cls, data: object) -> Optional["FargateConfig"]:
        """Read one block, or ``None`` for anything that is not usable.

        Every rejection returns ``None`` rather than a partially-populated object,
        so a caller cannot hold a config that looks present and is not. A secret
        entry that is not a two-string pair drops the WHOLE block, not just that
        entry: silently launching with one fewer secret than the operator wrote is
        how a task starts and then fails on a missing variable.

        ``assign_public_ip`` is read the same way: absent means ``False``, and a
        present value that is not a JSON boolean drops the block. The field decides
        network exposure, and coercing it would read the string ``"false"`` as
        true, which is the one direction this field must never be guessed in.
        """
        if not isinstance(data, dict):
            return None
        secrets: list[tuple[str, str]] = []
        raw_secrets = data.get("secrets", [])
        if not isinstance(raw_secrets, list) or len(raw_secrets) > _MAX_LIST_ITEMS:
            return None
        for entry in raw_secrets:
            if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
                return None
            name, arn = entry
            if not (isinstance(name, str) and isinstance(arn, str) and name and arn):
                return None
            if len(name) > _MAX_STRING_LEN or len(arn) > _MAX_STRING_LEN:
                return None
            secrets.append((name, arn))
        assign_public_ip = data.get("assign_public_ip", False)
        if not isinstance(assign_public_ip, bool):
            return None
        # The bound numbers, under the same discipline and for the same reason. ABSENT
        # means the operator did not say, so the engine's own default applies; a PRESENT
        # value of the wrong JSON type drops the whole block. The two are told apart by
        # :data:`_ABSENT` rather than by a ``None`` return from ``data.get``, because an
        # explicit ``null`` is a present value and must drop the block exactly as
        # ``assign_public_ip: null`` does.
        #
        # ``bool`` is a subclass of ``int``, so ``true`` would otherwise read as a
        # lifetime of one second -- the same trap ``fargate_engine._epoch_seconds``
        # guards before it accepts a number -- so it is refused by name. A float is
        # refused too: ``6.5`` is not a whole number of seconds, and rounding it would
        # be this reader guessing at a cost bound.
        #
        # There is deliberately no CEILING. The bounds above exist because an unbounded
        # string or list read from this file is a gateway memory-exhaustion surface, and
        # an integer is neither; a maximum lifetime would instead be a second invented
        # number, which is the thing this field exists to stop being necessary. A very
        # large value is an operator asking for effectively no lifetime bound, and the
        # engine's fixed running cap still holds the population.
        numbers: dict[str, Optional[int]] = {}
        for field_name in _INT_FIELD_NAMES:
            raw = data.get(field_name, _ABSENT)
            if raw is _ABSENT:
                numbers[field_name] = None
                continue
            if isinstance(raw, bool) or not isinstance(raw, int):
                return None
            numbers[field_name] = raw
        # EVERY string-typed field, in one place. `str()` on a raw value made any JSON
        # scalar truthy: `false` became the string "False", which is non-empty, so
        # `is_complete()` passed and the lane registered against a cluster named
        # "False" that does not exist. This is the same defect already fixed on
        # `assign_public_ip`, one field over, so the check is written once over the
        # field list rather than per field -- a string field added later is covered
        # without anyone remembering to add a branch.
        strings: dict[str, str] = {}
        for field_name, default in _STRING_FIELD_DEFAULTS.items():
            value = data.get(field_name, default)
            if not isinstance(value, str) or len(value) > _MAX_STRING_LEN:
                return None
            strings[field_name] = value
        subnets = _bounded_string_tuple(data.get("subnets"))
        security_groups = _bounded_string_tuple(data.get("security_groups"))
        if subnets is None or security_groups is None:
            return None
        # ONE splat, not two. mypy resolves a ``**`` argument against every parameter it
        # could reach, so two splats of different value types are each checked against
        # the other's fields and both are reported. Merging keeps both derivations
        # intact: the string defaults and the bound names are still read from the
        # dataclass rather than listed at this call site.
        derived: dict[str, Any] = {**strings, **numbers}
        candidate = cls(
            subnets=subnets,
            security_groups=security_groups,
            secrets=tuple(secrets),
            assign_public_ip=assign_public_ip,
            **derived,
        )
        return candidate if candidate.is_complete() else None


def _cpu_architectures() -> frozenset[str]:
    """The architectures the ENGINE accepts, read from it rather than copied.

    Read from the engine rather than copied, so no second list can drift from it.
    """
    return frozenset(CPU_ARCHITECTURES)


def _digest_pinned(image: str) -> bool:
    """Whether the ENGINE would accept this image reference as digest-pinned.

    Delegates to ``taskdef``'s own refusal so a movable tag is judged here by exactly
    the rule that would reject it at launch -- checked at config time so a hand-edited
    ``cloud.json`` naming a tag reads as no Fargate configuration instead of becoming a
    refusal on first use.
    """
    try:
        _refuse_undigested_image(image)
    except DocumentRefused:
        return False
    return True


def _string_field_defaults() -> dict[str, str]:
    """Every ``str``-typed field on :class:`FargateConfig`, with its default.

    Derived from the dataclass rather than listed, so adding a string field extends the
    non-string rejection and its test automatically. A hand-written list is what let
    ``cluster`` keep coercing with ``str()`` after ``assign_public_ip`` was fixed.
    """
    return {
        f.name: f.default
        for f in fields(FargateConfig)
        if f.type in ("str", str) and isinstance(f.default, str)
    }


def _int_field_names() -> tuple[str, ...]:
    """Every optional whole-number field on :class:`FargateConfig`, in declaration order.

    Derived from the dataclass for the reason :func:`_string_field_defaults` gives: a
    hand-written list is what let ``cluster`` keep coercing with ``str()`` after
    ``assign_public_ip`` was fixed one field over. A bound number added later is read
    with the same type discipline, and covered by the test that parametrizes over this
    tuple, without anyone remembering to extend either.

    ``f.type`` is compared against both the string and the object because this module
    carries ``from __future__ import annotations``, which makes every annotation a
    string today -- the same pair :func:`_string_field_defaults` compares for.
    """
    return tuple(
        f.name
        for f in fields(FargateConfig)
        if f.type in ("Optional[int]", Optional[int]) and f.default is None
    )


_STRING_FIELD_DEFAULTS = _string_field_defaults()
_INT_FIELD_NAMES = _int_field_names()


def _names_model_credential(secrets: tuple[tuple[str, str], ...]) -> bool:
    """True when a secret IS the crew's model credential, per the ENGINE's own check.

    Calls ``identity.secret_env_name`` instead of re-deriving what it accepts. Three
    rounds of review found the same defect while this was a local approximation: a
    tail-only test admitted a bare ``KIRO_API_KEY`` and a wrong-prefix
    ``junk/KIRO_API_KEY``, then a truthiness test on the crew segment admitted seven
    more spellings. Each one registered the lane so every launch through it refused --
    the offered-and-refusing state this module exists to prevent, reached from inside
    the check written to prevent it. Delegating makes the two agree by CONSTRUCTION, so
    no spelling can pass here and fail there, and no drift pin is needed because there
    is no second copy to drift.

    Imported at module scope and effectively free: ``identity`` imports only the standard
    library, and importing this module runs ``cloud/__init__.py`` -- which loads the AWS
    surface regardless -- so the engine modules add 5 modules and about 4 ms on top of the
    280 already loaded. Measured, because the cost is the only reason to put an import
    anywhere other than the top of the file.
    """
    if not secrets:
        return False
    refs = tuple(SecretRef(name=name, arn=arn) for name, arn in secrets)
    try:
        # EVERY reference, not just the first that matches. An early return accepted a
        # good credential ref sitting beside a malformed or cross-crew one, and the
        # engine then refused the whole document at launch -- registering a lane that
        # rejects every launch through it, which is the state this module exists to
        # prevent. Both calls are the ENGINE's own functions, not a second copy:
        # `secret_destinations_for` takes the references precisely so a caller holding
        # only secrets can apply that rule instead of approximating it.
        destinations = secret_destinations_for(refs)
        sole_binding({f"secrets[{i}].valueFrom": ref.arn for i, ref in enumerate(refs)})
    except Exception:  # noqa: BLE001 - any refusal means this set is not usable
        return False
    return MODEL_CREDENTIAL_ENV in destinations


def _bounded_string_tuple(value: object) -> Optional[tuple[str, ...]]:
    """Non-empty strings within the retention bounds, or ``None`` when unusable.

    ``None`` is a positive rejection that voids the whole block, used for the two
    shapes that must not be silently accepted: a list longer than ``_MAX_LIST_ITEMS``
    and a member string longer than ``_MAX_STRING_LEN``. Retaining either without a
    bound is the memory-exhaustion surface ``_MAX_LIST_ITEMS`` exists to close, and
    truncating instead would launch against a placement the operator did not write.

    A non-list still reads as the empty tuple rather than an error, so ``is_complete``
    stays the single place emptiness is judged; an empty required list is what leaves
    the lane unregistered there.
    """
    if not isinstance(value, list):
        return ()
    if len(value) > _MAX_LIST_ITEMS:
        return None
    # ALL-OR-NOTHING. Filtering the bad members out silently launched against a
    # placement the operator did not write: a subnet list with one non-string entry
    # became a shorter list, and the task ran in whichever subnets survived. One bad
    # member voids the block, like one bad secret entry does, so the operator sees an
    # unregistered lane instead of a task in the wrong place.
    for item in value:
        if not isinstance(item, str) or not item or len(item) > _MAX_STRING_LEN:
            return None
    return tuple(value)


@dataclass
class CloudConfig:
    """The OPERATOR's cloud configuration (no secrets), read and never written.

    This module has no writer. ``profile``, ``region`` and ``last_tag`` are still READ from
    here, because an install that launched before the launch record existed has its pointer
    in this file and ``cloud resume`` must keep working; ``cloud.launch_state`` is where
    those three are written now. The ``fargate`` block is the operator's own, and nothing
    in the product rewrites the file it lives in.
    """

    profile: str = ""
    region: str = DEFAULT_REGION
    last_tag: str = ""
    #: The ``fargate`` block EXACTLY as read from the file, or ``None`` when the file has
    #: none. Kept raw rather than judged, so a reader sees what the operator wrote even
    #: while it is half-finished. Whether the block is usable is
    #: :meth:`fargate_config`'s question.
    fargate: Any = None

    def fargate_config(self) -> Optional[FargateConfig]:
        """The Fargate block as a typed config, or ``None`` when it is not complete.

        ``None`` is what keeps the lane UNREGISTERED, so an operator who has not
        filled the block in is never offered a lane that would refuse them. This
        judges the raw block on every call rather than once at load, so the file
        round-trips untouched and the seam still sees complete-or-absent.
        """
        return FargateConfig.from_mapping(self.fargate)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "CloudConfig":
        """Read the record, tolerating every way the file can fail to be one.

        The tolerant policy: any unusable file reads as an unconfigured record, because a
        cloud command must not hand a raw traceback to an operator over a hand edit. The
        single fallback below is the whole refusal -- :meth:`_read_or_reason` decides WHAT
        went wrong, and this decides what to DO about it, which is nothing.

        :meth:`_read_or_reason` stays separate from this because the two answer different
        questions: it says WHAT is wrong with the file, and this decides what to do about it,
        which is nothing. A caller that needs the reason asks it directly rather than
        re-parsing, so there is one parser and no second reader to drift from it.
        """
        record, _reason = cls._read_or_reason(path or (config_dir() / _FILENAME))
        return record if record is not None else cls()

    @classmethod
    def _read_or_reason(cls, p: Path) -> "tuple[Optional[CloudConfig], Optional[str]]":
        """``(record, None)`` usable, ``(None, reason)`` present but not, ``(None, None)`` absent.

        The three-way answer is the point. Absent and unreadable are the same non-answer to
        a reader and opposite instructions to a writer: absent means write the first record,
        unreadable means do not touch the file. Collapsing them is the bug this shape exists
        to prevent.
        """
        try:
            # ONE read serving both the size bound and the parse, so the bytes measured
            # are the bytes parsed.
            #
            # Reading one byte PAST the ceiling is what makes the bound a bound: it is the
            # smallest read that can tell "at the limit" from "over it" without a second
            # look at the file, and it replaces the `stat()` for the same reason.
            with open(p, "rb") as fh:
                raw = fh.read(_MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            # Absence, not failure: the first run has no file and must be able to write one.
            return None, None
        except OSError as exc:
            # A file that exists and would not open (EACCES, EIO, a Windows share violation).
            # Indistinguishable from corruption to a reader, opposite to a writer.
            return None, f"{p} could not be read: {getattr(exc, 'strerror', None) or exc}"
        # Size BEFORE parse. The field ceilings below bound what a block may retain, but
        # json.loads builds the whole document in memory first, so a bound applied to the
        # parsed result never runs on the input that would exhaust it. Treated as a corrupt
        # file rather than an error, because every caller of this already tolerates that
        # and nothing here should raise into a cloud command.
        if len(raw) > _MAX_FILE_BYTES:
            return None, f"{p} is larger than {_MAX_FILE_BYTES} bytes"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            # Every way a hand-edited file can fail to BECOME a document, answered in one
            # place, because all of them mean the same thing to every caller: this is not a
            # config that can be read. That is the tolerate-a-corrupt-file promise in the
            # docstring, and a caller cannot act differently on any of them.
            #
            # `json.JSONDecodeError` IS a `ValueError`, so naming `ValueError` widens rather
            # than replaces, and the widening is load-bearing: the interpreter's
            # integer-string limit raises plain `ValueError` for a 5,000-digit number that
            # is perfectly valid JSON syntax. `RecursionError` is the parse side of the
            # bound the writer already answers -- the nesting a parser ACCEPTS is not the
            # nesting it can BUILD, and the limit is on total stack rather than on the
            # document, so there is no depth that is safe to assume either way.
            return None, f"{p} is not a readable JSON document: {type(exc).__name__}"
        # A hand-edited cloud.json may parse to valid JSON that is NOT an object
        # (e.g. `"hello"`, `[1,2]`, `42`, `null`); the .get() calls below would
        # then raise AttributeError and escape load() (handle_cloud only catches
        # AWS/validation errors), giving a raw traceback on every cloud command.
        # Honor the docstring's tolerate-a-corrupt-file promise: fall back to
        # defaults on any non-object shape.
        if not isinstance(data, dict):
            return None, f"{p} holds a JSON {type(data).__name__}, not an object"
        # Sanitize last_tag at the boundary: a hand-edited/corrupt cloud.json
        # must not carry a malformed tag into the resume path (downstream
        # validate_tag would raise; an empty tag just means "no last launch").
        last_tag = str(data.get("last_tag", ""))
        if last_tag and not _TAG_RE.match(last_tag):
            last_tag = ""
        record = cls(
            profile=str(data.get("profile", "")),
            region=str(data.get("region", "") or DEFAULT_REGION),
            last_tag=last_tag,
            # Deliberately NOT sanitized here, unlike last_tag: an incomplete block is
            # carried as written and judged by fargate_config() at the point of use, so an
            # operator's half-finished edit reads back as the bytes they wrote.
            fargate=data.get("fargate"),
        )
        return record, None
