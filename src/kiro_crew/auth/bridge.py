"""KAS integration seam.

Where the auth module meets the embedded KAS engine. KAS consumes auth one of two ways
(see docs/system-specs/modules/kas-auth.md); this module exposes both against a single
``KasAuthProvider``:

- ``handle_get_access_token(...)`` — answers the ``_kiro/auth/getAccessToken``
  acp-callback KAS raises when it is driven over ACP.
- ``as_iauthprovider(...)`` — a dict of async callables shaped like KAS's
  ``IAuthProvider`` (getToken / getProfileArn / isAuthenticated / readToken /
  resolveRequestCredential), for the in-process library injection path
  (``KiroAgentOptions.authProvider``).

The embedded-KAS runtime does not exist in this tree yet; this is the stable seam it
will bind to, kept real and tested so wiring KAS later is a call, not a rewrite.
"""

from __future__ import annotations

import logging

from kiro_crew.auth.provider import KasAuthProvider

logger = logging.getLogger(__name__)


async def handle_get_access_token(provider: KasAuthProvider) -> dict:
    """Answer a ``_kiro/auth/getAccessToken`` acp-callback (empty request body)."""
    return await provider.get_access_token_callback()


def as_iauthprovider(provider: KasAuthProvider) -> dict:
    """Return an IAuthProvider-shaped mapping for KAS library injection.

    Keys mirror the TypeScript ``IAuthProvider`` method names KAS calls. The bridge that
    embeds KAS adapts this mapping to the JS object KAS expects; keeping it a plain dict
    here means the auth module has no build-time dependency on the KAS runtime.
    """
    return {
        "getToken": provider.get_token,
        "getProfileArn": provider.get_profile_arn,
        "isAuthenticated": provider.is_authenticated,
        "readToken": provider.read_token,
        "resolveRequestCredential": provider.resolve_request_credential,
    }
