"""The read-only governance policy viewer: ``GET /api/governance/policy``.

Resolves the effective governance ceiling for every scope in ``SCOPE_CATALOG``,
with the level each value comes from, for the Security page; it has no write path.

Served through :mod:`kiro_crew.dashboard.handlers.security`, which callers and
tests import and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.executors import governance_executor
from kiro_crew.platform.context import current_context
from kiro_crew.platform.governance import (
    _SCOPE_ALIASES,
    CAPABILITY,
    ORDINAL,
    RULESET,
    SCOPE_CATALOG,
    SCOPEDMAP,
    CapabilityGate,
    GovernanceCeiling,
    OrdinalControl,
    ScopedMap,
    ScopedRuleset,
    _AndRuleset,
    _compose_controls,
    policy_level_control,
)
from kiro_crew.platform.governance_profiles import (
    HOST_SESSION_KEY,
    bound_surfaces,
    fallback_profile_names,
    resolve_active_scope,
    unknown_profile_scopes,
)

logger = logging.getLogger("kiro_crew.dashboard.handlers.security")


# ──────────────────────────────────────────────────────────────────────────
# Governance policy viewer — READ-ONLY effective ceiling across every scope
# ──────────────────────────────────────────────────────────────────────────
# The enterprise ceiling (Level 1 ``security_policy.json``) and per-surface
# profiles (Level 2 ``profiles/*.json``) are file-authored and deliberately
# un-editable via the UI (the agent cannot even read them — they sit on the
# sensitive-path keystone). This surface lets an operator SEE the resolved
# ceiling — for every governed scope, its effective state and where it comes
# from — without exposing any write path. It mirrors the model's own
# scope-name-agnostic style: a single per-archetype serializer, driven by
# ``SCOPE_CATALOG``, so the view auto-covers any scope a future release (or the
# companion) registers with zero handler edits.


def _serialize_ruleset(value: object) -> dict:
    """Serialize a ``RulesetLike`` (ScopedRuleset or composed ``_AndRuleset``).

    POSTURE ONLY — never the rule CONTENTS. A flat ``ScopedRuleset`` renders as
    ``{mode, allow_count, deny_count}``: the set MODE and how many entries it
    holds, but NOT the entries themselves. A composed ``_AndRuleset`` renders as
    ``{mode: "intersect", components: [...]}`` so the viewer can still show
    "narrowed by both levels".

    Why counts, not contents: this endpoint is served on the dashboard, which is
    reachable by the agent's own browser tooling (Playwright extension mode
    attaches to the user's authenticated Chrome). Serializing the exact allow/deny
    globs and command patterns would hand the agent a machine-readable copy of the
    security ceiling it is fenced from reading on disk (``security_policy.json`` /
    ``profiles`` are on the ``is_sensitive_path`` keystone) — and knowing the exact
    deny patterns is precisely what lets an agent craft an evasion. The human
    operator reads the authoritative contents from the files directly (outside the
    agent sandbox); the viewer shows POSTURE (which scopes are governed, their
    shape and strictness), not the secret contents.
    """
    if isinstance(value, ScopedRuleset):
        return {
            "mode": value.mode,
            "allow_count": len(value.allow),
            "deny_count": len(value.deny),
        }
    if isinstance(value, _AndRuleset):
        return {
            "mode": "intersect",
            "components": [
                _serialize_ruleset(value.outer),
                _serialize_ruleset(value.inner),
            ],
        }
    return {}


def _serialize_control(archetype: str, value: object) -> dict:
    """Serialize one effective archetype value to a UI-friendly dict.

    Dispatch is by ARCHETYPE (``spec.kind``), never by scope name — the same
    decoupling the evaluator uses — so a newly registered scope serializes with
    no edit here as long as it reuses one of the four archetypes.
    """
    if value is None:
        return {}
    if archetype == RULESET:
        return _serialize_ruleset(value)
    if archetype == ORDINAL and isinstance(value, OrdinalControl):
        return {"scale": value.scale, "floor": value.value}
    if archetype == CAPABILITY and isinstance(value, CapabilityGate):
        return {
            "enabled": value.enabled,
            "inner": {name: _serialize_ruleset(rs) for name, rs in value.scopes.items()},
        }
    if archetype == SCOPEDMAP and isinstance(value, ScopedMap):
        return {
            "members": _serialize_ruleset(value.members),
            "posture": {
                member: {leaf: _serialize_ruleset(rs) for leaf, rs in leaves.items()}
                for member, leaves in value.posture.items()
            },
        }
    return {}


def _surface_scope_note(source: str, policy_control: object, archetype: str) -> str:
    """Machine-readable caveat naming WHOSE ceiling a row describes.

    Returns one of ``""`` / ``"host_profile"`` / ``"policy_wide"``:

    * ``host_profile`` — the value is the HOST surface's posture alone. This is
      what stops the viewer mis-reporting a host-only pin as an install-wide
      "off": the shipped host profile disables ``cron``/``messaging``/``spawn``
      because the host process performs none of them, while the cron and messaging
      surfaces enable them under their own profiles.
    * ``policy_wide`` — a Level-1 ceiling produces this value, and policy binds
      every surface, so the row IS install-wide.
    * ``""`` — ungoverned; there is nothing to caveat.

    A composed ``policy+profile`` row needs the policy control itself, not just the
    source label, to classify correctly. For a CAPABILITY, ``enabled`` composes by
    AND across levels, so when the POLICY half is already off the result is off on
    every surface however permissive the profile is — reporting that as
    host-scoped would UNDER-claim a Level-1 ceiling, the same mislabel class this
    function exists to prevent, merely inverted. Only the profile-decides case is
    host-scoped.

    A string enum rather than a rendered sentence: this is contract data the
    frontend maps to a translated string, so it never ships English into a
    JSON body (per the i18n gate) and stays stable across languages.
    """
    if source == "policy":
        return "policy_wide"
    if source == "policy+profile":
        # A policy capability gate that is already OFF binds every surface.
        if (
            archetype == CAPABILITY
            and isinstance(policy_control, CapabilityGate)
            and not policy_control.enabled
        ):
            return "policy_wide"
        return "host_profile"
    if source == "profile":
        return "host_profile"
    return ""


def _other_bound_surfaces() -> list[str]:
    """Surface ids (excluding ``host``) that carry their OWN bound profile.

    Names only, sorted — never a control, count, or rule from those profiles. The
    viewer needs this to answer the question a host-only row provokes: "is cron
    really off, or is that just the host's ceiling?" Listing which surfaces are
    separately governed makes the host row's scope legible without widening the
    endpoint's POSTURE-only contract (see :func:`_serialize_ruleset`).

    Reads the same profile store ``resolve_active_scope`` uses, via the store's own
    ``bound_surfaces()`` accessor. Best-effort: on any store error it returns
    ``[]``, so the caveat degrades to absent rather than breaking the snapshot.
    """
    try:
        return [s for s in bound_surfaces() if s != "host"]
    except Exception:
        logger.warning("could not enumerate bound surfaces", exc_info=True)
        return []


def _distribution_posture() -> dict:
    """Central-policy-distribution posture for the viewer.  Never raises.

    A thin, total wrapper over
    :func:`kiro_crew.platform.policy_distribution.distribution_posture`, which is
    already posture-only (a scheme, never the URL; enums, never prose).  It exists
    for two reasons the caller cannot supply.

    It must not raise, because it is called from BOTH literals in the snapshot
    below — including the fail-safe one, whose whole job is to answer when
    something else already went wrong.

    And it must not FETCH.  This snapshot is browser-triggerable and the viewer
    repolls it every 30 seconds, so a network round trip here would put the fleet's
    policy endpoint behind an open dashboard tab.  ``distribution_posture`` reads
    only the parsed pins, the on-disk cache metadata and the refresher's in-memory
    record — the background refresher is what talks to the network.

    The degraded return carries an ``error_code``, not a bare ``configured: False``.
    The viewer renders the distribution block on ``configured || error_code``, so
    without the code this branch would render NOTHING — and a centrally-governed
    host whose posture could not be read would show the reassuring "no enterprise
    policy in effect" card, the exact confusion this field was added to remove.
    """
    try:
        from kiro_crew.platform.policy_distribution import distribution_posture

        return distribution_posture()
    except Exception:
        logger.warning("policy distribution posture unavailable", exc_info=True)
        # The import may be what failed, so the code cannot come from the module.
        return {"configured": False, "error_code": "misconfigured"}


def build_governance_policy_snapshot() -> dict:
    """Compute the effective governance ceiling across ALL scopes (host surface).

    Iterates ``SCOPE_CATALOG`` (so the list stays complete and auto-extends when
    a scope is registered) and, for each scope, intersects the boot-frozen
    POLICY control with the host-surface PROFILE control using the model's OWN
    composition algebra (``_compose_controls`` — the same helper the evaluator's
    ``compose_profiles`` path uses); it does not re-implement ``policy ∩
    profile``. A scope governed by neither level is reported ``ungoverned`` (it
    permits — the standalone default), so with NO policy and NO profile every
    scope is ``ungoverned`` and the response is byte-identical to a standalone
    host.

    **The reported ceiling is the HOST surface's, and only the host's.** The host
    profile governs in-process actions that no user-facing surface drives (app
    activation, workspace admission), so it legitimately pins capabilities like
    ``cron`` and ``messaging`` OFF — the host process itself never schedules a job
    or sends a message. Those same capabilities are typically ENABLED for the
    surfaces that do use them, under their own narrower profiles. A row therefore
    describes one surface's posture, never the whole install's: ``scope_note``
    carries that caveat per row (see :func:`_surface_scope_note`), so a viewer
    cannot render "Disabled by policy" as though the feature were off everywhere.

    Synchronous — reading the ceiling is in-memory, but ``resolve_active_scope``
    may read profile files, so async callers MUST offload it (see
    :func:`build_governance_policy_snapshot_async`). Fail-SAFE for DISPLAY: any
    unexpected governance error yields a well-formed ``unavailable`` response
    rather than raising, so a resolution glitch never breaks the Security page
    (this endpoint enforces nothing).
    """
    try:
        ceiling = getattr(current_context(), "governance", None)
        if ceiling is not None and not isinstance(ceiling, GovernanceCeiling):
            ceiling = None
        # Host-surface profile (bind: {type: surface, id: host}); usually None.
        profile = resolve_active_scope(HOST_SESSION_KEY)

        scopes: list[dict] = []
        for scope, spec in SCOPE_CATALOG.items():
            # Skip the folders.* aliases: they normalize to filesystem.* at parse
            # time, so a control is never stored under the alias key — emitting it
            # would be a permanently-ungoverned duplicate row.
            if scope in _SCOPE_ALIASES:
                continue
            # The policy level as ``resolve`` enforces it: in an enterprise build an
            # unnamed scope reports the built-in baseline, not "ungoverned".
            policy_control = policy_level_control(ceiling, scope)
            profile_control = profile.get(scope) if profile is not None else None

            if policy_control is not None and profile_control is not None:
                source = "policy+profile"
                effective = _compose_controls(policy_control, profile_control)
            elif policy_control is not None:
                source = "policy"
                effective = policy_control
            elif profile_control is not None:
                source = "profile"
                effective = profile_control
            else:
                source = "ungoverned"
                effective = None

            scopes.append(
                {
                    "scope": scope,
                    "archetype": spec.kind,
                    "governed": effective is not None,
                    "source": source,
                    "scope_note": _surface_scope_note(source, policy_control, spec.kind),
                    "detail": _serialize_control(spec.kind, effective),
                }
            )

        return {
            "version": ceiling.version if ceiling is not None else None,
            "has_policy": ceiling is not None,
            "profile": profile.name if profile is not None else None,
            # The snapshot resolves the HOST-surface profile only; narrower
            # per-surface/app/task profiles can tighten a scope further at runtime.
            # The field makes that scope explicit so the viewer never overclaims to
            # be the whole effective ceiling for every surface.
            "surface": "host",
            # Surfaces OTHER than host that carry their own profile. Names only.
            # The host profile disables cron/messaging/spawn because the host
            # process performs none of them; those surfaces enable what they need
            # under these profiles. Naming them is what lets the viewer show a
            # host row as one surface's posture instead of an install-wide "off".
            "other_bound_surfaces": _other_bound_surfaces(),
            # Every profile currently replaced by the deny-all fallback, by file
            # stem, sorted. NAMES ONLY — the same exposure contract as
            # ``other_bound_surfaces`` above, and no rule content, so the
            # posture-only rule in :func:`_serialize_ruleset` still holds.
            #
            # Deliberately NOT narrowed to the host profile. The store knows every
            # unusable profile, and a bound non-host one (``cron``, ``subagent``,
            # an app bind) deny-alls its own surface just as silently: it appears
            # in ``other_bound_surfaces`` looking healthy while the operator is
            # back to reading server logs, which is the exact harm this signal
            # exists to remove. Reporting the set also removes an ambiguity a
            # host-only boolean had: matching a resolved profile's ``name``
            # against file stems mislabels a profile whose declared name collides
            # with a broken sibling's stem, whereas these ARE the stems.
            "fallback_profiles": sorted(fallback_profile_names()),
            # Capability scopes a profile declares that THIS build does not
            # register, by file stem → sorted scope names. NAMES ONLY, same
            # exposure contract as the two fields above.
            #
            # These profiles LOADED (enforcement is intact and the key governs
            # nothing here) so they are absent from ``fallback_profiles`` — which
            # is exactly why they need their own field: the asymmetric key-open
            # tolerance means a tolerated key is otherwise visible only in a
            # startup log line. Non-empty is normal when the data home is shared
            # with an edition that registers extra rows, and is a typo signal
            # when it is not.
            "unknown_profile_scopes": {
                stem: sorted(scopes) for stem, scopes in unknown_profile_scopes().items()
            },
            # Whether this host's ceiling is fetched from a central source, and how
            # that fetch is going. The first field here that is not about the
            # ceiling's CONTENT but about its PROVENANCE, and it earns the space
            # because the alternative is worse: a host configured to follow a fleet
            # policy whose first fetch has not landed has no policy and no profile,
            # so without this the viewer renders a reassuring "no enterprise policy
            # in effect" for a machine that is supposed to have one.
            #
            # POSTURE ONLY, same contract as the fields above and then some:
            # ``distribution_posture`` reports the source's SCHEME and never its
            # URL. A stem was judged not to be rule content; an endpoint is a
            # stronger claim — it is the fleet's control plane, and this page is
            # reachable by the agent's own browser tooling, so naming it would tell
            # a prompt-injected agent where to aim. Every value is a number, a
            # boolean, or a machine-readable enum, so no English ships in this body.
            "distribution": _distribution_posture(),
            "unavailable": False,
            "scopes": scopes,
        }
    except Exception:
        # Display must never 500 the Security page on a governance glitch.
        logger.warning("governance policy snapshot unavailable", exc_info=True)
        return {
            "version": None,
            "has_policy": False,
            "profile": None,
            "surface": "host",
            "other_bound_surfaces": [],
            "fallback_profiles": [],
            "unknown_profile_scopes": {},
            # Present on the fail-safe literal too: the frontend reads this key
            # unconditionally, and a host that could not resolve its governance is
            # exactly when an operator needs to be told where its ceiling comes from.
            "distribution": _distribution_posture(),
            "unavailable": True,
            "scopes": [],
        }


async def build_governance_policy_snapshot_async() -> dict:
    """Build the governance-policy snapshot off the event loop.

    ``build_governance_policy_snapshot`` may walk the profile store (filesystem)
    via ``resolve_active_scope``, so it is offloaded to the dedicated
    ``governance_executor`` (``mc-gov``) — NOT the shared default pool — since this
    GET is browser-triggerable and profile-store I/O on a slow FS would otherwise
    pin the default-pool workers the event loop shares for DNS.
    """
    return await asyncio.get_running_loop().run_in_executor(
        governance_executor(), build_governance_policy_snapshot
    )


async def api_governance_policy(request: web.Request) -> web.Response:
    """GET /api/governance/policy — effective ceiling across all scopes (read)."""
    return web.json_response(await build_governance_policy_snapshot_async())
