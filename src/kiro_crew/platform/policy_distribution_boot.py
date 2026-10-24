"""Establishing the central ceiling at boot, and the checks every installation shares.

Parsing and verifying one fetched document, then resolving the tier: the cache-first
shortcut, the fetch window, :func:`fetch_once`, the hand-off when another process wins the
cache write, the outage fallback and the ``on_unavailable`` disposition. Also the gate a
candidate ceiling passes before it is installed (:func:`validate_ceiling`) and the
sanitiser every message of the tier goes through (:func:`_sanitize_detail`).

:mod:`kiro_crew.platform.policy_distribution` is the import path and the patch surface:
callers import these names from it and tests patch them there, and its mirror
(:mod:`kiro_crew._facade_mirror`) carries such a patch to this module. This module never
imports the facade.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Optional, Tuple

from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import (
    MIN_REFRESH_INTERVAL_SECS,
    SIGNATURE_VERIFIED,
    UNAVAILABLE_DEGRADE,
    GovernanceCeiling,
    PolicyDistribution,
)
from kiro_crew.platform.governance_health import mark_governance_incident
from kiro_crew.platform.policy_distribution_cache import (
    _CACHE_DOC_LEAF,
    _CACHE_META_LEAF,
    CachedPolicy,
    _cache_pair,
    _cache_write_lock,
    _claim_fetch_slot,
    _installed_digest,
    _record_installed,
    _source_digest,
    cache_dir,
    read_cache,
    read_cache_meta,
    touch_cache,
    write_cache,
)
from kiro_crew.platform.policy_distribution_source import (
    POLICY_UNAVAILABLE_ENV,
    POLICY_URL_ENV,
    FetchedPolicy,
    FetchRequest,
    _body_digest,
    _fetcher_for,
    _is_enterprise,
    _split_url,
    cache_only,
    host_pinned_source,
    request_headers,
    resolve_distribution,
)

logger = logging.getLogger("kiro_crew.platform.policy_distribution")


# ──────────────────────────────────────────────────────────────────────────
# Parse + verify one fetched document
# ──────────────────────────────────────────────────────────────────────────


def parse_distributed_policy(body: bytes, *, source: str) -> GovernanceCeiling:
    """Turn fetched bytes into a verified ceiling, or raise.

    Raises ``PlatformCompositionError`` on anything that makes the document
    unusable: not JSON, not an object, not a valid policy, or — when provenance is
    mandated — not signed by a trusted issuer.  Every caller treats a raise as "do
    not adopt this document", which at boot means the unavailable disposition and
    on a live refresh means keeping the ceiling already installed.
    """
    from kiro_crew.platform.governance import (
        _policy_signature_required,
        _verify_policy_signature,
        parse_policy,
    )

    # One safe label for every mention of the source below, computed once. The signature
    # verifier records its label in the SEL audit trail, which is on the keystone and so
    # not agent-reachable — but a label reaching this module is a URL that may BE the
    # credential, and an audit record is not a place to put one either. Redacting here,
    # rather than teaching the verifier about URLs, keeps the rule where the URL is known;
    # for the file tiers the same parameter is the operator's own path and belongs in the
    # record as-is.
    label = _redact_source(source, source) if source else "the central policy source"

    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PlatformCompositionError(
            f"policy fetched from {label} is not valid UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PlatformCompositionError(f"policy fetched from {label} is not a JSON object")

    state = _verify_policy_signature(data, source=label)
    ceiling = parse_policy(data, signature_state=state)
    # Mandated provenance is enforced HERE as well as at the shared boot gate, so a
    # refused document never reaches the cache. The opt-in is ``require_policy_signature``
    # in the admission policy — the operator-controlled trust root that is on the
    # keystone. Deliberately NOT a key in the fetched document: a document must not be
    # the authority on whether it has to be authentic, or an attacker rewriting it
    # would simply clear the flag. The state test comes first because it is free,
    # while the opt-in reads a file.
    if state != SIGNATURE_VERIFIED and _policy_signature_required():
        raise PlatformCompositionError(
            f"policy fetched from {label} is {state}, but require_policy_signature is "
            "set in the admission policy; sign the published document and provision the "
            "issuer's trust key there"
        )
    return ceiling


# ──────────────────────────────────────────────────────────────────────────
# The boot tier
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _LoadOutcome:
    ceiling: Optional[GovernanceCeiling] = None
    #: Why no ceiling was produced, for the caller's log/incident.  Empty when
    #: distribution simply is not configured.
    reason: str = ""


def load_distributed_policy(
    declared: Optional[PolicyDistribution] = None,
) -> Optional[GovernanceCeiling]:
    """The tier: resolve the source, then serve the cache or fetch.

    Returns ``None`` when central distribution is not configured, or when it is
    configured, could not be established, and ``on_unavailable`` is ``degrade`` —
    in both cases the caller falls through to the next precedence tier.  Raises
    ``PlatformCompositionError`` when it could not be established under the
    ``fail_closed`` default, which aborts boot.

    Order within the tier is cache-first-when-fresh, and that is a boot-latency
    decision as much as an availability one: a warm host must not make startup wait
    on a network round trip it is about to make again in the background anyway.

    **This function is not called only at boot**, which is what the fetch window
    below exists for. ``load_security_policy`` is also re-run per app callback by
    ``mcp_gateway/app_call.py``, so without a bound an open dashboard would put one
    network round trip behind every app call. The window makes that at most one
    fetch per :func:`_fetch_window` seconds, from all callers combined.
    """
    dist = resolve_distribution(declared)
    if cache_only():
        return _load_cache_only(dist)
    if not dist.enabled:
        return None

    cached = read_cache()
    # A cache recorded against a DIFFERENT source is not this source's
    # last-known-good. Serving it would let a retired endpoint keep governing a
    # host that has been repointed — including a redirect made specifically to
    # replace a compromised source.
    if cached is not None and cached.source_digest != _source_digest(dist.source):
        logger.info("the cached ceiling came from a different source; ignoring it after a repoint")
        cached = None

    window = _fetch_window(dist)
    # A zero ``refresh_interval_secs`` means BOOT-ONLY, and it has to mean that on this
    # path too. ``_fetch_window`` floors the window at ``MIN_REFRESH_INTERVAL_SECS`` so a
    # zero never reads as "fetch on every call", but a floor is still a cadence: the
    # per-app-call re-read above would go back to the source every 60s, so an operator
    # who asked to freeze the ceiling for the process lifetime got a poller anyway — one
    # that could hand an app callback a LOOSENED document while the gateway kept the
    # ceiling it booted on. Once this process has established a central ceiling, a
    # boot-only source is served from the cache and never re-fetched.
    #
    # Gated on ``central_ceiling_installed()``, so boot's own first call still fetches
    # (nothing is installed yet), and a boot that degraded without establishing one
    # retries rather than freezing on a ceiling it never had. ``max_cache_age_secs`` is a
    # separate bound and still applies below, so this is not "trust bytes of any age".
    # ``refresh_now`` does not come through here, so `kirocrew policy fetch` remains the
    # operator's explicit lever in this mode.
    boot_only = dist.effective_refresh_interval() == 0 and central_ceiling_installed()
    fresh_enough = (
        cached is not None
        and (boot_only or cached.age_secs() < window)
        # Both declarations, tightest-wins: a bound set only in the published document
        # would otherwise be dropped at every restart, which is the case it exists for.
        and not _cache_is_too_old(dist, cached)
    )
    if fresh_enough and cached is not None:
        try:
            ceiling = parse_distributed_policy(cached.body, source=dist.source)
            # Record it: a ceiling served from the cache is still a ceiling this tier
            # installed, and the 304 comparison in ``refresh_now`` reads an EMPTY digest
            # as "we do not know what is running" — so without this a cache-booted
            # gateway would never notice another process advancing the cache.
            _record_installed(cached.body)
            return ceiling
        except PlatformCompositionError as exc:
            # The cache is unusable, so it is not a shortcut. Fall through to a
            # live fetch rather than failing: the published document may well be
            # fine and this copy merely stale-and-broken.
            #
            # Sanitised, and deliberately NOT `exc_info=True`: a traceback prints the
            # exception message, and a parse refusal names the source it refused --
            # which for a pre-signed URL IS the credential. The log ring is served by
            # `GET /api/logs`, so this is the same surface every other message in this
            # module is redacted for.
            logger.warning(
                "the cached ceiling is unusable; fetching: %s",
                _sanitize_detail(str(exc), dist.source),
            )

    if not _claim_fetch_slot(window):
        # Another caller already tried within this window and we have no fresh cache
        # to shortcut on, so the answer is whatever the cache can still offer. Going
        # to the network again would not produce a different result — the source was
        # just consulted — and on the per-app-call path it would produce one request
        # per call. ``_from_cache_on_outage`` applies the staleness bound, and a
        # ``None`` from it lands on the ordinary unavailable disposition.
        reason = _sanitize_detail(
            f"a fetch of {dist.source} was already attempted within the last {window}s",
            dist.source,
        )
        throttled = _from_cache_on_outage(dist, cached, reason)
        if throttled is not None:
            return throttled
        _unavailable(dist, reason)
        return None

    outcome = _load_by_fetch(dist, cached)
    if outcome.ceiling is not None:
        return outcome.ceiling
    _unavailable(dist, outcome.reason)  # raises under the fail-closed default
    return None


def _load_cache_only(dist: PolicyDistribution) -> GovernanceCeiling:
    """The ceiling from the cache alone, for a child that may not fetch.

    **Fails closed rather than falling through.**  The parent only puts a child in this
    mode when its OWN ceiling came from this tier
    (:func:`central_ceiling_installed`) — so an absent, stale or unusable cache here is
    not "this host has no central policy", it is "the parent had one and could not pass
    it on", which a successful fetch with a failed cache WRITE produces. Falling through
    would then start arbitrary third-party code under a local or absent ceiling on a
    host the administrator governs, and it would do it silently. Refusing is loud, and
    the failure is a disabled app rather than an unbounded one.
    """
    cached = read_cache()
    if (
        cached is not None
        and _is_enterprise()
        and cached.source_digest != _source_digest(dist.source)
    ):
        # An enterprise build has exactly one source (RFC D2), so a cache recorded
        # against any other address is not the fleet's ceiling, even for a child.
        logger.info("the cached ceiling came from a different source; ignoring it")
        cached = None
    if cached is None:
        raise PlatformCompositionError(
            "cache-only policy resolution found no cached ceiling. The gateway is "
            "centrally governed, so this child cannot resolve the fleet policy and "
            "refuses to run under a local one. Check that the gateway could write "
            f"{cache_dir()}."
        )
    age = cached.age_secs()
    if _cache_is_too_old(dist, cached):
        bound = dist.max_cache_age_secs or _cached_max_cache_age(cached.body)
        raise PlatformCompositionError(
            f"the cached ceiling is {int(age)}s old, past the {bound}s "
            "staleness bound this fleet set; refusing to run a child under it."
        )
    try:
        # No source: this child was deliberately not given one, and the metadata records
        # only a digest. The parameter is used for error text, so "" is the honest value.
        ceiling = parse_distributed_policy(cached.body, source="")
    except PlatformCompositionError as exc:
        raise PlatformCompositionError(
            f"the cached ceiling is unusable in cache-only mode: {exc}"
        ) from exc
    _record_installed(cached.body)
    return ceiling


def effective_refresh_interval() -> int:
    """The poll cadence in force, or 0 for a boot-only source.

    Exists for the CLI, which has to tell an operator WHEN a running gateway will pick up
    what it just published: on the next cycle if there is one, at next start if there is
    not. ``refresh_now`` installs the ceiling in the calling process, and for
    ``kirocrew policy fetch`` that process exits immediately — so "applied" without a
    qualifier would claim something the command cannot do.
    """
    from kiro_crew.platform.governance import active_policy_distribution

    try:
        return resolve_distribution(active_policy_distribution()).effective_refresh_interval()
    except PlatformCompositionError:
        return 0


def effective_max_cache_age() -> int:
    """The staleness bound in force, wherever it was declared.

    A cache-only child is handed this rather than the raw environment variable, because
    the bound is just as likely to come from the FETCHED document's ``distribution``
    block — and reading only the env var would give the child no bound at all on a fleet
    that set one in the policy, so it would accept an arbitrarily stale ceiling.
    """
    from kiro_crew.platform.governance import active_policy_distribution

    try:
        return resolve_distribution(active_policy_distribution()).max_cache_age_secs
    except PlatformCompositionError:
        return 0


def central_ceiling_installed() -> bool:
    """Did THIS process's ceiling come from the central tier?

    The gateway asks before putting a child in cache-only mode. It is the difference
    between "the fleet policy is what governs here, pass it on" and "this host is
    running on a local tier or ungoverned", and only the first is a state a child should
    refuse to start without.
    """
    return bool(_installed_digest())


def _env_pins_the_source() -> bool:
    """Is the address coming from the host (env, or an enterprise build) rather than a document?

    When it is, a document's own ``distribution.source`` is already ignored:
    :func:`resolve_distribution` lets the environment win per setting, precisely so
    whatever provisions the host owns the address. So a declaration that differs is not a
    migration this host would follow, and refusing the document over it would break the
    ordinary canary — one host pinned elsewhere by env while the fleet publishes its
    canonical address. An enterprise build always pins: its source is a build-time
    constant (RFC D2), so no document can move it.
    """
    return bool(host_pinned_source())


def _declared_source(body: bytes) -> str:
    """The ``distribution.source`` a document declares for itself, or ``""``.

    Read straight from the JSON for the reason :func:`_cached_max_cache_age` gives: this
    runs before the document has been validated, so anything unreadable or not a non-empty
    string means "declares no source of its own" and the parse that follows reports it.
    """
    try:
        data = json.loads(body.decode("utf-8"))
        declared = data["distribution"]["source"]
    except Exception:
        return ""
    return declared.strip() if isinstance(declared, str) else ""


def _cached_max_cache_age(body: bytes) -> int:
    """A ``max_cache_age_secs`` the CACHED document declares for itself, or 0.

    The bound is just as likely to live in the published policy as in the bootstrap
    declaration -- ``effective_max_cache_age`` already assumes so for the child it hands
    the value to. But at BOOT the resolved ``PolicyDistribution`` comes from the env or a
    lower tier, so a fleet that set the bound only in its own document had it applied
    while the process ran and then silently dropped on the next restart: exactly the
    restart-during-an-outage case the bound exists for, where an expired ceiling would be
    admitted instead of refused.

    Read straight from the JSON rather than through ``parse_policy``: this runs before the
    document has been validated, and the alternative is either parsing twice or letting a
    malformed cache raise from inside a staleness pre-check. Anything unreadable or not a
    positive whole number yields 0 -- "declares no bound of its own" -- so a broken cache
    is refused by the parse that follows, not by this.
    """
    try:
        data = json.loads(body.decode("utf-8"))
        declared = data["distribution"]["max_cache_age_secs"]
    except Exception:
        return 0
    if isinstance(declared, bool) or not isinstance(declared, (int, float)):
        return 0
    # ``isfinite`` only for a float: it converts its argument to a float first, so a JSON
    # integer of 310 digits would raise OverflowError out of a pre-check whose whole
    # contract is "anything unusable yields 0". The ``try`` above does not cover this line.
    if isinstance(declared, float) and not math.isfinite(declared):
        return 0
    if declared != int(declared) or declared <= 0:
        return 0
    return int(declared)


def _cache_is_too_old(dist: PolicyDistribution, cached: "CachedPolicy") -> bool:
    """Whether *cached* has outlived the staleness bound, from EITHER declaration.

    Tightest-wins, matching the governance model's own rule: a bound in the cached
    document binds even when the bootstrap declaration has none, and a tighter bootstrap
    bound is not loosened by the document.
    """
    age = cached.age_secs()
    if dist.cache_too_old(age):
        return True
    own = _cached_max_cache_age(cached.body)
    return bool(own) and age > own


def _fetch_window(dist: PolicyDistribution) -> int:
    """How long a fetch's result stands before this tier will go out again.

    The fleet's own cadence when it set one; otherwise
    :data:`MIN_REFRESH_INTERVAL_SECS`.  A source with no ``refresh_interval_secs``
    means "establish the ceiling at boot", not "never cache and never bound" — and
    an unbounded reading is unsafe here rather than merely slow, because this tier
    is on the per-app-call reload path. Sixty seconds keeps a boot-only
    configuration behaving like one (boot's first call fetches) while bounding every
    later caller.

    Deliberately the SAME value for the cache shortcut and the fetch cooldown, so
    there is one window to reason about instead of two that can disagree.
    """
    return dist.effective_refresh_interval() or MIN_REFRESH_INTERVAL_SECS


def _load_by_fetch(dist: PolicyDistribution, cached: Optional[CachedPolicy]) -> _LoadOutcome:
    """Fetch, and on failure fall back to whatever the cache can still offer."""
    # Observed BEFORE the fetch, for the same reason and with the same ordering the live
    # refresh uses: the concurrent publish this guards against lands DURING the fetch, so a
    # snapshot taken afterwards already includes it. Boot is not exempt — a slow fetch of v2
    # racing a `policy fetch --force` that publishes v3 would otherwise overwrite the cache
    # with v2 and install it, rolling the ceiling BACKWARD to a possibly looser document on
    # the one code path where nothing is running yet to notice.
    expect_pair = _cache_pair(read_cache_meta())
    try:
        fetched = fetch_once(dist, cached)
    except PlatformCompositionError as exc:
        # A configuration error (unknown scheme, refused transport, an empty
        # document) is not transient. It must not be papered over by a cache,
        # because the operator would then never learn that the source they
        # configured is unusable.
        #
        # Re-raised through the sanitiser rather than verbatim: these messages are
        # built by the fetchers (and, through the seam, by an edition's transport) and
        # name the source, and a boot abort's text reaches stderr and any supervisor
        # that captures it. The module's rule is that the URL is emitted nowhere, and
        # a re-raise is not an exception to it.
        raise PlatformCompositionError(_sanitize_detail(str(exc), dist.source)) from exc
    except Exception as exc:
        # ``_sanitize_detail``, not ``_redact_source``: the exception text is the
        # ENDPOINT's, and an error page or a proxy echoing the request can carry the
        # Authorization header back. Redacting only the configured source would print it.
        reason = _sanitize_detail(
            f"could not fetch the ceiling from {dist.source}: {exc}", dist.source
        )
        logger.warning("%s", reason)
        return _LoadOutcome(ceiling=_from_cache_on_outage(dist, cached, reason), reason=reason)

    if fetched.not_modified:
        if cached is None:
            # A fetcher answered "unchanged" against validators we never sent.
            # Treating that as success would adopt nothing at all.
            return _LoadOutcome(
                reason=_sanitize_detail(
                    f"{dist.source} reported no change but nothing is cached", dist.source
                )
            )
        body, provenance = cached.body, "the re-validated cached copy"
    else:
        body, provenance = fetched.body, dist.source

    try:
        ceiling = parse_distributed_policy(body, source=dist.source)
    except PlatformCompositionError as exc:
        reason = _sanitize_detail(
            f"the ceiling published at {dist.source} is unusable: {exc}", dist.source
        )
        logger.error("%s", reason)
        # Do NOT cache a document we refused: a poisoned last-known-good would
        # outlive the bad push and keep this host failing after it was corrected.
        #
        # The cache is still an acceptable answer — it is a ceiling the fleet
        # published and this host verified. What is NOT acceptable is falling through
        # to a LOWER tier: ``on_unavailable`` answers "the ceiling could not be
        # REACHED", and a document we reached and REFUSED is a different question. An
        # invalid or signature-refused push must not quietly demote the host onto a
        # policy the administrator superseded, so with no usable cache this raises
        # regardless of the disposition.
        salvaged = _from_cache_on_outage(dist, cached, reason)
        if salvaged is not None:
            return _LoadOutcome(ceiling=salvaged)
        raise PlatformCompositionError(
            f"{reason}. Refusing to fall back to a lower policy tier: the document was "
            "read and refused, which is not an availability failure. Fix the published "
            f"document, or unset {POLICY_URL_ENV} to stop following it."
        ) from exc

    # The SAME two gates the live-refresh path applies, for the same reasons, so the two
    # paths agree on what is installable. Without them boot would cache and install a
    # document ``refresh_now`` refuses — and then the refresher would reject it on every
    # cycle for the lifetime of the process, logging a rejection forever while the host ran
    # on it.
    #
    # Handled exactly like the parse refusal above: salvage from the cache if it can still
    # serve, else raise. A document read and REFUSED is not an availability failure, so it
    # must not quietly demote the host onto a lower tier — and it is not cached either, so
    # a corrected push is not shadowed by a poisoned last-known-good.
    migrated = _declared_source(body)
    refusal = ""
    if migrated and migrated != dist.source and not _env_pins_the_source():
        refusal = (
            "the document published for this host moves distribution.source to a different "
            f"address. Migrating the source is a bootstrap change: repoint {POLICY_URL_ENV} "
            "(or the placed policy's distribution.source) instead, then publish."
        )
    else:
        try:
            validate_ceiling(ceiling)
        except PlatformCompositionError as exc:
            refusal = _sanitize_detail(
                f"the ceiling published at {dist.source} does not compose on this host: {exc}",
                dist.source,
            )
    if refusal:
        logger.error("%s", refusal)
        salvaged = _from_cache_on_outage(dist, cached, refusal)
        if salvaged is not None:
            return _LoadOutcome(ceiling=salvaged)
        raise PlatformCompositionError(refusal)

    # NOT recorded yet. ``_record_installed`` is what ``central_ceiling_installed`` answers
    # from, and the gateway flags an app backend cache-only on that answer — so recording
    # these bytes before the publish resolves would, on a lost swap whose winner is then
    # REJECTED, leave the host degraded while every child was told to resolve its ceiling from
    # a cache holding the document this host just refused. It is recorded at the end, once the
    # ceiling this function returns is the one that will govern.
    #
    # The cache is written only after the document proved usable, on both branches:
    # a 304 restarts the cached copy's age (it IS the published document right now,
    # so a stable policy must not trip a staleness bound for having confirmed that),
    # and new bytes become the new last-known-good.
    if fetched.not_modified and cached is not None:
        touch_cache(cached.meta(), etag=fetched.etag, last_modified=fetched.last_modified)
    elif not fetched.not_modified:
        published = write_cache(
            body,
            source=dist.source,
            etag=fetched.etag,
            last_modified=fetched.last_modified,
            expect_pair=expect_pair,
        )
        if not published and _cache_pair(read_cache_meta()) != expect_pair:
            # The swap lost: another writer published while this fetch was in flight, and
            # what is on disk now may be NEWER than what we hold. Boot must install
            # something, and the right something is the winner — so adopt the cache rather
            # than the bytes we fetched. It is re-gated rather than trusted, and adopted
            # WITHOUT a further write: the winner is already the cache, so re-publishing it
            # would be the overwrite this path exists to avoid. Not a retry loop — one
            # straight-line handoff — so a third writer arriving cannot spin it.
            logger.info(
                "another process published a newer ceiling during this boot fetch; adopting "
                "the cache instead of what was fetched"
            )
            winner = read_cache()
            if winner is not None:
                return _load_from_cached_winner(dist, winner)
        if not published:
            # ``write_cache`` is best-effort for the GATEWAY -- it is governed by what it
            # just fetched either way -- but a cache-only app backend resolves its ceiling
            # FROM this file. So an unpublished tightening leaves a stale, LOOSER document
            # as what every child spawned afterwards adopts: the exact failure the
            # cache-only handoff exists to prevent, arrived at by a swallowed write error.
            #
            # A stale pair that disagrees with what is being installed is therefore removed
            # rather than left. A child then finds no cache and fails CLOSED (a disabled
            # app, loudly) instead of starting under a ceiling the administrator
            # superseded. If it cannot be removed either -- the usual reason the write
            # failed in the first place -- the invariant cannot be established at all, and
            # that is a boot refusal rather than something to log and continue past.
            _discard_disagreeing_cache(body)
    _record_installed(body)
    logger.info("governance ceiling loaded from %s", _redact_source(provenance, dist.source))
    return _LoadOutcome(ceiling=ceiling)


def _load_from_cached_winner(dist: PolicyDistribution, winner: CachedPolicy) -> _LoadOutcome:
    """Adopt a cache another process published while this boot was fetching.

    Runs the winner through the SAME gates the fetched document goes through — parse, the
    source-migration refusal, ``validate_ceiling`` — because "someone else published it" is
    not evidence this host can run under it. A refusal here reports itself and leaves the
    caller on the ordinary unavailable disposition rather than installing a document this
    host rejects.

    Does not write the cache: the winner is already the cache, and re-publishing it would
    be the very overwrite this path exists to avoid.
    """
    try:
        # The REPOINT rule as well, which the CAS loss is exactly what makes reachable: the
        # winner was published by another process, and that process may have been configured
        # for a different source. Re-gating parse, migration and composition but not
        # provenance would let source B's ceiling govern a host pointed at source A.
        if winner.source_digest != _source_digest(dist.source):
            raise PlatformCompositionError(
                "it was cached against a different source, so it is not this host's "
                "last-known-good"
            )
        ceiling = parse_distributed_policy(winner.body, source=dist.source)
        migrated = _declared_source(winner.body)
        if migrated and migrated != dist.source and not _env_pins_the_source():
            raise PlatformCompositionError(
                "it moves distribution.source to a different address; migrating the source "
                f"is a bootstrap change ({POLICY_URL_ENV} or the placed policy)"
            )
        validate_ceiling(ceiling)
    except PlatformCompositionError as exc:
        reason = _sanitize_detail(
            f"the ceiling another process cached from {dist.source} is unusable: {exc}",
            dist.source,
        )
        logger.error("%s", reason)
        return _LoadOutcome(reason=reason)
    _record_installed(winner.body)
    logger.info("governance ceiling loaded from the cache another process just published")
    return _LoadOutcome(ceiling=ceiling)


def _discard_disagreeing_cache(installed: bytes) -> None:
    """Remove a cached pair that is not the document being installed.

    Called only when publishing *installed* failed. Raises ``PlatformCompositionError``
    if a disagreeing pair survives, because at that point nothing can make the cache
    agree with the ceiling this process is adopting, and a cache-only child would
    silently adopt the older one.

    A pair that already AGREES is left alone: an equal document is not stale, and the
    write may simply have been redundant. So is an ABSENT one: with nothing cached there
    is nothing a child could adopt instead, so a failed write costs the outage fallback
    and nothing else.
    """
    directory = cache_dir()
    digest = _body_digest(installed)
    # Cheap pre-check before taking a lock, which on a host that has never cached
    # anything would have to CREATE the directory and the lock file just to discover
    # there is nothing to remove. Re-read authoritatively under the lock below, so a
    # writer arriving in between is still seen.
    meta = read_cache_meta()
    if meta is None or meta.digest == digest:
        return
    try:
        with _cache_write_lock(directory):
            meta = read_cache_meta()
            if meta is None or meta.digest == digest:
                return
            logger.warning(
                "could not publish the fetched ceiling to the policy cache; removing the "
                "stale cached copy so an app backend cannot adopt it instead"
            )
            for leaf in (_CACHE_DOC_LEAF, _CACHE_META_LEAF):
                (directory / leaf).unlink(missing_ok=True)
            remaining = read_cache_meta()
            if remaining is not None and remaining.digest != digest:
                raise PlatformCompositionError("the stale cached ceiling could not be removed")
    except PlatformCompositionError:
        raise
    except Exception as exc:
        raise PlatformCompositionError(
            "the fetched ceiling could not be published to the policy cache and the stale "
            f"cached copy could not be removed ({exc}); a cache-only app backend would "
            f"adopt the superseded ceiling. Check that the gateway can write {cache_dir()}."
        ) from exc


def _from_cache_on_outage(
    dist: PolicyDistribution, cached: Optional[CachedPolicy], reason: str
) -> Optional[GovernanceCeiling]:
    """The last-known-good copy, when it is still an acceptable answer.

    This is the property that makes a central ceiling safe to depend on: a host
    keeps the ceiling it was last given rather than losing governance because a
    bucket had a bad minute.  It stops being acceptable in exactly two cases — no
    cache at all, and a cache past the fleet's ``max_cache_age_secs`` — and both
    hand the decision to ``on_unavailable``.
    """
    if cached is None:
        return None
    age = cached.age_secs()
    if _cache_is_too_old(dist, cached):
        logger.error(
            "the cached ceiling is %.0fs old, past the %ss bound, and %s",
            age,
            dist.max_cache_age_secs or _cached_max_cache_age(cached.body),
            reason,
        )
        return None
    try:
        ceiling = parse_distributed_policy(cached.body, source=dist.source)
    except PlatformCompositionError as exc:
        # Sanitised rather than a traceback, for the reason the loader's sibling arm gives.
        logger.error(
            "the cached ceiling is also unusable: %s", _sanitize_detail(str(exc), dist.source)
        )
        return None
    _record_installed(cached.body)
    mark_governance_incident("degraded", f"policy_distribution:cache:{int(age)}s")
    logger.warning("serving the cached governance ceiling (%.0fs old) because %s", age, reason)
    return ceiling


def _unavailable(dist: PolicyDistribution, reason: str) -> None:
    """Apply ``on_unavailable`` when no ceiling could be established."""
    detail = reason or _sanitize_detail(
        f"the ceiling at {dist.source} could not be established", dist.source
    )
    if dist.on_unavailable == UNAVAILABLE_DEGRADE:
        mark_governance_incident("degraded", "policy_distribution:unavailable")
        logger.error(
            "central policy distribution is configured but unusable (%s); falling "
            "through to the next policy tier as on_unavailable=%s",
            detail,
            UNAVAILABLE_DEGRADE,
        )
        return None
    mark_governance_incident("failed_closed", "policy_distribution:unavailable")
    if _is_enterprise():
        # No local lever to suggest: an enterprise build requires its central policy.
        raise PlatformCompositionError(
            f"this enterprise build requires its central policy, and no ceiling could be "
            f"established ({detail}). Refusing to start."
        )
    raise PlatformCompositionError(
        f"central policy distribution is configured but no ceiling could be "
        f"established ({detail}). Refusing to run ungoverned. Set "
        f"{POLICY_UNAVAILABLE_ENV}={UNAVAILABLE_DEGRADE} to fall through instead, "
        f"or unset {POLICY_URL_ENV} to stop fetching centrally."
    )


def fetch_once(dist: PolicyDistribution, cached: Optional[CachedPolicy] = None) -> FetchedPolicy:
    """One conditional fetch through the registered transport for *dist.source*."""
    fetcher = _fetcher_for(dist.source)
    result = fetcher(
        FetchRequest(
            url=dist.source,
            timeout_secs=dist.effective_timeout(),
            headers=request_headers(),
            etag=cached.etag if cached else "",
            last_modified=cached.last_modified if cached else "",
        )
    )
    if not result.not_modified and not result.body:
        raise PlatformCompositionError(f"policy source {dist.source!r} returned an empty document")
    return result


#: A request-header value shorter than this is not substituted out of a message. It is not a
#: credential at that length, and replacing a 1-3 character string would corrupt every
#: message that happened to contain those characters — the redaction would do more damage
#: than the leak it was guarding against.
_MIN_SUBSTITUTABLE_SECRET = 8


def _request_header_secrets() -> Tuple[str, ...]:
    """The exact header VALUES this host sends, longest first.  Never raises.

    Longest first so a value that is a prefix of another does not leave the remainder
    behind. Never raises because this feeds the sanitiser, and a sanitiser that can fail is
    one that stops sanitising at the worst moment: ``request_headers`` refuses malformed
    JSON by design, and that refusal must not become an exception on a logging path.
    """
    try:
        values = [v for v in request_headers().values() if len(v) >= _MIN_SUBSTITUTABLE_SECRET]
    except Exception:
        return ()
    return tuple(sorted(set(values), key=len, reverse=True))


def _sanitize_detail(text: str, source: str) -> str:
    """Make *text* safe for the CLI and the log ring.

    Three passes, because there are three different things in here that must not be
    published, from three different authors:

    * the SOURCE, which the operator configured — :func:`_redact_source`;
    * the request CREDENTIAL, which the operator also configured, substituted by exact
      value. ``security.redact`` below recognises credential SHAPES, and
      ``KIROCREW_POLICY_HEADERS`` is deliberately arbitrary: an ``X-Fleet-Key`` holding an
      opaque blob matches no pattern anyone can write, so pattern matching cannot reach it.
      What makes this tractable is that the value is not a pattern to us — we sent it, so
      we can substitute the string itself, which is strictly stronger than any shape rule
      for exactly the values at risk;
    * anything the ENDPOINT put in the message. A malformed document reaches the CLI
      through a parser error, and ``json``'s errors quote the offending text — so a
      document that echoes back the request's ``Authorization`` header (an endpoint
      returning its own request, a misconfigured proxy's error page) would carry that
      credential into ``kirocrew policy fetch`` and into ``GET /api/logs``. The bytes are
      not ours, so the shared credential + exfiltration-URL chain applies to them, the
      same way every other stderr-bearing surface in this package handles text it did
      not author.
    """
    from kiro_crew import security

    text = _redact_source(text, source)
    for secret in _request_header_secrets():
        text = text.replace(secret, "[REDACTED: policy request header]")
    return security.redact(text)


def _redact_source(text: str, source: str) -> str:
    """Replace the source URL (and its bare hostname) in *text* with its scheme.

    **This module emits the source URL nowhere.**  One rule rather than a per-surface
    judgement, because the surfaces are not as separable as they look: the log ring is
    served by ``GET /api/logs`` and rendered in the dashboard, which the agent's own
    browser tooling can drive, so "it is only a log line" is not a boundary here. The
    operator configured the endpoint and does not need it echoed; the scheme plus the
    error text is what diagnoses a failure, and ``kirocrew policy source`` reports the
    scheme by design.

    Redacting the STRING rather than rewriting each message is deliberate. These
    messages are assembled from exceptions raised all over this module and, through the
    fetcher seam, from an edition's own transport, so a per-message rule would be one
    someone has to remember; a substring replacement cannot be forgotten. It is a
    defence-in-depth measure over the messages themselves, not a substitute for them
    being careful.
    """
    if not source:
        return text
    parts = _split_url(source)
    scheme = parts.scheme or "unknown"
    placeholder = f"<the {scheme} policy source>"
    text = text.replace(source, placeholder)
    # The HOSTNAME on its own, because plenty of transport errors never quote the
    # whole URL: a TLS mismatch says "hostname 'config.corp.example' doesn't match",
    # and a DNS failure names the host alone. Replacing only the full source would
    # miss both and print the endpoint anyway. Done second so a message carrying the
    # complete URL still gets the more informative substitution.
    host = parts.hostname or ""
    if host:
        text = text.replace(host, placeholder)
    return text


def validate_ceiling(ceiling: GovernanceCeiling) -> None:
    """Everything :func:`apply_ceiling` checks, without installing anything.

    Split out so a refresh can prove a candidate BEFORE it publishes the cache: the
    cache is what a concurrently-starting cache-only child adopts, so writing a document
    this host would refuse would hand that child a ceiling the gateway rejected.
    """
    from kiro_crew.platform.governance import assert_policy_signature_satisfied
    from kiro_crew.platform.governance_profiles import assert_profile_floor

    assert_policy_signature_satisfied(ceiling)
    assert_profile_floor(ceiling)
