"""Explicit operator consent before Kiro Crew spends money in an AWS account.

Two optional features reach a PAID AWS service through the provider's own
credential chain: Amazon Polly (text-to-speech, :mod:`kiro_crew.voice_reply`)
and Amazon Transcribe (speech-to-text, :mod:`kiro_crew.transcribe` and
:mod:`kiro_crew.dashboard.stt_stream`). Both omit ``--profile`` / pass no
credential resolver when no profile is configured, so "no profile set" does not
mean "no account" -- it means "whichever account the ambient environment
resolves to", which can be one the operator never intended to bill.

Selecting the provider IS the consent point, not the first request
-----------------------------------------------------------------
The first request cannot be the confirmation point in practice. Polly synthesis
is triggered from surfaces with nobody watching: ``voice_reply`` fires from a
Slack thread reply and from ``auto_reply_to_voice`` (a voice memo arrives, a
spoken reply goes back), and a scheduled job can drive either. A blocking
prompt there has no one to answer it, and "no confirmation available means no
request" would then silently disable the feature rather than protect anyone.

So the gate is at CONFIGURATION time: turning a paid provider on is what asks,
and the answer is durable. What this module enforces at the call site is a
cheap LOCAL check that issues no AWS request of its own -- which matters,
because an identity probe before every synthesis would itself be exactly the
unwanted traffic the operator is trying to avoid.

Where the grant lives, and why not ``config.json``
--------------------------------------------------
``aws_service_consent.json`` sits on the read+write KEYSTONE floor
(``security._CREW_SECRET_LEAVES``), the same placement as ``computer_use.json``
and ``ops_mission_control_policy.json``, and for the same reason: this is an
authorization record, not a preference. ``config.json`` is writable by any
auto-approved agent shell, so a grant stored there could be minted by a
prompt-injected agent -- consenting, on the operator's behalf, to spending the
operator's money. The authenticated dashboard handler opens the path directly and
is the only writer. There is deliberately no CLI verb: a terminal command that
records a grant on request is a grant an automated caller can take, and its guard
would have to key on an env var an in-process agent can unset.

Known limit, stated rather than papered over
--------------------------------------------
A grant is keyed on ``(service, profile, region)`` and records the account id
that was confirmed. A profile NAME is not an account -- ``aws configure set
credential_process ... --profile <name>`` repoints an existing profile without
touching the credential files -- so the live account is re-checked on every
gated call, bounded by a short probe cache, and a mismatch refuses the call and
revokes the grant.

What that does NOT cover: the check needs a probe that can run. When the account
cannot be resolved the call is refused rather than allowed, so the failure mode
is a withheld paid call during an outage, not an unconfirmed charge. And
``aws configure set`` itself remains available to an agent shell, so this
contains the consequence rather than removing the cause.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import aws_consent_path

logger = logging.getLogger(__name__)

#: Paid AWS services this module gates. The id is the stored grant key, so
#: renaming one invalidates existing grants (fail-closed: the operator is asked
#: again) rather than silently authorizing the wrong service.
SERVICE_POLLY = "polly"
SERVICE_TRANSCRIBE = "transcribe"
GATED_SERVICES: frozenset[str] = frozenset({SERVICE_POLLY, SERVICE_TRANSCRIBE})

#: Human-facing service names for the confirmation surfaces and the log lines.
SERVICE_LABELS: dict[str, str] = {
    SERVICE_POLLY: "Amazon Polly",
    SERVICE_TRANSCRIBE: "Amazon Transcribe",
}

#: Lock filename beside the consent file -- NOT the file itself, because
#: ``atomic_write`` renames a new inode over it and a lock on the old inode
#: protects nothing. Same placement and reasoning as
#: ``ops_mission_control.policy_store._PolicyLock``.
_LOCK_FILENAME = ".aws_consent.lock"

#: Profile names and regions are interpolated into an ``aws`` CLI argv. Values
#: are argv elements (never a shell string), so the classic injection does not
#: apply, but a leading dash would still let a value be read as an OPTION
#: rather than as the value of the option before it. Constrain both to the
#: charset AWS itself allows and require a leading alphanumeric.
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@=+-]{0,127}$")
_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: How long an identity probe result stays usable. The probe is only run by the
#: confirmation surfaces (settings panel load, CLI ``show``, the grant POST),
#: never on the synthesis path, so this only avoids re-probing across a burst of
#: panel renders.
_PROBE_TTL_SECS = 30.0

_probe_cache: dict[tuple[str, str], tuple[float, "Identity"]] = {}


@dataclass(frozen=True)
class Grant:
    """A recorded consent for one service under one profile+region."""

    service: str
    profile: str
    region: str
    account: str
    arn: str
    granted_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "profile": self.profile,
            "region": self.region,
            "account": self.account,
            "arn": self.arn,
            "granted_at": self.granted_at,
        }


@dataclass(frozen=True)
class Identity:
    """Result of ``aws sts get-caller-identity`` for a profile+region."""

    ok: bool
    account: str = ""
    arn: str = ""
    #: Operator-facing reason the probe failed. Already credential-redacted.
    detail: str = ""


def credential_source(profile: str) -> str:
    """Describe WHERE credentials for ``profile`` come from, for display.

    An empty profile is the case the issue reporter named: nothing is passed to
    the provider, so its own default chain resolves (environment variables, the
    shared config's ``default`` profile, container/instance metadata). Naming
    that explicitly is the point -- "default" reads as safe, "whichever account
    the ambient environment resolves to" does not.
    """
    if profile:
        return f"profile {profile}"
    return "AWS CLI default credential provider chain"


def _read_all() -> dict[str, Any]:
    """The whole store, or ``{}`` when it is missing or unreadable.

    Failing soft is the right read behaviour -- an authorization record that
    cannot be parsed is not an authorization, so every service refuses. See
    :func:`_preserve_if_unreadable` for what happens before a WRITE, where
    failing soft would otherwise discard the unreadable bytes.
    """
    try:
        raw = json.loads(aws_consent_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("AWS consent store is unreadable; treating every service as unconfirmed")
        return {}
    return raw if isinstance(raw, dict) else {}


def _preserve_if_unreadable() -> None:
    """Copy an unreadable store aside before a write replaces it.

    ``_read_all`` fails soft to ``{}``, so a write built on it would replace an
    unparseable file wholesale and the old bytes would be gone. What is lost is
    not a working authorization -- an unreadable store already grants nothing --
    but it may still hold the operator's other service grant, and discarding it
    silently is not this function's call to make.

    Preserved rather than refused. Refusing the write would leave an operator
    with a corrupt file unable to re-confirm from the dashboard at all, needing
    manual file surgery to recover, which is a worse outcome than a sidecar copy
    for a file that was already authorizing nothing. Found in review.
    """
    path = aws_consent_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("could not read the AWS consent store to preserve it", exc_info=True)
        return
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return  # Readable; the write is a normal read-modify-write.
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    sidecar = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        # restrict_to_owner=True locks the temp file down BEFORE the preserved
        # contents reach it (the previous post-rename lockdown left them readable
        # under the inherited DACL on Windows for the write window, issue #5285)
        # and implies the owner-only POSIX mode. The default
        # restrict_on_error="raise" surfaces a lockdown failure into this
        # except, where the whole preservation attempt is already warn-only —
        # and because the failure now happens before the rename, a sidecar that
        # could not be protected never exists at the final path at all.
        atomic_write(sidecar, raw, restrict_to_owner=True)
        logger.warning(
            "AWS consent store was unreadable; preserved the previous contents at %s "
            "before recording a new confirmation",
            sidecar.name,
        )
    except OSError:
        logger.warning("could not preserve the unreadable AWS consent store", exc_info=True)


class _ConsentLock:
    """Exclusive lock around a read-modify-write of the consent file.

    Every writer here is a read-modify-write over a file ``atomic_write``
    REPLACES wholesale, so two concurrent grants (Polly from the voice panel,
    Transcribe from the STT panel) would each write their own key onto a stale
    snapshot and the later one would silently drop the other. On an
    authorization record that is a correctness defect, not a lost-update
    annoyance: the dropped grant is a feature that then refuses to run while
    its panel shows it as confirmed.
    """

    def __init__(self) -> None:
        self._fd: int | None = None

    def __enter__(self) -> "_ConsentLock":
        lock_file = aws_consent_path().parent / _LOCK_FILENAME
        self._fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        platform_compat.acquire_lock(self._fd, exclusive=True)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            try:
                platform_compat.release_lock(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None


def _write_all(data: dict[str, Any]) -> None:
    path = aws_consent_path()
    # Fail-loud lockdown BEFORE any content lands, same as the sibling keystone
    # stores: ``restrict_to_owner=True`` applies the owner-only DACL to the temp
    # file before the payload reaches it (the previous post-rename lockdown left
    # the authorization record readable under the inherited DACL on Windows for
    # the write window, issue #5285) and implies the owner-only POSIX mode. The
    # default ``restrict_on_error="raise"`` refuses to write a record it cannot
    # protect.
    #
    # No cleanup on failure any more. Every failure inside ``atomic_write`` —
    # lockdown, payload write (ENOSPC), rename — happens BEFORE the final path
    # is touched: the helper removes its temp file and re-raises, so an
    # unprotectable record never exists at ``path`` at all. The unlink the old
    # code ran existed to remove a NEW store already PUBLISHED at a wide DACL
    # when its post-write lockdown failed; that state is unreachable now, and
    # keeping the unlink would instead delete the previous, healthy,
    # already-locked-down store on any transient failure (both pre-push
    # reviews flagged exactly that data loss).
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True), restrict_to_owner=True)


def read_grant(service: str) -> Grant | None:
    """The stored grant for ``service``, or ``None`` when there is none.

    Fails soft to ``None`` (no consent) on a missing, unreadable, or malformed
    file: an authorization record that cannot be read is not an authorization.
    """
    row = _read_all().get(service)
    if not isinstance(row, dict):
        return None
    try:
        return Grant(
            service=str(row["service"]),
            profile=str(row.get("profile", "")),
            region=str(row.get("region", "")),
            account=str(row.get("account", "")),
            arn=str(row.get("arn", "")),
            granted_at=str(row.get("granted_at", "")),
        )
    except (KeyError, TypeError):
        logger.warning("AWS consent record for %r is malformed; treating as absent", service)
        return None


def record_grant(
    service: str, *, profile: str, region: str, account: str, arn: str, granted_at: str
) -> Grant:
    """Persist consent for ``service`` under ``profile``+``region``."""
    if service not in GATED_SERVICES:
        raise ValueError(f"unknown gated service {service!r}")
    grant = Grant(
        service=service,
        profile=profile,
        region=region,
        account=account,
        arn=arn,
        granted_at=granted_at,
    )
    with _ConsentLock():
        # Inside the lock, before the read: a concurrent writer must not be able
        # to slip between preserving the old bytes and replacing them.
        _preserve_if_unreadable()
        data = _read_all()
        data[service] = grant.to_dict()
        _write_all(data)
    audit_decision(
        service,
        outcome="granted",
        detail=f"account={account} region={region or '(provider default)'} "
        f"source={credential_source(profile)}",
    )
    return grant


def revoke(service: str) -> bool:
    """Drop consent for ``service``. Returns True when a grant was removed."""
    with _ConsentLock():
        data = _read_all()
        if service not in data:
            return False
        del data[service]
        _write_all(data)
    audit_decision(service, outcome="revoked")
    return True


def is_granted(service: str, *, profile: str, region: str) -> tuple[bool, str]:
    """Whether a grant exists matching this profile+region. LOCAL only.

    Returns ``(granted, reason)``. ``reason`` is an operator-facing sentence for
    the refusal log, so every declined call says what to do about it rather
    than failing mutely.

    This is the first of two checks. It does NOT verify the live account -- see
    :func:`authorize`, which is what call sites use. Kept separate because the
    local half is what the dashboard reports and what the tests pin.
    """
    grant = read_grant(service)
    label = SERVICE_LABELS.get(service, service)
    if grant is None:
        return False, (
            f"{label} use has not been confirmed. Nothing was sent to AWS. "
            f"Confirm it in Settings -> Voice."
        )
    if grant.profile != profile or grant.region != region:
        return False, (
            f"{label} was confirmed for {credential_source(grant.profile)} in region "
            f"{grant.region or '(provider default)'}, but this call would use "
            f"{credential_source(profile)} in region {region or '(provider default)'}. "
            f"Nothing was sent to AWS. Re-confirm in Settings -> Voice."
        )
    return True, ""


async def authorize(service: str, *, profile: str, region: str) -> tuple[bool, str]:
    """The full gate every paid call must pass: local grant AND live account.

    Why the account is re-verified rather than trusted from the grant: a profile
    NAME is not an account. ``aws configure set credential_process ... --profile
    <name>`` repoints an existing profile at a different account without touching
    the credential files directly, so a grant keyed only on the profile name
    would keep authorizing calls after the account under it changed. Checking the
    name alone was measurably insufficient, so the account is checked too.

    Cost is bounded, not per-call: ``sts:GetCallerIdentity`` is free and its
    result is cached for :data:`_PROBE_TTL_SECS`, so a voice-heavy session pays
    one extra free call per window rather than one per synthesis.

    Fails CLOSED in every direction: no grant, a grant naming no account, an
    account that differs (which also revokes the stale grant), an account that
    cannot be resolved, and a grant withdrawn or changed while the check runs.
    That last case matters because the probe spawns a subprocess and is therefore
    a real suspension point, so the grant is re-asserted immediately before the
    allow rather than trusted from before the await.
    """
    granted, reason = is_granted(service, profile=profile, region=region)
    if not granted:
        return False, reason

    label = SERVICE_LABELS.get(service, service)
    grant = read_grant(service)
    if grant is None:
        # Withdrawn between the local check and here. Deny: an absent grant is
        # not a grant, and treating it as one let a call through moments after
        # the operator revoked consent. Found in review.
        return False, (
            f"{label} consent was withdrawn while this request was being checked. "
            f"Nothing was sent to AWS."
        )
    if not grant.account:
        # A grant that names no account cannot be verified against one. The
        # confirmation path never records such a grant (it refuses without a
        # resolved account), so this only arises from a hand-edited file --
        # which is exactly the case that must not skip the account check.
        return False, (
            f"{label} has a stored confirmation that names no AWS account, so it "
            f"cannot be verified. Nothing was sent to AWS. Re-confirm in "
            f"Settings -> Voice."
        )

    # Cache DELIBERATELY bypassed for an authorization decision. A cached answer
    # means a window in which a profile repointed at another account is still
    # authorized by the previous account's result, and the window is exactly the
    # thing this check exists to close. The probe is free and non-mutating, and it
    # goes to the account the operator already consented to, so paying it per
    # call costs latency rather than money. The cache stays for the confirmation
    # surfaces, where it only coalesces repeated panel renders.
    identity = await probe_identity(profile, region, use_cache=False)
    if not identity.ok or not identity.account:
        # Fail CLOSED. An earlier revision allowed this so a transient STS fault
        # would not stop voice output, but that let a repointed profile bill an
        # unconfirmed account, and on a host with boto3 but no `aws` CLI it meant
        # the account was never verified at all. Denying costs little: a grant
        # can only exist where the probe once succeeded, because the confirmation
        # refuses to record without a resolved account -- so this withholds a
        # paid call during an outage rather than breaking a working setup.
        return False, (
            f"{label} was confirmed for AWS account {grant.account}, but that "
            f"account could not be re-checked just now, so nothing was sent to "
            f"AWS. {identity.detail} The confirmation is unchanged -- retry once "
            f"the AWS CLI can resolve credentials again."
        )

    if identity.account != grant.account:
        # Off the event loop: revoke does file I/O behind a cross-process lock.
        await asyncio.to_thread(revoke, service)
        return False, (
            f"{label} was confirmed for AWS account {grant.account}, but "
            f"{credential_source(profile)} now resolves to account "
            f"{identity.account}. Nothing was sent to AWS and the confirmation "
            f"was withdrawn. Re-confirm in Settings -> Voice."
        )

    # Re-assert the grant AFTER the probe, immediately before allowing. The probe
    # spawns a subprocess, so it is a real suspension point -- long enough for the
    # operator to press Withdraw, or for a drift check on another request to
    # revoke. Without this the decision could be made from a grant that no longer
    # exists. Same gate-and-act adjacency the repo already applies elsewhere.
    still = read_grant(service)
    if still is None or still.to_dict() != grant.to_dict():
        return False, (
            f"{label} consent changed while this request was being checked. "
            f"Nothing was sent to AWS."
        )

    # Audited on every verification. The probe is uncached here, so this is
    # one event per gated call -- the price of the check being per-call.
    audit_decision(
        service,
        outcome="verified",
        detail=f"account={identity.account} source={credential_source(profile)}",
    )
    return True, ""


async def refuse_and_log(service: str, *, profile: str, region: str) -> bool:
    """:func:`authorize` plus the refusal log and audit. True when it may proceed.

    A single helper so every gated call site refuses identically -- the reason
    reaches the operator's log exactly once, at the point the request did not
    happen, and the denial reaches the tamper-evident audit log.
    """
    granted, reason = await authorize(service, profile=profile, region=region)
    if not granted:
        logger.warning("AWS request refused: %s", reason)
        audit_decision(service, outcome="denied", detail=reason)
    return granted


def audit_decision(service: str, *, outcome: str, detail: str = "") -> None:
    """Record a consent state change or a denial in the Security Event Log.

    Grants, revocations and DENIALS are recorded; allows are not. An allow
    happens once per synthesis and would bury the events that matter in noise,
    while every entry here answers a question an operator or an incident review
    actually asks: who authorized spending in this account, when was it
    withdrawn, and what was refused.

    Never raises: an audit failure must not be what stops a refusal from being
    enforced. Offloaded from the caller's perspective by staying synchronous and
    cheap -- ``sel()`` writes are already the established pattern here.
    """
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="operator" if outcome in ("granted", "revoked") else "gateway",
            operation=f"aws_consent.{outcome}",
            outcome=outcome,
            source="aws-consent",
            resources=f"{service}: {detail[:200]}" if detail else service,
        )
    except Exception:  # pragma: no cover - audit must never break the gate
        logger.debug("could not write the AWS consent audit event", exc_info=True)


def _inputs_are_safe(profile: str, region: str) -> bool:
    """Whether the profile/region are shaped safely enough to pass to the CLI.

    ``run_aws`` puts these straight into an argv as the value of ``--profile`` /
    ``--region``. They are argv elements, never a shell string, so the classic
    injection does not apply -- but a leading dash would let a value be read as
    an OPTION rather than as the value of the option before it, and both come
    from ``config.json``, which an auto-approved agent shell can write.
    """
    if profile and not _PROFILE_RE.match(profile):
        logger.warning("refusing identity probe: AWS profile name has an unexpected shape")
        return False
    if region and not _REGION_RE.match(region):
        logger.warning("refusing identity probe: AWS region has an unexpected shape")
        return False
    return True


async def probe_identity(profile: str, region: str, *, use_cache: bool = True) -> Identity:
    """Resolve which account ``profile``+``region`` would actually bill.

    ``sts:GetCallerIdentity`` is free and non-mutating, and it is the ONLY AWS
    call this module makes.

    Delegated to :func:`kiro_crew.cloud.aws.run_aws` rather than spawning the CLI
    here. That is the package's single chokepoint for ``aws`` invocations and it
    already provides everything this probe hand-rolled -- the OS sandbox wrap, a
    credential-scrubbed environment, the resource-limited spawn and a timeout --
    plus one thing the hand-rolled version did not: an agent-session chokepoint
    that only lets exact read-only operations through, and
    ``("sts", "get-caller-identity")`` is already on that allowlist. Three
    siblings (``cloud/iam.py``, ``cloud/source.py``, ``deploy/iam.py``) run this
    same call the same way, so this stops being a fourth spelling. Review found
    the duplication.

    ``run_aws`` is synchronous, so it goes to a thread: this is called from the
    gateway's event loop.
    """
    key = (profile, region)
    now = asyncio.get_running_loop().time()
    if use_cache:
        cached = _probe_cache.get(key)
        if cached is not None and (now - cached[0]) < _PROBE_TTL_SECS:
            return cached[1]

    if not _inputs_are_safe(profile, region):
        return Identity(ok=False, detail="The configured AWS profile or region is not valid.")
    if not await asyncio.to_thread(_aws_cli_resolvable):
        return Identity(
            ok=False,
            detail=(
                "The AWS CLI could not be found, so the account cannot be shown. "
                "Install it, or choose a local provider that needs no AWS account."
            ),
        )

    try:
        rc, out, err = await asyncio.to_thread(
            _run_aws,
            ["sts", "get-caller-identity", "--output", "json"],
            profile,
            region,
        )
    except Exception as exc:
        # ``run_aws`` raises for a refused chokepoint call and for a sandbox that
        # cannot be built. Either way the account is unknown, which the caller
        # treats as fail-closed.
        logger.info("identity probe could not run: %s", exc)
        identity = Identity(ok=False, detail="The AWS account could not be resolved.")
        _probe_cache[key] = (now, identity)
        return identity

    if rc != 0:
        identity = Identity(ok=False, detail=_redacted(err) or "Credentials did not resolve.")
    else:
        try:
            parsed = json.loads(out or "{}")
        except json.JSONDecodeError:
            parsed = {}
        account = str(parsed.get("Account", "")) if isinstance(parsed, dict) else ""
        arn = str(parsed.get("Arn", "")) if isinstance(parsed, dict) else ""
        if not account:
            identity = Identity(ok=False, detail="The AWS CLI returned no account id.")
        else:
            identity = Identity(ok=True, account=account, arn=arn)

    _probe_cache[key] = (now, identity)
    return identity


def _aws_cli_resolvable() -> bool:
    """Thread-side probe: is the ``aws`` CLI invocable from where we spawn?

    Routes through the deploy engine's shared well-known-dirs resolver (#4770)
    so a GUI-launched gateway's minimal PATH does not fail the consent gate
    closed before the voice sites' own resolved spawns ever run — the spawn
    below already resolves absolutely via ``cloud.aws.run_aws``, so the probe
    must agree with it. Imported at call time for the same reason as
    ``_run_aws``.
    """
    from kiro_crew.deploy.engine import resolve_aws_bin

    return shutil.which(resolve_aws_bin()) is not None


def _run_aws(args: list[str], profile: str, region: str) -> tuple[int, str, str]:
    """Thread-side import of the cloud chokepoint.

    Imported at call time, not module scope: ``kiro_crew.cloud`` is an optional
    provisioning subsystem, and the voice/STT paths that import THIS module for
    the local gate must not pay for it.
    """
    from kiro_crew.cloud.aws import run_aws

    return run_aws(args, profile, region, timeout=15)


def reconcile_drift(service: str, identity: Identity) -> bool:
    """Revoke the grant when the live account is not the confirmed one.

    Returns True when a grant was revoked. Called from the confirmation
    surfaces, which are the only places an identity is probed -- so this is
    where the profile-repointed-at-a-new-account case is caught. A failed probe
    is NOT drift (it proves nothing about the account), so it leaves the grant
    alone.
    """
    if not identity.ok or not identity.account:
        return False
    grant = read_grant(service)
    if grant is None or not grant.account or grant.account == identity.account:
        return False
    logger.warning(
        "AWS consent for %s revoked: it was confirmed for account %s but %s now resolves to a "
        "different account. The operator will be asked again.",
        service,
        grant.account,
        credential_source(grant.profile),
    )
    return revoke(service)


def _redacted(raw: str) -> str:
    """First line of CLI stderr, credential-redacted and length-capped.

    ``run_aws`` already decodes, so this takes text rather than bytes.
    """
    if not raw:
        return ""
    # Imported here rather than at module scope: ``security`` is a heavy module
    # and this module is imported by the voice/STT call paths purely for the
    # local gate, which needs none of it.
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    lines = raw.strip().splitlines()
    first = lines[0] if lines else ""
    first, _ = redact_credentials(first)
    first, _ = redact_exfiltration_urls(first)
    return first[:300]
