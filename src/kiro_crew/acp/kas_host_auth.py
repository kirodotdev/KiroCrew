"""Crew as the KAS engine's auth owner: vault probe and callback answer.

The relay described in :mod:`kiro_crew.acp.kas_transport` has two auth owners.
This module is the runtime's seam to the one where Crew owns the credential:

- :func:`vault_holds_identity_off_loop` decides, at spawn time, whether the
  relay is started without ``--auth-method cli`` -- true only when Crew's own
  vault (:mod:`kiro_crew.auth.store`) holds a usable identity. It is
  :func:`kiro_crew.auth.bridge.vault_holds_identity` on a worker thread: a plain
  read of the vault, no refresh and no network, that never raises -- a vault that
  cannot be read is reported as "no identity", which keeps the spawn on the
  cli-owned path a signed-out operator already has.
- :func:`answer_get_access_token` renders the response for the engine's
  ``_kiro/auth/getAccessToken`` request from
  :class:`kiro_crew.auth.provider.KasAuthProvider` (resolve + refresh under the
  cross-process lock, refresh token withheld). Failures are mapped to a
  token-free category string the runtime can both log and return.

Both are thin over :mod:`kiro_crew.auth.bridge`, the auth module's own KAS seam;
this module exists so the ACP runtime has one import for "Crew-owned auth" and so
each half is testable without a live process.

Why the ``kiro_crew.auth`` imports below are deferred to call time: this module
is imported by :mod:`kiro_crew.acp.runtime`, which is on the gateway's boot path
and on the tool-approval hook's import chain. ``kiro_crew.auth`` pulls the
``cryptography`` native wheel (the vault is AES-GCM), and two gates forbid that on
those chains -- ``test_approval_chain_no_cryptography.py`` (a platform-mismatched
wheel must break WeCom media, not every tool approval) and
``test_kas_login_api.py::test_handlers_package_does_not_import_kas_at_boot`` (the
auth subsystem loads on first use, not at boot). A KAS spawn is that first use.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class HostAuthCallbackError(Exception):
    """The callback could not be answered. ``str(exc)`` is token-free by construction."""


async def vault_holds_identity_off_loop() -> bool:
    """:func:`kiro_crew.auth.bridge.vault_holds_identity` on a worker thread.

    The probe is blocking file IO with a never-raises contract (a broken vault
    reads as "no identity", so the spawn degrades to cli-owned); this is the
    shape the async spawn path calls it in.
    """
    # Deferred: see the module docstring (boot-path and cryptography gates).
    from kiro_crew.auth.bridge import vault_holds_identity

    return await asyncio.to_thread(vault_holds_identity)


async def answer_get_access_token() -> dict:
    """Build the ``_kiro/auth/getAccessToken`` response from Crew's vault.

    Returns the covenant shape (``accessToken``, ``expiresAt``, optional
    ``profileArn`` / ``provider`` / ``authMethod``); the refresh token is never
    part of it. Raises :class:`HostAuthCallbackError` with a token-free message
    when no credential is stored or the refresh fails, so the runtime can return
    the same string to the engine as a JSON-RPC error.
    """
    # Deferred: see the module docstring (boot-path and cryptography gates).
    from kiro_crew.auth.bridge import default_token_store, handle_get_access_token
    from kiro_crew.auth.provider import KasAuthProvider, NotAuthenticated
    from kiro_crew.auth.store import TokenStoreError

    try:
        # Vault only: an ambient KIRO_API_KEY in the gateway's environment must not
        # stand in for an identity the operator signed out of (and the engine would
        # send it with the wrong token type anyway -- it is not an OIDC bearer).
        provider = KasAuthProvider(default_token_store(), allow_env_api_key=False)
        return await handle_get_access_token(provider)
    except NotAuthenticated as exc:
        # The engine renders this string in its sign-in prompt; keep it short.
        raise HostAuthCallbackError("not signed in to Kiro Crew") from exc
    except TokenStoreError as exc:
        # Detail (a path, never a secret) stays in the local log; the engine gets
        # only the category.
        logger.warning("KAS auth callback: sign-in vault error: %s", exc)
        raise HostAuthCallbackError("sign-in vault error") from exc
    except Exception as exc:  # noqa: BLE001
        # An unexpected exception's message could carry response bytes from a
        # refresh endpoint; expose only its type, and only in the local log.
        logger.warning("KAS auth callback: refresh failed (%s)", type(exc).__name__)
        raise HostAuthCallbackError("refresh failed") from exc
