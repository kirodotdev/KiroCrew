"""A Settings MCP sign-in on the KAS backend, kept in the owner's grant vault.

The kiro-cli mint (:mod:`kiro_crew.connections.mint`) leaves its grant in
kiro-cli's own OAuth cache, which a KAS engine never reads. On KAS the same
Sign in click instead runs the sign-in through a KAS engine process:

- The process is spawned for a reserved session key bound to the gateway owner
  (:data:`kiro_crew.mcp_grant_principals.OWNER_PRINCIPAL`) before the spawn, so
  its handshake declares ``secretStorage`` and the tokens the engine stores land
  in the owner's per-user grant vault. A
  later owner KAS chat is bound to the same principal and reads the same vault.
  The route is owner-only, which is what makes ``owner`` the right principal.
- Nothing here reads or writes a vault entry. Only the engine does, over the
  request path that store already guards, so no token passes through this
  module and none is copied between stores.
- The sign-in itself is the engine's: the process
  (:class:`kiro_crew.agent_sdk.mcp_sign_in.KasSignInProcess`) asks for it, and the consent URL comes back as the session's OAuth request, which takes
  the same credential gate as the kiro-cli mint before it is published.
- Completion is the engine reporting the server ``connected``, and it counts
  only while the process is still bound to the owner's vault.

It is opt-in through ``agent.kas_persist_mcp_sign_in``. With that off the engine
would keep the grant in the mint process's memory and lose it at dispose, so the
route keeps the kiro-cli mint (:func:`kas_mint_applies`). If the setting turns
off between that check and this flow, the row fails with
``kas_sign_in_not_kept`` and nothing is spawned.

It shares the kiro-cli mint's row table, single-server spec, TTL, cancel and
dead-holder paths: the row's ``client`` is that process, which has
the ``shutdown`` and ``is_process_alive`` those paths call.

INVARIANT, as in the kiro-cli mint: no filesystem operation runs on the event
loop. Config reads, spec writes and data-home resolution go through
``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from kiro_crew import mcp_grant_principals
from kiro_crew.agent_sdk.mcp_sign_in import (
    KasSignInProcess,
    kas_sign_in_kept,
    sign_in_persistence_enabled,
)

# The row table and its lock are read through the module at call time, never
# bound here, so this flow always shares the table the kiro-cli mint writes.
from kiro_crew.connections import mint as _mint
from kiro_crew.connections.mint import (
    MintState,
    _dispose_mint,
    _log_mint_outcome,
    _sweep_mint_specs,
    _write_mint_agent_spec,
    reserve_mint_row,
)
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.runtime_ownership import RuntimeTeardownCommitted, claim_runtime_tenancy
from kiro_crew.security import oauth_url_contains_credential, sanitized_oauth_endpoint_display

logger = logging.getLogger(__name__)

__all__ = ["KAS_MINT_KEY_PREFIX", "STORE_KAS", "kas_mint_applies", "mint_key", "start_kas_mint"]

#: Prefix of the session key a KAS mint process is spawned for. No dashboard
#: slot, channel or cron key takes this shape, so the binding is the mint's own.
KAS_MINT_KEY_PREFIX = "connections-mint-kas-"
#: The row's ``store`` once it is a KAS mint.
STORE_KAS = "kas"


def kas_mint_applies() -> bool:
    """Whether Settings sign-ins go through KAS on this gateway. Blocking.

    True only when owner dashboard chats run on KAS AND
    ``agent.kas_persist_mcp_sign_in`` is on (``agent_sdk.mcp_sign_in``). With
    persistence off a KAS sign-in would die with the mint process, so the button
    keeps the kiro-cli mint it ran before this path existed.
    """
    return kas_sign_in_kept()


def mint_key(slug: str) -> str:
    """The session key a KAS mint for *slug* is spawned and bound for."""
    return KAS_MINT_KEY_PREFIX + slug


async def _set_row(slug: str, token: str, row: MintState) -> bool:
    """Replace *slug*'s row with *row* if it is still this attempt's. True: written."""
    async with _mint._mints_lock:
        if _mint._mints.get(slug, {}).get("token") != token:
            return False
        _mint._mints[slug] = {
            "started": time.monotonic(),
            "token": token,
            "store": STORE_KAS,
            **row,
        }
        return True


async def _fail(slug: str, token: str, reason: str, rejected_endpoint: str | None = None) -> None:
    """Record a terminal failure on this attempt's row and audit it.

    The audit line carries the slug and a reason code only, never a URL.
    """
    row: MintState = {"state": "failed", "reason": reason}
    if rejected_endpoint is not None:
        row["rejected_endpoint"] = rejected_endpoint
    await _set_row(slug, token, row)
    await asyncio.to_thread(_log_mint_outcome, slug, "error", f"reason={reason} store=kas")


async def _wait_reported(process: KasSignInProcess, server: str, deadline: float) -> None:
    """Read frames until *server* is in a report bucket or *deadline* passes."""
    # The mint spawned this runtime and is its only tenant, so its liveness is
    # the mint's own reading (here and in the two loops below).
    while time.monotonic() < deadline and process.is_process_alive():  # pid-owner-ok
        if any(server in names for names in process.buckets().values()):
            return
        await process.read_frames()


async def _consent_url(process: KasSignInProcess, server: str, deadline: float) -> str | None:
    """The consent URL the engine sends for *server*, ``""`` if it connected first.

    ``None`` when neither happened before *deadline*.
    """
    while time.monotonic() < deadline and process.is_process_alive():  # pid-owner-ok
        await process.read_frames()
        url = process.consent_url(server)
        if url:
            return url
        if server in process.buckets()["ready"]:
            return ""
    return None


async def _finish_granted(slug: str, token: str, process: KasSignInProcess, detail: str) -> bool:
    """Mark the row granted only while the grant is kept in the owner's vault."""
    if not process.keeps_grants():
        await _fail(slug, token, "kas_vault_unavailable")
        return False
    if await _set_row(slug, token, {"state": "granted"}):
        await asyncio.to_thread(_log_mint_outcome, slug, "ok", f"reason={detail} store=kas")
    return True


async def _kas_mint_watcher(slug: str, token: str, process: KasSignInProcess, server: str) -> None:
    """Hold the mint until the engine reports *server* connected, or the TTL ends."""
    try:
        deadline = time.monotonic() + _mint._MINT_TTL_SECONDS
        while time.monotonic() < deadline:
            if not process.is_process_alive():  # pid-owner-ok
                # Committed here rather than left to the next mint-state poll,
                # which may never come once the page is closed: the row expires
                # (``mint_process_gone``) and its spec and tenancy are released.
                await _mint.expire_dead_holder(slug)
                return
            await process.read_frames()
            if server not in process.buckets()["ready"]:
                continue
            async with _mint._mints_lock:
                entry = _mint._mints.get(slug)
                done = entry if entry is not None and entry.get("token") == token else None
            if done is not None:
                # Judged while the process still runs: keeps_grants reads its
                # live vault binding. The holdings are released after.
                await _finish_granted(slug, token, process, "signed_in")
                await _dispose_mint(done)
            logger.info("KAS MCP sign-in for %r completed", slug)
            return
        doomed: MintState | None = None
        async with _mint._mints_lock:
            entry = _mint._mints.get(slug)
            if (
                entry is not None
                and entry.get("token") == token
                and entry.get("state") == "waiting"
            ):
                entry["state"] = "expired"
                doomed = entry
        if doomed is not None:
            await _dispose_mint(doomed)
        logger.info("KAS MCP sign-in for %r expired", slug)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 -- the watcher must never take the gateway down
        logger.debug("KAS MCP sign-in watcher failed [%s]", slug, exc_info=True)


async def start_kas_mint(
    slug: str,
    mcp_url: str,
    token: str | None = None,
    prior: MintState | None = None,
    pinned: dict[str, Any] | None = None,
) -> None:
    """Run *slug*'s Settings sign-in through a KAS engine bound to the owner.

    Fire-and-forget with the same contract as ``start_oauth_mint``: never raises,
    and every outcome is a row state the mint feed serves. *mcp_url* is accepted
    for that contract; the engine reads the server from the single-server spec.
    """
    del mcp_url
    if token is None:
        token, prior = await reserve_mint_row(slug)
    if prior is not None:
        await _dispose_mint(prior)
    await asyncio.to_thread(_sweep_mint_specs)

    if not await asyncio.to_thread(sign_in_persistence_enabled):
        await _fail(slug, token, "kas_sign_in_not_kept")
        return

    holdings: MintState = {}
    handed_off = False
    try:
        from kiro_crew.config.loader import data_home

        work_dir = (await asyncio.to_thread(data_home)) / "connections" / "mint"
        agent_name, spec_path = await (
            asyncio.to_thread(_write_mint_agent_spec, slug)
            if pinned is None
            else asyncio.to_thread(_write_mint_agent_spec, slug, pinned)
        )
        holdings["agent"] = agent_name
        holdings["spec_path"] = spec_path
        key = mint_key(slug)
        # Bound BEFORE the spawn: the handshake reads the principal once.
        mcp_grant_principals.bind(key, mcp_grant_principals.OWNER_PRINCIPAL)
        process = KasSignInProcess(work_dir=work_dir, agent=agent_name, grant_key=key)
        holdings["client"] = process
        # One budget over the spawn, the first status report and the consent
        # URL, the same one the kiro-cli mint gives its readiness.
        budget = _mint._MINT_READY_TIMEOUT_SECONDS
        deadline = time.monotonic() + budget
        await asyncio.wait_for(process.start(), timeout=budget)
        try:
            holdings["tenancy"] = (
                claim_runtime_tenancy(process.runtime, holder="connections.mint") or ""
            )
        except RuntimeTeardownCommitted:
            holdings["tenancy"] = ""
        if not process.keeps_grants():
            await _fail(slug, token, "kas_vault_unavailable")
            return

        server = mcp_server_alias(slug)
        await _wait_reported(process, server, deadline)
        buckets = process.buckets()
        if server in buckets["ready"]:
            # The vault already held a grant the engine could use.
            await _finish_granted(slug, token, process, "validated_grant")
            return
        if server not in buckets["awaiting_auth"]:
            await _fail(slug, token, "mint_server_absent")
            return
        if not process.begin(server):
            await _fail(slug, token, "mint_sign_in_busy")
            return
        oauth_url = await _consent_url(process, server, deadline)
        if oauth_url is None:
            raise asyncio.TimeoutError
        if not oauth_url:
            await _finish_granted(slug, token, process, "signed_in")
            return
        if await asyncio.to_thread(oauth_url_contains_credential, oauth_url):
            logger.warning("KAS MCP sign-in for %r produced a URL with a credential pattern", slug)
            endpoint = await asyncio.to_thread(sanitized_oauth_endpoint_display, oauth_url)
            await _fail(slug, token, "mint_url_rejected", endpoint)
            return

        async with _mint._mints_lock:
            entry = _mint._mints.get(slug)
            if entry is None or entry.get("token") != token:
                superseded = True
            else:
                superseded = False
                entry.update(holdings)
                entry.update(
                    {
                        "state": "waiting",
                        "oauth_url": oauth_url,
                        "store": STORE_KAS,
                        "watcher": asyncio.get_running_loop().create_task(
                            _kas_mint_watcher(slug, token, process, server)
                        ),
                    }
                )
                holdings = {}
                handed_off = True
        outcome = "reason=superseded" if superseded else "url_minted=True"
        await asyncio.to_thread(_log_mint_outcome, slug, "ok", f"{outcome} store=kas")
    except Exception as exc:  # noqa: BLE001 -- background task; record, never raise
        logger.warning("KAS MCP sign-in for %r failed: %s", slug, type(exc).__name__)
        await _dispose_mint(holdings)
        holdings = {}
        if not handed_off:
            # Once the waiting row owns the process, its watcher, Cancel and the
            # TTL release it; a late failure here must not orphan that row.
            await _fail(slug, token, "mint_" + type(exc).__name__.lower()[:32])
    finally:
        if holdings:
            await _dispose_mint(holdings)
