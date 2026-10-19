"""The request path over the warm table: one activation becomes rows, and a Connect adopts one.

:func:`warm_mint_all` is what ``POST /api/connections/premint`` runs. It claims the rows
(``shared_rows``), activates the shared process once, moves the popped challenges into the table
(:func:`_absorb_warm_requests`) and releases every claim the activation did not fill. The claim is
taken before the ``try`` that rolls it back, and every await until the claim settles runs inside
that ``try``, so a cancellation releases the whole claim set. :func:`adopt_shared_mint` is what
``POST /api/connections/mint`` tries before it reserves a row of its own: it takes ownership of an
unclaimed premint, rotating its token and its watcher together, and on a miss re-drains the newest
live shared session that activated the provider (:func:`_recover_redrained_requests`).

Every URL is screened for an embedded credential before it reaches the table, by the facade's
``_credential_bearing_slugs``. That screen, the runtime singleton, the row-liveness verdict, the
candidate scan, the activation and the audit event are read through the facade at call time
(:func:`_warm_facade`) because the facade defines them; the grant watcher and the absorb step are
read there because tests substitute them on the facade.
"""

from __future__ import annotations

import asyncio
import logging
from types import ModuleType

from kiro_crew.connections.mint import _mints, _mints_lock, _new_mint_token
from kiro_crew.connections.registry import Provider
from kiro_crew.connections.warm_runtime.mint_process import _WarmMintResult
from kiro_crew.connections.warm_runtime.shared_rows import (
    _activations_in_use,
    _claim_shared_mints,
    _dispose_displaced_rows,
    _release_shared_claims,
)
from kiro_crew.mcp_utils import mcp_server_alias

logger = logging.getLogger("kiro_crew.connections.warm")


def _warm_facade() -> ModuleType:
    """The warm facade, resolved per call because it holds the names this module reads late.

    Imported here rather than at module scope because the facade imports this module: a
    top-level import would make a cold import of either module circular.
    """
    from kiro_crew.connections import warm  # circular import: the facade composes this module

    return warm


async def _adopt_shared_row(slug: str, mcp_url: str) -> str | None:
    """Take ownership of ``slug``'s UNCLAIMED premint. Returns its new row token, or None.

    THE handoff, and the reason the premint has a consumer at all. Without it Connect
    would call ``reserve_mint_row`` unconditionally, which pops WHATEVER row is at the
    slug -- so ``start_oauth_mint`` disposes the very URL the warm table minted for that
    click, and the cold spawn it then pays is the only thing the user sees. ``None`` means
    no row was adoptable at this instant; the public flow may recover a late frame from the
    existing live session before it falls through to the dedicated cold path.

    FOUR refusals, each closing a different way to hand back a URL that cannot work: no
    row; a row that is not an unclaimed premint (``shared`` absent -- a cold flow's or
    an already-adopted one, whose owner's token fences it); a claim still ``minting``,
    which holds nothing to give; and a row whose holder is gone, judged by
    :func:`_warm_row_alive` -- the SAME generation-and-activation pair the reaper uses,
    because a URL is redeemable only while the process holds its verifier AND the
    session still answers its redirect.

    ATOMIC BY CONSTRUCTION, like :func:`_claim_shared_mints`: everything between the
    read and the last write is synchronous, so two clicks racing one row serialize on
    ``_mints_lock`` and the loser observes ``shared`` already cleared. Nothing is
    disposed here, so the engine's dispose-outside-the-table-lock rule is not merely
    respected but unreachable.

    THE TOKEN IS ROTATED, and the watcher with it. A fresh token is what fences the
    adopting tab against the premint's own rollback and against a sibling tab, but
    every write in :func:`~kiro_crew.connections.mint._mint_watcher` is guarded on the
    token it was started with -- so rotating alone would leave the row watched by a
    task that cannot touch it: nothing would flip it to ``granted``, nothing
    would expire it, and it would hold the shared process resident for good. Re-arming
    is therefore part of the same synchronous run, not a follow-up.

    PROVENANCE IS KEPT. ``generation``/``activation`` stay exactly as the activation
    stamped them, because ownership moved and the verifier did not: the row is still
    judged, parked and withdrawn by the warm table (:func:`_warm_table_row`).
    """
    adopted: str | None = None
    async with _mints_lock:
        entry = _mints.get(slug)
        if (
            entry is not None
            and entry.get("shared")
            and entry.get("state") == "waiting"
            and entry.get("oauth_url")
            and _warm_facade()._warm_row_alive(entry)
        ):
            adopted = _new_mint_token()
            stale = entry.pop("watcher", None)
            if stale is not None:
                stale.cancel()
            entry["token"] = adopted
            entry.pop("shared", None)
            entry["watcher"] = asyncio.get_running_loop().create_task(
                _warm_facade()._mint_watcher(slug, mcp_url, adopted)
            )
    if adopted is not None:
        # Off the loop: the FIRST ``sel()`` of a process constructs the log, and the warm
        # module pins every such hop with a drift guard.
        await asyncio.to_thread(
            _warm_facade()._log_warm_event, "connections_warm_mint_adopt", f"provider:{slug}"
        )
    return adopted


async def adopt_shared_mint(slug: str, mcp_url: str) -> str | None:
    """Adopt a warm row, recovering late frames from the live shared session once."""
    adopted = await _adopt_shared_row(slug, mcp_url)
    if adopted is not None:
        return adopted
    try:
        redrained = await _warm_facade()._warm_mint.redrain_for(slug)
        if redrained is not None:
            await _recover_redrained_requests(redrained)
    except Exception:  # noqa: BLE001 -- a dedicated cold mint remains the fallback
        logger.debug("warm mint adoption re-drain failed", exc_info=True)
    return await _adopt_shared_row(slug, mcp_url)


async def _absorb_warm_requests(result: _WarmMintResult, claims: dict[str, str]) -> list[str]:
    """Move popped challenges into the mint table. Returns the slugs now waiting."""
    popped = {
        str(request.get("serverName") or ""): str(request.get("oauthUrl") or "")
        for request in result.requests
    }
    # An activation names its challenges by the alias the SPEC mounted; the table is keyed by
    # registry slug. Resolved once, here, so the screen and the store judge the same string.
    resolved = {
        slug: popped.get(mcp_server_alias(slug)) or popped.get(slug) or ""
        for slug in (provider["slug"] for provider in result.providers)
    }
    minted: list[str] = []
    unfulfilled: dict[str, str] = {}
    tainted: set[str] = set()
    try:
        # Screened BEFORE the table lock: the verdict is a pure function of the URL, and the
        # gate can read the operator's endpoint extension off disk. A refused URL never
        # reaches the store, so no card can be handed one.
        tainted = await asyncio.to_thread(_warm_facade()._credential_bearing_slugs, resolved)
        loop = asyncio.get_running_loop()
        async with _mints_lock:
            for provider in result.providers:
                slug = provider["slug"]
                token = claims.get(slug, "")
                entry = _mints.get(slug)
                if entry is None or not token or entry.get("token") != token:
                    # Superseded while the activation ran. Its challenge stays alive in the
                    # session that produced it and nothing points at it; the sweep collects
                    # that session.
                    continue
                url = resolved.get(slug, "")
                if not url or slug in tainted:
                    # Released either way, not failed: the card asks for a fresh mint rather
                    # than sitting on a claim nothing will ever fill. For a tainted slug
                    # that fresh (cold) mint is also where the user learns WHICH endpoint
                    # was refused -- mint.py names it on its rejection card -- so the
                    # release, not a failed row, is what makes the warm refusal actionable.
                    unfulfilled[slug] = token
                    continue
                entry.update(
                    {
                        "state": "waiting",
                        "oauth_url": url,
                        "generation": result.generation,
                        "activation": result.activation,
                        "watcher": loop.create_task(
                            _warm_facade()._mint_watcher(slug, provider["mcp_url"], token)
                        ),
                    }
                )
                minted.append(slug)
    finally:
        # Settlement on EVERY post-activation exit, which is why it is a ``finally``. The
        # session was registered by ``_activate_locked`` before this function ran, and the
        # sweep only ever collects a SETTLED session -- so a raise anywhere above (the
        # screen's thread hop is a cancellation point, and the gate itself can fail on an
        # unreadable file) would leave the session, and the loopback callback servers it
        # owns, resident for the life of the process. The in-use snapshot is taken HERE,
        # under the lock, so it reflects whatever the loop above actually installed rather
        # than what it intended to.
        async with _mints_lock:
            in_use = _activations_in_use()
        await _warm_facade()._warm_mint.settle_activation(result.activation, in_use)
    for slug in tainted:
        logger.warning("Shared mint for %r produced a URL with a credential pattern", slug)
        await asyncio.to_thread(
            _warm_facade()._log_warm_event,
            "connections_warm_mint_url",
            f"provider:{slug}",
            "refused",
        )
    if unfulfilled:
        await _release_shared_claims(unfulfilled)
    return minted


def _providers_with_requests(result: _WarmMintResult) -> list[Provider]:
    names = {
        str(request.get("serverName") or "")
        for request in result.requests
        if request.get("oauthUrl")
    }
    return [
        provider
        for provider in result.providers
        if provider["slug"] in names or mcp_server_alias(provider["slug"]) in names
    ]


async def _recover_redrained_requests(result: _WarmMintResult) -> list[str]:
    """Install attributable late frames through the ordinary warm-table fences."""
    providers = _providers_with_requests(result)
    if not providers:
        return []
    claims, displaced = await _claim_shared_mints([provider["slug"] for provider in providers])
    if not claims:
        return []
    try:
        await _dispose_displaced_rows(displaced)
        narrowed = _WarmMintResult(
            generation=result.generation,
            activation=result.activation,
            providers=providers,
            requests=result.requests,
        )
        minted = await _warm_facade()._absorb_warm_requests(narrowed, claims)
    except BaseException:
        await _release_shared_claims(claims)
        raise
    if minted:
        logger.info("Shared mint re-drain recovered late row(s): %s", ", ".join(sorted(minted)))
    return minted


async def warm_mint_all(
    providers: list[Provider] | None = None, *, arm_supervision: bool = True
) -> list[str]:
    """Warm every mintable provider's approval URL in ONE activation.

    Returns the slugs now holding a URL. Never raises for a mint failure -- that leaves the
    cards exactly as they were, asking for a mint. CANCELLATION does propagate, after the
    claims are released.

    The claim is taken BEFORE the process is ensured, because ensuring and activating are
    one locked step (see ``mint_for``) and nothing may run between them. ``providers``
    therefore only decides which rows are CLAIMED; what gets activated is the snapshot the
    lock holder computes, so a claim the activation did not cover is released rather than
    left minting forever.

    ``POST /api/connections/premint`` passes the candidates it scanned, so an explicit
    ``providers`` is the ordinary call shape; ``None`` re-scans for a caller that has no
    list of its own. ``arm_supervision`` is false only for the automatic replacement: the
    replacement inherits the page-owned lifecycle without recursively earning another retry.
    """
    if providers is None:
        candidates, audit_recorded = await asyncio.to_thread(
            _warm_facade()._audited_mintable_providers
        )
        if candidates and not audit_recorded:
            logger.warning(
                "grant-presence audit for the implicit warm scan could not be recorded; "
                "proceeding unaudited"
            )
    else:
        candidates = list(providers)
    if not candidates:
        return []
    if arm_supervision:
        _warm_facade()._warm_mint.arm_supervision()

    claims, displaced = await _claim_shared_mints([provider["slug"] for provider in candidates])
    if not claims:
        # A displaced row is only ever recorded alongside the claim that displaced it, so an
        # empty claim set means there is nothing to dispose either.
        return []
    try:
        # Inside the protected region on purpose: this is the awaiting half of the claim, so
        # a cancellation here must roll the whole claim set back rather than strand it.
        await _dispose_displaced_rows(displaced)
        result = await _warm_facade()._warm_activate()
        if result is None:
            await _release_shared_claims(claims)
            return []

        minted = await _warm_facade()._absorb_warm_requests(result, claims)
        activated = {provider["slug"] for provider in result.providers}
        stranded = {slug: token for slug, token in claims.items() if slug not in activated}
        if stranded:
            await _release_shared_claims(stranded)
    except BaseException:
        # BaseException, not Exception: a CancelledError landing between the claim and the
        # activation would otherwise leave every row ``minting`` with no watcher and no
        # activation. ``expire_dead_mints`` judges ``waiting`` rows only, so nothing would
        # ever withdraw them -- and they keep ``_shared_mints_pending`` true, so the reaper
        # never retires the process either. The release awaits to completion: a task is
        # handed its cancellation once, so this cleanup runs before the raise resumes it.
        await _release_shared_claims(claims)
        raise
    await asyncio.to_thread(
        _warm_facade()._log_warm_event,
        "connections_warm_mint",
        f"activated:{len(claims)} minted:{len(minted)}",
    )
    logger.info("Shared mint activation warmed %d of %d card(s)", len(minted), len(claims))
    return minted
