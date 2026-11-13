"""Turn the control plane's enterprise-management policy into a Kiro Crew policy document.

The policy arrives as Trust V2 YAML: ``rules: [{capability, effect, match | exclude,
when?}]``, already resolved for the ``CREW`` client surface. Kiro Crew governs only the
capabilities in :data:`MANAGED`; every other capability belongs to another client and
is ignored. A rule on a meta-capability (``all``, ``builtin``, ``filesystem``) reaches
each managed capability beneath it (:func:`_meta_children`).

Per managed capability with a resource the rules reduce to one Crew ruleset, because a
Crew ruleset has one mode (allow-list or deny-list). A capability any rule names is
governed: as the policy language reads it, what no ``allow`` grants is denied.

* ``deny``/``ask`` with neither ``match`` nor ``exclude``: nothing is allowed (``allow []``);
* ``deny``/``ask`` with ``exclude E``: only ``E`` is allowed (several excludes intersect).
  The control plane sends one beside every grant, so it says what is allowed;
* ``allow`` with ``match L`` and no such ``exclude``: ``L`` is allowed;
* ``allow`` with no ``match`` and no such ``exclude``: everything is allowed, so the
  rules become a deny-list of the ``deny match`` entries (empty when there are none);
* ``deny``/``ask`` with ``match L``: ``L`` is taken out of the allowed set. For a
  capability whose baseline allows something (``permission_mode``) and that no
  ``allow`` names, that baseline is the allowed set it is taken out of;
* a capability rules name but nothing grants: nothing is allowed.

An ``allow`` with ``when`` or with ``exclude`` grants nothing here: the reply is already
resolved for this user, so neither shape is expected, and dropping a grant only
narrows. A ``deny`` with ``when`` applies as if unconditional, which also only narrows.
A pattern that is exactly ``*`` covers every resource.

Each row converts a policy pattern into the patterns its Crew scope's matcher reads
(:attr:`Managed.convert`). A pattern with no exact Crew form is dropped from an allowed
set, which narrows, and widened in a denied set, which also narrows. A denied pattern
that cannot be widened safely refuses the policy (:class:`Unrepresentable`).

A denied glob removes the allowed literals it matches. A denied entry inside an allowed
glob, or a denied glob beside an allowed glob, has no one-mode form. The translation
refuses it with :class:`Unrepresentable` rather than widen or narrow the set, so the
fetch fails. Entries compare as the row's Crew matcher compares them. ``ask`` reads as
``deny``. A managed capability no rule names is left out of the document, so the
enterprise baseline holds (off, or Crew's default for an open control).

A ruleset row may carry a floor: entries its Crew scope never forbids
(``approval_modes`` keeps ``normal``). It is added to an allowed set and taken
out of a denied one, because the scope refuses a policy that forbids it.

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

A gated row (``subagent``) is a Crew capability switch with an inner list: its ruleset
lands as ``{"enabled": bool, "scopes": {<inner>: ruleset}}`` (:func:`_gate`).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, cast

import yaml  # type: ignore[import-untyped]

from kiro_crew.platform.governance import _ORDINAL_SCALES, MODE_ALLOW, MODE_DENY
from kiro_crew.platform.governance_parts.baseline import BASELINE_CONTROLS
from kiro_crew.platform.governance_parts.catalog import SCOPE_CATALOG
from kiro_crew.platform.governance_parts.matching import _MATCHERS, _expand

logger = logging.getLogger(__name__)

#: A ruleset row: the rules reduce to one allow-list or deny-list (:func:`ruleset`).
RULESET = "ruleset"
#: An on/off row: the rules reduce to ``{"enabled": bool}`` (:func:`switch`).
SWITCH = "switch"
#: An on/off row with an inner list: a ruleset under ``{"enabled", "scopes"}`` (:func:`_gate`).
GATE = "gate"

#: A policy pattern's place in a reduction: what it grants, or what it takes away.
ALLOW_ROLE = "allow"
DENY_ROLE = "deny"

_EVERYTHING = "*"


class TranslationError(ValueError):
    """The policy cannot be translated."""


class Unrepresentable(TranslationError):
    """The rules for a managed capability have no one-mode Crew form."""


def _single_star(pattern: str, role: str) -> List[str]:
    """A policy pattern whose only wildcard is ``*`` (spec 4.3), for an fnmatch matcher.

    ``?`` and ``[`` are literal in the policy language and special to fnmatch, so each
    is put in a one-character class.
    """
    del role
    return [pattern.replace("[", "[[]").replace("?", "[?]")]


def _drop_or_refuse(pattern: str, role: str, why: str) -> List[str]:
    """No Crew form: an allowed pattern is dropped (narrows); a denied one refuses."""
    if role == ALLOW_ROLE:
        return []
    raise Unrepresentable(f"denied pattern {pattern[:80]!r}: {why}")


def _mcp_patterns(pattern: str, role: str) -> List[str]:
    """A ``server/tool`` policy pattern as Crew ``mcp`` references (``@server[/tool]``).

    Crew's ``mcp`` matcher has no wildcard: ``@server`` covers every tool of that server
    and ``@server/tool`` only itself. ``server/*`` is ``@server``. A tool glob is widened
    to its whole server when denied and dropped when allowed. A server glob has no Crew
    form.
    """
    text = pattern.strip()
    server, sep, tool = text.partition("/")
    if not server or text.startswith("@") or any(c in server for c in "*?["):
        return _drop_or_refuse(pattern, role, "no Crew form for this MCP server name")
    if not sep:
        # Names no ``server/tool`` resource, so an allow grants nothing; a deny is
        # read as the whole server, which only narrows.
        return [] if role == ALLOW_ROLE else [f"@{server}"]
    if tool == _EVERYTHING:
        return [f"@{server}"]
    if not tool or "/" in tool:
        return _drop_or_refuse(pattern, role, "not a server/tool reference")
    if "*" in tool:
        return [] if role == ALLOW_ROLE else [f"@{server}"]
    return [f"@{server}/{tool}"]


def _expand_braces(pattern: str, start: int = 0) -> Optional[List[str]]:
    """``{a,b}`` alternatives as separate patterns, or ``None`` when Crew cannot say it.

    A brace with no comma, such as ``{prod}``, is literal, as in minimatch. A range
    (``{1..3}``) and a nested or unbalanced brace have no Crew form.
    """
    open_at = pattern.find("{", start)
    if open_at < 0:
        return None if "}" in pattern[start:] else [pattern]
    end = pattern.find("}", open_at)
    if end < 0 or "{" in pattern[open_at + 1 : end] or "}" in pattern[start:open_at]:
        return None
    body = pattern[open_at + 1 : end]
    if ".." in body:
        return None
    if "," not in body:
        return _expand_braces(pattern, end + 1)
    out: List[str] = []
    for alt in body.split(","):
        rest = _expand_braces(pattern[:open_at] + alt + pattern[end + 1 :], open_at + len(alt))
        if rest is None:
            return None
        out.extend(rest)
    return out


def _any_case(pattern: str) -> str:
    """*pattern* with each ASCII letter outside a ``[...]`` class matching either case."""
    out: List[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "[":
            # A class runs to the first ``]`` after its first member, which may itself
            # be ``]`` (after an optional ``!``, fnmatch's only negation), as fnmatch
            # reads it. With no closing ``]`` the ``[`` is literal, written ``[[]`` so
            # it cannot open a class with a later one.
            j = i + 1
            if j < len(pattern) and pattern[j] == "!":
                j += 1
            if j < len(pattern) and pattern[j] == "]":
                j += 1
            end = pattern.find("]", j)
            if end >= 0:
                out.append(pattern[i : end + 1])
                i = end + 1
                continue
            out.append("[[]")
        elif ch.isascii() and ch.isalpha():
            out.append(f"[{ch.lower()}{ch.upper()}]")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


#: The separator of a policy-language path. KGL writes ``/`` on every host, so this is
#: the language's own separator, not the gateway's ``os.sep``.
_SEP = "/"
#: The two-character openers of a minimatch extended glob, such as ``@(a|b)``.
_EXTGLOB_OPENERS = ("@(", "+(", "!(", "?(", "*(")


def _non_canonical(text: str) -> str:
    """Why *text* is not a canonical policy path, or ``""`` when it is.

    Crew's path matcher normalizes the item it checks and matches the pattern as
    written, so only a pattern already in the item's form can match: an absolute path
    (``/``, ``~`` or ``~/...``) with one ``/`` between segments, no trailing ``/``, no
    ``.`` or ``..`` segment, and none of the shapes fnmatch and minimatch read
    differently.
    """
    if not text:
        return "an empty path"
    if "\\" in text:
        return "a backslash separator"
    if len(text) >= 2 and text[0].isascii() and text[0].isalpha() and text[1] == ":":
        return "a drive letter"
    if not (text.startswith("/") or text == "~" or text.startswith("~/")):
        return "not an absolute or home path"
    if "$" in text or any(op in text for op in _EXTGLOB_OPENERS):
        return "no Crew form for this path glob"
    if "[" in text:
        # minimatch and fnmatch read bracket classes differently (``^``, an unclosed
        # ``[``, a leading ``]``, POSIX classes), so a class has no exact Crew form.
        return "a bracket class has no exact Crew form"
    if text == "/":
        return ""
    segments = text.split(_SEP)
    if segments[-1] == "":
        return "a trailing separator"
    if "" in segments[1:]:
        return "a repeated separator"
    if "." in segments or ".." in segments:
        return "a dot segment"
    return ""


def _globstar_as_star(pattern: str) -> str:
    """*pattern* with each whole ``**`` segment joined to the one before it by ``*``.

    A ``*`` there covers every choice of zero or more folders, and more, which a deny
    may.
    """
    segments = pattern.split(_SEP)
    out = [segments[0]]
    for segment in segments[1:]:
        if segment == "**":
            out[-1] += "*"
        else:
            out.append(segment)
    return _SEP.join(out)


def _fs_one(pattern: str, role: str) -> List[str]:
    """One canonical policy path as Crew ``path`` matcher patterns."""
    wild = any(c in pattern for c in "*?[")
    if role == ALLOW_ROLE:
        partial = [segment for segment in pattern.split(_SEP) if segment != "**"]
        if any(c in segment for segment in partial for c in "*?["):
            # A policy ``*`` stays inside one path segment; Crew's crosses ``/``.
            return []
        # Fewer choices only narrow an allow.
        variants = [pattern, pattern.replace("/**/", "/")]
        if pattern.endswith("/**"):
            variants.append(pattern[: -len("/**")])
    else:
        # ``~`` is expanded first, because ``~*`` does not name the home directory.
        variants = [_globstar_as_star(_expand(pattern))]
    out: List[str] = []
    for v in variants:
        out.append(v)
        if not wild:
            # A pattern with no wildcard names a directory too and covers beneath it.
            out.append(v.rstrip("/") + "/*")
    if role == DENY_ROLE:
        # The policy language matches case-insensitively on a case-insensitive
        # filesystem; folding every deny keeps that and only narrows elsewhere.
        out = [_any_case(v) for v in out]
    return list(dict.fromkeys(out))


def _fs_patterns(pattern: str, role: str) -> List[str]:
    """A policy file-path glob (minimatch, spec 4.3) as Crew ``path`` matcher patterns.

    Only a canonical path is read (:func:`_non_canonical`); any other refuses a deny
    and is dropped from an allow. Each brace alternative is checked on its own, since
    a brace can rebuild a repeated separator or a dot segment. An allow is exact for a
    path whose only wildcard is a whole ``**`` segment, or with none (then it also
    covers what is beneath it); any other allow is dropped. A deny is widened where
    Crew cannot be exact: letters match either case, and a ``**`` segment may stand
    for none.
    """
    expanded = _expand_braces(pattern)
    if expanded is None:
        return _drop_or_refuse(pattern, role, "unbalanced or nested braces")
    out: List[str] = []
    for one in expanded:
        why = _non_canonical(one)
        if why:
            return _drop_or_refuse(pattern, role, why)
        out.extend(_fs_one(one, role))
    return list(dict.fromkeys(out))


@dataclass(frozen=True)
class Managed:
    """Where a policy capability lands in a Crew policy document, and in which form."""

    path: Tuple[str, ...]
    kind: str = RULESET
    #: Entries the Crew scope never forbids, added to a translated allow-list.
    floor: Tuple[str, ...] = ()
    #: The Crew matcher the landed patterns are read with.
    matcher: str = "identifier"
    #: One policy pattern, in an allowed or a denied set, as Crew patterns.
    convert: Callable[[str, str], List[str]] = _single_star
    #: A gated row's inner list (``capabilities.spawn`` -> ``agents``).
    inner: str = ""
    #: Every value of a closed list, so a deny-list can be written as an allow-list.
    values: Tuple[str, ...] = ()
    #: The scope's enterprise baseline when it allows something. With no grant of its
    #: own, a ``deny match`` then takes entries out of this list instead of leaving none.
    baseline: Tuple[str, ...] = ()


#: Each capability Kiro Crew governs from enterprise management, and where it lands.
MANAGED: Mapping[str, Managed] = MappingProxyType(
    {
        "messaging_channel": Managed(("channels", "members")),
        "messaging": Managed(("capabilities", "messaging"), SWITCH),
        "cron": Managed(("capabilities", "cron"), SWITCH),
        # Open controls (RFC D3) under their appendix policy terms, so a
        # remote deny still turns them off; unnamed, they keep Crew's default.
        "memory_writes": Managed(("capabilities", "memory_writes"), SWITCH),
        "theme_persona": Managed(("capabilities", "theme_persona"), SWITCH),
        "permission_mode": Managed(
            ("approval_modes",),
            floor=SCOPE_CATALOG["approval_modes"].always_permitted,
            # The policy language's closed ``approvalMode`` list; only ``normal``
            # is the floor, so a rule may take out ``trust`` or ``trust_reads``.
            values=("normal", "trust_reads", "trust", "yolo"),
            baseline=tuple(cast(List[str], BASELINE_CONTROLS["approval_modes"]["allow"])),
        ),
        "shell": Managed(("commands",), matcher=SCOPE_CATALOG["commands"].matcher),
        "fs_read": Managed(
            ("filesystem", "read"),
            matcher=SCOPE_CATALOG["filesystem.read"].matcher,
            convert=_fs_patterns,
        ),
        "fs_write": Managed(
            ("filesystem", "write"),
            matcher=SCOPE_CATALOG["filesystem.write"].matcher,
            convert=_fs_patterns,
        ),
        "network_egress": Managed(
            ("network", "egress"), matcher=SCOPE_CATALOG["network.egress"].matcher
        ),
        "mcp": Managed(("mcp",), matcher=SCOPE_CATALOG["mcp"].matcher, convert=_mcp_patterns),
        "subagent": Managed(
            ("capabilities", "spawn"),
            GATE,
            matcher=SCOPE_CATALOG["capabilities.spawn"].scope_matchers["agents"],
            inner="agents",
        ),
        "tailnet_origin": Managed(("capabilities", "tailnet_origin"), SWITCH),
        "browser_use": Managed(("capabilities", "browse"), SWITCH),
        "theme_install": Managed(("capabilities", "theme_install"), SWITCH),
    }
)

#: A digest of :data:`MANAGED`. The policy cache records it with each translated copy
#: (``policy_distribution_source._source_identity``), so a copy an older table wrote is
#: refetched and translated again instead of served.
TRANSLATION_TAG = hashlib.sha256(
    repr(sorted((name, row.path, row.kind, row.floor) for name, row in MANAGED.items())).encode()
).hexdigest()[:12]

#: How often the running gateway re-reads the policy: five minutes, the Kiro agent's
#: enterprise-management refresh interval. A document with no interval is read at boot
#: only.
REFRESH_INTERVAL_SECS = 300

#: The boot controls of every translated document. The enterprise policy has no rule for
#: them, so they hold the enterprise defaults: a failed policy load refuses to start, and
#: no boot-time sandbox requirement. ``allow_terminal`` is left out, so a translated
#: policy has no opinion on the dashboard terminal. The sandbox level itself is
#: the ``sandbox.min_level`` floor :func:`sandbox_floor` sets (RFC D7).
#: A change here bumps ``enterprise_policy_fetch.TRANSLATION_REVISION``, so the first
#: refresh after an upgrade fetches a full reply instead of keeping an older build's
#: document. TRANSLATION_TAG is left alone: changing it would refuse that cached copy
#: outright and stop an offline host from booting.
BOOT: Mapping[str, bool] = MappingProxyType({"require_sandbox": False, "fail_closed": True})

_DENY_EFFECTS = ("deny", "ask")
_EFFECTS = ("allow",) + _DENY_EFFECTS
_GLOB_CHARS = frozenset("*?[")
_DESCRIPTION = "Translated from the organisation's enterprise-management policy"
_DEFAULT_ROW = Managed(())

#: The setting that carries the minimum sandbox level (RFC D7).
SANDBOX_SETTING = "crew_sandbox_min_level"
#: The levels, loosest to strictest. Owned by the governance enforcer, never re-encoded.
_SANDBOX_SCALE: Tuple[str, ...] = _ORDINAL_SCALES["sandbox"]
#: A floor at or below this level adds nothing to Crew's own default, so none is set.
_SANDBOX_DEFAULT_LEVEL = "standard"


def _folds(row: Managed) -> bool:
    """Whether the row's Crew matcher ignores case and surrounding space."""
    return row.matcher in ("identifier", "host", "mcp")


def _key(pattern: str, row: Managed = _DEFAULT_ROW) -> str:
    return pattern.strip().casefold() if _folds(row) else pattern


def _is_glob(pattern: str, row: Managed = _DEFAULT_ROW) -> bool:
    if row.matcher == "mcp":
        # ``@server`` covers every ``@server/tool``.
        return "/" not in pattern
    return bool(_GLOB_CHARS & set(pattern))


def _matches(item: str, pattern: str, row: Managed = _DEFAULT_ROW) -> bool:
    return _MATCHERS[row.matcher](item, pattern)


def _rules(policy: str) -> List[Mapping[str, Any]]:
    try:
        doc = yaml.safe_load(policy)
    except yaml.YAMLError:
        raise TranslationError("the enterprise policy is not valid YAML") from None
    rules = doc.get("rules") if isinstance(doc, dict) else None
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        raise TranslationError("the enterprise policy has no list of rules")
    return rules


def _meta_children(capability: str, effect: object) -> Tuple[str, ...]:
    """The managed capabilities a rule on meta-capability *capability* reaches.

    The hierarchy is the one the KAS translation reads
    (:func:`kiro_crew.agent_sdk.kas_capabilities.meta_capability_expansion`). In the
    policy language ``all`` also holds every Crew term, which that table does not list,
    so a deny on ``all`` reaches every managed capability and an allow reaches only the
    table's: the stricter reading either way.
    """
    from kiro_crew.agent_sdk.kas_capabilities import meta_capability_expansion

    expansion = meta_capability_expansion(capability)
    if expansion is None:
        return ()
    if capability == "all" and effect != "allow":
        return tuple(MANAGED)
    return tuple(c for c in expansion if c in MANAGED)


def _with_meta(rules: List[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """*rules*, plus a copy of each meta-capability rule for every managed child.

    A meta-capability takes no resource, so a copy carries no ``match`` or ``exclude``:
    a deny copy then denies the whole child (it only narrows), and an allow that named
    resources grants nothing.
    """
    out = list(rules)
    for rule in rules:
        capability = rule.get("capability")
        if not isinstance(capability, str):
            raise TranslationError("a rule's 'capability' is not a string")
        children = _meta_children(capability, rule.get("effect"))
        if not children:
            continue
        scoped = _present(rule, "match") or _present(rule, "exclude")
        if scoped and rule.get("effect") == "allow":
            continue
        base = {k: v for k, v in rule.items() if k not in ("capability", "match", "exclude")}
        out.extend({**base, "capability": child} for child in children)
    return out


def _present(rule: Mapping[str, Any], key: str) -> bool:
    """Whether *rule* names resources under *key*: an empty list names none."""
    return rule.get(key) is not None and rule.get(key) != []


def _patterns(rule: Mapping[str, Any], key: str, capability: str) -> List[str]:
    raw = rule.get(key)
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise TranslationError(f"{capability}: '{key}' is not a list of strings")
    return list(raw)


def _covers_everything(patterns: List[str], row: Managed) -> bool:
    """Whether a pattern is ``*`` as the row's matcher reads it (spaces count unless it folds)."""
    return any(_key(p, row) == _EVERYTHING for p in patterns)


def _converted(patterns: List[str], role: str, row: Managed) -> List[str]:
    out: List[str] = []
    for p in patterns:
        out.extend(row.convert(p, role))
    return out


def _admits(patterns: List[str], entry: str, row: Managed = _DEFAULT_ROW) -> bool:
    """Does the allowed set *patterns* admit the literal *entry*?"""
    return any(
        _key(p, row) == _key(entry, row) or (_is_glob(p, row) and _matches(entry, p, row))
        for p in patterns
    )


def _intersect(sets: List[List[str]], capability: str, row: Managed = _DEFAULT_ROW) -> List[str]:
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
            seen.setdefault(_key(p, row), p)
    allowed: List[str] = []
    for p in seen.values():
        if not _is_glob(p, row):
            if all(_admits(patterns, p, row) for patterns in sets):
                allowed.append(p)
            continue
        lacking = [s for s in sets if all(_key(q, row) != _key(p, row) for q in s)]
        if not lacking:
            allowed.append(p)
        elif any(_is_glob(q, row) for s in lacking for q in s):
            raise Unrepresentable(f"{capability}: allowed globs {p!r} and another do not intersect")
    return allowed


def _without_case_classes(text: str) -> str:
    """*text* with each one-letter class :func:`_any_case` writes put back as its letter."""
    out: List[str] = []
    i = 0
    while i < len(text):
        window = text[i : i + 4]
        if (
            len(window) == 4
            and window[0] == "["
            and window[3] == "]"
            and window[1].isascii()
            and window[1].islower()
            and window[2] == window[1].upper()
        ):
            out.append(window[1])
            i += 4
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _literal_prefix(pattern: str, row: Managed) -> str:
    """The text every match of *pattern* starts with, compared as the row compares."""
    if row.matcher == "path":
        # Crew's path matcher reads either separator alike on Windows.
        pattern = _without_case_classes(_expand(pattern).replace("\\", "/")).casefold()
    else:
        pattern = _key(pattern, row)
    cut = min((i for i, c in enumerate(pattern) if c in _GLOB_CHARS), default=len(pattern))
    return pattern[:cut]


def _may_meet(a: str, b: str, row: Managed) -> bool:
    """Whether patterns *a* and *b* can match one item: one literal prefix starts the other.

    Errs towards yes, so a disjoint pair it cannot prove is still refused.
    """
    if row.matcher == "mcp":
        # ``@server`` covers only ``@server/...``: servers meet when they are equal.
        return _key(a, row).partition("/")[0] == _key(b, row).partition("/")[0]
    pa, pb = _literal_prefix(a, row), _literal_prefix(b, row)
    return pa.startswith(pb) or pb.startswith(pa)


def _without_denied(
    allowed: List[str], denied: List[str], capability: str, row: Managed
) -> List[str]:
    for entry in denied:
        # Removing the equal entry is not enough: what is left may still admit it.
        allowed = [p for p in allowed if _key(p, row) != _key(entry, row)]
        globs = [p for p in allowed if _is_glob(p, row) and _may_meet(p, entry, row)]
        if any(_matches(entry, p, row) for p in globs):
            raise Unrepresentable(f"{capability}: deny {entry!r} inside an allowed glob")
        if _is_glob(entry, row):
            # A denied glob removes the allowed literals it matches exactly. Beside
            # an allowed glob it can meet, what is left has no list form.
            allowed = [p for p in allowed if _is_glob(p, row) or not _matches(p, entry, row)]
            if globs:
                raise Unrepresentable(f"{capability}: deny {entry!r} beside an allowed glob")
    return allowed


def _sorted_unique(patterns: List[str], row: Managed) -> List[str]:
    unique: Dict[str, str] = {}
    for p in patterns:
        unique.setdefault(_key(p, row), p)
    return sorted(unique.values())


def ruleset(
    rules: List[Mapping[str, Any]], capability: str, row: Optional[Managed] = None
) -> Optional[Dict[str, Any]]:
    """The Crew ruleset for *capability*'s rules, or ``None`` if no rule names it."""
    row = row or MANAGED.get(capability) or _DEFAULT_ROW
    named = deny_all = allow_all = allow_named = False
    denied: List[str] = []
    excludes: List[List[str]] = []
    granted: List[str] = []
    for rule in rules:
        if rule.get("capability") != capability:
            continue
        named = True
        effect = rule.get("effect")
        if effect not in _EFFECTS:
            raise TranslationError(f"{capability}: unknown effect {str(effect)[:40]!r}")
        # An empty ``match`` or ``exclude`` names nothing, as an omitted one does.
        match = _patterns(rule, "match", capability) if _present(rule, "match") else None
        exclude = _patterns(rule, "exclude", capability) if _present(rule, "exclude") else None
        if effect == "allow":
            # Named even when skipped below: a dropped grant must still keep the
            # baseline out, so it only narrows.
            allow_named = True
            if rule.get("when") is not None or exclude is not None:
                continue
            if match is None or _covers_everything(match, row):
                allow_all = True
            else:
                granted.extend(_converted(match, ALLOW_ROLE, row))
            continue
        if match is not None and not _covers_everything(match, row):
            # A deny's own ``exclude`` beside its ``match`` is ignored: the whole
            # ``match`` is denied, which only narrows.
            denied.extend(_converted(match, DENY_ROLE, row))
        elif exclude is not None and match is None:
            if not _covers_everything(exclude, row):
                excludes.append(_converted(exclude, ALLOW_ROLE, row))
            else:
                # Excluding everything from the deny allows everything.
                allow_all = True
        else:
            deny_all = True
    if not named:
        return None
    if deny_all:
        return {"mode": MODE_ALLOW, "allow": []}
    if excludes:
        allowed = _intersect(excludes, capability, row)
    elif allow_all:
        return {"mode": MODE_DENY, "deny": _sorted_unique(denied, row)}
    elif row.baseline and not allow_named:
        # Only denies name it (``permission_mode``): they narrow the baseline it keeps.
        allowed = list(row.baseline)
    else:
        allowed = granted
    allowed = _without_denied(allowed, denied, capability, row)
    return {"mode": MODE_ALLOW, "allow": _sorted_unique(allowed, row)}


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


def _with_floor(rs: Dict[str, Any], row: Managed) -> Dict[str, Any]:
    floor = row.floor
    if rs["mode"] == MODE_DENY:
        if not row.values:
            kept = {_key(e) for e in floor}
            return {"mode": MODE_DENY, "deny": [p for p in rs["deny"] if _key(p) not in kept]}
        # A denied glob may match a floor entry, which the scope refuses to deny, so
        # the closed list is written out as what stays allowed.
        rs = {
            "mode": MODE_ALLOW,
            "allow": [v for v in row.values if not any(_matches(v, p, row) for p in rs["deny"])],
        }
    allowed = {_key(p): p for p in rs["allow"]}
    for entry in floor:
        allowed.setdefault(_key(entry), entry)
    return {"mode": MODE_ALLOW, "allow": sorted(allowed.values())}


def _gate(rs: Optional[Dict[str, Any]], inner: str) -> Optional[Dict[str, Any]]:
    """A ruleset as a capability switch over its inner list."""
    if rs is None:
        return None
    if rs["mode"] == MODE_ALLOW and not rs["allow"]:
        return {"enabled": False}
    if rs["mode"] == MODE_DENY and not rs["deny"]:
        return {"enabled": True}
    return {"enabled": True, "scopes": {inner: rs}}


def _control(
    rules: List[Mapping[str, Any]], capability: str, row: Managed
) -> Optional[Dict[str, Any]]:
    if row.kind == SWITCH:
        return switch(rules, capability)
    rs = ruleset(rules, capability, row)
    if row.kind == GATE:
        return _gate(rs, row.inner)
    if rs is not None and row.floor:
        rs = _with_floor(rs, row)
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
    rules = _with_meta(_rules(policy))
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
