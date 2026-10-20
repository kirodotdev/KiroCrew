"""Automatic re-arms: the shared process after it dies, one provider after its grant is invalidated.

An explicit page warm arms ONE automatic replacement (``warm._WarmMintRuntime.arm_supervision``);
on a death ``start_rearm`` reserves the dead generation's single-flight task, which runs
:func:`_rearm_dead_warm_mint`: the ordinary tri-state candidate scan, then ``warm_mint_all`` with
``arm_supervision`` false, so the replacement cannot earn itself another attempt. An invalidated stored grant re-arms its one provider through
:func:`rearm_invalidated_provider`, fire-and-forget and single-flight per slug. The registry
:data:`_invalidation_rearms` holds the only strong reference to each such task, and
:func:`_settle_invalidation_rearms` cancels and awaits them at teardown, before the processes are
retired.

The candidate scan, ``warm_mint_all`` and the scheduled coroutine are read through the facade at
call time (:func:`_warm_facade`): the facade defines the scan, and tests substitute the other two
there. The registry is re-exported there as the same dict.
"""

from __future__ import annotations

import asyncio
import logging
from types import ModuleType

from kiro_crew.connections.mint import _mints, _mints_lock
from kiro_crew.connections.warm_runtime.shared_rows import _LIVE_STATES

logger = logging.getLogger("kiro_crew.connections.warm")


def _warm_facade() -> ModuleType:
    """The warm facade, resolved per call because it holds the names this module reads late.

    Imported here rather than at module scope because the facade imports this module: a
    top-level import would make a cold import of either module circular.
    """
    from kiro_crew.connections import warm  # circular import: the facade composes this module

    return warm


async def _rearm_dead_warm_mint(generation: int) -> list[str]:
    """Re-warm the still-unconnected eligible roster after one process death.

    Candidate selection reuses the ordinary tri-state grant scan: only a confirmed absence
    initiates consent, while a present or unreadable grant is skipped. ``arm_supervision`` is
    false so this replacement cannot recursively earn another automatic replacement.
    """
    try:
        candidates, audit_recorded = await asyncio.to_thread(
            _warm_facade()._audited_mintable_providers
        )
    except Exception:  # noqa: BLE001 -- the cold path remains available to every Connect
        logger.debug("warm mint re-arm candidate scan failed", exc_info=True)
        return []
    if not candidates:
        logger.info(
            "Shared mint generation %d died; no confirmed-unconnected providers need re-arm",
            generation,
        )
        return []
    if not audit_recorded:
        logger.warning(
            "grant-presence audit for the automatic re-arm scan could not be recorded; "
            "proceeding unaudited"
        )
    logger.warning(
        "Shared mint generation %d died; re-arming %d eligible provider(s)",
        generation,
        len(candidates),
    )
    return await _warm_facade().warm_mint_all(candidates, arm_supervision=False)


#: One in-flight invalidation re-arm per provider slug, and the only strong reference to each
#: task: nothing awaits them, so without this the loop's own weak reference lets a re-arm be
#: collected mid-activation. Keyed by slug so a second invalidation of the SAME provider is a
#: no-op while its re-arm runs, and two different providers never serialize behind each other.
_invalidation_rearms: dict[str, asyncio.Task] = {}


def _forget_invalidation_rearm(slug: str, task: asyncio.Task) -> None:
    """Drop ``task``'s registration, unless a newer re-arm has already taken its place."""
    if _invalidation_rearms.get(slug) is task:
        del _invalidation_rearms[slug]


async def _settle_invalidation_rearms() -> None:
    """Cancel and await every in-flight invalidation re-arm. Called only from teardown.

    The same settlement :meth:`_WarmMintRuntime.shutdown` gives its own supervision re-arm --
    cancel, then ``gather`` with ``return_exceptions=True`` so the child's ``CancelledError`` is
    consumed here instead of surfacing out of teardown. These tasks need it MORE, not less:
    nothing awaits them in the ordinary course, so a Disconnect landing moments before teardown
    otherwise leaves a task unsettled at loop close -- reported as ``Task was destroyed but it is
    pending`` -- with its activation half-way through minting.

    Settled BEFORE the processes are retired (see :func:`shutdown_warm_mint`). A re-arm cancelled
    first rolls its claims back through ``warm_mint_all``'s own protected region; retiring first
    would leave an activation already past that point free to spawn a replacement process after
    teardown had finished, which is the leak this settlement exists to prevent, one layer down.

    A task that already completed is left alone -- its result and its own done callback's
    bookkeeping are untouched -- and the registry is emptied either way, because once teardown
    has run no re-arm is in flight by definition.
    """
    current = asyncio.current_task()
    pending = [
        task
        for task in list(_invalidation_rearms.values())
        if task is not current and not task.done()
    ]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _invalidation_rearms.clear()


async def _rearm_invalidated_provider(slug: str) -> list[str]:
    """Restore a ready premint for ``slug`` after its stored grant was invalidated.

    An invalidated grant is what makes a provider warmable again, and nothing else was asking:
    the pool premints ahead of need, so a provider that held a grant was deliberately skipped
    and stayed skipped once that grant went away. The user's next Authorize then paid the full
    cold mint -- measured at ~23s against a warm sub-second -- for a card the pool could have
    had ready.

    ELIGIBILITY IS NOT RE-DERIVED HERE. The ordinary tri-state candidate scan is the authority,
    so this inherits every veto arming already has: a disabled entry, a provider with no usable
    auth configuration (no DCR and no pre-registered client id), a configured entry asking for
    something different from the registry, a grant that is still PRESENT because the revoke was
    refused or failed, and a grant cache that could not be read at all -- an absence nobody
    confirmed must not initiate consent. A provider the scan does not return is therefore
    skipped rather than warmed into a mint that would itself fail cold.

    A live row is left alone. Re-arming a provider already ``minting`` or ``waiting`` would
    displace a URL the card is showing and the user may be part-way through redeeming, which
    is the opposite of the readiness this exists to provide.

    ``arm_supervision`` is false for the same reason the death-driven re-arm sets it false:
    this replacement inherits whatever lifecycle the page already armed instead of recursively
    earning another automatic one.
    """
    try:
        candidates, audit_recorded = await asyncio.to_thread(
            _warm_facade()._audited_mintable_providers
        )
    except Exception:  # noqa: BLE001 -- the cold path remains available to every Connect
        logger.debug("warm mint invalidation re-arm candidate scan failed", exc_info=True)
        return []
    provider = next((item for item in candidates if item["slug"] == slug), None)
    if provider is None:
        logger.debug("Not re-arming %s after invalidation: it is not a warm candidate", slug)
        return []
    if not audit_recorded:
        logger.warning(
            "grant-presence audit for the invalidation re-arm scan could not be recorded; "
            "proceeding unaudited"
        )
    async with _mints_lock:
        prior = _mints.get(slug)
        if prior is not None and prior.get("state") in _LIVE_STATES:
            logger.debug("Not re-arming %s after invalidation: a live row already holds it", slug)
            return []
    logger.info("Re-arming the premint for %s after its stored grant was invalidated", slug)
    return await _warm_facade().warm_mint_all([provider], arm_supervision=False)


def rearm_invalidated_provider(slug: str) -> None:
    """Schedule a premint re-arm for ``slug``, without waiting for one.

    SYNCHRONOUS AND FIRE-AND-FORGET, because the callers are the invalidation paths
    themselves: a Disconnect must answer the user as soon as its own transaction lands, and an
    activation spawns a helper process and negotiates OAuth, which is seconds of work no
    invalidation may hang on. Scheduling is all this does -- whether a re-arm is warranted at
    all is :func:`_rearm_invalidated_provider`'s judgement, made on the pool's own scan.

    Off the loop -- a synchronous caller with no running loop -- there is nothing to schedule
    onto and nothing to warm for: the next activation's scan picks the provider up.
    """
    running = _invalidation_rearms.get(slug)
    if running is not None and not running.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("no running loop for the %s invalidation re-arm; leaving it to the scan", slug)
        return
    task = loop.create_task(_warm_facade()._rearm_invalidated_provider(slug))
    _invalidation_rearms[slug] = task
    task.add_done_callback(lambda done: _forget_invalidation_rearm(slug, done))
