"""Field metadata and value coercion shared by every config section owner.

``_meta`` builds the metadata a section field carries into the schema and the
config surfaces, the ``_safe_*`` readers turn a raw ``config.json`` value into
the field's type or its default without raising, and ``field_default`` reads the
default a DTO field declares. This is a leaf module: it imports nothing from
``kiro_crew``, so each section owner depends on it without a cycle, and
``config.sections`` re-exports every name.
"""

from __future__ import annotations

import functools
import math
import re as _re
from collections.abc import Mapping
from dataclasses import MISSING, Field
from dataclasses import fields as _dataclass_fields
from typing import Any


@functools.cache
def _declared_fields(dto: type) -> Mapping[str, Field[Any]]:
    """*dto*'s dataclass fields by name, computed once per DTO class."""
    return {f.name: f for f in _dataclass_fields(dto)}


def field_default(dto: type, name: str) -> Any:
    """The default declared on dataclass *dto*'s field *name*, a fresh one for a factory.

    The one implementation of that rule: ``sections.SectionReader`` reads its defaults
    through it, and so does a normalizer or single-value reader with no section
    mapping to wrap (a ``__post_init__``, a value coercer, a reader the gateway
    shares), in ``config.sections`` and in the section owners, which sit under that
    facade and never import it. A name with no field, or a field with no default,
    raises ``KeyError``.
    """
    spec = _declared_fields(dto)[name]
    if spec.default is not MISSING:
        return spec.default
    if spec.default_factory is not MISSING:
        return spec.default_factory()
    raise KeyError(name)


def _safe_int(value: object, default: int, lo: int | None = None, hi: int | None = None) -> int:
    """Convert a legacy numeric config value or return *default* on failure.

    Existing config files may contain numeric strings or integral floats from
    older writers. Preserve that compatibility while rejecting booleans.

    *lo*/*hi* clamp the result, mirroring :func:`_safe_float`. Pass them for any
    bounded knob: ``_clamp_security_bounds`` runs over the raw dict and skips
    non-int values, so a numeric STRING (``"1"``) slips past it and then
    coerces here — clamping at the coercion site is what actually enforces the
    declared range.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, float) and not value.is_integer():
        return default
    try:
        result = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        result = default
    if lo is not None:
        result = max(lo, result)
    if hi is not None:
        result = min(hi, result)
    return result


def _safe_nonnegative_int(value: object, default: int, hi: int | None = None) -> int:
    """Convert a legacy integer value and reject negative results.

    *hi* caps the result. Deliberately a ceiling only, with no matching floor
    argument: a negative value still returns *default* rather than clamping up to
    0, because 0 is MEANINGFUL for the budgets this guards (a zero chunk budget
    turns that sweep off). Clamping -1 to 0 would silently disable a sweep the
    operator never asked to disable, where returning the default keeps it running.
    The ceiling has no such ambiguity, and it is where the exposure was: an absurd
    hand-edited budget loaded verbatim and became real scheduled work.
    """
    result = _safe_int(value, default)
    if result < 0:
        return default
    return result if hi is None else min(hi, result)


def _port_or_unset(value: object) -> int:
    """A TCP port, or 0 (unset) when the value is malformed or out of range.

    Deliberately NOT the clamp convention used for bounded knobs: a clamped
    port is as wrong as a malformed one — a tunnel that forwards 8080 does not
    forward 65535 either — so anything outside 1..65535 falls back to unset
    (ephemeral) rather than becoming a live pin the operator never named.
    """
    result = _safe_int(value, 0)
    return result if 0 < result <= 65535 else 0


def _safe_bool(value: object, default: bool) -> bool:
    """Return *value* only when it is a real bool, else *default*."""
    return value if isinstance(value, bool) else default


def _coerce_bool(value: object, default: bool) -> bool:
    """As :func:`_safe_bool`, but reading the spellings a hand edit produces.

    ``config.json`` is hand-editable and ``bool("false")`` is ``True``, so a
    field whose two wrong answers are not symmetric cannot fold every non-bool
    to its own default: ``"false"`` has to mean False. A real bool is returned
    as-is; ``true``/``false``/``1``/``0``/``yes``/``no``/``on``/``off``
    (case-insensitive) map to their value; anything else falls back to *default*,
    which the caller picks to fail safe. The one copy: ``hooks`` re-exports it
    for the opt-out flags, which it protects for the same reason.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        spelling = value.strip().lower()
        if spelling in ("true", "1", "yes", "on"):
            return True
        if spelling in ("false", "0", "no", "off"):
            return False
    return default


def _safe_list(value: object) -> list:
    """Return *value* if it is a list, else []. Guards list()/comprehensions in
    config parse against a malformed (non-list) config value that would either
    crash (int/None) or silently mis-coerce (a string char-splits) — config
    load must degrade to the default, never raise."""
    return value if isinstance(value, list) else []


def _safe_dict(value: object) -> dict:
    """Return *value* if it is a dict, else {}. Guards .items()/dict() in config
    parse against a non-dict config value (which would raise AttributeError)."""
    return value if isinstance(value, dict) else {}


def _safe_float(
    value: object,
    default: float,
    lo: float | None = None,
    hi: float | None = None,
) -> float:
    """Return a real JSON number or *default*, clamped to [lo, hi].

    Non-finite results (NaN/Infinity) are replaced with *default* — NaN compares
    false against any bound so it would silently bypass clamping (e.g. a
    configured ``tips_cadence_hours: NaN`` would permanently suppress tips).
    """
    # Keep compatibility with config files written by older CLI versions while
    # excluding booleans, which Python otherwise treats as numeric values.
    if isinstance(value, bool):
        return default
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        # OverflowError: json parses arbitrarily large ints fine, but float()
        # on a several-hundred-digit int raises — must not crash config load.
        result = default
    if not math.isfinite(result):
        result = default
    if lo is not None and result < lo:
        result = lo
    if hi is not None and result > hi:
        result = hi
    return result


_COLOR_HEX_RE = _re.compile(r"^#[0-9a-fA-F]{6}$")


def _safe_color(value: object) -> str:
    """Return a valid lowercase ``#rrggbb`` hex color, or ``""`` on junk.

    config.json is hand-editable, so a non-string or malformed value must
    collapse to empty (no agent color) rather than crash the load or propagate
    to an inline CSS style attribute.
    """
    if not isinstance(value, str) or not value:
        return ""
    v = value.strip().lower()
    if _COLOR_HEX_RE.match(v):
        return v
    return ""


def _meta(label: str, help: str, **kwargs: object) -> dict:
    """Helper to build field metadata dicts with safe defaults."""
    return {"label": label, "help": help, **kwargs}


def _coerce_int(raw: object, default: int) -> int:
    """Return ``int(raw)`` or *default* if *raw* isn't a clean base-10 integer.

    Fail closed against a hand-edited non-numeric config value (e.g. ``"abc"``)
    that would otherwise raise in ``int()`` and crash config load.
    """
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default
