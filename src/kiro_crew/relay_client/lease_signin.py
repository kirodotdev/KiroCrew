"""The lease credential of a personal Crew: the Crew's own Kiro sign-in.

A personal Crew's host leases with its owner's own sign-in, IAM Identity Center or
an organization's identity provider, not a key. :func:`read_lease_sign_in` reads
that sign-in through :class:`~kiro_crew.auth.provider.KasAuthProvider`, the path
the relay viewer uses, so the token is refreshed when it nears its end, and once
more when the lease service refused it (*rejected*). The refreshed token is saved
only where the sign-in already keeps it, the Crew's sign-in vault; nothing here
writes a file or keeps a copy.

Only an IAM Identity Center or external identity provider sign-in that names a
Kiro profile can own a personal Crew, so anything else is refused here, before a
request is sent. Every failure is a :class:`LeaseCredentialError`, which the lease
client reads again on its backoff steps: signing in again resumes hosting without
a restart. No message carries a token, a refresh token or the profile ARN.

The Crew's sign-in looks up Kiro profiles only on the production control plane,
so a sign-in made against a pre-production one stores no profile and is refused
here until the sign-in can name its control plane.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from kiro_crew.relay_client.lease import TOKEN_TYPE_EXTERNAL_IDP, LeaseCredentialError

#: The stored identity kinds that can own a personal Crew.
PERSONAL_OWNER_IDENTITIES = frozenset({"identity_center", "external_idp"})


@dataclass(frozen=True, repr=False)
class LeaseSignIn:
    """One read of the sign-in: what a lease call carries. Never printed."""

    bearer: str
    #: The ``TokenType`` header: ``None`` for IAM Identity Center, ``EXTERNAL_IDP``
    #: for a token from an outside identity provider.
    token_type: Optional[str]
    profile_arn: str

    def __repr__(self) -> str:
        return "LeaseSignIn(...)"


def _default_provider() -> Any:
    from kiro_crew.auth.bridge import default_token_store
    from kiro_crew.auth.provider import KasAuthProvider

    # allow_env_api_key=False: an ambient KIRO_API_KEY must never stand in for the
    # owner's sign-in.
    return KasAuthProvider(default_token_store(), allow_env_api_key=False)


async def read_lease_sign_in(
    rejected: Optional[str] = None, *, provider: Optional[Any] = None
) -> LeaseSignIn:
    """The Crew's sign-in for a lease call, refreshed as needed.

    *rejected* is a bearer the lease service just answered HTTP 401: if the store
    still holds it, it is refreshed now. Raises :class:`LeaseCredentialError`.
    """
    from kiro_crew.auth.provider import NotAuthenticated
    from kiro_crew.auth.refresh import IdentitySignedOut, RefreshBlocked, RefreshRejected

    if provider is None:
        provider = _default_provider()
    try:
        token = await provider.current(rejected_access_token=rejected)
    except (NotAuthenticated, IdentitySignedOut, RefreshRejected, RefreshBlocked):
        raise LeaseCredentialError("the Crew is not signed in to Kiro") from None
    except Exception as exc:  # noqa: BLE001 - a transient refresh failure; type only
        raise LeaseCredentialError(
            f"the Kiro sign-in could not be refreshed ({type(exc).__name__})"
        ) from None
    if token.identity not in PERSONAL_OWNER_IDENTITIES:
        raise LeaseCredentialError(
            "the Crew's Kiro sign-in can't host a personal Crew; sign in with IAM "
            "Identity Center or your organization's identity provider, and sign out of "
            "Builder ID or any social sign-in on this Crew"
        )
    # The stored profile itself, never kas_profile_arn's fixed Builder ID value.
    profile_arn = token.profile_arn or ""
    if not profile_arn.strip():
        raise LeaseCredentialError("the Crew's Kiro sign-in names no Kiro profile")
    bearer = (token.access_token or "").strip()
    if not bearer or not all(33 <= ord(ch) < 127 for ch in bearer):
        raise LeaseCredentialError("the Kiro sign-in's token must be one line of printable ASCII")
    # As the relay viewer does: the header follows the sign-in's auth method.
    token_type = TOKEN_TYPE_EXTERNAL_IDP if token.auth_method == "external_idp" else None
    return LeaseSignIn(bearer=bearer, token_type=token_type, profile_arn=profile_arn.strip())
