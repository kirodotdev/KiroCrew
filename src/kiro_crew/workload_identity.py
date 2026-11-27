"""The Crew's workload identity as its Kiro sign-in, off by default.

A team Crew deployed as a workload (an EKS pod with workload identity) has no
person's sign-in, no kiro-cli login and no Crew API key. It signs in to Kiro with
its own JWT-SVID instead. Crew does not speak the SPIFFE Workload API itself: a
sidecar such as ``spiffe-helper`` fetches the JWT-SVID for the Kiro audience,
writes it to a file and keeps it fresh, and Crew reads that file for each use.

Settings are environment variables, because they describe the deployment (the
pod spec sets them), not anything an operator edits at runtime:

``KIROCREW_WORKLOAD_IDENTITY``
    ``spiffe`` turns it on; empty or absent leaves it off. Any other value is a
    misconfiguration, still read as on, so the Crew never falls back to a person.
``KIROCREW_WORKLOAD_IDENTITY_SVID_FILE``
    The absolute path of the file the sidecar writes the JWT-SVID to.
``KIROCREW_WORKLOAD_IDENTITY_AUDIENCE``
    The audience the JWT-SVID must name: the Kiro runtime URL for the Crew's
    stage. Required: nothing public names that URL yet.
``KIROCREW_WORKLOAD_IDENTITY_PROFILE_ARN``
    The Kiro profile the Crew is registered under.

While it is on, the KAS engine's token callback answers from the file
(:mod:`kiro_crew.acp.kas_host_auth`), and nothing reads, renews or writes the
person sign-in vault. The relay lease reads a file named by the file form of
``tunnel.relay_lease_credential`` the same way (:func:`lease_bearer`), with the same
claim checks, and sends it with no ``TokenType``.

The JWT-SVID's signature is not checked here: the relying party checks it. Its
claims are, so a sidecar that wrote the wrong token fails here with a reason. The
token is never logged and never put in a ``repr``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional

from kiro_crew.bounded_read import bounded_read_regular_file

ENV_MODE = "KIROCREW_WORKLOAD_IDENTITY"
ENV_SVID_FILE = "KIROCREW_WORKLOAD_IDENTITY_SVID_FILE"
ENV_AUDIENCE = "KIROCREW_WORKLOAD_IDENTITY_AUDIENCE"
ENV_PROFILE_ARN = "KIROCREW_WORKLOAD_IDENTITY_PROFILE_ARN"
MODE_SPIFFE = "spiffe"

#: The governance class the engine is given: a Crew is served under an
#: organization's profile, never a personal one.
KAS_PROVIDER = "Enterprise"
#: The engine refuses a token with under 3 minutes left; a token this close to its
#: end means the sidecar stopped refreshing it.
MIN_REMAINING_SECONDS = 180.0
#: The largest file read; a JWT-SVID is a few kilobytes.
SVID_MAX_BYTES = 16 * 1024
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
#: The latest ``exp`` accepted: the last second of the year 9999 UTC, the last one
#: the engine's ``expiresAt`` can be written as. A later one is a broken token.
MAX_EXPIRES_AT = 253402300799.0

_PROFILE_ARN_RE = re.compile(
    r"\Aarn:aws[a-z-]*:codewhisperer:[a-z0-9-]+:\d{12}:profile/[A-Za-z0-9]+\Z", re.ASCII
)
_JWT_RE = re.compile(r"\A[A-Za-z0-9_-]+\.([A-Za-z0-9_-]+)\.[A-Za-z0-9_-]+\Z", re.ASCII)


class WorkloadIdentityError(Exception):
    """No usable JWT-SVID now. The message names the cause and never the token."""


@dataclass(frozen=True)
class Settings:
    svid_file: str
    audience: str
    profile_arn: str

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        """The settings, or :class:`WorkloadIdentityError` naming the bad one."""
        env = os.environ if env is None else env
        mode = (env.get(ENV_MODE) or "").strip().lower()
        if mode != MODE_SPIFFE:
            raise WorkloadIdentityError(f"{ENV_MODE} must be {MODE_SPIFFE!r}")
        svid_file = (env.get(ENV_SVID_FILE) or "").strip()
        if not os.path.isabs(svid_file):
            raise WorkloadIdentityError(f"{ENV_SVID_FILE} must be an absolute path")
        audience = (env.get(ENV_AUDIENCE) or "").strip()
        if not audience:
            raise WorkloadIdentityError(
                f"{ENV_AUDIENCE} is not set: set it to the Kiro runtime URL the "
                "JWT-SVID is issued for"
            )
        if not audience.startswith("https://") or any(ch.isspace() for ch in audience):
            raise WorkloadIdentityError(f"{ENV_AUDIENCE} must be an https:// audience")
        profile_arn = (env.get(ENV_PROFILE_ARN) or "").strip()
        if not _PROFILE_ARN_RE.match(profile_arn):
            raise WorkloadIdentityError(f"{ENV_PROFILE_ARN} must be a Kiro profile ARN")
        return cls(svid_file=svid_file, audience=audience, profile_arn=profile_arn)


@dataclass(frozen=True)
class Svid:
    token: str = field(repr=False)
    subject: str
    expires_at: float


def active(env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the deployment named the workload identity, even if misconfigured.

    Every person sign-in path asks this first. A misconfigured value still
    counts, so a typo fails closed instead of signing in as a person.
    """
    env = os.environ if env is None else env
    return bool((env.get(ENV_MODE) or "").strip())


def read_svid(settings: Settings, *, now: Optional[float] = None) -> Svid:
    """The JWT-SVID in the sidecar's file, read now. Blocking file IO.

    Raises :class:`WorkloadIdentityError` when the file cannot be read, holds no
    JWT, names the wrong audience or no SPIFFE subject, or is near its end. Each
    of those may pass once the sidecar writes the file again.
    """
    try:
        raw = bounded_read_regular_file(settings.svid_file, SVID_MAX_BYTES + 1)
    except OSError as exc:
        raise WorkloadIdentityError(
            f"the JWT-SVID file could not be read ({type(exc).__name__})"
        ) from None
    if len(raw) > SVID_MAX_BYTES:
        raise WorkloadIdentityError("the JWT-SVID file is too large")
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        raise WorkloadIdentityError("the JWT-SVID file is not ASCII") from None
    claims = _claims(token)
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.startswith("spiffe://"):
        raise WorkloadIdentityError("the JWT-SVID names no spiffe:// subject")
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience
    if not isinstance(audiences, list) or settings.audience not in audiences:
        raise WorkloadIdentityError("the JWT-SVID is not for the configured audience")
    expires_at = _expiry(claims.get("exp"))
    stamp = time.time() if now is None else now
    if expires_at - stamp < MIN_REMAINING_SECONDS:
        raise WorkloadIdentityError("the JWT-SVID is expired or about to expire")
    return Svid(token=token, subject=subject, expires_at=expires_at)


def _expiry(value: object) -> float:
    """The ``exp`` claim as a finite number of seconds, or the error naming it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkloadIdentityError("the JWT-SVID has no expiry")
    try:
        expires_at = float(value)
    except OverflowError:
        expires_at = math.inf
    if not math.isfinite(expires_at):
        raise WorkloadIdentityError("the JWT-SVID's expiry is not a finite time")
    if expires_at > MAX_EXPIRES_AT:
        raise WorkloadIdentityError("the JWT-SVID's expiry is past the year 9999")
    return expires_at


def _claims(token: str) -> dict:
    match = _JWT_RE.match(token)
    if match is None:
        raise WorkloadIdentityError("the JWT-SVID file holds no JWT")
    payload = match.group(1)
    try:
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(decoded)
    except (binascii.Error, ValueError, RecursionError):
        raise WorkloadIdentityError("the JWT-SVID's claims do not parse") from None
    if not isinstance(claims, dict):
        raise WorkloadIdentityError("the JWT-SVID's claims do not parse")
    return claims


def kas_access_token_answer(env: Optional[Mapping[str, str]] = None) -> dict:
    """The ``_kiro/auth/getAccessToken`` answer: the SVID, its expiry, the profile ARN.

    No ``authMethod``: ``external_idp`` would make the engine send a ``TokenType``
    header, and Kiro recognizes an SVID only without one. Blocking file IO.
    """
    settings = Settings.from_env(env)
    svid = read_svid(settings)
    return {
        "accessToken": svid.token,
        # From the epoch, not ``fromtimestamp``: Windows refuses times past 3000.
        "expiresAt": (_EPOCH + timedelta(seconds=svid.expires_at)).isoformat(),
        "profileArn": settings.profile_arn,
        "provider": KAS_PROVIDER,
    }


def lease_bearer(path: str, env: Optional[Mapping[str, str]] = None) -> str:
    """The JWT-SVID in the file at *path*, checked as :func:`read_svid` checks it.

    The relay lease's credential on a workload host: the same claims (``sub``,
    ``aud``, ``exp``) as the engine's sign-in, so a token the engine would refuse
    never goes to the lease service. Blocking file IO.
    """
    settings = replace(Settings.from_env(env), svid_file=os.path.expanduser(path))
    return read_svid(settings).token


def ready(env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether a usable JWT-SVID can be read now. Blocking file IO; never raises."""
    try:
        read_svid(Settings.from_env(env))
    except WorkloadIdentityError:
        return False
    return True


def catalog_identity(env: Optional[Mapping[str, str]] = None) -> str:
    """The account key the model catalog is kept under; ``""`` when misconfigured.

    The profile ARN names the account, so the key changes only when it does.
    """
    try:
        settings = Settings.from_env(env)
    except WorkloadIdentityError:
        return ""
    return hashlib.sha256(f"workload-identity\n{settings.profile_arn}".encode()).hexdigest()
