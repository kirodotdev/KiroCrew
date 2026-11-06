"""Turn the control plane's enterprise-management policy into a Kiro Crew policy document.

The policy arrives as Trust V2 YAML: ``rules: [{capability, effect, match | exclude,
when?}]``, already resolved for the ``CREW`` client surface. Kiro Crew governs only the
capabilities in :data:`MANAGED`; every other capability belongs to another client and
is ignored.

Per managed capability with a resource the rules reduce to one Crew ruleset, because a
Crew ruleset has one mode (allow-list or deny-list):

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

A ruleset row may carry a floor: entries its Crew scope never forbids
(``approval_modes`` keeps ``normal``, ``trust`` and ``trust_reads``). They are added to
the allowed set, because the scope refuses an allow-list that leaves one out.

An on/off capability (one with no resource) reduces to ``{"enabled": bool}``
instead, by :func:`switch`:

* any ``deny``/``ask``: ``enabled: false``;
* an ``allow`` and no deny: ``enabled: true``;
* no rule: unnamed, so the enterprise baseline holds (Crew's default for an open
  control such as ``cron``);
* a rule with ``match``, ``exclude`` or ``when``: ``enabled: false``. The policy language
  refuses ``match`` and ``exclude`` on an on/off capability, and the grant shape the control
  plane sends for one is not confirmed yet, so a shape this module does not know turns the
  capability off rather than guess. Leaving it unnamed would keep an open control on.

The reply's settings (``body.configuration``) arrive beside the policy. Each entry's
``value`` is the setting serialized as JSON. One setting is read today, by
:func:`sandbox_floor` (RFC D7): ``crew_sandbox_min_level`` becomes the floor
``sandbox.min_level``, tighten-only. ``cc`` and ``strict`` set the floor. ``off`` and
``standard`` set none, the same as no entry: Crew's own default already runs at
``standard``, and the sandbox enforcer treats any floor as a governed host, so a floor
that adds nothing would still change behaviour. Any other value is not a level the
organisation picked, so it sets none either and logs a warning: Crew adds no
strictness of its own. Every other setting is ignored.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml  # type: ignore[import-untyped]

from kiro_crew.platform.governance import _ORDINAL_SCALES, MODE_ALLOW
from kiro_crew.platform.governance_parts.catalog import SCOPE_CATALOG

logger = logging.getLogger(__name__)

#: A ruleset row: the rules reduce to one allow-list (:func:`ruleset`).
RULESET = "ruleset"
#: An on/off row: the rules reduce to ``{"enabled": bool}`` (:func:`switch`).
SWITCH = "switch"


@dataclass(frozen=True)
class Managed:
    """Where a policy capability lands in a Crew policy document, and in which form."""

    path: Tuple[str, ...]
    kind: str = RULESET
    #: Entries the Crew scope never forbids, added to a translated allow-list.
    floor: Tuple[str, ...] = ()


#: Each capability Kiro Crew governs from enterprise management, and where it lands.
MANAGED: Mapping[str, Managed] = MappingProxyType(
    {
        "messaging_channel": Managed(("channels", "members")),
        "messaging": Managed(("capabilities", "messaging"), SWITCH),
        "cron": Managed(("capabilities", "cron"), SWITCH),
        "permission_mode": Managed(
            ("approval_modes",), floor=SCOPE_CATALOG["approval_modes"].always_permitted
        ),
    }
)

#: How often the running gateway re-reads the policy: five minutes, the Kiro agent's
#: enterprise-management refresh interval. A document with no interval is read at boot
#: only.
REFRESH_INTERVAL_SECS = 300

#: The boot controls of every translated document. The enterprise policy has no rule for
#: them, so they hold the enterprise defaults: the terminal off, a failed policy load
#: refuses to start, and no boot-time sandbox requirement. The sandbox level itself is
#: the ``sandbox.min_level`` floor :func:`sandbox_floor` sets (RFC D7).
BOOT: Mapping[str, bool] = MappingProxyType(
    {"require_sandbox": False, "allow_terminal": False, "fail_closed": True}
)

_DENY_EFFECTS = ("deny", "ask")
_EFFECTS = ("allow",) + _DENY_EFFECTS
_GLOB_CHARS = frozenset("*?[")
_DESCRIPTION = "Translated from the organisation's enterprise-management policy"

#: The setting that carries the minimum sandbox level (RFC D7).
SANDBOX_SETTING = "crew_sandbox_min_level"
#: The levels, loosest to strictest. Owned by the governance enforcer, never re-encoded.
_SANDBOX_SCALE: Tuple[str, ...] = _ORDINAL_SCALES["sandbox"]
#: A floor at or below this level adds nothing to Crew's own default, so none is set.
_SANDBOX_DEFAULT_LEVEL = "standard"


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


def switch(rules: List[Mapping[str, Any]], capability: str) -> Optional[Dict[str, bool]]:
    """The ``{"enabled": ...}`` control for on/off *capability*, or ``None`` to keep
    the baseline."""
    granted = denied = unknown = False
    for rule in rules:
        if rule.get("capability") != capability:
            continue
        effect = rule.get("effect")
        if effect not in _EFFECTS:
            raise TranslationError(f"{capability}: unknown effect {str(effect)[:40]!r}")
        if any(rule.get(key) is not None for key in ("match", "exclude", "when")):
            unknown = True
        elif effect == "allow":
            granted = True
        else:
            denied = True
    if unknown or denied:
        # A shape this module does not know turns the capability off, as an
        # unconditional deny does. Leaving it unnamed is not safe: an unnamed
        # open control (``capabilities.cron``, RFC D3) keeps
        # Crew's default, so a policy that restricts it would widen it instead.
        return {"enabled": False}
    if granted:
        return {"enabled": True}
    return None


def _with_floor(rs: Dict[str, Any], floor: Tuple[str, ...]) -> Dict[str, Any]:
    allowed = {_key(p): p for p in rs["allow"]}
    for entry in floor:
        allowed.setdefault(_key(entry), entry)
    return {"mode": MODE_ALLOW, "allow": sorted(allowed.values())}


def _control(
    rules: List[Mapping[str, Any]], capability: str, row: Managed
) -> Optional[Dict[str, Any]]:
    if row.kind == SWITCH:
        return switch(rules, capability)
    rs = ruleset(rules, capability)
    if rs is not None and row.floor:
        rs = _with_floor(rs, row.floor)
    return rs


def neutral_document() -> Dict[str, Any]:
    """A document that governs nothing beyond what the build itself locks."""
    return {
        "version": 1,
        "description": _DESCRIPTION,
        "boot": dict(BOOT),
        "distribution": {"refresh_interval_secs": REFRESH_INTERVAL_SECS},
    }


def _sandbox_level(value: object) -> Optional[str]:
    """The level one ``crew_sandbox_min_level`` entry names, or ``None`` if it names none."""
    try:
        level = json.loads(value) if isinstance(value, str) else None
    except ValueError:
        level = None
    if isinstance(level, str) and level in _SANDBOX_SCALE:
        return level
    logger.warning(
        "The organisation's %s is not one of %s; it sets no sandbox floor",
        SANDBOX_SETTING,
        ", ".join(_SANDBOX_SCALE),
    )
    return None


def sandbox_floor(configuration: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """The ``sandbox.min_level`` floor the settings set, or ``None`` for no floor.

    Tighten-only (RFC D7): only a level stricter than Crew's own default sets one, so
    ``off`` and ``standard`` set none, the same as no entry. A value that is not a known
    level sets none and logs a warning. Several entries take the strictest known level.
    """
    named = [
        _sandbox_level(e.get("value")) for e in configuration if e.get("key") == SANDBOX_SETTING
    ]
    levels = [level for level in named if level is not None]
    if not levels:
        return None
    strictest = max(levels, key=_SANDBOX_SCALE.index)
    if _SANDBOX_SCALE.index(strictest) <= _SANDBOX_SCALE.index(_SANDBOX_DEFAULT_LEVEL):
        return None
    return strictest


def translate(policy: str, configuration: Sequence[Mapping[str, Any]] = ()) -> Dict[str, Any]:
    """Translate a Trust V2 *policy* and the reply's *configuration* into a Crew policy
    document, or raise."""
    rules = _rules(policy)
    doc = neutral_document()
    floor = sandbox_floor(configuration)
    if floor is not None:
        doc["sandbox"] = {"min_level": floor}
    for capability, row in MANAGED.items():
        control = _control(rules, capability, row)
        if control is None:
            continue
        node = doc
        for key in row.path[:-1]:
            node = node.setdefault(key, {})
        node[row.path[-1]] = control
    return doc
