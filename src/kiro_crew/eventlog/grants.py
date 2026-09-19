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

# Apps whose grant is being torn down. `is_app_enabled` still returns true until
# the config write later in teardown lands, so without this a concurrent
# `may_publish` between `invalidate()` and that write would re-populate the cache
# with a live grant and reopen the very window teardown_contributions exists to
# close. A revoked app is denied here regardless of its enabled state until
# `unrevoke` clears it (a re-enable re-registers trust).
_revoked: set[str] = set()


def _declaration(app: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """``(events, projections, units)`` for *app*, or empty triple.

    Empty on every failure -- app absent, disabled, manifest unreadable -- which
    denies by default. Patterns not prefixed with ``<app>/`` are dropped here, so
    a caller cannot be handed a grant over someone else's namespace even if the
    file on disk claims one.
    """
    now = time.time()
    with _cache_lock:
        if app in _revoked:
            # Being torn down: deny regardless of enabled state, and do not cache
            # (the tombstone, not the cache, is the source of truth until it lifts).
            return ((), (), ())
        entry = _cache.get(app)
        if entry is not None and now - entry[0] < _TTL_SECS:
            return entry[1]

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

    with _cache_lock:
        # A revoke that landed while we resolved (outside the lock) must win: do
        # not seed the cache with a live grant for an app now being torn down.
        if app in _revoked:
            return ((), (), ())
        _cache[app] = (now, resolved)
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

    Called by teardown so a disable takes effect on the next request rather than
    at the end of the TTL -- the frames and rows are torn down synchronously
    there, and leaving the grant answerable for another 30 seconds would let an
    append land after the rows it would have folded into were deleted.
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
