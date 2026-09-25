"""Who may read, append and publish on a unit's log.

One question per function, each answered from the app's own manifest
``contributions`` declaration, intersected for unit kinds with what an operator
approved. The HTTP handlers and the WebSocket hub both call these, so the answer
cannot differ between the two surfaces.

Four properties are deliberate:

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
* **A unit KIND is authority, so it is not taken from the manifest alone.** The
  rule above cannot guard ``units``: a kind is not namespaced -- ``member`` is the
  gateway's -- so there is no prefix for a pattern to fail. An app that rewrote
  its own manifest to add ``units: ["member"]`` would otherwise have granted
  itself read over a member's whole log. Declared kinds are therefore intersected
  with :func:`apps.manager.approved_unit_kinds`, which answers from a
  gateway-owned record OUTSIDE every app's own tree, so a kind must be both still
  declared AND already approved. The record's location is the point: an approval
  kept inside the app's directory would be writable by the very app it limits.
"""

from __future__ import annotations

import logging
import threading
from fnmatch import fnmatchcase

logger = logging.getLogger(__name__)

#: Cache of each app's declaration, keyed on :data:`_generation` rather than on
#: a clock. This sits on the append hot path and the manifest read has no cache
#: of its own, so it must not repeat per request; a declaration also changes only
#: on a lifecycle event, and every one of those bumps the counter. A timer was
#: doing two jobs badly against that: re-reading a declaration that had not
#: changed, and leaving a changed one answerable until the window closed.
_cache: dict[str, tuple[int, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]]] = {}
#: A Condition rather than a plain Lock so :func:`revoke` can WAIT on it: every
#: ``with _cache_lock`` below is unchanged, and the waiting is what lets a
#: revocation drain the commits already past the fence (see :data:`_inflight`).
_cache_lock = threading.Condition()

#: Commits per app that have passed the fence and not yet finished their durable
#: write. A fence check and the write it authorizes are two statements, and a
#: revocation landing between them would let an unauthorized event or row persist
#: -- so the fence check and the registration happen together under the lock, and
#: :func:`revoke` waits here until the app's count reaches zero.
#:
#: The lock is deliberately NOT held across the write: :func:`_declaration` takes
#: it on every grant question, including from the serving path, so holding it
#: across file IO would queue every app's grant reads behind one app's write --
#: the event-loop stall this module is otherwise careful to avoid.
_inflight: dict[str, int] = {}

#: How long :func:`revoke` waits for an app's in-flight commits before it gives up
#: and proceeds anyway. Teardown must not hang on a wedged writer, so the wait is
#: bounded and exhausting it is logged at warning -- a commit that outlives this
#: can still land, which is the residual the bound buys.
_DRAIN_TIMEOUT_SECS = 5.0


# Apps whose grant is being torn down. `is_app_enabled` still returns true until
# the config write later in teardown lands, so without this a concurrent
# `may_publish` between `invalidate()` and that write would re-populate the cache
# with a live grant and reopen the very window teardown_contributions exists to
# close. A revoked app is denied here regardless of its enabled state until
# `unrevoke` clears it (a re-enable re-registers trust).
_revoked: set[str] = set()

#: Bumped by every change to the answers above. A request that checked a grant,
#: then suspended (resolving a unit off-loop), cannot tell on resumption whether
#: its grant still holds: re-asking is not enough, because a same-name app
#: re-installed during the suspension answers yes while being a DIFFERENT app.
#: Comparing this counter answers the question that matters -- "did anything
#: about any grant change while I was away" -- and a mutation carries the value
#: it checked at so the commit can refuse rather than recreate torn-down state.
_generation = 0


def is_cached(app: str) -> bool:
    """Whether *app*'s declaration is cached against the CURRENT generation.

    For a caller deciding whether it must pay an executor hop before asking a
    grant question on the loop. Cheap: a dict read under the cache lock, no
    filesystem access. A revoked app answers True because it is answered from
    the tombstone without reading anything.
    """
    with _cache_lock:
        if app in _revoked:
            return True
        entry = _cache.get(app)
        return entry is not None and entry[0] == _generation


def warm(app: str) -> None:
    """Resolve *app*'s declaration into the cache. MUST be called OFF the loop.

    This is the off-loop half of the fix for manifest I/O on the serving loop.
    The grant questions below are sync, because one of their callers is a sync
    WebSocket frame filter that cannot become async; so rather than making the
    read async, the read is done HERE, in an executor, before the loop asks. The
    auth middleware calls this through ``asyncio.to_thread`` for the app it just
    authenticated, so by the time any handler asks a grant question the answer is
    a dict read.

    Deny-safe by construction: it only calls :func:`_declaration`, every failure
    of which resolves to the empty triple.
    """
    _declaration(app)


def _declaration(app: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """``(events, projections, units)`` for *app*, or empty triple.

    Empty on every failure -- app absent, disabled, manifest unreadable -- which
    denies by default. Patterns not prefixed with ``<app>/`` are dropped here, so
    a caller cannot be handed a grant over someone else's namespace even if the
    file on disk claims one.

    A cold cache still reads the manifest on the CALLING thread, which is why
    :func:`warm` exists: the auth middleware resolves an app's declaration in an
    executor right after authenticating it, so the serving loop meets a warm
    cache. The inline read stays as the correctness fallback for a caller that
    arrives before any warm -- answering empty there instead would 403 an app's
    first request, which is a worse trade than a rare read. Generation keying is
    what makes it rare: it is now reachable only after a lifecycle event, not
    every time a 30-second timer lapsed.
    """
    empty: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] = ((), (), ())
    with _cache_lock:
        if app in _revoked:
            # Being torn down: deny regardless of enabled state, and do not cache
            # (the tombstone, not the cache, is the source of truth until it lifts).
            return empty
        entry = _cache.get(app)
        # Keyed on the grant GENERATION rather than a timer. A declaration
        # changes only when an app is installed, enabled, disabled or torn down,
        # and every one of those bumps the counter -- so a warm entry stays warm
        # instead of being re-read every 30 seconds, and a lifecycle change
        # invalidates it at once instead of at the end of a window.
        if entry is not None and entry[0] == _generation:
            return entry[1]
        generation_at_read = _generation

    resolved = empty
    try:
        from kiro_crew.apps.manager import (
            approved_unit_kinds,
            get_app_manifest,
            is_app_enabled,
        )

        if is_app_enabled(app):
            manifest = get_app_manifest(app)
            if manifest is not None:
                prefix = f"{app}/"
                decl = manifest.contributions
                # Intersected, not read: see the fourth property in the module
                # docstring. `approved_unit_kinds` is empty for an app with no
                # entry in the approval record, so such an install contributes to
                # no kind until an operator approves one -- the app keeps every
                # other grant, and `units_pending_approval` shows the difference.
                approved = approved_unit_kinds(app)
                resolved = (
                    tuple(p for p in decl.events if p.startswith(prefix) and len(p) > len(prefix)),
                    tuple(
                        p for p in decl.projections if p.startswith(prefix) and len(p) > len(prefix)
                    ),
                    tuple(k for k in decl.units if k and k in approved),
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
            return empty
        # Stamped with the generation this read STARTED at, so a lifecycle event
        # that landed mid-read leaves the entry stale and the next call re-reads
        # rather than trusting a value resolved against the old world.
        _cache[app] = (generation_at_read, resolved)
    return resolved


def revocation_generation() -> int:
    """The current grant-state generation; see :data:`_generation`.

    Read it BEFORE the grant check, pass it to the mutation, and the mutation
    refuses if it has moved. Read after the check instead and the window is
    still open: a revoke landing between the two is invisible.
    """
    with _cache_lock:
        return _generation


def begin_commit(app: str) -> int:
    """Register an in-flight commit for *app*; returns the generation it begins in.

    Atomic with the generation read, and that is the whole point. The caller
    compares the returned value against the generation it was authorized in, and
    because the registration happened under the same lock hold there are only two
    orderings: this commit registers before a concurrent :func:`revoke` bumps, so
    that revoke waits for it; or the bump happened first, the returned value
    differs, and the caller abandons the write. Nothing lands in between.

    Always paired with :func:`end_commit` in a ``finally`` -- a slot never released
    makes the app's teardown wait out the full drain timeout.
    """
    with _cache_lock:
        _inflight[app] = _inflight.get(app, 0) + 1
        return _generation


def outstanding_commits(app: str) -> int:
    """How many of *app*'s commits are past the grant fence and still unwritten.

    Read by teardown BEFORE it deletes the app's rows. The drain in :func:`revoke`
    is bounded, so it can return with a commit still outstanding; deleting the rows
    then is what makes that commit's write permanent residue -- a row whose
    publisher is gone and which nothing remains to correct.

    Sound as a guard because a revoked grant admits no NEW commit: the generation
    moved, so :func:`begin_commit`'s caller compares and abandons. What this counts
    is therefore exactly the set authorized under the retired grant, and it only
    ever shrinks while teardown runs.
    """
    with _cache_lock:
        return _inflight.get(app, 0)


def end_commit(app: str) -> None:
    """Release a slot taken by :func:`begin_commit` and wake a waiting drain."""
    with _cache_lock:
        remaining = _inflight.get(app, 0) - 1
        if remaining > 0:
            _inflight[app] = remaining
        else:
            _inflight.pop(app, None)
        _cache_lock.notify_all()


def revoke(app: str) -> None:
    """Hard-deny *app*'s grant while it is torn down, then drop its cache.

    Set BEFORE the enabled state flips: `is_app_enabled` still answers true until
    the config write later in teardown, so the tombstone -- not the enabled flag
    -- is what closes the window. `unrevoke` lifts it when trust is re-granted.

    The generation is bumped HERE rather than after the rows are deleted, so a
    mutation already in flight sees the change before teardown removes anything.

    Then the commits ALREADY past the fence are drained. Bumping alone does not
    reach them: they were authorized in the generation just retired and their
    writes are still outstanding, so returning here would let teardown delete rows
    that a commit writes straight back. Waiting makes the guarantee whole -- once
    this returns, no commit authorized under the old grant is still unwritten.
    """
    global _generation
    with _cache_lock:
        _revoked.add(app)
        _cache.pop(app, None)
        _generation += 1
        if _inflight.get(app):
            # `wait_for` releases the lock while it waits, which is what lets those
            # commits finish and `end_commit` notify.
            drained = _cache_lock.wait_for(
                lambda: not _inflight.get(app), timeout=_DRAIN_TIMEOUT_SECS
            )
            if not drained:
                logger.warning(
                    "contributions: %r still has %d commit(s) in flight after %.1fs; "
                    "proceeding with teardown, so one of them may still land",
                    app,
                    _inflight.get(app, 0),
                    _DRAIN_TIMEOUT_SECS,
                )


def unrevoke(app: str) -> None:
    """Lift a teardown tombstone so a re-enabled app can be granted again."""
    global _generation
    with _cache_lock:
        _revoked.discard(app)
        _generation += 1


def invalidate(app: str | None = None) -> None:
    """Drop the cached declaration for *app*, or for every app.

    Called by teardown so a disable takes effect on the next request rather than
    at the end of the TTL -- the frames and rows are torn down synchronously
    there, and leaving the grant answerable for another 30 seconds would let an
    append land after the rows it would have folded into were deleted.
    """
    global _generation
    with _cache_lock:
        if app is None:
            _cache.clear()
            # A no-arg invalidate is the global reset; lift every tombstone too so
            # it is a true reset rather than leaving apps permanently denied.
            _revoked.clear()
        else:
            _cache.pop(app, None)
        _generation += 1


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
