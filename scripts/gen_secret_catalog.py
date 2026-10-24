#!/usr/bin/env python3
"""Generate ``src/kiro_crew/data/secret_catalog.generated.json`` from Betterleaks.

The secret catalog is a build-time port of the rule table that ships with
Betterleaks (``config/betterleaks.toml``, MIT licensed). The runtime scanner in
``kiro_crew.security.secret_catalog`` reads only the generated JSON; nothing at
runtime parses TOML, evaluates an expression program or touches the network.

Regenerating for a new upstream release::

    git clone https://github.com/betterleaks/betterleaks <checkout>
    git -C <checkout> checkout <release tag>
    python3 scripts/gen_secret_catalog.py --upstream <checkout>
    python3 -m pytest test/test_secret_catalog.py -q

Review the diff of the JSON (new, changed and skipped rules) like any other
security-relevant change. ``--check`` exits 1 when the committed file differs
from what the generator would write, so a hand edit of the JSON is caught.

What is ported
--------------
* Each rule's regex, translated from Go RE2 syntax to Python ``re`` syntax
  (:func:`translate_go_regex`). A rule whose regex Python cannot compile after
  translation is recorded in ``skipped``.
* The mechanical subset of each rule's ``filter`` program: an entropy floor and
  ``matchesAny`` / ``containsAny`` lists over the secret. Token-ratio clauses
  are dropped (they need a tokenizer this package does not ship), which only
  widens what the scanner reports. Any other program is skipped as
  ``cel-filter``.
* The global filter's ``matchesAny`` / ``containsAny`` lists, as
  ``global_allow_regexes``. The global prefilter is path based and does not
  apply to free text, so it is not carried.
* Allow-list entries that are themselves complete credential-shaped values are
  not carried, so this file never holds one. The rule then also reports those
  values, which only widens redaction.

Validation programs, analyzers, components, confidence and ``skipReport`` are
never ported: the scanner makes no network call. ``skipReport`` rules (parts of a
composite credential such as a host or client id) are kept as Identifier tier.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import tomllib
import warnings
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "src" / "kiro_crew" / "data" / "secret_catalog.generated.json"
UPSTREAM_CONFIG = Path("config") / "betterleaks.toml"
UPSTREAM_LABEL = "betterleaks v1.9.0 config/betterleaks.toml"
COMMAND = "python3 scripts/gen_secret_catalog.py --upstream <betterleaks checkout>"

TIER_SECRET = "secret"
TIER_IDENTIFIER = "identifier"

#: Every reason a rule can be skipped for. The test pins this set.
SKIP_REASONS = (
    "cel-filter",
    "hand-written",
    "no-regex",
    "path-only",
    "path-scoped",
    "regex-unsupported",
)

#: Formats the core redactor already reads with its own structural rule, which
#: validates more than a catalog regex can (a PEM block's END marker, a JWT's
#: JSON header), so the catalog copy is not ported. The two GCP rules match a
#: whole JSON object (``\{[^{]+...[^}]+\}``), which would remove its identifiers
#: and labels and backtracks on a crafted object; their secrets are a
#: ``client_secret`` / ``refresh_token`` value and a PEM key, which the label and
#: private-key rules already remove.
_HAND_WRITTEN = frozenset(
    {
        "private-key",
        "jwt",
        "gcp-application-default-credentials",
        "gcp-service-account",
        # Each vendor format lives in ONE table. The core's fixed patterns
        # (``security/redaction_patterns.py``) are the home of every vendor
        # prefix they hold: pass 1 runs them, they anchor its pre-filter, and
        # they catch shortened and rotated tokens. So a catalog rule for one of
        # those prefixes is not ported at all; the issued spellings it read
        # (``xoxe-``, ``sk-svcacct-``, ``sk_prod_``, a routable ``glpat-``) are
        # in the fixed pattern. ``test_no_vendor_format_lives_in_both_tables``
        # fails when a ported rule and a fixed pattern match the same token.
        "slack-bot-token",
        "slack-legacy-bot-token",
        "slack-legacy-token",
        "slack-legacy-workspace-token",
        "slack-user-token",
        "slack-config-access-token",
        "slack-config-refresh-token",
        "slack-session-token",
        "github-oauth",
        "github-refresh-token",
        "github-fine-grained-pat",
        "gitlab-pat",
        "gitlab-pat-routable",
        "gitlab-pat-routable-versioned",
        "stripe-access-token",
        "clerk-secret-key",
        "highnote-secret-live-key",
        "workos-production-api-key.1",
        # Issued as a JWT, which the structural JWT rule holds.
        "monday-api-token.1",
        "scalr-api-access-token.1",
        "workato-developer-api-token.1",
        "sendgrid-api-token",
        "openai-api-key",
        "anthropic-api-key",
        "anthropic-admin-api-key",
        "npm-access-token",
        "pypi-upload-token",
        "digitalocean-pat",
        "digitalocean-access-token",
        "digitalocean-refresh-token",
        "telegram-bot-api-token",
    }
)

#: Rule ids whose captured value names something rather than authenticating.
_IDENTIFIER_ID = re.compile(r"-(?:client-id|id|user|username)(?:\.\d+)?$")
#: Composite-part rules (``skipReport``) whose part is a location or a name.
_IDENTIFIER_PART = re.compile(r"-(?:url|host|domain|name|address)(?:\.\d+)?$")
#: Ported as Identifier regardless of shape: an AWS key id cannot sign a request,
#: and a Twilio API key SID (``SK`` + 32 hex) needs its separate secret to.
_IDENTIFIER_RULES = frozenset({"aws-access-token", "twilio-api-key"})
#: Id-shaped rule names whose captured value is still a credential: the curl
#: ``-u user:password`` rule captures the password half too.
_SECRET_RULES = frozenset({"curl-auth-user"})


class UnsupportedRegex(ValueError):
    """A Go RE2 construct with no faithful Python ``re`` spelling."""


class UnsupportedFilter(ValueError):
    """A filter program outside the mechanical subset."""


# ---------------------------------------------------------------------------
# Go RE2 -> Python re
# ---------------------------------------------------------------------------

_POSIX_CLASSES = {
    "alnum": "a-zA-Z0-9",
    "alpha": "a-zA-Z",
    "ascii": "\\x00-\\x7f",
    "blank": " \\t",
    "cntrl": "\\x00-\\x1f\\x7f",
    "digit": "0-9",
    "graph": "!-~",
    "lower": "a-z",
    "print": " -~",
    "punct": "!-/:-@\\[-`{-~",
    "space": " \\t\\n\\r\\f\\v",
    "upper": "A-Z",
    "word": "a-zA-Z0-9_",
    "xdigit": "0-9a-fA-F",
}

_FLAG_CHARS = frozenset("ims")

#: The upper bound every unbounded repeat (``+``, ``*``, ``{n,}``) outside a
#: capture group is given.
QUANTIFIER_MAX = 256
_OPEN_RANGE_RE = re.compile(r"\{(\d+),\}")
#: A character class with a bounded repeat, then the same class repeated again.
_ADJACENT_REPEATS_RE = re.compile(r"(\[(?:\\.|[^\]\\])+\])\{(\d+),(\d+)\}\1\{(\d+),(\d+)\}")


def _translate_escape(src: str, i: int, in_class: bool) -> tuple[str, int]:
    """Translate the escape starting at ``src[i] == '\\\\'``; return (text, next index)."""
    if i + 1 >= len(src):
        raise UnsupportedRegex("trailing backslash")
    ch = src[i + 1]
    if ch == "z" and not in_class:
        return "\\Z", i + 2
    if ch == "x" and i + 2 < len(src) and src[i + 2] == "{":
        end = src.find("}", i + 3)
        if end < 0:
            raise UnsupportedRegex("unterminated \\x{")
        value = int(src[i + 3 : end], 16)
        if value <= 0xFF:
            return f"\\x{value:02x}", end + 1
        if value <= 0xFFFF:
            return f"\\u{value:04x}", end + 1
        return f"\\U{value:08x}", end + 1
    if ch in "pPQEC":
        raise UnsupportedRegex(f"\\{ch} escape")
    return src[i : i + 2], i + 2


def _translate_class(src: str, i: int) -> tuple[str, int]:
    """Translate the bracket expression starting at ``src[i] == '['``."""
    out = ["["]
    j = i + 1
    if j < len(src) and src[j] == "^":
        out.append("^")
        j += 1
    if j < len(src) and src[j] == "]":
        out.append("\\]")
        j += 1
    while j < len(src):
        ch = src[j]
        if ch == "]":
            out.append("]")
            return "".join(out), j + 1
        if ch == "\\":
            text, j = _translate_escape(src, j, in_class=True)
            out.append(text)
            continue
        if ch == "[":
            match = re.match(r"\[:(\^?)([a-z]+):\]", src[j:])
            if match:
                if match.group(1) or match.group(2) not in _POSIX_CLASSES:
                    raise UnsupportedRegex("negated or unknown POSIX class")
                out.append(_POSIX_CLASSES[match.group(2)])
                j += match.end()
                continue
            out.append("\\[")
            j += 1
            continue
        if ch in "&~|" and j + 1 < len(src) and src[j + 1] == ch:
            # Python warns on doubled set operators; RE2 reads them as literals.
            out.append(ch + "\\" + ch)
            j += 2
            continue
        out.append(ch)
        j += 1
    raise UnsupportedRegex("unterminated character class")


def _parse_seq(
    src: str, i: int, depth: int, in_capture: bool = False, groups: list[int] | None = None
) -> tuple[list[Any], int]:
    """Parse a sequence up to the closing ``)`` of the current group (or the end)."""
    items: list[Any] = []
    while i < len(src):
        ch = src[i]
        if ch == ")":
            if depth == 0:
                raise UnsupportedRegex("unbalanced )")
            return items, i + 1
        if ch == "|":
            items.append("|")
            i += 1
        elif ch == "\\":
            text, i = _translate_escape(src, i, in_class=False)
            items.append(text)
        elif ch == "[":
            text, i = _translate_class(src, i)
            items.append(text)
        elif ch == "(":
            prefix, flags, i = _group_prefix(src, i)
            if flags is not None:
                items.append(("flags", flags))
                continue
            # *groups* is ``[capturing groups opened so far, the secret's group]``.
            secret = False
            if groups is not None and (prefix == "(" or prefix.startswith("(?P<")):
                groups[0] += 1
                secret = groups[0] == groups[1]
            body, i = _parse_seq(src, i, depth + 1, in_capture or secret, groups)
            items.append(("group", prefix, body))
        elif ch in "+*" and not in_capture:
            # RE2 runs an unbounded repeat in linear time; a backtracking engine
            # does not when two repeats overlap. Bounding every repeat around
            # the secret (prefixes, fillers, separators) bounds the work per
            # start position. A repeat inside the secret's own group stays as
            # upstream wrote it, so a long token is read whole; the screen
            # (:func:`regex_is_bounded`) refuses a rule where that is not linear.
            items.append(f"{{{1 if ch == '+' else 0},{QUANTIFIER_MAX}}}")
            i += 1
        elif not in_capture and (open_range := _OPEN_RANGE_RE.match(src, i)) is not None:
            low = int(open_range.group(1))
            items.append(f"{{{low},{max(low, QUANTIFIER_MAX)}}}")
            i = open_range.end()
        else:
            items.append(ch)
            i += 1
    if depth:
        raise UnsupportedRegex("unbalanced (")
    return items, i


def _group_prefix(src: str, i: int) -> tuple[str, str | None, int]:
    """Read a group opener at ``src[i] == '('``.

    Returns ``(prefix, None, next)`` for a group, or ``("", flags, next)`` for a
    bare ``(?flags)`` setter.
    """
    if not src.startswith("(?", i):
        return "(", None, i + 1
    named = re.match(r"\(\?P?<([A-Za-z_][A-Za-z0-9_]*)>", src[i:])
    if named:
        return f"(?P<{named.group(1)}>", None, i + named.end()
    for opener in ("(?:", "(?=", "(?!", "(?<=", "(?<!"):
        if src.startswith(opener, i):
            return opener, None, i + len(opener)
    flagged = re.match(r"\(\?([a-zA-Z]*)(?:-([a-zA-Z]*))?([:)])", src[i:])
    if not flagged:
        raise UnsupportedRegex("unknown group syntax")
    on, off, end = flagged.group(1), flagged.group(2) or "", flagged.group(3)
    if not set(on + off) <= _FLAG_CHARS:
        raise UnsupportedRegex("unsupported inline flag")
    if end == ":":
        return flagged.group(0), None, i + flagged.end()
    return "", on + ("-" + off if off else ""), i + flagged.end()


def _apply_flags(active: dict[str, bool], spec: str) -> dict[str, bool]:
    on, _, off = spec.partition("-")
    merged = dict(active)
    for flag in on:
        merged[flag] = True
    for flag in off:
        merged[flag] = False
    return merged


def _wrap(segment: str, active: dict[str, bool]) -> str:
    if not segment or not active:
        return segment
    on = "".join(sorted(f for f, v in active.items() if v))
    off = "".join(sorted(f for f, v in active.items() if not v))
    return f"(?{on}{'-' + off if off else ''}:{segment})"


def _emit(items: list[Any]) -> str:
    """Render parsed items, turning ``(?flags)`` setters into scoped groups.

    In RE2 a bare ``(?i)`` applies from that point to the end of the enclosing
    group, INCLUDING later alternation branches: ``X(?i)Y|Z`` is ``XY|Z`` with
    the flag on ``Y`` and ``Z``. So each branch is cut at its setters, and the
    flags in force at the end of one branch open the next.
    """
    branches: list[str] = []
    active: dict[str, bool] = {}
    segment: list[str] = []
    rendered: list[str] = []
    for item in items + ["|"]:
        if item == "|":
            rendered.append(_wrap("".join(segment), active))
            branches.append("".join(rendered))
            segment, rendered = [], []
        elif isinstance(item, tuple) and item[0] == "flags":
            rendered.append(_wrap("".join(segment), active))
            segment = []
            active = _apply_flags(active, item[1])
        elif isinstance(item, tuple):
            segment.append(item[1] + _emit(item[2]) + ")")
        else:
            segment.append(item)
    return "|".join(branches)


def translate_go_regex(pattern: str, *, secret_group: int | None = None) -> str:
    """Translate a Go RE2 pattern to an equivalent Python ``re`` pattern.

    Handles mid-pattern flag setters (with RE2's alternation scope), ``\\z``,
    POSIX bracket classes, ``\\x{...}`` and ``(?<name>...)``. ``$`` is kept as
    is: Python's ``$`` also matches just before a final newline, which can only
    widen a match by that position and never moves the captured secret. Raises
    :class:`UnsupportedRegex` for anything else Python cannot express, and when
    the result does not compile cleanly under ``re.ASCII``.
    """
    # With *secret_group* (a capturing group's number) every unbounded repeat
    # outside that group is bounded at QUANTIFIER_MAX. Without it nothing is:
    # the whole match is the secret, or the pattern is an allow-list entry
    # that runs on the secret alone.
    if secret_group is None:
        items, _ = _parse_seq(pattern, 0, 0, in_capture=True)
    else:
        items, _ = _parse_seq(pattern, 0, 0, groups=[0, secret_group])
    translated = _emit(items)
    # ``.`` or a line break, written as two alternatives that both accept ``\r``,
    # is one class to RE2 but an ambiguous branch point to a backtracking engine:
    # a run of ``\r`` then explores every split. Spell it as the one class it is.
    for ambiguous in ("(?:.|[\\n\\r])", "(?:.|[\\r\\n])", "(?:.|\\n)", "(?:.|\\r)"):
        translated = translated.replace(ambiguous, "[\\s\\S]")
    # Two bounded repeats of the same class side by side (``X{20,1000}X{0,24}``)
    # are one repeat to RE2 but a split point at every length to a backtracking
    # engine. Spell them as the one repeat they are.
    while True:
        merged = _ADJACENT_REPEATS_RE.sub(
            lambda m: f"{m[1]}{{{int(m[2]) + int(m[4])},{int(m[3]) + int(m[5])}}}", translated
        )
        if merged == translated:
            break
        translated = merged
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        try:
            re.compile(translated, re.ASCII)
        except (re.error, FutureWarning, DeprecationWarning) as exc:
            raise UnsupportedRegex(f"does not compile: {exc}") from exc
    return translated


# ---------------------------------------------------------------------------
# Filter programs: the mechanical subset
# ---------------------------------------------------------------------------

_SECRET = r'finding\["secret"\]'
_ENTROPY = re.compile(rf"^(?:filter\.)?entropy\({_SECRET}\)\s*(<=|<)\s*([0-9]+(?:\.[0-9]+)?)$")
_TOKEN_RATIO = re.compile(rf"^(?:filter\.)?tokenRatio\({_SECRET}\)\s*(?:>=|>|<=|<)\s*[0-9.]+$")
_LIST_CALL = re.compile(
    rf"^(?:filter\.)?(matchesAny|containsAny)\({_SECRET}\s*,\s*\[(.*)\]\s*\)$", re.DOTALL
)


def _split_top(expr: str, sep: str) -> list[str]:
    """Split ``expr`` on ``sep`` outside brackets, parentheses and string literals."""
    parts: list[str] = []
    depth = 0
    quote = ""
    start = 0
    i = 0
    while i < len(expr):
        ch = expr[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "`\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and expr.startswith(sep, i):
            parts.append(expr[start:i])
            i += len(sep)
            start = i
            continue
        i += 1
    if quote or depth:
        raise UnsupportedFilter("unbalanced expression")
    parts.append(expr[start:])
    return parts


def _encloses(expr: str) -> bool:
    """True when ``expr`` opens with ``(`` whose matching ``)`` is the last character."""
    depth = 0
    quote = ""
    for index, ch in enumerate(expr):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "`\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return index == len(expr) - 1
    return False


def _strip_parens(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and _encloses(expr):
        expr = expr[1:-1].strip()
    return expr


def _parse_string_list(body: str) -> list[str]:
    values: list[str] = []
    for raw in _split_top(body, ","):
        item = raw.strip()
        if not item:
            continue
        if len(item) >= 2 and item[0] == item[-1] == "`":
            values.append(item[1:-1])
        elif len(item) >= 2 and item[0] == item[-1] == '"' and "\\" not in item:
            values.append(item[1:-1])
        else:
            raise UnsupportedFilter("list item is not a plain string literal")
    return values


def parse_filter(program: str) -> tuple[float | None, list[str]]:
    """Reduce a filter program to ``(entropy_min, allow_regexes)``.

    ``allow_regexes`` are still in Go syntax. The program must be an ``||`` of
    clauses this function recognises; anything else raises
    :class:`UnsupportedFilter`.
    """
    if "//" in program or re.search(r"\blet\b", program):
        raise UnsupportedFilter("multi-statement program")
    entropy_min: float | None = None
    allow: list[str] = []
    pending = [program]
    while pending:
        clause = _strip_parens(pending.pop(0))
        pieces = _split_top(clause, "||")
        if len(pieces) > 1:
            pending = pieces + pending
            continue
        if _TOKEN_RATIO.match(clause):
            continue
        entropy = _ENTROPY.match(clause)
        if entropy:
            value = float(entropy.group(2))
            entropy_min = value if entropy_min is None else max(entropy_min, value)
            continue
        listed = _LIST_CALL.match(clause)
        if listed:
            values = _parse_string_list(listed.group(2))
            if listed.group(1) == "containsAny":
                values = [re.escape(v) for v in values]
            allow.extend(values)
            continue
        raise UnsupportedFilter("clause outside the mechanical subset")
    return entropy_min, allow


_REGEX_META = frozenset("^$*+?()[]{}|\\")


def _is_credential_literal(value: str, rule_regex: re.Pattern[str]) -> bool:
    """True when an allow entry is a plain literal the rule itself would match."""
    return not (_REGEX_META & set(value)) and rule_regex.search(value) is not None


# ---------------------------------------------------------------------------
# Catalog build
# ---------------------------------------------------------------------------


def rule_tier(rule: dict[str, Any]) -> str:
    """Identifier for rules whose value names something; Secret otherwise."""
    rule_id = rule["id"]
    if rule_id in _SECRET_RULES:
        return TIER_SECRET
    if rule_id in _IDENTIFIER_RULES or _IDENTIFIER_ID.search(rule_id):
        return TIER_IDENTIFIER
    if rule.get("skipReport") is True and (
        _IDENTIFIER_PART.search(rule_id) or "public key" in rule.get("description", "").lower()
    ):
        return TIER_IDENTIFIER
    return TIER_SECRET


#: Named groups that hold the secret part of a composite match (a connection
#: string's password). Redacting only that part keeps the host and user readable.
_SECRET_GROUP_NAMES = ("password", "secret", "token", "key")


def _secret_group(rule: dict[str, Any], compiled: re.Pattern[str]) -> int:
    """Which group holds the secret.

    Upstream's ``secretGroup``, else a named secret group, else the only group.
    With several unnamed groups and no ``secretGroup`` the groups are structure
    (a repeated GUID part), not the secret, so ``-1``: the whole match.
    """
    if "secretGroup" in rule:
        return int(rule["secretGroup"])
    for name in _SECRET_GROUP_NAMES:
        if name in compiled.groupindex:
            return compiled.groupindex[name]
    if compiled.groups > 1 and _first_group_holds_the_rest(compiled):
        # ``((live|test)_[a-f0-9]{35})``: the outer capture is the credential and
        # the inner ones are its parts; the whole match would take the label and
        # the closing delimiter with it.
        return 1
    return 0 if compiled.groups <= 1 else -1


def _first_group_holds_the_rest(compiled: re.Pattern[str]) -> bool:
    """Whether capture 1 encloses every other capture of *compiled*."""
    from re import _constants as sre_c  # type: ignore[attr-defined]
    from re import _parser as sre_parse  # type: ignore[attr-defined]

    def groups_in(tree: Any) -> set[int]:
        found: set[int] = set()
        for op, av in tree:
            if op is sre_c.SUBPATTERN:
                if av[0]:
                    found.add(av[0])
                found |= groups_in(av[3])
            elif op in (sre_c.MAX_REPEAT, sre_c.MIN_REPEAT, sre_c.POSSESSIVE_REPEAT):
                found |= groups_in(av[2])
            elif op is sre_c.BRANCH:
                for branch in av[1]:
                    found |= groups_in(branch)
            elif op in (sre_c.ASSERT, sre_c.ASSERT_NOT):
                found |= groups_in(av[1])
            elif op is sre_c.ATOMIC_GROUP:
                found |= groups_in(av)
        return found

    def first(tree: Any) -> Any:
        for op, av in tree:
            if op is sre_c.SUBPATTERN:
                if av[0] == 1:
                    return av[3]
                inner = first(av[3])
                if inner is not None:
                    return inner
            elif op in (sre_c.MAX_REPEAT, sre_c.MIN_REPEAT, sre_c.POSSESSIVE_REPEAT):
                inner = first(av[2])
                if inner is not None:
                    return inner
            elif op is sre_c.BRANCH:
                for branch in av[1]:
                    inner = first(branch)
                    if inner is not None:
                        return inner
        return None

    body = first(sre_parse.parse(compiled.pattern, compiled.flags))
    return body is not None and groups_in(body) == set(range(2, compiled.groups + 1))


#: Inputs a rule must scan in bounded time: its keyword, then a long run of one
#: unit, for each character a pattern is likely to repeat and each pair of them
#: two overlapping repeats can split (``0-``: ``[a-z0-9-]+-`` against ``\d+-``).
_SCREEN_CHARS = ("\r", "\n", " ", "\t", "a", "A", "0", "-", "_", ".", "/", "=", ":", "'", '"', "%")
_SCREEN_UNITS = _SCREEN_CHARS + tuple(
    a + b for a in ("a", "A", "0") for b in ("-", "_", ".", "/", "=", ":", " ", "+")
)
#: Two lengths four times apart: a linear scan takes about four times as long on
#: the longer one, a quadratic one about 16 times. The shorter one is well past
#: the widest bounded repeat (``QUANTIFIER_MAX`` units, a 372-character token),
#: so a bounded repeat already scans linearly at both.
_SCREEN_LENGTHS = (2048, 8192)
#: How much slower than linear the longer input may be before the rule is
#: refused, with a floor so timer noise on fast scans never refuses one.
_SCREEN_GROWTH_MAX = 3.0
_SCREEN_NOISE_FLOOR = 0.002
#: Seconds per character the longer input may cost: a linear scan with a large
#: constant (backtracking a 1,000-wide repeat at every start) stalls the
#: gateway as surely as a quadratic one.
_SCREEN_PER_CHAR_MAX = 20e-6
#: Seconds one rule may spend on every screening input together.
_SCREEN_BUDGET = 4.0


def _scan_time(compiled: re.Pattern[str], text: str) -> float:
    started = time.perf_counter()
    for _ in compiled.finditer(text):
        pass
    return time.perf_counter() - started


def _screen_worker(regex: str, keywords: list[str], conn: Any) -> None:
    compiled = re.compile(regex, re.ASCII)
    short, long = _SCREEN_LENGTHS
    ratio = long / short
    for keyword in keywords or [""]:
        for unit in _SCREEN_UNITS:
            for build in (
                lambda n: keyword + unit * (n // len(unit)) + "end",
                lambda n: (keyword + unit) * (n // (len(keyword) + len(unit))),
            ):
                fast, slow = _scan_time(compiled, build(short)), _scan_time(compiled, build(long))
                text_len = len(build(long))
                if slow > text_len * _SCREEN_PER_CHAR_MAX or (
                    slow > _SCREEN_NOISE_FLOOR and slow > fast * ratio * _SCREEN_GROWTH_MAX
                ):
                    conn.send(False)
                    return
    conn.send(True)


def regex_is_bounded(regex: str, keywords: list[str]) -> bool:
    """Whether a backtracking engine scans the screening inputs within the budget.

    RE2 runs in linear time on any pattern; Python's ``re`` does not. A rule that
    a long run of one character can stall is not ported, because the redactor
    runs on the gateway's event loop.
    """
    import multiprocessing

    # ``spawn`` exists on every platform (Windows has no ``fork``); the worker
    # is a top-level function, so it pickles.
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_screen_worker, args=(regex, keywords, child))
    proc.start()
    ok = parent.poll(_SCREEN_BUDGET) and parent.recv() is True
    if proc.is_alive():
        proc.kill()
    proc.join()
    return ok


#: The fixed literal a token opens with (``\b(re_[...``): three or more
#: characters, letters, digits and ``_`` or ``-``, before any class or group.
_INTRINSIC_PREFIX_RE = re.compile(r"\A(?:\\b)?\(?([A-Za-z0-9_-]{3,})(?=[\[(\\])")


def _intrinsic_prefix(go_regex: str) -> set[str]:
    """The token's own prefix as a keyword, when its regex opens with one: a
    vendor token (``re_`` for Resend) is found without the vendor's name beside
    it, as the stream must once that name was committed in an earlier chunk."""
    m = _INTRINSIC_PREFIX_RE.match(go_regex)
    # Only a prefix no word spells (it holds ``_``, ``-`` or a digit): a bare
    # word (``pat``, ``incoming``) would set the stream's hold off in prose.
    if m is None or not any(c in "_-0123456789" for c in m.group(1)):
        return set()
    return {m.group(1).lower()}


def _port_rule(rule: dict[str, Any]) -> dict[str, Any]:
    unbounded = re.compile(translate_go_regex(rule["regex"]), re.ASCII)
    group = _secret_group(rule, unbounded)
    if group == 0 and unbounded.groups == 1:
        group = 1  # the only group holds the secret
    regex = translate_go_regex(rule["regex"], secret_group=group if group > 0 else None)
    compiled = re.compile(regex, re.ASCII)
    if not regex_is_bounded(regex, [k.lower() for k in rule.get("keywords", [])]):
        raise UnsupportedRegex("not linear-time under a backtracking engine")
    entropy_min: float | None = None
    allow_go: list[str] = []
    if rule.get("filter", "").strip():
        entropy_min, allow_go = parse_filter(rule["filter"])
    allow = [translate_go_regex(v) for v in allow_go if not _is_credential_literal(v, compiled)]
    return {
        "id": rule["id"],
        "tier": rule_tier(rule),
        "regex": regex,
        "secret_group": _secret_group(rule, compiled),
        "keywords": sorted(
            {k.lower() for k in rule.get("keywords", [])} | _intrinsic_prefix(rule["regex"])
        ),
        "entropy_min": entropy_min,
        "allow_regexes": allow,
        "stopwords": sorted({s.lower() for s in rule.get("stopwords", [])}),
    }


def build_catalog(upstream: Path) -> dict[str, Any]:
    config = tomllib.loads((upstream / UPSTREAM_CONFIG).read_text(encoding="utf-8"))
    commit = subprocess.run(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    ).stdout.strip()
    license_text = (upstream / "LICENSE").read_text(encoding="utf-8")
    rules: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for rule in config["rules"]:
        reason = None
        if rule["id"] in _HAND_WRITTEN:
            reason = "hand-written"
        elif "regex" not in rule:
            reason = "path-only" if "path" in rule else "no-regex"
        elif "path" in rule:
            reason = "path-scoped"
        else:
            try:
                rules.append(_port_rule(rule))
            except UnsupportedFilter:
                reason = "cel-filter"
            except UnsupportedRegex:
                reason = "regex-unsupported"
        if reason:
            skipped.append({"id": rule["id"], "reason": reason})
    global_entropy, global_allow = parse_filter(config.get("filter", ""))
    if global_entropy is not None:
        raise SystemExit("global filter gained an entropy clause; port it explicitly")
    return {
        "_generated_by": f"scripts/gen_secret_catalog.py ({COMMAND})",
        "_source": f"{UPSTREAM_LABEL} @ {commit}",
        "_license": license_text,
        "global_allow_regexes": [translate_go_regex(v) for v in global_allow],
        "skipped": skipped,
        "rules": rules,
    }


def render(catalog: dict[str, Any]) -> str:
    return json.dumps(catalog, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--upstream", required=True, type=Path, help="betterleaks checkout")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true", help="exit 1 if the file would change")
    args = parser.parse_args(argv)
    text = render(build_catalog(args.upstream))
    current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
    if args.check:
        if current != text:
            print(f"{args.output} is out of date; rerun without --check", file=sys.stderr)
            return 1
        return 0
    if current != text:
        args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
