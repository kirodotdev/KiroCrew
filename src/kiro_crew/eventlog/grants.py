"""Who may read, append and publish on a unit's log.

One question per function, each answered from the app's own manifest
``contributions`` declaration and nothing else. The HTTP handlers and the
WebSocket hub both call these, so the answer cannot differ between the two
surfaces.

Three properties are deliberate:

* **Disabled means denied.** An app token survives a disable (the secret is not
  rotated), so every answer here first asks whether the app is enabled --
  mirroring ``ws_event_scope.app_events_revoked``, which exists for the same
  reason on the frame side.
* **Reading is per kind, not per event type** (contract §2). A subscriber sees
  every event of a unit it may subscribe to, which is why the contract puts
  sensitive data in the durable store an event points at rather than in the
  event.
* **The prefix rule is re-checked at use, not trusted from install.** A manifest
  is a file on disk that an app trusted to run code can rewrite, so a pattern
  that does not begin with the app's own name is ignored here even though
  ``AppManifest.validate`` would have refused it at install time.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from fnmatch import fnmatchcase

logger = logging.getLogger(__name__)

#: Short-TTL cache of each app's declaration. Same shape and reason as
#: ``token_auth._app_api_allowlist``: this is on the append hot path, the
#: manifest read has no internal cache, and a declaration changes rarely, so a
#: 30s TTL self-heals after enable/disable/update with no invalidation wiring.
_TTL_SECS = 30.0

_cache: dict[str, tuple[float, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]]] = {}
_cache_lock = threading.Lock()

#: Apps whose declaration is being re-resolved on a worker thread. A burst of
#: authorization checks arriving on one expired entry must start ONE read, not
#: one per request.
_refreshing: set[str] = set()

# Apps whose grant is being torn down. `is_app_enabled` still returns true until
# the config write later in teardown lands, so without this a concurrent
# `may_publish` between `invalidate()` and that write would re-populate the cache
# with a live grant and reopen the very window teardown_contributions exists to
# close. A revoked app is denied here regardless of its enabled state until
# `unrevoke` clears it (a re-enable re-registers trust).
_revoked: set[str] = set()


def _resolve(app: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """The I/O half: read *app*'s enabled state and manifest declaration.

    Blocks on the filesystem, so it runs on a worker thread for every call but
    the very first one for an app (see :func:`_declaration`).
    """
    empty: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] = ((), (), ())
    resolved = empty
    try:
        from kiro_crew.apps.manager import get_app_manifest, is_app_enabled

        if is_app_enabled(app):
            manifest = get_app_manifest(app)
            if manifest is not None:
                prefix = f"{app}/"
                decl = manifest.contributions
                resolved = (
                    tuple(p for p in decl.events if p.startswith(prefix) and len(p) > len(prefix)),
                    tuple(
                        p for p in decl.projections if p.startswith(prefix) and len(p) > len(prefix)
                    ),
                    tuple(k for k in decl.units if k),
                )
    except Exception:
        logger.warning(
            "contributions: could not resolve the declaration for %r; denying by default",
            app,
            exc_info=True,
        )
        resolved = empty
    return resolved


def _seed(app: str, resolved: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]) -> None:
    with _cache_lock:
        # A revoke that landed while we resolved (outside the lock) must win: do
        # not seed the cache with a live grant for an app now being torn down.
        if app in _revoked:
            return
        _cache[app] = (time.time(), resolved)


def is_cold(app: str) -> bool:
    """Whether the next :func:`_declaration` for *app* would do its I/O INLINE.

    False for a revoked app as well as a cached one, because the tombstone is
    answered before the cache and needs no read either way. Holds the lock for a
    dict lookup and nothing else, so an async caller can ask this on every
    request and pay a thread hop only when there is actually something to read.
    """
    with _cache_lock:
        return app not in _revoked and app not in _cache


async def warm(app: str) -> None:
    """Resolve *app*'s declaration OFF the event loop, before a predicate needs it.

    :func:`_resolve` reads ``installed.json`` and the app manifest, uncached, and
    :func:`_declaration` runs it inline on a cache miss -- so the first request of
    an app's session does that filesystem work on whichever thread asked. When
    that thread is the gateway's event loop, every other task waits on it.

    The predicates themselves cannot become async: as
    :func:`_refresh_in_background` records, the per-frame event scoper and the auth
    path classifier both answer inside a non-async function. So the resolve is
    hoisted to the nearest async boundary instead, which leaves every synchronous
    consumer downstream of it a cache hit. A TTL expiry needs no warm -- that path
    already refreshes on a worker thread and serves the previous answer meanwhile.
    """
    if not is_cold(app):
        return
    _seed(app, await asyncio.to_thread(_resolve, app))


def _refresh_in_background(app: str) -> None:
    """Re-resolve *app* off the caller's thread, at most one refresh at a time.

    The callers of :func:`_declaration` are synchronous BY CONSTRUCTION -- the
    per-frame event scoper and the auth path classifier both answer inside a
    non-async predicate -- so this cannot be an ``await asyncio.to_thread``. A
    worker thread is what keeps the manifest read off whichever thread asked,
    including the gateway's event loop.
    """
    with _cache_lock:
        if app in _refreshing:
            return
        _refreshing.add(app)

    def _run() -> None:
        try:
            _seed(app, _resolve(app))
        finally:
            with _cache_lock:
                _refreshing.discard(app)

    threading.Thread(target=_run, name=f"contrib-grant-{app[:32]}", daemon=True).start()


def _declaration(app: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """``(events, projections, units)`` for *app*, or empty triple.

    Empty on every failure -- app absent, disabled, manifest unreadable -- which
    denies by default. Patterns not prefixed with ``<app>/`` are dropped here, so
    a caller cannot be handed a grant over someone else's namespace even if the
    file on disk claims one.

    Reads from memory. A STALE entry is served as-is and re-resolved on a worker
    thread, so an expiring TTL does not turn an authorization check into a
    filesystem read on the gateway's event loop -- at 30s that would otherwise
    recur for the life of the process, and slow storage would stall every session
    on the loop, not just the request that asked.

    The one read this cannot move off the caller is an app's FIRST: there is no
    previous answer to serve, and returning empty would deny a request the app is
    entitled to make. It is bounded at one per app per process, and the revoke
    tombstone below is checked before the cache either way, so a teardown is
    never served from a stale entry.
    """
    now = time.time()
    with _cache_lock:
        if app in _revoked:
            # Being torn down: deny regardless of enabled state, and do not cache
            # (the tombstone, not the cache, is the source of truth until it lifts).
            return ((), (), ())
        entry = _cache.get(app)
    if entry is not None:
        if now - entry[0] >= _TTL_SECS:
            _refresh_in_background(app)
        return entry[1]

    resolved = _resolve(app)
    _seed(app, resolved)
    with _cache_lock:
        if app in _revoked:
            return ((), (), ())
    return resolved


def revoke(app: str) -> None:
    """Hard-deny *app*'s grant while it is torn down, then drop its cache.

    Set BEFORE the enabled state flips: `is_app_enabled` still answers true until
    the config write later in teardown, so the tombstone -- not the enabled flag
    -- is what closes the window. `unrevoke` lifts it when trust is re-granted.
    """
    with _cache_lock:
        _revoked.add(app)
        _cache.pop(app, None)


def unrevoke(app: str) -> None:
    """Lift a teardown tombstone so a re-enabled app can be granted again."""
    with _cache_lock:
        _revoked.discard(app)


def invalidate(app: str | None = None) -> None:
    """Drop the cached declaration for *app*, or for every app.

    NO production caller. Teardown reaches the same end through :func:`revoke`,
    which denies the app outright rather than re-deriving its declaration, so it
    does not need this -- and a reader who takes "called by teardown" on trust would
    look for a caller that is not there. Kept as the cache's own seam: the cache is
    this module's, and a module that caches an authority answer has to be able to
    forget one. Anything that starts calling it belongs in this docstring.
    """
    with _cache_lock:
        if app is None:
            _cache.clear()
            # A no-arg invalidate is the global reset; lift every tombstone too so
            # it is a true reset rather than leaving apps permanently denied.
            _revoked.clear()
        else:
            _cache.pop(app, None)


def _matches(patterns: tuple[str, ...], value: str) -> bool:
    """Whether *value* matches any pattern, ``*`` globbing, case-sensitive.

    ``fnmatchcase`` rather than ``fnmatch``: the latter applies the platform's
    case rules, so ``Mochi/Ping`` would match ``mochi/*`` on a case-insensitive
    filesystem and not on Linux -- an authority answer must not depend on that.
    """
    return any(fnmatchcase(value, pattern) for pattern in patterns)


def declares_contributions(app: str) -> bool:
    """Whether *app* declared any contribution at all.

    This is what grants the ``/api/eventlog/`` path prefix and the ``eventlog_*``
    frames. It is not authority over any particular unit, type or key: each
    request re-derives that from the same declaration.
    """
    events, projections, units = _declaration(app)
    return bool(events or projections or units)


def may_use_kind(app: str, kind: str) -> bool:
    """Whether *app* may subscribe to and append to units of *kind*."""
    _events, _projections, units = _declaration(app)
    return kind in units


def may_append(app: str, kind: str, event_type: str) -> bool:
    """Whether *app* may append *event_type* to a unit of *kind*.

    A BUILT-IN event type is refused first, the same shape and for the same
    reason as :func:`may_publish`'s built-in-key check. The declaration rule
    alone does not cover it: ``types.is_contributed_event_type``'s contract says
    a contributor's type is ``<app>/<name>`` with a namespace the built-ins do
    not own **because "an app cannot be named for one of these"** -- but nothing
    enforces that premise, since ``app_name_error`` reserves no namespace names.
    So an app installed as ``member`` declaring ``events: ["member/*"]`` passes
    ``Contributions.validate`` (the prefix matches its own name, ``member`` is a
    known kind) and its declaration then matches ``member/binding``, which is in
    ``ALL_EVENT_TYPES`` and which ``RosterProjection`` folds AUTHORITATIVELY --
    letting a contributor overwrite gateway-owned roster fields it never owned.

    Checking the type here is what the helper's own docstring asks for: it is
    syntax only, and "WHETHER a given app may append it is authority, decided at
    the HTTP boundary against that app's declared ``contributions.events``".
    """
    from kiro_crew.eventlog import types

    if not types.is_contributed_event_type(event_type):
        return False
    if not may_use_kind(app, kind):
        return False
    events, _projections, _units = _declaration(app)
    return _matches(events, event_type)


def may_publish(app: str, kind: str, key: str) -> bool:
    """Whether *app* may publish the projection *key* on a unit of *kind*.

    A built-in key is refused by the prefix rule alone -- ``roster`` has no
    ``<app>/`` prefix, so no declaration can match it -- but the check is written
    explicitly because "built-in keys cannot be published from outside" is the
    contract's own sentence, and a future kind whose built-in key happened to
    contain a slash would otherwise turn a silent no into a silent yes.
    """
    if key in _builtin_keys():
        return False
    if not may_use_kind(app, kind):
        return False
    _events, projections, _units = _declaration(app)
    return _matches(projections, key)


def _builtin_keys() -> frozenset[str]:
    from kiro_crew.eventlog import types

    return frozenset(types.ALL_PROJECTION_KEYS)
