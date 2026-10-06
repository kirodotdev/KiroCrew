"""Finite operator policy for monitor wall-clock budgets."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_RUNTIME_CEILING_SECS = 604_800
MAX_RUNTIME_CEILING_SECS = 2_592_000

_DURATION_UNITS: tuple[tuple[int, str], ...] = (
    (86_400, "day"),
    (3_600, "hour"),
    (60, "minute"),
)


def describe_duration(secs: int) -> str:
    """Human gloss for a whole-second bound: ``604800`` -> ``7 days``.

    Uses the largest unit that divides the value exactly, so the gloss never
    rounds a bound the reader will be held to; anything else stays in seconds.
    """
    for unit_secs, unit in _DURATION_UNITS:
        if secs >= unit_secs and secs % unit_secs == 0:
            count = secs // unit_secs
            return f"{count} {unit}" + ("" if count == 1 else "s")
    return f"{secs} second" + ("" if secs == 1 else "s")


def coerce_runtime_ceiling(value: object) -> int:
    """Malformed configuration falls back to the finite shipped policy.

    An unset key (``None``) is the ordinary default and is silent. A configured
    value this policy cannot honour is replaced by the shipped ceiling AND named
    in a warning, because the replacement is what every budget written from
    then on is validated against, and the operator who typed the value is the
    one who can correct it. Persisted budgets are not re-checked on load.
    """
    if type(value) is int and 1 <= value <= MAX_RUNTIME_CEILING_SECS:
        return value
    if value is not None:
        logger.warning(
            "monitoring.max_runtime_secs=%r is not an integer between 1 and %d; "
            "using the shipped ceiling of %d seconds (%s) instead",
            value,
            MAX_RUNTIME_CEILING_SECS,
            DEFAULT_RUNTIME_CEILING_SECS,
            describe_duration(DEFAULT_RUNTIME_CEILING_SECS),
        )
    return DEFAULT_RUNTIME_CEILING_SECS


def runtime_ceiling_secs() -> int:
    """Read the live policy without changing an existing monitor's deadline."""
    # Local to avoid a cycle: config.service_sections and config.section_builders
    # import this module.
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.config.live import snapshot

    config = snapshot()
    if config is None:
        config = KiroCrewConfig.load()
    return coerce_runtime_ceiling(getattr(config.monitoring, "max_runtime_secs", None))


def validate_runtime_secs(value: object, *, allow_unbounded: bool = False) -> int:
    """Validate a budget as a tool or API call writes it, with one set of integer bounds.

    Budgets already persisted are not re-checked on load.

    Zero retains its legacy meaning only on general AutoNudge surfaces. Monitor
    tools and structured monitors always require a positive finite budget.

    A whole-number float (``3600.0``, as a JSON body may spell an integer) is
    accepted and returned as ``int``; a bool, a fractional float and a
    non-finite float are refused.
    """
    ceiling = runtime_ceiling_secs()
    minimum = 0 if allow_unbounded else 1
    if type(value) is float and value.is_integer():
        value = int(value)
    if type(value) is not int or not minimum <= value <= ceiling:
        raise ValueError(
            f"max_runtime_secs must be an integer between {minimum} and {ceiling} "
            f"({describe_duration(ceiling)})"
        )
    return value
