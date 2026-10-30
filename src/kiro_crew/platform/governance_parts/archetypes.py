"""The four control archetypes and their composition algebra.

``Decision`` (the verdict record), ``ScopedRuleset`` with its composed form
``_AndRuleset``, ``OrdinalControl``, ``CapabilityGate`` and ``ScopedMap``, plus the
modes a ruleset takes and ``_compose_controls``, which composes two controls of one
archetype (policy over profile, or parent over child). The evaluator dispatches on these
shapes, never on a scope name. A change to how a control parses, answers or composes
goes here.

Served through :mod:`kiro_crew.platform.governance`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Protocol, Tuple, runtime_checkable

from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance_parts.matching import (
    _DEFAULT_MATCHER,
    _MATCHERS,
    _ORDINAL_SCALES,
    _url_host,
)

logger = logging.getLogger("kiro_crew.platform.governance")


# ScopedRuleset modes.
MODE_ALLOW = "allow"  # opt-in / closed set (deny-by-default)

MODE_DENY = "deny"  # opt-out / open set (allow-by-default)

_MODES = frozenset({MODE_ALLOW, MODE_DENY})


def _reject_unknown_keys(d: Mapping[str, object], known: "set[str]", container: str) -> None:
    """Enforce ``additionalProperties:false`` on an archetype container.

    Raise ``PlatformCompositionError`` if *d* carries any key outside *known* so a
    typo is a validation error (the schema's documented intent) rather than a
    silently-dropped — and potentially dangerous — setting.
    """
    extra = set(d) - known
    if extra:
        raise PlatformCompositionError(
            f"{container} has unknown key(s) {sorted(extra)} (additionalProperties:false; "
            "a typo is rejected fail-closed)"
        )


# ──────────────────────────────────────────────────────────────────────────
# Decision record
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Decision:
    """The outcome of a governance query.

    ``rule`` and ``layer`` name *why* — used by the audit record (Phase 8) and
    by ``policy explain`` (Phase 10).  ``permitted`` is the only field callers
    must branch on.
    """

    permitted: bool
    reason: str
    rule: str = ""  # rule1-allow | rule1-deny | rule2-intersect | ordinal | gate | default
    layer: str = ""  # policy | profile | both | default
    # The governed item this outcome is ABOUT. One gate query can carry several
    # identities for a single call (a prose title plus a trusted tool name plus an
    # MCP reference), so a denial has to say WHICH one it denied or the audit
    # record names the wrong subject. Empty when the caller asked about one item
    # and already knows it.
    item: str = ""
    # Whether a refusal came from a deny-mode pattern MATCHING the item (Rule 1
    # deny), as opposed to an allow-mode set merely not listing it or a disabled
    # scope. Set where the match happens (``ScopedRuleset.permits``) and carried
    # through every composition (``_AndRuleset.permits``, ``resolve``) unchanged,
    # because a fold relabels ``rule`` to ``rule2-intersect`` and a reader that
    # must tell "denied" from "not allowed" -- ``gate_decision``'s alias groups,
    # where an explicit deny on one spelling of an identity binds while an
    # allow-mode miss on one spelling does not -- cannot recover it from the
    # label. Never set on a permit.
    explicit_deny: bool = False


# A control that can answer "is this item permitted?" for ONE level.  Both
# ``ScopedRuleset`` and the composed ``_AndRuleset`` satisfy it — the evaluator
# only ever calls ``permits``, so it stays archetype-shape-agnostic.
#
# The two projection queries exist because a consumer cannot answer them with
# ``permits``: a per-tool rule (``@srv/delete``) matches only itself, so a
# sentinel probe sails past it, and a force-pin is a pattern to union rather than
# a question to ask. Reading ``.allow`` / ``.deny`` attributes off the control is
# not an option either — a composed fold is an ``_AndRuleset``, which holds its
# patterns in its halves. So the control answers for every tier it carries, and
# ``_AndRuleset`` answers by delegating to both halves.
@runtime_checkable
class RulesetLike(Protocol):
    def permits(self, item: str) -> Decision:  # pragma: no cover - protocol stub
        raise NotImplementedError

    def declared_patterns(self) -> Tuple[str, ...]:  # pragma: no cover - protocol stub
        """Every pattern this control names, in any list, at any tier."""
        raise NotImplementedError

    def force_deny_patterns(self) -> Tuple[str, ...]:  # pragma: no cover - protocol stub
        """Patterns this control denies outright, at any tier."""
        raise NotImplementedError


# ──────────────────────────────────────────────────────────────────────────
# Archetype 1 — ScopedRuleset
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ScopedRuleset:
    """A set of named items the agent may use, defined opt-in or opt-out.

    ``mode == "allow"``: only ``allow`` is permitted (``deny`` ignored — Rule 1).
    ``mode == "deny"``: everything except ``deny`` is permitted.
    An absent/empty ``allow`` under allow-mode is the **empty set** (deny-all),
    never "unconstrained".
    """

    mode: str
    allow: Tuple[str, ...] = ()
    deny: Tuple[str, ...] = ()
    matcher: str = _DEFAULT_MATCHER

    @staticmethod
    def from_dict(
        d: Mapping[str, object],
        *,
        matcher: str = _DEFAULT_MATCHER,
        scope: str = "",
    ) -> "ScopedRuleset":
        # additionalProperties:false — a typo'd key (e.g. "deney" instead of
        # "deny") must be a validation error, NOT silently dropped.  The
        # dangerous case is a deny-list typo: it would otherwise become an empty
        # deny list = allow-everything.  Fail closed instead.
        _reject_unknown_keys(d, {"mode", "allow", "deny"}, "ScopedRuleset")
        mode = str(d.get("mode", "")).strip().lower()
        if mode not in _MODES:
            raise PlatformCompositionError(
                f"ScopedRuleset.mode must be 'allow' or 'deny', got {mode!r}"
            )
        if matcher not in _MATCHERS:
            raise PlatformCompositionError(f"unknown matcher {matcher!r} for ScopedRuleset")
        allow = _str_tuple(d.get("allow"))
        deny = _str_tuple(d.get("deny"))
        # Rule 1: in allow-mode the deny array is ignored — warn so an operator
        # who set both does not believe a dead deny entry is protecting anything.
        # Every entry is ignored, so a count says all there is, and a deny entry
        # can be a pasted URL with credentials, so none is interpolated.
        if mode == MODE_ALLOW and deny:
            logger.warning(
                "ScopedRuleset mode=allow ignores all %d of its deny entries "
                "(Rule 1: allow beats deny); put hard bounds in policy, not a profile deny",
                len(deny),
            )
        # The ``host`` matcher tests an EXTRACTED host (``_url_host``), never the
        # URL an operator fetched, so an entry carrying a character no host can
        # contain never matches any item.  Warn rather than raise: a dead deny
        # silently permits what it was written to block, a dead allow silently
        # refuses it, and nothing else reports either.  Refusing the document
        # outright would instead turn one stale entry into a boot failure on
        # upgrade for a policy that loads today.
        if matcher == "host":
            # Only the list the engine reads: ``permits`` never consults ``allow``
            # in deny mode, and an allow-mode ``deny`` is already reported above.
            listed, live = ("allow", allow) if mode == MODE_ALLOW else ("deny", deny)
            effect = "refuses" if mode == MODE_ALLOW else "permits"
            for index, pattern in enumerate(live):
                # Each reason is a character or shape a host cannot carry, read off the
                # pattern itself.  Round-tripping ``_url_host`` is no test: it cuts a
                # bare IPv6 literal at its last colon (``::1`` gives ``:``) and a
                # netloc at the first ``?`` (``api?.skills.sh`` gives ``api``), so it
                # condemns live rules.
                name, _, port = pattern.strip().partition(":")
                stripped = pattern.strip()
                # IPv6 only when ``_url_host`` unwraps the bracket to an address:
                # it finds no host in the fnmatch class ``[a:].example.com``.
                ipv6 = stripped.startswith("[") and ":" in _url_host(stripped)
                if "/" in pattern:
                    why = "a '/' (a scheme, a path or a CIDR mask)"
                elif "@" in pattern:
                    why = "an '@' (userinfo)"
                elif ipv6:
                    # ``_url_host`` unwraps the brackets, so the item never has them.
                    # Any other bracket is an fnmatch character class and is live.
                    why = "IPv6 brackets"
                elif name and port.isdigit() and not set(name) & set("*?["):
                    # Exactly one colon, so a port: an IPv6 literal has two or more.
                    # A glob before it can absorb a colon: ``*:443`` matches ``fe80::443``.
                    why = "a port"
                else:
                    continue
                # Never interpolate the pattern: an operator pastes whole URLs,
                # with userinfo, signatures and ``?api_key=`` queries, and this
                # fires on every boot with no redaction before the sink.  The
                # position identifies the entry.
                reason = (
                    "scope %r uses matcher=host and can never match %s[%d]: the item "
                    "under test is a host, not a URL, and the entry carries %s, so it "
                    "is dead and the scope %s the host it names"
                )
                args: Tuple[object, ...] = (scope, listed, index, why, effect)
                logger.warning(reason, *args)
        return ScopedRuleset(mode=mode, allow=allow, deny=deny, matcher=matcher)

    def permits(self, item: str) -> Decision:
        """Rule 1 — within a single level."""
        match = _MATCHERS[self.matcher]
        if self.mode == MODE_ALLOW:
            ok = any(match(item, pat) for pat in self.allow)
            return Decision(
                ok,
                f"{item!r} {'in' if ok else 'not in'} allow-set",
                rule="rule1-allow",
            )
        # deny mode: permitted unless explicitly denied.
        hit = next((pat for pat in self.deny if match(item, pat)), None)
        if hit is not None:
            return Decision(
                False,
                f"{item!r} matches deny pattern {hit!r}",
                rule="rule1-deny",
                explicit_deny=True,
            )
        return Decision(True, f"{item!r} not denied", rule="rule1-deny")

    def declared_patterns(self) -> Tuple[str, ...]:
        """Every pattern this ruleset names, in either list.

        Both lists, because either one is an opinion about the items it names: an
        allow-mode set that lists some of a server's tools says as much about that
        server as a deny-mode set that excludes one.
        """
        return self.allow + self.deny

    def force_deny_patterns(self) -> Tuple[str, ...]:
        """The patterns this ruleset denies outright.

        Only a deny-mode set has any: an allow-mode set is an allowlist whose own
        ``deny`` is ignored (Rule 1), and ``gate_decision`` enforces it as the
        closed set it is, so it projects no pattern to union anywhere.
        """
        return self.deny if self.mode == MODE_DENY else ()

    def compose(self, narrower: "RulesetLike") -> "RulesetLike":
        """Rule 2 / inheritance — intersect this (ceiling) with a *narrower* set.

        The result permits an item iff BOTH permit it.  Two deny-mode rulesets
        with the same matcher flatten faithfully to a single union ScopedRuleset;
        any other combination (allow∩allow, allow∩deny) cannot be flattened
        without losing glob semantics, so it is returned as an
        :class:`_AndRuleset` view that re-checks both at query time.  Callers
        treat the result as a :class:`RulesetLike`.
        """
        if (
            isinstance(narrower, ScopedRuleset)
            and self.mode == MODE_DENY
            and narrower.mode == MODE_DENY
            and self.matcher == narrower.matcher
        ):
            return ScopedRuleset(
                mode=MODE_DENY,
                deny=_dedup(self.deny + narrower.deny),  # union
                matcher=self.matcher,
            )
        return _AndRuleset(self, narrower)


@dataclass(frozen=True)
class _AndRuleset:
    """The AND of two rulesets; ``permits`` requires both to permit.

    Used when :meth:`ScopedRuleset.compose` cannot flatten to a single ruleset.
    The halves are named ``outer`` / ``inner`` rather than ceiling/profile: a
    fold of three or more tiers nests one ``_AndRuleset`` inside another, so a
    half may itself be a composed pair (another policy tier), not the profile.
    The two-party denial labels keep the ``policy:`` / ``profile:`` prefixes
    and layers; a denial from a NESTED inner pair propagates that pair's own
    layer and label instead of being stamped ``profile``.
    Deliberately NOT a ``ScopedRuleset`` subclass — it satisfies
    :class:`RulesetLike` structurally, so the evaluator stays shape-agnostic.
    """

    outer: RulesetLike
    inner: RulesetLike

    def permits(self, item: str) -> Decision:
        o = self.outer.permits(item)
        if not o.permitted:
            # The outer refusal is the answer, but its PROVENANCE must not hide
            # the inner half's: an authority's allow-mode miss short-circuited
            # here would read as "not listed" even when the subordinate tier
            # denies the item outright, and a reader that must tell "denied"
            # from "not allowed" (``gate_decision``'s alias groups) would admit
            # the identity through another spelling. So the inner half is still
            # asked for its deny, and either half's explicit deny is carried.
            i = self.inner.permits(item)
            return Decision(
                False,
                f"policy: {o.reason}",
                rule="rule2-intersect",
                layer="policy",
                explicit_deny=o.explicit_deny or (not i.permitted and i.explicit_deny),
            )
        i = self.inner.permits(item)
        if not i.permitted:
            if isinstance(self.inner, _AndRuleset):
                # A nested pair is another policy tier, not the profile — its
                # decision already carries the right prefix and layer.
                return Decision(
                    False,
                    i.reason,
                    rule="rule2-intersect",
                    layer=i.layer,
                    explicit_deny=i.explicit_deny,
                )
            return Decision(
                False,
                f"profile: {i.reason}",
                rule="rule2-intersect",
                layer="profile",
                explicit_deny=i.explicit_deny,
            )
        return Decision(True, "permitted by both levels", rule="rule2-intersect", layer="both")

    def declared_patterns(self) -> Tuple[str, ...]:
        """Every pattern either half names — a half may be a nested pair."""
        return _dedup(self.outer.declared_patterns() + self.inner.declared_patterns())

    def force_deny_patterns(self) -> Tuple[str, ...]:
        """Every outright denial either half carries, unioned.

        A fold of an authority with a subordinate tier is this shape whenever the
        two modes differ, and the authority's denials bind through it: composition
        may only tighten, so a half's deny survives the fold it is folded into.
        """
        return _dedup(self.outer.force_deny_patterns() + self.inner.force_deny_patterns())


# ──────────────────────────────────────────────────────────────────────────
# Archetype 2 — OrdinalControl
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OrdinalControl:
    """A single value on an enforcer-owned strictness scale (e.g. sandbox tier).

    The scale name selects the order in ``_ORDINAL_SCALES``; the value itself
    must be a member of that scale.  Composition is strictest-of.
    """

    scale: str
    value: str

    def __post_init__(self) -> None:
        scale = _ORDINAL_SCALES.get(self.scale)
        if scale is None:
            raise PlatformCompositionError(f"unknown ordinal scale {self.scale!r}")
        if self.value not in scale:
            raise PlatformCompositionError(
                f"ordinal value {self.value!r} not in scale {self.scale!r} {scale}"
            )

    def rank(self) -> int:
        return _ORDINAL_SCALES[self.scale].index(self.value)

    def compose(self, narrower: "OrdinalControl") -> "OrdinalControl":
        """strictest-of(self, narrower).  A looser profile does NOT win here.

        Two layers enforce the ceiling: at BOOT, ``assert_governance_floor`` (wired
        via ``governance_profiles.assert_profiles_within_ceiling`` in
        ``bootstrap_context``) *rejects* a profile looser than the ceiling and
        aborts startup fail-closed; at RUNTIME, this method takes the stricter of
        the two so a stricter profile is honoured and a looser one can never
        downgrade the effective value (defense in depth even if a profile is
        hot-reloaded after boot).
        """
        if narrower.scale != self.scale:
            raise PlatformCompositionError(
                f"cannot compose ordinals on different scales: "
                f"{self.scale!r} vs {narrower.scale!r}"
            )
        return self if self.rank() >= narrower.rank() else narrower

    def is_at_least_as_strict_as(self, ceiling: "OrdinalControl") -> bool:
        return self.rank() >= ceiling.rank()


def _unmatchable_glob(glob: str) -> str:
    """Why a host glob can match no reading, or ``""`` when it might.

    A character check only: a reading holds only a URL host's characters
    (``matching._PLAIN_HOST_RE``), so a glob holding a ``[`` class, ``@``, ``/``,
    ``\\``, whitespace, a zone, or a ``:`` outside a hex IPv6 glob can never match. How an IP glob spells its
    numbers is not checked: it matches canonical readings as written (an
    alternate numeric spelling is tracked in the deferral issue for these lists).
    """
    import re

    from kiro_crew.platform.governance_parts.matching import _PLAIN_HOST_RE

    if "[" in glob:
        return "uses a [ ] character class; host lists take * and ? only"
    body = glob.replace("*", "").replace("?", "")
    if ":" in body and not re.fullmatch(r"[0-9a-f:]*", body):
        return "is not an IPv6 glob in canonical form (hex groups only, no zone or port)"
    if body and not _PLAIN_HOST_RE.fullmatch(body):
        return "has a character no host reading holds ('@', '/', a backslash or a space)"
    return ""


def _refuse_unmatchable_host_entries(scope_name: str, ruleset: "ScopedRuleset") -> None:
    """Refuse a ``host`` entry in a capability's list that no host reading can match.

    A capability's ``host`` lists are asked about hosts as clients send them
    (``matching.url_policy_hosts``): ASCII, no root dot, an IP literal in canonical
    form. An exact entry in another spelling would match nothing, so a deny would
    refuse nothing and an allow would admit nothing, silently. It fails to load
    instead, and the message says which form to write. A glob gets a character
    check only (``_unmatchable_glob``); how it spells IP numbers is not checked.
    Top-level ``network.egress`` keeps its
    own parsing; this applies only to lists inside a capability.
    """
    from kiro_crew.platform.governance_parts.matching import _PLAIN_HOST_RE, _canonical_ip

    for listed, entries in (("allow", ruleset.allow), ("deny", ruleset.deny)):
        for index, raw in enumerate(entries):
            entry = raw.strip()
            spelled = entry.lower()
            why = ""
            if not entry.isascii():
                why = "is not ASCII; write the name in its xn-- (IDNA) form"
            elif entry.endswith("."):
                why = "ends in a root dot; write it without the dot"
            elif entry.startswith("[") and ":" in entry:
                why = "is a bracketed IPv6 literal; write the address without brackets"
            elif any(c in entry for c in "*?["):
                why = _unmatchable_glob(spelled)
            else:
                # Exactly the form a host is asked in, or the entry can never match.
                ip = _canonical_ip(spelled)
                if ip:
                    asked = ip.split("%", 1)[0]
                    if asked != spelled:
                        why = f"is an IP literal not in the form hosts are asked in; write {asked}"
                elif ":" in spelled or not _PLAIN_HOST_RE.fullmatch(spelled):
                    why = (
                        "is not a host as clients send it "
                        "(brackets, a zone, a port, '/', '@', a backslash or a space)"
                    )
            if why:
                # The position, never the value: an entry can be a pasted URL.
                raise PlatformCompositionError(
                    f"capability scope {scope_name!r} {listed}[{index}] {why}"
                )


# ──────────────────────────────────────────────────────────────────────────
# Archetype 3 — CapabilityGate
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CapabilityGate:
    """An on/off capability, optionally bounded by named ScopedRuleset scopes.

    ``enabled`` composes by AND across levels (either side off → off).  Each
    entry in ``scopes`` is an orthogonal ruleset (e.g. spawn → {agents,
    cwd_roots}); all survive composition independently.
    """

    enabled: bool
    scopes: Mapping[str, RulesetLike] = field(default_factory=dict)

    @staticmethod
    def from_dict(
        d: Mapping[str, object],
        *,
        default_enabled: bool,
        scope_matchers: Optional[Mapping[str, str]] = None,
    ) -> "CapabilityGate":
        _reject_unknown_keys(d, {"enabled", "scopes"}, "CapabilityGate")
        raw_scopes = d.get("scopes") or {}
        if not isinstance(raw_scopes, dict):
            raise PlatformCompositionError("CapabilityGate.scopes must be an object")
        matchers = scope_matchers or {}
        for k, v in raw_scopes.items():
            # A declared list in the wrong shape would otherwise be dropped below,
            # leaving the capability with no bound on that list at all.
            if str(k) in matchers and not isinstance(v, dict):
                raise PlatformCompositionError(
                    f"CapabilityGate.scopes.{k} must be an object such as "
                    '{"mode": "allow", "allow": [...]}, '
                    f"got {type(v).__name__}"
                )
        scopes: Dict[str, RulesetLike] = {
            str(k): ScopedRuleset.from_dict(v, matcher=matchers.get(str(k), _DEFAULT_MATCHER))
            for k, v in raw_scopes.items()
            if isinstance(v, dict)
        }
        for name, ruleset in scopes.items():
            if isinstance(ruleset, ScopedRuleset) and ruleset.matcher == "host":
                _refuse_unmatchable_host_entries(name, ruleset)
        if "enabled" not in d:
            enabled_flag = default_enabled
        else:
            enabled = d["enabled"]
            if not isinstance(enabled, bool):
                raise PlatformCompositionError(
                    f"CapabilityGate.enabled must be a boolean, got {enabled!r}"
                )
            enabled_flag = enabled
        return CapabilityGate(
            enabled=enabled_flag,
            scopes=scopes,
        )

    def compose(self, narrower: "CapabilityGate") -> "CapabilityGate":
        merged: Dict[str, RulesetLike] = dict(self.scopes)
        for name, ruleset in narrower.scopes.items():
            base = merged.get(name)
            if isinstance(base, ScopedRuleset):
                merged[name] = base.compose(ruleset)
            elif base is not None:
                merged[name] = _AndRuleset(base, ruleset)
            else:
                merged[name] = ruleset
        return CapabilityGate(enabled=self.enabled and narrower.enabled, scopes=merged)

    def permits_scope_item(self, scope_name: str, item: str) -> Decision:
        if not self.enabled:
            return Decision(False, "capability disabled", rule="gate", layer="both")
        ruleset = self.scopes.get(scope_name)
        if ruleset is None:
            return Decision(True, f"scope {scope_name!r} unconstrained", rule="gate")
        return ruleset.permits(item)


# ──────────────────────────────────────────────────────────────────────────
# Archetype 4 — ScopedMap
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ScopedMap:
    """An allowlist of members where each member carries its own posture.

    ``members`` is a ruleset over member ids (which transports).  ``posture`` is
    per-member and **policy-only**: a profile carries ``members`` only, so an app
    can never widen a member's identity ceiling.
    """

    members: RulesetLike
    # member id -> {leaf name -> ruleset}; a leaf both sides of a fold name holds
    # their intersection, which may be an ``_AndRuleset``.
    posture: Mapping[str, Mapping[str, RulesetLike]] = field(default_factory=dict)

    @staticmethod
    def from_dict(d: Mapping[str, object], *, allow_posture: bool) -> "ScopedMap":
        _reject_unknown_keys(d, {"members", "posture"}, "ScopedMap")
        raw_members = d.get("members")
        if not isinstance(raw_members, dict):
            raise PlatformCompositionError("ScopedMap.members is required and must be an object")
        members = ScopedRuleset.from_dict(raw_members, matcher="identifier")
        raw_posture = d.get("posture")
        if raw_posture and not allow_posture:
            # Rule 6: posture is policy-only; a profile carrying it is rejected.
            raise PlatformCompositionError("ScopedMap.posture is policy-only; not allowed here")
        posture: Dict[str, Dict[str, ScopedRuleset]] = {}
        if isinstance(raw_posture, dict):
            for member, leaves in raw_posture.items():
                if not isinstance(leaves, dict):
                    continue
                # Rule 6: a posture key is valid only if its member id is admitted
                # by ``members`` — an orphan posture entry is dead config and a
                # likely typo, so fail closed rather than carry it silently.
                if not members.permits(str(member)).permitted:
                    raise PlatformCompositionError(
                        f"ScopedMap.posture key {member!r} is not admitted by members; "
                        "a posture entry must name an allowed member"
                    )
                posture[str(member)] = {
                    str(leaf): ScopedRuleset.from_dict(val, matcher="identifier")
                    for leaf, val in leaves.items()
                    if isinstance(val, dict)
                }
        return ScopedMap(members=members, posture=posture)

    def compose(self, narrower: "ScopedMap") -> "ScopedMap":
        # members intersect, and so does posture: the narrower side can only
        # tighten.  A profile never carries posture (``from_dict`` rejects it), so
        # over a profile this keeps the ceiling's posture unchanged; between two
        # policy tiers the lower tier's posture narrows the higher one's.
        base = self.members
        if isinstance(base, ScopedRuleset):
            composed: RulesetLike = base.compose(narrower.members)
        else:
            # An earlier fold already produced an ``_AndRuleset``; wrap it the
            # same way the sibling composers (``CapabilityGate.compose``,
            # ``_compose_controls``) do, so a third — and any later — tier's
            # narrowing is honoured instead of being silently dropped.
            composed = _AndRuleset(base, narrower.members)
        return ScopedMap(members=composed, posture=_compose_posture(self.posture, narrower.posture))

    def permits_member(self, member: str) -> Decision:
        return self.members.permits(member)

    def posture_permits(self, member: str, leaf: str, item: str) -> Decision:
        member_posture = self.posture.get(member)
        if not member_posture:
            return Decision(True, f"no posture for member {member!r}", rule="scopedmap")
        ruleset = member_posture.get(leaf)
        if ruleset is None:
            return Decision(True, f"no posture leaf {leaf!r} for {member!r}", rule="scopedmap")
        return ruleset.permits(item)


def _compose_posture(
    ceiling: Mapping[str, Mapping[str, RulesetLike]],
    narrower: Mapping[str, Mapping[str, RulesetLike]],
) -> Mapping[str, Mapping[str, RulesetLike]]:
    """Intersect two ``ScopedMap`` postures, leaf by leaf.

    An absent member or leaf permits everything, so a leaf only one side names
    carries through as a restriction, and a leaf both name permits an item only
    when both rulesets do.  No combination can permit what the ceiling refuses.
    """
    if not narrower:
        return ceiling
    merged: Dict[str, Dict[str, RulesetLike]] = {m: dict(ls) for m, ls in ceiling.items()}
    for member, leaves in narrower.items():
        target = merged.setdefault(member, {})
        for leaf, ruleset in leaves.items():
            base = target.get(leaf)
            if base is None:
                target[leaf] = ruleset
            elif isinstance(base, ScopedRuleset):
                target[leaf] = base.compose(ruleset)
            else:
                target[leaf] = _AndRuleset(base, ruleset)
    return merged


# ──────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ──────────────────────────────────────────────────────────────────────────
def _str_tuple(value: object) -> Tuple[str, ...]:
    """Coerce an untrusted JSON value into a tuple of strings (fail-safe)."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return ()


def _dedup(items: Tuple[str, ...]) -> Tuple[str, ...]:
    seen: Dict[str, None] = {}
    for it in items:
        seen.setdefault(it, None)
    return tuple(seen)


def _compose_controls(ceiling: object, narrower: object) -> object:
    """AND two controls of the same archetype (dispatch by type, not scope)."""
    if isinstance(ceiling, ScopedRuleset):
        return ceiling.compose(narrower)  # type: ignore[arg-type]
    if isinstance(ceiling, _AndRuleset):
        return _AndRuleset(ceiling, narrower)  # type: ignore[arg-type]
    if isinstance(ceiling, OrdinalControl) and isinstance(narrower, OrdinalControl):
        return ceiling.compose(narrower)
    if isinstance(ceiling, CapabilityGate) and isinstance(narrower, CapabilityGate):
        return ceiling.compose(narrower)
    if isinstance(ceiling, ScopedMap) and isinstance(narrower, ScopedMap):
        return ceiling.compose(narrower)
    # Archetype mismatch between parent and child for the same scope.
    raise PlatformCompositionError(
        f"cannot compose mismatched control types {type(ceiling)!r} / {type(narrower)!r}"
    )
