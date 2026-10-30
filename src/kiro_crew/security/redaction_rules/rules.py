"""Load and validate the credential rule table.

``rules.yaml`` beside this module is the only place a rule is defined. It is read
once through ``importlib.resources`` and every row is checked before any text is
scanned, so a malformed row stops the load instead of silently never firing.
Unknown fields, tiers, detectors and validators are refused rather than
ignored, because an ignored field is a rule that does less than its author
meant.
"""

from __future__ import annotations

import functools
import logging
import re
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Any

import yaml  # type: ignore[import-untyped]

from kiro_crew.security.redaction_rules.model import (
    NAME_DETECTORS,
    PATTERN_DETECTORS,
    Detector,
    Tier,
)

logger = logging.getLogger(__name__)

_TABLE_FILE = "rules.yaml"
_TABLE_VERSION = 1

#: Longest value a name-keyed detector reads, and the largest repeat count a
#: rule pattern may use. Every pattern is bounded so a scan stays linear in the
#: text length times a constant; a PEM or PGP block needs the larger bound.
MAX_VALUE_LEN = 2000
_MAX_REPEAT = 20_000
_MAX_PAIR_WINDOW = 2000

_TABLE_FIELDS = frozenset({"version", "allow", "label_exclude", "rules"})
_ROW_FIELDS = frozenset(
    {
        "id",
        "tier",
        "detector",
        "pattern",
        "labels",
        "pair_with",
        "validate",
        "replace",
        "allow",
        "corpus",
    }
)
_ALLOW_FIELDS = frozenset({"values", "suffixes", "patterns"})
_PAIR_FIELDS = frozenset({"pattern", "window"})
_REPLACE_MODES = frozenset({"value", "match"})
_RULE_ID_RE = re.compile(r"[a-z0-9]+(?:[._][a-z0-9]+)*")
_VALUE_GROUP = "v"


class RuleTableError(ValueError):
    """A rule table that does not pass validation."""


def normalise_name(name: str) -> str:
    """Field-name form labels are compared in: lowercase, no ``-``, ``_``, ``.`` or spaces."""
    return re.sub(r"[-_. ]", "", name.lower())


# ── Validators ──

_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_GITHUB_BODY_LEN = 30
_GITHUB_CHECK_LEN = 6
_GITHUB_PREFIX_LEN = 4


def base62_crc32(body: str) -> str:
    """The six-character base62 CRC32 a GitHub token appends to its body."""
    crc = zlib.crc32(body.encode("ascii"))
    digits = ""
    while crc:
        crc, rem = divmod(crc, 62)
        digits = _BASE62[rem] + digits
    return digits.rjust(_GITHUB_CHECK_LEN, "0")


def _github_crc32(value: str) -> bool:
    body = value[_GITHUB_PREFIX_LEN : _GITHUB_PREFIX_LEN + _GITHUB_BODY_LEN]
    check = value[_GITHUB_PREFIX_LEN + _GITHUB_BODY_LEN :]
    if len(body) != _GITHUB_BODY_LEN or len(check) != _GITHUB_CHECK_LEN:
        return False
    return base62_crc32(body) == check


def _aws_secret_shape(value: str) -> bool:
    """Rejects hex digests and single-class runs a key ID can sit beside."""
    if re.fullmatch(r"[0-9a-fA-F]+", value):
        return False
    classes = (
        any(c.islower() for c in value),
        any(c.isupper() for c in value),
        any(c.isdigit() for c in value),
    )
    return sum(classes) >= 2


VALIDATORS: Mapping[str, Callable[[str], bool]] = {
    "github_crc32": _github_crc32,
    "aws_secret_shape": _aws_secret_shape,
}


# ── Table model ──


@dataclass(frozen=True)
class Allow:
    """Values that look like a match but are not secrets."""

    values: frozenset[str] = frozenset()
    suffixes: tuple[str, ...] = ()
    patterns: tuple[re.Pattern[str], ...] = ()

    def permits(self, value: str) -> bool:
        if value.lower() in self.values:
            return True
        if any(value.endswith(s) for s in self.suffixes):
            return True
        return any(p.fullmatch(value) for p in self.patterns)


@dataclass(frozen=True)
class PairWith:
    pattern: re.Pattern[str]
    window: int


@dataclass(frozen=True)
class Rule:
    id: str
    tier: Tier
    detector: Detector
    pattern: re.Pattern[str] | None
    labels: frozenset[str]
    pair_with: PairWith | None
    validate: Callable[[str], bool] | None
    replace: str
    allow: Allow
    corpus: str


@dataclass(frozen=True)
class RuleTable:
    rules: tuple[Rule, ...]
    allow: Allow
    label_exclude: frozenset[str]

    def rule(self, rule_id: str) -> Rule:
        for row in self.rules:
            if row.id == rule_id:
                return row
        raise KeyError(rule_id)


# ── Validation ──


def _fail(where: str, why: str) -> RuleTableError:
    return RuleTableError(f"{where}: {why}")


def _check_fields(where: str, data: Any, allowed: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise _fail(where, "must be a mapping")
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise _fail(where, f"unknown field(s) {', '.join(map(str, unknown))}")
    return data


def _string_list(where: str, data: Any) -> list[str]:
    if not isinstance(data, list) or not all(isinstance(s, str) and s for s in data):
        raise _fail(where, "must be a list of non-empty strings")
    return list(data)


def _unbounded_repeat(pattern: str) -> str | None:
    """The first repeat in ``pattern`` that has no upper bound or exceeds the cap."""
    i, n, in_class = 0, len(pattern), False
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
            if pattern[i + 1 : i + 2] == "]":
                i += 1
        elif c in "*+" and not (i and pattern[i - 1] in "*+?}"):
            return c
        elif c == "{":
            m = re.match(r"\{(\d*)(,?)(\d*)\}", pattern[i:])
            if m:
                if m.group(2) and not m.group(3):
                    return m.group(0)
                if int(m.group(3) or m.group(1) or 0) > _MAX_REPEAT:
                    return m.group(0)
                i += len(m.group(0))
                continue
        i += 1
    return None


def _compile(where: str, pattern: Any) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern:
        raise _fail(where, "pattern must be a non-empty string")
    loose = _unbounded_repeat(pattern)
    if loose:
        raise _fail(where, f"pattern repeat {loose!r} is unbounded or over {_MAX_REPEAT}")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise _fail(where, f"pattern does not compile ({exc})") from None


def _build_allow(where: str, data: Any) -> Allow:
    if data is None:
        return Allow()
    data = _check_fields(where, data, _ALLOW_FIELDS)
    values = _string_list(f"{where}.values", data.get("values", []))
    suffixes = _string_list(f"{where}.suffixes", data.get("suffixes", []))
    patterns = _string_list(f"{where}.patterns", data.get("patterns", []))
    return Allow(
        values=frozenset(v.lower() for v in values),
        suffixes=tuple(suffixes),
        patterns=tuple(_compile(f"{where}.patterns", p) for p in patterns),
    )


def _build_pair(where: str, data: Any) -> PairWith:
    data = _check_fields(where, data, _PAIR_FIELDS)
    window = data.get("window")
    if not isinstance(window, int) or isinstance(window, bool):
        raise _fail(where, "window must be an integer")
    if not 0 < window <= _MAX_PAIR_WINDOW:
        raise _fail(where, f"window must be 1..{_MAX_PAIR_WINDOW}")
    return PairWith(pattern=_compile(where, data.get("pattern")), window=window)


def _enum(where: str, kind: type[Tier] | type[Detector], value: Any) -> Any:
    try:
        return kind(value)
    except ValueError:
        raise _fail(where, f"unknown {kind.__name__.lower()} {value!r}") from None


def _build_rule(index: int, data: Any) -> Rule:
    where = f"rules[{index}]"
    data = _check_fields(where, data, _ROW_FIELDS)
    rule_id = data.get("id")
    if not isinstance(rule_id, str) or not _RULE_ID_RE.fullmatch(rule_id):
        raise _fail(where, f"id {rule_id!r} is missing or malformed")
    where = f"rule {rule_id}"
    tier = _enum(where, Tier, data.get("tier"))
    detector = _enum(where, Detector, data.get("detector"))

    corpus = data.get("corpus")
    if corpus != f"rules/{rule_id}":
        raise _fail(where, f"corpus must be 'rules/{rule_id}'")

    replace = data.get("replace", "value")
    if replace not in _REPLACE_MODES:
        raise _fail(where, f"replace must be one of {sorted(_REPLACE_MODES)}")

    pattern = None
    labels: frozenset[str] = frozenset()
    validate = None
    if detector in PATTERN_DETECTORS:
        if "labels" in data:
            raise _fail(where, f"labels do not apply to detector {detector.value}")
        pattern = _compile(where, data.get("pattern"))
        if replace == "value" and _VALUE_GROUP not in pattern.groupindex:
            raise _fail(where, "replace: value needs a named group 'v' in the pattern")
        name = data.get("validate")
        if name is not None:
            if name not in VALIDATORS:
                raise _fail(where, f"unknown validator {name!r}")
            validate = VALIDATORS[name]
    elif detector in NAME_DETECTORS:
        for field in ("pattern", "validate"):
            if field in data:
                raise _fail(where, f"{field} does not apply to detector {detector.value}")
        if replace != "value":
            raise _fail(where, f"detector {detector.value} replaces the value only")
        if "labels" not in data:
            raise _fail(where, "labels are required")
        labels = frozenset(
            normalise_name(s) for s in _string_list(f"{where}.labels", data["labels"])
        )

    pair = None
    if detector is Detector.KEY_ID_PAIR:
        if "pair_with" not in data:
            raise _fail(where, "key_id_pair needs pair_with")
        pair = _build_pair(f"{where}.pair_with", data["pair_with"])
    elif "pair_with" in data:
        raise _fail(where, f"pair_with does not apply to detector {detector.value}")

    return Rule(
        id=rule_id,
        tier=tier,
        detector=detector,
        pattern=pattern,
        labels=labels,
        pair_with=pair,
        validate=validate,
        replace=replace,
        allow=_build_allow(f"{where}.allow", data.get("allow")),
        corpus=corpus,
    )


def build_table(data: Any) -> RuleTable:
    """Validate a parsed rule table and return it, or raise :class:`RuleTableError`."""
    data = _check_fields("table", data, _TABLE_FIELDS)
    if data.get("version") != _TABLE_VERSION:
        raise _fail("table", f"version must be {_TABLE_VERSION}")
    rows = data.get("rules")
    if not isinstance(rows, list) or not rows:
        raise _fail("table", "rules must be a non-empty list")
    rules = tuple(_build_rule(i, row) for i, row in enumerate(rows))
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise _fail(f"rule {rule.id}", "duplicate id")
        seen.add(rule.id)
    exclude = _string_list("table.label_exclude", data.get("label_exclude", []))
    return RuleTable(
        rules=rules,
        allow=_build_allow("table.allow", data.get("allow")),
        label_exclude=frozenset(normalise_name(s) for s in exclude),
    )


def table_from_yaml(text: str) -> RuleTable:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuleTableError(f"table: not valid YAML ({exc})") from None
    return build_table(data)


@functools.lru_cache(maxsize=1)
def load_table() -> RuleTable:
    """The packaged rule table, read and validated once per process."""
    text = resources.files(__package__).joinpath(_TABLE_FILE).read_text(encoding="utf-8")
    return table_from_yaml(text)
