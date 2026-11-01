"""Turn the control plane's enterprise-management policy into a Kiro Crew policy document.

The policy arrives as Trust V2 YAML: ``rules: [{capability, effect, match | exclude,
when?}]``, already resolved for the ``CREW`` client surface. Kiro Crew governs only the
capabilities in :data:`MANAGED`; every other capability belongs to another client and
is ignored.

Per managed capability the rules reduce to one Crew ruleset, because a Crew ruleset has
one mode (allow-list or deny-list):

* ``deny``/``ask`` with neither ``match`` nor ``exclude``: nothing is allowed (``allow []``);
* ``deny``/``ask`` with ``exclude E``: only ``E`` is allowed (several excludes intersect);
* ``deny``/``ask`` with ``match L``: beside an exclude, ``L`` is taken out of the
  allowed set. Alone it grants nothing, so the capability stays unnamed and the
  enterprise baseline (off) holds;
* ``allow``: no effect of its own. The control plane sends a ``deny exclude`` beside
  every grant, and that rule already says what is allowed.

A denied glob removes the allowed literals it matches. A denied entry inside an allowed
glob, or a denied glob beside an allowed glob, has no one-mode form. The translation refuses it with :class:`Unrepresentable` rather than
widen or narrow the set, so the fetch fails. Entries compare as Crew's identifier
matcher compares them: stripped and case-folded. ``ask`` reads as ``deny``. A managed
capability no rule restricts is left out of the document.
"""

from __future__ import annotations

import fnmatch
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml  # type: ignore[import-untyped]

from kiro_crew.platform.governance import MODE_ALLOW

#: Each capability Kiro Crew governs from enterprise management, and the path of its
#: ruleset in a Crew policy document.
MANAGED: Mapping[str, Tuple[str, ...]] = MappingProxyType(
    {"messaging_channel": ("channels", "members")}
)

#: How often the running gateway re-reads the policy: five minutes, the Kiro agent's
#: enterprise-management refresh interval. A document with no interval is read at boot
#: only.
REFRESH_INTERVAL_SECS = 300

#: The boot controls of every translated document. The enterprise policy has no rule for
#: them, so they hold the enterprise defaults: the terminal off, a failed policy load
#: refuses to start, and the sandbox requirement left at Crew's own default (RFC
#: appendix, "Minimum sandbox level").
BOOT: Mapping[str, bool] = MappingProxyType(
    {"require_sandbox": False, "allow_terminal": False, "fail_closed": True}
)

_DENY_EFFECTS = ("deny", "ask")
_EFFECTS = ("allow",) + _DENY_EFFECTS
_GLOB_CHARS = frozenset("*?[")
_DESCRIPTION = "Translated from the organisation's enterprise-management policy"


class TranslationError(ValueError):
    """The policy cannot be translated."""


class Unrepresentable(TranslationError):
    """The rules for a managed capability have no one-mode Crew form."""


def _is_glob(pattern: str) -> bool:
    return bool(_GLOB_CHARS & set(pattern))


def _key(pattern: str) -> str:
    return pattern.strip().casefold()


def _matches(item: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(_key(item), _key(pattern))


def _rules(policy: str) -> List[Mapping[str, Any]]:
    try:
        doc = yaml.safe_load(policy)
    except yaml.YAMLError:
        raise TranslationError("the enterprise policy is not valid YAML") from None
    rules = doc.get("rules") if isinstance(doc, dict) else None
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        raise TranslationError("the enterprise policy has no list of rules")
    return rules


def _patterns(rule: Mapping[str, Any], key: str, capability: str) -> List[str]:
    raw = rule.get(key)
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise TranslationError(f"{capability}: '{key}' is not a list of strings")
    return list(raw)


def _admits(patterns: List[str], entry: str) -> bool:
    """Does the allowed set *patterns* admit the literal *entry*?"""
    return any(_key(p) == _key(entry) or (_is_glob(p) and _matches(entry, p)) for p in patterns)


def _intersect(sets: List[List[str]], capability: str) -> List[str]:
    """The entries every allowed set in *sets* admits, as one list, or raise.

    A literal is kept when every set admits it, by equality or through a glob. A glob is
    kept only when every set names that same glob. Beside a set that names only literals,
    dropping it is exact: what the two share is those literals, which are kept on their
    own. Beside a set holding a different glob, the shared part has no list form, so the
    translation refuses rather than narrow it.
    """
    seen: Dict[str, str] = {}
    for patterns in sets:
        for p in patterns:
            seen.setdefault(_key(p), p)
    allowed: List[str] = []
    for p in seen.values():
        if not _is_glob(p):
            if all(_admits(patterns, p) for patterns in sets):
                allowed.append(p)
            continue
        lacking = [s for s in sets if all(_key(q) != _key(p) for q in s)]
        if not lacking:
            allowed.append(p)
        elif any(_is_glob(q) for s in lacking for q in s):
            raise Unrepresentable(f"{capability}: allowed globs {p!r} and another do not intersect")
    return allowed


def ruleset(rules: List[Mapping[str, Any]], capability: str) -> Optional[Dict[str, Any]]:
    """The Crew ruleset for *capability*'s rules, or ``None`` if none restricts it."""
    denied: List[str] = []
    excludes: List[List[str]] = []
    deny_all = False
    for rule in rules:
        if rule.get("capability") != capability:
            continue
        effect = rule.get("effect")
        if effect not in _EFFECTS:
            raise TranslationError(f"{capability}: unknown effect {str(effect)[:40]!r}")
        if effect == "allow":
            continue
        if rule.get("match") is not None:
            denied.extend(_patterns(rule, "match", capability))
        elif rule.get("exclude") is not None:
            excludes.append(_patterns(rule, "exclude", capability))
        else:
            deny_all = True
    if deny_all:
        return {"mode": MODE_ALLOW, "allow": []}
    if excludes:
        allowed = _intersect(excludes, capability)
        for entry in denied:
            # Removing the equal entry is not enough: what is left may still admit it.
            allowed = [p for p in allowed if _key(p) != _key(entry)]
            if any(_is_glob(p) and _matches(entry, p) for p in allowed):
                raise Unrepresentable(f"{capability}: deny {entry!r} inside an allowed glob")
            if _is_glob(entry):
                # A denied glob removes the allowed literals it matches exactly. Beside
                # an allowed glob, what is left has no list form.
                allowed = [p for p in allowed if _is_glob(p) or not _matches(p, entry)]
                if any(_is_glob(p) for p in allowed):
                    raise Unrepresentable(f"{capability}: deny {entry!r} beside an allowed glob")
        return {"mode": MODE_ALLOW, "allow": sorted(set(allowed))}
    # Deny ``match`` entries with no ``exclude`` grant nothing. Leaving the capability
    # unnamed keeps the enterprise baseline (off). A deny-list would replace that
    # baseline and turn on every channel it does not name.
    return None


def neutral_document() -> Dict[str, Any]:
    """A document that governs nothing beyond what the build itself locks."""
    return {
        "version": 1,
        "description": _DESCRIPTION,
        "boot": dict(BOOT),
        "distribution": {"refresh_interval_secs": REFRESH_INTERVAL_SECS},
    }


def translate(policy: str) -> Dict[str, Any]:
    """Translate a Trust V2 *policy* into a Crew policy document, or raise."""
    rules = _rules(policy)
    doc = neutral_document()
    for capability, path in MANAGED.items():
        rs = ruleset(rules, capability)
        if rs is None:
            continue
        node = doc
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = rs
    return doc
