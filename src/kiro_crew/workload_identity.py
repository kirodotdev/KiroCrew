"""The Crew's workload identity as its Kiro credential, off by default.

When ``KIROCREW_WORKLOAD_IDENTITY=spiffe`` is set, Crew Hub obtains its Kiro bearer
token by attestation instead of from a person's sign-in: it fetches a JWT-SVID
audienced to Kiro from the SPIFFE Workload API socket
(:mod:`kiro_crew.spiffe_workload_api`) and presents it, with the Crew's profile
ARN, to the KAS engine's ``_kiro/auth/getAccessToken`` callback
(:mod:`kiro_crew.acp.kas_host_auth`). The answer has no ``authMethod``, so the
engine sends no ``TokenType`` header and the Kiro backend's bearer check
recognizes the SVID by its issuer. The relay lease client reads the same SVID when
``tunnel.relay_lease_credential`` is :data:`LEASE_CREDENTIAL_REF`
(``workload-identity:jwt-svid``).

This is the Crew's own Kiro credential. It is distinct from the AgentCore agent
identity seam (:class:`kiro_crew.platform.interfaces.AgentIdentityProvider`).

Settings are environment variables, because they describe the deployment (a pod
spec sets them), not anything an operator edits at runtime:

``KIROCREW_WORKLOAD_IDENTITY``
    ``spiffe`` turns the source on; empty or absent leaves it off.
``KIROCREW_WORKLOAD_IDENTITY_AUDIENCE``
    The audience to request. Required when on.
``KIROCREW_WORKLOAD_IDENTITY_PROFILE_ARN``
    The Kiro profile the Crew is registered under. Required when on.
``KIROCREW_WORKLOAD_IDENTITY_SPIFFE_ID``
    Optional: the Crew's SPIFFE ID; an SVID for any other subject is refused.
``KIROCREW_WORKLOAD_IDENTITY_SOCKET``
    Optional: the Workload API endpoint; defaults to ``SPIFFE_ENDPOINT_SOCKET``.

The SVID is never logged and never put in a ``repr``; the log names its subject,
audience, issuer and lifetime, which are not secret.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Mapping, Optional

from kiro_crew.spiffe_workload_api import JwtSvid, WorkloadApiError, fetch_jwt_svid

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import SessionPrincipal
    from kiro_crew.platform.interfaces import WorkloadIdentity as PlatformWorkloadIdentity

logger = logging.getLogger(__name__)

ENV_MODE = "KIROCREW_WORKLOAD_IDENTITY"
ENV_AUDIENCE = "KIROCREW_WORKLOAD_IDENTITY_AUDIENCE"
ENV_PROFILE_ARN = "KIROCREW_WORKLOAD_IDENTITY_PROFILE_ARN"
ENV_SPIFFE_ID = "KIROCREW_WORKLOAD_IDENTITY_SPIFFE_ID"
ENV_SOCKET = "KIROCREW_WORKLOAD_IDENTITY_SOCKET"
SPIFFE_ENDPOINT_SOCKET = "SPIFFE_ENDPOINT_SOCKET"
MODE_SPIFFE = "spiffe"

#: ``tunnel.relay_lease_credential`` value that names the workload identity.
LEASE_CREDENTIAL_REF = "workload-identity:jwt-svid"
#: The governance class the engine is given: a Crew is served under an
#: organization's profile, never a personal one.
KAS_PROVIDER = "Enterprise"
#: A cached SVID is reused only while at least this much of it is left, and at
#: least half its lifetime; the engine refuses a token with under 3 minutes left.
REFRESH_MARGIN_SECONDS = 300.0

_PROFILE_ARN_RE = re.compile(
    r"\Aarn:aws[a-z-]*:codewhisperer:[a-z0-9-]+:\d{12}:profile/[A-Za-z0-9]+\Z", re.ASCII
)
_SPIFFE_ID_RE = re.compile(r"\Aspiffe://[a-z0-9._-]+(/[A-Za-z0-9._~%!$&'()*+,;=:@-]+)*\Z")


class WorkloadIdentityConfigError(ValueError):
    """A workload identity setting that cannot be used; the message names it."""


class WorkloadIdentityUnavailable(Exception):
    """No JWT-SVID could be had right now. The message never carries a token."""


@dataclass(frozen=True)
class WorkloadIdentitySettings:
    audience: str
    profile_arn: str
    endpoint: str
    spiffe_id: str = ""

    @classmethod
    def from_env(
        cls, env: Optional[Mapping[str, str]] = None
    ) -> Optional["WorkloadIdentitySettings"]:
        """The settings, ``None`` while the source is off, or a config error."""
        env = os.environ if env is None else env
        mode = (env.get(ENV_MODE) or "").strip().lower()
        if not mode:
            return None
        if mode != MODE_SPIFFE:
            raise WorkloadIdentityConfigError(f"{ENV_MODE} must be empty or {MODE_SPIFFE!r}")
        audience = (env.get(ENV_AUDIENCE) or "").strip()
        if not audience.startswith("https://") or any(ch.isspace() for ch in audience):
            raise WorkloadIdentityConfigError(f"{ENV_AUDIENCE} must be an https:// audience")
        profile_arn = (env.get(ENV_PROFILE_ARN) or "").strip()
        if not _PROFILE_ARN_RE.match(profile_arn):
            raise WorkloadIdentityConfigError(f"{ENV_PROFILE_ARN} must be a Kiro profile ARN")
        spiffe_id = (env.get(ENV_SPIFFE_ID) or "").strip()
        if spiffe_id and not _SPIFFE_ID_RE.match(spiffe_id):
            raise WorkloadIdentityConfigError(f"{ENV_SPIFFE_ID} must be a spiffe:// ID")
        endpoint = (env.get(ENV_SOCKET) or env.get(SPIFFE_ENDPOINT_SOCKET) or "").strip()
        if not endpoint:
            raise WorkloadIdentityConfigError(
                f"no Workload API socket: set {SPIFFE_ENDPOINT_SOCKET} or {ENV_SOCKET}"
            )
        return cls(
            audience=audience,
            profile_arn=profile_arn,
            endpoint=endpoint,
            spiffe_id=spiffe_id,
        )


def enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the deployment asked for the workload identity (even if misconfigured)."""
    env = os.environ if env is None else env
    return bool((env.get(ENV_MODE) or "").strip())


def _describe(svid: JwtSvid) -> str:
    lifetime = svid.lifetime
    return (
        f"sub={svid.spiffe_id} aud={','.join(svid.audience)} iss={svid.issuer or '-'} "
        f"lifetime={int(lifetime) if lifetime is not None else '-'}s "
        f"remaining={int(svid.remaining())}s"
    )


#: The newest SVID per settings. A race only costs a second fetch, so no lock.
_cache: dict[WorkloadIdentitySettings, JwtSvid] = {}
_logged_subjects: set[str] = set()


def _fresh_enough(svid: JwtSvid, now: float) -> bool:
    lifetime = svid.lifetime or 0.0
    return svid.remaining(now) > max(REFRESH_MARGIN_SECONDS, lifetime / 2)


async def current_svid(
    settings: WorkloadIdentitySettings, *, now: Optional[float] = None
) -> JwtSvid:
    """A JWT-SVID with comfortable lifetime left, fetched when the cached one is not."""
    stamp = time.time() if now is None else now
    held = _cache.get(settings)
    if held is not None and _fresh_enough(held, stamp):
        return held
    try:
        svid = await fetch_jwt_svid(
            settings.endpoint, settings.audience, spiffe_id=settings.spiffe_id
        )
    except WorkloadApiError as exc:
        logger.warning("workload identity: no JWT-SVID (%s)", exc)
        raise WorkloadIdentityUnavailable(str(exc)) from None
    _cache[settings] = svid
    level = logging.DEBUG if svid.spiffe_id in _logged_subjects else logging.INFO
    _logged_subjects.add(svid.spiffe_id)
    logger.log(
        level,
        "workload identity: obtained JWT-SVID %s",
        _describe(svid),
    )
    return svid


def current_svid_blocking(settings: WorkloadIdentitySettings) -> JwtSvid:
    """:func:`current_svid` for a worker thread that has no event loop of its own."""
    return asyncio.run(current_svid(settings))


async def kas_access_token_answer(settings: WorkloadIdentitySettings) -> dict:
    """The ``_kiro/auth/getAccessToken`` answer: the SVID, its expiry, the profile ARN.

    No ``authMethod``: ``external_idp`` would make the engine send a ``TokenType``
    header, and the bearer check only recognizes an SVID on the default path.
    """
    svid = await current_svid(settings)
    return {
        "accessToken": svid.token,
        "expiresAt": datetime.fromtimestamp(svid.expires_at, timezone.utc).isoformat(),
        "profileArn": settings.profile_arn,
        "provider": KAS_PROVIDER,
    }


def read_lease_credential() -> str:
    """The SVID for the relay lease call. Blocking: the lease client calls it in a thread.

    Raises :class:`WorkloadIdentityConfigError` when the source is off or
    misconfigured, which reading again cannot fix, and
    :class:`WorkloadIdentityUnavailable` when the agent has no SVID right now.
    """
    settings = WorkloadIdentitySettings.from_env()
    if settings is None:
        raise WorkloadIdentityConfigError(
            f"tunnel.relay_lease_credential is {LEASE_CREDENTIAL_REF} but {ENV_MODE} is not set"
        )
    return current_svid_blocking(settings).token


def seam_enabled(*, fallback: bool) -> bool:
    """Whether the platform's agent-identity seam reports a workload identity.

    The one predicate every workload site reads: the composed ``agent_identity``
    is this module's :class:`SpiffeAgentIdentityProvider` and reports
    ``enabled()``. An edition's own enabled provider does not reroute these
    sites to a SPIFFE sign-in. *fallback* is the answer when the context cannot
    be read; each caller names the safe one.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    def _spiffe_enabled() -> bool:
        provider = current_context().agent_identity
        return isinstance(provider, SpiffeAgentIdentityProvider) and provider.enabled()

    return bool(
        safe_context_call(
            _spiffe_enabled,
            fallback=fallback,
            log_message="agent_identity.enabled lookup failed",
        )
    )


class SpiffeAgentIdentityProvider:
    """The agent-identity seam on a host whose workload identity is a SPIFFE JWT-SVID.

    Composed by the public bootstrap when ``KIROCREW_WORKLOAD_IDENTITY`` is set.
    Only :meth:`enabled`, :meth:`workload_identity` and
    :meth:`vend_workload_access_token` differ from the disabled default; the
    Gateway methods answer as it does, so nothing else switches on.
    """

    def enabled(self) -> bool:
        return True

    def workload_identity(self) -> "PlatformWorkloadIdentity | None":
        from kiro_crew.platform.interfaces import WorkloadIdentity as PlatformWorkloadIdentity

        try:
            settings = WorkloadIdentitySettings.from_env()
        except WorkloadIdentityConfigError:
            return None
        held = _cache.get(settings) if settings is not None else None
        if held is None:
            return None
        # The SVID names the workload; it carries no ARN, and none is invented.
        return PlatformWorkloadIdentity(name=held.spiffe_id, arn="")

    def status(self) -> dict:
        return {}

    def gateway_mcp_spec(self) -> "dict | None":
        return None

    async def annotate_principal(self, principal: "SessionPrincipal") -> "SessionPrincipal":
        return principal

    async def vend_workload_access_token(self, principal: "SessionPrincipal") -> "str | None":
        settings = WorkloadIdentitySettings.from_env()
        if settings is None:
            return None
        return (await current_svid(settings)).token

    async def vend_gateway_inbound_token(self, principal: "SessionPrincipal") -> None:
        return None


def _reset_cache_for_tests() -> None:
    _cache.clear()
    _logged_subjects.clear()
