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
  token-free category string the runtime can both log and return. On a
  workload-identity host it answers from the Crew's JWT-SVID
  (:mod:`kiro_crew.workload_identity`) and the vault is never read.

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
import hashlib
import logging
import time

logger = logging.getLogger(__name__)

#: Delays before the second and third try of a refresh that failed on the
#: network (DNS, connect, a dropped or timed-out request). A refused grant is
#: never retried: only a new sign-in fixes it.
REFRESH_RETRY_DELAYS_SECS: tuple[float, ...] = (0.5, 1.5)

#: No retry starts once this long has passed since the first try, so a
#: black-holed route (each try may take the full ``refresh.HTTP_TIMEOUT``) still
#: answers the engine instead of holding its callback open.
REFRESH_RETRY_WINDOW_SECS = 10.0


def _is_transient_refresh_error(exc: BaseException) -> bool:
    """A failure that says nothing about the grant: the issuer was not reached, or
    its answer did not come back. aiohttp's DNS and connect errors are both
    ``ClientConnectionError``; ``ServerTimeoutError`` is also a timeout."""
    import aiohttp  # deferred with the rest of the refresh stack (module docstring)

    return isinstance(exc, (aiohttp.ClientConnectionError, asyncio.TimeoutError))


class HostAuthCallbackError(Exception):
    """The callback could not be answered. ``str(exc)`` is token-free by construction."""


def token_digest(access_token: str) -> str:
    """A one-way tag for an access token, so a runtime can tell which one it was
    served without holding the token itself."""
    return hashlib.sha256(access_token.encode("utf-8")).hexdigest()


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

    On a workload-identity host (:mod:`kiro_crew.workload_identity`) the answer is
    the Crew's JWT-SVID, and the vault is not read.
    """
    from kiro_crew import workload_identity

    if workload_identity.active():
        try:
            return await asyncio.to_thread(workload_identity.kas_access_token_answer)
        except workload_identity.WorkloadIdentityError as exc:
            # The message names the cause, never the token.
            logger.warning("KAS auth callback: workload identity: %s", exc)
            raise HostAuthCallbackError("workload identity unavailable") from exc
    # Deferred: see the module docstring (boot-path and cryptography gates).
    from kiro_crew.auth.bridge import default_token_store, handle_get_access_token
    from kiro_crew.auth.provider import KasAuthProvider, NotAuthenticated
    from kiro_crew.auth.store import TokenStoreError

    try:
        # Vault only: an ambient KIRO_API_KEY in the gateway's environment must not
        # stand in for an identity the operator signed out of (and the engine would
        # send it with the wrong token type anyway -- it is not an OIDC bearer).
        provider = KasAuthProvider(default_token_store(), allow_env_api_key=False)
        started = time.monotonic()
        for delay in (*REFRESH_RETRY_DELAYS_SECS, None):
            try:
                return await handle_get_access_token(provider)
            except Exception as exc:  # noqa: BLE001 - only network failures retry
                if (
                    delay is None
                    or not _is_transient_refresh_error(exc)
                    # The next try must START inside the window, after the backoff.
                    or time.monotonic() - started + delay >= REFRESH_RETRY_WINDOW_SECS
                ):
                    raise
                # Type only: see the last handler below.
                logger.info(
                    "KAS auth callback: refresh failed (%s); retrying in %.1fs",
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable: the last try raises or returns")
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


async def sign_in_refused(served_digest: str | None) -> bool:
    """Whether Crew's sign-in is refused, renewing a token the engine may hold.

    Asked when a KAS host on the Crew-owned route fail-closed its governance
    read. The engine caches the token it was served until that token's own
    expiry and never asks again when Kiro refuses it, and the callback serves
    the vault's token for as long as the vault thinks it is live, so a token
    Kiro refuses early reaches every new runtime until it expires. When the
    vault still holds the token this runtime was served (*served_digest*, from
    :func:`token_digest`), it is renewed now (``rejected_access_token``), so
    the retried start draws a new one; a peer that already renewed it makes
    this a plain read. Under the refresh lock, so many runtimes asking at once
    make one refresh.

    True only on proof that a new sign-in is needed: nothing stored, the issuer
    refused the renewal, or the renewal cannot be attempted and the token is
    already past its margin. A network failure, or any other error, is False:
    that start keeps its retryable error. So is a workload-identity host: no
    person sign-in can fix it, and the vault is never renewed there.
    """
    from kiro_crew import workload_identity

    if workload_identity.active():
        return False
    # Deferred: see the module docstring (boot-path and cryptography gates).
    from kiro_crew.auth.bridge import default_token_store
    from kiro_crew.auth.provider import KasAuthProvider, NotAuthenticated
    from kiro_crew.auth.refresh import RefreshBlocked, RefreshRejected

    store = default_token_store()
    try:
        stored = await asyncio.to_thread(store.resolve)
        if stored is None:
            return True
        served = served_digest is not None and token_digest(stored.access_token) == served_digest
        provider = KasAuthProvider(store, allow_env_api_key=False)
        try:
            await provider.current(rejected_access_token=stored.access_token if served else None)
        except RefreshBlocked:
            # Checked locally, no issuer asked: it says nothing about whether Kiro
            # took the token. A sign-in is needed only once the token itself is
            # past its margin; a live one keeps the retryable error.
            return stored.is_expired()
    except (NotAuthenticated, RefreshRejected):
        return True
    except Exception as exc:  # noqa: BLE001
        # Type only, as in answer_get_access_token.
        logger.warning("KAS sign-in check: renewal failed (%s)", type(exc).__name__)
        return False
    return False
