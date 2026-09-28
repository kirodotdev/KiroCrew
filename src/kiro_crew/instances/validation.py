"""Injection-safe validation for SSH and SSM connection inputs.

The ``SshTunnelManager`` and token-mint helper pass ``ssh_host`` and
``remote_bin`` into ``ssh`` argv lists, or ``ssm_target``/``aws_profile``/
``aws_region`` into ``aws ssm`` argv lists. Even though we never use a *local*
shell (``create_subprocess_exec`` takes an argv list), two classes of attack
remain:

1. **Option injection** — an ``ssh_host`` like ``-oProxyCommand=...`` (or an
   ``aws_profile``/``aws_region`` starting with ``-``) is parsed as an
   *option*, not a value, and can alter the local command's behavior. We
   defend by rejecting any segment that starts with ``-``.
2. **Remote shell injection** — ``remote_bin`` is embedded (double-quoted) into
   the remote command string that the remote shell evaluates. We forbid every
   shell metacharacter (``$ ; | & ` ( ) < > \n`` quotes …) so nothing can break
   out of the quotes or trigger command substitution. ``ssm_target`` is never
   embedded in a shell string (only passed as an ``aws`` CLI argument), but is
   still charset-bound to its known shapes — an EC2/SSM-managed-instance id, or
   an ECS task target for the Fargate lane.

Validation lives here, with the tunnel manager, rather than in the registry:
the registry does a light early-reject charset check, but this is the
authoritative guard applied immediately before a command line is built.
"""

from __future__ import annotations

import re

from kiro_crew.constants import AWS_PROFILE_NAME_RE

# Host charset: letters, digits, dot, hyphen, underscore, and a single optional
# ``user@`` prefix. No whitespace, no shell metacharacters. Length-bounded.
_HOST_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*\Z")
_MAX_HOST_LEN = 255

# remote_bin: an absolute or ~/ path to the kirocrew binary. Allows letters,
# digits, dot, underscore, slash, hyphen, tilde, and spaces. Crucially excludes
# ``$`` (no command substitution / var expansion we don't control), quotes,
# and every shell control character. Length-bounded.
_REMOTE_BIN_RE = re.compile(r"^[A-Za-z0-9._/~ -]{1,512}\Z")

# ssm_target: an EC2 instance id (i-<hex>) or SSM managed-instance id
# (mi-<hex>), 8-17 hex chars (AWS has used both the legacy 8-char and current
# 17-char id lengths). Never embedded in a shell string — passed only as an
# argv element to ``aws ssm`` — but still charset-bound to its known shape.
_SSM_TARGET_RE = re.compile(r"^(i|mi)-[a-f0-9]{8,17}\Z")
# ssm_target, Fargate lane: an ECS task target, ``ecs:<cluster>_<taskId>_<runtimeId>``
# — the shape ``aws ssm start-session --target`` accepts for a task running with
# ECS Exec enabled. Cluster names are AWS-bounded to 255 chars of
# ``[A-Za-z0-9_-]`` (first char alphanumeric so the value can never begin with
# ``-`` and be read as an option); the task id is 32 lowercase hex, and a Fargate
# runtime id is ``<32 hex>-<digits>``.
#
# Two details are load-bearing and were measured, not assumed:
#   * ``[0-9]`` rather than ``\d`` — ``\d`` also matches Unicode decimal digits,
#     so ``\d{1,20}`` ACCEPTS a runtime suffix of ``\u0661234567890`` (Arabic-Indic
#     digits). ``[0-9]`` rejects it. See ``test_ecs_target_rejects_unicode_digits``.
#   * ``\Z`` rather than ``$`` — ``$`` also matches just before a trailing
#     newline, so a ``$``-anchored pattern accepts ``"ecs:…-1234567890\n"``.
#
# Kept as its own anchored pattern beside the EC2 one rather than merged into a
# single alternation: one loose pattern spanning both shapes is how a charset
# leaks from one lane into the other.
_ECS_TARGET_RE = re.compile(
    r"^ecs:(?P<cluster>[A-Za-z0-9][A-Za-z0-9_-]{0,254})"
    r"_(?P<task>[0-9a-f]{32})"
    r"_(?P<runtime>[0-9a-f]{32}-[0-9]{1,20})\Z"
)
# Longest value either pattern can accept, DERIVED from the parts rather than
# stated as a number. It was stated, as 336, and that was wrong: the arithmetic in
# the comment beside it described a 20-digit runtime suffix, which comes to 346, so
# the constant silently overrode the pattern and FALSE-REJECTED every legal target
# whose suffix ran past 10 digits -- the check runs before the match, so the
# pattern never got to accept them. Failing closed made it a usability bug rather
# than a hole, but a bound that disagrees with the thing it bounds is the
# comment-contradicts-code defect in its most compact form.
#
# Built by constructing the longest legal ECS target: the ``ecs:`` scheme, a
# 255-character cluster (AWS's own limit), two 32-hex ids, their separators, and a
# 20-digit runtime suffix. A test asserts this exact value is accepted by both the
# pattern and the predicate, so the derivation cannot drift from either.
_LONGEST_ECS_TARGET = f"ecs:{'c' * 255}_{'0' * 32}_{'0' * 32}-{'9' * 20}"
_MAX_SSM_TARGET_LEN = len(_LONGEST_ECS_TARGET)
# ssm_run_as: Unix username shape, matching the charset cloud.ssm.run_command
# validates at the SSM chokepoint. Defaults to the launcher-provisioned AL2023
# user; other AMIs (e.g. Ubuntu) need their own.
_SSM_RUN_AS_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\Z")
_DEFAULT_SSM_RUN_AS = "ec2-user"

# aws_profile: a named profile from ~/.aws/config. Conservative charset (no
# shell metacharacters, no leading '-' to avoid being parsed as an option).
# '+' is included because IAM entity names permit it and it flows into
# generated profile names (e.g. SSO-derived "<account>+<permission-set>");
# it is not a shell metacharacter and the value is only ever passed as a
# discrete ``--profile <value>`` argv element. The shape is
# constants.AWS_PROFILE_NAME_RE — the single source of truth —
# aliased rather than re-spelled here; registry.py in turn aliases this
# module's name for its early record check. Behavior-neutral swap: the old
# local copy admitted a leading '-' in the class and relied on the explicit
# startswith('-') check in validate_aws_profile; the shared shape rejects it
# in the pattern as well.
_AWS_PROFILE_RE = AWS_PROFILE_NAME_RE

# aws_region: standard AWS region shape (e.g. us-east-1, us-gov-west-1).
_AWS_REGION_RE = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d{1,2}\Z")


class SshValidationError(ValueError):
    """Raised when an ssh_host or remote_bin fails injection-safe validation."""


class SsmValidationError(ValueError):
    """Raised when an ssm_target, aws_profile, or aws_region fails validation."""


def validate_ssh_host(ssh_host: str) -> str:
    """Return *ssh_host* if safe to place in an ssh argv, else raise.

    Accepts ``host``, ``host.fqdn``, an ssh-config alias, or ``user@host``.
    Rejects empty values, anything over 255 chars, any segment beginning with
    ``-`` (option injection), and any character outside the host charset.
    """
    if not ssh_host or not isinstance(ssh_host, str):
        raise SshValidationError("ssh_host must be a non-empty string")
    host = ssh_host.strip()
    if len(host) > _MAX_HOST_LEN:
        raise SshValidationError(f"ssh_host too long (>{_MAX_HOST_LEN} chars)")
    if host.startswith("-"):
        raise SshValidationError(f"ssh_host {host!r} must not start with '-' (option injection)")

    # Split an optional single user@ prefix; validate each segment.
    if host.count("@") > 1:
        raise SshValidationError(f"ssh_host {host!r} has more than one '@'")
    segments = host.split("@")
    for seg in segments:
        if not seg:
            raise SshValidationError(f"ssh_host {host!r} has an empty user/host segment")
        if seg.startswith("-"):
            raise SshValidationError(f"ssh_host segment {seg!r} must not start with '-'")
        if not _HOST_SEGMENT_RE.match(seg):
            raise SshValidationError(
                f"ssh_host segment {seg!r} contains invalid characters "
                f"(allowed: letters, digits, '.', '_', '-')"
            )
    return host


def validate_remote_bin(remote_bin: str) -> str:
    """Return *remote_bin* if safe to embed in the remote command, else raise.

    An empty string is allowed and means "use the candidate search". A non-empty
    value must match the path charset, not start with ``-``, and contain no shell
    metacharacters.
    """
    if remote_bin is None:
        return ""
    if not isinstance(remote_bin, str):
        raise SshValidationError("remote_bin must be a string")
    rb = remote_bin.strip()
    if not rb:
        return ""
    if rb.startswith("-"):
        raise SshValidationError(f"remote_bin {rb!r} must not start with '-'")
    if not _REMOTE_BIN_RE.match(rb):
        raise SshValidationError(
            f"remote_bin {rb!r} contains invalid characters "
            f"(allowed: letters, digits, '.', '_', '/', '~', '-', space)"
        )
    return rb


def ssm_target_matches(target: str) -> bool:
    """True when *target* is a well-formed SSM target for either lane.

    THE single definition of "is this an acceptable SSM target", shared by this
    module's authoritative :func:`validate_ssm_target` and the light early-reject
    check in :mod:`kiro_crew.instances.registry`. A security charset spelled out
    independently in both places is a charset that drifts, so the decision, not just
    the patterns, is shared here, and adding a third target shape changes both call
    sites at once.

    Expects an already-stripped value (see :func:`validate_ssm_target`): the
    length bound and both patterns reject surrounding whitespace.
    """
    if len(target) > _MAX_SSM_TARGET_LEN:
        return False
    return bool(_SSM_TARGET_RE.match(target) or _ECS_TARGET_RE.match(target))


def split_ecs_target(target: str) -> tuple[str, str, str] | None:
    """The ``(cluster, task_id, runtime_id)`` of an ECS target, or None.

    Here, beside the pattern, because this is the only module that knows how an
    ECS target is shaped. A caller that needs the cluster and task id -- the
    ``describe-tasks`` readiness preflight does -- would otherwise re-split the
    string on underscores, which is a second, looser reading of the same shape:
    a cluster name may itself contain underscores, so splitting on the first two
    is wrong and splitting on the last two only works by accident.

    Returns None for anything the validator would reject, so a caller cannot get
    parts out of a value that is not a well-formed target.
    """
    match = _ECS_TARGET_RE.match(target)
    if match is None:
        return None
    return match.group("cluster"), match.group("task"), match.group("runtime")


def validate_ssm_target(ssm_target: str) -> str:
    """Return *ssm_target* if it is a well-formed SSM target, else raise.

    Accepts ``i-<hex>`` (EC2 instance id), ``mi-<hex>`` (SSM managed instance,
    e.g. on-prem/hybrid), or ``ecs:<cluster>_<taskId>_<runtimeId>`` (an ECS task
    running with ECS Exec enabled — the Fargate lane).

    The value is passed only as a discrete ``--target <value>`` argv element to
    ``aws ssm``; it is never embedded in a string a shell evaluates. It is still
    charset-bound, for two reasons the argv form does not cover: a value starting
    with ``-`` would be read as an *option* rather than a target, and a value
    containing whitespace would smuggle extra arguments past the caller's intent.
    Both patterns are anchored, so neither is possible.
    """
    if not ssm_target or not isinstance(ssm_target, str):
        raise SsmValidationError("ssm_target must be a non-empty string")
    target = ssm_target.strip()
    # Validate the STRIPPED value and return that same stripped value. Returning
    # the caller's original instead would hand back the surrounding whitespace
    # that the patterns just refused — ``"  ecs:…\n"`` fails the match while its
    # stripped form passes, so the strip is what makes the check meaningful.
    if not ssm_target_matches(target):
        # Deliberately no regex in the message: this string flows verbatim into
        # the Settings form error, and the shapes in words say the same thing. A
        # mispasted id is the common case, so the reader needs the shape, not the
        # pattern.
        raise SsmValidationError(
            f"ssm_target {target!r} must be an EC2 instance id (i-...) or SSM "
            f"managed-instance id (mi-...) followed by 8 to 17 hex digits, or an "
            f"ECS task target (ecs:<cluster>_<task-id>_<runtime-id>)"
        )
    return target


def validate_ssm_run_as(ssm_run_as: str) -> str:
    """Return *ssm_run_as* if it is a safe Unix username, else raise.

    This is the remote user SSM commands are wrapped in (``sudo -u <user> -i``
    inside :func:`kiro_crew.cloud.ssm.run_command`). Empty means "use the
    default", so callers get the launcher-provisioned AL2023 user rather than an
    unquoted empty ``-u``. Validated here as well as at the chokepoint because
    this value now comes from user input in Settings.
    """
    if ssm_run_as is None:
        return _DEFAULT_SSM_RUN_AS
    if not isinstance(ssm_run_as, str):
        raise SsmValidationError("ssm_run_as must be a string")
    user = ssm_run_as.strip()
    if not user:
        return _DEFAULT_SSM_RUN_AS
    if not _SSM_RUN_AS_RE.match(user):
        raise SsmValidationError(
            f"ssm_run_as {user!r} is not a valid Unix username "
            f"(lowercase letters, digits, '_' and '-'; must not start with a digit)"
        )
    return user


def validate_aws_profile(aws_profile: str) -> str:
    """Return *aws_profile* if safe to pass as ``--profile``, else raise.

    An empty string is allowed and means "use the default credential chain".
    """
    if aws_profile is None:
        return ""
    if not isinstance(aws_profile, str):
        raise SsmValidationError("aws_profile must be a string")
    profile = aws_profile.strip()
    if not profile:
        return ""
    if profile.startswith("-"):
        raise SsmValidationError(f"aws_profile {profile!r} must not start with '-'")
    if not _AWS_PROFILE_RE.match(profile):
        raise SsmValidationError(
            f"aws_profile {profile!r} contains invalid characters "
            f"(allowed: letters, digits, '.', '_', '+', '-')"
        )
    return profile


def validate_aws_region(aws_region: str) -> str:
    """Return *aws_region* if it matches the standard AWS region shape, else raise.

    An empty string is allowed and means "use the profile's/environment's
    default region".
    """
    if aws_region is None:
        return ""
    if not isinstance(aws_region, str):
        raise SsmValidationError("aws_region must be a string")
    region = aws_region.strip()
    if not region:
        return ""
    if not _AWS_REGION_RE.match(region):
        raise SsmValidationError(
            f"aws_region {region!r} does not match the standard AWS region shape "
            f"(e.g. 'us-east-1', 'eu-west-2')"
        )
    return region
