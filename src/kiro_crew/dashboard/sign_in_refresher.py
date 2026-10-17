"""Gateway tick that renews the stored Kiro sign-in before it expires.

:func:`kiro_crew.auth.refresh.ensure_fresh` renews a stored Crew sign-in. Its
other production caller, :meth:`kiro_crew.auth.provider.KasAuthProvider.current`,
runs only when the engine's ``_kiro/auth/getAccessToken`` callback asks for a
credential, so on its own it leaves the stored expiry that the sign-in card and
the doctor command read unchanged while no agent turn runs, and leaves a grant
the issuer would refuse unreported until a chat turn fails.

:func:`run_sign_in_refresher` renews it from inside the gateway instead. It sleeps
until the highest-priority stored identity enters the refresh margin
(``REFRESH_MARGIN_SECS``) and then calls the same ``ensure_fresh`` the callback
uses, so the two cannot disagree: the per-identity asyncio lock and the
cross-process flock already serialize them, and whichever runs second re-reads
the store and skips its HTTP call. A refusal is recorded by ``ensure_fresh`` itself
(:meth:`TokenStore.mark_refresh_rejected`), which is exactly what the card and
``kirocrew doctor`` already read.

What the tick deliberately does NOT do:

- It never refreshes a token outside the margin, so it adds no refresh traffic
  the lazy path would not have made on the next turn.
- It does not retry a refused grant: while the rejected marker stands it waits for
  a new sign-in (any save clears the marker) instead of asking the issuer again.
- It does not change which auth owner a spawn gets; that is still
  :func:`kiro_crew.auth.bridge.vault_holds_identity`.

Imports from :mod:`kiro_crew.auth` are deferred until a vault file exists, for the
same reason :mod:`kiro_crew.acp.kas_host_auth` defers them: the auth subsystem
pulls the ``cryptography`` wheel and must load on first use, not at gateway boot.
A gateway nobody ever signed in on pays one ``stat`` per idle interval.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

#: Longest the tick sleeps between looks at the store. Bounds how late a sign-in
#: that lands while the tick sleeps is first examined; a fresh sign-in is far from
#: its margin, so this costs nothing but one vault read per interval.
IDLE_INTERVAL_SECS = 300.0

#: Shortest sleep, so a clock or rounding edge cannot spin the loop.
MIN_INTERVAL_SECS = 5.0

#: First retry delay after a transient refresh failure (issuer unreachable or
#: erroring); doubles per consecutive failure up to :data:`IDLE_INTERVAL_SECS`.
RETRY_BASE_SECS = 30.0

#: Seconds past the margin boundary the tick wakes, so ``ensure_fresh`` -- which
#: renews only a token already inside the margin -- sees it as due.
_WAKE_SLACK_SECS = 1.0

#: Total time one renewal request may take. Kept under
#: :data:`SHUTDOWN_DRAIN_SECS` so a gateway stop can wait for an in-flight
#: renewal to finish and persist the replacement token before the process exits.
REQUEST_TIMEOUT_SECS = 8.0

#: How long gateway shutdown waits for the tick to finish an in-flight renewal.
#: Runs alongside the gateway's own graceful shutdown and stays inside its
#: ``GRACEFUL_SHUTDOWN_SECS`` budget.
SHUTDOWN_DRAIN_SECS = 9.0


def _vault_file(home: Path) -> Path:
    """The KAS vault's ciphertext file (``auth/store.TokenStore`` layout)."""
    return home / "kas" / ".vault" / "secrets.enc"


async def refresh_once(home: Path, *, failures: int = 0) -> tuple[float, int]:
    """Look at the stored sign-in once; refresh it if it is due.

    Returns ``(seconds_to_sleep, consecutive_failures)``. Never raises except
    ``asyncio.CancelledError``: a failure is logged without any token value and
    turned into a retry delay, so one bad tick cannot end the loop.
    """
    if not await asyncio.to_thread(_vault_file(home).exists):
        return IDLE_INTERVAL_SECS, 0

    # Deferred: see the module docstring (boot-path and cryptography gates).
    import aiohttp

    from kiro_crew.auth.refresh import IdentitySignedOut, RefreshRejected, ensure_fresh
    from kiro_crew.auth.store import REFRESH_MARGIN_SECS, TokenStore, TokenStoreError

    store = TokenStore(home)
    try:
        token = await asyncio.to_thread(store.resolve)
    except TokenStoreError as exc:
        # A path or permissions verdict, never a secret.
        logger.warning("sign-in refresher: vault unreadable: %s", exc)
        return IDLE_INTERVAL_SECS, 0
    if token is None:
        return IDLE_INTERVAL_SECS, 0

    due_in = (
        token.expires_at.timestamp() - datetime.now(timezone.utc).timestamp() - REFRESH_MARGIN_SECS
    )
    if due_in > 0:
        return min(max(due_in + _WAKE_SLACK_SECS, MIN_INTERVAL_SECS), IDLE_INTERVAL_SECS), 0

    if token.refresh_blocker():
        # Nothing to renew it with (no refresh token, or missing client
        # credentials); only a new sign-in changes that.
        return IDLE_INTERVAL_SECS, 0
    if await asyncio.to_thread(store.refresh_rejected, token.identity) is not None:
        # The issuer already refused this grant and the user has been told; asking
        # again cannot succeed. A new sign-in clears the marker.
        return IDLE_INTERVAL_SECS, 0

    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            renewed = await ensure_fresh(store, token, session=session)
    except asyncio.CancelledError:
        raise
    except IdentitySignedOut:
        return IDLE_INTERVAL_SECS, 0
    except RefreshRejected:
        # ensure_fresh recorded the marker; the card and doctor now say so.
        logger.warning("sign-in refresher: the issuer refused the %s refresh", token.identity)
        return IDLE_INTERVAL_SECS, 0
    except Exception as exc:  # noqa: BLE001
        # Type only: a refresh error's message can carry issuer response bytes.
        return _retry(token.identity, type(exc).__name__, failures)
    if renewed.is_expired():
        # An issuer that hands back a lifetime shorter than the margin would
        # otherwise be asked again every MIN_INTERVAL_SECS; back off instead.
        return _retry(token.identity, "renewed token already inside the margin", failures)
    logger.debug("sign-in refresher: renewed %s sign-in", token.identity)
    # Re-read on the next pass to schedule against the renewed expiry.
    return MIN_INTERVAL_SECS, 0


def _retry(identity: str, reason: str, failures: int) -> tuple[float, int]:
    """Exponential retry delay after a failed renewal, logged without any token value."""
    failures += 1
    delay = min(RETRY_BASE_SECS * (2 ** (failures - 1)), IDLE_INTERVAL_SECS)
    logger.info(
        "sign-in refresher: %s refresh failed (%s); retrying in %.0fs", identity, reason, delay
    )
    return delay, failures


class _ShutdownSignal(Protocol):
    """What the loop reads from the gateway's shutdown event."""

    def is_set(self) -> bool: ...

    async def wait(self) -> bool: ...


async def run_sign_in_refresher(shutdown_event: _ShutdownSignal) -> None:
    """Keep the stored sign-in renewed until ``shutdown_event`` is set."""
    failures = 0
    while not shutdown_event.is_set():
        try:
            delay, failures = await refresh_once(data_home(), failures=failures)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # refresh_once already contains its own failures; this is the last
            # guard so an unexpected bug degrades to the old lazy-only behaviour
            # instead of an unhandled task exception.
            logger.warning("sign-in refresher: unexpected error", exc_info=True)
            delay, failures = IDLE_INTERVAL_SECS, 0
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            continue


async def drain_sign_in_refresher(
    task: asyncio.Task[None] | None, *, timeout: float = SHUTDOWN_DRAIN_SECS
) -> None:
    """Wait for the tick to stop once the shutdown event is set.

    An idle tick returns at once. A tick in the middle of a renewal finishes it,
    so a rotated refresh token is persisted before the gateway's hard exit
    instead of being consumed at the issuer and lost. Bounded by ``timeout``;
    never raises except ``asyncio.CancelledError``.
    """
    if task is None or task.done():
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "sign-in refresher: renewal still in flight after %.0fs at shutdown", timeout
        )
    except asyncio.CancelledError:
        if task.cancelled():
            return
        raise
    except Exception:  # noqa: BLE001
        logger.debug("sign-in refresher: ended with an error at shutdown", exc_info=True)
