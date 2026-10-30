"""The tier ladder: which local policies are read, and how they fold into one ceiling.

Where the env and home tiers are read (``_POLICY_ENV``, ``_policy_home_path``), the
per-process tier state (``_TierProcessState``, ``reset_process_state``,
``last_composed_ceiling``), the tier intersection and its once-per-process audits, the
fold ``compose_tier_ladder`` with its refresh entry point ``compose_installed_ceiling``,
and the boot loader ``load_security_policy``. A change to precedence between tiers goes
here.

Served through :mod:`kiro_crew.platform.governance`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

from kiro_crew import edition
from kiro_crew.config.paths import config_dir
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance_parts.archetypes import (
    ScopedMap,
    ScopedRuleset,
    _compose_controls,
)
from kiro_crew.platform.governance_parts.baseline import (
    ENTERPRISE_SLACK_CHANNELS,
    ENTERPRISE_UPDATE_CHANNEL,
    ENTERPRISE_YOLO_MAX_SECONDS,
)
from kiro_crew.platform.governance_parts.carriers import (
    _POLICY_DISTRIBUTION_URL_ENV,
    BootControls,
    GovernanceCeiling,
    PolicyDistribution,
    UpdatePins,
    _is_enterprise,
    _version_tuple,
)
from kiro_crew.platform.governance_parts.parsing import (
    _SANDBOX_FLAGS_SCOPE,
    _read_json_file,
    baseline_controls,
    parse_policy,
    with_inner_baseline,
)
from kiro_crew.platform.governance_parts.signature import _verify_policy_signature
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.platform.governance")


# Where the local security policy tiers are read from.  Both sit BENEATH the
# centrally distributed document and may only tighten it (``compose_tier_ladder``);
# the env file is the first subordinate, the home file the standalone operator's
# authoring location.  The companion-bundled resource is resolved separately by the
# caller that knows the active edition (see ``load_security_policy``).
_POLICY_ENV = "KIROCREW_SECURITY_POLICY"

_POLICY_HOME_LEAF = "security_policy.json"

# Names for the precedence tiers.  Set by the loader on the ceiling each tier
# produces and written into SEL audit records (``_audit_policy_tier``); strings
# rather than an enum because a SEL record is text.
TIER_CENTRAL = "central"  # the fetched, centrally distributed document

TIER_ENV = "env"  # KIROCREW_SECURITY_POLICY

TIER_BUNDLED = "bundled"  # the companion edition's packaged resource

TIER_HOME = "home"  # ~/.kiro/crew/security_policy.json


# Duplicated from ``policy_distribution.CACHE_DIR_LEAF`` on purpose, as
# ``_POLICY_DISTRIBUTION_URL_ENV`` is, and pinned equal by the same test.  Named here because
# ``assert_governance_paths_protected`` is a BOOT check that must not import the
# fetch engine to know what it is asserting.
_POLICY_CACHE_LEAF = "policy_cache"

# Also duplicated from ``policy_distribution``, and pinned by the same test.  Named
# here because the tier's ENTRY condition has to know about cache-only mode: such a
# child is deliberately given no source, so a source-only gate would skip the tier.
_POLICY_DISTRIBUTION_CACHE_ONLY_ENV = "KIROCREW_POLICY_CACHE_ONLY"


def _policy_home_path() -> Path:
    """Resolve the standalone security-policy path lazily.

    Deferred (not a module-level ``config_dir()`` capture) so importing this
    trust-root module never fires ``config_dir()`` — and thus the one-time
    data-home migration — as an import side effect. The migration must happen
    only at the single chosen point (``ensure_data_home()`` in the CLI prologue);
    the platform layer is documented as side-effect-free load-bearing infra.
    Callers (and tests) go through this accessor rather than a captured constant.
    """
    return config_dir() / _POLICY_HOME_LEAF


@dataclass
class _TierProcessState:
    """Every per-process value the tier ladder keeps, in ONE place.

    ``load_security_policy`` is not called once per process -- ``mcp_gateway.app_call``
    re-runs it on every app callback -- so anything that must happen once, or be
    remembered across a central refresh, lives here rather than being recomputed.
    Held in one dataclass rather than separate module globals so a test resets them
    together (:func:`reset_process_state`, mirroring
    ``policy_distribution.reset_process_state``): a fixture that resets some of them
    and forgets one is how ``last_bundled`` leaked between tests.

    ``last_bundled`` -- the companion edition's bundled document from the last load
    that resolved one. A refresh has no ``bundled_loader`` of its own, and without
    this the bundled rung would drop out of the ladder at the first poll, loosening a
    ceiling an edition tightened. The packaged resource is static for the process.

    ``home_unreadable_warned`` -- the one-time warning that an unusable home file
    (unreadable, or one ``parse_policy`` rejects) beneath a present authority was
    skipped has fired. Once, because the fold runs on
    every refresh poll and a warning per poll would be a warning per interval.

    ``env_beneath_central_warned`` -- the one-time warning that
    ``KIROCREW_SECURITY_POLICY`` only tightens has fired. A runbook may still describe
    it as a rollback lever that outranks the central document; the first compose of
    the env-beneath-central shape says otherwise, once.

    ``tier_intersects_audited`` -- the ``(authority, lower)`` pairs already recorded.
    The pairs are fixed for the process unless a tier appears or disappears, so a row
    per compose was a row per interaction, burying the one-time signal above.

    ``last_composed`` -- the fold now IN EFFECT: what :func:`load_security_policy`
    returned at boot, or what :func:`policy_distribution.apply_ceiling` installed on a
    refresh (recorded after ``set_context``, never for a fold that failed
    validation).  It is the comparison basis for the 304 re-fold
    (``policy_distribution._recompose_differs``): "did another tier move" is a
    question about the fold, and the fold is the only object this module can vouch
    for.  ``current_context().governance`` is NOT that object -- whatever installed it
    (``bootstrap``, the companion's ``compose``, a caller that ``replace()``-tags a
    field) may have made it structurally unequal to the fold while meaning the same
    ceiling, and a compare against it would re-install on every quiet poll and bump
    the governance generation each time.  Read it through
    :func:`last_composed_ceiling`.
    """

    last_bundled: Optional[Mapping[str, object]] = None
    env_beneath_central_warned: bool = False
    home_unreadable_warned: bool = False
    tier_intersects_audited: "set[Tuple[str, str]]" = field(default_factory=set)
    last_composed: Optional["GovernanceCeiling"] = None


_process_state = _TierProcessState()


def reset_process_state() -> None:
    """Clear every per-process value the tier ladder keeps.  Test helper."""
    global _process_state
    _process_state = _TierProcessState()


def last_composed_ceiling() -> Optional["GovernanceCeiling"]:
    """The last fold that actually LANDED, or ``None`` before the first one.

    "Landed" is the load-bearing word: :func:`load_security_policy` records its
    result (a boot whose fold fails the floor gates aborts the process, so that record
    cannot outlive a rejection), and :func:`policy_distribution.apply_ceiling` records
    only after ``set_context`` returns. :func:`compose_installed_ceiling` itself records
    nothing, because its caller may reject the fold.  See ``_TierProcessState``.
    """
    return _process_state.last_composed


def record_composed_ceiling(ceiling: "GovernanceCeiling") -> None:
    """Record *ceiling* as the fold now in effect.  Installer-only; see above."""
    _process_state.last_composed = ceiling


def _intersect_ceilings(
    authority: GovernanceCeiling, subordinate: GovernanceCeiling
) -> GovernanceCeiling:
    """Narrow *authority* by *subordinate* -- the subordinate can only tighten.

    Reuses :func:`_compose_controls`, the same per-scope AND the profile layer
    uses (allow∩, deny∪, ordinal=stricter, gate=AND), so "cannot widen" is a
    property of the primitive rather than a rule this function has to police.

    A scope the subordinate governs and the authority does not carries through:
    an ungoverned scope is *unrestricted*, so adding a restriction to it is a
    tightening, not an escape.  A scope the authority governs and the subordinate
    does not keeps the authority's value, because a subordinate cannot repeal by
    omission.

    Everything outside ``controls`` stays the AUTHORITY's -- identity, signature
    state, tier, distribution pins and the update *commands* -- except the two update
    pins that are themselves restrictions, which fold (:func:`_intersect_update_pins`).
    (The distribution pins are additionally re-applied by :func:`compose_tier_ladder`,
    which owns the one case this function cannot see: an authority that declared no
    pins at all, where keeping "the authority's" would discard the pins of the tier
    beneath it rather than preserve a choice.  Within a single intersection the rule
    below still holds.)
    A subordinate document must not be able to relabel whose ceiling this is, relax
    an update pin, redirect where the next document is fetched from, or widen its
    own escape hatch.

    Every field outside ``controls`` falls into one of three classes, and the class
    decides its precedence rule.  Get the class wrong and a lower tier widens the
    ceiling through a side door ``_compose_controls`` never sees -- or, the mirror
    failure, a lower tier's restriction is silently voided:

    * **A restriction that folds** -- ``updates.source`` (an allowlist; empty permits
      any remote), ``updates.min_version`` (a floor; empty is none),
      ``updates.channel`` (the authority's, else the subordinate's) and
      ``yolo_max_seconds`` (the lower cap).  A subordinate
      supplying one where the authority left it empty NARROWS the ceiling, so it is
      kept; where both set one, the authority's source stands and the higher floor
      wins.  Keeping "the authority's" here would discard a pure tightening and let
      code install from a source the local operator forbade.
    * **Absence means "no choice expressed"** -- ``distribution`` pins.  Nothing above
      chose, so a lower tier supplying one redirects nothing; ``compose_tier_ladder``
      lets the first declared value through.
    * **Absence means the fail-closed floor** -- ``fallback_profile``, ``tier``,
      identity, signature state, and the update *commands* (``check_command``,
      ``apply_command``, ``platform_commands``: a command runs unsandboxed as the
      gateway, so a lower tier supplying one is an escalation, not a restriction).
      Authority-only, never ``authority.x or subordinate.x``.  For
      ``fallback_profile`` specifically: an authority that declared none means an
      unusable profile file denies its whole surface, and a subordinate supplying a
      looser fallback would REPLACE that floor -- the escape hatch widened by the
      tier that is not allowed to widen anything.  A subordinate that wants a
      fallback asks the fleet to declare one.

    A new out-of-``controls`` field must be placed in one of the classes here before
    it is composed; copying a neighbour's rule is how the wrong class ships.
    """
    merged: Dict[str, object] = dict(authority.controls)
    for scope, sub_control in subordinate.controls.items():
        existing = merged.get(scope)
        if existing is None:
            merged[scope] = sub_control
            continue
        if scope == _SANDBOX_FLAGS_SCOPE:
            # The reserved sandbox boot flags ride as a plain ``dict`` with no
            # compose archetype, and ``_compose_controls`` refuses a ``dict`` as a
            # type mismatch -- so two tiers that each carry a documented flag would
            # abort boot here.  They are inert (nothing reads them; see
            # ``_SANDBOX_RESERVED_FLAGS``), so no direction widens the ceiling: the
            # authority's value wins per key, and a key it left unset is taken from
            # the subordinate, the "no choice expressed" class.
            merged[scope] = {**sub_control, **existing}  # type: ignore[dict-item]
            continue
        merged[scope] = _compose_controls(existing, sub_control)
    boot = BootControls(
        require_sandbox=authority.boot.require_sandbox or subordinate.boot.require_sandbox,
        allow_terminal=authority.boot.allow_terminal and subordinate.boot.allow_terminal,
        fail_closed=authority.boot.fail_closed or subordinate.boot.fail_closed,
    )
    # ``fallback_profile`` is deliberately NOT listed: ``replace(authority, ...)``
    # keeps the authority's, which is the fail-closed-floor class above.
    return replace(
        authority,
        boot=boot,
        controls=merged,
        updates=_intersect_update_pins(authority.updates, subordinate.updates),
        yolo_max_seconds=_lower_cap(authority.yolo_max_seconds, subordinate.yolo_max_seconds),
    )


def _lower_cap(authority: int, subordinate: int) -> int:
    """Two ``yolo_max_seconds`` caps fold to the lower; 0 (no cap) yields to either."""
    caps = [cap for cap in (authority, subordinate) if cap > 0]
    return min(caps) if caps else 0


def _intersect_update_pins(authority: UpdatePins, subordinate: UpdatePins) -> UpdatePins:
    """Fold the two update pins that are restrictions; keep the commands the authority's.

    ``source`` is an allowlist and ``min_version`` a floor, so a subordinate that sets
    one the authority left empty tightens the ceiling and must not be discarded.  Two
    source globs have no expressible intersection, so where both are set the
    authority's stands (the subordinate cannot loosen it; its own narrowing is not
    applied).  Two floors intersect as the higher one; an unparseable floor imposes
    none (:meth:`UpdatePins.meets_min_version`), so it yields to a parseable one.
    """
    source = authority.source or subordinate.source
    # A channel is a value, not an allowlist: the authority's stands, and a
    # subordinate's applies only where the authority chose none.
    channel = authority.channel or subordinate.channel
    min_version = authority.min_version
    if not authority.min_version:
        min_version = subordinate.min_version
    elif subordinate.min_version:
        upper = _version_tuple(authority.min_version)
        lower = _version_tuple(subordinate.min_version)
        if not upper:
            min_version = subordinate.min_version
        elif lower:
            width = max(len(upper), len(lower))
            if lower + (0,) * (width - len(lower)) > upper + (0,) * (width - len(upper)):
                min_version = subordinate.min_version
    return replace(authority, source=source, min_version=min_version, channel=channel)


def _audit_policy_tier(operation: str, authority_tier: str, lower_tier: str) -> None:
    """Audit a tier composition.  Best-effort, never fatal.

    Only tier NAMES are recorded -- module constants this process authored, never
    a path or URL from the document, following the same rule the distribution
    audit follows: the SEL is readable through agent-reachable surfaces and a
    document's own source may itself be credential-bearing.

    Tightening is not a privilege escalation, so its record is a side-effect rather
    than a precondition: making it fatal would let one unwritable SEL file refuse
    boot on every governed host in a fleet -- a self-inflicted outage far wider than
    the evidence gap it would close. (A record that GATES an action belongs to a path
    where a local document outranks the fleet ceiling; no such path exists in this
    ladder.)
    """
    try:
        sel().log_api_access(
            caller="_host",
            operation=operation,
            outcome="allowed",
            source="startup",
            resources=f"{authority_tier}<-{lower_tier}",
            error="",
        )
    except Exception:
        logger.debug("policy tier SEL emit unavailable", exc_info=True)


def _warn_home_unreadable_beneath_authority_once(home_path: Path, error: Exception) -> None:
    """Say once per process that an unusable home file was skipped beneath a higher tier.

    Unusable covers every shape ``_subordinate_ceiling`` skips: a read error, a JSON
    error, and a document ``parse_policy`` rejects -- and the ``distribution`` peek in
    :func:`load_security_policy` that moves on from a malformed home block. The
    governing tier (central, env or bundled) is unchanged, so nothing loosened; but a
    local operator who meant that file to tighten the ceiling, or to name where the
    ceiling lives, must be able to see that it did not.

    Only the path and the exception CLASS are logged. This line is served by
    ``GET /api/logs``, and a ``PolicyDistribution.from_dict`` error quotes the declared
    source URL, which :func:`_audit_policy_tier` and :func:`_audit_policy_signature`
    deliberately keep off agent-reachable surfaces because a source URL may itself be a
    credential. The full error still reaches the operator through the fatal path when
    the home file is the only ceiling.
    """
    if _process_state.home_unreadable_warned:
        return
    _process_state.home_unreadable_warned = True
    logger.warning(
        "security policy at %s is unusable (%s); skipped, the governing tier is "
        "unchanged. Fix or remove the file for a local tightening to apply.",
        home_path,
        type(error).__name__,
    )


def _warn_env_beneath_central_once(authority_tier: str) -> None:
    """Say once per process that the env document now only TIGHTENS the fleet ceiling.

    The precedence inversion this change ships is silent at the point it bites: a fleet
    whose runbook set ``KIROCREW_SECURITY_POLICY`` to roll back a bad central push finds
    the file demoted to tighten-only with no signal, mid-incident. This is the one shape
    where an upgraded operator's mental model is wrong, so the first time it composes
    the host says so -- a log line and a best-effort SEL row of the same shape as
    the tier-intersect record. Once, because the shape is stable for the process
    lifetime and a warning per compose would be a warning per refresh.
    """
    if _process_state.env_beneath_central_warned:
        return
    _process_state.env_beneath_central_warned = True
    logger.warning(
        "KIROCREW_SECURITY_POLICY is composed BENEATH the %s security policy and can "
        "only tighten it. It no longer overrides the fleet document; to recover from a "
        "bad central push, republish a good document at the source.",
        authority_tier,
    )
    _audit_policy_tier("security_policy_env_tightens_only", authority_tier, TIER_ENV)


def _audit_tier_intersect_once(authority_tier: str, lower_tier: str) -> None:
    """Record a tier intersection the first time that PAIR composes in this process.

    Keyed on the pair rather than a single process-wide flag so a tier that appears
    later (an env document set after boot, a central document that arrives on the
    first successful poll) is still recorded once, while the flagship shape --
    central + home present, recomposed on every app callback -- writes one row, not
    one per interaction. See ``_TierProcessState.tier_intersects_audited``.
    """
    pair = (authority_tier, lower_tier)
    if pair in _process_state.tier_intersects_audited:
        return
    _process_state.tier_intersects_audited.add(pair)
    _audit_policy_tier("security_policy_tier_intersect", authority_tier, lower_tier)


def compose_tier_ladder(
    *tiers: Optional[GovernanceCeiling],
) -> Optional[GovernanceCeiling]:
    """Fold present tiers, highest first, into one ceiling.

    The single implementation of precedence.  The highest present tier is the
    authority; each lower one may only tighten it (:func:`_intersect_ceilings`).
    There is deliberately NO path by which a lower tier replaces the authority: a
    time-boxed rollback grant is deliberately absent (tracked as a follow-up issue,
    see the enterprise governance guide), because a channel that lets a local document
    outrank the fleet ceiling is the override this ladder exists to remove, however it
    is dated.

    Extracted so that boot and a live central refresh cannot drift: a refresh that
    installed the fetched document alone would silently drop every local restriction
    below it.

    In an enterprise build the authority's unnamed scopes are first filled from the
    built-in baseline (:func:`_with_enterprise_baseline`), so a lower tier that names
    such a scope narrows the baseline instead of supplying the scope's whole answer.
    """
    present = [tier for tier in tiers if tier is not None]
    if not present:
        return None
    ceiling = _with_enterprise_baseline(present[0])
    # The distribution pins ride OUTSIDE ``controls`` (they are values the core
    # consumes, not a governed scope), so they are not carried by the per-scope
    # compose the rest of this fold uses -- every path that REBUILDS a ceiling has
    # to preserve them explicitly or the host silently loses the channel it fetches
    # the ceiling from.  One path did: an authority that declared no distribution
    # discarded the pins of the tier beneath it, which switches the central tier off
    # and drops every centrally supplied restriction -- the looser-ceiling failure
    # this ladder exists to prevent.  Tracking the value HERE, once, as the fold
    # proceeds is what makes preserving it structural rather than a rule each
    # ``return`` has to remember.
    #
    # Precedence is unchanged: a tier can only supply pins when NO tier above it
    # declared any, so a lower document still cannot redirect a fetch source its
    # authority chose.  When nothing above expressed a choice there is no choice to
    # redirect away from -- the "absence means no choice" class in
    # :func:`_intersect_ceilings`.
    #
    # An enterprise build never takes them from a lower tier (RFC D2): its source is a
    # build-time constant and its cadence and staleness bound come from the fetched
    # document alone, so a local block must not fill the gap a central one left.
    distribution = ceiling.distribution
    adopt_lower = not _is_enterprise()
    for lower in present[1:]:
        if adopt_lower and not distribution.declared and lower.distribution.declared:
            distribution = lower.distribution
        if lower.tier == TIER_ENV and ceiling.tier == TIER_CENTRAL:
            _warn_env_beneath_central_once(ceiling.tier)
        _audit_tier_intersect_once(ceiling.tier, lower.tier)
        ceiling = _intersect_ceilings(ceiling, lower)
    if distribution != ceiling.distribution:
        # Only when the fold actually moved them, so a ceiling that composes alone
        # is returned as the same object: callers assert that identity to prove a
        # single document was installed untouched.
        return replace(ceiling, distribution=distribution)
    return ceiling


def _with_enterprise_baseline(authority: GovernanceCeiling) -> GovernanceCeiling:
    """*authority* with every scope it does not name filled from the baseline.

    RFC D3 (``docs/request-for-change/rfc-crew-enterprise-edition.md``): in an
    enterprise build only the remote policy may turn a control on, and a scope it
    does not name holds its minimal set. :func:`_intersect_ceilings` lets a lower
    tier supply a scope its authority left out, which is a tightening only while an
    unnamed scope means "unrestricted". Filling the authority first keeps that fold
    tighten-only in an enterprise build too: a local tier composes WITH the baseline
    and can narrow it, never replace it. Only a central authority's named scopes
    replace the baseline; a local authority (no central document present) is
    intersected with it scope by scope. The ``oss`` edition returns *authority*
    unchanged (the same object).
    """
    if not edition.is_enterprise():
        return authority
    return _with_enterprise_value_floors(_with_baseline_controls(authority))


def _with_baseline_controls(authority: GovernanceCeiling) -> GovernanceCeiling:
    """The ``controls`` half of :func:`_with_enterprise_baseline`."""
    baseline = baseline_controls()
    if authority.tier != TIER_CENTRAL:
        # No central document above it: the authority is itself a LOCAL tier, so
        # its named scopes may only narrow the baseline, never replace it.
        merged: Dict[str, object] = dict(authority.controls)
        for scope, control in baseline.items():
            named = merged.get(scope)
            merged[scope] = control if named is None else _compose_controls(control, named)
        return replace(authority, controls=merged)
    # A named scope keeps the baseline of any inner list it leaves out
    # (``BASELINE_INNER_LISTS``), so a local tier cannot supply that list either.
    named = {scope: with_inner_baseline(scope, c) for scope, c in authority.controls.items()}
    missing = {scope: control for scope, control in baseline.items() if scope not in named}
    if not missing and all(named[s] is authority.controls[s] for s in named):
        return authority
    return replace(authority, controls={**named, **missing})


def _with_enterprise_value_floors(authority: GovernanceCeiling) -> GovernanceCeiling:
    """*authority* with the enterprise minimal values outside ``controls`` filled in.

    The same reason as the baseline controls: :func:`_intersect_ceilings` lets a lower
    tier supply ``updates.channel``, ``yolo_max_seconds`` and the Slack
    ``allowed_channel_ids`` posture leaf when the authority left them out, which in an
    enterprise build would replace the minimal value instead of narrowing it. A
    central authority's own values stand; a local authority's may only narrow.
    """
    central = authority.tier == TIER_CENTRAL
    channel = authority.updates.channel if central else ""
    channel = channel or ENTERPRISE_UPDATE_CHANNEL
    cap = authority.yolo_max_seconds
    if not central or cap <= 0:
        cap = min(cap, ENTERPRISE_YOLO_MAX_SECONDS) if cap > 0 else ENTERPRISE_YOLO_MAX_SECONDS
    controls = authority.controls
    chan_map = controls.get("channels")
    if (
        isinstance(chan_map, ScopedMap)
        and chan_map.members.permits("slack").permitted
        and _SLACK_CHANNEL_LEAF not in chan_map.posture.get("slack", {})
    ):
        empty = ScopedRuleset.from_dict(dict(ENTERPRISE_SLACK_CHANNELS), matcher="identifier")
        slack = {**chan_map.posture.get("slack", {}), _SLACK_CHANNEL_LEAF: empty}
        posture = {**chan_map.posture, "slack": slack}
        controls = {**controls, "channels": replace(chan_map, posture=posture)}
    if (
        channel == authority.updates.channel
        and cap == authority.yolo_max_seconds
        and controls is authority.controls
    ):
        return authority
    return replace(
        authority,
        updates=replace(authority.updates, channel=channel),
        yolo_max_seconds=cap,
        controls=controls,
    )


#: The ``slack`` posture leaf naming the channels (``slack.channel_policy.LEAF``).
_SLACK_CHANNEL_LEAF = "allowed_channel_ids"


def _subordinate_ceiling(
    bundled: Optional[Mapping[str, object]],
    home_data: Optional[Dict[str, object]],
    home_error: Optional[Exception],
    home_path: Path,
    env_data: Optional[Dict[str, object]] = None,
    env_path: Optional[Path] = None,
    *,
    beneath_authority: bool = False,
) -> Optional[GovernanceCeiling]:
    """Tiers 2-4, first present wins: env -> bundled -> home.

    Mutually exclusive by design, and collectively the *subordinate*: whichever one
    is present may only tighten whatever authority sits above it.

    *beneath_authority* says a central document is present above this fold. A home
    file that cannot be USED -- unreadable, not JSON, JSON that ``parse_policy``
    rejects, or bytes on which verifying or parsing raises anything at all -- is
    then reported and treated as ABSENT rather than raised: the
    authority still governs, which is the fail-closed direction, and a raise would
    hand whoever owns ``~/.kiro/crew`` a lever that refuses boot and freezes every
    refresh on a fleet host -- an availability lever, not a tightening. One path
    for every shape of unusable, so JSON validity does not split the behaviour.
    With no authority the home file is the only ceiling, so its error stays fatal.

    *env_data* / *env_path* let a caller that ALREADY read the env document hand it
    over instead of having this function read the file a second time -- which matters
    because :func:`load_security_policy` has to peek that document's ``distribution``
    block before the central tier runs, and two reads could disagree.  Omitting them
    does not opt the tier out: the read then happens here instead, so
    a caller that does not care about the declaration keeps the env tier for free.
    """
    if env_data is None and env_path is None:
        env_data, env_path = _read_env_policy()
    if env_data is not None and env_path is not None:
        env_state = _verify_policy_signature(env_data, source=str(env_path))
        return replace(parse_policy(env_data, signature_state=env_state), tier=TIER_ENV)
    if bundled is not None:
        # The bundled tier is NOT exempt from a fleet that opted into
        # require_policy_signature. The plugin-admission manifest signature covers
        # only name/publisher/version/capabilities
        # (admission.PluginManifest.signing_payload), NOT the packaged
        # security_policy.json bytes, so "covered by admission" did not in fact
        # protect the resource -- a tampered bundled policy would have loaded
        # unchecked.
        bundled_state = _verify_policy_signature(bundled, source="companion-bundled resource")
        return replace(parse_policy(bundled, signature_state=bundled_state), tier=TIER_BUNDLED)
    if home_error is not None:
        if beneath_authority:
            _warn_home_unreadable_beneath_authority_once(home_path, home_error)
            return None
        raise PlatformCompositionError(
            f"security policy at {home_path} is unreadable: {home_error}"
        ) from home_error
    if home_data is not None:
        try:
            home_state = _verify_policy_signature(home_data, source=str(home_path))
            parsed = parse_policy(home_data, signature_state=home_state)
        except Exception as exc:
            # Valid JSON the schema refuses (a stale ``version``, an unknown key) is
            # the same lever as an unreadable file, reached one step later -- and so
            # is anything ELSE these two calls raise on user-owned bytes. The catch is
            # total on purpose: this is the one boundary where a document a standard
            # user controls meets the fleet ceiling, and an exception class nobody
            # anticipated (the lone-surrogate UnicodeEncodeError was one) must land
            # on the same skip-and-warn path, not escape the loader as ungoverned.
            if beneath_authority:
                _warn_home_unreadable_beneath_authority_once(home_path, exc)
                return None
            raise
        return replace(parsed, tier=TIER_HOME)
    return None


def _read_env_policy() -> Tuple[Optional[Dict[str, object]], Optional[Path]]:
    """Read the env tier eagerly, for the central tier's declaration peek.

    Returns ``(None, None)`` when ``KIROCREW_SECURITY_POLICY`` is unset.  Unlike the
    home tier the error is RAISED here rather than captured and re-raised further
    down: the env tier outranks bundled and home, so there is no lower tier whose own
    failure could have taken precedence over it, and hoisting the read only moves the
    refusal earlier than the central fetch -- a fleet that pointed this variable at a
    file it cannot read is misconfigured whether or not the poll would have answered.
    The message is the one this path has always produced.
    """
    raw_env = os.environ.get(_POLICY_ENV, "").strip()
    if not raw_env:
        return None, None
    env_path = Path(raw_env)
    try:
        return _read_json_file(env_path), env_path
    except Exception as exc:  # fail-closed: a fleet pointed here on purpose.
        raise PlatformCompositionError(
            f"security policy at {env_path} (from {_POLICY_ENV}) is unreadable: {exc}"
        ) from exc


def _read_home_policy() -> Tuple[Optional[Dict[str, object]], Optional[Exception], Path]:
    """Read the home tier eagerly, capturing rather than raising its error.

    The CENTRAL tier needs to see whether a lower tier declares a ``distribution``
    source, so the read has to happen before it -- but an unreadable home file must
    still raise at its OWN point in precedence, further down, with its own message.
    """
    home_path = _policy_home_path()
    if not home_path.exists():
        return None, None, home_path
    try:
        return _read_json_file(home_path), None, home_path
    except Exception as exc:
        return None, exc, home_path


def compose_installed_ceiling(central: GovernanceCeiling) -> GovernanceCeiling:
    """Re-apply the tier ladder around a freshly fetched *central* document.

    A central refresh replaces exactly one rung of the ladder.  Installing the
    fetched document by itself would drop every local restriction BELOW it, so a host
    tightened at boot would find that tightening silently gone at the first
    successful poll -- a ceiling that loosens itself on a timer.  This routes a
    refresh through the same :func:`compose_tier_ladder` boot uses, so the two cannot
    diverge.

    Returns *central* unchanged when no other tier is present.
    """
    home_data, home_error, home_path = _read_home_policy()
    subordinate = _subordinate_ceiling(
        _process_state.last_bundled,
        home_data,
        home_error,
        home_path,
        beneath_authority=True,
    )
    # Tagged here exactly as boot tags it: ``parse_distributed_policy`` returns an
    # untiered ceiling, and an untagged authority makes ``compose_tier_ladder``'s
    # ``ceiling.tier == TIER_CENTRAL`` guard False -- so a host that reached the fleet
    # document only on a later poll never got the env-tightens-only warning, and its
    # intersect row read ``""<-env``.
    if central.tier != TIER_CENTRAL:
        central = replace(central, tier=TIER_CENTRAL)
    composed = compose_tier_ladder(central, subordinate)
    # compose_tier_ladder only returns None when every argument was None, and
    # ``central`` never is; the fallback keeps the signature honest for mypy.
    #
    # Deliberately NOT recorded as ``last_composed`` here: this is a pure fold, and the
    # caller may go on to REJECT it (``apply_ceiling`` validates the fold before it
    # installs). Recording a rejected fold would make the next 304 poll compare
    # against it, read "nothing moved", and skip -- so a tightening that failed
    # validation once would never install after the operator fixed the document.
    # ``record_composed_ceiling`` is called by the installer, after ``set_context``
    # succeeds.
    return composed if composed is not None else central


def load_security_policy(
    *, bundled_loader: Optional[Callable[[], Optional[Mapping[str, object]]]] = None
) -> Optional[GovernanceCeiling]:
    """Load the enterprise security ceiling, or ``None`` for editable defaults.

    Precedence, highest first.  The top tier is the AUTHORITY; every tier below it
    may only **tighten** it:

    1. the **centrally distributed document** — fetched from ``KIROCREW_POLICY_URL``
       or from the ``distribution.source`` a lower tier declares, served from the
       last-known-good cache when the endpoint is unreachable.  See
       :mod:`kiro_crew.platform.policy_distribution`.
    2. ``KIROCREW_SECURITY_POLICY`` env path — local operator channel.
    3. ``bundled_loader()`` — the companion-bundled resource, supplied by the
       caller that knows the active edition.  The public core passes ``None``.
    4. ``~/.kiro/crew/security_policy.json`` — standalone operator-authored.
    5. None → editable secure-defaults (standalone, ungoverned ceiling).

    **Tiers 2–4 are mutually exclusive** (first present wins among them) and
    collectively form the *subordinate*; the central tier stacks above them.  The
    result is ``central ∘ subordinate`` under :func:`_intersect_ceilings`, so a local
    document can add restrictions but never remove one.

    **Why the env tier sits beneath the central push.**  An env tier that outranks
    the fleet document is a rollback lever, and a rollback lever makes the enterprise
    ceiling advisory: any account that can set an environment variable could point it
    at a permissive file and the fleet's ceiling would never bind.  There is no local
    rollback lever in this ladder: recovery from a bad central push is by
    re-publishing a good document at the source, and a local, time-boxed override is
    tracked as a follow-up issue (see the enterprise governance guide) rather than
    provided here.

    A **present-but-unreadable / invalid** policy at the env path, or at the home
    path when it is the only ceiling, raises ``PlatformCompositionError``
    (fail-closed to strictest), mirroring ``admission.load_admission_policy`` — a
    fleet that meant to enforce something must never silently fall open.  Beneath a
    central document the home file is the one exception: it is skipped with a
    once-per-process warning and the authority governs unchanged
    (:func:`_subordinate_ceiling`), because raising there would let whoever owns
    ``~/.kiro/crew`` refuse boot and freeze every refresh on a fleet host while the
    ceiling itself is unaffected.

    **Signature verification (``identity.signature``).**  Every tier's document is
    checked as it is read, so every ``GovernanceCeiling`` carries its own verdict.
    The verdict is recorded on ``GovernanceCeiling.signature_state`` and is
    **advisory by default**, so existing installs and policy files keep working
    unchanged.  ``require_policy_signature`` in the admission policy makes a verified
    signature mandatory.  Enforcement lives in
    :func:`assert_policy_signature_satisfied`, which runs once on the FINAL composed
    ceiling — this function is called more than once per boot with different
    arguments, so a tier the final ceiling never came from must not be able to abort
    boot.

    **Every failure to load is a composition error.**  Any exception that is not
    already a ``PlatformCompositionError`` -- a ``ValueError`` out of a schema
    helper, a ``KeyError`` on an unexpected shape, an encoding error -- is raised
    as one, naming the stage that failed.  Boot runs this loader under handlers
    that re-raise a composition error and DEGRADE anything else to the ungoverned
    default context, so a raw exception here would turn "the policy could not be
    read" into "there is no policy": a fleet that set a ceiling or
    ``require_policy_signature`` would boot open.  The home file beneath an
    authority keeps its own skip-and-warn path (:func:`_subordinate_ceiling`
    catches everything there), so this does not hand its owner a boot lever.
    """
    stage = _LoadStage()
    try:
        return _load_security_policy(bundled_loader, stage)
    except PlatformCompositionError:
        raise
    except Exception as exc:
        raise PlatformCompositionError(
            f"security policy could not be loaded ({stage.label}): "
            f"{type(exc).__name__}: {exc}; refusing to boot ungoverned (fail-closed)"
        ) from exc


class _LoadStage:
    """Which step of :func:`load_security_policy` is running, for its refusal text."""

    __slots__ = ("label",)

    def __init__(self) -> None:
        self.label = "start"


def _load_security_policy(
    bundled_loader: Optional[Callable[[], Optional[Mapping[str, object]]]],
    stage: _LoadStage,
) -> Optional[GovernanceCeiling]:
    """The body of :func:`load_security_policy`; *stage* names the step in flight."""
    stage.label = "companion-bundled policy"
    # Resolved BEFORE the central tier because the fetch source may be declared by
    # a lower tier's ``distribution`` block, and the peek follows the same
    # precedence the tiers themselves do. The bundled resource is resolved even when
    # the env tier is set: the env tier does not short-circuit the central one, so
    # its declaration still has to be seen.
    bundled = bundled_loader() if bundled_loader is not None else None
    if bundled is not None:
        # Remembered so a later central refresh can still see this tier; see
        # ``_TierProcessState.last_bundled``.
        _process_state.last_bundled = bundled

    stage.label = "home policy read"
    home_data, home_error, home_path = _read_home_policy()
    # Read BEFORE the central tier for the same reason the home tier is: the peek
    # below has to see this document's ``distribution`` block. Read once and passed
    # down to ``_subordinate_ceiling`` so the tier and the peek cannot see different
    # bytes.
    stage.label = f"env policy read ({_POLICY_ENV})"
    env_data, env_path = _read_env_policy()

    # Tier 1 — the centrally distributed ceiling. Returns None when distribution is
    # not configured, or when it is configured, could not be established, and the
    # policy chose to degrade; raises under the fail-closed default. Fully inert —
    # not even an import — when nothing declares a source.
    # ``_POLICY_DISTRIBUTION_CACHE_ONLY_ENV`` is in this condition because a
    # cache-only child (an app backend) is given NO source by design — the tier is
    # what reads the cache the gateway wrote, so gating entry on a source would skip
    # it and drop that child to a local or absent ceiling: exactly the looser-ceiling
    # failure cache-only mode exists to prevent.
    #
    # The variable IS user-settable, and the cache directory IS user-writable, so a
    # local account can set it and plant a document. Two things bound what that buys,
    # and a reviewer proposing to gate the flag on something else should weigh them:
    # (1) the planted document is verified exactly as a fetched one is, so under
    # ``require_policy_signature`` it is refused; (2) it is the same power the account
    # already has through KIROCREW_POLICY_URL, which every tier honours by design.
    # Activating cache-only through a parent-authenticated channel rather than the
    # environment is tracked as a follow-up; it is a new IPC contract, not a change to
    # this ladder.
    central: Optional[GovernanceCeiling] = None
    stage.label = "distribution declaration"
    # The peek follows the SAME order the tiers do, which is why the env document is
    # in the chain: it is the tier directly beneath central, above bundled and home.
    # Leaving it out meant an env-declared ``distribution.source`` was silently
    # ignored and the central tier never loaded from it.
    #
    # An enterprise build skips the peek and always enters the tier (RFC D2): its source
    # is the build-time constant, no local block may tune it, and a missing or
    # unreachable central policy aborts boot.
    enterprise = _is_enterprise()
    declared = (
        None if enterprise else _declared_distribution(env_data) or _declared_distribution(bundled)
    )
    # The home peek is a LOOKUP for a declared source, not a ruling on the home file:
    # a malformed home ``distribution`` block declares no source, so the peek moves on.
    # Whether that file is then skipped or fatal is decided once, by
    # ``_subordinate_ceiling`` -- which re-parses the same document only if the home
    # tier is the one selected, after the central tier is known and after env and
    # bundled have had precedence. Ruling here as well would refuse boot on a host
    # that env or central would have governed with the home file ignored. Moving on
    # is not silent, though: when env or bundled then governs with no central, this
    # is the only place the skipped block is ever seen, and a fleet that mistyped
    # where its ceiling lives must find out from a log line, not from nothing. Same
    # once-per-process warning as the skip in ``_subordinate_ceiling`` (it latches, so
    # a home file that is then also skipped there warns once, not twice).
    if declared is None and not enterprise:
        try:
            declared = _declared_distribution(home_data)
        except PlatformCompositionError as exc:
            _warn_home_unreadable_beneath_authority_once(home_path, exc)
    if (
        enterprise
        or declared is not None
        or os.environ.get(_POLICY_DISTRIBUTION_URL_ENV, "").strip()
        or os.environ.get(_POLICY_DISTRIBUTION_CACHE_ONLY_ENV, "").strip()
    ):
        from kiro_crew.platform.policy_distribution import load_distributed_policy

        stage.label = "central policy"
        distributed = load_distributed_policy(declared)
        if distributed is not None:
            central = replace(distributed, tier=TIER_CENTRAL)

    # Tiers 2–4 — the subordinate, first present wins.
    if env_path is not None:
        stage.label = f"env policy at {env_path} (from {_POLICY_ENV})"
    elif bundled is not None:
        stage.label = "companion-bundled policy"
    else:
        stage.label = f"home policy at {home_path}"
    subordinate = _subordinate_ceiling(
        bundled,
        home_data,
        home_error,
        home_path,
        env_data,
        env_path,
        beneath_authority=central is not None,
    )

    stage.label = "tier composition"
    composed = compose_tier_ladder(central, subordinate)
    # Recorded even when None: a host with no policy at any tier has no fold for a
    # refresh to compare against, and a refresh cannot run there anyway.
    _process_state.last_composed = composed
    if composed is None:
        # No policy at any tier → ungoverned (editable defaults).
        #
        # This function deliberately does NOT refuse boot here, because it cannot tell
        # whether it is the LAST word on the question. ``load_security_policy()`` is
        # called more than once per boot with different arguments: the core calls it
        # with NO bundled_loader first (``bootstrap.build_default_context``) and a
        # companion edition re-invokes it WITH its loader afterwards. "No policy at any
        # tier" is therefore only decidable once composition has finished, so the
        # fail-closed refusal lives in :func:`assert_policy_signature_satisfied`, which
        # boot calls on the FINAL context alongside the other governance floor gates.
        return None
    return composed


def _declared_distribution(
    data: Optional[Mapping[str, object]],
) -> Optional[PolicyDistribution]:
    """Peek a lower tier's ``distribution`` block, for the central tier's source.

    Returns ``None`` when *data* is absent, or declares no source AND the environment
    supplies none either — so the central tier stays completely inert, not even imported,
    on every install that does not use it.

    The environment half of that condition is load-bearing, not defensive. A block with
    settings and no ``source`` is legitimate when ``KIROCREW_POLICY_URL`` supplies the
    address: that is the ordinary two-channel split, where whatever provisions the host owns
    the address while the fleet publishes the cadence, the staleness bound and — the one
    that bites — ``on_unavailable``. Discarding such a declaration parsed it and then threw
    it away, so a fleet that had chosen ``degrade`` silently got the ``fail_closed`` default
    and aborted startup on the first outage. ``resolve_distribution`` overlays the
    environment onto whatever is returned here, so returning the settings is what lets the
    two channels combine at all.

    Deliberately does **not** validate the rest of the document.  This is a peek at
    one key, ahead of the tier that will parse the whole thing: a policy whose
    OTHER keys are malformed must still fail at its own tier with its own message,
    not here with a confusing one about distribution.  A malformed ``distribution``
    block itself does raise, because that block is what this function exists to
    read and a fleet that mistyped where its ceiling comes from must not silently
    get no central distribution at all.
    """
    if not isinstance(data, Mapping):
        return None
    raw = data.get("distribution")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise PlatformCompositionError("security policy 'distribution' must be an object")
    declared = PolicyDistribution.from_dict(raw)
    if declared.enabled:
        return declared
    # Source-less, so only worth carrying when the environment names the address the
    # settings are for. ``from_dict`` already refuses a block with settings and no source
    # from either channel, which is the case that means "the author believed distribution
    # was on"; anything left here is an empty block, which is inert by any reading.
    if os.environ.get(_POLICY_DISTRIBUTION_URL_ENV, "").strip():
        return declared
    return None
