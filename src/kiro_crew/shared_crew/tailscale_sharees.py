"""People Tailscale shows this machine, beyond the owner: the People list's ``tailscale_share`` rows.

The local Tailscale daemon lists every node it can see and the user who owns
each. A user other than this machine's owner, with a node that is not
ACL-tagged, is somebody the owner shared with or a fellow member of the
tailnet. They show in People before they ever connect, so the owner sees who a
share reached and a first visit lands on a row that already exists.

Reading the daemon is blocking, so it runs off the event loop under a short
timeout and its answer is cached for :data:`CACHE_TTL_SECS`. A daemon that is
down, slow or unreadable yields no rows. It is never an error for the route.

Nothing here admits anyone: a row is a label, and access is decided by the
Tailscale access rules and the direct listener's allowlist.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from kiro_crew.dashboard import tailnet_localapi
from kiro_crew.dashboard.tailnet_direct import TailnetUser, parse_tailnet_users
from kiro_crew.shared_crew.contracts import Principal, Provenance

logger = logging.getLogger(__name__)

CACHE_TTL_SECS = 15.0
#: The await budget for one People request; the daemon read keeps its own bound.
READ_TIMEOUT_SECS = 6.0


@dataclass(frozen=True)
class Sharee:
    """One person the daemon lists, with the principal a direct visit would give them."""

    user: TailnetUser
    principal: Principal


@dataclass
class _Cache:
    expires: float
    users: tuple[TailnetUser, ...]


_LOCK = threading.Lock()
_cache: _Cache | None = None
_clock: Callable[[], float] = time.monotonic
_read: Callable[[], tailnet_localapi.Answer] = tailnet_localapi.status_with_peers


def sharee_principal(user: TailnetUser) -> Principal:
    """The principal the direct listener would name for *user*'s login.

    Built by the same function, so the id is the one a later connection gets
    and the row merges with the observed one.
    """
    from kiro_crew.shared_crew.identity import tailnet_principal

    return tailnet_principal(
        user.login,
        "",
        Provenance.TAILSCALE_LISTENER,
        display_name=user.display_name,
    )


def _users_cached() -> tuple[TailnetUser, ...]:
    """The cached user list, refreshed when stale. Blocking; never raises."""
    global _cache
    with _LOCK:
        now = _clock()
        if _cache is not None and _cache.expires > now:
            return _cache.users
        try:
            users = tuple(parse_tailnet_users(_read().data))
        except Exception:  # noqa: BLE001 -- the daemon is optional input
            logger.debug("tailscale peers read failed", exc_info=True)
            users = ()
        _cache = _Cache(expires=now + CACHE_TTL_SECS, users=users)
        return users


async def read_sharees() -> list[Sharee]:
    """The people the daemon lists besides the owner. Never raises, never blocks the loop."""
    try:
        users = await asyncio.wait_for(asyncio.to_thread(_users_cached), READ_TIMEOUT_SECS)
    except Exception:  # noqa: BLE001 -- includes the timeout
        logger.debug("tailscale peers unavailable", exc_info=True)
        return []
    out: list[Sharee] = []
    for user in users:
        try:
            out.append(Sharee(user=user, principal=sharee_principal(user)))
        except Exception:  # noqa: BLE001 -- one malformed login drops one row
            logger.debug("tailscale peer skipped", exc_info=True)
    return out


def login_for(user_id: str) -> str:
    """The lower-cased login of cached user *user_id*, or ``""``. Cache only, no daemon read."""
    with _LOCK:
        users = _cache.users if _cache is not None else ()
    for user in users:
        if user.user_id == user_id:
            return user.login.strip().lower()
    return ""


def reset_for_tests(
    *,
    read: Callable[[], tailnet_localapi.Answer] | None = None,
    clock: Callable[[], float] | None = None,
) -> None:
    global _cache, _read, _clock
    with _LOCK:
        _cache = None
        _read = read or tailnet_localapi.status_with_peers
        _clock = clock or time.monotonic


__all__ = [
    "CACHE_TTL_SECS",
    "READ_TIMEOUT_SECS",
    "Sharee",
    "login_for",
    "read_sharees",
    "reset_for_tests",
    "sharee_principal",
]
