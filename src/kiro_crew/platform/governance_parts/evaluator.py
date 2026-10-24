"""Answering a governance query against policy and profile.

``resolve`` (one item, ``policy ∩ profile``), ``resolve_ordinal`` (the strictest of the
two levels), ``gate_decision`` (the gate's verdict for one tool call),
and ``resolve_pinned_commands`` (the ``commands`` force-pins). Pure functions over the
carriers; a new query goes here.

Served through :mod:`kiro_crew.platform.governance`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping, Optional, Tuple

from kiro_crew import edition
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance_parts.archetypes import (
    CapabilityGate,
    Decision,
    OrdinalControl,
    ScopedMap,
    ScopedRuleset,
    _AndRuleset,
    _dedup,
)
from kiro_crew.platform.governance_parts.baseline import BASELINE_UNGATED
from kiro_crew.platform.governance_parts.carriers import GovernanceCeiling, Profile
from kiro_crew.platform.governance_parts.catalog import _SCOPE_ALIASES
from kiro_crew.platform.governance_parts.parsing import baseline_controls
from kiro_crew.platform.governance_parts.tool_calls import (
    _governed_targets,
    classify_tool_args,
    classify_tool_title,
)
from kiro_crew.platform.guest_paths import guest_also_local, guest_root

# ──────────────────────────────────────────────────────────────────────────
# Evaluator (Phase 4) — pure functions, no I/O
# ──────────────────────────────────────────────────────────────────────────
# The runtime resolution path.  It NEVER pre-merges policy and profile into one
# object; it queries each level independently and ANDs (Rule 2), so the engine
# is identical for every scope and never branches on a scope name.

_PERMIT_NOT_GOVERNED = Decision(True, "not governed", rule="default", layer="default")

_BASELINE_REASON = "enterprise baseline"

# A scope with neither a baseline row nor an ungated reason (one registered at run
# time) is refused in an enterprise build: an empty allow-set denies every item.
_DENY_UNLISTED = ScopedRuleset(mode="allow", allow=())


def _policy_level(
    ceiling: Optional[GovernanceCeiling], scope: str, *, baseline: bool = True
) -> Tuple[object, bool]:
    """The policy level's control for *scope*, and whether the baseline supplied it.

    The remote policy answers a scope it names. In an enterprise build a scope it
    does not name is answered by the built-in baseline (RFC D3), never left
    ungoverned. Only the POLICY level is filled in this way: a profile narrows the
    policy beneath it, so an unnamed profile scope stays ``None`` and cannot
    impose the baseline again under a remote widening. That is why a ``Profile``
    passed in the ceiling's place (``governance_profiles`` asks ``may_skip_gate``
    about one profile alone) and a caller asking about the profile layer alone
    (``baseline=False``) get no baseline.
    """
    control = ceiling.get(scope) if ceiling is not None else None
    if (
        control is not None
        or not baseline
        or isinstance(ceiling, Profile)
        or not edition.is_enterprise()
    ):
        return control, False
    # A policy's ``folders.*`` rule is stored under its ``filesystem.*`` scope at
    # parse time, so an alias query must find it there before the baseline answers.
    canonical = _SCOPE_ALIASES.get(scope, scope)
    named = ceiling.get(canonical) if ceiling is not None else None
    if named is not None:
        return named, False
    if canonical in BASELINE_UNGATED:
        return None, False
    return baseline_controls().get(canonical, _DENY_UNLISTED), True


def policy_level_control(ceiling: Optional[GovernanceCeiling], scope: str) -> Optional[object]:
    """The control the policy level enforces for *scope* (see :func:`_policy_level`).

    For readers that inspect a ceiling's control directly instead of calling
    :func:`resolve`, such as ``governance.may_skip_gate``; reading
    ``ceiling.get`` there would treat an unnamed scope as ungoverned in an
    enterprise build and skip the gate the baseline is enforced at.
    """
    return _policy_level(ceiling, scope)[0]


def _query_level(control: object, scope: str, item: str) -> Decision:
    """Query ONE level's control for *item* in *scope* (Rule 1).

    A ``None`` control means the level does not govern this scope → permit
    (truth-table "not-governed" rows).  In an enterprise build the POLICY level
    is never ``None`` here for a gated scope: :func:`_policy_level` supplies the
    built-in baseline first, so only a profile reaches this permit.  Dispatch is by archetype, not scope
    name.  ``scope`` is used only to locate the sub-leaf for the structured
    archetypes (capabilities.<cap>.<inner>, channels member).
    """
    if control is None:
        return _PERMIT_NOT_GOVERNED
    if isinstance(control, (ScopedRuleset, _AndRuleset)):
        return control.permits(item)
    if isinstance(control, CapabilityGate):
        # scope form "capabilities.<cap>" governs the gate itself; an item names
        # an inner scope element as "<inner>:<value>" (e.g. "agents:researcher").
        inner, sep, value = item.partition(":")
        if sep:
            return control.permits_scope_item(inner, value)
        return (
            Decision(True, "capability enabled", rule="gate")
            if control.enabled
            else Decision(False, "capability disabled", rule="gate")
        )
    if isinstance(control, ScopedMap):
        # item is a member id, or "member/leaf:value" for a posture query.
        member, sep, rest = item.partition("/")
        if not sep:
            return control.permits_member(item)
        leaf, _, value = rest.partition(":")
        return control.posture_permits(member, leaf, value)
    if isinstance(control, OrdinalControl):
        # Ordinals are not queried per-item; resolve_ordinal handles them.
        return Decision(True, "ordinal not item-queried", rule="ordinal")
    raise PlatformCompositionError(f"scope {scope!r} carries unknown control {type(control)!r}")


def resolve(
    ceiling: Optional[GovernanceCeiling],
    profile: Optional[Profile],
    scope: str,
    item: str,
    *,
    baseline: bool = True,
) -> Decision:
    """Resolve whether *item* is permitted in *scope* — ``policy ∩ profile``.

    Rule 2: an item is effectively permitted only if BOTH levels permit it.
    Policy is the hard ceiling; the profile can only further restrict.  Either
    level being ``None`` (or not governing the scope) contributes a permit, so
    ``resolve(None, None, …)`` permits everything (the ungoverned standalone
    default) and a policy-deny is final regardless of the profile. In an
    enterprise build a scope the policy does not name is answered by the built-in
    baseline instead (:func:`_policy_level`); ``baseline=False`` asks the profile
    layer alone, for a caller that judges each layer separately.
    """
    policy_control, from_baseline = _policy_level(ceiling, scope, baseline=baseline)
    policy_dec = _query_level(policy_control, scope, item)
    if from_baseline:
        policy_dec = replace(policy_dec, reason=f"{_BASELINE_REASON}: {policy_dec.reason}")
    profile_control = profile.get(scope) if profile is not None else None
    if not policy_dec.permitted:
        # Same provenance rule as ``_AndRuleset.permits``: a policy allow-mode
        # miss is the answer, but a profile deny beneath it still marks the
        # refusal as an explicit deny.
        _below = _query_level(profile_control, scope, item)
        return Decision(
            False,
            f"policy denies: {policy_dec.reason}",
            rule=policy_dec.rule,
            layer="policy",
            explicit_deny=policy_dec.explicit_deny
            or (not _below.permitted and _below.explicit_deny),
        )

    profile_dec = _query_level(profile_control, scope, item)
    if not profile_dec.permitted:
        return Decision(
            False,
            f"profile denies: {profile_dec.reason}",
            rule=profile_dec.rule,
            layer="profile",
            explicit_deny=profile_dec.explicit_deny,
        )

    layer = (
        "both"
        if (policy_control is not None and profile_control is not None)
        else (
            "policy"
            if policy_control is not None
            else "profile" if profile_control is not None else "default"
        )
    )
    return Decision(True, "permitted", rule="rule2-intersect", layer=layer)


def gate_decision(
    ceiling: Optional[GovernanceCeiling],
    profile: Optional[Profile],
    tool_title: str,
    *,
    tool_kind: str = "",
    raw_params: Optional[Mapping[str, object]] = None,
    diff_path: str = "",
    mcp_ref: str = "",
    extra_titles: Tuple[str, ...] = (),
    alias_groups: Tuple[Tuple[str, ...], ...] = (),
    deny_aliases: Tuple[str, ...] = (),
) -> Decision:
    """Resolve a PreToolUse gate title against the governance ceiling ∩ profile.

    Classifies the (heterogeneous) title to its governed scope + item, then
    ``resolve``s it.  When ``tool_kind`` / ``raw_params`` are supplied (the ACP
    event carries them), the real arguments are ALSO classified
    (:func:`classify_tool_args`) so path/host scopes the title cannot carry —
    ``filesystem.write`` (edit path), ``network.egress`` (fetch host) — are
    enforced at the same gate.  ``diff_path`` is the path the tool call's diff
    content block named (``event.diff_path``); for an edit it joins the
    classified ``filesystem.write`` target set, so a diff-only edit does not
    reach an ALLOW-mode confinement pathless.  A title/args pair the gate does
    not govern is permitted here — an ungoverned scope permits.  When BOTH
    levels are ungoverned the result permits (the standalone default), so a
    host with no policy + no profile behaves exactly as today.

    ``mcp_ref`` supplies an ALREADY-canonical ``@server`` / ``@server/tool``
    reference for a caller that holds the server and tool as separate trusted
    fields; it is checked in the ``mcp`` scope alongside anything the title maps
    to.  Taking the reference directly is what makes such a call exact: the
    ``mcp__<server>__<tool>`` title form is read by splitting on the LAST ``__``,
    so it cannot represent a tool name containing ``__``, and a caller holding
    the untangled fields must not be made to encode them into a form that loses
    the distinction.

    ``extra_titles`` carries any further NON-model-authored names the caller holds
    for the SAME call (the trusted ``_meta.kiro.toolName`` beside a prose display
    title). They belong in this one call rather than a second one: a separate call
    re-resolves the active profile, so a hot reload between the two could answer
    each question from a different snapshot and permit a tool that both complete
    profiles deny. Empty entries are ignored.

    ``alias_groups`` carries identities that have SEVERAL spellings for ONE tool
    -- a KAS built-in's raw id beside the kiro-cli name a rule about it is
    written under (``("str_replace", "fs_write")``). A group is one question:
    it is DENIED if any spelling meets an explicit deny rule at either level (a
    deny-mode ``tools.deny: ["str_replace"]`` still binds), and otherwise
    PERMITTED when EACH governing level permits some spelling -- the OR is taken
    within a level and the intersection across levels, so a ceiling granting
    ``str_replace`` beneath a profile granting ``fs_write`` admits the one tool
    both granted, while an allow-mode profile listing only ``fs_write`` admits
    the call rather than also demanding the raw id no operator wrote. A group
    member that is also the display title covers the title, so the title is not
    asked a second time as a lone item the allow set would have to carry.

    ``deny_aliases`` carries spellings whose rules may REFUSE the call but never
    admit it -- a KAS ``delete_file`` reads under ``fs_write`` because an operator
    who denied writes denied deletes, yet kiro-cli's ``fs_write`` cannot delete,
    so an allow-mode rule naming ``fs_write`` must not become a deletion grant.
    Each is asked for an explicit deny only; an allow-mode miss on it is not a
    refusal, and the identity itself (in ``extra_titles`` or the title) still
    has to be permitted on its own name.
    """
    _grouped = {s for group in alias_groups for s in group if s}
    pairs = list(classify_tool_title(tool_title)) if tool_title not in _grouped else []
    # Additional NON-model-authored titles the caller holds for the same call --
    # e.g. the trusted ``_meta.kiro.toolName`` beside an LLM-authored display
    # title. They are classified here, in ONE decision, rather than asked as
    # separate calls: each separate call would resolve the active profile again,
    # so a profile hot-reloaded mid-call could serve a DIFFERENT snapshot to each
    # question and a tool denied by both complete profiles could be permitted by
    # every individual lookup. One snapshot, every target, deny if any denies.
    # Empty entries are skipped: an empty title classifies to the unprefixed
    # scopes as a real queryable item (``('tools', '')``), not a no-op, so it
    # could match a rule it has nothing to do with.
    for extra in extra_titles:
        if extra:
            pairs.extend(classify_tool_title(extra))
    # A remote-workspace call's title-derived path (``Reading <path>``) names the
    # same guest file its arguments do, so it is read the way they are; a plain
    # twin judged as this gateway's path would refuse a permitted guest read.
    root = guest_root(raw_params)
    if root is not None:
        also_local = guest_also_local(raw_params)
        pairs = [
            (scope, target)
            for scope, item in pairs
            for target in (
                _governed_targets((item,), root, also_local)
                if scope.startswith("filesystem.")
                else (item,)
            )
        ]
    pairs.extend(classify_tool_args(tool_kind, raw_params, diff_path))
    if mcp_ref:
        pairs.append(("mcp", mcp_ref))
    # Order-preserving dedupe -- a caller whose title already equals its trusted
    # identity must not pay the same resolve twice.
    pairs = list(dict.fromkeys(pairs))
    if not pairs and not alias_groups and not deny_aliases:
        return Decision(True, "title not name-gate-governed", rule="default")
    # Deny-only spellings first: an explicit deny written under the alias binds
    # the identity, and nothing else about the alias is consulted -- an
    # allow-mode set that does not list it is not a refusal, because the alias
    # is not a grant surface for this identity. The identity's own name is still
    # in ``pairs`` and has to be permitted there.
    for alias in deny_aliases:
        if not alias:
            continue
        for scope, item in dict.fromkeys(classify_tool_title(alias)):
            decision = resolve(ceiling, profile, scope, item)
            if not decision.permitted and decision.explicit_deny:
                return replace(decision, item=item)
    # Deny if ANY governed scope the title/args map to denies it (the unprefixed
    # case maps to both commands+tools; an ungoverned scope permits, so this only
    # tightens).  Return the first denial for a precise audit reason.
    for scope, item in pairs:
        decision = resolve(ceiling, profile, scope, item)
        if not decision.permitted:
            # Name the identity that actually denied: with several targets in one
            # query the caller cannot infer it, and an audit naming the prose
            # title instead of the trusted tool name is a misleading record.
            return replace(decision, item=item)
    # One identity, several spellings. An explicit deny on any spelling is final
    # -- read from ``explicit_deny``, which every fold carries through, never
    # from the ``rule`` label, which a composed ruleset relabels
    # ``rule2-intersect`` and would hide a subordinate tier's deny behind an
    # authority's allow. Short of that, the group is judged PER LAYER: a layer
    # permits the identity if it permits any spelling, and the identity is
    # admitted when every layer does. Resolving each spelling through the
    # intersection first would refuse a ceiling that grants ``str_replace``
    # beneath a profile that grants ``fs_write`` -- each layer granted the one
    # tool, in its own vocabulary -- so the OR is taken inside each layer and the
    # intersection across them. Spellings are tried in order and a refusing
    # layer's LAST refusal is the one reported, so an allow-mode miss is named
    # in the vocabulary the operator writes rules in.
    for group in alias_groups:
        spellings = [s for s in group if s]
        if not spellings:
            continue
        refusal: Optional[Decision] = None
        # The profile pass asks the profile alone: filling its empty policy slot
        # with the baseline would refuse beneath a remote grant what the ceiling
        # pass already admitted.
        layers = ((ceiling, None, True), (None, profile, False))
        for layer_ceiling, layer_profile, layer_baseline in layers:
            if layer_ceiling is None and layer_profile is None and not layer_baseline:
                continue
            admitted = False
            last_refusal: Optional[Decision] = None
            for spelling in spellings:
                spelling_ok = True
                for scope, item in dict.fromkeys(classify_tool_title(spelling)):
                    decision = resolve(
                        layer_ceiling, layer_profile, scope, item, baseline=layer_baseline
                    )
                    if decision.permitted:
                        continue
                    if decision.explicit_deny:
                        return replace(decision, item=item)
                    spelling_ok = False
                    last_refusal = replace(decision, item=item)
                if spelling_ok:
                    admitted = True
            # Every layer is walked before a refusal is answered, so an explicit
            # deny in the second layer is reported over an allow-miss in the first.
            if not admitted and last_refusal is not None and refusal is None:
                refusal = last_refusal
        if refusal is not None:
            return refusal
    return Decision(True, "permitted by all mapped scopes", rule="rule2-intersect")


def resolve_pinned_commands(
    ceiling: Optional[GovernanceCeiling],
    profile: Optional[Profile] = None,
) -> Tuple[str, ...]:
    """Effective force-pinned command patterns = ceiling pins ∪ profile pins.

    Deny-mode denies compose by UNION (tightest-wins), so the effective pin set
    is the order-preserving, de-duplicated union of the ceiling's and the active
    profile's ``commands`` deny patterns. hooks.py calls this and unions the
    result into security.py's effective denied set so an enterprise/policy pin
    (and a bound profile's command deny) overrides the user's per-rule opt-out.
    Returns ``()`` for an ungoverned standalone host (ceiling and profile both
    None or neither governing ``commands``) — no behavior change there.
    """
    pins: Tuple[str, ...] = ()
    if ceiling is not None:
        pins = pins + ceiling.pinned_command_patterns()
    if profile is not None:
        pins = pins + profile.pinned_command_patterns()
    return _dedup(pins)


def resolve_ordinal(
    ceiling: Optional[GovernanceCeiling],
    profile: Optional[Profile],
    scope: str,
) -> Optional[OrdinalControl]:
    """Resolve the effective ordinal value = strictest-of(policy, profile).

    Returns ``None`` when neither level governs the ordinal (caller keeps its
    own default).  Used by Phase 7 to clamp ``approval_mode`` / ``sandbox`` —
    the result is a floor: the enforcer must run at least this strict.
    """
    pol = ceiling.get(scope) if ceiling is not None else None
    pro = profile.get(scope) if profile is not None else None
    if pol is not None and not isinstance(pol, OrdinalControl):
        raise PlatformCompositionError(f"scope {scope!r} policy value is not an ordinal")
    if pro is not None and not isinstance(pro, OrdinalControl):
        raise PlatformCompositionError(f"scope {scope!r} profile value is not an ordinal")
    if pol is not None and pro is not None:
        return pol.compose(pro)
    return pol or pro
