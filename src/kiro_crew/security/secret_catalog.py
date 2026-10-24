"""Vendor secret-rule catalog scanner (ported from Betterleaks, MIT).

The rule table lives in ``kiro_crew/data/secret_catalog.generated.json``, written
by ``scripts/gen_secret_catalog.py`` from an upstream release; see that script
for what is ported and how to regenerate. This module only loads the table and
scans text with it. It is stdlib only, makes no network call, and never
validates a finding against a vendor.

:func:`catalog_findings` returns the span of the SECRET part of every Secret-tier
finding. A caller that redacts merges overlapping spans itself.

A finding is dropped, in this order, when:

1. the secret's Shannon entropy is below the rule's floor;
2. the rule's allow-list (or the global allow-list) matches the secret;
3. the whole secret is an identifier shape (:func:`is_identifier_value`): an AWS
   key id, a UUID, a 40- or 64-character hex digest, a 12-digit account number
   or an ARN. Those name a thing and cannot authenticate, so they stay readable.
   A vendor whose own token format may be hex or a UUID (its rule spells a
   hex-only character class, or every keyword names the vendor) keeps its
   finding when the value is hex or a UUID: there the vendor's keyword and
   label, not the value's shape, say what the value is.

Identifier-tier rules are loaded for :func:`catalog_rule_tier` but never report.
"""

from __future__ import annotations

import contextvars
import json
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.credential_patterns import TIER_IDENTIFIER, TIER_SECRET
from kiro_crew.credential_values import NAMING_VALUE

CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "secret_catalog.generated.json"

#: Lower-cases ASCII letters only. ``str.lower`` can change a string's length
#: (``"İ".lower()`` is two characters), and every keyword is ASCII, so offsets
#: found in the lowered text must stay offsets in the original.
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _ascii_lower(text: str) -> str:
    return text.translate(_ASCII_LOWER)


#: Prefix on every rule id this module reports, so a merged record names its source.
RULE_PREFIX = "catalog:"

#: Identifier shapes no secret ever takes: an AWS key id, an account number, an ARN.
_NAMING_VALUE = re.compile(NAMING_VALUE, re.ASCII)
#: Identifier shapes some vendors also use for secrets: a UUID, a 40- or 64-hex digest.
_HEX_VALUE = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    r"|[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}",
    re.ASCII,
)
#: A character class made only of hex digits (``[a-f0-9]``, ``[0-9A-F]``, ...): a
#: rule that spells one expects a hex or UUID secret.
_HEX_CLASS = re.compile(r"\[[0-9a-fA-F-]+\]")

#: Words a hex value's label carries when the value is a digest, not a key.
#: Whole words of the label only (``commit_sha``, ``gitCommitSha``): ``sha``
#: inside ``shared`` names nothing.
_DIGEST_WORDS = frozenset(
    {"commit", "sha", "sha1", "sha256", "digest", "checksum", "revision", "rev", "ref"}
)
#: A label's words: a run of capitals, a capitalised word, a lower-case word, or
#: digits glued to the word before them (``sha256``).
_LABEL_WORD_RE = re.compile(r"(?:[A-Z]+(?![a-z])|[A-Z]?[a-z]+)[0-9]*")


def _names_digest(label: str) -> bool:
    """Whether a word of *label* names a digest."""
    return any(w.group().lower() in _DIGEST_WORDS for w in _LABEL_WORD_RE.finditer(label))


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    tier: str
    regex: re.Pattern[str]
    secret_group: int
    keywords: tuple[str, ...]
    entropy_min: float | None
    allow: tuple[re.Pattern[str], ...]
    stopwords: tuple[str, ...]
    hex_secret: bool


@dataclass(frozen=True)
class _Catalog:
    rules: tuple[_Rule, ...]
    by_id: dict[str, _Rule]
    global_allow: tuple[re.Pattern[str], ...]
    #: The Secret-tier rules, and the keyword index over them.
    secret_rules: tuple[_Rule, ...] = ()
    keywordless: tuple[int, ...] = ()
    rules_by_keyword: dict[str, tuple[int, ...]] = field(default_factory=dict)
    #: Every keyword, longest first, matched at each position (a look-ahead).
    keyword_re: re.Pattern[str] = re.compile(r"(?!)")
    #: Each keyword with every keyword that is a substring of it.
    keyword_closure: dict[str, frozenset[str]] = field(default_factory=dict)
    #: The first three characters of every keyword (all are 3+ long).
    keyword_heads: frozenset[str] = frozenset()
    #: Per keyword, the most characters its rules' matches can read
    #: (:func:`_rule_reach`), so the stream holds a value at most that long.
    keyword_reach: dict[str, int] = field(default_factory=dict)


#: A repeat's upper bound in a ported regex.
_REPEAT_BOUND_RE = re.compile(r"(?<!\\)\{(?:[0-9]+,)?([0-9]+)\}")
#: An unbounded repeat outside a character class (the generator leaves the
#: secret's own repeats as upstream wrote them).
_UNBOUNDED_RE = re.compile(r"(?<!\\)(?:[+*]|\{[0-9]+,\})")
_CHAR_CLASS_RE = re.compile(r"\[(?:\\.|[^\]\\])*\]")


def _rule_reach(pattern: str) -> int:
    """At most how many characters *pattern* reads, for the stream's hold: the
    sum of its repeats' upper bounds, at least 64, at most
    :data:`CATALOG_VALUE_MAX` (which an unbounded repeat reaches)."""
    if _UNBOUNDED_RE.search(_CHAR_CLASS_RE.sub("", pattern)):
        return CATALOG_VALUE_MAX
    total = sum(int(bound) for bound in _REPEAT_BOUND_RE.findall(pattern))
    return min(CATALOG_VALUE_MAX, max(64, total))


def _keyword_index(rules: tuple[_Rule, ...]) -> dict[str, Any]:
    """The Secret-tier keyword index :class:`_Catalog` keeps."""
    secret = tuple(rule for rule in rules if rule.tier == TIER_SECRET)
    by_keyword: dict[str, list[int]] = {}
    for i, rule in enumerate(secret):
        for keyword in rule.keywords:
            by_keyword.setdefault(keyword, []).append(i)
    words = sorted(by_keyword, key=len, reverse=True)
    reach = {
        word: max(_rule_reach(secret[i].regex.pattern) for i in by_keyword[word]) for word in words
    }
    pattern = "|".join(re.escape(w) for w in words) or "(?!)"
    return {
        "secret_rules": secret,
        "keywordless": tuple(i for i, rule in enumerate(secret) if not rule.keywords),
        "rules_by_keyword": {k: tuple(v) for k, v in by_keyword.items()},
        "keyword_re": re.compile(f"(?=({pattern}))"),
        "keyword_closure": {w: frozenset(x for x in words if x in w) for w in words},
        "keyword_heads": frozenset(w[:3] for w in words),
        "keyword_reach": reach,
    }


#: Keywords that are ordinary words of any secret field, not a vendor's name.
_GENERIC_KEYWORDS = frozenset(
    {"key", "secret", "token", "access", "api", "auth", "password", "pass", "pwd", "client"}
)


def _hex_secret(raw: dict[str, Any]) -> bool:
    """Whether a rule's own format may be hex or a UUID: its regex spells a
    hex-only class, or every keyword names its vendor (``datadog``), so a hex
    value beside it is that vendor's key, not a digest beside a word like ``key``."""
    keywords = set(raw["keywords"])
    return _HEX_CLASS.search(raw["regex"]) is not None or bool(
        keywords and not keywords & _GENERIC_KEYWORDS
    )


_lock = threading.Lock()
_catalog: _Catalog | None = None


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.ASCII)


def _load() -> _Catalog:
    global _catalog
    if _catalog is not None:
        return _catalog
    with _lock:
        if _catalog is None:
            data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
            rules = tuple(
                _Rule(
                    rule_id=RULE_PREFIX + raw["id"],
                    tier=raw["tier"],
                    regex=_compile(raw["regex"]),
                    secret_group=int(raw["secret_group"]),
                    keywords=tuple(raw["keywords"]),
                    entropy_min=raw["entropy_min"],
                    allow=tuple(_compile(p) for p in raw["allow_regexes"]),
                    stopwords=tuple(raw["stopwords"]),
                    hex_secret=_hex_secret(raw),
                )
                for raw in data["rules"]
            )
            _catalog = _Catalog(
                rules=rules,
                by_id={rule.rule_id: rule for rule in rules},
                global_allow=tuple(_compile(p) for p in data.get("global_allow_regexes", [])),
                **_keyword_index(rules),
            )
    return _catalog


def shannon_entropy(value: str) -> float:
    """Shannon entropy in bits per character (the upstream definition)."""
    if not value:
        return 0.0
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in Counter(value).values())


def is_naming_value(value: str) -> bool:
    """True when the WHOLE value is a key ID, an account ID or an ARN: names only."""
    return _NAMING_VALUE.fullmatch(value) is not None


def is_identifier_value(value: str) -> bool:
    """True when the WHOLE value is an identifier shape that cannot authenticate."""
    return _NAMING_VALUE.fullmatch(value) is not None or _HEX_VALUE.fullmatch(value) is not None


def catalog_rule_ids() -> tuple[str, ...]:
    """Every loaded rule id (both tiers), in catalog order."""
    return tuple(rule.rule_id for rule in _load().rules)


def catalog_rule_tier(rule_id: str) -> str:
    """``'secret'`` or ``'identifier'`` for a loaded rule id; ``KeyError`` otherwise."""
    tier = _load().by_id[rule_id].tier
    return TIER_IDENTIFIER if tier == TIER_IDENTIFIER else TIER_SECRET


def _secret_span(rule: _Rule, match: re.Match[str]) -> tuple[int, int]:
    """The secret's span: the rule's group, the whole match for ``-1``, else the first group."""
    if rule.secret_group < 0:
        return match.span()
    if rule.secret_group:
        return match.span(rule.secret_group)
    for group in range(1, (match.re.groups or 0) + 1):
        start, end = match.span(group)
        if end > start:
            return start, end
    return match.span()


def _dropped(rule: _Rule, secret: str, global_allow: tuple[re.Pattern[str], ...]) -> bool:
    if rule.entropy_min is not None and shannon_entropy(secret) < rule.entropy_min:
        return True
    if any(p.search(secret) for p in rule.allow) or any(p.search(secret) for p in global_allow):
        return True
    lowered = secret.lower()
    if any(word in lowered for word in rule.stopwords):
        return True
    if _NAMING_VALUE.fullmatch(secret) is not None:
        return True
    return not rule.hex_secret and _HEX_VALUE.fullmatch(secret) is not None


#: Keywords (and text) released before the text being scanned, by the stream:
#: a vendor keyword committed earlier still selects its rules (``# Airtable``,
#: then a token any number of commits later), as the whole text does for the
#: batch pass. The rules still run on the scanned text only.
KEYWORD_CONTEXT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "catalog_keyword_context", default=""
)


def _keyword_text(text: str) -> str:
    """*text*, lower-cased, behind the caller's keyword context, if any."""
    context = KEYWORD_CONTEXT.get()
    return _ascii_lower(f"{context}\n{text}" if context else text)


#: Below this length one pass of the keyword regex is cheaper than a substring
#: search per rule; above it the per-rule search (C-level ``in``) is.
_KEYWORD_SCAN_MAX = 4096


def _candidate_rules(catalog: _Catalog, lowered: str) -> list[_Rule]:
    """The Secret-tier rules whose keywords occur in *lowered*, in catalog order."""
    if len(lowered) > _KEYWORD_SCAN_MAX:
        return [
            rule
            for rule in catalog.secret_rules
            if not rule.keywords or any(k in lowered for k in rule.keywords)
        ]
    # Each keyword occurrence starts where the longest-first alternation matches
    # some keyword that it is a prefix of, so the closure finds every one.
    indexes = set(catalog.keywordless)
    for m in catalog.keyword_re.finditer(lowered):
        for keyword in catalog.keyword_closure[m.group(1)]:
            indexes.update(catalog.rules_by_keyword[keyword])
    return [catalog.secret_rules[i] for i in sorted(indexes)]


def catalog_keywords_in(text: str) -> frozenset[str]:
    """The Secret-tier rule keywords that occur in *text*. A finite set: at most
    every keyword the catalog has."""
    catalog = _load()
    found: set[str] = set()
    for m in catalog.keyword_re.finditer(_ascii_lower(text)):
        found.update(catalog.keyword_closure[m.group(1)])
    return frozenset(found)


def has_catalog_keyword(text: str) -> bool:
    """Whether *text* holds any Secret-tier rule keyword (a cheap pre-scan)."""
    lowered = _keyword_text(text)
    catalog = _load()
    if len(lowered) > _KEYWORD_SCAN_MAX:
        return any(k in lowered for k in catalog.rules_by_keyword)
    # Every keyword is 3+ characters, so one must start at a position whose
    # three characters begin some keyword: a set lookup per position first.
    heads = catalog.keyword_heads
    if not any(lowered[i : i + 3] in heads for i in range(len(lowered) - 2)):
        return False
    return catalog.keyword_re.search(lowered) is not None


def catalog_keyword_start(text: str, lo: int = 0) -> int | None:
    """Where the first Secret-tier rule keyword at or after *lo* starts, or None.

    A rule's match starts near its keyword, so the stream holds from here while
    the match may still be arriving. The stream asks twice per feed about the
    same buffer, so the last answer is kept.
    """
    global _last_keyword_start
    if len(text) > _MEMO_MAX:
        # A large text is never kept alive by the memo: answer it uncached.
        _last_keyword_start = (None, 0, None)
        return _catalog_keyword_start(text, lo)
    # One tuple, read and replaced whole: a thread never pairs another
    # thread's text with this one's answer.
    last_text, last_lo, last_found = _last_keyword_start
    if last_lo == lo and last_text == text:
        return last_found
    found = _catalog_keyword_start(text, lo)
    _last_keyword_start = (text, lo, found)
    return found


#: The largest text the memo keeps a reference to: a stream's buffer, which the
#: stream bounds well below this (its fail-closed ceiling), not a whole event.
_MEMO_MAX = 16 * 1024


#: ``(text, lo, answer)`` of the last :func:`catalog_keyword_start` call.
_last_keyword_start: tuple[str | None, int, int | None] = (None, 0, None)


def _catalog_keyword_start(text: str, lo: int) -> int | None:
    # A match still arriving ends in a value with no blank after it yet, so it
    # starts at most _CATALOG_PREFIX_REACH before the trailing non-blank run
    # (keyword, filler, separator), and that run is at most CATALOG_VALUE_MAX
    # long. Only that window is scanned, so a short tail costs a short scan.
    end = len(text)
    floor = max(lo, end - CATALOG_ARRIVING_REACH)
    run = max(text.rfind(blank, floor) for blank in " \t\n\r\f\v") + 1
    lo = max(floor, run - _CATALOG_PREFIX_REACH)
    tail = text[lo:]
    lowered = _ascii_lower(tail)
    catalog = _load()
    # A set lookup per position is far cheaper than the keyword alternation,
    # and most tails hold no keyword head at all.
    heads = catalog.keyword_heads
    if not any(lowered[j : j + 3] in heads for j in range(len(lowered) - 2)):
        return None
    for m in catalog.keyword_re.finditer(lowered):
        # At most as long as the keyword's rules can read: a longer one no
        # rule redacts.
        reach = max(catalog.keyword_reach[k] for k in catalog.keyword_closure[m.group(1)])
        if end - (lo + m.start()) > _CATALOG_PREFIX_REACH + reach:
            continue
        if "[REDACTED" in tail[m.start() :]:
            continue  # what follows is already a tag, not a value
        # A keyword inside the trailing run is a token still arriving (a whole
        # match is the secret: ``ATATT3...``). Before the run it is the
        # keyword's token, a few words or the separator, and a value.
        if lo + m.start() >= run or _CATALOG_ARRIVING_RE.fullmatch(tail, m.start()):
            return lo + m.start()
    return None


#: The longest value the stream holds for a catalog keyword: the stream's own
#: fail-closed ceiling for a long token (``_STREAM_HOLDBACK_JWT_MAX``). A rule
#: whose secret repeats without a bound (a long service-account token) holds
#: this far; a bounded one only as far as its repeats reach.
CATALOG_VALUE_MAX = 4096

#: How far before its value a catalog match starts: token, filler, quotes, the
#: longest operator and its padding.
_CATALOG_PREFIX_REACH = 48 + 20 + 3 + 4 + 5

#: What follows a catalog keyword while its rule's match may still be arriving,
#: with a value that has no blank after it yet: the keyword's token and up to
#: three short words (``airtable token is <PAT>``, ``FlyV1 <token>``), or the
#: token, the rules' filler, the upstream assignment operator set and a value
#: (``heroku key = <uuid>``). Each piece is bounded and the pieces are told
#: apart by blanks, so a fullmatch is linear in the tail.
_CATALOG_ARRIVING_RE = re.compile(
    r"""\S{0,48}(?:[ \t]{1,3}[\w.-]{1,20}){0,3}[ \t]{1,3}\S*"""
    r"""|\S{0,48}[ \t\w.-]{0,20}[\s'"]{0,3}"""
    rf"""(?:(?:=|>|:{{1,3}}=|\|\||:|=>|\?=|,)[`'"\s=]{{0,5}}\S{{0,{CATALOG_VALUE_MAX}}})?"""
)

#: The longest tail the stream reads for a catalog match: prefix and value.
CATALOG_ARRIVING_REACH = _CATALOG_PREFIX_REACH + CATALOG_VALUE_MAX


def _findings(text: str, rules: list[_Rule] | None = None) -> list[tuple[int, int, int, str]]:
    """``(match_start, start, end, rule_id)`` of each kept Secret-tier finding, from
    *rules* (default: the rules whose keywords occur in *text*)."""
    if not text:
        return []
    catalog = _load()
    findings: list[tuple[int, int, int, str]] = []
    for rule in _candidate_rules(catalog, _keyword_text(text)) if rules is None else rules:
        for match in rule.regex.finditer(text):
            start, end = _secret_span(rule, match)
            if start < 0 or end <= start:
                continue
            if _dropped(rule, text[start:end], catalog.global_allow):
                continue
            if (
                rule.hex_secret
                and _HEX_VALUE.fullmatch(text, start, end) is not None
                and _names_digest(text[match.start() : start])
            ):
                # A hex value whose own label names a commit or a digest
                # (``NETLIFY_COMMIT_REF=``, ``git.commit.sha:``) is that digest,
                # an Identifier, even beside a vendor keyword.
                continue
            findings.append((match.start(), start, end, rule.rule_id))
    return findings


def catalog_findings(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, rule_id)`` of each Secret-tier finding's secret, sorted by start."""
    return sorted((start, end, rule_id) for _m, start, end, rule_id in _findings(text))


def catalog_match_spans_near(text: str, pos: int) -> list[tuple[int, int]]:
    """``(start, end)`` of each Secret-tier finding whose keyword sits just before
    *pos*, from that KEYWORD to its secret's end: a stream cut inside one would
    part the keyword from its value, and a rule only runs where its keyword is
    (``airtable: <PAT>``, whose match starts at the PAT).

    Only keywords within :data:`_CATALOG_PREFIX_REACH` before *pos* can start
    such a span, so only their rules run, over the reach after them.
    """
    lo = max(0, pos - _CATALOG_PREFIX_REACH)
    catalog = _load()
    region = _ascii_lower(text[lo : pos + 1])
    rules = [rule for rule in _candidate_rules(catalog, region) if rule.keywords]
    if not rules:
        return []
    window = text[lo : lo + CATALOG_ARRIVING_REACH + _CATALOG_PREFIX_REACH]
    lowered = _ascii_lower(window)
    by_id = {rule.rule_id: rule for rule in rules}
    spans: list[tuple[int, int]] = []
    for match_start, _start, end, rule_id in _findings(window, rules):
        # The nearest keyword of this rule at or before the match, in the window.
        keyword_at = max(
            (lowered.rfind(k, 0, match_start + len(k)) for k in by_id[rule_id].keywords),
            default=-1,
        )
        start = keyword_at if 0 <= keyword_at < match_start else match_start
        spans.append((lo + start, lo + end))
    return sorted(spans)
