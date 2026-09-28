"""The operator's self-managed GitLab/Jira allowlists and the session-card chip switch.

All three come out of one ``dashboard`` config read, run off the event loop and
cached for a short TTL, so URL validation and slot serialization -- both sync code
on the loop -- read them with a pure attribute lookup. The allowlists fail CLOSED
while cold; the chip switch fails OPEN.
"""

from __future__ import annotations

import asyncio
import logging
import time

from kiro_crew.dashboard.source_providers import LOGGER_NAME
from kiro_crew.loop_lock import LoopBoundLock

logger = logging.getLogger(LOGGER_NAME)


_GITLAB_HOSTS_TTL_SECS = 30.0
# Cached allowlist snapshots. Populated only by _load_source_link_settings()
# running in a worker thread, so every reader on the event loop is a pure dict
# lookup. GitLab and Jira allowlists come out of the SAME config read and share
# one TTL, lock, and generation counter: they change together (one config file)
# and consumers that memoize parse results (the per-slot sidebar source links)
# fold a single generation into their cache key either way.
_gitlab_hosts_snapshot: frozenset[str] = frozenset()
_jira_hosts_snapshot: frozenset[str] = frozenset()
# Whether the sidebar renders a session card's PR/issue chips at all
# (``dashboard.session_card_source_links``). Same read, same TTL, same
# generation as the allowlists above, for the same reason: it is another
# ``dashboard`` field off the same config file, and it is consumed by the same
# synchronous slot serialization that cannot read config itself.
#
# Starts TRUE where the allowlists start EMPTY, and the asymmetry is deliberate:
# an unknown host must fail CLOSED (do not recognize a link yet), but the chip
# strip predates this switch, so a cold snapshot must fail OPEN or every install
# would render no chips until the first refresh lands.
_session_card_chips_snapshot: bool = True
_gitlab_hosts_loaded_at = 0.0
# Bumped whenever either snapshot's CONTENT changes. Consumers that memoize a
# parse result (per-slot sidebar source links) fold this into their cache key so
# a later allowlist load invalidates decisions made against the cold snapshot.
_gitlab_hosts_generation = 0
_gitlab_hosts_lock = LoopBoundLock()


def gitlab_hosts_generation() -> int:
    """Monotonic counter identifying the current allowlist snapshot."""
    return _gitlab_hosts_generation


def _gitlab_hosts_fresh() -> bool:
    return bool(
        _gitlab_hosts_loaded_at
        and time.monotonic() - _gitlab_hosts_loaded_at < _GITLAB_HOSTS_TTL_SECS
    )


def _publish_provider_hosts(gitlab: frozenset[str], jira: frozenset[str]) -> None:
    """Install freshly loaded snapshots, bumping the generation on real change."""
    global _gitlab_hosts_snapshot, _jira_hosts_snapshot
    global _gitlab_hosts_loaded_at, _gitlab_hosts_generation
    if gitlab != _gitlab_hosts_snapshot or jira != _jira_hosts_snapshot:
        _gitlab_hosts_snapshot = gitlab
        _jira_hosts_snapshot = jira
        _gitlab_hosts_generation += 1
    _gitlab_hosts_loaded_at = time.monotonic()


def _publish_session_card_chips(enabled: bool) -> None:
    """Install the chip switch snapshot, bumping the SHARED generation on change.

    Its own publisher rather than a third argument to
    :func:`_publish_provider_hosts`, so a caller that only has hosts to install
    cannot silently reset the switch. The generation is shared on purpose: the
    owner websocket's refresh round pushes a fresh slots payload whenever it
    moves, which is what makes the chips appear or disappear without a reload.

    Callers outside the refresh must go through
    :func:`publish_session_card_chips_now`, which orders them against an
    in-flight load.
    """
    global _session_card_chips_snapshot, _gitlab_hosts_generation
    if enabled != _session_card_chips_snapshot:
        _session_card_chips_snapshot = enabled
        _gitlab_hosts_generation += 1


async def publish_session_card_chips_now(enabled: bool) -> None:
    """Install a just-WRITTEN chip switch value, ordered against the refresh.

    The switch has two writers the allowlists do not: the config PUT, which
    publishes at write time so the click is not stuck behind the TTL, and the
    refresh poll. Taking ``_gitlab_hosts_lock`` -- which
    :func:`ensure_gitlab_hosts_loaded` holds ACROSS its threaded load -- is what
    keeps them ordered: a poll already in flight is holding a reading from before
    the write, and publishing that after the write would resume the chips, and the
    credentialed polling behind them, for another full interval. Waiting for the
    lock means the write always lands last.

    The wait is bounded by that load, which is one config read.
    """
    async with _gitlab_hosts_lock:
        _publish_session_card_chips(enabled)


def session_card_source_links_enabled() -> bool:
    """Are the sidebar's per-card PR/issue chips switched on? Cache-only read.

    Safe to call from sync code on the event loop -- slot serialization and the
    check-refresh feeds do, once per slots push. It never touches the
    filesystem: the value arrives only from :func:`_load_source_link_settings`
    running in a worker thread, so an operator's edit takes effect within one TTL
    instead of stalling every push on a config read.
    """
    return _session_card_chips_snapshot


def _load_source_link_settings() -> tuple[frozenset[str], frozenset[str], bool]:
    """Read the source-link config: GitLab hosts, Jira hosts, chip switch.

    BLOCKING -- never on the loop. ``KiroCrewConfig.load()`` stats, reads,
    parses, and validates config files, so a slow or network-backed config
    directory would stall the sole event loop. Callers reach this only through
    :func:`ensure_gitlab_hosts_loaded`.

    One read for all three because they live in one file and are consumed by the
    same synchronous slot serialization; a second read would double the cost of
    every refresh round and let the two halves disagree within one round.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        dashboard = KiroCrewConfig.load().dashboard
        return (
            frozenset(dashboard.gitlab_hosts),
            frozenset(dashboard.jira_hosts),
            bool(dashboard.session_card_source_links),
        )
    except Exception:
        logger.debug("source-link settings unavailable", exc_info=True)
        # Hosts fail closed, the chip switch fails open: an unreadable config
        # must not blank a strip the user never asked to hide.
        return frozenset(), frozenset(), True


async def ensure_gitlab_hosts_loaded() -> frozenset[str]:
    """Refresh the cached allowlist off the event loop when the TTL has expired.

    Awaited by every async entry point before it validates a URL, so an operator
    adding an instance takes effect within one TTL without a gateway restart and
    without a config read ever running on the loop.

    The refresh is serialized: two concurrent loads could otherwise interleave so
    that a reader holding the PRE-revocation config installs its snapshot after
    the post-revocation one and resets the TTL, re-admitting a host the operator
    just removed for another full interval. The lock plus a post-acquire
    freshness recheck means exactly one load happens per expiry.
    """
    global _gitlab_hosts_snapshot, _gitlab_hosts_loaded_at
    if _gitlab_hosts_fresh():
        return _gitlab_hosts_snapshot
    async with _gitlab_hosts_lock:
        # Another waiter may have refreshed while this one queued.
        if _gitlab_hosts_fresh():
            return _gitlab_hosts_snapshot
        gitlab, jira, chips = await asyncio.to_thread(_load_source_link_settings)
        _publish_provider_hosts(gitlab, jira)
        # Safe to install unconditionally: this lock is held across the load
        # above, so a config write that raced it waits and publishes after us
        # (see publish_session_card_chips_now).
        _publish_session_card_chips(chips)
        return gitlab


def _allowed_gitlab_hosts() -> frozenset[str]:
    """Return the cached self-managed GitLab hosts without touching the filesystem.

    Safe to call from sync code on the event loop (URL parsing, slot
    serialization). The snapshot comes only from config -- never from the browser
    -- and is matched exactly, so neither a pasted URL nor a lookalike suffix can
    widen it. Before the first :func:`ensure_gitlab_hosts_loaded` completes the
    snapshot is empty, which fails closed (a self-managed URL is simply not
    recognized yet).
    """
    return _gitlab_hosts_snapshot


def _allowed_jira_hosts() -> frozenset[str]:
    """Return the cached self-hosted Jira hosts. Same discipline as GitLab.

    Loaded by the same :func:`ensure_gitlab_hosts_loaded` refresh (one config
    read covers both allowlists), so every entry point that awaits it before
    parsing has this snapshot warm too. Fails closed while cold.
    """
    return _jira_hosts_snapshot
