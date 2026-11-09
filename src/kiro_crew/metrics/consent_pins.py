"""Who decides whether metrics are recorded: the env-var pins.

Split out of :mod:`kiro_crew.metrics.provider`, which re-exports every name here
and stays the import path callers (and test patches) use. ``KIROCREW_TELEMETRY``
and ``DO_NOT_TRACK`` pin local recording.
"""

from __future__ import annotations

import os
from typing import Optional

from kiro_crew import beacon

# Env-var opt-in. ``KIROCREW_TELEMETRY`` lets a host turn
# LOCAL metrics on (or force them off) without editing ~/.kiro/crew/config.json —
# handy for CI, containers, and one-off debugging. Truthy => enable, falsy =>
# disable, unset/blank => defer to the ``telemetry.enabled`` config flag (itself
# default False). This gates LOCAL collection ONLY: external OTLP egress needs the
# active telemetry provider to name a destination (the default provider does so
# only for a non-empty ``telemetry.otlp_endpoint``), so merely flipping this var
# never causes data to leave the host (egress stays off by default).
# Public: a UI control over ``telemetry.enabled`` names this variable when it
# reports the setting as pinned, and naming it from here keeps the message and the
# lookup from drifting apart.
TELEMETRY_ENV_VAR = "KIROCREW_TELEMETRY"
_ENV_TRUTHY = frozenset({"1", "true", "yes", "on"})
_ENV_FALSY = frozenset({"0", "false", "no", "off"})


def env_pin() -> Optional[bool]:
    """Return the ``KIROCREW_TELEMETRY`` pin, or ``None`` when the var is unset.

    Public because a UI control over ``telemetry.enabled`` has to know the config
    flag is not the last word: a pinned host would otherwise offer a switch whose
    write can never change what is collected. Resolving the pin here (rather than
    re-reading the env var at the call site) keeps the control and the collector
    from disagreeing about what "on" means.
    """
    # DO_NOT_TRACK first: off wins, so a truthy DO_NOT_TRACK pins recording off
    # even when KIROCREW_TELEMETRY asks for it.
    if beacon.do_not_track():
        return False
    raw = os.environ.get(TELEMETRY_ENV_VAR, "").strip().lower()
    if raw in _ENV_TRUTHY:
        return True
    if raw in _ENV_FALSY:
        return False
    return None


def pin_env_var() -> str:
    """Name the env var behind :func:`env_pin`, for a UI note that says which to unset."""
    return beacon.DO_NOT_TRACK_ENV if beacon.do_not_track() else TELEMETRY_ENV_VAR
