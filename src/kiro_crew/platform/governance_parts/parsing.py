"""Turning a policy or profile document into its carrier.

``parse_policy`` and ``parse_profile``, the per-scope control parser
(``_parse_controls``) with the structural keys, key-open namespaces and sandbox boot
flags it accepts, and ``_read_json_file``. Validation of what a document may say goes
here.

Served through :mod:`kiro_crew.platform.governance`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance_parts.archetypes import (
    CapabilityGate,
    OrdinalControl,
    ScopedMap,
    ScopedRuleset,
    _compose_controls,
)
from kiro_crew.platform.governance_parts.carriers import (
    SIGNATURE_UNCHECKED,
    Bind,
    BootControls,
    GovernanceCeiling,
    PolicyDistribution,
    Profile,
    UpdatePins,
    _apply_agentcore_gateway_url,
    _apply_agentcore_posture,
    _apply_agentcore_workload_name,
    _capability_raw_for_gate,
)
from kiro_crew.platform.governance_parts.catalog import (
    _SCOPE_ALIASES,
    CAPABILITY,
    ORDINAL,
    RULESET,
    SCOPE_CATALOG,
    SCOPEDMAP,
    ScopeSpec,
)

logger = logging.getLogger("kiro_crew.platform.governance")


# Schema version this loader understands.  A file declaring a different version
# fails closed rather than being parsed under guessed semantics.
POLICY_VERSION = 1


# Profile name schema (Appendix A): lowercase alnum + hyphen, alnum-initial.
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _parse_control(scope: str, spec: ScopeSpec, raw: object, *, is_policy: bool) -> object:
    """Parse one raw JSON control value into its archetype, per the catalog."""
    if not isinstance(raw, dict):
        if spec.kind == ORDINAL and isinstance(raw, str):
            return OrdinalControl(scale=spec.ordinal_scale, value=raw)
        raise PlatformCompositionError(f"scope {scope!r} must be an object")
    if spec.kind == RULESET:
        ruleset = ScopedRuleset.from_dict(raw, matcher=spec.matcher, scope=scope)
        for floor in spec.always_permitted:
            # An ``always_permitted`` identifier is one this scope may not forbid.
            # Two reasons qualify, and the catalog entry says which applies: the
            # scope cannot FUNCTION without it (``approval_modes``' ``normal``, the
            # interactive floor -- denying it would brick tool approval), or its
            # enforcement is NOT IMPLEMENTED yet, so accepting a deny would
            # advertise a control that does not hold. Either way, refuse at parse
            # time rather than boot into a state the policy misdescribes -- and
            # refuse HERE because the trust-root ``security_policy.json`` is the one
            # file the dashboard may not rewrite to repair itself.
            #
            # An ALLOW-list that merely omits the floor denies it just as
            # effectively, which is why this asks the resolved ruleset rather than
            # inspecting the deny list.
            if not ruleset.permits(floor).permitted:
                raise PlatformCompositionError(
                    f"scope {scope!r} must not forbid {floor!r} - it is not deniable "
                    f"in this scope, either because the scope cannot function "
                    f"without it or because its enforcement is not implemented yet; "
                    f"a deny is refused rather than accepted and left unenforced. "
                    f"Omit it from 'deny', or include it in 'allow'"
                )
        return ruleset
    if spec.kind == ORDINAL:
        # Accept {"value": ...} / {"min_level": ...} / {"mode": ...}; a bare
        # string value is handled above.
        value = raw.get("value") or raw.get("min_level") or raw.get("mode")
        if not isinstance(value, str):
            raise PlatformCompositionError(f"ordinal scope {scope!r} needs a string value")
        return OrdinalControl(scale=spec.ordinal_scale, value=value)
    if spec.kind == CAPABILITY:
        gate_raw: Mapping[str, object] = _capability_raw_for_gate(scope, raw, is_policy=is_policy)
        return CapabilityGate.from_dict(
            gate_raw, default_enabled=spec.capability_default, scope_matchers=spec.scope_matchers
        )
    if spec.kind == SCOPEDMAP:
        return ScopedMap.from_dict(raw, allow_posture=is_policy)
    raise PlatformCompositionError(f"scope {scope!r} has unknown archetype {spec.kind!r}")


# Structural (non-governed) keys consumed by parse_policy/parse_profile, not as
# governed scopes.
_STRUCTURAL_KEYS = frozenset(
    {
        "version",
        "boot",
        "identity",
        "name",
        "bind",
        "extends",
        "description",
        "updates",
        "distribution",
        "fallback",
    }
)


# Top-level keys that are "key-open" namespaces: every child becomes a
# ``<key>.<child>`` catalog scope (the enforcer registry, not the loader, decides
# which are known — an unknown child fails closed).  ``capabilities`` is the
# built-in example; the companion can register e.g. ``vault.*`` and have it parse
# WITHOUT editing this loader (the extensibility contract).
_KEY_OPEN_NAMESPACES = frozenset({"capabilities"})

# ``sandbox`` carries the ordinal ``min_level`` plus non-governed boot flags
# (require_isolation, env_scrub_prefixes) kept raw under a reserved scope.
_SANDBOX_FLAGS_SCOPE = "sandbox._flags"

# The EXHAUSTIVE set of non-governed ``sandbox`` boot flags that may ride raw
# under ``_SANDBOX_FLAGS_SCOPE``.  Anything else under ``sandbox`` fails closed
# like every other fixed-child namespace (filesystem, folders, network).
#
# Why an allowlist rather than blanket tolerance: ``sandbox.min_level`` is the
# ordinal floor with the widest blast radius in the catalog, and the reserved
# scope is WRITE-ONLY — nothing reads it back.  Blanket tolerance therefore
# turned a one-character typo into silent total loss of the floor:
# ``{"sandbox": {"min_levl": "strict"}}`` parsed clean, reported OK from
# ``kirocrew policy validate``, rendered as a governed scope in
# ``kirocrew policy show``, and left ``sandbox.min_level`` absent — so
# ``sandbox._governance_sandbox_floor()`` read "ungoverned" and clamped nothing.
# Green validation with zero enforcement is precisely the failure class this
# model exists to prevent, so an unrecognized child must be loud.
#
# Why not reject EVERY non-``min_level`` child: these two names are documented
# in-code above as reserved, and a deployed policy may already carry one.
# Rejecting them would abort boot on a policy that parses today, converting a
# diagnostic gap into an availability regression.  Keeping the two known names
# accepted costs nothing (they are inert either way) and confines the new
# strictness to names nobody has been told are valid.
_SANDBOX_RESERVED_FLAGS = frozenset({"require_isolation", "env_scrub_prefixes"})


def _parse_controls(
    data: Mapping[str, object],
    *,
    is_policy: bool,
    unknown_out: Optional[List[str]] = None,
    profile_name: str = "",
) -> Dict[str, object]:
    """Flatten a policy/profile JSON body into the catalog's dotted-scope map.

    Data-driven against ``SCOPE_CATALOG`` so a newly ``register_scope``'d family
    parses with no loader edit (the extensibility contract):

    * a top-level key that is itself a flat catalog scope is taken directly;
    * a nested namespace (``<key>.<sub>`` exists in the catalog) descends
      generically — every child ``<sub>`` that resolves to a catalog scope is
      parsed, and an unknown child fails closed;
    * ``capabilities`` (and any registered key-open namespace) maps each child to
      ``<key>.<child>``, deferring the known/unknown decision to the catalog.

    An unknown governed key fails closed (tamper-evidence / Rule 8), with ONE
    narrow exception: see ``unknown_out``.

    ``unknown_out`` opts into KEY-OPEN TOLERANCE and is passed ONLY by
    :func:`parse_profile`. When present, an unknown child of a key-open namespace
    (``capabilities.*``) does not invalidate the profile — but ONLY in the
    ASYMMETRIC case where the child is a dict whose ``enabled`` is exactly
    ``True``. Such a child is skipped, warned about, and appended here for the
    caller to record on ``Profile.unknown_scopes``. Every KNOWN sibling in the same
    block still parses and is still enforced.

    Every OTHER unknown child — ``enabled: false``, ``enabled`` absent, a non-dict
    value, anything malformed — RAISES exactly as it did before the tolerance
    existed, so the loader degrades the whole profile to its bind-preserving
    deny-all fallback.

    Why the rule is asymmetric, and why it is profile-only:

    * a profile is **narrow-only**, and the intersection is performed by
      :func:`resolve` (rule-2 intersect of ceiling ∘ profile), so an unknown
      ``enabled: true`` cannot change ANY decision in ANY build: if the scope is
      unregistered there is nothing to decide, and if a later build registers it
      the profile merely declines to narrow it. Skipping it therefore loses
      nothing, and that holds even when the key is a typo;
    * an unknown NARROWING (``enabled: false``, or a malformed child whose intent
      cannot be read) is indistinguishable from a typo'd narrowing of a CORE
      capability — ``{"spwan": {"enabled": false}}`` reads exactly like a failed
      attempt to disable ``spawn``. Honoring the operator's intent requires
      failing closed there: the loud deny-all fallback surfaces the typo, whereas
      tolerating it would silently grant what the operator tried to deny;
    * the POLICY/ceiling path keeps raising (``unknown_out`` stays ``None``), so
      tamper-evidence on the ceiling is unchanged (Rule 8). The policy's optional
      top-level ``fallback`` object parses with ``is_policy=False`` but no
      ``unknown_out``, so it fails closed at boot;
    * unknown **top-level** governed families still fail closed either way — only
      children inside an already-known key-open family are eligible.

    The concrete defect this fixes: the internal edition ``register_scope``s extra
    ``capabilities.*`` rows and seeds a profile naming them with ``enabled: true``;
    a public edition reading the SAME data home rejects that whole profile and
    degrades the surface to deny-all. Cross-edition data-home sharing is supported,
    so an inert declaration must not be fatal.

    Accepted residual: a typo'd capability name carrying ``enabled: true`` is
    tolerated rather than surfaced. That is inert by the first bullet above (it
    could not have changed a decision), so the cost is a missed diagnostic, not a
    permission change. ``Profile.unknown_scopes`` is what surfaces it.

    This relies on ``SCOPE_CATALOG`` being APPEND-ONLY (a scope is added or
    retired, never renamed in place) — otherwise a renamed key declared
    ``enabled: true`` would be silently tolerated instead of surfacing as the
    migration it is.
    """
    controls: Dict[str, object] = {}
    # Precompute, per top-level key, the set of dotted children in the catalog.
    nested_children: Dict[str, set] = {}
    for scope in SCOPE_CATALOG:
        head, sep, tail = scope.partition(".")
        if sep and not tail.startswith("_"):
            nested_children.setdefault(head, set()).add(tail)

    def take(scope: str, raw: object, *, tolerate_unknown: bool = False) -> None:
        # Normalize an alias (folders.* → filesystem.*) so it lands in the SAME
        # control key the gate queries, and a profile's folders.* actually narrows
        # the policy's filesystem.* ceiling.
        scope = _SCOPE_ALIASES.get(scope, scope)
        spec = SCOPE_CATALOG.get(scope)
        if spec is None:
            # ASYMMETRIC TOLERANCE (profiles only, key-open families only): an
            # unknown child whose payload is EXACTLY ``{"enabled": true}`` is
            # inert — the intersection lives in resolve() and a profile is
            # narrow-only, so declining to narrow an unregistered scope cannot
            # change any decision in any build. The payload must be the exact
            # one-key dict: a capability payload can carry inner narrowing
            # rulesets (``spawn`` has ``agents``, ``publish`` has
            # ``destinations``), so ``{"enabled": true, "agents": {...}}`` is
            # an ENABLE-plus-NARROWING and skipping it would drop the inner
            # narrowing — that shape, any other extra key, ``enabled: false``,
            # a truthy-but-not-True enabled (``1``, ``"true"``), or a non-dict
            # is indistinguishable from a typo'd narrowing of a core
            # capability, so it must fail closed: honoring the operator's
            # intent means the loud deny-all fallback rather than silently
            # granting what they tried to deny. Key-set equality plus an ``is
            # True`` identity check (never ``==``, which admits ``1``) is what
            # makes the inertness proof airtight.
            if (
                tolerate_unknown
                and unknown_out is not None
                and isinstance(raw, dict)
                and set(raw.keys()) == {"enabled"}
                and raw["enabled"] is True
            ):
                logger.warning(
                    "profile %r declares capability scope %r which this build does not "
                    "register; skipping it because it is declared enabled=true, which "
                    "cannot change any decision (a profile only narrows, and the "
                    "intersection is applied in resolve). Known capabilities in this "
                    "profile are still enforced. If this name is a typo, correct it — "
                    "the same key with enabled=false would instead fail closed.",
                    profile_name or "<unnamed>",
                    scope,
                )
                unknown_out.append(scope)
                return
            raise PlatformCompositionError(f"unknown governed scope {scope!r} (fail-closed)")
        if scope in controls:
            # Both folders.* and filesystem.* present (or a dup) → intersect so
            # neither silently wins; keeps the narrow-only invariant.
            existing = controls[scope]
            new = _parse_control(scope, spec, raw, is_policy=is_policy)
            controls[scope] = _compose_controls(existing, new)
        else:
            controls[scope] = _parse_control(scope, spec, raw, is_policy=is_policy)

    for key, raw in data.items():
        if key in _STRUCTURAL_KEYS:
            continue
        if key in SCOPE_CATALOG:
            # A flat top-level scope (e.g. tools, mcp, commands, channels).
            take(key, raw)
        elif key in _KEY_OPEN_NAMESPACES and isinstance(raw, dict):
            # Every child is a catalog scope; an unknown child fails closed in
            # take() for a POLICY, and is tolerated + recorded for a PROFILE.
            tolerate = not is_policy and unknown_out is not None
            for child, child_raw in raw.items():
                take(f"{key}.{child}", child_raw, tolerate_unknown=tolerate)
        elif key in nested_children and isinstance(raw, dict):
            # A fixed-child namespace (filesystem, folders, network, sandbox, …).
            for sub, sub_raw in raw.items():
                dotted = f"{key}.{sub}"
                if dotted in SCOPE_CATALOG:
                    take(dotted, sub_raw)
                elif key == "sandbox" and sub in _SANDBOX_RESERVED_FLAGS:
                    # A KNOWN non-governed boot flag rides raw under a reserved
                    # scope.  The membership test is what keeps a typo'd
                    # ``min_level`` from being swallowed here instead of
                    # reaching the fail-closed raise below.
                    controls.setdefault(_SANDBOX_FLAGS_SCOPE, {})  # type: ignore[arg-type]
                    controls[_SANDBOX_FLAGS_SCOPE][sub] = sub_raw  # type: ignore[index]
                else:
                    raise PlatformCompositionError(f"unknown governed key {dotted!r} (fail-closed)")
        else:
            raise PlatformCompositionError(f"unknown governed key {key!r} (fail-closed)")
    return controls


# ──────────────────────────────────────────────────────────────────────────
# Loader
# ──────────────────────────────────────────────────────────────────────────
def _coerce_boot_flag(
    boot_raw: Mapping[str, object], key: str, *, default: bool, closed: bool
) -> bool:
    """Strict read of one ``boot`` gate flag.

    A real boolean is honoured and an absent key takes the documented
    default. Anything else (including explicit null) is warned about and
    read in the fail-closed direction: a bare ``bool()`` would read a
    ``"false"`` string as true, which is the fail-open direction.
    """
    if key not in boot_raw:
        return default
    value = boot_raw[key]
    if isinstance(value, bool):
        return value
    # Log the TYPE, never the value: a mis-typed flag can carry a secret
    # (a credential pasted into the policy), and this warning lands in the
    # persistent gateway log. The key names the misconfiguration; the type
    # is all an operator needs to fix it.
    logger.warning(
        "security policy boot flag %r must be a boolean, got %s — reading fail-closed as %s",
        key,
        type(value).__name__,
        closed,
    )
    return closed


def parse_policy(
    data: Mapping[str, object], *, signature_state: str = SIGNATURE_UNCHECKED
) -> GovernanceCeiling:
    """Parse a policy mapping into a frozen ``GovernanceCeiling``.

    Fails closed (raises ``PlatformCompositionError``) on any structural problem
    so a malformed-but-present policy never silently degrades to ungoverned.

    ``signature_state`` is supplied by :func:`load_security_policy`, which is the
    only caller that has the trust root.  It defaults to ``SIGNATURE_UNCHECKED``
    (not ``SIGNATURE_UNSIGNED``) because a direct ``parse_policy`` call has PROVEN
    nothing about provenance — reporting "unsigned" would conflate "we looked and
    found no signature" with "nobody looked".
    """
    version = data.get("version")
    if version != POLICY_VERSION:
        raise PlatformCompositionError(
            f"security policy version {version!r} unsupported (expected {POLICY_VERSION})"
        )
    boot_raw = data.get("boot")
    if not isinstance(boot_raw, dict):
        raise PlatformCompositionError("security policy requires a 'boot' object")
    boot = BootControls(
        require_sandbox=_coerce_boot_flag(boot_raw, "require_sandbox", default=True, closed=True),
        allow_terminal=_coerce_boot_flag(boot_raw, "allow_terminal", default=False, closed=False),
        fail_closed=_coerce_boot_flag(boot_raw, "fail_closed", default=True, closed=True),
    )
    controls = _parse_controls(data, is_policy=True)
    composed_posture = _apply_agentcore_posture(data, controls, boot)
    composed_gateway_url = _apply_agentcore_gateway_url(data, controls, boot)
    composed_workload_name = _apply_agentcore_workload_name(data, controls, boot)
    identity = data.get("identity") or {}
    issuer = str(identity.get("issuer", "")) if isinstance(identity, dict) else ""
    signature = str(identity.get("signature", "")) if isinstance(identity, dict) else ""
    raw_updates = data.get("updates")
    if raw_updates is not None and not isinstance(raw_updates, dict):
        raise PlatformCompositionError("security policy 'updates' must be an object")
    raw_distribution = data.get("distribution")
    if raw_distribution is not None and not isinstance(raw_distribution, dict):
        raise PlatformCompositionError("security policy 'distribution' must be an object")
    # Optional operator-declared fallback profile (see GovernanceCeiling.fallback_profile).
    # Parsed as a narrow-only PROFILE (is_policy=False): resolve() intersects it with
    # this ceiling like any per-surface profile when a profile FILE is unusable, so it
    # can only narrow the ceiling. Unknown scopes inside it fail closed at boot, on
    # BOTH sides of the key-open asymmetry: this call passes no ``unknown_out``, so a
    # ``capabilities.*`` child this build does not register raises here even when it
    # declares ``enabled: true`` — the case a real profile FILE tolerates. The policy
    # document is the tamper-evidence surface (Rule 8), so it gets no tolerance.
    raw_fallback = data.get("fallback")
    fallback_profile: "Optional[Profile]" = None
    if raw_fallback is not None:
        if not isinstance(raw_fallback, dict):
            raise PlatformCompositionError("security policy 'fallback' must be an object")
        # The fallback payload is a controls-only profile body. EVERY structural
        # key (name/bind/extends/updates/fallback/…) is skipped by _parse_controls,
        # so a payload like {"extends": "lockdown"} would silently lose its intent
        # and the fallback would resolve to ceiling-permitted controls instead of the
        # intended restriction. Reject any structural key so such a fallback fails
        # closed at boot rather than vanishing.
        stray = sorted(_STRUCTURAL_KEYS & raw_fallback.keys())
        if stray:
            raise PlatformCompositionError(
                f"security policy 'fallback' may not contain structural key(s) {stray} — "
                "it is a controls-only profile body"
            )
        fallback_profile = Profile(
            name="_fallback", controls=_parse_controls(raw_fallback, is_policy=False)
        )
    return GovernanceCeiling(
        version=POLICY_VERSION,
        boot=boot,
        controls=controls,
        identity_issuer=issuer,
        identity_signature=signature,
        signature_state=signature_state,
        updates=UpdatePins.from_dict(raw_updates or {}),
        distribution=PolicyDistribution.from_dict(raw_distribution or {}),
        fallback_profile=fallback_profile,
        agentcore_identity_posture=composed_posture,
        agentcore_gateway_url=composed_gateway_url,
        agentcore_workload_name=composed_workload_name,
    )


def parse_profile(data: Mapping[str, object]) -> Profile:
    """Parse a profile mapping into a frozen ``Profile`` (narrow-only).

    Raises ``PlatformCompositionError`` on a structural problem; callers that
    must not abort (the per-surface profile loader) catch this and fall back to
    :func:`deny_all_profile` (Validation rule 5).
    """
    name = str(data.get("name", "")).strip()
    if not name:
        raise PlatformCompositionError("profile requires a 'name'")
    # Schema: name MUST match ^[a-z0-9][a-z0-9-]*$ (Appendix A).  A non-conforming
    # name is a schema-invalid profile → the loader turns this raise into the
    # most-restrictive deny-all built-in (Validation rule 5), never the ceiling.
    if not _PROFILE_NAME_RE.match(name):
        raise PlatformCompositionError(f"profile name {name!r} must match ^[a-z0-9][a-z0-9-]*$")
    # ``updates`` is POLICY-ONLY. It is in _STRUCTURAL_KEYS (so _parse_controls
    # skips it rather than failing as an unknown scope), which would make a
    # profile's copy silently inert — so reject it loudly here instead. A profile
    # is narrow-only and there is no narrower version of pointing somewhere else:
    # a per-app profile redirecting the update source would be escalation.
    if "updates" in data:
        raise PlatformCompositionError(
            "profiles may not set 'updates' — update pins are policy-only "
            "(a profile redirecting the update source would be privilege escalation)"
        )
    # ``distribution`` is POLICY-ONLY for a stronger version of the same reason,
    # and it is in _STRUCTURAL_KEYS for the same mechanical one (so _parse_controls
    # skips it rather than failing as an unknown scope, which would make a
    # profile's copy silently inert). Redirecting where the CEILING is fetched from
    # is not a narrowing at all — it replaces the whole enforcement document, so a
    # profile that could do it would be the widest escalation in this model.
    if "distribution" in data:
        raise PlatformCompositionError(
            "profiles may not set 'distribution' — central policy distribution is "
            "policy-only (a profile redirecting where the ceiling is fetched from "
            "would replace the enforcement document, not narrow it)"
        )
    # ``fallback`` is POLICY-ONLY for the same reason ``updates`` is. It is in
    # _STRUCTURAL_KEYS (so parse_policy's _parse_controls skips it rather than
    # rejecting it as an unknown scope), which would make a profile's copy silently
    # inert — a validation hole. A profile IS a per-surface narrowing; there is no
    # narrower "what an unusable profile falls back to", and a profile declaring its
    # own fallback would be nonsensical. Reject it loudly here (the loader turns this
    # into the deny-all fallback via Validation rule 5).
    if "fallback" in data:
        raise PlatformCompositionError(
            "profiles may not set 'fallback' — the unusable-profile fallback is "
            "policy-only (a profile declaring its own fallback is meaningless)"
        )
    bind: Optional[Bind] = None
    raw_bind = data.get("bind")
    if isinstance(raw_bind, dict):
        btype = str(raw_bind.get("type", "")).strip()
        if btype not in ("surface", "app", "task"):
            raise PlatformCompositionError(
                f"profile bind.type must be surface|app|task, got {btype!r}"
            )
        bind = Bind(type=btype, id=str(raw_bind.get("id", "")))
    # Key-open tolerance is opted into HERE and nowhere else (by passing
    # ``unknown_out``): an unknown ``capabilities.*`` child declared
    # ``enabled: true`` is inert, so it does not cost a cross-edition data home its
    # whole profile. Any other unknown child still raises. See _parse_controls.
    unknown: List[str] = []
    controls = _parse_controls(data, is_policy=False, unknown_out=unknown, profile_name=name)
    return Profile(
        name=name,
        bind=bind,
        extends=str(data.get("extends", "")),
        controls=controls,
        unknown_scopes=tuple(unknown),
    )


def _read_json_file(path: Path) -> Dict[str, object]:
    """Read + JSON-parse a governance file.  Raises on any failure (fail-closed)."""
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise PlatformCompositionError(f"governance file {path} is not a JSON object")
    return data
