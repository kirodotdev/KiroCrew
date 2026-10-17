"""The service's account baselines: the retirement baseline, the interim latch, absence runs.

Bound into :class:`KiroPrerequisiteService`. Reads ``self._assume_ready``,
``self._has_probed``, ``self._platform``, ``self._home``, ``self._environ``,
``self._data_home`` and ``self._probe_identity``; reads and writes
``self._identity_cache``, ``self._identity_cache_at``,
``self._identity_cache_absence_definitive``, ``self._identity_absence_run``,
``self._identity_absence_run_swept``, ``self._relocation_logged``,
``self._session_identity``, ``self._interim_identity_observed`` and
``self._interim_identity_gen``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from kiro_crew.kiro_prerequisite_identity import (
    _AUTH_FINGERPRINT_ABSENT,
    _api_key_identity_claim,
    _combine_identity_fingerprints,
    _crew_vault_fingerprint,
    _crew_vault_holds_no_identity,
    _identity_components,
    _store_choice_is_certain,
    identity_fingerprint,
)
from kiro_crew.kiro_prerequisite_identity_store import (
    identity_store_is_relocated,
    kiro_identity_store_path,
)

if TYPE_CHECKING:
    from kiro_crew.kiro_prerequisite_service import KiroPrerequisiteService

logger = logging.getLogger("kiro_crew.kiro_prerequisite")


# How long a computed fingerprint may be reused. Bounds both the SQLite reads and
# the SEL audit events a poll storm can produce (N dashboard tabs poll status every
# few seconds), so the store is observed per action rather than per poll. Read off
# time.monotonic() rather than the service's injected clock: this is a real-time
# rate bound, not part of the probe's testable timing contract.
_AUTH_FINGERPRINT_CACHE_SECS = 5.0


# Upper bound on the boot-time baseline seed's store read. The seed sits on the
# gateway startup path ahead of the listeners binding, so a hung store (locked
# SQLite, stalled network filesystem) must cost boot at most this much; on
# timeout the seed refuses and the fail-safe unset-baseline sweep remains.
_SEED_BASELINE_TIMEOUT_SECS = 5.0


async def current_identity_fingerprint(
    self: KiroPrerequisiteService, *, allow_cached: bool = True
) -> str:
    """Return the store's current identity fingerprint.

    Off-loop: a read-only SQLite open plus a couple of small queries.

    ``allow_cached`` decides whether a value read within
    :data:`_AUTH_FINGERPRINT_CACHE_SECS` may be reused. The cache exists for
    ONE caller -- the status surface, which N dashboard tabs poll every few
    seconds, so without it a poll storm becomes one SQLite read and one SEL
    audit event per tab per poll.

    Anything ACTING on the answer passes ``allow_cached=False``. A cached
    value is by definition up to a few seconds old, and a logout inside that
    window would otherwise be invisible to the turn that follows it -- the
    stale child would take the turn under the account just signed out. Turns
    and probes are human-paced, so reading fresh for them costs nothing worth
    having.

    What this replaces either way is the expensive signal: a ``whoami`` spawn,
    which is what the boot-only probe exists to keep off the send path.
    """

    now = time.monotonic()
    if (
        allow_cached
        and self._identity_cache_at
        and now - self._identity_cache_at < _AUTH_FINGERPRINT_CACHE_SECS
    ):
        return self._identity_cache

    def _read() -> tuple[str, bool]:
        # Both the relocation guard and the win32 path resolver stat the
        # filesystem, so the whole resolve-and-read runs in this worker
        # thread and stats stay off the event loop.
        relocated = identity_store_is_relocated(self._platform, self._home, self._environ)
        if relocated:
            # Do not read the default path: with the CLI pointed elsewhere, a
            # leftover database there would fingerprint an account nobody is
            # signed into, and a logout in the real store would change nothing
            # we can see. Absent is never reconciled, so this re-sweeps every
            # turn instead of trusting a stale file.
            if not self._relocation_logged:
                # Once per service: this arm runs on every poll, and without
                # the log an absent-because-relocated host is indistinguishable
                # from a signed-out user -- the silence that made the identity
                # probe's failures undiagnosable without reading source.
                self._relocation_logged = True
                logger.info(
                    "Kiro identity store is env-relocated on %s; reporting the "
                    "identity as absent instead of reading the fixed anchor",
                    self._platform,
                )
            store = _AUTH_FINGERPRINT_ABSENT
        store_definitive: list[bool] = []
        if not relocated:
            store = identity_fingerprint(
                kiro_identity_store_path(self._platform, self._home, self._environ),
                definitive=store_definitive,
            )
        # An API-key host keeps no identity row in the store, so without the
        # key component it reads as signed out on every turn -- see
        # _api_key_identity_claim. But the key may join ONLY a definitive
        # store answer: a child can still authenticate from the store (a
        # harness that strips the key, like KAS), so a key standing in for a
        # store that was unauditable, unreadable or relocated -- or that holds
        # a login no stable claim identifies (a social login) -- would seed a
        # key-only baseline under which a real store account switch compares
        # equal. Such a read withholds ONLY the key component, so the
        # fingerprint is exactly what it was before the key was counted: the
        # store and vault components as read (possibly all absent).
        api_key = _AUTH_FINGERPRINT_ABSENT
        store_is_definitive = bool(store_definitive) and store_definitive[-1]
        key_definitive: list[bool] = []
        if store_is_definitive:
            api_key = _api_key_identity_claim(
                self._environ, self._data_home / ".env", definitive=key_definitive
            )
        # The latch judges every component from the combined string (see
        # _identity_components), so only the combined value is returned.
        fingerprint = _combine_identity_fingerprints(store, _crew_vault_fingerprint(), api_key)
        # Each source reports a read it could not complete as "" too, so an
        # empty value proves a sign-out only when every one of them answered:
        # the store definitively, as the only store the host can have; the
        # key's .env in the claim's own read; the vault at all. Checked only on
        # an empty value, so a signed-in host pays nothing.
        absence_is_definitive = (
            not fingerprint
            and store_is_definitive
            and _store_choice_is_certain(self._platform, self._home)
            and bool(key_definitive)
            and key_definitive[-1]
            and _crew_vault_holds_no_identity()
        )
        return fingerprint, absence_is_definitive

    try:
        fingerprint, absence_is_definitive = await asyncio.to_thread(_read)
    except Exception:
        # An unreadable store reports "no identity", matching
        # identity_fingerprint's own contract, rather than "unchanged" --
        # guessing "unchanged" is what keeps a stale account alive.
        logger.warning("Kiro identity fingerprint could not be read", exc_info=True)
        fingerprint, absence_is_definitive = _AUTH_FINGERPRINT_ABSENT, False
    self._maybe_latch_interim_identity(fingerprint)
    if absence_is_definitive and not self._identity_cache_absence_definitive:
        # A proven sign-out after any other reading starts a new run; see
        # identity_absence_already_swept.
        self._identity_absence_run += 1
    self._identity_cache = fingerprint
    self._identity_cache_absence_definitive = absence_is_definitive
    self._identity_cache_at = now
    return fingerprint


def note_absence_swept(self: KiroPrerequisiteService, run: int) -> None:
    """Record that a sweep under the proven sign-out *run* completed.

    Runs only grow, so the newest one wins: a slower sweep that started under an
    older run cannot undo the record of a newer one.
    """

    self._identity_absence_run_swept = max(self._identity_absence_run_swept, run)


def _maybe_latch_interim_identity(self: KiroPrerequisiteService, fingerprint: str) -> None:
    """Latch when a fresh read observes a DIFFERENT account than the baseline.

    The retirement gate compares the live fingerprint to a baseline, which
    is blind to a round trip: seed A -> switch to B (a child spawns under
    B) -> switch back to A compares equal, and the B-authenticated child
    serves the next turn. Any fresh read that lands DURING the interim
    window -- a status poll (every few seconds per open dashboard tab), a
    turn gate, a readiness probe -- records the observation here, and
    :meth:`identity_changed_since_sessions` reports changed until a sweep
    completes, whatever the store says by then.

    Latching is deliberately refused for every observation that could be a
    TRANSIENT READ FAILURE rather than a different account -- a sticky flag
    armed by a blip would force retire-until-complete sweeps on a healthy
    host, re-creating the perpetual recycle loop the seeded baseline
    exists to remove:

    - no baseline recorded: nothing to differ from (the unset baseline
      already reports changed on its own);
    - every component that differs from the baseline's is ABSENT now: an
      unreadable/relocated store, a vault that failed to read, or a
      sign-out, each indistinguishable from a blip. Real sign-outs are
      still caught by the ordinary (non-sticky) baseline comparison. The
      components -- kiro-cli store, API key, Crew vault -- are judged
      separately, so a key-only or vault-only gateway can still latch, and
      a store blip on a host that also carries a key cannot latch through
      the key's unchanged component.

    A component that APPEARS or CHANGES is never a blip -- reads lose
    components under failure, they do not gain them -- so those latch.
    """

    baseline = self._session_identity
    if baseline is None or fingerprint == baseline:
        return
    # Latch only when some component is PRESENT and differs from the
    # baseline's: reads lose components under failure, they do not gain or
    # alter them, so an appearing or changed component cannot be a blip. A
    # component that is merely ABSENT (an unreadable store, a vault that
    # failed to read) never latches on its own -- the non-sticky baseline
    # comparison still reports it as changed for as long as it persists.
    # Judged per component (store, API key, Crew vault), so a vault-only or
    # key-only gateway can still latch, and a store blip on a host that also
    # carries a key cannot arm the latch through the key's unchanged half.
    if not any(
        now and now != was
        for now, was in zip(_identity_components(fingerprint), _identity_components(baseline))
    ):
        return
    # Bump the generation on EVERY qualifying observation, not only the
    # arming one: an observation landing while the latch is already armed
    # must still postdate a sweep that read the store before it, or the
    # sweep's reconcile would erase it (see note_sessions_reconciled).
    self._interim_identity_gen += 1
    if not self._interim_identity_observed:
        self._interim_identity_observed = True
        logger.info(
            "Interim account observed since the last reconciled baseline; "
            "the next turn gate will sweep even if the store switches back"
        )


async def identity_changed_since_probe(self: KiroPrerequisiteService) -> bool:
    """Whether the signed-in account differs from the one the LATCH describes.

    The status surface's consumer. Advancing this baseline is
    :meth:`_stamp_probe`'s business, so it must never decide
    whether a running child is stale -- see
    :meth:`identity_changed_since_sessions` for why.

    False while ``assume_ready`` holds (a test or offline gateway asserts its
    own readiness and has no store to compare against) and false before the
    first probe, when there is no recorded identity to differ from.
    """

    if self._assume_ready or not self._has_probed:
        return False
    return await self.current_identity_fingerprint() != self._probe_identity


async def identity_changed_since_sessions(self: KiroPrerequisiteService) -> tuple[bool, str]:
    """Whether running children predate the account the store now names.

    Returns ``(changed, live_fingerprint)``.

    This tracks a SEPARATE baseline from :meth:`identity_changed_since_probe`
    on purpose. One shared baseline has two consumers -- the status card and
    session retirement -- and whichever observes the change first advances it,
    so the other never sees it. In practice the dashboard polls status every
    few seconds while turns are minutes apart, so a single baseline means the
    poll re-probes, stamps the new identity, and the stale child is then never
    retired: the cheap half silently consumes the signal the consequential
    half exists to act on. A baseline per consumer removes the coupling.

    An UNSET baseline reports changed. It means we do not know which account
    the running children loaded, and "do not know" must not resolve to "they
    match": readiness is probed a few seconds AFTER boot while a session can be
    spawned eagerly before that, so a logout landing in the gap would otherwise
    be recorded as the starting point and the pre-logout child would keep
    answering as the previous account. Sweeping once when the baseline is unset
    costs one cold start per gateway lifetime -- the conversation is restored
    from disk -- and it is the only answer that cannot be silently wrong.

    The baseline is therefore advanced ONLY by
    :meth:`note_sessions_reconciled`, after a sweep that actually completed.
    """

    if self._assume_ready:
        return (False, _AUTH_FINGERPRINT_ABSENT)
    # Never cached: this answer decides whether a running child may take the
    # next turn, and a value a few seconds old would let a logout inside that
    # window pass unnoticed.
    live = await self.current_identity_fingerprint(allow_cached=False)
    if self._session_identity is None:
        return (True, live)
    if self._interim_identity_observed:
        # Some fresh read since the last complete sweep observed a DIFFERENT
        # account. Even if the store has since switched back (live equals the
        # baseline again), children spawned during the interim window hold
        # that account's credential, so report changed until a sweep
        # completes. See _maybe_latch_interim_identity.
        return (True, live)
    return (live != self._session_identity, live)


def note_sessions_reconciled(
    self: KiroPrerequisiteService, fingerprint: str, *, observations_before: int | None = None
) -> None:
    """Record that running children now match *fingerprint*.

    Advanced ONLY by the retirement path, so no other consumer can retire
    this baseline's signal on its behalf.

    *observations_before* is the :attr:`identity_observation_generation`
    the caller captured right after the gate read that initiated the
    sweep. A COMPLETE sweep retired or flag-marked every kiro-backed child
    that predates that read -- but an observation with a NEWER generation
    names an account the sweep never compared against (a store flap
    observed by a status poll while the sweep ran, after its permits
    reopened cold starts). Clearing the latch then would erase the only
    record that account existed: a child spawned under it inside a
    bracket-read disagreement is unstamped, invisible to the stamp gate,
    and live equals the baseline so no later read re-latches. The latch
    is therefore kept when a newer observation exists, and the next turn
    gate runs one more sweep that covers it. ``None`` (tests, legacy
    callers) clears unconditionally -- the production gate always passes
    the captured generation, pinned by a source scan.
    """

    self._session_identity = fingerprint
    if observations_before is not None and self._interim_identity_gen > observations_before:
        logger.info(
            "Interim account observed while the sweep ran; keeping the "
            "latch so the next turn gate sweeps again"
        )
        return
    # A COMPLETE sweep retired or flag-marked every kiro-backed child, so
    # nothing predating it survives unmarked -- the interim observation is
    # resolved. A child spawned under yet another account AFTER the sweep
    # re-latches on the next fresh read that observes it.
    self._interim_identity_observed = False


async def seed_sessions_baseline(self: KiroPrerequisiteService) -> bool:
    """Adopt the CURRENT store identity as the running-children baseline.

    Called once at gateway startup, BEFORE anything can spawn a kiro-backed
    child. Every child necessarily postdates this read, so the account it
    authenticates under is the one recorded here or a later one -- and a
    later one compares unequal, which is exactly the change
    :meth:`identity_changed_since_sessions` exists to catch. That makes the
    once-per-lifetime unset-baseline sweep unnecessary on a host whose store
    is readable, and removing it matters: on a live gateway that sweep's
    completion precondition (nothing busy, nothing mid-start, no runtime
    surviving) is routinely unsatisfiable -- the sweep retires idle sessions,
    dashboard slots eagerly respawn them, and the in-flight starts keep the
    next sweep incomplete -- so the baseline never advances and every turn
    recycles healthy, seconds-old children forever.

    Returns whether a baseline was recorded. Refuses -- keeping the
    fail-safe unset-baseline behaviour -- when:

    - ``assume_ready`` holds: a test or offline gateway asserts its own
      readiness and has no store to compare against;
    - a baseline is already recorded: a seed must never mask a pending
      change another consumer is still retrying to reconcile;
    - the store cannot be fingerprinted: seeding "" would make "cannot
      tell" the accepted steady state, so every later account switch would
      compare equal to "" and go undetected. Unset instead re-sweeps each
      turn, bounding how long a child can outlive the account it loaded;
    - the store read exceeds :data:`_SEED_BASELINE_TIMEOUT_SECS`: this
      await sits on the gateway boot path ahead of the listeners binding,
      so a hung store must not stall startup indefinitely.

    Known residual, narrowed twice: the interim-identity latch
    (:meth:`_maybe_latch_interim_identity`) catches an A->B->A round trip
    whenever ANY fresh fingerprint read lands during the interim window,
    and the spawn-identity stamp (:meth:`read_spawn_identity` +
    ``flag_identity_stamp_mismatches``) catches the child itself even when
    NO read observed the interim -- the stamp recorded at its spawn still
    names the interim account. What remains is the sub-second gap inside a
    single spawn (see :func:`stamp_spawn_identity`); closing it needs the
    child to report the identity it actually loaded.
    """

    if self._assume_ready or self._session_identity is not None:
        return False
    try:
        # Bounded: this await sits on the gateway boot path ahead of the
        # listeners binding, so a hung store (locked SQLite, stalled
        # network filesystem) must delay startup by at most this deadline,
        # not indefinitely. On timeout, refuse toward the existing
        # fail-safe unset-baseline sweep -- boot proceeds unchanged.
        live = await asyncio.wait_for(
            self.current_identity_fingerprint(allow_cached=False),
            timeout=_SEED_BASELINE_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Identity baseline NOT seeded (store read exceeded %.0fs); "
            "keeping the fail-safe unset-baseline sweep",
            _SEED_BASELINE_TIMEOUT_SECS,
        )
        return False
    if not live:
        logger.info(
            "Identity baseline NOT seeded (store unreadable); keeping the "
            "fail-safe unset-baseline sweep"
        )
        return False
    self._session_identity = live
    logger.info("Identity baseline seeded at startup; boot sweep skipped")
    return True


async def read_spawn_identity(self: KiroPrerequisiteService) -> str:
    """Fresh identity fingerprint for stamping a just-spawned child.

    The spawn-time counterpart of the turn gate's live read: the value it
    returns is recorded on the new provider (``spawn_identity``) so
    ``flag_identity_stamp_mismatches`` can later prove the child loaded a
    different account than the store now names -- including across an
    A->B->A round trip that no poll or turn observed, which neither the
    baseline nor the interim latch can see.

    Uncached on purpose (a cached value predating the spawn would stamp
    the wrong account) and bounded like the boot seed: a spawn already
    takes seconds, but a hung store must not add an unbounded await to
    every cold start. Failure returns the absent sentinel, which
    :func:`stamp_spawn_identity` refuses to record -- an unstamped child
    keeps exactly the pre-stamping protections. The read itself flows
    through the ordinary path, so an interim account it happens to observe
    feeds the latch as any other read would.
    """

    if self._assume_ready:
        return _AUTH_FINGERPRINT_ABSENT
    try:
        return await asyncio.wait_for(
            self.current_identity_fingerprint(allow_cached=False),
            timeout=_SEED_BASELINE_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Spawn identity read exceeded %.0fs; child left unstamped",
            _SEED_BASELINE_TIMEOUT_SECS,
        )
        return _AUTH_FINGERPRINT_ABSENT
