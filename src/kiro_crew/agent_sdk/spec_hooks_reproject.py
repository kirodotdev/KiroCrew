"""Re-projecting a stale KAS session batch at a turn's claim.

kiro-cli runs an agent spec's PreToolUse hooks on every session; on KAS Crew runs
them in its place. A KAS session keeps the agent batch it registered, which
withholds auto-approval only for what the hooks covered then, so a hook added
since would never see a call the session auto-approves. A dashboard chat, a
sub-agent and a task step reset such a stale session at claim time
(:func:`reproject_claimed_session`, under the lease). Cron and channel turns do
the same through :func:`reproject_unattended_claim`, which cannot rely on a held
lease: a hard Stop can pop the session and let another message claim a successor
while the staleness probe runs, so the reset is fenced to the session the caller
captured at its acquisition (:class:`ClaimToken`) and refuses rather than
resetting that successor.

These live here, not in :mod:`kiro_crew.agent_sdk.spec_hooks`, only to keep that
module under its line cap; they are re-exported from it for the modules that
import them there. The spec-reading seams they need
(:func:`~kiro_crew.agent_sdk.spec_hooks.hook_projection_stale`,
:class:`~kiro_crew.agent_sdk.spec_hooks.StaleProjectionError`,
``_MAX_REPROJECTIONS``) are imported lazily, so this module has no import-time
dependency on its parent and the two load in either order.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

logger = logging.getLogger("kiro_crew.agent_sdk.spec_hooks")

#: A fence *expect* that resets unconditionally, distinct from the ``None`` entry
#: a fenced reset reads as "no live session".
_NO_FENCE: Any = object()


class ClaimToken(NamedTuple):
    """What a caller captures at its acquisition, before any await, and threads
    into :func:`reproject_unattended_claim` so the helper never re-reads the live
    session itself.

    *entry* is the session-manager entry a fenced ``reset(expect_session=...)``
    compares against; *stop_gen* is the user-Stop count at that moment, the
    baseline the caller abandons on. Captured at the claim, BEFORE any renderer
    setup or other await, so a Stop and a successor claim landing in that window
    cannot slip past either check.
    """

    entry: Any
    stop_gen: int


def live_session_entry(sessions: Any, session_key: str) -> Any:
    """The entry ``reset(expect_session=...)`` compares against, or ``None``.

    Read through the manager's own registry and key folding; a session stand-in
    without them answers ``None``, and the caller resets unfenced, as before.
    """
    registry = getattr(sessions, "_sessions", None)
    if not isinstance(registry, dict):
        return None
    fold = getattr(sessions, "_fold_key", None)
    key = fold(session_key) if callable(fold) else session_key
    try:
        return registry.get(key)
    except Exception:  # noqa: BLE001 - an unreadable registry means reset unfenced
        return None


def _claim_stop_generation(sessions: Any, session_key: str) -> int:
    """The user-Stop count for *session_key*, read defensively (0 when unknown)."""
    reader = getattr(sessions, "stop_generation", None)
    if not callable(reader):
        return 0
    try:
        value = reader(session_key)
    except Exception:  # noqa: BLE001 - a stand-in predating the counter reads as 0
        return 0
    return value if isinstance(value, int) else 0


def make_claim_token(sessions: Any, session_key: str) -> ClaimToken:
    """Capture *session_key*'s claim token: its live entry and Stop count now.

    Minted by the acquisition primitives (:func:`acquire_with_token` and the cron
    model-fallback acquire) the instant the claim returns, before any await, so
    the entry and the Stop baseline are this turn's own and not a successor's. A
    caller never builds one itself; it receives the token the acquisition returns.
    """
    return ClaimToken(
        live_session_entry(sessions, session_key),
        _claim_stop_generation(sessions, session_key),
    )


async def acquire_with_token(
    sessions: Any, session_key: str, **kwargs: Any
) -> tuple[tuple[Any, bool, bool], ClaimToken]:
    """``sessions.get_or_create`` with the claim token minted the instant it returns.

    No await runs between the claim and the mint, so a later await in the caller
    cannot swap in a successor the token would then name. Every channel caller
    acquires through this, and cron through its model-fallback acquire, so no
    caller mints a token itself.
    """
    got = await sessions.get_or_create(session_key, **kwargs)
    return got, make_claim_token(sessions, session_key)


async def _reproject(
    sessions: Any,
    session_key: str,
    agent_id: str,
    claimed: tuple[Any, bool, bool],
    claim: Callable[[], Awaitable[tuple[Any, bool, bool]]],
) -> tuple[tuple[Any, bool, bool], bool]:
    """*claimed*, reset and claimed again while stale; and whether it still is.

    The lease-held path (:func:`reproject_claimed_session`): no fence is needed
    because the caller holds the session across the whole call.
    """
    from kiro_crew.agent_sdk.spec_hooks import _MAX_REPROJECTIONS, hook_projection_stale

    for _ in range(_MAX_REPROJECTIONS):
        if not await hook_projection_stale(claimed[0], agent_id):
            return claimed, False
        logger.info(
            "agent %r: the claimed session auto-approves a capability a PreToolUse "
            "hook now covers; re-projecting it before the turn runs",
            agent_id,
        )
        await sessions.reset(session_key)
        claimed = await claim()
        # The reset recorded this task as the holder of the permit it popped; this
        # claim's permit is the one its release is for (a claim that allocated has
        # already said so, and saying it again changes nothing).
        adopt = getattr(sessions, "adopt_turn", None)
        if callable(adopt):
            adopt(session_key)
    return claimed, await hook_projection_stale(claimed[0], agent_id)


async def reproject_claimed_session(
    sessions: Any,
    session_key: str,
    agent_id: str,
    claimed: tuple[Any, bool, bool],
    claim: Callable[[], Awaitable[tuple[Any, bool, bool]]],
) -> tuple[Any, bool, bool]:
    """The claim to run the turn on, re-projected when the claimed session is stale.

    Called with the lease held, so no other turn can change what the session
    runs between this check and the tools. A session that went stale while this
    turn waited for it (the pre-claim reset was declined because another turn
    held it) is reset here, under the lease, and *claim* registers a fresh batch.
    Raises :class:`StaleProjectionError` when the session is still stale after
    :data:`_MAX_REPROJECTIONS` resets, rather than running the turn on a batch
    that would auto-approve a call a hook covers.
    """
    from kiro_crew.agent_sdk.spec_hooks import StaleProjectionError

    claimed, stale = await _reproject(sessions, session_key, agent_id, claimed, claim)
    if stale:
        raise StaleProjectionError(
            f"agent {agent_id!r}: the session could not be re-projected for the "
            "PreToolUse hooks now in place; the turn was not run"
        )
    return claimed


class UnattendedClaim(NamedTuple):
    """:func:`reproject_unattended_claim`'s answer."""

    claimed: tuple[Any, bool, bool]
    #: The replay gap was opened (a reset was attempted); the caller closes it
    #: after its release.
    reprojected: bool
    #: Still stale after :data:`_MAX_REPROJECTIONS` resets: the caller keeps its
    #: stale-batch fallback, it does not refuse the turn.
    still_stale: bool
    #: A hard Stop replaced the claimed session with a successor another turn
    #: owns during the staleness probe: nothing was reset or re-claimed, and the
    #: caller must abandon this turn rather than run it.
    superseded: bool = False


async def reproject_unattended_claim(
    sessions: Any,
    session_key: str,
    agent_id: str,
    claimed: tuple[Any, bool, bool],
    claim: Callable[[], Awaitable[tuple[tuple[Any, bool, bool], Any]]],
    token: ClaimToken,
) -> UnattendedClaim:
    """A cron or channel claim, re-projected when its KAS batch is stale.

    *token* is the caller's :class:`ClaimToken`, captured at its acquisition
    before any await. Each reset is fenced to the session that token names, so a
    hard Stop that popped it and let another message claim a successor while the
    caller's renderer setup or probe ran cannot be reset by this turn: the fenced
    reset refuses and this returns :attr:`UnattendedClaim.superseded`, and the
    caller abandons without re-claiming. This helper never reads the live session
    itself; *claim* returns ``(claimed_tuple, fresh_entry)`` so the caller hands
    back the re-claimed entry for the next fence.

    The replay gap opens before the first reset, so a Stop while the key has no
    session is still recorded and every other claim on the key waits for this
    turn (``SessionManager.open_replay_gap``); the caller closes it after its
    release. A reset or claim that raises closes the gap here.
    """
    from kiro_crew.agent_sdk.spec_hooks import _MAX_REPROJECTIONS, hook_projection_stale

    opened = False
    superseded = False
    still_stale = False
    expect = token.entry if token.entry is not None else _NO_FENCE

    def _open_gap() -> None:
        # Probed: a session stand-in may predate the gap.
        nonlocal opened
        opener = getattr(sessions, "open_replay_gap", None)
        if callable(opener):
            opener(session_key)
        opened = True

    try:
        for _ in range(_MAX_REPROJECTIONS):
            if not await hook_projection_stale(claimed[0], agent_id):
                break
            logger.info(
                "agent %r: the claimed session auto-approves a capability a PreToolUse "
                "hook now covers; re-projecting it before the turn runs",
                agent_id,
            )
            if not opened:
                _open_gap()
            if expect is _NO_FENCE:
                await sessions.reset(session_key)
            elif not await sessions.reset(session_key, expect_session=expect):
                # A successor another turn owns holds the key: a hard Stop popped
                # the claimed session and a newer message claimed it while the
                # caller's pre-reproject await ran. Leave it, and abandon.
                logger.info(
                    "agent %r: the claimed session was replaced before re-projection; "
                    "abandoning this turn rather than resetting its successor",
                    agent_id,
                )
                superseded = True
                break
            claimed, fresh_entry = await claim()
            # The reset recorded this task as the holder of the permit it popped;
            # this claim's permit is the one its release is for.
            adopt = getattr(sessions, "adopt_turn", None)
            if callable(adopt):
                adopt(session_key)
            # The re-claim's own entry fences the second pass; the open gap holds
            # every other claim, so the caller's recapture is this turn's.
            expect = fresh_entry if fresh_entry is not None else _NO_FENCE
        else:
            still_stale = await hook_projection_stale(claimed[0], agent_id)
    except BaseException:
        # The caller learns of no re-projection, so it closes no gap: this does.
        closer = getattr(sessions, "close_replay_gap", None)
        if opened and callable(closer):
            closer(session_key)
        raise
    if still_stale:
        logger.warning(
            "agent %r: the session is still stale after %d re-projections; this "
            "turn keeps the stale-batch fallback",
            agent_id,
            _MAX_REPROJECTIONS,
        )
    return UnattendedClaim(claimed, opened, still_stale, superseded)


def close_reprojection_gap(sessions: Any, session_key: str) -> None:
    """Close the gap :func:`reproject_unattended_claim` left open, after the release.

    Owner-checked and idempotent (``SessionManager.close_replay_gap``); probed,
    as the open is.
    """
    closer = getattr(sessions, "close_replay_gap", None)
    if callable(closer):
        closer(session_key)
