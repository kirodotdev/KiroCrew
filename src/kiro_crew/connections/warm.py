"""The shared warm-mint TABLE: which shared rows still hold a redeemable URL.

The cold path (:mod:`kiro_crew.connections.mint`) pays a full kiro-cli spawn PER provider
for one approval URL. The warm design serves every card's URL from ONE process instead, and
splits in two: the TABLE (this module) and the LIFECYCLE that fills it.

This slice ships the TABLE half only -- the row shape a shared mint uses (``shared`` /
``generation`` / ``activation`` on :class:`~kiro_crew.connections.mint.MintState`), the
liveness registry those stamps are read against (:class:`_WarmMintRuntime`), and the
withdrawal chokepoint the dashboard's status path calls (:func:`expire_dead_mints`).
Nothing here spawns, activates, parks or kills a process.

Redeemability takes TWO questions and they die independently: the PKCE verifier lives in
the PROCESS (``generation_is_live``) while the loopback listener answering the redirect
belongs to the SESSION (``activation_is_live``). Process liveness alone passed a
terminated-session row, which is how a card kept serving an unredeemable URL. Both
failures are recorded in ``docs/architecture/design-notes/connections-warm-table.md``.

DEFERRED to slice N2b: the entire lifecycle -- spec planning, spawn, activation, parking,
the reaper, and ``warm_mint_all``. Until it lands nothing sets ``shared`` on a row, so
:func:`expire_dead_mints` is a no-op scan and the registry below stays empty. Both are
written to answer correctly the moment N2b starts filling them, parked generations
included: a reader blind to a parked process withdraws a code that process can still
redeem, so completing the predicate later would mean revisiting this decision under a
live bug.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.connections.mint import MintState, _dispose_mint, _mints, _mints_lock

logger = logging.getLogger(__name__)


def _runtime_alive(runtime: Any) -> bool:
    """Liveness of one warm process. Never raises into a mint."""
    if runtime is None:
        return False
    try:
        return bool(runtime.is_alive())
    except Exception:  # noqa: BLE001 — liveness must never raise into a mint
        logger.debug("warm mint liveness check failed", exc_info=True)
        return False


class _WarmMintRuntime:
    """The liveness registry a shared row's ``generation``/``activation`` are read against.

    Both containers are filled by the deferred lifecycle (slice N2b) and are empty until it
    lands. They are READ here rather than in N2b because the reader is what decides whether
    a card's URL is withdrawn, and the parked case is exactly the one where a wrong answer
    destroys a code the user could still redeem.
    """

    def __init__(self) -> None:
        self._runtime: Any = None
        #: Bumped on every spawn. Rows record the generation that minted them, letting a
        #: stand-down tell "nothing needs this" from "killing it strands a user mid-consent".
        self._generation = 0
        #: Generations kept alive ONLY because a card still holds one of their URLs.
        self._retiring: list[tuple[int, Any]] = []
        #: Live sessions by activation id -- each owns the loopback servers for its
        #: challenges, so one is held while a card points at one of its URLs.
        self._sessions: dict[int, Any] = {}

    def is_alive(self) -> bool:
        return _runtime_alive(self._runtime)

    def generation_is_live(self, generation: int) -> bool:
        """True while the process that minted ``generation`` can still redeem."""
        if generation <= 0:
            return False
        if generation == self._generation:
            return self.is_alive()
        return any(
            parked == generation and _runtime_alive(runtime) for parked, runtime in self._retiring
        )

    def activation_is_live(self, activation: int) -> bool:
        """True while the SESSION that minted ``activation`` still listens."""
        if activation <= 0:
            return False
        return activation in self._sessions


_warm_mint = _WarmMintRuntime()


def _warm_row_alive(entry: MintState) -> bool:
    """Whether a SHARED row's URL can still actually be redeemed.

    Two things must be alive and they die independently: the PKCE verifier in the PROCESS,
    and the loopback listener in the SESSION. Process liveness alone passed a
    terminated-session row, which is how a card kept serving an unredeemable URL -- which
    is also why the cold engine's ``_mint_holder_alive`` is deliberately NOT reused: it
    reads the row's own ``client``, which a shared row does not own.
    """
    if not _warm_mint.generation_is_live(int(entry.get("generation") or 0)):
        return False
    return _warm_mint.activation_is_live(int(entry.get("activation") or 0))


async def expire_dead_mints() -> list[str]:
    """Withdraw every shared row whose holding process is gone. THE chokepoint."""
    doomed: list[str] = []
    async with _mints_lock:
        for slug, entry in _mints.items():
            if not entry.get("shared") or entry.get("state") != "waiting":
                continue
            if _warm_row_alive(entry):
                continue
            entry["state"] = "expired"
            entry["reason"] = "mint_process_gone"
            await _dispose_mint(entry)
            doomed.append(slug)
    if doomed:
        logger.info("Withdrew %d approval URL(s) whose minting process is gone", len(doomed))
    return doomed
