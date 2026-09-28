"""The shared rows of the mint table: who owns each one, and the claim that installs them.

TWO AXES, and conflating them is what the handoff had to separate. ``shared`` is OWNERSHIP: an
unclaimed premint any Connect may adopt, and the only mark a row still ``minting`` carries.
``generation``/``activation`` is PROVENANCE: the verifier lives in the shared process, so
``warm._warm_row_alive`` judges redeemability however the row is owned. Adoption clears the first
and keeps the second, so every warm-side predicate keys on :func:`_warm_table_row` -- the
disjunction -- and the cold engine's ``_mint_holder_alive`` ABSTAINS on a row carrying a
``generation`` rather than reading its absent ``client`` as death.

IDENTITY: a row is fenced by its own opaque ``token``, never by the batch clock reading in
``started``. ``time.monotonic()`` has ~15.6ms granularity on Windows, so two Connects for
one provider inside a tick read as one row and a late absorb overwrites the newer claim --
the same reasoning
:func:`~kiro_crew.connections.mint._new_mint_token` records for the cold engine. WITHDRAWAL
is the other axis and does NOT use the token: a row is expired because the process that
holds its verifier is gone, so ``warm._expire_shared_mints`` narrows by ``generation`` only.

ATOMICITY: :func:`_claim_shared_mints` contains no await, so a caller either holds every
claim it asked for or none -- the claim is taken before ``warm.warm_mint_all`` enters the
``try`` that rolls it back, which makes any await in that loop an unprotected cancel window.
The rows a claim displaced come back to the caller and are disposed inside that ``try``
(:func:`_dispose_displaced_rows`), and the rollback itself is :func:`_release_shared_claims`.

The table is the cold engine's: ``_mints`` and ``_mints_lock`` are bound from
:mod:`kiro_crew.connections.mint` at import exactly as the facade binds them, so both read one
table object. Disposing a row's holdings goes through the facade's ``_dispose_mint`` binding
(:func:`_warm_facade`), which is the seam the warm engine's own callers and tests address.
"""

from __future__ import annotations

import time
from types import ModuleType

from kiro_crew.connections.mint import MintState, _mints, _mints_lock, _new_mint_token


def _warm_facade() -> ModuleType:
    """The warm facade, resolved per call because it owns the patchable ``_dispose_mint``.

    Imported here rather than at module scope because the facade imports this module: a
    top-level import would make a cold import of either module circular.
    """
    from kiro_crew.connections import warm  # circular import: the facade composes this module

    return warm


#: The row states a shared mint is still working on. ``minting`` counts: a claim with no
#: URL yet is exactly what a cancelled activation must not leave behind.
_LIVE_STATES = ("minting", "waiting")


def _warm_table_row(entry: MintState) -> bool:
    """True when the SHARED table owns this row's lifecycle -- claimed, or warm-minted.

    TWO disjuncts, and both are load-bearing, because ``shared`` and ``generation``
    answer different questions and :func:`~kiro_crew.connections.warm.adopt_shared_mint` moves only the first.

    ``shared`` is OWNERSHIP: an unclaimed premint any Connect may adopt. It is also the
    ONLY mark a row still ``minting`` carries, which is precisely the row a cancelled
    activation must not leave behind -- so a generation-only test would stop counting it.

    ``generation`` is PROVENANCE: the PKCE verifier lives in the shared process, so
    redeemability is judged by :func:`~kiro_crew.connections.warm._warm_row_alive` no matter who owns the row.
    Adoption clears ``shared`` and keeps this, so an ownership-only test drops the
    adopted row out of every count here -- and the counts are what keep its process
    parked and its session held. The reaper would then retire the process holding the
    URL the user is part-way through redeeming, which is the worst outcome available on
    this path.
    """
    return bool(entry.get("shared")) or bool(entry.get("generation"))


def _live_row_count(generation: int) -> int:
    """How many cards are still mid-consent on ``generation``."""
    return sum(
        1
        for entry in _mints.values()
        if _warm_table_row(entry)
        and entry.get("generation") == generation
        and entry.get("state") in _LIVE_STATES
    )


def _generation_holds_live_rows(generation: int) -> bool:
    """True while killing ``generation`` would strand a redeemable code."""
    return _live_row_count(generation) > 0


def _activations_in_use() -> set[int]:
    """Activation ids a live shared row still points at -- the sweep's keep-set."""
    return {
        int(entry["activation"])
        for entry in _mints.values()
        if _warm_table_row(entry) and entry.get("activation") and entry.get("state") in _LIVE_STATES
    }


def _shared_mints_pending() -> bool:
    """True while any card still needs the shared process alive."""
    return any(
        _warm_table_row(entry) and entry.get("state") in _LIVE_STATES for entry in _mints.values()
    )


def _mint_is_cold_held(entry: MintState | None) -> bool:
    """True when a dedicated client -- not the shared process -- holds this URL."""
    return entry is not None and entry.get("state") == "waiting" and entry.get("client") is not None


def _mint_is_adopted(entry: MintState | None) -> bool:
    """True when a caller has taken ownership of a WARM row, so it is nobody's to reclaim.

    :func:`_mint_is_cold_held` cannot answer this and never could: an adopted row owns
    no ``client`` either -- its verifier is in the shared process -- so the cold test
    reads it as free and the claim loop below replaces a URL the user is part-way
    through redeeming. ``shared`` is the whole distinction: it is set while the premint
    is unclaimed and cleared by :func:`~kiro_crew.connections.warm.adopt_shared_mint`.
    """
    return (
        entry is not None
        and entry.get("state") == "waiting"
        and bool(entry.get("generation"))
        and not entry.get("shared")
    )


async def _claim_shared_mints(slugs: list[str]) -> tuple[dict[str, str], list[MintState]]:
    """Claim ``slugs`` for the shared process. Returns ``({slug: row token}, displaced rows)``.

    The token is the row's OWN identity and it is what every later step fences on. A batch
    ``time.monotonic()`` reading cannot do that job: it has ~15.6ms granularity on Windows,
    so two Connects for one provider inside a single tick read as the same row and a late
    absorb writes its URL over the newer claim (see ``_new_mint_token``, which records the
    same reasoning for the cold engine).

    ATOMIC BY CONSTRUCTION: the loop contains NO await, so the caller either gets every
    claim or none. Awaiting ``_dispose_mint`` on each replaced row would suspend
    on a client teardown and again on the shielded spec removal in that function's
    ``finally`` -- and the claim is taken BEFORE ``warm_mint_all`` enters the try that rolls
    it back, so a cancellation there would leave earlier slugs installed as ``minting`` with no
    caller holding their tokens. Nothing withdraws such a row (``expire_dead_mints`` judges
    ``waiting`` only) and it keeps ``_shared_mints_pending`` true, so the process is never
    retired either. The replaced rows come back for the caller to dispose INSIDE that try
    instead -- which also puts the dispose outside the table lock, where the mint engine's
    own rule wants it.
    """
    claimed: dict[str, str] = {}
    displaced: list[MintState] = []
    started = time.monotonic()
    async with _mints_lock:
        for slug in slugs:
            prior = _mints.get(slug)
            if _mint_is_cold_held(prior) or _mint_is_adopted(prior):
                # A CALLER owns this provider's URL -- a dedicated client holds its
                # verifier, or a Connect adopted the warm row this table minted. Leave
                # its URL on the card rather than replace a working link.
                continue
            if prior is not None:
                # Hand it back, don't just drop it: the replaced row may own a watcher, and
                # a watcher outliving its row expires the NEW mint on the OLD mint's
                # deadline. Recorded only alongside the claim that displaced it, so a
                # non-empty list always implies a non-empty claim set.
                displaced.append(prior)
            token = _new_mint_token()
            _mints[slug] = {
                "state": "minting",
                # Informational only -- when the claim was taken. Never a fence.
                "started": started,
                "shared": True,
                "token": token,
            }
            claimed[slug] = token
    return claimed, displaced


async def _dispose_displaced_rows(rows: list[MintState]) -> None:
    """Release the holdings of the rows a claim replaced -- watcher, client, PID, spec.

    Split out of the claim itself because it awaits: see ``_claim_shared_mints``. Called
    from inside the caller's protected region, so a cancellation here rolls the claims back
    rather than stranding them.
    """
    for row in rows:
        await _warm_facade()._dispose_mint(row)


async def _release_shared_claims(claims: dict[str, str]) -> None:
    """Drop unfulfilled claims so the card asks for a fresh mint.

    Keyed on the row token, so a claim already superseded by a newer one at the same slug
    is left alone rather than dropped out from under the activation now filling it.
    """
    async with _mints_lock:
        for slug, token in claims.items():
            entry = _mints.get(slug)
            if entry is not None and entry.get("token") == token and entry.get("shared"):
                await _warm_facade()._dispose_mint(entry)
                _mints.pop(slug, None)
