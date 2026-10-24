"""Central distribution of the Level-1 governance ceiling.

An enterprise IT admin owns **one** ``security_policy.json`` and every machine in
the fleet follows it: each host fetches the document from a central location,
keeps the last-known-good copy on disk, and re-fetches on an interval so a pushed
change binds without a restart, a redeploy, or a visit to the host.  This module
is that engine.  ``governance.PolicyDistribution`` is the parsed declaration;
``governance.load_security_policy`` calls in here for the fetch tier.

Where the source comes from
---------------------------

Two channels, and the split is deliberate:

* ``KIROCREW_POLICY_URL`` (plus the ``KIROCREW_POLICY_*`` siblings below) — the
  **fleet lever**, the same role ``KIROCREW_SECURITY_POLICY`` already plays.  A
  config-management push (Jamf / Intune / Ansible / Chef / Puppet) sets one
  variable and the host is centrally governed with no file to place and no
  package to rebuild.  It also carries the per-machine request credential, which
  a published document must not.
* ``distribution`` inside a policy that some LOWER tier already supplies — the
  **self-refresh** channel.  A fleet places one small bootstrap policy once (or
  an edition bundles it), and that document names where its own successors come
  from.  This is what makes "push a change to every instance" a property of the
  policy rather than of whatever placed it.

The env channel wins per setting, so a host can be redirected or have its
refresh interval changed without editing a signed document.

Precedence, and why this tier sits where it does
------------------------------------------------

``load_security_policy`` resolves, highest first.  This tier is the AUTHORITY;
every tier below it may only **tighten** it:

1. **this tier** — the centrally-distributed document.
2. ``KIROCREW_SECURITY_POLICY`` — an explicit LOCAL file.
3. the companion-bundled resource.
4. ``<data-home>/security_policy.json``.
5. none → ungoverned.

Tiers 2–4 are mutually exclusive (first present wins among them) and collectively
form the *subordinate*, which intersects into the authority above it.  So this tier
outranks ``KIROCREW_SECURITY_POLICY``: while the env file sat above the fetched
document the enterprise ceiling was advisory — any account that can set an
environment variable could point it at a permissive file and the fleet's ceiling
never bound.  Tiers 3 and 4 sit below for the older reason, unchanged: they are what
the fetched document is *replacing*, and a fleet that ships a bootstrap policy naming
a source expects the source to win, or the bootstrap could never be superseded.

Availability, and the one thing this must never do
--------------------------------------------------

A ceiling that a network can withhold is a ceiling that a network can also
*remove*, so the dispositions here are chosen around two failures that pull in
opposite directions:

* **The endpoint is unreachable.**  A cached last-known-good copy is served, and
  that is the normal answer — a host does not stop working because a bucket had a
  bad minute.  With no cache at all the host has no ceiling, and
  ``on_unavailable`` decides: ``fail_closed`` (the default) refuses to start,
  because a fleet that pointed a host at a central ceiling meant that ceiling to
  bind and "we could not tell" must not read as "run unbounded"; ``degrade``
  falls through to the next tier and records a governance incident.
* **The pushed document is bad.**  At boot there is nothing to fall back to, so a
  document that does not parse or does not verify fails like any other tier.
  **On a live refresh it is REJECTED and the running ceiling is kept** — a
  refresh validates a candidate through the same floor gates boot uses and only
  installs it if they all pass.  This is the asymmetry that keeps a typo from
  taking down a fleet that is already up.

Trust
-----

Fetched bytes are the tier where provenance matters most, and this module adds no
new trust root to establish it.  ``identity.signature`` is verified exactly as
for a file tier, against a key held in the operator-controlled admission policy —
never a key the fetched document supplies about itself.  The opt-in is that policy's
``require_policy_signature``, and this tier deliberately adds no per-tier flag of its
own: a document must not be the authority on whether it has to be authentic.  A
document that fails a mandated check is never cached, so a rejection does not persist
as a poisoned last-known-good.

The cache directory is on the ``security._SENSITIVE_HOME_DIRS`` keystone, which
is load-bearing rather than tidy: the cache holds both the document AND the
recorded source it came from, so an agent able to write it could point its own
ceiling somewhere it controls and have the next boot honour it.

Transport
---------

``register_policy_fetcher`` is an append-only seam mirroring
``governance.register_scope`` — including its refusal to silently shadow a
built-in: a scheme maps to a callable, and re-registering one raises.  The built-ins are
``https``, ``file``, and ``http`` restricted to loopback hosts.  An edition that
needs request signing for its own object store, or a management channel that is
not HTTP at all, registers a fetcher at import time and changes nothing else —
the cache, the validation, the refresh loop and the dispositions are shared.
"""

from __future__ import annotations

import importlib as _importlib
import logging
import os
import sys as _sys
import threading
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable, Dict, Optional, Tuple

from kiro_crew.atomic_write import atomic_write, read_bytes_with_retry  # noqa: F401 - re-export
from kiro_crew.config.paths import config_dir  # noqa: F401 - re-export
from kiro_crew.constants import ENV_TRUTHY  # noqa: F401 - re-export
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance import (  # noqa: F401 - re-export
    DEFAULT_FETCH_TIMEOUT_SECS,
    MAX_DURATION_SECS,
    MAX_POLICY_BYTES,
    MIN_REFRESH_INTERVAL_SECS,
    SIGNATURE_VERIFIED,
    UNAVAILABLE_DEGRADE,
    UNAVAILABLE_FAIL_CLOSED,
    GovernanceCeiling,
    PolicyDistribution,
)
from kiro_crew.platform.governance_health import mark_governance_incident
from kiro_crew.platform_compat import (  # noqa: F401 - re-export
    file_lock,
    is_link_or_junction,
    make_owner_only_dir,
    path_writable_by_current_user,
    stat_writable_by_current_user,
)

# isort: split
from kiro_crew import _facade_mirror
from kiro_crew.platform.policy_distribution_boot import (
    _declared_source,
    _env_pins_the_source,
    _sanitize_detail,
    fetch_once,
    parse_distributed_policy,
    validate_ceiling,
)
from kiro_crew.platform.policy_distribution_cache import (
    _CACHE_DOC_LEAF,
    _CACHE_META_LEAF,
    _POST_INSTALL_HOOKS,
    CachedPolicy,
    _cache_pair,
    _cache_write_lock,
    _ensure_cache_dir,
    _installed_digest,
    _meta_payload,
    _record_installed,
    _source_digest,
    _write_cache_file,
    cache_dir,
    read_cache,
    read_cache_meta,
    touch_cache,
    write_cache,
)
from kiro_crew.platform.policy_distribution_source import (
    POLICY_URL_ENV,
    _body_digest,
    _split_url,
    resolve_distribution,
)

logger = logging.getLogger("kiro_crew.platform.policy_distribution")

#: Refresh statuses reported by :func:`refresh_now`.  Names, not booleans, because
#: the CLI, the policy viewer and the audit trail all need to tell "nothing has
#: changed" apart from "a push was refused" — collapsing them would report a
#: rejected ceiling as a healthy no-op.
REFRESH_NOT_CONFIGURED = "not_configured"
REFRESH_UNCHANGED = "unchanged"
REFRESH_APPLIED = "applied"
REFRESH_REJECTED = "rejected"
REFRESH_UNREACHABLE = "unreachable"


# ──────────────────────────────────────────────────────────────────────────
# Live refresh — installing a pushed change without a restart
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RefreshOutcome:
    """The result of one refresh attempt, for the CLI, the viewer and the audit.

    ``detail`` carries no source URL — see :func:`_redact_source` for why this is the
    boundary where that matters.
    """

    status: str
    detail: str = ""
    signature_state: str = ""


def _recompose_differs(central: GovernanceCeiling) -> bool:
    """Would re-folding the ladder around *central* change what the ladder last produced?

    The question the 304 fast path in :func:`refresh_now` has to ask: the central
    document is provably the one installed, but the local rung below it is read from
    disk at compose time and can move without the endpoint publishing anything.
    Composes exactly as :func:`apply_ceiling` does and compares structurally --
    ``GovernanceCeiling`` is a frozen dataclass, so equality is the whole ladder's
    content, not identity.

    The basis is the loader's OWN last fold (``governance.last_composed_ceiling``),
    not ``current_context().governance``.  The installed object is whatever
    ``bootstrap`` or the companion's ``compose`` put into the context after
    ``load_security_policy`` returned, and any ``replace()`` or re-tagging on that
    path makes it structurally unequal to the fold while meaning the same ceiling.
    Compared against THAT, every quiet 304 would re-install and bump the governance
    generation, invalidating everything keyed on it once per interval -- the exact
    churn the "adopt only when it differs" rule exists to avoid.  Before the first
    fold has been recorded there is nothing to compare but the context, so that is
    the one-time fallback; the first install records a fold and every later poll has
    a stable basis.
    """
    from kiro_crew.platform.context import current_context
    from kiro_crew.platform.governance import compose_installed_ceiling, last_composed_ceiling

    # ``last_composed_ceiling`` is what LANDED, never a fold the gates rejected -- so a
    # tightening refused once is still "different" on the next poll and retried.
    prior = last_composed_ceiling()
    if prior is None:
        prior = current_context().governance
    return compose_installed_ceiling(central) != prior


def apply_ceiling(ceiling: GovernanceCeiling) -> None:
    """Validate *ceiling* as boot would, then install it process-wide.

    The validation is the whole point: a pushed document must clear the same floor
    gates boot applies, so a refresh cannot install a ceiling that this host would
    have refused to start under.  Both gates raise, and a raise here means the
    caller keeps the ceiling already installed.

    Installation replaces one field of the frozen context.  Every enforcement
    chokepoint reads ``current_context().governance`` per decision rather than
    capturing it, so the swap binds on the next call with nothing to invalidate by
    hand — and ``set_context`` bumps the ceiling generation, which is what makes
    the profile store recompose against the new ceiling instead of serving
    snapshots built against the retired one.

    The profile check is ``assert_profile_floor``, not the boot gate
    ``assert_profiles_within_ceiling``.  The extra refusal the boot gate carries is
    about the profile STORE's state rather than about the ceiling, and it is
    boot-only by design: at runtime a profile file that became unreadable is
    deliberately reported rather than escalated, so honouring it here would let one
    stray local file block every future fleet policy change on this host — including
    a tightening.  The ordinal floor, which is what "would this host have started
    under the candidate" actually means, still applies.
    """
    # Compose BEFORE validating and installing. A refresh replaces exactly one rung
    # of the ladder, so installing the fetched document by itself would drop every
    # local restriction below it -- a host tightened at boot would find that
    # tightening gone at the first successful poll, a ceiling that loosens itself on
    # a timer. Routing through the loader's own composition is what keeps boot and
    # refresh from diverging, and validating the COMPOSED result means the floor gates
    # judge what will actually govern.
    from kiro_crew.platform.context import current_context, set_context
    from kiro_crew.platform.governance import compose_installed_ceiling, record_composed_ceiling

    composed = compose_installed_ceiling(ceiling)
    # Validate the COMPOSED result, not the fetched rung: the floor gates judge what
    # will actually govern, and a path that installs without validating admits a
    # ceiling this host would have refused to boot under.
    validate_ceiling(composed)
    set_context(replace(current_context(), governance=composed))
    # Recorded AFTER the install, so a fold the gates rejected is never the basis the
    # next 304 poll compares against: were it recorded before, the next re-fold would
    # equal the rejected one, read "nothing moved", and skip -- leaving the looser
    # ceiling installed even after the operator fixed the profile.
    record_composed_ceiling(composed)

    # NOTE: the selectable-ACP-backend set is deliberately NOT recomputed here.
    #
    # Every other chokepoint reads ``current_context().governance`` per decision, so the
    # swap above is all they need. The backend gate cannot: its single gate
    # ``resolve_selected_backend`` runs inside ``KiroCrewConfig.load()``, and reading a
    # ceiling there re-enters that load (harness-parity H3). Its state therefore lives in
    # the ``acp_backends`` registry, established once from ``bootstrap_context``.
    #
    # Recomputing the registry here would bind the new ceiling for backend SELECTION
    # while leaving sessions and pooled providers already running a now-denied harness
    # untouched -- an enforcement that looks complete and is not. Retiring live work needs
    # a session-lifecycle path that does not exist yet (tracked separately), so the
    # promise this scope makes is deliberately the narrow one it can keep: the set is
    # decided at gateway start, and a policy change binds on the next start. That is
    # stated in the dashboard panel and in
    # ``docs/system-specs/modules/governance.md``. Wiring the recompute in is a
    # one-line change once retirement exists -- ``apply_selectable_denials`` assigns
    # ``baseline - denied`` rather than mutating, so it is already safe to re-run with a
    # looser ceiling.


def refresh_now(*, force: bool = False) -> RefreshOutcome:
    """Re-fetch the central ceiling and install it if it is usable.

    Never raises: this runs on a background timer and from an operator command,
    and neither wants an exception for "the endpoint was down".  Every failure mode
    keeps the running ceiling and reports itself.

    ``force`` skips the conditional-request validators so an operator can prove the
    round trip end to end, which is what they actually want from a manual fetch —
    a 304 tells them nothing about whether the document they just published is
    being read correctly.
    """
    from kiro_crew.platform.governance import active_policy_distribution

    try:
        dist = resolve_distribution(active_policy_distribution())
    except PlatformCompositionError as exc:
        logger.error(
            "policy distribution is misconfigured: %s",
            _sanitize_detail(str(exc), os.environ.get(POLICY_URL_ENV, "")),
        )
        return RefreshOutcome(
            REFRESH_REJECTED, "policy distribution is misconfigured; see the gateway log"
        )
    if not dist.enabled:
        return RefreshOutcome(REFRESH_NOT_CONFIGURED)
    # Observed BEFORE the fetch, and this ordering is the whole control: the concurrent
    # publish this guards against lands DURING the fetch, so a snapshot taken afterwards
    # already includes it and the compare-and-swap below would pass while overwriting a
    # NEWER document with the one this call fetched.
    #
    # The raw on-disk identity, deliberately not filtered by source: the question is "is
    # the cache still the pair I saw", and filtering would make a legitimate repoint (whose
    # cache holds the retired endpoint's document) look like a lost race.
    expect_pair = _cache_pair(read_cache_meta())
    pre_fetch = read_cache()
    if pre_fetch is not None and pre_fetch.source_digest != _source_digest(dist.source):
        pre_fetch = None
    cached = None if force else pre_fetch
    try:
        fetched = fetch_once(dist, cached)
    except PlatformCompositionError as exc:
        # A configuration or content error, not a transport one: an unknown scheme,
        # a refused transport, an over-size body, an empty document. Reporting these
        # as "unreachable" would send an operator hunting a network problem that is
        # really a bad push or a typo'd source, so they are classified with the other
        # refusals.
        return _refresh_failure(
            REFRESH_REJECTED,
            dist,
            f"refused the fetch from {dist.source}: {exc}",
            incident="rejected",
        )
    except Exception as exc:
        return _refresh_failure(
            REFRESH_UNREACHABLE, dist, f"could not fetch {dist.source}: {exc}", incident="refresh"
        )

    # A 304 is answered against the CACHE's validators, and the cache is written by
    # other processes too: gatewayd's per-app-call reload, an app backend's boot, and
    # `kirocrew policy fetch` — the step the operator guide recommends for verifying a
    # rollout. So "the source has nothing newer than the cache" does NOT imply "this
    # process is already running it. Without the digest comparison below, one
    # `policy fetch` on a host would cache v2, and this poller would then report
    # `unchanged` forever while the gateway kept enforcing v1 — the live-refresh
    # guarantee silently withdrawn, under the most reassuring status there is.
    body = fetched.body
    if fetched.not_modified:
        if cached is None:
            # A fetcher answered "unchanged" against validators we never sent — which is the
            # normal state under ``--force``, where the validators are deliberately skipped.
            # Reporting that as UNCHANGED made ``kirocrew policy fetch --force`` exit 0 having
            # established nothing, and exiting 0 is exactly what a config-management run reads
            # as "this host took the change". The loader's own arm already refuses it; this is
            # the same refusal on the refresh path.
            return _refresh_failure(
                REFRESH_REJECTED,
                dist,
                f"{dist.source} reported no change, but nothing is cached to compare against "
                "-- with --force no validators were sent, so there was nothing for it to "
                "answer. Treat this as a source that does not honour a conditional request.",
                incident="rejected",
            )
        installed = _installed_digest()
        # Nothing to install when the cache is already what is running. An EMPTY digest
        # deliberately does NOT skip: it means this process does not know what it
        # installed — the case of a boot that DEGRADED to ungoverned — and skipping there
        # would leave such a host permanently below a ceiling another process had already
        # cached. Adopting the cache cannot displace another rung, because
        # ``apply_ceiling`` folds the candidate through the loader's own ladder before it
        # validates or installs anything, so every local restriction below it stays.
        if _body_digest(cached.body) == installed:
            # Re-validate even when nothing changed. "Unchanged" is a statement about the
            # DOCUMENT, and the trust root is a separate input that moves on its own
            # schedule: a fleet turning on ``require_policy_signature`` or rotating a trust
            # key makes the running ceiling untrusted without the endpoint publishing
            # anything, and a 304 would otherwise let it stand indefinitely. Cheap enough
            # to do per interval.
            try:
                unchanged = parse_distributed_policy(cached.body, source=dist.source)
            except PlatformCompositionError as exc:
                return _refresh_failure(
                    REFRESH_REJECTED,
                    dist,
                    f"the ceiling in effect no longer satisfies the trust root: {exc}",
                    incident="rejected",
                )
            # The OTHER rungs move on their own schedule too. The digest above answers
            # only "is the central document the one installed"; a local document that
            # appears or tightens between two central publishes would otherwise reach
            # the running ceiling only when the central body next changed -- a host
            # above its own floor for as long as the endpoint stayed quiet. Re-fold
            # the ladder here exactly as an install would and adopt the result when,
            # and only when, it differs: a genuinely unchanged poll keeps the
            # installed object (and its generation) untouched, so nothing downstream
            # is invalidated for a no-op.
            try:
                if _recompose_differs(unchanged):
                    logger.info(
                        "the central document is unchanged but another tier moved; "
                        "re-installing the composed ceiling"
                    )
                    apply_ceiling(unchanged)
            except PlatformCompositionError as exc:
                return _refresh_failure(
                    REFRESH_REJECTED,
                    dist,
                    f"the ceiling in effect no longer composes on this host: {exc}",
                    incident="compose",
                )
            touch_cache(cached.meta(), etag=fetched.etag, last_modified=fetched.last_modified)
            # Hooks run on this path too, not just on an install. They are best-effort by
            # design, so a transient failure — the tailnet daemon busy for one cycle —
            # would otherwise never be retried: the document does not change, every later
            # poll returns UNCHANGED, and a control the ceiling forbids stays materialised
            # until someone restarts the host. Running them wherever a poll CONFIRMS what
            # governs makes the retry automatic, and they are cheap and idempotent for it:
            # the tailnet one is a governance evaluation that returns immediately unless
            # the capability is actually denied.
            _run_post_install_hooks()
            return RefreshOutcome(REFRESH_UNCHANGED)
        logger.info("the cached ceiling differs from the one installed; adopting the cache")
        body = cached.body

    try:
        ceiling = parse_distributed_policy(body, source=dist.source)
    except PlatformCompositionError as exc:
        return _refresh_failure(
            REFRESH_REJECTED,
            dist,
            f"refused the ceiling published at {dist.source}: {exc}",
            incident="rejected",
        )

    # The pair to restore if the install below refuses. This module promises that a
    # document failing a mandated check is never left as the last-known-good, and the
    # publish-before-install ordering is what puts one at risk: the bytes are on disk
    # before ``apply_ceiling`` has had its say, and that step can refuse for reasons the
    # earlier validation could not see (a profile, the trust root, or a tier-1 pin that
    # moved between the two). Without this, a rejected push becomes what the next boot —
    # and every cache-only child — adopts.
    #
    # The same pre-fetch snapshot the swap expects, not a fresh read: restoring a copy
    # published by another writer during this fetch would undo ITS work, and a copy from
    # a different source is not a rollback target at all (re-caching a retired endpoint's
    # document under the new endpoint's name is what the repoint rule exists to refuse).
    previous = pre_fetch

    # A candidate that MOVES the source is refused rather than installed. Nothing else
    # would notice: the refresher re-reads the installed ceiling each cycle, so it would
    # start polling the new address and the migration would look like it worked — until a
    # restart, where the bootstrap declaration (the env variable or the lower-tier file)
    # still names the OLD source and the cache, recorded against the new one, is discarded
    # by the repoint rule. A fleet that had retired the old address would then have hosts
    # that run fine and cannot reboot, with nothing in between to warn them.
    #
    # Migrating a source is a BOOTSTRAP change: move ``KIROCREW_POLICY_URL`` (or the
    # ``distribution.source`` in the placed policy) and the new address is durable on the
    # next boot as well as this one. Refusing here keeps the two channels consistent
    # instead of offering a migration that only half-applies.
    migrated = _declared_source(body)
    if migrated and migrated != dist.source and not _env_pins_the_source():
        return _refresh_failure(
            REFRESH_REJECTED,
            dist,
            "the published document moves distribution.source to a different address. A "
            "live refresh cannot migrate the source: the bootstrap declaration would "
            f"still name the old one at the next boot. Repoint {POLICY_URL_ENV} (or the "
            "placed policy's distribution.source) instead, then publish.",
            incident="rejected",
            signature_state=ceiling.signature_state,
        )

    try:
        # Validate BEFORE publishing, then publish BEFORE installing. A cache-only child
        # starting concurrently adopts whatever the cache holds, so installing first
        # leaves a window where the gateway enforces the new ceiling and a fresh app
        # backend adopts the retired one — and publishing first without validating would
        # hand that child a document this host is about to refuse.
        validate_ceiling(ceiling)
        if not write_cache(
            body,
            source=dist.source,
            etag=fetched.etag,
            last_modified=fetched.last_modified,
            expect_pair=expect_pair,
        ):
            # Not published, and the two reasons want different answers. Re-reading is how
            # they are told apart, and it is the honest way: the swap's own condition was
            # "the cache still holds what I observed".
            moved = _cache_pair(read_cache_meta()) != expect_pair
            if moved:
                # Another writer published during this fetch. Installing what we fetched
                # could roll the ceiling BACKWARD to a looser document, so this refresh
                # simply stands down; the next cycle finds the cache differs from what is
                # installed and adopts it, which is the path a 304 against a moved-ahead
                # cache already takes. Not a failure of anything, so not an incident.
                logger.info(
                    "another process published a newer ceiling during this fetch; standing "
                    "down and leaving it to the next cycle"
                )
                return RefreshOutcome(
                    REFRESH_UNCHANGED,
                    "another process published a newer ceiling during this fetch; it will "
                    "be adopted on the next cycle",
                )
            # The write itself failed. Reported like the read-back confirmation below,
            # because it is the same operator problem: the gateway cannot write the cache,
            # so a cache-only child would inherit the previous ceiling.
            return _refresh_failure(
                REFRESH_REJECTED,
                dist,
                "the refreshed ceiling could not be published to the policy cache, so a "
                "cache-only child would inherit the previous one; keeping the running "
                f"ceiling. Check that the gateway can write {cache_dir()}.",
                incident="compose",
                signature_state=ceiling.signature_state,
            )
        # ``write_cache`` is best-effort by design — a host that cannot write it is
        # still governed by what it fetched — but a cache-only child inherits the
        # ceiling FROM that file. So a swallowed write failure here would leave the
        # gateway enforcing the new (possibly tighter) ceiling while every app backend
        # spawned afterwards adopted the older, looser one. Confirming the publish is
        # what makes "the cache is the administrator's ceiling" true rather than
        # usually true.
        published = read_cache()
        # The SOURCE as well as the bytes. A repoint that publishes identical bytes plus a
        # failed metadata write leaves the old source's cache passing a body-only check,
        # and it is the recorded source the next boot's repoint rule judges — so the
        # confirmation would report a publish that a restart then discards, failing closed
        # on a host whose refresh had just reported success.
        if (
            published is None
            or published.body != body
            or published.source_digest != _source_digest(dist.source)
        ):
            raise PlatformCompositionError(
                "the refreshed ceiling could not be published to the policy cache, so a "
                "cache-only child would inherit the previous one; keeping the running "
                f"ceiling. Check that the gateway can write {cache_dir()}."
            )
        apply_ceiling(ceiling)
    except Exception as exc:
        # Only if the cache still holds what THIS refresh published: another process may
        # have published a newer document between the publish above and this failure, and
        # rolling back over it would destroy a valid ceiling nobody asked us to touch.
        _restore_cache(previous, published_pair=(_body_digest(body), _source_digest(dist.source)))
        # The document is well-formed but this host cannot run under it — a bound
        # profile is looser than the new ceiling, or a mandated signature is
        # missing. Keeping the running ceiling is the whole reason a live refresh
        # validates before installing: a fleet that is already up must not be taken
        # down by a push, and the operator has an audited reason to look at.
        return _refresh_failure(
            REFRESH_REJECTED,
            dist,
            f"the ceiling published at {dist.source} does not compose on this host: {exc}",
            incident="compose",
            signature_state=ceiling.signature_state,
        )

    _record_installed(body)
    # After the install, so a hook sees the ceiling it is meant to enforce. Also run on the
    # UNCHANGED path above — see the note there for why a confirming poll is the right
    # trigger rather than an install alone.
    _run_post_install_hooks()
    logger.info(
        "installed a refreshed governance ceiling from the %s source",
        _split_url(dist.source).scheme or "unknown",
    )
    _audit_refresh(REFRESH_APPLIED, dist)
    return RefreshOutcome(REFRESH_APPLIED, signature_state=ceiling.signature_state)


def _restore_cache(previous: Optional["CachedPolicy"], *, published_pair: Tuple[str, str]) -> None:
    """Put *previous* back as the last-known-good after a refused install.

    A **compare-and-swap**, like the publish it undoes: the rollback happens only while
    the cache still holds *published_pair*, the ``(body digest, source digest)`` this
    refresh put there. The pair for the same reason the publish uses one — identical bytes
    under a different source are not this refresh's work to undo. The
    publish and this failure are separated by ``apply_ceiling``, so another process can
    publish in between — and rolling back over it would destroy a valid, newer ceiling
    nobody asked us to touch. When the digests disagree, the newer copy is left alone;
    this refresh's document is not the one on disk, so there is nothing of ours to
    undo.

    Called only from ``refresh_now``'s failure arm, where the new bytes are already on
    disk because the publish is deliberately confirmed before the install. Restoring
    keeps this module's promise that a document failing a mandated check never becomes
    what the next boot adopts.

    ``previous`` is ``None`` when there was nothing cached for this source, and then
    there is nothing to restore TO: the rejected bytes stay, and that is the honest
    state — deleting them would turn a refused push into "no central ceiling at all",
    which a cache-only child answers by refusing to start. A stale-but-composable
    document is the better of the two, and the running ceiling is untouched either way.

    Best-effort and never raises, for the same reason ``write_cache`` is: the caller is
    already returning a failure, and a restore that cannot be written must not turn a
    reported rejection into an exception on a background timer.

    The age is deliberately NOT restarted -- ``fetched_at`` is carried from the copy
    being put back, so a fleet's ``max_cache_age_secs`` still measures from the last
    successful fetch rather than from this failure.
    """
    meta = previous.meta() if previous is not None else None
    directory = _ensure_cache_dir()
    if directory is None:
        return
    try:
        with _cache_write_lock(directory):
            observed = read_cache_meta()
            if _cache_pair(observed) != published_pair:
                logger.info(
                    "another process published a ceiling after this refresh's own publish; "
                    "leaving it in place rather than rolling back over it"
                )
                return
            # INVALIDATE FIRST, then write the previous copy back. The ordering is what
            # makes a partial failure safe: the bytes on disk right now are the ones this
            # host just REFUSED, so if the restoring write fails after a delete the cache
            # is ABSENT — a cache-only child then fails closed (a disabled app, loudly) and
            # the next boot re-fetches. Writing over them first and failing halfway would
            # leave the rejected document as the last-known-good, which is the one outcome
            # this function exists to prevent. The gap is held under the lock and the
            # gateway's own ceiling is untouched throughout.
            for leaf in (_CACHE_DOC_LEAF, _CACHE_META_LEAF):
                (directory / leaf).unlink(missing_ok=True)
            if meta is None:
                # Nothing to restore TO, and deleting was the whole job. Not the same
                # trade-off as removing a stale-but-composable copy: leaving the refused
                # bytes would make the next boot serve them from cache instead of
                # re-fetching, so a source the administrator has since corrected would not
                # reach this host until the window expired, and a cache-only child would
                # adopt the refused ceiling meanwhile.
                return
            assert previous is not None  # meta is None iff previous is, handled above
            payload = _meta_payload(
                source_digest=meta.source_digest,
                etag=meta.etag,
                last_modified=meta.last_modified,
                digest=meta.digest,
                # Carried from the copy being put back, NOT restarted: otherwise a
                # repeatedly-failing refresh would keep resetting the staleness clock and
                # a fleet's max_cache_age_secs would never notice a source that broke.
                now=meta.fetched_at,
            )
            for path, data in (
                (directory / _CACHE_DOC_LEAF, previous.body),
                (directory / _CACHE_META_LEAF, payload),
            ):
                _write_cache_file(path, data)
    except Exception:
        # Reported at ERROR with a governance incident, not a quiet warning: the caller is
        # already returning a rejection and cannot raise (it runs on a background timer),
        # so this log is the only signal that the host has lost its outage fallback.
        logger.error(
            "could not restore the previous cached ceiling; this host has no "
            "last-known-good copy until the next successful fetch",
            exc_info=True,
        )
        mark_governance_incident("degraded", "policy_distribution:restore")


def _refresh_failure(
    status: str,
    dist: PolicyDistribution,
    detail: str,
    *,
    incident: str,
    signature_state: str = "",
) -> RefreshOutcome:
    """Log, mark, audit and report one refresh failure.  The single failure tail.

    An unreachable source is a WARNING and a ``degraded`` mark — a network having a
    bad minute is not an integrity event.  Everything else is an ERROR and
    ``failed_closed``: a document was refused, which is a decision.  Both are
    derivable from *status*, so a new failure site cannot pair them wrongly.
    """
    unreachable = status == REFRESH_UNREACHABLE
    detail = _sanitize_detail(detail, dist.source)
    (logger.warning if unreachable else logger.error)("%s", detail)
    mark_governance_incident(
        "degraded" if unreachable else "failed_closed", f"policy_distribution:{incident}"
    )
    _audit_refresh(status, dist)
    return RefreshOutcome(status, detail, signature_state)


#: SEL ``reason`` per refresh status.  Fixed phrases rather than the caller's
#: ``detail``, because that detail is built for an operator reading a log and
#: embeds the source URL — see :func:`_audit_refresh`.
_AUDIT_REASONS = {
    REFRESH_APPLIED: "installed a refreshed ceiling from the central source",
    REFRESH_REJECTED: "refused the ceiling published at the central source",
    REFRESH_UNREACHABLE: "the central source could not be reached",
}


def _audit_refresh(status: str, dist: PolicyDistribution) -> None:
    """Record a refresh outcome in the SEL.  Best-effort.

    Only the outcomes that CHANGED something or failed are recorded — an unchanged
    poll is the common case and auditing it would append one HMAC-chained row per
    interval per host for a decision that decided nothing.

    **Nothing here names the endpoint.**  The source is recorded as its SCHEME, and
    the reason is a fixed phrase keyed on the status rather than the prose the
    caller logged — that prose is written for an operator debugging a fetch and
    interpolates the URL. The SEL is readable through surfaces the agent can reach,
    and the endpoint is the fleet's control plane, so naming it would tell a
    prompt-injected agent exactly where to aim. The operator gets the URL in the
    gateway log and from ``kirocrew policy fetch``, which are operator surfaces the
    way the policy file itself is.
    """
    try:
        # Both imports are function-local. The SEL and the profile module are heavy
        # relative to this trust root, and this runs at most once per refresh
        # interval, so the import is already cached by the second call. The session
        # key comes from the module that DEFINES the host sentinel rather than a
        # literal here, so the two cannot drift.
        from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY
        from kiro_crew.sel import sel

        scheme = _split_url(dist.source).scheme or "unknown"
        sel().log_governance_decision(
            scope="distribution",
            item=scheme,
            outcome="allowed" if status == REFRESH_APPLIED else "denied",
            rule=status,
            layer="policy",
            reason=_AUDIT_REASONS.get(status, status),
            session_key=HOST_SESSION_KEY,
            tool_name="policy_distribution",
        )
    except Exception:
        logger.debug("could not audit the policy refresh", exc_info=True)


class _Refresher:
    """The background poll loop.  One per process."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last: Optional[RefreshOutcome] = None
        self._last_at: float = 0.0

    def start(self, interval_secs: int) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(max(interval_secs, MIN_REFRESH_INTERVAL_SECS),),
                name="kc-policy-refresh",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the loop to exit and wait up to *timeout* for it.

        The handle is cleared only once the thread has actually died.  Clearing it
        on a timed-out join would let the next :meth:`start` create a SECOND
        polling thread beside one that is still running — and the join here is
        deliberately short (the gateway gives it a fraction of a second), so a
        timeout is the expected case whenever a fetch is in flight.  Leaving a
        dead-but-not-``None`` handle costs nothing, because ``start`` tests
        ``is_alive()`` rather than identity.  The stop event stays set either way,
        so a surviving thread exits at the end of its current cycle.
        """
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        if not thread.is_alive():
            with self._lock:
                if self._thread is thread:
                    self._thread = None

    def _run(self, interval_secs: int) -> None:
        logger.info("polling the central policy source every %ds", interval_secs)
        # Wait BEFORE the first poll: boot has just established the ceiling from
        # this same source, so an immediate re-fetch would be a redundant round
        # trip on every host at startup — and across a fleet restarting together,
        # a thundering herd against the admin's own endpoint.
        while not self._stop.wait(interval_secs):
            try:
                outcome = refresh_now()
            except Exception:  # pragma: no cover - refresh_now is already total
                logger.warning("the policy refresh cycle raised", exc_info=True)
                continue
            with self._lock:
                self._last, self._last_at = outcome, time.time()
            # Re-read the cadence from the ceiling now in effect, so a pushed
            # document can retune its own polling — an admin lengthening the
            # interval during an incident should not need a fleet restart for it to
            # take. A push that REMOVES the source stops the loop for the same
            # reason: the fleet said to stop following it.
            interval_secs = self._next_interval(interval_secs)
            if interval_secs <= 0:
                # WARNING, not info: on the policy-block channel this is reachable by
                # publishing a document that FORGOT to carry its own ``distribution``
                # block forward, and the symptom is a fleet that silently stops
                # following its administrator. Deliberate withdrawal looks identical
                # from here, so it is not a governance incident — but it must be
                # visible in the log rather than inferred from the absence of polling.
                logger.warning(
                    "the ceiling now in effect names no central source; stopped polling. "
                    "A restart still re-fetches from whatever names the source at boot; "
                    "to keep polling, carry the 'distribution' block into each published "
                    "document or set %s.",
                    POLICY_URL_ENV,
                )
                return

    @staticmethod
    def _next_interval(current: int) -> int:
        """The interval for the next cycle: 0 to stop, else a clamped positive value.

        Keeps *current* on any resolution error.  A ceiling that momentarily cannot
        be read is not an instruction to stop polling — the next cycle is what would
        fix it.
        """
        from kiro_crew.platform.governance import active_policy_distribution

        try:
            dist = resolve_distribution(active_policy_distribution())
        except PlatformCompositionError as exc:
            # No resolved `dist` to redact against, so the env source is the one to
            # withhold -- the same fallback `refresh_now`'s misconfigured arm uses.
            logger.warning(
                "could not re-read the refresh cadence; keeping it: %s",
                _sanitize_detail(str(exc), os.environ.get(POLICY_URL_ENV, "")),
            )
            return current
        if not dist.enabled:
            return 0
        # A local KIROCREW_SECURITY_POLICY file never stops this loop: it is a
        # subordinate tier and tightens whatever the loop installs, so there is
        # nothing for the refresher to stand down for.
        return dist.effective_refresh_interval() or current

    def last(self) -> Tuple[Optional[RefreshOutcome], float]:
        with self._lock:
            return self._last, self._last_at

    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()


_REFRESHER = _Refresher()


def register_post_install_hook(hook: Callable[[], None]) -> None:
    """Register *hook* to run after a live refresh installs a new ceiling.

    A ceiling swap changes what every LIVE evaluation answers, and most controls are live
    evaluations: the sandbox floor is re-read per spawn, the governance gate per tool call.
    A few are not — they are **materialised** once, when an action is taken, and then
    outlive the decision. A published tailnet origin is the case that matters: the
    ``capabilities.tailnet_origin`` gate fires at publish time, so a ceiling that later
    pins it off does not retract what is already serving. Before live refresh existed the
    ceiling only changed at boot and that could not arise; this seam is what keeps it from
    arising now.

    Append-only, like :func:`register_policy_fetcher`, and deliberately unaware of what it
    is calling. Hooks run on the refresher thread, best-effort: an exception is logged and
    the next hook still runs, because a control that cannot be re-derived must not stop the
    ceiling that was already installed from being reported as installed.
    """
    _POST_INSTALL_HOOKS.append(hook)


def _run_post_install_hooks() -> None:
    """Re-derive materialised controls after an install.  Never raises.

    A failure is reported at ERROR **and marked as a governance incident**, because a
    swallowed one is a control the ceiling calls for and the host is not applying, with
    nothing on any surface saying so. The mark is what puts it on ``security_posture`` and the
    dashboard rather than leaving it in a log nobody is reading.

    It does NOT unwind the install, and that is the deliberate direction. Rolling back would
    restore the OLD, looser ceiling — strictly less protection than the new one with one
    materialised control stale, since the tightened ceiling still binds every call the stale
    control does not pre-approve. Combined with the retry (hooks run on every confirming poll,
    and the reprojection memo advances only after a success), a transient failure costs one
    interval; a persistent one is visible and the ceiling that was installed stays installed.
    """
    for hook in list(_POST_INSTALL_HOOKS):
        try:
            hook()
        except Exception:
            logger.error(
                "a post-install governance hook failed; a control the installed ceiling calls "
                "for is not being applied on this host. It is retried on the next confirming "
                "poll; the installed ceiling is unaffected and still binds everything that "
                "control does not pre-approve",
                exc_info=True,
            )
            mark_governance_incident("degraded", "policy_distribution:post_install_hook")


def start_refresher() -> bool:
    """Start the background poll loop if the active ceiling asks for one.

    Returns whether a loop is now running.  Safe to call more than once — a second
    call while one is alive is a no-op, so a process with two entry points does not
    end up polling twice.
    """
    from kiro_crew.platform.governance import active_policy_distribution

    try:
        dist = resolve_distribution(active_policy_distribution())
    except PlatformCompositionError as exc:
        logger.warning(
            "policy distribution is misconfigured; not polling: %s",
            _sanitize_detail(str(exc), os.environ.get(POLICY_URL_ENV, "")),
        )
        return False
    interval = dist.effective_refresh_interval()
    if not dist.enabled or interval <= 0:
        return False
    return _REFRESHER.start(interval)


def stop_refresher(timeout: float = 2.0) -> None:
    """Stop the background poll loop (gateway shutdown, and tests)."""
    _REFRESHER.stop(timeout)


def refresher_running() -> bool:
    return _REFRESHER.running()


# ──────────────────────────────────────────────────────────────────────────
# Read-only posture, for the CLI and the policy viewer
# ──────────────────────────────────────────────────────────────────────────


#: ``distribution_posture()["error_code"]`` values.  A machine-readable enum, never
#: a rendered sentence: the posture is served in a JSON body the dashboard
#: translates, so English prose here would ship an untranslatable string — and an
#: exception's ``str()`` would additionally leak the endpoint, which the posture
#: exists to withhold.
POSTURE_ERROR_MISCONFIGURED = "misconfigured"


def distribution_posture() -> Dict[str, object]:
    """Posture of central distribution, with no secret and no endpoint in it.

    Deliberately reports the source's SCHEME and never its URL, for the reason the
    policy viewer already withholds rule contents: the dashboard is reachable by
    the agent's own browser tooling, and the endpoint is the fleet's control plane.
    Naming it would tell a prompt-injected agent exactly where to aim, while the
    scheme alone answers what an operator actually asks — is this host centrally
    governed, and is the channel encrypted.

    Every value is a number, a boolean, or a machine-readable enum, so the
    dashboard renders translated text and no English ships in a JSON body.  A
    fetcher's exception message appears nowhere in the result: it is prose, and it
    routinely embeds the URL.

    Total: any resolution failure reports ``configured: false`` with an
    ``error_code`` rather than raising, because this feeds a status panel.
    """
    from kiro_crew.platform.governance import active_policy_distribution

    posture: Dict[str, object] = {
        "configured": False,
        "source_scheme": "",
        "refresh_interval_seconds": 0,
        "max_cache_age_seconds": 0,
        "on_unavailable": "",
        "refresher_running": False,
        "cache_present": False,
        "cache_age_seconds": None,
        "last_refresh_status": "",
        "last_refresh_age_seconds": None,
        "error_code": "",
    }
    try:
        dist = resolve_distribution(active_policy_distribution())
    except PlatformCompositionError as exc:
        # The reason is logged, not returned: it is English and it names the
        # endpoint. ``misconfigured`` is enough for the panel to tell an operator
        # to go read the policy file, which is where the detail belongs anyway.
        logger.warning(
            "central policy distribution is misconfigured: %s",
            _sanitize_detail(str(exc), os.environ.get(POLICY_URL_ENV, "")),
        )
        posture["error_code"] = POSTURE_ERROR_MISCONFIGURED
        return posture
    if not dist.enabled:
        return posture

    posture.update(
        {
            "configured": True,
            "source_scheme": _split_url(dist.source).scheme.lower(),
            "refresh_interval_seconds": dist.effective_refresh_interval(),
            "max_cache_age_seconds": dist.max_cache_age_secs,
            "on_unavailable": dist.on_unavailable,
            "refresher_running": refresher_running(),
        }
    )
    # Metadata only: the posture needs a source and an age, and this endpoint is
    # repolled every 30 seconds by every open Security tab. Reading a document of up
    # to MAX_POLICY_BYTES to answer "how old is it" is the waste that only shows up
    # once a viewer is left open.
    meta = read_cache_meta()
    if meta is not None and meta.source_digest == _source_digest(dist.source):
        posture["cache_present"] = True
        posture["cache_age_seconds"] = int(meta.age_secs())
    outcome, at = _REFRESHER.last()
    if outcome is not None:
        posture["last_refresh_status"] = outcome.status
        posture["last_refresh_age_seconds"] = max(0, int(time.time() - at))
    return posture


__all__ = [
    "POLICY_URL_ENV",
    "POLICY_HEADERS_ENV",
    "POLICY_REFRESH_ENV",
    "POLICY_TIMEOUT_ENV",
    "POLICY_MAX_AGE_ENV",
    "POLICY_UNAVAILABLE_ENV",
    "POLICY_CACHE_ONLY_ENV",
    "cache_only",
    "central_ceiling_installed",
    "effective_max_cache_age",
    "POLICY_DISTRIBUTION_ENV_VARS",
    "CACHE_DIR_LEAF",
    "REFRESH_NOT_CONFIGURED",
    "REFRESH_UNCHANGED",
    "REFRESH_APPLIED",
    "REFRESH_REJECTED",
    "REFRESH_UNREACHABLE",
    "POSTURE_ERROR_MISCONFIGURED",
    "FetchRequest",
    "FetchedPolicy",
    "PolicyFetcher",
    "CachedPolicy",
    "RefreshOutcome",
    "register_policy_fetcher",
    "registered_policy_schemes",
    "request_headers",
    "resolve_distribution",
    "cache_dir",
    "read_cache",
    "write_cache",
    "touch_cache",
    "reset_fetch_window",
    "reset_process_state",
    "load_distributed_policy",
    "fetch_once",
    "apply_ceiling",
    "validate_ceiling",
    "refresh_now",
    "start_refresher",
    "stop_refresher",
    "refresher_running",
    "distribution_posture",
]


# This module is the facade of central policy distribution: every name is reached through
# it as ``kiro_crew.platform.policy_distribution.<name>``. A read is served by the owner that
# binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every
# owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. The
# live-refresh path, the refresher thread and the read-only posture stay here. New work
# goes to the owner of its responsibility:
#
#   ``policy_distribution_source`` -- the tier's settings, the fetch records, the transports
#   ``policy_distribution_cache`` -- the last-known-good cache and the tier's process state
#   ``policy_distribution_boot`` -- parsing, verifying and resolving the tier at boot
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.platform.policy_distribution_source",
    "kiro_crew.platform.policy_distribution_cache",
    "kiro_crew.platform.policy_distribution_boot",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.platform.policy_distribution_boot import (  # noqa: F401
        _MIN_SUBSTITUTABLE_SECRET,
        _cache_is_too_old,
        _cached_max_cache_age,
        _discard_disagreeing_cache,
        _fetch_window,
        _from_cache_on_outage,
        _load_by_fetch,
        _load_cache_only,
        _load_from_cached_winner,
        _LoadOutcome,
        _redact_source,
        _request_header_secrets,
        _unavailable,
        central_ceiling_installed,
        effective_max_cache_age,
        effective_refresh_interval,
        load_distributed_policy,
    )
    from kiro_crew.platform.policy_distribution_cache import (  # noqa: F401
        _AZURE_SAS_MARKERS,
        _AZURE_SAS_QUERY_NAMES,
        _CACHE_LOCK_LEAF,
        _FETCH_SLOT_LOCK,
        _INSTALLED_LOCK,
        _PRESIGN_QUERY_PREFIXES,
        CACHE_DIR_LEAF,
        CachedMeta,
        _cache_dir_ready,
        _claim_fetch_slot,
        _identity_query,
        _installed_body_digest,
        _last_fetch_attempt,
        reset_fetch_window,
        reset_process_state,
    )
    from kiro_crew.platform.policy_distribution_source import (  # noqa: F401
        _FETCHER_LOCK,
        _FETCHERS,
        _LOOPBACK_HOSTNAME,
        ENTERPRISE_POLICY_URL,
        POLICY_CACHE_ONLY_ENV,
        POLICY_DISTRIBUTION_ENV_VARS,
        POLICY_HEADERS_ENV,
        POLICY_MAX_AGE_ENV,
        POLICY_REFRESH_ENV,
        POLICY_TIMEOUT_ENV,
        POLICY_UNAVAILABLE_ENV,
        FetchedPolicy,
        FetchRequest,
        PolicyFetcher,
        _assert_transport_permitted,
        _env_number,
        _fetch_file,
        _fetch_http,
        _fetcher_for,
        _is_enterprise,
        _is_loopback,
        _NoRedirects,
        _open,
        _opener_handlers,
        cache_only,
        host_pinned_source,
        register_policy_fetcher,
        registered_policy_schemes,
        request_headers,
    )
